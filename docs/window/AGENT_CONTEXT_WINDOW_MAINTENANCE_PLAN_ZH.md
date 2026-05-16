> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# Agent 上下文窗口维护方案

更新时间：2026-05-04

本文只讨论 agent 系统的上下文窗口、工具结果摘要、Function Calling 主链和压缩机制。目标是先稳定 Docker 运行环境中的核心 agent 路径，再决定是否继续做更大的上下文管理重构。

> 当前状态说明：本文是本轮 window 改造的早期总体草案，部分“当前状态”和“预计改动”描述已被 `MOD_01` 至 `MOD_07` 的实施结果更新。后续执行以 `WINDOW_WORK_OUTLINE_ZH.md` 和具体 `MOD_XX` 文件为准。

## Goal

- 保证 Function Calling 路线稳定，不因为 XML fallback、额外模型摘要或压缩失败导致主链断流。
- 保留模型做会话级上下文压缩的能力，但把它从工具执行热路径中移出。
- 明确 `item_stream / tool_ledger / context_state / compacted_history / replacement_history` 的边界。
- 给后续改造提供一个可拆分、可回滚、可验证的路线。

## Assumptions / constraints

- 运行环境以 Docker 为准，不再优先维护本地裸 venv 差异。
- 当前优先级是稳定 agent 系统，不主动扩大到安全加固、UI 重构或非必要工程硬化。
- `agent_function_calling_enabled=True` 是主路线。
- `agent_function_calling_fallback_xml=False` 已经是当前默认方向。
- qwen-turbo 可以继续作为低成本压缩模型，但不能让核心 FC/tool loop 依赖它的成功和延迟。

## Research

### Current state

关键文件：

- `backend/app/config.py`
  - FC 默认开启：`agent_function_calling_enabled=True`
  - XML fallback 默认关闭：`agent_function_calling_fallback_xml=False`
  - pre-turn / mid-run compaction 默认开启
  - `agent_context_state_model=qwen-turbo`
  - `agent_budget_compression_model=qwen-turbo`

- `backend/app/services/chat_context_store.py`
  - `ConversationItemStreamStore.canonical_history()` 识别 `compact_boundary`
  - `replacement_history` 已能作为 compact 后的替代历史
  - `canonical_replay_rows()` 从 replacement history 和 active entries 重建上下文

- `backend/app/services/conversation_context_compaction_service.py`
  - `build_artifacts()` 当前顺序执行两类模型提炼：
    - `_extract_context_state()`
    - `_extract_compacted_history()`
  - 两者都使用 `agent_context_state_provider/model`
  - compacted history 失败时上层有 deterministic fallback summary

- `backend/app/services/react_agent.py`
  - `_prepare_llm_messages()` 会拼接状态层、replacement history、recent windows、RAG 和临时消息，再按 budget 裁剪。
  - `_maybe_pre_turn_compact()` 和 `_maybe_mid_run_compact()` 已经把 runtime compaction 接入主链。
  - `_tool_result_ledger_summary_text()` 当前会对单个工具结果调用 `_compress_text_with_qwen_turbo(...)`，source 为 `chat.tool_result_ledger_summary`。
  - `_build_tool_result_ledger_entries()` 对每个工具结果逐条 `await` 上述摘要，这是当前最明显的热路径风险点。
  - FC 失败时只有在 `agent_function_calling_fallback_xml=True` 才回退 XML；当前默认关闭。

### External implementations

本轮参考了这些实现：

- Aider：使用 `ChatSummary` 做旧历史摘要，并有后台线程和 stale summary 保护。
- OpenHands：`LLMSummarizingCondenser` 是独立 condenser 子系统，不混在工具执行热路径。
- Gemini CLI：用 `UTILITY_COMPRESSOR` 做状态快照，同时对工具输出先做 deterministic masking / distillation / offload。
- Roo-Code：达到阈值后 model condense；失败时 fallback sliding truncation，并修复 tool_use/tool_result 配对。
- Continue：`compactChatHistory()` 在上下文阈值前后触发 session compaction，不逐个工具结果同步摘要。
- Goose：同时有 `compact_messages()` 和异步批量 old tool pair summarization；这是最接近“模型总结工具结果”的设计，但它保护当前 turn、批量、异步，不阻塞单次工具执行。
- OpenCode：`SessionCompaction` 是一等服务；工具输出进入 compaction 前先截断，summary 有明确结构、tail budget 和事件。
- Cline：重点是确定性 truncation、checkpoint、tool_use/tool_result 配对修复；auto-condense 是独立方向。

结论：

