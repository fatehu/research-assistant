# AI 科研助手 / Research Assistant

AI 科研助手是一个面向科研工作的全栈研究平台，目标是把“查资料、读论文、做综述、复现实验、形成创新方案、撰写项目文档、导出 Word”串成一个端到端工作流。系统不是单纯的聊天页面，也不是单独的文献库，而是由主 Agent、检索工具、论文阅读器、Project worker、结构化文档 artifact、DOCX 模板和 runtime-worker 共同组成的科研工作台。

平台的核心设计是分工清晰：

- 主 Agent 负责理解需求、规划研究路径、调用检索工具、组织证据、维护上下文、解释结果。
- 学术检索和公网检索负责把外部资源带入平台，但资源本身仍遵守原始网站和 API 的能力边界。
- 文献库和阅读器负责保存论文、解析 PDF、生成页面级阅读体验和证据定位。
- 文献综述 skill 负责围绕主题收集论文、沉淀单篇 review，并生成最终综述。
- Project worker 负责论文复现、代码运行、调试和优化，主 Agent 不直接替代 worker 做长期实施。
- 文档 artifact 负责把长文写作拆成可编辑 block，让用户和 Agent 可以按章节协作。
- DOCX 模板和 Claude document skill 负责把结构化内容导出为可编辑 Word 文件，并支持后续精修。

## 业务能力

### 1. 联网调研和研究综述

用户可以在 Chat 中提出研究问题，平台会根据需求选择公网搜索、学术检索或知识库检索，整理来源、摘要、重点和可追溯引用，形成研究综述式回答。

支持的检索方向：

| 类型 | 工具 / 服务 | 适用场景 |
| --- | --- | --- |
| 公网搜索 | `web_search`、`web_scrape` | 查网页、项目主页、新闻、技术博客、政策说明、机构资料。 |
| 学术检索 | `literature_search` | 查论文摘要、作者、年份、DOI、开放获取链接、PDF URL。 |
| 知识库检索 | `knowledge_search` | 查用户已经上传或入库的资料。 |

学术检索支持：

- `auto`
- OpenAlex
- Semantic Scholar
- arXiv
- PubMed
- CrossRef

系统会尽量使用这些 API 的原生优势，例如 OpenAlex 的规模和开放元数据、Semantic Scholar 的摘要和引用信息、arXiv 的预印本全文入口、PubMed 的生物医学索引、CrossRef 的 DOI 元数据。`multi` 不作为默认强推路径，优先使用 `auto` 或明确指定数据源。

文献综述 skill 的典型流程：

1. 用户给出明确研究主题。
2. `literature_review_start` 创建 `/app/uploads/literature_reviews/{review_id}`。
3. `literature_search` 用英文检索论文，也可根据主题补充中文公网搜索。
4. `literature_review_download_pdf` 下载可合法访问的 PDF 到 `pdf/`。
5. `literature_review_pdf_to_markdown` 把 PDF 转为 Markdown，保存到 `md/`。
6. `review_writer mode=paper` 为每篇论文生成 `review/{paper_key}.md`。
7. `review_writer mode=final` 读取所有单篇 review，结合主题生成 `review/final.md`。
8. `/literature-reviews` 页面展示 review 工作区，支持浏览和下载 JSON/Markdown 文件。

重要边界：

- `md/*.md` 是论文全文 Markdown，默认用于 Zoekt 检索，不建议整篇直接塞进上下文。
- `review/*.md` 是成品单篇综述和最终综述，是回答综述问题时的优先阅读对象。
- `md/*.json` 是 PDF-to-Markdown 的解析报告，不是综述 JSON。
- 全文翻译通常超过单轮输出预算，系统应改为定向片段翻译或在对应 artifact block 中说明限制。

### 2. 单篇论文阅读和管理

文献模块支持从搜索结果、DOI、arXiv、PubMed、OpenAlex、Semantic Scholar 或论文链接入库。论文不强制绑定知识库，只要保存到文献库即可阅读、下载 PDF、收藏、分类和在 Chat 中引用。

主要能力：

