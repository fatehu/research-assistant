/**
 * 通用 UI 组件集合
 * 包含状态徽章、角色标签、骨架屏、空状态等
 */
import { motion } from 'framer-motion'
import { UserRole, MentorshipStatus } from '@/services/api'
import {
  UserOutlined,
  CrownOutlined,
  TeamOutlined,
  BookOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  MinusCircleOutlined,
  InboxOutlined,
} from '@ant-design/icons'

// ==================== 状态徽章 ====================

interface StatusBadgeProps {
  status: MentorshipStatus
  size?: 'sm' | 'md' | 'lg'
  showDot?: boolean
}

const statusConfig: Record<MentorshipStatus, {
  label: string
  bg: string
  text: string
  border: string
  dot: string
  icon: React.ReactNode
}> = {
  [MentorshipStatus.PENDING]: {
    label: '待审批',
    bg: 'bg-amber-500/20',
    text: 'text-amber-400',
    border: 'border-amber-500/30',
    dot: 'bg-amber-400',
    icon: <ClockCircleOutlined />,
  },
  [MentorshipStatus.ACTIVE]: {
    label: '已关联',
    bg: 'bg-emerald-500/20',
    text: 'text-emerald-400',
    border: 'border-emerald-500/30',
    dot: 'bg-emerald-400',
    icon: <CheckCircleOutlined />,
  },
  [MentorshipStatus.ARCHIVED]: {
    label: '已归档',
    bg: 'bg-slate-500/20',
    text: 'text-slate-400',
    border: 'border-slate-500/30',
    dot: 'bg-slate-400',
    icon: <MinusCircleOutlined />,
  },
}

const sizeClasses = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-2.5 py-1 text-sm',
  lg: 'px-3 py-1.5 text-base',
}

export const StatusBadge = ({ status, size = 'md', showDot = true }: StatusBadgeProps) => {
  const config = statusConfig[status]
  
  return (
    <span
      className={`
        inline-flex items-center gap-1.5 rounded-full border font-medium
        ${config.bg} ${config.text} ${config.border} ${sizeClasses[size]}
      `}
    >
      {showDot && (
        <span className={`w-1.5 h-1.5 rounded-full ${config.dot} animate-pulse`} />
      )}
      {config.label}
    </span>
  )
}

// ==================== 角色徽章 ====================

interface RoleBadgeProps {
  role: UserRole
  size?: 'sm' | 'md' | 'lg'
  showIcon?: boolean
}

const roleConfig: Record<UserRole, {
  label: string
  bg: string
  text: string
  border: string
  icon: React.ReactNode
}> = {
  [UserRole.ADMIN]: {
    label: '管理员',
    bg: 'bg-rose-500/20',
    text: 'text-rose-400',
    border: 'border-rose-500/30',
    icon: <CrownOutlined />,
  },
  [UserRole.MENTOR]: {
    label: '导师',
    bg: 'bg-violet-500/20',
    text: 'text-violet-400',
    border: 'border-violet-500/30',
    icon: <TeamOutlined />,
  },
  [UserRole.STUDENT]: {
    label: '学生',
    bg: 'bg-blue-500/20',
    text: 'text-blue-400',
    border: 'border-blue-500/30',
    icon: <BookOutlined />,
  },
}

export const RoleBadge = ({ role, size = 'md', showIcon = true }: RoleBadgeProps) => {
  const config = roleConfig[role]
  
  return (
    <span
      className={`
        inline-flex items-center gap-1.5 rounded-full border font-medium
        ${config.bg} ${config.text} ${config.border} ${sizeClasses[size]}
      `}
    >
      {showIcon && config.icon}
      {config.label}
    </span>
  )
}

// ==================== 骨架屏 ====================

interface SkeletonProps {
  className?: string
}

export const CardSkeleton = ({ className = '' }: SkeletonProps) => (
  <div className={`animate-pulse ${className}`}>
    <div className="bg-slate-700/50 rounded-2xl p-6">
      <div className="flex items-center gap-4">
        <div className="w-16 h-16 bg-slate-600/50 rounded-xl" />
        <div className="flex-1 space-y-3">
          <div className="h-4 bg-slate-600/50 rounded w-1/3" />
          <div className="h-3 bg-slate-600/50 rounded w-1/2" />
          <div className="h-3 bg-slate-600/50 rounded w-2/3" />
        </div>
      </div>
    </div>
  </div>
)

export const ListSkeleton = ({ count = 3 }: { count?: number }) => (
  <div className="space-y-4">
    {Array.from({ length: count }).map((_, i) => (
      <div key={i} className="animate-pulse">
        <div className="flex items-center gap-4 p-4 bg-slate-800/50 rounded-xl">
          <div className="w-12 h-12 bg-slate-700/50 rounded-full" />
          <div className="flex-1 space-y-2">
            <div className="h-4 bg-slate-700/50 rounded w-1/4" />
            <div className="h-3 bg-slate-700/50 rounded w-1/2" />
          </div>
          <div className="w-20 h-8 bg-slate-700/50 rounded-lg" />
        </div>
      </div>
    ))}
  </div>
)

