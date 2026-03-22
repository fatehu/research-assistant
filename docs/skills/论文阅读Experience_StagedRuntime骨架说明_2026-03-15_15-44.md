# 论文阅读 Experience Staged Runtime 骨架说明

时间：2026-03-15 15:44

## 背景

当前 `/experience` 与 `/workbench` 已经具备：

- `page_dossier`
- 邻页结构化 `VL-flash` 上下文
- agent/tool 使用
- shared renderer 与 workbench 可观测性

但 runtime 仍然把太多责任压在一个 agent 主阶段里：

- planning
- tool use
- page generation

这会让 `/experience` 难以继续朝真正的 generative UI 产品推进，也让 `/workbench` 很难把运行时意图解释清楚。

## 本轮目标

先把 staged runtime 从“可观测骨架”推进到真实执行边界：

1. `page dossier`
2. `planning brief`
3. `planner`
4. `tool/enricher`
5. `page generation`
6. `experience materialization`

## 本轮不做

- 不重写 `/experience` renderer
- 不扩大 `/read` 责任
- 不让模型生成任意前端代码
- 不一次性拆成多个昂贵的 LLM 阶段

## 实现策略

### 1. 增加 deterministic `planning_brief`

在 runtime 内基于：

- 当前页 targets/assets
- 邻页 continuity
- page dossier
- 当前 user intent

先生成一个小的 `planning_brief`，用于约束后续 agent 主阶段。

它应该回答：

- 这一页是什么 archetype
- 当前页最值得讲的主线是什么
- 邻页 continuity 如何影响页面
- 这一页最值得调用哪些工具
- 这一页最值得保留哪些 section/module 方向
- 这一页 tool/enricher 的预算护栏是什么

### 2. 把 runtime 切成真实三阶段

不再把 planning、tool use、page generation 混在一个 agent 主阶段里，而是明确分成：

- `planner`
- `tool_enricher`
- `page_generation`

并且保留：

- legacy agent-core fallback
- stage-specific timeout / exception fallback
- planner output normalization
- tool enrichment packet compaction
- deterministic tool budget enforcement

### 3. prompt 改成 staged 输入

- planner stage：吃 `page_dossier + planning_brief + adjacent_page_context`
- page-generation stage：吃 `planner_output + tool_enrichment_packet + compose/page dossier`
- planner stage 必须显式遵守 `tool_budget`
- tool/enricher 执行层必须真正执行：
  - `max_tool_requests`
  - `max_reader_native_requests`
  - `max_public_web_requests`
  - `duplicate_query_policy`
  - `per_tool_timeout_seconds`

这样 `/experience` 的 rich webpage 生成不再依赖一个 opaque agent 阶段，而是由 planner 和 tool/enricher 的中间结果驱动。

### 4. `/experience` 与 `/workbench` 暴露 staged runtime inspect 面

除了已有的：

- `Page Dossier`
- `Planning Brief`
- `Adjacent Page Context`
- `Runtime Stages`
- `Tool Trace`

这轮再新增：

- `Planner Output`
- `Tool Enrichment Packet`
- `Tool Budget`
- `Budget Summary / Suppression`

这样前端就能直接检查：

- planner 选了什么页面策略
- planner 要求了哪些 tool requests
- tool/enricher 实际补回了什么
- page generation 是否真的基于这些输入在构页

## 对 plan 的影响

本轮会把 rollout plan 的目标架构从：

`compose payload -> generative plan -> experience runtime`

收敛成：

`compose payload -> page dossier -> planning brief -> planner -> tool/enricher -> page generation -> experience runtime`

这是 staged runtime 的第一步，不是最终形态。

## 追加约束：正文主干优先

后续验证中确认，`/experience` 之前偏向“compact artifact page”，会让 planner 和 content budget 共同裁掉大量当前页正文。这与产品目标冲突。

因此补充一条硬约束：

- 当前页 `body_flow_target_ids` 必须成为 `/experience` 的主阅读骨架
- planner / page_generation 只能决定如何围绕主干增强理解
- `content_budget` 只作用于：
  - `supporting_resources`
  - `explainer_cluster`
  - `question_lab`
  - `widget_blocks`
- 不得再把正文主干压缩成少量 target 的“阅读支撑正文”

这意味着 `/experience` 的 rich webpage 必须优先满足：

1. 内容完整
2. 帮助理解
3. 阅读友好
4. 再考虑紧凑和卡片化

## 验收标准

1. runtime 真正执行：
   - `planner`
   - `tool/enricher`
   - `page_generation`
2. `generative_plan.meta` 里能看到：
   - `planning_brief`
   - `tool_budget`
   - `planner_output`
   - `tool_enrichment_packet`
   - `runtime_stage_trace`
3. `experience_plan.meta` 里也能看到：
   - `planning_brief`
   - `tool_budget`
   - `planner_output`
   - `tool_enrichment_packet`
   - `runtime_stage_trace`
4. `/workbench` 与 `/experience` 能直观看到：
   - planning brief
   - tool budget
   - planner output
   - tool enrichment packet
   - runtime stages
   - tool trace
5. 保留 legacy fallback，不影响现有 `/experience` 页面渲染
