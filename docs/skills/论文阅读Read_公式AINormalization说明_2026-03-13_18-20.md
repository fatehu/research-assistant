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
3. 正文显示仍保持 `image-first`
4. AI 规范化结果只作为补充：
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
   - 主显示继续 image-first
   - `normalized_latex` 作为补充信息进入节点 props
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

3. `EquationBlock` 仍保持 image-first
   - 正文主显示继续是公式截图
   - OCR transcript 保留为折叠辅助信息
   - AI 规范化结果也以折叠区显示，不替代证据图

4. AI 上下文可见变更痕迹
   - `/read` 的 `decision_log` 会追加：
     - `layout_uid_v1:equations_normalized=<count>`
   - 便于在右栏 `AI 决策` 中确认本页是否出现公式 normalization
