# 文献管理模块

## 功能概述

文献管理模块提供完整的学术论文管理功能，包括：

1. **论文搜索** - 接入 Semantic Scholar 和 arXiv API
2. **收藏夹管理** - 创建收藏夹组织论文
3. **PDF 下载** - 自动下载 PDF 并存储到知识库
4. **引用图谱** - 使用 Vis.js 可视化论文引用关系
5. **Agent 工具** - AI 助手可调用文献搜索

## 文件结构

```
backend/
├── app/
│   ├── models/
│   │   └── literature.py      # 数据模型（Paper, PaperCollection 等）
│   ├── schemas/
│   │   └── literature.py      # Pydantic 模式
│   ├── services/
│   │   └── literature_service.py  # Semantic Scholar/arXiv API 服务
│   └── api/
│       └── literature.py      # REST API 路由
├── alembic/versions/
│   └── 004_literature.py      # 数据库迁移

frontend/
├── src/
│   ├── pages/literature/
│   │   ├── LiteraturePage.tsx     # 主页面
│   │   ├── CitationGraph.tsx      # 引用图谱组件
│   │   ├── PaperDetailPanel.tsx   # 论文详情面板
│   │   └── index.ts
│   ├── stores/
│   │   └── literatureStore.ts     # Zustand 状态管理
│   └── services/
│       └── api.ts                 # API 类型和方法（已更新）
```

## API 端点

### 搜索
- `GET /api/literature/search` - 搜索论文
- `GET /api/literature/search/history` - 搜索历史

### 论文管理
- `GET /api/literature/papers` - 获取论文列表
- `GET /api/literature/papers/{id}` - 获取论文详情
- `POST /api/literature/papers` - 保存论文
- `PATCH /api/literature/papers/{id}` - 更新论文
- `DELETE /api/literature/papers/{id}` - 删除论文
- `POST /api/literature/papers/{id}/download-pdf` - 下载 PDF

### 收藏夹
- `GET /api/literature/collections` - 获取收藏夹列表
- `POST /api/literature/collections` - 创建收藏夹
- `PATCH /api/literature/collections/{id}` - 更新收藏夹
- `DELETE /api/literature/collections/{id}` - 删除收藏夹
- `POST /api/literature/collections/add-paper` - 添加论文到收藏夹
- `POST /api/literature/collections/remove-paper` - 从收藏夹移除论文

### 引用图谱
- `GET /api/literature/graph/{paper_id}` - 获取论文引用图谱

### 初始化
- `POST /api/literature/init` - 初始化用户默认收藏夹

## Agent 工具

Agent 可以使用 `literature_search` 工具搜索学术论文：

```python
# 工具参数
{
    "query": "搜索关键词",
    "source": "semantic_scholar" | "arxiv",
    "max_results": 5,
    "year_start": 2020,  # 可选
    "year_end": 2024     # 可选
}
```

示例对话：
```
用户: 帮我搜索关于 transformer 架构的最新论文
助手: [调用 literature_search 工具]
      找到以下相关论文：
      1. Attention is All You Need (2017)
      2. BERT: Pre-training of Deep Bidirectional Transformers (2018)
      ...
```

## 数据模型

### Paper（论文）
- `id`: 主键
- `user_id`: 用户 ID
- `semantic_scholar_id`: Semantic Scholar ID
- `arxiv_id`: arXiv ID
- `doi`: DOI
- `title`: 标题
- `abstract`: 摘要
- `authors`: 作者列表（JSON）
- `year`: 发表年份
- `venue`: 发表期刊/会议
- `citation_count`: 引用数
- `pdf_url`: PDF 链接
- `pdf_path`: 本地 PDF 路径
- `pdf_downloaded`: 是否已下载
- `tags`: 用户标签
- `notes`: 用户笔记
- `rating`: 评分 (1-5)
- `is_read`: 阅读状态

### PaperCollection（收藏夹）
- `id`: 主键
- `user_id`: 用户 ID
- `name`: 名称
- `description`: 描述
- `color`: 颜色
- `icon`: 图标
- `is_default`: 是否默认
- `paper_count`: 论文数量

## 使用说明

### 1. 运行数据库迁移

```bash
cd backend
alembic upgrade head
```

### 2. 安装前端依赖

```bash
cd frontend
npm install vis-network
```

### 3. 配置 API Key（可选）

在 `.env` 中配置 Semantic Scholar API Key 以获得更高的请求配额：

```
SEMANTIC_SCHOLAR_API_KEY=your_api_key_here
```

### 4. 启动服务

```bash
# 后端
cd backend
uvicorn app.main:app --reload

# 前端
cd frontend
npm run dev
```

### 5. 访问文献管理

登录后，在侧边栏点击"文献管理"即可进入。

## 功能详解

### 论文搜索
- 支持 Semantic Scholar（更全面的引用数据）和 arXiv（最新预印本）
- 可按年份过滤
- 显示引用数、作者、摘要等信息
- 一键保存到个人文献库

### 收藏夹
- 默认创建：所有论文、待读、已读、收藏
- 自定义创建新收藏夹
- 支持自定义颜色
- 论文可属于多个收藏夹

### 引用图谱
- 基于 Vis.js 实现交互式图谱
- 显示论文的引用关系（引用了谁、被谁引用）
- 支持缩放、拖拽、聚焦
- 多种布局模式（层级、力导向）

### PDF 管理
- 自动检测 PDF 链接
- 下载 PDF 到本地
- 可选择添加到知识库（支持向量检索）

## 注意事项

1. **API 限制**: Semantic Scholar 公共 API 有速率限制，建议配置 API Key
2. **arXiv**: arXiv API 不返回引用数，仅返回预印本信息
3. **引用图谱**: 仅 Semantic Scholar 来源的论文支持引用图谱功能
4. **PDF 下载**: 并非所有论文都有公开的 PDF 链接
