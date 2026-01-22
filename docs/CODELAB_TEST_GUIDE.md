# 代码实验室功能 - 修改说明与测试方案

## 一、代码修改说明

### 1. 新增文件

#### 1.1 后端 API - `backend/app/api/codelab.py`

**功能**: Jupyter-style Notebook 后端服务

**主要组件**:

```python
# 数据模型
class CellOutput(BaseModel):     # 单元格输出（流式、结果、图表、错误）
class Cell(BaseModel):           # Notebook 单元格
class NotebookCreate(BaseModel): # 创建请求
class NotebookUpdate(BaseModel): # 更新请求
class ExecuteRequest(BaseModel): # 执行请求
class ExecuteResponse(BaseModel): # 执行响应

# 核心函数
async def execute_python_code(code: str, timeout: int = 30):
    """
    沙箱执行 Python 代码
    - 创建临时文件执行代码
    - 捕获 stdout/stderr
    - 处理 matplotlib 图表转 base64
    - 支持 30 秒超时
    """

# API 端点
GET  /api/codelab/notebooks              # 获取 Notebook 列表
POST /api/codelab/notebooks              # 创建 Notebook
GET  /api/codelab/notebooks/{id}         # 获取详情
PATCH /api/codelab/notebooks/{id}        # 更新 Notebook
DELETE /api/codelab/notebooks/{id}       # 删除 Notebook
POST /api/codelab/notebooks/{id}/execute # 执行单元格
POST /api/codelab/execute                # 直接执行代码
POST /api/codelab/notebooks/{id}/cells   # 添加单元格
DELETE /api/codelab/notebooks/{id}/cells/{cell_id} # 删除单元格
POST /api/codelab/notebooks/{id}/run-all # 运行所有单元格
```

**代码执行流程**:
```
1. 接收代码 → 2. 添加预处理代码(导入matplotlib等)
                ↓
3. 写入临时文件 → 4. subprocess 执行
                    ↓
5. 捕获输出 → 6. 解析图表(base64) → 7. 返回结果
```

#### 1.2 前端页面 - `frontend/src/pages/codelab/CodeLabPage.tsx`

**功能**: Jupyter-style 交互式界面

**主要组件**:

```tsx
// 输出渲染器 - 处理不同类型的输出
const CellOutputRenderer = ({ output }) => {
  // stream: 文本流输出
  // execute_result: 表达式结果
  // display_data: 图表(image/png)
  // error: 错误信息
}

// 单元格组件
const NotebookCell = ({
  cell,           // 单元格数据
  isSelected,     // 是否选中
  isRunning,      // 是否运行中
  onRun,          // 运行回调
  onUpdate,       // 更新回调
  ...
}) => {
  // Monaco Editor 代码编辑
  // Markdown 渲染/编辑
  // 输出展示区
}

// 主页面
const CodeLabPage = () => {
  // Notebook 列表视图
  // Notebook 编辑视图
  // 快捷键处理 (Ctrl+S, Ctrl+Enter, Shift+Enter)
}
```

**UI 特性**:
- Monaco Editor 代码编辑器
- 执行计数显示 `In [1]:`
- 运行状态动画
- 图表内嵌显示
- Markdown 实时预览
- 响应式布局

#### 1.3 前端导出 - `frontend/src/pages/codelab/index.ts`

```typescript
export { default as CodeLabPage } from './CodeLabPage'
```

---

### 2. 修改文件

#### 2.1 `backend/app/main.py`

**变更**: 注册代码实验室路由

```python
# 新增导入
from app.api import auth, users, chat, health, knowledge, literature, codelab

# 新增路由注册
app.include_router(codelab.router, prefix="/api/codelab", tags=["代码实验室"])
```

#### 2.2 `frontend/src/services/api.ts`

**变更**: 添加代码实验室 API 类型和接口

```typescript
// 新增类型定义
export interface CellOutput { ... }
export interface Cell { ... }
export interface Notebook { ... }
export interface ExecuteRequest { ... }
export interface ExecuteResponse { ... }

// 新增 API 对象
export const codelabApi = {
  listNotebooks,
  createNotebook,
  getNotebook,
  updateNotebook,
  deleteNotebook,
  executeCell,
  executeCode,
  addCell,
  deleteCell,
  runAll,
}
```

