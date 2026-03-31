# adjacent context 单因素评估

## 背景

当前知识库搜索主链支持 `include_adjacent_chunks`，会在命中 chunk 周围补充相邻 chunk 内容。

相关实现：

- [knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py)
- [contextual_retrieval_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/contextual_retrieval_service.py)

## 问题

需要确认：

- adjacent context 是否保持原始排序不变
- 相邻窗口补全是否稳定
- 不同窗口大小的额外时延是否可接受

## 评估方式

这项能力不会改变召回排序本身，更适合做 **上下文增强 probe**，而不是直接用公开 BEIR 的 `NDCG / Recall / MRR`。

当前评估入口：

- [run_context_probe.py](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/run_context_probe.py)

评估口径：

- 直接复用 backend `search_knowledge(...)`
- 关闭 `hybrid / rerank / rewrite / compression`
- 只打开 `include_adjacent_chunks`
- 对比：
  - baseline 结果顺序
  - adjacent context 命中率
  - 平均补充 chunk 数
  - 平均额外时延

## 当前状态

`done`

## 正式结果

评测样本：

- 知识库：`146`
- 查询：`motif design / constrained folding / ArchiveII / NUPACK / SAMFEO`
- `top_k = 5`
- `adjacent_window = 1`
- 评测方式：直接复用 backend `search_knowledge(...)`，只打开 `include_adjacent_chunks`
- 结果文件：
  - [metrics.json](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/output/kb146/20260330-071359-adjacent-context-probe-dim0/metrics.json)

关键结果：

- `order_unchanged_ratio = 1.0`
- `enriched_result_ratio = 1.0`
- `avg_adjacent_items = 2.0`
- `avg_adjacent_chars = 503.0`

## 结论

- adjacent context 在当前样本上 **不改变排序**
- `25` 个结果全部都补到了相邻上下文，命中率 `100%`
- 每个结果平均补 `2` 个相邻块，平均新增约 `503` 字符
- 这项能力当前可以视为 **稳定的上下文增强项**

## 注意

- 当前 `baseline_avg_search_ms / enabled_avg_search_ms` 同样受 **首个 query 冷启动** 污染
- 因为 probe 是单独进程，第一次查询会支付 embedding 模型冷启动成本
- 因此这轮结果只正式采用：
  - 排序是否变化
  - 相邻上下文命中率
  - 平均补充 payload
