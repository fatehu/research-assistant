import { useState, useEffect } from 'react'
import { Outlet, useNavigate, useLocation, useParams } from 'react-router-dom'
import { Layout, Menu, Input, Avatar, Dropdown, Button, Tooltip, Modal, Empty, Badge } from 'antd'
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
  DeleteOutlined,
  MoreOutlined,
  HistoryOutlined,
  InboxOutlined,
  DownOutlined,
  RightOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import type { Conversation } from '@/services/api'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

const { Sider, Header, Content } = Layout

// 对话列表项组件
const ConversationItem = ({ 
  conv, 
  isActive,
  onClick,
  onDelete 
}: { 
  conv: Conversation
  isActive: boolean
  onClick: () => void
  onDelete: () => void
}) => {
  const [showActions, setShowActions] = useState(false)
  
  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -10 }}
      className={`group relative flex items-center gap-3 px-3 py-2.5 rounded-xl cursor-pointer transition-all ${
        isActive
          ? 'bg-emerald-500/20 border border-emerald-500/30'
          : 'hover:bg-white/5 border border-transparent'
      }`}
      onClick={onClick}
      onMouseEnter={() => setShowActions(true)}
      onMouseLeave={() => setShowActions(false)}
    >
      {/* 图标 */}
      <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 ${
        isActive ? 'bg-emerald-500/30' : 'bg-slate-700/50'
      }`}>
        <MessageOutlined className={isActive ? 'text-emerald-400' : 'text-slate-400'} />
      </div>
      
      {/* 标题和时间 */}
      <div className="flex-1 min-w-0">
        <div className={`text-sm truncate ${isActive ? 'text-white font-medium' : 'text-slate-300'}`}>
          {conv.title || '新对话'}
        </div>
        <div className="text-xs text-slate-500 mt-0.5">
          {dayjs(conv.updated_at).fromNow()}
        </div>
      </div>
      
      {/* 操作按钮 */}
      <AnimatePresence>
        {showActions && (
          <motion.div
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            className="absolute right-2"
          >
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'delete',
                    icon: <DeleteOutlined />,
                    label: '删除',
                    danger: true,
                    onClick: (e) => {
                      e.domEvent.stopPropagation()
                      onDelete()
                    },
                  },
                ],
              }}
              trigger={['click']}
            >
              <Button
                type="text"
                size="small"
                icon={<MoreOutlined />}
                onClick={(e) => e.stopPropagation()}
                className="text-slate-400 hover:text-white hover:bg-white/10"
              />
            </Dropdown>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// 可折叠的分组组件
const CollapsibleGroup = ({
  title,
  count,
  children,
  defaultExpanded = true,
}: {
  title: string
  count: number
  children: React.ReactNode
  defaultExpanded?: boolean
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded)

  return (
    <div className="mb-2">
      <div
        className="flex items-center gap-2 px-2 py-1.5 rounded-lg cursor-pointer hover:bg-white/5 transition-all"
        onClick={() => setExpanded(!expanded)}
      >
        <motion.div
          animate={{ rotate: expanded ? 0 : -90 }}
          transition={{ duration: 0.2 }}
        >
          <DownOutlined className="text-slate-500 text-xs" />
        </motion.div>
        <span className="text-xs text-slate-500 flex-1">{title}</span>
        <Badge 
          count={count} 
          size="small"
          style={{ 
            backgroundColor: 'rgba(71, 85, 105, 0.5)',
            color: 'rgba(148, 163, 184, 1)',
            fontSize: '10px',
            boxShadow: 'none',
          }}
        />
      </div>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="space-y-1 mt-1">
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

const MainLayout = () => {
  const navigate = useNavigate()
  const location = useLocation()
  const { conversationId } = useParams()
  const [collapsed, setCollapsed] = useState(false)
  const [deleteModalVisible, setDeleteModalVisible] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null)
  const [historyExpanded, setHistoryExpanded] = useState(true)
  
  const { user, logout } = useAuthStore()
  const { conversations, fetchConversations, deleteConversation } = useChatStore()
  
  useEffect(() => {
    fetchConversations()
  }, [])
  
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
    { type: 'divider' as const },
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
  
  const handleNewChat = () => {
    // 不立即创建对话，只跳转到聊天页面
    // 对话会在发送第一条消息时创建
    navigate('/chat')
  }
  
  const handleDeleteConversation = async () => {
    if (deleteTarget) {
      await deleteConversation(deleteTarget)
      setDeleteModalVisible(false)
      setDeleteTarget(null)
      // 如果删除的是当前对话，跳转到聊天首页
      if (conversationId && parseInt(conversationId) === deleteTarget) {
        navigate('/chat')
      }
    }
  }
  
  const showDeleteModal = (id: number) => {
    setDeleteTarget(id)
    setDeleteModalVisible(true)
  }
  
  // 按日期分组对话
  const groupedConversations = conversations.reduce((groups, conv) => {
    const date = dayjs(conv.updated_at)
    const today = dayjs().startOf('day')
    const yesterday = today.subtract(1, 'day')
    const weekAgo = today.subtract(7, 'day')
    
    let group: string
    if (date.isAfter(today)) {
      group = '今天'
    } else if (date.isAfter(yesterday)) {
      group = '昨天'
    } else if (date.isAfter(weekAgo)) {
      group = '最近 7 天'
    } else {
      group = '更早'
    }
    
    if (!groups[group]) {
      groups[group] = []
    }
    groups[group].push(conv)
    return groups
  }, {} as Record<string, Conversation[]>)
  
  const groupOrder = ['今天', '昨天', '最近 7 天', '更早']
  
  return (
    <Layout className="h-screen">
      {/* 侧边栏 */}
      <Sider
        trigger={null}
        collapsible
        collapsed={collapsed}
        width={280}
        collapsedWidth={72}
        className="!flex !flex-col h-screen"
        style={{ background: 'linear-gradient(180deg, #0f172a 0%, #1e293b 100%)' }}
      >
        {/* Logo - 固定高度 */}
        <div className="h-16 flex items-center justify-center border-b border-white/5 px-4 flex-shrink-0">
          <motion.div className="flex items-center gap-3" initial={false}>
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-teal-600 flex items-center justify-center shadow-lg shadow-emerald-500/20">
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
        
        {/* 新建对话按钮 - 固定高度 */}
        <div className="p-3 flex-shrink-0">
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleNewChat}
            className="w-full bg-gradient-to-r from-emerald-500 to-teal-600 border-none hover:from-emerald-600 hover:to-teal-700 rounded-xl h-10"
            size={collapsed ? 'middle' : 'large'}
          >
            {!collapsed && '新建对话'}
          </Button>
        </div>
        
        {/* 主菜单 - 固定高度 */}
        <div className="flex-shrink-0">
          <Menu
            mode="inline"
            selectedKeys={[
              location.pathname.startsWith('/chat') ? '/chat' : 
              location.pathname.startsWith('/knowledge') ? '/knowledge' : 
              location.pathname
            ]}
            items={menuItems}
            onClick={handleMenuClick}
            className="border-none bg-transparent"
          />
        </div>
        
        {/* 历史对话区域 - 可滚动 */}
        {!collapsed && (
          <div className="flex-1 flex flex-col overflow-hidden px-3 mt-2">
            {/* 历史对话头部 */}
            <div 
              className="flex items-center gap-2 text-xs text-slate-500 mb-2 px-1 py-1 rounded-lg cursor-pointer hover:bg-white/5 transition-all flex-shrink-0"
              onClick={() => setHistoryExpanded(!historyExpanded)}
            >
              <motion.div
                animate={{ rotate: historyExpanded ? 0 : -90 }}
                transition={{ duration: 0.2 }}
              >
                <DownOutlined className="text-xs" />
              </motion.div>
              <HistoryOutlined />
              <span>历史对话</span>
              <Badge 
                count={conversations.length} 
                size="small"
                className="ml-auto"
                style={{ 
                  backgroundColor: 'rgba(71, 85, 105, 0.5)',
                  color: 'rgba(148, 163, 184, 1)',
                  boxShadow: 'none',
                }}
              />
            </div>
            
            {/* 对话列表 - 关键滚动区域 */}
            {historyExpanded && (
              <div 
                className="flex-1 overflow-y-auto pb-4"
                style={{
                  scrollbarWidth: 'thin',
                  scrollbarColor: 'rgba(100, 116, 139, 0.5) transparent',
                }}
              >
                {conversations.length === 0 ? (
                  <div className="flex flex-col items-center justify-center py-8 text-slate-500">
                    <InboxOutlined className="text-3xl mb-2 opacity-50" />
                    <span className="text-xs">暂无对话</span>
                  </div>
                ) : (
                  <div className="space-y-1">
                    {groupOrder.map((group) => {
                      const convs = groupedConversations[group]
                      if (!convs || convs.length === 0) return null
                      
                      return (
                        <CollapsibleGroup
                          key={group}
                          title={group}
                          count={convs.length}
                          defaultExpanded={group === '今天' || group === '昨天'}
                        >
                          {convs.map((conv) => (
                            <ConversationItem
                              key={conv.id}
                              conv={conv}
                              isActive={conversationId ? parseInt(conversationId) === conv.id : false}
                              onClick={() => navigate(`/chat/${conv.id}`)}
                              onDelete={() => showDeleteModal(conv.id)}
                            />
                          ))}
                        </CollapsibleGroup>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
        
        {/* 折叠按钮 - 固定在底部 */}
        <div className="p-3 border-t border-white/5 flex-shrink-0">
          <Button
            type="text"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed(!collapsed)}
            className="w-full text-slate-400 hover:text-white hover:bg-white/5"
          />
        </div>
      </Sider>
      
      {/* 主内容区 */}
      <Layout className="bg-slate-950">
        {/* 顶部栏 */}
        <Header className="h-14 px-6 flex items-center justify-between bg-slate-900/50 border-b border-white/5 backdrop-blur-xl">
          {/* 搜索 */}
          <div className="flex-1 max-w-md">
            <Input
              prefix={<SearchOutlined className="text-slate-500" />}
              placeholder="搜索..."
              className="bg-slate-800/50 border-slate-700/50 rounded-lg hover:border-slate-600"
            />
          </div>
          
          {/* 右侧 */}
          <div className="flex items-center gap-4">
            {/* 模型状态 */}
            <div className="hidden md:flex items-center gap-2 px-3 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-emerald-400 text-sm">DeepSeek</span>
            </div>
            
            {/* 用户菜单 */}
            <Dropdown
              menu={{ items: userMenuItems, onClick: handleUserMenuClick }}
              placement="bottomRight"
            >
              <div className="flex items-center gap-2 cursor-pointer hover:opacity-80 transition-opacity">
                <Avatar
                  size={36}
                  icon={<UserOutlined />}
                  className="bg-gradient-to-br from-blue-500 to-indigo-600"
                />
                <span className="text-slate-300 hidden md:inline">{user?.username}</span>
              </div>
            </Dropdown>
          </div>
        </Header>
        
        {/* 内容 */}
        <Content className="overflow-hidden">
          <Outlet />
        </Content>
      </Layout>
      
      {/* 删除确认弹窗 */}
      <Modal
        title="删除对话"
        open={deleteModalVisible}
        onOk={handleDeleteConversation}
        onCancel={() => setDeleteModalVisible(false)}
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        <p>确定要删除这个对话吗？此操作不可撤销。</p>
      </Modal>
    </Layout>
  )
}

export default MainLayout