- 文献搜索和分页加载。
- PDF 下载和本地保存。
- 收藏夹、分类、开放获取筛选和排序。
- PDF 阅读、页面跳转、注释和评论。
- Reader Workbench 和 Experience V2。
- 单篇论文问答和证据定位。
- 与 Project 复现工作流衔接。

主要页面：

| 路由 | 说明 |
| --- | --- |
| `/literature` | 文献搜索、收藏、PDF 下载、分类管理。 |
| `/literature/:paperId/read` | 单篇论文阅读。 |
| `/literature/:paperId/read/workbench` | Reader 工作台。 |
| `/literature/:paperId/experience-v2` | 生成式论文阅读体验页。 |
| `/literature/:paperId/workbench-v2` | 生成式阅读调试工作台。 |
| `/literature/:paperId/read/review` | Reader review / publish。 |

Reader 的技术链路：

1. PDF 解析为页面文本、结构块、图表和素材。
2. 后端构建 page dossier，包括当前页、相邻页、关键 claims、术语和视觉资源。
3. 生成式 reader runtime 产出受约束的 plan。
4. 后端做 renderer contract 校验、锚点校验和 fallback。
5. 前端用白名单组件渲染，不执行任意 HTML。

关键文件：

- `backend/app/api/literature.py`
- `backend/app/services/generative_reader_agent_runtime.py`
- `backend/app/services/generative_reader_agent_core.py`
- `backend/app/services/generative_reader_agent_tools.py`
- `backend/app/services/literature_reader_compose_service.py`
- `frontend/src/pages/literature/PaperReaderPage.tsx`
- `frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx`
- `frontend/src/pages/literature/PaperReaderExperienceV2Page.tsx`
- `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`

### 3. 论文复现和代码落地

论文复现采用 Project-first 方式。主 Agent 负责研究判断和任务拆解，Claude Code worker 负责代码实施、命令运行、调试和交付。

工作流：

1. 用户选择一篇论文或在 Chat 中提出复现需求。
2. `paper_research_prepare` 创建或复用 Project。
3. Project 根目录固定为 `/app/uploads/projects/{project_id}`。
4. 系统生成 reference bundle：
   - `reference/paper/paper_pdf2md.md`
   - `reference/paper/paper_interpretation.md`
   - `reference/paper/paper_interpretation.json`
   - `reference/repo/readme_intake.json`
5. 主 Agent 基于论文、代码仓库和用户目标制定复现方案。
6. `project_claude` 把实施任务交给 Claude Code worker。
7. worker 在 Project 目录内修改代码、安装依赖、运行实验、记录结果。
8. 主 Agent 汇总结果、解释问题、提出下一步优化方向。

Project 工具边界：

- Project 工具专用于论文复现、代码编写、代码优化和实验验证。
- DOCX 生成、模板管理、文献综述不应 fallback 到 Project 工具。
- 如果 Claude Code 不可达，主 Agent 应报告阻塞点，而不是自行替代 worker 长时间执行。

关键工具：

| 工具 | 用途 |
| --- | --- |
| `paper_search` | 查找已保存论文。 |
| `paper_research_prepare` | 准备 Project 和 reference bundle。 |
| `paper_research_status` | 查看论文复现 readiness 和 Project 状态。 |
| `project_tree` | 查看 Project 目录。 |
| `project_read_file` | 读取 Project 文件。 |
| `project_write_file` | 在受控场景写入 Project 文件。 |
| `project_claude` | 调 Claude Code worker 实施代码任务。 |
| `paper_research_search_project_zoekt` | 在 Project 内做 Zoekt 搜索。 |
| `paper_research_probe_repo` | 探测代码仓库。 |
| `paper_research_probe_url` | 探测论文、文档或资源 URL。 |

### 4. 创新方案设计和验证

平台可以根据用户诉求，结合学术检索、论文阅读、文献综述和代码实验，形成从研究空白到可验证方案的闭环。

典型路径：

