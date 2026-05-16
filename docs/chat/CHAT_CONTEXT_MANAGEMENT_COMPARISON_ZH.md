# Chat 上下文管理对照与可借鉴点

更新时间：2026-04-05

本文对比 4 套实现：

- 本仓库 `/chat`
- `tmp/reference-repos/codex`
- `tmp/reference-repos/claude-code-sourcemap`
- `tmp/reference-repos/claw-code`

目标不是评价 UI，也不是讨论工具路由，而是只看上下文管理主链：

- 多轮会话如何持久化
- 单轮迭代如何保存
- tool / evidence 如何进入后续上下文
- compact 如何改写历史
- 上下文窗口如何裁剪

---

## 1. 结论摘要

### 1.1 一句话判断

当前本仓库的 `/chat` 已经不是“消息数组 + 最近几轮拼接”的简单实现，而是：

- **事实层已经成型**
- **compact 已进入主链**
- **canonical history 还差一档**
- **budget 管理仍偏保守**

更准确地说：

- 结构层不弱：`item_stream + turn_store + tool_ledger + compact_boundary + replacement_history`
- 压缩层可用：`context_state + compacted_history + replacement_history`
- 弱点不在“有没有 compact”，而在：
  1. `evidence_ledger` 还不够硬
  2. compact 后历史还偏“状态化重组”，不够 thread-canonical
  3. 上下文预算仍主要由固定 system budget 驱动，不是模型窗口感知

### 1.2 最值得借鉴的点

从三家里最值得借鉴的，不是 UI，也不是新 router，而是这 4 件事：

1. **Codex 的 compact 后 replacement history 语义**
2. **Claude 的显式 compact boundary 和 tool_use_summary**
3. **Claude 的稳定上下文缓存**
4. **Claw 的职责拆分清晰度**

### 1.3 不应误读成的路线

这次对照**不应**推导出下面这些方向：

- 不要为了“对齐 Codex/Claude”去做新的 request router
- 不要把 `/chat` 改回纯 `last-N messages`
- 不要把 compact 降级成纯文本 summary
- 不要把 pause / steer 这类 turn-handle runtime 改造误认为上下文管理必需项

---

## 2. 当前本仓库到底怎么做

### 2.1 事实层已经建立

当前 `/chat` 的主事实来源已经不是 `messages.metadata.context_debug`，而是：

- `turn_store`
- `item_stream`
- `tool_ledger`
- `context_state`
- `compacted_history`

关键入口：

- `backend/app/services/chat_context_store.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/services/react_agent.py`
- `backend/app/services/conversation_context_compaction_service.py`

其中：

- `ConversationItemStreamStore.canonical_history()` 会识别 `compact_boundary` 和 `replacement_history`
- `canonical_replay_rows()` 会从 replacement history 作为 replay baseline 开始恢复历史
- `tool_ledger` 已经进入 compaction 输入，不再只靠 item stream 反推

这意味着：

- 旧历史不是简单丢掉
- compact 也不是“多存一句摘要”
- 后续回答上下文已经开始由事实层重建

### 2.2 请求时上下文组装是“分层上下文”，不是 last-N

`react_agent.py` 里当前会组装这些层：

- `conversation_state`
- `persisted_anchor`
- `persisted_summary`
- `replacement_history`
- `recently_slid`
- `recent`
- `memory`
- `older_summary`

然后再按 token budget 裁剪。

这说明你们现在的 `/chat` 已经不是“只送最近 N 条消息”，而是：

- 有 compact 后替代历史
- 有最近窗口
- 有滑出窗口
- 有状态层和记忆层

### 2.3 compact 已进入主链，而且支持 mid-run

当前 compact 已经有三条路径：

- 后台 compact worker
- manual compact
- mid-run compaction

compact 之后会持久化：

- `context_state`
- `compacted_history`
- `replacement_history`
- `compact_boundary`
- `history_event`
- `context_snapshot`

其中 `mid_run_compaction` 会在同一次 run 中刷新 `item_stream`，再重建 `history_messages`，而不是等下一轮才生效。

### 2.4 当前最明显的短板

当前真正的短板主要有 3 个：

1. **证据层还不够硬**
   - `evidence_ledger` 仍有一部分依赖 LLM 提炼
   - `source_label / turn_id / tool_call_id / source_type` 还没有完全确定性派生

