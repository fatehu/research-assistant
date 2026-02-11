# 研究助手系统 (Research Assistant)

一个为科研团队设计的智能研究助手平台，集成 AI 对话、知识库管理、文献管理、代码实验室等功能，支持多角色协作与资源共享。

## ✨ 核心功能

| 模块 | 说明 |
|------|------|
| 🤖 **AI 智能对话** | 多 LLM 支持（DeepSeek、OpenAI、Ollama）、流式响应、ReAct Agent 工具调用 |
| 📚 **知识库** | 文档向量化、语义搜索、RAG 检索、**智能分块**（层级化 / 多策略）|
| 🔬 **文献管理** | Semantic Scholar / arXiv 论文搜索、PDF 下载、收藏分类、阅读标注 |
| 💻 **代码实验室** | Jupyter 风格笔记本、Python 沙箱执行、AI Notebook Agent |
| 👥 **多角色协作** | 管理员 / 导师 / 学生三级角色、研究组、邀请系统、资源共享 |

## 🏗️ 技术栈

| 层级 | 技术 |
|------|------|
| **前端** | React 18 + TypeScript + Vite + Ant Design + Zustand + Framer Motion |
| **后端** | FastAPI + SQLAlchemy (async) + PostgreSQL + pgvector + Redis |
| **AI/ML** | sentence-transformers（本地嵌入）、多 LLM Provider 抽象层 |
| **部署** | Docker Compose 一键部署 |

## 📦 快速开始

### 前置要求

- Docker & Docker Compose
- 至少 4GB 内存（本地嵌入模型需要）

### 部署步骤

```bash
# 1. 克隆仓库
git clone <repo-url>
cd research-assistant

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，配置 LLM API Key 等

# 3. 启动服务
docker compose up -d

# 4. 访问
# 前端: http://localhost:3000
# 后端 API: http://localhost:8888
```

### 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Frontend | 3000 | React 前端 |
| Backend | 8888 | FastAPI 后端 API |
| PostgreSQL | 5432 | 数据库（含 pgvector 扩展）|
| Redis | 6379 | 缓存 & 会话管理 |

## 🔧 配置说明

### 嵌入模型

系统支持 **本地嵌入** 和 **API 嵌入** 两种方式，在 `.env` 中配置：

```env
# 嵌入提供者: local / aliyun / openai / ollama
EMBEDDING_PROVIDER=local

# 本地模型配置
LOCAL_EMBEDDING_MODEL=BAAI/bge-m3       # 推荐: 多语言SOTA
LOCAL_EMBEDDING_DEVICE=auto             # auto / cpu / cuda / mps
LOCAL_EMBEDDING_DIMENSION=0             # 0=使用模型默认维度
```

**创建知识库时可选择嵌入模型**，支持的模型包括：

| 模型 | 维度 | 类型 | 说明 |
|------|------|------|------|
| `BAAI/bge-m3` | 1024 | 本地 | 多语言 SOTA，推荐 |
| `BAAI/bge-large-zh-v1.5` | 1024 | 本地 | 中文优化 |
| `BAAI/bge-large-en-v1.5` | 1024 | 本地 | 英文优化 |
| `allenai/specter2` | 768 | 本地 | 科研论文专用 |
| `text-embedding-v2` | 1536 | 阿里云 API | DashScope |
| `text-embedding-3-small` | 1536 | OpenAI API | OpenAI |
| `nomic-embed-text` | 768 | Ollama | 本地 API |

### LLM 模型

```env
# LLM 提供者: deepseek / openai / ollama
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_key
DEEPSEEK_MODEL=deepseek-chat

# 或使用本地 Ollama
# LLM_PROVIDER=ollama
# OLLAMA_BASE_URL=http://host.docker.internal:11434
```

## 📚 知识库系统

### 智能分块 (Smart Chunking)

系统提供 **层级化智能分块**，支持多种预设策略：

