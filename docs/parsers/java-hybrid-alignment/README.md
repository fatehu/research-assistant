# Java Hybrid 对齐研究记录

这是一组**研究/对齐记录**，不是产品最终设计定稿。

目标很明确：把我们已经确认的 Java hybrid 思路、以及 Python 当前实现与 Java 的偏差，按工程文档方式固化下来，方便后续做实现对齐、回归核对和评审沟通。

## 这个专题记录什么

1. Java hybrid 的总体原则和职责边界。
2. Java hybrid 里 backend、模型、OCR、picture description 的分工。
3. Python 当前实现和 Java 之间已经确认的差异。
4. 20 文档子集 gate 的已知结果和页型收益/回退特征。

## 目录结构

- [JAVA_HYBRID_OVERVIEW_ZH.md](./JAVA_HYBRID_OVERVIEW_ZH.md): Java hybrid 的总原则、链路和组件分工。
- [PYTHON_VS_JAVA_GAPS_ZH.md](./PYTHON_VS_JAVA_GAPS_ZH.md): 已确认的 Python 与 Java 偏差。
- [BENCHMARK_NOTES_ZH.md](./BENCHMARK_NOTES_ZH.md): 20 文档子集 gate 的结果和页型观察。

## 阅读顺序

建议先读 `JAVA_HYBRID_OVERVIEW_ZH.md`，再读 `PYTHON_VS_JAVA_GAPS_ZH.md`，最后看 `BENCHMARK_NOTES_ZH.md` 作为结果补充。

## 范围说明

- 这里只记录研究结论和对齐事实。
- 这里不定义最终产品方案。
- 这里不改代码，不替代实现文档，也不代表所有后续决策。

