# Chat 上下文管理对比与差距分析

更新时间：2026-04-02

本文对比 4 套实现：

- 本仓库 `/chat`
- `openai/codex`
- `ChinaSiro/claude-code-sourcemap`
- `instructkr/claw-code`

目标不是评价 UI，而是梳理上下文管理主链本身：

- 多轮会话如何持久化
- 单轮迭代如何保存
- tool 信息如何进入后续上下文
- 上下文窗口如何裁剪
- compact/summary 如何参与后续对话

## 1. 结论摘要

当前本仓库的 `/chat` 已经不再是“纯消息数组 + 最近几轮拼接”的简单实现，已经具备：

- `ConversationContextState`
- `compacted_history`
- `HistoryLog`
- `context_snapshots`
- `turn_store`
- `item_stream`
- `tool_ledger`
- 后台 compaction worker

这些底座方向是对的，没有白做。

但和 `Codex`、`Claude` 相比，仍有 3 个核心短板：

1. `evidence_ledger` 还不是足够硬的确定性派生层，仍有一部分依赖 LLM 提炼。
2. `compact boundary` 虽已进入主链并支持 mid-run 生效，但还没达到 Codex/Claude 那种高度事件化、线程级 canonical history 的程度。
3. item-first timeline 和 turn/item 展示仍未完全做透，用户可见层还可以更接近 Codex/Claude。

一句话判断：

- 在当前已读到的公开代码切面上，本仓库在“语义压缩、后台 compaction、持久状态层”这些维度做得更重；但这不代表整体产品能力或最终工程成熟度高于 `claw-code`，后者很可能有未在当前切面中体现的取舍与能力。
- 但距离 `Codex` / `Claude QueryEngine` 的成熟形态，还差“tool/item 主数据化”和“compact 边界主数据化”这两刀。

## 2. 当前本仓库怎么做

### 2.1 会话级状态

当前已经有会话级持久状态：

- `context_state`
- `compacted_history`
- `history_log`
- `context_snapshots`
- `turn_store`
- `item_stream`
- `tool_ledger`

主要入口：

- `backend/app/services/chat_context_store.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/services/conversation_context_compaction_service.py`

其中：

- `HistoryLog` 保存 compact/状态演进事件
- `ConversationContextCompactionService` 用 `qwen3.5-flash` 提取：
  - `conversation_context_state.v2`
  - `conversation_compacted_history.v1`

关键代码：

- `backend/app/services/chat_context_store.py`
- `backend/app/services/conversation_context_compaction_service.py`

### 2.2 请求时上下文组装

主回答链在 `backend/app/services/react_agent.py` 里完成：

- 先读 `item_stream / turn_store / tool_ledger`
- 再读 `ConversationContextState`
- 再读 `compacted_history`
- 再组：
  - `replacement_history`
  - `recent`
  - `recently_slid`
  - `persisted_anchor`
  - `persisted_summary`
  - `memory`
  - `older_summary`

然后在 token 预算超限时继续裁剪。

这说明本仓库已经不是“只有 last-N message”，而是事实层驱动的分层上下文。

### 2.3 单轮迭代保存

单轮中间过程现在主要通过这些层保留：

- `item_stream`
- `turn_store`
- `tool_ledger`
- `context.persist_events`

`react_steps` 和 `metadata.context_debug` 已经退出 `/chat` 热路径事实来源。

### 2.4 当前最重要的问题

尽管已经有事实层，但主链仍有明显的收尾工作：

- `evidence_ledger` 还不够硬
- compact 结果仍偏“状态化”而不是完全线程级 canonical history
- repo 里仍有数据库层老 schema 字段这类冷路径债

## 3. Codex 的做法

### 3.1 主数据模型：Thread -> Turn -> Item

`Codex` 的核心不是消息列表，而是线程事件流。

见：

- `/tmp/codex-repo/codex-rs/exec/src/exec_events.rs`

其中顶层是：

- `thread.started`
- `turn.started`
- `turn.completed`
- `item.started`
- `item.updated`
- `item.completed`

真正的会话内容落在 `ThreadItemDetails` 里，类型包括：

- `AgentMessage`
- `Reasoning`
- `CommandExecution`
- `FileChange`
- `McpToolCall`
- `CollabToolCall`
- `WebSearch`
- `TodoList`
- `Error`

这意味着：

- tool 信息不是 assistant 消息的附属字段
- reasoning 不是消息里的隐含文本
- command/web/mcp 都是同级 item

### 3.2 Tool 信息是一等对象

