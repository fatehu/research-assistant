# Tool Registry 审计与修复方案

日期：2026-05-06

本文记录本轮对工具体系的验证结果、CodeLab 与知识库工具的边界判断，以及后续修复方案。本文只描述方案，不包含代码改动。

## 目标

本轮修复的目标不是新增能力，而是把现有工具池收敛到更清晰的状态：

- 明确哪些工具仍是当前主路径。
- 明确哪些工具是旧流程残留或 legacy-hidden。
- 明确 CodeLab / Notebook / Chat / Paper reproduction 的工具边界。
- 修复工具选择合同漂移，避免流程专用工具被错误暴露或错误隐藏。
- 建立后续验收命令，确保修复不会破坏现有主链路。

## 当前工具分层

当前系统至少有两层工具概念：

1. Notebook / CodeLab 操作工具
   - 代表工具：`notebook_execute`、`notebook_cell`、`notebook_variables`、`notebook_cleanup`、`pip_install`、`code_analysis`、`error_diagnosis`。
   - 这些工具面向 notebook cell、变量、内核、安装包和代码诊断。
   - 它们本身不需要知识库。

2. AgentCore 工具注册池
   - 入口：`backend/app/services/agent_tools_impl/registry.py` 的 `ToolRegistry`。
   - Chat、CodeLab Agent、Notebook Agent 都不同程度复用它。
   - 这里会注册通用工具、知识库工具、文献工具、DOCX 工具、Project/Paper 工具、MCP 工具等。

因此，CodeLab 出现 `knowledge_search` 的直接原因不是 notebook 执行工具依赖知识库，而是 CodeLab Agent 复用了统一 `ToolRegistry`。

## Docker 验证摘录

本轮验证均以 Docker 内结果为准。

服务状态：

- `backend` 可访问 `http://localhost:8888/docs`。
- `postgres` healthy。
- `redis` healthy。
- `frontend` 可启动。
- `codelab-runner` 可启动。
- `pdf-hybrid-backend` 可启动。

注册池盘点：

- Chat 本地注册工具数：31。
- CodeLab 本地注册工具数：14。
- CodeLab + Notebook 上下文注册工具数：21。
- MCP warmup：`enabled=true`，`status=ready`，发现 18 个 MCP 工具。

MCP 工具来源：

- `mcp.tavily.*`
- `mcp.firecrawl.*`

关键测试结果：

- Tool architecture / Notebook / MCP 注册组：`60 passed, 1 failed`。
- Paper / DOCX / Literature review / MCP / Skill：`52 passed`。
- ReAct / legacy hidden / chat tool ledger：`102 passed`。
- Reader composed + parallel 风险组：`3 failed, 1 passed`。
- `check_no_new_broad_excepts.py`：passed。
- `check_contract_alignment.py`：passed。
- Frontend lint：0 errors，1 warning。

## 当前注册工具分组

当前注册到 chat/codelab/notebook 工具池的本地工具按前缀可分为：

- `activate_*`
  - `activate_skill`
- `document_artifact_*`
  - `document_artifact_read`
  - `document_artifact_update_block`
  - `document_artifact_update_blocks`
- `docx_*`
  - `docx_generate_with_claude`
  - `docx_refine_with_claude`
- `knowledge_*`
  - `knowledge_search`
- `literature_*`
  - `literature_search`
- `literature_review_*`
  - `literature_review_start`
  - `literature_review_download_pdf`
  - `literature_review_read`
  - `literature_review_search_zoekt`
  - `literature_review_pdf_to_markdown`
- `notebook_*`
  - `notebook_execute`
  - `notebook_variables`
  - `notebook_cell`
  - `notebook_cleanup`
- `paper_research_*`
  - `paper_research_prepare`
  - `paper_research_status`
  - `paper_research_search_project_zoekt`
  - `paper_research_probe_repo`
  - `paper_research_probe_url`
- `project_*`
  - `project_tree`
  - `project_read_file`
  - `project_write_file`
  - `project_bash`
  - `project_claude`
- `web_*`
  - `web_search`
  - `web_scrape`
- 其他通用工具
  - `calculator`
  - `datetime`
  - `text_analysis`
  - `unit_converter`
  - `paper_search`
  - `review_writer`
  - `pip_install`
  - `code_analysis`
  - `error_diagnosis`

## 疑似废弃或旧流程残留

以下工具类仍存在于 `backend/app/services/agent_tools_impl/registry.py`，但没有注册进当前 chat/codelab/notebook 本地工具池：

