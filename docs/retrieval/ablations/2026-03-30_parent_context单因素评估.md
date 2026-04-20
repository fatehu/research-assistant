# parent context 单因素评估

## 背景

当前知识库搜索主链支持 `include_parent_context`，会在命中 paragraph 后补充其父级 chunk 的标题和简短正文预览。

相关实现：

- [knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py)
- [contextual_retrieval_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/contextual_retrieval_service.py)

## 问题

需要确认：

- parent context 是否保持原始排序不变
- parent context 能否稳定补到结果里
- parent context 的额外时延是否可接受

## 评估方式

这项能力不会改变召回排序本身，更适合做 **上下文增强 probe**，而不是直接用公开 BEIR 的 `NDCG / Recall / MRR`。

当前评估入口：

- [run_context_probe.py](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/run_context_probe.py)

评估口径：

- 直接复用 backend `search_knowledge(...)`
- 关闭 `hybrid / rerank / rewrite / compression`
- 只打开 `include_parent_context`
- 对比：
  - baseline 结果顺序
  - parent context 命中率
  - section title 回填率
  - 平均额外时延

## 当前状态

`done`

## 正式结果

评测样本：

- 知识库：`146`
- 查询：`motif design / constrained folding / ArchiveII / NUPACK / SAMFEO`
- `top_k = 5`
- 评测方式：直接复用 backend `search_knowledge(...)`，只打开 `include_parent_context`
- 结果文件：
  - [metrics.json](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/output/kb146/20260330-071359-parent-context-probe-dim0/metrics.json)

关键结果：

- `order_unchanged_ratio = 1.0`
- `enriched_result_ratio = 0.64`
- `avg_parent_context_chars = 254.38`
- `section_backfill_count = 0`

## 结论

- parent context 在当前样本上 **不改变排序**
- `25` 个结果里有 `16` 个成功补到父级上下文，命中率 `64%`
- 单条命中的父级补充文本平均约 `254` 字符
- 这项能力更像 **上下文增强**，不是召回/排序增强

## 注意

- 当前 `baseline_avg_search_ms / enabled_avg_search_ms` 受 **首个 query 冷启动** 明显污染
- 原因是 probe 进程第一次查询会支付 embedding 模型冷启动成本
- 因此这轮结果只把 **排序稳定性** 和 **上下文命中率** 作为正式结论，不把平均时延当成最终结论
