# Chunking Development Boundary

## 1. 当前状态

当前 `docs/chunking/` 对应的这轮工作已经完成，范围是：

- PDF 分块入库主线
- 入库专用 Markdown 输出
- `SmartChunkingService` 的模式保留与内核替换
- 分块 metadata 统一

这里不再记录早期方案讨论，只记录当前真实实现。

## 2. 当前生产主链

当前分块入库主链是：

- `PDF structured JSON -> ingest-md -> SmartChunkingService -> chunks`

其中：

- `structured JSON`
  - 指 `PdfStructuredDocument` / `PdfSemanticBlock`
- `ingest-md`
  - 指入库专用自然 Markdown
- `chunks`
  - 指知识库最终入库块

## 3. 当前代码落点

### 3.1 入库 Markdown

现有 preview / eval renderer 不动：

- `backend/app/services/local_structured_pdf/markdown_renderer.py`

当前入库专用 renderer：

- `backend/app/services/local_structured_pdf/ingest_markdown_renderer.py`

当前接线路径：

- `backend/app/services/pdf_rag_ingest_service.py`

真实行为：

- `PdfRagIngestService` 只负责本地 PDF 结构化提取和 `ingest-md` 输出
- 本地 PDF 最终分块统一走 `SmartChunkingService`
- 旧 block-based chunk 构建已经退出知识库主入库链

## 4. ingest-md 边界

`ingest-md` 的目标是：

- 保持自然 Markdown
- 服务入库和分块
- 不污染正文

当前已经稳定表达：

- `heading`
- `paragraph`
- `list_item`
- `table`
- `equation`
- `caption`
- `figure_meta`
- `footnote`

当前明确不做：

- 不把 `block_id / bbox / line_ids / page` 这类技术元数据写进正文
- 不为了 chunking 改坏现有 preview / eval Markdown

## 5. SmartChunkingService 当前边界

保留 5 个产品模式：

1. `fixed`
2. `semantic`
3. `hierarchical`
4. `academic`
5. `hybrid`

这 5 个模式没有被删除，也不是临时兼容层。

## 6. 模式与引擎的当前实现

当前实现不是重写一套新 chunking，而是：

- 保留模式层
- 替换模式内部引擎
- 保留统一输出契约

当前接入关系：

- `fixed`
  - 优先走 LangChain splitter
  - 失败时回退 legacy fixed
- `semantic`
  - 优先走 LlamaIndex semantic splitter
  - 失败时回退 legacy semantic
- `hierarchical`
  - 优先走 LlamaIndex hierarchical parser
  - 失败时回退 legacy hierarchical
- `academic`
  - 保持系统自有编排
  - 当前由 Markdown header split + 章节内 semantic split 组合
  - 失败时回退 legacy academic
- `hybrid`
  - 保持系统自有路由
  - 当前负责 academic / semantic 的动态选择

## 7. Metadata 当前边界

当前 chunk metadata 采用两层：

- 核心字段
  - 兼容现有知识库和 API 契约
- `extra`
  - 存放 splitter / header_path / content_flags / start_index / end_index 等扩展信息

当前不把这些当生产主要求：

- `bbox`
- `line_ids`
- `block_id`
- 复杂 source map 持久化

如果以后需要，只能作为 sidecar / debug 能力单独讨论。

## 8. 当前不再作为本目录主范围的内容

以下内容当前不算 `docs/chunking/` 的主范围：

- benchmark 选型讨论
- block-based vs protected-md 的旧方向讨论
- 完整检索链路评测
- rerank / rewrite / hybrid retrieval 调优

如果以后重新启动评测项目，应新开文档，并以当时真实代码状态为准。

## 9. 当前完成判定

当前这轮“分块入库”可以视为完成，完成标准是：

- 入库专用 `ingest-md` 已落地
- `PdfRagIngestService` 已收敛为提取/渲染层，不再承担旧分块产出
- `SmartChunkingService` 的 5 个模式已保留
- 第三方 splitter 已接入模式内部
- metadata 输出已统一到现有系统契约

## 10. 后续如果继续

后续如果还继续写 `docs/chunking/`，只允许记录：

- 对当前实现的修订
- 新增 splitter / mode 的真实接线
- metadata 字段的真实变化
- 已经开始并落地的后续评测工作

不要再把未实现方案、早期脑图、候选路线写回主文档。
