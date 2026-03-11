# 论文阅读 Generative UI Block 协议与 Action Bus 测试记录

时间：2026-03-11 19:49  
范围：`/literature/78/experience?page=7`

## 本轮变更

- 修复前端运行时错误：`hasNonDraftExperiencePlan is not defined`
- 抽出 `useExperienceActionBus`
- 让 `/experience` 真正消费 block `ui_actions / event_bindings`
- 页面顶部展示最近一次触发的协议事件
- `focus_target / return_to_reader` 接到页面焦点状态

## 自动化验证

### 1. Frontend lint

命令：

```bash
npm --prefix frontend run lint
```

结果：

- 通过
- `0 error`
- 仍有 `9 warnings`
- warning 主要是已有的 React Hook 依赖提示，不是本轮新增 error

### 2. Backend runtime tests

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q
```

结果：

- 通过
- `31 passed`

### 3. Backend literature API subset

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q
```

结果：

- 通过
- `7 passed, 12 deselected`

## 运行态验证

### 服务可达性

宿主机侧验证：

- `http://localhost:3000` -> `HTTP 200`
- `http://localhost:8888/docs` -> `HTTP 200`

说明：

- Docker Desktop 启动后，`research_frontend` 和 `research_backend` 端口映射正常
- WSL 内部 `localhost` 直连不作为最终验收依据，后续统一以宿主机侧可达性为准

### Playwright 补充验证

本轮执行了 Playwright 浏览器会话验证，结果分两部分：

- 能打开浏览器并命中登录页
- 登录页 ref 可抓取，登录动作本身可执行

当前限制：

- Playwright CLI 的会话登录态复用不稳定
- 再次 `open /experience` 时会回到 `/login`
- 因此这轮没有把 Playwright 结果算成“完整登录态 E2E 通过”

当前对页面修复的浏览器层闭环依据为：

- 宿主机 `HTTP 200`
- 用户手动确认 `/literature/78/experience?page=7` 可正常访问

### 用户反馈验证

用户已确认：

- `/literature/78/experience?page=7` 可以正常访问
- 之前的页面异常由 `hasNonDraftExperiencePlan is not defined` 触发

本轮已修复该错误，并重新纳入自动化校验流程。

## 已知问题

1. 当前 `npm run lint` 仍保留 9 条 hook warning。
2. `useExperienceActionBus` 目前只在 `/experience` 生效，`/workbench` 还未接入。
3. block 事件目前只做本地页面反馈，还未回传 agent。

## 后续测试重点

1. 点击图解面板、问题项、资源来源按钮后，顶部是否出现“已触发”反馈。
2. 点击图解面板后，焦点区域是否随 `focus_target` 变化。
3. `/experience` 是否更像在响应 block 协议，而不是静态模板卡片。
