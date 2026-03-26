# 在线多模态精读入库方案

## 1. 背景

当前知识库文档入库链路适合通用文本，但对科研 PDF 仍有两个核心短板：

1. PDF 原生文本层经常有坏字形、错阅读顺序、公式丢失、表格打散。
2. 现有分块主要基于提取后的纯文本，无法把 `公式 / 表格 / caption / footnote` 当成一等对象。

仓库当前已有两部分能力可以直接复用：

- 知识库上传与后台处理入口在 [backend/app/api/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py)
- Reader 侧已有 DashScope 多模态调用封装和 PDF 页面级布局理解能力，分别在 [backend/app/services/dashscope_multimodal_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/dashscope_multimodal_service.py) 和 [backend/app/services/reader_multimodal_layout_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/reader_multimodal_layout_service.py)

这份方案的目标不是替换现有本地链路，而是新增一条用户可选的 `在线多模态精读入库` 管线。

## 2. 目标与边界

### 2.1 目标

- 为 PDF 提供一条独立的高质量入库模式，直接看页图提取正文、公式、表格。
- 让模型负责结构化提取和语义边界判断，但最终 chunk 仍由程序落地。
- 保留页码、bbox、块类型等可追溯信息，支撑后续 citation 和 query-aware rerank。
- 让用户在上传时显式选择处理模式，而不是隐式替换现有链路。

### 2.2 非目标

- 不在第一期替换所有文档类型，只做 PDF。
- 不在第一期做图片内容描述和 figure OCR，图片直接跳过或保留占位。
- 不在第一期改写整个检索系统，只补一条新的入库管线。
- 不把模型自由生成的 chunk 文本当唯一真相，避免无约束重写。

## 3. 产品形态

上传 PDF 时新增处理模式：

- `本地快速`
  - 保持当前逻辑。
  - 适合普通文本型 PDF 或对成本敏感的场景。
- `在线多模态精读`
  - 渲染页图，调用多模态模型提取块级结构。
  - 适合论文、公式多、表格多、双栏复杂版面。
- `自动`
  - 先做轻量页级检测，再在 `本地快速` 和 `在线多模态精读` 间路由。

同时增加提取目标：

- `通用`
- `论文/公式优先`
- `表格优先`

第一期默认建议：

- 仅对 `pdf` 展示处理模式。
- 默认值设为 `本地快速`，避免上线初期意外放大成本。
- 当知识库或系统级开关启用后，再允许用户选择 `在线多模态精读`。

## 4. 核心设计原则

### 4.1 模型决定边界，程序决定落库

不让模型一步直接输出最终 chunks。正确流程是：

1. 模型输出结构化 `PageBlock`
2. 模型输出文档级 `ChunkPlan`
3. 程序按 `ChunkPlan + token 限制 + 追溯元数据` 生成最终 `DocumentChunk`

这样可以同时拿到：

- 多模态对复杂版面的理解能力
- 语义分块能力
- 可控的 chunk 大小
- 页码 / bbox / block id 级追溯

### 4.2 结构化提取优先于纯文本修复

不继续把工程重点放在“抽出坏文本后再修复”。对科研 PDF，更合理的顺序是：

1. 优先多模态看页图
2. 输出结构化块
3. 文本层仅作为辅助对齐、失败兜底和质量检查

### 4.3 与现有链路并存，而不是硬切换

现有 `pdf_rag_line_pipeline_enabled` 仍保留，作为：

- 低成本 fast path
- 在线链路失败时的 fail-open 兜底
- 非 PDF 文档的默认路径

## 5. 方案总览

### 5.1 上传入口

修改 [backend/app/api/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py) 的上传接口，新增表单字段：

- `ingest_mode`
  - `local_fast | online_mm | auto`
- `extract_profile`
  - `general | academic_formula | table_first`

第一期不新增数据库列，直接写入 `Document.metadata_`：

