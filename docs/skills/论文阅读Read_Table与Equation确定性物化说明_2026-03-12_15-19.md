# 论文阅读 Read Table 与 Equation 确定性物化说明

时间：2026-03-12 15:19

## 背景

`/read` 已经收敛到：

- `DocMind -> uniqueId(layout_id) -> qwen3.5-plus grouping -> 简化阅读节点`
- 高光优先走 `uniqueId -> blocks[].pos`

但 `layout_uid_v1` 仍有两个明显缺口：

1. `table` group 只会被识别，不会被真正物化，结果是 `TablePanel.rows = []`
2. 常见公式/LaTeX 仍容易掉回 prose

## 这轮设计

### 1. `/read` 把 table 和 equation 当成 deterministic islands

`/read` 不再尝试让 AI 自由生成复杂表格或公式说明，而是：

- AI 只负责 `uniqueId` 级分组
- 确定性 materializer 负责把 group 落成稳定阅读节点

这和 `/experience` 的 generative UI 目标不同，符合 `/read` 当前“轻量 AI 排版 + 正文清洗 + 证据核验”的定位。

### 2. `page_grounding_v1` 分型扩展

新增或收紧了这些 `node_kind`：

- `table_caption`
- `equation`

判定优先级：

- `Table N ...`、`table_caption` 优先归 `table_caption`
- `equation / formula / math` 类型或常见数学表达，归 `equation`

### 3. `layout_uid_v1` fallback/grouping 规则扩展

确定性 fallback 现在支持：

- `figure + adjacent figure_caption`
- `table + adjacent table_caption`
- `equation` 单独成组

### 4. `TablePanel` 新物化方式

表格不再只产 `rows=[]`，而是产：

- `title`
- `headers`
- `header_row_count`
- `matrix`
- `rows`
- `caption`
- `notes`
- `raw_markdown`

物化顺序：

1. 优先解析 markdown-like table 文本
2. 没有 markdown 风格时，按 `blocks[].pos` 做几何行聚类
3. 生成稳定矩阵和 body rows

### 5. `EquationBlock` 新物化方式

公式组会直接落成 `EquationBlock`：

- `latex`
- `label`
- `description`

当前目标不是完整数学排版，而是：

- 不再掉回 prose
- 保留公式文本
- 保留 uniqueId 溯源和高光

## 前端渲染调整

`TablePanel` 不再只把 `rows` 拼成 `a | b | c` 文本，而是：

- 优先按 `matrix + header_row_count` 渲染 HTML table
- 其次回退到旧 `rows`
- 再差也保留 `raw_markdown`

同时：

- caption 和 notes 会稳定显示
- markdown 导出优先使用 `matrix`

## 当前已知限制

这轮只解决“常见表格 / 常见公式”：

- 已支持 markdown-like 表格
- 已支持普通网格表按 block 几何聚类
- 仍未支持复杂 `rowspan / colspan`
- 仍未支持完整数学公式渲染引擎（当前只是 dedicated block，不再混成 prose）

## 结论

这轮之后，`/read` 的 `layout_uid_v1` 更接近：

- `uniqueId` 分组
- AI 清洗正文
- 轻量 HTML 流式阅读
- deterministic table / equation islands
- `uniqueId -> blocks[].pos` 高光

## Cache 处理

这轮同步把 `/read` compose engine 升到了 `reader_compose_v5`，避免旧 cache 继续返回：

- `TablePanel.rows = []`
- 旧的 table/prose 混排结果