`mcpToolCall` 直接带：

- `server`
- `tool`
- `status`
- `arguments`
- `result`
- `error`

这类结构体在 app-server 协议和 SDK 中也是标准化的，不靠 UI 自己猜。

这点是本仓库和 `Codex` 最大的差距之一。

### 3.3 Compact 不是一句摘要，而是 replacement history

见：

- `/tmp/codex-repo/codex-rs/core/src/compact.rs`
- `/tmp/codex-repo/codex-rs/core/src/codex/rollout_reconstruction.rs`

`Codex` compact 完并不只是保存 `summary_text`，而是保存：

- `message`
- `replacement_history`

后续恢复线程时，`rollout_reconstruction` 会直接把 `replacement_history` 作为 surviving baseline。

这意味着：

- compact 之后的上下文是可重建的
- compact 是线程历史结构变化，不只是文本概括

### 3.4 上下文窗口管理

`Codex` 也会因为 context window 超限而移除最旧 history item，但 compact 完不会退回普通摘要，而是直接重写 compact 后历史。

这比“older summary + recent raw”更像线程级 canonical history。

## 4. Claude 的做法

### 4.1 QueryEngine 持有整段会话状态

见：

- `/tmp/claude-sourcemap/restored-src/src/QueryEngine.ts`

`QueryEngine` 自己声明：

- 一个 QueryEngine 对应一个 conversation
- 每次 `submitMessage()` 是一个 turn
- `mutableMessages`、usage、permissionDenials、file cache 会跨 turn 持续存在

这说明 Claude 的会话状态不是 UI 临时算出来的，而是 QueryEngine 主链维护的。

### 4.2 tool/progress/attachment/system 都进入主链

在 `QueryEngine.ts` 中，以下内容都会进入 `mutableMessages` 或 transcript：

- `assistant`
- `progress`
- `attachment`
- `user`
- `system`
- `tool_use_summary`

特别重要的是：

- `progress` 会即时写入 transcript
- `tool_use_summary` 是独立消息类型
- `permissionDenials` 是独立状态，不附属于普通 assistant 内容

### 4.3 Compact boundary 是显式边界

在 `QueryEngine.ts` 中，`system.compact_boundary` 不是普通提示消息，而是：

- 进入 `mutableMessages`
- 一旦出现，就直接裁掉 boundary 之前的消息
- 成为后续上下文的显式边界

这非常关键。它意味着：

- compact 不是“多了一个摘要”
- 而是会话保留段发生了明确切换

### 4.4 Tool summary 是独立能力

见：

- `/tmp/claude-sourcemap/restored-src/src/services/toolUseSummary/toolUseSummaryGenerator.ts`

Claude 会把一批完成的 tools：

- name
- input
- output

交给轻模型生成一个简短 label，再作为 `tool_use_summary` 消息发出。

这让 tool 历史既结构化，又有高层摘要，而不是只有低层原始结果。

### 4.5 上下文缓存

见：

- `/tmp/claude-sourcemap/restored-src/src/context.ts`

`getSystemContext()` 和 `getUserContext()` 都做了 `memoize`，会在会话期间缓存。

这说明 Claude 把大量“对话期间稳定不变”的上下文资产当作缓存对象处理，而不是每轮重新计算。

## 5. claw-code 的做法

### 5.1 优点：对象拆分清楚

见：

- `/tmp/claw-code/src/query_engine.py`
- `/tmp/claw-code/src/transcript.py`
- `/tmp/claw-code/src/history.py`

它的好处在于对象边界很清楚：

- `QueryEnginePort`
- `TranscriptStore`
- `HistoryLog`
- `StoredSession`

### 5.2 缺点：tool/context 过于简化

`claw-code` 的 tool 信息只有：

- `matched_tools`
- `permission_denials`

`TranscriptStore` 只保存字符串 prompt，不保存完整 tool result。

compact 也只是：

- `mutable_messages[-N:]`
- transcript 截断

所以它更像一个“结构合理的极简原型”，不是成熟的 tool/context 管理方案。

## 6. 四者逐项对比

### 6.1 主数据模型

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| 主事实来源 | `turn_store + item_stream + tool_ledger`，消息为投影 | `thread/turn/item` | `QueryEngine.mutableMessages + transcript` | `session + transcript + history` |
| 是否 message-driven | 热路径已不是 | 否 | 否 | 是，但对象简单 |
| 会话状态对象化 | 强 | 很强 | 很强 | 中等 |

结论：

- 本仓库热路径已经把对象抬升为主数据，但数据库层旧 schema 债仍在，完成度仍不如 Codex/Claude。

