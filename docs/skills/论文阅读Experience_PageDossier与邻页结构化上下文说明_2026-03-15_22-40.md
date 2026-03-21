## 背景

`/experience` 原先虽然已经接入前后页 `VL-flash` 参考，但输入仍然偏弱：

- 邻页上下文基本只有 `text`
- runtime prompt 把邻页严格限制成 `reference-only`
- `/workbench` 无法直接看到这条输入链

这不符合当前目标：让 `/experience` 和 `/workbench` 基于当页 payload、前后页结构化参考、agent/tool/mcp 自主构建内容丰富的网页。

## 这轮改动

### 1. 邻页 `VL-flash` 输出升级为结构化上下文

`backend/app/api/literature.py`

- `_extract_adjacent_page_reference_text(...)` 不再只返回单个 `text`
- 现在返回：
  - `summary`
  - `body_text`
  - `figures[]`
  - `tables[]`
  - `equations[]`
  - `continuation_hints[]`
  - `raw_text`

目标是让邻页不再只是 OCR 补丁，而是带图片/表格/公式描述的 continuity context。

### 2. 新增 `page_dossier`

`backend/app/api/literature.py`

- `_build_experience_page_dossier(...)` 会把：
  - 当前页 compose 基本信息
  - enrichment targets
  - assets 摘要
  - quality 摘要
  - 结构化 `adjacent_page_context`
  组装成一个统一的 `page_dossier`

### 3. runtime prompt 真正消费 `page_dossier`

`backend/app/services/generative_reader_agent_runtime.py`

- `build_plan(...)` 新增 `page_dossier`
- `_build_agent_prompt(...)` 把 `adjacent_page_context` 和 `page_dossier` 一起喂给 agent
- prompt 从“邻页只能弱参考”调整成：
  - 当前页仍是 anchor
  - 但邻页结构化信息可以强参与 continuity、figure/table explanation、page narrative planning

### 4. `/workbench` 可见

`frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx`

- 新增：
  - `Page Dossier`
  - `Adjacent Page Context`
- 这样可以直接看到：
  - 当前页 dossier
  - 上一页/下一页的结构化文本、图片描述、表格描述、公式描述、承接提示

### 5. `/experience` 与 `/workbench` 增加运行期可观测性

这轮继续把“能不能上线”所需的可观测性补齐：

- `backend/app/services/generative_reader_agent_runtime.py`
  - `build_experience_plan(...)` 会把这些信息写进 `experiencePlan.meta`
    - `resource_strategy`
    - `used_tools`
    - `tool_trace_summary`
    - `adjacent_page_context` 的轻量摘要
- `frontend/src/pages/literature/PaperReaderExperiencePage.tsx`
  - `页面生成细节` 里新增：
    - 资源策略
    - 邻页参考
    - Tool Trace
- `frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx`
  - `Page Dossier` 不再只有 raw JSON
  - 新增 dossier 摘要卡
  - 新增 `Tool Trace` 面板

目标是让 `/experience` 和 `/workbench` 不只“能生成”，还要能解释：

- 为什么这样构页
- 用了哪些工具
- 邻页输入有没有真正参与
- 当前页 dossier 到底包含了什么

## 合同变化

### Backend schema

`backend/app/schemas/literature.py`

- 新增 `ReaderAdjacentPageItem`
- 新增 `ReaderAdjacentPageContext`
- `ReaderGenerativePlanResponse`
  - 新增 `adjacent_page_context`
  - 新增 `page_dossier`
- `ReaderExperiencePlanResponse`
  - 新增 `adjacent_page_context`
  - 新增 `page_dossier`

### Frontend API types

`frontend/src/services/api.ts`

- 新增 `ReaderAdjacentPageItem`
- 新增 `ReaderAdjacentPageContext`
- 对应 response 类型新增：
  - `adjacent_page_context`
  - `page_dossier`

## 当前边界

这轮还没有把 `/experience` 完全改造成“自由网页生成器”。

当前达成的是：

- 邻页不再只是 `text`
- `/experience` runtime 现在能吃更强的 dossier 输入
- `/workbench` 能直接 inspect 这条输入链

还没做的是：

- page dossier 驱动的 staged runtime（planner -> enricher -> formatter）
- 更激进的 agent/tool/mcp 页面生成自由度
- 更丰富的 `/experience` block/layout 扩展
