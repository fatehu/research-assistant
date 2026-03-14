# 论文阅读 Read LayoutUidV1 分组管线骨架说明

时间：2026-03-12 13:44

## 背景

`/read` 不再承担页面级 generative UI 产品设计目标，但仍需要保留：

- AI 清洗正文
- 简化后的 AI 排版
- 准确的溯源和高光

旧的 `/read` 默认实现仍然容易落回 `simplified_v2` 的复杂壳层与旧 panel-plan 语义。为了把 `/read` 收回到“HTML 式流式阅读 + uniqueId 溯源”的目标，本轮新增了新的内部 pipeline 骨架：

- pipeline version: `layout_uid_v1`

## 这轮新增的核心能力

### 1. 新的 `/read` 内部 pipeline 选择开关

在 `LiteratureReaderComposeService` 中新增：

- `_use_layout_uid_pipeline()`

当 `reader_pipeline_version == "layout_uid_v1"` 时，`build_or_get_composed_payload()` 不再优先走：

- `simplified_v2`
- 旧 semantic-atom pipeline

而是优先走：

- `_build_layout_uid_pipeline_result(...)`

默认流量暂时没有切换，这一版只是先把新链路接进系统，便于逐步验证。

为了让这一版可以直接验收，前端 `/read` 现在支持通过 URL 查询参数显式请求新链路：

- `compose=layout_uid_v1`

例如：

- `http://localhost:3000/literature/78/read?page=7&kb=84&compose=layout_uid_v1`

页面上会额外显示一个紫色 `layout_uid_v1` 标签，避免误把旧缓存结果当成新链路。

### 2. 以 `uniqueId` 为最小单位的 prompt 输入

新的 `_build_layout_uid_prompt_payload(...)` 直接消费 `page_grounding_v1.layout_atoms`，只给模型发送：

- `layout_id`
- `reading_order`
- `layout_type`
- `layout_sub_type`
- `node_kind`
- `text`
- `include_in_main_flow`
- `region_hint`
- `layout_pos`
- `block_count`

关键约束：

- 最小单位只能是 `layout_id(uniqueId)`
- 不发送 block 级几何细节给 grouping prompt
- 不允许模型拆分一个 `layout_id`

这样做的目标是让 `/read` 的 grouping 语义和高光语义都围绕 `uniqueId` 收敛，而不是继续被旧 markdown/block hint 污染。

### 3. layout_uid_v1 grouping contract

新的 system prompt 要求模型输出：

- `groups`
- `omissions`
- `notes`

并满足：

- `layout_id` exactly once
- 不允许 invent id
- 不允许 split layout ownership
- 目标是 `html_like_reading_flow`

当前允许的 `group_kind`：

- `title`
- `section_heading`
- `paragraph`
- `list`
- `figure`
- `table`
- `metadata`
- `doi`
- `header`
- `footer`
- `noise`

### 4. deterministic fallback grouping

如果模型输出有这些问题：

- duplicate layout id
- missing layout id
- unknown layout id
- 空 groups

新链路不会直接炸掉，而是自动回退到 deterministic fallback：

- `figure + adjacent figure_caption` 自动合并
- 连续 paragraph 最多合并成一个 paragraph group
- `doi / metadata / header / footer / noise` 进入 omissions

这保证了 `layout_uid_v1` 先成为一条稳定链，而不是另一条脆弱实验链。

### 5. 轻量 materialize 到 `/read` 组件树

当前骨架会把 `layout_uid_v1` 的 group plan materialize 成一个轻量 panel plan，再复用现有 `_panel_plan_to_ui_plan(...)`：

- `title` -> `SectionHeading`
- `section_heading` -> `SectionHeading`
- `paragraph` -> `ParagraphProse`
- `list` -> `ListBlock`
- `figure` -> `FigurePanel`
- `table` -> `TablePanel`

这样做的目的是：

- 先复用现有 `/read` 渲染器
- 先把 grouping 最小单位切对
- 暂时不在这一轮同时重写整套前端渲染执行器

## 这轮没有做的事

- 没有切默认 `/read` 流量到 `layout_uid_v1`
- 没有移除旧的 `simplified_v2`
- 没有改 `/read` 的前端 evidence/highlight 运行时
- 没有把 `uniqueId -> blocks[].pos` 高光链完全替换到默认路径

## 预期下一步

1. 让 `layout_uid_v1` 真正消费 `qwen3.5-plus + rendered page image` 的 grouping 结果做页面验证。
2. 继续把 `source_layout_ids -> uniqueId -> block_positions` 接进 `/read` 的高光链。
3. 再决定何时把 `/read` 默认 pipeline 从旧分支切到 `layout_uid_v1`。
