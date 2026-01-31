# 研究助手系统 - 新开发功能清单

## 📋 版本信息
- **开发周期**: Session 1-17
- **最后更新**: 2026-01-31
- **补丁文件数**: 54

---

## 🎯 一、多角色系统 (Sessions 1-7)

### 1.1 角色体系
| 角色 | 权限 | 功能入口 |
|------|------|----------|
| **管理员 (admin)** | 系统管理、用户审核 | `/admin/*` |
| **导师 (mentor)** | 学生管理、资源共享、公告发布 | `/mentor/*` |
| **学生 (student)** | 查看共享资源、接收公告 | `/student/*` |

### 1.2 数据库模型
- `ResearchGroup` - 研究组
- `GroupMember` - 组成员关系
- `Invitation` - 邀请/申请记录
- `SharedResource` - 共享资源
- `Announcement` - 公告
- `AnnouncementRead` - 公告已读记录

### 1.3 邀请系统
- 导师邀请学生加入研究组
- 学生申请加入导师研究组
- 支持接受/拒绝/取消操作
- 自动建立 mentor_id 关联

---

## 👨‍🏫 二、导师功能模块

### 2.1 学生管理 (`/mentor/students`)
| 功能 | 描述 |
|------|------|
| 学生列表 | 显示所有关联学生，支持分页 |
| 活跃度追踪 | 7天活跃度统计、最后活动时间 |
| 研究数据 | 论文数、知识库数、笔记数 |
| 邀请学生 | 通过邮箱邀请新学生 |
| 移除学生 | 解除导师-学生关系 |

### 2.2 研究组管理 (`/mentor/groups`)
| 功能 | 描述 |
|------|------|
| 创建研究组 | 设置名称、描述、最大成员数 |
| 成员管理 | 查看/移除组成员 |
| 邀请记录 | 待处理申请、已发送邀请 |
| 组信息编辑 | 修改组名称、描述 |

### 2.3 公告管理 (`/mentor/announcements`)
| 功能 | 描述 |
|------|------|
| 发布公告 | 支持标题、内容、置顶 |
| 定向发布 | 可选择发给特定研究组或全体学生 |
| 阅读统计 | 查看已读/未读人数 |
| 编辑/删除 | 管理已发布公告 |

---

## 👨‍🎓 三、学生功能模块

### 3.1 我的导师 (`/student/mentor`)
| 功能 | 描述 |
|------|------|
| 导师信息 | 显示当前导师头像、姓名、研究方向 |
| 研究组列表 | 显示已加入的研究组 |
| 申请导师 | 搜索并申请成为导师的学生 |
| 申请状态 | 查看待处理的申请 |

### 3.2 公告通知 (`/student/announcements`)
| 功能 | 描述 |
|------|------|
| 公告列表 | 显示导师发布的公告 |
| 未读标记 | 高亮显示未读公告 |
| 自动已读 | 打开详情自动标记已读 |

---

## 🔗 四、资源共享系统 (Session 12)

### 4.1 共享资源页面 (`/mentor/shares` & `/student/shared`)

#### 支持的资源类型
| 类型 | 图标 | 说明 |
|------|------|------|
| 论文 (paper) | 📄 | 单篇论文共享 |
| 文献集 (paper_collection) | 📁 | 整个文献集共享 |
| 知识库 (knowledge_base) | 📚 | 知识库共享 |
| 笔记本 (notebook) | ✏️ | 笔记本共享 |

#### 共享对象
| 类型 | 描述 | 权限要求 |
|------|------|----------|
| 研究组 | 共享给特定研究组的所有成员 | 组成员 |
| 所有学生 | 共享给导师的全体学生 | 仅导师 |

### 4.2 核心功能

#### 共享给我
- 📋 **资源列表** - 分类显示共享给我的资源
- 🔍 **筛选功能** - 按资源类型筛选
- 👁️ **详情查看** - 论文摘要、作者、引用数
- 📥 **添加到库** - 将共享论文复制到自己的文献库

#### 我的共享
- 📤 **共享资源** - 选择资源 → 选择对象 → 确认共享
- ✅ **批量共享** - 同时选择多个资源共享
- ❌ **取消共享** - 撤回已共享的资源

