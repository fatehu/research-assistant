# PDF Layout 解析启用决策与落地说明（2026-02-12）

## 1. 背景与目标

现有 PDF 处理链路主要依赖 `pypdf/pdfplumber + 正则/启发式清洗`。  
在复杂排版论文中，容易出现两类问题：

- 图表内 OCR 碎片混入正文，影响分块质量。
- 仅靠正则清洗存在误删正文或漏删噪声风险。

本次目标：

- 引入 layout-aware 解析器作为上游能力，减少对后置正则清洗的依赖。
- 保持线上稳定性，避免“某些 PDF 变好、某些 PDF 变差”。

## 2. 评测结论（真实论文样本）

样本：

- `Attention Is All You Need`（1706.03762）
- `Llama2`（2307.09288）

核心指标：

- `fragment_ratio`：碎片行比例（越低越好）
- `sentence_like_ratio`：句子化比例（越高越好）

### 2.1 原始提取对比（未清洗）

| 文档 | 解析器 | fragment_ratio | sentence_like_ratio |
|---|---|---:|---:|
| Attention | pypdf | 0.4489 | 0.0987 |
| Attention | markitdown | 0.4143 | 0.1065 |
| Llama2 | pypdf | 0.2145 | 0.1618 |
| Llama2 | markitdown | 0.5467 | 0.1039 |

结论：

- `markitdown` 在部分文档（Attention）有改善。
- 但在部分文档（Llama2）明显退化，不适合“无条件全量替换”。

### 2.2 实际链路对比（提取后再 preprocess）

| 文档 | 链路 | fragment_ratio | sentence_like_ratio |
|---|---|---:|---:|
| Attention | pypdf + preprocess | 0.1028 | 0.1621 |
| Attention | markitdown + preprocess | 0.2184 | 0.1421 |
| Llama2 | pypdf + preprocess | 0.0707 | 0.1936 |
| Llama2 | markitdown + preprocess | 0.3137 | 0.1576 |

结论：

- 仅靠“固定启用 markitdown”不能保证整体质量优于 pypdf。
- 必须加入自动质量门控与回退。

## 3. 最终启用策略

采用“启用 layout + 自动降级”的保守方案：

1. 默认尝试 `markitdown[pdf]`。
2. 对 layout 输出做质量评估：
   - `lines >= 200`
   - `fragment_ratio >= 0.45`
   - `sentence_like_ratio <= 0.12`
3. 若命中退化阈值，放弃该 layout 结果，继续回退到下一解析器或 `pypdf/pdfplumber`。

运行时烟测结果：

- Attention：`extractor=markitdown`
- Llama2：`extractor=pypdf`（markitdown 被质量门控拦截后自动回退）

## 4. 为什么暂不默认启用 docling

`docling` 在当前镜像组合（`torch/torchvision/transformers`）下存在运行时兼容性问题：

- `RuntimeError: operator torchvision::nms does not exist`
- 导致 `from docling.document_converter import DocumentConverter` 失败

因此当前版本保留 `docling` 为注释依赖，代码仍支持可选导入（未来可在独立依赖矩阵验证后再启用）。

## 5. 代码改动摘要

- 新增 layout 提取与回退链路：`backend/app/services/document_service.py`
- 新增 layout 输出质量门控：`backend/app/services/document_service.py`
- 新增配置项：
  - `PDF_LAYOUT_PARSER`
  - `PDF_LAYOUT_MIN_CHARS`
  - 文件：`backend/app/config.py`、`.env.example`
- OCR 清洗只在 PDF 且有噪声信号时触发：
  - `backend/app/services/smart_chunking/text_preprocessor.py`
  - `backend/app/services/smart_chunking/service.py`
- 记录实际 PDF 提取器到文档 metadata：
  - `backend/app/api/knowledge.py`
- 依赖调整：
  - 启用：`markitdown[pdf]>=0.1.4`
  - 保留注释：`docling>=2.73.0`
  - 文件：`backend/requirements.txt`

## 6. 测试与验证

已通过：

- `pytest tests/test_text_preprocessor_pdf_cleanup.py tests/test_document_service_layout_parser.py -q`
- `pytest tests/test_smart_chunking.py::TestChunkingService::test_fixed_chunking_basic -q`
- 容器内真实 PDF 烟测（自动选择/回退验证）

## 7. 运维建议

- 线上先保持：`PDF_LAYOUT_PARSER=auto`。
- 如遇特定批次文档质量下降：
  - 临时切回：`PDF_LAYOUT_PARSER=none`（直接 pypdf/pdfplumber）。
- 若后续需要启用 `docling`：
  - 先在隔离环境验证 `torch/torchvision/transformers` 版本矩阵，再放开依赖注释。
