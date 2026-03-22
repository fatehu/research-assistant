# 论文阅读 `/read` 公式 `image-first` 测试记录（2026-03-12 23:20）

## 测试目标

验证 `/read` 公式块从“低质量 OCR -> KaTeX 主显示”收敛为“公式裁图主显示，OCR 文本仅辅助降级”。

## 本轮改动前基线

- 页面：`http://localhost:3000/literature/85/read?page=3&kb=84`
- 已知问题：
  - 正文中的公式块会额外显示“公式证据”裁图。
  - 右侧 evidence preview 会再次显示相同裁图，产生重复。
  - 主显示的 KaTeX 来自低质量 OCR 文本，内容和排版都不可靠。

## 风险点

- 误伤 `/read` 全局 evidence preview 主链。
- 误删 `证据` 菜单或 hover/pinned evidence。
- `EquationBlock` 降级逻辑不足，导致 evidence image 不可用时空白。

## 本轮需要执行的验证

### 自动化

- `npm --prefix frontend run lint`
- `cd frontend && npx tsc --noEmit`
- 后端定向回归：
  - `backend/tests/test_literature_reader_composed.py`
  - 重点覆盖 `EquationBlock` props 生成与 contract 兼容

### 本轮实际执行结果

- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_composed.py -k "equation_props or materialize_table_and_equation" -q`
  - 结果：`2 passed`
- `npm --prefix frontend run lint`
  - 结果：通过
- `cd frontend && npx tsc --noEmit`
  - 结果：通过
- `docker compose restart backend frontend`
  - 结果：两容器恢复正常
- `curl http://localhost:3000`
  - 结果：`200`
- `curl http://localhost:8888/docs`
  - 结果：`200`

### 2026-03-12 23:37 增量验证

- 调整项：
  - `display_formula` 不再用整页比例做最小裁图高度保底
  - `EquationBlockNode` 将 `renderMode` 纳入依赖，避免 image-first 继续复用旧图
- `npm --prefix frontend run lint`
  - 结果：通过
- `cd frontend && npx tsc --noEmit`
  - 结果：通过
- `docker compose restart frontend`
  - 结果：完成
- `curl http://localhost:3000`
  - 结果：`200`

### 手动

- `http://localhost:3000/literature/85/read?page=3&kb=84`

重点观察：

1. 正文主显示是否改成公式图优先。
2. 页面中是否不再重复出现“公式证据”补充块语义。
3. 右侧 `AI 上下文` evidence preview 是否仍然存在。
4. `... -> 证据` 是否仍可用。

## 回退策略

若本轮引入回归，直接回退本轮涉及的 `EquationBlock` 相关文件，并恢复当前 `/read` 公式显示逻辑。
