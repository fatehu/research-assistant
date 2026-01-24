# CodeLab ReAct Agent 测试方案

## 测试环境准备

### 1. 启动服务
```bash
# 重建并启动容器
docker-compose down -v
docker-compose up --build -d

# 查看日志
docker-compose logs -f backend
```

### 2. 运行数据库迁移
```bash
# 运行所有迁移（包括 notebooks 表）
docker-compose exec backend alembic upgrade head

# 验证迁移成功
docker-compose exec backend alembic current
# 应该显示: 005_notebook (head)
```

### 3. 确认服务健康
```bash
curl http://localhost:8000/api/health
# 预期: {"status": "healthy", ...}
```

---

## 持久化验证测试

### 重启后数据保留测试
```bash
# 1. 创建 Notebook 并执行代码
TOKEN="your_access_token"
NB=$(curl -s -X POST http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"持久化测试"}')
NOTEBOOK_ID=$(echo $NB | jq -r '.id')

# 2. 执行代码
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"code":"x = 42\nprint(x)"}'

# 3. 重启容器
docker-compose restart backend

# 4. 验证 Notebook 仍然存在
curl -s http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID \
  -H "Authorization: Bearer $TOKEN" | jq .title
# 应该返回: "持久化测试"

# 5. 验证 Cell 输出已保存
curl -s http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID \
  -H "Authorization: Bearer $TOKEN" | jq '.cells[0].outputs'
# 应该包含之前执行的输出
```

---

## 一、基础功能测试

### 1.1 用户认证
```bash
# 注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","username":"testuser","password":"test123456"}'

# 登录
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"test123456"}'
# 保存返回的 access_token
```

### 1.2 Notebook CRUD
```bash
TOKEN="your_access_token"

# 创建 Notebook
curl -X POST http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"测试 Notebook"}'

# 列出 Notebooks
curl http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer $TOKEN"

# 获取单个 Notebook
curl http://localhost:8000/api/codelab/notebooks/{notebook_id} \
  -H "Authorization: Bearer $TOKEN"

# 删除 Notebook
curl -X DELETE http://localhost:8000/api/codelab/notebooks/{notebook_id} \
  -H "Authorization: Bearer $TOKEN"
```

### 1.3 代码执行
```bash
NOTEBOOK_ID="your_notebook_id"
CELL_ID="your_cell_id"

# 执行单元格
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/cells/$CELL_ID/execute" \
  -H "Authorization: Bearer $TOKEN"

# 直接执行代码
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"code":"print(\"Hello World\")\n1 + 1"}'
```

---

## 二、Agent 工具测试

### 2.1 未授权模式测试

**测试目标**: 验证未授权时 Agent 只提供代码建议，不执行代码

```bash
# 发送消息 (user_authorized=false)
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "帮我生成一段数据可视化代码",
    "user_authorized": false,
    "stream": false
  }'
```

**预期行为**:
- ✅ Agent 提供代码建议
- ✅ 不调用 `notebook_execute` 工具
- ✅ 提示用户开启授权以执行代码

### 2.2 授权模式测试

**测试目标**: 验证授权后 Agent 可以执行代码

```bash
# 发送消息 (user_authorized=true)
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "执行 print(1+1) 并告诉我结果",
    "user_authorized": true,
    "stream": false
  }'
```

**预期行为**:
- ✅ Agent 调用 `notebook_execute` 工具
- ✅ 代码成功执行
- ✅ 返回执行结果 "2"

### 2.3 notebook_variables 工具测试

```bash
# 先执行一些代码创建变量
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"code":"x = 10\ny = [1,2,3]\nimport pandas as pd\ndf = pd.DataFrame({\"a\":[1,2,3]})"}'

# 让 Agent 查看变量
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "当前有哪些变量？",
    "user_authorized": false
  }'
```

**预期行为**:
- ✅ Agent 调用 `notebook_variables` 工具
- ✅ 返回变量列表: x (int), y (list), df (DataFrame)

