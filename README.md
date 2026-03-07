# AI 科研助手

面向科研团队的全栈研究工作台，覆盖论文检索与阅读、知识库构建、RAG 问答、Notebook 实验、导师/学生协作，以及可选的 MCP 工具接入。

当前分支 `feature/reader-workbench-v2-phase12` 的核心主题是 **Reader Workbench V2**：把论文阅读从“PDF 查看器 + 问答面板”升级成“结构化解析 + 组件编排 + 评审发布”的生成式阅读工作流。

## 这条分支的重点

近期演进集中在 Reader V2 主链路，相关提交包括：

- `feat(reader): add multimodal review loop and publish pipeline`
- `feat(reader): refine workbench readability and controls`
- `fix(reader): render paper pdf inline`
- `feat(reader): route simplified pipeline through semantic atoms`
- `merge: integrate rag-pdf-dual-path-gate into reader-workbench-v2-phase12`

它们共同把论文阅读链路推进到下面这个形态：

1. `Document Mind DocStructure` 负责高保真 PDF 结构提取。
2. 后端将结构规范化为页面结构和证据锚点。
3. `single_agent_v2` / composed pipeline 生成可渲染的 React 组件树，而不是只拼接纯文本。
4. Review Workbench 支持观察、打补丁、自动修补、发布，最终把评审通过的 UI 覆盖到正式阅读页。
5. 页面和知识处理状态通过事件流回推到前端，便于用户感知长链路进度。

## 核心能力

| 模块 | 当前能力 |
| --- | --- |
| Reader Workbench V2 | 论文页面结构解析、生成式阅读布局、证据锚点预览、内联提问、主题/版式调节、阅读评审与发布 |
| 文献管理 | Semantic Scholar / arXiv 搜索、收藏、PDF 下载、论文详情、阅读记录、评注 |
| 多模态评审发布 | Review Session、观测截图上传、局部 patch、auto-patch、发布已审核快照到正式阅读页 |
| 知识库与 RAG | 文档上传、向量化、混合检索、Query Rewrite、Contextual Compression、层级化 Smart Chunking |
| Chunk Quality Gate | 文档分块质量检测、可疑块修复、失败开关、门禁指标 |
| CodeLab | Jupyter 风格 Notebook、隔离 Runner 执行、变量预览、Kernel 生命周期管理、Notebook Agent |
| AI 对话与 Agent | 多 LLM Provider、流式对话、受控工具链、论文阅读 Agent、Notebook Agent |
| 多角色协作 | 管理员 / 导师 / 学生角色、研究组、邀请、公告、共享资源、共享论文入库 |
| MCP 集成 | 可配置 MCP Server、工具路由、失败回退、本地 UI 管理、可选内部 web/literature server |
| 部署与运维 | Docker Compose、一键起服务、Alembic 自动迁移、Redis 缓存、可选 GPU / MCP profile |

## Reader Workbench V2

`feature/reader-workbench-v2-phase12` 的主价值几乎都在 Reader 侧。当前代码里的主链路由以下模块构成：

- 结构提取：`backend/app/services/document_mind_parser_service.py`
- 组合与发布：`backend/app/services/literature_reader_compose_service.py`
- 基础阅读服务：`backend/app/services/literature_reader_service.py`
- 多模态布局辅助：`backend/app/services/reader_multimodal_layout_service.py`
- 单智能体编排/校验：`backend/app/services/reader_single_agent_controller.py`、`backend/app/services/reader_single_agent_validator.py`
- 前端阅读页：`frontend/src/pages/literature/PaperReaderPage.tsx`
- 前端评审页：`frontend/src/pages/literature/PaperReaderReviewPage.tsx`

当前工作台具备这些行为：

- 按页生成组件化阅读界面，而不是仅显示原始 PDF 文本。
- 保留证据锚点，可跳转到原文区域并显示证据预览。
- 支持主题风格、字号、行高、版式宽度等阅读可读性调节。
- 在 Review Workbench 中审视“真实 React 渲染结果”，而不是只看 JSON。
- 对评审结果做 observation、patch、auto-patch，然后发布为正式阅读覆盖层。
- 在必要时启用多模态辅助布局，降低双栏串列、侧栏混入正文等问题。

## 系统架构

### 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 18, TypeScript, Vite, Ant Design, Zustand, Framer Motion, react-pdf, Monaco |
| 后端 | FastAPI, SQLAlchemy Async, Alembic |
| 数据层 | PostgreSQL + pgvector, Redis |
| AI / 检索 | DeepSeek / OpenAI / DashScope / Ollama, sentence-transformers, reranker, hybrid retrieval |
| 阅读解析 | pypdf, pdfplumber, MarkItDown, Document Mind, 多模态布局辅助 |
| Notebook | 独立 CodeLab Runner, nbformat, nbconvert |
| 扩展 | MCP Client / Server, SSE 状态事件 |

### 主要服务

