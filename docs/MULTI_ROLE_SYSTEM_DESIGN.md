# 多角色系统设计文档

## 1. 概述

本文档描述了研究助手系统的多角色扩展方案，引入管理员（Admin）、导师（Mentor）、学生（Student）三种身份并建立关联关系。

### 1.1 设计目标

- 支持三种用户角色：管理员、导师、学生
- 建立导师-学生关联关系
- 支持研究组管理功能
- 实现邀请/申请机制
- 支持资源共享功能
- 保持与现有系统的兼容性

### 1.2 角色定义

| 角色 | 说明 | 主要功能 |
|-----|------|---------|
| Admin | 系统管理员 | 用户管理、角色分配、系统监控 |
| Mentor | 导师 | 学生管理、研究组管理、资源共享 |
| Student | 学生 | 申请导师、查看共享资源、日常使用 |

## 2. 数据库设计

### 2.1 新增枚举类型

```python
class UserRole(str, Enum):
    ADMIN = "admin"
    MENTOR = "mentor"
    STUDENT = "student"

class InvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

class ShareType(str, Enum):
    KNOWLEDGE_BASE = "knowledge_base"
    PAPER_COLLECTION = "paper_collection"
    NOTEBOOK = "notebook"

class SharePermission(str, Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
```

### 2.2 用户表扩展

在 `users` 表中新增以下字段：

| 字段 | 类型 | 说明 |
|-----|------|------|
| role | Enum | 用户角色，默认 student |
| mentor_id | Integer | 导师ID（学生专用） |
| department | String | 所属院系 |
| research_direction | String | 研究方向 |
| joined_at | DateTime | 加入导师时间 |

### 2.3 研究组表 (research_groups)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| name | String | 组名 |
| description | Text | 描述 |
| mentor_id | Integer | 导师ID |
| max_members | Integer | 最大成员数，默认20 |
| is_active | Boolean | 是否激活 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |

### 2.4 组成员表 (group_members)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| user_id | Integer | 用户ID |
| group_id | Integer | 组ID |
| role | String | 组内角色 (member/leader) |
| joined_at | DateTime | 加入时间 |

### 2.5 邀请表 (invitations)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| type | String | 类型 (mentor_invite/student_apply) |
| from_user_id | Integer | 发起人ID |
| to_user_id | Integer | 接收人ID |
| status | Enum | 状态 |
| message | Text | 附言 |
| expires_at | DateTime | 过期时间 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |

### 2.6 资源共享表 (shared_resources)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| resource_type | Enum | 资源类型 |
| resource_id | Integer | 资源ID |
| owner_id | Integer | 所有者ID |
| shared_with_id | Integer | 共享对象ID |
| permission | Enum | 权限级别 |
| created_at | DateTime | 创建时间 |

### 2.7 公告表 (announcements)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| title | String | 标题 |
| content | Text | 内容 |
| author_id | Integer | 作者ID |
| target_role | Enum | 目标角色（可选） |
| is_pinned | Boolean | 是否置顶 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |

### 2.8 公告已读表 (announcement_reads)

| 字段 | 类型 | 说明 |
|-----|------|------|
| id | Integer | 主键 |
| announcement_id | Integer | 公告ID |
| user_id | Integer | 用户ID |
| read_at | DateTime | 阅读时间 |

## 3. API 设计

### 3.1 管理员 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/admin/users | GET | 获取用户列表 |
| /api/admin/users/{id} | GET | 获取用户详情 |
| /api/admin/users/{id}/role | PUT | 更新用户角色 |
| /api/admin/users/{id}/toggle-active | PUT | 切换用户状态 |
| /api/admin/users/{id} | DELETE | 删除用户 |
| /api/admin/statistics | GET | 获取系统统计 |

### 3.2 导师 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/mentor/students | GET | 获取学生列表 |
| /api/mentor/students/{id} | GET | 获取学生详情 |
| /api/mentor/students/invite | POST | 邀请学生 |
| /api/mentor/students/{id} | DELETE | 移除学生 |
| /api/mentor/groups | GET | 获取研究组列表 |
| /api/mentor/groups | POST | 创建研究组 |
| /api/mentor/groups/{id} | PUT | 更新研究组 |
| /api/mentor/groups/{id} | DELETE | 删除研究组 |
| /api/mentor/groups/{id}/members | POST | 添加成员 |
| /api/mentor/groups/{id}/members/{mid} | DELETE | 移除成员 |

### 3.3 学生 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/student/mentor | GET | 获取导师信息 |
| /api/student/mentors/search | GET | 搜索导师 |
| /api/student/mentor/apply | POST | 申请导师 |
| /api/student/mentor | DELETE | 离开导师 |

### 3.4 邀请 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/invitations/sent | GET | 获取发出的邀请 |
| /api/invitations/received | GET | 获取收到的邀请 |
| /api/invitations/{id}/accept | PUT | 接受邀请 |
| /api/invitations/{id}/reject | PUT | 拒绝邀请 |
| /api/invitations/{id}/cancel | PUT | 取消邀请 |

