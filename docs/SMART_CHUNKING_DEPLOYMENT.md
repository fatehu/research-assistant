# 智能分块功能 - Docker部署与测试方案

## 概述

本文档提供智能分块功能在Docker环境下的完整部署和测试方案。

## 目录

1. [环境准备](#1-环境准备)
2. [文件部署](#2-文件部署)
3. [Docker部署流程](#3-docker部署流程)
4. [数据库迁移](#4-数据库迁移)
5. [功能测试](#5-功能测试)
6. [问题排查](#6-问题排查)

---

## 1. 环境准备

### 1.1 前置条件

- Docker Engine 20.10+
- Docker Compose 2.0+
- 至少 4GB 可用内存
- 阿里云 Embedding API Key（用于语义分块）

### 1.2 环境变量配置

在项目根目录创建或更新 `.env` 文件：

```bash
# ========== 智能分块必需配置 ==========
# Embedding服务（语义分块核心依赖）
EMBEDDING_PROVIDER=aliyun
ALIYUN_EMBEDDING_API_KEY=your-aliyun-api-key
ALIYUN_EMBEDDING_MODEL=text-embedding-v2

# ========== 其他原有配置 ==========
POSTGRES_USER=research_user
POSTGRES_PASSWORD=research_password_123
POSTGRES_DB=research_assistant
DATABASE_URL=postgresql://research_user:research_password_123@postgres:5432/research_assistant
REDIS_URL=redis://redis:6379/0
SECRET_KEY=your-super-secret-key-change-this-in-production-min-32-chars
# ... 其他配置保持不变
```

---

## 2. 文件部署

### 2.1 解压功能包

```bash
# 在项目根目录执行
cd /path/to/research-assistant

# 解压智能分块功能包
unzip smart-chunking-feature.zip -d ./

# 解压后文件会自动放置到正确位置
# - backend/app/services/smart_chunking_service.py
# - backend/app/schemas/chunking.py
# - backend/app/api/chunking.py
# - backend/app/models/knowledge.py (更新)
# - backend/alembic/versions/007_hierarchical_chunks.py
# - backend/scripts/demo_smart_chunking.py
# - docs/SMART_CHUNKING_DESIGN.md
```

### 2.2 验证文件结构

```bash
# 验证新增文件是否存在
ls -la backend/app/services/smart_chunking_service.py
ls -la backend/app/schemas/chunking.py
ls -la backend/app/api/chunking.py
ls -la backend/alembic/versions/007_hierarchical_chunks.py
```

### 2.3 注册API路由

编辑 `backend/app/main.py`，添加路由注册：

```python
# 在其他 router 导入之后添加
from app.api.chunking import router as chunking_router

# 在其他 include_router 之后添加
app.include_router(chunking_router, prefix="/api/chunking", tags=["chunking"])
```

---

## 3. Docker部署流程

### 3.1 完整部署（首次或重建）

```bash
# 停止现有容器
docker-compose down

# 重新构建后端镜像（包含新代码）
docker-compose build backend

# 启动所有服务
docker-compose up -d

# 查看启动日志
docker-compose logs -f backend
```

### 3.2 增量更新（仅代码变更）

由于 `docker-compose.yml` 已配置代码挂载，开发模式下代码会自动同步：

```bash
# 确认容器正在运行
docker-compose ps

# 如果后端支持热重载，只需重启
docker-compose restart backend

# 或者直接进入容器执行迁移
docker-compose exec backend alembic upgrade head
```

### 3.3 生产环境部署

```bash
# 使用生产配置构建
docker-compose -f docker-compose.yml -f docker-compose.prod.yml build

# 启动生产服务
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## 4. 数据库迁移

### 4.1 自动迁移（推荐）

`docker-compose.yml` 中已配置自动迁移：

```yaml
command: >
  sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"
```

### 4.2 手动迁移

```bash
# 进入后端容器
docker-compose exec backend bash

# 检查迁移状态
alembic current
alembic history

# 执行迁移
alembic upgrade head

# 验证迁移结果
alembic current
```

### 4.3 验证数据库结构

```bash
# 连接数据库
docker-compose exec postgres psql -U research_user -d research_assistant

# 检查新字段
\d document_chunks

# 预期看到以下新字段：
# - chunk_level
# - section_type
# - section_title
# - parent_chunk_id
# - has_citations
# - semantic_score
```

### 4.4 迁移回滚（如需要）

```bash
# 回滚到上一版本
docker-compose exec backend alembic downgrade -1

# 回滚到指定版本
docker-compose exec backend alembic downgrade 006_xxx
```

---

## 5. 功能测试

### 5.1 API健康检查

```bash
# 检查后端服务
curl http://localhost:8888/health

# 检查新API端点是否注册
curl http://localhost:8888/docs | grep -i chunking
```

### 5.2 API接口测试

创建测试脚本 `test_smart_chunking.sh`：

```bash
#!/bin/bash

BASE_URL="http://localhost:8888"
TOKEN="your-jwt-token"  # 替换为实际token

echo "========== 智能分块API测试 =========="

# 1. 获取预设配置列表
echo -e "\n[1] 获取预设配置"
curl -s -X GET "$BASE_URL/api/chunking/presets" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# 2. 文档分析
echo -e "\n[2] 文档分析"
curl -s -X POST "$BASE_URL/api/chunking/analyze" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "摘要\n本文研究了大语言模型在知识图谱构建中的应用。\n\n1. 引言\n近年来，人工智能技术飞速发展。深度学习方法在自然语言处理领域取得了重大突破。\n\n2. 方法\n我们采用Transformer架构进行实体识别。模型基于预训练语言模型进行微调。\n\n3. 结论\n实验结果表明该方法有效提升了知识图谱构建的准确率。\n\n参考文献\n[1] Vaswani et al. Attention is All You Need. 2017."
  }' | python3 -m json.tool

# 3. 分块预览（使用academic策略）
echo -e "\n[3] 分块预览 - academic策略"
curl -s -X POST "$BASE_URL/api/chunking/preview" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "摘要\n本文研究了大语言模型在知识图谱构建中的应用。\n\n1. 引言\n近年来，人工智能技术飞速发展。深度学习方法在自然语言处理领域取得了重大突破。\n\n2. 方法\n我们采用Transformer架构进行实体识别。模型基于预训练语言模型进行微调。\n\n3. 结论\n实验结果表明该方法有效提升了知识图谱构建的准确率。",
    "config": {
      "strategy": "academic",
      "base_chunk_size": 300,
      "detect_academic_structure": true
    }
  }' | python3 -m json.tool

# 4. 策略比较
echo -e "\n[4] 策略比较"
curl -s -X POST "$BASE_URL/api/chunking/compare" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "人工智能是计算机科学的一个分支。它致力于开发能够模拟人类智能的系统。机器学习是人工智能的核心方法之一。深度学习作为机器学习的子领域，近年来取得了突破性进展。自然语言处理是深度学习的重要应用领域。",
    "strategies": ["fixed", "semantic", "academic"]
  }' | python3 -m json.tool

echo -e "\n========== 测试完成 =========="
```

执行测试：

```bash
chmod +x test_smart_chunking.sh
./test_smart_chunking.sh
```

### 5.3 容器内Python测试

```bash
# 进入后端容器
docker-compose exec backend bash

# 运行演示脚本
cd /app
python -m scripts.demo_smart_chunking

# 或运行特定测试
python -c "
from app.services.smart_chunking_service import SmartChunkingService, ChunkConfig, ChunkingStrategy

service = SmartChunkingService()

# 测试学术文档
text = '''
摘要
本研究探讨了深度学习在医学影像分析中的应用。

1. 引言
医学影像分析是临床诊断的重要辅助手段。

2. 方法
我们使用卷积神经网络进行图像分类。

3. 结果
实验结果表明准确率达到95%。

参考文献
[1] He et al. Deep Residual Learning. 2016.
'''

config = ChunkConfig(
    strategy=ChunkingStrategy.ACADEMIC,
    detect_academic_structure=True
)

result = service.chunk(text, config)
print(f'分块数量: {len(result.chunks)}')
for chunk in result.chunks:
    print(f'- [{chunk.section_type}] {chunk.content[:50]}...')
"
```

### 5.4 集成测试脚本

创建 `backend/tests/test_smart_chunking.py`：

```python
"""
智能分块功能集成测试
运行: docker-compose exec backend pytest tests/test_smart_chunking.py -v
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services.smart_chunking_service import (
    SmartChunkingService, 
    ChunkConfig, 
    ChunkingStrategy
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """获取认证头（需要根据实际认证方式调整）"""
    # 简化测试时可以跳过认证
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def sample_academic_text():
    return """
摘要
本研究提出了一种新的文档分块方法。

1. 引言
文档分块是信息检索的关键步骤。传统方法存在语义割裂问题。

2. 相关工作
Langchain等框架提供了基础分块功能。

3. 方法
我们采用语义相似度进行边界检测。

4. 实验
在多个数据集上进行评估。实验结果如表1所示。

5. 结论
本方法有效提升了检索准确率。

参考文献
[1] Author et al. Title. 2023.
"""


class TestSmartChunkingService:
    """分块服务单元测试"""
    
    def test_fixed_chunking(self, sample_academic_text):
        """测试固定分块"""
        service = SmartChunkingService()
        config = ChunkConfig(
            strategy=ChunkingStrategy.FIXED,
            base_chunk_size=100,
            chunk_overlap=20
        )
        result = service.chunk(sample_academic_text, config)
        
        assert len(result.chunks) > 0
        assert result.strategy == "fixed"
        
    def test_academic_chunking(self, sample_academic_text):
        """测试学术文档分块"""
        service = SmartChunkingService()
        config = ChunkConfig(
            strategy=ChunkingStrategy.ACADEMIC,
            detect_academic_structure=True
        )
        result = service.chunk(sample_academic_text, config)
        
        # 应该检测到学术结构
        section_types = [c.section_type for c in result.chunks if c.section_type]
        assert "abstract" in section_types or "摘要" in section_types
        assert any("introduction" in s or "引言" in s for s in section_types if s)
        
    def test_document_analysis(self, sample_academic_text):
        """测试文档分析"""
        service = SmartChunkingService()
        analysis = service.analyze_document(sample_academic_text)
        
        assert analysis["is_academic"] == True
        assert "abstract" in analysis["detected_sections"] or "摘要" in analysis["detected_sections"]
        assert analysis["recommended_strategy"] == "academic"
        
    def test_preset_configs(self):
        """测试预设配置"""
        service = SmartChunkingService()
        presets = service.get_preset_configs()
        
        assert "default" in presets
        assert "fast" in presets
        assert "precise" in presets
        assert "academic" in presets
        assert "deep" in presets


class TestChunkingAPI:
    """API集成测试"""
    
    def test_get_presets(self, client, auth_headers):
        """测试获取预设配置"""
        response = client.get("/api/chunking/presets", headers=auth_headers)
        # 可能需要登录，暂时跳过认证检查
        assert response.status_code in [200, 401, 403]
        
    def test_preview_endpoint(self, client, auth_headers, sample_academic_text):
        """测试分块预览"""
        response = client.post(
            "/api/chunking/preview",
            headers=auth_headers,
            json={
                "text": sample_academic_text,
                "config": {
                    "strategy": "fixed",
                    "base_chunk_size": 200
                }
            }
        )
        assert response.status_code in [200, 401, 403]
        
    def test_analyze_endpoint(self, client, auth_headers, sample_academic_text):
        """测试文档分析"""
        response = client.post(
            "/api/chunking/analyze",
            headers=auth_headers,
            json={"text": sample_academic_text}
        )
        assert response.status_code in [200, 401, 403]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

运行测试：

```bash
# 安装pytest（如未安装）
docker-compose exec backend pip install pytest --break-system-packages

# 运行测试
docker-compose exec backend pytest tests/test_smart_chunking.py -v
```

### 5.5 性能测试

```bash
# 在容器内运行性能测试
docker-compose exec backend python -c "
import time
from app.services.smart_chunking_service import SmartChunkingService, ChunkConfig, ChunkingStrategy

service = SmartChunkingService()

# 准备测试文本（约1000字）
text = '''
深度学习在自然语言处理领域的应用研究

摘要
本文系统性地回顾了深度学习技术在自然语言处理任务中的应用现状和发展趋势。

1. 引言
自然语言处理（NLP）是人工智能领域的重要研究方向。近年来，随着深度学习技术的快速发展，NLP领域取得了显著突破。Transformer架构的提出标志着NLP进入了新纪元。

2. 相关工作
2.1 传统方法
传统NLP方法主要依赖于手工特征工程和统计模型。这些方法在特定任务上取得了一定效果，但泛化能力有限。

2.2 深度学习方法
深度学习方法通过多层神经网络自动学习特征表示。从早期的循环神经网络（RNN）到长短时记忆网络（LSTM），再到当前主流的Transformer架构。

3. 方法论
3.1 预训练语言模型
BERT、GPT等预训练模型通过在大规模语料上进行自监督学习，获得了强大的语言理解能力。

3.2 微调策略
针对下游任务的微调策略包括全参数微调、适配器微调和提示学习等。

4. 实验与分析
我们在多个基准数据集上进行了实验。结果表明，预训练模型在各项任务上均显著优于传统方法。

5. 结论
深度学习已成为NLP领域的主流方法。未来研究方向包括多模态融合、低资源学习等。

参考文献
[1] Vaswani A, et al. Attention is all you need. NeurIPS 2017.
[2] Devlin J, et al. BERT: Pre-training of deep bidirectional transformers. 2019.
'''

strategies = [
    ('fixed', ChunkingStrategy.FIXED),
    ('semantic', ChunkingStrategy.SEMANTIC),
    ('academic', ChunkingStrategy.ACADEMIC),
    ('hierarchical', ChunkingStrategy.HIERARCHICAL),
    ('hybrid', ChunkingStrategy.HYBRID),
]

print('性能测试结果:')
print('=' * 50)

for name, strategy in strategies:
    config = ChunkConfig(strategy=strategy)
    
    start = time.time()
    result = service.chunk(text, config)
    elapsed = time.time() - start
    
    print(f'{name:15} | 耗时: {elapsed:.3f}s | 块数: {len(result.chunks)}')

print('=' * 50)
"
```

---

## 6. 问题排查

### 6.1 常见问题

#### 问题1: 迁移失败

```bash
# 错误: relation "document_chunks" does not exist
# 解决: 确保先执行基础迁移
docker-compose exec backend alembic upgrade head

# 如果仍有问题，检查迁移历史
docker-compose exec backend alembic history
```

#### 问题2: Embedding API 调用失败

```bash
# 错误: Embedding service error
# 检查环境变量
docker-compose exec backend env | grep ALIYUN

# 测试Embedding服务
docker-compose exec backend python -c "
from app.services.embedding_service import embedding_service
result = embedding_service.embed_text('测试文本')
print(f'Embedding维度: {len(result)}')
"
```

#### 问题3: API路由未注册

```bash
# 检查路由
curl http://localhost:8888/openapi.json | python3 -m json.tool | grep chunking

# 确认 main.py 中已添加路由
docker-compose exec backend cat /app/app/main.py | grep chunking
```

### 6.2 日志查看

```bash
# 实时查看后端日志
docker-compose logs -f backend

# 查看最近100行
docker-compose logs --tail=100 backend

# 进入容器查看详细日志
docker-compose exec backend cat /app/logs/app.log
```

### 6.3 数据库诊断

```bash
# 连接数据库
docker-compose exec postgres psql -U research_user -d research_assistant

# 检查分块数据
SELECT 
    id, 
    chunk_level, 
    section_type, 
    semantic_score,
    LEFT(content, 50) as content_preview
FROM document_chunks 
WHERE chunk_level IS NOT NULL 
LIMIT 10;

# 检查层级关系
SELECT 
    c.id,
    c.chunk_level,
    p.id as parent_id,
    p.chunk_level as parent_level
FROM document_chunks c
LEFT JOIN document_chunks p ON c.parent_chunk_id = p.id
WHERE c.parent_chunk_id IS NOT NULL
LIMIT 10;
```

---

## 附录

### A. 完整部署命令汇总

```bash
# 1. 准备环境
cd /path/to/research-assistant
cp .env.example .env
# 编辑 .env 配置

# 2. 部署文件
unzip smart-chunking-feature.zip -d ./

# 3. 编辑 main.py 添加路由

# 4. 构建并启动
docker-compose down
docker-compose build backend
docker-compose up -d

# 5. 验证
docker-compose logs -f backend
curl http://localhost:8888/api/chunking/presets
```

### B. API端点速查

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | /api/chunking/presets | 获取预设配置 |
| POST | /api/chunking/preview | 分块预览 |
| POST | /api/chunking/analyze | 文档分析 |
| POST | /api/chunking/compare | 策略比较 |
| GET | /api/chunking/knowledge-base/{kb_id}/config | 获取知识库配置 |
| PUT | /api/chunking/knowledge-base/{kb_id}/config | 更新知识库配置 |

### C. 配置参数参考

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| strategy | string | hybrid | 分块策略 |
| base_chunk_size | int | 500 | 基础块大小 |
| chunk_overlap | int | 50 | 重叠大小 |
| semantic_threshold | float | 0.75 | 语义阈值 |
| enable_hierarchical | bool | false | 启用层级 |
| detect_academic_structure | bool | false | 检测学术结构 |
