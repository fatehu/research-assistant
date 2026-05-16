# Local Structured PDF 本地模式说明（2026-03-26）

## 1. 背景与目标

本地模式是为当前仓库新建的一条独立 PDF 解析线，目标是参考 `opendataloader-pdf` 的本地规则思路，在 Python 里实现一条可持续演进的本地结构化解析器。

这条线的核心目标是：

1. 把 PDF 先解析成结构化块，而不是直接抽扁平文本。
2. 用确定性规则优先解决大多数数字 PDF 的阅读顺序、标题层级和表格恢复。
3. 为后续 selective hybrid 打好基础，而不是一开始就把整条链交给多模态模型。
4. 与旧 PDF 入库链路隔离，先把新解析器打磨成熟，再决定如何接入知识库主流程。

当前源码目录：

- [local_structured_pdf](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf)

## 2. 当前定位

本地模式目前是：

- 一条新的独立解析链
- 默认以确定性本地处理为主
- 可输出结构化块和 Markdown
- 已具备完整 benchmark 与外部 holdout 验证工具链

本地模式目前还不是：

- 旧 PDF 上传主链的直接替代
- OCR 主链
- 全量多模态解析链
- 针对扫描件/海报页的最终方案

## 3. 架构概览

当前本地模式的处理流程是：

1. `native extract`
   - [native_extractor.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/native_extractor.py)
   - 组合使用 `pdfplumber + PyMuPDF + pypdf`
   - 提取 `words/chars/images/lines/rects/curves/text_blocks/tables/struct-tree flags`

2. `page normalize`
   - [page_normalizer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_normalizer.py)
   - 过滤页面噪声、页边碎片、竖排边栏元数据，并组装稳定 text lines

3. `document resolve`
   - [document_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/document_resolver.py)
   - 负责页眉页脚、页码剔除、双栏判定、阅读顺序、跨 gutter 宽行拆分

4. `block build`
   - [block_builder.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/block_builder.py)
   - 从已排序行恢复 `heading / paragraph / list_item`

5. `table detect`
   - [table_detector.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/table_detector.py)
   - 恢复表格块并做表格结构物化

6. `post-process`
   - [block_role_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/block_role_resolver.py)
   - [auxiliary_block_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/auxiliary_block_resolver.py)
   - [front_matter_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/front_matter_resolver.py)
   - [heading_refiner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/heading_refiner.py)
   - [toc_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/toc_resolver.py)
   - [section_resolver.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/section_resolver.py)

7. `render`
   - [markdown_renderer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/markdown_renderer.py)
   - 输出 Markdown，用于 benchmark 和人工 review

8. `hybrid plan`
   - [page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py)
   - [hybrid_planner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_planner.py)
   - 先产出 `local/backend` 页级路由清单，为 selective hybrid 做入口层

9. `hybrid execute`
   - [ollama_page_parser.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/ollama_page_parser.py)
   - [hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)
   - [hybrid_fusion_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_fusion_service.py)
   - [hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py)
   - 只对 triage 判成 `backend` 的页调用本地 `Ollama Qwen VL`
   - 模型不直接输出最终内部 schema，而是先输出更松的 docling 风格 backend 结果
   - Python 侧 `hybrid_backend_transformer` 再把 backend 结果转成内部块，并统一走后处理
   - backend 文本家具项 `page_header / page_footer` 会在 transformer 层直接过滤
   - backend 页内对象会按 `bbox` 重排阅读顺序，不盲信模型给出的顺序

总入口：

- [pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/pipeline.py)
- [hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py)

## 4. 当前已完成的工作

### 4.1 原子级提取层

已经完成：

- 多引擎原子提取
- 统一数据契约
- 页级失败隔离和运行时依赖预检

对应文件：

- [contracts.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/contracts.py)
- [native_extractor.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/native_extractor.py)

### 4.2 本地阅读顺序主链

已经完成：

- 重复页眉页脚剔除
- 页码 / running head 剔除
- 双栏判定与左列后右列排序
- 跨 gutter 明显同行误拼拆分
- 避免把居中首页 front matter 和居中 display block 误判成双栏

### 4.3 结构块恢复

已经完成：

- `heading / paragraph / list_item` 基础恢复
- heading 层级估计
- section context 继承
- front matter 降级与首页重排
- caption / footnote 基础识别
- 目录/条目页角色处理

### 4.4 表格恢复

已经完成：

- 页级 `PyMuPDF table` 作为强结构信号
- 列锚点推断和表头压平
- 稀疏图表轴标签误判拦截
- 表格块保护，避免后续错误降级

### 4.5 评测与外部验证工具

已经完成：

