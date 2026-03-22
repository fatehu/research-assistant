# 论文阅读 Generative UI Block Registry 测试记录

时间：2026-03-11 23:09

## 变更范围

- `frontend/src/pages/literature/GenerativeExperienceRenderer.tsx`
- `frontend/src/pages/literature/experienceBlockRegistry.tsx`

## 测试目标

- 确认 renderer 引入 registry 后，前端类型和 lint 不回退
- 确认 `/experience` 页面仍可渲染
- 记录本轮浏览器层遇到的问题，不把 HMR 抖动误记成业务逻辑错误

## 自动化结果

### 1. 定向 eslint

命令：

```bash
cd frontend
./node_modules/.bin/eslint src/pages/literature/PaperReaderExperiencePage.tsx src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/useExperienceActionBus.ts src/pages/literature/experienceBlockRegistry.tsx
```

结果：

- 退出码 `0`
- 无输出

### 2. TypeScript

命令：

```bash
cd frontend
./node_modules/.bin/tsc -p tsconfig.json --noEmit
```

结果：

- 退出码 `0`
- 无输出

### 3. 宿主机可达性

命令：

```bash
cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"
```

结果：

- `HTTP/1.1 200 OK`

## 浏览器层结果

使用 skill：

- `playwright`

流程：

1. 复用已有登录会话 `gexp`
2. 检查 `/literature/78/experience?page=7`
3. 抓取 console error

结果：

- 页面截图仍正常渲染
- 发现 2 条 console error，内容是：
  - `Failed to load resource ... GenerativeExperienceRenderer.tsx?t=... 404`
  - `[hmr] Failed to reload /src/pages/literature/GenerativeExperienceRenderer.tsx`

判断：

- 这是本轮对同一路径文件“删除后重建”触发的 Vite HMR 抖动
- 不属于 `/experience` 业务逻辑运行错误
- 页面截图仍正常渲染，说明不是页面已经坏掉

补充结果：

- 宿主机直接访问仍是 `HTTP 200`
- 新的 Playwright 冷启动登录会话可正常进入登录页和 dashboard

## 已知问题

- 本轮浏览器层残留一个开发态 HMR 问题：
  修改 `GenerativeExperienceRenderer.tsx` 的方式导致已有开发会话里出现一次 module 404
- 这需要在开发态做一次页面硬刷新或前端容器/dev server 重载
- 本轮没有把这个问题包装成“已经完全没有浏览器告警”
- 已执行：

```bash
cmd.exe /c docker compose restart frontend
cmd.exe /c curl -I "http://localhost:3000/literature/78/experience?page=7"
```

结果：

- `frontend` 容器已重启
- 路由仍返回 `HTTP/1.1 200 OK`

## 结论

本轮 block registry 收敛后：

- 定向 eslint 通过
- TypeScript 通过
- 页面宿主机路由可达
- 浏览器层发现的是开发态 HMR 抖动，不是 plan/runtime/renderer 业务错误
