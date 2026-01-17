# 调试指南

## 查看后端日志

### 1. 查看实时日志

```bash
# 查看 backend 容器的实时日志
docker-compose logs -f backend

# 或者只看最近 100 行
docker-compose logs --tail=100 backend
```

### 2. 关键日志标记

ReAct Agent 相关的日志都带有 `[ReAct]` 前缀：

```
[ReAct] 开始迭代 1
[ReAct] 进入 think 模式
[ReAct] 思考完成: ...
[ReAct] 进入 action 模式
[ReAct] 收到 action: {"tool": "web_search", ...}
[ReAct] 执行工具: web_search, 参数: {...}
[ReAct] 工具结果: success=True, output=...
[ReAct] 工具执行完成，返回以开始新迭代
[ReAct] 开始迭代 2
...
[ReAct] 完成: iterations=2, steps=5, answer_len=500
```

Chat API 相关的日志带有 `[Chat]` 前缀：

```
[Chat] 对话完成: thought_len=100, content_len=500
```

### 3. 常见问题排查

#### 问题：工具没有被调用

检查日志中是否有：
- `[ReAct] 进入 action 模式` - 如果没有，说明 LLM 没有输出 `<action>` 标签
- `[ReAct] 收到 action: ...` - 如果有这行但没有后续的执行，说明 JSON 解析失败

#### 问题：回复内容不完整或包含标签

检查日志中是否有：
- `[ReAct] 未找到标准格式，使用清理后的响应作为答案` - 说明 LLM 输出格式不符合预期
- `[ReAct] 检测到裸 JSON action` - 说明 LLM 没有使用 `<action>` 标签

### 4. 调试模式

可以在 `.env` 文件中设置更详细的日志级别：

```bash
# 在 .env 中添加
LOG_LEVEL=DEBUG
```

然后重启服务：

```bash
docker-compose down
docker-compose up -d
```

## 查看前端控制台

1. 打开浏览器开发者工具 (F12)
2. 切换到 Console 标签
3. 发送消息后查看 SSE 事件

## 网络请求监控

1. 打开浏览器开发者工具 (F12)
2. 切换到 Network 标签
3. 筛选 "EventStream" 或 "stream"
4. 查看 SSE 事件流

## 测试命令

测试天气查询（应该触发 web_search 工具）：
```
今天广州天气怎么样
```

测试计算器（应该触发 calculator 工具）：
```
计算 sqrt(144) + 100
```

测试直接回答（不应该调用工具）：
```
你好
```
