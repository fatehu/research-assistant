> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 01: Tool Ledger 确定性摘要

阶段：P0

状态：已实施，Docker API 回归通过

## 目标

把 `tool_ledger.summary` 的单次工具结果摘要从同步 qwen-turbo 调用改为确定性规则摘要。

## 不改什么

- 不改当前 turn 给模型看的 tool observation。
- 不改 `context_state` / `compacted_history` 的 qwen-turbo 会话级压缩。
- 不新增异步 old tool pair summary。
- 不重新打开 XML fallback。
- 不改 skill 激活、workflow binding、decision_state gate、tool override 和 skill session prompt 注入。
- 不改变任何 `result_data` / `metadata` 的原始持久化内容。

## 当前问题

当前 `backend/app/services/react_agent.py` 中：

- `_build_tool_result_ledger_entries(...)` 会逐个工具结果等待 `_tool_result_ledger_summary_text(...)`。
- `_tool_result_ledger_summary_text(...)` 在普通工具结果上调用 `_compress_text_with_qwen_turbo(...)`。
- 调用 source 是 `chat.tool_result_ledger_summary`。

这让 tool ledger 入账依赖额外模型延迟、限流和失败。

## 现在存在的不足

### 1. 热路径依赖 qwen-turbo

不足：

- 每个 tool result 入账都可能触发一次 qwen-turbo。
- 多工具并发执行完成后，ledger 写入仍会串行等摘要。
- qwen timeout、限流、网络抖动会影响 FC/tool loop。

要改成：

- tool result 入账只做本地确定性摘要。
- qwen-turbo 只保留在会话级 `context_state / compacted_history` 压缩。

### 2. 摘要语义不可预测

不足：

- 模型摘要可能改写、漏掉或重排路径、ID、stage、status。
- 对 prompt 业务流来说，这些字段比自然语言流畅度更重要。

要改成：

- 摘要前半段固定放结构化锚点。
- 自然语言 observation 只作为 preview，不作为主事实来源。

### 3. compaction preview 只截取 summary 前 220 字符

不足：

- `ConversationContextCompactionService._tool_ledger_to_state_preview(...)` 会把 `summary` 压到 220 字符。
- 如果关键字段被放在摘要后面，进入 `tool_ledger_preview` 时会丢失。

要改成：

- 第一行必须是高密度 key/value 锚点。
- `project_id / literature_review_id / execution_id / review_path / block_ids` 等必须优先出现在前 220 字符内。

### 4. skill 流程不可完全自动测试

不足：

- paper-reproduction、literature-review、artifact-parallel-writing 很多行为由 prompt 约束驱动。
- 单元测试只能防止字段被抹掉，不能证明模型一定按 workflow 走。

要改成：

- 单测覆盖字段保真。
- Docker 人工回归覆盖“继续”类 prompt 流程。
- 文档记录账号、场景、预期行为和回归结果。

## Skill 流程风险

本项不能按“普通摘要优化”处理。当前 skill 流程有大量 prompt 约束，无法只靠单元测试证明完整正确。

主要风险：

- `tool_ledger.summary` 会进入 `ConversationContextCompactionService._tool_ledger_to_state_preview(...)`。
- `tool_ledger.summary` 也会进入 `_tool_rows_to_evidence_candidates(...)`，成为 `evidence_ledger` 候选摘要。
- `context_state / evidence_ledger / decision_state` 之后会进入 prompt，影响用户说“继续”时的方向。
- skill 流程依赖工具 observation 中的路径、review id、project id、execution id、status、stage、block id 等信号。

已确认的保护点：

- paper skill 的 `workflow_binding` 主要由 `result.data` 中的 `paper/project/workspace/status_summary/background_execution` 派生，不直接依赖 `tool_ledger.summary`。
- `active_skill_names` 持久化和 skill prompt 注入不依赖 `tool_ledger.summary`。
- `decision_state` 有一部分由 `_build_tool_workflow_summary(...)` 和 workflow binding 合并产生，不完全依赖 ledger summary。

