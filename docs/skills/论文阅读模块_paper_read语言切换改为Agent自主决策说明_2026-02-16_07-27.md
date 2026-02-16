# 论文阅读模块_paper_read 语言切换策略改为 Agent 自主决策说明

- 时间：2026-02-16 07:27
- 背景：原方案通过扫描论文文本统计主语言后再优先关键词。现改为更轻量模式：由 Agent 基于命中质量自主决定是否切换语言重试。

## 本次改动
1. 移除论文主语言检测路径
- 删除 `paper_read` 中基于前几页文本检测主语言的逻辑（不再做该统计）。
- `paper_read` 不再返回 `paper_primary_language` 字段。

2. 改为命中质量驱动重试
- `paper_read` 输出新增检索诊断行：`quality/top_score/query_lang`。
- 当命中偏弱（`top_score < 0.08`）时，输出建议：
  - 中文 query：提示可改成英文关键词重试。
  - 英文 query：提示可改中文重试。
- `data` 增加：`query_language`、`quality`、`top_score`、`suggest_retry`。

3. Agent 提示词和 observation 跟进
- 系统提示改为：`paper_read` 首次命中弱时，可做中英互换再重试一次。
- `paper_read` observation 的 followup 文案加入明确指令：`quality=low` 时允许语言切换重试。

## 结果
- 工具侧计算更轻量。
- 语言切换策略由 Agent 在运行时根据命中质量动态决策，而非预先固定。
