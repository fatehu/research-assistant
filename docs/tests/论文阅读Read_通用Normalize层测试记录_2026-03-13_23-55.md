# 论文阅读 Read 通用 Normalize 层测试记录

时间：2026-03-13 23:55

## 目标

验证 `/read` 通用 normalize 层满足：

- 只改展示文本，不改证据真值
- normalize 结果能进入 `page_grounding_v1`
- grouping 和正文物化会优先使用 `normalized_text`
- `AI 上下文` 能展示变更摘要
- 公式链保持 image-first，不影响全局 evidence preview

## 计划中的验证项

### Backend

1. `layout_atoms / reading_nodes` schema 支持 normalize 字段
2. text normalization prompt payload 只发送允许的 `node_kind`
3. AI 输出必须 exact-once 覆盖候选 `layout_id`
4. grounding 应用 normalize 后：
   - 原 `raw_text / clean_text` 保留
   - `normalized_text` 正确写回
5. `layout_uid` grouping prompt 优先用 `normalized_text`
6. `panel_plan -> ui_plan` 的 `ParagraphProse / SectionHeading / ListBlock` 优先用 `normalized_text`

### Frontend

1. `/read` 主正文优先展示 normalize 后文本
2. `AI 上下文` 可以看到 normalize 变更摘要
3. `EquationBlock` 仍保持 image-first

## 回归重点页

- `paper 85 / page 3`
  - 公式 image-first 不被破坏
- `paper 85 / page 8`
  - 普通 prose/list normalize
- `paper 78 / page 7`
  - 现有较好页不应回退

## 结果

### 自动化

1. `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "text_normalization or equation_normalization or apply_layout_uid_text_normalization or prefer_normalized_text" -q`
   - `5 passed`
2. `npm --prefix frontend run lint`
   - `EXIT:0`
3. `cd frontend && npx tsc --noEmit`
   - `EXIT:0`
4. `./backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
   - 通过
5. `./backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`
   - 通过

### 本轮确认

- `layout_atoms / reading_nodes` 已支持：
  - `normalized_text`
  - `normalization_reason`
  - `normalization_mode`
  - `normalization_confidence`
- `layout_uid` text normalization 现在会：
  - 只处理允许的 `node_kind`
  - 强制 `layout_id` exact-once 覆盖
  - 只把变更项写回 grounding
- grouping prompt 和 `/read` 的 `SectionHeading / ParagraphProse / ListBlock` 物化都会优先用 `normalized_text`
- `/read` 右侧 `AI 上下文` 新增 `Normalize 变更` 区块
- 公式仍保持 image-first，且 AI normalize 痕迹会并入同一个 `Normalize 变更` 汇总里
- `/read` 主阅读区允许把 `^数字` 型 normalize 标记渲染成真正的上标，但 evidence 主链仍保持纯文本 anchor，不做富文本参与坐标或 quote 计算。
- `footer / header / doi / metadata` 这类被 omit 的 layout 现在也会进入 normalize prompt
- `Intentional Omissions` 现在按 omission reason 分组，并显示被隐藏 layout 的可读文本；若存在 `normalized_text`，则同时显示原文与修正文案
- `footer` 需要单独一栏展示，验证点：
  - footer 类隐藏项不会再只出现在通用 omission 列表里
  - footer 内的链接/脚注文本可直接阅读
  - 通用 omission 列表仅保留 header/noise/metadata/doi 等其他隐藏项

### 风险与边界

- 本轮没有触碰全局 evidence preview 主路径
- 本轮没有让 AI 修改 geometry / ownership / source ids
- 本轮只做展示层 normalize，不做翻译和自由改写

### 已发现的 live 风险

- `paper 85 / page 8` 暴露过一次真实链路问题：
  - `layout_uid_text_normalization` 先出现 DashScope `RemoteDisconnected`
  - 随后兼容接口又拿到了 `http://localhost:8888/api/...` 形式的页图 URL
  - provider 直接返回 `invalid_parameter_error`
- 这说明 live normalize 还需要补一层：
  - 本地 `file://` 失败后，不再把 `localhost / 内网` 资产 URL 当作 `image_url` 回退给兼容接口

### 后续修正

- backend 已改为：
  - DashScope 本地 `file://` 图片调用失败时先重试一次
  - 兼容接口回退只接受公网可达 `image_url`
  - `localhost / 127.0.0.1 / docker service host` 等地址不再塞进 `image_url`
- compose 版本已升到 `reader_compose_v15`，用于切断旧 `reader_compose_v14` 缓存
- 同时修正 `page_grounding_v1` 的尺寸解析来源：
  - 一旦 `page_render_asset` 已经生成，`page_image.width / height` 也必须从这份本地资产解析
  - 不再回头访问 `docmind_page_image_url / path`
  - 避免 page 8 这类页面在 fresh compose 刚开始就额外产出一次 `failed to fetch grounding page image size: HTTP Error 403: Forbidden`
- 进一步收敛页图来源：
  - 当没有 `page_render_asset` 时，DocMind 整页图必须先持久化到本地缓存，再通过本地 API URL 提供给 `page_grounding_v1.page_image`
  - 回归需要验证：
    - `page_image.path` 指向本地缓存文件
    - `page_image.url` 指向本地 reader 资产路由，而不是临时 DocMind URL
    - `page_image.source` 明确标记为本地化后的 DocMind 图
    - `page_image.origin_url` 仍保留原始临时 URL，便于诊断
