# CodeLab & Notebook ReAct Agent 维护文档

## 目录

1. [系统概述](#1-系统概述)
2. [架构设计](#2-架构设计)
3. [核心组件](#3-核心组件)
4. [数据模型](#4-数据模型)
5. [API 端点](#5-api-端点)
6. [Agent 工具](#6-agent-工具)
7. [上下文管理](#7-上下文管理)
8. [持久化机制](#8-持久化机制)
9. [实时更新机制](#9-实时更新机制)
10. [安全机制](#10-安全机制)
11. [性能优化](#11-性能优化)
12. [故障排查](#12-故障排查)
13. [扩展开发](#13-扩展开发)

---

## 1. 系统概述

### 1.1 功能定位

CodeLab 是一个类 Jupyter Notebook 的交互式代码实验环境，集成了 AI Agent 能力，支持：

- **交互式代码执行**: 支持 Python 代码的即时执行，Cell 之间共享变量
- **AI 辅助编程**: 通过 ReAct Agent 自动执行代码、分析数据、生成图表
- **数据持久化**: Notebook 和执行结果持久化到数据库，重启不丢失
- **实时协作**: AI 执行的代码和结果实时显示在用户界面

### 1.2 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + TypeScript + Tailwind CSS + Ant Design |
| 后端 | FastAPI + SQLAlchemy + asyncio |
| 数据库 | PostgreSQL |
| AI | OpenAI API / 兼容 API |
| 实时通信 | Server-Sent Events (SSE) |

---

## 2. 架构设计

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          前端 (React)                            │
├─────────────────────────────────────────────────────────────────┤
│  CodeLabPage.tsx          NotebookAgentPanel.tsx                │
│  - Notebook 列表          - AI 对话面板                          │
│  - Cell 编辑器            - 实时消息流                           │
│  - 代码执行               - 工具调用显示                         │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP / SSE
┌────────────────────────────▼────────────────────────────────────┐
│                          后端 (FastAPI)                          │
├─────────────────────────────────────────────────────────────────┤
│  codelab.py (API)                                                │
│  ├── Notebook CRUD                                               │
│  ├── Cell 执行                                                   │
│  └── Agent 对话 (SSE)                                            │
├─────────────────────────────────────────────────────────────────┤
│  react_agent.py (ReAct Agent)                                    │
│  ├── 思考 (Thought)                                              │
│  ├── 行动 (Action)                                               │
│  └── 观察 (Observation)                                          │
├─────────────────────────────────────────────────────────────────┤
│  notebook_tools.py (Agent 工具)                                  │
│  ├── NotebookExecuteTool   - 执行代码                            │
│  ├── NotebookVariablesTool - 查看变量                            │
│  ├── NotebookCellTool      - 操作 Cell                           │
│  ├── PipInstallTool        - 安装包                              │
│  ├── WebScrapeTool         - 爬取网页                            │
│  └── CodeAnalysisTool      - 代码分析                            │
├─────────────────────────────────────────────────────────────────┤
│  PythonKernel (执行内核)                                         │
│  └── 每个 Notebook 一个独立的命名空间                             │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                       数据层                                     │
├─────────────────────────────────────────────────────────────────┤
│  内存缓存 (_notebooks_cache)     PostgreSQL (notebooks 表)       │
│  - 快速读取                      - 持久化存储                     │
│  - Agent 工具访问                - 重启后恢复                     │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
用户输入 "画一个正弦波"
         │
         ▼
┌─────────────────┐
│ NotebookAgent   │ ── POST /agent/chat (SSE)
│ Panel (前端)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ ReActAgent      │ ── 1. 解析用户意图
│ (后端)          │ ── 2. 选择工具: notebook_execute
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ NotebookExecute │ ── 3. 生成 matplotlib 代码
│ Tool            │ ── 4. 在 PythonKernel 中执行
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ PythonKernel    │ ── 5. 执行代码，捕获输出
│                 │ ── 6. 处理图像 (base64)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ _notebooks_cache│ ── 7. 创建新 Cell，添加到缓存
│ + PostgreSQL    │ ── 8. 同步到数据库
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ SSE 事件流      │ ── 9. 发送 observation 事件
│ (new_cell 数据) │     包含完整的 Cell 对象
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ 前端 onAddCell  │ ── 10. 直接追加到 UI
│ 回调            │ ── 11. 自动滚动到新 Cell
└─────────────────┘
```

---

## 3. 核心组件

### 3.1 PythonKernel (执行内核)

**文件**: `backend/app/api/codelab.py`

```python
class PythonKernel:
    """
    Python 执行内核 - 为每个 Notebook 维护一个持久化的执行上下文
    所有 cell 共享同一个命名空间，变量在 cell 之间保持
    """
    
    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id
        self.execution_count = 0
        self.namespace: Dict[str, Any] = {}  # 共享命名空间
        self._initialize_namespace()
```

**特性**:
- 每个 Notebook 独立的执行环境
- Cell 之间共享变量
- 预导入常用库: numpy, pandas, matplotlib 等
- 支持图像输出 (base64 编码)
- 超时控制 (默认 30 秒)

**命名空间初始化**:
```python
# 预导入的库
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import torch  # 如果可用
import sklearn  # 如果可用
```

### 3.2 KernelManager (内核管理器)

```python
class KernelManager:
    """管理所有 Notebook 的执行内核"""
    _kernels: Dict[str, PythonKernel] = {}
    
    def get_or_create_kernel(notebook_id: str) -> PythonKernel
    def destroy_kernel(notebook_id: str) -> None
    def reset_kernel(notebook_id: str) -> None
    def get_kernel(notebook_id: str) -> Optional[PythonKernel]
```

**内核生命周期**:
1. **创建**: 首次访问 Notebook 时自动创建
2. **重用**: 同一 Notebook 的多次执行复用同一内核
3. **重置**: 用户点击"重启内核"时清空命名空间
4. **销毁**: 删除 Notebook 时销毁

### 3.3 ReActAgent (推理-行动 Agent)

**文件**: `backend/app/services/react_agent.py`

```python
class ReActAgent:
    """
    ReAct (Reasoning + Acting) 框架实现
    
    流程:
    1. Thought: 分析问题
    2. Action: 选择并执行工具
    3. Observation: 观察结果
    4. 重复 1-3 直到完成
    5. Answer: 输出最终回答
    """
```

**状态机**:
```
IDLE → THINKING → ACTING → OBSERVING → THINKING → ... → ANSWERING → DONE
                                                    ↓
                                                  ERROR
```

**输出格式**:
```xml
<think>分析用户需求...</think>
<action>{"tool": "notebook_execute", "input": {"code": "..."}}</action>

<!-- 收到工具结果后 -->
<think>根据执行结果分析...</think>
<answer>代码已执行，结果显示...</answer>
```

### 3.4 NotebookService (数据库服务)

**文件**: `backend/app/services/notebook_service.py`

```python
class NotebookService:
    """Notebook 数据库操作服务"""
    
    async def get_user_notebooks(user_id: int) -> List[Dict]
    async def get_notebook(notebook_id: str, user_id: int) -> Optional[Dict]
    async def create_notebook(user_id, title, description) -> Dict
    async def update_notebook(notebook_id, user_id, title, description) -> Dict
    async def delete_notebook(notebook_id, user_id) -> bool
    async def add_cell(notebook_id, user_id, cell_type, source, index) -> Dict
    async def update_cell(notebook_id, user_id, cell_id, ...) -> Dict
    async def delete_cell(notebook_id, user_id, cell_id) -> Dict
    async def save_cell_execution(notebook_id, user_id, cell_id, outputs, execution_count) -> Dict
```

---

## 4. 数据模型

### 4.1 数据库表结构

**notebooks 表**:
```sql
CREATE TABLE notebooks (
    id VARCHAR(36) PRIMARY KEY,           -- UUID
    user_id INTEGER NOT NULL REFERENCES users(id),
    title VARCHAR(255) DEFAULT 'Untitled Notebook',
    description TEXT,
    execution_count INTEGER DEFAULT 0,
    metadata JSON DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_notebooks_user_id ON notebooks(user_id);
```

**notebook_cells 表**:
```sql
CREATE TABLE notebook_cells (
    id VARCHAR(36) PRIMARY KEY,           -- UUID
    notebook_id VARCHAR(36) NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    cell_type VARCHAR(20) DEFAULT 'code', -- 'code' | 'markdown'
    source TEXT DEFAULT '',
    execution_count INTEGER,
    outputs JSON DEFAULT '[]',
    metadata JSON DEFAULT '{}',
    position INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_cells_notebook_id ON notebook_cells(notebook_id);
CREATE INDEX idx_cells_position ON notebook_cells(position);
```

### 4.2 ORM 模型

```python
# backend/app/models/notebook.py

class Notebook(Base):
    __tablename__ = "notebooks"
    
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255))
    description = Column(Text)
    execution_count = Column(Integer, default=0)
    notebook_metadata = Column("metadata", JSON)  # 避免保留字
    
    cells = relationship("NotebookCell", back_populates="notebook",
                        cascade="all, delete-orphan",
                        order_by="NotebookCell.position")

class NotebookCell(Base):
    __tablename__ = "notebook_cells"
    
    id = Column(String(36), primary_key=True)
    notebook_id = Column(String(36), ForeignKey("notebooks.id", ondelete="CASCADE"))
    cell_type = Column(String(20), default="code")
    source = Column(Text, default="")
    execution_count = Column(Integer, nullable=True)
    outputs = Column(JSON, default=list)
    cell_metadata = Column("metadata", JSON)  # 避免保留字
    position = Column(Integer, default=0)
```

### 4.3 Cell 输出格式

```python
# CellOutput 结构
{
    "output_type": "stream" | "execute_result" | "display_data" | "error",
    "content": "输出内容",
    "mime_type": "text/plain" | "image/png" | "text/html" | ...
}

# 示例: 文本输出
{"output_type": "stream", "content": "Hello, World!", "mime_type": "text/plain"}

# 示例: 图像输出
{"output_type": "display_data", "content": "base64编码...", "mime_type": "image/png"}

# 示例: 错误输出
{"output_type": "error", "content": "Traceback: ...", "mime_type": "text/plain"}
```

---

## 5. API 端点

### 5.1 Notebook 管理

| 方法 | 端点 | 描述 |
|------|------|------|
| GET | `/api/codelab/notebooks` | 获取用户所有 Notebook |
| POST | `/api/codelab/notebooks` | 创建新 Notebook |
| GET | `/api/codelab/notebooks/{id}` | 获取 Notebook 详情 |
| PATCH | `/api/codelab/notebooks/{id}` | 更新 Notebook |
| DELETE | `/api/codelab/notebooks/{id}` | 删除 Notebook |

### 5.2 Cell 操作

| 方法 | 端点 | 描述 |
|------|------|------|
| POST | `/api/codelab/notebooks/{id}/cells` | 添加新 Cell |
| DELETE | `/api/codelab/notebooks/{id}/cells/{cell_id}` | 删除 Cell |
| POST | `/api/codelab/notebooks/{id}/execute` | 执行代码 |
| POST | `/api/codelab/notebooks/{id}/run-all` | 执行所有 Cell |

### 5.3 内核操作

| 方法 | 端点 | 描述 |
|------|------|------|
| POST | `/api/codelab/notebooks/{id}/restart-kernel` | 重启内核 |
| GET | `/api/codelab/notebooks/{id}/kernel-status` | 获取内核状态 |
| POST | `/api/codelab/notebooks/{id}/interrupt` | 中断执行 |

### 5.4 Agent 对话

| 方法 | 端点 | 描述 |
|------|------|------|
| POST | `/api/codelab/notebooks/{id}/agent/chat` | Agent 对话 (SSE) |
| GET | `/api/codelab/notebooks/{id}/agent/context` | 获取上下文 |
| GET | `/api/codelab/notebooks/{id}/agent/history` | 获取对话历史 |
| DELETE | `/api/codelab/notebooks/{id}/agent/history` | 清空历史 |
| POST | `/api/codelab/notebooks/{id}/agent/suggest-code` | 代码建议 |
| POST | `/api/codelab/notebooks/{id}/agent/explain-error` | 错误解释 |
| POST | `/api/codelab/notebooks/{id}/agent/analyze-data` | 数据分析 |

### 5.5 SSE 事件格式

```typescript
// 开始事件
{type: "start", provider: "openai", model: "gpt-4"}

// 思考事件
{type: "thought", content: "用户需要画一个正弦波..."}

// 工具调用事件
{type: "action", tool: "notebook_execute", input: {code: "..."}}

// 工具结果事件
{
  type: "observation",
  success: true,
  output: "✅ 代码执行成功",
  notebook_updated: true,
  cell_id: "xxx-xxx",
  new_cell: {id: "...", cell_type: "code", source: "...", outputs: [...]}
}

// 回答事件
{type: "answer", content: "我已经创建了正弦波图形..."}

// 完成事件
{type: "done"}

// 错误事件
{type: "error", error: "执行失败: ..."}

// 授权请求事件
{type: "authorization_required", action: "execute_code"}
```

---

## 6. Agent 工具

### 6.1 工具列表

| 工具名 | 类型 | 需要授权 | 描述 |
|--------|------|----------|------|
| `notebook_execute` | 核心 | ✅ | 在 Notebook 内核中执行 Python 代码 |
| `notebook_variables` | 只读 | ❌ | 获取当前变量状态 |
| `notebook_cell` | 操作 | ✅* | 操作单元格 (add/delete/update/get) |
| `pip_install` | 操作 | ✅ | 安装 Python 包 (白名单限制) |
| `web_scrape` | 只读 | ❌ | 爬取网页内容 |
| `code_analysis` | 只读 | ❌ | 代码质量分析 |
| `literature_search` | 只读 | ❌ | 学术文献搜索 |
| `web_search` | 只读 | ❌ | 互联网搜索 |
| `calculator` | 只读 | ❌ | 数学计算 |

*`notebook_cell` 的 `get` 操作不需要授权

### 6.2 工具详情

#### 6.2.1 NotebookExecuteTool

```python
class NotebookExecuteTool(Tool):
    name = "notebook_execute"
    description = "在 Notebook 的 Python 内核中执行代码"
    parameters = {
        "code": {"type": "string", "description": "要执行的 Python 代码"},
        "description": {"type": "string", "description": "代码功能描述（可选）"}
    }
```

**执行流程**:
1. 检查用户授权
2. 获取 Notebook 对应的 PythonKernel
3. 在内核命名空间中执行代码
4. 捕获标准输出、返回值、图像
5. 创建新 Cell 并添加到 Notebook
6. 同步到数据库
7. 返回结果和 `new_cell` 数据

**返回数据**:
```python
ToolResult(
    success=True,
    output="✅ 代码执行成功\n📤 输出:\nHello World",
    data={
        "cell_id": "uuid",
        "execution_count": 1,
        "execution_time_ms": 150,
        "notebook_updated": True,
        "new_cell": {...}  # 完整的 Cell 对象
    }
)
```

#### 6.2.2 NotebookCellTool

```python
class NotebookCellTool(Tool):
    name = "notebook_cell"
    description = "操作 Notebook 的单元格"
    parameters = {
        "action": {"type": "string", "enum": ["add", "delete", "update", "get"]},
        "cell_id": {"type": "string", "description": "单元格 ID"},
        "cell_type": {"type": "string", "enum": ["code", "markdown"]},
        "content": {"type": "string", "description": "单元格内容"},
        "index": {"type": "integer", "description": "插入位置"}
    }
```

**操作类型**:
- `get`: 获取所有 Cell 概要 (无需授权)
- `add`: 添加新 Cell
- `update`: 更新 Cell 内容
- `delete`: 删除 Cell

#### 6.2.3 PipInstallTool

```python
class PipInstallTool(Tool):
    name = "pip_install"
    description = "安装 Python 包"
    parameters = {
        "packages": {"type": "array", "items": {"type": "string"}}
    }
```

**白名单**:
```python
ALLOWED_PACKAGES = {
    # 数据科学
    'numpy', 'pandas', 'scipy', 'statsmodels',
    # 可视化
    'matplotlib', 'seaborn', 'plotly', 'bokeh',
    # 机器学习
    'scikit-learn', 'xgboost', 'lightgbm',
    # 深度学习
    'torch', 'tensorflow', 'transformers',
    # 网络
    'requests', 'httpx', 'beautifulsoup4',
    # ... 更多
}
```

#### 6.2.4 WebScrapeTool

```python
class WebScrapeTool(Tool):
    name = "web_scrape"
    description = "爬取网页内容"
    parameters = {
        "url": {"type": "string", "description": "网页 URL"},
        "selector": {"type": "string", "description": "CSS 选择器（可选）"},
        "extract_type": {"type": "string", "enum": ["text", "html", "links", "tables"]}
    }
```

**黑名单域名**:
```python
BLOCKED_DOMAINS = {'localhost', '127.0.0.1', '0.0.0.0', 'internal', 'intranet'}
```

---

## 7. 上下文管理

### 7.1 Notebook 上下文

Agent 在对话时会获取以下上下文信息：

```python
# 构建系统消息
system_context = f"""你是一个专业的数据科学助手...

## 当前 Notebook 信息
- ID: {notebook_id}
- 标题: {notebook.get('title', '未命名')}
- 单元格数量: {len(cells)} (代码: {len(code_cells)})
- 执行次数: {notebook.get('execution_count', 0)}

## 最近代码单元格（最近5个）
{cells_summary}

## 当前变量状态
{variables_info}

## 可用工具
{tools_description}
"""
```

### 7.2 变量上下文

```python
# 获取当前内核中的变量
kernel = kernel_manager.get_kernel(notebook_id)
variables = kernel.get_variables() if kernel else {}

# 返回格式
{
    "x": "int: 10",
    "df": "DataFrame(100, 5): columns=['a', 'b', 'c', 'd', 'e']",
    "model": "LinearRegression()"
}
```

### 7.3 对话历史管理

```python
# 内存存储对话历史
_agent_histories: Dict[str, Dict[str, Any]] = {}

def get_agent_history(notebook_id: str, user_id: int) -> Dict:
    """获取或创建对话历史"""
    key = f"{user_id}:{notebook_id}"
    if key not in _agent_histories:
        _agent_histories[key] = {
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
    return _agent_histories[key]
```

---

## 8. 持久化机制

### 8.1 双层存储架构

```
┌─────────────────────────────────────────────────────┐
│                    内存缓存层                        │
│                 (_notebooks_cache)                   │
│                                                     │
│  特点:                                              │
│  - 快速读取                                         │
│  - Agent 工具直接访问                               │
│  - 重启后清空                                       │
└───────────────────────┬─────────────────────────────┘
                        │ 同步
┌───────────────────────▼─────────────────────────────┐
│                   数据库持久层                       │
│               (PostgreSQL)                          │
│                                                     │
│  特点:                                              │
│  - 持久化存储                                       │
│  - 重启后恢复                                       │
│  - 支持事务                                         │
└─────────────────────────────────────────────────────┘
```

### 8.2 缓存加载策略

```python
# 懒加载：首次访问时加载
async def _load_user_notebooks_to_cache(db: AsyncSession, user_id: int):
    if user_id in _loaded_users:
        return  # 已加载过
    
    service = NotebookService(db)
    notebooks = await service.get_user_notebooks(user_id)
    for nb in notebooks:
        _notebooks_cache[nb['id']] = nb
    _loaded_users.add(user_id)
```

### 8.3 写入同步策略

```python
# API 端点写入时
async def create_notebook(data, current_user, db):
    # 1. 写入数据库
    service = NotebookService(db)
    notebook = await service.create_notebook(...)
    
    # 2. 同步到缓存
    _notebooks_cache[notebook['id']] = notebook
    
    return notebook

# Agent 工具写入时
# 在 observation 事件处理中同步
if notebook_updated:
    async with AsyncSessionLocal() as db_session:
        service = NotebookService(db_session)
        # 直接使用相同的 cell_id 写入数据库
        new_db_cell = NotebookCell(
            id=new_cell.get('id'),  # 保持 ID 一致
            ...
        )
        notebook_model.cells.append(new_db_cell)
        await db_session.commit()
```

---

## 9. 实时更新机制

### 9.1 SSE 事件流

```python
# 后端生成 SSE 事件
async def generate_response():
    async for event in agent.run_stream(...):
        event_type = event.get("type")
        event_data = event.get("data")
        
        if event_type == "observation":
            # 提取 new_cell 数据
            new_cell = tool_data.get("new_cell")
            
            yield f"data: {json.dumps({
                'type': 'observation',
                'notebook_updated': True,
                'new_cell': new_cell
            })}\n\n"
```

### 9.2 前端实时更新

```typescript
// NotebookAgentPanel.tsx
const handleEvent = (event: AgentChatEvent) => {
  if (event.type === 'observation') {
    if (event.notebook_updated && event.new_cell && onAddCell) {
      // 直接添加到 UI，无需刷新
      onAddCell(event.new_cell)
    } else if (event.notebook_updated && event.updated_cell && onUpdateCell) {
      // 更新已存在的 Cell
      onUpdateCell(event.updated_cell)
    }
  }
}

// CodeLabPage.tsx
const handleAgentAddCell = useCallback((newCell: Cell) => {
  startTransition(() => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      // 检查重复
      const exists = prev.cells.some(c => c.id === newCell.id)
      if (exists) return prev
      return { ...prev, cells: [...prev.cells, newCell] }
    })
  })
  // 自动滚动
  setTimeout(() => setSelectedCellIndex(currentNotebook.cells.length), 100)
}, [currentNotebook])
```

---

## 10. 安全机制

### 10.1 授权控制

```python
# 工具级别授权检查
class NotebookExecuteTool(Tool):
    async def execute(self, code: str, **kwargs) -> ToolResult:
        # 检查用户是否授权
        if not self.user_authorized:
            return ToolResult(
                success=False,
                output="执行代码需要用户授权。请先启用「允许 AI 操作 Notebook」选项。",
                error="authorization_required",
                data={"requires_authorization": True, "action": "execute_code"}
            )
        # ... 执行逻辑
```

**需要授权的操作**:
- `notebook_execute`: 执行代码
- `notebook_cell` (add/update/delete): 修改 Cell
- `pip_install`: 安装包

### 10.2 代码执行沙箱

```python
# 执行超时控制
def execute(self, code: str, timeout: int = 30):
    # 使用 signal 或线程实现超时
    ...

# 危险操作过滤（可扩展）
BLOCKED_PATTERNS = [
    r'os\.system',
    r'subprocess\.',
    r'eval\s*\(',
    r'exec\s*\(',
]
```

### 10.3 pip 安装白名单

```python
ALLOWED_PACKAGES = {
    'numpy', 'pandas', 'matplotlib', 'seaborn',
    'scikit-learn', 'torch', 'tensorflow',
    # ... 完整白名单见 notebook_tools.py
}

# 检查
if package.lower() not in ALLOWED_PACKAGES:
    return ToolResult(success=False, output=f"包 '{package}' 不在白名单中")
```

### 10.4 网页爬取限制

```python
BLOCKED_DOMAINS = {'localhost', '127.0.0.1', '0.0.0.0', 'internal', 'intranet'}

def _is_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    return not any(blocked in domain for blocked in BLOCKED_DOMAINS)
```

---

## 11. 性能优化

### 11.1 前端优化

```typescript
// React 18 并发特性
const [isPending, startTransition] = useTransition()
const deferredCells = useDeferredValue(currentNotebook?.cells)

// 非紧急更新
const updateCell = useCallback((cellId: string, source: string) => {
  startTransition(() => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      return {
        ...prev,
        cells: prev.cells.map(cell =>
          cell.id === cellId ? { ...cell, source } : cell
        )
      }
    })
  })
}, [])

// 缓存计算结果
const stats = useMemo(() => ({
  totalCells: notebooks.reduce((acc, nb) => acc + nb.cells.length, 0),
  totalExecutions: notebooks.reduce((acc, nb) => acc + nb.execution_count, 0)
}), [notebooks])
```

### 11.2 后端优化

```python
# 数据库连接池
engine = create_async_engine(
    async_database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True
)

# 缓存加速读取
async def get_notebook_cached(db, notebook_id, user_id):
    # 先查缓存
    if notebook_id in _notebooks_cache:
        nb = _notebooks_cache[notebook_id]
        if nb.get('user_id') == user_id:
            return nb
    # 缓存未命中，从数据库加载
    ...

# 内核复用
class KernelManager:
    _kernels: Dict[str, PythonKernel] = {}
    # 同一 Notebook 复用内核
```

---

## 12. 故障排查

### 12.1 常见问题

#### 问题: Cell 刷新后丢失
**原因**: 缓存未同步到数据库
**排查**:
```bash
# 检查数据库
docker-compose exec db psql -U postgres -d research_assistant
SELECT * FROM notebook_cells WHERE notebook_id = 'xxx';

# 检查日志
docker-compose logs backend | grep "同步到数据库"
```

**解决**: 确保 observation 事件处理中的数据库同步正常执行

#### 问题: AI 添加代码不刷新
**原因**: `new_cell` 数据未正确传递
**排查**:
```bash
# 检查 SSE 事件
curl -N "http://localhost:8000/api/codelab/notebooks/xxx/agent/chat" ...

# 查找 new_cell 字段
```

**解决**: 确保 `NotebookCellTool._add_cell` 返回 `new_cell` 数据

#### 问题: 内核状态丢失
**原因**: 容器重启导致内核清空
**说明**: 这是预期行为，内核状态不持久化

#### 问题: 执行超时
**解决**:
```python
# 增加超时时间
result = kernel.execute(request.code, timeout=60)

# 或在前端提示用户
if event.type === 'error' && event.error.includes('timeout'):
  message.warning('执行超时，请简化代码')
```

### 12.2 日志位置

```bash
# 后端日志
docker-compose logs -f backend

# 数据库日志
docker-compose logs -f db

# 前端开发者工具
F12 -> Console / Network
```

### 12.3 调试模式

```python
# backend/app/config.py
class Settings:
    debug: bool = True  # 启用详细日志

# 使用 loguru
from loguru import logger
logger.info(f"[NotebookExecute] notebook_id={self.notebook_id}")
logger.debug(f"[NotebookExecute] 代码: {code[:200]}...")
```

---

## 13. 扩展开发

### 13.1 添加新工具

```python
# 1. 创建工具类
class MyCustomTool(Tool):
    name = "my_custom_tool"
    description = "工具描述"
    parameters = {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "参数1描述"}
        },
        "required": ["param1"]
    }
    
    async def execute(self, param1: str, **kwargs) -> ToolResult:
        # 实现逻辑
        return ToolResult(success=True, output="结果")

# 2. 注册工具
# 在 ToolRegistry._register_notebook_tools 中添加
self.register(MyCustomTool())
```

### 13.2 自定义输出类型

```python
# 添加新的 output_type
if output_type == 'custom_widget':
    output_parts.append(f"📊 [自定义组件: {content.get('type')}]")

# 前端渲染
const renderOutput = (output: CellOutput) => {
  if (output.output_type === 'custom_widget') {
    return <CustomWidget data={output.content} />
  }
  // ...
}
```

### 13.3 添加新的 Cell 类型

```python
# 后端: 支持新类型
cell_type = Column(String(20), default="code")  # 'code' | 'markdown' | 'sql'

# 前端: 新渲染器
const CellEditor = ({ cell }) => {
  switch (cell.cell_type) {
    case 'code': return <CodeEditor ... />
    case 'markdown': return <MarkdownEditor ... />
    case 'sql': return <SqlEditor ... />  // 新增
  }
}
```

### 13.4 集成外部服务

```python
# 示例: 集成 Jupyter Kernel Gateway
class JupyterKernelTool(Tool):
    name = "jupyter_kernel"
    description = "在远程 Jupyter 内核中执行代码"
    
    async def execute(self, code: str, kernel_id: str, **kwargs) -> ToolResult:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{JUPYTER_GATEWAY_URL}/api/kernels/{kernel_id}/execute",
                json={"code": code}
            )
            return ToolResult(success=True, output=response.json()['output'])
```

---

## 附录

### A. 文件结构

```
backend/
├── app/
│   ├── api/
│   │   └── codelab.py           # 主 API (1500+ 行)
│   ├── models/
│   │   └── notebook.py          # 数据库模型
│   ├── services/
│   │   ├── notebook_service.py  # 数据库服务
│   │   ├── notebook_tools.py    # Agent 工具 (1400+ 行)
│   │   ├── react_agent.py       # ReAct Agent (700 行)
│   │   └── agent_tools.py       # 通用工具 (1100+ 行)
│   └── core/
│       └── database.py          # 数据库配置
├── alembic/
│   └── versions/
│       └── 005_notebook.py      # 迁移文件

frontend/
├── src/
│   ├── pages/
│   │   └── codelab/
│   │       └── CodeLabPage.tsx  # 主页面 (1000 行)
│   ├── components/
│   │   └── NotebookAgentPanel.tsx  # Agent 面板 (400 行)
│   └── services/
│       └── api.ts               # API 客户端
```

### B. 配置参数

```python
# backend/app/config.py
class Settings:
    # ReAct Agent
    react_max_iterations: int = 10
    react_temperature: float = 0.7
    
    # 代码执行
    default_timeout: int = 30
    max_output_length: int = 10000
    
    # 数据库
    database_url: str = "postgresql://..."
```

### C. 环境变量

```bash
# .env
DATABASE_URL=postgresql://postgres:postgres@db:5432/research_assistant
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
SERPER_API_KEY=xxx  # 可选，用于网页搜索
```

---

**文档版本**: 1.0
**最后更新**: 2026-01-24
**作者**: Claude AI Assistant
