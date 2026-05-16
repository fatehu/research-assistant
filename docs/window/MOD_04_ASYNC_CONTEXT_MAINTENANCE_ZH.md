> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 04: 异步上下文维护调研报告

阶段：P2

状态：已实施，Docker focused 与人工 skill 回归通过

更新时间：2026-05-04

## 本报告目的

本报告复用本轮会话前面已经完成的上下文窗口调研，避免重新消耗 token 做同一轮外部仓库分析。它只回答下一阶段 `MOD_04` 的准备问题：

- 当前系统里 qwen-turbo 还在哪些上下文路径被调用。
- 哪些调用属于“会话级压缩”，哪些属于“主 prompt 构造热路径”。
- 现有 background worker 能不能承接异步 compaction。
- 参考仓库给出的稳定边界是什么。
- 下一步有哪些可选改造路线，以及实施前还需要哪些决策。

## 已复用的历史调研结论

完整背景见：

- `AGENT_CONTEXT_WINDOW_MAINTENANCE_PLAN_ZH.md`
- `AGENT_TOOL_LEDGER_QWEN_P0_PROPOSAL_ZH.md`
- `MOD_02_COMPACTION_BOUNDARY_VERSION_ZH.md`

可直接复用的结论：

- 模型做上下文压缩是主流方案，不需要否定 qwen-turbo 会话级 compaction。
- 成熟实现的关键差异不是“是否用模型”，而是“模型摘要是否阻塞核心 agent 执行路径”。
- `tool_ledger` 应该是事实账本和检索索引，不应该逐条工具结果同步调用模型生成自然语言摘要。
- 会话级 compaction artifact 必须带来源边界；`MOD_02` 已经补了 source fingerprint 和条件写回。
- 异步或耗时 compaction 完成后必须检查 stale；旧结果不能覆盖新 `item_stream`。

## 当前代码现状

### 1. 会话级 compaction

入口：

- `ConversationContextCompactionService._extract_context_state()`
  - source: `chat_compaction.context_state`
  - 模型：`settings.agent_context_state_provider/model`
  - 当前默认：`aliyun / qwen-turbo`

- `ConversationContextCompactionService._extract_compacted_history()`
  - source: `chat_compaction.compacted_history`
  - 模型：同上

- `ConversationContextCompactionService.build_artifacts()`
  - 当前串行执行 `_extract_context_state()` 和 `_extract_compacted_history()`。
  - 也就是说，一次 compaction 最多会连续等待两次 qwen-turbo 调用。

触发路径：

- manual compact：`/chat/conversations/{id}/compact` 调用 `compact_now()`，同步等待。
- background compact：`enqueue_conversation()` 投递到内存队列，由 `_worker()` 异步执行。
- ReAct pre-turn compact：`ReActAgent._maybe_pre_turn_compact()` 在下一轮发给主模型前同步执行。
- ReAct mid-run compact：`ReActAgent._maybe_mid_run_compact()` 在 run 内 token 压力或 context truncation 后同步执行。

已完成保护：

- `MOD_02` 已将 manual/background/pre-turn/mid-run 写回统一到 `commit_conversation_compaction_if_current()`。
- stale 时跳过写回，不写 `context_state` / `compacted_history` / `compact_boundary`。

### 2. background worker 能力

现有 worker：

- `ConversationContextCompactionService.start_background_worker()`
  - FastAPI startup 时启动。
  - `conversation_context_compaction_enabled=True` 时生效。

- `ConversationContextCompactionService.enqueue_conversation(conversation_id)`
  - 使用内存 `asyncio.Queue[int]`。
  - `_queued_ids` 能防止同一个 conversation 重复排队。

- `_worker()`
  - 消费 conversation id。
  - 执行 `_compact_conversation(conversation_id, mode="auto")`。
  - 异常只记录日志，不抛给用户请求。

现有限制：

- `_queued_ids` 只防止排队重复，不覆盖“正在运行中”的更细粒度状态。
- 队列只存 conversation id，没有任务类型、触发原因、source fingerprint、优先级、deadline。
- worker 是进程内内存队列，Docker 单 backend 实例下可用；如果未来多 backend 副本，不能天然做到跨进程去重。
- 当前 worker 仍调用同一个 `_compact_conversation()`，会同时做 context_state 和 compacted_history 两个模型提炼。

