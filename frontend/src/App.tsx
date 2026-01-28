import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { Spin } from 'antd'
import { useAuthStore } from '@/stores/authStore'
import MainLayout from '@/components/layout/MainLayout'
import LoginPage from '@/pages/auth/LoginPage'
import RegisterPage from '@/pages/auth/RegisterPage'
import DashboardPage from '@/pages/dashboard/DashboardPage'
import ChatPage from '@/pages/chat/ChatPage'
import KnowledgePage from '@/pages/knowledge/KnowledgePage'
import { LiteraturePage } from '@/pages/literature'
import { CodeLabPage } from '@/pages/codelab'

// 角色相关页面 - 懒加载
import { UsersPage as AdminUsersPage } from '@/pages/admin'
import { StudentsPage as MentorStudentsPage, GroupsPage as MentorGroupsPage } from '@/pages/mentor'
import { MentorPage as StudentMentorPage } from '@/pages/student'

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

function App() {
  const { checkAuth, isInitialized } = useAuthStore()
  
  // 应用启动时验证认证状态
  useEffect(() => {
    if (!isInitialized) {
      checkAuth()
    }
  }, [checkAuth, isInitialized])
  
  return (
    <BrowserRouter>
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
              <MainLayout />
            </PrivateRoute>
          }
        >
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<DashboardPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="chat/:conversationId" element={<ChatPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="knowledge/:kbId" element={<KnowledgePage />} />
          <Route path="literature" element={<LiteraturePage />} />
          <Route path="code" element={<CodeLabPage />} />
          <Route path="code/:notebookId" element={<CodeLabPage />} />
          
          {/* ========== 管理员路由 ========== */}
          <Route 
            path="admin/users" 
            element={
              <RoleRoute allowedRoles={['admin']}>
                <AdminUsersPage />
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
                <MentorStudentsPage />
              </RoleRoute>
            } 
          />
          <Route 
            path="mentor/groups" 
            element={
              <RoleRoute allowedRoles={['mentor']}>
                <MentorGroupsPage />
              </RoleRoute>
            } 
          />
          <Route 
            path="mentor/announcements" 
            element={
              <RoleRoute allowedRoles={['mentor']}>
                <PlaceholderPage title="公告管理" />
              </RoleRoute>
            } 
          />
          <Route 
            path="mentor/shares" 
            element={
              <RoleRoute allowedRoles={['mentor']}>
                <PlaceholderPage title="资源共享" />
              </RoleRoute>
            } 
          />
          
          {/* ========== 学生路由 ========== */}
          <Route 
            path="student/mentor" 
            element={
              <RoleRoute allowedRoles={['student']}>
                <StudentMentorPage />
              </RoleRoute>
            } 
          />
          <Route 
            path="student/shared" 
            element={
              <RoleRoute allowedRoles={['student']}>
                <PlaceholderPage title="共享资源" />
              </RoleRoute>
            } 
          />
          <Route 
            path="student/announcements" 
            element={
              <RoleRoute allowedRoles={['student']}>
                <PlaceholderPage title="公告通知" />
              </RoleRoute>
            } 
          />
          
          {/* 个人设置页面 */}
          <Route path="profile" element={<PlaceholderPage title="个人资料" />} />
          <Route path="settings" element={<PlaceholderPage title="设置" />} />
        </Route>
        
        {/* 404 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
