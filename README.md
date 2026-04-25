# AI 科研助手 / Research Assistant

面向科研工作的全栈 AI 研究平台。当前系统已经不只是“聊天 + 知识库 + 论文阅读”，而是围绕科研任务形成了从选题调研、论文综述、单篇论文解读、代码复现、实验验证、结构化写作、DOCX 导出与二次调优的工作台。

当前业务主线可以概括为：

1. 根据用户诉求联网检索，例如公网搜索、OpenAlex、Semantic Scholar、arXiv、PubMed、CrossRef，整理重点并回答，相当于做一个可追溯来源的研究综述。
2. 解读单篇论文，包括 PDF 阅读、结构化阅读页、证据定位、页面级 Generative UI 和论文问答。
3. 围绕一篇论文建立复现 Project，把论文正文、解读、仓库 intake 和运行环境交给 Claude Code worker，完成代码复现、运行、调试和优化。
4. 根据用户诉求结合学术检索提出创新设计方法，再通过 Project / CodeLab / Claude worker 做进一步验证。
5. 支持自定义撰写模板，例如国基、科研项目、教研项目。用户可以上传成品模板、撰写说明、普通参考附件，平台分析出 Markdown 生成约束和 DOCX 生成约束，再生成可编辑的结构化 artifact。
6. 支持 Word / DOCX 导出，并支持在已有 docx_id 工作区内继续让 Claude 修改、调优和重新导出。

## 当前相对旧 README 的主要增量

旧 README 主要描述了 Reader Workbench V2、知识库、文献管理和 CodeLab。当前代码又增加或强化了这些能力：

| 增量 | 现在的状态 |
| --- | --- |
| Agent Skill 控制面 | `.agents/skills/` 下有 `literature-review`、`paper-reproduction`、`paper2code`，skill 激活后有 session system prompt 和工具边界。 |
| 文献综述工作流 | `literature-review` skill 可以创建 review workspace、搜索论文、下载 PDF、PDF 转 Markdown、生成单篇 review、合成 final review。 |
| 文献综述管理页面 | `/literature-reviews` 展示 `/app/uploads/literature_reviews/{review_id}`，可浏览 `review/*.md`、JSON/MD 文件并下载。 |
| 论文复现 Project | `/projects`、`paper_research_prepare/status`、`project_claude`、Project Zoekt 等工具贯通论文到代码落地。 |
| runtime-worker / Claude Code | 独立 `runtime-worker` 运行 Claude Code、Bash、DOCX Claude；支持流式输出、session 续用、document-skills plugin。 |
| DOCX 模板管理 | `/templates` 管理模板、附件、默认 DOCX 样式提示词；支持 Pandoc、LibreOffice、OOXML 解析。 |
| 文档 artifact | Chat 右侧结构化文档工作台，按 block 读写、局部改写、选择 block 发送给主 Agent。 |
| DOCX 生成 / 精修 | `docx_generate_with_claude` 用路径清单生成 DOCX；`docx_refine_with_claude` 在已有 docx_id 目录继续修改 DOCX。 |
| Generative UI | Reader Workbench / Experience 页面基于 page dossier、generative plan、renderer contract 生成页面级阅读体验。 |
| 上下文管理 | `context-preview`、`context_state`、`turn_store`、`item_stream`、compaction、citation_index、artifact_updated 已进入主聊天链路。 |
| 引用和工具流展示 | 工具结果、Claude JSONL 流、引用来源、artifact 更新都通过 SSE 和前端 store 做实时投影。 |

## 产品能力地图

### 1. 联网调研与研究综述

用户可以直接在 Chat 中提出研究问题，Agent 会根据工具池选择公网搜索、学术检索或知识库检索。

技术上分为两条检索路径：

| 路径 | 工具 / 服务 | 说明 |
| --- | --- | --- |
| 公网搜索 | `web_search`、`web_scrape` | `web_search` 优先 Tavily，再 Serper，再 DDGS。Serper 使用 Google Serper API。适合查网页、新闻、项目主页、资料页。 |
| 学术检索 | `literature_search` | 支持 `auto`、`openalex`、`semantic_scholar`、`arxiv`、`pubmed`、`crossref`。支持年份、领域、开放获取、排序、分页 token。 |
| 知识库检索 | `knowledge_search` | 面向用户上传资料和知识库，支持向量、全文、混合检索、Reranker、query rewrite、contextual compression。 |

