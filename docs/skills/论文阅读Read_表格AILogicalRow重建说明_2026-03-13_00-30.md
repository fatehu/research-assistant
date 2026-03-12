# 论文阅读 Read 表格 AI Logical Row 重建说明

时间：2026-03-13 00:30

## 背景

`/read` 已切到 `layout_uid_v1` 主链，表格的最小真值已经收敛为：

- `docmind_structure.layouts[type=table]`
- `cells[]`
- `uniqueId -> blocks[].pos`

但 `paper 85 / page 7` 这类 benchmark table 暴露出一个更深层问题：

- DocMind 给出的 `cells[]` 和几何 truth 足够好
- 但它表达的是 **physical rows**
- 不是最终用户看到的 **logical rows**

典型表现：

- 一行 benchmark name + reported value
- 下一物理行才给出 local eval / uncertainty
- 从表观上看它们属于同一个逻辑 benchmark row
- 但 DocMind 并没有把它们编码成一个 row group

因此，单纯依赖 deterministic cell grid 仍会出现：

- 行数变多
- 数值错位
- `±` 行和数值行无法正确配对

## 目标

为 `/read` 增加一层 **AI-assisted table logical-row reconstruction**：

- AI 只负责决定：
  - physical rows 如何并成 logical rows
  - 哪些 logical rows 属于 header / data / note
- AI 不负责：
  - 改写 cell 文本
  - 发明几何
  - 修改 `uniqueId` ownership
  - 修改高光来源

换句话说：

- **DocMind 负责 geometry truth**
- **AI 负责 table semantics**

## 新流程

### 1. DocMind 真值层

输入：

- `table_cells`
- `layout_id(uniqueId)`
- `blocks[].pos`
- 当前页渲染图

这一层只做 Python 整理，不做 AI 推理。

产物：

- `physical_rows`
  - 每个 physical row 只保留：
    - `row_index`
    - `cells[]`
    - `col_start / col_end`
    - `text`
    - `layout_ids`

### 2. AI 逻辑重建层

模型：

- `qwen3.5-plus`

输入：

- 当前 table 的 `physical_rows`
- caption / table label
- 当前页渲染图

输出：

- `logical_rows`
  - 每一项只包含：
    - `logical_row_id`
    - `row_role`
      - `header`
      - `data`
      - `note`
    - `source_row_indices`
    - `rationale`

约束：

- 每个 `source_row_index` 必须 **exactly once**
- 不允许改写 cell 文本
- 不允许发明 cell
- 不允许修改 geometry
- 不允许改变 `layout_ids`

### 3. Deterministic materialization

AI 只给 grouping。

真正的表格仍由 deterministic 代码重建：

- 按 `logical_rows` 合并 physical rows
- 保留 `cells[]`
- 继续用 `rowspan / colspan`
- 继续用 `uniqueId -> blocks[].pos`

### 4. Evidence / Highlight

证据链完全不变：

- 整表证据继续是主路径
- 高光继续只认 DocMind
- `uniqueId -> blocks[].pos`

不能因为 table AI reconstruction 再次改动：

- hover evidence 主链
- pinned evidence 主链
- `证据` 菜单可用性

## 为什么要这样做

这条链路的好处是：

1. 几何和语义分层
   - 减少模型“发明表格”的风险
2. fallback 明确
   - AI grouping 非法时，直接回退到当前 deterministic 表格
3. 与 `/read` 定位一致
   - `/read` 只做稳定流式阅读，不做复杂 generative UI
4. 与 `/experience` 分工清晰
   - `/experience` 以后可以做更自由的表格解释
   - `/read` 只追求结构正确、证据稳定

## 运行时门禁

AI table logical-row reconstruction 只有在同时满足以下条件时才尝试：

- 当前 group 是 `table`
- `table_cells` 非空
- 存在当前页渲染图
- physical row 数量在合理范围内

否则直接使用当前 deterministic table materializer。

## fallback 规则

以下任一情况都必须 fallback：

- AI 输出不是 JSON
- `source_row_indices` 覆盖不完整
- 同一物理行被分配多次
- 行顺序被打乱
- `row_role` 非法

fallback 后：

- 不报错
- 不影响 `/read`
- 继续用当前 deterministic 表格

## 第一阶段实现范围

第一阶段只做：

- `physical_rows -> logical_rows` AI grouping
- 新 contract
- deterministic materialization 接 AI grouping

第一阶段不做：

- cell 文本改写
- AI 直接输出 HTML table
- AI 发明公式/表格结构
- 改全局 evidence preview

## 回退策略

如果这一阶段效果不好：

- 可以完全关闭 AI table logical-row reconstruction
- `/read` 自动回到当前 deterministic table materializer
- 不会影响公式 image-first 和全局证据链