### 3. ReAct 同步 compaction

当前 ReAct 主链仍有同步 compaction：

- pre-turn：
  - 先估算 candidate rows 和 token pressure。
  - 满足阈值后调用 `ConversationContextCompactionService.build_artifacts()`。
  - 成功后写回并刷新 `context.messages`。

- mid-run：
  - 触发条件是 `context.context_truncated` 或 token pressure。
  - 达到 `agent_mid_run_compaction_min_iteration` 后最多每 run 执行 `agent_mid_run_compaction_max_per_run` 次。
  - 成功后刷新当前运行上下文。

风险边界：

- pre-turn 阶段同步等待 qwen-turbo 会增加下一轮首 token 延迟。
- mid-run 阶段同步等待 qwen-turbo 会卡住当前 agent 循环。
- `MOD_03` heartbeat 可以降低用户误判断流，但不能减少实际等待。
- `MOD_02` 只能防止 stale artifact 写回，不能避免重复或过多 compaction 启动。

### 4. budget 压缩路径（实施前风险，已由 MOD_07 收口）

`MOD_04` 调研时，除了会话级 compaction，还有 budget 文本压缩：

- `ReActAgent._compress_text_with_qwen_turbo()`
  - source: `chat.budget.message_summary`
  - source: `chat.budget.message_truncation`
  - 用于 `_build_system_compression_message()` 和 `_truncate_message_content_to_token_budget()`。

这条路径不是 `tool_ledger`，也不是 `context_state/compacted_history`，但它发生在 prompt 构造和 budget 裁剪阶段，仍可能影响主链延迟。

后续 `MOD_07` 已将这条路径改为本地 head/tail 确定性裁剪，并移除 ReAct budget helper 中的 `_compress_text_with_qwen_turbo()` 源码入口。

初步判断：

- 它应该作为 `MOD_04` 的风险项记录。
- 但是否本轮一起改，需要单独决策；否则 MOD_04 会从“会话级 compaction 调度”扩大成“所有 qwen budget 压缩清理”。

### 5. project 工具里的 qwen 压缩

`agent_tools_impl/registry.py` 中 `project_tree` 类工具有目录树整理器：

- 使用 `agent_budget_compression_provider`
- 模型写死为 `qwen-turbo`
- 有 timeout

它是具体工具内部的辅助摘要，不属于会话窗口核心 compaction。当前不建议并入 MOD_04，除非后续专门清理 project 工具体验。

## 外部参考实现要点

本轮已克隆或缓存到 `tmp/external-context-repos` / `tmp/reference-repos`，无需重新拉取。

### Aider

参考：

- `tmp/external-context-repos/aider/aider/history.py`
- `tmp/external-context-repos/aider/aider/coders/base_coder.py`

要点：

- `ChatSummary` 面向旧聊天历史，不是逐工具结果热路径。
- summarizer 可以后台线程执行。
- 后台 summary 结束后会比较 `summarizing_messages == done_messages`；如果期间历史变了，不用旧 summary 覆盖当前历史。

本项目对应：

- `MOD_02` 已吸收 stale 保护。
- `MOD_04` 可吸收“后台生成，完成后再条件应用”的调度思想。

### Goose

参考：

- `tmp/external-context-repos/goose/crates/goose/src/context_mgmt/mod.rs`

要点：

- `compact_messages()` 做会话级模型压缩。
- `maybe_summarize_tool_pairs()` 异步处理旧 tool request/response pair。
- `tool_ids_to_summarize()` 明确保护当前 turn / 最近 N 个 tool calls。

本项目对应：

- 旧工具对模型摘要如果要做，应该是 P2 后台批量任务，不是当前 turn 内同步任务。
- 不能覆盖 `tool_ledger` 原事实，只能追加 `tool_pair_summary` 或刷新 context_state evidence。

### Continue

参考：

- `tmp/external-context-repos/continue/extensions/cli/src/compaction.ts`
- `tmp/external-context-repos/continue/extensions/cli/src/stream/streamChatResponse.autoCompaction.ts`

要点：

