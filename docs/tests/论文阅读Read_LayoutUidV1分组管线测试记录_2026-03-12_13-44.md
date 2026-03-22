# 论文阅读 Read LayoutUidV1 分组管线测试记录

时间：2026-03-12 13:44

## 本轮目标

验证新接入的 `/read` 内部 pipeline 骨架：

- pipeline version `layout_uid_v1`
- 只按 `uniqueId(layout_id)` 做 grouping
- grouping 异常时 deterministic fallback 生效
- 不切默认流量，但系统内分支可被正确选择

## 自动化验证

### 1. `/read` 定向回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "layout_uid or page_grounding_v1 or doi_layout_outside_main_flow" -q
```

结果：

```text
6 passed, 106 deselected
```

覆盖点：

- `page_grounding_v1` 继续成立
- `layout_uid_v1` prompt 只发送 `uniqueId` 级 atom
- duplicate / missing `layout_id` 会触发 fallback
- `figure + adjacent figure_caption` 会被 deterministic fallback 合并
- `build_or_get_composed_payload()` 在 `reader_pipeline_version=layout_uid_v1` 时会优先选择新 builder

### 2. Contract 对齐检查

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
```

结果：

```text
Contract alignment guard passed.
```

### 2.1 `/read` cached 路径 pipeline 区分回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "pipeline_version_override or repair_malformed_fallback_payload" -q
```

结果：

```text
2 passed, 19 deselected
```

覆盖点：

- `/reader/composed/cached` 会把 `pipeline_version` 透传给 compose service
- 带 `compose=layout_uid_v1` 的页面不会从 cached 恢复阶段误读回旧 pipeline 结果

### 3. Broad exception 守卫

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

```text
Broad exception guard passed.
```

### 4. Frontend lint

命令：

```bash
npm --prefix frontend run lint
```

结果：

```text
0 errors / 1 warning
```

现存 warning：

- `frontend/src/pages/literature/PaperReaderPage.tsx`
  - `react-hooks/exhaustive-deps`

## 直接验收入口

这轮已经把新链路接成可直接访问的 URL 入口，不需要手动改配置：

- `http://localhost:3000/literature/78/read?page=7&kb=84&compose=layout_uid_v1`
- `http://localhost:3000/literature/85/read?page=1&kb=84&compose=layout_uid_v1`

页面验收标记：

- `/read` 顶部 AI Reader 区域会出现一个紫色 `layout_uid_v1` 标签
- 右侧 `AI 上下文` 面板头部也会出现同样的标签
- cached 恢复路径已按 `pipeline_version` 区分，不会再把旧 pipeline 的缓存误拿回来

## 本轮结论

- `layout_uid_v1` 已经接入系统内部选择逻辑
- 还没有切默认流量
- 新分支已经有：
  - uniqueId 级 prompt contract
  - exact-once grouping 校验
  - deterministic fallback
  - 轻量 materialization

## 已知限制

- 这一轮没有跑完整 `frontend build`
- 这一轮没有切真实默认流量，所以用户面默认 `/read` 仍然是旧链路
- 新链路目前仍复用了旧 `_panel_plan_to_ui_plan(...)` 做 materialization，这是一种过渡实现，不是最终终态

## 后续修正补记

### 标题被错误归到 `AI 资产`

现象：

- `http://localhost:3000/literature/85/read?page=1&kb=84&compose=layout_uid_v1`
- 标题 `Quantitative Analysis of Performance Drop in DeepSeek Model Quantization`
- 虽然模型分组正常，但被前端放进右栏 `AI 资产`

根因：

- `PaperReaderPage.tsx` 里有一条旧 `/read` 壳层规则：
  只要某个节点文本与论文标题重复，就自动从主阅读流挪到右栏 context。
- 这条规则原本是为了旧 `PaperHeaderCard` 重复标题去重。
- `layout_uid_v1` 没有依赖 `PaperHeaderCard`，所以这条旧规则会把正确生成的 `SectionHeading` 错误挪走。

修正：

- 只有在当前 compose 结果里真的存在 `PaperHeaderCard` 时，才允许把“与论文标题重复”的节点挪到右栏。
- 对 `layout_uid_v1` 这种没有专门 header 卡片的新链路，标题保留在正文主画布。

验证：

- `npm --prefix frontend run lint`
  - `0 error / 1 warning`
