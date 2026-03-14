# 论文阅读 Read Table 与 Equation 确定性物化测试记录

时间：2026-03-12 15:19

## 目标

验证 `/read` 的 `layout_uid_v1` 新增能力：

- table_caption / equation 分型
- table + caption fallback 合并
- table/equation deterministic materialization
- contract 对齐

## 自动化结果

### 1. 后端 service 定向回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "table_caption_and_equation or merge_table_with_adjacent_caption or materialize_table_and_equation or layout_uid or page_grounding_v1 or doi_layout_outside_main_flow" -q
```

结果：

```text
10 passed, 107 deselected
```

覆盖点：

- `table_caption` / `equation` 分型
- `table + table_caption` fallback 合并
- `TablePanel` matrix/header/body 物化
- `EquationBlock` 物化
- `layout_uid_v1` / `page_grounding_v1` 既有回归未回退

### 2. API 定向回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "pipeline_version_override or default_to_layout_uid_v1_pipeline or repair_malformed_fallback_payload" -q
```

结果：

```text
3 passed, 19 deselected
```

补充：

- `engine_version` 已随这轮 materializer 变更切到 `reader_compose_v5`
- 这样 `/read` 再次生成时不会继续复用旧的空表格 cache

### 3. Contract / guard

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- contract alignment：passed
- broad exception guard：passed

### 4. 前端静态校验

命令：

```bash
npm --prefix frontend run lint
```

结果：

- 通过，未新增 error

补充：

- 针对 `readerComponents/index.tsx` / `schemas.ts` 的定向 eslint 在当前挂载盘环境里退出很慢，但未返回 lint error
- 这与仓库现有 WSL / bind mount 下前端静态检查偏慢的问题一致

## 人工验收建议

优先验这两个地址：

- `http://localhost:3000/literature/85/read?page=7&kb=84`
- `http://localhost:3000/literature/85/read?page=7&kb=84&compose=layout_uid_v1`

重点看：

1. 表格不再退化成 `(暂无结构化行数据)`
2. 表格主体不再被拼成一长段 prose
3. 表格 caption 归位，不再和正文混掉
4. 公式不再掉回普通段落，而是单独成 block

## 已知限制

- 复杂 `rowspan / colspan` 仍未解决
- 复杂多级表注 / 数据注记仍可能只以 caption/notes 形式显示
- 公式目前是 dedicated block，不是完整数学渲染引擎
