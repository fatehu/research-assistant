> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 07: Budget 路径确定性裁剪

阶段：P2

状态：已实施，Docker focused 与真实 API skill 回归通过

更新时间：2026-05-04

## 目标

收掉 ReAct prompt budget 裁剪路径里的 qwen-turbo 调用，让本轮 prompt 构造不因为预算摘要/消息裁剪等待额外模型。

本项只处理：

- `source="chat.budget.message_summary"`
- `source="chat.budget.message_truncation"`

## 不改什么

- 不删除会话级 `context_state / compacted_history` qwen-turbo compaction。
- 不改变 manual compact API 的同步语义。
- 不把 mid-run compaction 改成后台任务。
- 不处理 `project_tree.focused_tree` 的目录树整理模型调用。
- 不新增备用模型分支或默认关闭开关。

## 当前不足

`backend/app/services/react_agent.py` 中：

- `_build_system_compression_message()` 在摘要超出阈值时调用 `_compress_text_with_qwen_turbo(... source="chat.budget.message_summary")`。
- `_truncate_message_content_to_token_budget()` 在单条消息超预算时调用 `_compress_text_with_qwen_turbo(... source="chat.budget.message_truncation")`。
- 两者都发生在 `_prepare_llm_messages()` 的 prompt 构造/预算裁剪阶段，属于当前 turn 主链附近。

这和本轮调研结论不一致：成熟实现通常把模型用于 session/history compaction，而预算裁剪、工具输出截断、tail 选择优先用确定性规则完成。

## 参考实现结论

- Continue：模型用于 `compactChatHistory()`；为让 compaction prompt 放得下，先用确定性 `pruneLastMessage()`。
- OpenCode：模型用于 `SessionCompaction`；tail budget、旧工具输出 prune、`toolOutputMaxChars` 是确定性规则。
- OpenHands：模型用于 `LLMSummarizingCondenser`；普通长文本用 `maybe_truncate()` 做 head/tail 裁剪。
- Goose：模型用于会话级 compact 和旧 tool pair 批量总结；不在每次 prompt budget 裁剪时逐条同步压缩。
- Gemini CLI：超大 tool output 先本地 truncation/offload；secondary LLM intent summary 是阈值触发的附加增强，失败忽略。

## 改造方案

1. 删除 ReAct budget helper 对 `_compress_text_with_qwen_turbo()` 的依赖。
2. `_build_system_compression_message()` 保留现有 `_summarize_messages()` 规则摘要，再用本地 head/tail 裁剪保证摘要不超过预算。
3. `_truncate_message_content_to_token_budget()` 改成本地 head/tail 裁剪：
   - 保留前段和尾段。
   - 插入固定 marker，记录 role、kind、原估算 token 和目标 token。
   - 不调用任何 LLM provider。
4. 保持 `_prepare_llm_messages()` 的窗口划分、recent turn 保护、context debug 结构不变。

## 验收标准

- `rg "chat\\.budget\\.message_summary|chat\\.budget\\.message_truncation" backend/app` 无命中。
- budget 裁剪测试中 monkeypatch `LLMService` 抛错时仍能完成。
- 大 observation 被确定性裁剪，并保留 truncation marker。
- older/recent history 仍能生成系统压缩消息，不静默丢失。
- Docker focused 回归通过。
- paper-reproduction skill 主线回归不退回 notebook/workspace/execution 路线。

## 实施记录

- `backend/app/services/react_agent.py`
  - 删除 ReAct budget helper 中的 `_compress_text_with_qwen_turbo()`。
  - 新增 `_truncate_text_head_tail_to_token_budget()`，用二分选择 head/tail 保留长度，确保结果落入目标 token 预算。
  - `_build_system_compression_message()` 保留 `_summarize_messages()` 规则摘要；摘要仍超预算时插入 `system-compression-summary-truncated` marker 并做本地 head/tail 裁剪。
  - `_truncate_message_content_to_token_budget()` 改为本地 head/tail 裁剪，插入 `system-compression-truncated` marker，记录 `role/kind/original_tokens/target_tokens`。
- 测试同步：
  - `test_agent_context_budget.py` 增加预算系统摘要不调用 `LLMService` 的回归。
  - `test_agent_function_calling_fallback.py` 将旧 qwen 超时测试改为 budget 裁剪不调用 `LLMService` 的回归。
  - `test_agent_tool_ledger_summary.py` 改为从 provider 层断言 ledger 摘要不调用 `LLMService`。

## 验证结果

- `docker compose exec -T backend python -m py_compile app/services/react_agent.py tests/test_agent_context_budget.py tests/test_agent_function_calling_fallback.py tests/test_agent_tool_ledger_summary.py`
  - 结果：passed。
- `docker compose exec -T backend python -m pytest tests/test_agent_context_budget.py tests/test_agent_function_calling_fallback.py tests/test_agent_tool_ledger_summary.py -q`
  - 结果：44 passed。
- `docker compose exec -T backend python -m pytest tests/test_conversation_context_compaction_service.py tests/test_agent_runtime_context_resilience.py tests/test_chat_send_api.py tests/test_chat_manual_compact_api.py tests/test_chat_context_preview_api.py -q`
  - 结果：68 passed。
- `docker compose exec -T backend python -m pytest tests/test_paper_reproduction_skill_assets.py tests/test_paper_grounding_tools.py tests/test_agent_skill_service.py -q`
  - 结果：40 passed。
- `rg "chat\\.budget\\.message_summary|chat\\.budget\\.message_truncation|_compress_text_with_qwen_turbo" backend/app backend/tests`
  - 结果：无命中。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - 结果：Broad exception guard passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
  - 结果：Contract alignment guard passed。
- Docker 真实 API paper-reproduction skill 回归：
  - 登录开发账号 `yuiooyww@gmail.com / 123456`。
  - 新建会话 `conversation_id=199`。
  - 请求查看 `paper_id=113` 的 Project + Claude Code + sandbox 主线状态。
  - 结果：工具结果只包含 `paper_research_status`；未调用旧 execution 工具；回答包含 Project ID 10、`project_claude` 下一步，并明确当前不处于 notebook/workspace/execution 路线。

## 遗留问题

- 会话级 `context_state / compacted_history` qwen-turbo compaction 仍保留。
- manual compact 仍保持同步 API 语义。
- mid-run compaction 仍保持同步兜底，本项未改。
- `project_tree.focused_tree` 的目录树整理模型调用仍保留，本项未覆盖。