但仍需保护：

- compaction 后继续对话时，`tool_ledger.summary` 仍可能是旧工具结果进入 `context_state/evidence_ledger` 的短线索。
- 因此确定性摘要不能只保留 `tool/status`，必须保留各 skill 的业务锚点。

## Skill-critical 摘要锚点

P0 的确定性摘要至少要覆盖这些锚点：

- 通用工具：
  - `tool`
  - `status`
  - `success`
  - `error`
  - `permission_required`
  - `execution_time_ms`
  - `output_tokens_estimate`
  - `truncated`

- 文件 / Project 工具：
  - `relative_path`
  - `repo_relative_path`
  - `path`
  - `line_number`
  - `line_start`
  - `line_end`
  - `allowed_paths`

- paper-reproduction：
  - `paper_id`
  - `project_id`
  - `workspace_id`
  - `notebook_id`
  - `current_stage`
  - `execution_id`
  - `background_execution.execution_id`
  - `background_execution.stage`
  - `background_execution.status`
  - `status_summary.baseline_execution_id`
  - `status_summary.tuning_execution_id`

- literature-review：
  - `literature_review_id`
  - `paper_key`
  - `pdf_path`
  - `md_path`
  - `report_path`
  - `review_path`
  - `relative_path`
  - `page_count`
  - `character_count`
  - `result_count`

- artifact-parallel-writing：
  - `artifact_id`
  - `block_id`
  - `block_ids`
  - `updated_blocks`
  - `status`
  - write failure detail

这些字段优先来自 `result_data` 和 `metadata`，不是从自然语言 observation 里猜。

## 参考实现

- Gemini CLI：工具输出优先本地 truncation / masking / offload；模型 intent summary 只做超大输出的可选附加。
- OpenCode：旧工具结果进入 compaction 前先本地截断；session compaction 才用模型。
- Goose：模型总结 tool pair 是异步、批量、保护当前 turn，不是同步 per-result。
- Continue：会话 compaction 失败不阻断主流程。

详细依据见 `AGENT_TOOL_LEDGER_QWEN_P0_PROPOSAL_ZH.md`。

## 推荐改法

保留 `_tool_result_ledger_summary_text(...)` 的函数名和 async 签名，降低调用点变化。

但实现方式必须从“通用本地截断”收紧为“工具关键字段模板 + 本地 preview”。

内部改为：

1. 拼接稳定字段：
   - `tool`
   - `status`
   - `relative_path` / `repo_relative_path`
   - `execution_id`
   - `page` / `chunk_index`
   - `result_count`
   - `source_labels`

2. 优先使用结构化 detail：
   - validation errors
   - schema errors
   - grounding conflicts
   - draft/global errors
   - allowed paths

3. 无结构化 detail 时使用本地 preview：
   - 失败优先 `error`
   - 授权优先 permission text
   - 成功使用 observation 的短 preview

4. 对 skill-critical 工具走字段模板：
   - `paper_research_*`
   - `project_*`
   - `literature_review_*`
   - `document_artifact_*`

5. 摘要硬限制：
   - 普通摘要候选：待讨论，建议 600 或 900 chars。
   - 结构化错误摘要：待讨论，建议 1200 chars。

## 代码实施步骤

### Step 1: 保留调用面，替换内部实现

修改文件：

- `backend/app/services/react_agent.py`

保留：

- `_tool_result_ledger_summary_text(cls, item: ExecutedToolCall) -> str`
- `_build_tool_result_ledger_entries(...)` 的调用方式
- `_tool_result_detail_for_ledger(...)`
- `_tool_result_has_structured_debug_detail(...)`

移除：

- `_tool_result_ledger_summary_text(...)` 内部对 `_compress_text_with_qwen_turbo(...)` 的调用。
- `source="chat.tool_result_ledger_summary"` 这条热路径。

### Step 2: 新增确定性摘要 helper

建议在 `react_agent.py` 中新增本地 helper，全部为纯函数/类方法：

