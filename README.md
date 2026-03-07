# AI 科研助手 / Research Assistant

面向科研团队的全栈研究工作台，覆盖 AI 对话、知识库与 RAG、论文搜索与阅读、CodeLab Notebook、多角色协作，以及可选的 MCP 工具接入。

这不是单点功能项目，而是一套围绕科研真实工作流搭起来的平台：

- 找论文、收论文、下载 PDF、按页阅读
- 把论文和资料入库，做向量检索、问答和引用回溯
- 在阅读页里直接做生成式编排、证据定位、Review / Publish
- 在 CodeLab 里运行 Python、保留上下文、调用 Notebook Agent
- 让管理员、导师、学生共享资源、公告和协作入口

## 核心能力

| 模块 | 当前能力 |
|------|------|
| AI 对话 | 多 LLM Provider、流式响应、Agent 工具调用、来源引用 |
| 知识库 / RAG | 文档上传、Embedding、智能分块、混合检索、Reranker、Query Rewrite、Contextual Compression |
| 文献管理 | Semantic Scholar / arXiv 搜索、收藏、PDF 下载、分类管理、引用信息、阅读位置记忆 |
| 论文阅读工作台 | Reader Workbench V2、结构化页面解析、组件化阅读界面、证据锚点定位、Review / Publish 闭环 |
| CodeLab | Jupyter 风格 Notebook、Python 沙箱执行、变量查看、Notebook Agent |
| 多角色协作 | 管理员 / 导师 / 学生路由、共享资源、邀请、公告、统计页 |
| MCP | 可选外部 MCP Server 接入，支持工具路由、重试、熔断和前端配置 |

## 系统功能矩阵

### 用户侧功能

| 功能域 | 功能点 |
|------|------|
| AI 对话 | 流式聊天、多模型接入、Agent 工具调用、来源引用展示 |
| 知识库 | 创建知识库、上传文档、文档入库、跨库检索、共享知识库访问 |
| 智能分块 | 多预设策略、层级分块、学术文档优化、分块配置管理 |
| 文献管理 | 论文搜索、论文收藏、PDF 下载、分类整理、引用信息查看 |
| 论文阅读 | 按页阅读、结构化阅读界面、证据锚点跳转、阅读位置记忆、内联提问 |
| Review / Publish | 人工观察、局部 Patch、自动修复、发布快照 |
| CodeLab | Notebook 创建、编辑、单元运行、全量运行、变量查看、结果回显 |
| Notebook Agent | 代码建议、错误解释、数据分析辅助、上下文感知问答 |
| 协作入口 | 管理员、导师、学生分角色页面，资源共享、公告与邀请 |

### 平台能力

| 能力域 | 能力点 |
|------|------|
| 检索增强 | Hybrid Retrieval、Reranker、Query Rewrite、Contextual Compression |
| Reader 引擎 | PDF 结构提取、`page_structure_v3`、语义原子、组件化编排、SSE 流式 patch |
| 多模态辅助 | 版面理解、布局建议、证据定位增强 |
| 代码执行 | 独立 `codelab-runner`、沙箱执行、超时控制、内核生命周期管理 |
| MCP 扩展 | 外部 MCP Server 接入、工具前缀、路由策略、失败回退 |
| 状态反馈 | 长任务状态流、事件推送、前端感知处理进度 |
| 管理能力 | 用户管理、统计页、公告、邀请、权限边界 |
| 基础设施 | Docker Compose、PostgreSQL + pgvector、Redis、Alembic 迁移 |

## 当前开发重点

截至 2026-03-07，这个仓库最近一轮的主增量集中在论文阅读链路：

- `Reader Workbench V2` 已经从“PDF + 问答面板”升级成“结构化页面 + 组件编排 + 评审发布”的工作台。
- 阅读链路支持 `single_agent_v2`、语义原子、流式 UI patch，以及更稳的证据锚点定位。
- Review / Publish 工作流已经接通，支持观察、Patch、自动修复和发布快照。
- Reader 页内联 PDF、工作台可读性和操作控件在最近一轮里继续收口。

如果你是第一次进入仓库，可以先把这部分理解成：平台整体是稳定多模块结构，而目前最活跃的产品面是论文阅读工作台。

## Reader Workbench V2

Reader 页当前不是简单展示 PDF 文本，而是按页面结构做组件化阅读。实际链路大致如下：

