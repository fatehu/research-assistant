> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 08: Project Tree 确定性紧凑输出

阶段：P2

状态：已实施

更新时间：2026-05-04

## 目标

把 `project_tree` 从“完整目录树 + qwen focused tree”改为确定性 compact tree + candidate files，减少当前 turn token 压力和模型猜不存在路径的概率。

## 背景

`chat/200` 中：

- `project_tree` 成功，但输出估算约 7335 tokens，耗时约 8 秒。
- 模型随后读取了不存在的 `FASTTEXT_REPRODUCTION_REPORT.md`。
- 系统恢复正常，后续读到 `experiment_summary.md` 并给出正确总结。

这说明完整目录树并没有稳定约束模型路径选择。问题不在压缩截断，而在工具给模型的 action surface 太宽。

## 不改什么

- 不改 Project 文件系统根目录语义。
- 不删除 `ToolResult.data["tree"]` 中的完整树，保留给调试和前端需要时使用。
- 不新增 qwen/turbo fallback 或默认关闭开关。
- 不改 `project_claude`、`project_bash`、`project_write_file`。

## 改造方案

1. `project_tree` output 默认不再拼完整 `Tree:`。
2. 删除 `project_tree.focused_tree` qwen 调用。
3. 用本地规则输出：
   - `Candidate files`
   - `Directory summary`
   - `Compact tree`
4. candidate files 优先暴露真实可读路径：
   - 根目录报告类 Markdown。
   - `experiment_summary.md`
   - `reference/paper/*`
   - `reference/repo/readme_intake.json`
   - `repo/source/README.md`
   - 少量 scripts/config/results 文件。
5. `project_read_file` 读不存在路径时返回相近候选，帮助模型纠正路径。

## 验收标准

- `project_tree` 不调用 `LLMService`。
- `project_tree` output 不包含完整递归 `Tree:`。
- `project_tree` data 仍保留完整 `tree`。
- `project_tree` output 包含 `Candidate files` 和 `Compact tree`。
- `project_read_file` 对不存在路径返回 `suggested_paths`。
- Docker focused 回归通过。

## 实施记录

- `ProjectTreeTool` 不再调用 qwen/turbo 目录树整理器，已删除 `_load_project_tree_focus_context` 和 `_summarize_project_tree_for_agent`。
- `project_tree` observation 改为：
  - `Candidate files`
  - `Directory summary`
  - `Compact tree`
  - 一条明确提示：完整递归树未放入 observation，读文件优先使用候选真实路径。
- `ToolResult.data` 继续保留完整递归 `tree`，并新增：
  - `compact_tree`
  - `candidate_files`
  - `directory_summary`
  - `full_tree_in_output=false`
- `focused_tree` 与 `important_paths` 作为兼容字段保留，但内容来自本地确定性结果，不再来自模型。
- `project_read_file` 在 `project_file_not_found` 时返回 `suggested_paths`，用真实项目文件路径引导模型纠错。
- 真实 chat 验证后发现模型最终回答可能把 `Directory summary` 的 examples 混入 `Candidate files` 表达；已把 observation 标题强化为：
  - `Candidate files (verified exact readable paths)`
  - `Directory summary (examples are not candidate files)`
  - `examples (not candidate files)`
  并在 observation 尾部明确提示只有 verified candidate file paths 才能作为 Candidate files 报告。

## 验证结果

- Docker 语法检查通过：
  - `docker compose exec -T backend python -m py_compile app/services/agent_tools_impl/registry.py tests/test_paper_grounding_tools.py`
- Docker focused 回归通过：
  - `docker compose exec -T backend python -m pytest tests/test_paper_grounding_tools.py -q`：31 passed。
  - `docker compose exec -T backend python -m pytest tests/test_paper_reproduction_skill_assets.py tests/test_agent_skill_service.py -q`：10 passed。
  - `docker compose exec -T backend python -m pytest tests/test_agent_tool_ledger_summary.py tests/test_agent_context_budget.py -q`：19 passed。
  - `docker compose exec -T backend python -m pytest tests/test_agent_runtime_context_resilience.py -q`：17 passed。
  - `docker compose exec -T backend python -m pytest tests/test_agent_function_calling_fallback.py tests/test_react_agent_citation_policy.py::test_build_tool_result_ledger_entries_carries_structured_metadata -q`：26 passed。
  - `docker compose exec -T backend python -m pytest tests/test_chat_context_preview_api.py -q`：7 passed。
- Docker 守卫通过：
  - `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
- Docker 真实 Project 10 验证：
  - `project_tree(project_id=10)` 成功，`output_tokens_estimate=448`，`truncated=false`，observation 不含完整 `Tree:`。
  - `candidate_files` 包含 `experiment_summary.md`、`reference/paper/paper_interpretation.md`、`reference/repo/readme_intake.json`、`repo/source/FASTTEXT_REPRODUCTION_REPORT.md`。
  - `project_read_file(project_id=10, relative_path="FASTTEXT_REPRODUCTION_REPORT.md")` 返回 `project_file_not_found`，并建议 `repo/source/FASTTEXT_REPRODUCTION_REPORT.md`。
- 真实 chat 页面第一次验证：
  - `conversation_id=200` 中只调用 `project_tree({"project_id":10})`，未调用 `project_read_file` 或其他工具。
  - 工具结果成功，`output_tokens_estimate=448`，`truncated=false`。
  - 模型最终回答把 `Directory summary` 的 `data/` examples 写进了 Candidate Files 表达，因此追加了字段边界强化。
- 真实 chat 页面第二次验证：
  - 同一 `conversation_id=200` 里再次要求调用同一工具时，模型复用上一轮成功 observation，未重新调用工具；这是防重复工具调用策略的预期表现。
  - 新建 `conversation_id=201` 后重新测试，实际调用 `project_tree({"project_id":10})`，工具结果成功，`output_tokens_estimate=518`，`truncated=false`。
  - 最终回答的 Candidate files 只包含 verified exact readable paths，未再混入 `data/` examples。

## 遗留问题

- 完整递归 `tree` 仍在 `ToolResult.data` 中生成，当前保留给调试和兼容；如果后续真实项目目录继续膨胀，可另开修改项把 data 里的完整树也改为按需生成或分页。
