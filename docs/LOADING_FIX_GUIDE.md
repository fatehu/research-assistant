# CodeLab Loading 问题排查与修复指南

## 问题描述

页面一直显示 Loading 状态，无法正常使用。

## 可能原因

### 1. 后端服务未正常运行

**排查方法：**
```bash
# 检查后端健康状态
curl http://localhost:8000/health

# 检查 Docker 容器状态
docker-compose ps

# 查看后端日志
docker-compose logs backend
```

### 2. API 请求超时或失败

**排查方法：**
```bash
# 测试 CodeLab API（需要先登录获取 token）
curl -X GET http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 3. 前端代码问题

当前代码中的 `loadNotebooks()` 函数没有设置加载状态，可能导致：
- 列表加载时没有 loading 提示
- 请求失败后状态未正确重置

## 修复方案

### 修复 1: 添加列表加载状态

修改 `frontend/src/pages/codelab/CodeLabPage.tsx`：

```tsx
const CodeLabPage = () => {
  // ... 现有状态
  const [isLoading, setIsLoading] = useState(false)
  const [isListLoading, setIsListLoading] = useState(true)  // 新增：列表加载状态
  const [error, setError] = useState<string | null>(null)   // 新增：错误状态

  // 修改 loadNotebooks 函数
  const loadNotebooks = useCallback(async () => {
    setIsListLoading(true)
    setError(null)
    try {
      const data = await codelabApi.listNotebooks()
      setNotebooks(data)
    } catch (error: any) {
      console.error('加载 Notebook 列表失败:', error)
      setError(error.message || '加载失败，请检查网络连接')
    } finally {
      setIsListLoading(false)
    }
  }, [])
```

### 修复 2: 添加请求超时

修改 `frontend/src/services/api.ts`：

```typescript
// 创建 axios 实例时添加超时配置
const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,  // 30 秒超时
  headers: {
    'Content-Type': 'application/json',
  },
})
```

### 修复 3: 添加错误提示 UI

在列表视图中添加错误处理：

```tsx
// 在列表视图中添加
{isListLoading ? (
  <div className="flex items-center justify-center py-20">
    <Spin size="large" tip="加载中..." />
  </div>
) : error ? (
  <div className="text-center py-20">
    <p className="text-red-400 mb-4">{error}</p>
    <Button onClick={loadNotebooks}>重试</Button>
  </div>
) : (
  // ... 原有的 Notebook 列表渲染
)}
```

## 完整修复补丁

以下是需要修改的关键代码段：

### CodeLabPage.tsx 修改

找到并替换以下代码：

```tsx
// 原始代码
const [isLoading, setIsLoading] = useState(false)
const [isSaving, setIsSaving] = useState(false)
const [showNotebookList, setShowNotebookList] = useState(!notebookId)

// 替换为
const [isLoading, setIsLoading] = useState(false)
const [isListLoading, setIsListLoading] = useState(true)
const [isSaving, setIsSaving] = useState(false)
const [error, setError] = useState<string | null>(null)
const [showNotebookList, setShowNotebookList] = useState(!notebookId)
```

```tsx
// 原始的 loadNotebooks 函数
const loadNotebooks = useCallback(async () => {
  try {
    const data = await codelabApi.listNotebooks()
    setNotebooks(data)
  } catch (error) {
    console.error('加载 Notebook 列表失败:', error)
  }
}, [])

// 替换为
const loadNotebooks = useCallback(async () => {
  setIsListLoading(true)
  setError(null)
  try {
    const data = await codelabApi.listNotebooks()
    setNotebooks(data)
  } catch (error: any) {
    console.error('加载 Notebook 列表失败:', error)
    if (error.response?.status === 401) {
      setError('登录已过期，请重新登录')
    } else if (error.code === 'ECONNABORTED') {
      setError('请求超时，请检查网络连接')
    } else {
      setError(error.message || '加载失败，请稍后重试')
    }
  } finally {
    setIsListLoading(false)
  }
}, [])
```

## 调试步骤

### 步骤 1: 检查后端服务

```bash
# 1. 确保所有服务正在运行
docker-compose up -d

# 2. 等待服务启动
sleep 10

# 3. 检查健康状态
curl http://localhost:8000/health

# 4. 检查日志
docker-compose logs -f backend
```

### 步骤 2: 检查网络请求

在浏览器开发者工具中：
1. 打开 Network 标签
2. 刷新页面
3. 查找 `/api/codelab/notebooks` 请求
4. 检查：
   - 请求是否发出
   - 响应状态码
   - 响应内容

### 步骤 3: 检查控制台错误

在浏览器开发者工具的 Console 标签中查看是否有错误信息。

### 步骤 4: 验证认证状态

```javascript
// 在浏览器控制台执行
const authStorage = localStorage.getItem('auth-storage')
console.log(JSON.parse(authStorage))
```

## 快速验证脚本

```bash
# 保存为 debug_codelab.sh 并执行
#!/bin/bash

API_URL="http://localhost:8000"

echo "1. 检查健康状态..."
curl -s "$API_URL/health" | head -c 100
echo ""

echo "2. 测试登录..."
LOGIN=$(curl -s -X POST "$API_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123456"}')
echo "$LOGIN" | head -c 200
echo ""

TOKEN=$(echo "$LOGIN" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)

if [ -n "$TOKEN" ]; then
  echo "3. 测试获取 Notebook 列表..."
  curl -s "$API_URL/api/codelab/notebooks" \
    -H "Authorization: Bearer $TOKEN" | head -c 500
  echo ""
else
  echo "登录失败，无法继续测试"
fi
```

## 常见问题

### Q: 后端返回 401 Unauthorized

A: Token 过期或无效，需要重新登录。

### Q: 后端返回 500 Internal Server Error

A: 后端代码出错，检查 `docker-compose logs backend`。

### Q: 请求一直 pending

A: 可能是后端服务未启动或网络问题，检查 `docker-compose ps`。

### Q: CORS 错误

A: 检查 `backend/app/main.py` 中的 CORS 配置是否包含前端域名。
