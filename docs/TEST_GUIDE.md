# CodeLab Agent AI 功能测试指南

## 测试环境准备

### 前端环境

```bash
cd frontend
npm install
npm run dev
```

### 后端环境

```bash
cd backend
pip install -r requirements.txt

# 配置 LLM 服务 (在 .env 或环境变量中)
export OPENAI_API_KEY="your-api-key"
# 或其他 LLM 服务配置

python -m uvicorn app.main:app --reload
```

---

## 功能测试清单

### 1. 顶部信息页面测试

#### 1.1 欢迎区域显示

**测试步骤:**
1. 打开 CodeLab 页面
2. 确认在没有选择 Notebook 时显示欢迎区域

**预期结果:**
- [x] 显示标题 "Code Lab"
- [x] 显示副标题 "交互式 Python 开发环境"
- [x] 显示统计信息卡片
- [x] 显示功能介绍卡片

#### 1.2 统计信息准确性

**测试步骤:**
1. 创建多个 Notebook
2. 在不同 Notebook 中添加 cells
3. 执行一些 cells
4. 返回主页查看统计信息

**预期结果:**
- [x] Notebooks 数量正确
- [x] 总单元格数正确
- [x] 总执行次数正确

#### 1.3 功能卡片动画

**测试步骤:**
1. 刷新页面
2. 观察功能卡片的出现动画

**预期结果:**
- [x] 卡片依次淡入显示
- [x] 悬停时有轻微放大效果
- [x] 渐变背景正常显示

---

### 2. 性能优化测试

#### 2.1 编辑器输入响应测试

**测试步骤:**
1. 创建一个 Notebook，添加 20+ 个 cells
2. 选择一个 cell，快速输入代码
3. 观察输入延迟

**预期结果:**
- [x] 输入无明显卡顿
- [x] 字符即时显示
- [x] 其他 cells 不会因为输入而闪烁

#### 2.2 Cell 状态更新测试

**测试步骤:**
1. 运行一段需要较长时间的代码
2. 在等待结果时，编辑另一个 cell
3. 观察 UI 响应

**预期结果:**
- [x] 编辑操作不被阻塞
- [x] 输出结果正常显示
- [x] 没有明显的 UI 冻结

#### 2.3 大量数据渲染测试

**测试步骤:**
1. 运行代码产生大量输出（如 print 1000 行）
2. 观察输出渲染过程
3. 同时进行其他操作

**预期结果:**
- [x] 输出逐步渲染
- [x] 不会阻塞用户交互
- [x] 滚动流畅

---

### 3. Agent AI 助手测试

#### 3.1 面板开关测试

**测试步骤:**
1. 打开一个 Notebook
2. 点击工具栏中的 "AI 助手" 按钮
3. 点击面板右上角的关闭按钮

**预期结果:**
- [x] 面板从右侧滑入
- [x] 面板从右侧滑出
- [x] 动画流畅

#### 3.2 快捷操作测试

**测试步骤:**
1. 打开 Agent 面板
2. 点击各个快捷操作按钮

**预期结果:**
- [x] "分析数据" - 发送相应的 prompt
- [x] "数据可视化" - 发送相应的 prompt
- [x] "数据清洗" - 发送相应的 prompt
- [x] "建模建议" - 发送相应的 prompt
- [x] "解释代码" - 发送相应的 prompt
- [x] "优化代码" - 发送相应的 prompt

#### 3.3 对话功能测试

**测试步骤:**
1. 在输入框中输入问题
2. 按 Enter 发送
3. 观察响应

**预期结果:**
- [x] 消息立即显示在对话区
- [x] 显示加载指示器
- [x] AI 响应流式显示
- [x] 代码块正确高亮

#### 3.4 代码操作测试

**测试步骤:**
1. 让 AI 生成一段代码
2. 悬停在代码块上
3. 测试复制、插入、运行按钮

**预期结果:**
- [x] "复制" - 代码复制到剪贴板
- [x] "插入到 Notebook" - 代码插入到当前 cell 或新 cell
- [x] "运行代码" - 代码插入并执行

#### 3.5 面板展开/收起测试

**测试步骤:**
1. 点击面板右上角的展开按钮
2. 再次点击收起

**预期结果:**
- [x] 面板宽度从 400px 变为 600px
- [x] 面板宽度从 600px 变回 400px
- [x] 动画过渡自然

---

### 4. Notebook 控制功能测试

#### 4.1 Cell 选择器测试

**测试步骤:**
1. 创建多个 cells
2. 打开 Agent 面板
3. 使用 Cell 选择器切换 cell

**预期结果:**
- [x] 选择器显示所有 cells
- [x] 显示 cell 类型和预览
- [x] 切换后 Notebook 中对应 cell 被选中
- [x] 显示成功提示

#### 4.2 清除输出测试

**测试步骤:**
1. 运行多个 cells 产生输出
2. 点击 Agent 面板中的 "清除所有输出" 按钮

**预期结果:**
- [x] 所有 cell 输出被清除
- [x] 显示成功提示
- [x] Notebook 立即更新

#### 4.3 添加 Cell 测试

**测试步骤:**
1. 选择一个 cell
2. 点击 "添加代码 Cell" 按钮
3. 点击 "添加 Markdown Cell" 按钮

**预期结果:**
- [x] 在当前 cell 之后插入新的代码 cell
- [x] 在当前 cell 之后插入新的 Markdown cell
- [x] 新 cell 自动获得焦点
- [x] Cell 计数器更新

#### 4.4 当前位置显示测试

