# PDF2MD 当前实现说明

本文档只描述仓库里**当前活动中的本地 PDF 解析主线**，不描述已经废弃或冻结的旧实验路线。

当前目标可以概括为两句话：

- PDF 先被解析成结构化文档 `PdfStructuredDocument`
- 再由 `LocalPdfMarkdownRenderer` 渲染成 Markdown，供 benchmark、预览和知识库入库使用

## 1. 当前对外模式

当前知识库上传侧与本地 PDF 解析相关的模式有三种：

- `local_fast`
  - 默认模式
  - 走纯本地规则解析
- `local_hybrid`
  - 走本地路由 + `docling-fast` backend
- `online_mm`
  - 在线多模态精读
  - 不属于本文重点

后端模式映射在：

- `local_fast -> fast`
- `local_hybrid -> hybrid`

对应代码：

- `backend/app/api/knowledge.py`
- `backend/app/services/pdf_rag_ingest_service.py`
- `backend/app/schemas/knowledge.py`

当前默认配置：

- `backend/app/config.py`
- `pdf_rag_structured_mode = "fast"`

## 2. 当前本地 PDF 主线

### 2.1 fast 模式

`fast` 模式使用：

- `backend/app/services/local_structured_pdf/pipeline.py`
- 类：`LocalStructuredPdfPipeline`

当前流水线顺序是：

1. `LocalPdfNativeExtractor`
2. `LocalPdfPageNormalizer`
3. `LocalPdfDocumentResolver`
4. `LocalPdfBlockBuilder`
5. `LocalPdfTableDetector`
6. `LocalPdfBlockRoleResolver`
7. 启发式后处理（`balanced` 配置下开启）
   - `LocalPdfAuxiliaryBlockResolver`
   - `LocalPdfFrontMatterResolver`
   - `LocalPdfHeadingRefiner`
   - `LocalPdfTocResolver`
8. `LocalPdfSectionResolver`

`PdfRagIngestService` 当前创建 `fast` pipeline 时使用：

- `heuristic_profile="balanced"`

### 2.2 hybrid 模式

`hybrid` 模式使用：

- `backend/app/services/local_structured_pdf/docling_fast_hybrid_pipeline.py`
- 类：`LocalStructuredPdfDoclingFastHybridPipeline`

它的职责是：

1. 先跑一遍本地结构化主线，得到本地文档和 triage 输入
2. 用 `docling-fast` 的 Python 版 orchestrator 做页级路由
3. 如果命中 backend 页，则把**整份 PDF**发送给 `docling-fast backend`
4. 把 backend 返回结果转成内部结构
5. 和本地结果做融合
6. 再跑一次本地后处理，得到最终 `PdfStructuredDocument`

这里要注意两点：

- 路由是页级的
- 但当前 backend 请求语义是 whole-PDF，不是裁页子 PDF

## 3. 结构化输出是什么

本地 PDF 解析的真实产物不是 Markdown，而是：

- `PdfStructuredDocument`
- `PdfSemanticBlock`

定义在：

- `backend/app/services/local_structured_pdf/contracts.py`

### 3.1 `PdfStructuredDocument`

核心字段：

- `pages`
- `blocks`
- `body_font_size`

### 3.2 `PdfSemanticBlock`

当前块级元数据包含：

- `block_id`
- `block_type`
- `page_start`
- `page_end`
- `text`
- `bbox`
- `line_ids`
- `column_id`
- `region`
- `avg_font_size`
- `reading_order_start`
- `reading_order_end`
- `heading_level`
- `parent_heading_id`
- `section_heading_ids`
- `section_titles`
- `section_path`
- `table_rows`

这意味着当前系统在“结构化阶段”是**知道块边界和块类型的**，不是只拿一段纯文本。

## 4. Markdown 是怎么生成的

Markdown 渲染器在：

- `backend/app/services/local_structured_pdf/markdown_renderer.py`

类：

- `LocalPdfMarkdownRenderer`

当前渲染规则比较朴素：

- `heading`
  - 渲染为 `#` 到 `######`
- `table`
  - 渲染为 GitHub 风格 Markdown 表格
- `list_item`
  - 保留原列表标记；没有标记时补 `- `
- 其它块
  - 统一压平为普通文本段落

块与块之间用一个空行分隔。

这份 Markdown 当前主要用于：

- benchmark 输出
- 预览
- 知识库 `document_text`

## 5. 知识库入库现在怎么用这条主线

知识库 PDF 入库主服务在：

- `backend/app/services/pdf_rag_ingest_service.py`

当前流程是：

1. `fast` 或 `hybrid` 解析出 `PdfStructuredDocument`
2. 渲染成完整 Markdown，作为 `document_text`
3. **直接从 `document.blocks` 构建 chunks**

也就是说，当前主线 chunk 不是从 Markdown 再切出来的，而是从结构化 block 直接生成。

## 6. 当前 chunk 生成方式

当前 structured PDF 入库 chunk 逻辑也在：

- `backend/app/services/pdf_rag_ingest_service.py`

现状可以概括为：

- 基本接近“一个 block 一个 chunk”
- 不是 `SmartChunkingService` 主导
- 不是从 Markdown 二次切分

