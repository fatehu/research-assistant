# AgentCore统一化阶段4：Notebook Agent 单入口收敛说明（2026-02-16 08:16）

## 1. 本阶段目标
- 在应用挂载层移除重复 Notebook Agent 路由，实现单一入口。
- 保持前端调用路径不变：`/api/v1/codelab/notebooks/{notebook_id}/agent/*`。

## 2. 改动文件白名单
- `backend/app/main.py`

## 3. 使用技术与做法
- 仅保留 `codelab.router` 在 `/api/v1/codelab` 前缀下提供 Agent 接口。
- 取消并行挂载 `agent.router` 和 `notebook_agent.router`，避免同路径多实现覆盖。

## 4. 达成效果
- OpenAPI 路由来源唯一，线上行为稳定可预期。
- 保留 `agent.py`、`notebook_agent.py` 文件用于历史参考，不对外暴露重复接口。

## 5. 风险与回滚
- 若发现依赖旧 router 的隐式行为，可临时恢复对应 `include_router` 行并补兼容层。