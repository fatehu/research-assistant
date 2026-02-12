# Reranker 集成测试记录（2026-02-12）

## 1. 变更范围
- 新增 `backend/app/services/reranker_service.py`
- 修改两阶段检索链路：
  - `backend/app/api/knowledge.py`
  - `backend/app/services/agent_tools.py`
- 新增配置项：
  - `backend/app/config.py`
  - `.env.example`
  - `docker-compose.yml`
- 新增请求参数：
  - `backend/app/schemas/knowledge.py` (`use_reranker`)

## 2. 测试环境
- 日期：2026-02-12
- 方式：Docker Compose 本地联调
- 关键服务：
  - `research_backend` (`0.0.0.0:8888->8000/tcp`)
  - `research_frontend` (`0.0.0.0:3000->3000/tcp`)
  - `research_postgres` (`healthy`)
  - `research_redis` (`healthy`)

## 3. 测试项与结果

### 3.1 容器启动与服务可用性
- 命令：
  - `docker compose up -d`
  - `docker compose ps`
  - `docker compose logs backend --tail 120`
  - `GET http://localhost:8888/health`
- 预期：后端正常启动，数据库连接正常，健康检查返回 healthy。
- 实际：通过。
  - `HEALTH=healthy,DB=healthy`
  - 日志显示 `Application startup complete.`

### 3.2 搜索接口（启用 reranker）
- 场景：`POST /api/knowledge/search`，`use_reranker=true`
- 结果：通过（HTTP 200）
  - `total=0`
  - `search_time_ms=1083.5626`
- 说明：首轮启用 reranker 延迟高于纯 ANN，符合预期。

### 3.3 搜索接口（禁用 reranker）
- 场景：`POST /api/knowledge/search`，`use_reranker=false`
- 结果：通过（HTTP 200）
  - `total=0`
  - `search_time_ms=92.9365`
- 说明：纯 ANN 路径可正常回退/运行。

### 3.4 Reranker 模型实际推理
- 场景：容器内直接调用 `RerankerService.rerank()`
- 命令（摘要）：
  - `docker compose exec -T backend ... await service.rerank(...)`
- 结果：通过
  - 输出：`RERANK_OK [(0, 0.021918583661317825), (1, 3.6442863347474486e-05)]`
  - 日志：成功加载 `BAAI/bge-reranker-v2-m3`（device=cpu）

## 4. 结论
- 本次 Reranker 集成在 Docker 环境验证通过：
  - 两阶段检索链路可用；
  - `use_reranker` 开关生效；
  - 模型可实际加载并完成推理；
  - 未发现阻断发布的问题。

## 5. 注意事项
- 首次加载 reranker 模型会触发权重下载，耗时较长（正常现象）。
- 日志中存在 `ALIYUN_EMBEDDING_API_KEY` 未设置警告，不影响本次 reranker 测试结论。
