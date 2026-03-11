# 论文阅读 Generative UI Block 协议与 Action Bus 收敛说明

时间：2026-03-11 19:49  
范围：`/literature/:paperId/experience`

## 背景

当前 generative UI 已进入 `display_* -> storyboard/content_budget -> blocks -> ui_actions/event_bindings` 阶段，但 renderer 仍有两类残留：

- 页面把 block 当成静态卡片渲染，协议对象虽已存在，但没有真正驱动页面状态。
- action dispatch、最近一次事件反馈、焦点切换逻辑散落在页面组件内部，不利于继续推进 Phase 3 的统一 renderer。

这轮收敛目标不是做更多模板效果，而是把 block 协议进一步落实为页面内可消费的行为层。

## 设计目标

1. 继续保持 `/read` 稳定，不把 generative experience 强塞回阅读器。
2. `/experience` 继续只消费 plan/runtime contract，不从前端模板反推交互语义。
3. 让 block 从“静态引用”升级为“可触发、可回传、可影响页面状态”的页面对象。
4. 为后续统一 `GenerativeExperienceRenderer` 和共享 event bus 预留收敛点。

## 本轮设计

### 1. 页面内 Action Bus

新增 `frontend/src/pages/literature/useExperienceActionBus.ts`，职责只有三件事：

- 维护当前激活的 `activeTargetId`
- 维护最近一次触发的 `lastUiEvent`
- 根据 block `ui_actions` 统一执行 `dispatchBlockAction`

这层是页面内最小 action bus，不依赖后端新增接口，也不改变 `/read`。

### 2. 协议优先，而不是模板优先

renderer 不再自己猜动作含义，而是：

- 从 `ReaderExperienceBlockRef.ui_actions` 里取动作
- 从 `ReaderExperienceBlockRef.event_bindings` 里取事件语义
- 由 `dispatchBlockAction` 执行最小页面状态变化

当前已接入的 block 类型：

- `RelatedResourceCard`
- `GlossaryPanel`
- `QuestionStarterPanel`
- `figure-focus-accordion`

### 3. 最小可见反馈

`/experience` 顶部增加最近一次协议事件反馈：

- 显示动作 label
- 显示 event name
- 显示 target ref

目的不是做成最终交互 UI，而是确认：

- 这页已经开始消费协议
- block 动作确实在驱动页面

### 4. 焦点切换

当前已接的页面状态：

- `focus_target`：切换当前 `activeTargetId`
- `return_to_reader`：退回 hero 主焦点

这让 `focus_stage` 不再只是固定展示最初 focus，而会响应 block 触发。

## 为什么这一步符合 plan

这一步仍然在主线内：

- 它不是继续堆模板卡片
- 它不是只修某一页文案
- 它不是把交互写死在前端 if/else 里

它服务的是：

- Phase 2：把 contract 做实
- Phase 3：让 renderer 更像执行层
- Phase 5：让交互真正可用

## 当前已知限制

1. 这还是页面内 action bus，不是跨页面共享总线。
2. `ui_actions/event_bindings` 目前只做本地状态消费，还没回传 agent。
3. `workbench` 还没有复用这条 bus。
4. 交互还没有 telemetry 埋点。

## 下一步

1. 把 `useExperienceActionBus` 继续抽象成 renderer 级依赖，而不是页面私有实现。
2. 让 `workbench` 使用相同 event/action 消费层，避免第二套交互逻辑。
3. 引入 block-level telemetry。
4. 逐步把 `QuestionStarterPanel` 接成真实 follow-up 入口，而不是只做本地反馈。
