# query rewrite 评估范围

- 日期：`2026-03-30`
- 阶段：`retrieval ablation`
- 状态：`done`

## 问题

当前知识库搜索主链支持 Query Rewrite。

实现位置：

- [query_rewrite_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/query_rewrite_service.py)

需要判断：

- rewrite 是否真的提高检索质量
- rewrite 在当前线上配置下是否稳定
- rewrite 的收益是否值得它引入的 LLM 成本和额外延迟

## 当前观察

当前 rewrite 依赖：

- LLM 可用性
- rewrite 策略组合
- 缓存命中
- 跳过逻辑

这意味着它不是纯 retrieval 算法层变量，而是带明显运行时环境依赖。

## 当前观察

已补 backend service 直连 runner：

- [run_rewrite.py](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/run_rewrite.py)

当前宿主机 probe 结果：

- `LLMService(provider=deepseek)` 配置正常
- 直接 `LLMService.chat()` 最小调用成功
- 直接 `QueryRewriteService.rewrite_query()` 最小调用成功
- `5` 并发 rewrite probe 全部成功

本轮直接观测到：

- `deepseek` API key 存在，`base_url=https://api.deepseek.com`
- 域名连通性正常，对根路径访问返回 `401`，说明网络可达
- rewrite 单次调用正常返回 `synonym` 扩展
- rewrite `5` 并发下也未复现 `Connection error`
- 之前 eval 里出现的 `Connection error` 没有在最小 probe 中复现

这说明：

- rewrite 不是“没接上”
- 当前没有发现静态配置错误
- benchmark 期间出现的 `Connection error` 更像是临时外部连接不稳定，而不是本地实现或配置错误

## 新增修正

后续排查发现，部分 eval 脚本此前存在“循环内反复 `asyncio.run()`”的问题，会让带 `AsyncOpenAI` client 的全局 service 复用已关闭事件循环。

这一项已经在 eval runner 侧修正，因此：

- 当前 rewrite 相关判断应以 backend service probe 为准
- 不再把旧 probe 中的事件循环副作用当成 rewrite 服务本身的问题

## 当前结果

已完成一轮同口径 smoke 对照，数据集为：

- `SciFact / 5 queries / 100 corpus`
- embedding: `BAAI/bge-m3 / 1024 dim`
- rewrite: `force + synonym`

对照结果：

- dense-only
  - `NDCG@10 = 0.9`
  - `MRR@10 = 0.86667`
  - 结果文件：
    - [metrics.json](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/output/scifact/20260330-144808-baai-bge-m3-dim1024/metrics.json)
- dense + query rewrite
  - `NDCG@10 = 1.0`
  - `MRR@10 = 1.0`
  - `avg_vector_variants = 4.0`
  - `llm_called_ratio = 1.0`
  - 结果文件：
    - [metrics.json](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/retrieval_beir/output/scifact/20260330-144043-baai-bge-m3-rewrite-dim1024/metrics.json)

当前信号：

- 在这组小样本上，`synonym rewrite` 明显改善了顶部排序
- 但运行时间极高，单次 smoke 总耗时超过 `300s`
- 因此现阶段只能判定“有收益信号”，不能直接作为默认策略结论

## 当前决定

- rewrite 单因素消融已经完成
- backend service 已接通
- 当前环境下 rewrite 服务本身可正常工作
- 小样本 smoke 上有正向收益信号
- 但延迟成本过高，暂不建议直接默认开启

## 等待后续消融回答的问题

1. 在稳定 LLM 环境下，rewrite 对召回和排序是否有稳定提升
2. `synonym / hyde / decompose` 各自的边际收益
3. rewrite 的延迟、失败率和缓存命中率是否值得默认开启

## 暂不执行的动作

- 不立即修改 `enable_query_rewrite` 默认值
- 不立即对 `query_rewrite_strategies` 下结论

## 后续落点

- 进入组合消融时，再补：
  - 更大样本下的 rewrite 收益稳定性
  - `hyde / decompose` 与 `synonym` 的边际收益对比
  - 失败率/超时率统计
