# AI 科研助手 / Research Assistant

截至 2026-03-07，本仓库当前主线工作集中在 `feature/reader-workbench-v2-phase12`：围绕论文阅读工作台 V2，把 PDF 结构解析、生成式阅读界面、人工 Review / Publish 闭环、RAG 检索、CodeLab 实验台和多角色协作串成一套完整的科研工作流。

它不是单一的“聊天”或“知识库”项目，而是一套面向科研团队的全栈平台：

- 论文搜索、收藏、下载、入库、按页阅读
- Reader Workbench V2：把论文页结构转换为可交互的阅读组件树
- 知识库与 RAG：智能分块、混合检索、重排、上下文压缩
- CodeLab：Jupyter 风格 Notebook + Python 沙箱执行 + Notebook Agent
- Chat / Agent / MCP：统一工具链与流式交互
- 管理员 / 导师 / 学生多角色协作、共享与公告

## 当前分支重点

`feature/reader-workbench-v2-phase12` 最近一轮演进，核心集中在论文阅读链路：

| 日期 | 进展 |
|------|------|
| 2026-03-07 | `feat(reader): route simplified pipeline through semantic atoms`，简化链路改为走语义原子 |
| 2026-03-07 | `fix(reader): render paper pdf inline`，阅读器内联 PDF 呈现稳定化 |
| 2026-03-07 | `feat(reader): refine workbench readability and controls`，工作台可读性与控件体验继续收口 |
| 2026-03-06 | `feat(reader): add multimodal review loop and publish pipeline`，补齐 Review / Publish 闭环 |
| 2026-03-03 | `feat(reader): migrate composed pipeline to single_agent_v2`，生成式阅读主链切到 `single_agent_v2` |
| 2026-02-26 | `feat(reader): 完成阅读工作台V2阶段改造并补齐文档`，Reader Workbench V2 第一轮落地完成 |
| 2026-02-25 | `feat(literature-backend): add generative/composed reader core` 与 `feat(literature-frontend): implement actionable composed reader UI`，生成式阅读后端和前端基座建立 |

## 核心能力

| 模块 | 当前能力 |
|------|------|
| 论文阅读工作台 V2 | Document Mind / PDF 结构解析、`page_structure_v3`、`single_agent_v2` 组件编排、SSE 流式 UI patch、证据锚点定位、Review / Publish 工作台 |
| 文献管理 | Semantic Scholar / arXiv 搜索、论文收藏、PDF 下载、分类管理、引用图、阅读位置记忆 |
| 知识库 / RAG | 本地或 API Embedding、智能分块、混合检索、Query Rewrite、Reranker、Contextual Compression、Chunk Quality Gate |
| CodeLab | Notebook 增删改、单 Cell / 全量运行、变量查看、Notebook Agent、独立沙箱 Runner |
| Chat / Agent | 流式对话、Agent Core、工具调用、来源引用、跨模块统一配置 |
| 协作系统 | 管理员 / 导师 / 学生角色路由、资源共享、邀请、公告、统计页 |
| MCP | 可选外部 MCP Server 接入，支持工具前缀、路由、重试、熔断与前端配置 |

## Reader Workbench V2 是怎么工作的

以代码和当前分支实现为准，论文页的生成式阅读链路大致如下：

1. 论文 PDF 下载并入库，Reader 页面按 paper / page 打开。
2. 后端优先使用 Document Mind DocStructure 提取版面结构，再标准化为 `page_structure_v3`。
3. 简化链路与 Compose 链路会把结构块整理为语义原子、关系和页面导航信息。
4. `single_agent_v2` 基于页面元数据、结构块和渲染图像，输出受约束的 UI 组件树。
5. 后端对组件白名单、锚点所有权、最小门禁和布局合同做校验，失败时降级而不是整页清空。
6. 前端通过 SSE 接收 `plan_draft`、`plan_patch`、`assets`、`quality_report`、`done` 等事件，增量渲染 Reader Workbench。
7. 用户可以进入 `/literature/:paperId/read/review` 对生成结果做人工观察、Patch、自动修复，再发布为可复用快照。

这条链路对应的关键实现主要在：

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
| AI / 检索 | OpenAI / DeepSeek / 阿里云 / Ollama，多模态布局辅助，本地嵌入，重排与压缩 |
| PDF / Reader | pypdf, pdfplumber, MarkItDown, Alibaba Cloud Document Mind |
| Notebook | Monaco Editor, Notebook Agent, 独立 `codelab-runner` 沙箱服务 |
| 部署 | Docker Compose，`mcp` profile 可选启用内部 MCP 服务 |

