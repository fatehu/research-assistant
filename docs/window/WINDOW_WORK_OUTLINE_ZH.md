> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# Window 工作总纲

更新时间：2026-05-04

## 记录原则

- `docs/window` 是本轮 agent 上下文窗口维护的工作台账。
- 所有代码改动前，先在对应 `MOD_XX` 文件明确边界和验收。
- 每个修改项只维护一个主文件；总纲只做索引、状态和决策记录。
- 已完成实现后，必须回填实施记录和验证结果。

## 当前范围

本轮只覆盖 agent 系统上下文窗口相关维护：

- Function Calling 主链稳定性。
- XML fallback 收口。
- tool ledger 摘要从同步 qwen-turbo 热路径移出。
- qwen-turbo 会话级 compaction 的边界保留。
- heartbeat、异步维护、budget 确定性裁剪和观测作为后续阶段讨论。

不覆盖：

- UI 大改。
- 新 agent 架构重写。
- 安全加固。
- 非 Docker 运行环境优先级提升。

## 文档索引

### 总体方案

- `AGENT_CONTEXT_WINDOW_MAINTENANCE_PLAN_ZH.md`
  - 角色：总体路线和阶段拆分。
  - 状态：草案，待讨论。

- `AGENT_TOOL_LEDGER_QWEN_P0_PROPOSAL_ZH.md`
  - 角色：tool ledger qwen-turbo P0 的详细调研和方案依据。
  - 状态：草案，作为 `MOD_01` 的背景材料。

### 修改项文件

- `MOD_01_TOOL_LEDGER_DETERMINISTIC_SUMMARY_ZH.md`
  - 修改项：tool ledger 单次工具结果摘要确定性化。
  - 阶段：P0。
  - 状态：已实施，Docker API 回归通过。

- `MOD_02_COMPACTION_BOUNDARY_VERSION_ZH.md`
  - 修改项：compaction artifact 边界版本保护。
  - 阶段：P1。
  - 状态：已实施，Docker focused 回归通过。

- `MOD_03_CHAT_HEARTBEAT_ZH.md`
  - 修改项：chat SSE heartbeat。
  - 阶段：P1。
  - 状态：已实施，Docker focused 回归通过。

- `MOD_04_ASYNC_CONTEXT_MAINTENANCE_ZH.md`
  - 修改项：异步 context refresh / old tool pair summary。
  - 阶段：P2。
  - 状态：已实施，Docker focused 与人工 skill 回归通过。

- `MOD_05_CONTEXT_BUDGET_OBSERVABILITY_ZH.md`
  - 修改项：预算、压缩触发、stale 跳过和确定性裁剪观测统一。
  - 阶段：P3。
  - 状态：已实施，Docker focused 回归通过。

- `MOD_06_PAPER_REPRODUCTION_PROJECT_ONLY_AUDIT_ZH.md`
  - 修改项：paper-reproduction project-only 路线与旧 workspace/notebook 残留清点。
  - 阶段：审阅项。
  - 状态：已实施收口，Docker focused 回归通过。

- `MOD_07_BUDGET_DETERMINISTIC_TRUNCATION_ZH.md`
  - 修改项：prompt budget 路径确定性裁剪，移除 `chat.budget.message_summary/message_truncation` qwen-turbo 热路径。
  - 阶段：P2。
  - 状态：已实施，Docker focused 与真实 API skill 回归通过。

- `MOD_08_PROJECT_TREE_DETERMINISTIC_COMPACT_ZH.md`
  - 修改项：`project_tree` 规则化 compact 输出和候选路径，移除目录树 qwen 整理。
  - 阶段：P2。
  - 状态：已实施，Docker focused、守卫和真实 Project 10 验证通过。

## 当前推荐顺序

1. 整理并提交本轮已完成的 `MOD_05` 变更。
2. 后续稳定性维护继续保持低风险、小范围、先文档后代码，不扩展到 notebook/codelab 产品面。

## 决策记录