- `compactChatHistory()` 是 session history compaction。
- auto-compaction 失败时继续使用原 history。
- 会给用户侧 compaction start/error/continue 事件，但失败不是硬中断。

本项目对应：

- manual compact 可以同步，因为用户显式触发。
- 自动 compaction 失败或 stale 时应该只记录 history/debug，不阻断主 agent。

### OpenCode

参考：

- `tmp/external-context-repos/opencode/packages/opencode/src/session/compaction.ts`
- `tmp/external-context-repos/opencode/packages/opencode/src/session/message-v2.ts`

要点：

- `SessionCompaction` 是一等服务，有明确 `compaction` part。
- 选择要压缩的历史时保留 tail turns / recent tokens。
- 工具输出进入 compaction 前先确定性截断。
- compaction part 带 parent / tail 等边界信息。

本项目对应：

- `compact_boundary` 和 `source_fingerprint` 已接近其边界模型。
- 后续可以把 `history_event` / `context_snapshots` 的 trigger reason 补齐，用于可观测性。

### Gemini CLI / Cline / OpenHands / Codex / Claude Code

复用结论：

- 工具事件是结构化 item 或 content block。
- 上下文管理围绕结构化事件做 replay、filter、compact。
- 长工具输出通常先 deterministic truncation / masking / offload，再进入模型压缩。
- 没有看到“每个工具结果入账时同步模型摘要”这种主流设计。

## 问题拆分

### 已解决

- `MOD_01`：tool ledger 单次工具摘要不再同步调用 qwen-turbo。
- `MOD_02`：compaction artifact 写回前有 source fingerprint 校验，stale 不写回。
- `MOD_03`：SSE heartbeat 避免长模型/长工具期间前端误判断流。
- `MOD_06`：paper-reproduction 主线收回 Project + Claude Code + sandbox，不再混旧 workspace/notebook execution 路线。

### 仍存在

1. **自动 background compact 已异步，但能力粗糙**
   - 只有 conversation id，没有任务类型、触发原因、优先级、deadline。
   - 不能表达“只刷新 context_state”或“只做 compacted_history”。

2. **pre-turn / mid-run compact 仍同步**
   - 这是 qwen-turbo 对核心 agent 路径的主要剩余影响。
   - 但这两条路径也承担“避免 prompt 超窗”的即时职责，不能简单全部后台化。

3. **context_state 与 compacted_history 当前串行生成**
   - 一次 compaction 会先 state，再 history。
   - 对低成本模型来说成本可接受，但延迟是两次模型调用叠加。

4. **in-flight 去重仍不完整**
   - 同一 conversation 正在 compact 时，另一路 pre-turn/mid-run/manual 仍可能启动新 compaction。
   - `MOD_02` 能防止旧结果写回，但不能减少浪费。

5. **budget message summary/truncation 仍可能调用 qwen-turbo**
   - 这属于 prompt budget 热路径。
   - 是否一并处理，需要决定范围。

## 可选路线

### 路线 A：只强化后台队列，不动 ReAct 同步 compact

改法：

- 给 background queue 增加 trigger reason、requested_at、last source fingerprint、mode。
- 继续让 pre-turn/mid-run 同步执行。
- 加 in-flight registry，避免 background 和 ReAct 重复启动同一 conversation compaction。

优点：

- 改动小，风险低。
- 不影响当前“超窗时立即压缩”的安全性。

缺点：

- qwen-turbo 仍可能卡 pre-turn / mid-run。
- 只能减少重复后台任务，不能明显降低主链延迟。

适用：

- 如果当前最关注稳定，不急于降低 qwen 延迟。

### 路线 B：pre-turn 优先读已有 artifact，不同步新压缩

改法：

- pre-turn 只使用已有 `context_state/compacted_history` 和 deterministic sliding window。
- 如果检测到 token pressure 或 old history exists，只投递 background compact。
- 本 turn 继续用 recent window + existing replacement_history 跑。
- manual compact 仍同步。
- mid-run 先保持同步兜底。

优点：

- 明显降低新一轮用户输入后的首 token 等待。
- 风险低于完全移除 mid-run 同步兜底。
- 与 Continue/Goose 的 fail-open 风格一致。

