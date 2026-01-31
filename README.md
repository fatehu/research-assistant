# 研究助手多角色系统 (Multi-Role Research Assistant)

[![Version](https://img.shields.io/badge/version-1.0.18-blue.svg)]()
[![Sessions](https://img.shields.io/badge/sessions-1--18-green.svg)]()

一个为科研团队设计的智能研究助手系统，支持多角色协作、资源共享、AI 对话等功能。

## 🎯 系统概述

### 核心功能

| 功能模块 | 说明 |
|---------|------|
| **多角色系统** | 管理员、导师、学生三级角色体系 |
| **团队协作** | 研究组管理、邀请系统、导师-学生关系 |
| **资源共享** | 论文、知识库、文献集、笔记本共享 |
| **AI 智能助手** | 基于知识库的对话、代码执行、文献分析 |
| **文献管理** | 论文搜索、收藏、分类、标注 |
| **知识库** | 文档向量化、语义搜索、RAG 检索 |
| **代码实验室** | Jupyter 风格笔记本、Python 执行环境 |

### 角色权限

```
┌────────────────────────────────────────────────────────────┐
│                        管理员 (Admin)                       │
│  • 用户管理（创建、审核、禁用）                              │
│  • 角色分配（设置导师、学生）                                │
│  • 系统配置                                                │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                        导师 (Mentor)                        │
│  • 学生管理（邀请、移除、查看活动）                          │
│  • 研究组管理（创建、配置、成员管理）                        │
│  • 资源共享（知识库、论文、笔记本）                          │
│  • 发布公告                                                │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                        学生 (Student)                       │
│  • 访问共享资源（只读）                                      │
│  • 个人文献库管理                                           │
│  • 个人知识库管理                                           │
│  • AI 对话（可使用共享知识库）                               │
│  • 查看公告、申请加入研究组                                  │
└────────────────────────────────────────────────────────────┘
```

## 📦 安装指南

### 前置要求

- Docker & Docker Compose
- 已部署的研究助手基础系统

### 快速安装

```bash
# 1. 解压补丁包
unzip multi-role-patch.zip

# 2. 备份现有文件
cp -r backend/app backend/app.bak
cp -r frontend/src frontend/src.bak

# 3. 应用补丁
cp -rf patch/backend/* backend/
cp -rf patch/frontend/* frontend/

# 4. 运行数据库迁移
docker exec -it research_backend alembic upgrade head

# 5. 重启服务
docker-compose restart backend frontend

# 6. 创建管理员账户
docker exec -it research_backend python scripts/create_admin.py
```

### 升级已有系统

如果您之前已安装过旧版本，需要先修改数据库：

```bash
# 修改 shared_resources 表的 resource_id 列类型
docker exec -i research_postgres psql -U research_user -d research_assistant \
  < patch/backend/scripts/migrate_resource_id_type.sql
```

## 🔧 配置说明

### 环境变量

在 `.env` 文件中配置：

```env
# 共享功能开关（默认开启）
SHARING_ENABLED=true

# 数据库配置
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname

# AI 模型配置
OPENAI_API_KEY=your_api_key
```

## 📖 功能详解

### 1. 多角色系统

#### 创建管理员
```bash
docker exec -it research_backend python scripts/create_admin.py
# 按提示输入用户名、密码、邮箱
```

#### 角色转换（管理员操作）
- 访问 `/admin/users`
- 选择用户 → 更改角色 → 选择导师/学生

### 2. 团队协作

#### 导师邀请学生
1. 导师访问 `/mentor/students`
2. 点击「邀请学生」
3. 输入学生邮箱
4. 学生收到邀请后接受

#### 创建研究组
1. 导师访问 `/mentor/groups`
2. 点击「创建研究组」
3. 设置名称、描述、最大人数

### 3. 资源共享

#### 共享知识库（引用模式）

```
导师                              学生
┌──────────────┐                ┌──────────────┐
│ 📚 我的知识库 │   ──共享──>   │ 📤 共享知识库 │
│  - doc1.pdf  │    (引用)      │  (只读引用)  │
│  - doc2.md   │                │              │
└──────────────┘                └──────────────┘
                                       │
                                       ▼
                              AI 对话可直接使用
```

**特点**：
- 不复制数据，只建立引用
- 导师更新后学生即时可见
- 学生可在 AI 对话中选择使用

#### 共享笔记本（只读模式）

导师共享笔记本后，学生可以：
- 查看所有代码单元格
- 查看 Markdown 内容
- 查看执行输出和图表
- **不能**编辑或执行代码

### 4. AI 对话集成

学生在 AI 对话中可以：
- 选择自己的知识库
- 选择导师共享的知识库
- 同时搜索多个知识库

```typescript
// 前端选择器自动包含共享知识库
const { own, shared } = await knowledgeApi.getAvailableKnowledgeBases()
```

## 🗂️ 文件结构

```
patch/
├── backend/
│   ├── alembic/versions/
│   │   └── 006_multi_role.py        # 数据库迁移
│   ├── app/
│   │   ├── api/
│   │   │   ├── admin.py             # 管理员 API
│   │   │   ├── mentor.py            # 导师 API
│   │   │   ├── student.py           # 学生 API
│   │   │   ├── share.py             # 资源共享 API
│   │   │   ├── invitations.py       # 邀请系统 API
│   │   │   ├── announcements.py     # 公告 API
│   │   │   ├── knowledge.py         # 知识库 API (修改)
│   │   │   └── literature.py        # 文献 API (修改)
│   │   ├── models/
│   │   │   ├── user.py              # 用户模型 (修改)
│   │   │   └── role.py              # 角色相关模型
│   │   ├── schemas/
│   │   │   ├── user.py              # 用户 Schema
│   │   │   └── role.py              # 角色 Schema
│   │   ├── services/
│   │   │   └── agent_tools.py       # AI 工具 (修改)
│   │   └── core/
│   │       └── permissions.py       # 权限控制
│   └── scripts/
│       ├── create_admin.py          # 创建管理员
│       └── migrate_resource_id_type.sql  # 列类型迁移
│
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── admin/               # 管理员页面
│   │   │   ├── mentor/              # 导师页面
│   │   │   ├── student/             # 学生页面
│   │   │   ├── shared/              # 共享资源页面
│   │   │   ├── dashboard/           # 仪表盘
│   │   │   ├── knowledge/           # 知识库 (修改)
│   │   │   ├── literature/          # 文献 (修改)
│   │   │   └── user/                # 用户设置
│   │   ├── components/
│   │   │   ├── layout/              # 布局组件
│   │   │   └── team/                # 团队组件
│   │   ├── stores/
│   │   │   ├── authStore.ts         # 认证 (修改)
│   │   │   ├── roleStore.ts         # 角色状态
│   │   │   ├── knowledgeStore.ts    # 知识库 (修改)
│   │   │   └── literatureStore.ts   # 文献 (修改)
│   │   ├── services/
│   │   │   └── api.ts               # API 服务 (修改)
│   │   └── App.tsx                  # 路由 (修改)
│
└── docs/
    ├── FEATURE_LIST.md              # 功能清单
    ├── MULTI_ROLE_SYSTEM_DESIGN.md  # 系统设计
    └── PATCH_INSTALLATION.md        # 安装说明
```

## 🔌 API 端点

### 管理员 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 获取用户列表 |
| PUT | `/api/admin/users/{id}/role` | 修改用户角色 |
| PUT | `/api/admin/users/{id}/status` | 启用/禁用用户 |

### 导师 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/mentor/students` | 获取我的学生 |
| GET | `/api/mentor/groups` | 获取研究组 |
| POST | `/api/mentor/groups` | 创建研究组 |
| DELETE | `/api/mentor/students/{id}` | 移除学生 |

### 学生 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/student/mentor` | 获取我的导师 |
| GET | `/api/student/groups` | 获取我的研究组 |
| POST | `/api/student/apply/{mentor_id}` | 申请导师 |

### 资源共享 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/share/shared-with-me` | 共享给我的资源 |
| GET | `/api/share/my-shares` | 我共享的资源 |
| POST | `/api/share/` | 共享资源 |
| DELETE | `/api/share/{id}` | 取消共享 |
| GET | `/api/share/detail/{id}` | 共享资源详情 |
| GET | `/api/share/my-notebooks` | 我的笔记本列表 |

### 知识库 API（扩展）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/knowledge/available` | 获取可用知识库（含共享）|
| POST | `/api/knowledge/search?include_shared=true` | 搜索（含共享知识库）|

## 🐛 故障排除

### 问题：共享资源页面 500 错误

**原因**：数据库 `resource_id` 列类型不匹配

**解决**：
```bash
docker exec -i research_postgres psql -U research_user -d research_assistant \
  -c "ALTER TABLE shared_resources ALTER COLUMN resource_id TYPE VARCHAR(50) USING resource_id::VARCHAR;"
```

### 问题：看不到共享的知识库

**检查步骤**：
1. 确认 `SHARING_ENABLED=true`
2. 检查导师是否已共享
3. 检查学生是否已关联导师

```bash
# 查看后端日志
docker logs -f research_backend

# 检查共享记录
docker exec -it research_postgres psql -U research_user -d research_assistant \
  -c "SELECT * FROM shared_resources;"
```

### 问题：迁移失败

**解决**：
```bash
# 清理失败的迁移
docker exec -it research_backend python scripts/cleanup_failed_migration.py

# 重新运行迁移
docker exec -it research_backend alembic upgrade head
```

## 📊 数据模型

### 新增数据表

```sql
-- 研究组
research_groups (id, name, description, mentor_id, ...)

-- 组成员
group_members (id, group_id, user_id, role, joined_at)

-- 邀请记录
invitations (id, type, from_user_id, to_user_id, group_id, status, ...)

-- 共享资源
shared_resources (id, resource_type, resource_id, owner_id, 
                  shared_with_type, shared_with_id, permission, ...)

-- 公告
announcements (id, mentor_id, group_id, title, content, ...)

-- 公告已读
announcement_reads (id, announcement_id, user_id, read_at)
```

### 用户表扩展

```sql
-- users 表新增字段
ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'student';
ALTER TABLE users ADD COLUMN mentor_id INTEGER REFERENCES users(id);
ALTER TABLE users ADD COLUMN department VARCHAR(200);
ALTER TABLE users ADD COLUMN research_direction VARCHAR(500);
```

## 🔄 版本历史

| 版本 | Session | 主要更新 |
|------|---------|----------|
| 1.0.1-7 | 1-7 | 多角色基础系统 |
| 1.0.8-11 | 8-11 | 邀请系统、公告功能 |
| 1.0.12-13 | 12-13 | 资源共享（论文、文献集）|
| 1.0.14-16 | 14-16 | 知识库共享（引用模式）|
| 1.0.17 | 17 | 全局搜索支持共享 |
| 1.0.18 | 18 | 笔记本共享（只读）|

## 📝 License

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**开发者**：Claude AI Assistant  
**最后更新**：2026-01-31