- `_tool_result_status_label(item)`
  - 输出 `成功 / 失败 / 需授权`。

- `_tool_result_anchor_parts(item)`
  - 收集通用字段和基础路径字段。
  - 输出形如 `["tool=...", "status=...", "project_id=..."]`。

- `_tool_result_skill_anchor_parts(item)`
  - 按工具族补充 skill-critical 字段。
  - `paper_research_* / project_* / literature_review_* / document_artifact_*` 分支明确。

- `_tool_result_preview_for_ledger(item, limit=240)`
  - 本地生成 observation preview。
  - 失败优先 `error`，授权优先 permission 文本，成功只取短 head/tail。

- `_join_tool_ledger_summary(anchor_parts, detail, preview, limit)`
  - 负责最终拼接和硬截断。

### Step 3: 摘要布局固定

目标格式：

```text
tool=paper_research_status | status=成功 | project_id=6 | paper_id=113 | current_stage=ready | reference_ready=true
Detail:
- ...
Preview:
...
```

布局规则：

- 第一行只放 key/value 锚点。
- 第一行必须尽量控制在 220 字符内。
- `Detail` 只放结构化错误、allowed_paths、workflow hint。
- `Preview` 只放 observation 短摘，不放完整输出。

### Step 4: 字段来源优先级

字段来源优先级：

1. `item.result_data`
2. `item.metadata`
3. `item.arguments`
4. `item.error`
5. `item.observation_output` preview

禁止：

- 从 observation 长文本里用复杂正则猜 project/review/block 状态。
- 把 preview 当成主事实。

### Step 5: 保守处理 metadata

默认建议：

- P0 不新增 metadata 字段，先减少行为变化。

如果需要观测，再加：

- `ledger_summary_mode=deterministic.v1`
- `ledger_preview_truncated=true|false`

但这一步应单独确认，不和摘要逻辑绑死。

## 目标形态示例

### paper-reproduction status

现在的问题形态：

- qwen 可能把结果改写成“项目已经准备好了”，但漏掉 `project_id/current_stage/reference_ready`。

目标形态：

```text
tool=paper_research_status | status=成功 | paper_id=113 | project_id=6 | current_stage=ready | reference_ready=true
Preview:
Project reference bundle ready; use project_claude as worker.
```

### execution started

目标形态：

```text
tool=paper_research_start_execution | status=成功 | project_id=6 | execution_id=baseline-001 | background_execution_id=run-abc | background_stage=baseline_repro | background_status=running
Preview:
后台 execution 已启动；后续继续观察 execution 状态。
```

### literature review PDF to Markdown

目标形态：

```text
tool=literature_review_pdf_to_markdown | status=成功 | literature_review_id=review-xxx | paper_key=abc | md_path=md/abc.md | report_path=md/abc.json | page_count=12 | character_count=45678
```

### artifact batch update

目标形态：

```text
tool=document_artifact_update_blocks | status=成功 | artifact_id=artifact-xxx | updated_blocks=block_a,block_b,block_c
Preview:
已批量更新 3 个 blocks。
```

### failure

目标形态：

```text
tool=document_artifact_update_blocks | status=失败 | error=document_artifact_update_failed
Detail:
- block_id 不存在或 updates 校验失败；需要重新 document_artifact_read 确认 block_ids。
```

## 额外验收要求

除了普通单测，还必须有 skill 流程保护：

- paper-reproduction：
  - `paper_research_status` 或 `paper_research_prepare` 的 ledger summary 保留 `project_id/current_stage`。
  - `paper_research_start_execution` 的 ledger summary 保留 `background_execution.execution_id/stage/status`。
  - `project_claude` 的 ledger summary 保留执行结果和后续动作所需 ID。

- literature-review：
  - `literature_review_start` 保留 `literature_review_id`。
  - `literature_review_pdf_to_markdown` 保留 `md_path/report_path/page_count`。
  - `review_writer` 保留 `review_path` 或 final path。

- artifact-parallel-writing：
  - `document_artifact_read` 保留 block id/title 线索。
  - `document_artifact_update_blocks` 保留更新 block ids 和失败 detail。

