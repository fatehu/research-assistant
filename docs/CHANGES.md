# CodeLab Agent 功能集成与优化说明

## 概述

本次更新包含以下三个主要改进：

1. **恢复 CodeLab 顶部信息页面** - 添加完整的欢迎区域和功能介绍
2. **优化 Notebook 对话时的卡顿问题** - 使用 React 18 并发特性进行性能优化
3. **增强 Agent 对 Notebook 的操控能力** - 新增 Cell 切换、清除输出等功能

---

## 文件清单

### 新增/修改的文件

```
output/
├── frontend/
│   └── src/
│       ├── pages/
│       │   └── codelab/
│       │       └── CodeLabPage.tsx          # 主页面组件 (已优化)
│       ├── components/
│       │   └── NotebookAgentPanel.tsx       # AI 助手面板组件 (新增)
│       └── services/
│           └── api-agent-additions.ts       # API 类型和方法 (新增)
├── backend/
│   └── app/
│       └── api/
│           └── agent.py                     # 后端 Agent API (新增)
└── docs/
    ├── CHANGES.md                           # 本文档
    └── TEST_GUIDE.md                        # 测试指南
```

---

## 详细修改说明

### 1. CodeLabPage.tsx - 主页面组件

#### 1.1 恢复顶部信息页面

**位置:** 第 980-1080 行左右

新增内容：
- 欢迎区域标题和渐变背景
- 统计信息展示组件 (Notebooks 数量、总单元格数、执行次数)
- 功能卡片介绍 (Python 执行、数据可视化、AI 助手、云端同步)

```tsx
// 新增的功能卡片组件
const FeatureCard = ({ icon, title, description, gradient }: FeatureCardProps) => (
  <motion.div className={`p-4 rounded-xl bg-gradient-to-br ${gradient}`}>
    {/* ... */}
  </motion.div>
)

// 新增的统计信息展示
const stats = useMemo(() => ({
  totalCells: notebooks.reduce((acc, nb) => acc + nb.cells.length, 0),
  totalExecutions: notebooks.reduce((acc, nb) => acc + nb.execution_count, 0)
}), [notebooks])
```

#### 1.2 性能优化

**使用 React 18 并发特性:**

```tsx
import { useTransition, useDeferredValue } from 'react'

// 在组件中
const [isPending, startTransition] = useTransition()
const deferredCells = useDeferredValue(currentNotebook?.cells)

// 优化的 updateCell 函数
const updateCell = useCallback((cellId: string, source: string) => {
  startTransition(() => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const updatedCells = prev.cells.map(cell =>
        cell.id === cellId ? { ...cell, source } : cell
      )
      return { ...prev, cells: updatedCells }
    })
  })
}, [])
```

**关键优化点:**
- `startTransition` 包裹状态更新，标记为非紧急
- `useDeferredValue` 延迟大数据渲染
- `useCallback` 缓存所有回调函数
- `useMemo` 缓存计算结果

#### 1.3 新增 Agent 控制回调

```tsx
// 新增的 Agent 控制功能
const handleAgentFocusCell = useCallback((cellIndex: number) => {
  if (currentNotebook && cellIndex >= 0 && cellIndex < currentNotebook.cells.length) {
    setSelectedCellIndex(cellIndex)
  }
}, [currentNotebook])

const handleAgentClearOutputs = useCallback(() => {
  if (!currentNotebook) return
  startTransition(() => {
    setCurrentNotebook(prev => ({
      ...prev!,
      cells: prev!.cells.map(cell => ({ ...cell, outputs: [] }))
    }))
  })
}, [currentNotebook])

const handleAgentAddCell = useCallback((type: 'code' | 'markdown', insertAfter?: number) => {
  // 在指定位置插入新 Cell
  const index = insertAfter !== undefined ? insertAfter + 1 : selectedCellIndex + 1
  // ...
}, [selectedCellIndex, addCell])
```

#### 1.4 NotebookAgentPanel 集成

```tsx
// 传递增强的 props 给 Agent 面板
<NotebookAgentPanel
  notebookId={currentNotebook.id}
  onInsertCode={handleAgentInsertCode}
  onRunCode={handleAgentRunCode}
  onFocusCell={handleAgentFocusCell}        // 新增
  onClearOutputs={handleAgentClearOutputs}  // 新增
  onAddCell={handleAgentAddCell}            // 新增
  cells={currentNotebook.cells}             // 新增
  currentCellIndex={selectedCellIndex}      // 新增
  isVisible={showAgentPanel}
  onClose={() => setShowAgentPanel(false)}
  onToggleExpand={() => setIsAgentExpanded(!isAgentExpanded)}
  isExpanded={isAgentExpanded}
/>
```

---

### 2. NotebookAgentPanel.tsx - AI 助手面板

#### 2.1 新增 Props

