# /chat 评测案例

这份清单用于验证 `/chat` 的真实链路，不是单元测试替代品。

## 目标

- 跑通真实账号下的 `/chat` 主链
- 留下可在 UI 中验收的对话
- 覆盖：
  - 首轮直答
  - `context-preview -> send_plan -> send`
  - 长对话
  - `manual compact`
  - compact 之后旧 `send_plan` 发送
  - live mid-run compact 探针
- 同时保留一条确定性的 backend 回归，确保 mid-run compact 语义不是只靠 live 命中

## 案例矩阵

### 1. 首轮直答

- 输入：
  - 一条明确要求 `use_tools=false` 的解释型问题
- 预期：
  - `preview_mode=direct`
  - 返回 `send_plan`
  - 发送成功
  - `turn_store/item_stream` 有新记录

### 2. 跟进问答 + 长对话

- 输入：
  - 一条简短 follow-up
  - 多条带长背景文本的 follow-up
- 预期：
  - 对话持续成功
  - `turn_store` 和 `item_stream` 持续增长
  - 预演能看到真实上下文状态

### 3. 手动压缩

- 输入：
  - 对长对话执行 `manual compact`
- 预期：
  - `compacted_history.replacement_history` 非空
  - `item_stream` 有 `compact_boundary`
  - `history_log` 记录 `manual_compact`

### 4. compact 后旧 send_plan 发送

- 输入：
  - compact 前先生成一条 `send_plan`
  - compact 后仍带旧 `send_plan_id` 发送
- 预期：
  - 不阻塞
  - 发送成功
  - revision 校验能让系统安全重规划，而不是炸掉

### 5. live mid-run compact 探针

- 输入：
  - 一条长上下文、强阶段化、允许工具的 stress prompt
- 预期：
  - 对话成功完成
  - 如果当前阈值命中，则看到：
    - `history_log.title=mid_run_compact`
    - 或 `item_stream.kind=compact_boundary,status=mid_run`
    - 或 `context_snapshot.mode=mid_run`
- 说明：
  - 这条在 live 环境下不保证每次命中，取决于当时的上下文预算和模型输出

### 6. 确定性 mid-run compact 回归

- 命令：
```bash
docker compose exec -T backend python -m pytest tests/test_agent_runtime_context_resilience.py -q -k test_mid_run_compaction_appends_boundary_and_refreshes_context
```
- 预期：
  - 通过
- 作用：
  - 补足 live 环境不一定命中的问题

## 运行脚本

真实账号 smoke：

```bash
docker compose exec -T backend python /app/scripts/chat_flow_eval.py \
  --base-url http://127.0.0.1:8000 \
  --email <email> \
  --password <password> \
  --register-if-missing
```

脚本会：

- 使用指定账号登录
- 创建带 `[chat-eval]` 前缀的对话
- 跑完整案例
- 把结果写到 `tmp/chat-eval/chat-eval-report-*.json`

## 验收建议

- 在前端 `/chat` 用同一账号查看新建的 `[chat-eval]` 对话
- 重点看：
  - 当前回合是否按 `turn/item` 呈现
  - compact 后历史是否被 `replacement_history` 接住
  - `manual compact` 后后续发送是否正常
  - live probe 是否出现 `mid_run_compact`
