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
        </Route>
        
        {/* 404 */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
