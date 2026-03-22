# 论文阅读 Generative UI Renderer 收敛与页面执行层抽离说明

时间：2026-03-11 22:56

## 背景

在前几轮迭代里，`/experience` 已经具备：

- `display_*` 展示层文案 contract
- `page_brief.storyboard / content_budget`
- section-level `blocks`
- block `user_actions / agent_actions / ui_actions / event_bindings`
- 页面内 `useExperienceActionBus`

但真正消费这些 contract 的主要逻辑仍然堆在：

- `frontend/src/pages/literature/PaperReaderExperiencePage.tsx`

这会造成两个问题：

1. 页面文件同时承担路由、参数、加载状态、layout 执行、block 渲染、交互 dispatch。
2. 后续如果继续做 block registry、runtime event bus、incremental patch，就会继续绑死在页面模板分支里。

## 本轮目标

把 `/experience` 的执行层从页面壳中拆出来，形成真正的 renderer 落点。

目标不是“换个文件名”，而是让后续 Phase 3 能继续推进：

- 页面负责 route / loader / alerts / params / details
- renderer 负责 section layout / block execution / focus rendering / action feedback

## 实现

### 1. 新增独立 renderer

新增：

- `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`

它负责：

- 按 `layout_variant` 划分主区 / 侧栏 / 底部
- 按 section `section_region` 执行 narrative sections
- 按 `blocks` 优先顺序解析 resource / interaction / widget
- 渲染 hero card、focus stage、reading flow、explainer、resource、question 等 section
- 消费 block `ui_actions` 并触发 `dispatchBlockAction`
- 在 hero 区显示最近一次协议事件反馈

### 2. 页面文件收瘦

`PaperReaderExperiencePage.tsx` 现在主要保留：

- URL 参数同步
- shared surface loader 状态
- compose / generative / experience 数据整理
- 顶部 header 和参数面板
- error / warning / empty states
- details 区
- 将 view model 交给 `GenerativeExperienceRenderer`

### 3. action bus 不再被页面模板绑死

renderer 不再自己维护散落的动作状态，而是消费：

- `useExperienceActionBus`

这样后面把页面内 action bus 升级成 shared event bus 时，不需要再从页面壳里拆第二次。

## 为什么这一步符合 Generative UI 主线

这一步不是“重构页面代码风格”，而是为 Generative UI runtime 收敛做基础设施。

它直接服务于以下目标：

- renderer 更接近纯执行层
- 页面结构继续由 plan 驱动，而不是页面里继续堆判断
- block 协议拥有统一执行入口
- 后续 block registry / runtime telemetry / incremental patch 有明确落点

换句话说，这一步是在减少“模板残留”，而不是增加模板残留。

## 当前边界

本轮没有解决这些问题：

- `/workbench` 还没有完全复用 `GenerativeExperienceRenderer`
- block registry 还没有正式抽成独立注册表
- incremental patch / runtime telemetry 还没接
- frontend `build` 还没在当前 9p/WSL 挂载环境下重跑完整闭环

这些仍属于后续 Phase 3 工作。

## 对应计划位置

本轮对应：

- `Phase 3: Build a single GenerativeExperienceRenderer`
- `Phase 3: Continue converging frontend runtime toward a pure execution layer`

本轮完成后，`GenerativeExperienceRenderer` 已经具备单独推进 block registry、shared event bus、incremental patch 的基础。 
