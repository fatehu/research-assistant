# 论文阅读 Read：LayoutUidV1 默认切换与 UniqueId 高光测试记录

## 变更范围

- `/read` 默认 compose pipeline 切到 `layout_uid_v1`
- `/read` evidence/highlight 优先走 `source_layout_id -> page_grounding_v1.evidence_map -> blocks[].pos`

## 自动化验证

### Backend

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "layout_uid or page_grounding_v1 or doi_layout_outside_main_flow or panel_plan_to_ui_plan_should_emit_layout_geometry_anchor_and_source_atom_ids or pipeline_version_should_default_to_layout_uid_v1_when_unset" -q
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "pipeline_version_override or default_to_layout_uid_v1_pipeline or repair_malformed_fallback_payload" -q
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- `8 passed`
- `3 passed`
- `Contract alignment guard passed.`
- `Broad exception guard passed.`

新增覆盖点：

- 默认 pipeline 在未显式 override 时应为 `layout_uid_v1`
- `_panel_plan_to_ui_plan(...)` 应输出 layout-based anchor
- `source_atom_ids` 在 `layout_uid_v1` 中应承载 `source_layout_ids`
- page grounding 保持 `uniqueId` 原子单元和 DOI 非主阅读流约束

### Frontend

命令：

```bash
npm --prefix frontend run lint
```

结果：

- 通过，`0 error`

## 直接验收地址

- `http://localhost:3000/literature/78/read?page=7&kb=84`
- `http://localhost:3000/literature/85/read?page=1&kb=84`
- 如需显式对照：
  - `http://localhost:3000/literature/78/read?page=7&kb=84&compose=layout_uid_v1`
  - `http://localhost:3000/literature/85/read?page=1&kb=84&compose=layout_uid_v1`

## 本轮已知限制

- `layout_uid_v1` 的 table materialization 还没完成。
- 当前 table 只能被分到 `table` group，但还没有 deterministic rows/cells 结构，因此 table-heavy 页仍会乱。
