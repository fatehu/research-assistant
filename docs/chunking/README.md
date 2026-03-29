# Chunking

`docs/chunking/` 现在只记录 **当前已经落地的分块入库方案**。

这一阶段的结论已经明确：

- 分块入库主线按 `PDF structured JSON -> ingest-md -> SmartChunkingService -> chunks` 收敛
- 现有 preview / eval 用 `json2md` 不动
- 入库专用 Markdown 走独立 renderer
- 本地 PDF 旧 block-based chunk 残留已退出知识库主入库链
- `SmartChunkingService` 保留 5 个产品模式
- 模式内部优先使用成熟开源 splitter，失败时自动回退 legacy 实现

当前文档：

- [DEVELOPMENT_BOUNDARY.md](./DEVELOPMENT_BOUNDARY.md)
  - 当前真实状态
  - 已完成范围
  - 当前实现边界
  - 后续如果继续做，只从这里续写
- [../retrieval/README.md](../retrieval/README.md)
  - 分块入库完成后的下一阶段检索优化入口

当前不在这里维护：

- 旧 benchmark 选型讨论
- 已过时的 block-based / protected-md 方案讨论
- 未落地的架构分叉草案

一句话：

- `docs/chunking/` 现在描述的是 **已经做完的分块入库实现**，不是早期方案讨论区。
