# Chat Item Stream、Replay 与压缩边界说明

更新时间：2026-05-08

本文描述当前 `/chat` 上下文主链的实际运行机制。它不是改造计划，也不是外部框架对照，而是给维护者看的运行说明：

- item stream 里到底存了什么。
- replay 是什么，不是什么。
- 每个 item 属性在上下文重建、压缩和 Provider Messages 中如何去留。
- 压缩边界如何影响后续历史。
- Provider Messages 为什么会出现额外 system message。

## 1. 当前结论

当前 `/chat` 不是单纯的“最近 N 条 messages”。主链是：

1. 对话运行时持续写入 `item_stream`、`turn_store`、`tool_ledger`。
2. `item_stream` 通过 `canonical_history()` 识别最新 `compact_boundary`。
3. `canonical_replay_rows()` 生成后续可投喂上下文的消息投影。
4. `react_agent.py` 把稳定前缀、replacement history、RAG 预取证据、近期窗口和临时消息拼成候选消息。
5. budget 层做确定性压缩和裁剪。
6. `LLMService.sanitize_provider_messages()` 删除内部字段，只留下 provider 支持的消息结构。

也就是说，事实层和模型输入层是两回事：

- `item_stream` 是事实事件流。
- replay 是从事实事件流投影出的可继续对话历史。
- Provider Messages 是最后真正发给模型的消息数组。

## 2. 核心术语

### item_stream

`item_stream` 是 `Conversation.metadata_` 里的持久事件流，当前结构版本是 `conversation_item_stream.v1`。它记录一个会话中发生过的结构化事件，例如用户消息、助手消息、工具调用、工具结果、压缩边界和历史事件。

核心代码：

- `backend/app/services/chat_context_store.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/api/chat.py`

### replay

replay 不是“重新执行工具”，也不是“把所有 item 原样发给模型”。它是从 `item_stream` 重建后续模型可见历史的投影过程。

当前有三类相关方法：

- `ConversationItemStreamStore.replay()`：返回 item_stream 原始条目的字典形式，主要用于存储和调试。
- `canonical_replay_rows()`：返回 replacement history 加上当前 active message-like items。
- `canonical_active_message_rows()`：只返回当前 active message-like items，不包含 replacement history。

### compact_boundary

`compact_boundary` 是一个系统 item。它告诉 canonical history：

- 旧历史压缩到哪个 `message_id`。
- 压缩后用哪些 `replacement_history` 替代旧历史。
- 当前 turn 是否需要通过 `keep_turn_id` 保留。
- 这次压缩基于哪个 `source_fingerprint`。

它本身不是普通聊天消息，不会直接作为用户/助手消息发给模型。

### replacement_history

`replacement_history` 是 compact 后留下的替代历史。它不是普通摘要展示文本，而是后续 replay 的 baseline。

如果存在最新有效 `compact_boundary`，`canonical_replay_rows()` 会先放入 replacement history，再追加边界之后的 message-like items。

### context_state 和 compacted_history

这两层都是派生层，不是事实层。

- `context_state`：当前主题、用户目标、约束、未解决问题、已确认事实、evidence ledger、决策状态。
- `compacted_history`：历史锚点、历史摘要、replacement history、压缩边界 message id。

它们由 `ConversationContextCompactionService.build_artifacts()` 生成，写回前会经过 source fingerprint 条件提交，旧 artifact 不能覆盖新的 item stream。

### Provider Messages

Provider Messages 是最终传给 LLM provider 的消息数组。它可以包含额外 system message，因为系统会把当前会话状态、历史锚点、RAG 预取证据、历史压缩结果作为系统上下文前缀注入。

所以已经有 system prompt 时，Provider Messages 里仍可能看到额外 system message。这不是重复 prompt，而是运行时上下文注入。

## 3. 数据流

```text
用户消息 / 助手消息 / 工具事件
        |
        v
item_stream + tool_ledger + turn_store
        |
        v
canonical_history()
        |
        +-- 识别最新 compact_boundary
        +-- 读取 replacement_history
        +-- 过滤边界之前的旧 message-like items
        +-- 按 keep_turn_id 保留当前 turn 必要消息
        |
        v
canonical_replay_rows()
        |
        v
react_agent._prepare_llm_messages()
        |
        +-- 稳定 system 前缀
        +-- replacement history
        +-- deterministic history compression
        +-- RAG 预取证据
        +-- recently_slid / recent / ephemeral messages
        +-- budget 裁剪
        |
        v
LLMService.sanitize_provider_messages()
        |
        v
Provider Messages
```

## 4. item kind 去留