- 另外补一条 contract 回归：
  - `layout_uid_text_normalization` 已经写回 `page_grounding_v1` 后，
  - `_ensure_payload_contract(...)` 重新构建 grounding 时必须保留这些 enrichments
  - 避免出现：
    - `decision_log = layout_uid_v1:text_normalized=11`
    - 但 `page_grounding_v1.layout_atoms[*].normalized_text` 仍全空
- 额外兼容旧缓存：
  - 如果 `layout_advice_v3.text_normalizations.normalization_plan` 已存在，
  - 但 `page_grounding_v1` 还没同步这些 normalize 结果，
  - `_ensure_payload_contract(...)` 应能在读缓存时回填 `normalized_text / normalization_summary`
- 继续补一条缓存读路径回归：
  - 当 payload 已带 `page_grounding_v1.page_image=1360x1760` 和 `layout_atoms.normalized_text`
  - 但 `docmind_structure / page_structure_v3` 为空时，
  - `_ensure_payload_contract(...)` 仍必须保留这份 grounding，并把 `source_anchor_refs.quote_text / geometry / bbox_hint` 刷新成当前 grounding 对应的新值。
- 再补一条缓存持久化回归：
  - `_read_payload_from_db(...)` 在发现 contract repair 修正了 stale anchors 后，
  - 必须把修正后的 payload 回写到 `row.payload_json` 并提交，
  - 避免 live 页面每次都读到旧 anchor 再临时修一遍。
- 再补一条 display-only 回归：
  - `paper 85 / page 8` 这类已经写回 `normalized_text=llama.cpp^6` 的页面，
  - 主阅读区应显示真正的上标 `6`，
  - 但 evidence preview 的 anchor `quote_text` 和页面尺寸不得因为 display formatting 再次发生漂移。
- 再补一条坐标系回归：
  - 当 `grounding_pages/page_X.png` 已存在且尺寸与 `page_render_asset` 不同，
  - `page_grounding_v1.page_image` 必须优先指向本地化的 DocMind 页图与其真实尺寸，
  - 前端空间索引也必须优先信 `page_grounding_v1.page_image.width/height`，不能再让旧 anchor 尺寸覆盖它。
- 再补一条图链隔离回归：
  - `layout_uid_v1` 跑 text normalization / grouping 时可以继续使用 `page_render_asset` 作为 prompt 图，
  - 但执行完成后不得把这张 prompt 图覆写回 `page_grounding_v1.page_image`。
  - 回归验证：
    - `page_grounding_v1.page_image.source = docmind_page_image_localized`
    - prompt 图仍可单独走 `page_render_asset`
    - 旧缓存读修后，anchor `bbox_hint.page_width/page_height` 与 `geometry.page_width/page_height` 都刷新成 `1483x1920`
- 已补 footer link fallback 回归：
  - `test_apply_layout_uid_text_normalization_to_grounding_should_backfill_footer_link_urls`
  - 期望：
    - `Shttps://api-docs.deepseek.com/` -> `^8 https://api-docs.deepseek.com/`
    - `Yhttps://cloud.tencent.com/document/product/1772/115963` -> `^9 https://cloud.tencent.com/document/product/1772/115963`
    - fallback 不再保留 `normalization_mode=no_change`
    - 改为：
      - `normalization_mode = footer_link_fallback`
      - `normalization_reason = footer_link_cleanup`
- 本轮 footer bundle prompt 收敛不新增完整自动化回归，先按手动验证为主：
  - `Footer / Links` 中，若 AI 把某个 footer layout 标成：
    - `mode = footer_hide_fragment`
    - `normalized_text = ""`
  - 则该碎片不应继续出现在侧栏展示里。
  - 预期由 AI 直接修好的 URL / 脚注说明保留在同组其他 footer layout 中。
# 运行时页图链路回归

- 目标：
  - 禁止临时 DocMind URL 进入 `/read` 运行时主链。
  - prompt 图仅走本地文件；grounding 图仅走本地化资产；`origin_url` 仅作调试元数据。
- 覆盖回归：
  - `test_build_page_grounding_v1_should_not_fallback_to_render_asset_when_docmind_url_is_stale`
  - `test_merge_existing_grounding_enrichments_should_not_keep_stale_render_asset_page_image`
  - `test_ensure_payload_contract_should_refresh_layout_uid_anchors_from_current_grounding`
  - `test_build_layout_uid_pipeline_result_should_not_overwrite_grounding_page_image_with_prompt_asset`
  - `test_stream_reader_docmind_page_image_localizes_remote_url`
- 本轮验证：
  - `backend/tests/test_literature_reader_composed.py -k "not_fallback_to_render_asset_when_docmind_url_is_stale or merge_existing_grounding_enrichments_should_not_keep_stale_render_asset_page_image or refresh_layout_uid_anchors_from_current_grounding or build_layout_uid_pipeline_result_should_not_overwrite_grounding_page_image_with_prompt_asset" -q`
  - `backend/tests/test_literature_reader_api.py -k "stream_reader_docmind_page_image_reads_local_file or stream_reader_docmind_page_image_localizes_remote_url or stream_reader_grounding_page_asset_reads_localized_file" -q`
  - `python3 -m py_compile backend/app/services/literature_reader_compose_service.py backend/app/api/literature.py backend/tests/test_literature_reader_composed.py backend/tests/test_literature_reader_api.py`
  - `cd frontend && npx eslint src/pages/literature/PaperReaderPage.tsx`
