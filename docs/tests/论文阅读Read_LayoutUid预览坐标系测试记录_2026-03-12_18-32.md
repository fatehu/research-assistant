# 论文阅读 Read LayoutUid 预览坐标系测试记录

时间：2026-03-12 18:32

## 目标

验证 `layout_uid_v1` 的 evidence 预览不再把 DocMind 页图坐标错误地贴到 PDF.js 渲染页上。

## 事实证据

### 1. 真实 payload 尺寸

通过真实接口抓取 `paper=85 page=7 kb=84`：

- `page_grounding_v1.evidence_map[*].layout_pos / block_positions` 使用的坐标基准约为 `1483 x 1920`
- `coord_version = layout_uid_v1`
- 示例锚点：
  - `bbox_hint.page_width = 1483`
  - `bbox_hint.page_height = 1920`

### 2. PDF 实际页尺寸

从原 PDF `2505.02390` 第 7 页读取：

- `mediabox = 612 x 792`
- PDF.js 以 `scale=2.4` 渲染时约为 `1468.8 x 1900.8`

### 3. 偏移根因

说明旧实现存在统一缩放误差：

- DocMind 页图坐标直接贴到 PDF.js 画布
- 坐标基准不一致
- 所有高亮统一偏移，而不是单点坏数据

## 自动化

### backend

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "docmind_page_image or pipeline_version_override or repair_malformed_fallback_payload" -q
```

结果：

- `4 passed`

### frontend

命令：

```bash
cd frontend
npx eslint src/pages/literature/PaperReaderPage.tsx
```

结果：

- 退出码 `0`

命令：

```bash
cd frontend
npx tsc --noEmit
```

结果：

- 当前会话中未拿到稳定退出日志，不能记为通过

### 运行态探针

命令：

```bash
GET http://localhost:8888/api/v1/literature/reader/docmind-page-image/85/7
```

结果：

- `HTTP 200`
- `content-type = image/png`
- 实际图片尺寸：`1483 x 1920`

## 手工验收地址

- `http://localhost:3000/literature/85/read?page=7&kb=84`
- `http://localhost:3000/literature/85/read?page=3&kb=84`

## 手工验收点

1. hover/click 后 evidence 预览的高亮不再整体向右下偏移
2. pinned evidence 的命中区域与底图对齐
3. 旧 `anchor_v2` 页面行为不回退

