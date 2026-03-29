# Python vs Java 已确认偏差

这份文档只记录已经确认的差异，不推演未验证的实现细节。

## 1. triage 更激进

Python 的 triage 比 Java 更激进，强信号组合更接近“页型分类 + 阈值组合”。

这会带来一个直接后果：Python 更容易把页推向 backend，尤其是在页级特征不够稳的时候。可核对的源码路径是 [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py) 和 [backend/app/services/local_structured_pdf/hybrid_planner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_planner.py)。

## 2. light visual page 误路由

最明显的误路由页型是 light visual page with usable native text。

典型例子是 `01030000000107` 这类页：页面有可用原生文本，但视觉信号又足以诱发 triage 走错。

当前研究结论是：Python triage 还没有把“可用 native text 的轻视觉页”稳定地区分出来。对应可核对路径同样是 [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py)。

## 3. backend 的职责边界不同

Java backend 更像文档元素 JSON 提供者；Python backend 更像页级结构候选生成器。可核对的 Python 侧实现是 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。

这更接近当前对齐判断，而不只是词面差异：

- Java backend 提供松结构元素，后续由 transformer 做内部对象映射。当前仓库里可直接核对的是 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。
- Python backend 更强调页级候选和局部修复，transformer 承担更多结构收口工作。对应可核对路径是 [backend/app/services/local_structured_pdf/ollama_page_parser.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/ollama_page_parser.py) 和 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。

## 4. Python transformer 职责更重

Python 的 transformer 不只是纯映射，还要做更多程序侧控制，包括：

- anchor / line_id 校验
- bbox 兜底
- 阅读顺序重排

Java transformer 相对更像纯映射，程序侧负担更轻。

## 5. fallback / orchestration 差异

当前研究结论是：Java orchestration 更像先双路执行再合并；Python 更像先尽量本地建好，再对部分页补 backend。需要结合 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py) 和 [backend/app/services/local_structured_pdf/ollama_page_parser.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/ollama_page_parser.py) 去核对。

对应地，fallback 设计也不一样：

- Java 是批次级 + 页级两层 fallback。当前仓库里可核对的近似支撑是 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py) 和 [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py)。
- Python 更像页级有效性门控 + 本地结果兜底。可核对路径是 [backend/app/services/local_structured_pdf/ollama_page_parser.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/ollama_page_parser.py)。

## 6. 直接影响

这些差异会导致两边的结果特征不一样；下面三条应理解为当前研究结论，而不是直接从 evaluator 原始字段抽出来的定性标签：

- Python 更容易提前把页送进 backend。
- Python 的收口逻辑更重，程序侧修复更多。
- Java 更依赖 backend 返回的松结构元素，再统一转内部对象。

## 7. 当前研究结论的对照结论

当前对齐判断可以概括成下面四句：

1. Python triage 比 Java 更激进。
2. `01030000000107` 是当前 20 文档 gate 里最值得关注的 light visual page 误路由样本，可核对路径是 [backend/tmp/manual_review/bm20_hybrid_20260327_141735/balanced/bm20_hybrid_20260327/trace/local-structured-pdf/01030000000107.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_hybrid_20260327_141735/balanced/bm20_hybrid_20260327/trace/local-structured-pdf/01030000000107.json)。
3. Java backend 更像元素 JSON 提供者，Python backend 更像结构候选生成器，可核对路径是 [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)。
4. Java orchestration / fallback 更分层，Python 更偏前置本地建模后补 backend，可核对路径是 [backend/app/services/local_structured_pdf/hybrid_pipeline.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_pipeline.py) 和 [backend/app/services/local_structured_pdf/ollama_page_parser.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/ollama_page_parser.py)。

## 8. 可对照的仓库路径

以下路径和本专题直接相关：

- [docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md)
- [docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md)
- [backend/app/services/local_structured_pdf/page_triage_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/page_triage_service.py)
- [backend/app/services/local_structured_pdf/hybrid_planner.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_planner.py)
- [backend/app/services/local_structured_pdf/hybrid_backend_transformer.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_backend_transformer.py)
- [backend/app/services/local_structured_pdf/hybrid_fusion_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/local_structured_pdf/hybrid_fusion_service.py)
