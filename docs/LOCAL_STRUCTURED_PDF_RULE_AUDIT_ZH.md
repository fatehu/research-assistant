# Local Structured PDF 规则审计

## 目的

这份文档用于审计 `backend/app/services/local_structured_pdf/` 当前规则链的“通用性”与“过拟合风险”。

目标不是否定规则本身，而是回答两个问题：

1. 当前规则是否已经出现明显的 benchmark 定制化倾向。
2. 接下来应该继续补哪些通用处理器，避免再沿着低分文档逐个修洞。

## 总结结论

当前代码里没有发现以下类型的问题：

- 没有 `doc_id` 级特判
- 没有针对某篇 benchmark 文档标题、机构名、作者名的白名单
- 没有“如果文本包含某个特定短语就走特殊分支”的硬编码

但是，存在一种更隐性的风险：

- 规则增长明显受 benchmark 低分样本驱动
- 某些启发式已经开始绑定“常见 benchmark 版式”
- 少数模块已经从“几何/结构规则”滑向“经验性文本模式”

因此当前判断是：

- 不是“硬编码作弊”
- 但已经进入“benchmark-informed heuristics 边界变窄”的阶段

## 风险分层

### 低风险规则

这类规则主要基于 PDF 几何、页面边界、对象关系，具有较强跨文档稳定性，可以保留。

1. 页面噪声过滤
   - `tiny_word / out_of_page / zero_bbox`
   - 位置: [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py#L87)

2. 重复页眉页脚和页码剔除
   - 重复签名、页码模式、`title | page` / `page | title`
   - 位置: [document_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/document_resolver.py#L63)

3. 双栏阅读顺序和跨栏宽行拆分
   - 主要基于 bbox、boundary、column band
   - 位置: [document_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/document_resolver.py#L108)

4. 普通表格的行列对齐、anchor interval、sparse chart 拦截
   - 主要基于列锚点、对齐一致性、稠密度
   - 位置: [table_detector.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/table_detector.py#L1146)
   - 位置: [table_detector.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/table_detector.py#L1684)

### 中风险规则

这类规则仍然是通用模式，但已经引入文本语义假设，适合保留，同时要限制继续膨胀。

1. caption / footnote 前缀识别
   - `figure/table/chart/image/photo/plate`
   - `1. / * / †` 样式脚注
   - 位置: [auxiliary_block_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/auxiliary_block_resolver.py#L9)

2. heading 基础模式
   - `appendix/chapter`
   - `Title:` 冒号标题
   - heading continuation
   - 位置: [heading_refiner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/heading_refiner.py#L12)
   - 位置: [heading_refiner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/heading_refiner.py#L239)

3. 首页 front matter 降级
   - `abstract/keywords/summary/contents`
   - 作者行、机构行、邮箱/URL/DOI
   - 位置: [front_matter_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/front_matter_resolver.py#L10)
   - 位置: [front_matter_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/front_matter_resolver.py#L150)

4. line grouping 中的 heading/table/footnote 语义切分
   - 位置: [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py#L148)
   - 位置: [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py#L334)

### 高风险规则

这类规则不是错误，但继续沿着这条路加规则，会很容易变成“对 benchmark 常见版式有效，对泛化文档不稳”。

1. `front_matter` 关键词列表
   - `_AFFILIATION_KEYWORDS` 已经带有较强经验性，尤其 `"ai"` 这种短词风险偏高
   - 位置: [front_matter_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/front_matter_resolver.py#L22)
   - 风险: 误伤普通标题、组织名较短的正文块、技术术语标题

2. `parallel short title band / panel heading`
   - 这是为了卡片式/面板式页面加的 heading 提升逻辑
   - 位置: [heading_refiner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/heading_refiner.py#L186)
   - 位置: [heading_refiner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/heading_refiner.py#L211)
   - 风险: 容易把 panel label、legend、短字段名抬成 heading

3. `PyMuPDF` 表头后处理
   - `_expand_centered_dual_header_rows`
   - `_collapse_sparse_header_rows`
   - `_shift_orphan_header_cells`
   - 位置: [table_detector.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/table_detector.py#L1818)
   - 风险: 这是“结构修复”，不是“结构恢复”；如果继续加更多 header 重写规则，很容易把错误输出合理化

4. 文字块驱动的同行拆分
   - 这是当前 `NID` 提升的重要来源，但也属于高杠杆规则
   - 位置: [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py#L170)
   - 位置: [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py#L258)
   - 风险: 如果再往里叠更多阈值，会把 block provider 的噪声放大

## 从结果反推的真实短板

不看单个 benchmark 文档，按当前低分分布，剩余问题主要是这四类。

### 1. Visual Page / Poster / Image-heavy Page

特征：

- `nid` 很低
- `mhs` 很低
- `teds` 多数不存在
- 典型表现是整页几乎没有可靠文本主流

这说明当前本地链缺少：

- 页面类型分诊
- 视觉页 fallback

### 2. Sparse Form Table / Worksheet Table

特征：

- `teds = 0` 或很低
- 空白单元格多
- 表头跨行
- 更像表单/练习册/对照模板，不像普通 dense table

这说明当前表格器还偏向：

- dense matrix table

而没有单独建模：

- sparse form table

### 3. Complex Region Reading Order

特征：

- `nid` 低，但不一定有表格问题
- 页面里常见 sidebar、panel、instruction box、form area

这说明当前主排序仍然偏向：

- `line order + column order`

而不是：

- `region graph order`

### 4. Heading Role Confusion

特征：

- `mhs` 低，但阅读顺序未必差
- 标题、卡片标题、caption、front matter 之间角色混淆

这说明当前 heading 处理仍然偏向：

- 单块启发式

而不是：

- 页面内样式族 + 角色分类

## 后续规则边界

从现在开始，新增规则必须遵守下面几条。

1. 不允许增加任何 `doc_id`、标题词、机构名白名单。
2. 不允许为了修复某个样本，引入只对单页版式成立的文本 if。
3. benchmark 只能做回归门，不能再反向定义规则。
4. 新规则必须尽量落在以下层级：
   - page triage
   - region segmentation
   - sparse form table processor
   - heading role classifier
5. 如果一个规则本质是在“修正提取结果文本”，优先判断是否应该上移成更早的结构处理器。

## 建议的下一步

### 第一优先级

做 `page triage`

输出至少五类：

- `plain_text`
- `dense_table`
- `sparse_form`
- `mixed_layout`
- `visual_page`

这一步会直接减少：

- 视觉页误走文本主链
- 表单页误走 dense-table 路径
- 图文混排页误走普通双栏排序

### 第二优先级

做 `sparse_form_table processor`

原则：

- 空白格是合法结构
- 表头可跨行
- 列关系优先级高于文本密度

### 第三优先级

做 `region graph reading order`

不要继续主要依赖：

- line sort
- boundary split
- block overlap

而是先切页面区域，再做区域间顺序。

### 第四优先级

把 `front_matter + heading_refiner + caption/footnote` 收敛成统一的 `block role classifier`

角色至少区分：

- document_title
- section_heading
- panel_heading
- caption
- footnote
- front_matter
- body

## 审计结论

当前版本已经达到并略超 Java 本地基线，但这不代表规则链已经健康。

当前最重要的结论不是“还能继续补哪些规则”，而是：

- 普通表格和普通论文版面已经够强
- 再继续沿着 benchmark 低分样本补启发式，收益会越来越差
- 接下来必须从“修规则”转向“补处理器”

这份审计可以视为当前本地规则线的冻结边界。