### 4.3 知识库共享 (Session 14+)

**引用/快捷方式模式** - 不复制数据，只建立引用关系

| 特性 | 说明 |
|------|------|
| **引用模式** | 共享时不复制数据，接收者通过引用访问原知识库 |
| **实时同步** | 原作者更新内容后，接收者立即可见 |
| **知识库列表显示** | 共享的知识库会显示在用户的知识库列表中（标记"共享"）|
| **AI对话可选** | 在AI对话的知识库选择器中可以选择共享的知识库 |
| **向后兼容** | 默认行为不变，通过 `include_shared` 参数开启 |

**用户体验**：
- 导师共享知识库 → 学生在知识库列表看到（标记为"共享"）
- 学生选择共享知识库 → AI对话中即可使用
- 导师更新知识库 → 学生自动看到最新内容

### 4.4 API 端点
```
# 资源共享
GET  /api/share/shared-with-me      # 获取共享给我的资源
GET  /api/share/shared-with-me/count # 获取各类型数量统计
GET  /api/share/my-shares           # 获取我共享出去的资源
GET  /api/share/my-groups           # 获取可共享的研究组
GET  /api/share/my-papers           # 获取我的论文列表
GET  /api/share/my-collections      # 获取我的文献集列表
GET  /api/share/my-knowledge-bases  # 获取我的知识库列表
GET  /api/share/detail/{id}         # 获取共享资源详情
POST /api/share/                    # 共享资源
POST /api/share/batch               # 批量共享
POST /api/share/copy-to-library/{id} # 复制论文到我的库
POST /api/share/copy-collection-papers/{id} # 批量复制文献集论文
DELETE /api/share/{id}              # 取消共享

# 知识库共享
GET  /api/knowledge/available?include_shared=true  # 获取可用知识库
POST /api/knowledge/search?include_shared=true     # 搜索（含共享）
```

---

## 👤 五、用户管理

### 5.1 管理员用户管理 (`/admin/users`)
| 功能 | 描述 |
|------|------|
| 用户列表 | 分页显示所有用户 |
| 角色分配 | 设置用户角色 (admin/mentor/student) |
| 状态管理 | 启用/禁用用户 |
| 搜索筛选 | 按姓名、邮箱、角色筛选 |

### 5.2 个人资料 (`/profile`)
| 功能 | 描述 |
|------|------|
| 基本信息 | 姓名、邮箱、头像、简介 |
| 研究方向 | 设置研究领域标签 |
| 头像上传 | 支持 Base64 或 URL |

### 5.3 系统设置 (`/settings`)
| 功能 | 描述 |
|------|------|
| 密码修改 | 验证旧密码后修改 |
| 偏好设置 | 主题、语言等个性化配置 |

---

## 🎨 六、2026 设计系统 (Session 12)

### 6.1 设计语言
| 元素 | 规格 |
|------|------|
| **字体** | Inter, -apple-system |
| **主色** | HSL(215, 85%, 55%) 冷调蓝 |
| **强调色** | HSL(160, 75%, 45%) 翡翠绿 |
| **圆角** | 8/12/16px (sm/md/lg) |
| **阴影** | 多层柔和 + 发光层 |

### 6.2 交互效果
- **微发光边框** - 悬停时边框发出柔和光晕
- **内容抬升** - 悬停时 `translateY(-2px)` + 阴影增强
- **噪点纹理** - 全局 SVG noise overlay
- **玻璃态** - `backdrop-filter: blur(20px)`

### 6.3 组件增强
- 表格操作按钮高可见度 (带背景+边框)
- 下拉菜单玻璃态效果
- 分页渐变高亮
- 标签页流光指示器

---

## 📁 七、文件清单

