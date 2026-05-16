# `/literature/dabian` 相关文档审阅

更新时间：2026-05-08

## 结论

当前仓库没有发现 `/literature/dabian` 路由、页面或专门文档。

已核对范围：

- `frontend/src/App.tsx`
- `frontend/src/pages/literature`
- `backend/app/api`
- `backend/app/services`
- `docs`

当前存在的文献相关前端路由是：

- `/literature`
- `/literature/:paperId/read`
- `/literature/:paperId/read/review`
- `/literature/:paperId/read/workbench`
- `/literature/:paperId/experience`
- `/literature/:paperId/workbench-v2`
- `/literature/:paperId/experience-v2`
- `/literature-reviews`

因此，如果 `dabian` 指“答辩/演示页”，它目前不是一个已经落地的产品面。不能把它当成已有模块写运行文档。

## 现有文档是否合适

### `docs/LITERATURE_MODULE.md`

适合作为早期文献管理模块概览，但不适合作为当前 `/read`、Ask、composed reader 或答辩演示的设计文档。

主要原因：

- 它描述的是搜索、收藏、PDF 下载、引用图谱等基础功能。
- 没覆盖现在的 `/literature/:paperId/read` 证据链、AI 阅读、Ask session、agentic/classic 问答。
- 文件结构也落后于当前代码，当前 reader 相关页面和服务已经明显更多。

### `docs/LITERATURE_TEST_GUIDE.md`

适合作为回归测试清单，尤其是其中 `/read` AI 阅读与证据链回归部分仍有价值。

但它不适合作为产品设计或架构说明：

- 文档前半部分仍是基础文献模块测试。
- `/read` 回归被插在 UI 测试流程中，结构上更像测试补丁。
- 没有说明 Ask session、知识库 readiness、agentic PDF-only fallback、来源跳转和分数展示这些近期讨论过的行为。

## 建议

短期不要补一个名为 `/literature/dabian` 的正式设计文档，除非路由和产品边界已经确定。否则文档会先于实现，后续容易误导维护。

当前更合适的拆分是：

- `/chat` 上下文、replay、压缩边界：见 `docs/chat/CHAT_CONTEXT_ITEM_STREAM_REPLAY_ZH.md`。
- `/literature/:paperId/read` 阅读器回归：继续维护 `docs/LITERATURE_TEST_GUIDE.md` 的 `/read` 回归段落。
- 文献 Ask 的架构说明：如后续要整理，应单独新增 `LITERATURE_ASK_AGENT_ZH.md`，不要混到 `dabian` 名下。

## 当前缺口

如果后续要把文献阅读做成答辩/演示材料，需要先确认这些边界：

- `dabian` 是独立页面，还是现有 `/read` 的预设模式。
- 演示对象是单篇论文、论文集，还是复现项目。
- 是否需要复用 `/chat` agent 上下文机制，还是继续使用 `LiteratureQASession`。
- 是否需要固定示例论文、示例问题、示例来源跳转和离线可复现数据。

在这些边界确认前，现有文档只能说明“当前没有 `/literature/dabian` 模块”，不应写成已实现能力。
