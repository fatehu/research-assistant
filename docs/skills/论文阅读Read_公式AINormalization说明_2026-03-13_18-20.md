# 论文阅读 Read 公式 AI Normalization 说明

时间：2026-03-13 18:20

## 背景

当前 `/read` 的公式节点主要依赖：

- DocMind `type=formula` 的几何与块位置
- OCR/解析后的 `text`

这条链存在一个明确缺口：

- 几何真值可靠
- 公式文本语义不可靠
- 像上标、下标、希腊字母、范数、括号等，容易被 OCR 弄脏

典型误差：

- 上标被识成普通数字
- `θ` 被识成 `0`
- 范数、下标、函数名粘连

## 目标

在不改变证据真值的前提下，引入一层 `AI normalization`：

- 让 AI 基于当前页图、公式 layout、OCR 文本、style 信息，对公式做展示层规范化
- 输出：
  - `normalized_text`
  - `normalized_latex`
  - `normalization_reason`
  - `normalization_confidence`
- 不改变：
  - `uniqueId`
  - geometry
  - evidence/highlight

## 原则

1. geometry truth 仍然只认 DocMind
2. AI normalization 只改展示层，不改证据层
3. 正文显示优先使用 AI 产出的 `normalized_latex`
4. 原公式图片退回 fallback：
   - `normalized_latex` 可用时，正文先渲染 LaTeX
   - `normalized_latex` 不可用时，再回退到原公式截图
5. AI 规范化结果需要真正进入正文展示，而不只是折叠附加信息：
   - 便于用户理解
   - 便于后续 `/experience` 使用
   - 便于在 AI 上下文中复核

## 需要保留的 style 信息

当前 `layout_atoms` 不应只保留文本和 pos，还需要保留：

- layout alignment
- layout lineHeight
- block styleId

这些信息将作为 AI normalization 的辅助输入，帮助模型判断：

- 是否是上标/下标
- 是否是编号
- 是否是公式主体还是 trailing note

## 实施策略

1. 扩展 `page_grounding_v1.layout_atoms`
   - 保留 style 相关字段

2. 增加 equation normalization pass
   - 触发点：`group_kind == equation`
   - 输入：
     - 当前页渲染图
     - equation atoms
     - raw/clean text
     - block text + styleId + pos
   - 输出：
     - `normalized_text`
     - `normalized_latex`
     - `reason`
     - `confidence`

3. materialize 到 `EquationBlock`
   - 当 `normalized_latex` 存在且通过校验时，正文优先 `math-first`
   - 当 `normalized_latex` 不存在或为空时，正文回退 `image-first`
   - OCR transcript 继续保留为辅助信息
   - `normalized_latex` 与原因/置信度继续进入节点 props
   - 供 AI 上下文和后续 `/experience` 使用

4. 严格 fallback
   - AI 输出不合法，直接退回当前 transcript 方案
   - 不允许因为 normalization 失败而破坏公式节点

## 回退方式

如果本轮导致 `/read` 公式节点异常：

1. 回退本次提交
2. 恢复当前 image-first + transcript 方案
3. 保留新增文档，后续再以更小范围推进

## 本轮已实现

1. grounding style 字段已经保留到 `page_grounding_v1`
   - `layout_atoms[*].alignment`
   - `layout_atoms[*].line_height`
   - `layout_atoms[*].blocks[*].style_id`

2. `layout_uid_v1` 新增 equation normalization pass
   - 触发点：`group_kind == equation`
   - 输入：
     - 当前页渲染图
     - equation atoms
     - raw/clean text
     - alignment / line_height
     - block `style_id + pos`
   - 输出：
     - `normalized_text`
     - `normalized_latex`
     - `normalization_reason`
     - `normalization_confidence`
     - `normalization_mode`

3. `EquationBlock` 改为 AI LaTeX 优先
   - `normalized_latex` 可用时，正文主显示为 KaTeX 渲染结果
   - 原公式截图降为 fallback
   - OCR transcript 保留为折叠辅助信息
   - AI 规范化结果不再只放在折叠区，而是直接服务正文展示

4. AI 上下文可见变更痕迹
   - `/read` 的 `decision_log` 会追加：
     - `layout_uid_v1:equations_normalized=<count>`
   - 便于在右栏 `AI 决策` 中确认本页是否出现公式 normalization

## 2026-03-14 增补：FigurePanel AI Insight 改造

### 背景

当前 `/read` 的 `FigurePanel.ai_insight` 多数来自模板文案：

- 只是复述 caption
- 并不真正利用页图或图像内容
- 容易让用户误以为这是一条独立 AI 观察

### 目标

在不改动 evidence 和 ownership 的前提下，把 `/read` 的图像说明改成真正的 image-grounded refinement：

- 仍然沿用当前 `layout_uid_v1` 流程
- 只在 `group_kind == figure` 时追加一轮局部 refinement
- 输入：
  - 当前页图
  - figure group 的 caption / source label
  - source layout atoms 与 focus bbox
- 输出：
  - `ai_insight`
- 不改变：
  - `caption`
  - `source_layout_ids`
  - geometry / evidence

### 原则

1. `ai_insight` 是展示层增强，不是证据
2. caption 保留为原文/清洗后的说明，不让 AI 任意改写
3. `ai_insight` 必须是短句、图像导向、不能只是机械重复 caption
4. 如果 refinement 失败，优先留空，不再强行注入模板式“AI 深度洞察”

### 回退边界

- 只改 `/read` `layout_uid_v1` 的 figure refinement
- 不改 `/experience` / `/workbench`
- 不改 `FigurePanel` 组件协议，只继续使用 `props.ai_insight`
- 不改 evidence preview 主链
# 2026-03-14 增补：page 86 / page 4 最小回退边界

- 只修两点：
  - `EquationBlock` 从 equation atom 独立 block 中恢复编号标签，例如 `(1)`。
  - `FigurePanel` 对多子图 / mixed figure 不再优先拼原生 PDF 图片；默认改走区域渲染，并对 display crop 施加轻微外扩，避免漏掉边角标题。
- 不改：
  - evidence preview 主链
  - grounding 坐标基准
  - 其他 `/read` normalize 逻辑
