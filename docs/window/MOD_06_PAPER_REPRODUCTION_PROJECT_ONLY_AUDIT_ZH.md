> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 06：paper-reproduction project-only 路线与旧 workspace/notebook 残留清点

状态：已实施收口，Docker focused 回归通过

日期：2026-05-04

## 用户确认边界

2026-05-04 用户确认：

- `codelab/notebook` 原本是 notebook-agent 类 Cursor 的 Jupyter 实验室设计。
- 早期曾计划用它承接论文复现，但该路线难度和复杂度过高，已经放弃作为 paper-reproduction 主路径。
- 后续论文复现工作采用当前 Project + Claude Code + sandbox 路线。
- 不动 `codelab/notebook` 产品面；只要求 paper-reproduction skill 业务边界不要再和旧 notebook/workspace 混淆。

## 背景

用户指出：当前论文复现代码应该已经以 Project 作为工作单元，不应再依赖旧的 workspace/notebook 机制。本文件只做清点和边界判断，不做代码删除。

## 当前结论

当前 paper-reproduction 的主路径已经是 Project-only：

- `paper_research_prepare`：创建/复用 Project，并生成 `/app/uploads/projects/{project_id}/reference/`。
- `paper_research_status`：按 `paper_id` 或 `project_id` 定位 Project，只读检查 reference bundle。
- `project_tree`、`project_read_file`、`project_write_file`、`project_bash`、`project_claude`：全部以 `/app/uploads/projects/{project_id}` 为根目录。
- `.agents/skills/paper-reproduction/SKILL.md` 明确要求：prepare 完成后，把实际复现工作交给 `project_claude`，不要主 agent 自行运行训练或调试。

旧 workspace/notebook 复现机制没有完全删除，但已经不在当前 chat FC 主工具池中：

- `PaperResearchWriteExecutionSpecTool`
- `PaperResearchLaunchClaudeCodeTool`
- `PaperResearchWriteExecutionScriptTool`
- `PaperResearchReadExecutionSpecTool`
- `PaperResearchStartExecutionTool`
- `PaperResearchReadExecutionTool`
- `PaperResearchTailExecutionLogTool`
- `PaperResearchCancelExecutionTool`

这些类仍定义在 `backend/app/services/agent_tools_impl/registry.py`，但 `DefaultToolProvider.build_default_tools()` 当前没有注册它们。Docker 内实际 `ToolRegistry.list_tools()` 只看到以下 paper/project 工具：

- `paper_search`
- `project_tree`
- `project_read_file`
- `project_write_file`
- `project_bash`
- `project_claude`
- `paper_research_prepare`
- `paper_research_status`
- `paper_research_search_project_zoekt`
- `paper_research_probe_repo`
- `paper_research_probe_url`

## Skill 业务残留二次确认

2026-05-04 复查 `paper-reproduction` skill 业务层：

- `.agents/skills/paper-reproduction/SKILL.md` 没有 `notebook`、`workspace`、`codelab` 复现路线说明。
- `.agents/skills/paper-reproduction/skill.yaml` 的业务约束是 Project-first，并明确把代码修改、命令执行、调试、训练运行和复现交付交给 `project_claude`。
- `.agents/skills/paper-reproduction/agents/openai.yaml` 的默认 prompt 也是 Project + `project_claude` 路线。

因此，skill 文档本体没有 codelab/notebook 路线残留。

实施前仍存在的残留主要在运行时 skill/agent 支撑层：

- `skill.yaml` 的 `blocked_tool_names` 仍列出旧 `paper_research_write_execution_script`、`paper_research_write_execution_spec`、`paper_research_start_execution`。这不是主路径入口，而是为了阻止主 agent 误用旧 execution 路线。
- `backend/app/services/react_agent.py` 仍有旧 execution 工具名的 decision action、workflow next action、highlight 和重复读取控制。
- `backend/app/api/chat.py` 仍有旧 execution 工具名到阶段的映射。
- `react_agent.py` 的 workflow binding / ledger anchor 仍保留 `workspace_id`、`notebook_id` 字段，属于历史兼容字段，当前 paper-reproduction project-only 路线不应依赖它们。

处理判断已落地为代码收口：

- `skill.yaml` 的 `blocked_tool_names` 只保留 `project_bash`、`project_write_file`，不再把旧 execution 工具名写进 skill 业务配置。
- `react_agent.py` 内部仍保留 `legacy execution` 隐藏集合，用于防止旧工具被意外重新暴露，但不作为 skill 正向业务说明。
- `react_agent.py` 不再建议 `start_execution/read_execution`，旧 execution 工具结果只会被识别为 `legacy_execution_route` 并收束为报告阻塞。
- `chat.py` 不再把旧 execution 工具名或 `executions/` 路径推断为 paper workflow execution 阶段。
- paper-reproduction 的 workflow binding 新写入只保留 `paper_id/project_id/current_stage/current_draft_id`；不再从工具结果写入 `workspace_id/notebook_id/baseline_execution_id/tuning_execution_id`。
- tool ledger 对 paper/project 工具不再提升 `workspace_id/notebook_id/background_execution_id/background_stage/background_status` 等旧锚点。