| kind | item_stream | replay | 压缩/状态 | Provider Messages |
| --- | --- | --- | --- | --- |
| `user_message` | 保留 | 进入 replay | 进入 message preview | 作为 `user` content |
| `assistant_message` | 保留 | 进入 replay | 进入 message preview | 作为 `assistant` content |
| `stopped_assistant_message` | 保留 | 进入 replay | 进入 message preview | 作为 `assistant` content |
| `system_message` | 保留 | 进入 replay | 进入 message preview | 作为 `system` content |
| `message` | 兼容旧通用消息 | 进入 replay | 进入 message preview | 按 role 进入 |
| `thought` | 保留 | 不进入 canonical replay | 可用于调试，不是主输入 | 不发送 |
| `reasoning_summary` | 保留 | 仅在没有 replay 消息时作为 fallback thought | 可被 compaction message rows 使用 | 不直接发送 |
| `tool_call` | 保留 | 不作为聊天消息 replay | 进入 tool ledger preview | 不直接发送 |
| `tool_result` | 保留 | 不作为聊天消息 replay | 进入 tool ledger preview 和 evidence candidates | 不直接发送 |
| `tool_use_summary` | 保留 | 仅在没有 replay 消息时作为 fallback thought | 可辅助状态提取 | 不直接发送 |
| `permission_denial` | 保留 | 不进入 canonical replay | 可辅助状态提取 | 不直接发送 |
| `compact_boundary` | 保留 | 改写 replay 边界 | 写入 compacted_history 边界 | 不直接发送 |
| `history_event` | 保留 | 不进入 canonical replay | 观测和诊断 | 不发送 |

## 5. item 属性去留

| 属性 | item_stream | canonical replay | compaction/context_state | Provider Messages | 说明 |
| --- | --- | --- | --- | --- | --- |
| `item_id` | 保留 | 不输出 | 用于去重、边界和 fingerprint | 丢弃 | item 的稳定身份 |
| `kind` | 保留 | 只用于筛选 | 用于识别 tool、boundary、history event | 丢弃 | 控制 item 语义 |
| `turn_id` | 保留 | 不输出 | 用于 `keep_turn_id`、active tool rows、evidence 归属 | 丢弃 | 归属字段，不能交给模型自由判断 |
| `role` | 保留 | 输出 | message preview 使用 | 保留 | Provider 只接受 `system/user/assistant/tool` |
| `content` | 保留 | 输出 | message preview 使用并截断 | 保留 | 最终模型主要读取字段 |
| `message_id` | 保留 | 不输出 | 用于 `compact_boundary_message_id` 和边界过滤 | 丢弃 | 与数据库 message 行对应 |
| `run_id` | 保留 | 不输出 | 运行诊断 | 丢弃 | 单次运行追踪 |
| `iteration` | 保留 | 不输出 | tool preview、mid-run 诊断 | 丢弃 | ReAct 迭代编号 |
| `tool_name` | 保留 | 不输出 | tool ledger preview、evidence ledger | 丢弃 | 工具事实归属 |
| `tool_call_id` | 保留 | 不输出 | tool pair 归属和 evidence ledger | 丢弃 | 工具调用和结果配对 |
| `status` | 保留 | 不输出 | tool 状态、boundary mode、history event | 丢弃 | 运行状态 |
| `arguments` | 保留 | 不输出 | tool arguments preview，最多取前几项并截断 | 丢弃 | 不原样塞进后续 prompt |
| `thought` | 保留 | 内部 replay row 可带 | state preview 可短截断读取 | 丢弃 | provider sanitizer 不保留该字段 |
| `summary` | 保留 | 不输出 | tool summary、history event、boundary summary | 丢弃 | 常用于派生层，不是普通消息 content |
| `success` | 保留 | 不输出 | tool preview 和 evidence candidate 过滤 | 丢弃 | 失败 tool result 不进入 confirmed evidence |
| `error` | 保留 | 不输出 | tool preview 诊断 | 丢弃 | 错误事实，不是聊天文本 |
| `permission_required` | 保留 | 不输出 | 工具权限状态 | 丢弃 | 防止模型误判工具成功 |
| `execution_time_ms` | 保留 | 不输出 | 观测 | 丢弃 | 性能诊断 |
| `output_tokens_estimate` | 保留 | 不输出 | 工具输出规模观测 | 丢弃 | 判断工具结果是否已裁剪 |
| `truncated` | 保留 | 不输出 | tool preview 和观测 | 丢弃 | 记录输出是否被规则截断 |
| `parallel_group` | 保留 | 不输出 | 并行工具归组 | 丢弃 | 运行归属 |
| `metadata` | 保留 | 内部 replay row 可带 | source labels、fingerprint、replacement history 等 | 丢弃 | Provider Messages 不携带 metadata |
| `created_at` | 保留 | 不输出 | 排查时序 | 丢弃 | item 时间 |

补充说明：

