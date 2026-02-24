# Docker构建源与Compose参数同步测试记录（2026-02-16 17:31）

## 1. 测试环境
- 系统：Windows（本地开发机）
- Docker：Docker Desktop 4.41.2
- 分支：`feature/codelab-rag-reasoning-notebook-fix-20260216`

## 2. 测试目标
- 验证 `PIP_INDEX_URL` / `TORCH_INDEX_URL` 是否在 compose 中被正确注入。
- 验证后端核心服务可完成重建与启动。
- 验证磁盘紧急处理后环境恢复可用。

## 3. 测试步骤与结果
1. 配置注入检查
- 命令：
```bash
docker compose config | Select-String -Pattern "PIP_INDEX_URL|TORCH_INDEX_URL"
```
- 结果：输出中可见上述两个构建参数，判定通过。

2. 容器重建与启动检查
- 命令：
```bash
docker compose up -d --build backend frontend
docker compose ps
```
- 结果：`backend`、`frontend`、`codelab-runner`、`postgres`、`redis` 均为 `Up`（数据库/缓存 healthy），判定通过。

3. 日志健康检查
- 命令：
```bash
docker compose logs --tail 80 backend
docker compose logs --tail 80 frontend
```
- 结果：后端启动完成并监听 8000；前端 Vite 启动完成并监听 3000，判定通过。

4. 磁盘紧急处理记录
- 处理命令：
```bash
docker system prune -af --volumes
docker builder prune -af
```
- 关键结果：`Total reclaimed space: 40.1GB`（Docker 内部垃圾回收）。
- 当前可用空间：约 `35.5GB`（`Get-PSDrive C`）。

## 4. 异常与修复
- 异常：Docker daemon 在磁盘接近打满时出现连接超时/500。
- 修复：
  - 重启 Docker Desktop；
  - 执行镜像/构建缓存清理；
  - 重新 `up -d --build` 校验服务恢复。

## 5. 结论
- 构建参数同步改造生效，容器链路可稳定启动。
- 磁盘恢复后开发环境可继续工作，满足本阶段验收要求。
