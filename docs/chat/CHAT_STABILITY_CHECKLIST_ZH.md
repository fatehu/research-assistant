# Chat 稳定性清单

更新时间：2026-04-02

本文是当前本仓库 `/chat` 主链的稳定性清单，目标不是继续设计新架构，而是回答这几个实际问题：

- 现在 `/chat` 哪些功能点已经跑通
- 哪些层是事实来源，哪些层只是投影视图
- 哪些旧逻辑不能再回引
- 每次继续改 `/chat` 之前，最少要跑哪些检查

配套背景文档见：

- [Chat 上下文管理对比与差距分析](./CHAT_CONTEXT_MANAGEMENT_COMPARISON_ZH.md)

## 1. 当前结论

截至本文更新时间，`/chat` 主链已经完成从“消息 + debug 拼装”到“事件事实层 + 压缩派生层 + 展示投影视图”的稳定化收尾，当前没有发现新的阻断性漏洞。

已经确认跑通的关键链路：

- 新会话首发消息
- 同会话 `context-preview`
- `manual compact`
- `send_plan` 复用发送
- `preview -> compact -> 使用旧 send_plan 再发送`
- 无工具直答 SSE 返回
- `tool_ledger` 持久化
- `item_stream` / `turn_store` 持久化
- `item_stream` 投影消息读取
- mid-run compaction 同 run 生效
- preview 轻量 planner 化

当前判断：

- `/chat` 目前是可用且稳定的
- 运行热路径上的旧机制已经退出主链
- 数据库层的 `ConversationSummary` 和 `Message.react_steps/action/action_input/observation` 也已完成迁移清理
- 剩余问题主要是 `evidence_ledger` 的继续硬化，以及更高阶的 item-first timeline 完整度，不是 `/chat` 主链断点

## 2. 事实来源边界

当前 `/chat` 的边界必须按下面这套理解，后续继续改时不要再混回去。

### 2.1 事实层

这一层是 source of truth。

- `turn_store`
- `item_stream`
- `tool_ledger`

含义：

- `turn_store`：一轮用户消息触发的一次回合及其结果
- `item_stream`：回合内部的事件流，例如 `user_message`、`assistant_message`、`tool_call`、`tool_result`、`compact_boundary`
- `tool_ledger`：工具调用与结果的结构化账本

当前状态：

- `/chat/send`
- `/chat/context-preview`
- `manual compact`
- `GET /chat/conversations/:id`
- `GET /chat/conversations/:id/messages`

都已经优先消费这层，而不是回退到旧 `Message.metadata`、`react_steps` 或 `transcript_store`。

### 2.2 派生层

这一层从事实层提炼而来，不是事实层本身。

- `context_state`
- `compacted_history`
- `replacement_history`
- `evidence_ledger`

含义：

- `context_state`：当前主题、目标、已确认事实、未解决问题等会话状态
- `compacted_history`：历史锚点、历史摘要、compact 边界信息
- `replacement_history`：compact 后替代旧历史进入后续上下文的内容
- `evidence_ledger`：从工具和对话结果提炼出的可复用结论

当前状态：

- `context_state / compacted_history` 仍然是必要的派生层
- 但它们不再被当成原始真相
- `replacement_history` 和 `compact_boundary` 已经进入会话主数据，并能在同一次 run 中生效

### 2.3 展示层

这一层只用于前端展示和调试，不是事实来源。

- `messages`
- `context_debug`
- `preview`
- 各类 SSE 事件可视化面板

必须坚持：

- `context_debug` 不能再成为 compaction 输入
- `messages.metadata` 不能再泄露旧的 `react_steps/context_debug` 作为事实
- 展示层允许丢失、重算、替换，但不能反向驱动事实层

当前状态：

- `context_debug` 仍然存在，但只保留在 preview 返回和当次 SSE 投影视图中
- `send_plan`、`done payload`、持久化消息 metadata 都不再把它当可复用事实

## 3. 当前已验证通过

### 3.1 自动化回归

以下测试已通过：

- `tests/test_chat_context_store.py`
- `tests/test_conversation_context_compaction_service.py`
- `tests/test_chat_manual_compact_api.py`
- `tests/test_chat_context_preview_api.py`
- `tests/test_agent_runtime_context_resilience.py`
- `tests/test_agent_context_budget.py`
- `tests/test_agent_function_calling_fallback.py -k execute_tool_calls_persists_tool_ledger_entries`

本轮实测结果：

- `16 passed`
- `6 passed`
- `7 passed`
- `1 passed`

### 3.2 真实 API 烟测

已实测通过的流程：

1. 注册临时用户
2. 首次 `/api/v1/chat/send`
3. `/api/v1/chat/context-preview`
4. `/api/v1/chat/conversations/{id}/compact`
5. 带 `send_plan_id` 的第二次 `/api/v1/chat/send`
6. `preview -> compact -> 使用旧 send_plan 再发送`

观察结论：

- 首发、预演、压缩、复用都能闭环
- `send_plan` 正常复用时，日志会出现复用提示
- `compact` 之后再发送旧 `send_plan` 不会炸
- 近 20 分钟日志中未发现新的 `Traceback`、500 或 `conversation_item_stream_missing`

## 4. 冻结边界

下面这些边界现在应视为冻结，不要再随手打破。

