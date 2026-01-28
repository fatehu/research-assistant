/**
 * 用户角色和师生关系类型定义
 */

// ==================== 用户角色 ====================

export enum UserRole {
  ADMIN = 'admin',
  MENTOR = 'mentor',
  STUDENT = 'student',
}

export const UserRoleLabels: Record<UserRole, string> = {
  [UserRole.ADMIN]: '管理员',
  [UserRole.MENTOR]: '导师',
  [UserRole.STUDENT]: '学生',
}

export const UserRoleColors: Record<UserRole, { bg: string; text: string; border: string }> = {
  [UserRole.ADMIN]: {
    bg: 'bg-rose-500/20',
    text: 'text-rose-400',
    border: 'border-rose-500/30',
  },
  [UserRole.MENTOR]: {
    bg: 'bg-violet-500/20',
    text: 'text-violet-400',
    border: 'border-violet-500/30',
  },
  [UserRole.STUDENT]: {
    bg: 'bg-blue-500/20',
    text: 'text-blue-400',
    border: 'border-blue-500/30',
  },
}

// ==================== 师生关系状态 ====================

export enum MentorshipStatus {
  PENDING = 'pending',
  ACTIVE = 'active',
  ARCHIVED = 'archived',
}

export const MentorshipStatusLabels: Record<MentorshipStatus, string> = {
  [MentorshipStatus.PENDING]: '待审批',
  [MentorshipStatus.ACTIVE]: '已关联',
  [MentorshipStatus.ARCHIVED]: '已归档',
}

export const MentorshipStatusColors: Record<MentorshipStatus, { bg: string; text: string; border: string; dot: string }> = {
  [MentorshipStatus.PENDING]: {
    bg: 'bg-amber-500/20',
    text: 'text-amber-400',
    border: 'border-amber-500/30',
    dot: 'bg-amber-400',
  },
  [MentorshipStatus.ACTIVE]: {
    bg: 'bg-emerald-500/20',
    text: 'text-emerald-400',
    border: 'border-emerald-500/30',
    dot: 'bg-emerald-400',
  },
  [MentorshipStatus.ARCHIVED]: {
    bg: 'bg-slate-500/20',
    text: 'text-slate-400',
    border: 'border-slate-500/30',
    dot: 'bg-slate-400',
  },
}

// ==================== 用户相关接口 ====================

export interface UserProfile {
  title?: string          // 职称/头衔
  department?: string     // 院系/部门
  research_area?: string  // 研究方向
  bio?: string            // 个人简介
  contact?: string        // 联系方式
  office?: string         // 办公室
  website?: string        // 个人主页
  [key: string]: unknown  // 其他自定义字段
}

export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  is_active: boolean
  role: UserRole
  profile_data?: UserProfile
  preferred_llm_provider: string
  preferences: Record<string, unknown>
  created_at: string
  last_login?: string
}

export interface UserBrief {
  id: number
  username: string
  full_name?: string
  avatar?: string
  role: UserRole
  profile_data?: UserProfile
}

// ==================== 师生关系接口 ====================

export interface Mentorship {
  id: number
  mentor_id: number
  student_id: number
  status: MentorshipStatus
  request_message?: string
  response_message?: string
  metadata?: Record<string, unknown>
  created_at: string
  updated_at: string
  approved_at?: string
  archived_at?: string
  mentor?: UserBrief
  student?: UserBrief
}

export interface MentorshipListResponse {
  items: Mentorship[]
  total: number
}

export interface MentorshipCreateRequest {
  mentor_id: number
  request_message?: string
  metadata?: Record<string, unknown>
}

export interface MentorshipUpdateRequest {
  status: MentorshipStatus
  response_message?: string
}

// ==================== 类型守卫函数 ====================

export const isAdmin = (user: User | null): boolean => {
  return user?.role === UserRole.ADMIN
}

export const isMentor = (user: User | null): boolean => {
  return user?.role === UserRole.MENTOR || user?.role === UserRole.ADMIN
}

export const isStudent = (user: User | null): boolean => {
  return user?.role === UserRole.STUDENT
}

export const hasRole = (user: User | null, roles: UserRole[]): boolean => {
  if (!user) return false
  return roles.includes(user.role)
}

// ==================== 学生活动类型 ====================

export interface StudentActivity {
  id: string
  type: 'conversation' | 'notebook' | 'knowledge' | 'literature' | 'codelab'
  title: string
  description?: string
  timestamp: string
  student: UserBrief
}

export const ActivityTypeLabels: Record<StudentActivity['type'], string> = {
  conversation: 'AI 对话',
  notebook: '笔记本',
  knowledge: '知识库',
  literature: '文献',
  codelab: '代码实验',
}

export const ActivityTypeIcons: Record<StudentActivity['type'], string> = {
  conversation: 'MessageOutlined',
  notebook: 'FileTextOutlined',
  knowledge: 'DatabaseOutlined',
  literature: 'BookOutlined',
  codelab: 'CodeOutlined',
}