2. **compact 后历史还不够 canonical**
   - 已有 `replacement_history`
   - 但整体仍偏“状态 + 投影”的混合模型
   - 还没达到 Codex 那种“compact 直接改写 surviving baseline”的彻底程度

3. **预算层仍偏固定 system budget**
   - 当前核心预算还是 `agent_context_max_input_tokens=10000`
   - trim 和 mid-run compaction trigger 都主要围绕这个固定值工作
   - 还不是模型窗口感知的 soft budget

---

## 3. 三家各自怎么做

## 3.1 Codex

关键参考：

- `tmp/reference-repos/codex/codex-rs/core/src/compact.rs`
- `tmp/reference-repos/codex/codex-rs/core/src/codex/rollout_reconstruction.rs`
- `tmp/reference-repos/codex/sdk/python/src/codex_app_server/api.py`

### 3.1.1 主数据模型是 thread / turn / item

Codex 的核心不是 message list，而是 thread 上的 turn/item 生命周期。

从 SDK 和 runtime 结构看，它强调的是：

- turn 是独立句柄
- item 是会话中的一等对象
- compact/replay 以 thread history 为中心，不以 UI 展示为中心

### 3.1.2 compact 的核心是 replacement history

`compact.rs` 最重要的不是生成 summary，而是：

- 在 compact turn 中真正生成 `replacement_history`
- 调 `replace_compacted_history(...)`
- 再由 replay / reconstruction 机制把它当成 surviving baseline

`rollout_reconstruction.rs` 进一步说明：

- 重建历史时会优先寻找最新 surviving `replacement_history`
- `reference_context_item` 和 `previous_turn_settings` 也一起参与恢复

这比“older summary + recent raw messages”更像 canonical thread history。

### 3.1.3 model context window 是 runtime 显式概念

在 compact turn 的 `TurnStartedEvent` 里，Codex 会显式带上：

- `model_context_window`

这说明在 Codex 里，“模型窗口大小”是 runtime 的一等元数据，而不是只存在于配置注释里。

### 3.1.4 steer / TurnHandle 是 runtime 能力，不是 compact 能力

`api.py` 里有：

- `Thread.compact()`
- `TurnHandle.steer()`

这说明：

- compact 和 steer 是两套不同 runtime 语义
- 不应把 Codex 的 steer 误读为上下文管理本身

**对本仓库最值得借鉴的点：**

- compact 后历史更彻底地以 `replacement_history` 为 baseline
- 模型窗口应该成为 runtime 显式元数据

**不该照搬的点：**

- turn-handle / steer / interrupt 整套 runtime

## 3.2 Claude

关键参考：

- `tmp/reference-repos/claude-code-sourcemap/restored-src/src/QueryEngine.ts`
- `tmp/reference-repos/claude-code-sourcemap/restored-src/src/services/toolUseSummary/toolUseSummaryGenerator.ts`
- `tmp/reference-repos/claude-code-sourcemap/restored-src/src/context.ts`

### 3.2.1 QueryEngine 持有整段会话状态

Claude 的会话不是 UI 临时拼出来的，而是 `QueryEngine` 自己长期持有：

- `mutableMessages`
- `permissionDenials`
- usage
- 各类 attachment / file cache

也就是说，Claude 的上下文主链不是“每次请求临时 assemble 一次 messages”，而是：

- engine 自身就是 conversation container

### 3.2.2 compact boundary 是显式边界

在 `QueryEngine.ts` 里：

- `compact_boundary` 会进入 transcript / messages
- 一旦 boundary 出现，boundary 之前的历史会被明确裁断

这和“多塞一条摘要消息”完全不是一个语义。

### 3.2.3 tool_use_summary 是独立对象

`toolUseSummaryGenerator.ts` 做的不是保留原始 tool log，而是：

- 读取一批已完成 tools 的 input / output
- 让轻模型生成一个简短 label
- 作为 `tool_use_summary` 独立进入主链

这提供了一种很实用的折中：

- 保留结构化 tool 历史
- 同时给出高层进展摘要

### 3.2.4 稳定上下文资产会缓存

`context.ts` 里：

- `getSystemContext()`
- `getUserContext()`

都做了 `memoize`。

这说明 Claude 会把“对当前 conversation 来说稳定不变的上下文资产”缓存起来，而不是每轮重新做全量计算。

