> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 05: 上下文预算与退化观测统一

阶段：P3

状态：已实施，Docker focused 回归通过

## 目标

统一记录上下文预算、裁剪、模型压缩失败、stale 跳过和确定性裁剪的触发原因，方便后续诊断 agent 是否因为窗口压力退化。

## 不改什么

- 不改变 P0 的 tool ledger 摘要策略。
- 不改变当前模型选择。
- 不引入新的外部观测系统。

## 推荐方向

在 context debug / history event / logs 中区分：

- deterministic truncation
- model compaction
- compaction skipped / stale
- stale compaction skipped
- qwen compaction failure
- token budget overflow

记录关键指标：

- input token estimate before/after
- effective budget
- compacted message count
- qwen latency
- qwen failure count
- skipped count

## 执行方案

本项只补观测，不改变压缩、裁剪、模型选择和 tool 路由。

### 1. 统一 context debug 事件

在 `context_debug` 中新增稳定字段：

- `context_observability_version`
- `context_observability_events`
- `message_tokens_before_trim`
- `message_tokens_after_trim`
- `deterministic_truncation_applied`
- `deterministic_truncation_reasons`
- `token_budget_overflow_after_trim`

事件使用小而固定的 schema：

- `kind`
- `phase`
- `reason`
- `mode`
- `trigger`
- `source`
- `input_tokens_before`
- `input_tokens_after`
- `effective_budget`
- `compacted_messages`
- `summary_chars`
- `source_entry_count`
- `current_entry_count`

### 2. 记录本地确定性裁剪

`_prepare_llm_messages()` 在以下路径补事件：

- budget disabled
- budget checked
- older / recently slid / recent history deterministic system compression
- deterministic content truncation
- after-trim token overflow

### 3. 记录模型 compaction 状态

运行内 compaction 在 `context_debug` 中补事件：

- model compaction committed
- model compaction skipped because stale
- pre-turn compaction deferred / skipped

后台/manual compaction 在 history event detail 和日志中补统一 `event=` / `reason=` 字段：

- `model_compaction_committed`
- `model_compaction_skipped`
- `model_compaction_failed`

### 4. 不纳入本项

- 不做 UI 展示。
- 不改数据库 schema。
- 不接入 Prometheus/Sentry/外部 telemetry。
- 不修改 qwen-turbo 会话级 compaction。
- 不修改 notebook/codelab/notebook-agent 产品面。

## 参考实现

- Gemini CLI：tool output masking / truncation 有明确 telemetry event。
- OpenCode：session compaction 有事件和状态。
- Continue：auto compaction 有成功/失败处理。

## 验收标准

- 能从日志或 debug payload 判断本轮是否触发压缩。
- 能区分模型压缩失败、stale 跳过和本地确定性裁剪。
- 不增加主链额外模型调用。
- focused tests 覆盖：
  - deterministic truncation debug event。
  - pre-turn deferred/skipped debug event。
  - runtime compaction stale skip event。
  - manual/background history detail 包含统一 event/reason。

## 实施记录

2026-05-05 已实施：

- `backend/app/services/react_agent.py`
  - 新增 `context_observability_version=context_observability.v1`。
  - 新增 `context_observability_events`，统一记录 budget check、deterministic compression/truncation、pre-turn deferred/skipped、runtime model compaction committed/skipped。
  - 新增 `message_tokens_before_trim`、`message_tokens_after_trim`、`deterministic_truncation_applied`、`deterministic_truncation_reasons`、`token_budget_overflow_after_trim`。
  - `_prepare_llm_messages()` 在本地 head/tail/系统摘要裁剪路径记录 deterministic 事件，不增加模型调用。
  - `_maybe_pre_turn_compact()` 记录 pre-turn compaction deferred/skipped 原因。
  - runtime compaction 提交和 stale skip 写入同一事件 schema。
- `backend/app/services/conversation_context_compaction_service.py`
  - manual/background compaction 的 history detail 和日志补充 `event=model_compaction_*` 与 `reason=*`。
  - worker start、item stream missing、异常失败日志补统一事件名。
- `backend/app/services/agent_runtime_service.py`
  - stale source 条件提交失败时，在 history detail 中补 `event=model_compaction_skipped` 和 `current_entry_count`。
- 测试同步：
  - `test_agent_context_budget.py` 覆盖 deterministic content truncation 事件。
  - `test_agent_runtime_context_resilience.py` 覆盖 pre-turn deferred/skipped 事件。
  - `test_conversation_context_compaction_service.py` 覆盖 stale skip history detail。
  - `test_agent_runtime_service.py` 覆盖 stale skip current fingerprint 指标。

## 验证结果

本地 focused 回归通过：

- `python3 -m py_compile backend/app/services/react_agent.py backend/app/services/conversation_context_compaction_service.py backend/app/services/agent_runtime_service.py backend/tests/test_agent_context_budget.py backend/tests/test_agent_runtime_context_resilience.py backend/tests/test_conversation_context_compaction_service.py backend/tests/test_agent_runtime_service.py`
- `git diff --check`
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_agent_context_budget.py -q`：15 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_agent_runtime_context_resilience.py -q`：17 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_conversation_context_compaction_service.py -q`：15 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_agent_runtime_service.py -q`：7 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_chat_send_api.py -q`：20 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_chat_manual_compact_api.py -q`：9 passed。
- `backend/.venv-incremental/bin/python -m pytest backend/tests/test_agent_function_calling_fallback.py -q`：25 passed。
- `backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py`：passed。
- `backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py`：passed。

Docker focused 回归通过：

- `docker compose exec -T backend python -m py_compile app/services/react_agent.py app/services/conversation_context_compaction_service.py app/services/agent_runtime_service.py tests/test_agent_context_budget.py tests/test_agent_runtime_context_resilience.py tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_service.py`
- `docker compose exec -T backend python -m pytest tests/test_agent_context_budget.py tests/test_agent_runtime_context_resilience.py tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_service.py -q`：54 passed。
- `docker compose exec -T backend python -m pytest tests/test_chat_send_api.py tests/test_chat_manual_compact_api.py tests/test_agent_function_calling_fallback.py -q`：54 passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`：passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`：passed。

说明：

- 使用 `docker compose exec -T backend python backend/checks/...` 和 `python checks/...` 跑守卫会因容器工作目录不是仓库根而失败；已用仓库根挂载方式通过。

## 遗留问题

- 本项只写 debug/history/log，不做 UI 展示；如果后续要在管理界面查看这些事件，需要另开 UI 小项。
- 未接入外部 telemetry；如果后续需要按天统计 skipped/failed count，再另开观测汇总项。
