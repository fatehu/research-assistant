# 论文阅读 Experience Staged Runtime 骨架测试记录

时间：2026-03-15 15:44

## 目标

验证 `/experience` / `/workbench` 的 staged runtime 实执行：

- `planning_brief`
- `tool_budget`
- `planner_output`
- `tool_enrichment_packet`
- `runtime_stage_trace`
- workbench 可视化

## 预期检查

### backend

- staged runtime 执行顺序为：
  - `planner`
  - `tool_enricher`
  - `page_generation`
- `page_brief.body_flow_target_ids` 会保留当前页有序正文 target，且不会在 contract 校验时丢失
- `planning_brief / tool_budget / planner_output / tool_enrichment_packet / runtime_stage_trace`
  会进入 `generative_plan.meta`
- `experience_plan.meta` 会保留并扩展该阶段信息
- tool/enricher 会真正执行：
  - native/public-web budget
  - duplicate-query suppression
  - per-tool timeout

### frontend

- `/experience` 的页面生成细节可看到：
  - `Planning Brief`
  - `Tool Budget`
  - `Planner Output`
  - `Tool Enrichment`
  - `Runtime Stages`
- `/workbench` 的 debug 面板可看到：
  - `Page Dossier`
  - `Planning Brief`
  - `Tool Budget`
  - `Planner Output`
  - `Adjacent Page Context`
  - `Tool Enrichment Packet`
  - `Runtime Stages`
  - `Tool Trace`

## 命令

- `python3 -m py_compile backend/app/services/generative_reader_agent_runtime.py backend/app/api/literature.py`
- `python3 -m py_compile backend/app/services/generative_reader_agent_runtime.py backend/app/schemas/literature.py backend/tests/test_generative_reader_agent_runtime.py`
- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q`
- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan_should_build_and_cache_experience_plan or experience_plan_cached_should_derive_experience_when_generative_plan_exists" -q`
- `npm --prefix frontend run lint -- --quiet`
- `cd frontend && npx tsc --noEmit`

## 结果

- `py_compile`：通过
- runtime 定向 pytest：`36 passed`
- `/experience` API 定向 pytest：`2 passed`
- frontend lint：通过
- frontend `tsc --noEmit`：通过

## 结论

- staged runtime 已从单一 opaque agent 阶段拆成：
  - `planner`
  - `tool_enricher`
  - `page_generation`
- `reading_flow` 现在默认以当前页 `body_flow_target_ids` 为骨架，而不是只保留少量 focus/support targets
- `ReaderPageBrief` contract 已正式包含 `body_flow_target_ids`
- `planning_brief / tool_budget / planner_output / tool_enrichment_packet / runtime_stage_trace`
  已进入 `generative_plan.meta`，并在 `experience_plan.meta` 中保留
- `/experience` 与 `/workbench` 已能直接显示：
  - `Planning Brief`
  - `Tool Budget`
  - `Planner Output`
  - `Tool Enrichment`
  - `Runtime Stages`
  - 邻页结构化上下文与 `page_dossier`
  - `Tool Trace`
- tool/enricher 不再是“planner 说了算就执行”，而是有 deterministic budget guardrails。
