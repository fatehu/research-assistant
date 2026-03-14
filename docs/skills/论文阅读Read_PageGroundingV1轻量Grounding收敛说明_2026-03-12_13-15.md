# 论文阅读 Read PageGroundingV1 轻量 Grounding 收敛说明

时间：2026-03-12 13:15  
范围：`/read` 轻量 grounding 底座，服务后续 `/read` 收敛与 `/experience` 输入

## 背景

`/read` 不再承担页面级 generative UI 产品目标，但仍需要保留：

- AI 清洗正文
- 简化后的 AI 排版
- 准确的证据溯源与高光

当前 `ReaderComposePayload` 里混入了大量旧 compose / multimodal / assembly 元数据，不适合作为后续 `/experience` 的稳定输入层。

## 本次设计

新增 `page_grounding_v1`，并将其挂入 `ReaderComposePayload`。

它只保留轻量、稳定、可复用的 grounding 信息：

- `layout_atoms`
  - 最小单位是 DocMind `uniqueId`
  - 保留 `layout_id / type / sub_type / raw_text / clean_text / layout_pos / blocks / canonical_block_ids`
- `reading_nodes`
  - 当前先按 `uniqueId` 一对一 materialize
  - 为后续 `/read` 瘦身和 `/experience` 输入提供稳定正文节点
- `evidence_map`
  - 溯源和高光直接绑定 `uniqueId -> blocks[].pos`
  - `geometry_source = docmind_layout_blocks`
- `page_image`
  - 引用当前页的渲染图

## 关键约束

- 不改动 DocMind ownership / geometry 真值
- 不替换当前 `/read` 默认 pipeline，只增加稳定底座
- `uniqueId` 是 layout 原子单元，不能再被 block/markdown 拆坏
- DOI / metadata / header / footer 默认不进入主阅读流

## 为什么先做这一层

这一步把 `/read` 的“复杂 compose 输出”和“后续真正可复用的 grounding 输入”分开了。

后续可以基于同一层继续做：

1. `/read` 新 pipeline：`DocMind -> qwen3.5-plus(+page image) -> uniqueId grouping`
2. `/experience` 直接消费 `page_grounding_v1`，减少对旧 compose 大 payload 的依赖
3. 高光链改成 `source_layout_ids -> blocks[].pos`

## 涉及文件

- `backend/app/schemas/literature.py`
- `backend/app/services/literature_reader_compose_service.py`
- `frontend/src/services/api.ts`
- `backend/tests/test_literature_reader_composed.py`