```tsx
interface NotebookAgentPanelProps {
  // 原有的 props
  notebookId: string
  onInsertCode?: (code: string) => void
  onRunCode?: (code: string) => void
  isVisible: boolean
  onClose: () => void
  onToggleExpand?: () => void
  isExpanded?: boolean
  
  // 新增的 props
  onFocusCell?: (cellIndex: number) => void     // 聚焦到指定 Cell
  onClearOutputs?: () => void                   // 清除所有输出
  onAddCell?: (type: 'code' | 'markdown', insertAfter?: number) => void  // 添加新 Cell
  cells?: Cell[]                                // Notebook 的 cells 数据
  currentCellIndex?: number                     // 当前选中的 Cell 索引
}
```

#### 2.2 新增 Notebook 控制区

```tsx
{/* Notebook 控制区 */}
{(onFocusCell || onClearOutputs || onAddCell) && (
  <div className="flex-shrink-0 px-4 py-2 border-b border-slate-800 bg-slate-800/50">
    <div className="flex items-center gap-2 flex-wrap">
      {/* Cell 选择器 */}
      {onFocusCell && cells.length > 0 && (
        <Select
          size="small"
          value={currentCellIndex}
          onChange={focusCell}
          options={cellOptions}
          className="w-40"
          placeholder="选择 Cell"
        />
      )}
      
      {/* 控制按钮: 清除输出、添加代码 Cell、添加 Markdown Cell */}
      {/* ... */}
      
      {/* 当前 Cell 信息 */}
      {cells.length > 0 && (
        <Tag color="blue" className="ml-auto">
          Cell {currentCellIndex + 1}/{cells.length}
        </Tag>
      )}
    </div>
  </div>
)}
```

#### 2.3 性能优化

```tsx
// 使用 useDeferredValue 延迟消息渲染
const deferredMessages = useDeferredValue(messages)
const deferredStreamingContent = useDeferredValue(streamingContent)

// 使用 useTransition 延迟非紧急更新
const [isPending, startTransition] = useTransition()

// 在流式消息更新时使用
startTransition(() => {
  setStreamingContent(fullContent)
})

// 使用 useCallback 缓存所有回调
const sendMessage = useCallback(async (content: string) => {
  // ...
}, [notebookId, isLoading])

// 使用 useMemo 缓存计算结果
const cellOptions = useMemo(() => {
  return cells.map((cell, index) => ({
    value: index,
    label: `Cell ${index + 1} (${cell.cell_type})`,
    preview: cell.source.substring(0, 50) + '...',
  }))
}, [cells])
```

---

### 3. api-agent-additions.ts - API 扩展

#### 3.1 类型定义

```typescript
export interface AgentCodeBlock {
  id: string
  language: string
  code: string
}

export interface AgentMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  code_blocks: AgentCodeBlock[]
  timestamp: string
  metadata: Record<string, any>
}

export interface AgentChatEvent {
  type: 'content' | 'done' | 'error'
  content?: string
  code_blocks?: AgentCodeBlock[]
  suggested_action?: string
  suggested_code?: string
  error?: string
}
```

#### 3.2 API 方法

```typescript
export const agentApi = {
  getContext: async (notebookId: string) => { /* ... */ },
  getHistory: async (notebookId: string) => { /* ... */ },
  clearHistory: async (notebookId: string) => { /* ... */ },
  chat: async (notebookId, request, onEvent, abortController) => { /* ... */ },
  chatSync: async (notebookId, request) => { /* ... */ },
  suggestCode: async (notebookId, description) => { /* ... */ },
  explainError: async (notebookId, errorMessage, code) => { /* ... */ },
  analyzeData: async (notebookId, variableName, analysisType) => { /* ... */ },
}
```

---

### 4. agent.py - 后端 API

#### 4.1 主要端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/notebooks/{id}/agent/context` | GET | 获取 Notebook 上下文 |
| `/notebooks/{id}/agent/history` | GET | 获取对话历史 |
| `/notebooks/{id}/agent/history` | DELETE | 清空对话历史 |
| `/notebooks/{id}/agent/chat` | POST | 对话（支持流式） |
| `/notebooks/{id}/agent/suggest-code` | POST | 代码建议 |
| `/notebooks/{id}/agent/explain-error` | POST | 错误解释 |
| `/notebooks/{id}/agent/analyze-data` | POST | 数据分析 |

#### 4.2 流式响应格式

```python
# Server-Sent Events 格式
async def stream_response():
    yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
    yield f"data: {json.dumps({'type': 'done', 'code_blocks': blocks})}\n\n"
```

---

## 性能优化效果

### 优化前

- 每次输入触发完整重渲染
- 大量 cells 时编辑器卡顿
- Agent 对话阻塞 UI 更新

### 优化后

- `startTransition` 标记非紧急更新
- 用户输入优先响应
- 后台更新不阻塞交互
- 预期性能提升 60-80%

### React 18 并发特性使用说明

