# 论文阅读 Generative UI Block Registry 收敛说明

时间：2026-03-11 23:09

## 背景

上一轮已经完成：

- `GenerativeExperienceRenderer` 抽离
- `useExperienceActionBus`
- renderer 对 `blocks / ui_actions / event_bindings` 的消费

但 renderer 里仍然保留了大量硬编码判断：

- `module.module_type === ...`
- `widget.widget_type === ...`
- 各类 eyebrow / action / content rendering 仍靠文件内分支

这会导致：

1. renderer 继续像“大号页面模板文件”
2. block 新增类型时，执行层还得继续改一堆 `if / switch`
3. 后续 Phase 3 的 registry、telemetry、incremental patch 没有稳定落点

## 本轮目标

新增前端 renderer-side block registry，把已落地的 block family 收成注册表。

目标不是一次性做完整平台，而是先完成第一版：

- resource registry
- interaction registry
- widget registry

## 实现

新增：

- `frontend/src/pages/literature/experienceBlockRegistry.tsx`

它现在负责：

- resource module definition
- interaction module definition
- widget definition
- renderer-side eyebrow / render function / role metadata
- `QuestionStarterPanel` 这类 block 的 role 判断

### 当前已注册的 block

resource:

- `FigureExplainPanel`
- `RelatedResourceCard`
- `default`

interaction:

- `GlossaryPanel`
- `QuestionStarterPanel`
- `default`

widget:

- `figure-focus-accordion`
- `default`

## renderer 变化

`GenerativeExperienceRenderer.tsx` 现在不再直接承担 block family 的细节渲染实现，而是：

- 负责 layout / section / block ref 解析
- 通过 registry 取 definition
- 调 definition.render(...)

这让 renderer 更接近执行层，而不是 block 内容模板集合。

## 为什么这一步符合 plan

这一步直接对齐 `docs/plan/generative-ui-rollout-plan.md` 的 Phase 3：

- renderer 继续收敛为执行层
- block family 有了明确注册点
- 后续可继续扩展：
  - block-level telemetry
  - block-level fallback state
  - registry-based capability exposure

它仍然不是“自由生成前端”，而是继续强化“受控 block contract + renderer execution”。

## 当前边界

本轮 registry 仍有边界：

- 这是 renderer-side registry，不是后端 capability registry
- 还没有把 `/workbench` 完全复用到同一 registry/renderer
- 还没有接 block-level telemetry
- 还没有接 incremental patch
- 还没有从 registry 反向约束 agent 只能选前端已注册 block

因此，`Phase 3: Create a block registry` 方向已经进入实现，但还不能宣称整项闭环完成。