文献综述的专用 skill 是 `literature-review`：

1. `literature_review_start` 创建 `/app/uploads/literature_reviews/{review_id}`。
2. `literature_search` 检索候选论文。
3. `literature_review_download_pdf` 保存 PDF 到 `pdf/`。
4. `literature_review_pdf_to_markdown` 把 PDF 转为完整 Markdown，落盘到 `md/`，不把全文塞进普通上下文。
5. `review_writer mode=paper` 生成每篇论文的 `review/{paper_key}.md`。
6. `review_writer mode=final` 读取所有单篇 review，结合主题生成 `review/final.md`。
7. `literature_review_read` 只读取成品 review Markdown。
8. `literature_review_search_zoekt` 在 `md/*.md` 和 `review/*.md` 中做定向证据检索。

关键边界：

- `md/*.md` 是论文全文 Markdown，默认不直接进上下文。
- `review/*.md` 是 LLM 生成的单篇综述和最终综述，是回答综述问题时的优先读取对象。
- `md/*.json` 是 PDF-to-Markdown 解析报告，不是综述 JSON。
- 整篇论文全文翻译不适合单轮输出，应该用 Zoekt 定向定位原文片段或在 artifact 中写明限制。

### 2. 单篇论文解读

文献模块支持从搜索结果或链接入库：

- OpenAlex / Semantic Scholar / arXiv / PubMed / CrossRef 搜索。
- DOI、arXiv、PubMed、OpenAlex、Semantic Scholar、期刊详情页链接解析。
- PDF 下载、收藏夹、分类管理、PDF 阅读。
- 论文加入知识库或保持为文献库资源，避免强制和知识库绑定。

Reader 侧有几层体验：

| 页面 | 路由 | 说明 |
| --- | --- | --- |
| 基础阅读 | `/literature/:paperId/read` | PDF 阅读、页面跳转、注释、评论、入库、问答入口。 |
| Reader Workbench | `/literature/:paperId/read/workbench` | 页面结构、相邻页上下文、组件化阅读。 |
| Experience V2 | `/literature/:paperId/experience-v2` | 面向用户的生成式阅读体验页。 |
| Workbench V2 | `/literature/:paperId/workbench-v2` | 调试/评审 generative plan、page dossier、contract 的工作台。 |
| Review / Publish | `/literature/:paperId/read/review` | 人工观察、patch、auto-patch、发布快照。 |

核心技术链路：

1. PDF 解析为页面结构和文本块。
2. 后端构建 page dossier，包括当前页、相邻页上下文、关键 claims、术语、图表和素材。
3. Generative reader runtime 生成受约束的 plan，而不是自由 HTML。
4. 后端做 renderer contract 校验、锚点校验、fallback。
5. 前端通过 `GenerativeExperienceRenderer` 和相关组件渲染。

关键文件：

- `backend/app/api/literature.py`
- `backend/app/services/generative_reader_agent_runtime.py`
- `backend/app/services/generative_reader_agent_core.py`
- `backend/app/services/generative_reader_agent_tools.py`
- `backend/app/services/literature_reader_compose_service.py`
- `backend/app/services/reader_compose_agent_runtime.py`
- `frontend/src/pages/literature/PaperReaderPage.tsx`
- `frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx`
- `frontend/src/pages/literature/PaperReaderExperienceV2Page.tsx`
- `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`

### 3. 论文复现与代码落地

论文复现不再让主 Agent 自己乱跑命令，而是 Project-first：

1. 用户从论文页或 Chat 发起复现。
2. `paper_research_prepare` 创建或复用 Project。
3. Project 根目录固定为 `/app/uploads/projects/{project_id}`。
4. prepare 生成 reference bundle：
   - `reference/paper/paper_pdf2md.md`
   - `reference/paper/paper_interpretation.md`
   - `reference/paper/paper_interpretation.json`
   - `reference/repo/readme_intake.json`
5. 主 Agent 是策划者：负责论文理解、复现准备、方案决策、风险判断、下一步指令。
6. Claude Code 是实施 worker：通过 `project_claude` 在 Project 目录内改代码、跑命令、调试、交付结果。
7. Project Zoekt 用于定向搜索项目文件，不是目录浏览器。