- Provider sanitizer 只保留 provider 可识别字段：`role`、`content`、assistant 的 `tool_calls`、tool message 的 `tool_call_id/name`。
- 从 item_stream replay 出来的历史通常只有 `system/user/assistant`，不会把历史 tool result 作为 provider 的 tool message 重放。
- `metadata`、`thought`、`turn_id`、`tool_call_id` 这类字段用于系统归属和派生，不直接交给 provider。

## 6. canonical history 规则

`canonical_history()` 的核心规则如下：

1. 扫描 item_stream，找到最后一个 `compact_boundary`。
2. 从最后一个带 `replacement_history` 的 boundary 中取 replacement history。
3. boundary 之后的 items 作为 active entries。
4. 如果 boundary 带 `keep_turn_id`，则额外保留 boundary 之前同一 turn 的 message-like items。
5. 如果有 `boundary_message_id`，过滤掉边界之前的旧 message-like items。
6. 非 message-like item 不参与聊天 replay，但仍可被 tool ledger 或状态提取读取。

message-like 当前包括：

- `message`
- `user_message`
- `assistant_message`
- `stopped_assistant_message`
- `system_message`

## 7. Provider Messages 的组成

`react_agent._prepare_llm_messages()` 会把内部上下文组装为候选消息，顺序大致是：

1. 稳定 system 前缀：
   - 会话上下文状态。
   - 持久历史锚点。
   - 历史摘要。
   - 记忆上下文。
2. persisted `replacement_history`。
3. 本地确定性历史压缩消息。
4. 本轮 RAG 预取证据。
5. `recently_slid` 原文窗口。
6. `recent` 原文窗口。
7. 当前 run 的临时消息。

之后 budget 层会按有效预算进行本地确定性处理：

- 旧窗口先转成系统压缩摘要。
- 滑出窗口必要时压缩。
- 近期窗口必要时保留最近 turn，压缩更早部分。
- 内容仍超预算时做 head/tail 裁剪。

这些本地压缩不是模型压缩，不调用 qwen-turbo。

## 8. 模型压缩什么时候发生

当前模型压缩仍保留，但不在每个工具结果后同步执行。

### manual compact

用户显式请求 compact 时同步执行，成功后写入：

- `context_state`
- `compacted_history`
- `compact_boundary`
- `history_event`
- `context_snapshot`

### background compact

后台 worker 消费结构化任务。pre-turn 满足条件时会投递 `full_compaction`，但不会阻塞当前 turn 等模型压缩完成。

### pre-turn compact

当前 pre-turn 路径是 background-first：

- 如果没有可压缩历史，跳过。
- 如果低于 token 压力且没有 old history，跳过。
- 满足条件时投递后台任务。
- 当前 turn 继续使用现有 canonical history 和本地 budget 规则。

### mid-run compact

mid-run 是同步兜底，只在 run 内出现上下文裁剪或 token 压力时触发，并受这些条件限制：

- `agent_mid_run_compaction_enabled`
- 最小 iteration
- 每 run 最大次数
- source fingerprint 条件提交

如果压缩结果过期，写回会被跳过，当前 run 不使用 stale artifact。

## 9. 压缩失败或过期时会怎样

模型压缩失败、超时或 stale，不应该中断主 agent。当前策略是 fail open：

- 不写旧 `context_state`。
- 不写旧 `compacted_history`。
- 不追加旧 `compact_boundary`。
- 写 history/debug event 说明 skipped/failed。
- 当前上下文继续依靠 canonical active history、replacement history 和确定性 budget 裁剪。

因此，“没有压缩成功”不等于立刻丢失整段历史。真正会影响 provider 输入的是最终 budget 裁剪结果。

## 10. 和 `/literature` ask 的关系

`/literature/:paperId/read` 里的右侧 Ask 是独立的文献问答链路，不直接复用 `/chat` 的 item_stream 机制。

当前文献 Ask 的主要持久层是：

- `LiteratureQASession`
- `LiteratureQAMessage`

它的 agentic 模式会在每次请求前构造 scope context message，告诉模型当前论文、知识库、document ids 和本地 PDF 路径。它的历史窗口当前是简单读取同 session 最近 10 条 user/assistant 消息。

所以本文主要适用于 `/chat`，不要把这里的 item_stream/replay 机制误认为 `/literature` Ask 已经全量具备。

## 11. 维护边界

后续修改时建议守住这些边界：

- `item_stream` 和 `tool_ledger` 是事实层，不要让展示层 metadata 反向污染它们。
- `compact_boundary` 只能由 source fingerprint 校验后的提交写入。
- `replacement_history` 是 replay baseline，不是普通 UI 摘要。
- raw tool observation 不应跨轮原样进入后续 Provider Messages。
- `context_state` 可以由模型提取，但 turn/tool/source 归属必须尽量由结构化字段提供。
- Provider Messages 只用于调试“实际发给模型的内容”，不能反推事实层完整性。