```json
{
  "ingest_request": {
    "mode": "online_mm",
    "extract_profile": "academic_formula",
    "requested_by": 1
  }
}
```

### 5.2 后台处理分支

在 [backend/app/api/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py) 的 `process_document_task()` 内新增分支顺序：

1. 判断是否为 `pdf`
2. 读取 `doc.metadata_.ingest_request`
3. 若命中 `online_mm`，走 `OnlineMmIngestService`
4. 若命中 `auto`，先做文档/页级检测再决定走向
5. 否则保持当前 `pdf_rag_line_pipeline_enabled -> extract_text -> smart_chunking` 链路

### 5.3 新增服务

新增 [backend/app/services/online_mm_ingest_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/online_mm_ingest_service.py)：

- 输入：
  - `file_path`
  - `document_name`
  - `extract_profile`
- 输出：
  - `document_text`
  - `blocks`
  - `chunk_plan`
  - `chunks`
  - `report`
  - `applied`
  - `failure_reason`

职责拆分为 6 步：

1. `render_pdf_pages`
   - 将 PDF 渲染为页图
   - 第一期开 `dpi=180~220`
2. `extract_page_blocks`
   - 每页调用多模态模型，输出结构化块
3. `merge_document_blocks`
   - 合并页级块，修正跨页标题、脚注、重复 header/footer
4. `plan_chunks`
   - 基于块序列生成 chunk 边界计划
5. `materialize_chunks`
   - 生成最终可入库 chunk 文本和 metadata
6. `build_report`
   - 记录模型、耗时、token、页数、失败页等统计

## 6. 数据契约

### 6.1 PageBlock

第一期统一块 schema：

```json
{
  "block_id": "p0005_b0012",
  "type": "paragraph",
  "page": 5,
  "order": 12,
  "bbox": { "x0": 82.4, "y0": 131.6, "x1": 510.7, "y1": 228.9 },
  "text": "Attention is all you need ...",
  "latex": null,
  "table_markdown": null,
  "title_hint": null,
  "confidence": 0.93,
  "needs_review": false
}
```

`type` 第一期开这些值：

- `heading`
- `paragraph`
- `list`
- `equation`
- `table`
- `caption`
- `footnote`
- `header`
- `footer`

约束：

- `heading/paragraph/list/footnote` 用 `text`
- `equation` 优先填 `latex`
- `table` 优先填 `table_markdown`
- `header/footer` 默认不进最终 chunks，但保留在结构流里用于去重和质量诊断

### 6.2 ChunkPlan

模型不直接返回最终 chunk 文本，而是返回块边界：

```json
{
  "chunk_id": "c0018",
  "chunk_type": "table",
  "title": "Table 3: Translation quality",
  "block_ids": ["p0008_b0007", "p0008_b0008"],
  "needs_parent_context": true,
  "retrieval_tags": ["table", "results", "bleu"]
}
```

第一期 `chunk_type`：

- `paragraph`
- `equation`
- `table`

### 6.3 最终 DocumentChunk metadata

不要求第一期改 SQL 表结构，先扩展 [backend/app/models/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/models/knowledge.py) 里的 `DocumentChunk.metadata_`：

```json
{
  "ingest_mode": "online_mm",
  "chunk_type": "equation",
  "block_ids": ["p0006_b0005"],
  "page_span": [6, 6],
  "bboxes": [
    { "page": 6, "x0": 91.2, "y0": 241.0, "x1": 507.7, "y1": 290.2 }
  ],
  "source_model": "qwen3-vl-flash",
  "extract_profile": "academic_formula",
  "has_table": false,
  "has_equation": true
}
```

这一步可以避免第一期引入 Alembic 迁移。

## 7. 模型与提示词策略

### 7.1 默认模型

第一期默认：

- 主模型：`qwen3-vl-flash`

原因：