### 2.4 pip_install 工具测试

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "帮我安装 seaborn 库",
    "user_authorized": true
  }'
```

**预期行为**:
- ✅ Agent 调用 `pip_install` 工具
- ✅ seaborn 在白名单中，安装成功
- ✅ 返回安装成功信息

**安全测试** - 尝试安装非白名单包:
```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "帮我安装 malicious-package",
    "user_authorized": true
  }'
```

**预期行为**:
- ✅ 拒绝安装，提示包不在白名单

### 2.5 web_scrape 工具测试

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "爬取 https://httpbin.org/html 的内容",
    "user_authorized": true
  }'
```

**预期行为**:
- ✅ Agent 调用 `web_scrape` 工具
- ✅ 返回网页文本内容

**安全测试** - 尝试访问内部地址:
```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "爬取 http://localhost:8000/api/health",
    "user_authorized": true
  }'
```

**预期行为**:
- ✅ 拒绝访问，提示内部地址被阻止

### 2.6 code_analysis 工具测试

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "分析这段代码的问题: for i in range(len(lst)): print(lst[i])",
    "user_authorized": false
  }'
```

**预期行为**:
- ✅ Agent 调用 `code_analysis` 工具
- ✅ 识别出 `range(len())` 反模式
- ✅ 提供优化建议

### 2.7 literature_search 工具测试

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "搜索关于 transformer 架构的论文",
    "user_authorized": false
  }'
```

**预期行为**:
- ✅ Agent 调用 `literature_search` 工具
- ✅ 返回相关论文列表

---

## 三、流式响应测试

### 3.1 SSE 流式测试

使用浏览器或专用工具测试 SSE:

```javascript
// 浏览器控制台测试
const eventSource = new EventSource(
  'http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/chat?message=hello&user_authorized=true',
  { headers: { 'Authorization': 'Bearer your_token' } }
);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(data.type, data);
};
```

**预期事件序列**:
1. `start` - 开始事件 (provider, model)
2. `thought` - Agent 思考过程
3. `action` - 工具调用 (tool, input)
4. `observation` - 工具结果
5. `answer` - 最终回答
6. `done` - 结束事件

---

## 四、端到端场景测试

### 4.1 数据分析工作流

```
用户输入: "生成一个示例数据集，然后进行统计分析和可视化"
```

**预期流程**:
1. Agent 调用 `notebook_execute` 生成数据
2. Agent 调用 `notebook_execute` 进行统计分析
3. Agent 调用 `notebook_execute` 生成图表
4. 返回分析结果和可视化说明

### 4.2 错误处理工作流

```
用户输入: "执行 print(undefined_variable)"
```

**预期流程**:
1. Agent 调用 `notebook_execute`
2. 执行失败，返回 NameError
3. Agent 分析错误原因
4. 提供修复建议

### 4.3 多轮对话测试

```
对话1: "创建一个 DataFrame df，包含姓名和年龄两列"
对话2: "给 df 添加一列工资"  
对话3: "计算平均工资"
```

**预期行为**:
- ✅ 变量 `df` 在多轮对话中保持
- ✅ 每次修改都基于上一次的状态

---

## 五、性能测试

### 5.1 并发测试

```bash
# 使用 Apache Bench 进行并发测试
ab -n 100 -c 10 -H "Authorization: Bearer $TOKEN" \
   http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID

# 预期: 响应时间 < 500ms, 错误率 < 1%
```

### 5.2 长代码执行测试

```python
# 执行耗时操作
code = """
import time
for i in range(5):
    print(f"Step {i}")
    time.sleep(1)
print("Done")
"""
```

**预期行为**:
- ✅ 在 60s 超时内完成
- ✅ 流式返回输出

---

## 六、持久化测试

### 6.1 重启后数据保留测试

```bash
# 1. 创建 Notebook 并执行代码
# 2. 记录 notebook_id
# 3. 重启容器
docker-compose restart backend

# 4. 检查 Notebook 是否存在
curl http://localhost:8000/api/codelab/notebooks/$NOTEBOOK_ID \
  -H "Authorization: Bearer $TOKEN"
```

