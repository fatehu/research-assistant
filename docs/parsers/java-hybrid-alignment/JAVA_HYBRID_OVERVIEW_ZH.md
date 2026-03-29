# Java Hybrid 总览

这份文档只记录已经确认的 Java hybrid 思路，不扩展未验证设计。

## 1. 总原则

Java hybrid 的核心不是把全文直接交给大模型，而是：

1. 本地规则链做主干。
2. 页级 triage 先分流。
3. 只有复杂页才送 backend。
4. backend 返回松结构 JSON。
5. Java 再用 `DoclingSchemaTransformer` 转内部对象。这个表述目前应按当前研究结论理解；仓库里可直接核对的对应实现是 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。
6. 最后再做本地后处理。

也就是说，Java hybrid 的默认思路是“本地优先，复杂页补强”，不是“全量模型化”。

## 2. 典型流程

当前研究结论里的链路是：

1. 本地处理。
2. 页级 triage。
3. 复杂页批量送 backend。
4. backend 返回 docling-like 结构。
5. Java transformer 转内部对象。
6. 本地后处理收口。

这个顺序在当前研究材料里更适合写成“Java 侧保留变换和收口控制”。可核对的源码路径是 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py) 和 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。至于“backend 不直接承担最终 schema 收口责任”，当前没有同等稳定的 Java 源码支撑，建议保守表述为研究结论。

## 3. backend 和模型分别做什么

### backend 做什么

backend 不是最终 schema 输出器，也不是 line_id 提供器，更不是最终 Markdown 生成器。它更像一个文档元素 JSON 提供者，输出的是松结构结果，供后续 transformer 收口。可直接核对的实现路径是 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。

当前研究结论里，backend 返回的对象类型更接近：

- `texts`
- `tables`
- `pictures`
- `label`
- `prov`
- `bbox`

### 模型做什么

模型主要用于复杂页补强，尤其是：

- 复杂阅读顺序页
- 真视觉页
- 图文混排页
- 结构信号不足的难页

模型不是 Java hybrid 的主干，不负责整份文档的默认路径。

## 4. 什么时候使用 backend

backend 只在复杂页使用。当前研究结论是：

- 常规数字 PDF 不需要强行全量 backend。
- 只有 triage 判定复杂、或本地信号不足时，才送 backend。
- `auto` 模式只送复杂页。
- `full` 模式才是整份文档都走 backend。

这意味着 backend 更接近 selective fallback / augmentation 资源，而不是默认全局执行器；这一点可由 [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py) 和 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py) 里的 `auto`/`full` 分流逻辑侧面核对。

## 5. OCR、picture description、docling-fast、SmolVLM、EasyOCR 的角色

### `auto`

`auto` 表示只把复杂页送 backend，普通页继续走本地链，可核对路径是 [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py) 和 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py)。

### `full`

`full` 表示整份文档都走 backend，属于更重的模式，可核对路径同上。

### `OCR`

OCR 只用于扫描件或 image-based PDF。标准 digital PDF 不需要 `--force-ocr`。

### `picture description`

picture description 是图像描述能力，不等于主解析链。

### `docling-fast`

`docling-fast` 是 hybrid backend，本质上是文档解析服务侧的主后端之一。

### `SmolVLM 256M`

`SmolVLM 256M` 只用于 picture description，不是 hybrid 主解析器。

### `EasyOCR`

`EasyOCR` 是 OCR 路径的一部分，属于扫描件/图像文本识别链路。

## 6. 这套思路的边界

从当前研究结论看，Java hybrid 的关键不是模型更大，而是职责切分更清楚：

- 本地规则链负责主干稳定性。
- triage 负责页级分流。
- backend 负责复杂页结构补强。
- Java transformer 负责内部对象转换。
- 本地后处理负责最终收口。

## 7. 可对照的仓库路径

以下路径是当前仓库里已经存在、且和本专题强相关的对照材料：

- [docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md)
- [docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md)
- [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py)
- [backend/app/services/local_structured_pdf/hybrid_planner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_planner.py)
- [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)
- [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py)