每个 chunk 目前会带上这些 metadata：

- `source_kind = pdf_structured_rag_v2`
- `structured_mode`
- `block_id`
- `block_type`
- `raw_block_content`
- `pages`
- `bbox`
- `line_ids`
- `page_span`
- `section_path_titles`
- `section_path`
- `heading_level`
- `table_row_count`

### 6.1 当前 chunk 设计的特点

优点：

- 能保留块边界
- 表格不会被文本 chunker 切碎
- heading / section path 元数据天然存在

限制：

- chunk 大小不均匀
- 还没有接入当前通用 `SmartChunkingService`
- `extract_profile / extract_granularity` 这两个在线模式参数，不是本地 structured 主线的真实 chunking 控制面

## 7. `docling-fast backend` 当前实际状态

backend 服务文件在：

- `backend/app/services/local_structured_pdf/opendataloader_upstream_hybrid_server.py`

当前它已经是仓库里的正式运行文件，不依赖 `tmp/` 运行时。

### 7.1 当前默认值

在 `docker-compose.yml` 中，当前默认是：

- `PDF_HYBRID_BACKEND_FORCE_OCR=false`
- `PDF_HYBRID_BACKEND_ENRICH_FORMULA=false`
- `PDF_HYBRID_BACKEND_ENRICH_PICTURE_DESCRIPTION=false`

因此当前主线实际跑的是：

- Docling 默认 OCR / layout / table structure
- 不开 formula enrich
- 不开 picture description

### 7.2 Qwen 当前的真实状态

当前 `Qwen` 不在 benchmark 主线中发挥核心作用。

具体状态：

- `Qwen OCR`
  - 还没有真正接成 Docling OCR engine
  - 当前只保留了 placeholder 日志
- `picture description`
  - 代码支持接本地 Ollama / OpenAI-compatible 接口
  - 但默认关闭
- `formula enrich`
  - 代码支持开关
  - 但默认关闭

因此当前主线可以理解为：

- `fast`
  - 纯本地规则 PDF 解析
- `hybrid`
  - 本地规则 + Docling 默认 backend 能力

而不是：

- `Qwen` 主导的 PDF 解析

## 8. 当前与 Smart Chunking 的关系

当前状态是：

- 原生 `md/txt` 仍然走 `SmartChunkingService`
- structured PDF 主线还没有接入 `SmartChunkingService`

也就是说，仓库里现在实际上存在两种 chunk 入口：

1. 通用文本 chunking
2. structured PDF block-based chunking

这也是当前系统里一个明确还没有收敛完成的点。

## 9. 当前这条主线已经替代了什么

当前 structured PDF 主线已经替代的是：

- 旧的 line-based PDF RAG 主提取路线

当前知识库 PDF 上传主线已经变成：

- `local_fast`
- `local_hybrid`
- `online_mm`

其中：

- `local_fast` / `local_hybrid`
  - 都走 `PdfRagIngestService`
- 旧的 line-RAG 不再是 PDF 上传主提取逻辑

## 10. 已知限制

本文只记录当前真实存在的限制，不写未来计划。

### 10.1 structured PDF 主线还没接入统一智能分块

当前 PDF structured 入库 chunk 仍由 `PdfRagIngestService` 自己生成，不走统一的 `SmartChunkingService`。

### 10.2 当前 hybrid backend 仍是 Docling 默认 OCR

当前 `Qwen OCR` 没有真正接入 Docling OCR stage。

### 10.3 formula / picture 当前默认不增强

默认不开：

- `formula enrich`
- `picture description`

### 10.4 Markdown 是降维表示

结构化阶段拥有完整块元数据；Markdown 只是渲染结果，不是结构化真相源。

## 11. 关键代码索引

### 11.1 解析与混合路由

- `backend/app/services/local_structured_pdf/pipeline.py`
- `backend/app/services/local_structured_pdf/docling_fast_hybrid_pipeline.py`
- `backend/app/services/local_structured_pdf/docling_fast_triage_service.py`
- `backend/app/services/local_structured_pdf/hybrid_backend_transformer.py`
- `backend/app/services/local_structured_pdf/hybrid_fusion_service.py`

### 11.2 结构化契约与渲染

- `backend/app/services/local_structured_pdf/contracts.py`
- `backend/app/services/local_structured_pdf/markdown_renderer.py`

### 11.3 知识库入库

- `backend/app/services/pdf_rag_ingest_service.py`
- `backend/app/api/knowledge.py`
- `backend/app/schemas/knowledge.py`

### 11.4 backend 运行时

- `backend/app/services/local_structured_pdf/opendataloader_upstream_hybrid_server.py`
- `docker-compose.yml`

### 11.5 模型缓存与运行时模型说明

- `docs/models/README.md`

## 12. 当前结论

当前仓库里的“PDF2MD”已经不是简单的“PDF -> 纯文本 -> Markdown”。

它的真实形态是：

1. PDF -> 结构化块文档
2. 结构化块文档 -> Markdown
3. 知识库主线当前仍直接使用结构化块做 chunk

所以如果后面要继续演进，真正应该继续收敛的是：

- 结构化 PDF 与统一智能分块的关系
- 而不是再回到旧的 line-based PDF 提取路线
