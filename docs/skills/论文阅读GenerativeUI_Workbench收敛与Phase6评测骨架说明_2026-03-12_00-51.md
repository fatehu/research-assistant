# 论文阅读 Generative UI：Workbench 收敛与 Phase 6 评测骨架说明

## 背景

本轮工作的目标不是继续打磨 `/read/workbench` 的独立展示，而是把它进一步收回到同一条 generative UI 主链路里：

- `/experience` 继续作为目标产品页
- `/workbench` 只做 debug / inspection surface
- renderer、block registry、action bus、surface loader 只维护一套

同时，为了避免系统继续依赖“观感很好/感觉不好”的主观判断，本轮补入了 Phase 6 的第一版评测骨架。

## 本轮设计决策

### 1. Workbench 改为复用 shared renderer/runtime

`PaperReaderWorkbenchPage.tsx` 不再维护另一套 section/block 渲染实现，而是：

- 复用 `useReaderSurfaceLoader`
- 复用 `useExperienceActionBus`
- 复用 `GenerativeExperienceRenderer`
- 复用 `experienceBlockRegistry`

Workbench 现在的职责变成：

- 展示同一份 experience preview
- 在 preview 下方挂 story map / enhancement outline / targets / plan meta 等 debug 信息

这样做的目的：

- 避免 `/experience` 与 `/workbench` 长期分叉
- 避免修 `/experience` 时，debug 页继续走旧模板逻辑
- 让调试页观察到的对象与产品页实际执行的对象一致

### 2. Block contract 增加运行态状态

`ReaderExperienceBlockRef.state` 从简单的 `ready/empty` 扩展为：

- `ready`
- `loading`
- `partial`
- `empty`
- `error`

设计意图：

- 让 renderer 可以做 block-level 降级，而不是 section-level 整块失败
- 为后续 incremental patch、telemetry、interaction follow-up 做最小运行态基础

这仍然是 generative UI 方向，因为系统开始表达“页面对象在运行中处于什么状态”，而不是只把 block 当静态内容容器。

### 3. 用 Phase 6 评测骨架替代“只靠肉眼看”

本轮新增了三类评测资产：

- golden set fixture
- generative snapshot fixture
- experience snapshot fixture

目的不是宣布“评测体系已成熟”，而是先把这些对象固定下来：

- 什么样的 page archetype / storyboard / contract 算稳定
- 什么样的 experience section order / region / block protocol 算稳定
- 至少覆盖：
  - figure-heavy
  - methods-heavy
  - concept-heavy

目前仍然是混合种子集：

- 1 个真实 paper page
- 2 个 contract fixture

这足够支撑 Phase 6 起步，但还不等于完整真实样本池。

## 为什么这轮不是“模板 + AI 文案”

如果只是模板化收敛，最简单的做法是：

- 保留 `/workbench` 的独立页面壳
- 在前端继续手写 block family 分支
- 遇到坏块时直接隐藏整段

本轮没有走这条路，而是：

- 让 `/workbench` 复用相同的 plan/runtime/rendering 路径
- 让 block 状态进入 contract
- 让 renderer 执行 contract，而不是反过来“猜系统状态”
- 用 snapshot / golden set 固定 generative object 的形状

这才是继续向 generative UI runtime 推进，而不是把当前模板系统继续包一层。

## 当前仍然缺什么

### Phase 3

- incremental block patching
- runtime telemetry

### Phase 5

- 真实 follow-up interaction
- 统一 event bus 继续上收

### Phase 6

- 全真实页面 golden set
- interaction usefulness / latency / fallback / cache hit 等指标统计
- review 回灌机制

## 对下一轮的约束

- 不再把 `/workbench` 当第二产品页继续堆展示逻辑
- 不再通过前端模板分支去掩盖 block/state contract 的缺失
- 优先推进：
  - incremental patch
  - telemetry
  - evaluation coverage