1. 用 `web_search` 和 `literature_search` 收集公开资料与学术论文。
2. 用文献综述 skill 建立主题材料池。
3. 对关键论文进入 Reader 进行精读。
4. 主 Agent 总结现有方法、研究空白、可行创新点和实验假设。
5. 需要代码验证时进入 Project 或 CodeLab。
6. worker 执行代码、实验和调试。
7. 结果回到 Chat、artifact 或 DOCX 模板中形成报告、项目书或论文草稿。

### 5. 自定义撰写模板和结构化写作

模板管理页面在 `/templates`，用于管理国基、科研项目、教研项目、毕业论文、综述报告等写作模板。

目录约定：

| 目录 | 内容 |
| --- | --- |
| `/app/uploads/docx` | DOCX 根目录。 |
| `/app/uploads/docx/templates/{template_id}` | 模板、附件、约束和分析产物。 |
| `/app/uploads/docx/artifacts/{conversation_id}/{artifact_id}` | Chat document artifact JSON。 |
| `/app/uploads/docx/{docx_id}` | DOCX 生成和精修工作区。 |

模板附件类型：

| 类型 | 作用 |
| --- | --- |
| 成品/样例模板 `sample_template` | 分析版式、标题层级、页眉页脚、目录、页码和表格样式，主要影响 DOCX 生成约束。 |
| 撰写说明/填报指南 `writing_guide` | 抽取章节结构、内容要求、字数限制、填报口径和注意事项，主要影响 Markdown 生成约束。 |
| 普通参考附件 `reference` | 作为生成时参考材料，不主动总结为强约束。 |

模板分析链路：

1. 用户上传模板附件。
2. 用户点击分析。
3. 后端用 Pandoc、LibreOffice 和 OOXML 解析 DOC/DOCX/Markdown/文本等文件。
4. LLM 生成两类可编辑约束：
   - `md_constraints`：给主 Agent 生成内容和 artifact schema 使用。
   - `docx_constraints`：给 Claude document skill 生成 Word 样式使用。
5. 用户检查并编辑约束。
6. Chat 中选择模板，生成 document artifact。
7. 主 Agent 按 block 读取、生成和修改内容。
8. 用户可以在右侧面板手动编辑、预览、局部改写和选择 block 作为下一轮上下文。

document artifact 的设计：

- 一篇文档不是一整坨 Markdown，而是由多个 block 组成。
- 每个 block 有 `block_id`、标题、层级、目标字数、写作约束、Markdown 内容和状态。
- 主 Agent 可以通过 `document_artifact_read` 读取指定 block，通过 `document_artifact_update_block` 写回指定 block。
- 前端会通过 SSE 接收 `artifact_updated`，把后端工具写入实时 patch 到当前会话。
- `/templates` 的 DOCX 区会展示关联 artifact，便于按 `artifact_id` 区分来源和状态。

### 6. DOCX 导出和后续精修

平台不在业务代码里复刻官方 Word 生成 skill，而是准备合理的工作目录、输入路径和约束，让 Claude Code 调用官方 `document-skills:docx` 完成 Word 生成。

DOCX 生成工作区：

- 固定目录：`/app/uploads/docx/{docx_id}`
- 工作方式：在该目录中让 Claude Code 工作，必要时 resume 同一个 session。
- 启动模式：runtime-worker 中 Claude Code 以 bypass/yolo 模式运行。

生成时准备的文件：

- `docx_inputs_manifest.json`
- `requirements.md`
- `default_docx_style_prompt.md`
- `template_md_constraints.md`
- `docx_request.json`
- artifact JSON 路径
- 模板附件路径

主要工具：

| 工具 | 用途 |
| --- | --- |
| `docx_generate_with_claude` | 根据 artifact、模板路径和要求生成 DOCX/PDF。 |
| `docx_refine_with_claude` | 在已有 docx_id 目录内继续修改 DOCX。 |

生成原则：

- 大文件传路径，不把完整 artifact JSON 或模板内容直接塞进 prompt。
- Claude 输出状态通过 runtime-worker 流式返回，前端只展示用户需要的摘要、阶段、路径和错误。
- 目录、页码、导航、页眉页脚、交叉引用等优先交给 Word 原生结构和官方 document skill 处理。
- 如果不能实现某项 Word 功能，Claude 应在 notes 中明确说明。