| 服务 | 默认端口 | 作用 |
| --- | --- | --- |
| `frontend` | `3000` | Web UI |
| `backend` | `8888` | FastAPI API 与 SSE |
| `postgres` | `5432` | 业务库 + pgvector |
| `redis` | `6379` | 缓存、状态、会话 |
| `codelab-runner` | `8099` | Notebook 沙箱执行 |
| `mcp_web` | `8091` | 可选内部 MCP Web Server |
| `mcp_literature` | `8092` | 可选内部 MCP Literature Server |

## 快速开始

### 前置要求

- Docker Desktop / Docker Engine
- Docker Compose v2
- 至少 8GB 内存
- 若启用本地嵌入或 GPU 推理，建议准备额外显存 / 模型缓存空间

### 1. 克隆并切到目标分支

```bash
git clone <repo-url>
cd research-assistant
git checkout feature/reader-workbench-v2-phase12
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

最少要确认这些值：

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `SECRET_KEY`
- `DEFAULT_LLM_PROVIDER` 及对应 API Key
- `CODELAB_RUNNER_TOKEN`

如果你要跑 Reader V2 的完整增强链路，再补这些：

- `DOCUMENT_MIND_ACCESS_KEY_ID`
- `DOCUMENT_MIND_ACCESS_KEY_SECRET`
- `ALIYUN_API_KEY` 或其它 Reader 所需模型 Key
- `SERPER_API_KEY`（若需要 web 搜索）

### 3. 启动核心服务

```bash
docker compose up -d --build postgres redis codelab-runner backend frontend
```

应用启动时会自动执行：

- `alembic upgrade head`
- FastAPI / Vite dev server 启动
- Reader / CodeLab / MCP 关键配置打印

### 4. 可选：启动 MCP profile

```bash
docker compose --profile mcp up -d mcp_web mcp_literature
```

### 5. 访问入口

- 前端：`http://localhost:3000`
- 后端 OpenAPI：`http://localhost:8888/docs`
- Redoc：`http://localhost:8888/redoc`

### 6. 健康检查

```bash
docker compose ps
docker compose logs --tail 100 backend
docker compose logs --tail 100 frontend
docker compose logs --tail 100 codelab-runner
```

## 推荐环境变量分组

`.env.example` 已经按模块给出默认值。实际部署时，优先关注下面几组。

### 基础运行

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 连接串 |
| `REDIS_URL` | Redis 连接串 |
| `SECRET_KEY` | JWT 签名密钥 |
| `AUTO_CREATE_TABLES` | 开发环境可用，生产建议 `false` |
| `CORS_ALLOW_ORIGINS` | 前端域名白名单 |

### LLM / Embedding

| 变量 | 说明 |
| --- | --- |
| `DEFAULT_LLM_PROVIDER` | `deepseek` / `openai` / `aliyun` / `ollama` |
| `EMBEDDING_PROVIDER` | `local` / `aliyun` / `openai` / `ollama` |
| `LOCAL_EMBEDDING_MODEL` | 本地嵌入模型，默认 `BAAI/bge-m3` |
| `ENABLE_RERANKER` | 是否启用 reranker |
| `ENABLE_HYBRID_RETRIEVAL` | 是否启用混合检索 |
| `ENABLE_QUERY_REWRITE` | 是否启用查询改写 |
| `ENABLE_CONTEXTUAL_COMPRESSION` | 是否启用上下文压缩 |

### Reader Workbench V2

| 变量 | 说明 |
| --- | --- |
| `READER_PIPELINE_MODE` | 当前默认 `single_agent_v2` |
| `READER_PIPELINE_VERSION` | 当前默认 `simplified_v2` |
| `READER_AGENT_MODEL` | Reader 编排主模型 |
| `READER_AGENT_MAX_STEPS` | Reader 智能体最大步数 |
| `READER_MM_ASSIST_ENABLED` | 多模态辅助布局开关 |
| `READER_DOCUMENT_MIND_ENABLED` | 是否启用 Document Mind 主链路 |
| `DOCUMENT_MIND_OPTION` | 默认 `docStructure` |
| `READER_COMPOSE_LATENCY_BUDGET_MS` | 组合链路延迟预算 |

### Chunk Quality Gate / 文档门禁

| 变量 | 说明 |
| --- | --- |
| `CHUNK_QUALITY_GATE_ENABLED` | 分块质量门禁开关 |
| `CHUNK_REPAIR_ENABLED` | 是否尝试自动修复坏块 |
| `CHUNK_QUALITY_GATE_BAD_THRESHOLD` | 坏块阈值 |
| `CHUNK_QUALITY_GATE_DOC_FAIL_RATIO` | 整篇失败比例阈值 |

### CodeLab / MCP

| 变量 | 说明 |
| --- | --- |
| `CODELAB_RUNNER_ENABLED` | Notebook Runner 开关 |
| `CODELAB_RUNNER_URL` | Runner 服务地址 |
| `CODELAB_RUNNER_TOKEN` | Runner 鉴权令牌 |
| `MCP_ENABLED` | 是否启用 MCP |
| `MCP_CONFIG_PATH` | MCP 配置文件路径 |
| `MCP_TOOL_ROUTES` | 本地工具到远端 MCP 工具的路由配置 |

