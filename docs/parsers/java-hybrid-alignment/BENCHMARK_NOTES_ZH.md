# 20 文档子集 Gate 记录

这份记录只覆盖 20 文档子集 gate 的已知结果，不代表 Java hybrid 官方全量结果，也不代表最终线上结论。
这里的 20 文档子集 gate 指的是本专题回收的 manual_review 结果，不是全量 benchmark。

## 1. 指标结果

当前 20 文档 gate 的对比结果如下：

- deterministic: `overall 0.6116 / nid 0.7636 / teds 0.4490 / mhs 0.5225 / 0.6673s-doc`
  - 来源：[/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_det_20260327/balanced/bm20_hybrid/suite_summary.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_det_20260327/balanced/bm20_hybrid/suite_summary.json)
- hybrid: `overall 0.6358 / nid 0.7956 / teds 0.4490 / mhs 0.5407 / 8.5533s-doc`
  - 来源：[/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_hybrid_20260327_141735/balanced/bm20_hybrid_20260327/prediction/local-structured-pdf/evaluation.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_hybrid_20260327_141735/balanced/bm20_hybrid_20260327/prediction/local-structured-pdf/evaluation.json)

从结果看，hybrid 在 `overall / nid / mhs` 上有提升，`teds` 持平，但代价是单文档耗时明显上升。

## 2. 当前收益页型

当前 20 文档 gate 分析里看起来更有收益的页型主要是：

- 真视觉页 `01030000000141`
- 复杂阅读顺序页 `01030000000121`
- 复杂阅读顺序页 `01030000000172`

这些页更能体现当前 20 文档 gate 里的 hybrid 收益特征；是否能推广到更大集合，当前没有足够文件支撑。

## 3. 当前回退页型

当前 20 文档 gate 分析里看起来更容易回退的页型是：

- 轻视觉页 `01030000000107`

这类页说明 triage 还存在误路由；在当前 gate 里，hybrid 不一定能把轻视觉页的成本转成收益。

## 4. 表格页观察

当前 20 文档 gate 分析里，强表格页基本是中性结果，主要依赖 failed-page fallback 把 `TEDS` 保住。

这里的含义是：

- hybrid 还没有把强表格页稳定转成明显收益。
- 当前收益主要集中在视觉页和阅读顺序复杂页。

## 5. 解释边界

这些结果只来自当前 20 文档 gate，不能外推成 Java hybrid 全量官方结果。

它的用途更接近：

- 判断当前策略是否值得继续推进
- 识别哪些页型是收益点
- 识别哪些页型仍然是回退点

## 6. 可对照的仓库路径

以下路径是本专题的结果对照材料：

- [docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/parsers/LOCAL_STRUCTURED_PDF_MODE_ZH.md)
- [docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md](/mnt/d/codefield/agent-platform/research-assistant/docs/LOCAL_STRUCTURED_PDF_RULE_AUDIT_ZH.md)
- [deterministic 20 文档 suite_summary.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_det_20260327/balanced/bm20_hybrid/suite_summary.json)
- [hybrid 20 文档 evaluation.json](/mnt/d/codefield/agent-platform/research-assistant/backend/tmp/manual_review/bm20_hybrid_20260327_141735/balanced/bm20_hybrid_20260327/prediction/local-structured-pdf/evaluation.json)