- 模型做上下文压缩是主流做法。
- 主流差异不在“是否用模型”，而在“模型压缩是否卡住核心执行路径”。
- 成熟实现通常先确定性处理工具输出，再在 session/context 边界做模型压缩。

## Analysis

### Problem split

当前问题应该拆成四个不同层级：

1. **FC 稳定性**
   - 主路径应该是 FC。
   - XML fallback 继续保持关闭，后续只保留必要的旧测试或迁移兼容。

2. **工具结果账本**
   - `tool_ledger` 是事实索引，不是完整工具输出归档。
   - 工具结果摘要必须是稳定、快速、失败安全的。
   - 单个工具结果不应同步调用 qwen-turbo。

3. **上下文压缩**
   - qwen-turbo 继续适合做 `context_state` / `compacted_history`。
   - 但压缩应优先出现在 pre-turn、manual compact、mid-run overflow 或后台维护点，而不是每个 tool result 之后。

4. **异步维护**
   - 压缩、旧工具对总结、证据 ledger 刷新，都更适合异步或低优先级任务。
   - 同步路径只需要保证“当前 turn 可以继续跑，并有足够 recent context”。

### Options

1. **最小收口：只移除工具结果 qwen 摘要**
   - 改动最小。
   - 立刻消除 FC/tool loop 中的额外模型依赖。
   - 不解决 compaction 双模型调用和状态过期问题。

2. **分层收口：工具摘要确定性化 + compaction 保留模型 + 异步化设计预留**
   - 与外部成熟实现最一致。
   - 风险可控，每一步都能单独验证。
   - 需要补少量测试和观测字段。

3. **大重构：把 context_state、compacted_history、tool pair summary 全部做成后台 worker**
   - 长期架构更干净。
   - 当前改动面偏大，容易影响已经跑通的主链。
   - 不适合作为下一步直接落地。

### Decision

推荐选 **方案 2：分层收口**。

原因：

- 不否定 qwen-turbo 压缩路线，避免把已有上下文能力打掉。
- 先拿掉最危险的同步 per-tool-result 模型调用，收益明确。
- 保留未来参考 Goose 的异步批量 tool pair summary 空间。
- 不需要一次性重写 `item_stream` 或 `replacement_history`。

## Proposed architecture

### Layer 1: core FC loop

职责：

- 接收 LLM FC tool_calls。
- 执行工具。
- 写入 action / observation / tool message / item_stream。
- 继续下一轮 FC 或输出最终 answer。

约束：

- 不调用 XML fallback，除非显式配置开启。
- 不调用 qwen-turbo 做工具结果摘要。
- 不因 compaction 模型失败而中断当前工具执行。

### Layer 2: deterministic tool ledger

职责：

- 为 `tool_ledger` 写入短摘要。
- 保留 tool_name、status、path、execution_id、page、chunk、source_labels、error code、truncated flag 等结构化信号。
- 对 observation 只做本地裁剪和规则提取。

输出建议：

- `summary`: 规则生成的 1-4 行摘要。
- `metadata.raw_preview`: 可选短 preview，长度硬限制。
- `metadata.compaction_hint`: 可选，标记是否值得后续异步总结。

非目标：

- 不在这里生成长自然语言总结。
- 不在这里提炼最终事实。

### Layer 3: conversation compaction

职责：

- 基于事实层生成或刷新：
  - `context_state`
  - `compacted_history`
  - `replacement_history`
  - `compact_boundary`

模型调用策略：

- qwen-turbo 只在 context/session 边界调用。
- manual compact 可以同步，因为用户显式请求 compact。
- pre-turn / mid-run 可以同步，但必须保留 deterministic fallback。
- 普通工具结果不触发同步 qwen。

需要补强：

- compact artifact 写入时记录 `source_boundary_message_id`、`source_item_stream_updated_at` 或等价版本。
- 如果 compact 输入已经过期，丢弃旧 artifact，不覆盖新状态。
- compact 失败要有状态记录，但不阻断主 agent。

### Layer 4: async maintenance

职责：

- 后台刷新 context_state。
- 后台批量总结旧 tool pairs。
- 后台重算 evidence_ledger。

触发条件建议：

- run 结束后。
- token 估算超过阈值但当前 turn 已安全完成。
- tool_ledger 中旧工具结果累计超过阈值。

Goose 风格的 old tool pair summary 可以作为后续 P2，不进入 P0。

### Layer 5: heartbeat / observability

职责：

- 在长 LLM 请求、长工具执行、mid-run compaction 等阶段持续给 SSE 发 heartbeat。
- 区分正在模型响应、正在工具执行、正在 compact、正在等待后台 execution。