## Generative UI

平台有两类生成式 UI。

### Reader Generative UI

面向论文阅读页，输入 page dossier、相邻页上下文、图表资源和用户阅读 profile，输出受约束的阅读 plan、story substrate、page brief 和 renderer contract。前端只渲染白名单组件，保证体验稳定、可校验。

相关页面：

- `/literature/:paperId/experience-v2`
- `/literature/:paperId/workbench-v2`

### Document Artifact UI

面向结构化写作，输入模板 schema、用户需求和 Agent 生成内容，输出可编辑的 block 面板。用户可以预览 Markdown、手动修改、选择 block 发送给 Agent、局部改写，并把结果交给 DOCX 工具导出 Word。

相关页面：

- `/chat/:conversationId`
- 右侧 `DocumentArtifactPanel`
- `/templates`

## 上下文管理

Chat 不是简单把全部历史消息拼接给模型。系统提供了更完善的上下文管理，让长对话、工具调用、引用、skill、artifact 和 worker 进度能够合理协同。

| 层 | 作用 |
| --- | --- |
| `context-preview` | 发送前预估本轮上下文、工具、RAG、skill prompt 和 token 预算。 |
| `context_state` | 会话级状态，例如当前主题、目标、已确认事实和未解决问题。 |
| `turn_store` | 一轮用户请求触发的一次完整回合。 |
| `item_stream` | 回合内细粒度事件流，例如用户消息、assistant 消息、工具调用、工具结果和 compact boundary。 |
| `workflow_control` | 管理继续、等待、分支、手动确认等工作流行为。 |
| `citation_index` | 维护回答中 `[网页X]` / `[来源X]` 的来源索引。 |
| `artifact_updated` | 工具更新 document artifact 后通过 SSE 实时同步前端。 |
| compaction | 长对话压缩，保留事实层，避免旧过程日志污染后续上下文。 |
| tool output truncation | 工具结果按全局预算进入上下文，保留可读摘要和关键首尾内容。 |

这套机制用于保证：

- 长对话仍能形成稳定主题和任务状态。
- 工具过程和最终回答分开展示。
- 引用说明可以从当前回答和历史 citation index 回填。
- artifact block 写入后前端能实时更新。
- Claude worker 长时间工作时，用户可以看到阶段、进度和结果路径。

关键文件：

- `backend/app/api/chat.py`
- `backend/app/services/react_agent.py`
- `backend/app/services/chat_context_store.py`
- `backend/app/services/conversation_context_compaction_service.py`
- `frontend/src/stores/chatStore.ts`
- `frontend/src/pages/chat/components/ContextDebugWindow.tsx`
- `frontend/src/pages/chat/components/TurnTimeline.tsx`
- `frontend/src/pages/chat/components/TurnProcessLanes.tsx`

## Agent 和 Worker 协作结构

```text
用户
  ↓
主 Agent / React Agent
  - 理解诉求
  - 激活 skill
  - 选择工具
  - 管理上下文
  - 维护引用和 artifact
  - 解释结果
  ↓
受控工具层
  - web_search / literature_search / knowledge_search
  - document_artifact_read / document_artifact_update_block
  - paper_research_prepare / paper_research_status
  - docx_generate_with_claude / docx_refine_with_claude
  ↓
Worker 层
  - project_claude：代码复现、运行、调试
  - docx Claude：Word 生成和修改
  - codelab-runner：Notebook / Python 执行
  - PDF-to-Markdown / Zoekt / Pandoc / LibreOffice
```

协作原则：

- 主 Agent 是研究策划者和调度者。
- Claude Code worker 是实施者。
- Bash / probe 工具用于环境探测、路径确认和轻量检查。
- 大文件走 workspace 和路径，不走 prompt 全文。
- 不同业务工具保持边界，避免 Project、DOCX、综述互相误用。

## 系统架构

