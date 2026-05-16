> Window 文档交付流程
>
> 1. 先在 `WINDOW_WORK_OUTLINE_ZH.md` 登记范围、状态、讨论结论和下一步。
> 2. 每个修改项必须有单独 `MOD_XX_*.md` 文件，先写方案、边界、参考实现和验收标准，再进入代码改动。
> 3. 代码实施完成后，回到对应 `MOD_XX_*.md` 回填实施记录、验证结果、遗留问题。
> 4. 总纲只维护索引和阶段状态；具体技术细节留在单项文件，避免多个文档互相漂移。

# MOD 03: Chat SSE Heartbeat

阶段：P1

状态：已实施

## 目标

在长模型请求、长工具执行、mid-run compaction 等阶段持续发 SSE heartbeat，降低用户侧误判断流的概率。

## 不改什么

- heartbeat 不进入 LLM prompt。
- heartbeat 不写入 `item_stream` / `tool_ledger`。
- heartbeat 不改变 agent 推理状态。

## 当前观察

系统已有部分 heartbeat：

- runtime worker 中已有 Claude stream heartbeat，并通过 `project_claude` 等长工具转发为 chat live event。
- status event bus 已有 heartbeat，主要覆盖后台任务订阅通道。
- 前端 `chatStore` 已对 `heartbeat` 事件做静默处理，不会写入消息或工具步骤 UI。
- `/chat/send` 的 agent 分支虽然能透传工具 heartbeat，但主循环在等待 `live_event_queue.get()` 时可能长期没有任何 SSE 输出；这会让浏览器、代理或用户误判为断流。
- direct model stream 当前直接消费 `llm_service.chat_stream(...)`；模型首 token 或 chunk 间隔过长时，也缺少 SSE 保活。

## 推荐改法

统一 `/chat/send` 的 heartbeat 事件语义，只作为运行态 SSE：

- `phase=model_wait`
- `phase=tool_execution`
- `phase=compaction` 暂不单独强识别；当前先由 agent 主循环保活覆盖，后续如 agent 内部显式暴露 compaction phase 再细分。
- `phase=background_execution_wait`

本次实施边界：

- 新增 `/chat/send` 通用 heartbeat 间隔配置，默认 15 秒。
- agent 分支等待 live event queue 超过间隔时发 `heartbeat`。
- direct LLM stream 通过后台 pump + queue 包一层，等待 chunk 超过间隔时发 `heartbeat`。
- runtime-worker 转发来的 `heartbeat` 继续透传，但补齐 `conversation_id`、`turn_id`、`phase` 等 chat 侧上下文。

不把 heartbeat 写入 `item_stream`、`tool_ledger`、assistant message、reasoning summary 或 compaction artifact。

## 验收标准

- 长时间工具执行期间前端能收到 heartbeat。
- 长时间模型等待期间前端能收到 heartbeat。
- heartbeat 不污染持久化事实层。

## 实施记录

- `backend/app/config.py` 新增 `chat_sse_heartbeat_seconds`，默认 15 秒。
- `backend/app/api/chat.py` 新增 chat SSE heartbeat payload 与异步迭代包装器。
- direct LLM stream 改为后台 pump + queue：等待模型 chunk 超过 heartbeat 间隔时发 `heartbeat`，收到 chunk 后仍按原来的 `content` 事件输出。
- ReAct agent 分支在等待 `live_event_queue` 超过 heartbeat 间隔时发 `heartbeat`。
- ReAct agent 分支继续透传 runtime-worker 工具 heartbeat，并补齐 `conversation_id`、`turn_id`、`phase`、`timestamp`。
- heartbeat 只作为 SSE 输出，不进入 assistant message、reasoning item、`item_stream`、`tool_ledger` 或 background persisted event。

## 验证结果

- `docker compose exec -T backend python -m pytest tests/test_chat_send_api.py -q`
  - 20 passed。
- `docker compose exec -T backend python -m pytest tests/test_paper_grounding_tools.py::test_project_claude_tool_forwards_runtime_heartbeat tests/test_agent_function_calling_fallback.py -q`
  - 26 passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_no_new_broad_excepts.py`
  - passed。
- `docker run --rm -v "$PWD":/repo -w /repo research-assistant-backend:latest python backend/checks/check_contract_alignment.py`
  - passed。
- `git diff --check -- backend/app/api/chat.py backend/app/config.py backend/tests/test_chat_send_api.py docs/window/MOD_03_CHAT_HEARTBEAT_ZH.md docs/window/WINDOW_WORK_OUTLINE_ZH.md`
  - passed。

## 遗留问题

- 当前没有在 ReAct agent 内部显式发 `phase=compaction`；mid-run compaction 期间会被 chat agent 主循环的 `model_wait` heartbeat 覆盖。若后续要给前端展示更精确阶段，需要在 `react_agent.py` 的 compaction 前后增加非事实层 phase/live event。
- backend compose 服务容器不包含完整仓库根目录，两个静态 guard 在该容器内会误报路径缺失；本次改用同一 backend 镜像挂载仓库根目录执行。
