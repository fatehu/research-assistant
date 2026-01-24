# CodeLab AI 助手升级 - ReAct Agent

## 概述

本次升级将代码实验室的 AI 助手从基础的 LLM 聊天模式升级为完整的 ReAct Agent，具备以下能力：

- ✅ **Notebook 操控**: 直接执行代码、操作单元格、查看变量
- ✅ **pip 安装**: 在内核中安装 Python 包（白名单限制）
- ✅ **网页爬取**: 抓取网页内容用于数据分析
- ✅ **文献搜索**: 多源学术论文搜索（Semantic Scholar, arXiv, PubMed 等）
- ✅ **代码分析**: 语法检查、风格建议、性能优化
- ✅ **授权控制**: 用户可控制 AI 是否有权限操作 Notebook

## 架构变更

### 后端

#### 1. 新增文件

**`backend/app/services/notebook_tools.py`** (1327 行)

包含以下工具类：

| 工具 | 功能 | 授权要求 |
|------|------|----------|
| `NotebookExecuteTool` | 在 Notebook 内核执行 Python 代码 | ✅ 需要授权 |
| `NotebookVariablesTool` | 获取当前变量状态 | ❌ 无需授权 |
| `NotebookCellTool` | 操作单元格（添加/删除/更新） | ✅ 需要授权 |
| `PipInstallTool` | 安装 Python 包 | ✅ 需要授权 |
| `WebScrapeTool` | 爬取网页内容 | ❌ 无需授权 |
| `CodeAnalysisTool` | 代码质量分析 | ❌ 无需授权 |
| `EnhancedLiteratureSearchTool` | 学术文献搜索 | ❌ 无需授权 |

#### 2. 修改文件

**`backend/app/services/agent_tools.py`**

- `ToolRegistry` 类新增参数：
  - `notebook_id`: Notebook ID
  - `kernel_manager`: 内核管理器
  - `notebooks_store`: Notebook 存储
  - `user_authorized`: 用户授权状态
- 新增 `_register_notebook_tools()` 方法

**`backend/app/api/codelab.py`**

新增 Agent 相关端点：

| 端点 | 方法 | 功能 |
|------|------|------|
| `/notebooks/{id}/agent/context` | GET | 获取 Notebook 上下文 |
| `/notebooks/{id}/agent/history` | GET | 获取对话历史 |
| `/notebooks/{id}/agent/history` | DELETE | 清空对话历史 |
| `/notebooks/{id}/agent/chat` | POST | Agent 对话（流式） |
| `/notebooks/{id}/agent/suggest-code` | POST | 生成代码建议 |
| `/notebooks/{id}/agent/explain-error` | POST | 解释错误 |
| `/notebooks/{id}/agent/analyze-data` | POST | 分析数据变量 |

### 前端

**`frontend/src/services/api.ts`**

更新类型定义：
- `AgentChatRequest`: 新增 `user_authorized` 字段
- `AgentChatEvent`: 新增事件类型（thought, action, observation, answer, authorization_required）

**`frontend/src/components/NotebookAgentPanel.tsx`**

已有功能（无需修改）：
- 授权开关 UI
- 事件处理逻辑
- 流式响应显示

## 安全机制

### pip 安装白名单

仅允许安装以下类别的包：

```python
ALLOWED_PACKAGES = {
    # 数据科学: numpy, pandas, scipy, statsmodels
    # 可视化: matplotlib, seaborn, plotly, bokeh
    # 机器学习: scikit-learn, xgboost, lightgbm
    # 深度学习: torch, tensorflow, transformers
    # NLP: nltk, spacy, jieba
    # 网络: requests, httpx, beautifulsoup4
    # ... 共 60+ 个常用包
}
```

### 网页爬取限制

- 阻止访问: localhost, 127.0.0.1, 内网域名
- 超时: 30 秒
- 内容截断: 默认 5000 字符
- 必须使用 User-Agent

### 授权控制

- **两级授权模型**:
  - 只读操作（查看变量、分析代码）: 无需授权
  - 修改操作（执行代码、安装包、操作单元格）: 需要用户授权
- 授权通过前端开关控制，传递到后端 API
- 授权状态不持久化，每次会话独立

## 使用示例

### 1. 数据分析工作流

```
用户: 分析这个 CSV 文件的数据分布

Agent: 
<think>需要先加载数据，然后分析分布</think>
<action>{"tool": "notebook_execute", "input": {"code": "df = pd.read_csv('data.csv')\ndf.describe()"}}</action>
[Observation: 执行成功，显示统计信息]
<think>已获取统计信息，生成可视化</think>
<action>{"tool": "notebook_execute", "input": {"code": "df.hist(figsize=(12,8))\nplt.show()"}}</action>
[Observation: 图表已生成]
<answer>数据分析完成。数据集包含 X 行 Y 列...</answer>
```

### 2. 网页爬取 + 分析

```
用户: 爬取这个网页的表格数据并分析

Agent:
<action>{"tool": "web_scrape", "input": {"url": "https://...", "extract": "tables"}}</action>
[Observation: 提取到 3 个表格]
<action>{"tool": "notebook_execute", "input": {"code": "df = pd.DataFrame(...)\ndf.head()"}}</action>
<answer>已成功爬取并加载数据...</answer>
```

### 3. 包安装

```
用户: 我需要使用 seaborn 库

Agent:
<action>{"tool": "pip_install", "input": {"packages": ["seaborn"]}}</action>
[需要授权 → 用户确认]
[Observation: 安装成功]
<action>{"tool": "notebook_execute", "input": {"code": "import seaborn as sns\nsns.set_style('darkgrid')"}}</action>
<answer>seaborn 已安装并配置完成</answer>
```

## 依赖

无需安装额外依赖，所有必需包已在 `requirements.txt` 中：

```
beautifulsoup4==4.12.3
httpx==0.27.0
lxml  # via beautifulsoup4
```

## 测试

启动服务后，可以通过以下方式测试：

1. 打开 CodeLab 页面
2. 创建新 Notebook
3. 打开 AI 助手面板
4. 开启「允许 AI 操作」开关
5. 发送消息测试工具调用

## 故障排除

### Agent 不调用工具

检查：
- LLM 服务是否正常
- 工具注册是否成功（查看日志）
- 系统提示词是否包含工具列表

### 工具执行失败

检查：
- 授权状态是否开启
- 内核是否运行
- 包是否在白名单中

### 流式响应中断

检查：
- SSE 连接是否超时
- 代理/负载均衡器配置