## 主要用户路径

| 功能 | 路由 |
| --- | --- |
| 登录 / 注册 | `/login`、`/register` |
| Dashboard | `/dashboard` |
| Chat | `/chat`、`/chat/:conversationId` |
| Knowledge | `/knowledge`、`/knowledge/:kbId` |
| Smart Chunking | `/knowledge/chunking`、`/knowledge/:kbId/chunking` |
| Literature | `/literature` |
| Reader Workbench | `/literature/:paperId/read` |
| Review Workbench | `/literature/:paperId/read/review?sessionId=...&snapshotId=...` |
| CodeLab | `/code`、`/code/:notebookId` |
| 管理/导师/学生/共享 | `/admin/*`、`/mentor/*`、`/student/*`、`/shared/*` |

## 关键 API

### Literature / Reader

- `GET /api/v1/literature/search`
- `POST /api/v1/literature/papers`
- `POST /api/v1/literature/papers/{paper_id}/download-pdf`
- `POST /api/v1/literature/papers/{paper_id}/reader/composed/review-session`
- `GET /api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}`
- `POST /api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/observation`
- `POST /api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/patch`
- `POST /api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/auto-patch`
- `POST /api/v1/literature/papers/{paper_id}/reader/composed/review-session/{session_id}/publish`

### Knowledge / Chunking

- `POST /api/v1/knowledge/knowledge-bases`
- `POST /api/v1/knowledge/knowledge-bases/{id}/documents/upload`
- `POST /api/v1/knowledge/search`
- `GET /api/v1/chunking/presets`
- `POST /api/v1/chunking/preview`
- `PUT /api/v1/chunking/knowledge-bases/{id}/config`

### CodeLab

- `GET /api/v1/codelab/notebooks`
- `POST /api/v1/codelab/notebooks`
- `POST /api/v1/codelab/notebooks/{notebook_id}/execute`
- `POST /api/v1/codelab/notebooks/{notebook_id}/run-all`
- `POST /api/v1/codelab/notebooks/{notebook_id}/restart-kernel`
- `GET /api/v1/codelab/notebooks/{notebook_id}/kernel-status`

### MCP / Share / Mentor-Student

- `GET /api/v1/mcp/config`
- `PUT /api/v1/mcp/config`
- `GET /api/v1/share/shared-with-me`
- `GET /api/v1/mentor/students`
- `GET /api/v1/student/mentor`

## 本地开发

### 前端

```bash
cd frontend
npm install
npm run dev
npm run build
```

### 后端

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 常用验证

```bash
docker compose logs --tail 200 backend
docker compose logs --tail 200 frontend
docker compose logs --tail 200 codelab-runner
```

如果你在调 Reader V2，优先检查：

- `backend/app/api/literature.py`
- `backend/app/services/literature_reader_compose_service.py`
- `backend/app/services/reader_multimodal_layout_service.py`
- `frontend/src/pages/literature/PaperReaderPage.tsx`
- `frontend/src/pages/literature/PaperReaderReviewPage.tsx`

## 项目结构

```text
research-assistant/
├── backend/
│   ├── app/
│   │   ├── api/                 # FastAPI 路由
│   │   ├── core/                # 数据库、认证、权限、错误处理
│   │   ├── models/              # SQLAlchemy 模型
│   │   ├── schemas/             # Pydantic 模型
│   │   └── services/            # Reader / RAG / CodeLab / MCP 核心逻辑
│   ├── alembic/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── literature/      # Reader Workbench / Review Workbench
│   │   │   ├── knowledge/       # 知识库与 Smart Chunking
│   │   │   ├── codelab/         # Notebook 工作台
│   │   │   └── chat/            # 对话与会话管理
│   │   ├── components/
│   │   ├── services/api.ts
│   │   └── App.tsx
│   └── package.json
├── docs/                        # 阶段说明、测试记录、部署手册
├── docker-compose.yml
├── docker-compose.gpu.yml
├── .env.example
└── README.md
```

## 运维提示

- 生产环境建议使用 Alembic，不要依赖 `AUTO_CREATE_TABLES=true`。
- 启用 `Document Mind`、多模态 Reader 和本地嵌入时，模型与 OCR 服务的网络可达性很关键。
- CodeLab 通过独立 Runner 执行 Python 代码，生产环境务必更换 `CODELAB_RUNNER_TOKEN`。
- MCP 默认关闭；若开启，建议同时配置工具路由、超时、重试和熔断参数。
- Windows 宿主上若出现热更新不稳定，可保留 `.env.example` 中的 polling 相关前端变量。

## 相关文档

- `docs/LITERATURE_MODULE.md`
- `docs/SMART_CHUNKING_DEPLOYMENT.md`
- `docs/CODELAB_MAINTENANCE.md`
- `docs/MULTI_ROLE_SYSTEM_DESIGN.md`
- `docs/skills/论文阅读器_单一事实来源与现状基线_2026-03-02_11-30.md`

## License

MIT
