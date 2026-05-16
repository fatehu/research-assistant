> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# Tool Ledger qwen-turbo P0 改造方案

更新时间：2026-05-04

本文只讨论一个问题：`tool_ledger` 的单次工具结果摘要是否还应该同步调用 qwen-turbo，以及如果不应该，具体怎么改。

> 当前状态说明：本文是 `MOD_01` 的实施前背景方案。`MOD_01` 已完成 tool ledger 确定性摘要；`MOD_07` 已进一步移除 ReAct prompt budget 裁剪中的 `_compress_text_with_qwen_turbo` 源码入口。后续执行以 `WINDOW_WORK_OUTLINE_ZH.md` 和具体 `MOD_XX` 文件为准。

## 结论

推荐方案：**P0 先把 `tool_ledger` 的单次工具结果摘要改为确定性规则摘要，qwen-turbo 只保留在会话级 compaction 路径。**

也就是说：

- 移除热路径调用：`source="chat.tool_result_ledger_summary"`。
- 保留会话压缩调用：`source="chat_compaction.context_state"` 和 `source="chat_compaction.compacted_history"`。
- 不在 P0 引入异步 tool pair summary；只预留字段和后续入口。

核心边界：

- `tool observation`：当前 turn 继续推理用，仍然要保留必要工具输出。
- `tool_ledger.summary`：长期账本索引，不承担完整语义总结，必须稳定、短、快、失败安全。

## 当前本地落点

关键路径：

- `backend/app/services/react_agent.py`
  - `_tool_result_ledger_summary_text(...)`
  - `_build_tool_result_ledger_entries(...)`
  - `_execute_tool_calls(...)`

当前行为：

1. `_execute_tool_calls(...)` 执行工具。
2. `_build_tool_result_ledger_entries(...)` 遍历每个 `ExecutedToolCall`。
3. 每个工具结果都会 `await _tool_result_ledger_summary_text(item)`。
4. `_tool_result_ledger_summary_text(...)` 在非结构化 debug detail 时调用：
   - `_compress_text_with_qwen_turbo(...)`
   - `source="chat.tool_result_ledger_summary"`
   - `target_token_budget=120`

风险：

- 单个工具结果入账依赖额外模型调用。
- 多工具并发执行后，ledger 入账仍会逐条等待摘要。
- qwen 延迟、限流、失败会污染 FC/tool loop 的稳定性。
- 这类摘要不是当前 turn 必需信息，放在热路径收益不足。

## 参考仓库

### Gemini CLI

参考文件：

- `tmp/external-context-repos/gemini-cli/packages/core/src/context/toolOutputMaskingService.ts`
- `tmp/external-context-repos/gemini-cli/packages/core/src/context/toolDistillationService.ts`

做法：

- 工具输出先走本地 masking / truncation / offload。
- 保护最新工具输出窗口。
- 只有超大输出才可选生成 intent summary。
- intent summary 有阈值和 timeout，失败不影响结构化截断。

可借鉴点：

- 工具输出处理先确定性，再考虑模型摘要。
- 模型摘要是附加优化，不是工具结果入账前置条件。

### OpenCode

参考文件：

- `tmp/external-context-repos/opencode/packages/opencode/src/session/compaction.ts`
- `tmp/external-context-repos/opencode/packages/opencode/src/session/message-v2.ts`

做法：

- `SessionCompaction.prune(...)` 扫描旧 tool part，保护最近 turn，达到阈值后标记 compacted。
- `truncateToolOutput(...)` 在进入 compaction prompt 前本地截断工具输出。
- compacted 旧工具结果给模型时变成占位文本，而不是完整输出。

可借鉴点：

- 工具输出进入长期上下文前先本地裁剪。
- 会话级 compaction 才调用模型。

### Goose

参考文件：

- `tmp/external-context-repos/goose/crates/goose/src/context_mgmt/mod.rs`

做法：

- `compact_messages(...)` 是会话级模型压缩。
- `do_compact(...)` 在压缩失败时逐步移除中间 tool response，降低上下文长度。
- `maybe_summarize_tool_pairs(...)` 会异步总结旧 tool request/response pair。
- `tool_ids_to_summarize(...)` 明确保护最后 N 个 tool calls，不总结当前 turn。

可借鉴点：

- 如果未来需要模型总结工具对，也应该异步、批量、保护当前 turn。
- 这不是 P0，最多是 P2。

### Continue

参考文件：

- `tmp/external-context-repos/continue/extensions/cli/src/compaction.ts`
- `tmp/external-context-repos/continue/extensions/cli/src/stream/streamChatResponse.autoCompaction.ts`

做法：

- `compactChatHistory(...)` 是 session history compaction。
- compaction 失败时继续使用原 history，不阻塞当前流程。

可借鉴点：

- 模型压缩失败不应成为主流程硬失败。

### OpenHands / Aider

参考文件：

- `tmp/external-context-repos/openhands-sdk-dist/extracted/openhands/sdk/context/condenser/llm_summarizing_condenser.py`
- `tmp/external-context-repos/aider/aider/history.py`
- `tmp/external-context-repos/aider/aider/coders/base_coder.py`

做法：

- LLM summarizer/condenser 面向旧事件或旧聊天历史。
- 不是每个工具结果同步入账时调用。

可借鉴点：

- 模型摘要应作为 history/session condenser，而不是 ledger writer。

### Codex / Claude Code

参考文件：

- `tmp/reference-repos/codex/sdk/typescript/src/items.ts`
- `tmp/reference-repos/codex/codex-rs/rollout/src/policy.rs`
- `tmp/reference-repos/claude-code-sourcemap/restored-src/src/utils/analyzeContext.ts`
- `tmp/reference-repos/claude-code-sourcemap/restored-src/src/utils/attachments.ts`