## 旧机制残留

### 代码层残留

`_PaperResearchToolBase._resolve_project_workspace()` 仍会通过 `ProjectService.get_project_payload()` 读取 `primary_workspace` 或 `workspaces`，再查 `PaperExperimentWorkspace`。

但 `ProjectService._serialize_project()` 当前强制返回：

- `primary_workspace_id: None`
- `primary_workspace: None`
- `workspaces: []`
- `workspace_count: 0`

因此旧 execution 类即使被重新注册，也会在当前 project-only 数据结构下走到 `workspace_not_ready`，而不是进入真实执行。

### 数据模型残留

`backend/app/models/literature.py` 仍包含：

- `PaperExperimentWorkspace`
- `PaperExperimentRun`
- `research_project_workspaces`
- `ResearchProject.primary_workspace_id`
- `ResearchProject.primary_workspace`
- `ResearchProject.workspaces`

这些是历史表/字段，不能直接等同于当前 paper-reproduction 主路径依赖。

### Agent 状态提示残留

`backend/app/api/chat.py` 和 `backend/app/services/react_agent.py` 仍有旧 execution stage、workflow next action、tool highlight、decision action 映射，例如：

- `paper_research_write_execution_spec`
- `paper_research_start_execution`
- `paper_research_read_execution`
- `paper_research_launch_claude_code`

这些残留不会让 FC 工具池自动暴露旧工具，但会造成维护认知混淆，也可能影响历史 workflow 状态展示或测试夹具。

### codelab/notebook 产品面

`backend/app/api/codelab.py`、`backend/app/api/codelab_agent.py`、`backend/app/api/notebook_agent.py`、`backend/app/services/notebook_workspace_service.py` 仍是独立 notebook/codelab 功能面。

本次不建议把它们和 paper-reproduction 的旧 workspace 机制混为一类删除，因为它们仍有专门 API、测试和用户数据：

- 当前 Docker 数据库中 `notebooks` 表有 49 条记录。
- `/app/uploads/codelab/notebooks` 占约 76 MB。
- user 1 下仍有多个 notebook，包括 `LeCun 1989 Notebook Reproduction`。

## Docker 现场

在当前 Docker 环境中：

- `research_projects`：user 1 只有 Project 10，标题为 `Bag of Tricks for Efficient Text Classification - Research Project`，`primary_paper_id=113`，`primary_workspace_id=None`。
- `research_project_workspaces`：空。
- `paper_experiment_workspaces`：空。
- `paper_experiment_runs`：空。
- `/app/uploads` 下未发现 `execution_spec.json`、`execution_result.json`、`execution.log`。
- `/app/uploads/projects/10` 约 1.4 GB，其中 `repo/source` 约 1.4 GB，`data` 约 48 MB，`reference` 约 64 KB。

这说明用户记忆中的 `paper execution running` 不在当前 DB/文件现场里；如果曾经运行过，可能是旧环境、旧数据卷、runtime-worker 内存态，或已经清理掉的路径。

## 风险判断

当前主路径稳定性：

- Project-only prepare/status/inspection/Claude worker 路线是当前实际路线。
- FC 工具池不会把旧 execution 工具暴露给正常 chat 主链。
- paper-reproduction skill 也通过 `skill.yaml` 和 ReActAgent 逻辑阻止主 agent 使用 `project_bash`、`project_write_file`、旧 execution spec/start 作为 `project_claude` 的替代 worker。

当前不足：

- 旧 execution 类仍留在 `registry.py`，读代码时会误导维护者以为这是仍可用主路径。
- stage/workflow 提示里仍把旧 execution 当成路径之一。
- `PaperExperimentWorkspace`/`PaperExperimentRun` 数据模型仍存在，但 ProjectService 已经把 workspace 序列化压平为空，语义分裂。
- codelab/notebook 是独立功能面，不能因为 paper-reproduction project-only 就直接删除，需要另行判断产品是否保留。

## 建议方案

建议先做小范围收口，不直接删除 codelab/notebook：

1. 文档上把旧 execution 工具标记为 legacy-unregistered。
2. 在代码注释或命名上标明旧 execution 类不是 project-only 主路径，避免后续误用。
3. 从 `chat.py` / `react_agent.py` 中移除或降级旧 execution 的 workflow next-action 暗示，避免 agent 状态层继续提示 `start_execution/read_execution`。
4. 保留 `PaperExperimentWorkspace` / `PaperExperimentRun` 模型，直到确认没有迁移/历史数据/管理后台依赖。
5. codelab/notebook 功能面暂不删除，只单独建清理议题。
6. paper-reproduction skill 的业务定义固定为 Project + Claude Code + sandbox，不再把 codelab/notebook 作为复现工作流 fallback 或替代实现。