**测试步骤:**
1. 观察 Agent 面板中的 Cell 位置标签
2. 切换不同的 cell

**预期结果:**
- [x] 显示 "Cell X/Y" 格式
- [x] 切换时实时更新

---

### 5. 流式响应测试

#### 5.1 长响应测试

**测试步骤:**
1. 询问 AI 一个需要长篇回答的问题
2. 观察响应过程

**预期结果:**
- [x] 文字逐步显示
- [x] 光标指示器闪烁
- [x] 完成后光标消失

#### 5.2 停止生成测试

**测试步骤:**
1. 发送一个请求
2. 在 AI 响应过程中点击停止按钮

**预期结果:**
- [x] 响应立即停止
- [x] 已生成的内容保留
- [x] 可以发送新消息

#### 5.3 错误处理测试

**测试步骤:**
1. 断开网络连接
2. 发送消息
3. 重新连接网络

**预期结果:**
- [x] 显示错误提示
- [x] 不会崩溃
- [x] 恢复连接后可继续使用

---

### 6. 对话历史测试

#### 6.1 历史加载测试

**测试步骤:**
1. 进行几轮对话
2. 关闭 Agent 面板
3. 重新打开 Agent 面板

**预期结果:**
- [x] 对话历史被保留
- [x] 显示加载指示器
- [x] 历史消息正确显示

#### 6.2 清空历史测试

**测试步骤:**
1. 确保有对话历史
2. 点击清空对话按钮
3. 确认操作

**预期结果:**
- [x] 显示确认对话框
- [x] 确认后历史被清空
- [x] 显示空状态提示

#### 6.3 刷新上下文测试

**测试步骤:**
1. 进行对话
2. 在 Notebook 中执行新代码
3. 点击刷新上下文按钮

**预期结果:**
- [x] 上下文被刷新
- [x] AI 能感知到新的变量和输出

---

### 7. API 端点测试

使用 curl 或 Postman 进行 API 测试:

#### 7.1 获取上下文

```bash
curl -X GET "http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/context" \
     -H "Authorization: Bearer {token}"
```

**预期响应:**
```json
{
  "notebook_id": "xxx",
  "notebook_title": "My Notebook",
  "cell_count": 5,
  "execution_count": 3,
  "variables": {"df": "DataFrame", "x": "int"},
  "recent_outputs": [...],
  "code_summary": "..."
}
```

#### 7.2 获取历史

```bash
curl -X GET "http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/history" \
     -H "Authorization: Bearer {token}"
```

#### 7.3 清空历史

```bash
curl -X DELETE "http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/history" \
     -H "Authorization: Bearer {token}"
```

#### 7.4 对话 (非流式)

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/chat" \
     -H "Authorization: Bearer {token}" \
     -H "Content-Type: application/json" \
     -d '{"message": "分析当前数据", "stream": false}'
```

#### 7.5 代码建议

```bash
curl -X POST "http://localhost:8000/api/codelab/notebooks/{notebook_id}/agent/suggest-code?description=画一个折线图" \
     -H "Authorization: Bearer {token}"
```

---

### 8. 边界情况测试

#### 8.1 空 Notebook 测试

**测试步骤:**
1. 创建新的空 Notebook
2. 打开 Agent 面板
3. 进行对话

**预期结果:**
- [x] 正常显示
- [x] Cell 选择器显示空或禁用
- [x] AI 能处理空上下文

#### 8.2 大量 Cells 测试

**测试步骤:**
1. 创建包含 50+ cells 的 Notebook
2. 打开 Agent 面板
3. 测试各项功能

**预期结果:**
- [x] Cell 选择器能显示所有选项
- [x] 性能无明显下降
- [x] 切换 cell 响应及时

#### 8.3 长代码块测试

**测试步骤:**
1. 让 AI 生成很长的代码
2. 观察渲染效果

**预期结果:**
- [x] 代码块正确显示
- [x] 滚动条出现
- [x] 复制功能正常

#### 8.4 特殊字符测试

**测试步骤:**
1. 发送包含特殊字符的消息（如 emoji、中文、代码）
2. 观察显示效果

**预期结果:**
- [x] 正确显示所有字符
- [x] 代码正确高亮
- [x] 不会出现乱码

---

## 性能基准测试

### 测试环境

- 浏览器: Chrome 最新版
- 设备: MacBook Pro / Windows PC
- 网络: 稳定 WiFi

### 测试指标

| 指标 | 目标 | 实际值 |
|------|------|--------|
| 首次渲染时间 | < 500ms | ___ms |
| Cell 编辑输入延迟 | < 50ms | ___ms |
| Agent 面板打开时间 | < 200ms | ___ms |
| 消息发送到显示响应 | < 1s | ___ms |
| 代码插入时间 | < 100ms | ___ms |

### 使用 React DevTools 分析

1. 安装 React DevTools 浏览器扩展
2. 打开 Profiler 标签
3. 录制操作过程
4. 分析组件渲染时间

---

## 已知问题

1. **问题**: [描述问题]
   **状态**: 待修复
   **临时解决方案**: [如果有]

---

## 测试报告模板

```
测试日期: ____年__月__日
测试人员: ________
浏览器版本: ________
操作系统: ________

功能测试结果:
- 顶部信息页面: ✅/❌
- 性能优化: ✅/❌
- Agent AI 助手: ✅/❌
- Notebook 控制: ✅/❌
- 流式响应: ✅/❌
- 对话历史: ✅/❌
- API 端点: ✅/❌

发现的问题:
1. 
2. 

建议:
1. 
2. 
```

---

## 联系方式

如有问题，请联系开发团队。
