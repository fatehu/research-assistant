# 论文阅读 `/read`：Table Cells 与行级证据测试记录

时间：2026-03-12 15:56

## 本轮目标

修复 `layout_uid_v1` 在 `/read` 表格页上的两个真实问题：

1. 表格被错误降维成“一行很多列”。
2. 表格证据只剩整表级，没有行级定位。

## 运行前提

- 后端：Docker compose 开发态，`uvicorn --reload`
- 前端：Vite dev
- 默认 `/read` pipeline：`layout_uid_v1`

## 自动化验证

### 1. `/read` composed service 定向回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "table_cells_in_page_grounding or materialize_table_and_equation or layout_uid or page_grounding_v1 or doi_layout_outside_main_flow" -q
```

结果：

- `10 passed`

覆盖点：

- `page_grounding_v1` 保留 DocMind `table_cells`
- `layout_uid_v1` table materializer 优先用 `table_cells`
- `TablePanel` 生成 `matrix/header_row_count/caption/row_evidence`
- `EquationBlock` 仍正常 materialize

### 2. `/read` API 定向回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "default_to_layout_uid_v1_pipeline or pipeline_version_override or repair_malformed_fallback_payload" -q
```

结果：

- `3 passed`

覆盖点：

- `/read` 默认仍走 `layout_uid_v1`
- `pipeline_version` 覆盖仍可用
- fallback payload repair 仍未回退

### 3. 合同检查

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- `Contract alignment guard passed.`
- `Broad exception guard passed.`

### 4. 前端 reader 相关 lint

命令：

```bash
cd frontend
./node_modules/.bin/eslint src/pages/literature/readerComponents/index.tsx src/pages/literature/readerComponents/schemas.ts --format unix
```

结果：

- 无报错输出

### 5. 前端 typecheck

命令：

```bash
cd frontend
./node_modules/.bin/tsc --noEmit --pretty false -p tsconfig.json
```

结果：

- 当前环境下未拿到稳定退出结果
- 没有记录到本轮改动直接触发的错误输出
- 不算完整通过

## 运行态取证

通过 live cache 查询 `paper 85 / page 7`，确认：

### 修复前真实问题

- `page_grounding_v1.layout_atoms[table].blocks` 只有整表 block
- `ui_plan.components[TablePanel].props.rows` 被压成单条超长记录
- `ui_plan.components[TablePanel].source_anchor_refs` 只有整表级 anchor

### 修复依据

原始 `docmind_structure.layouts[*].type=table` 中实际存在：

- `cells[*].ysc/yec/xsc/xec`
- `cells[*].layouts[*].uniqueId`
- `cells[*].pos`

说明表格细粒度证据在原始层存在，问题出在 `/read` 物化链而不是 DocMind 缺数据。

## 验收建议

建议手动验收：

- `http://localhost:3000/literature/85/read?page=7&kb=84`

预期：

1. 表格不再只剩第一行或一行很多列的假矩阵。
2. 表格 caption 并回 `TablePanel`，不再完全拆成独立正文段。
3. 悬停表格行时能触发对应证据预览。
4. 点击表格行时能跳到对应 PDF 高光区域。

注意：

- 由于 compose engine 已升到 `reader_compose_v6`，旧 cache 不应再命中。
- 如果浏览器仍展示旧结果，先硬刷新，再点一次“重新生成”。
