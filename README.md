# AI 科研助手平台

基于 ReAct Agent 的综合科研助手平台，支持多厂商 LLM，提供智能对话、知识库管理、文献检索等功能。

## 🚀 功能特性

### 阶段 1（已完成）
- ✅ **用户认证系统** - JWT 认证，注册/登录/退出
- ✅ **Dashboard 工作台** - 快速输入、统计概览、最近对话
- ✅ **AI 对话聊天** - 流式响应、ReAct 思考过程展示
- ✅ **多厂商 LLM 支持** - DeepSeek（默认）、OpenAI、阿里云通义、Ollama
- ✅ **暗色主题 UI** - 玻璃态效果、流畅动画

### 阶段 2（已完成）
- ✅ **向量知识库** - 创建、管理多个知识库
- ✅ **文档上传处理** - 支持 PDF、TXT、Markdown、HTML
- ✅ **智能分片** - 自动文本分割，保持语义完整性
- ✅ **向量存储** - pgvector 高效向量存储与检索
- ✅ **语义搜索** - 基于 HNSW 索引的快速相似度搜索
- ✅ **阿里云 Embedding** - text-embedding-v2 模型（1536维）

### ReAct Agent 框架（已完成）
- ✅ **ReAct 推理框架** - Reasoning + Acting 循环
- ✅ **工具调用系统** - 自动选择和执行工具
- ✅ **思考过程可视化** - 展示 AI 的完整推理链
- ✅ **推理过程持久化** - 保存完整的 ReAct 步骤到数据库
- ✅ **多轮迭代显示** - 前端展示完整的多轮推理过程
- ✅ **精美 UI 设计** - 卡片式消息、渐变边框、时间线展示
- ✅ **多工具支持**:
  - 📚 **知识库搜索** - 检索用户上传的文档
  - 🌐 **网络搜索** - Serper API (Google搜索) + DuckDuckGo 备用
  - 🧮 **计算器** - 数学计算（三角函数、对数等）
  - 📅 **日期时间** - 获取当前时间
  - 📊 **文本分析** - 字数统计、关键词提取
  - 🔄 **单位转换** - 长度、重量、温度等

## 🤖 ReAct Agent 工作原理

ReAct (Reasoning + Acting) 是一种让 AI 能够进行推理和使用工具的框架：

```
用户问题 → 思考(Thought) → 行动(Action) → 观察(Observation) → ... → 最终回答(Answer)
```

### 示例流程

**用户**: "帮我搜索知识库中关于 Transformer 的内容，然后计算 sin(45°)"

**Agent 执行过程**:

1. **Thought**: 用户需要两件事：搜索知识库和数学计算。先搜索知识库。

2. **Action**: `{"tool": "knowledge_search", "input": {"query": "Transformer"}}`

3. **Observation**: 找到 3 条相关结果...

4. **Thought**: 知识库搜索完成，现在进行计算。

5. **Action**: `{"tool": "calculator", "input": {"expression": "sin(radians(45))"}}`

6. **Observation**: 计算结果: 0.7071067812

7. **Answer**: 
   - 知识库中找到关于 Transformer 的内容...
   - sin(45°) = 0.707

## 🛠️ 技术栈

### 后端
- **框架**: FastAPI + SQLAlchemy + Alembic
- **数据库**: PostgreSQL + pgvector + Redis
- **认证**: JWT (python-jose)
- **LLM**: OpenAI 兼容接口（多厂商）
- **Embedding**: 阿里云 text-embedding-v2

### 前端
- **框架**: React 18 + TypeScript + Vite
- **UI 库**: Ant Design 5 + Tailwind CSS
- **状态管理**: Zustand
- **动画**: Framer Motion
- **Markdown**: react-markdown + react-syntax-highlighter

### 向量数据库
- **pgvector**: PostgreSQL 向量扩展
- **索引**: HNSW (Hierarchical Navigable Small World)
- **距离函数**: 余弦距离 (Cosine Distance)

## 📦 快速开始

### 使用 Docker Compose（推荐）

1. **克隆项目并配置环境变量**
```bash
cd research-assistant
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/Mac
```

2. **编辑 .env 文件，填入你的 API Keys**
```env
# DeepSeek（默认）
DEEPSEEK_API_KEY=your-deepseek-api-key

# 或者使用其他提供商
OPENAI_API_KEY=your-openai-api-key
ALIYUN_API_KEY=your-aliyun-api-key
```

3. **启动服务**
```bash
docker-compose up --build
```

4. **访问应用**
- 前端: http://localhost:3000
- 后端 API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health

### 本地开发（不使用 Docker）

#### 后端
```bash
cd backend

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate     # Windows
# source venv/bin/activate # Linux/Mac

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
set DATABASE_URL=postgresql://user:pass@localhost:5432/research_assistant
set REDIS_URL=redis://localhost:6379/0
set DEEPSEEK_API_KEY=your-api-key
set SECRET_KEY=your-secret-key-min-32-chars

# 数据库迁移
alembic upgrade head

# 启动服务
uvicorn app.main:app --reload --port 8000
```