### 6.2 多轮对话

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| 会话跨轮持久化 | 有 | 有 | 有 | 有 |
| 后台 compact | 有 | 有 | 有 | 无明显独立后台 compact |
| compact 后上下文延续 | `replacement_history + compact_boundary + context_state` | `replacement_history` | `compact boundary` | 截断后剩余文本 |

结论：

- 本仓库多轮状态层已建立，compact 也已进入主链；差距主要在 canonical history 的成熟度。

### 6.3 单轮迭代

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| 迭代建模 | `turn_store + item_stream + tool_ledger` | `item lifecycle` | message/event lifecycle | `TurnResult` |
| 工具轮可重放性 | 中等 | 强 | 强 | 弱 |
| reasoning 是否独立对象 | 部分是，`reasoning_summary/tool_use_summary` 已 item 化 | 是 | 部分是 | 否 |

结论：

- 本仓库已经 item 化不少，但还没到 Codex 那种彻底的 `item lifecycle` 粒度。

### 6.4 tool 信息

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| tool call/result 是否一等对象 | 是，已进入 `tool_ledger + item_stream` | 是 | 是 | 否 |
| permission denial 独立化 | 弱 | 强 | 强 | 中 |
| tool summary | 中，`tool_use_summary` 已 item 化 | 可通过 item/rollout 组合 | 强 | 无 |

结论：

- 这一维已经明显收窄，剩下的差距在 item lifecycle 精细度和证据层确定性。

### 6.5 上下文窗口

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| recent/slid/older 分层 | 有 | 不以此为核心 | 不以此为核心 | 无 |
| budget 裁剪策略 | 启发式 | thread history 级 | compact/snip boundary 级 | last-N |
| compact 边界显式化 | 有 | 有 | 有 | 无 |

结论：

- 本仓库的窗口分层和边界语义已经更接近 Codex/Claude，但请求时拼装味道仍然更重。

### 6.6 压缩方法

| 维度 | 本仓库 | Codex | Claude | claw-code |
| --- | --- | --- | --- | --- |
| 语义压缩模型 | `qwen3.5-flash` | 远程 compact task / replacement history | compact boundary + tool summary | 无 |
| 压缩产物 | `context_state + compacted_history + replacement_history` | `summary + replacement_history` | boundary + summarized tool use | 截断 |
| 压缩是否进入后续主历史 | 是，但仍偏状态化 | 是 | 是 | 弱 |

结论：

- 本仓库压缩质量和结构都已明显增强，但压缩产物的“线程级 canonical history 感”仍不如 Codex/Claude。

## 7. 本仓库已做对的部分

这些方向应保留：

1. `ConversationContextState`
2. `compacted_history`
3. `HistoryLog`
4. `context_snapshots`
5. 后台 compaction worker
6. `manual compact`
7. `recent / recently_slid / older` 分层
8. `turn_store / item_stream / tool_ledger`

这些并不是错误方向，反而是走向成熟架构的必要底座。

## 8. 本仓库最需要补的部分

### 8.1 第一优先级：让 `evidence_ledger` 更硬

应继续从：

- `tool_ledger`
- `item_stream`
- `turn_store`

确定性生成：

- `source_labels`
- `tool_names`
- `turn_ids`
- `tool_call_ids`
- `status`

自然语言 `summary` 可以继续保持软表达，但归属关系应尽量硬化。

### 8.2 第二优先级：让 item-first timeline 更完整

当前已经有：

- `reasoning_summary`
- `tool_use_summary`
- `compact_boundary`

下一步应继续往：

- 更细的 item lifecycle
- 更强的前端 turn/item 时间线

推进。

## 9. 推荐路线

不推翻现有底座，按下面顺序推进：

1. 继续硬化 `evidence_ledger`
2. 继续强化 item-first timeline 和 turn/item 展示
3. 再强化 item-first timeline 与更轻的 preview

## 10. 最终判断

基于当前已读到的公开实现切面，可以把本仓库的 `/chat` 上下文管理大致放在这样的位置：

- 已经过了“纯 message history”阶段
- 热路径已经进入 `item/ledger/boundary` 主导阶段
- 但仍未达到 `Codex` / `Claude QueryEngine` 那种更彻底的 canonical thread history 成熟程度

真正的差距不在“摘要够不够聪明”，而在：

- 证据层是否足够硬
- compact 后历史是否更 canonical
- repo 级旧债是否彻底清场

这是后续继续改造 `/chat` 的主方向。