#### 2.3 `frontend/src/App.tsx`

**变更**: 添加路由配置

```tsx
// 新增导入
import { CodeLabPage } from '@/pages/codelab'

// 新增路由
<Route path="code" element={<CodeLabPage />} />
<Route path="code/:notebookId" element={<CodeLabPage />} />
```

#### 2.4 `frontend/src/pages/dashboard/DashboardPage.tsx`

**变更**: 开放功能入口

```tsx
// 文献管理 - 修改
{
  ...
  path: '/literature',
  disabled: false,  // 从 true 改为 false
}

// 代码实验 - 修改
{
  ...
  path: '/code',
  disabled: false,  // 从 true 改为 false
}
```

#### 2.5 `frontend/src/components/layout/MainLayout.tsx`

**变更**: 开放侧边栏菜单

```tsx
{
  key: '/code',
  icon: <CodeOutlined />,
  label: '代码实验室',
  disabled: false,  // 从 true 改为 false
}
```

#### 2.6 `frontend/package.json`

**变更**: 添加依赖

```json
{
  "dependencies": {
    "@monaco-editor/react": "^4.6.0",  // 新增
    ...
  }
}
```

---

## 二、测试方案

### 1. 环境准备

#### 1.1 后端环境

```bash
cd backend

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 确保安装科学计算库
pip install numpy pandas matplotlib torch --break-system-packages

# 启动服务
uvicorn app.main:app --reload --port 8000
```

#### 1.2 前端环境

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

---

### 2. API 接口测试

#### 2.1 创建 Notebook

```bash
# 请求
curl -X POST http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "测试 Notebook"}'

# 预期响应
{
  "id": "uuid-xxx",
  "user_id": 1,
  "title": "测试 Notebook",
  "cells": [...],
  "execution_count": 0
}
```

#### 2.2 获取 Notebook 列表

```bash
curl -X GET http://localhost:8000/api/codelab/notebooks \
  -H "Authorization: Bearer <token>"
```

#### 2.3 执行代码

```bash
# 测试简单打印
curl -X POST http://localhost:8000/api/codelab/notebooks/{id}/execute \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "print(\"Hello World\")",
    "cell_id": "xxx",
    "timeout": 30
  }'

# 预期响应
{
  "success": true,
  "outputs": [
    {"output_type": "stream", "content": "Hello World", "mime_type": "text/plain"}
  ],
  "execution_count": 1,
  "execution_time_ms": 50
}
```

#### 2.4 测试 Python 代码自动测试脚本

创建 `backend/test_codelab_api.py`:

