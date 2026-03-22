# 论文阅读 Read LayoutUid 预览坐标系收敛说明

时间：2026-03-12 18:32

## 问题

`/read` 切到 `layout_uid_v1` 之后，证据按钮和 hover 预览已经恢复，但 evidence 预览中的高亮区域整体一致地向右下偏移。

这不是单个锚点坏掉，而是统一坐标基准不一致：

- `layout_uid_v1` 的几何来自 DocMind 页图坐标
- `renderAnchorEvidenceImage()` 之前把这些点直接画在 PDF.js 渲染页上
- `paper 85 page 7` 的真实数据里：
  - DocMind 页图：`1483 x 1920`
  - PDF 原页：`612 x 792`
  - PDF.js 以 `scale=2.4` 渲染时约为 `1468.8 x 1900.8`

这会造成统一比例误差，表现为所有高亮都一致偏移。

## 新设计

`layout_uid_v1` 的 evidence 预览不再使用 PDF.js 画布作为底图，而改成：

1. backend 新增 `/api/v1/literature/reader/docmind-page-image/{paper_id}/{page}`
2. route 优先返回 `docmind_structure.page_image_path`
3. 没有本地路径时代理 `docmind_structure.page_image_url`
4. frontend 对 `coord_version = layout_uid_v1` 的 anchor：
   - 直接加载这张 DocMind 原始页图
   - 在同一坐标系下画 `geometry.polygons / bbox_hint`

## 为什么这样做

- `layout_uid_v1` 的几何真值本来就来自 DocMind 页图
- 在同坐标系底图上画 polygon，不需要再猜 PDF.js viewport 缩放
- 这条修复不会影响旧的 `anchor_v2`，旧链路仍然走 PDF.js 文本/几何匹配

## 影响范围

- `/read` evidence hover 预览
- `/read` pinned evidence 卡片里的裁图

不影响：

- `/experience`
- `/workbench`
- `/read` 旧 `anchor_v2` 预览
- PDF 主阅读区本体

## 当前结论

对于 `layout_uid_v1`：

- evidence 行为恢复的关键是前几轮修的 `ActionBar / page gate`
- evidence 位置准确的关键是本轮把底图改回 DocMind 原始页图