- 现有 Reader 配置里已经有 `qwen3-vl-flash` 的稳定落点
- 本地实际小样评估中，`qwen3-vl-flash` 对公式和表格结构明显更稳
- 当前实现明确采用 fail-fast，不做模型级兜底

### 7.2 两阶段提示词

第一阶段：`page_block_extract`

- 输入：单页页图
- 输出：该页 `PageBlock[]`
- 要求：
  - 只输出 JSON
  - 不补写图片说明
  - 不编造不存在文本
  - 表格使用 `table_markdown`
  - 公式使用 `latex`

第二阶段：`document_chunk_plan`

- 输入：全部 `PageBlock[]`
- 输出：`ChunkPlan[]`
- 要求：
  - 只输出边界和 chunk 类型
  - 不改写已有块内容
  - 优先保持标题-正文、caption-表格、公式-解释段的语义邻近

## 8. 自动路由策略

`auto` 不引入训练模型，先用规则：

- 文本层字符异常比例过高
- 页内重复 overlay 文本明显
- 双栏跨栏拼接严重
- 公式密度高
- 表格密度高
- 扫描页占比高

命中条件时直接走 `online_mm`。不再训练一套“是否需要修复”的分类模型。

## 9. 后端改动清单

### 9.1 配置项

在 [backend/app/config.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/config.py) 增加：

- `kb_online_mm_ingest_enabled: bool = False`
- `kb_online_mm_default_mode: Literal["local_fast", "online_mm", "auto"] = "local_fast"`
- `kb_online_mm_primary_model: str = "qwen3-vl-flash"`
- `kb_online_mm_chunk_planner_model: str = "qwen3.5-plus"`
- `kb_online_mm_timeout_ms: int = 120000`
- `kb_online_mm_render_dpi: int = 200`
- `kb_online_mm_pages_per_call: int = 1`
- `kb_online_mm_fail_open_to_local: bool = True`
- `kb_online_mm_max_pages_soft_limit: int = 80`
- `kb_online_mm_max_estimated_cost_rmb: float = 1.0`

### 9.2 上传接口与 schema

需要改动：

- [backend/app/api/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/knowledge.py)
- [backend/app/schemas/knowledge.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/schemas/knowledge.py)

建议新增：

- `DocumentUploadMode`
- `DocumentExtractProfile`
- `DocumentUploadOptions`

`DocumentUploadResponse` 补充返回：

- `processing_mode`
- `extract_profile`

### 9.3 Service 层

新增：

- [backend/app/services/online_mm_ingest_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/online_mm_ingest_service.py)

复用：

- [backend/app/services/dashscope_multimodal_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/dashscope_multimodal_service.py)

建议抽取公共能力：

- 从 Reader 相关服务中抽出 PDF 页图渲染和 JSON 校验的通用 helper，避免把 `reader_multimodal_layout_service.py` 直接硬塞进知识库入库链路

原因很简单：

- Reader 当前的设计目标是“布局辅助，不改写正文”
- 新入库链路的目标是“结构化提取 + 语义分块”
- 两者可以复用底层能力，但不应共享同一套 prompt contract

## 10. 前端改动清单

### 10.1 API 与 store

需要改动：

- [frontend/src/services/api.ts](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/services/api.ts)
- [frontend/src/stores/knowledgeStore.ts](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/stores/knowledgeStore.ts)

把当前：

```ts
uploadDocument(kbId, file)
```

扩展为：

```ts
uploadDocument(kbId, file, options)
```

`options` 第一版：

- `ingestMode`
- `extractProfile`

### 10.2 上传交互

需要改动：

- [frontend/src/pages/knowledge/KnowledgePage.tsx](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/pages/knowledge/KnowledgePage.tsx)

当前上传按钮是直接 `Upload -> beforeUpload -> handleUpload(file)`。

改为：

1. 先选择文件
2. 若是 PDF，弹出“处理方式”对话框
3. 用户确认后再调用上传接口

对话框展示：