这些测试不能证明 prompt 业务流完全正确，但可以防止关键锚点被摘要改动抹掉。

## 是否写 metadata

待讨论。

可选字段：

- `ledger_summary_mode=deterministic.v1`
- `ledger_preview_truncated=true|false`
- `async_summary_candidate=true|false`

最小 P0 可以不写 metadata，避免扩大行为面。

## 验收标准

- `rg "chat.tool_result_ledger_summary" backend/app` 不再命中热路径调用。
- monkeypatch `_compress_text_with_qwen_turbo` 抛错时，tool ledger 仍能生成。
- 大 observation 能被本地截断。
- 失败、授权、结构化错误工具结果摘要仍有可读关键信息。
- skill-critical 工具摘要保留继续流程所需 ID、路径、stage、status。
- FC 工具调用相关测试通过。

## 人工回归场景

因为 skill 是 prompt 约束业务流，P0 实施后还需要 Docker 中做人工场景检查：

Docker 开发环境临时账号：

- email: `yuiooyww@gmail.com`
- password: `123456`
- 用途：仅用于本地 Docker 人工 skill 回归。

1. paper-reproduction 已有 project，用户说“继续”，agent 应先 status，再按 ready 状态使用 `project_claude`，不能回到自己读写复现。
2. paper-reproduction execution running，用户说“继续”，agent 应观察 execution 或让 `project_claude` 继续，而不是重新 prepare。
3. literature-review 已有 review workspace，用户说“继续写综述”，agent 应复用 `literature_review_id` 和 review 路径。
4. artifact-parallel-writing 批量写失败后，agent 应基于最新 read/list observation 重建计划，而不是凭记忆声称完成。

## 实施记录

实施时间：2026-05-04

修改文件：

- `backend/app/services/react_agent.py`
- `backend/tests/test_agent_tool_ledger_summary.py`

已实施内容：

- 保留 `_tool_result_ledger_summary_text(...)` 的 async 签名和调用面。
- 移除 `_tool_result_ledger_summary_text(...)` 内部对 `_compress_text_with_qwen_turbo(...)` 的调用。
- 移除后端热路径中的 `source="chat.tool_result_ledger_summary"`。
- 新增确定性 tool result ledger 摘要 helper：
  - `_tool_result_status_label(...)`
  - `_tool_result_anchor_parts(...)`
  - `_tool_result_skill_anchor_parts(...)`
  - `_tool_result_preview_for_ledger(...)`
  - `_join_tool_ledger_summary(...)`
- 摘要格式固定为：
  - 第一行：高优先级 key/value 锚点，尽量控制在 220 字符内。
  - `More:`：第一行放不下的次级锚点。
  - `Detail:`：结构化校验错误、schema error、grounding conflict、allowed_paths 等。
  - `Preview:`：本地截断后的 observation/error 文本。
- `paper_research_* / project_* / project_claude` 保留 `paper_id/project_id/workspace_id/current_stage/execution_id/background_execution/session_id` 等锚点。
- `literature_review_* / review_writer` 保留 `literature_review_id/paper_key/md_path/review_path/final_review_path/report_path/page_count/character_count/review_count` 等锚点。
- `literature_review_read mode=list` 额外保留 `review_paths`，避免已有 review 文件路径只落在 preview 中。
- `document_artifact_*` 保留 `artifact_id/block_id/block_ids/block_count/block_status/workflow_hint` 等锚点。
- P0 未新增 metadata 字段，避免扩大行为面。

边界确认：

- 未改变当前 turn 给模型看的 tool observation。
- 未改变 `result_data` / `metadata` 的原始持久化内容。
- 未改变 skill 激活、workflow binding、decision_state gate。
- 未改变会话级 `context_state / compacted_history` qwen-turbo 压缩。
- 未重新打开 XML fallback。

## 验证结果

Docker 后端自动化验证：

- `docker compose exec -T backend python -m pytest tests/test_agent_tool_ledger_summary.py -q`
  - 结果：4 passed
