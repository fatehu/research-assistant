# 论文阅读 Read 公式 AI Normalization 测试记录

时间：2026-03-13 18:20

## 目标

验证 `/read` 公式节点支持：

- style 信息保留
- AI normalization 输出接入
- AI LaTeX 优先主显示
- 原图作为 fallback

## 计划验证

1. grounding 合同
   - `layout_atoms.blocks[*].style_id` 存在
   - `layout_atoms.alignment / line_height` 存在

2. equation normalization
   - AI 返回的 `normalized_text / normalized_latex / reason / confidence`
     能稳定进入 `EquationBlock.props`
   - 非法返回时能 fallback

3. 前端
   - `normalized_latex` 存在时，正文优先显示 KaTeX
   - `normalized_latex` 缺失时，正文回退显示公式图
   - OCR transcript 与 normalization 原因继续保留

## 风险

- 不能让 formula normalization 改动全局 evidence preview 主链
- 不能让低质量 AI 输出覆盖原图 fallback

## 已执行验证

1. Backend compile
   - `python3 -m py_compile backend/app/services/literature_reader_compose_service.py backend/app/services/reader_component_contract_service.py backend/app/services/reader_single_agent_validator.py`
   - 结果：通过

2. Backend targeted pytest
   - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py::test_build_layout_uid_equation_props_should_include_ai_normalization_fields -q`
   - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py::test_layout_uid_group_plan_to_panel_plan_should_apply_equation_normalization -q`
   - `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py::test_ensure_payload_contract_should_build_page_grounding_v1_from_layout_unique_ids -q`
   - 结果：`3 passed`

3. Backend guards
   - `./backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`
   - `./backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`
   - 结果：通过

4. Frontend checks
   - `npm --prefix frontend run lint`
   - `cd frontend && npx tsc --noEmit`
   - 结果：通过

## 2026-03-14 增补：FigurePanel AI Insight 验证

### 目标

- `FigurePanel.ai_insight` 不再来自模板 caption 复述
- `layout_uid_v1` 的 figure 节点可接入局部 AI refinement
- refinement 不影响 caption、source ids、evidence

### 计划验证

1. Backend unit
   - `figure_refinement` 的短 insight 能通过 normalize
   - `layout_uid_group_plan_to_panel_plan(...)` 能把 figure refinement 写入 `FigurePanel.props.ai_insight`

2. Runtime behavior
   - figure refinement 缺失时，`FigurePanel` 不再被 repair 阶段强行写入模板式 insight
   - equation/table 行为不受影响