如果后续确实需要“托管执行”能力，建议新建 Project-native execution，而不是复活旧 workspace：

- 根目录固定为 `/app/uploads/projects/{project_id}`。
- execution 文件落到 `projects/{project_id}/executions/{execution_id}/`。
- 不再依赖 `notebook_id`、`PaperExperimentWorkspace` 或 `research_project_workspaces`。
- 与 `project_claude` 的职责保持清楚：`project_claude` 负责开放式代码实施，Project-native execution 只负责已确定命令的托管运行和状态读取。

## 验收标准

若进入代码实施，最低验收为：

- `ToolRegistry.list_tools()` 中仍只暴露当前 project-only 工具，不重新暴露旧 execution 工具。
- paper-reproduction 激活时，`project_bash`、`project_write_file`、旧 execution write/start 仍被隐藏或执行阻断。
- `paper_research_prepare` / `paper_research_status` 的 Docker API 回归仍通过。
- 不影响 codelab/notebook API 与既有测试。

## 实施记录

2026-05-04 已实施：

- `.agents/skills/paper-reproduction/skill.yaml`
  - 从 `blocked_tool_names` 移除旧 execution 工具名。
  - 保留 `project_bash`、`project_write_file` 作为 paper-reproduction 主 agent 自行工作的阻断项。
- `backend/app/api/chat.py`
  - 从 paper tool stage hints 移除旧 execution 工具映射。
  - `executions/` 路径不再自动推断为 paper workflow execution 阶段。
  - observation 阶段状态不再对旧 execution start/read/cancel 做特殊推进。
- `backend/app/services/react_agent.py`
  - 把旧 execution 工具名改为内部 legacy hidden set。
  - 从用户可见 scope reminder、decision action、workflow next action、workflow highlight、重复读控制中移除旧 execution 推进语义。
  - legacy execution 结果只作为 `legacy_execution_route` 收束，不再引导检查 execution spec 或继续 read/start execution。
  - paper-reproduction 新 workflow binding 不再写入 workspace/notebook/execution id。
  - paper/project tool ledger 不再提升 workspace/notebook/background execution 锚点。
- 测试同步：
  - 旧 execution spec 失败测试改为验证 `report_blocker + legacy_execution_route`。
  - ledger 测试改为验证不再提升 background execution 锚点。

## 验证结果

Docker focused 回归通过：

- `python -m pytest tests/test_react_agent_citation_policy.py -q`：53 passed
- `python -m pytest tests/test_agent_function_calling_fallback.py -q`：25 passed
- `python -m pytest tests/test_agent_tool_ledger_summary.py -q`：4 passed
- `python -m pytest tests/test_conversation_context_compaction_service.py -q`：9 passed
- `python -m pytest tests/test_chat_send_api.py -q`：18 passed
- `python -m pytest tests/test_agent_skill_service.py -q`：8 passed

2026-05-04 追加主线回归：

- 新增 `tests/test_paper_reproduction_skill_assets.py` 断言：paper-reproduction skill 资产不能重新出现 `notebook/workspace/codelab` 或旧 `paper_research_*execution*` 业务词。
- `python -m pytest tests/test_paper_reproduction_skill_assets.py -q`：2 passed
- `python -m pytest tests/test_tool_registry_knowledge_search.py tests/test_react_agent_citation_policy.py tests/test_chat_send_api.py -q`：86 passed
- `python -m pytest tests/test_project_reference_builder_service.py tests/test_project_runtime_service.py tests/test_project_runtime_overview.py -q`：27 passed
- `python -m pytest tests/test_paper_grounding_tools.py -q`：30 passed
- `python backend/checks/check_contract_alignment.py`：passed
- `python backend/checks/check_no_new_broad_excepts.py`：passed

Docker 现场检查：

- `paper-reproduction` skill 的 `blocked_tool_names` 为 `project_bash`、`project_write_file`。
- 当前 chat `ToolRegistry.list_tools()` 中 paper/project 工具为 `paper_search`、`project_tree`、`project_read_file`、`project_write_file`、`project_bash`、`project_claude`、`paper_research_prepare`、`paper_research_status`、`paper_research_search_project_zoekt`、`paper_research_probe_repo`、`paper_research_probe_url`。
- 旧 `paper_research_*execution*` 工具未出现在 chat 工具池。
- `paper_research_status(paper_id=113)` 成功，解析到 `project_id=10`，`reference_ready=True`，Project payload 未暴露 workspace/notebook keys。

## 暂不做

- 不删除 `notebooks` 表和 codelab API。
- 不删除 `/app/uploads/codelab/notebooks` 现场数据。
- 不修改 codelab/notebook 产品面。
- 不重写 ProjectRuntimeService。
- 不把旧 workspace execution 临时接回主路径。
