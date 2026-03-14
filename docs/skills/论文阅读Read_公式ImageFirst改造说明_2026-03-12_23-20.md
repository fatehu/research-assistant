# 论文阅读 `/read` 公式 `image-first` 改造说明（2026-03-12 23:20）

## 背景

当前 `/read` 的 `EquationBlock` 存在两个结构性问题：

1. 正文区域会主动补一张“公式证据”裁图，同时右侧 `AI 上下文` 也会显示同一份 evidence preview，形成重复。
2. 公式主显示依赖 DocMind 的 OCR 文本拼成 `latex` 后再走 KaTeX，但当前 DocMind 原始公式对象并不提供标准 LaTeX，只提供：
   - `type=formula`
   - `subType=formula`
   - `pos`
   - `blocks[].pos`
   - OCR/解析后的 `text`

以 `paper 85 / page 3` 为例，原始对象的核心字段是：
- `uniqueId = a42c09bde8f073da94b7af9ad7cb35c4`
- `type = formula`
- `subType = formula`
- `text = minEx~DcaliblfFp(x)-fquant(0x)||,(1)0`

这说明：
- 几何真值可靠，适合裁图和高光。
- 文本语义不可靠，不适合作为主显示的公式语义来源。

## 目标

将 `/read` 的公式显示收敛为：

- **image-first**
  - 正文主显示优先使用公式裁图。
- **DocMind geometry-first**
  - 高光与 evidence preview 仍只认 DocMind `uniqueId -> blocks[].pos`。
- **OCR transcript optional**
  - OCR 文本只作为辅助转写，不再默认直接走 KaTeX 主显示。

## 不做的事

- 不修改 `/read` 的全局 evidence preview 主路径。
- 不继续尝试从低质量 OCR 文本强行恢复高保真 LaTeX。
- 不影响正文、表格、普通段落的现有 evidence 链路。

## 这轮预期改动

### Backend

- `EquationBlock` props 增加轻量显示语义：
  - `render_mode`
  - `transcript`
- `latex` 保留兼容，但不再默认作为 `/read` 主显示依据。
- `where ...` 说明继续单独拆到 `description`。

### Frontend

- `EquationBlockNode` 默认按 `image-first` 渲染：
  - 不再出现额外的“公式证据”卡片标题。
  - 不再把低质量 OCR 文本直接强行渲成 KaTeX 主体。
- 若 evidence image 可用：
  - 正文直接显示公式图。
- 若 evidence image 不可用：
  - 才退回 transcript/latex 的文本降级。

### 2026-03-12 23:37 增量收口

- 正文公式图与右侧 evidence preview 不再复用同一套裁图语义。
- `display_formula` 变体只服务正文展示：
  - 按公式 `uniqueId` 的 polygon/bbox 紧裁
  - 去掉整页比例保底造成的大块上下文
  - 不绘制高亮覆盖层
- 右侧 `AI 上下文` 仍使用默认 evidence preview：
  - 保留高亮覆盖
  - 保留核验语义

## 回退边界

如果这轮实现出现问题，优先回退以下文件的本轮改动：

- `backend/app/services/literature_reader_compose_service.py`
- `backend/app/services/reader_component_contract_service.py`
- `backend/app/services/reader_single_agent_validator.py`
- `frontend/src/pages/literature/readerComponents/index.tsx`
- `frontend/src/pages/literature/readerComponents/schemas.ts`

## 手动验收页

- `http://localhost:3000/literature/85/read?page=3&kb=84`

## 验收标准

1. 正文区域不再出现“公式证据”重复卡片语义。
2. 公式主显示优先是裁图，而不是错误的 KaTeX。
3. 右侧 `AI 上下文` 的 evidence preview 继续可用。
4. `证据` 菜单、hover preview、pinned evidence 不受影响。
