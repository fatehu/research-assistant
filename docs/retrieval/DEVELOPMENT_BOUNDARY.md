# Retrieval Development Boundary

## 1. 当前状态

当前 `docs/retrieval/` 对应的是这条已经落地的知识库搜索主链：

- 查询改写
- 向量检索
- 可选混合全文检索
- RRF 融合
- 可选 rerank
- 可选上下文压缩
- 父级/相邻上下文补全

这里不再记录旧方案脑图，只记录当前真实代码行为和后续优化边界。

## 2. 当前生产主链

当前知识库搜索主链在：

- `backend/app/api/knowledge.py`
  - `POST /api/v1/knowledge/search`

真实执行顺序是：

1. 权限解析与知识库范围确定
2. Query Rewrite
3. 按 `(embedding_model, embedding_dimension)` 分组做向量检索
4. 可选 FTS 混合检索
5. RRF 融合
6. 可选 reranker 精排
7. 可选 contextual compression
8. 父级上下文补全
9. 相邻 chunk 上下文补全

## 3. 当前代码落点

### 3.1 搜索主入口

- `backend/app/api/knowledge.py`

### 3.2 当前子能力

- Query Rewrite
  - `backend/app/services/query_rewrite_service.py`
- Hybrid Retrieval / RRF
  - `backend/app/services/hybrid_retrieval_service.py`
- Reranker
  - `backend/app/services/reranker_service.py`
- Contextual Compression
  - `backend/app/services/contextual_compression_service.py`
- HNSW / vector search tuning
  - `backend/app/services/vector_search_tuning.py`
- 邻接上下文和 embedding 输入组织
  - `backend/app/services/contextual_retrieval_service.py`

## 4. 当前检索链的真实特征

### 4.1 向量检索

当前不是单一固定向量检索，而是：

- 先按 `embedding_model + embedding_dimension` 分组
- 每组分别生成查询向量
- 每组独立执行 pgvector 检索
- 最后再把多组结果合并

这意味着后续检索优化必须考虑：

- 多维向量共存
- 模型维度分组
- HNSW `ef_search` 动态调整

### 4.2 混合检索

当前支持：

- vector-only
- vector + FTS hybrid

混合检索不是独立系统，而是当前搜索接口里的可选阶段。

### 4.3 排序链

当前排序不是单层：

- 先向量分数 / 文本分数
- 再 RRF 融合
- 再可选 reranker

所以后续检索优化要明确是在优化：

- recall
- candidate fusion
- final ranking

不要把这些问题混成一个指标。

### 4.4 结果增强

当前搜索结果还可能继续叠加：

- contextual compression
- parent context
- adjacent chunk context

这些会影响最终返回质量和延迟，但不属于第一层候选召回本身。

## 5. 当前范围内的优化方向

后续 `docs/retrieval/` 允许记录并推进的方向：

- 向量检索召回质量
- 混合检索融合质量
- rerank 收益与成本
- query rewrite 收益与成本
- contextual compression 收益与成本
- 搜索延迟和分阶段耗时
- 维度策略与检索质量/成本平衡

## 6. 当前不在本目录主范围的内容

以下内容当前不属于 `docs/retrieval/` 主范围：

- PDF 提取质量
- `json2md / ingest-md` 表达策略
- 分块策略本身
- 旧 block-based / protected-md 方案讨论

这些内容分别归：

- `docs/pdf2md/`
- `docs/chunking/`

## 7. 当前优化原则

后续检索优化必须遵守：

- 先按当前真实主链分析，不脑补
- 区分召回、融合、精排、压缩四层问题
- 不把完整搜索链上的所有开关同时改动
- 每次改动都说明影响的是哪一层

## 8. 当前阶段的收口点

当前可以认为：

- 分块入库阶段已收敛
- 检索优化阶段从当前搜索主链正式开始

后续如果继续写 `docs/retrieval/`，只记录：

- 当前真实检索链的修订
- 已落地优化
- 新的性能/质量诊断结论

不要再把未实现的候选路线写成现状。
