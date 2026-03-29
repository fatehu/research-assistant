# Retrieval

`docs/retrieval/` 现在只记录 **当前已经落地的检索主链** 和后续检索优化的真实边界。

这一阶段的起点已经明确：

- 分块入库主线已经收敛到 `PDF structured JSON -> ingest-md -> SmartChunkingService -> chunks`
- 后续检索优化从当前知识库搜索主链继续，不再回到旧的分块方案讨论
- 检索优化关注的是当前线上实际链路，不是历史候选方案

当前文档：

- [DEVELOPMENT_BOUNDARY.md](./DEVELOPMENT_BOUNDARY.md)
  - 当前真实检索链
  - 当前已落地能力
  - 后续检索优化边界
- [../../backend/eval/retrieval_beir/README.md](../../backend/eval/retrieval_beir/README.md)
  - 注意：BEIR 评测依赖放在独立 eval 环境，不进入主 `backend/requirements.txt`
  - 第一版公开 BEIR `dense-only` 评测入口

当前不在这里维护：

- 旧的分块架构争论
- 未落地 benchmark 脑图
- 与当前知识库搜索主链无关的泛化 RAG 方案

一句话：

- `docs/retrieval/` 现在描述的是 **当前真实检索系统**，后续检索优化只从这里续写。