1. 论文 PDF 下载并入库。
2. 后端优先使用 Document Mind DocStructure 提取版面结构，再标准化为 `page_structure_v3`。
3. 页面结构会被整理成语义原子、关系和导航信息。
4. `single_agent_v2` 基于页面元数据、结构块和渲染图像，输出受约束的 UI 组件树。
5. 后端执行组件白名单、锚点所有权、最小门禁和布局合同校验；失败时节点级降级，而不是整页清空。
6. 前端通过 SSE 接收 `plan_draft`、`plan_patch`、`assets`、`quality_report`、`done` 等事件，增量渲染阅读工作台。
7. 用户可以进入 Review 页面做人工观察、局部修补、自动修补，并将结果发布为正式阅读快照。

关键实现主要在：

- `backend/app/api/literature.py`
- `backend/app/services/literature_reader_compose_service.py`
- `backend/app/services/reader_single_agent_controller.py`
- `backend/app/services/render_pipeline_contract.py`
- `frontend/src/pages/literature/PaperReaderPage.tsx`
- `frontend/src/pages/literature/PaperReaderReviewPage.tsx`
- `frontend/src/pages/literature/readerComponents/`

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18, TypeScript, Vite, Ant Design, Zustand, Framer Motion |
| 后端 | FastAPI, SQLAlchemy Async, Pydantic v2, Alembic |
| 存储 | PostgreSQL + pgvector, Redis |
| AI / 检索 | OpenAI / DeepSeek / 阿里云 / Ollama，本地嵌入，混合检索，重排与压缩 |
| PDF / Reader | pypdf, pdfplumber, MarkItDown, Alibaba Cloud Document Mind |
| Notebook | Monaco Editor, 独立 `codelab-runner` 沙箱服务 |
| 部署 | Docker Compose，`mcp` profile 可选启用内部 MCP 服务 |

## 快速开始

### 前置要求

- Docker Desktop 或 Docker Engine + Docker Compose
- 至少 4 GB 可用内存；启用本地 Embedding 时建议 8 GB 以上
- Windows PowerShell 建议先执行 `chcp 65001`

### 1. 克隆并配置环境变量

```bash
git clone <repo-url>
cd research-assistant
cp .env.example .env
```

至少确认这些值：

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `SECRET_KEY`
- `CODELAB_RUNNER_TOKEN`
- 至少一组 LLM 凭证：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ALIYUN_API_KEY`

如果你要启用高保真的 Reader Workbench V2，建议额外配置：

- `PDF_LAYOUT_PARSER=document_mind`
- `READER_DOCUMENT_MIND_ENABLED=true`
- `DOCUMENT_MIND_ACCESS_KEY_ID`
- `DOCUMENT_MIND_ACCESS_KEY_SECRET`

### 2. 启动默认服务

```bash
docker compose up -d --build backend frontend
```

这条命令会连带拉起依赖服务：

- `postgres`
- `redis`
- `codelab-runner`
- `backend`
- `frontend`

### 3. 可选启用 MCP Profile

```bash
docker compose --profile mcp up -d mcp_web mcp_literature
```

### 4. 访问地址

| 服务 | 地址 |
|------|------|
| 前端 | http://localhost:3000 |
| 后端 API | http://localhost:8888 |
| FastAPI Docs | http://localhost:8888/docs |
| PostgreSQL | `localhost:5432` |
| Redis | `localhost:6379` |
| MCP Web | http://localhost:8091 |
| MCP Literature | http://localhost:8092 |

### 5. 启动后自检

```bash
docker compose ps
docker compose logs --tail 100 backend
docker compose logs --tail 100 frontend
docker compose logs --tail 100 codelab-runner
```

## 关键环境变量

这里只列最常用的一组；完整列表请看 `.env.example`。

### 基础运行

```env
APP_ENV=development
DATABASE_URL=postgresql://research_user:change_me@postgres:5432/research_assistant
REDIS_URL=redis://redis:6379/0
SECRET_KEY=change_me_to_a_random_string_at_least_32_chars_long
```

### LLM Provider

```env
DEFAULT_LLM_PROVIDER=deepseek

DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o

ALIYUN_API_KEY=
ALIYUN_MODEL=qwen-plus

OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=llama3
```

### Embedding / 检索

```env
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_MODEL=BAAI/bge-m3
ENABLE_HYBRID_RETRIEVAL=true
ENABLE_RERANKER=true
ENABLE_QUERY_REWRITE=true
ENABLE_CONTEXTUAL_COMPRESSION=true
CHUNK_QUALITY_GATE_ENABLED=false
```

### Reader Workbench V2

```env
PDF_LAYOUT_PARSER=document_mind
READER_DOCUMENT_MIND_ENABLED=true
READER_PIPELINE_MODE=single_agent_v2
READER_PIPELINE_VERSION=simplified_v2
READER_MM_ASSIST_ENABLED=true
READER_AGENT_MAX_STEPS=12
READER_AGENT_MAX_REPAIR_ROUNDS=2
READER_COMPOSE_LATENCY_BUDGET_MS=20000
READER_COMPOSE_LATENCY_BUDGET_MAX_MS=25000
```

如果没有 Document Mind 凭证，可以先降级为：

```env
READER_DOCUMENT_MIND_ENABLED=false
PDF_LAYOUT_PARSER=markitdown
```

### CodeLab / MCP

```env
CODELAB_RUNNER_ENABLED=true
CODELAB_RUNNER_URL=http://codelab-runner:8099
CODELAB_RUNNER_TOKEN=change_me_internal_runner_token
CODELAB_EXEC_TIMEOUT_HARD_SECONDS=20
CODELAB_MAX_CONCURRENCY_PER_USER=2