- 本地 benchmark 导出脚本
- suite runner
- 外部 holdout 构建器
- READoc 小样本接入
- 人工 review 用的一页 PDF / PNG / Markdown 工作流

对应文件：

- [export_local_structured_pdf_benchmark.py](/mnt/d/codefield/agent-platform/research-assistant/backend/scripts/export_local_structured_pdf_benchmark.py)
- [run_local_structured_pdf_eval_suites.py](/mnt/d/codefield/agent-platform/research-assistant/backend/scripts/run_local_structured_pdf_eval_suites.py)
- [external_holdout_builder.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/external_holdout_builder.py)
- [readoc_holdout_builder.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/readoc_holdout_builder.py)

### 4.6 Hybrid v1 入口

已经完成：

- 页级 `triage`
- 本地 `Ollama Qwen VL` 页级结构解析器
- backend-like loose output -> transformer
- backend 页失败自动回退本地结果
- backend 页与本地结果的保守 merge
- merge 后继续复用本地后处理，而不是让模型直接写最终 Markdown

当前 Hybrid v1 的边界：

- 只做页级 selective hybrid，不做整篇全量多模态
- 模型优先返回更松的 `texts / tables / pictures / elements`
- prompt 不再让模型输出内部 `line_id`
- 内部锚点只留在程序侧使用，模型主协议改成 `label/text/bbox/prov`
- 最终 Markdown 仍然由本地程序渲染
- 当前还没有把 hybrid 接入旧知识库主链

### 4.7 Hybrid 当前模型策略

当前默认策略是：

1. `page triage` 先决定哪些页走 backend
2. backend 优先使用本地 `Ollama` 上的 `qwen3.5:0.8b`
3. 如有需要，可按链式升级到 `2b / 4b`

当前推荐配置：

- `LOCAL_STRUCTURED_PDF_HYBRID_MODEL_CHAIN=qwen3.5:0.8b,qwen3.5:2b-q4_K_M,qwen3.5:4b-q4_K_M`
- `LOCAL_STRUCTURED_PDF_HYBRID_ENABLE_NATIVE_FALLBACK=false`
- `LOCAL_STRUCTURED_PDF_HYBRID_DISABLE_THINKING=true`

这条链的意图不是默认把所有页都交给更大模型，而是：

- 先让 `0.8b` 处理大多数可解释难页
- 只有结果明显不足时，再升级到 `2b / 4b`
- 默认优先走 Ollama 的 `/v1/chat/completions`
- `native /api/chat` 只保留为显式开启的 fallback，不再作为默认路径
- 当前对文档解析任务推荐保持 non-thinking，避免小模型在 JSON 输出上发散

这和 `opendataloader-pdf` 的 hybrid 思路保持一致：

- 本地规则链仍是主干
- backend 只负责难页
- backend 输出先经过 transformer，再进入本地后处理

## 5. 当前评测状态

### 5.1 主 benchmark

当前主 benchmark 使用本地镜像：

- [local-bench](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/local-bench)

最新结果：