- `paper_research_assess_repo_mainpath`
- `paper_research_git_status`
- `paper_research_git_diff`
- `paper_research_git_log`
- `paper_research_git_show`
- `paper_research_inspect_runtime`
- `paper_research_write_execution_spec`
- `paper_research_launch_claude_code`
- `paper_research_write_execution_script`
- `paper_research_read_execution_spec`
- `paper_research_start_execution`
- `paper_research_read_execution`
- `paper_research_tail_execution_log`
- `paper_research_cancel_execution`

这些工具不是完全没有引用：

- `react_agent.py` 中仍有 legacy hidden set。
- 少量 workflow/stage 映射仍提到旧 execution 路线。
- 若干测试和窗口文档仍使用这些名称验证 legacy 行为。
- `docs/window/MOD_06_PAPER_REPRODUCTION_PROJECT_ONLY_AUDIT_ZH.md` 已明确指出旧 execution 路线不是当前 paper reproduction 主路径。

初步判断：

- 这些工具应视为 `legacy-unregistered`。
- 它们不应重新暴露给普通 Chat 或 CodeLab。
- 它们可以短期保留为兼容历史状态和测试夹具，但需要明确标记，避免维护者误以为仍是主路径。

## CodeLab 与知识库是否串线

结论：有边界串味，但不是完全误接。

合理部分：

- CodeLab Agent 使用 `route_profile="codelab"` 创建 `ToolRegistry`。
- `ToolRegistry` 在有 `db/db_session_factory + user_id` 时会注册 `knowledge_search`。
- 现有测试 `test_tool_registry_registers_knowledge_search_only_when_db_available` 明确断言 CodeLab profile 下会注册 `knowledge_search`，但不会注册 paper/project 工具。
- 这说明设计意图可能是：CodeLab Agent 在用户显式要求查知识库时，可以使用知识库作为辅助资料来源。

不干净部分：

- Notebook / CodeLab 纯执行工具本身不需要知识库。
- CodeLab Agent 的工具池把 notebook 工具和统一 AgentCore 工具挂在同一个注册器里，边界偏宽。
- `notebook_agent.py` 的旧 `NotebookToolRegistry` 更可疑：它调用 `ToolRegistry` 时没有传 `route_profile="codelab"`，之后再手动注册 notebook 工具，容易继承默认 chat 工具池语义。
- 当前选择器合同漂移：`_CODELAB_INTENT_TOOL_MAP` 声明 `knowledge_query -> knowledge_search`，但实际 `_select_codelab_tool_names()` 只有在用户文本显式命中知识库关键词时才加入 `knowledge_search`。因此 `select_tool_names_for_intent("knowledge_query")` 不传文本时漏选 `knowledge_search`。

因此：

- CodeLab 支持“显式知识库查询”可以保留。
- CodeLab 默认 notebook 任务不应携带知识库。
- 旧 Notebook Agent 入口需要重点审计，避免继承 Chat 工具池。
- 选择器需要统一合同：要么 intent-only 可信，要么所有调用必须基于 user_text；不能两套规则互相打架。

## 当前红灯

### 1. CodeLab `knowledge_query` 选择器合同失败

失败测试：

- `backend/tests/test_tool_architecture_refactor.py::test_codelab_tool_selection_filters_tools_and_keeps_fallback`

现象：

- 调用 `select_tool_names_for_intent("knowledge_query")` 时，只返回 `datetime`、`calculator` 等 fallback 工具。
- `knowledge_search` 没有进入候选工具。

风险：

- 如果某条流程先把用户请求分类为 `knowledge_query`，再只传 intent 选工具，就会漏掉真正需要的知识库工具。

### 2. Reader composed integration 测试合同漂移

失败测试：

- `tests/integration/test_reader_composed_stream_workbench_v2.py::test_reader_composed_stream_workbench_v2_done_payload`
- `tests/integration/test_reader_composed_stream_workbench_v2.py::test_reader_composed_inline_query_disabled_contract`

现象：

- 测试仍按旧签名传入 `db=` 参数。
- 当前接口函数已不接收 `db`，而是在函数内部开 session。

风险：

- 更像测试合同漂移，不一定是产品功能失败。
- 但 integration 测试不再覆盖真实接口合同。

### 3. 并行工具延迟测试不稳定

失败测试：

- `backend/tests/test_agent_parallel_calls.py::test_parallel_tool_calls_reduce_latency`

现象：

