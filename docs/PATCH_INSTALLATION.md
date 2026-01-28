# 多角色系统补丁安装指南

## 概述

本补丁为研究助手系统添加多角色功能，包括管理员、导师、学生三种角色及其相关管理功能。

## 补丁内容

```
patch/
├── backend/
│   ├── alembic/versions/
│   │   └── 006_multi_role.py          # 数据库迁移（新增表和字段）
│   ├── app/
│   │   ├── api/
│   │   │   ├── admin.py               # 管理员API（新增）
│   │   │   ├── mentor.py              # 导师API（新增）
│   │   │   ├── student.py             # 学生API（新增）
│   │   │   ├── invitations.py         # 邀请API（新增）
│   │   │   ├── share.py               # 共享API（新增）
│   │   │   └── announcements.py       # 公告API（新增）
│   │   ├── core/
│   │   │   └── permissions.py         # 权限装饰器（新增）
│   │   ├── models/
│   │   │   ├── __init__.py            # 模型导出（覆盖更新）
│   │   │   ├── role.py                # 角色相关模型（新增）
│   │   │   └── user.py                # 用户模型（覆盖更新）
│   │   ├── schemas/
│   │   │   ├── role.py                # 角色相关Schema（新增）
│   │   │   └── user.py                # 用户Schema（覆盖更新）
│   │   └── main.py                    # 主入口（覆盖更新，添加路由）
│   └── scripts/
│       └── create_admin.py            # 管理员创建脚本（新增）
├── frontend/
│   └── src/
│       ├── App.tsx                    # 应用入口（覆盖更新，添加路由）
│       ├── components/layout/
│       │   └── MainLayout.tsx         # 主布局（覆盖更新，添加角色菜单）
│       ├── pages/
│       │   ├── admin/
│       │   │   ├── index.ts           # 导出（新增）
│       │   │   └── UsersPage.tsx      # 用户管理页（新增）
│       │   ├── mentor/
│       │   │   ├── index.ts           # 导出（新增）
│       │   │   ├── StudentsPage.tsx   # 学生管理页（新增）
│       │   │   └── GroupsPage.tsx     # 研究组管理页（新增）
│       │   └── student/
│       │       ├── index.ts           # 导出（新增）
│       │       └── MentorPage.tsx     # 我的导师页（新增）
│       ├── services/
│       │   └── api.ts                 # API服务（覆盖更新，添加角色API）
│       └── stores/
│           ├── authStore.ts           # 认证状态（覆盖更新，添加角色支持）
│           └── roleStore.ts           # 角色状态管理（新增）
└── docs/
    ├── MULTI_ROLE_SYSTEM_DESIGN.md    # 系统设计文档
    └── PATCH_INSTALLATION.md          # 安装指南（本文档）
```

## 安装步骤

### 1. 备份现有数据

```bash
# 备份数据库
pg_dump -U postgres research_assistant > backup_$(date +%Y%m%d).sql

# 备份代码
cp -r research-assistant research-assistant-backup
```

### 2. 复制补丁文件

```bash
# 进入项目目录
cd research-assistant

# 复制后端文件
cp -r patch/backend/* backend/

# 复制前端文件
cp -r patch/frontend/* frontend/

# 复制文档
cp -r patch/docs/* docs/
```

### 3. 运行数据库迁移

**⚠️ 重要**: 如果之前迁移失败过，**必须先清理数据库中的残留数据**！

#### Docker 环境清理方法（推荐）：

```bash
# 方法1: 使用 SQL 脚本清理（推荐）
docker exec -i research_postgres psql -U research_user -d research_assistant < backend/scripts/cleanup_migration.sql

# 方法2: 手动执行 SQL
docker exec -it research_postgres psql -U research_user -d research_assistant

# 在 psql 中执行：
DROP TYPE IF EXISTS share_permission CASCADE;
DROP TYPE IF EXISTS share_type CASCADE;
DROP TYPE IF EXISTS invitation_status CASCADE;
DROP TYPE IF EXISTS user_role CASCADE;
DELETE FROM alembic_version WHERE version_num = '006_multi_role';
\q

# 清理完成后重启容器
docker-compose restart backend
```

#### 非 Docker 环境清理方法：

```bash
cd backend

# 使用 Python 脚本清理
python scripts/cleanup_failed_migration.py

# 或直接连接数据库执行 SQL
psql -U research_user -d research_assistant < scripts/cleanup_migration.sql
```