#### 前端
```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

## 📁 项目结构

```
research-assistant/
├── docker-compose.yml          # Docker 编排配置
├── .env.example                # 环境变量示例
├── README.md                   # 项目文档
│
├── backend/                    # 后端服务
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini            # 数据库迁移配置
│   ├── alembic/               # 迁移脚本
│   │   └── versions/
│   │       └── 001_initial.py
│   └── app/
│       ├── main.py            # 应用入口
│       ├── config.py          # 配置管理
│       ├── core/
│       │   ├── database.py    # 数据库连接
│       │   └── security.py    # 认证安全
│       ├── models/
│       │   ├── user.py        # 用户模型
│       │   └── conversation.py # 对话模型
│       ├── schemas/           # Pydantic 模式
│       │   ├── user.py
│       │   └── chat.py
│       ├── api/               # API 路由
│       │   ├── health.py
│       │   ├── auth.py
│       │   ├── users.py
│       │   └── chat.py
│       └── services/
│           └── llm_service.py # LLM 服务
│
└── frontend/                   # 前端应用
    ├── Dockerfile
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.js
    └── src/
        ├── main.tsx
        ├── App.tsx            # 路由配置
        ├── index.css          # 全局样式
        ├── components/
        │   └── layout/
        │       └── MainLayout.tsx
        ├── pages/
        │   ├── auth/
        │   │   ├── LoginPage.tsx
        │   │   └── RegisterPage.tsx
        │   ├── dashboard/
        │   │   └── DashboardPage.tsx
        │   └── chat/
        │       └── ChatPage.tsx
        ├── stores/
        │   ├── authStore.ts   # 认证状态
        │   └── chatStore.ts   # 聊天状态
        └── services/
            └── api.ts         # API 服务
```

## 🔌 API 接口

### 认证
| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/auth/register` | 用户注册 |
| POST | `/api/auth/login` | 用户登录 |
| GET | `/api/auth/me` | 获取当前用户 |
| POST | `/api/auth/logout` | 退出登录 |

### 用户
| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/users/profile` | 获取用户资料 |
| PUT | `/api/users/profile` | 更新用户资料 |
| GET | `/api/users/llm-providers` | 获取可用 LLM 列表 |

### 聊天
| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/chat/conversations` | 获取对话列表 |
| POST | `/api/chat/conversations` | 创建新对话 |
| GET | `/api/chat/conversations/{id}` | 获取对话详情 |
| DELETE | `/api/chat/conversations/{id}` | 删除对话 |
| POST | `/api/chat/send` | 发送消息（支持 SSE 流式 + Agent 工具） |

### 知识库
| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/knowledge/knowledge-bases` | 获取知识库列表 |
| POST | `/api/knowledge/knowledge-bases` | 创建知识库 |
| GET | `/api/knowledge/knowledge-bases/{id}` | 获取知识库详情 |
| PUT | `/api/knowledge/knowledge-bases/{id}` | 更新知识库 |
| DELETE | `/api/knowledge/knowledge-bases/{id}` | 删除知识库 |

### 文档
| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/knowledge/knowledge-bases/{kb_id}/documents` | 获取文档列表 |
| POST | `/api/knowledge/knowledge-bases/{kb_id}/documents/upload` | 上传文档 |
| GET | `/api/knowledge/knowledge-bases/{kb_id}/documents/{doc_id}` | 获取文档详情 |
| DELETE | `/api/knowledge/knowledge-bases/{kb_id}/documents/{doc_id}` | 删除文档 |
| GET | `/api/knowledge/knowledge-bases/{kb_id}/documents/{doc_id}/status` | 处理状态 |

### 向量搜索
| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/knowledge/search` | 语义向量搜索 |

### 健康检查
| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/health` | 服务健康状态 |
| GET | `/health/llm` | LLM 连接状态 |

## 🎨 UI 特性

- **暗色主题**: 深蓝渐变背景，护眼舒适
- **玻璃态效果**: 半透明磨砂玻璃风格
- **流畅动画**: Framer Motion 过渡效果
- **响应式设计**: 适配桌面和移动端
- **ReAct 展示**: 折叠面板显示 AI 思考过程
- **流式响应**: 打字机效果实时显示
- **代码高亮**: 支持多种编程语言语法高亮

## 🔧 配置说明

### LLM 提供商配置

在 `.env` 文件中配置你的 LLM 提供商：

```env
# 默认提供商
DEFAULT_LLM_PROVIDER=deepseek

# DeepSeek（推荐，性价比高）
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# OpenAI
OPENAI_API_KEY=sk-xxx
OPENAI_MODEL=gpt-4o

# 阿里云通义
ALIYUN_API_KEY=sk-xxx
ALIYUN_MODEL=qwen-plus
```

### Embedding 配置（阿里云 text-embedding-v2）

```env
# Embedding 服务
EMBEDDING_PROVIDER=aliyun
ALIYUN_EMBEDDING_API_KEY=your-api-key
ALIYUN_EMBEDDING_MODEL=text-embedding-v2
```

**text-embedding-v2 参数：**
- 向量维度：1536
- 最大输入：2048 tokens
- 支持语言：中文、英文
- [API 文档](https://help.aliyun.com/zh/dashscope/developer-reference/text-embedding-api-details)

## 📝 开发计划

- [x] **阶段 1**: 基础框架 + 用户认证 + Dashboard + 基本 Agent 聊天
- [x] **阶段 2**: 向量知识库模块 (pgvector)
- [ ] **阶段 3**: 文献管理模块
- [ ] **阶段 4**: 论文编写助手
- [ ] **阶段 5**: 代码实验室
- [ ] **阶段 6**: 科研资讯 & 热点追踪
- [ ] **阶段 7**: 高级上下文管理 + 端侧大模型

## 🐛 常见问题

### Docker 启动失败
1. 确保 Docker Desktop 已启动
2. 检查端口 3000、8000、5432、6379 是否被占用
3. 尝试 `docker-compose down -v` 后重新启动

### LLM 调用失败
1. 检查 API Key 是否正确配置
2. 检查网络连接（部分 API 可能需要代理）
3. 查看后端日志 `docker-compose logs backend`

### 数据库迁移问题
```bash
# 重置数据库
docker-compose down -v
docker-compose up --build
```

## 📄 License

MIT License