缺点：

- 某些长对话首次触发时，pre-turn 不能立即得到最新摘要。
- 如果 existing artifact 很旧，当前 turn 只能靠 sliding window。

适用：

- 如果要优先优化“用户发消息后 agent 开始响应慢”的问题。

### 路线 C：mid-run 也改成后台优先，只保留 deterministic truncation

改法：

- mid-run 不再同步 qwen compact。
- 超窗时只做 deterministic truncation / preserve recent turns。
- 投递后台 compaction，下一轮使用结果。

优点：

- 最大程度减少 run 内 qwen 卡顿。
- 主 agent 路径更纯粹。

缺点：

- 风险最高。
- 对需要长程状态的 skill 流程可能更容易丢上下文。
- paper-reproduction 这种 prompt 约束流程缺少强测试，风险不适合一步到位。

适用：

- 不建议作为下一步直接实施。

### 路线 D：拆分 state refresh 和 history compaction

改法：

- background 队列支持任务类型：
  - `context_state_refresh`
  - `history_compaction`
  - `tool_pair_summary`
- run 结束后默认投递 `context_state_refresh`。
- 只有 token pressure 或 old history 足够多时才投递 `history_compaction`。
- manual compact 可同时跑两类。

优点：

- 避免每次都串行跑两个 qwen 调用。
- 更容易逐步引入异步化。
- 可以先把低风险的 state refresh 后台化。

缺点：

- 需要改 `ConversationCompactionArtifacts` 或拆出任务结果模型。
- 测试面比路线 A/B 更大。

适用：

- 推荐作为 MOD_04 的中期目标，但不一定第一步就完整落地。

## 推荐下一步

推荐采用分阶段路线：**B + 轻量 D，不直接做 C**。

第一步直接做可用改造：

1. 给 compaction worker 引入任务描述对象，而不是只传 conversation id。
2. 增加 in-flight registry，至少在单 backend 进程内避免同 conversation 同类任务重复启动。
3. run 完成后投递 `full_compaction` 时记录 trigger reason。
4. pre-turn 触发 compaction 时直接投递后台 compact，不同步等待 qwen-turbo。
5. mid-run 暂时保留同步兜底，因为它处理的是 run 内已经发生的 truncation / token pressure。

这个路线的理由：

- 不会一次性打掉 skill 流程依赖的上下文能力。
- 能先减少用户新 turn 的 qwen 同步等待。
- `MOD_02` 已经提供 stale 写回保护，后台结果晚到也不会污染历史。
- 后续如果人工 skill 回归稳定，再讨论 mid-run 是否也后台优先。

## 建议的实施边界

### 本项应该做

- 设计并实现 compaction task payload：
  - `conversation_id`
  - `mode`
  - `task_kind`
  - `trigger`
  - `requested_at`
  - `source`

- 单进程 in-flight 去重：
  - key: `(conversation_id, task_kind)`
  - queued/running 都视为已有任务。
  - manual compact 不被后台任务吞掉；manual 可以独立同步执行，但写回仍受 `MOD_02` 保护。

- background worker 先支持：
  - `full_compaction`
  - 不接入暂时没有执行逻辑的 `context_refresh` / `history_compaction`。

- history event / logger 记录：
  - trigger reason
  - stale_source
  - source entry count
  - task kind

### 本项暂不做

- 不删除 qwen-turbo 会话级 compaction。
- 不把 mid-run compaction 一步改成纯后台。
- 不做跨进程分布式锁。
- 不改 codelab/notebook。
- 不引入新的外部队列系统。
- 不改变 tool ledger 已确定的 deterministic summary 路线。
- 不处理 project_tree 内部 qwen-turbo 目录树摘要。

## 验收标准草案

- background queue 可以区分任务来源和 trigger reason。
- 同一 backend 进程内，同一 conversation 同类 background compaction 不会重复排队/并发运行。
- stale artifact 仍由 `MOD_02` 条件提交拦住。
- pre-turn 触发 compaction 时不会等待 qwen-turbo；会记录 deferred debug，并投递后台任务。
- manual compact 仍可同步返回结果。
- mid-run 同步兜底行为不被误伤。
- paper-reproduction skill 回归能继续走 Project + Claude Code + sandbox，不因 pre-turn 延迟策略改变而丢 workflow binding。

