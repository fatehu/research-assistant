# 文献管理模块测试指南

## 测试前准备

### 1. 启动服务

```bash
cd research-assistant
docker-compose up -d
```

或者分别启动：

```bash
# 后端
cd backend
pip install -r requirements.txt
alembic upgrade head  # 运行数据库迁移
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

### 2. 确认服务状态

```bash
# 检查后端健康状态
curl http://localhost:8000/health

# 预期返回
# {"status":"healthy","database":"connected",...}
```

---

## 第一部分：后端 API 测试

### 测试 1.1：用户登录获取 Token

```bash
# 如果没有账户，先注册
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","username":"testuser","password":"test123456"}'

# 登录获取 token
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"test123456"}'

# 保存返回的 access_token，后续请求需要使用
# 设置环境变量（替换为实际 token）
export TOKEN="your_access_token_here"
```

### 测试 1.2：初始化文献模块

```bash
curl -X POST http://localhost:8000/api/literature/init \
  -H "Authorization: Bearer $TOKEN"

# 预期返回
# {"message":"初始化成功","collections_created":4}
# 或者如果已初始化
# {"message":"已初始化"}
```

### 测试 1.3：搜索论文 (Semantic Scholar)

```bash
curl -X GET "http://localhost:8000/api/literature/search?query=transformer+attention&source=semantic_scholar&limit=5" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回：包含 papers 数组的 JSON
# {
#   "total": 1000,
#   "offset": 0,
#   "papers": [
#     {
#       "source": "semantic_scholar",
#       "external_id": "xxx",
#       "title": "Attention Is All You Need",
#       "authors": [...],
#       "citation_count": 50000,
#       ...
#     }
#   ],
#   "query": "transformer attention",
#   "source": "semantic_scholar"
# }
```

### 测试 1.4：搜索论文 (arXiv)

```bash
curl -X GET "http://localhost:8000/api/literature/search?query=large+language+model&source=arxiv&limit=5" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回：arXiv 论文列表
```

### 测试 1.5：保存论文

```bash
# 使用搜索结果中的数据保存论文
curl -X POST http://localhost:8000/api/literature/papers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "semantic_scholar",
    "external_id": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention Is All You Need",
    "abstract": "The dominant sequence transduction models...",
    "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
    "year": 2017,
    "venue": "NeurIPS",
    "citation_count": 50000,
    "url": "https://arxiv.org/abs/1706.03762",
    "pdf_url": "https://arxiv.org/pdf/1706.03762.pdf",
    "arxiv_id": "1706.03762",
    "fields_of_study": ["Computer Science", "Machine Learning"]
  }'

# 预期返回：保存的论文详情，包含 id
```

### 测试 1.6：获取论文列表

```bash
curl -X GET "http://localhost:8000/api/literature/papers" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回：用户保存的所有论文列表
```

### 测试 1.7：获取收藏夹列表

```bash
curl -X GET "http://localhost:8000/api/literature/collections" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回：默认收藏夹列表
# [
#   {"id":1,"name":"所有论文","is_default":true,...},
#   {"id":2,"name":"待读",...},
#   {"id":3,"name":"已读",...},
#   {"id":4,"name":"收藏",...}
# ]
```

### 测试 1.8：创建新收藏夹

```bash
curl -X POST http://localhost:8000/api/literature/collections \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "深度学习",
    "description": "深度学习相关论文",
    "color": "#8b5cf6"
  }'

# 预期返回：新创建的收藏夹
```

### 测试 1.9：添加论文到收藏夹

```bash
# 替换 paper_id 和 collection_id 为实际值
curl -X POST http://localhost:8000/api/literature/collections/add-paper \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "paper_id": 1,
    "collection_ids": [2, 5]
  }'

# 预期返回
# {"message":"已添加到收藏夹"}
```

### 测试 1.10：更新论文（添加笔记、评分）

```bash
curl -X PATCH http://localhost:8000/api/literature/papers/1 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "notes": "这是 Transformer 的开创性论文，提出了自注意力机制",
    "rating": 5,
    "is_read": true,
    "tags": ["transformer", "attention", "经典论文"]
  }'

