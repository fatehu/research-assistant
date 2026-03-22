## 背景

`layout_uid_v1` 的表格 AI logical-row 重建骨架已经接入 `literature_reader_compose_service.py`，但运行态返回的 `TablePanel.props` 里仍然看不到：

- `logical_rows`
- `logical_header_row_count`
- `reconstruction_mode`
- `reconstruction_notes`

这会造成：

- 前端仍回退到 deterministic `rows`
- 用户看到的 live 表格结果和“AI logical-row 已接入”的说法不一致

## 已确认原因

不是 AI pass 没跑，而是 panel plan 转最终 `ui_plan.components` 时，`TablePanel` props 被裁剪了。

位置：

- `backend/app/services/literature_reader_compose_service.py`
- `_panel_plan_to_ui_plan(...)`
- `sanitize_props(...)` 中的 `component == "TablePanel"` 分支

当前分支只保留了：

- `rows`
- `matrix`
- `headers`
- `column_widths`
- `table_cells`
- `header_row_count`
- `caption`
- `notes`
- `raw_markdown`
- `row_evidence`
- `cell_evidence`
- `ai_insight`

没有保留 AI logical-row 相关字段。

## 本次修复目标

1. 把 `logical_rows` 真正写入最终 `ui_plan.components[*].props`
2. 把 `logical_header_row_count / reconstruction_mode / reconstruction_notes` 一并保留
3. 升 `COMPOSE_ENGINE_VERSION`，强制旧 `reader_compose_v9` cache 失效
4. 补回归测试，证明 `_panel_plan_to_ui_plan(...)` 不再裁掉这批字段

## 回退边界

如果这轮修复导致 `/read` 表格运行态异常，可以只回退：

- `COMPOSE_ENGINE_VERSION`
- `_panel_plan_to_ui_plan(...)` 中 `TablePanel` props 保留逻辑
- 对应新增测试

不要回退：

- 现有 `layout_uid_v1` 分组主链
- 全局 evidence preview 主链
- 公式 image-first 改造
