# 论文阅读 Read 通用 Normalize 层说明

时间：2026-03-13 23:55

## 背景

当前 `/read` 的 `layout_uid_v1` 已经把 DocMind `uniqueId` 作为最小阅读单元，并且：

- 几何与高光真值继续来自 DocMind
- `/read` 默认走 `layout_uid_v1`
- 表格和公式已经开始分流：
  - table: table logical rows
  - equation: image-first + AI normalization

但普通 `prose / layout text` 仍主要依赖：

- `raw_text`
- `clean_text`

这会留下两类体验问题：

1. OCR / layout 噪声直接出现在正文
   - 上标被识别成普通数字
   - 连字、断词、标号、脚注残留
   - 标题/列表项粘连

2. `/experience` 后续拿到的 `/read` grounding 还不够干净
   - 缺一层统一的展示文本 normalize

## 目标

为 `/read` 增加一层通用 `Normalize`：

- 在不改变证据真值的前提下，让 AI 参与展示层文本修正
- 保留原始文本和证据
- 为 `/experience` 提供更干净的 grounding 输入
- 在 `AI 上下文` 中明确展示哪些 layout 被修正、为什么被修正
- 对于被隐藏的 `header / footer / doi / metadata`，normalize 结果也应留在 `AI 上下文` 里，便于复核

## 非目标

本轮不做：

- 不修改 `uniqueId`
- 不修改 geometry / evidence ownership
- 不修改高光来源
- 不翻译文本
- 不让 AI 自由重写段落含义
- 不让 AI 接管表格几何或公式证据
- 不把已经被 omit 的 `header / footer / doi / metadata` 重新塞回正文主阅读流

## 设计

### 1. 数据层

在 `page_grounding_v1` 上新增 normalize 字段：

- `layout_atoms[*].normalized_text`
- `layout_atoms[*].normalization_reason`
- `layout_atoms[*].normalization_confidence`
- `layout_atoms[*].normalization_mode`

并同步到：

- `reading_nodes[*].normalized_text`
- `reading_nodes[*].normalization_reason`
- `reading_nodes[*].normalization_confidence`
- `reading_nodes[*].normalization_mode`

原始字段继续保留：

- `raw_text`
- `clean_text`

### 2. AI 输入

仅对下列 `node_kind` 运行通用 normalize：

- `title`
- `section_heading`
- `paragraph`
- `list`
- `figure_caption`
- `table_caption`
- `metadata`
- `doi`
- `header`
- `footer`

不进入这轮通用 normalize 的：

- `table`
- `equation`
- `noise`

输入内容：

- 当前页渲染图
- `layout_id`
- `node_kind`
- `raw_text / clean_text`
- `alignment`
- `line_height`
- `blocks[*].style_id`
- `blocks[*].text`

### 3. AI 输出

AI 只能输出：

- `layout_id`
- `normalized_text`
- `reason`
- `confidence`
- `mode`

约束：

- 必须对输入的候选 `layout_id` 做 exact-once 返回
- 可以返回 “no_change”
- 不允许改 source ids / geometry / ownership

### 4. 应用方式

- `/read` 正文物化时优先使用 `normalized_text`
- grouping prompt 也优先使用 `normalized_text`
- evidence / preview / quote 继续保留原始文本链路
- 对 `^数字` 这类 normalize 标记，只允许正文展示层把它解释成视觉样式，例如真正的上标；不能把这类展示层格式化反写到 geometry、ownership 或 evidence preview 主链。

### 5. AI 上下文

右栏 `AI 上下文` 增加 normalize 变更摘要：

- 哪个 `layout_id` 被改动
- 原文本 -> normalized_text
- `reason`
- `confidence`

`Intentional Omissions` 也要做二次整理：

- 不再只显示重复的 `hide / recoverable / footer`
- 按 omission `reason` 分组
- 每组展示被隐藏 layout 的可读文本预览
- 若该 layout 存在 `normalized_text`，同时展示 `source -> normalized`
- `footer` 应单独成栏展示，不与通用 omission 混在一起；因为这类内容经常承载链接、脚注编号和参考来源，用户需要快速阅读和复核

### 6. 公式

公式继续走现有：

- image-first
- 独立 equation normalization