```text
research-assistant/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── chat.py
│   │   │   ├── literature.py
│   │   │   ├── literature_reviews.py
│   │   │   ├── docx_templates.py
│   │   │   ├── projects.py
│   │   │   └── codelab.py
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── react_agent.py
│   │   │   ├── agent_skill_service.py
│   │   │   ├── agent_tools_impl/
│   │   │   ├── document_artifact_service.py
│   │   │   ├── docx_template_service.py
│   │   │   ├── docx_runtime_service.py
│   │   │   ├── literature_service.py
│   │   │   ├── literature_review_workspace_service.py
│   │   │   ├── project_service.py
│   │   │   ├── project_reference_builder_service.py
│   │   │   ├── project_runtime_service.py
│   │   │   ├── generative_reader_agent_runtime.py
│   │   │   ├── chat_context_store.py
│   │   │   └── conversation_context_compaction_service.py
│   │   └── runtime_worker/
│   ├── alembic/
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── chat/
│   │   │   ├── literature/
│   │   │   ├── literatureReviews/
│   │   │   ├── projects/
│   │   │   ├── templates/
│   │   │   ├── knowledge/
│   │   │   └── codelab/
│   │   ├── services/api.ts
│   │   └── stores/
├── .agents/skills/
│   ├── literature-review/
│   ├── paper-reproduction/
│   └── paper2code/
├── docs/
├── docker-compose.yml
└── README.md
```

## 主要页面

| 路由 | 说明 |
| --- | --- |
| `/dashboard` | 平台入口和工作台概览。 |
| `/chat`、`/chat/:conversationId` | 主 Agent 对话、工具流、上下文窗口、document artifact。 |
| `/chat/manage` | 对话管理。 |
| `/knowledge`、`/knowledge/:kbId` | 知识库、文档和 RAG 管理。 |
| `/knowledge/:kbId/chunking` | Smart Chunking 配置。 |
| `/literature` | 文献搜索、收藏、PDF 下载和分类。 |
| `/literature/:paperId/read` | 单篇论文阅读。 |
| `/literature/:paperId/experience-v2` | 生成式论文阅读体验页。 |
| `/literature/:paperId/workbench-v2` | 生成式阅读工作台。 |
| `/literature/:paperId/read/review` | Reader Review / Publish。 |
| `/literature-reviews` | 文献综述 workspace 管理。 |
| `/projects`、`/projects/:projectId` | 论文复现 Project 管理。 |
| `/templates` | DOCX 模板、artifact 和生成工作区管理。 |
| `/code`、`/code/:notebookId` | CodeLab Notebook。 |
| `/admin/*`、`/mentor/*`、`/student/*` | 分角色管理与协作页面。 |

## 关键 API

