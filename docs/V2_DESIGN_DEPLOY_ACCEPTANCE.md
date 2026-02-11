# 智能分块系统 V2 补丁 — 设计 · 部署 · 验收指南

> 文档版本: 1.0  
> 适用范围: `backend/app/services/smart_chunking*`, `backend/app/schemas/chunking.py`, `backend/app/api/chunking.py`

---

## 目录

1. [补丁概览](#1-补丁概览)
2. [设计文档](#2-设计文档)
3. [文件清单与变更说明](#3-文件清单与变更说明)
4. [部署指南](#4-部署指南)
5. [验收测试指南](#5-验收测试指南)
6. [回滚方案](#6-回滚方案)
7. [已知局限与后续建议](#7-已知局限与后续建议)

---

## 1. 补丁概览

### 解决了什么问题

| # | 问题 | 严重度 | 修复方式 |
|---|------|--------|---------|
| 1 | 语义边界检测算法缺陷：滑动窗口 80% 重叠平滑掉真实语义跳变信号 | 严重 | 重写为相邻句子余弦距离 + 百分位断点检测 |
| 2 | 引用上下文保护导致不可控重叠：事后扩展使几乎所有 chunk 膨胀 | 严重 | 改为分句阶段合并引用句，精准无副作用 |
| 3 | 全局单例 `SmartChunkingService` 在 FastAPI 并发下状态互踩 | 严重 | 改为工厂函数，每次请求独立实例 |
| 4 | `chunk_by_semantics` 用空格拼接句子，丢失换行/段落格式 | 中等 | 直接从原文按位置截取 |
| 5 | 1732 行单文件，7 个 class 混在一起，违反单一职责 | 中等 | 拆分为 7 模块的 Python 包 |

### 没有解决的问题（诚实声明）

- `base_chunk_size` 仍以字符计数而非 token 计数（中英文粒度差 4-5 倍）
- 学术结构检测仍基于正则匹配（PDF 提取的纯文本效果有限）
- 无端到端 RAG 检索质量评估（Precision@K / Recall@K 未做）
- 前端分块配置页的「语义阈值」滑块暂时无实际效果（详见 §3 说明）

---

## 2. 设计文档

### 2.1 新架构概览

```
app/services/
├── smart_chunking_service.py          ← 21 行 shim（向后兼容导出层）
└── smart_chunking/                    ← 新建 Python 包
    ├── __init__.py          ( 57 行)  公共 API 导出
    ├── types.py             (136 行)  枚举、数据类、异常 — 纯数据，零依赖
    ├── academic_detector.py (109 行)  学术结构检测 — 纯正则，零依赖
    ├── text_preprocessor.py (190 行)  分句 + OCR 降噪 — 依赖 types
    ├── semantic_chunker.py  (208 行)  V2 语义边界检测 — 依赖 types, academic_detector, numpy
    ├── hierarchical_chunker.py (381 行) 层级分块 — 依赖 types, academic_detector
    └── service.py           (511 行)  主服务编排 — 依赖以上所有 + embedding_service
```

**依赖方向**：`types.py` → `academic_detector.py` → `text_preprocessor.py` / `semantic_chunker.py` / `hierarchical_chunker.py` → `service.py`。无循环依赖。

**向后兼容层**：`smart_chunking_service.py` 只有一行实质代码 `from app.services.smart_chunking import *`，使得项目中所有 `from app.services.smart_chunking_service import X` 的代码零修改继续工作。

### 2.2 V2 语义边界检测算法

**旧算法（滑动窗口）的问题**：

```
窗口1: [S1, S2, S3, S4, S5]  →  mean_embedding_1
窗口2: [S2, S3, S4, S5, S6]  →  mean_embedding_2
                                  ↑ 80% 重叠 → 相似度差异被严重平滑
```

两个窗口共享 4/5 的句子，窗口平均 embedding 几乎相同，真正的语义跳变被淹没。阈值逻辑 `max(semantic_threshold, P25)` 又使两种策略互相否决。

**V2 算法（相邻句子余弦距离）**：

```
S1 ←→ S2: distance = 0.05   （同主题，低距离）
S2 ←→ S3: distance = 0.08
S3 ←→ S4: distance = 0.42   ← 语义跳变！高于 P95 阈值 → 此处切分
S4 ←→ S5: distance = 0.06   （新主题内部，低距离）
```

对齐业界标准做法（LlamaIndex `SemanticSplitter`、Greg Kamradt 的 Semantic Chunking 方案）：
1. 每个句子独立 embedding
2. 计算相邻对的余弦距离 `1 - cosine_similarity`
3. 取距离分布的高百分位（默认 P95）作为阈值
4. 超过阈值的位置即为语义边界

**`breakpoint_percentile` 参数**（替代旧 `semantic_threshold`）：
- 95（默认）：只有距离最大的 5% 位置被视为边界 → 切分少，块大
- 85：前 15% 距离的位置 → 切分多，块小
- 推荐范围：85–95

### 2.3 V2 引用保护策略

**旧策略**：分块完成后，对每个包含引用标记的 chunk 向前/后扩展到最近句号。

问题：科研论文几乎每段都有引用 → 几乎所有 chunk 都被扩展 → 大面积不可控重叠。

**V2 策略**：在 `split_to_sentences()` 阶段，以引用标记开头的句子与前一句合并为不可分割单元。

```python
# 原始分句结果：
["深度学习方法表现优异。", "[1] Vaswani 等提出了 Transformer。", "效果显著。"]

# V2 引用保护后：
["深度学习方法表现优异。 [1] Vaswani 等提出了 Transformer。", "效果显著。"]
```

只合并以 `[1]`、`(Author, 2020)`、`Author (2020)` 开头的句子。句末引用（如 "效果好 [1]。"）不触发合并——这是正确的，因为句末引用不会因切分而丢失上下文。

### 2.4 并发安全修复

**旧设计**：
```python
smart_chunking_service = SmartChunkingService()  # 全局单例
# chunk_document() 中修改 self._embedding_cache, self._embedding_call_count
# → FastAPI 并发请求互相覆盖
```

**V2 设计**：
```python
def create_chunking_service() -> SmartChunkingService:
    return SmartChunkingService()  # 每次创建新实例

# 全局变量改为代理（向后兼容）
class _ServiceProxy:
    def __getattr__(self, name):
        return getattr(SmartChunkingService(), name)
smart_chunking_service = _ServiceProxy()
```

API 层和便捷函数都已改为调用 `create_chunking_service()`。

---

## 3. 文件清单与变更说明

### 需要新增的文件（8 个）

| 文件路径 | 行数 | 说明 |
|---------|------|------|
| `backend/app/services/smart_chunking/__init__.py` | 57 | 包公共 API 导出 |
| `backend/app/services/smart_chunking/types.py` | 136 | 枚举、dataclass、异常 |
| `backend/app/services/smart_chunking/academic_detector.py` | 109 | 学术结构检测 |
| `backend/app/services/smart_chunking/text_preprocessor.py` | 190 | 分句 + OCR 降噪 |
| `backend/app/services/smart_chunking/semantic_chunker.py` | 208 | V2 语义分块器 |
| `backend/app/services/smart_chunking/hierarchical_chunker.py` | 381 | 层级分块 + enforce_limit |
| `backend/app/services/smart_chunking/service.py` | 511 | 主服务、工厂、预设 |
| `backend/tests/test_semantic_boundary.py` | 477 | V2 专用测试（离线可运行） |

### 需要覆盖的文件（3 个）

| 文件路径 | 变更说明 |
|---------|---------|
| `backend/app/services/smart_chunking_service.py` | 1732 行 → 21 行 shim。**整文件替换**。旧逻辑全部在新包里。 |
| `backend/app/schemas/chunking.py` | 新增 `breakpoint_percentile` 字段（50.0–99.9，默认 95.0）。`semantic_threshold` 保留但标注弃用。 |
| `backend/app/api/chunking.py` | 所有 `SmartChunkingService()` → `create_chunking_service()`；配置读写新增 `breakpoint_percentile`。 |

### 不需要修改的文件

- `backend/app/api/knowledge.py` — 导入路径不变，零修改
- `backend/app/models/` — 数据模型不变
- `backend/alembic/` — 无数据库迁移
- `backend/tests/test_smart_chunking.py` — 原有测试保留
- `backend/tests/test_smart_chunking_full.py` — 同上
- `backend/scripts/demo_smart_chunking.py` — 导入路径不变
- `frontend/` — API 向后兼容，无需改前端代码

### 不需要删除的文件

本补丁没有需要删除的文件。

### 前端注意事项

`SmartChunkingPage.tsx` 中有一个 `semantic_threshold` 的 Slider（第 541 行）。**这个滑块暂时不再影响核心语义检测算法**（新算法用 `breakpoint_percentile` 控制）。滑块不会报错，值仍会被保存到知识库元数据，但对分块结果无实际影响。

如果未来想让用户控制 V2 算法的灵敏度，需要在前端新增一个 `breakpoint_percentile` 的 Slider（范围 50–99，默认 95）。**这不在本次补丁范围内**。

---

## 4. 部署指南

### 4.1 前提条件

- 后端环境可以正常运行（Docker 或本地）
- 有 `backend/` 目录的写入权限
- 建议先备份

### 4.2 部署步骤

```bash
# ====== 第 1 步：备份 ======
cd <项目根目录>
cp backend/app/services/smart_chunking_service.py \
   backend/app/services/smart_chunking_service.py.bak
cp backend/app/schemas/chunking.py \
   backend/app/schemas/chunking.py.bak
cp backend/app/api/chunking.py \
   backend/app/api/chunking.py.bak

# ====== 第 2 步：解压补丁 ======
# 将 smart-chunking-v2-patch.zip 解压到项目根目录
unzip -o smart-chunking-v2-patch.zip -d <项目根目录>

# 解压后的目录结构会自动对齐：
#   backend/app/services/smart_chunking/       ← 新建包
#   backend/app/services/smart_chunking_service.py  ← 覆盖为 shim
#   backend/app/schemas/chunking.py            ← 覆盖
#   backend/app/api/chunking.py                ← 覆盖
#   backend/tests/test_semantic_boundary.py    ← 新增
#   docs/V2_DESIGN_DEPLOY_ACCEPTANCE.md        ← 本文档

# ====== 第 3 步：验证文件 ======
# 确认新包已就位
ls -la backend/app/services/smart_chunking/
# 应看到 7 个 .py 文件

# 确认 shim 是小文件
wc -l backend/app/services/smart_chunking_service.py
# 应为 21 行

# ====== 第 4 步：运行离线测试 ======
# Docker 环境
docker-compose exec backend pytest tests/test_semantic_boundary.py -v

# 或本地环境
cd backend && python -m pytest tests/test_semantic_boundary.py -v

# 预期：21 个测试全部通过（不需要 embedding 服务）

# ====== 第 5 步：运行原有测试（可选，需要 embedding 服务在线） ======
docker-compose exec backend pytest tests/test_smart_chunking.py -v

# ====== 第 6 步：重启后端服务 ======
docker-compose restart backend
# 或
# 如果是本地开发：重新启动 uvicorn

# ====== 第 7 步：验证 API 可用 ======
curl -s http://localhost:8000/api/chunking/presets | python -m json.tool
# 应返回 5 个预设配置
```

### 4.3 注意事项

- **不需要数据库迁移**：`breakpoint_percentile` 存储在知识库的 JSON metadata 中
- **不需要安装新依赖**：没有新增 Python 包
- **不需要修改前端**：API 向后兼容
- **已有文档的分块结果不会自动更新**：新算法只影响之后的分块操作。如果需要对旧文档应用新算法，在前端「分块配置」页面重新处理

---

## 5. 验收测试指南

### 5.1 自动化测试（21 项）

运行命令：
```bash
docker-compose exec backend pytest tests/test_semantic_boundary.py -v
```

| 测试类 | 测试项 | 验收标准 |
|--------|--------|---------|
| **TestBoundaryDetection** (7 项) | | |
| | `test_no_boundary_for_single_topic` | 同主题 4 句话，P95 阈值下不产生边界 |
| | `test_boundary_at_topic_shift` | NLP→疫苗主题跳变处检测到边界 |
| | `test_multiple_topic_shifts` | 三个主题产生 ≥2 个边界 |
| | `test_empty_and_minimal_input` | 空列表和单句不崩溃 |
| | `test_two_sentences` | 两句话正常工作（V2 最低要求 ≥2） |
| | `test_embedding_failure_returns_empty` | embedding 失败安全降级 |
| | `test_percentile_controls_sensitivity` | P50 的边界数 ≥ P99 的边界数 |
| **TestChunkBySemantics** (3 项) | | |
| | `test_chunk_preserves_original_text` | chunk 内容来自原文截取，非空格拼接 |
| | `test_chunk_no_space_join` | 中文句间无人为空格 |
| | `test_empty_input` | 空输入不崩溃 |
| **TestCitationProtection** (3 项) | | |
| | `test_citation_sentences_merged` | 以 `[1]` 开头的句子与前句合并 |
| | `test_citation_protection_disabled` | 关闭保护时引用句保持独立 |
| | `test_no_uncontrolled_overlap` | 句末引用不触发合并 |
| **TestConcurrencySafety** (3 项) | | |
| | `test_factory_creates_new_instances` | 工厂每次返回不同实例 |
| | `test_instances_have_independent_state` | 实例间缓存不互相污染 |
| | `test_proxy_backward_compat` | 全局代理可正常调用 |
| **TestConfigBackwardCompat** (3 项) | | |
| | `test_old_params_accepted` | 旧参数 `semantic_threshold`、`window_size` 可传入 |
| | `test_default_breakpoint_percentile` | 默认值为 95.0 |
| | `test_presets_have_breakpoint_percentile` | 5 个预设都有此字段 |
| **TestEndToEndWithMock** (2 项) | | |
| | `test_semantic_chunking_e2e` | 语义策略端到端，chunk 内容在原文中 |
| | `test_fixed_chunking_no_embedding` | 固定策略不调用 embedding |

**通过标准**：21 项全部 PASSED。

### 5.2 手动验收测试

#### 测试 A：API 预设列表

```bash
curl http://localhost:8000/api/chunking/presets
```

**预期**：返回 5 个预设（default / fast / precise / academic / deep），每个包含 `strategy` 和 `recommended_for` 字段。

#### 测试 B：分块预览（需要登录 token）

```bash
curl -X POST http://localhost:8000/api/chunking/preview \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "# 摘要\n本文提出了一种新方法。\n\n# 1. 引言\n深度学习近年来发展迅速。传统方法存在局限性。\n\n# 2. 方法\n我们采用了Transformer架构。注意力机制是核心。\n\n# 3. 实验\n在数据集上进行了评估。结果表明方法有效 [1]。\n\n# 4. 结论\n本文的贡献总结如下。\n\n# 参考文献\n[1] Vaswani A, et al. Attention is all you need. 2017.",
    "preset": "academic"
  }'
```

**预期**：
- `strategy` 为 `"academic"`
- `chunks` 非空
- 有 chunk 的 `metadata.section_type` 为 `"abstract"` 或 `"introduction"` 等
- 有 chunk 的 `metadata.has_citations` 为 `true`
- `stats.total_chunks` > 1

#### 测试 C：文档分析

```bash
curl -X POST http://localhost:8000/api/chunking/analyze \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"text": "# 摘要\n...(同上)..."}'
```

**预期**：
- `is_academic` 为 `true`
- `recommended_strategy` 为 `"academic"`
- `detected_sections` 非空
- `document_stats` 包含 `total_chars`、`total_sentences` 等

#### 测试 D：向后兼容 — 旧参数不报错

```bash
curl -X POST http://localhost:8000/api/chunking/preview \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "测试文本。这是一段简单的内容。用于验证向后兼容性。",
    "config": {
      "strategy": "fixed",
      "base_chunk_size": 200,
      "semantic_threshold": 0.65
    }
  }'
```

**预期**：正常返回 200，不报 422 验证错误。`semantic_threshold` 被接受但不影响 fixed 策略。

#### 测试 E：新参数 breakpoint_percentile

```bash
curl -X POST http://localhost:8000/api/chunking/preview \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "...(长文本)...",
    "config": {
      "strategy": "semantic",
      "breakpoint_percentile": 85.0
    }
  }'
```

**预期**：正常返回 200，使用语义策略分块。

#### 测试 F：并发安全（压测）

```bash
# 同时发 10 个请求
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8000/api/chunking/preview \
    -H "Authorization: Bearer <TOKEN>" \
    -H "Content-Type: application/json" \
    -d '{"text": "并发测试文本 '$i'。这是一段测试。需要足够长才能触发分块。重复若干句话使其超过默认块大小。", "preset": "fast"}' &
done
wait
```

**预期**：10 个请求全部正常返回，无 500 错误、无数据串扰。

### 5.3 验收通过标准汇总

| 类别 | 标准 | 通过条件 |
|------|------|---------|
| 自动化测试 | `test_semantic_boundary.py` | 21/21 PASSED |
| 原有测试 | `test_smart_chunking.py` | 全部 PASSED（需 embedding 在线） |
| API 预设 | 测试 A | 返回 5 个预设 |
| 分块预览 | 测试 B | 学术文档正确识别 |
| 文档分析 | 测试 C | 正确推荐策略 |
| 向后兼容 | 测试 D | 旧参数不报错 |
| 新参数 | 测试 E | breakpoint_percentile 生效 |
| 并发安全 | 测试 F | 10 并发无错误 |

---

## 6. 回滚方案

如果出现问题需要回滚：

```bash
cd <项目根目录>

# 1. 恢复备份文件
cp backend/app/services/smart_chunking_service.py.bak \
   backend/app/services/smart_chunking_service.py
cp backend/app/schemas/chunking.py.bak \
   backend/app/schemas/chunking.py
cp backend/app/api/chunking.py.bak \
   backend/app/api/chunking.py

# 2. 删除新建的包（不会影响任何旧代码）
rm -rf backend/app/services/smart_chunking/

# 3. 新增的测试文件可以保留也可以删除（不影响功能）
# rm backend/tests/test_semantic_boundary.py  # 可选

# 4. 重启后端
docker-compose restart backend
```

回滚后系统恢复到补丁前状态，所有功能不受影响。

---

## 7. 已知局限与后续建议

### 关于本次补丁效果的诚实说明

**可以确定的改进**：
- 语义边界检测算法从理论上对齐了业界标准做法，信号清晰度优于旧版滑动窗口
- 引用保护不再产生不可控的 chunk 重叠
- 并发安全问题已消除
- 中文分块不再有人为插入的空格
- 代码从 1732 行巨文件变为 7 个职责清晰的模块

**无法确定的事情**：
- 分块质量的实际提升幅度（没有做 Precision@K / Recall@K 评估）
- 新算法在特定领域论文上是否一定比旧算法好（取决于 embedding 模型质量）
- `breakpoint_percentile` 的最优默认值（95.0 是保守选择，可能需要根据实际使用调优）

### 建议的后续步骤（按优先级排列）

1. **前端适配**：在 SmartChunkingPage 中新增 `breakpoint_percentile` 滑块（范围 50–99），替代当前失效的 `semantic_threshold` 滑块
2. **端到端 RAG 评估**：用标注的查询-文档对评估不同 `breakpoint_percentile` 值下的检索质量
3. **字符→token 计数**：引入 tokenizer 使 `base_chunk_size` 以 token 为单位
4. **PDF 解析升级**：集成 layout-aware 解析器（如 Docling / Marker）
5. **Contextual Retrieval**：每个 chunk 前缀文档级摘要，提升检索时的上下文理解
