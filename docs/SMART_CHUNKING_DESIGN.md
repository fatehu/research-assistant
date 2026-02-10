# 智能分块策略设计文档

## 📋 概述

本文档描述了为科研助手平台定制开发的智能分块策略，支持语义分块和层级分块，并为用户提供灵活的自定义能力。

---

## 🎯 设计目标

1. **语义分块** - 使用嵌入相似度检测语义边界，在语义变化处切分
2. **层级分块** - 创建多层级的分块表示，支持不同粒度的检索
3. **学术文档优化** - 识别论文结构（摘要、方法、结论等）
4. **用户自主性** - 提供预设配置和自定义选项

---

## 🏗️ 架构设计

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                   SmartChunkingService                       │
│                      (统一入口)                              │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Semantic   │  │Hierarchical │  │   Academic          │  │
│  │  Chunker    │  │  Chunker    │  │   Structure         │  │
│  │             │  │             │  │   Detector          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│                    Embedding Service                         │
│                  (阿里云 text-embedding-v2)                  │
└─────────────────────────────────────────────────────────────┘
```

### 文件结构

```
backend/app/
├── services/
│   ├── smart_chunking_service.py   # 核心分块服务
│   ├── document_service.py         # 文档处理（已有）
│   └── embedding_service.py        # 嵌入服务（已有）
├── api/
│   └── chunking.py                 # 分块 API 路由
├── schemas/
│   └── chunking.py                 # Pydantic schemas
├── models/
│   └── knowledge.py                # 数据模型（已更新）
└── alembic/versions/
    └── 007_hierarchical_chunks.py  # 数据库迁移
```

---

## 📊 分块策略

### 1. 固定分块 (Fixed)

最简单的分块方式，按固定大小切分。

```python
config = ChunkConfig(
    strategy=ChunkingStrategy.FIXED,
    base_chunk_size=500,
    chunk_overlap=50
)
```

**适用场景**: 大批量文档快速处理

### 2. 语义分块 (Semantic)

使用嵌入向量检测语义边界。

**算法流程**:
1. 将文本分割为句子
2. 为每个句子生成嵌入向量
3. 使用滑动窗口计算相邻句子组的相似度
4. 在相似度骤降处（低于阈值）切分

```python
config = ChunkConfig(
    strategy=ChunkingStrategy.SEMANTIC,
    semantic_threshold=0.75,    # 相似度阈值
    window_size=5,              # 滑动窗口大小
    min_semantic_chunk=100,     # 最小块大小
    max_semantic_chunk=1500     # 最大块大小
)
```

**适用场景**: 需要精确检索的重要文档

### 3. 层级分块 (Hierarchical)

创建多层级结构，支持不同粒度的检索。

```
Document Level (文档级)
    └── Section Level (章节级)
            └── Paragraph Level (段落级)
```

```python
config = ChunkConfig(
    strategy=ChunkingStrategy.HIERARCHICAL,
    enable_hierarchical=True,
    hierarchy_levels=[
        ChunkLevel.PARAGRAPH,
        ChunkLevel.SECTION,
        ChunkLevel.DOCUMENT
    ]
)
```

**适用场景**: 长文档、书籍、需要多层级索引

### 4. 学术论文分块 (Academic)

专门针对学术论文优化，识别论文结构。

**识别的章节类型**:
- `abstract` - 摘要
- `introduction` - 引言
- `related_work` - 相关工作
- `methodology` - 方法
- `experiment` - 实验
- `results` - 结果
- `discussion` - 讨论
- `conclusion` - 结论
- `references` - 参考文献

```python
config = ChunkConfig(
    strategy=ChunkingStrategy.ACADEMIC,
    detect_academic_structure=True,
    preserve_citations=True
)
```

**适用场景**: 学术论文、研究报告、技术文档

### 5. 混合策略 (Hybrid) - 推荐

自动检测文档类型并选择最佳策略。

```python
config = ChunkConfig(
    strategy=ChunkingStrategy.HYBRID
    # 会自动检测并选择最佳策略
)
```

**决策逻辑**:
1. 检测是否为学术文档 → 使用 Academic 策略
2. 检测到多个章节 → 使用 Hierarchical 策略
3. 长文档 → 使用 Semantic 策略
4. 其他 → 使用 Semantic + Hierarchical 组合

---

## 🔧 API 接口

### 预设配置

```http
GET /api/chunking/presets
```

返回可用的预设配置列表。

| 预设 | 策略 | 适用场景 |
|------|------|---------|
| `default` | hybrid | 通用文档 |
| `fast` | fixed | 大批量处理 |
| `precise` | semantic | 重要文档 |
| `academic` | academic | 学术论文 |
| `deep` | hierarchical | 长文档/书籍 |

### 分块预览

```http
POST /api/chunking/preview
Content-Type: application/json

{
    "text": "要分块的文本...",
    "preset": "academic",
    // 或者自定义配置
    "config": {
        "strategy": "semantic",
        "semantic_threshold": 0.7,
        "enable_hierarchical": true
    }
}
```

### 文档分析

```http
POST /api/chunking/analyze
```

分析文档结构，推荐最佳分块策略。

**返回示例**:
```json
{
    "is_academic": true,
    "detected_sections": ["abstract", "introduction", "methodology"],
    "has_citations": true,
    "recommended_strategy": "academic",
    "recommended_reason": "检测到学术文档结构"
}
```

### 策略比较

```http
POST /api/chunking/compare?strategies=fast&strategies=semantic&strategies=hybrid
```

比较不同策略的分块效果。

### 知识库配置

```http
# 获取配置
GET /api/chunking/knowledge-base/{kb_id}/config

# 更新配置
PUT /api/chunking/knowledge-base/{kb_id}/config

