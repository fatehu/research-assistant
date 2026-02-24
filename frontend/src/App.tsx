import { Suspense, lazy, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Spin } from 'antd'
import { useAuthStore } from '@/stores/authStore'
import ErrorBoundary from '@/components/common/ErrorBoundary'
import MainLayout from '@/components/layout/MainLayout'
import LoginPage from '@/pages/auth/LoginPage'
import RegisterPage from '@/pages/auth/RegisterPage'
import DashboardPage from '@/pages/dashboard/DashboardPage'
import ChatPage from '@/pages/chat/ChatPage'
import KnowledgePage from '@/pages/knowledge/KnowledgePage'
const ChatManagePage = lazy(() => import('@/pages/chat/ChatManagePage'))
const SmartChunkingPage = lazy(() => import('@/pages/knowledge/SmartChunkingPage'))
const LiteraturePage = lazy(() => import('@/pages/literature').then((m) => ({ default: m.LiteraturePage })))
const PaperReaderPage = lazy(() => import('@/pages/literature').then((m) => ({ default: m.PaperReaderPage })))
const CodeLabPage = lazy(() => import('@/pages/codelab').then((m) => ({ default: m.CodeLabPage })))
const AdminUsersPage = lazy(() => import('@/pages/admin').then((m) => ({ default: m.UsersPage })))
const MentorStudentsPage = lazy(() => import('@/pages/mentor').then((m) => ({ default: m.StudentsPage })))
const MentorGroupsPage = lazy(() => import('@/pages/mentor').then((m) => ({ default: m.GroupsPage })))
const MentorAnnouncementsPage = lazy(() => import('@/pages/mentor').then((m) => ({ default: m.AnnouncementsPage })))
const StudentMentorPage = lazy(() => import('@/pages/student').then((m) => ({ default: m.MentorPage })))
const StudentAnnouncementsPage = lazy(() => import('@/pages/student').then((m) => ({ default: m.AnnouncementsPage })))
const ProfilePage = lazy(() => import('@/pages/user').then((m) => ({ default: m.ProfilePage })))
const SettingsPage = lazy(() => import('@/pages/user').then((m) => ({ default: m.SettingsPage })))
const SharedResourcesPage = lazy(() => import('@/pages/shared').then((m) => ({ default: m.SharedResourcesPage })))
const SharedResourceViewPage = lazy(() => import('@/pages/shared').then((m) => ({ default: m.SharedResourceViewPage })))