- [evaluation.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/local-bench/prediction/local-structured-pdf/evaluation.json#L12)

当前分数：

- `overall_mean = 0.8478255148717149`
- `nid_mean = 0.9021149020749729`
- `teds_mean = 0.604330973307236`
- `mhs_mean = 0.7684803115610138`

对照 Java 本地基线：

- [opendataloader baseline](/mnt/d/codefield/agent-platform/research-assistant/tmp/opendataloader-bench/prediction/opendataloader/evaluation.json#L12)

当前对比结论：

- `overall` 已高于 Java 本地线
- `TEDS` 明显高于 Java 本地线
- `MHS` 已高于 Java 本地线
- `NID` 仍低于 Java 本地线，剩余差距主要集中在复杂页面顺序

### 5.2 外部 READoc 小样本

当前外部样本结果：

- [READoc sample evaluation](/mnt/d/codefield/agent-platform/research-assistant/backend/eval/results/local_structured_pdf_external/balanced/readoc_arxiv_sample/prediction/local-structured-pdf/evaluation.json#L30)

当前分数：

- `overall_mean = 0.4712139806458938`
- `nid_mean = 0.672980365316281`
- `mhs_mean = 0.2694475959755066`

这组分数不能直接与 `opendataloader-bench` 横向比较。它的作用是：

- 暴露真实场景失败类型
- 约束启发式泛化
- 防止解析器只对主 benchmark 有效

## 6. 当前已经验证有效的通用改进方向

实践证明，下面这些方向是有效的：

1. 用外部样本找失败类型，而不是直接追外部分数。
2. 优先修页面几何和结构错误，而不是修最终文本表面现象。
3. 先把“假两栏、页边噪声、front matter 乱序”修掉，再谈更重的复杂块恢复。
4. 表格路径要尽量利用页面几何和表结构信号，而不是主要靠文本修补。

## 7. 当前已知不足

当前剩余不足主要不是普通论文正文，而是下面几类：

### 7.1 复杂阅读顺序

仍然是本地模式剩余最大短板，主要表现为：

- 首页 `hero / title / author / abstract / body` 复杂混排
- 卡片式页面
- 图文混排页
- 特殊展示块与正文的局部乱序

### 7.2 公式与 display block

当前对公式、展示块、定理环境、居中定义块的处理仍偏弱，容易出现：

- 公式前后顺序不稳
- display block 被拆成普通段落
- 数学内容的 Markdown 质量不足

### 7.3 视觉型页面

例如：

- 海报页
- 图片主导页
- 扫描页
- 弱文本页

这些页当前不属于本地模式的强项。

### 7.4 性能

当前本地模式在 `elapsed_per_doc` 上明显慢于 Java 基线。

这说明当前 Python 组合栈虽然能力面已经接近甚至超过，但速度还不是最终形态。

## 8. 后续本地模式最值得继续做的工作

### 第一优先级：继续补 `NID`

这是当前本地模式最直接的剩余差距。

建议重点做：

- 首页区域排序
- `hero/front-matter/body` 区域分流
- 复杂正文中的 display block 排序
- references / contents / appendix 页的区域级排序

### 第二优先级：page triage

建议引入页级分诊，把页面先分成：

- `plain_text`
- `dense_table`
- `sparse_form`
- `mixed_layout`
- `visual_page`
- `formula_or_display_heavy`

本地模式后续很多能力都应该建立在 page triage 之上。

### 第三优先级：region graph reading order

当前主排序仍然偏向：

- line order
- column order

后续应逐步升级成：

- region segmentation
- region graph ordering

### 第四优先级：公式和特殊展示块

建议新增：

- display-math / theorem / definition / proof block 识别
- 公式上下文排序
- 数学页面的局部专用路径

### 第五优先级：tagged PDF / struct tree 快路径

当前虽然已经探测 `mark_info` 和 `struct_tree`，但还没有真正建立完整的 tagged 快路径。

这块后续值得做，因为它对高质量数字 PDF 的收益会很高。

### 第六优先级：性能优化

建议后续单独做一轮性能收敛：

- 减少重复解析
- 缩减跨阶段数据复制
- 控制 `PyMuPDF` 与 `pdfplumber` 的重复工作
- 为大批量导出引入更稳的 profiling

## 9. 本地模式与 hybrid 的关系

当前阶段已经进入 hybrid v1，但不建议立刻把整条链改成全量多模态。

更合理的路线是：

1. 继续把本地模式作为默认主链。
2. 在本地模式前或中间加入 `page triage`。
3. 仅对低置信度页面走多模态补强。
4. 多模态只负责复杂页的区域理解、顺序补强和特殊块识别。
5. 最终 Markdown / chunk 仍尽量由本地程序生成。

当前已经落地的是：

- 页级 triage
- `Ollama Qwen VL` 页级 parser
- backend 页失败自动回退本地
- backend 页与本地结果的保守 merge
- benchmark/export 脚本已支持 `--pipeline-mode hybrid --hybrid-mode auto|full --write-trace`

当前需要特别澄清的一点：

- `opendataloader-pdf` 的 hybrid 主体并不是“让一个 256M 小视觉模型直接输出整页结构 schema”
- 它的主 backend 是 `docling-fast` 这类文档解析服务，客户端只请求 `JSON`，再由 Java 侧 transformer 转成内部对象
- 官方文档里提到的 `SmolVLM (256M)` 只用于 `picture description`，不是 hybrid 主解析器
- 因此，本仓库当前这条 `Ollama Qwen 0.8B` 本地 hybrid，只能视为“页级辅助解析实验线”，不能把它等同于 Java hybrid backend

这也解释了为什么小参数本地模型不适合承担过重职责：

- 它更适合返回松一点的 `elements/type/line_ids`，再由程序做 transform
- 不适合直接承担 OCR、复杂表格重建、整页自由结构生成
- 对 `visual_or_scanned` 这类弱文本页，当前 anchored parser 仍然不是最终方案

当前可调的 hybrid 关键环境变量：

- `OLLAMA_BASE_URL`
- `LOCAL_STRUCTURED_PDF_HYBRID_MODEL`
- `LOCAL_STRUCTURED_PDF_HYBRID_TIMEOUT_SECONDS`
- `LOCAL_STRUCTURED_PDF_HYBRID_RENDER_DPI`
- `LOCAL_STRUCTURED_PDF_HYBRID_MAX_IMAGE_SIDE`
- `LOCAL_STRUCTURED_PDF_HYBRID_MAX_LINES_PER_PAGE`

注意：

- `hybrid` 不再自动复用通用 `OLLAMA_MODEL`
- 必须显式设置 `LOCAL_STRUCTURED_PDF_HYBRID_MODEL`
- 这样可以避免把普通文本模型误当作视觉模型去解析 PDF 页图

当前还没有落地的是：

- 与知识库上传主链的接线
- `hybrid` benchmark 专项评测
- 多模态页结果的更细粒度 table/formula 专用后处理

也就是说，本地模式不是 hybrid 的过渡废案，而是 hybrid 的主干和兜底。

### 9.1 Upstream 兼容 hybrid backend

为了对齐 `opendataloader-pdf` 仓库的整体 hybrid 架构，而不只是对齐 Java 侧 orchestration，
当前仓库额外提供了一个 upstream 兼容 backend：

- [opendataloader_compat_server.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/opendataloader_compat_server.py)

它复用了 upstream Python backend 的基本服务形态：

- `GET /health`
- `POST /v1/convert/file`
- `status/document/errors/failed_pages` 响应 envelope

当前实现策略是：

- 保留 upstream 风格服务接口
- 用本仓库的本地/混合解析 adapter 替代 upstream 的 `DocumentConverter`
- 把结果转成 docling-like `pages/texts/tables/pictures` JSON

Docker Compose 已提供独立服务：

```bash
docker compose up -d pdf-hybrid-backend
```

默认端口：

- `http://localhost:5002/health`
- `http://localhost:5002/v1/convert/file`

默认模型链：

- `qwen3.5:0.8b`
- `qwen3.5:2b-q4_K_M`
- `qwen3.5:4b-q4_K_M`

可通过 `.env` 覆盖：

- `LOCAL_STRUCTURED_PDF_HYBRID_MODEL`
- `LOCAL_STRUCTURED_PDF_HYBRID_MODEL_CHAIN`
- `PDF_HYBRID_BACKEND_FORCE_OCR`
- `PDF_HYBRID_BACKEND_OCR_LANG`
- `PDF_HYBRID_BACKEND_ENRICH_FORMULA`
- `PDF_HYBRID_BACKEND_ENRICH_PICTURE_DESCRIPTION`
- `PDF_HYBRID_BACKEND_PICTURE_DESCRIPTION_PROMPT`

当前 compat backend 的 `force_ocr / enrich_formula / enrich_picture_description` 已经映射到 Qwen 路径里的任务提示，不再依赖 upstream 的 `EasyOCR / SmolVLM / docling-fast` 模型位下载。也就是说，独立 `pdf-hybrid-backend` 服务继续复用本仓库 backend 镜像与本地 Ollama Qwen 链，不额外引入这些 upstream 依赖。

### 9.2 当前可直接使用的命令

单 PDF 人工 review bundle：

```bash
docker compose exec -T backend sh -lc '
  PYTHONPATH=/app python scripts/export_local_structured_pdf_review.py \
    --pdf /app/tmp/local-bench/pdfs/01030000000001.pdf \
    --pipeline-mode hybrid \
    --hybrid-mode auto \
    --page-limit 1
'
```

这会在 `backend/tmp/manual_review/<pdf-stem>/` 下输出：

- `parsed.md`
- `document.json`
- `trace.json`（仅 hybrid）
- `summary.json`
- `page-0001.png`

跑 suite / benchmark 导出：

```bash
docker compose exec -T backend sh -lc '
  PYTHONPATH=/app python scripts/run_local_structured_pdf_eval_suites.py \
    --pipeline-mode hybrid \
    --hybrid-mode auto \
    --write-trace
'
```

## 10. 配套文档

规则通用性与过拟合风险审计：

- [LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md)

外部验证策略：

- [LOCAL_STRUCTURED_PDF_EXTERNAL_EVAL_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/LOCAL_STRUCTURED_PDF_EXTERNAL_EVAL_ZH.md)

## 11. 当前结论

本地模式已经从“实验性骨架”进入“可正式维护的独立解析器”阶段。

当前判断是：

1. 本地模式已经具备独立 benchmark 能力，并且主指标已基本超过 Java 本地基线。
2. 它已经足够作为后续 hybrid 的主干。
3. 后续工作重点不应再是泛化地继续堆表格规则，而应转向：
   - `NID`
   - page triage
   - region ordering
   - display / formula / visual page

这也是本地模式下一阶段最值得投入的方向。
