> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 02: Compaction 边界版本保护

阶段：P1

状态：已实施，Docker focused 回归通过

## 目标

让 `context_state` / `compacted_history` / `compact_boundary` 写入时能确认输入边界仍然有效，避免旧 compact artifact 晚到后覆盖或折叠新 `item_stream`。

## 不改什么

- 不移除 qwen-turbo 会话级 compaction。
- 不把所有 compaction 改成后台任务。
- 不重写 `item_stream` / `replacement_history` 数据模型。

## 当前现状

### 已有保护

- `ConversationItemStreamStore` payload 有 `updated_at`。
- `compacted_history` payload 有 `updated_at`、`compact_boundary_message_id`、`up_to_message_id`。
- `AgentRuntimeService.get_conversation_revision()` 会把 `item_stream.updated_at`、`turn_store.updated_at`、`compacted_history.updated_at`、`context_state.updated_at` 纳入 hash。
- `preview_chat_context -> send_plan -> send_message` 已经用 `conversation_revision` 防止旧 preview 复用到新的 send。

### 缺口

当前 compaction 写入没有在持久化前重新校验输入边界：

- 手动/后台 compact：`ConversationContextCompactionService._compact_conversation()` 读取 `item_stream`，调用模型生成 artifact，然后依次写 `context_state`、`compacted_history`、history event、snapshot、`compact_boundary`。
- agent pre-turn / mid-run compact：`ReActAgent._gather_runtime_compaction_inputs()` 读取 `item_stream`，生成 artifact 后由 `_persist_runtime_compaction()` 写 `context_state`、`compacted_history`、history event、snapshot、`compact_boundary`。
- 上述流程中，模型调用可能耗时；如果期间新消息、tool event、assistant event 或另一个 compact boundary 被写入，旧 artifact 仍会继续写入。

潜在风险：

- pre-turn、mid-run、后台 compact 同时发生时，旧 artifact 可能晚到。
- `compact_boundary` 是追加到 `item_stream` 的系统条目；如果旧 boundary 追加在新 item 后面，`canonical_history()` 会把该 boundary 当作最新边界，可能导致较新的 item 被折叠出 active history。
- `context_state` 也可能被旧输入生成的状态覆盖，不过危害小于旧 boundary 折叠新 item。
- `history_event` / snapshot 当前能看见 compact 发生，但不容易诊断输入边界是否 stale。

## 推荐改法：P1 边界指纹 + 条件提交

### 1. 统一 compaction source fingerprint

生成 compaction artifact 前记录输入指纹：

- `source_item_stream_updated_at`
- `source_item_stream_entry_count`
- `source_active_entry_count`
- `source_latest_message_id`
- `source_boundary_message_id`
- `source_replacement_checkpoint_item_id`

这些字段写入：

- `compacted_history.metadata` 或顶层 `source_*` 字段。
- `compact_boundary.metadata.source_fingerprint`。
- history event / context snapshot detail。

### 2. 持久化前重新校验

提交 artifact 前重新读取当前 `item_stream` 并重算 fingerprint。

推荐判定：

- `current.item_stream.updated_at == source_item_stream_updated_at`
- `current.entry_count == source_item_stream_entry_count`
- `current.latest_message_id == source_latest_message_id`
- `current.boundary_message_id == source_boundary_message_id`

任一不匹配，则判定 stale。

### 3. stale 时跳过写回且不阻断

stale 后不抛给主 agent，不强行重试，不写 `compacted_history`，不追加 `compact_boundary`。

处理方式：

- agent pre-turn / mid-run：跳过本次 formal compaction，保留当前内存上下文继续跑；在 `context_debug` 记录 `*_compaction_skipped=stale_source`。
- 手动 compact：返回当前最新 artifacts 状态，同时写 history event：`manual_compact_stale_skipped`。
- 后台 compact：只写 history event：`auto_compact_stale_skipped`。

### 4. 最小实现边界

优先不重写 metadata 模型，不加数据库迁移。

建议新增一个 runtime 层条件提交方法，避免多次 read-modify-write：

```python
commit_conversation_compaction_if_current(
    conversation_id,
    source_fingerprint,
    context_state,
    compacted_history,
    compact_boundary_entry,
    history_event,
    context_snapshot,
) -> {"committed": bool, "reason": str, "current_fingerprint": dict}
```

该方法在一个 DB session 中：

1. 读取 conversation metadata。
2. 重算当前 item stream fingerprint。
3. fingerprint 匹配才同时写 `context_state`、`compacted_history`、history event、snapshot、item stream boundary。
4. fingerprint 不匹配只写 stale history event，或者返回给调用方写 event。