// 路由守卫组件
const PrivateRoute = ({ children }: { children: React.ReactNode }) => {
  const { isAuthenticated, isInitialized } = useAuthStore()

  // 等待初始化完成
  if (!isInitialized) {
    return (
      <div className="h-screen flex items-center justify-center bg-slate-900">
        <Spin size="large" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}

// 公共路由组件（已登录用户重定向到首页）
const PublicRoute = ({ children }: { children: React.ReactNode }) => {
  const { isAuthenticated, isInitialized } = useAuthStore()

  // 等待初始化完成
  if (!isInitialized) {
    return (
      <div className="h-screen flex items-center justify-center bg-slate-900">
        <Spin size="large" />
      </div>
    )
  }

  if (isAuthenticated) {
    return <Navigate to="/dashboard" replace />
  }

  return <>{children}</>
}

// 角色守卫组件
const RoleRoute = ({
  children,
  allowedRoles
}: {
  children: React.ReactNode
  allowedRoles: string[]
}) => {
  const { user, isAuthenticated, isInitialized } = useAuthStore()

  // 等待初始化完成
  if (!isInitialized) {
    return (
      <div className="h-screen flex items-center justify-center bg-slate-900">
        <Spin size="large" />
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }

  // 检查角色权限
  if (user && !allowedRoles.includes(user.role || 'student')) {
    return <Navigate to="/dashboard" replace />
  }

  return <>{children}</>
}

// 占位页面组件
const PlaceholderPage = ({ title }: { title: string }) => (
  <div className="h-full flex items-center justify-center bg-slate-950">
    <div className="text-center">
      <h1 className="text-2xl font-bold text-white mb-2">{title}</h1>
      <p className="text-slate-400">功能开发中...</p>
    </div>
  </div>
)

const RouteLoading = () => (
  <div className="h-full flex items-center justify-center bg-slate-950">
    <Spin size="large" />
  </div>
)

const withSuspense = (node: React.ReactNode) => (
  <Suspense fallback={<RouteLoading />}>{node}</Suspense>
)

function App() {
  const { checkAuth, isInitialized } = useAuthStore()

  // 应用启动时验证认证状态
  useEffect(() => {
    if (!isInitialized) {
      checkAuth()
    }
  }, [checkAuth, isInitialized])

  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Routes>
        {/* 公共路由 */}
        <Route
          path="/login"
          element={
            <PublicRoute>
              <LoginPage />
            </PublicRoute>
          }
        />
        <Route
          path="/register"
          element={
            <PublicRoute>
              <RegisterPage />
            </PublicRoute>
          }
        />

        {/* 私有路由 */}
        <Route
          path="/"
          element={
            <PrivateRoute>
              <ErrorBoundary>
                <MainLayout />
              </ErrorBoundary>
            </PrivateRoute>
          }
        >
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="chat/manage" element={withSuspense(<ChatManagePage />)} />
          <Route path="chat/:conversationId" element={<ChatPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="knowledge/:kbId" element={<KnowledgePage />} />
          <Route path="knowledge/:kbId/chunking" element={withSuspense(<SmartChunkingPage />)} />
          <Route path="knowledge/chunking" element={withSuspense(<SmartChunkingPage />)} />
          <Route path="literature" element={withSuspense(<LiteraturePage />)} />
          <Route path="literature/:paperId/read" element={withSuspense(<PaperReaderPage />)} />
          <Route path="code" element={withSuspense(<CodeLabPage />)} />
          <Route path="code/:notebookId" element={withSuspense(<CodeLabPage />)} />

          {/* ========== 管理员路由 ========== */}
          <Route
            path="admin/users"
            element={
              <RoleRoute allowedRoles={['admin']}>
                {withSuspense(<AdminUsersPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="admin/statistics"
            element={
              <RoleRoute allowedRoles={['admin']}>
                <PlaceholderPage title="系统统计" />
              </RoleRoute>
            }
          />

          {/* ========== 导师路由 ========== */}
          <Route
            path="mentor/students"
            element={
              <RoleRoute allowedRoles={['mentor']}>
                {withSuspense(<MentorStudentsPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="mentor/groups"
            element={
              <RoleRoute allowedRoles={['mentor']}>
                {withSuspense(<MentorGroupsPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="mentor/announcements"
            element={
              <RoleRoute allowedRoles={['mentor']}>
                {withSuspense(<MentorAnnouncementsPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="mentor/shares"
            element={
              <RoleRoute allowedRoles={['mentor']}>
                {withSuspense(<SharedResourcesPage />)}
              </RoleRoute>
            }
          />

          {/* ========== 学生路由 ========== */}
          <Route
            path="student/mentor"
            element={
              <RoleRoute allowedRoles={['student']}>
                {withSuspense(<StudentMentorPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="student/shared"
            element={
              <RoleRoute allowedRoles={['student']}>
                {withSuspense(<SharedResourcesPage />)}
              </RoleRoute>
            }
          />
          <Route
            path="student/announcements"
            element={
              <RoleRoute allowedRoles={['student']}>
                {withSuspense(<StudentAnnouncementsPage />)}
              </RoleRoute>
            }
          />

          {/* 个人设置页面 */}
          <Route path="profile" element={withSuspense(<ProfilePage />)} />
          <Route path="settings" element={withSuspense(<SettingsPage />)} />

          {/* 共享资源详情页 - 所有已登录用户可访问 */}
          <Route path="shared/view/:shareId" element={withSuspense(<SharedResourceViewPage />)} />
        </Route>

        {/* 404 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