```python
"""
代码实验室 API 测试脚本
"""
import requests
import json

BASE_URL = "http://localhost:8000"
TOKEN = "your_token_here"  # 替换为实际 token

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def test_create_notebook():
    """测试创建 Notebook"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks",
        headers=headers,
        json={"title": "API 测试 Notebook"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    print(f"✅ 创建 Notebook 成功: {data['id']}")
    return data["id"]

def test_execute_print(notebook_id, cell_id):
    """测试打印输出"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": "print('Hello from test')", "cell_id": cell_id}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == True
    assert any("Hello from test" in str(o.get("content", "")) for o in data["outputs"])
    print("✅ 打印输出测试通过")

def test_execute_numpy(notebook_id, cell_id):
    """测试 NumPy"""
    code = """
import numpy as np
arr = np.array([1, 2, 3, 4, 5])
print(f"Mean: {arr.mean()}")
arr
"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": code, "cell_id": cell_id}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == True
    print("✅ NumPy 测试通过")

def test_execute_pandas(notebook_id, cell_id):
    """测试 Pandas"""
    code = """
import pandas as pd
df = pd.DataFrame({'A': [1, 2, 3], 'B': [4, 5, 6]})
print(df.to_string())
"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": code, "cell_id": cell_id}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == True
    print("✅ Pandas 测试通过")

def test_execute_matplotlib(notebook_id, cell_id):
    """测试 Matplotlib 图表"""
    code = """
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 10, 100)
plt.figure(figsize=(8, 4))
plt.plot(x, np.sin(x), label='sin(x)')
plt.plot(x, np.cos(x), label='cos(x)')
plt.legend()
plt.title('Trigonometric Functions')
plt.show()
"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": code, "cell_id": cell_id}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == True
    # 检查是否有图表输出
    has_image = any(o.get("mime_type") == "image/png" for o in data["outputs"])
    assert has_image, "应该有图表输出"
    print("✅ Matplotlib 图表测试通过")

def test_execute_error(notebook_id, cell_id):
    """测试错误处理"""
    code = "undefined_variable"
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": code, "cell_id": cell_id}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == False
    assert any(o.get("output_type") == "error" for o in data["outputs"])
    print("✅ 错误处理测试通过")

def test_timeout(notebook_id, cell_id):
    """测试超时"""
    code = """
import time
time.sleep(5)
print("done")
"""
    resp = requests.post(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}/execute",
        headers=headers,
        json={"code": code, "cell_id": cell_id, "timeout": 2}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] == False
    print("✅ 超时处理测试通过")

def test_delete_notebook(notebook_id):
    """测试删除 Notebook"""
    resp = requests.delete(
        f"{BASE_URL}/api/codelab/notebooks/{notebook_id}",
        headers=headers
    )
    assert resp.status_code == 200
    print("✅ 删除 Notebook 测试通过")

if __name__ == "__main__":
    print("🧪 开始代码实验室 API 测试\n")
    
    # 创建测试 Notebook
    notebook_id = test_create_notebook()
    
    # 获取第一个 cell 的 ID
    resp = requests.get(f"{BASE_URL}/api/codelab/notebooks/{notebook_id}", headers=headers)
    cell_id = resp.json()["cells"][0]["id"]
    
    # 运行测试
    test_execute_print(notebook_id, cell_id)
    test_execute_numpy(notebook_id, cell_id)
    test_execute_pandas(notebook_id, cell_id)
    test_execute_matplotlib(notebook_id, cell_id)
    test_execute_error(notebook_id, cell_id)
    test_timeout(notebook_id, cell_id)
    
    # 清理
    test_delete_notebook(notebook_id)
    
    print("\n✅ 所有 API 测试通过!")
```

---

### 3. 前端功能测试

#### 3.1 Notebook 列表页

| 测试项 | 操作 | 预期结果 |
|--------|------|----------|
| 页面加载 | 访问 `/code` | 显示 Notebook 列表页，展示功能介绍卡片 |
| 空状态 | 无 Notebook | 显示空状态提示 |
| 创建 Notebook | 点击"新建 Notebook" | 创建成功，跳转到编辑页 |
| 打开 Notebook | 点击列表项 | 跳转到 `/code/{id}` |
| 删除 Notebook | 点击更多菜单→删除 | 弹出确认框，确认后删除 |

#### 3.2 Notebook 编辑页

| 测试项 | 操作 | 预期结果 |
|--------|------|----------|
| 加载 Notebook | 访问 `/code/{id}` | 正确显示所有单元格 |
| 修改标题 | 编辑顶部标题 | 标题实时更新 |
| 代码编辑 | 在代码单元格输入 | Monaco Editor 正常工作，语法高亮 |
| 运行单元格 | 点击运行按钮或 Shift+Enter | 显示运行中状态，输出结果 |
| 输出显示 | 执行 print | 显示文本输出 |
| 图表显示 | 执行 matplotlib | 图表以图片形式嵌入显示 |
| 错误显示 | 执行错误代码 | 红色错误框显示错误信息 |
| 添加单元格 | 点击底部"+ 代码" | 新增代码单元格 |
| 添加 Markdown | 点击"+ Markdown" | 新增 Markdown 单元格 |
| 删除单元格 | 单元格菜单→删除 | 单元格被删除 |
| 移动单元格 | 单元格菜单→上移/下移 | 单元格位置改变 |
| 切换类型 | 单元格菜单→转为 Markdown | 单元格类型切换 |
| 保存 | 点击保存按钮或 Ctrl+S | 显示保存成功提示 |
| 全部运行 | 点击"全部运行" | 依次执行所有代码单元格 |
| 返回列表 | 点击"返回列表" | 跳转回列表页 |