### 4.1 compaction 只吃事实层

允许输入：

- `item_stream`
- `tool_ledger`
- 事实层转出的 transcript/message rows

不允许再回引：

- `Message.react_steps`
- `metadata.context_debug`
- `metadata.reasoning_summary` 作为主输入
- 已退出热路径的旧 summary/transcript 机制

### 4.2 preview 只负责规划，不负责执行

`/api/v1/chat/context-preview` 当前职责：

- 路由判断
- 上下文装配
- 生成 `send_plan`

不能再做：

- 初始化真实 MCP 执行环境
- 提前执行工具
- 提前做外部搜索/抓取

### 4.3 send 优先复用 send_plan

`send_plan` 合法时：

- 应优先复用
- 不应再无意义地重跑 route/context assembly

`send_plan` 非法时：

- 必须安全地重新规划
- 不允许误复用旧草案

### 4.4 消息响应只返回稳定字段

`GET /chat/conversations/:id` 和相关消息响应中：

- 只返回 UI 需要的稳定 metadata
- 不再把老的 `context_debug/react_steps` 从 `Message.metadata` 整包透出

## 5. 已修复的高风险问题

以下问题已经明确修过，不应再回归。

### 5.1 旧 metadata 泄露 debug/steps

问题：

- `message_to_response(...)` 会把旧消息里的 `context_debug/react_steps` 透给前端

后果：

- 展示层重新污染事实边界

现状：

- 已修

### 5.2 短会话 repeated compact 写入空 boundary

问题：

- 在没有真实 `compacted_history` 的情况下仍然写入 `compact_boundary`

后果：

- 后续上下文切分会把空 boundary 当成有效边界

现状：

- 已修

### 5.3 preview 过重

问题：

- 预演阶段初始化真实工具链和 MCP

后果：

- 体验慢
- 预演成本过高

现状：

- 已降为 planner 路径
- 仍有 router LLM 成本，但已不是“半次真实请求”

### 5.4 send_plan 无法稳定复用

问题：

- revision 校验时机不对，plan 常常在本轮自己失效

后果：

- 名义上有复用，实际上不复用

现状：

- 已修

## 6. 当前已知但非阻断的架构债

这些仍然是问题，但不是当前主链断点。

### 6.1 `evidence_ledger` 还不够硬

现状：

- 已开始吃 `tool_ledger`
- 已保留 `tool_names / source_labels / turn_ids / tool_call_ids`
- 但自然语言 `summary` 和部分归纳判断仍部分依赖 LLM 提炼

风险：

- 可解释性和确定性还可以更强

### 6.2 repo 级冷路径与数据库旧字段已清出 `/chat` 主链

现状：

- 旧冷路径对象和公共 chat 类型字段已经清出 `/chat` 热路径
- 回填脚本也已改为直接从 `messages + tool_ledger + history_log + compacted_history` 重建事件流
- 前端公共 `Message` 类型不再暴露 `action/action_input/observation`
- Alembic 已删除：
  - `conversation_summaries`
  - `messages.react_steps`
  - `messages.action`
  - `messages.action_input`
  - `messages.observation`

这些都已经退出 `/chat` 热路径或公共 chat API。

风险：

- 如果后续做 schema 演进不清楚边界，仍可能在新表或新 metadata 上重新引入并行事实源

## 7. 每次改 `/chat` 前必须跑的清单

### 7.1 后端测试

```bash
docker compose exec -T backend python -m pytest \
  tests/test_chat_context_store.py \
  tests/test_conversation_context_compaction_service.py \
  tests/test_chat_manual_compact_api.py \
  tests/test_chat_context_preview_api.py -q

docker compose exec -T backend python -m pytest \
  tests/test_agent_runtime_context_resilience.py \
  tests/test_agent_context_budget.py -q

docker compose exec -T backend python -m pytest \
  tests/test_agent_function_calling_fallback.py -q \
  -k execute_tool_calls_persists_tool_ledger_entries
```

### 7.2 前端静态检查

```bash
cd frontend
npx eslint src/pages/chat src/stores/chatStore.ts src/services/api.ts
```

### 7.3 最小真实烟测

至少跑这 4 步：

1. 首发 `/chat/send`
2. `/chat/context-preview`
3. `/chat/conversations/{id}/compact`
4. 带 `send_plan_id` 再次 `/chat/send`

## 8. 后续继续改时的红线

后续继续演进 `/chat` 时，禁止再做这些事：

- 再让 `context_debug` 成为事实输入
- 再把 `react_steps` 作为主链数据来源
- 再让 preview 提前初始化真实 MCP/工具执行环境
- 再让消息层 metadata 整包透传给前端
- 在没有真实 compact 产物时写入假的 `compact_boundary`
- 同时维护多套互相冲突的 route/context assembly 主链

## 9. 下一阶段建议

如果继续推进，而不是冻结当前状态，优先顺序建议是：

1. 把 `evidence_ledger` 进一步做成确定性派生层
2. 继续强化 item-first timeline 和前端 turn/item 可视化
3. 再考虑更轻的 preview 和更强的 item-first timeline

一句话收口：

当前 `/chat` 已经进入“稳定维护”阶段。后续继续改时，应优先守住事实层边界，并把剩余清理聚焦在 repo 级冷路径和 schema 债，而不是再往展示层塞新逻辑。
