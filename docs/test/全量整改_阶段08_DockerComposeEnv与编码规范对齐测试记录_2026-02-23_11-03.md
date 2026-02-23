# 全量整改 阶段08 测试记录：Docker/Compose/Env 与编码规范对齐

- 时间：2026-02-23 11:03
- 测试人：Codex
- 分支：`feature/full-remediation-2026-02`

## 环境

- 操作系统：Windows（PowerShell）
- 编码前置：`chcp 65001`
- 说明：当前主机 Docker Desktop Linux Engine 未启动

## 命令与结果

1. `docker compose config`
- 结果：通过
- 说明：使用临时环境变量注入必填项（`POSTGRES_PASSWORD`、`DATABASE_URL`、`SECRET_KEY`、`CODELAB_RUNNER_TOKEN`）完成配置展开校验。

2. `docker compose up -d --build backend frontend`
- 结果：失败
- 关键错误：`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.`

3. `docker compose ps`
- 结果：失败（同上，Docker 引擎未就绪）

4. `docker compose logs --tail 100 backend`
- 结果：失败（同上，Docker 引擎未就绪）

5. `docker compose logs --tail 100 frontend`
- 结果：失败（同上，Docker 引擎未就绪）

## 失败定位与处置

- 根因：本机未连接 Docker Desktop Linux Engine，不属于本次代码改动引入问题。
- 处置：先完成配置静态校验并落档；待 Docker 服务恢复后按同命令回放冒烟。

## 回放清单（Docker 恢复后）

1. `docker compose up -d --build backend frontend`
2. `docker compose ps`
3. `docker compose logs --tail 100 backend`
4. `docker compose logs --tail 100 frontend`
5. 手测链路：上传文档 -> 处理完成 -> 检索 -> 结果卡片

## 结论

- 阶段8代码与配置修改在静态层面可通过。
- 动态容器冒烟受环境阻塞，已明确阻塞原因与回放步骤。