注意：

- heartbeat 是运行状态信号，不应写入对话事实层。
- 不应进入 LLM prompt。

## Implementation plan

### P0: 收掉工具热路径风险

目标：

- FC/tool loop 不再同步调用 qwen-turbo 摘要单个工具结果。
- XML fallback 保持默认关闭。
- qwen 失败不会影响工具结果入账。

预计改动：

1. 修改 `backend/app/services/react_agent.py`
   - 将 `_tool_result_ledger_summary_text()` 改成确定性规则摘要。
   - 删除或旁路其中 `_compress_text_with_qwen_turbo(... source="chat.tool_result_ledger_summary")`。
   - 保留结构化 debug detail 的本地摘要逻辑。

2. 补测试
   - 验证工具结果入 ledger 时不会调用 qwen 压缩。
   - 验证大 observation 会被本地截断。
   - 验证失败工具、授权工具、带 path/execution_id 的工具都有稳定 summary。

3. 回归
   - FC 工具调用链。
   - `tool_ledger` / `item_stream` 持久化。
   - XML fallback 默认关闭行为。

### P1: compaction 状态边界稳定化

目标：

- qwen-turbo 压缩保留，但 artifact 写入更安全。
- 防止旧 compact 结果覆盖新 item_stream。

预计改动：

1. 在 compaction 输入和输出中加入边界版本字段。
2. 持久化前校验 item_stream 边界仍匹配。
3. 给 `build_artifacts()` 增加更明确的 failure status，而不是只有空 dict。
4. 保留 deterministic fallback summary。

### P1: heartbeat

目标：

- 用户侧不再误判长模型调用或长工具执行为断流。

预计改动：

1. 梳理 `/chat/send` SSE 的 heartbeat 入口。
2. 在 FC model wait、tool execution wait、mid-run compaction wait 周围发非事实层 heartbeat。
3. 不把 heartbeat 写入 `item_stream` 或 `tool_ledger`。

### P2: 异步上下文维护

目标：

- 将非必要的 context_state 刷新和旧工具对总结转移到后台。

预计改动：

1. 复用现有 compaction worker 或新增轻量维护任务。
2. run 结束后投递 context refresh。
3. 对旧 tool pairs 做可选批量摘要。
4. 保护最近 N turns，不总结当前 turn。

### P3: 预算与观测统一

目标：

- 让模型窗口、预算、裁剪、压缩触发更容易诊断。

预计改动：

1. 在 context_debug / history_event 中记录 compaction trigger reason。
2. 区分 deterministic truncation、model compaction、fallback compaction。
3. 统计 qwen compaction latency、failure、fallback count。

## Tests to run

P0 最小测试：

- `python -m pytest backend/tests/test_agent_function_calling_fallback.py -q`
- `python -m pytest backend/tests/test_agent_runtime_context_resilience.py -q`
- `python -m pytest backend/tests/test_conversation_context_compaction_service.py -q`

建议新增或扩展：

- `backend/tests/test_agent_tool_ledger_summary.py`
  - 不调用 qwen。
  - 大工具输出 deterministic 截断。
  - 结构化错误摘要保留。

Docker 验证：

- `docker compose up -d --build backend frontend`
- `docker compose ps`
- 在 Docker backend 内跑对应 pytest。

## Risks / edge cases

- 如果工具 observation 的关键信息只存在长文本深处，P0 的本地摘要可能丢信息。因此必须保留短 preview、结构化字段和 raw observation 的原持久化位置。
- 如果 mid-run compaction 同步耗时较长，heartbeat 只能改善用户感知，不能减少实际延迟。
- 如果 context_state 和 compacted_history 同时由多个路径刷新，需要边界版本避免旧结果覆盖新结果。
- 如果未来重新启用 XML fallback，必须明确它只是故障兼容，不是常规双协议运行。

## Open questions

需要讨论后再决定：

1. P0 是否只做“移除工具结果 qwen 摘要”，不碰 compaction artifact 版本？
2. pre-turn / mid-run compaction 是否继续允许同步 qwen，还是 P1 就开始改成后台优先？
3. 旧工具对异步摘要是否真的需要？还是 `tool_ledger + deterministic summary + context_state` 已经够用？
4. heartbeat 只覆盖 `/chat/send`，还是也同步整理 CodeLab / runtime worker 中已有 heartbeat 语义？
5. qwen-turbo 压缩失败是否需要前端可见提示，还是只写 history/debug 事件？

## Q&A results

待本方案讨论后补充。
