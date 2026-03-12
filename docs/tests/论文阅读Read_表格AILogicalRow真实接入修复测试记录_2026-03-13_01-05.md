## 目标

验证 `/read` 的表格 AI logical-row 重建不是只停留在 panel plan 内部，而是：

- 真正进入最终 `ui_plan.components[*].props`
- 前端能消费 `logical_rows`
- 旧 `reader_compose_v9` cache 不再继续遮蔽新结果

## 预期验证

1. 后端单测：
   - `_panel_plan_to_ui_plan(...)` 对 `TablePanel` 保留 `logical_rows`
   - 保留 `logical_header_row_count`
   - 保留 `reconstruction_mode`
   - 保留 `reconstruction_notes`
2. 运行态：
   - `paper 85 / page 7` fresh rebuild 后，最新 `TablePanel.props.reconstruction_mode == ai_logical_rows`
   - `logical_rows.length > 0`
3. 缓存：
   - 最新 payload `engine_version` 不再是 `reader_compose_v9`

## 已知风险

- 如果 fresh rebuild 依然拿不到 `logical_rows`，问题就不在 props 裁剪，而在 AI table pass 自身未产出结果。
- 这时需要继续查：
  - `_build_layout_uid_table_refinement_map(...)`
  - `layout_uid_table_logical_rows:*` phase

## 本轮结果

- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "keep_ai_table_logical_row_fields or apply_ai_table_logical_rows" -q`
  - `2 passed`
- `./backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
  - passed
- `./backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`
  - passed
- `python3 -m py_compile backend/app/services/literature_reader_compose_service.py backend/tests/test_literature_reader_composed.py`
  - passed

### 运行态 fresh rebuild 证据

强制对 `paper 85 / page 7 / kb 84` 执行 fresh rebuild 后，live payload 返回：

- `engine_version = reader_compose_v10`
- `build_mode = compose_agent_layout_uid_v1`
- `TablePanel.props.reconstruction_mode = ai_logical_rows`
- `TablePanel.props.logical_rows.length = 13`
- `TablePanel.props.logical_header_row_count = 1`

这说明这轮修复后，AI logical-row 重建已经真正进入 live payload，不再只是停留在 panel plan 内部。
