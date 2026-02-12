# 前端架构重构文档

## 概述

将 4 个 800-1100 行的单体页面组件拆分为模块化结构，同时新增公共 hooks、Error Boundary 和统一 API 错误处理。

### 原始 vs 重构 — 主文件行数对比

| 页面 | 原始行数 | 重构后主文件 | 最大子组件 | 拆出模块数 |
|------|---------|------------|-----------|-----------|
| ChatPage | 1104 | 193 | ReActPanel (264) | 10 |
| CodeLabPage | 1084 | 125 | useNotebook hook (441) | 8 |
| KnowledgePage | 1018 | 522 | SearchResultCard (111) | 5 |
| LiteraturePage | 866 | 403 | CollectionSidebar (123) | 7 |

> **诚实说明**: KnowledgePage 和 LiteraturePage 的主文件仍有 400-500 行。原因是这两个页面包含多个 Modal/Drawer 和双视图切换逻辑，这些「页面级编排逻辑」不适合进一步拆分（强行拆分会增加 prop drilling 和理解成本）。真正的渲染性组件（卡片、侧边栏等）已全部提取。

---

## 交付文件清单

所有文件位于 `frontend/src/` 下，按原项目目录结构组织。

### 新增公共模块

```
hooks/
├── useDebounce.ts          # 值防抖 + 回调防抖
├── usePagination.ts        # 客户端分页 + 无限滚动加载更多
├── useStreamResponse.ts    # SSE 流式响应 UI 状态管理
└── index.ts                # Barrel export

components/common/
└── ErrorBoundary.tsx        # React Error Boundary (class 组件)

utils/
└── apiErrorHandler.ts       # 统一 API 错误解析 + 弹窗提示
```

### ChatPage 拆分

```
pages/chat/
├── ChatPage.tsx             # 主文件 (193行): 路由参数、store 连接、副作用编排
├── constants.tsx            # toolIcons / toolNames 映射表
└── components/
    ├── CodeBlock.tsx         # 代码高亮 + 复制按钮
    ├── ThinkingPanel.tsx     # 最终思考面板（可展开/收起）
    ├── ReActPanel.tsx        # 实时推理过程面板（按迭代分组）
    ├── HistoryReActPanel.tsx # 历史消息推理面板
    ├── MessageBubble.tsx     # 消息气泡（用户/AI 双风格, Markdown 渲染）
    ├── EmptyState.tsx        # 空状态欢迎页（快捷提示 + 工具图标）
    ├── ChatInput.tsx         # 输入区域（TextArea + 发送/停止按钮）
    ├── ChatMessages.tsx      # 消息列表容器（含加载/错误/空/流式状态）
    └── index.ts
```

### CodeLabPage 拆分

```
pages/codelab/
├── CodeLabPage.tsx           # 主文件 (125行): 路由 → 视图切换 → 快捷键
├── hooks/
│   └── useNotebook.ts        # Notebook/Cell 全部 CRUD + Agent 回调
└── components/
    ├── FeatureCard.tsx        # 功能介绍卡片
    ├── StatCard.tsx           # 统计数据卡片
    ├── CellOutputRenderer.tsx # Cell 输出渲染（stream/result/image/error）
    ├── NotebookCell.tsx       # 单元格组件（Monaco Editor + Markdown + 输出）
    ├── NotebookListView.tsx   # Notebook 列表视图
    ├── NotebookEditorView.tsx # Notebook 编辑器视图（工具栏 + Cells + Agent）
    └── index.ts
```

### KnowledgePage 拆分

```
pages/knowledge/
├── KnowledgePage.tsx          # 主文件 (522行): 双视图 + 模态框逻辑
├── utils.ts                   # getFileIcon / getStatusTag / formatFileSize / 常量
└── components/
    ├── KnowledgeBaseCard.tsx       # 知识库卡片（文档数/分片数/Tokens）
    ├── SharedKnowledgeBaseCard.tsx # 共享知识库卡片（只读）
    ├── SearchResultCard.tsx        # 搜索结果卡片（层级标签/上下文展开）
    └── index.ts
```

### LiteraturePage 拆分

```
pages/literature/
├── LiteraturePage.tsx         # 主文件 (403行): 搜索/Tab/模态框编排
├── constants.ts               # SOURCES 数据源配置 + getSourceInfo
└── components/
    ├── PaperCard.tsx              # 论文卡片视图
    ├── PaperListItem.tsx          # 论文列表视图项
    ├── SearchResultCard.tsx       # 搜索结果卡片
    ├── SearchResultListItem.tsx   # 搜索结果列表项
    ├── CollectionSidebar.tsx      # 收藏夹侧边栏
    └── index.ts
```

---

## 迁移步骤

### 1. 复制新文件

```bash
# 假设项目根目录为 research-assistant/frontend

# 新增公共模块
cp -r src/hooks/ <项目>/frontend/src/hooks/
cp -r src/components/common/ <项目>/frontend/src/components/common/
cp -r src/utils/apiErrorHandler.ts <项目>/frontend/src/utils/

# 替换页面文件
cp -r src/pages/chat/ <项目>/frontend/src/pages/chat/
cp -r src/pages/codelab/ <项目>/frontend/src/pages/codelab/
cp -r src/pages/knowledge/ <项目>/frontend/src/pages/knowledge/
cp -r src/pages/literature/ <项目>/frontend/src/pages/literature/
```

### 2. 需要删除的文件

**无需删除任何文件。** 重构采用「替换 + 新增」模式：
- 各 `*Page.tsx` 主文件是原文件的重构版本，直接覆盖
- 新增的 `components/` 和 `hooks/` 子目录、`constants.ts`、`utils.ts` 是全新文件

### 3. 需要保留的原文件

