# Docker构建源与Compose参数同步说明（2026-02-16 17:31）

## 1. 阶段目标
- 解决 Docker 构建过程中网络波动导致的依赖拉取不稳定问题。
- 将构建参数显式下发到各后端相关服务，避免“本地可构建、某服务不可构建”的不一致。

## 2. 白名单文件
- `docker-compose.yml`
- `.env.example`

## 3. 使用技术
- 统一通过构建参数传递 Python 包索引源：`PIP_INDEX_URL`。
- 保持 torch 源参数可配置：`TORCH_INDEX_URL`。
- 用 `.env.example` 公开默认值，确保新环境可复现。

## 4. 具体改造
1. `docker-compose.yml`
- 为以下服务增加 build args：
  - `backend`
  - `codelab-runner`
  - `mcp_web`
  - `mcp_literature`
- 新增参数：
  - `PIP_INDEX_URL`（默认 `https://pypi.tuna.tsinghua.edu.cn/simple`）
  - `TORCH_INDEX_URL`（保持原有可配置逻辑）

2. `.env.example`
- 新增 `PIP_INDEX_URL` 示例项，并说明用途（降低构建过程 SSL/中断风险）。

## 5. 达成效果
- 后端相关镜像构建路径参数统一，减少环境漂移。
- 在网络抖动环境下构建稳定性提升，降低反复重建失败概率。
- 配置公开化，可直接在新机器或 CI 环境复用。

## 6. 可感知验证点
- `docker compose config` 中可见 `PIP_INDEX_URL` 与 `TORCH_INDEX_URL` 已注入。
- `docker compose up -d --build backend frontend` 可完成并成功拉起服务。
- `docker compose ps` 显示核心容器处于 `Up` 状态。

## 7. 回滚路径
- 回滚 `docker-compose.yml` 的 build args 注入块。
- 回滚 `.env.example` 中新增的 `PIP_INDEX_URL`。