关键工具：

| 工具 | 用途 |
| --- | --- |
| `paper_search` | 查找已保存论文。 |
| `paper_research_prepare` | 创建/准备 Project 和 reference bundle。 |
| `paper_research_status` | 读取 Project/readiness 状态。 |
| `project_tree` | 查看 Project 目录结构。 |
| `project_read_file` | 读取 Project 内已知文件。 |
| `project_write_file` | 写入 Project 内文件，主要用于受控场景。 |
| `project_claude` | 调 Claude Code worker 完成真正的代码实施。 |
| `paper_research_search_project_zoekt` | 在 Project 内做高性能文本搜索。 |
| `paper_research_probe_repo` | 探测仓库 URL。 |
| `paper_research_probe_url` | 探测下载/文档 URL。 |

关键约束：

- Project 工具只用于论文复现、代码优化、代码编写。
- DOCX 生成、文献综述、模板管理、普通文件查看不能 fallback 到 Project 工具。
- 如果 Claude Code 不可达，主 Agent 应报告阻塞，不应自己替代执行训练或代码改动。

关键文件：

- `.agents/skills/paper-reproduction/SKILL.md`
- `.agents/skills/paper-reproduction/skill.yaml`
- `backend/app/services/project_service.py`
- `backend/app/services/project_reference_builder_service.py`
- `backend/app/services/project_runtime_service.py`
- `backend/app/services/paper_experiment_service.py`
- `backend/app/services/agent_tools_impl/registry.py`
- `frontend/src/pages/projects/ProjectsPage.tsx`

### 4. 创新设计与验证

创新设计不是单独一个页面，而是由现有能力组合出来的研究闭环：

1. 先用 `web_search` / `literature_search` 查公开资料和学术论文。
2. 用文献综述 skill 建立主题级材料池。
3. 对关键论文进入 Reader / Project。
4. 主 Agent 整理研究空白、可做创新点、实验假设。
5. 需要代码验证时进入 Project 或 CodeLab。
6. Claude Code worker 或 Notebook Agent 执行实验、调试、记录结果。
7. 再回到 Chat / artifact / DOCX 模板生成报告、申报书或研究方案。

这个链路的关键是主 Agent 和 worker 分层：

- 主 Agent：负责检索、判断、计划、上下文、证据和最终解释。
- Claude Code worker：负责文件修改、命令运行、代码调试、结果产出。
- Bash / probe 工具：只做环境探测、路径确认、轻量检查，不作为长期实施主体。

### 5. 模板管理、结构化写作和 DOCX

模板管理页面在 `/templates`。

模板目录：

- DOCX 根目录：`/app/uploads/docx`
- 模板目录：`/app/uploads/docx/templates`
- 文档 artifact：`/app/uploads/docx/artifacts/{conversation_id}/{artifact_id}/artifact.json`
- DOCX 生成工作区：`/app/uploads/docx/{docx_id}`

模板附件分三类：

| 类型 | 作用 |
| --- | --- |
| 成品/样例模板 `sample_template` | 用来分析版式、标题、页眉页脚、目录、页码、表格样式等，主要影响 DOCX 生成约束。 |
| 撰写说明/填报指南 `writing_guide` | 用来抽取章节结构、写作要求、字数、内容边界，主要影响 Markdown 生成约束。 |
| 普通参考附件 `reference` | 生成时交给 Claude 参考，不主动总结为强约束。 |

分析链路：

1. 用户上传附件。
2. 用户点击分析。
3. 后端用 Pandoc / LibreOffice / OOXML 解析 DOC、DOCX、Markdown、文本等文件。
4. LLM 生成两类约束：
   - `md_constraints`：给主 Agent 生成内容和 artifact schema 用。
   - `docx_constraints`：给 Claude document skill 生成 Word 样式用。
5. 用户可以编辑这些约束。
6. Chat 中选择模板，生成结构化 document artifact。
7. 主 Agent 通过 `document_artifact_read` / `document_artifact_update_block` 读写 block。
8. 用户可以局部改写 block，也可以选择若干 block 作为下一轮消息上下文。
9. 最后通过 Claude document skill 导出 DOCX。

文档 artifact 的思想：