以下原有文件被重构后的文件引用，**不在本次交付范围内，请勿删除**：

```
src/stores/literatureStore.ts   # LiteraturePage 依赖
src/stores/knowledgeStore.ts    # KnowledgePage 依赖 (如果有)
src/services/api.ts             # 所有页面依赖的 API 类型和方法
src/pages/chat/PaperDetailPanel.tsx  # （如存在）LiteraturePage 引用
src/pages/literature/PaperDetailPanel.tsx  # LiteraturePage Drawer 内容
src/components/NotebookAgentPanel.tsx      # CodeLabPage Agent 面板
```

### 4. 验证

```bash
cd frontend
npm run build   # TypeScript 编译检查
npm run dev     # 手动验证 4 个页面功能正常
```

重点检查：
- ChatPage: 消息发送/接收、流式响应、ReAct 推理面板展示
- CodeLabPage: Notebook 创建/打开/保存、Cell 运行/添加/删除、AI 助手面板
- KnowledgePage: 知识库列表/详情切换、搜索、共享知识库
- LiteraturePage: 论文搜索/保存、收藏夹操作、卡片/列表视图切换

---

## 各模块详细说明

### 公共 Hooks

#### useDebounce
```typescript
// 值防抖 - 用于搜索输入等场景
const debouncedQuery = useDebounce(searchQuery, 300)

// 回调防抖 - 用于 API 调用等场景
const debouncedSearch = useDebouncedCallback((query: string) => {
  api.search(query)
}, 300)
```

#### usePagination
```typescript
// 客户端分页
const { currentData, currentPage, totalPages, goToPage } = usePagination(allItems, {
  pageSize: 20,
  initialPage: 1,
})

// 无限滚动加载更多
const { data, loading, hasMore, loadMore, reset } = useLoadMorePagination(
  async (offset, limit) => api.fetchItems({ offset, limit }),
  { pageSize: 20 }
)
```

#### useStreamResponse
```typescript
const {
  streamingContent, streamingThought, isStreaming, isThinking,
  startStream, appendContent, appendThought, stopStream, reset,
} = useStreamResponse()
```
> **注意**: 此 hook 仅管理 UI 临时状态（打字效果、思考指示器）。消息持久化由各页面的 store 负责。

### ErrorBoundary

```tsx
import ErrorBoundary from '@/components/common/ErrorBoundary'

// 基础用法 - 内置默认错误 UI
<ErrorBoundary>
  <SomeComponent />
</ErrorBoundary>

// 自定义错误 UI
<ErrorBoundary fallback={<div>出错了</div>}>
  <SomeComponent />
</ErrorBoundary>

// Render prop 模式（可获取错误信息和重置方法）
<ErrorBoundary fallback={(error, reset) => (
  <div>
    <p>{error.message}</p>
    <button onClick={reset}>重试</button>
  </div>
)}>
  <SomeComponent />
</ErrorBoundary>
```

### apiErrorHandler

```typescript
import { handleApiError, parseApiError, ApiErrorType } from '@/utils/apiErrorHandler'

// 方式 1: 解析 + 自动弹窗提示
try {
  await api.doSomething()
} catch (error) {
  handleApiError(error, '操作名称') // 自动弹 message.error
}

// 方式 2: 仅解析，自行处理
const parsed = parseApiError(error)
if (parsed.type === ApiErrorType.Unauthorized) {
  navigate('/login')
} else if (parsed.type === ApiErrorType.RateLimited) {
  // 显示限流提示
}
```

自动处理的特殊情况：
- **401 Unauthorized**: 弹窗提示 + 跳转登录页
- **取消请求 (AbortController)**: 静默忽略，不弹窗
- **网络错误**: 提示"网络连接失败"

---

## 架构决策说明

### 为什么不用 Context/Redux 替代 prop drilling？

项目已使用 Zustand store（ChatStore、LiteratureStore 等）。子组件通过 props 接收回调而非直接连接 store，原因是：
1. 子组件保持"纯展示"定位，可被不同场景复用
2. 避免子组件与特定 store 耦合
3. Props 接口即文档，明确组件依赖

### 为什么 CodeLabPage 提取了 useNotebook hook 而其他页面没有？

CodeLabPage 的状态管理（15+ useState + 15+ useCallback）全部是本地状态，没有 Zustand store。将它们提取为 hook 能将 1084 行拆为 125 行（页面）+ 441 行（hook），职责清晰。

其他三个页面已有对应的 Zustand store，状态操作分散在 store 中，页面组件本身的逻辑量不足以单独提取 hook。

### 为什么 KnowledgePage 主文件仍有 500+ 行？

KnowledgePage 包含 3 个 Modal（创建知识库、搜索测试、删除确认）和列表/详情双视图切换。这些模态框逻辑与页面状态紧密耦合（比如创建后刷新列表、搜索需要当前知识库 ID），提取为独立组件会引入大量 prop 传递且不增加可维护性。真正复杂的渲染逻辑（卡片组件）已全部提取。

---

## 注意事项

1. **导入路径**: 所有文件使用 `@/` 路径别名（对应 `./src`），与项目 `vite.config.ts` 中的配置一致
2. **类型引用**: 组件 Props 中引用的 `Paper`、`PaperSearchResult`、`Notebook`、`Cell`、`CellOutput` 等类型来自 `@/services/api`
3. **样式**: 保持原有 Tailwind CSS 类名不变，包括 glass-card 等自定义类和 Ant Design 覆盖样式（`!` 前缀）
4. **动画**: framer-motion 的 `motion.div` 和 `AnimatePresence` 保持不变
5. **Monaco Editor**: NotebookCell 中的 Editor 配置与原始完全一致
6. **React 18**: useTransition 和 useDeferredValue 用法保留在 useNotebook hook 中