# 预期返回：更新后的论文详情
```

### 测试 1.11：获取引用图谱

```bash
# 需要论文有 semantic_scholar_id
curl -X GET "http://localhost:8000/api/literature/graph/1?max_nodes=20" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回
# {
#   "nodes": [
#     {"id": "xxx", "title": "...", "type": "center", ...},
#     {"id": "yyy", "title": "...", "type": "citing", ...},
#     {"id": "zzz", "title": "...", "type": "referenced", ...}
#   ],
#   "edges": [
#     {"from": "yyy", "to": "xxx", "type": "cites"},
#     {"from": "xxx", "to": "zzz", "type": "cites"}
#   ],
#   "center_id": "xxx"
# }
```

### 测试 1.12：获取搜索历史

```bash
curl -X GET "http://localhost:8000/api/literature/search/history?limit=10" \
  -H "Authorization: Bearer $TOKEN"

# 预期返回：搜索历史列表
```

---

## 第二部分：前端 UI 测试

### 测试 2.1：访问文献管理页面

1. 打开浏览器访问 `http://localhost:3000`
2. 登录账户
3. 点击左侧导航栏的 **"文献管理"**
4. **验证**：页面应显示左侧收藏夹列表、搜索栏、论文列表区域

### 测试 2.2：搜索论文

1. 在搜索栏选择数据源（Semantic Scholar 或 arXiv）
2. 输入搜索词，如 "BERT language model"
3. 点击搜索按钮
4. **验证**：
   - 显示"搜索结果"标签页
   - 显示论文卡片列表
   - 每个卡片显示标题、作者、年份、引用数、摘要

### 测试 2.3：保存论文

1. 在搜索结果中找到一篇论文
2. 点击论文卡片右侧的 **"保存"** 按钮
3. **验证**：
   - 按钮变为 "已保存" 状态
   - 提示 "论文已保存"
   - 切换到"我的文献库"可以看到该论文

### 测试 2.4：切换视图模式

1. 点击右上角的视图切换按钮
2. 分别测试 **卡片视图** 和 **列表视图**
3. **验证**：论文以不同方式展示

### 测试 2.5：收藏夹管理

1. 在左侧收藏夹区域点击 **"+"** 按钮
2. 输入收藏夹名称和颜色
3. 点击确认创建
4. **验证**：新收藏夹出现在列表中

5. 点击不同收藏夹
6. **验证**：论文列表根据收藏夹过滤

### 测试 2.6：查看论文详情

1. 点击某篇论文卡片
2. **验证**：右侧详情面板打开，显示：
   - 论文标题
   - 作者信息
   - 发表年份和期刊
   - 引用数
   - 摘要（可展开）
   - 链接（论文主页、PDF、arXiv、DOI）
   - 收藏夹标签
   - 用户标签
   - 笔记区域
   - 评分

### 测试 2.7：编辑笔记和标签

1. 在详情面板中点击笔记旁的 **"编辑"** 按钮
2. 输入笔记内容
3. 点击保存
4. **验证**：笔记保存成功

5. 点击标签旁的编辑按钮
6. 添加几个标签
7. 点击保存
8. **验证**：标签显示在论文卡片上

### 测试 2.8：评分和阅读状态

1. 在详情面板中点击星星评分
2. **验证**：评分更新

3. 点击 "标记为已读" 按钮
4. **验证**：论文显示"已读"标签

### 测试 2.9：查看引用图谱

1. 选择一篇来自 Semantic Scholar 的论文
2. 点击详情面板中的 **"引用图谱"** 按钮
3. **验证**：
   - 切换到图谱标签页
   - 显示节点和连接
   - 中心节点（红色）= 当前论文
   - 绿色节点 = 引用此论文的论文
   - 黄色节点 = 此论文引用的论文

