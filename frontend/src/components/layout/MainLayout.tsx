import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Input, Avatar, Dropdown, Button, Tooltip } from 'antd'
import {
  HomeOutlined,
  MessageOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  CodeOutlined,
  GlobalOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SearchOutlined,
  UserOutlined,
  SettingOutlined,
  LogoutOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'

const { Sider, Header, Content } = Layout

const MainLayout = () => {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const { user, logout } = useAuthStore()
  const { conversations, fetchConversations, createConversation } = useChatStore()
  
  useEffect(() => {
    fetchConversations()
  }, [fetchConversations])
  
  const menuItems = [
    {
      key: '/dashboard',
      icon: <HomeOutlined />,
      label: '工作台',
    },
    {
      key: '/chat',
      icon: <MessageOutlined />,
      label: 'AI 对话',
    },
    {
      key: '/knowledge',
      icon: <DatabaseOutlined />,
      label: '知识库',
      disabled: true,
    },
    {
      key: '/papers',
      icon: <FileTextOutlined />,
      label: '文献管理',
      disabled: true,
    },
    {
      key: '/code',
      icon: <CodeOutlined />,
      label: '代码实验室',
      disabled: true,
    },
    {
      key: '/feed',
      icon: <GlobalOutlined />,
      label: '资讯追踪',
      disabled: true,
    },
  ]
  
  const userMenuItems = [
    {
      key: 'profile',
      icon: <UserOutlined />,
      label: '个人资料',
    },
    {
      key: 'settings',
      icon: <SettingOutlined />,
      label: '设置',
    },
    {
      type: 'divider' as const,
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      danger: true,
    },
  ]
  
  const handleMenuClick = ({ key }: { key: string }) => {
    navigate(key)
  }
  
  const handleUserMenuClick = ({ key }: { key: string }) => {
    if (key === 'logout') {
      logout()
      navigate('/login')
    }
  }
  
  const handleNewChat = async () => {
    const conversation = await createConversation()
    navigate(`/chat/${conversation.id}`)
  }
  
  return (
    <Layout className="h-screen">
      {/* 侧边栏 */}
      <Sider
        trigger={null}
        collapsible
        collapsed={collapsed}
        width={260}
        collapsedWidth={72}
        className="relative"
      >
        {/* Logo */}
        <div className="h-16 flex items-center justify-center border-b border-white/5">
          <motion.div
            className="flex items-center gap-3"
            initial={false}
            animate={{ opacity: 1 }}
          >
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center shadow-lg shadow-blue-500/25">
              <span className="text-white font-bold text-sm">AI</span>
            </div>
            <AnimatePresence>
              {!collapsed && (
                <motion.span
                  initial={{ opacity: 0, width: 0 }}
                  animate={{ opacity: 1, width: 'auto' }}
                  exit={{ opacity: 0, width: 0 }}
                  className="text-white font-semibold text-lg whitespace-nowrap overflow-hidden"
                >
                  科研助手
                </motion.span>
              )}
            </AnimatePresence>
          </motion.div>
        </div>
        
        {/* 新建对话按钮 */}
        <div className="p-3">
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleNewChat}
            className="w-full"
            size={collapsed ? 'middle' : 'large'}
          >
            {!collapsed && '新建对话'}
          </Button>
        </div>
        
        {/* 主菜单 */}
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={handleMenuClick}
          className="border-none"
        />
        
        {/* 最近对话列表 */}
        {!collapsed && conversations.length > 0 && (
          <div className="absolute bottom-20 left-0 right-0 px-3">
            <div className="text-xs text-gray-500 mb-2 px-2">最近对话</div>
            <div className="space-y-1 max-h-40 overflow-y-auto">
              {conversations.slice(0, 5).map((conv) => (
                <div
                  key={conv.id}
                  onClick={() => navigate(`/chat/${conv.id}`)}
                  className="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer transition-colors truncate text-sm text-gray-400 hover:text-gray-200"
                >
                  {conv.title}
                </div>
              ))}
            </div>
          </div>
        )}
        
        {/* 折叠按钮 */}
        <div className="absolute bottom-0 left-0 right-0 p-3 border-t border-white/5">
          <Button
            type="text"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed(!collapsed)}
            className="w-full text-gray-400 hover:text-white"
          />
        </div>
      </Sider>
      
      {/* 主内容区 */}
      <Layout>
        {/* 顶部栏 */}
        <Header className="h-16 px-6 flex items-center justify-between bg-transparent border-b border-white/5">
          {/* 全局搜索 */}
          <div className="flex-1 max-w-xl">
            <Input
              prefix={<SearchOutlined className="text-gray-500" />}
              placeholder="搜索文献、知识库、对话..."
              className="rounded-full"
              size="large"
            />
          </div>
          
          {/* 用户菜单 */}
          <div className="flex items-center gap-4">
            <Tooltip title={`当前模型: DeepSeek`}>
              <div className="px-3 py-1 rounded-full bg-blue-500/10 text-blue-400 text-sm flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
                DeepSeek
              </div>
            </Tooltip>
            
            <Dropdown
              menu={{
                items: userMenuItems,
                onClick: handleUserMenuClick,
              }}
              placement="bottomRight"
            >
              <div className="flex items-center gap-2 cursor-pointer hover:opacity-80 transition-opacity">
                <Avatar
                  size={36}
                  icon={<UserOutlined />}
                  className="bg-gradient-to-br from-blue-500 to-purple-500"
                />
                <span className="text-gray-300 hidden md:inline">
                  {user?.username}
                </span>
              </div>
            </Dropdown>
          </div>
        </Header>
        
        {/* 内容区 */}
        <Content className="overflow-hidden">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}

export default MainLayout