### 3.5 共享 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/share | POST | 共享资源 |
| /api/share/by-me | GET | 获取我共享的资源 |
| /api/share/with-me | GET | 获取共享给我的资源 |
| /api/share/{id} | PUT | 更新共享权限 |
| /api/share/{id} | DELETE | 取消共享 |

### 3.6 公告 API

| 端点 | 方法 | 说明 |
|-----|------|------|
| /api/announcements | GET | 获取公告列表 |
| /api/announcements | POST | 创建公告 |
| /api/announcements/{id} | PUT | 更新公告 |
| /api/announcements/{id} | DELETE | 删除公告 |
| /api/announcements/{id}/read | POST | 标记已读 |
| /api/announcements/unread-count | GET | 获取未读数 |

## 4. 权限设计

### 4.1 角色权限矩阵

| 功能 | Admin | Mentor | Student |
|-----|-------|--------|---------|
| 用户管理 | ✓ | - | - |
| 角色分配 | ✓ | - | - |
| 系统统计 | ✓ | - | - |
| 学生管理 | - | ✓ | - |
| 研究组管理 | - | ✓ | - |
| 邀请学生 | - | ✓ | - |
| 资源共享 | - | ✓ | - |
| 申请导师 | - | - | ✓ |
| 查看共享资源 | - | ✓ | ✓ |
| 基础功能 | ✓ | ✓ | ✓ |

### 4.2 权限装饰器

```python
# 需要管理员权限
@require_admin
async def admin_only_function():
    pass

# 需要导师权限
@require_mentor
async def mentor_only_function():
    pass

# 需要学生权限
@require_student
async def student_only_function():
    pass

# 需要导师或管理员权限
@require_mentor_or_admin
async def mentor_or_admin_function():
    pass
```

## 5. 业务流程

### 5.1 导师邀请学生流程

```
导师发起邀请 → 创建邀请记录(pending) → 学生收到通知
                                      ↓
                            学生接受/拒绝邀请
                                      ↓
                      更新邀请状态 + 建立/不建立关联
```

### 5.2 学生申请导师流程

```
学生发起申请 → 创建申请记录(pending) → 导师收到通知
                                      ↓
                            导师接受/拒绝申请
                                      ↓
                      更新申请状态 + 建立/不建立关联
```

### 5.3 邀请过期机制

- 导师邀请：7天有效期
- 学生申请：30天有效期
- 过期自动标记为 cancelled

## 6. 前端设计

### 6.1 设计主题

采用「学术深空」(Academic Deep Space) 设计主题：

- 主色调：深暗色 (#0D1117, #161B22)
- 强调色：金色 (#D4AF37)、蓝色 (#4A90D9)、银蓝 (#6B8E9F)
- 成功色：翡翠绿 (#52c41a)
- 渐变效果：用于卡片头部和按钮

### 6.2 页面结构

```
/admin/users      - 用户管理页面
/mentor/students  - 学生管理页面
/mentor/groups    - 研究组管理页面
/student/mentor   - 我的导师页面
```

### 6.3 组件设计

- 角色标签：不同颜色区分角色
- 统计卡片：渐变背景展示数据
- 数据表格：支持搜索、筛选、分页
- 模态框：深色主题，圆角设计
- 空状态：友好提示引导用户

## 7. 测试方案

### 7.1 单元测试

```python
# 测试角色权限
def test_admin_can_manage_users():
    pass

def test_mentor_can_manage_students():
    pass

def test_student_can_apply_mentor():
    pass
```

### 7.2 集成测试

```python
# 测试邀请流程
def test_invitation_flow():
    # 1. 导师邀请学生
    # 2. 学生接受邀请
    # 3. 验证关联建立
    pass

# 测试申请流程
def test_application_flow():
    # 1. 学生申请导师
    # 2. 导师接受申请
    # 3. 验证关联建立
    pass
```

### 7.3 前端测试

```javascript
// 测试角色路由守卫
describe('RoleRoute', () => {
  it('should redirect non-admin from admin page', () => {})
  it('should allow admin to access admin page', () => {})
})

// 测试角色菜单
describe('RoleMenu', () => {
  it('should show admin menu for admin user', () => {})
  it('should show mentor menu for mentor user', () => {})
  it('should show student menu for student user', () => {})
})
```

## 8. 部署指南

### 8.1 数据库迁移

```bash
# 运行迁移
alembic upgrade head

# 或指定版本
alembic upgrade 006_multi_role
```

### 8.2 创建管理员

```bash
# 交互式创建
python scripts/create_admin.py

# 命令行参数
python scripts/create_admin.py \
  --email admin@example.com \
  --username admin \
  --password your_password
```

### 8.3 前端部署

```bash
# 安装依赖
npm install

# 构建
npm run build

# 开发模式
npm run dev
```

## 9. 注意事项

1. **向后兼容**：现有用户默认为 student 角色
2. **数据安全**：删除用户前需处理关联数据
3. **性能考虑**：大量用户时使用分页查询
4. **邀请管理**：定期清理过期邀请
5. **权限检查**：所有API都需验证权限

## 10. 后续优化

1. 添加批量操作功能
2. 支持导师之间的协作
3. 添加活动日志记录
4. 支持更细粒度的资源权限
5. 添加数据导出功能