**当前状态**: ⚠️ 未完全集成数据库，重启会丢失
**完成后预期**: ✅ Notebook 和 Cell 数据保留

---

## 七、错误场景测试

| 场景 | 输入 | 预期结果 |
|------|------|----------|
| 无效 notebook_id | 随机 UUID | 404 Notebook 不存在 |
| 无授权 Token | 不带 Authorization | 401 Unauthorized |
| 语法错误代码 | `print(` | 返回 SyntaxError |
| 超时代码 | `while True: pass` | 60s 后超时错误 |
| 空消息 | `""` | 400 Bad Request |

---

## 八、前端集成测试

### 8.1 UI 测试清单

- [ ] 创建新 Notebook
- [ ] 添加/删除/移动单元格
- [ ] 执行代码并显示输出
- [ ] 显示图表 (matplotlib)
- [ ] Agent 面板开关
- [ ] 授权开关状态切换
- [ ] 流式显示 Agent 思考过程
- [ ] 代码块复制/插入/运行按钮
- [ ] 变量查看器

### 8.2 浏览器兼容性

- [ ] Chrome (最新版)
- [ ] Firefox (最新版)
- [ ] Safari (最新版)
- [ ] Edge (最新版)

---

## 九、日志检查点

### 后端日志应包含:
```
INFO  | [WebSearch] Serper API Key 已配置
INFO  | 已注册 Notebook 工具集，授权状态: True/False
INFO  | ReAct 迭代 1/10
INFO  | [ReAct] 思考完成: ...
INFO  | [ReAct] 执行工具: notebook_execute
INFO  | [NotebookExecute] 代码: ...
INFO  | 工具执行完成: notebook_execute, 成功: True
INFO  | [ReAct] 回答完成
```

### 不应出现的错误:
```
ERROR | 'CellOutput' object has no attribute 'get'
ERROR | 'str' object has no attribute 'get'  
ERROR | 'coroutine' object has no attribute 'provider'
ERROR | 'LLMService' object has no attribute 'model'
```

---

## 十、测试报告模板

```markdown
# CodeLab Agent 测试报告

**测试日期**: YYYY-MM-DD
**测试版本**: commit_hash
**测试环境**: Docker / Local

## 测试结果汇总

| 类别 | 通过 | 失败 | 跳过 |
|------|------|------|------|
| 基础功能 | X | X | X |
| Agent 工具 | X | X | X |
| 流式响应 | X | X | X |
| 端到端场景 | X | X | X |
| 性能测试 | X | X | X |
| 错误处理 | X | X | X |

## 发现的问题

1. [BUG] 问题描述
   - 复现步骤
   - 预期行为
   - 实际行为

## 建议改进

1. 建议内容
```

---

## 快速测试脚本

```bash
#!/bin/bash
# test_codelab_agent.sh

BASE_URL="http://localhost:8000"
TOKEN="your_token_here"

echo "=== 1. 健康检查 ==="
curl -s $BASE_URL/api/health | jq .

echo "=== 2. 创建 Notebook ==="
NB=$(curl -s -X POST $BASE_URL/api/codelab/notebooks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Agent Test"}')
NOTEBOOK_ID=$(echo $NB | jq -r '.id')
echo "Notebook ID: $NOTEBOOK_ID"

echo "=== 3. 执行简单代码 ==="
curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"code":"x = 42\nprint(x)"}' | jq .

echo "=== 4. Agent 对话 (未授权) ==="
curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"查看当前变量","user_authorized":false,"stream":false}' | jq .

echo "=== 5. Agent 对话 (已授权) ==="
curl -s -X POST "$BASE_URL/api/codelab/notebooks/$NOTEBOOK_ID/agent/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"计算 x * 2 并打印结果","user_authorized":true,"stream":false}' | jq .

echo "=== 测试完成 ==="
```

将此脚本保存为 `test_codelab_agent.sh` 并执行:
```bash
chmod +x test_codelab_agent.sh
./test_codelab_agent.sh
```