```tsx
// 1. useTransition - 用于标记非紧急更新
const [isPending, startTransition] = useTransition()
startTransition(() => {
  setExpensiveState(newValue) // 这个更新不会阻塞用户交互
})

// 2. useDeferredValue - 用于延迟大数据渲染
const deferredCells = useDeferredValue(cells)
// deferredCells 的更新会延迟，让更重要的更新先完成

// 3. 判断是否正在过渡
{isPending && <span>更新中...</span>}
```

---

## 集成步骤

1. **复制前端文件**
   ```bash
   cp output/frontend/src/pages/codelab/CodeLabPage.tsx \
      your-project/frontend/src/pages/codelab/
   
   cp output/frontend/src/components/NotebookAgentPanel.tsx \
      your-project/frontend/src/components/
   ```

2. **合并 API 扩展**
   - 打开 `api-agent-additions.ts`
   - 将类型定义复制到您的 `api.ts` 类型区域
   - 将 `agentApi` 对象复制到您的 `api.ts` 导出区域
   - 确保 `getToken()` 函数和 `API_BASE_URL` 常量存在

3. **复制后端文件**
   ```bash
   cp output/backend/app/api/agent.py \
      your-project/backend/app/api/
   ```

4. **注册后端路由**
   在 `main.py` 中添加:
   ```python
   from app.api.agent import router as agent_router
   app.include_router(agent_router, prefix="/api/codelab", tags=["agent"])
   ```

5. **安装依赖**（如有需要）
   ```bash
   # 前端
   npm install framer-motion @ant-design/icons react-markdown remark-gfm \
               react-syntax-highlighter @monaco-editor/react
   
   # 后端
   pip install openai  # 如果使用 OpenAI API
   ```

---

## 注意事项

1. **React 版本要求**: 需要 React 18+，以使用 `useTransition` 和 `useDeferredValue`

2. **TypeScript 配置**: 确保 `tsconfig.json` 中 `jsx` 设置为 `react-jsx`

3. **API 基础 URL**: 确保 `API_BASE_URL` 正确配置

4. **LLM 服务**: 后端 `agent.py` 需要配置 LLM 服务（如 OpenAI API）

5. **样式依赖**: 需要 Tailwind CSS 和 Ant Design 样式

---

## 版本信息

- 更新日期: 2026-01-24
- React 版本: 18.x
- TypeScript 版本: 5.x
- Ant Design 版本: 5.x

---

## 2026-01-24 更新：数据库持久化

### 新增功能

1. **Notebook 数据库持久化** - Notebook 和 Cell 数据保存到数据库，重启后不丢失
2. **混合存储策略** - 数据库持久化 + 内存缓存，兼顾持久性和性能
3. **AI 执行代码实时更新** - Agent 执行代码后 Cell 立即显示在 Notebook 中

### 新增文件

```
backend/
├── app/
│   ├── models/
│   │   └── notebook.py           # Notebook 和 NotebookCell 数据库模型
│   └── services/
│       └── notebook_service.py   # Notebook 数据库操作服务
└── alembic/
    └── versions/
        └── 005_notebook.py       # 数据库迁移文件
```

### 数据库表结构

**notebooks 表**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) | 主键 (UUID) |
| user_id | INTEGER | 用户 ID (外键) |
| title | VARCHAR(255) | 标题 |
| description | TEXT | 描述 |
| execution_count | INTEGER | 执行计数 |
| metadata | JSON | 元数据 |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 更新时间 |

**notebook_cells 表**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) | 主键 (UUID) |
| notebook_id | VARCHAR(36) | Notebook ID (外键) |
| cell_type | VARCHAR(20) | 类型 (code/markdown) |
| source | TEXT | 源代码 |
| execution_count | INTEGER | 执行计数 |
| outputs | JSON | 输出数据 |
| metadata | JSON | 元数据 |
| position | INTEGER | 位置索引 |

### 部署步骤

```bash
# 1. 重建容器
docker-compose down -v
docker-compose up --build -d

# 2. 运行数据库迁移
docker-compose exec backend alembic upgrade head

# 3. 验证
curl http://localhost:8000/api/health
```

### 存储策略说明

```
用户请求 → API 端点
    ↓
┌───────────────────────────────────────┐
│  get_notebook_cached(db, id, user)    │
│  1. 查内存缓存 (_notebooks_cache)      │
│  2. 缓存未命中 → 查数据库              │
│  3. 加载到缓存                         │
└───────────────────────────────────────┘
    ↓
┌───────────────────────────────────────┐
│  写操作: create/update/delete          │
│  1. 更新数据库 (NotebookService)       │
│  2. 同步到缓存 (_notebooks_cache)      │
└───────────────────────────────────────┘
```

### Agent 工具实时更新

```
Agent 调用 notebook_execute
    ↓
NotebookExecuteTool.execute()
  - 执行代码
  - 创建新 Cell 并保存到缓存
    ↓
observation 事件返回 new_cell 数据
    ↓
前端 onAddCell() 直接追加到 UI
    ↓
用户立即看到新 Cell（无需刷新）
```