## 快速开始

### 前置要求

- Docker Desktop 或 Docker Engine + Docker Compose
- 至少 4 GB 可用内存；如果启用本地 Embedding，建议 8 GB 以上
- 如在 Windows PowerShell 下运行，建议先执行 `chcp 65001`

### 1. 克隆并配置环境变量

```bash
git clone <repo-url>
cd research-assistant
cp .env.example .env
```

必须至少配置这些值：

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `SECRET_KEY`
- `CODELAB_RUNNER_TOKEN`
- 至少一组 LLM 凭证：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ALIYUN_API_KEY`

如果你要启用 Reader Workbench V2 的结构化 PDF 能力，建议额外配置：

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

README 只列当前分支最关键的一组；完整列表请看 [`.env.example`](.env.example)。

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

如果你没有 Document Mind 凭证，可以先把：

```env
READER_DOCUMENT_MIND_ENABLED=false
PDF_LAYOUT_PARSER=markitdown
```

作为降级方案，但论文页结构保真度会明显下降。

### CodeLab

```env
CODELAB_RUNNER_ENABLED=true
CODELAB_RUNNER_URL=http://codelab-runner:8099
CODELAB_RUNNER_TOKEN=change_me_internal_runner_token
CODELAB_EXEC_TIMEOUT_HARD_SECONDS=20
CODELAB_MAX_CONCURRENCY_PER_USER=2
```

### MCP

```env
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

如果要回归 Reader Workbench V2 的流式界面链路，还可以重点看：

- `backend/tests/integration/test_reader_composed_stream_workbench_v2.py`
- `backend/tests/test_literature_reader_api.py`

## 主要页面与接口

### 前端页面

| 路由 | 说明 |
|------|------|
| `/chat` | AI 对话 |
| `/knowledge` | 知识库主页 |
| `/knowledge/:kbId/chunking` | Smart Chunking 配置页 |
| `/literature` | 文献搜索、收藏与分类 |
| `/literature/:paperId/read` | Reader Workbench / 论文阅读页 |
| `/literature/:paperId/read/review` | Compose Review / Publish 工作台 |
| `/code` | CodeLab Notebook 列表 |
| `/code/:notebookId` | CodeLab Notebook 详情页 |
| `/admin/statistics` | 管理统计 |
| `/mentor/*` / `/student/*` | 导师 / 学生协作路由 |

### 关键 API

| 方法 | 端点 | 用途 |
|------|------|------|
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/stream` | Reader Workbench SSE 流式生成 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/generative/stream` | 文本型 Reader 流式生成 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/review-session` | 创建 Review Session |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/publish` | 发布 Review 快照 |
| `POST` | `/api/v1/knowledge/search` | 知识库检索 |
| `GET` | `/api/v1/chunking/presets` | 获取智能分块预设 |
| `GET` | `/api/v1/codelab/notebooks` | Notebook 列表 |
| `POST` | `/api/v1/codelab/notebooks/{id}/run-all` | 全量执行 Notebook |
| `POST` | `/api/v1/chat/send` | 流式对话 |

## 目录结构

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

如果你接下来要继续开发 phase12，这几份文档最有参考价值：

- [docs/skills/论文阅读器_单一事实来源与现状基线_2026-03-02_11-30.md](docs/skills/论文阅读器_单一事实来源与现状基线_2026-03-02_11-30.md)
- [docs/V2_DESIGN_DEPLOY_ACCEPTANCE.md](docs/V2_DESIGN_DEPLOY_ACCEPTANCE.md)
- [docs/SANDBOX_ARCHITECTURE.md](docs/SANDBOX_ARCHITECTURE.md)
- [docs/MULTI_ROLE_SYSTEM_DESIGN.md](docs/MULTI_ROLE_SYSTEM_DESIGN.md)
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md)

## 已知现实

- Reader Workbench V2 仍在持续迭代，当前最强链路是 `single_agent_v2 + semantic atoms + review/publish`。
- 如果没有 Document Mind 凭证，Reader 页可以降级运行，但结构质量和证据定位会打折。
- 本地 Embedding 首次启动会下载模型权重，冷启动较慢，后续依赖 `model_cache` volume 缓存。
- CodeLab 默认依赖内部 `codelab-runner`；不要直接把 Runner 暴露到公网。

## License

MIT License
