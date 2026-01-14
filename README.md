# AI 科研助手平台

基于 ReAct Agent 的综合科研助手平台，支持多厂商 LLM，提供智能对话、知识库管理、文献检索等功能。

## 🚀 阶段 1 功能（已完成）

- ✅ **用户认证系统** - JWT 认证，注册/登录/退出
- ✅ **Dashboard 工作台** - 快速输入、统计概览、最近对话
- ✅ **AI 对话聊天** - 流式响应、ReAct 思考过程展示
- ✅ **多厂商 LLM 支持** - DeepSeek（默认）、OpenAI、阿里云通义、Ollama
- ✅ **暗色主题 UI** - 玻璃态效果、流畅动画

## 🛠️ 技术栈

### 后端
- **框架**: FastAPI + SQLAlchemy + Alembic
- **数据库**: PostgreSQL + Redis
- **认证**: JWT (python-jose)
- **LLM**: OpenAI 兼容接口（多厂商）

### 前端
- **框架**: React 18 + TypeScript + Vite
- **UI 库**: Ant Design 5 + Tailwind CSS
- **状态管理**: Zustand
- **动画**: Framer Motion
- **Markdown**: react-markdown + react-syntax-highlighter

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
| POST | `/api/chat/send` | 发送消息（支持 SSE 流式） |

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

### Embedding 配置（阶段2使用）

```env
EMBEDDING_PROVIDER=aliyun
ALIYUN_EMBEDDING_API_KEY=your-api-key
ALIYUN_EMBEDDING_MODEL=text-embedding-v2
```

## 📝 开发计划

- [x] **阶段 1**: 基础框架 + 用户认证 + Dashboard + 基本 Agent 聊天
- [ ] **阶段 2**: 向量知识库模块
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
