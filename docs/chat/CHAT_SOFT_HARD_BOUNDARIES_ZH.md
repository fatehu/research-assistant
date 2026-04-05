# Chat 软硬边界清单

更新时间：2026-04-02

本文的目标不是继续扩设计，而是把 `/chat` 当前系统里：

- 哪些必须是硬边界
- 哪些可以交给 LLM 软处理
- 哪些必须拆成“结构硬、表述软”
- 哪些现在不该再做
- 哪些后面应该补

一次性说明白。

配套文档：

- [Chat 稳定性清单](./CHAT_STABILITY_CHECKLIST_ZH.md)
- [Chat 上下文管理对比与差距分析](./CHAT_CONTEXT_MANAGEMENT_COMPARISON_ZH.md)

---

## 1. 基本原则

这里的“硬/软”不是“规则 vs 模型”这么简单。

更准确地说：

- **硬**：必须由系统根据事实、ID、权限、预算、显式用户设置来决定，不能让模型自由发挥。
- **软**：允许模型做归纳、压缩、路由、表述优化，只要不改写事实归属。
- **混合**：结构和归属必须硬，文本表述可以软。

一句话：

- **事实归属硬**
- **自然语言表达软**

---

## 2. 当前系统应如何分层

### 2.1 硬层：必须 deterministic

这些层必须由系统直接生成或校验。

#### 事实层

- `turn_store`
- `item_stream`
- `tool_ledger`

它们回答的是：

- 哪个 turn 发生了什么
- 哪个 item 属于哪个 turn
- 哪次 tool call 对应哪个 result
- 哪个 compact boundary 在哪里

当前代码入口：

- `backend/app/services/chat_context_store.py`
- `backend/app/services/agent_runtime_service.py`
- `backend/app/api/chat.py`

#### 系统边界层

- token budget
- recent / recently_slid / replacement history 的拼装顺序
- preview/send 的 revision 校验
- tool gating / permission
- send_plan 的草稿哈希和会话 revision

这些也必须是硬的。

当前代码入口：

- `backend/app/services/react_agent.py`
- `backend/app/api/chat.py`
- `backend/app/config.py`

#### 用户显式设置

- chat 偏好
- 是否默认用工具
- 语言/简洁度等显式设置

这些如果存在，应该作为硬输入看待，而不是模型“猜测偏好”。

---

### 2.2 软层：允许 LLM 做归纳和压缩

这些内容可以交给模型做，但它们不能反向定义事实。

- routing decision
- active topic
- user goal
- open questions
- reasoning summary
- tool use summary
- compacted history 的自然语言总结
- evidence summary 的自然语言表述

当前代码入口：

- `backend/app/services/react_agent.py`
- `backend/app/services/conversation_context_compaction_service.py`

---

### 2.3 混合层：结构硬，表述软

这是接下来最该继续收的部分。

#### `evidence_ledger`

这层不该是纯硬，也不该是纯软。

应拆成：

**硬字段**

- `turn_ids`
- `tool_call_ids`
- `tool_names`
- `source_labels`
- `status`

**软字段**

- `summary`
- `why_it_matters`

也就是：

- “这条证据来自哪里”必须硬
- “这条证据怎么概括”可以软

#### `compacted_history`

这层也应拆开看：

**硬字段**

- `compact_boundary_message_id`
- `up_to_message_id`
- compact 覆盖范围

**软字段**

- `history_anchors`
- `history_summary`
- `replacement_history` 的自然语言内容

---

## 3. 明确不该做的事

下面这些，后面不要再做。

### 3.1 不该让 LLM 决定事实归属

包括：

- 这条 evidence 属于哪个 turn
- 哪个 tool call 产出了这条 evidence
- 哪些 source label 与它绑定
- compact boundary 覆盖到哪里

这些都应该由系统从事实层确定。

### 3.2 不该让规则或 LLM 偷偷持久化“长期约束”

例如：

- 从普通聊天里自动抽“以后都用中文”
- 从一句“先简短说”里自动变成长期默认

这类只能作为候选项，必须让用户看见、确认、修改。

### 3.3 不该让 raw tool observation 跨轮原样进入上下文

同一 run 内当然要保留 observation，供模型继续推理。

但跨轮后：

- 原始 observation 应留在 `tool_ledger`
- 进入后续上下文的应是提炼后的 evidence / summary

### 3.4 不该再让展示层成为事实来源

包括：

- `context_debug`
- message metadata 里的旧 debug/steps
- UI 临时状态

它们只能展示，不能反向参与 compaction 或 context assembly。

---

## 4. 结合三家参考仓的结论

### 4.1 Codex

`Codex` 的强项是：**事件和归属非常硬**。

- `thread -> turn -> item`
- tool call/result 是标准 item
- compact boundary / replacement history 有清楚归属

它软的部分主要是：

- compact 后的替代历史文本
- 摘要类表达

所以它不是“纯硬系统”，而是：

- **结构极硬**
- **摘要可软**

### 4.2 Claude

`Claude` 的强项是：**QueryEngine 主链非常硬**。

- `mutableMessages`
- `compact_boundary`
- `permissionDenials`
- `tool_use_summary` 事件位置

它软的部分主要是：

- tool use summary 本身
- 历史压缩后的表述

所以 Claude 也不是“全靠 LLM 决定一切”，而是：

- **边界硬**
- **摘要软**

### 4.3 claw-code

`claw-code` 更轻，做法更保守：

- session/transcript/history 边界清楚
- 软层能力没有前两者那么重

它给我们的启发不是“多智能”，而是：

- 先把边界理清
- 不要让并行事实源乱长

---

## 5. 当前系统的建议边界

### 5.1 必须硬的

- `turn_store`
- `item_stream`
- `tool_ledger`
- `compact_boundary_message_id`
- `send_plan` 的 revision/hash 校验
- token budget 和窗口裁剪顺序
- 用户已确认偏好

### 5.2 可以软的

- topic / goal / open questions
- reasoning summary
- tool use summary
- history summary
- evidence 的自然语言表述

### 5.3 必须改成混合的

- `evidence_ledger`
- `replacement_history`
- 用户偏好候选项

---

## 6. 接下来该加什么

### 6.1 候选偏好层

需要新增一层：

- 自动提炼候选项
- 但不自动生效
- 用户可在上下文窗口中：
  - 接受
  - 忽略
  - 改写
  - 保存为默认

### 6.2 更硬的 evidence provenance

`evidence_ledger` 需要继续补：

- `derived_from`
- `confidence_source`
- `provenance_kind`

至少要明确：

- 是来自 tool result
- 还是来自 assistant final answer
- 还是来自 compacted synthesis

### 6.3 item-first timeline

前端后续应该更直接展示：

- turn
- item
- compact boundary
- replacement history
- tool use summary
- evidence sink

这样用户才能真正看懂“哪些东西会进入下一轮”。

---

## 7. 最终收口

本系统后续继续优化时，应始终守这个判断：

- **硬的负责定义事实**
- **软的负责压缩和表述**
- **混合层必须把归属和文本拆开**

如果一句话概括：

**不要让 LLM 发明事实，但要让 LLM 帮系统把事实讲得更短、更顺。**
