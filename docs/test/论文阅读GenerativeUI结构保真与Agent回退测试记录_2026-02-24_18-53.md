# 论文阅读 Generative UI 结构保真与 Agent 回退测试记录

时间：2026-02-24 18:53

## 测试环境
- 系统：Windows（本地开发环境）
- 后端测试环境：`.venv-ragtest`
- 容器：Docker Compose

## 执行命令与结果
1. Python 语法检查
- 命令：
  - `python -m py_compile backend/app/services/literature_reader_service.py backend/app/api/literature.py backend/app/models/literature.py backend/app/schemas/literature.py backend/tests/test_literature_reader_generative.py`
- 结果：
  - 通过

2. 后端目标测试
- 命令：
  - `.venv-ragtest\\Scripts\\python.exe -m pytest -q backend/tests/test_literature_reader_generative.py backend/tests/test_literature_reader_api.py`
- 结果：
  - `15 passed`
- 覆盖点：
  - `Introduction` heading 识别
  - 低置信度触发 Agent 回退
  - Agent 无效回退自动降级
  - Redis 命中跳过重建
  - source_signature 变化触发重建
  - SSE 事件顺序
  - 预读去重与边界裁剪
  - 图片策略（`image_hint` 不返回外网图地址）

3. 前端构建
- 命令：
  - `npm run build`（目录：`frontend`）
- 结果：
  - 通过（`tsc -b` + `vite build`）

4. Docker 回归
- 命令：
  - `docker compose up -d --build backend frontend`
  - `docker compose ps`
  - `docker compose logs --tail 100 backend`
  - `docker compose logs --tail 100 frontend`
- 结果：
  - `backend/frontend` 重建并启动成功
  - 后端日志确认执行迁移：`018_task_status_contract -> 019_reader_gpage_cache`
  - 前端服务正常监听 `:3000`

## 过程中问题与处理
- 问题：
  - 测试初次执行缺少 `asyncpg`，导致导入数据库层失败。
- 处理：
  - 在 `.venv-ragtest` 安装 `asyncpg` 后重跑测试通过。

## 验收结论
- 结构化链路已接入：后端解析主干 + 低置信度 Agent 回退 + 锚点校验。
- 前端已切换为后端 payload 主链路并支持流式分段渲染。
- 模式记忆、风格模板白名单、邻页预读、共享缓存均已落地。
- 后端/前端构建与容器回归通过，可进入手测（含 PLOS `Introduction` 对照场景）。