- 不直接把长文当一坨 Markdown。
- 先把模板抽成 section / block schema。
- 每个 block 有 `block_id`、标题、层级、字数、约束、Markdown 内容。
- 主 Agent 可以按 block 读取、修改、生成。
- 前端右侧 `DocumentArtifactPanel` 提供可编辑面板、Markdown 预览、局部改写和 block 选择。

DOCX 生成工具：

| 工具 | 用途 |
| --- | --- |
| `docx_generate_with_claude` | 创建或使用 `/app/uploads/docx/{docx_id}`，把 artifact_path、template_file_paths、requirements_path 写进 `docx_inputs_manifest.json`，交给 Claude Code + document-skills/docx 生成 DOCX/PDF。 |
| `docx_refine_with_claude` | 在已有 docx_id 目录内继续修改现有 DOCX，适合修目录、页码、样式、封面、参考文献和导出 PDF。 |

关键设计：

- 平台不直接复刻官方 document skill。
- 平台准备工作目录、输入路径、模板约束和输出路径。
- Claude Code 负责调用官方 `document-skills:docx` 工作流完成复杂 Word 生成。
- 大文件不直接塞进 prompt；通过 `docx_inputs_manifest.json` 传路径，避免流式输出被超长 JSONL 打爆。

关键文件：

- `backend/app/api/docx_templates.py`
- `backend/app/models/docx.py`
- `backend/app/services/docx_template_service.py`
- `backend/app/services/docx_runtime_service.py`
- `frontend/src/pages/templates/TemplateManagementPage.tsx`
- `frontend/src/pages/chat/components/DocumentArtifactPanel.tsx`

### 6. Word / DOCX 导出

DOCX 导出依赖 `runtime-worker`：

- `runtime-worker` 容器内安装 Claude Code。
- Claude Code 配置官方 `document-skills` plugin。
- 工作目录固定为 `/app/uploads/docx/{docx_id}`。
- 平台生成：
  - `docx_inputs_manifest.json`
  - `requirements.md`
  - `default_docx_style_prompt.md`
  - `template_md_constraints.md`
  - `docx_request.json`
- Claude 输出：
  - `generated_document.docx`
  - `generated_document.pdf`，如果环境支持。

如果需要后续修改，使用 `docx_refine_with_claude`，它会在同一个 docx_id 目录写入 `docx_refine_request.json`，默认继续该目录的 Claude session。

## Generative UI 完成情况

平台目前有两类 Generative UI。

### Reader Generative UI

这是论文阅读页的生成式界面。

输入：

- PDF 页面结构。
- page dossier。
- 相邻页上下文。
- 图表和资源包。
- 用户选择的 reader profile。

输出：

- 受约束的 generative plan。
- story substrate。
- page brief。
- contract validation。
- renderer 可执行的组件树。

前端不执行任意 HTML，而是渲染白名单组件。

关键页面：

- `/literature/:paperId/experience-v2`
- `/literature/:paperId/workbench-v2`

### Chat Document Artifact UI

这是结构化写作的生成式界面。

输入：

- 模板生成的 section / block schema。
- 用户需求。
- 主 Agent 对每个 block 的生成或修改。

输出：

- 可编辑的 document artifact。
- 每个 block 可被选择、局部改写、预览、保存。
- 可转交 `docx_generate_with_claude` 导出 Word。

关键页面：

- `/chat/:conversationId`
- 右侧 `DocumentArtifactPanel`

## 复杂上下文管理机制

Chat 不是简单把历史消息全部塞给模型。当前有多层上下文管理：

| 层 | 作用 |
| --- | --- |
| `context-preview` | 发送前预估本轮上下文、工具、RAG、skill prompt、token 预算。 |
| `context_state` | 会话级状态，例如当前主题、目标、已确认事实、未解决问题。 |
| `turn_store` | 一轮用户请求触发的一次完整回合，包括推理、工具、结果。 |
| `item_stream` | 回合内细粒度事件流，例如 user message、assistant message、tool call、tool result、compact boundary。 |
| `workflow_control` | 控制继续、等待、分支、手动确认等工作流行为。 |
| `citation_index` | 回答中 `[网页X]` / `[来源X]` 的来源索引，用于前端引用说明。 |
| `artifact_updated` | 工具更新 document artifact 后通过 SSE 实时 patch 前端状态。 |
| compaction | 长对话压缩，只吃事实层，避免把旧 debug/react_steps 当作事实继续污染上下文。 |
| tool output truncation | 工具结果可按全局预算截断进入上下文，保留可读摘要，避免超 token。 |