# 应用预设
POST /api/chunking/knowledge-base/{kb_id}/apply-preset
{
    "preset": "academic"
}
```

---

## 💾 数据模型

### DocumentChunk 新增字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `chunk_level` | String(20) | 分块层级 (paragraph/section/document) |
| `section_type` | String(50) | 学术章节类型 |
| `section_title` | String(500) | 章节标题 |
| `parent_chunk_id` | Integer | 父块ID（层级关系） |
| `has_citations` | Boolean | 是否包含引用 |
| `semantic_score` | Float | 语义连贯性得分 |

### KnowledgeBase 分块配置

存储在 `metadata` 字段中：

```json
{
    "chunking_config": {
        "strategy": "hybrid",
        "semantic_threshold": 0.75,
        "enable_hierarchical": true,
        "hierarchy_levels": ["paragraph", "section"],
        "detect_academic_structure": true,
        "preserve_citations": true
    }
}
```

---

## 📈 用户自主性

### 1. 预设选择

用户可以选择预设配置快速开始：

- **默认** - 适合大多数情况
- **快速** - 牺牲质量换取速度
- **精确** - 更好的语义边界
- **学术** - 论文专用
- **深度** - 完整层级结构

### 2. 自定义配置

高级用户可以调整每个参数：

```javascript
// 前端示例
const config = {
    strategy: "semantic",
    base_chunk_size: 600,
    chunk_overlap: 80,
    semantic_threshold: 0.7,
    min_semantic_chunk: 150,
    max_semantic_chunk: 1200,
    enable_hierarchical: true,
    hierarchy_levels: ["paragraph", "section"],
    detect_academic_structure: true,
    preserve_citations: true
};
```

### 3. 分块预览

在应用前预览分块效果：

1. 选择配置
2. 粘贴示例文本
3. 查看分块结果
4. 调整参数
5. 应用到知识库

### 4. 策略比较

并排比较不同策略的效果：

- 块数量
- 平均块大小
- 块大小分布
- 示例分块内容

---

## 🔄 集成指南

### 1. 文档上传时使用智能分块

```python
# knowledge.py 中的文档处理
from app.services.smart_chunking_service import SmartChunkingService, ChunkConfig

async def process_document(doc: Document, kb: KnowledgeBase):
    # 获取知识库的分块配置
    chunking_config = kb.chunking_config
    
    config = ChunkConfig(
        strategy=chunking_config.get("strategy", "hybrid"),
        semantic_threshold=chunking_config.get("semantic_threshold", 0.75),
        # ... 其他配置
    )
    
    # 使用智能分块
    service = SmartChunkingService()
    result = await service.chunk_document(doc.content, config)
    
    # 保存分块
    for chunk in result["chunks"]:
        db_chunk = DocumentChunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            content=chunk.content,
            chunk_level=chunk.metadata.level.value,
            section_type=chunk.metadata.section_type,
            # ...
        )
        db.add(db_chunk)
```

### 2. 注册 API 路由

```python
# main.py
from app.api.chunking import router as chunking_router

app.include_router(
    chunking_router,
    prefix="/api/chunking",
    tags=["chunking"]
)
```

### 3. 运行数据库迁移

```bash
cd backend
alembic upgrade head
```

---

## 📊 性能考虑

### 语义分块的代价

- 需要为每个句子生成嵌入向量
- 增加 API 调用次数和处理时间
- 建议对大文档使用批量处理

### 优化建议

1. **批量处理** - 使用 `embed_texts` 批量获取嵌入
2. **缓存** - 缓存已处理文档的分块结果
3. **异步处理** - 大文档使用后台任务处理
4. **预设优先** - 鼓励用户使用预设配置

### 资源估算

| 策略 | 1000字文档处理时间 | API 调用次数 |
|------|------------------|-------------|
| fixed | ~0.1s | 0 |
| semantic | ~2-3s | ~20-30 |
| hierarchical | ~3-5s | ~30-50 |
| academic | ~3-5s | ~30-50 |
| hybrid | ~2-5s | ~20-50 |

---

## 🧪 测试建议

### 单元测试

```python
# test_smart_chunking.py
import pytest
from app.services.smart_chunking_service import *

@pytest.mark.asyncio
async def test_semantic_chunking():
    service = SmartChunkingService()
    config = ChunkConfig(strategy=ChunkingStrategy.SEMANTIC)
    
    text = "这是第一段内容。这是第二段内容。"
    result = await service.chunk_document(text, config)
    
    assert len(result["chunks"]) > 0
    assert result["strategy"] == "semantic"

@pytest.mark.asyncio
async def test_academic_detection():
    service = SmartChunkingService()
    
    academic_text = """
    # Abstract
    This paper presents...
    
    # Introduction
    Recent advances in...
    """
    
    assert service._detect_academic_document(academic_text) == True
```

### 集成测试

1. 上传学术论文 PDF
2. 验证章节检测
3. 验证层级结构
4. 验证检索效果

---

## 📝 后续优化

1. **语义分块优化**
   - 引入更智能的边界检测算法
   - 支持自定义分割规则

2. **多模态支持**
   - 识别图表、公式
   - 保留图表上下文

3. **增量更新**
   - 支持文档部分更新
   - 只重新分块变化的部分

4. **质量评估**
   - 自动评估分块质量
   - 提供优化建议

---

## 📚 参考资料

- [LangChain Text Splitters](https://python.langchain.com/docs/modules/data_connection/document_transformers/)
- [Semantic Chunking Paper](https://arxiv.org/abs/2312.06648)
- [阿里云 text-embedding-v2 文档](https://help.aliyun.com/zh/dashscope/developer-reference/text-embedding-api-details)
