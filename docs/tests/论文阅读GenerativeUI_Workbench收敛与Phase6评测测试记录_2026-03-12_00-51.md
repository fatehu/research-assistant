# 论文阅读 Generative UI：Workbench 收敛与 Phase 6 评测骨架测试记录

## 范围

- Workbench 收敛到 shared renderer/runtime
- block-level state contract
- Phase 6 evaluation fixtures / snapshot tests / asset guard

## 自动化验证

### Backend

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q
```

结果：

- `32 passed`

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_ui_evaluation.py -q
```

结果：

- `3 passed`

```bash
backend/.venv-incremental/bin/python backend/checks/check_generative_ui_eval_assets.py
```

结果：

- `Generative UI eval asset guard passed.`

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
```

结果：

- `Contract alignment guard passed.`

```bash
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- `Broad exception guard passed.`

### Frontend

```bash
npm --prefix frontend run lint
```

结果：

- `0 error / 1 warning`

剩余 warning：

- `frontend/src/pages/literature/PaperReaderPage.tsx`

```bash
npm --prefix frontend run build
```

结果：

- 使用 `timeout 240s npm --prefix frontend run build`
- 退出码：`124`
- 日志已进入：
  - `tsc -b && vite build`
  - `vite v5.4.21 building for production...`
  - `transforming...`
- 当前环境仍有 Docker/WSL bind mount 下慢构建的已知风险
- 本轮不记为通过

## 路由可达性

```bash
cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"
cmd.exe /c curl -I "http://localhost:3000/literature/78/read/workbench?page=7"
```

结果：

- 两条路由均返回 `HTTP/1.1 200 OK`

## 浏览器验证

### `/experience`

- 使用已有登录态打开：
  `http://localhost:3000/literature/78/experience?page=7&reader=curious_generalist`
- 页面状态为“已就绪”
- hero、reading path、resources、glossary、question 等 block 正常显示
- 未见新的 console error

### `/workbench`

- 使用已有登录态打开：
  `http://localhost:3000/literature/78/read/workbench?page=7&reader=curious_generalist`
- 共享 experience preview 正常显示
- story map / enhancement outline / targets / plan meta 等 debug 面板正常挂载
- 页面行为符合“debug shell over shared experience output”，不再像第二套产品页实现

## 本轮暴露并修复的问题

- 修复 `GenerativeReaderAgentRuntime._is_generic_figure_focus_panel` 静态方法内部错误引用 `self` 的问题

## 已知限制

- Phase 6 的 golden set 目前是混合种子集：
  - 1 个真实 paper page
  - 2 个 contract fixture
- 这足够做 snapshot/guard，但还不是完整真实样本池
- frontend `build` 在 `vite build` 阶段超时退出，当前仍未得到稳定成功结果