做法概括：

- tool call / tool result 是结构化 item 或 content block。
- 后续上下文管理围绕这些结构化事件做 replay、analysis、filter、compact。

可借鉴点：

- `tool_ledger` 不是首创概念，而是本仓库对工具事件事实层的显式拆分。
- 账本层更应该记录确定性字段，而不是每条都走模型解释。

## 方案选项

### 方案 A：彻底本地化 tool_ledger summary

做法：

- `_tool_result_ledger_summary_text(...)` 不再是模型摘要器。
- 它只拼接：
  - tool name
  - status
  - path / execution_id / page / chunk
  - result_count / source_labels
  - error / authorization
  - structured validation detail
  - observation 的短 preview
- 最终硬限制长度，例如 600-900 chars。

优点：

- 最稳定。
- 没有额外模型延迟和失败面。
- 测试简单。

缺点：

- 对长自然语言 observation 的“深层语义”提炼能力变弱。

### 方案 B：本地化为默认，保留关闭的模型摘要开关

做法：

- 新增配置，例如 `agent_tool_ledger_model_summary_enabled=False`。
- 默认完全本地化。
- 显式开启时才允许 `_tool_result_ledger_summary_text(...)` 调 qwen。

优点：

- 回滚和 A/B 方便。
- 如果发现某些工具确实需要模型摘要，可以局部恢复。

缺点：

- 保留了热路径重新引入模型依赖的入口。
- 需要维护一个默认不用的分支。

### 方案 C：本地化 + 异步 old tool pair summary

做法：

- P0 做本地 ledger summary。
- P2 新增异步 worker，参考 Goose：
  - 只处理旧 tool pairs。
  - 保护最近 N 个 tool calls / 当前 turn。
  - 批量执行。
  - 写入新的 `tool_pair_summary` item 或 context_state evidence，而不是覆盖原 ledger。

优点：

- 兼顾稳定性和长期语义压缩。
- 符合 Goose 的成熟边界。

缺点：

- P0 不应直接做，否则改动面扩大。

## 推荐方案

推荐采用 **方案 A + P2 预留**。

具体含义：

- P0 直接移除同步 qwen 调用。
- 不加热路径模型摘要开关。
- 保留 `_compress_text_with_qwen_turbo(...)` 给会话级 compaction 使用。
- 文档中记录 P2 可以参考 Goose 做异步旧工具对总结。

不推荐方案 B，原因是它看似保险，实际会让热路径重新出现一个“默认关闭但以后可能被误开”的复杂度入口。

## P0 具体改法

### 1. 改 `_tool_result_ledger_summary_text`

建议保留函数名和 async 签名，降低调用点改动。

内部逻辑改成：

1. 生成稳定字段：
   - `tool=...`
   - `status=成功|失败|需授权`
   - `relative_path=...`
   - `execution_id=...`
   - `page=...`
   - `chunk_index/total_chunks=...`
   - `result_count=...`
   - `source_labels=...`

2. 优先使用结构化 detail：
   - `structured_validation_errors`
   - `schema_errors`
   - `grounding_conflicts`
   - `draft_errors`
   - `global_errors`
   - `allowed_paths`

3. 没有结构化 detail 时，使用本地 preview：
   - 失败：优先 `error`
   - 授权：优先 permission text
   - 成功：使用 `observation_output` 的 head/tail 或前 N 字符

4. 硬限制最终长度：
   - 普通摘要：约 600 chars。
   - 结构化错误：约 1200 chars。

5. 不调用：
   - `_compress_text_with_qwen_turbo(...)`
   - `LLMService(...)`
   - 任何 provider。

### 2. 不改 observation 主链

P0 不改变：

- `item.tool_message`
- `context.messages.append(item.tool_message)`
- 当前 turn 给模型看的 tool observation。

原因：

- 这次只收 `tool_ledger.summary`。
- observation 裁剪是另一个问题，应该跟 context budget / tool output masking 一起讨论。

### 3. 不改 compaction 模型调用

P0 不改变：

- `_extract_context_state(...)`
- `_extract_compacted_history(...)`
- `_build_system_compression_message(...)`
- `agent_budget_compression_model`

原因：

- qwen-turbo 做会话级压缩是主流路线。
- 本次只处理 per-tool-result 热路径调用。

### 4. 可加 metadata hint，但不强制

可选添加：

- `metadata["ledger_summary_mode"] = "deterministic.v1"`
- `metadata["ledger_preview_truncated"] = true|false`
- `metadata["async_summary_candidate"] = true|false`

是否加这些字段需要讨论。P0 最小实现可以不加，避免 metadata 行为变化。

## 验收标准

P0 完成后应满足：

- `rg "chat.tool_result_ledger_summary" backend/app` 找不到热路径调用。
- 工具结果入账不依赖 qwen-turbo。
- qwen-turbo 故障不影响 `_build_tool_result_ledger_entries(...)`。
- `tool_ledger.summary` 仍包含 tool、status、关键路径、错误和短 preview。
- FC 工具调用测试仍通过。
- compaction 相关测试仍通过。

建议测试：

- monkeypatch `_compress_text_with_qwen_turbo` 直接抛错，确认 tool ledger 仍能生成。
- 大 observation 输入，确认 summary 被本地截断。
- structured validation errors 输入，确认摘要包含关键错误和 allowed paths。
- authorization_required 输入，确认 status 和 permission text 稳定。

## 待讨论问题

1. P0 是否采用“保留 async 签名、内部无 await”的最小改法？
2. 是否需要在 `metadata` 里写 `ledger_summary_mode=deterministic.v1`？
3. 本地 preview 的硬限制用 600 chars 还是 900 chars？
4. 是否明确把 Goose 风格异步 tool pair summary 放到 P2，不进入本次改动？
