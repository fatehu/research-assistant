# 全量整改 阶段08：Docker/Compose/Env 与编码规范对齐

- 时间：2026-02-23 11:03
- 提交类型：`chore(infra)`
- 关联范围：`backend/Dockerfile`、`docker-compose.yml`、`.env.example`、`README.md`

## 目标

- 统一容器 UTF-8 运行时环境，降低日志与中文文本乱码风险。
- 修复前后端默认端口契约偏差，避免前端连错后端端口。
- 固化“容器重建 + 健康检查 + 手测链路”的闭环流程。

## 实施内容

- `backend/Dockerfile`
- 新增运行时编码环境变量：`LANG`、`LC_ALL`、`PYTHONUTF8`、`PYTHONIOENCODING`、`PYTHONUNBUFFERED`。
- `docker-compose.yml`
- 为 `backend`、`codelab-runner`、`mcp_web`、`mcp_literature`、`frontend` 注入统一 UTF-8 相关环境变量。
- 保留业务环境变量与现有服务拓扑不变，仅做编码与运行时一致性增强。
- `.env.example`
- 新增 `CONTAINER_LANG`、`CONTAINER_LC_ALL`、`CONTAINER_PYTHONUTF8`、`CONTAINER_PYTHONIOENCODING`。
- 将 `VITE_API_BASE_URL`、`VITE_WS_BASE_URL` 默认值从 `8000` 对齐为 `8888`（与 Compose 端口映射一致）。
- `README.md`
- 启动命令改为 `docker compose up -d --build backend frontend`。
- 补充健康检查命令 `ps/logs` 与 UTF-8/容器闭环约束说明。

## 风险与回滚

- 风险：部分主机把 `LANG/LC_ALL` 绑定到本地化环境，新增统一值后日志排序/区域格式可能与旧行为不同。
- 风险：如果用户本地 `.env` 仍是 `8000`，前端可能继续访问旧端口。
- 回滚：可回退本提交；或只撤销 `.env.example` 与 `README.md` 的默认端口说明，保持原端口策略。

## 结果

- 容器运行时编码策略在镜像层和编排层完成双重对齐。
- 文档中的部署与健康核验流程与 `scripts/code.md` 的闭环要求保持一致。