| 方法 | 端点 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/chat/send` | 主聊天 SSE。 |
| `POST` | `/api/v1/chat/context-preview` | 发送前上下文预览。 |
| `POST` | `/api/v1/chat/conversations/{id}/branch` | 创建对话分支。 |
| `POST` | `/api/v1/chat/conversations/{id}/compact` | 手动压缩上下文。 |
| `GET/POST` | `/api/v1/chat/conversations/{id}/document-artifact` | 读取或创建当前对话 artifact。 |
| `PATCH` | `/api/v1/chat/conversations/{id}/document-artifact/blocks/{block_id}` | 更新 artifact block。 |
| `POST` | `/api/v1/chat/conversations/{id}/document-artifact/blocks/{block_id}/rewrite-span` | artifact 局部改写。 |
| `GET` | `/api/v1/literature/search` | 学术搜索。 |
| `POST` | `/api/v1/literature/papers/{paper_id}/download-pdf` | 下载 PDF。 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/stream` | Reader Workbench SSE。 |
| `GET` | `/api/v1/literature-reviews/overview` | 文献综述工作区列表。 |
| `GET` | `/api/v1/literature-reviews/{review_id}` | 文献综述工作区详情。 |
| `GET` | `/api/v1/literature-reviews/{review_id}/files/content` | 读取综述文件内容。 |
| `GET` | `/api/v1/projects` | Project 列表。 |
| `GET` | `/api/v1/projects/{project_id}/folder-tree` | Project 文件树。 |
| `GET` | `/api/v1/docx/templates/overview` | 模板、artifact 和 DOCX 工作区概览。 |
| `POST` | `/api/v1/docx/templates` | 新建或更新模板。 |
| `POST` | `/api/v1/docx/templates/{template_id}/files` | 上传模板附件。 |
| `POST` | `/api/v1/docx/templates/{template_id}/analyze` | 分析模板并生成约束。 |
| `PUT` | `/api/v1/docx/templates/default-docx-style-prompt` | 更新默认 DOCX 样式提示词。 |
| `GET` | `/api/v1/codelab/notebooks` | Notebook 列表。 |
| `POST` | `/api/v1/codelab/notebooks/{id}/agent/chat` | Notebook Agent。 |

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | React 18、TypeScript、Vite、Zustand、Ant Design、Framer Motion。 |
| 后端 | FastAPI、SQLAlchemy Async、Pydantic v2、Alembic。 |
| 存储 | PostgreSQL + pgvector、Redis、本地 `/app/uploads` 文件系统。 |
| LLM | OpenAI-compatible、DeepSeek、阿里云、Ollama 等 provider。 |
| Agent | ReAct-style tool loop、skill session prompt、tool registry、SSE。 |
| 学术检索 | OpenAlex、Semantic Scholar、arXiv、PubMed、CrossRef。 |
| 公网检索 | Tavily、Serper、DDGS fallback。 |
| RAG | Embedding、Hybrid Retrieval、Reranker、Query Rewrite、Contextual Compression、Smart Chunking。 |
| PDF | pypdf、pdfplumber、MarkItDown、Pandoc、LibreOffice、可选 Document Mind。 |
| 代码执行 | runtime-worker、Claude Code、codelab-runner、Notebook Agent。 |
| DOCX | Claude Code document-skills/docx、python-docx、Pandoc、OOXML 解析辅助。 |
| 搜索索引 | Zoekt，用于 Project 和文献综述 Markdown 检索。 |
| 部署 | Docker Compose，默认开发模式前端热更新。 |

## 快速开始

### 前置要求

- Docker Desktop 或 Docker Engine + Docker Compose。
- 建议 8 GB 以上内存。
- Windows PowerShell 建议先执行 `chcp 65001`。
- 至少配置一个可用 LLM provider。

### 1. 配置环境变量

```bash
git clone <repo-url>
cd research-assistant
cp .env.example .env
```

至少确认：

```env
POSTGRES_PASSWORD=
DATABASE_URL=
SECRET_KEY=
DEFAULT_LLM_PROVIDER=
DEEPSEEK_API_KEY=
OPENAI_API_KEY=
ALIYUN_API_KEY=
```

搜索建议配置：

```env
TAVILY_API_KEY=
SERPER_API_KEY=
SEMANTIC_SCHOLAR_API_KEY=
OPENALEX_EMAIL=
CROSSREF_EMAIL=
PUBMED_API_KEY=
```

runtime-worker / Claude Code / DOCX 建议配置：

```env
PROJECT_RUNTIME_WORKER_ENABLED=true
PROJECT_RUNTIME_WORKER_URL=http://runtime-worker:8109
PROJECT_RUNTIME_WORKER_TOKEN=dev-runtime-worker-token
CLAUDE_CODE_BINARY=claude
CLAUDE_CODE_OUTPUT_FORMAT=stream-json
CLAUDE_CODE_DANGEROUSLY_SKIP_PERMISSIONS=true
```

Reader 高保真结构解析可选：

```env
PDF_LAYOUT_PARSER=document_mind
READER_DOCUMENT_MIND_ENABLED=true
DOCUMENT_MIND_ACCESS_KEY_ID=
DOCUMENT_MIND_ACCESS_KEY_SECRET=
```

如果没有 Document Mind：

```env
READER_DOCUMENT_MIND_ENABLED=false
PDF_LAYOUT_PARSER=markitdown
```

### 2. 启动服务

```bash
docker compose up -d --build backend frontend runtime-worker
```

常见依赖会一起启动：

- `postgres`
- `redis`
- `backend`
- `frontend`
- `runtime-worker`
- `codelab-runner`