## 建议测试

新增或扩展：

- `backend/tests/test_conversation_context_compaction_service.py`
  - enqueue task metadata。
  - queued/running 去重。
  - worker 失败不阻断。

- `backend/tests/test_agent_runtime_context_resilience.py`
  - pre-turn background-first 时不调用 qwen build_artifacts。
  - pre-turn 投递 background compact 后继续使用 existing context。
  - mid-run 仍能同步 compact。

- `backend/tests/test_chat_send_api.py`
  - send 完成后投递 task，done payload 不等待后台 compaction。

Docker 回归：

- `docker compose exec -T backend python -m pytest tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py tests/test_chat_send_api.py -q`
- 人工 paper-reproduction skill 回归：登录开发账号，确认 project status / Claude Code 路径没有退回 workspace/notebook/execution。

## 已按用户反馈收敛的决策

1. pre-turn compaction 改成后台优先正式路径。
   - 不做默认关闭开关。
   - mid-run 暂不动。

2. `context_state` 和 `compacted_history` 第一版不拆任务。
   - 只接入有实际执行逻辑的 `full_compaction`。
   - 后续拆分必须另补真实执行逻辑。

3. 是否把 budget message summary/truncation 的 qwen-turbo 调用纳入 MOD_04？
   - 推荐：先不纳入代码改动，只作为风险记录；否则范围会扩大。

4. 是否需要跨进程去重？
   - 推荐：暂不做。当前 Docker 单 backend 实例下单进程去重足够；未来多副本再用数据库锁或 Redis。

5. 旧 tool pair summary 是否进入本轮？
   - 推荐：不进入第一步。先把 compaction 调度稳定，再讨论 Goose 风格 tool pair summary。

## 调研结论

MOD_04 不应该理解为“把 qwen-turbo 全删掉”。

更准确的目标是：

- 保留 qwen-turbo 在会话级压缩中的价值。
- 把非必要的 qwen 调用从核心 agent 等待路径挪开。
- 对必须同步的压缩保留边界保护和 heartbeat。
- 用 task metadata / in-flight registry 先解决重复启动和不可观测问题。

当前最稳妥的下一步是直接把 pre-turn compaction 改成后台优先的正式路径，并配套完成 compaction task queue 结构化和 in-flight 去重。不要做“默认关闭、以后再启用”的半成品开关。

## 执行方案

本执行方案采用“小步但可用”的路线。第一轮代码要让 pre-turn 后台优先成为实际生效路径，不做默认关闭的占位开关，也不接入暂时没有实际执行意义的 task kind。

### 总原则

- 本项目当前按本地 Docker 研发系统推进，不按生产灰度或企业级多副本约束设计。
- 优先用最快速度完成一条真实可用的完整链路，再用 Docker 自动回归和人工 skill 回归确认生态闭环。
- manual compact 保持同步，用户显式触发时可以等待结果。
- mid-run compact 暂时保持同步兜底，避免在 run 内已经超窗时丢失长程状态。
- pre-turn compact 作为第一条后台优先正式路径：触发后投递后台 compaction，本 turn 不同步等待 qwen-turbo。
- background compaction 结果仍复用 `MOD_02` 的 source fingerprint 条件写回。
- 只做单 backend 进程内去重，不做跨进程锁、不引入 Redis/外部队列。
- 不新增默认关闭的“预留开关”；如果某个接入点没有明确收益，就不接入。
- 不处理 codelab/notebook，不处理 project_tree 内部 qwen 目录树摘要，不做 Goose 风格 old tool pair summary。

### Phase 0：基线确认

目标：

- 确认当前 Docker backend 的 compaction worker 已启动。
- 确认现有 MOD_02/MOD_03/MOD_06 回归仍是可用基线。

动作：

- 读取 startup 日志里的 `ConversationCompactionStartup`。
- 跑 focused tests：
  - `tests/test_conversation_context_compaction_service.py`
  - `tests/test_agent_runtime_context_resilience.py`
  - `tests/test_chat_send_api.py`

退出标准：

