# 论文阅读 Read 表格 AI Logical Row 重建测试记录

时间：2026-03-13 00:30

## 目标

为 `/read` 表格接入一层 AI logical-row reconstruction，但严格限制影响范围：

- 只改表格语义重建
- 不改全局 evidence/highlight 主链
- 出错必须 fallback

## 回退基线

如果本轮实现失败，需确保以下页面仍然保持可用：

- `http://localhost:3000/literature/85/read?page=7&kb=84`
- `http://localhost:3000/literature/85/read?page=3&kb=84`
- `http://localhost:3000/literature/78/read?page=7&kb=84`

## 本轮计划验证

### Backend

- 新增 table logical-row plan prompt payload 构造
- 新增 table logical-row plan normalize / validate
- 新增 deterministic materialize with AI row groups
- 非法 plan fallback 到当前 deterministic table builder

### Frontend

- `TablePanel` 优先消费 `logical_rows`
- 缺失时继续走现有 deterministic 逻辑

## 必须覆盖的回归点

1. page 7 table-heavy
   - benchmark table 不再明显错行
2. page 3 formula-heavy
   - 公式 image-first 不被误伤
3. prose-heavy 页面
   - hover / pinned evidence 不被误伤
4. 整表证据链
   - `证据` 菜单仍可用
   - pinned evidence 仍可用

## 2026-03-13 01:55 实施进度

已落地：

- backend:
  - `layout_uid_v1` 表格二次 AI pass prompt / normalize / fallback skeleton
  - `TablePanel.logical_rows`
  - `TablePanel.logical_header_row_count`
  - `TablePanel.reconstruction_mode / reconstruction_notes`
- frontend:
  - `TablePanel` 优先消费 `logical_rows`
  - 缺失时继续走现有 deterministic 逻辑

## 已完成验证

- `./backend/.venv-incremental/bin/python -m py_compile backend/app/services/literature_reader_compose_service.py backend/app/services/reader_component_contract_service.py backend/tests/test_literature_reader_composed.py`
  - 通过
- `./backend/.venv-incremental/bin/python -m py_compile backend/app/schemas/literature.py`
  - 通过
- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py::test_normalize_layout_uid_table_logical_row_plan_should_require_exact_once_coverage -q`
  - `1 passed`
- `./backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
  - 通过
- `./backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`
  - 通过

## 尚未拿到稳定结果

- `frontend` 定向 eslint
- `frontend` `tsc --noEmit`
- `test_layout_uid_group_plan_to_panel_plan_should_apply_ai_table_logical_rows`

这些命令在当前环境里没有拿到稳定退出结果，所以本记录不将其算通过。

## 下一步

- 继续确认 `logical_rows` 物化测试
- 重启 `backend/frontend`
- 在真实页面验证：
  - 表格是否不再完全依赖前端 continuation-row 猜测
  - 证据链是否保持不变