| 预设 | 适用场景 | 特点 |
|------|----------|------|
| FAST | 快速处理 | 大块、少分片 |
| PRECISE | 精确检索 | 小块、多分片 |
| ACADEMIC | 学术论文 | 识别章节结构 |
| DEEP | 深度分析 | 最细粒度分块 |
| DEFAULT | 通用 | 平衡效果 |

分块层级：
- **段落级** (paragraph) — 适合精确检索
- **章节级** (section) — 适合获取完整上下文
- **文档级** (document) — 全文检索

### 向量搜索

```
POST /api/knowledge/search
{
  "query": "深度学习在NLP中的应用",
  "knowledge_base_ids": [1, 2],
  "top_k": 5,
  "chunk_level": "paragraph",
  "include_parent_context": true
}
```

## 🗂️ 项目结构

```
research-assistant/
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI 路由
│   │   │   ├── knowledge.py  # 知识库 + 嵌入模型 API
│   │   │   ├── chat.py       # AI 对话
│   │   │   ├── literature.py # 文献管理
│   │   │   ├── codelab.py    # 代码实验室
│   │   │   ├── chunking.py   # 智能分块配置
│   │   │   ├── agent.py      # ReAct Agent
│   │   │   ├── admin.py      # 管理员
│   │   │   ├── mentor.py     # 导师
│   │   │   ├── student.py    # 学生
│   │   │   └── share.py      # 资源共享
│   │   ├── models/           # SQLAlchemy 数据模型
│   │   ├── schemas/          # Pydantic 验证模型
│   │   ├── services/         # 业务逻辑
│   │   │   ├── embedding_service.py      # 嵌入服务（本地/API）
│   │   │   ├── smart_chunking_service.py # 智能分块引擎
│   │   │   └── document_service.py       # 文档处理
│   │   ├── core/             # 核心组件（数据库、认证、权限）
│   │   └── config.py         # 配置管理
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/            # 页面组件
│   │   │   ├── knowledge/    # 知识库（含模型选择）
│   │   │   ├── chat/         # AI 对话
│   │   │   ├── literature/   # 文献管理
│   │   │   ├── codelab/      # 代码实验室
│   │   │   └── ...           # 管理/导师/学生/共享
│   │   ├── stores/           # Zustand 状态管理
│   │   ├── services/api.ts   # API 客户端
│   │   └── App.tsx           # 路由配置
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## 🔌 主要 API

### 知识库

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/knowledge/embedding-models` | 获取可用嵌入模型列表 |
| POST | `/api/knowledge/knowledge-bases` | 创建知识库（可选嵌入模型）|
| POST | `/api/knowledge/knowledge-bases/{id}/documents/upload` | 上传文档 |
| POST | `/api/knowledge/search` | 向量搜索（支持层级过滤）|
| GET | `/api/knowledge/available` | 获取可用知识库（含共享）|

### AI 对话

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/chat/send` | 流式 AI 对话 |
| GET | `/api/chat/conversations` | 获取对话列表 |

### 文献管理

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/literature/search` | 搜索论文 |
| POST | `/api/literature/papers` | 保存论文 |
| POST | `/api/literature/papers/{id}/download-pdf` | 下载 PDF |

### 智能分块配置

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/chunking/presets` | 获取分块预设列表 |
| GET | `/api/chunking/knowledge-bases/{id}/config` | 获取知识库分块配置 |
| PUT | `/api/chunking/knowledge-bases/{id}/config` | 更新分块配置 |

## 🐛 故障排除

### 本地嵌入模型加载慢

首次启动需下载模型权重（~2GB），后续通过 Docker Volume `model_cache` 缓存：

```bash
# 查看模型缓存
docker volume inspect research-assistant_model_cache

# 查看后端日志确认加载状态
docker logs -f research_backend
```

### 维度不匹配

不同嵌入模型的向量维度不同，**已有知识库不支持切换模型**。如需切换，需要：
1. 删除原知识库
2. 使用新模型重新创建
3. 重新上传文档

### 容器状态检查

```bash
docker compose ps
docker compose logs backend --tail 50
docker compose logs frontend --tail 50
```

## 📝 License

MIT License

---

**最后更新**：2026-02-11
