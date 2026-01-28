import { useState, useEffect, useRef, useMemo } from 'react'
import { Outlet, useNavigate, useLocation, useParams } from 'react-router-dom'
import { Layout, Menu, Input, Avatar, Dropdown, Button, Tooltip, Modal, Empty, Badge, Divider } from 'antd'
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
  BookOutlined,
  LoadingOutlined,
  TeamOutlined,
  CrownOutlined,
  SolutionOutlined,
  UsergroupAddOutlined,
  NotificationOutlined,
  ShareAltOutlined,
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

// 角色标签组件
const RoleBadge = ({ role }: { role: string }) => {
  const roleConfig = {
    admin: { label: '管理员', color: 'bg-gradient-to-r from-amber-500 to-orange-500', icon: <CrownOutlined /> },
    mentor: { label: '导师', color: 'bg-gradient-to-r from-blue-500 to-indigo-500', icon: <SolutionOutlined /> },
    student: { label: '学生', color: 'bg-gradient-to-r from-emerald-500 to-teal-500', icon: <UserOutlined /> },
  }
  const config = roleConfig[role as keyof typeof roleConfig] || roleConfig.student
  
  return (
    <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium text-white ${config.color}`}>
      {config.icon}
      <span>{config.label}</span>
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
  
  // 搜索相关状态
  const [searchValue, setSearchValue] = useState('')
  const [searchResults, setSearchResults] = useState<Array<{
    message_id: number
    conversation_id: number
    conversation_title: string
    role: string
    content_snippet: string
    created_at: string
  }>>([])
  const [showSearchResults, setShowSearchResults] = useState(false)
  const [isSearching, setIsSearching] = useState(false)
  const searchInputRef = useRef<any>(null)
  const searchContainerRef = useRef<HTMLDivElement>(null)
  const searchTimeoutRef = useRef<NodeJS.Timeout | null>(null)
  
  const { user, logout } = useAuthStore()
  const { conversations, fetchConversations, deleteConversation } = useChatStore()
  
  useEffect(() => {
    fetchConversations()
  }, [])
  
  // 搜索消息（防抖）
  const handleSearch = async (value: string) => {
    setSearchValue(value)
    
    // 清除之前的定时器
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current)
    }
    
    if (!value.trim()) {
      setSearchResults([])
      setShowSearchResults(false)
      return
    }
    
    // 防抖：300ms后执行搜索
    searchTimeoutRef.current = setTimeout(async () => {
      setIsSearching(true)
      try {
        const { chatApi } = await import('@/services/api')
        const response = await chatApi.searchMessages(value.trim(), 20)
        setSearchResults(response.results)
        setShowSearchResults(true)
      } catch (error) {
        console.error('搜索失败:', error)
        setSearchResults([])
      } finally {
        setIsSearching(false)
      }
    }, 300)
  }
  
  // 选择搜索结果 - 跳转到对话并定位到消息
  const handleSelectSearchResult = (result: typeof searchResults[0]) => {
    navigate(`/chat/${result.conversation_id}`, { 
      state: { highlightMessageId: result.message_id } 
    })
    setSearchValue('')
    setSearchResults([])
    setShowSearchResults(false)
  }
  
  // 点击外部关闭搜索结果
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target as Node)) {
        setShowSearchResults(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])
  
  // 基础菜单项
  const baseMenuItems = [
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
      key: '/literature',
      icon: <BookOutlined />,
      label: '文献管理',
    },
    {
      key: '/code',
      icon: <CodeOutlined />,
      label: '代码实验室',
      disabled: false,
    },
    {
      key: '/feed',
      icon: <GlobalOutlined />,
      label: '资讯追踪',
      disabled: true,
    },
  ]

  // 角色专属菜单项
  const roleMenuItems = useMemo(() => {
    const userRole = user?.role || 'student'
    const items: any[] = []

    if (userRole === 'admin') {
      items.push({
        key: 'admin-divider',
        type: 'divider',
      })
      items.push({
        key: 'admin-group',
        label: '系统管理',
        type: 'group',
        children: [
          {
            key: '/admin/users',
            icon: <TeamOutlined />,
            label: '用户管理',
          },
          {
            key: '/admin/statistics',
            icon: <SettingOutlined />,
            label: '系统统计',
          },
        ],
      })
    }

    if (userRole === 'mentor') {
      items.push({
        key: 'mentor-divider',
        type: 'divider',
      })
      items.push({
        key: 'mentor-group',
        label: '导师中心',
        type: 'group',
        children: [
          {
            key: '/mentor/students',
            icon: <UsergroupAddOutlined />,
            label: '学生管理',
          },
          {
            key: '/mentor/groups',
            icon: <TeamOutlined />,
            label: '研究组',
          },
          {
            key: '/mentor/announcements',
            icon: <NotificationOutlined />,
            label: '公告管理',
          },
          {
            key: '/mentor/shares',
            icon: <ShareAltOutlined />,
            label: '资源共享',
          },
        ],
      })
    }

    if (userRole === 'student') {
      items.push({
        key: 'student-divider',
        type: 'divider',
      })
      items.push({
        key: 'student-group',
        label: '学习中心',
        type: 'group',
        children: [
          {
            key: '/student/mentor',
            icon: <SolutionOutlined />,
            label: '我的导师',
          },
          {
            key: '/student/shared',
            icon: <ShareAltOutlined />,
            label: '共享资源',
          },
          {
            key: '/student/announcements',
            icon: <NotificationOutlined />,
            label: '公告通知',
          },
        ],
      })
    }

    return items
  }, [user?.role])

  // 合并菜单项
  const menuItems = [...baseMenuItems, ...roleMenuItems]
  
  const userMenuItems = [
    {
      key: 'role',
      icon: <CrownOutlined />,
      label: <RoleBadge role={user?.role || 'student'} />,
      disabled: true,
    },
    { type: 'divider' as const },
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
    } else if (key === 'profile') {
      navigate('/profile')
    } else if (key === 'settings') {
      navigate('/settings')
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
  
  const showDeleteModal = (convId: number) => {
    setDeleteTarget(convId)
    setDeleteModalVisible(true)
  }
  
  // 按日期分组对话
  const groupedConversations = useMemo(() => {
    const groups: Record<string, Conversation[]> = {
      '今天': [],
      '昨天': [],
      '过去7天': [],
      '更早': [],
    }
    
    const now = dayjs()
    const today = now.startOf('day')
    const yesterday = today.subtract(1, 'day')
    const weekAgo = today.subtract(7, 'day')
    
    conversations.forEach((conv) => {
      const convDate = dayjs(conv.updated_at)
      if (convDate.isAfter(today)) {
        groups['今天'].push(conv)
      } else if (convDate.isAfter(yesterday)) {
        groups['昨天'].push(conv)
      } else if (convDate.isAfter(weekAgo)) {
        groups['过去7天'].push(conv)
      } else {
        groups['更早'].push(conv)
      }
    })
    
    return groups
  }, [conversations])
  
  const groupOrder = ['今天', '昨天', '过去7天', '更早']
  
  // 获取当前选中的菜单键
  const selectedKey = useMemo(() => {
    const path = location.pathname
    // 精确匹配
    if (menuItems.some(item => item.key === path)) {
      return path
    }
    // 检查子菜单
    for (const item of menuItems) {
      if ('children' in item && item.children) {
        const found = item.children.find((child: any) => child.key === path || path.startsWith(child.key + '/'))
        if (found) return found.key
      }
    }
    // 前缀匹配
    if (path.startsWith('/chat')) return '/chat'
    if (path.startsWith('/knowledge')) return '/knowledge'
    if (path.startsWith('/code')) return '/code'
    if (path.startsWith('/admin')) return '/admin/users'
    if (path.startsWith('/mentor')) return '/mentor/students'
    if (path.startsWith('/student')) return '/student/mentor'
    return '/dashboard'
  }, [location.pathname, menuItems])

  return (
    <Layout className="h-screen bg-slate-950">
      {/* 侧边栏 */}
      <Sider
        width={280}
        collapsedWidth={72}
        collapsed={collapsed}
        className="bg-slate-900/95 border-r border-white/5 flex flex-col"
        style={{ 
          height: '100vh',
          position: 'fixed',
          left: 0,
          top: 0,
          bottom: 0,
          zIndex: 100,
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Logo区域 - 固定 */}
        <div className="h-14 flex items-center justify-center px-4 border-b border-white/5 flex-shrink-0">
          {!collapsed ? (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex items-center gap-3"
            >
              <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center">
                <span className="text-white font-bold text-lg">R</span>
              </div>
              <span className="text-white font-semibold text-lg tracking-tight">研究助手</span>
            </motion.div>
          ) : (
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center">
              <span className="text-white font-bold text-lg">R</span>
            </div>
          )}
        </div>
        
        {/* 新对话按钮 - 固定 */}
        <div className="p-3 flex-shrink-0">
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleNewChat}
            className={`w-full h-10 bg-gradient-to-r from-emerald-500 to-cyan-500 border-0 rounded-xl font-medium ${
              collapsed ? 'px-0' : ''
            }`}
          >
            {!collapsed && '新对话'}
          </Button>
        </div>
        
        {/* 可滚动区域 */}
        <div className="flex-1 overflow-y-auto flex flex-col min-h-0">
          {/* 导航菜单 */}
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            onClick={handleMenuClick}
            items={menuItems}
            className="bg-transparent border-none px-2"
            style={{
              '--ant-menu-item-bg': 'transparent',
              '--ant-menu-item-active-bg': 'rgba(16, 185, 129, 0.1)',
              '--ant-menu-item-selected-bg': 'rgba(16, 185, 129, 0.15)',
              '--ant-menu-item-color': 'rgb(148, 163, 184)',
              '--ant-menu-item-hover-color': 'rgb(255, 255, 255)',
              '--ant-menu-item-selected-color': 'rgb(52, 211, 153)',
            } as React.CSSProperties}
          />
          {/* 菜单样式覆盖 */}
          <style>{`
            .ant-menu-item-group-title {
              color: rgb(100, 116, 139) !important;
              font-size: 11px !important;
              font-weight: 600 !important;
              text-transform: uppercase !important;
              letter-spacing: 0.05em !important;
              padding: 12px 16px 4px !important;
            }
            .ant-menu-item-group-list .ant-menu-item {
              color: rgb(203, 213, 225) !important;
            }
            .ant-menu-item-group-list .ant-menu-item:hover {
              color: rgb(255, 255, 255) !important;
              background-color: rgba(255, 255, 255, 0.05) !important;
            }
            .ant-menu-item-group-list .ant-menu-item-selected {
              color: rgb(52, 211, 153) !important;
              background-color: rgba(16, 185, 129, 0.15) !important;
            }
          `}</style>
          
          {/* 对话历史 - 仅在展开时显示 */}
          {!collapsed && (
            <div className="flex-1 flex flex-col min-h-0 mt-2 border-t border-white/5 pt-2">
              {/* 历史标题 */}
              <div 
                className="flex items-center gap-2 px-4 py-2 text-slate-400 text-sm cursor-pointer hover:text-slate-300 transition-colors flex-shrink-0"
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
        </div>
        
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
      <Layout className="bg-slate-950" style={{ marginLeft: collapsed ? 72 : 280, transition: 'margin-left 0.2s' }}>
        {/* 顶部栏 */}
        <Header className="h-14 px-6 flex items-center justify-between bg-slate-900/50 border-b border-white/5 backdrop-blur-xl" style={{ zIndex: 100 }}>
          {/* 搜索 */}
          <div className="flex-1 max-w-md relative" ref={searchContainerRef}>
            <Input
              ref={searchInputRef}
              prefix={isSearching ? <LoadingOutlined className="text-emerald-400" /> : <SearchOutlined className="text-slate-500" />}
              placeholder="搜索历史消息..."
              value={searchValue}
              onChange={(e) => handleSearch(e.target.value)}
              onFocus={() => searchValue && searchResults.length > 0 && setShowSearchResults(true)}
              className="bg-slate-800/50 border-slate-700/50 rounded-lg hover:border-slate-600"
              allowClear
            />
            {/* 搜索结果下拉 */}
            <AnimatePresence>
              {showSearchResults && searchResults.length > 0 && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="absolute top-full left-0 right-0 mt-2 bg-slate-800 border border-slate-700 rounded-xl shadow-2xl max-h-96 overflow-y-auto"
                  style={{ zIndex: 9999 }}
                >
                  <div className="p-2">
                    <div className="text-xs text-slate-500 px-3 py-2 border-b border-slate-700/50 mb-1">
                      找到 {searchResults.length} 条相关消息
                    </div>
                    {searchResults.map((result) => (
                      <div
                        key={`${result.conversation_id}-${result.message_id}`}
                        onClick={() => handleSelectSearchResult(result)}
                        className="flex items-start gap-3 px-3 py-3 rounded-lg cursor-pointer hover:bg-slate-700/50 transition-colors group"
                      >
                        <div className={`w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 ${
                          result.role === 'user' ? 'bg-blue-500/20' : 'bg-emerald-500/20'
                        }`}>
                          {result.role === 'user' ? (
                            <UserOutlined className="text-blue-400 text-sm" />
                          ) : (
                            <MessageOutlined className="text-emerald-400 text-sm" />
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <span className="text-xs font-medium text-emerald-400 truncate max-w-[150px]">
                              {result.conversation_title}
                            </span>
                            <span className="text-xs text-slate-600">·</span>
                            <span className="text-xs text-slate-500">
                              {result.role === 'user' ? '你' : 'AI'}
                            </span>
                          </div>
                          <div className="text-sm text-slate-300 line-clamp-2 leading-relaxed">
                            {result.content_snippet}
                          </div>
                          <div className="text-xs text-slate-600 mt-1">
                            {dayjs(result.created_at).fromNow()}
                          </div>
                        </div>
                        <div className="opacity-0 group-hover:opacity-100 transition-opacity text-slate-500">
                          <RightOutlined className="text-xs" />
                        </div>
                      </div>
                    ))}
                  </div>
                </motion.div>
              )}
              {showSearchResults && searchValue && !isSearching && searchResults.length === 0 && (
                <motion.div
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  className="absolute top-full left-0 right-0 mt-2 bg-slate-800 border border-slate-700 rounded-xl shadow-2xl p-6"
                  style={{ zIndex: 9999 }}
                >
                  <div className="text-center">
                    <InboxOutlined className="text-3xl text-slate-600 mb-2" />
                    <div className="text-slate-500 text-sm">未找到相关消息</div>
                    <div className="text-slate-600 text-xs mt-1">尝试使用不同的关键词</div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
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
                  src={user?.avatar}
                  className="bg-gradient-to-br from-blue-500 to-indigo-600"
                />
                <div className="hidden md:flex flex-col">
                  <span className="text-slate-300 text-sm leading-tight">{user?.username}</span>
                  <span className="text-slate-500 text-xs leading-tight">{user?.role === 'admin' ? '管理员' : user?.role === 'mentor' ? '导师' : '学生'}</span>
                </div>
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
