import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Input, Button, Statistic, Row, Col, Empty, Modal, message } from 'antd'
import {
  SendOutlined,
  MessageOutlined,
  FileTextOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  RocketOutlined,
  BookOutlined,
  ExperimentOutlined,
  LoadingOutlined,
  ArrowRightOutlined,
  ClockCircleOutlined,
  TeamOutlined,
  UserOutlined,
  CrownOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useKnowledgeStore } from '@/stores/knowledgeStore'
import { useMentorshipStore } from '@/stores/mentorshipStore'
import { RoleBadge } from '@/components/ui'
import { StudentActivities } from '@/components/team/MentorDashboard'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

const { TextArea } = Input

const hasBrokenTitle = (value?: string | null) => {
  const text = String(value || '').trim()
  if (!text) return true
  const brokenCount = (text.match(/\?/g) || []).length + (text.match(/�/g) || []).length
  return brokenCount >= Math.ceil(text.length * 0.45)
}

const safeConversationTitle = (value?: string | null) => {
  const text = String(value || '').trim()
  return hasBrokenTitle(text) ? '未命名对话' : text
}

const DashboardPage = () => {
  const navigate = useNavigate()
  const { user, isMentor, isStudent, isAdmin } = useAuthStore()
  const { conversations, createConversation, isSending } = useChatStore()
  const { knowledgeBases, totalKnowledgeBases, fetchKnowledgeBases } = useKnowledgeStore()
  const {
    myMentorship, 
    myStudents, 
    studentActivities,
    pendingCount,
    fetchMyMentorship,
    fetchPendingRequests,
    fetchMyStudents,
    fetchPendingCount,
  } = useMentorshipStore()
  const [quickInput, setQuickInput] = useState('')
  const [showAllActivities, setShowAllActivities] = useState(false)
  const [showAllConversations, setShowAllConversations] = useState(false)
  
  // 获取知识库列表和角色相关数据
  useEffect(() => {
    fetchKnowledgeBases()
    
    // 根据角色加载数据
    if (isStudent()) {
      fetchMyMentorship()
    } else if (isMentor()) {
      fetchPendingRequests()
      fetchMyStudents()
      fetchPendingCount()
    }
  }, [user?.role])
  
  // 计算知识库总文档数
  const totalDocuments = knowledgeBases.reduce((sum, kb) => sum + kb.document_count, 0)
  
  // 获取问候语
  const getGreeting = () => {
    const hour = new Date().getHours()
    if (hour < 6) return '🌙 夜深了'
    if (hour < 12) return '🌅 早上好'
    if (hour < 14) return '☀️ 中午好'
    if (hour < 18) return '🌤️ 下午好'
    return '🌆 晚上好'
  }
  
  // 快速发送消息
  const handleQuickSend = async () => {
    if (!quickInput.trim() || isSending) return
    
    try {
      const conversation = await createConversation(quickInput.slice(0, 30))
      navigate(`/chat/${conversation.id}`, { state: { initialMessage: quickInput } })
    } catch (error) {
      message.error('创建对话失败')
    }
  }
  
  // 快捷入口卡片（根据角色调整）
  const quickAccessCards = [
    {
      icon: <MessageOutlined className="text-2xl" />,
      title: 'AI 对话',
      desc: '智能问答与分析',
      gradient: 'from-emerald-500 to-teal-600',
      shadow: 'shadow-emerald-500/20',
      path: '/chat',
    },
    {
      icon: <TeamOutlined className="text-2xl" />,
      title: isMentor() ? '学生管理' : '我的团队',
      desc: isMentor() ? '管理指导关系' : '选择导师',
      gradient: 'from-violet-500 to-purple-600',
      shadow: 'shadow-violet-500/20',
      path: '/student/mentor',
      badge: isMentor() && pendingCount > 0 ? pendingCount : undefined,
    },
    {
      icon: <DatabaseOutlined className="text-2xl" />,
      title: '知识库',
      desc: '文档管理与检索',
      gradient: 'from-blue-500 to-indigo-600',
      shadow: 'shadow-blue-500/20',
      path: '/knowledge',
    },
    {
      icon: <BookOutlined className="text-2xl" />,
      title: '文献管理',
      desc: '论文搜索与收藏',
      gradient: 'from-amber-500 to-orange-600',
      shadow: 'shadow-amber-500/20',
      path: '/literature',
    },
  ]
  
  // 快速提问建议
  const quickPrompts = [
    { icon: '🔬', text: '深度学习最新进展' },
    { icon: '🧠', text: 'Transformer 原理解析' },
    { icon: '📊', text: '如何设计实验方案' },
    { icon: '💻', text: 'PyTorch vs TensorFlow' },
  ]

  const orderedConversations = [...conversations].sort(
    (a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf()
  )
  const recentConversations = orderedConversations.slice(0, 5)
  const mentorshipTarget = isMentor() ? '/mentor/students' : '/student/mentor'
  const mentorshipTargetLabel = isMentor() ? '前往导师中心' : '前往我的导师'
  
  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-900 to-slate-950">
      <div className="max-w-6xl mx-auto p-6 space-y-8">
        {/* 欢迎卡片 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <Card className="border-0 overflow-hidden" style={{ 
            background: 'linear-gradient(135deg, rgba(16, 185, 129, 0.1) 0%, rgba(20, 184, 166, 0.05) 100%)',
            borderRadius: 20,
            border: '1px solid rgba(16, 185, 129, 0.2)',
          }}>
            <div className="relative py-4">
              {/* 背景装饰 */}
              <div className="absolute top-0 right-0 w-80 h-80 bg-gradient-to-br from-emerald-500/10 to-teal-500/10 rounded-full blur-3xl -translate-y-1/2 translate-x-1/3" />
              <div className="absolute bottom-0 left-0 w-60 h-60 bg-gradient-to-tr from-blue-500/10 to-violet-500/10 rounded-full blur-3xl translate-y-1/2 -translate-x-1/3" />
              
              <div className="relative z-10">
                <div className="flex items-center gap-2 mb-3">
                  <div className="w-8 h-8 rounded-lg bg-emerald-500/20 flex items-center justify-center">
                    <ThunderboltOutlined className="text-emerald-400" />
                  </div>
                  <span className="text-slate-400 text-sm font-medium">科研助手 · 智能工作台</span>
                  {user?.role && (
                    <RoleBadge role={user.role} />
                  )}
                </div>
                
                <h1 className="text-3xl font-bold text-white mb-2">
                  {getGreeting()}，{user?.full_name || user?.username}
                </h1>
                <p className="text-slate-400 mb-8 text-base">
                  {isMentor() && myStudents.length > 0 ? (
                    <>
                      你目前指导 <span className="text-emerald-400">{myStudents.length}</span> 名学生
                      {pendingCount > 0 && (
                        <span className="text-amber-400">，有 {pendingCount} 份待处理申请</span>
                      )}
                    </>
                  ) : isStudent() && !myMentorship ? (
                    <>还没有选择导师？前往 <span className="text-violet-400 cursor-pointer hover:underline" onClick={() => navigate('/student/mentor')}>我的导师</span> 找到合适的导师</>
                  ) : (
                    <>
                      我可以帮你解答科研问题、分析数据、撰写论文。
                      {totalDocuments > 0 && (
                        <span className="text-emerald-400"> 已接入你的 {totalDocuments} 篇知识文档！</span>
                      )}
                    </>
                  )}
                </p>
                
                {/* 快速输入框 */}
                <div className="relative max-w-3xl">
                  <TextArea
                    value={quickInput}
                    onChange={(e) => setQuickInput(e.target.value)}
                    placeholder="输入你的问题，我会展示完整的思考过程..."
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    className="text-base bg-slate-800/60 border-slate-700/50 rounded-2xl pr-28"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        handleQuickSend()
                      }
                    }}
                  />
                  <Button
                    type="primary"
                    icon={isSending ? <LoadingOutlined /> : <SendOutlined />}
                    onClick={handleQuickSend}
                    disabled={!quickInput.trim() || isSending}
                    className="absolute right-3 bottom-3 rounded-xl h-10 px-6"
                  >
                    开始
                  </Button>
                </div>
                
                {/* 快速提问建议 */}
                <div className="flex flex-wrap gap-2 mt-5">
                  {quickPrompts.map((prompt, index) => (
                    <motion.button
                      key={index}
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 0.2 + index * 0.05 }}
                      onClick={() => setQuickInput(prompt.text)}
                      className="flex items-center gap-2 px-4 py-2 rounded-xl bg-slate-800/40 border border-slate-700/50 text-slate-300 text-sm hover:bg-slate-700/50 hover:border-emerald-500/30 hover:text-white transition-all"
                    >
                      <span>{prompt.icon}</span>
                      <span>{prompt.text}</span>
                    </motion.button>
                  ))}
                </div>
              </div>
            </div>
          </Card>
        </motion.div>
        
        {/* 统计概览 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
        >
          <Row gutter={16}>
            {[
              { title: '对话总数', value: conversations.length, icon: <MessageOutlined />, color: '#10b981' },
              { title: '知识文档', value: totalDocuments, icon: <FileTextOutlined />, color: '#8b5cf6', suffix: '篇' },
              { 
                title: isMentor() ? '我的学生' : '知识库', 
                value: isMentor() ? myStudents.length : totalKnowledgeBases, 
                icon: isMentor() ? <UserOutlined /> : <DatabaseOutlined />, 
                color: '#3b82f6', 
                suffix: isMentor() ? '人' : '个' 
              },
              { 
                title: isMentor() ? '待处理申请' : '代码运行', 
                value: isMentor() ? pendingCount : 0, 
                icon: isMentor() ? <ClockCircleOutlined /> : <ExperimentOutlined />, 
                color: isMentor() && pendingCount > 0 ? '#f59e0b' : '#f59e0b', 
                suffix: isMentor() ? '份' : '次' 
              },
            ].map((stat, index) => (
              <Col xs={12} sm={6} key={index}>
                <motion.div
                  initial={{ opacity: 0, scale: 0.9 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.15 + index * 0.05 }}
                >
                  <Card className="border-slate-800 bg-slate-800/30 rounded-2xl hover:bg-slate-800/50 transition-colors">
                    <Statistic
                      title={<span className="text-slate-400 text-sm">{stat.title}</span>}
                      value={stat.value}
                      prefix={<span style={{ color: stat.color }}>{stat.icon}</span>}
                      valueStyle={{ color: stat.color, fontSize: 28 }}
                      suffix={stat.suffix && <span className="text-base text-slate-500">{stat.suffix}</span>}
                    />
                  </Card>
                </motion.div>
              </Col>
            ))}
          </Row>
        </motion.div>
        
        {/* 快捷入口 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          <div className="flex items-center gap-2 mb-5">
            <RocketOutlined className="text-emerald-400" />
            <h2 className="text-lg font-semibold text-white">快捷入口</h2>
          </div>
          <Row gutter={16}>
            {quickAccessCards.map((card, index) => (
              <Col xs={12} md={6} key={index}>
                <motion.div
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.25 + index * 0.05 }}
                  whileHover={{ scale: 1.02, y: -4 }}
                  whileTap={{ scale: 0.98 }}
                >
                  <Card
                    className="rounded-2xl cursor-pointer transition-all duration-300 h-full border-slate-700/50 bg-slate-800/40 hover:border-emerald-500/30"
                    onClick={() => navigate(card.path)}
                  >
                    <div className="flex flex-col items-center text-center py-4 relative">
                      {/* 徽章 */}
                      {card.badge && (
                        <div className="absolute -top-2 -right-2 w-6 h-6 bg-amber-500 rounded-full flex items-center justify-center text-white text-xs font-bold shadow-lg">
                          {card.badge}
                        </div>
                      )}
                      <div
                        className={`w-14 h-14 rounded-2xl bg-gradient-to-br ${card.gradient} flex items-center justify-center text-white mb-4 ${card.shadow} shadow-lg`}
                      >
                        {card.icon}
                      </div>
                      <h3 className="text-white font-semibold mb-1">{card.title}</h3>
                      <p className="text-slate-500 text-sm">{card.desc}</p>
                    </div>
                  </Card>
                </motion.div>
              </Col>
            ))}
          </Row>
        </motion.div>

        {/* 导师专属：学生活动 */}
        {isMentor() && studentActivities.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.25 }}
          >
            <div className="flex items-center justify-between mb-5">
              <div className="flex items-center gap-2">
                <UserOutlined className="text-violet-400" />
                <h2 className="text-lg font-semibold text-white">学生动态</h2>
              </div>
              <Button 
                type="text" 
                className="text-slate-400 hover:text-violet-400"
                onClick={() => setShowAllActivities(true)}
              >
                查看全部 <ArrowRightOutlined />
              </Button>
            </div>
            <Card className="rounded-2xl border-slate-800 bg-slate-800/30">
              <StudentActivities activities={studentActivities.slice(0, 5)} />
            </Card>
          </motion.div>
        )}
        
        {/* 最近对话 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.3 }}
        >
          <div className="flex items-center justify-between mb-5">
            <div className="flex items-center gap-2">
              <ClockCircleOutlined className="text-emerald-400" />
              <h2 className="text-lg font-semibold text-white">最近对话</h2>
            </div>
            {conversations.length > 0 && (
              <Button 
                type="text" 
                className="text-slate-400 hover:text-emerald-400"
                onClick={() => setShowAllConversations(true)}
              >
                查看全部 <ArrowRightOutlined />
              </Button>
            )}
          </div>
          <Card className="rounded-2xl border-slate-800 bg-slate-800/30">
            {conversations.length > 0 ? (
              <div className="divide-y divide-slate-800">
                {recentConversations.map((conv, index) => (
                  <motion.div
                    key={conv.id}
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.35 + index * 0.05 }}
                    onClick={() => navigate(`/chat/${conv.id}`)}
                    className="flex items-center justify-between p-4 hover:bg-slate-700/30 cursor-pointer transition-colors group first:rounded-t-xl last:rounded-b-xl"
                  >
                    <div className="flex items-center gap-4">
                      <div className="w-10 h-10 rounded-xl bg-emerald-500/20 flex items-center justify-center">
                        <MessageOutlined className="text-emerald-400" />
                      </div>
                      <div>
                        <h4 className="text-white font-medium group-hover:text-emerald-400 transition-colors">
                          {safeConversationTitle(conv.title)}
                        </h4>
                        <p className="text-slate-500 text-sm mt-0.5">
                          {conv.message_count || 0} 条消息 · {dayjs(conv.updated_at).fromNow()}
                        </p>
                      </div>
                    </div>
                    <ArrowRightOutlined className="text-slate-600 opacity-0 group-hover:opacity-100 group-hover:text-emerald-400 transition-all transform group-hover:translate-x-1" />
                  </motion.div>
                ))}
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div className="text-slate-500">
                    <p>暂无对话记录</p>
                    <p className="text-sm mt-1">开始你的第一次 AI 对话吧</p>
                  </div>
                }
                className="py-10"
              >
                <Button type="primary" onClick={() => navigate('/chat')} className="mt-2 rounded-xl">
                  开始对话
                </Button>
              </Empty>
            )}
          </Card>
        </motion.div>

        <Modal
          title={
            <div className="flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg bg-violet-500/20 border border-violet-400/30 flex items-center justify-center">
                <UserOutlined className="text-violet-300 text-sm" />
              </span>
              <span className="text-slate-100">全部学生动态</span>
            </div>
          }
          open={showAllActivities}
          onCancel={() => setShowAllActivities(false)}
          centered
          width={900}
          closeIcon={<span className="text-slate-400 hover:text-white">✕</span>}
          className="dashboard-all-modal"
          styles={{
            content: {
              background: 'linear-gradient(145deg, rgba(10, 16, 30, 0.96) 0%, rgba(18, 34, 64, 0.92) 100%)',
              border: '1px solid rgba(148, 163, 184, 0.25)',
              boxShadow: '0 24px 80px rgba(2, 6, 23, 0.65)',
            },
            header: {
              background: 'transparent',
              borderBottom: '1px solid rgba(148, 163, 184, 0.2)',
              padding: '16px 18px',
            },
            body: {
              padding: '14px 18px 10px',
            },
            footer: {
              background: 'transparent',
              borderTop: '1px solid rgba(148, 163, 184, 0.2)',
              padding: '12px 18px 16px',
            },
          }}
          footer={
            <div className="flex items-center justify-end gap-3">
              <Button
                key="mentor"
                icon={<TeamOutlined />}
                onClick={() => {
                  setShowAllActivities(false)
                  navigate(mentorshipTarget)
                }}
                className="!h-10 !rounded-xl !border-violet-400/40 !bg-violet-500/10 !text-violet-100 hover:!border-violet-300 hover:!text-white"
              >
                {mentorshipTargetLabel}
              </Button>
              <Button
                key="close"
                type="primary"
                onClick={() => setShowAllActivities(false)}
                className="!h-10 !rounded-xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 hover:!from-emerald-400 hover:!to-cyan-400"
              >
                关闭
              </Button>
            </div>
          }
        >
          <div className="mb-3 rounded-xl border border-violet-400/25 bg-violet-500/10 px-3 py-2.5 text-xs text-violet-100/90">
            共 {studentActivities.length} 条动态，点击下方按钮可进入完整学生管理页面。
          </div>
          <div className="max-h-[62vh] overflow-y-auto pr-1">
            <StudentActivities activities={studentActivities} />
          </div>
        </Modal>

        <Modal
          title={
            <div className="flex items-center gap-2">
              <span className="w-7 h-7 rounded-lg bg-emerald-500/20 border border-emerald-400/30 flex items-center justify-center">
                <MessageOutlined className="text-emerald-300 text-sm" />
              </span>
              <span className="text-slate-100">全部对话（{orderedConversations.length}）</span>
            </div>
          }
          open={showAllConversations}
          onCancel={() => setShowAllConversations(false)}
          centered
          width={900}
          closeIcon={<span className="text-slate-400 hover:text-white">✕</span>}
          className="dashboard-all-modal"
          styles={{
            content: {
              background: 'linear-gradient(145deg, rgba(10, 16, 30, 0.96) 0%, rgba(18, 34, 64, 0.92) 100%)',
              border: '1px solid rgba(148, 163, 184, 0.25)',
              boxShadow: '0 24px 80px rgba(2, 6, 23, 0.65)',
            },
            header: {
              background: 'transparent',
              borderBottom: '1px solid rgba(148, 163, 184, 0.2)',
              padding: '16px 18px',
            },
            body: {
              padding: '14px 18px 10px',
            },
            footer: {
              background: 'transparent',
              borderTop: '1px solid rgba(148, 163, 184, 0.2)',
              padding: '12px 18px 16px',
            },
          }}
          footer={
            <div className="flex items-center justify-end gap-3">
              <Button
                key="chat"
                icon={<MessageOutlined />}
                onClick={() => {
                  setShowAllConversations(false)
                  navigate('/chat')
                }}
                className="!h-10 !rounded-xl !border-emerald-400/40 !bg-emerald-500/10 !text-emerald-100 hover:!border-emerald-300 hover:!text-white"
              >
                前往聊天页
              </Button>
              <Button
                key="close"
                type="primary"
                onClick={() => setShowAllConversations(false)}
                className="!h-10 !rounded-xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 hover:!from-emerald-400 hover:!to-cyan-400"
              >
                关闭
              </Button>
            </div>
          }
        >
          <div className="mb-3 rounded-xl border border-emerald-400/25 bg-emerald-500/10 px-3 py-2.5 text-xs text-emerald-100/90">
            可直接点击任一会话进入详情，或进入聊天页管理全部历史会话。
          </div>
          {orderedConversations.length > 0 ? (
            <div className="max-h-[62vh] overflow-y-auto space-y-2 pr-1">
              {orderedConversations.map((conv) => (
                <button
                  key={conv.id}
                  type="button"
                  onClick={() => {
                    setShowAllConversations(false)
                    navigate(`/chat/${conv.id}`)
                  }}
                  className="w-full text-left flex items-center justify-between px-3 py-3 rounded-xl bg-slate-900/70 border border-slate-700/70 hover:bg-slate-800/85 hover:border-emerald-400/40 transition-all"
                >
                  <div className="min-w-0">
                    <p className="font-medium text-slate-100 truncate">
                      {safeConversationTitle(conv.title)}
                    </p>
                    <p className="text-xs text-slate-400 mt-0.5">
                      {conv.message_count || 0} 条消息 · {dayjs(conv.updated_at).fromNow()}
                    </p>
                  </div>
                  <ArrowRightOutlined className="text-emerald-300/80 ml-4 flex-shrink-0" />
                </button>
              ))}
            </div>
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无对话记录"
              className="py-10"
            />
          )}
        </Modal>
      </div>
    </div>
  )
}

export default DashboardPage