### 后端 (19 文件)
```
backend/
├── alembic/versions/
│   └── 006_multi_role.py          # 数据库迁移
├── app/
│   ├── api/
│   │   ├── admin.py               # 管理员 API
│   │   ├── mentor.py              # 导师 API
│   │   ├── student.py             # 学生 API
│   │   ├── share.py               # 共享 API
│   │   ├── invitations.py         # 邀请 API
│   │   ├── announcements.py       # 公告 API
│   │   ├── literature.py          # 文献 API (修改)
│   │   └── knowledge.py           # 知识库 API (修改，支持共享)
│   ├── services/
│   │   └── agent_tools.py         # Agent工具 (修改，支持共享知识库搜索)
│   ├── core/
│   │   └── permissions.py         # 权限控制
│   ├── models/
│   │   ├── role.py                # 角色模型
│   │   ├── user.py                # 用户模型 (修改)
│   │   └── __init__.py            # 模型导出
│   ├── schemas/
│   │   ├── role.py                # 角色 Schema
│   │   └── user.py                # 用户 Schema (修改)
│   └── main.py                    # 主入口 (修改)
└── scripts/
    ├── create_admin.py            # 创建管理员脚本
    └── cleanup_*.py/sql           # 清理脚本
```

### 前端 (35 文件)
```
frontend/src/
├── main.tsx                       # 入口 (修改)
├── App.tsx                        # 路由 (修改)
├── styles/
│   └── design-system.css          # 设计系统
├── services/
│   └── api.ts                     # API 服务 (修改)
├── stores/
│   ├── authStore.ts               # 认证 Store (修改)
│   ├── roleStore.ts               # 角色 Store
│   ├── knowledgeStore.ts          # 知识库 Store (修改，支持共享搜索)
│   └── literatureStore.ts         # 文献 Store (修改)
├── components/
│   ├── layout/
│   │   └── MainLayout.tsx         # 布局 (修改)
│   └── team/
│       ├── MentorCard.tsx         # 导师卡片
│       ├── MentorDashboard.tsx    # 导师面板
│       └── StudentStatusPanel.tsx # 学生状态
└── pages/
    ├── admin/
    │   ├── UsersPage.tsx          # 用户管理
    │   └── index.ts
    ├── mentor/
    │   ├── StudentsPage.tsx       # 学生管理
    │   ├── GroupsPage.tsx         # 研究组管理
    │   ├── AnnouncementsPage.tsx  # 公告管理
    │   └── index.ts
    ├── student/
    │   ├── MentorPage.tsx         # 我的导师
    │   ├── AnnouncementsPage.tsx  # 公告通知
    │   └── index.ts
    ├── shared/
    │   ├── SharedResourcesPage.tsx    # 资源共享列表
    │   ├── SharedResourceViewPage.tsx # 共享资源详情
    │   └── index.ts
    ├── knowledge/
    │   └── KnowledgePage.tsx      # 知识库页面 (修改，显示共享知识库)
    ├── literature/
    │   └── PaperDetailPanel.tsx   # 论文详情面板
    ├── user/
    │   ├── ProfilePage.tsx        # 个人资料
    │   ├── SettingsPage.tsx       # 系统设置
    │   └── index.ts
    └── dashboard/
        └── DashboardPage.tsx      # 仪表盘 (修改)
```

---

## 🚀 部署说明

```bash
# 1. 解压补丁
unzip multi-role-patch.zip

# 2. 复制后端文件
cp -r patch/backend/* backend/

# 3. 复制前端文件
cp -r patch/frontend/* frontend/

# 4. 运行数据库迁移
cd backend
alembic upgrade head

# 5. 创建管理员账号
python scripts/create_admin.py

# 6. 重启服务
docker-compose restart
```

---

## ✅ 功能验证清单

- [ ] 管理员登录 → 用户管理页面
- [ ] 导师登录 → 学生管理、研究组、公告、资源共享
- [ ] 学生登录 → 我的导师、公告、共享资源
- [ ] 导师邀请学生 → 学生收到邀请
- [ ] 学生申请导师 → 导师收到申请
- [ ] 导师发布公告 → 学生收到通知
- [ ] 导师共享论文 → 学生可见并可添加到库
- [ ] 批量共享功能正常
- [ ] 复制论文到我的库功能正常
- [ ] 2026 设计风格一致

---

**总计新增功能**: 32项核心功能  
**涉及 API 端点**: 42+  
**新增/修改文件**: 54个