- 当前基线测试通过。
- 不进入业务代码改造前，先确认工作区没有意外冲突。

### Phase 1：结构化 compaction task

目标：

- 把 background queue 从 `conversation_id` 升级为结构化任务，先提升可观测性和后续可扩展性。

拟新增数据结构：

```python
@dataclass(frozen=True)
class ConversationCompactionTask:
    conversation_id: int
    task_kind: str  # 第一版只接入 "full_compaction"
    mode: str       # "auto" | "manual" | "pre_turn_deferred" | "run_completed"
    trigger: str
    source: str
    requested_at: str
```

第一版执行策略：

- 第一版只允许 `task_kind="full_compaction"`，并调用现有 `_compact_conversation(..., mode="auto")`。
- 暂不接入 `context_refresh` / `history_compaction`，避免出现“字段存在但没有实际行为”的半成品。
- 后续如果要拆分 state/history，另开小项并补实际执行逻辑。

拟修改文件：

- `backend/app/services/conversation_context_compaction_service.py`
  - 新增 task dataclass。
  - `_queue: asyncio.Queue[int]` 改为 `_queue: asyncio.Queue[ConversationCompactionTask]`。
  - `enqueue_conversation()` 保持兼容旧调用，内部包装为 `full_compaction` task。
  - 新增 `enqueue_task()` 供后续 pre-turn deferred 使用。

测试：

- `test_enqueue_conversation_wraps_legacy_id_as_task`
- `test_worker_runs_full_compaction_task`
- `test_worker_logs_task_metadata_on_failure`

### Phase 2：单进程 queued/running 去重

目标：

- 减少同一 conversation 同类 background compaction 重复启动。
- 解决 `MOD_02` 只能防写回、不能防浪费的问题。

设计：

- `_queued_keys: set[tuple[int, str]]`
- `_running_keys: set[tuple[int, str]]`
- dedupe key: `(conversation_id, task_kind)`
- enqueue 时如果 key 在 queued 或 running 中，直接返回 `{"queued": False, "reason": "duplicate"}`。
- worker 开始时从 queued 移到 running；结束后移除 running。

边界：

- manual compact 不走 background dedupe；它是同步显式操作。
- manual 与 background 同时发生时，仍由 `MOD_02` 条件写回保证最终一致。
- 单进程 dedupe 只解决当前 Docker 单 backend 场景，不承诺多副本一致性。

拟修改文件：

- `backend/app/services/conversation_context_compaction_service.py`

测试：

- `test_enqueue_task_dedupes_queued_task`
- `test_enqueue_task_dedupes_running_task`
- `test_worker_clears_running_key_after_success`
- `test_worker_clears_running_key_after_failure`

### Phase 3：run 完成后的 background task metadata

目标：

- 保持现有 send 完成后投递 background compact 的行为，但补齐 trigger/source。

当前调用点：

- `/chat/send` 多处 `get_conversation_context_compaction_service().enqueue_conversation(conversation_id)`
- 普通 direct model 完成后。
- ReAct agent 完成后。
- planner 相关完成路径后。
- 非 stream send 完成后。
- execution continuation 完成后。

改法：

- 不大范围改所有调用点的业务逻辑。
- 将 `enqueue_conversation()` 增加可选参数：
  - `trigger="run_completed"`
  - `source="chat.send"`
  - `mode="auto"`
  - `task_kind="full_compaction"`
- 旧调用不传参数时仍包装成 `full_compaction`，这是兼容已有调用，不新增第二套行为。
- 关键 send 完成路径可以逐步传更具体 source，例如：
  - `chat.send.direct_stream_completed`
  - `chat.send.react_completed`
  - `chat.send.non_stream_completed`

拟修改文件：

- `backend/app/api/chat.py`
- `backend/app/services/execution_continuation_service.py`
- `backend/app/services/conversation_context_compaction_service.py`

测试：

- `test_chat_send_enqueues_compaction_without_waiting_for_worker`
- `test_enqueue_task_records_trigger_source`

### Phase 4：pre-turn background-first 正式路径

目标：