这套机制解决的问题：

- 长对话不因为无限历史而崩。
- 工具流和最终回答可以分开展示。
- Claude worker 工作时前端能看到进度。
- artifact block 更新可以实时同步到当前会话。
- 引用说明可以从当前回答和历史 citation index 回填。

关键文件：

- `backend/app/api/chat.py`
- `backend/app/services/react_agent.py`
- `backend/app/services/chat_context_store.py`
- `backend/app/services/conversation_context_compaction_service.py`
- `frontend/src/stores/chatStore.ts`
- `frontend/src/pages/chat/components/ContextDebugWindow.tsx`
- `frontend/src/pages/chat/components/TurnTimeline.tsx`
- `frontend/src/pages/chat/components/TurnProcessLanes.tsx`

## 主 Agent 与 Worker 协作结构

当前系统不是一个 Agent 干所有事，而是分层协作。

```text
用户
  ↓
主 Agent / React Agent
  - 理解诉求
  - 激活 skill
  - 选择工具
  - 管理上下文
  - 维护引用和 artifact
  - 向用户解释结果
  ↓
受限工具层
  - literature_search / web_search / knowledge_search
  - document_artifact_read/update_block
  - paper_research_prepare/status
  - docx_generate/refine_with_claude
  ↓
Worker 层
  - project_claude: 代码复现、运行、调试
  - docx Claude: Word 生成和修改
  - codelab-runner: Notebook/Python 执行
  - PDF-to-Markdown / Zoekt / Pandoc / LibreOffice
```

关键原则：

- 主 Agent 是策划者、研究员和调度者。
- Claude Code worker 是实施者。
- Project 工具不能被 DOCX 和文献综述误用。
- 文献综述工具不能被 Project 语义污染。
- DOCX 工具不处理论文复现。
- 大文件走路径和 workspace，不走 prompt 全文。

## 系统架构