// ==================== 空状态 ====================

interface EmptyStateProps {
  icon?: React.ReactNode
  title: string
  description?: string
  action?: React.ReactNode
}

export const EmptyState = ({ icon, title, description, action }: EmptyStateProps) => (
  <motion.div
    initial={{ opacity: 0, y: 10 }}
    animate={{ opacity: 1, y: 0 }}
    className="flex flex-col items-center justify-center py-12 px-4"
  >
    <div className="w-20 h-20 rounded-full bg-slate-800/50 flex items-center justify-center mb-4">
      {icon || <InboxOutlined className="text-3xl text-slate-500" />}
    </div>
    <h3 className="text-lg font-medium text-slate-300 mb-2">{title}</h3>
    {description && (
      <p className="text-sm text-slate-500 text-center max-w-sm mb-4">{description}</p>
    )}
    {action}
  </motion.div>
)

// ==================== 用户头像 ====================

interface UserAvatarProps {
  user: {
    avatar?: string
    full_name?: string
    username: string
  }
  size?: 'sm' | 'md' | 'lg' | 'xl'
  className?: string
}

const avatarSizes = {
  sm: 'w-8 h-8 text-xs',
  md: 'w-10 h-10 text-sm',
  lg: 'w-14 h-14 text-lg',
  xl: 'w-20 h-20 text-2xl',
}

export const UserAvatar = ({ user, size = 'md', className = '' }: UserAvatarProps) => {
  const initials = (user.full_name || user.username)
    .split(' ')
    .map(n => n[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()

  if (user.avatar) {
    return (
      <img
        src={user.avatar}
        alt={user.full_name || user.username}
        className={`rounded-full object-cover ${avatarSizes[size]} ${className}`}
      />
    )
  }

  return (
    <div
      className={`
        rounded-full bg-gradient-to-br from-violet-500 to-purple-600
        flex items-center justify-center text-white font-medium
        ${avatarSizes[size]} ${className}
      `}
    >
      {initials || <UserOutlined />}
    </div>
  )
}

// ==================== 时间线 ====================

interface TimelineItem {
  status: 'completed' | 'current' | 'pending'
  label: string
  time?: string
}

interface TimelineProps {
  items: TimelineItem[]
}

export const Timeline = ({ items }: TimelineProps) => (
  <div className="flex items-center justify-between w-full">
    {items.map((item, index) => (
      <div key={index} className="flex items-center flex-1">
        <div className="flex flex-col items-center">
          <div
            className={`
              w-8 h-8 rounded-full flex items-center justify-center text-sm font-medium
              ${item.status === 'completed' 
                ? 'bg-emerald-500/30 text-emerald-400 border-2 border-emerald-500' 
                : item.status === 'current'
                  ? 'bg-amber-500/30 text-amber-400 border-2 border-amber-500 animate-pulse'
                  : 'bg-slate-700/50 text-slate-500 border-2 border-slate-600'
              }
            `}
          >
            {index + 1}
          </div>
          <span className={`mt-2 text-xs ${
            item.status === 'pending' ? 'text-slate-500' : 'text-slate-300'
          }`}>
            {item.label}
          </span>
          {item.time && (
            <span className="text-xs text-slate-500 mt-0.5">{item.time}</span>
          )}
        </div>
        {index < items.length - 1 && (
          <div className={`flex-1 h-0.5 mx-2 ${
            item.status === 'completed' ? 'bg-emerald-500/50' : 'bg-slate-700'
          }`} />
        )}
      </div>
    ))}
  </div>
)

// ==================== 统计卡片 ====================

interface StatCardProps {
  icon: React.ReactNode
  label: string
  value: number | string
  trend?: {
    value: number
    isUp: boolean
  }
  gradient: string
}

export const StatCard = ({ icon, label, value, trend, gradient }: StatCardProps) => (
  <motion.div
    whileHover={{ scale: 1.02 }}
    className={`
      relative overflow-hidden rounded-2xl p-6
      bg-gradient-to-br ${gradient}
      border border-white/10
    `}
  >
    <div className="flex items-start justify-between">
      <div>
        <p className="text-white/70 text-sm mb-1">{label}</p>
        <p className="text-3xl font-bold text-white">{value}</p>
        {trend && (
          <p className={`text-sm mt-2 ${trend.isUp ? 'text-emerald-300' : 'text-rose-300'}`}>
            {trend.isUp ? '↑' : '↓'} {Math.abs(trend.value)}%
          </p>
        )}
      </div>
      <div className="w-12 h-12 rounded-xl bg-white/10 flex items-center justify-center text-2xl text-white/80">
        {icon}
      </div>
    </div>
    {/* 装饰圆 */}
    <div className="absolute -right-4 -bottom-4 w-24 h-24 rounded-full bg-white/5" />
  </motion.div>
)