#### 3.3 Markdown 单元格

| 测试项 | 操作 | 预期结果 |
|--------|------|----------|
| 编辑模式 | 点击 Markdown 单元格 | 显示编辑器 |
| 预览模式 | 编辑后点击外部 | 渲染 Markdown |
| 双击编辑 | 双击渲染后的内容 | 进入编辑模式 |
| 语法支持 | 输入标题、列表、代码块 | 正确渲染 |

---

### 4. 集成测试场景

#### 场景 1: 数据分析流程

```
1. 创建新 Notebook "数据分析"
2. Cell 1: 导入库
   import pandas as pd
   import matplotlib.pyplot as plt
   
3. Cell 2: 创建数据
   df = pd.DataFrame({
       'month': ['Jan', 'Feb', 'Mar', 'Apr'],
       'sales': [100, 150, 200, 180]
   })
   df
   
4. Cell 3: 绘制图表
   plt.bar(df['month'], df['sales'])
   plt.title('Monthly Sales')
   plt.show()
   
5. 保存 Notebook
6. 刷新页面，验证数据持久化
```

#### 场景 2: 机器学习示例

```
1. 创建 Notebook "ML Demo"
2. Cell 1: 
   import numpy as np
   from sklearn.linear_model import LinearRegression
   
   X = np.array([[1], [2], [3], [4], [5]])
   y = np.array([2, 4, 5, 4, 5])
   
   model = LinearRegression()
   model.fit(X, y)
   print(f"系数: {model.coef_[0]:.2f}")
   print(f"截距: {model.intercept_:.2f}")
   
3. 验证输出正确
```

#### 场景 3: 错误恢复

```
1. 执行有错误的代码
2. 验证错误信息清晰
3. 修改代码
4. 重新执行，验证成功
```

---

### 5. 性能测试

| 测试项 | 指标 | 预期 |
|--------|------|------|
| 代码执行响应 | 简单代码执行时间 | < 500ms |
| 图表渲染 | 图表生成时间 | < 2s |
| 大数据量 | 100行代码执行 | < 5s |
| 超时处理 | 死循环代码 | 30s 后超时 |
| 并发执行 | 同时运行多个单元格 | 依次执行，无错误 |

---

### 6. 边界测试

| 测试项 | 输入 | 预期结果 |
|--------|------|----------|
| 空代码 | 执行空单元格 | 无输出，无错误 |
| 超长代码 | 1000+ 行代码 | 正常执行或超时 |
| 特殊字符 | 中文、emoji | 正常显示 |
| 无限循环 | `while True: pass` | 超时后返回错误 |
| 大量输出 | 打印 10000 行 | 输出截断或完整显示 |
| 文件操作 | 读写临时文件 | 在沙箱内正常工作 |

---

### 7. 安全测试

| 测试项 | 风险代码 | 预期结果 |
|--------|----------|----------|
| 系统命令 | `os.system('rm -rf /')` | 沙箱隔离，不影响主系统 |
| 网络请求 | `requests.get()` | 正常工作或被限制 |
| 文件访问 | 访问系统文件 | 权限限制 |
| 内存消耗 | 创建大数组 | 内存限制或超时 |

---

## 三、已知限制

1. **数据持久化**: 当前使用内存存储，重启后数据丢失（生产环境需改用数据库）
2. **执行环境**: 依赖服务器已安装的 Python 库
3. **并发限制**: 同一 Notebook 同时执行多个单元格可能有冲突
4. **输出大小**: 大量输出可能影响性能

---

## 四、后续优化建议

1. 添加数据库持久化 (SQLite/PostgreSQL)
2. 使用 Docker 容器隔离执行环境
3. 添加代码自动补全
4. 支持更多语言 (JavaScript, R)
5. 添加变量检查器
6. 支持导出为 .ipynb 格式
