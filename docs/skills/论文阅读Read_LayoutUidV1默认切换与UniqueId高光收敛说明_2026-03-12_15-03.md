# 论文阅读 Read：LayoutUidV1 默认切换与 UniqueId 高光收敛说明

## 背景

`/read` 已经不再承担最终 generative UI 产品面的页面设计目标，但仍需要稳定承担：

- AI 清洗正文
- 简化 AI 排版
- 准确溯源与高光
- 作为 `/experience` 的高质量 grounding 输入

前一阶段已经落下：

- `page_grounding_v1`
- `layout_uid_v1`
- `compose=layout_uid_v1` 显式切换能力

这一轮的目标是把这条新链路从“可选”推进到“默认”，并把高光主链从旧的 block/bbox 混合模式收敛到 `uniqueId -> blocks[].pos`。

## 设计决策

### 1. `layout_uid_v1` 成为 `/read` 默认主链

改动点：

- `backend/app/config.py`
- `backend/app/services/literature_reader_compose_service.py`
- `.env`
- `docker-compose.yml`

统一效果：

- 默认 `reader_pipeline_version = layout_uid_v1`
- `/read` 在没有 `compose=` 查询参数时，也默认走 `uniqueId` 分组链
- 仍然允许用 URL 显式指定 pipeline，便于对照验收

这样做的理由：

- 旧 `simplified_v2` 已经和新的 `/read` 目标不一致
- 用户现在认可的是 `layout_uid_v1` 的阅读效果，而不是旧 pipeline
- 再保留旧链为默认，只会持续放大行为漂移

### 2. 高光切到 `uniqueId -> blocks[].pos`

核心原则：

- 高光真值优先信 `page_grounding_v1.evidence_map`
- `source_layout_id` 是 `/read` 新链的主锚点
- `source_atom_ids` 在 `layout_uid_v1` 中不再复用 block id，而是直接承载 `source_layout_ids`

后端收敛：

- `_panel_plan_to_ui_plan(...)` 现在优先为每个 `source_layout_id` 生成 layout-based anchor
- anchor 几何来自：
  - `page_grounding_v1.evidence_map[].block_positions`
  - 缺失时退到 `layout_pos`
- 生成的 anchor 会带：
  - `source_layout_id`
  - `geometry_version=poly_v1`
  - `coord_version=layout_uid_v1`

前端收敛：

- `readerComponents` 的 evidence 操作会同时传：
  - `sourceBlockIds`
  - `sourceAtomIds`
- `PaperReaderPage` 会先尝试从 `page_grounding_v1` 的 layout 空间索引中构建 preview/highlight
- 只有 layout 路径拿不到几何时，才退回旧的 `page_structure_v3` block 几何

## 结果

这一轮完成后：

- `/read` 默认主链已经切到 `layout_uid_v1`
- `compose=layout_uid_v1` 仍可保留作为显式对照标签
- `/read` 的 hover / preview / jump evidence 开始以 `uniqueId` 为主坐标系统

仍未解决：

- 表格的 deterministic materialization 还没做
- `layout_uid_v1` 目前只是把 table 识别为 table，不会把 rows/cells 真正做出来

这意味着：

- 当前 `/read` 在 prose / heading / figure 页面上更接近目标
- table-heavy 页面仍需下一阶段专门补 `table bundle -> deterministic TablePanel`
