## 目标

验证 `/read` 表格 AI logical-row 提示词和 prompt payload 已明确覆盖：

- `value row + uncertainty row`
- blank first-column continuation row
- multi-line header

## 预期验证

1. `_layout_uid_table_logical_row_system_prompt()` 文本中明确出现：
   - value row
   - uncertainty row
   - `(±...)`
   - blank first-column continuation

2. `_build_layout_uid_table_logical_row_prompt_payload()` 输出中包含行级 pairing hints

3. 现有 exact-once 逻辑不回退

## 本轮只验证

- prompt / payload 约束强化
- 不要求这一轮直接证明表格最终视觉效果完全收稳

## 本轮结果

- `python3 -m py_compile backend/app/services/literature_reader_compose_service.py backend/tests/test_literature_reader_composed.py`
  - passed
- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "value_uncertainty_pairing or pairing_hints or apply_ai_table_logical_rows or keep_ai_table_logical_row_fields" -q`
  - `4 passed`
- `./backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
  - passed

## 当前结论

- prompt 已明确覆盖：
  - multi-line header
  - value row + uncertainty row `(±...)`
  - blank first-column continuation row
- prompt payload 已补 `hints`
  - `blank_first_column`
  - `numeric_like_count`
  - `uncertainty_like_count`
  - `contains_pm`
  - `looks_like_uncertainty_row`
- 现有 exact-once validator 未被削弱
