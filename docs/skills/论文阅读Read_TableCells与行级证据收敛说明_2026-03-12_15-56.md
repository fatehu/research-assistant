# 论文阅读 `/read`：Table Cells 与行级证据收敛说明

时间：2026-03-12 15:56

## 背景

`layout_uid_v1` 切到 `/read` 默认主链后，`TablePanel` 仍有两个根问题：

1. 表格在 `page_grounding_v1` 抽取时被降成整表级 `layout + block`，丢掉了 DocMind `cells[]`。
2. 表格证据只剩整表级 anchor，没有行级定位能力。

用户在 `paper 85 / page 7` 的真实结果里观察到：

- 表格被压成“一行很多列”的假矩阵。
- 右栏证据预览只剩整表级，没有行级定位。

## 原因定位

### 1. 原始 DocMind 数据其实有完整 `cells[]`

真实 `docmind_structure.layouts[*]` 的 `table` layout 中包含：

- `numRow`
- `numCol`
- `cells[*].ysc/yec/xsc/xec`
- `cells[*].pos`
- `cells[*].layouts[*].uniqueId`

这说明表格行列真值在 DocMind 原始层是存在的。

### 2. `page_grounding_v1` 在抽取时丢掉了 `cells[]`

旧实现只保留：

- `layout_pos`
- `blocks`
- `canonical_block_ids`

导致后续 `TablePanel` 物化只能基于：

- markdown-like 文本
- 或整表 block 几何

这会把复杂表格误压成单行矩阵。

### 3. `TablePanel` props 在 panel plan -> ui plan 转换时再次被裁掉

旧 `normalize_props(TablePanel)` 只保留：

- `title`
- `rows`

会把：

- `matrix`
- `headers`
- `caption`
- `row_evidence`

再次丢失。

## 本轮设计

### 1. `page_grounding_v1` 保留表格 cell 真值

对 `layout_type=table` 的 layout atom 额外保留：

- `table_cells[*].cell_id`
- `table_cells[*].row_start / row_end`
- `table_cells[*].col_start / col_end`
- `table_cells[*].text`
- `table_cells[*].layout_ids`
- `table_cells[*].polygons`

同样写入 `evidence_map[*].table_cells`。

### 2. `TablePanel` 优先用 `table_cells` 重建矩阵

物化顺序改成：

1. `table_cells`
2. markdown-like rows
3. block bbox clustering

也就是说，只有在 DocMind 没给 cell 真值时，才退回旧方法。

### 3. 为每个表格行生成 row-level evidence

`TablePanel.props.row_evidence[*]` 新增：

- `row_index`
- `label`
- `source_atom_ids`
- `anchor`

其中 `anchor.geometry.polygons` 直接来自该行所有 cell polygon 的并集。

### 4. `/read` 前端让表格行直接触发证据预览

`TablePanel` 现在支持：

- 悬停行 -> `onPreviewAnchors`
- 点击行 -> `onJumpAnchor`

这让表格证据不再只有整表级入口。

### 5. 自动合并紧邻的 `table_caption`

即使 AI grouping 把 `table` 和 `table_caption` 拆成两个 group，panel materializer 也会在 `/read` 层把它们重新并回同一个 `TablePanel`。

## 当前边界

这轮修的是：

- DocMind `cells[]` 不再丢
- 常见行列表不再被降成单行假矩阵
- 行级 evidence/highlight 回来

还没完全做的是：

- rowspan/colspan 的精细 HTML 合并
- 多层表头的完整语义化
- 表格脚注与注释的更细粒度绑定

## 对 `/read` 定位的影响

这次修正继续符合 `/read` 的定位：

- 不是做 generative UI 页面设计
- 而是做稳定的 HTML 式流式阅读
- AI 清洗正文与简化排版
- 准确高光与证据核验
