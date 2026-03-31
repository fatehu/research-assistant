# contextual compression 评估范围

- 日期：`2026-03-30`
- 阶段：`retrieval ablation`
- 状态：`done`

## 问题

当前知识库搜索主链支持 contextual compression。

实现位置：

- [contextual_compression_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/contextual_compression_service.py)

需要判断：

- compression 是否改善最终返回质量
- compression 是否损伤召回后的证据完整性
- compression 的成本是否值得默认开启

## 当前观察

compression 发生在：

- 候选召回之后
- rerank 之后或与其配合
- 最终结果返回之前

所以它不属于第一层检索召回本身。

## 当前观察

已补 backend service 直连 probe runner：

- [run_compression_probe.py](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/run_compression_probe.py)

当前极小样本 probe（`SciFact / 3 queries / top-3 candidates`）结果：

- `used_compression_ratio = 1.0`
- `compression_ratio = 0.2765`
- `avg_relevance_score = 0.25`
- `fallback_reasons`
  - `batch_low_relevance_extractive = 3`
  - `batch_missing_item_extractive = 1`

另外，直接 backend service probe 结果显示：

- `ContextualCompressionService.compress_chunks()` 最小调用成功
- `5` 并发 compression probe 也全部成功
- 当前没有复现 `Connection error`

但需要注意：

- 当前 `contextual_compression_mode=batch`
- `contextual_compression_min_relevance = 4.0`
- 本轮 probe 返回的 `relevance_score = 1.0`

这意味着当前大量 `extractive fallback` 不是因为 LLM 没调通，而是：

- LLM 已返回结果
- 但返回分数低于本地阈值
- 因此被系统按设计降级成 extractive fallback

这说明：

- `ContextualCompressionService` 已真实调用
- 当前主要问题不是连接失败，而是阈值过严或 batch 输出质量不够高
- 现在能观察到运行特征，但还不能把它当成最终质量结论

## 新增验证

后续发现，旧版 probe 脚本存在“循环内反复 `asyncio.run()`”问题，会让全局 `LLMService` 复用已关闭事件循环，表现成：

- `APIConnectionError('Connection error.')`
- `cause=RuntimeError('Event loop is closed')`

这一点已经在 eval runner 侧修正。修正后重新对照：

### `single + min_relevance=4.0`

- `used_compression_ratio = 1.0`
- `fallback_reasons = {"low_relevance_extractive": 4}`
- 无 `Connection error`

说明：

- single 模式本身是稳定可调用的
- 当前主要是阈值过严，所有结果都被判成 low relevance fallback

### `single + min_relevance=1.0`

- `used_compression_ratio = 1.0`
- `fallback_reasons = {"low_relevance_extractive": 3}`
- 无 `Connection error`

说明：

- 降低阈值后 fallback 数量有改善
- 但 single 模式下仍然存在低相关度判退

### `batch + min_relevance=4.0`

- `used_compression_ratio = 0.75`
- `fallback_reasons`
  - `batch_compression_error_extractive = 1`
  - `batch_low_relevance_extractive = 2`
  - `batch_missing_item_extractive = 1`
- 仍会出现 `empty batch compression payload`

说明：

- 修掉事件循环问题后，batch 模式的主要问题更明确地收敛为：
  - batch 输出经常不是可解析的稳定 JSON
  - 或 item 不全
  - 并且 relevance 判定偏严

结论更新为：

- 旧 probe 里的部分“连接失败”是 eval 脚本问题
- 真正剩下的 compression 问题主要在：
  - batch 输出稳定性
  - `min_relevance` 阈值

## 当前决定

- compression 单因素系统 probe 已完成
- 这一项不纳入当前公开 `BEIR` 检索指标主对照
- 当前结论是：
  - `single` 稳定性明显好于 `batch`
  - `min_relevance=4.0` 偏严
  - 当前不适合默认开启

原因：

- `NDCG / Recall / MRR` 更适合评召回和排序
- compression 更接近最终响应质量和上下文可用性问题

## 等待后续消融回答的问题

1. compression 对最终答案质量的真实提升
2. compression 是否会裁掉关键证据
3. 当前 `min_relevance=4.0` 是否过严
4. batch 模式是否比 single 模式更容易触发 fallback
5. compression 的延迟和失败回退情况

## 暂不执行的动作

- 不立即修改 `enable_contextual_compression` 默认值
- 不立即调整 compression 阈值参数
- 不立即把当前 fallback 误判成 “LLM 连接失败”

## 后续落点

- 进入组合或系统级评测时，再补：
  - single/batch 更大样本对照
  - `min_relevance` 阈值对照
  - compression 对最终答案质量的正式系统级结论