- 让 pre-turn 不同步等待 qwen-turbo；投递后台 compact，本 turn 用已有 `compacted_history` + sliding window 继续。
- 不新增 `agent_pre_turn_compaction_background_first` 这种默认关闭开关。
- 现有 `agent_pre_turn_compaction_enabled` 仍作为“是否启用 pre-turn compaction 机制”的总开关；启用时就是后台优先路径。

触发行为：

- 当 `_maybe_pre_turn_compact()` 判断满足 old history 或 token pressure 时：
  - 调用 `enqueue_task(task_kind="full_compaction", mode="pre_turn_deferred", trigger=...)`
  - 写 `context.context_debug["pre_turn_compaction_deferred"] = True`
  - 返回 `False`
  - 不调用 `build_artifacts()`，因此不等待 qwen-turbo。

安全边界：

- 只有 pre-turn 改为后台优先正式路径。
- `agent_pre_turn_compaction_enabled` 仍是既有总开关；不新增默认关闭开关。
- mid-run 不改。
- manual compact 不改。
- 如果当前 `context.messages` 已经严重超预算，仍依赖现有 deterministic truncation 兜底；不在本 phase 引入新的截断策略。

拟修改文件：

- `backend/app/services/react_agent.py`
- `backend/app/services/conversation_context_compaction_service.py`

测试：

- `test_pre_turn_background_first_defers_compaction_without_build_artifacts`
- `test_pre_turn_background_first_records_context_debug`
- `test_pre_turn_enabled_path_uses_deferred_compaction`
- `test_mid_run_compaction_still_syncs`

### Phase 5：观测补齐

目标：

- 能从日志/history/debug 里解释 compaction 为什么发生、为什么跳过、是否被去重。

落点：

- worker logger 增加：
  - `conversation_id`
  - `task_kind`
  - `mode`
  - `trigger`
  - `source`
  - `queued/running duplicate reason`

- history event：
  - 对真正执行的 compact，沿用 `manual_compact` / `auto_compact`。
  - stale 已由 MOD_02 记录 `*_compact_stale_skipped`。
  - duplicate enqueue 不写 history event，只写 debug/logger，避免污染对话事实层。

测试：

- 以 service-level test 验证 duplicate 返回值和 task metadata。
- 不强行测试日志文本，避免 brittle。

### Phase 6：Docker 与人工回归

自动回归：