4. 测试图谱交互：
   - 鼠标滚轮缩放
   - 拖拽移动
   - 点击节点显示详情
   - 使用工具栏按钮（放大、缩小、适应、聚焦）
   - 切换布局模式

### 测试 2.10：下载 PDF

1. 找到有 PDF 链接的论文
2. 点击 **"下载 PDF"** 按钮
3. **验证**：
   - 显示下载中状态
   - 下载成功后显示"PDF 已下载"

---

## 第三部分：Agent 工具测试

### 测试 3.1：通过对话搜索论文

1. 进入 **"AI 对话"** 页面
2. 发送消息：
   ```
   帮我搜索关于 GPT-4 的最新论文
   ```
3. **验证**：
   - AI 调用 `literature_search` 工具
   - 返回相关论文列表
   - 显示标题、作者、年份等信息

### 测试 3.2：指定数据源搜索

1. 发送消息：
   ```
   在 arXiv 上搜索 vision transformer 相关论文，只要最近两年的
   ```
2. **验证**：
   - AI 使用 arXiv 数据源
   - 返回 2023-2024 年的论文

### 测试 3.3：组合查询

1. 发送消息：
   ```
   搜索深度学习在医学影像方面的应用，给我5篇引用最多的论文
   ```
2. **验证**：AI 返回按引用数排序的相关论文

---

## 第四部分：错误处理测试

### 测试 4.1：网络错误

1. 断开网络连接
2. 尝试搜索论文
3. **验证**：显示友好的错误提示

### 测试 4.2：重复保存

1. 尝试保存同一篇论文两次
2. **验证**：提示"论文已存在"

### 测试 4.3：删除默认收藏夹

1. 尝试删除"所有论文"收藏夹
2. **验证**：提示"默认收藏夹不可删除"

### 测试 4.4：无效搜索

1. 搜索空字符串或特殊字符
2. **验证**：显示适当的提示

---

## 测试检查清单

| 功能 | 测试点 | 通过 |
|------|--------|------|
| **搜索** | Semantic Scholar 搜索 | ☐ |
| | arXiv 搜索 | ☐ |
| | 年份过滤 | ☐ |
| | 搜索历史记录 | ☐ |
| **论文管理** | 保存论文 | ☐ |
| | 查看论文列表 | ☐ |
| | 查看论文详情 | ☐ |
| | 更新笔记 | ☐ |
| | 更新标签 | ☐ |
| | 更新评分 | ☐ |
| | 标记已读/未读 | ☐ |
| | 删除论文 | ☐ |
| **收藏夹** | 查看收藏夹列表 | ☐ |
| | 创建收藏夹 | ☐ |
| | 添加论文到收藏夹 | ☐ |
| | 从收藏夹移除论文 | ☐ |
| | 删除收藏夹 | ☐ |
| | 按收藏夹过滤 | ☐ |
| **引用图谱** | 加载图谱数据 | ☐ |
| | 节点显示正确 | ☐ |
| | 边显示正确 | ☐ |
| | 缩放/拖拽交互 | ☐ |
| | 布局切换 | ☐ |
| **PDF** | 检测 PDF 链接 | ☐ |
| | 下载 PDF | ☐ |
| **Agent** | 调用搜索工具 | ☐ |
| | 返回格式化结果 | ☐ |
| **UI** | 卡片视图 | ☐ |
| | 列表视图 | ☐ |
| | 详情面板 | ☐ |
| | 响应式布局 | ☐ |

---

## 常见问题

### Q: 搜索返回空结果
A: 检查网络连接，Semantic Scholar API 可能有速率限制，稍后重试

### Q: 引用图谱无法加载
A: 确保论文有 `semantic_scholar_id`，arXiv 来源的论文不支持引用图谱

### Q: PDF 下载失败
A: 部分论文 PDF 可能需要付费或受访问限制

### Q: 数据库迁移失败
A: 检查数据库连接，确保运行 `alembic upgrade head`