- 产出数量正确：2 个 observation，1 个 done。
- 但耗时约 `0.99s`，不满足 `<0.5s` 的断言。

风险：

- 可能是真并行没有生效。
- 也可能是测试阈值过紧，受 LLM mock 两轮迭代、日志和容器调度影响。
- 需要进一步确认工具调用阶段是否并行，而不是只用总耗时判断。

### 4. Chat 暴露完整工具池

现象：

- Chat profile 下 `select_tool_names_for_intent()` 不做意图收窄。
- 普通聊天、docx、paper、knowledge、code 意图都会得到同一组 31 个工具。

风险：

- 对流程专用工具较多的系统来说，完整暴露会增加误触概率。
- 尤其是 paper/project/docx/literature review 工具全部同池时，模型可能在非目标流程里看到不该使用的工具。

## 修复原则

1. 先收敛工具边界，再考虑删除旧代码。
2. 不把旧 execution 工具重新暴露给主工具池。
3. CodeLab 允许显式知识库查询，但默认 notebook 任务必须保持 notebook-first。
4. Chat 是否全量暴露工具，需要作为单独产品决策；短期可先通过 skill 和 route profile 限制高风险工具。
5. 所有修复必须配套 Docker 内回归测试。
6. 不在本轮直接删除模型、表、历史数据结构，除非另行确认。

## 建议修复方案

### Phase 1：文档和分类收敛

目标：

- 给每个工具分配状态标签。

建议状态：

- `active-global`
  - 通用工具，例如 `calculator`、`datetime`、`text_analysis`、`unit_converter`。
- `active-chat-flow`
  - Chat 中可用，但有明确流程语义，例如 `document_artifact_*`、`docx_*`。
- `active-codelab`
  - Notebook / CodeLab 专用工具，例如 `notebook_*`、`pip_install`、`code_analysis`。
- `active-paper-project`
  - 当前 paper reproduction 主路径工具，例如 `paper_research_prepare`、`paper_research_status`、`project_claude`、`project_tree`、`project_read_file`。
- `active-literature-review`
  - 文献综述工作流工具，例如 `literature_review_*`、`review_writer`。
- `optional-codelab-explicit`
  - CodeLab 中只有用户显式要求才启用的工具，例如 `knowledge_search`、`web_search`、`web_scrape`、`literature_search`。
- `legacy-unregistered`
  - 旧 execution 工具，例如 `paper_research_start_execution`、`paper_research_write_execution_spec`。
- `test-drift`
  - 当前主要是测试签名和实现不一致的 reader composed integration 测试。

验收：

- 本文档更新。
- 不改代码。

### Phase 2：修复 CodeLab 工具选择合同

目标：

- 统一 `intent` 与 `user_text` 的优先级。

可选方案 A：保持现有测试合同

- `select_tool_names_for_intent("knowledge_query")` 即使没有 user_text，也必须包含 `knowledge_search`。
- 显式 notebook/local-file 文本可以覆盖为 code task，避免误查知识库。

优点：

- 符合现有失败测试。
- intent-only 调用更可靠。

风险：

- 如果上游 intent 分类误判为 `knowledge_query`，会引入知识库工具。

可选方案 B：改为 user_text-first 合同

- CodeLab 只有显式用户文本命中知识库关键词时才加入 `knowledge_search`。
- 更新测试，明确 intent-only 在 CodeLab 中不可信。

优点：

- 更保守，减少 CodeLab 串线。

风险：

- 与当前 `_CODELAB_INTENT_TOOL_MAP` 和既有测试相冲突。
- 需要审计所有调用点，避免传 intent 不传文本。

推荐：

- 采用方案 A，但补充更强的本地 notebook 任务覆盖规则：
  - 如果 user_text 是上传数据集、当前 notebook、cell、python、plot、pandas/sklearn 等本地任务，即使 intent 传错，也强制 code task。
  - 如果 user_text 为空且 intent 是 `knowledge_query`，按 intent 选择 `knowledge_search`。

### Phase 3：收紧 Notebook Agent 旧入口

目标：

- 确认 `notebook_agent.py` 是否仍是产品入口。
- 如果仍使用，避免默认 chat 工具池串入 notebook。

建议：

- 将 `NotebookToolRegistry` 初始化路径与 `route_profile="codelab"` 对齐。
- 或者显式声明 notebook agent 只暴露 notebook 工具 + 少量 fallback。
- 保留知识库查询只作为显式可选入口。

验收：

- `notebook_agent` 不应默认暴露 paper/project/docx 工具。
- `codelab_agent` 与 `notebook_agent` 的工具池差异有明确文档说明。

