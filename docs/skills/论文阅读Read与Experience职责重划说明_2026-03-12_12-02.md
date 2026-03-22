# 论文阅读 `/read` 与 `/experience` 职责重划说明

时间：2026-03-12 12:02

## 背景

此前系统一度尝试把 generative UI 直接落在 `/read` 内部，但实践结果已经证明这条路径会混淆两类目标：

- 稳定阅读器目标
- 页面级 generative UI 目标

这会导致 `/read` 的遗留 compose/highlight 问题不断干扰 `/experience` 的主线推进。

## 新边界

### `/read`

`/read` 的目标收敛为：

- 简化后的 AI 排版阅读
- HTML 式流式阅读
- 清洗后的正文展示
- 原文证据核验
- PDF 阅读、批注、评论

`/read` 不再承担：

- 页面级 generative UI 产品设计
- 叙事型页面组织
- block/page 级体验编排主线

当前嵌入在 `/read` 中的 AI 编排视图，不再继续走复杂页面设计路线，但仍保留为阅读器内部的简化 AI 排版能力。

### `/experience`

`/experience` 是 generative UI 的唯一产品主面。

它承担：

- 页面级结构生成
- 阅读路径设计
- block 组合与交互
- 资源补充与解释层
- 页面体验级中文展示

### `/read/workbench`

`/read/workbench` 仅保留为：

- 调试
- 检查
- plan/runtime 对照

不作为产品终态继续打磨。

## 对实现策略的影响

1. `/read` 的遗留 compose/highlight 问题单独归类为稳定性债务。
2. `/read` 的修复优先级只围绕：
   - 不阻塞阅读
   - 保留 AI 清洗正文与简化排版
   - 保证证据高光准确性优先于花哨布局
   - fallback 安全
3. `/experience` 继续承接：
   - contract
   - renderer
   - block/event/action
   - evaluation
4. 不再把 `/read` 上某个“看起来不错的样本页”视为 generative-ui 成功标准。

## 立即执行约束

- 讨论 generative-ui 进展时，默认以 `/experience` 为主对象。
- 讨论 `/read` 时，默认以“清洗后的 HTML 流式阅读 + 证据核验”来判断对错。
- `/read` 的历史 AI 编排问题，不再用来否定 `/experience` 主线架构。