**对本仓库最值得借鉴的点：**

- `compact_boundary` 的显式边界语义
- `tool_use_summary` 的独立对象化
- 稳定 context asset 的缓存

**不该照搬的点：**

- 把一切都塞进 `mutableMessages` 的具体实现方式
- 直接复制 Claude 的 transcript 结构

## 3.3 Claw

关键参考：

- `tmp/reference-repos/claw-code/src/query_engine.py`
- `tmp/reference-repos/claw-code/src/transcript.py`
- `tmp/reference-repos/claw-code/src/history.py`
- `tmp/reference-repos/claw-code/src/tools.py`

### 3.3.1 优点是职责边界清楚

Claw 这套 Python 代码最明显的优点不是能力强，而是边界清楚：

- `QueryEnginePort`
- `TranscriptStore`
- `HistoryLog`
- `StoredSession`

对于理解“上下文管理有哪些职责层”，它很有参考价值。

### 3.3.2 但它的上下文能力明显更轻

Claw 当前的 compact 更像：

- `mutable_messages[-N:]`
- `transcript.compact(keep_last)`

tool 信息也主要停留在：

- `matched_tools`
- `permission_denials`

而不是完整 tool result / evidence / replacement history。

所以它更像：

- **结构合理的极简实现**
- 不是成熟的 context runtime 目标形态

**对本仓库最值得借鉴的点：**

- 边界拆分清晰
- 对象职责简单明确

**不该照搬的点：**

- 轻量 transcript 截断
- 弱 tool/evidence 建模

---

## 4. 四者逐项对比

## 4.1 主数据模型

| 维度 | 本仓库 | Codex | Claude | Claw |
| --- | --- | --- | --- | --- |
| 主事实来源 | `turn_store + item_stream + tool_ledger`，message 为投影 | `thread / turn / item` | `QueryEngine.mutableMessages + transcript` | `session + transcript + history` |
| 是否仍明显 message-driven | 热路径已不是 | 否 | 否 | 是 |
| 会话状态对象化 | 强 | 很强 | 很强 | 中等 |

结论：

- 本仓库已经跨过“纯 message history”阶段
- 仍落后于 Codex/Claude 的点，在于 canonical history 更彻底的对象化

## 4.2 compact / canonical history

| 维度 | 本仓库 | Codex | Claude | Claw |
| --- | --- | --- | --- | --- |
| compact 后是否有替代历史 | 有，`replacement_history` | 有，且是 surviving baseline | 有，靠 `compact_boundary` 切换保留段 | 弱 |
| compact 是否进入主链 | 是 | 是 | 是 | 弱 |
| compact 后是否可重放 | 中上 | 强 | 强 | 弱 |

结论：

- 你们已经有 replacement history，不再是纯 summary
- 但 Codex/Claude 的 compact 语义仍然更 canonical

## 4.3 tool / evidence 进入后续上下文

| 维度 | 本仓库 | Codex | Claude | Claw |
| --- | --- | --- | --- | --- |
| tool 调用是否是一等对象 | 是，`tool_ledger + item_stream` | 是 | 是 | 否 |
| tool 高层摘要 | 有，但还不够彻底 | 可由 item/rollout 组合 | 强，`tool_use_summary` | 无 |
| evidence/source 归属硬度 | 中 | 强 | 中上 | 弱 |

结论：

- 这一维本仓库已经明显强于 Claw
- 与 Codex/Claude 的差距主要在 evidence/source 的确定性

## 4.4 上下文窗口 / budget

| 维度 | 本仓库 | Codex | Claude | Claw |
| --- | --- | --- | --- | --- |
| recent/slid/older 分层 | 有 | 不是核心抽象 | 不是核心抽象 | 无 |
| budget 驱动 | 固定 system budget | runtime 显式模型窗口 + compact | boundary / engine 状态主导 | 小预算 + last-N |
| 模型窗口是否是一等元数据 | 还不是 | 是 | 相对更隐式 | 否 |

结论：

- 本仓库的预算治理仍偏“工程保守方案”
- 这是当前最值得继续升级的一层

---

## 5. 可借鉴、可局部参考、不建议照搬

## 5.1 建议直接借鉴

### 5.1.1 更硬的 evidence ledger

方向：