本轮不把公式混进通用 prose normalize，但 AI 上下文应同时展示公式 normalize 痕迹。

## 回退

如果本轮导致 `/read` 正文或 grouping 出现回归：

1. 回退本轮提交
2. 恢复仅使用 `clean_text / raw_text`
3. 保留文档和测试记录，用于后续小范围重试

## 运行时注意

- normalize 这层依赖当前页渲染图作为视觉参考。
- live 请求里必须优先使用本地 render asset (`file://...`) 送图。
- 如果多模态本地送图失败，不应再把 `localhost / 内网 asset URL` 退回给兼容接口作为 `image_url`，否则会出现：
  - `BadRequestError: The provided URL does not appear to be valid`
  - 最终 normalize 完全不产出变更，页面继续显示 `llama.cpp6 / API8 / API 9in` 这类脏文本。
- `page_grounding_v1` 在补 `page_image.width / height` 时，也必须沿用同一份 `page_render_asset.url / path`。
- 不允许在已经拿到本地 render asset 之后，又回头读取 `docmind_page_image_url / docmind_page_image_path`；否则会在 page 8 这类临时 URL 页面上白白触发一次 `403 Forbidden`，并干扰后续 normalize 诊断。
- `/read` 必须严格区分两条图链：
  - `AI prompt 图` 可以使用 `page_render_asset`
  - `page_grounding_v1.page_image` 必须继续代表 evidence/highlight 的坐标基准图
- 不允许在 `layout_uid_v1` 主流程里把 prompt 用的 `page_render_asset` 再写回 `page_grounding_v1.page_image`；这会把 evidence 坐标基准从本地化 DocMind 页图错误切回 `1360x1760` 的 render asset，重新造成统一右偏。
- 如果没有 `page_render_asset`，但确实需要使用 DocMind 整页图，则必须先把 DocMind 临时图持久化到本地，再回填：
  - `page_grounding_v1.page_image.path`
  - 一个指向本地缓存资产的 API `url`
  - 明确的 `source` 标记，例如 `docmind_page_image_localized`
  - 以及保留原始来源的 `origin_url`
- 一旦本地持久化完成，`page_grounding_v1` 和后续 `/read` 链路都不应继续直接暴露或依赖临时 DocMind URL。
- 对 evidence/highlight 来说，`page_grounding_v1.page_image` 必须优先代表 **DocMind 本地化页图**，而不是 `page_render_asset`。
- `page_render_asset` 可以继续作为 AI prompt 图，但 evidence 几何使用的 `uniqueId -> blocks[].pos` 与 `page_image.width/height` 必须来自同一套 DocMind 坐标系；否则会重新出现统一右偏。
- `layout_uid_text_normalization` 产生的 enrichments 必须在最终 `_ensure_payload_contract(...)` 后继续保留。
- 不允许在 compose 主流程里先把 `normalized_text / normalization_summary` 写回 `page_grounding_v1`，又在 contract repair 阶段用一份重新构建的 grounding 把这些字段冲掉；否则会出现“decision_log 显示 text_normalized=11，但落库的 grounding 仍全为空”的假成功状态。
- 对已经生成但 grounding 未同步的缓存页，contract repair 还需要能从 `layout_advice_v3.text_normalizations.normalization_plan` 回填 `page_grounding_v1`。
- 这样旧缓存页在读取时也能恢复 normalize 结果，不必强依赖再次 fresh compose 才能看到 `normalized_text`。
- 对于只剩最终 payload、但不再携带完整 `docmind_structure / page_structure_v3` 的缓存读路径，contract repair 不能把现有 `page_grounding_v1` 冲成空对象。
- 这类缓存读路径必须保留现有的 `page_image / layout_atoms / evidence_map / reading_nodes`，再基于当前 grounding 刷新 `layout_uid_v1` anchors；否则会出现正文已显示 `llama.cpp^6`，但右侧 evidence 仍然使用旧的 `llama.cpp6` 文本和旧页面尺寸，继续整体右偏。
- 读缓存时如果 contract repair 实际修正了 payload，修正结果必须回写到缓存层：
  - DB `paper_reader_page_caches.payload_json`
  - Redis exact cache
