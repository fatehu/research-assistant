# 论文阅读 Read PageGroundingV1 测试记录

时间：2026-03-12 13:15

## 目标

验证 `/read` 新增的 `page_grounding_v1` 已经能稳定产出：

- `uniqueId` 级 `layout_atoms`
- page-local `reading_nodes`
- `uniqueId -> blocks[].pos` 的 `evidence_map`
- 轻量 `page_image` 引用

## 自动化验证

### 1. 定向 pytest

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "page_grounding_v1 or doi_layout_outside_main_flow or mark_layout_monotony" -q
```

结果：

```text
3 passed, 105 deselected
```

覆盖点：

- 一个 `uniqueId` 下多个 block 仍被视为一个 `layout_atom`
- DOI-like layout 会被识别为 `doi`，并标记为不进入主阅读流
- 原有 `layout_monotony` 合同未回退

### 2. 合同对齐

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
```

结果：

```text
Contract alignment guard passed.
```

### 3. Frontend lint

命令：

```bash
npm --prefix frontend run lint
```

结果：

- `0 error`
- `1 warning`
- warning 位于 `frontend/src/pages/literature/PaperReaderPage.tsx`

### 4. Frontend build

命令：

```bash
timeout 180s npm --prefix frontend run build
```

结果：

- 未通过
- 失败原因不是 TypeScript 或 Vite transform 错误
- 实际错误为输出目录权限：

```text
EACCES, Permission denied: /mnt/d/codefield/agent-platform/research-assistant/frontend/dist/assets
```

补充检查：

- `frontend/dist` 属主：`yui:yui`
- `frontend/dist/assets` 属主：`root:root`

所以这轮 build 失败属于当前环境下的输出目录权限问题，不归因为本次 `page_grounding_v1` 逻辑改动。

## 运行时证据

`page_grounding_v1` 不是只在新生成 payload 上才有。

它挂在 `LiteratureReaderComposeService._ensure_payload_contract(...)` 中，因此：

- Redis cache hit
- DB cache hit
- compatible cache hit
- fresh build

这四条 `/read` compose 主路径都会补齐 `page_grounding_v1`。

关键代码：

- `backend/app/services/literature_reader_compose_service.py`
- `_ensure_payload_contract(...)`
- `_build_page_grounding_v1(...)`

## 本轮已知限制

- 这一步只新增轻量 grounding，不替换现有 `/read` 默认 simplified pipeline
- `reading_nodes` 当前仍是 `uniqueId` 一对一 materialize，还不是后续 AI grouping 版