如果实现时间要进一步压低，也可以先做“写入前重新读取并跳过”的轻量版；但轻量版仍有验证后到追加 boundary 之间的小 race。推荐直接做条件提交方法。

## 参考实现

- Aider：后台 summary 完成后有 stale summary 保护。
- OpenCode：session compaction 有明确 parent / selected history / prior summary。
- Continue：compaction 失败时不阻断当前流程。

本项目对应取法：

- 学 Aider：异步或耗时 summary 完成后必须检查是否 stale。
- 学 OpenCode：artifact 要记录它压缩的是哪一段历史。
- 学 Continue：compaction stale/失败不阻断主流程。

## 验收标准

- 旧 compaction 结果不会覆盖新 item stream 状态。
- compaction skipped/stale 能被日志或 history_event 观察。
- 现有 manual compact 和 mid-run compact 流程不破坏。
- skill prompt / workflow binding / paper-reproduction project-only 流程不受影响。

## 拟修改文件

- `backend/app/services/agent_runtime_service.py`
  - 已增加 item stream fingerprint 和条件提交方法，集中完成 stale 校验与 artifact 写入。
- `backend/app/services/conversation_context_compaction_service.py`
  - 手动/后台 compact 已改为带 source fingerprint 的条件提交。
- `backend/app/services/react_agent.py`
  - pre-turn / mid-run compact 已复用同一条件提交；stale 时 fail open 并写 `context_debug`。
- `backend/tests/test_conversation_context_compaction_service.py`
  - 已覆盖手动 compact stale skip。
- `backend/tests/test_agent_runtime_context_resilience.py`
  - 已覆盖 mid-run stale skip，不污染运行中 context。
- `backend/tests/test_agent_runtime_service.py`
  - 已覆盖 runtime 条件提交成功与 stale skip。

## 已确认决策

- stale manual compact 采用跳过写回且不阻断：不抛错、不阻断主流程，返回当前最新 artifacts，并写 `manual_compact_stale_skipped` history event。
- 接受 runtime 层条件提交方法：`context_state`、`compacted_history`、history event、snapshot、`compact_boundary` 在同一次 metadata 更新中完成。
- 本项不触及 qwen-turbo 调用方式：qwen-turbo 仍负责会话级 compaction artifact 生成，本项只保护 artifact 写回边界。
- ReAct pre-turn / mid-run stale 时，不能把 stale compaction 生成的 context_state 写入运行内存；只有 commit 成功后才刷新 `context.conversation_state` / `context.compacted_history` / `context.messages`。

## 实施记录

- `AgentRuntimeService.build_item_stream_fingerprint()` 生成 source fingerprint，包含 `item_stream.updated_at`、entry count、last item id、active entry count、latest message id、boundary message id、replacement checkpoint item id。
- `AgentRuntimeService.commit_conversation_compaction_if_current()` 在单个 DB session 中重读 `item_stream` 并重算 fingerprint：
  - 匹配：写入 `context_state`、`compacted_history`、history event、context snapshot，并追加 `compact_boundary`。
  - 不匹配：只写 stale history event，不写 `context_state` / `compacted_history` / `compact_boundary`。
- `ConversationContextCompactionService._compact_conversation()` 在模型生成 artifact 前记录 source fingerprint；写回改用 runtime 条件提交。
- `ReActAgent._gather_runtime_compaction_inputs()` 记录 source fingerprint；`_persist_runtime_compaction()` 写回改用 runtime 条件提交。
- ReAct stale skip 时只记录 `context_debug["*_compaction_skipped"] = "stale_source"`，不增加 `mid_run_compactions`，不刷新 active history。

## 验证结果

- `docker compose exec -T backend python -m py_compile app/services/agent_runtime_service.py app/services/conversation_context_compaction_service.py app/services/react_agent.py tests/test_agent_runtime_service.py tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py`
- `docker compose exec -T backend python -m pytest tests/test_agent_runtime_service.py tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py -q`
  - 结果：34 passed。
- `docker compose exec -T backend python -m pytest tests/test_chat_manual_compact_api.py tests/test_chat_context_preview_api.py tests/test_chat_send_api.py -q`
  - 结果：36 passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - 结果：Broad exception guard passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
  - 结果：Contract alignment guard passed。

## 遗留问题

- compaction 单 backend 进程内 queued/running 去重已在 `MOD_04` 实现；跨进程/持久化队列未做。
- qwen-turbo 会话级 compaction 的 pre-turn 调度已在 `MOD_04` 改为后台投递；manual compact 与 mid-run compaction 仍保持原同步语义。