MCP_ENABLED=false
MCP_TOOL_PREFIX=mcp
MCP_CONFIG_PATH=mcp_servers.json
MCP_TOOL_ROUTES={}
```

## 本地开发

### 前端

```bash
cd frontend
npm install
npm run dev
```

### 后端

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 常用验证命令

```bash
cd frontend
npm run build
```

```bash
cd backend
pytest tests/test_literature_reader_api.py \
       tests/test_literature_reader_composed.py \
       tests/test_reader_single_agent_controller.py \
       tests/test_reader_single_agent_validator.py
```

## 主要页面与接口

### 前端页面

| 路由 | 说明 |
|------|------|
| `/chat` | AI 对话 |
| `/knowledge` | 知识库主页 |
| `/knowledge/:kbId/chunking` | Smart Chunking 配置页 |
| `/literature` | 文献搜索、收藏与分类 |
| `/literature/:paperId/read` | Reader Workbench / 论文阅读页 |
| `/literature/:paperId/read/review` | Review / Publish 工作台 |
| `/code` | CodeLab Notebook 列表 |
| `/code/:notebookId` | CodeLab Notebook 详情页 |
| `/admin/statistics` | 管理统计 |
| `/mentor/*` / `/student/*` | 导师 / 学生协作路由 |

### 关键 API

| 方法 | 端点 | 用途 |
|------|------|------|
| `POST` | `/api/v1/chat/send` | 流式对话 |
| `POST` | `/api/v1/knowledge/search` | 知识库检索 |
| `GET` | `/api/v1/chunking/presets` | 获取智能分块预设 |
| `GET` | `/api/v1/literature/search` | 搜索论文 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/stream` | Reader Workbench SSE 流式生成 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/review-session` | 创建 Review Session |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/publish` | 发布 Review 快照 |
| `GET` | `/api/v1/codelab/notebooks` | Notebook 列表 |
| `POST` | `/api/v1/codelab/notebooks/{id}/run-all` | 全量执行 Notebook |

## 项目结构

```text
research-assistant/
├── backend/
│   ├── app/
│   │   ├── api/                    # FastAPI 路由
│   │   ├── core/                   # 配置、认证、数据库
│   │   ├── models/                 # SQLAlchemy 模型
│   │   ├── schemas/                # Pydantic Schema
│   │   ├── services/               # 业务服务
│   │   │   ├── literature_reader_compose_service.py
│   │   │   ├── reader_single_agent_controller.py
│   │   │   ├── render_pipeline_contract.py
│   │   │   ├── smart_chunking/
│   │   │   ├── notebook_service.py
│   │   │   └── ...
│   │   └── sandbox_runner/         # CodeLab 独立沙箱执行器
│   ├── alembic/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── literature/
│   │   │   ├── knowledge/
│   │   │   ├── chat/
│   │   │   ├── codelab/
│   │   │   └── ...
│   │   ├── components/
│   │   ├── services/api.ts
│   │   └── App.tsx
├── docs/
├── docker-compose.yml
├── .env.example
└── README.md
```

## 推荐先读的文档

- `docs/CONFIGURATION.md`
- `docs/TESTING_GUIDE.md`
- `docs/MULTI_ROLE_SYSTEM_DESIGN.md`
- `docs/SANDBOX_ARCHITECTURE.md`
- `docs/V2_DESIGN_DEPLOY_ACCEPTANCE.md`
- `docs/skills/论文阅读器_单一事实来源与现状基线_2026-03-02_11-30.md`

## 已知现实

- Reader Workbench V2 仍在持续迭代，但已经是平台当前最强的产品面。
- 如果没有 Document Mind 凭证，Reader 链路可以降级运行，但结构质量和证据定位会打折。
- 本地 Embedding 首次启动会下载模型权重，冷启动较慢，后续依赖 `model_cache` volume 缓存。
- CodeLab 默认依赖内部 `codelab-runner`，不要直接把 Runner 暴露到公网。

## License

MIT License