```text
research-assistant/
├── backend/
│   ├── app/
│   │   ├── api/                         # FastAPI 路由
│   │   │   ├── chat.py                  # Chat SSE、context-preview、branch、artifact
│   │   │   ├── literature.py            # 文献搜索、阅读、Reader/Experience
│   │   │   ├── literature_reviews.py    # 文献综述工作区浏览
│   │   │   ├── docx_templates.py        # DOCX 模板管理
│   │   │   ├── projects.py              # 复现 Project 管理
│   │   │   └── codelab.py               # Notebook / CodeLab
│   │   ├── models/                      # SQLAlchemy 模型
│   │   ├── schemas/                     # Pydantic Schema
│   │   ├── services/
│   │   │   ├── react_agent.py           # 主 Agent loop、工具调用、上下文策略
│   │   │   ├── agent_skill_service.py   # skill 加载和 session prompt 注入
│   │   │   ├── agent_tools_impl/        # 工具注册和实现
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
│   │   └── runtime_worker/              # runtime-worker FastAPI app
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
| `/dashboard` | 统一入口 / Command Center。 |
| `/chat`、`/chat/:conversationId` | 主 Agent 对话、工具流、上下文窗口、document artifact。 |
| `/chat/manage` | 对话管理。 |
| `/knowledge`、`/knowledge/:kbId` | 知识库管理、文档、RAG。 |
| `/knowledge/:kbId/chunking` | Smart Chunking 配置。 |
| `/literature` | 文献搜索、收藏、PDF 下载、分类。 |
| `/literature/:paperId/read` | 单篇论文阅读。 |
| `/literature/:paperId/experience-v2` | 生成式论文阅读体验页。 |
| `/literature/:paperId/workbench-v2` | Generative Reader 调试/工作台。 |
| `/literature/:paperId/read/review` | Reader Review / Publish。 |
| `/literature-reviews` | 文献综述 workspace 管理。 |
| `/projects`、`/projects/:projectId` | 论文复现 Project 管理和文件树。 |
| `/templates` | DOCX 模板管理。 |
| `/code`、`/code/:notebookId` | CodeLab Notebook。 |
| `/admin/*`、`/mentor/*`、`/student/*` | 分角色管理与协作页面。 |

## 关键 API

| 方法 | 端点 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/chat/send` | 主聊天 SSE。 |
| `POST` | `/api/v1/chat/context-preview` | 发送前上下文预览。 |
| `POST` | `/api/v1/chat/conversations/{id}/branch` | 对话分支，复制当前 session 相关内容。 |
| `POST` | `/api/v1/chat/conversations/{id}/compact` | 手动压缩上下文。 |
| `GET/POST` | `/api/v1/chat/conversations/{id}/document-artifact` | 读取/创建当前对话 artifact。 |
| `PATCH` | `/api/v1/chat/conversations/{id}/document-artifact/blocks/{block_id}` | 更新 artifact block。 |
| `POST` | `/api/v1/chat/conversations/{id}/document-artifact/blocks/{block_id}/rewrite-span` | artifact 局部改写。 |
| `GET` | `/api/v1/literature/search` | 学术搜索。 |
| `POST` | `/api/v1/literature/papers/{paper_id}/download-pdf` | 下载 PDF。 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/stream` | Reader Workbench SSE。 |
| `POST` | `/api/v1/literature/papers/{paper_id}/reader/composed/review-session` | Reader review session。 |
| `GET` | `/api/v1/literature-reviews/overview` | 文献综述工作区列表。 |
| `GET` | `/api/v1/literature-reviews/{review_id}` | 文献综述工作区详情。 |
| `GET` | `/api/v1/literature-reviews/{review_id}/files/content` | 读取综述文件内容。 |
| `GET` | `/api/v1/projects` | Project 列表。 |
| `GET` | `/api/v1/projects/{project_id}/folder-tree` | Project 文件树。 |
| `GET` | `/api/v1/docx/templates/overview` | 模板和 DOCX 工作区概览。 |
| `POST` | `/api/v1/docx/templates` | 新建/更新模板。 |
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
| DOCX | Claude Code document-skills/docx、python-docx/Pandoc/OOXML 解析辅助。 |
| 搜索索引 | Zoekt 用于 Project 和文献综述 Markdown 检索。 |
| 部署 | Docker Compose，默认开发模式前端热更新。 |

## 快速开始

### 前置要求

- Docker Desktop 或 Docker Engine + Docker Compose。
- 建议 8 GB 以上内存。
- Windows PowerShell 建议先执行 `chcp 65001`。
- 至少配置一个可用 LLM provider。

### 1. 克隆并配置环境变量

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

学术和公网搜索建议配置：

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

访问：

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

确认 Claude Code / document skills：

```bash
docker compose exec runtime-worker claude --version
```

如果 DOCX 生成不工作，优先检查：

- `runtime-worker` 是否启动。
- `claude` CLI 是否可用。
- 模型 API 是否能支持 Claude Code agent loop。
- `/app/uploads/docx/{docx_id}` 内是否有 `docx_inputs_manifest.json`。
- Claude 是否输出了过长 Read 结果；正确方式是让它按路径读取，不把大文件打印到流式输出。

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

当前常用轻量检查：

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
| `/app/uploads/projects/{project_id}` | 论文复现 Project，含 `reference/`、代码仓库、运行产物。 |
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

## 已知边界

- 公网搜索不是直接调用浏览器 Google；目前主要是 Tavily、Serper 和 DDGS fallback。
- PDF 下载必须遵守网站规则和开放获取边界；403/404 通常应跳过候选或换源，不应批量硬抓。
- 文献综述默认做 12 篇可读全文论文，但实际数量取决于 PDF 可下载性和解析质量。
- 整篇论文全文翻译容易超过模型输出预算，当前建议做定向片段翻译或写入限制说明。
- DOCX 复杂排版依赖 Claude Code + document-skills、Pandoc、LibreOffice 和模型执行质量。
- `multi` 学术搜索不是默认强推路径；当前更推荐 `auto` 或指定官方 API 数据源。
- Project 工具不能作为 DOCX/综述失败后的兜底工具。

## 推荐先读

- `.agents/skills/literature-review/SKILL.md`
- `.agents/skills/paper-reproduction/SKILL.md`
- `docs/chat/CHAT_STABILITY_CHECKLIST_ZH.md`
- `docs/retrieval/DEVELOPMENT_BOUNDARY.md`
- `docs/LITERATURE_TEST_GUIDE.md`
- `docs/CONFIGURATION.md`

## License

MIT License