- 从 `tool_ledger + item_stream + turn_store` 尽量确定性派生：
  - `source_labels`
  - `source_type`
  - `turn_id`
  - `tool_call_id`
  - `status`

原因：

- 这是当前本仓库和 Codex/Claude 最真实的差距
- 也是引用、tool replay、debug 可解释性的基础

### 5.1.2 compact 后历史更接近 surviving baseline

方向：

- 继续强化 `replacement_history`
- 让 compact 后后续上下文更多直接围绕 surviving history 构建
- 逐步减少“状态 + 投影拼装”的味道

原因：

- 这是 Codex 最值得借鉴的地方
- 也是你们现有架构已经最接近、最容易继续推进的方向

### 5.1.3 更完整的 item-first timeline

方向：

- 继续把 `reasoning_summary`
- `tool_use_summary`
- `compact_boundary`
- 关键 tool/error/progress 事件

都做成更稳定的 item/timeline 视图。

原因：

- 这会同时改善可解释性、调试性和 replay 能力

## 5.2 建议局部参考

### 5.2.1 Claude 风格稳定上下文缓存

方向：

- 把对当前 conversation 稳定不变的前缀资产缓存起来
- 避免每轮都重复计算 system/user 前缀和部分派生层

原因：

- 可以降低 assemble 成本
- 不需要改动主事实层

### 5.2.2 模型感知 soft budget

方向：

- 不直接等于模型窗口
- 而是：
  - `effective_budget = min(system_cap, model_window - reserve)`

原因：

- 这是本仓库预算层最合理的升级路径
- 既保留工程可控性，也能吃到大窗口模型红利

### 5.2.3 Claude 风格 tool summary

方向：

- 保留原始 tool ledger
- 再为一批完成的 tools 生成简短高层标签

原因：

- 有利于 timeline 可读性
- 不会牺牲结构化信息

## 5.3 不建议照搬

### 5.3.1 request router

原因：

- 三家上下文管理主链都不是靠 request router 建起来的
- 这会把重点从事实层/history/boundary 偏走

### 5.3.2 pause / steer runtime

原因：

- 这是 turn-handle runtime 能力
- 不是上下文管理本身
- 当前 `/chat` 架构下改造成本显著高于收益

### 5.3.3 回退到 last-N message 或纯 transcript 截断

原因：

- 这会直接丢掉你们已经建立起来的事实层优势
- 只会让 compact 和证据层退化

---

## 6. 对本仓库的建议优先级

## 6.1 第一优先级：硬化 evidence / source ledger

目标：

- 引用、工具结果、证据归属尽量确定性化

为什么先做：

- 这是对用户可感知、对 debug 也最有价值的一层
- 比单纯再做更聪明的 summary 更重要

## 6.2 第二优先级：继续收紧 canonical history

目标：

- 让 compact 后历史更像 surviving baseline
- 减少“状态 + 临时拼装”的味道

为什么第二个做：

- 你们已经有 `replacement_history`
- 继续做收益高，且不需要推翻现有底座

## 6.3 第三优先级：把 budget 升级成模型感知 soft budget

目标：

- 模型窗口成为 runtime 显式元数据
- trim / compact trigger 逐步吃 `effective_budget`

为什么第三个做：

- 当前 budget 层是最明显的保守点
- 但它不应先于 evidence/canonical history 去改

## 6.4 第四优先级：缓存稳定上下文资产

目标：

- 降低每轮 assemble 成本
- 减少无意义重复组装

为什么放第四：

- 这主要是效率层收益
- 不如前 3 项影响上下文语义本身

---

## 7. 最终判断

基于当前仓库和 `tmp/reference-repos` 的本地参考实现，可以把本仓库 `/chat` 的上下文管理定位成：

- 已经过了“纯 message history”阶段
- 事实层和 compact 主链已经成型
- 明显强于轻量 transcript 截断式实现
- 但仍未达到 Codex / Claude 那种更彻底的 canonical history 成熟程度

真正的差距不在“摘要够不够聪明”，而在：

- 证据层是否足够硬
- compact 后历史是否足够 canonical
- budget 是否真正模型感知

后续继续改 `/chat`，优先应该收的是：

1. `evidence_ledger`
2. `canonical replacement history`
3. `model-aware soft budget`

而不是：

- 新 router
- 真 steer
- 回退到 last-N messages