- 处理模式
- 提取目标
- 预计耗时
- 预计成本
- “适合论文/公式/表格复杂 PDF” 的说明

第一期不需要复杂设计，使用现有 Ant Design Modal + Radio + Select 即可。

## 11. 入库与检索兼容策略

第一期不改检索主流程，但新增 chunk metadata 后，要保证以下约定：

- `table` chunk 的 `section_type` 可映射为 `table`
- `equation` chunk 的 `section_type` 可映射为 `equation`
- 普通正文仍为 `paragraph`

这样现有检索、精排和上下文压缩服务先不用推倒重写，只需在后续阶段逐步加权：

- query 含 `table/results/ablation/BLEU` 时优先 `table`
- query 含 `equation/loss/formula/objective` 时优先 `equation`

## 12. 失败与回退

第一期必须 fail-open：

- 模型调用失败
- JSON 校验失败
- 成本超限
- 页数超软限制
- 文档结构结果为空

回退策略：

1. 记录 `doc.metadata_.online_mm_ingest.error`
2. 若 `kb_online_mm_fail_open_to_local = true`，回退到当前本地链路
3. 否则标记文档失败，并把失败原因回传给前端

## 13. 测试与验收

### 13.1 后端测试

新增：

- 上传接口参数解析测试
- `auto` 路由规则测试
- `PageBlock` / `ChunkPlan` JSON 校验测试
- `materialize_chunks()` 单测
- fail-open 回退测试

建议放在：

- `backend/tests/test_knowledge_upload_api.py`
- `backend/tests/test_online_mm_ingest_service.py`

### 13.2 前端测试

至少覆盖：

- PDF 上传时展示处理模式弹窗
- 非 PDF 上传时不展示多模态选项
- 处理模式和提取目标能正确传给 API

### 13.3 样本验收集

第一期直接使用真实科研 PDF 做回归集，不要只用 toy 文档。

建议最小集：

- `Attention Is All You Need`
  - 正文双栏
  - 数学公式
  - 结果表格
- 另选一篇扫描质量差的 PDF
- 另选一篇表格密集型 PDF

验收口径：

- 公式是否以可用 LaTeX 形式落地
- 表格是否保留列结构
- chunk 是否能按语义而不是硬字符切断
- 页面引用是否可追溯

## 14. 分阶段落地

### Phase 1

目标：后端可用，前端暂不开放

- 配置项
- `OnlineMmIngestService`
- 手工 API 参数触发
- fail-open 回退
- 基础测试

### Phase 2

目标：知识库上传页可选

- `KnowledgePage` 上传弹窗
- API/store 参数打通
- 基础成本/耗时提示

### Phase 3

目标：自动路由与检索增强

- `auto` 模式
- query-aware chunk type weighting
- 文档处理统计面板

## 15. 推荐的第一刀

如果只做一刀最值钱的实现，建议按下面顺序：

1. 在上传 API 增加 `online_mm` 处理模式
2. 新增 `OnlineMmIngestService`
3. 只支持三类 chunk
   - `paragraph`
   - `equation`
   - `table`
4. 主模型固定 `qwen3-vl-flash`
5. 失败回退当前本地链路

这条路径能最快验证核心价值：

- 复杂科研 PDF 的提取质量是否明显提升
- 大模型语义分块是否优于当前纯文本链路
- 成本是否处于可接受范围

## 16. 结论

这条方案不是“把现有 PDF 文本提取再修一修”，而是新增一条正式的高质量入库管线：

- 上传时用户可选
- 结构化提取优先
- 模型负责结构和边界
- 程序负责最终落库
- 现有本地链路保留为 fast path 和 fail-open

这样做的收益是清晰的：

- 工程复杂度比“继续修坏文本 + 训练修复判定模型”更低
- 对科研 PDF 的质量提升更直接
- 与现有知识库/RAG 架构兼容，不需要一次性推倒重来
