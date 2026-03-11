# 论文阅读 Generative UI Renderer 抽离测试记录

时间：2026-03-11 22:56

## 变更范围

- `frontend/src/pages/literature/PaperReaderExperiencePage.tsx`
- `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`
- `frontend/src/pages/literature/useExperienceActionBus.ts`

## 测试目标

- 确认 `/experience` renderer 抽离后仍能正常加载
- 确认后端 generative/experience contract 没被打断
- 确认页面运行态没有新增 console error
- 确认本轮没有引入新的前端 lint error / hooks warning

## 自动化结果

### 1. 后端 runtime 回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q
```

结果：

- `31 passed`

### 2. 后端 API 回归

命令：

```bash
backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan or generative_plan or reader_experience_flow" -q
```

结果：

- `7 passed`
- `12 deselected`

### 3. 后端契约/防御检查

命令：

```bash
backend/.venv-incremental/bin/python backend/checks/check_contract_alignment.py
backend/.venv-incremental/bin/python backend/checks/check_no_new_broad_excepts.py
```

结果：

- `Contract alignment guard passed.`
- `Broad exception guard passed.`

### 4. 前端 lint

命令：

```bash
npm --prefix frontend run lint
```

结果：

- `0 error`
- `3 warnings`

warning 位于：

- `frontend/src/pages/literature/PaperReaderPage.tsx`
- `frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx`

说明：

- 本轮改动涉及的三个文件经定向 eslint 检查无新增 warning。

定向命令：

```bash
cd frontend
./node_modules/.bin/eslint src/pages/literature/PaperReaderExperiencePage.tsx src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/useExperienceActionBus.ts
```

结果：

- 退出码 `0`
- 无输出

### 5. 前端 TypeScript

命令：

```bash
cd frontend
./node_modules/.bin/tsc -p tsconfig.json --noEmit
```

结果：

- 退出码 `0`
- 无输出

## 浏览器层验证

### 1. 服务可达

命令：

```bash
cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"
```

结果：

- `HTTP/1.1 200 OK`

### 2. Playwright 登录与页面打开

使用 skill：

- `playwright`

流程：

1. 打开 `/login`
2. 使用 `yuiooyww@gmail.com / 123456` 登录
3. 在同一 `--session gexp` 会话中打开 `/literature/78/experience?page=7`
4. 截图并检查 console error

结果：

- 登录成功，进入 `/dashboard`
- 在同一会话中打开 `/literature/78/experience?page=7&reader=curious_generalist`
- 页面截图已生成：
  - `output/playwright/experience-renderer-extraction-2026-03-11.png`
- console error 结果：
  - `0 errors`
  - `0 warnings`

## 已知限制

- Playwright 对该页的 `snapshot` 输出为空文件，但截图和 console 检查正常。
- `frontend build` 本轮未重跑；当前仓库仍运行在 9p/WSL 挂载盘环境，构建链路偏慢，后续在收尾阶段继续确认完整 build 闭环。

## 结论

本轮 renderer 抽离后：

- `/experience` 仍可正常访问
- 后端 generative/experience contract 回归通过
- 前端无新增 lint error
- 定向 eslint / tsc 均通过
- 浏览器层未发现新增 console error