访问地址：

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:3000 |
| 后端 API | http://localhost:8888 |
| FastAPI Docs | http://localhost:8888/docs |
| runtime-worker | http://localhost:8109 |
| PostgreSQL | `localhost:5432` |
| Redis | `localhost:6379` |

### 3. 启动后自检

```bash
docker compose ps
docker compose logs --tail 100 backend
docker compose logs --tail 100 runtime-worker
docker compose logs --tail 100 frontend
```

确认 Claude Code 和 document skills：

```bash
docker compose exec runtime-worker claude --version
```

如果 DOCX 生成不工作，优先检查：

- `runtime-worker` 是否启动。
- `claude` CLI 是否可用。
- 模型 API 是否支持 Claude Code agent loop。
- `/app/uploads/docx/{docx_id}` 内是否存在 `docx_inputs_manifest.json`。
- Claude 是否把大文件完整打印到了流式输出；合理方式是按路径读取和处理。

## 本地开发

前端：

```bash
cd frontend
npm ci
npm run dev
npm run lint
```

后端：

```bash
cd backend
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Docker 内验证：

```bash
docker compose exec -T backend python -m pytest tests/test_paper_grounding_tools.py -q
cd frontend && npm run lint
```

常用轻量检查：

```bash
python3 -m py_compile \
  backend/app/services/agent_tools_impl/registry.py \
  backend/app/services/react_agent.py \
  backend/app/services/docx_template_service.py \
  backend/app/services/document_artifact_service.py
```

## 关键数据目录

| 目录 | 内容 |
| --- | --- |
| `/app/uploads/literature_reviews/{review_id}` | 文献综述 workspace，含 `pdf/`、`md/`、`review/`。 |
| `/app/uploads/projects/{project_id}` | 论文复现 Project，含 `reference/`、代码仓库和运行产物。 |
| `/app/uploads/docx/templates/{template_id}` | DOCX 模板、附件、约束和分析产物。 |
| `/app/uploads/docx/artifacts/{conversation_id}/{artifact_id}` | Chat document artifact JSON。 |
| `/app/uploads/docx/{docx_id}` | DOCX 生成和精修工作区。 |
| `/tmp/claude-home-app/.claude/projects/...` | runtime-worker 内 Claude Code session 缓存。 |

## 关键工具清单

### 通用研究

- `web_search`
- `web_scrape`
- `knowledge_search`
- `literature_search`
- `paper_search`

### 文献综述

- `literature_review_start`
- `literature_review_download_pdf`
- `literature_review_pdf_to_markdown`
- `literature_review_read`
- `literature_review_search_zoekt`
- `review_writer`

### 论文复现 / Project

- `paper_research_prepare`
- `paper_research_status`
- `project_tree`
- `project_read_file`
- `project_write_file`
- `project_bash`
- `project_claude`
- `paper_research_search_project_zoekt`
- `paper_research_probe_repo`
- `paper_research_probe_url`

### 文档写作 / DOCX

- `activate_skill`
- `document_artifact_read`
- `document_artifact_update_block`
- `docx_generate_with_claude`
- `docx_refine_with_claude`

## 运行边界

- 公网搜索不是直接调用浏览器 Google；默认走 Tavily、Serper 和 DDGS fallback。
- PDF 下载必须遵守网站规则和开放获取边界；403/404 应跳过候选或换源。
- 文献综述默认追求足量可读全文论文，但实际数量取决于 PDF 可下载性和解析质量。
- 整篇论文全文翻译通常不适合单轮输出，应使用 Zoekt 定位原文片段或写入 artifact 限制说明。
- DOCX 高级排版依赖 Claude Code、document-skills、Pandoc、LibreOffice 和模型执行质量。
- Project 工具不能作为 DOCX 或文献综述失败后的兜底工具。

## 推荐先读

- `.agents/skills/literature-review/SKILL.md`
- `.agents/skills/paper-reproduction/SKILL.md`
- `docs/chat/CHAT_STABILITY_CHECKLIST_ZH.md`
- `docs/retrieval/DEVELOPMENT_BOUNDARY.md`
- `docs/LITERATURE_TEST_GUIDE.md`
- `docs/CONFIGURATION.md`

## License

MIT License