- `docker compose exec -T backend python -m pytest tests/test_agent_function_calling_fallback.py -q`
  - 结果：25 passed
- `docker compose exec -T backend python -m pytest tests/test_conversation_context_compaction_service.py -q`
  - 结果：9 passed

Docker 真实 API skill 回归：

- 账号：
  - email: `yuiooyww@gmail.com`
  - password: `123456`
- paper-reproduction 只读状态场景：
  - 新会话：`conversation_id=195`
  - 请求：`paper_id=113` / `project_id=10`，限制不启动 execution、不调用 `project_claude`。
  - 结果：agent 调用 `paper_research_status`，回答中确认 `project_id=10`、`paper_id=113`、`reference_ready=True`，并建议下一步交给 `project_claude`。
  - ledger 摘要：`tool=paper_research_status | status=成功 | paper_id=113 | project_id=10 | reference_ready=true ...`
  - “继续” context preview：`preview_mode=agent`，tool ledger 中仍可见上述锚点。
- literature-review 复用 workspace 场景：
  - 新会话：`conversation_id=196`
  - 请求：只读调用 `literature_review_read mode=list`，`literature_review_id=review-20260425053243-85976a3c`，限制不调用 `review_writer`。
  - 结果：agent 列出已有 13 个 review Markdown 文件。
  - ledger 摘要：保留 `literature_review_id=review-20260425053243-85976a3c`、`review_dir=.../review`、`review_paths=review/final.md,...`。
  - “继续写综述” context preview：`preview_mode=agent`，tool ledger 中仍可见 review workspace/path 锚点。
- artifact-parallel-writing 只读场景：
  - 新会话：`conversation_id=197`
  - 新建 artifact：`artifact_id=mod01-regression-artifact`，blocks 为 `intro`、`method`。
  - 请求：只读调用 `document_artifact_read`，限制不调用 update 工具。
  - 结果：agent 列出 `artifact_id=mod01-regression-artifact` 和 `block_id=intro,method`。
  - ledger 摘要：`tool=document_artifact_read | status=成功 | artifact_id=mod01-regression-artifact | block_ids=intro,method ...`
- artifact-parallel-writing 失败路径：
  - 会话：`conversation_id=197`
  - 请求：调用 `document_artifact_update_blocks` 尝试更新不存在的 `block_id=missing_block`。
  - 结果：工具失败，agent 未声称写入成功，回答报告“未找到 block: missing_block”。
  - ledger 摘要：`tool=document_artifact_update_blocks | status=失败 | block_ids=missing_block | error=document_artifact_update_failed ...`
  - “继续” context preview：`preview_mode=agent`，tool ledger 中仍可见 read 成功锚点和 update 失败锚点。

热路径检查：

- `rg "chat\\.tool_result_ledger_summary" backend/app`
  - 结果：无命中。

静态检查：

- `docker run --rm -v "$PWD:/repo" -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - 结果：Broad exception guard passed.
- `docker run --rm -v "$PWD:/repo" -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
  - 结果：Contract alignment guard passed.

备注：

- 直接在 `docker compose exec -T backend python checks/...` 下跑两个静态检查会误报路径缺失，因为后端容器工作目录是 `/app` 后端视图，不是仓库根目录视图；已改用同一后端镜像挂载仓库根目录验证。

## 遗留问题

- paper-reproduction execution running 场景本轮未启动真实 execution，避免在回归中触发训练/后台执行；当前仅覆盖 ready project 的 status/继续上下文路径。
- 会话级 qwen-turbo compaction 不是本 P0 修改范围；后续 `MOD_04` 已将 pre-turn compaction 改为后台投递，`MOD_07` 已收掉 prompt budget 裁剪里的 qwen 调用，manual compact 与 mid-run compaction 仍保持原同步语义。
- `tool_ledger.summary` 第一行会优先保留最关键锚点；超长路径可能进入 `More:`，当前通过本地 path head/tail 截断降低丢失风险。