#### 全新安装（无需清理）：

```bash
cd backend
alembic upgrade head
```

### 4. 创建管理员账户

```bash
# 交互式创建（推荐）
python scripts/create_admin.py

# 或使用命令行参数
python scripts/create_admin.py \
  --email admin@example.com \
  --username admin \
  --password your_secure_password
```

### 5. 安装前端依赖

```bash
cd frontend

# 如果有新依赖
npm install
```

### 6. 重启服务

```bash
# 重启后端
# 使用 systemd
sudo systemctl restart research-assistant-backend

# 或使用 Docker
docker-compose restart backend

# 或手动重启
pkill -f "uvicorn"
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 重启前端（如果是开发模式）
npm run dev
```

## 验证安装

### 1. 检查数据库迁移

```sql
-- 连接数据库
psql -U postgres -d research_assistant

-- 检查新表
\dt

-- 应该看到以下新表：
-- research_groups
-- group_members
-- invitations
-- shared_resources
-- announcements
-- announcement_reads

-- 检查用户表新字段
\d users
-- 应该看到 role, mentor_id, department, research_direction, joined_at 字段
```

### 2. 检查 API 端点

```bash
# 获取 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"your_password"}' \
  | jq -r '.access_token')

# 测试管理员API
curl -X GET http://localhost:8000/api/admin/users \
  -H "Authorization: Bearer $TOKEN"

# 测试系统统计
curl -X GET http://localhost:8000/api/admin/statistics \
  -H "Authorization: Bearer $TOKEN"
```

### 3. 检查前端页面

1. 以管理员身份登录
2. 检查侧边栏是否显示「用户管理」菜单
3. 访问 /admin/users 页面
4. 验证用户列表和统计数据显示正常

## 配置说明

### 环境变量

补丁不需要额外的环境变量配置。

### 权限配置

默认权限配置：
- 新注册用户默认为 `student` 角色
- 只有 `admin` 可以修改用户角色
- 邀请有效期：导师邀请 7 天，学生申请 30 天
- 研究组最大成员数：20（可在创建时自定义）

## 回滚方案

如需回滚，执行以下步骤：

### 1. 回滚数据库

```bash
cd backend

# 回滚到上一版本
alembic downgrade -1

# 或回滚到指定版本
alembic downgrade 005_xxx  # 替换为实际的上一版本号
```

### 2. 恢复代码

```bash
# 从备份恢复
cp -r research-assistant-backup/* research-assistant/

# 或使用 git
git checkout HEAD -- backend/app/models/user.py
git checkout HEAD -- backend/app/schemas/user.py
git checkout HEAD -- backend/app/main.py
# ... 其他需要恢复的文件
```

### 3. 恢复数据库（如果需要）

```bash
psql -U postgres -d research_assistant < backup_YYYYMMDD.sql
```

## 常见问题

### Q: 迁移失败怎么办？

A: 检查以下几点：
1. 确保数据库连接正常
2. 检查是否有未完成的迁移
3. 查看 alembic 日志获取详细错误信息

```bash
# 查看当前迁移状态
alembic current

# 查看迁移历史
alembic history
```

### Q: 前端页面显示异常？

A: 尝试以下步骤：
1. 清除浏览器缓存
2. 检查控制台错误
3. 确保后端 API 正常运行
4. 重新构建前端

```bash
cd frontend
rm -rf node_modules/.cache
npm run build
```

### Q: 权限不正确？

A: 检查以下几点：
1. 确认用户角色已正确设置
2. 清除 localStorage 中的缓存登录状态
3. 重新登录获取新的 token

```javascript
// 在浏览器控制台执行
localStorage.removeItem('auth-storage')
location.reload()
```

### Q: 如何将现有用户升级为导师或管理员？

A: 使用管理员账户登录后，在用户管理页面修改用户角色，或直接修改数据库：

```sql
-- 设置为导师
UPDATE users SET role = 'mentor' WHERE email = 'user@example.com';

-- 设置为管理员
UPDATE users SET role = 'admin' WHERE email = 'user@example.com';
```

## 技术支持

如遇到问题，请：
1. 查阅 `docs/MULTI_ROLE_SYSTEM_DESIGN.md` 了解系统设计
2. 查阅 `docs/API_ENDPOINTS.md` 了解 API 详情
3. 提交 Issue 并附上错误日志