- `docker compose exec -T backend python -m pytest tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py tests/test_chat_send_api.py -q`
- `docker compose exec -T backend python -m pytest tests/test_chat_manual_compact_api.py tests/test_chat_context_preview_api.py -q`
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`

人工 skill 回归：

- 使用开发账号：
  - email: `yuiooyww@gmail.com`
  - password: `123456`
- 场景：
  - 打开 paper-reproduction 相关会话。
  - 查询已有 paper/project 状态。
  - 确认 workflow binding 仍保留 `paper_id/project_id`。
  - 确认 agent 仍走 Project + Claude Code + sandbox。
  - 确认没有退回 workspace/notebook/execution 路线。

## 文件级实施清单

第一轮建议改动文件：

- `backend/app/services/conversation_context_compaction_service.py`
  - task dataclass。
  - task queue。
  - queued/running dedupe。
  - enqueue 返回状态。

- `backend/app/services/react_agent.py`
  - pre-turn background-first 正式路径。
  - context_debug 记录 deferred。

- `backend/app/api/chat.py`
  - send 完成后 enqueue 传入 source/trigger；未改到的旧调用由 `enqueue_conversation()` 包装为同一个 `full_compaction` 行为。

- `backend/tests/test_conversation_context_compaction_service.py`
  - queue/task/dedupe/worker 回归。

- `backend/tests/test_agent_runtime_context_resilience.py`
  - pre-turn deferred 与 mid-run sync 回归。

- `backend/tests/test_chat_send_api.py`
  - send 不等待 background compaction。

文档：

- `docs/window/MOD_04_ASYNC_CONTEXT_MAINTENANCE_ZH.md`
  - 实施后回填执行结果。

- `docs/window/WINDOW_WORK_OUTLINE_ZH.md`
  - 更新状态和决策记录。

## 运维退出手段

- 如果 task queue/dedupe 出问题：
  - 直接修复或回退本次提交；不额外保留第二套运行分支。

- 如果 pre-turn background-first 影响 skill：
  - 先看人工 skill 回归证据，定位是 deferred 时机、worker 去重还是 context replay 问题。
  - 如果必须回退，回退本项 pre-turn 改动；不在代码里长期保留默认关闭分支。

- 如果 background worker 异常影响运行：
  - 设置 `conversation_context_compaction_enabled=False`。
  - 这是既有系统级退出手段，不是本项新增分支。

## 已确认执行原则

1. pre-turn compaction 直接改为后台优先正式路径。
   - 启用 `agent_pre_turn_compaction_enabled` 时，pre-turn 就不再同步等待 qwen-turbo。

2. 第一版只接入实际可执行的 `full_compaction` task_kind。
   - 不接入暂时没有执行逻辑的 `context_refresh/history_compaction`。

3. budget message summary/truncation 的 qwen 调用排除在本次代码改动外。
   - 只记录风险，不扩大本项范围。

4. 只做单进程 in-flight 去重。
   - 当前目标是本地 Docker 快速完整迭代，不做跨进程锁。

5. 不写默认关闭的预留开关，不写没有明确意义的备用分支。
   - 接入就让它真实生效；没有实际执行价值的分支不接入。

## 实施记录

- `ConversationContextCompactionService` 的后台队列从裸 `conversation_id` 升级为 `ConversationCompactionTask`：
  - `conversation_id`
  - `task_kind`
  - `mode`
  - `trigger`
  - `source`
  - `requested_at`
- 第一版只接入 `task_kind="full_compaction"`，执行现有 `_compact_conversation()`，不接入空的 `context_refresh/history_compaction` 分支。
- 新增单进程 queued/running 去重：
  - key 为 `(conversation_id, task_kind)`。
  - queued/running 中已有同类任务时，不重复排队。
  - worker 成功、失败或跳过后都会清理 running key。
- `/chat/send` 与 execution continuation 完成后投递 background compaction 时补充 `mode/trigger/source`。
- `ReActAgent._maybe_pre_turn_compact()` 改为正式后台优先：
  - 满足 old history 或 token pressure 时只投递 `full_compaction`。
  - 不调用 `ConversationContextCompactionService.build_artifacts()`。
  - 不同步等待 qwen-turbo。
  - 写入 `context_debug["pre_turn_compaction_deferred"]` 等调试信息。
- `ReActAgent.run()` 在首轮输出非事实层 thought：说明较早上下文压缩已交给后台维护，当前任务继续使用现有上下文。

## 验证结果

- `docker compose exec -T backend python -m py_compile app/services/conversation_context_compaction_service.py app/services/react_agent.py app/api/chat.py app/services/execution_continuation_service.py tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py tests/test_chat_send_api.py`
- `docker compose exec -T backend python -m pytest tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py tests/test_chat_send_api.py -q`
  - 结果：52 passed。
- `docker compose exec -T backend python -m pytest tests/test_agent_runtime_service.py tests/test_chat_manual_compact_api.py tests/test_chat_context_preview_api.py tests/test_agent_function_calling_fallback.py -q`
  - 结果：48 passed。
- `docker compose exec -T backend python -m pytest tests/test_paper_reproduction_skill_assets.py tests/test_paper_grounding_tools.py -q`
  - 结果：32 passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - 结果：Broad exception guard passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
  - 结果：Contract alignment guard passed。
- Docker 人工 paper-reproduction skill 回归：
  - 登录开发账号 `yuiooyww@gmail.com / 123456`。
  - 在 `/chat` 发送：查看 `paper_id=113` 的 Project + Claude Code + sandbox 主线状态。
  - 结果：工具调用走 `paper_research_status`，返回 `paper_id=113`、`project_id=10`、reference bundle ready、下一步使用 `project_claude`；未退回 notebook/workspace/execution 路线。

## 遗留问题

- mid-run compaction 仍保持同步兜底，本项未改。
- budget message summary/truncation 的 qwen 调用已在 `MOD_07_BUDGET_DETERMINISTIC_TRUNCATION_ZH.md` 收掉。
- `context_refresh/history_compaction` 拆分任务未接入；后续如果要做，必须另补真实执行逻辑。