- 否则同一页会每次读取都重复修，且某些绕过 repair 的链路仍会继续拿到旧的 `1232x1843` anchor 几何。
- 对于已经产生 `normalized_text=llama.cpp^6` 这类结果的页面，证据链仍保持纯文本与 DocMind 几何真值：
  - anchor `quote_text` 可以继续保存 `^6`
  - 只有 `/read` 主阅读区的 React 渲染层把它转成 `<sup>`
  - evidence preview、quote 对比和缓存修复都不能依赖富文本格式化
- `footer` 链接 normalize 还需要一层本地 fallback：
  - 当 AI 已经成功修正前几个脚注链接，例如 `^6`、`^7`
  - 但后续同组 footer URL 仍然保留 `Shttps://...`、`Yhttps://...` 这类 OCR 噪声时，
  - 可以基于 `reading_order` 顺延 marker，并仅对 URL 本身做 canonical cleanup，
  - 生成 `^8 https://...`、`^9 https://...`
  - 同时明确标记：
    - `normalization_mode = footer_link_fallback`
    - `normalization_reason = footer_link_cleanup`
  - 这层 fallback 只允许修正文案，不允许改 geometry、layout_id 或 evidence ownership。
- 后续主方向不是继续扩这层 fallback，而是给 normalize prompt 直接补 `footer_bundles` 上下文：
  - 把连续的 `footer_note / corner_note / footnote` 收成 bundle
  - 一并提供：
    - `layout_id`
    - `reading_order`
    - `source_text`
    - `style_id`
    - `is_marker_only`
    - `contains_url`
  - 让 AI 在同一 bundle 里自行判断：
    - 脚注编号与 URL 的对应关系
    - URL OCR 噪声的修复
    - 哪些项该保留、拼接、或只作为辅助上下文
  - 本地 fallback 只做兜底，不再继续变成主要方案。
- 为了尽量少动 `/read` 侧栏结构，footer 的下一步不再新写前端拼接逻辑，而是给 AI 更大的 bundle 级修复自由：
  - prompt 允许 AI 在同一 `footer_bundle` 内决定 `keep / rewrite / hide_fragment`
  - 当某个碎片只是页码、孤立 marker、重复 URL 噪声时，可以返回：
    - `normalized_text = ""`
    - `mode = footer_hide_fragment`
  - 前端 `Footer / Links` 仅跳过这些被 AI 标成 `footer_hide_fragment` 的碎片，其余仍沿用现有 omission 视图。
  - 这条改动只影响 footer rail，不影响正文、不影响 evidence 主链。
# 运行时页图链路收敛补充

- `/read` 的 prompt 图与 grounding 图已彻底分离：
  - prompt 图只允许使用本地 `page_render_asset` 或已本地化的页图文件，通过 DashScope `file://...` 输入。
  - grounding 图只允许使用本地化 DocMind 图，写入 `page_grounding_v1.page_image` 的 `url` 只能是 reader 本地资产路由；`origin_url` 仅保留为调试元数据。
- `page_grounding_v1.page_image.url` 不再回退为临时 DocMind URL；本地化失败时会留空，并优先根据 grounding geometry 推断尺寸。
- `_invoke_single_agent_model(...)` 不再把远端 `image_url` 作为多模态兜底；本地文件不可用时退回 text-only，而不是重新暴露临时 URL。
- 前端 `PaperReaderPage.tsx` 不再从 `docmind_structure.page_image_url` 为 `asset:` 和 DOI 回退取图，只允许使用本地化 grounding asset。
- `/api/v1/literature/reader/docmind-page-image/{paper_id}/{page}` 不再直接代理远端 DocMind URL；若无法先本地化，则返回 `404`。
- 结构缓存层也必须尽早持久化 DocMind 页图合同：
  - `docmind_structure.page_image_width / page_image_height` 优先来自 `doc_info.pages[*].imageWidth / imageHeight`
  - fresh `/read` payload` 构建时，应尽量把 `page_image_url` 本地化到 `grounding_pages/page_<n>.*`
  - 即使临时 URL 已过期，旧缓存页至少也要先回填真实 `1483 x 1920` 这类 DocMind 整页尺寸，不能继续退回内容边界推断值
