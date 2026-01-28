/**
 * 路由守卫组件
 * 用于角色权限控制和认证保护
 */
import { Navigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'
import { UserRole } from '@/services/api'
import { Result, Button, Spin } from 'antd'
import { LockOutlined, LoadingOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'

interface RouteGuardProps {
  children: React.ReactNode
}

interface RoleGuardProps extends RouteGuardProps {
  allowedRoles: UserRole[]
  showForbidden?: boolean  // 是否显示403页面，否则重定向
  redirectTo?: string
}

// 私有路由：需要登录
export const PrivateRoute = ({ children }: RouteGuardProps) => {
  const { isAuthenticated, isInitialized, isLoading } = useAuthStore()
  const location = useLocation()

  // 等待初始化完成
  if (!isInitialized || isLoading) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="text-center"
        >
          <Spin 
            indicator={<LoadingOutlined className="text-4xl text-emerald-500" spin />} 
          />
          <p className="mt-4 text-slate-400">正在加载...</p>
        </motion.div>
      </div>
    )
  }

  if (!isAuthenticated) {
    // 保存当前路径，登录后重定向回来
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  return <>{children}</>
}

// 公开路由：已登录则重定向
export const PublicRoute = ({ children }: RouteGuardProps) => {
  const { isAuthenticated, isInitialized } = useAuthStore()
  const location = useLocation()

  // 等待初始化完成
  if (!isInitialized) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <Spin indicator={<LoadingOutlined className="text-4xl text-emerald-500" spin />} />
      </div>
    )
  }

  if (isAuthenticated) {
    // 如果有保存的重定向路径，跳转到那里
    const from = (location.state as { from?: Location })?.from?.pathname || '/dashboard'
    return <Navigate to={from} replace />
  }

  return <>{children}</>
}

// 角色守卫：需要特定角色
export const RoleGuard = ({ 
  children, 
  allowedRoles, 
  showForbidden = true,
  redirectTo = '/dashboard'
}: RoleGuardProps) => {
  const { user, isAuthenticated, isInitialized, hasRole } = useAuthStore()
  const location = useLocation()

  // 等待初始化完成
  if (!isInitialized) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <Spin indicator={<LoadingOutlined className="text-4xl text-emerald-500" spin />} />
      </div>
    )
  }

  // 未登录，重定向到登录页
  if (!isAuthenticated || !user) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  // 检查角色权限
  if (!hasRole(allowedRoles)) {
    if (showForbidden) {
      return (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          className="min-h-screen bg-slate-900 flex items-center justify-center p-4"
        >
          <Result
            icon={
              <div className="w-24 h-24 mx-auto rounded-full bg-red-500/20 flex items-center justify-center mb-6">
                <LockOutlined className="text-4xl text-red-400" />
              </div>
            }
            title={
              <span className="text-2xl font-semibold text-white">
                访问受限
              </span>
            }
            subTitle={
              <span className="text-slate-400">
                抱歉，您没有权限访问此页面。请联系管理员获取相应权限。
              </span>
            }
            extra={
              <Button 
                type="primary"
                onClick={() => window.history.back()}
                className="bg-emerald-600 hover:bg-emerald-700 border-none"
              >
                返回上一页
              </Button>
            }
            className="max-w-md"
            style={{ 
              background: 'rgba(30, 41, 59, 0.5)', 
              borderRadius: '1rem',
              padding: '2rem',
              border: '1px solid rgba(100, 116, 139, 0.2)'
            }}
          />
        </motion.div>
      )
    }
    return <Navigate to={redirectTo} replace />
  }

  return <>{children}</>
}

// 导师路由守卫
export const MentorRoute = ({ children }: RouteGuardProps) => (
  <RoleGuard allowedRoles={[UserRole.MENTOR, UserRole.ADMIN]}>
    {children}
  </RoleGuard>
)

// 学生路由守卫
export const StudentRoute = ({ children }: RouteGuardProps) => (
  <RoleGuard allowedRoles={[UserRole.STUDENT]}>
    {children}
  </RoleGuard>
)

// 管理员路由守卫
export const AdminRoute = ({ children }: RouteGuardProps) => (
  <RoleGuard allowedRoles={[UserRole.ADMIN]}>
    {children}
  </RoleGuard>
)