### Phase 4：标记 legacy execution 工具

目标：

- 降低维护者误读成本。

建议：

- 不删除旧 execution 类。
- 在注册器或文档中标记为 `legacy-unregistered`。
- 保留 `react_agent.py` legacy hidden set。
- 移除或降级用户可见 workflow/stage 提示中对旧 execution 路线的暗示。

验收：

- `ToolRegistry.list_tools()` 不暴露旧 execution 工具。
- paper-reproduction skill 不引导 `start_execution/read_execution`。
- 旧 execution 仅作为历史状态、测试夹具、legacy summary 存在。

### Phase 5：Reader composed 测试合同修复

目标：

- 让 integration 测试重新覆盖真实接口合同。

建议：

- 更新测试调用方式，适配当前接口内部 session 模式。
- 或通过 dependency override/mock session factory，而不是直接传 `db=`。

验收：

- `tests/integration/test_reader_composed_stream_workbench_v2.py` 通过。
- 不改变 reader composed runtime 行为，除非另行发现产品 bug。

### Phase 6：并行工具测试重写为阶段性断言

目标：

- 判断工具调用是否并行，而不是被容器调度总耗时误伤。

建议：

- 测试中记录每个工具的 start/end 时间。
- 断言两个 `parallel_safe` 工具的执行窗口有重叠。
- 总耗时阈值放宽或删除。

验收：

- `test_agent_parallel_calls.py` 在 Docker 中稳定通过。
- 如果没有重叠，再进入代码层修复并行逻辑。

## 建议验收命令

工具选择 / Notebook / MCP：

```powershell
docker compose exec -T backend python -m pytest `
  tests/test_tool_architecture_refactor.py `
  tests/test_tool_registry_knowledge_search.py `
  tests/test_tool_registry_mcp_bridge.py `
  tests/test_notebook_execute_tool.py `
  tests/test_notebook_cell_tool.py -q
```

Paper / DOCX / Literature review / Skill / MCP：

```powershell
docker compose exec -T backend python -m pytest `
  tests/test_paper_grounding_tools.py `
  tests/test_paper_reproduction_skill_assets.py `
  tests/test_agent_skill_service.py `
  tests/test_internal_mcp_servers.py `
  tests/test_mcp_config.py `
  tests/test_mcp_templates.py -q
```

ReAct / legacy hidden / chat tool ledger：

```powershell
docker compose exec -T backend python -m pytest `
  tests/test_react_agent_citation_policy.py `
  tests/test_agent_function_calling_fallback.py `
  tests/test_chat_send_api.py `
  tests/test_agent_tool_ledger_summary.py -q
```

Reader composed + parallel 风险组：

```powershell
docker compose exec -T backend python -m pytest `
  tests/integration/test_reader_composed_stream_workbench_v2.py `
  tests/test_agent_parallel_calls.py -q
```

静态检查：

```powershell
docker run --rm -v "${PWD}:/workspace" -w /workspace research-assistant-backend:latest `
  python backend/checks/check_no_new_broad_excepts.py

docker run --rm -v "${PWD}:/workspace" -w /workspace research-assistant-backend:latest `
  python backend/checks/check_contract_alignment.py
```

前端 lint：

```powershell
docker compose exec -T frontend npm run lint
```

## 本轮建议的第一批修复顺序

推荐先做最小闭环：

1. 修复 CodeLab `knowledge_query` 选择器合同。
2. 审计并收紧 `notebook_agent.py` 的工具池 profile。
3. 给 legacy execution 工具加明确分类，不重新暴露。
4. 修复 reader composed integration 测试签名漂移。
5. 重写 parallel 测试断言，确认是否真并行。

暂不建议：

- 直接删除 legacy execution 类。
- 直接删除 Notebook / CodeLab 独立产品面。
- 直接把 Chat 工具池改成强 intent-filtering，除非另开一轮产品行为评审。

## 待确认问题

1. CodeLab 是否允许显式查知识库？
   - 推荐：允许，但必须显式。

2. `notebook_agent.py` 是否仍是线上入口？
   - 如果是，需要优先修。
   - 如果不是，应标记为 legacy route 或准备下线计划。

3. Chat 是否继续完整暴露 31 个工具？
   - 短期可保留。
   - 中期建议按 skill / route / user intent 做收窄。

4. 旧 `paper_research_* execution` 类是否只保留一轮兼容期？
   - 推荐：先标记 legacy，不删除。