- 2026-05-04：确认模型压缩不是问题本身；问题是 qwen-turbo 被放在 per-tool-result 同步热路径。
- 2026-05-04：确认 `tool_ledger` 不是首创概念，属于成熟 agent 系统中的工具事件事实层同构设计。
- 2026-05-04：当前倾向为 P0 采用 deterministic ledger summary，qwen-turbo 继续留在会话级 compaction。
- 2026-05-04：用户确认实施 `MOD_01`；已移除 per-tool-result qwen-turbo ledger summary 热路径，保留会话级 compaction。
- 2026-05-04：`MOD_01` Docker 真实 API 回归通过：paper status、literature review list、artifact read、artifact update failure 均保留关键 ledger 锚点。
- 2026-05-04：清点 paper-reproduction 路线：当前 FC 工具池只注册 project-only 工具；旧 `paper_research_*execution*` 类仍在代码中，但未注册到 chat 默认工具池，且依赖旧 `PaperExperimentWorkspace/notebook_id`，当前 Docker 数据库中 workspace/run 相关表为空。
- 2026-05-04：用户确认 `codelab/notebook` 是早期 notebook-agent/Jupyter 实验室路线，保留为独立产品面，不作为 paper-reproduction 主路径；后续论文复现固定采用 Project + Claude Code + sandbox。
- 2026-05-04：`MOD_06` 已收口：paper-reproduction skill 配置、chat workflow stage、ReAct workflow next-action/highlight、workflow binding 和 tool ledger 不再推进旧 execution/workspace 路线；codelab/notebook 未改动。
- 2026-05-04：`MOD_06` 主线回归通过：skill 资产防回退测试、ToolRegistry/skill 约束、chat workflow、Project reference/runtime、paper grounding 和 Docker 现场 `paper_research_status(paper_id=113)` 均通过。
- 2026-05-04：用户确认执行 `MOD_03` heartbeat；已在 `/chat/send` direct model stream 和 ReAct agent live event queue 上增加非事实层 SSE heartbeat，前端继续静默忽略，不污染 `item_stream` / `tool_ledger`。
- 2026-05-04：`MOD_02` 现状审阅完成：已有 `conversation_revision` 能保护 preview send_plan 复用，但 compact artifact 写入前缺少 source fingerprint 校验；核心风险是旧 `compact_boundary` 晚到并追加到 `item_stream` 末尾，导致 canonical history 折叠较新的 item。
- 2026-05-04：用户确认执行 `MOD_02`；已增加 item stream source fingerprint 和 runtime 条件提交，manual/background/pre-turn/mid-run compact stale 时跳过写回，不写 `context_state` / `compacted_history` / `compact_boundary`。
- 2026-05-04：`MOD_02` 明确不触及 qwen-turbo 生成逻辑；本次只保护模型压缩 artifact 的写回边界。ReAct stale skip 时也不会把 stale context_state 写入运行内存。
- 2026-05-04：`MOD_02` Docker focused 回归通过：runtime 条件提交、conversation compaction stale skip、ReAct mid-run stale skip 共 34 项通过；chat manual compact / context preview / send API 共 36 项通过；后端 broad-except 和 contract alignment 守卫通过。
- 2026-05-04：`MOD_04` 调研报告已补充，复用本轮外部仓库调研并对照当前代码。结论：下一步不应删除 qwen-turbo，而应先做 compaction task metadata、单进程 in-flight 去重，并讨论 pre-turn 是否后台优先；mid-run 暂保留同步兜底。
- 2026-05-04：`MOD_04` 执行方案已补充并按用户反馈收敛：不做默认关闭开关，不写无意义备用分支；pre-turn background-first 作为正式路径接入，第一版只接入实际执行的 `full_compaction` task，完成结构化队列、单进程 queued/running 去重、trigger/source 观测、Docker 与人工 skill 回归。
- 2026-05-04：`MOD_04` 已实施：background compaction queue 改为结构化 `ConversationCompactionTask`，增加单进程 queued/running 去重；pre-turn compaction 改为后台投递 `full_compaction`，不再同步等待 qwen-turbo；send/execution 完成路径补充 trigger/source。
- 2026-05-04：`MOD_04` Docker focused 回归通过：compaction service / ReAct context resilience / chat send 共 52 项通过；runtime service / manual compact / context preview / FC fallback 共 48 项通过；paper-reproduction skill assets / paper grounding 共 32 项通过；后端 broad-except 与 contract alignment 守卫通过。
- 2026-05-04：`MOD_04` 人工 skill 回归通过：Docker frontend 登录开发账号后查询 `paper_id=113`，工具调用走 `paper_research_status`，返回 `project_id=10`、reference bundle ready 和 `project_claude` 下一步，未退回 notebook/workspace/execution 路线。
- 2026-05-04：确认 budget message summary/truncation 的 qwen 调用属于 prompt 构造/预算裁剪热路径；参考仓库普遍把模型用于 session/history compaction，预算裁剪优先用确定性 head/tail、tail budget、tool output prune 或 offload。用户确认执行 `MOD_07`。
- 2026-05-04：`MOD_07` 已实施：ReAct prompt budget 摘要与消息裁剪改为本地 head/tail 确定性裁剪，移除 `chat.budget.message_summary/message_truncation` 和 `_compress_text_with_qwen_turbo` 源码入口；会话级 compaction 不变。
- 2026-05-04：`MOD_07` Docker focused 回归通过：budget/FC/ledger 共 44 项、compaction/chat 共 68 项、paper-reproduction skill/grounding 共 40 项；后端 broad-except 与 contract alignment 守卫通过。
- 2026-05-04：`MOD_07` 真实 API skill 回归通过：`conversation_id=199` 只产生 `paper_research_status` 工具结果，未调用旧 execution 工具，回答包含 Project ID 10 与 `project_claude` 下一步。
- 2026-05-04：复查 `chat/200`：当前 turn tool observation 未触发 budget 截断，但 `project_tree` 输出约 7335 tokens 且模型仍读取了不存在的 `FASTTEXT_REPRODUCTION_REPORT.md`。用户确认执行 `MOD_08`，将 `project_tree` 改为确定性 compact tree + candidate files，并移除 `project_tree.focused_tree` qwen 调用。
- 2026-05-04：`MOD_08` 已实施：`project_tree(project_id=10)` observation 降到 448 tokens，候选路径包含真实 `repo/source/FASTTEXT_REPRODUCTION_REPORT.md`；根路径读取失败时 `project_read_file` 会返回 suggested paths。
- 2026-05-05：真实 chat 页面验证 `project_tree(project_id=10)` 成功且只调用该工具；模型最终回答曾把 `Directory summary` examples 混入 Candidate Files 表达，因此补充字段边界标签，明确 examples 不是 candidate files。
- 2026-05-05：字段边界强化后，新建 `conversation_id=201` 真实 chat 回归通过：重新调用 `project_tree(project_id=10)`，工具结果 `output_tokens_estimate=518`、`truncated=false`，最终回答未再把 `data/` examples 混入 Candidate files。
- 2026-05-05：用户确认系统大方向已开发完成，后续只做稳定性维护加固，不动 notebook/codelab/notebook-agent 产品面，不把其作为 paper-reproduction fallback。
- 2026-05-05：执行 `MOD_05`：统一 context debug / history event / logs 的上下文观测字段，新增 `context_observability_events`、before/after token、deterministic truncation reason、model compaction committed/skipped/failed 事件；不改变压缩策略、不增加模型调用。
- 2026-05-05：`MOD_05` Docker focused 回归通过：context budget / runtime resilience / compaction / runtime service 共 54 项，chat send / manual compact / FC fallback 共 54 项，broad-except 与 contract alignment 守卫通过。

## 下一步

- 下一步整理本轮 `MOD_05` 变更，按功能边界提交。
- paper-reproduction 后续只围绕 Project + Claude Code + sandbox 做回归和维护，不把 codelab/notebook 当作 fallback。
- 旧 `paper_research_*execution*` 类仍作为未注册 legacy 代码保留；如需彻底删除模型/服务代码，另开清理项并先确认迁移边界。
