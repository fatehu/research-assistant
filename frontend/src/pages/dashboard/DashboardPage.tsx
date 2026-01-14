import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Input, Button, Statistic, Row, Col, Empty, Spin, message } from 'antd'
import {
  SendOutlined,
  MessageOutlined,
  FileTextOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  RocketOutlined,
  BookOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'

const { TextArea } = Input

const DashboardPage = () => {
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const { conversations, createConversation, sendMessage, isSending } = useChatStore()
  const [quickInput, setQuickInput] = useState('')
  
  // 获取问候语
  const getGreeting = () => {
    const hour = new Date().getHours()
    if (hour < 6) return '夜深了'
    if (hour < 12) return '早上好'
    if (hour < 14) return '中午好'
    if (hour < 18) return '下午好'
    return '晚上好'
  }
  
  // 快速发送消息
  const handleQuickSend = async () => {
    if (!quickInput.trim() || isSending) return
    
    try {
      const conversation = await createConversation()
      navigate(`/chat/${conversation.id}`, { state: { initialMessage: quickInput } })
    } catch (error) {
      message.error('创建对话失败')
    }
  }
  
  // 快捷入口卡片
  const quickAccessCards = [
    {
      icon: <MessageOutlined className="text-2xl" />,
      title: 'AI 对话',
      desc: '与 AI 助手交流',
      color: 'from-blue-500 to-blue-600',
      path: '/chat',
    },
    {
      icon: <DatabaseOutlined className="text-2xl" />,
      title: '知识库',
      desc: '管理文档与检索',
      color: 'from-purple-500 to-purple-600',
      path: '/knowledge',
      disabled: true,
    },
    {
      icon: <BookOutlined className="text-2xl" />,
      title: '文献管理',
      desc: '搜索与收藏论文',
      color: 'from-green-500 to-green-600',
      path: '/papers',
      disabled: true,
    },
    {
      icon: <ExperimentOutlined className="text-2xl" />,
      title: '代码实验室',
      desc: '运行 Python 代码',
      color: 'from-orange-500 to-orange-600',
      path: '/code',
      disabled: true,
    },
  ]
  
  // 快速提问建议
  const quickPrompts = [
    '帮我总结一下深度学习的最新进展',
    '解释 Transformer 架构的核心原理',
    '如何设计一个高效的推荐系统？',
    '对比 PyTorch 和 TensorFlow 的优缺点',
  ]
  
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="max-w-6xl mx-auto space-y-6">
        {/* 欢迎卡片 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <Card className="glass-card overflow-hidden">
            <div className="relative">
              {/* 背景装饰 */}
              <div className="absolute top-0 right-0 w-64 h-64 bg-gradient-to-br from-blue-500/20 to-purple-500/20 rounded-full blur-3xl -translate-y-1/2 translate-x-1/2" />
              
              <div className="relative z-10">
                <div className="flex items-center gap-3 mb-2">
                  <ThunderboltOutlined className="text-2xl text-yellow-400" />
                  <span className="text-gray-400 text-sm">科研助手 · 智能工作台</span>
                </div>
                
                <h1 className="text-3xl font-bold text-white mb-2">
                  {getGreeting()}，{user?.full_name || user?.username}！
                </h1>
                <p className="text-gray-400 mb-6">
                  有什么我可以帮助你的？开始一段新的研究探索吧。
                </p>
                
                {/* 快速输入框 */}
                <div className="relative">
                  <TextArea
                    value={quickInput}
                    onChange={(e) => setQuickInput(e.target.value)}
                    placeholder="输入你的问题，让 AI 助手帮助你..."
                    autoSize={{ minRows: 2, maxRows: 4 }}
                    className="pr-16 text-base"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault()
                        handleQuickSend()
                      }
                    }}
                  />
                  <Button
                    type="primary"
                    icon={isSending ? <Spin size="small" /> : <SendOutlined />}
                    onClick={handleQuickSend}
                    disabled={!quickInput.trim() || isSending}
                    className="absolute right-2 bottom-2"
                  >
                    发送
                  </Button>
                </div>
                
                {/* 快速提问建议 */}
                <div className="flex flex-wrap gap-2 mt-4">
                  {quickPrompts.map((prompt, index) => (
                    <motion.div
                      key={index}
                      initial={{ opacity: 0, scale: 0.9 }}
                      animate={{ opacity: 1, scale: 1 }}
                      transition={{ delay: index * 0.1 }}
                    >
                      <Button
                        size="small"
                        className="text-gray-400 border-gray-600 hover:text-blue-400 hover:border-blue-500"
                        onClick={() => setQuickInput(prompt)}
                      >
                        {prompt}
                      </Button>
                    </motion.div>
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
            <Col xs={12} sm={6}>
              <Card className="glass-card">
                <Statistic
                  title={<span className="text-gray-400">对话总数</span>}
                  value={conversations.length}
                  prefix={<MessageOutlined className="text-blue-400" />}
                  valueStyle={{ color: '#60a5fa' }}
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card className="glass-card">
                <Statistic
                  title={<span className="text-gray-400">知识文档</span>}
                  value={0}
                  prefix={<FileTextOutlined className="text-purple-400" />}
                  valueStyle={{ color: '#a78bfa' }}
                  suffix="篇"
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card className="glass-card">
                <Statistic
                  title={<span className="text-gray-400">收藏文献</span>}
                  value={0}
                  prefix={<BookOutlined className="text-green-400" />}
                  valueStyle={{ color: '#4ade80' }}
                  suffix="篇"
                />
              </Card>
            </Col>
            <Col xs={12} sm={6}>
              <Card className="glass-card">
                <Statistic
                  title={<span className="text-gray-400">代码运行</span>}
                  value={0}
                  prefix={<ExperimentOutlined className="text-orange-400" />}
                  valueStyle={{ color: '#fb923c' }}
                  suffix="次"
                />
              </Card>
            </Col>
          </Row>
        </motion.div>
        
        {/* 快捷入口 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
        >
          <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <RocketOutlined className="text-blue-400" />
            快捷入口
          </h2>
          <Row gutter={16}>
            {quickAccessCards.map((card, index) => (
              <Col xs={12} md={6} key={index}>
                <motion.div
                  whileHover={{ scale: card.disabled ? 1 : 1.02 }}
                  whileTap={{ scale: card.disabled ? 1 : 0.98 }}
                >
                  <Card
                    className={`glass-card cursor-pointer transition-all duration-300 ${
                      card.disabled
                        ? 'opacity-50 cursor-not-allowed'
                        : 'hover:border-blue-500/50'
                    }`}
                    onClick={() => !card.disabled && navigate(card.path)}
                  >
                    <div className="flex flex-col items-center text-center py-2">
                      <div
                        className={`w-12 h-12 rounded-xl bg-gradient-to-br ${card.color} flex items-center justify-center text-white mb-3 shadow-lg`}
                      >
                        {card.icon}
                      </div>
                      <h3 className="text-white font-medium mb-1">{card.title}</h3>
                      <p className="text-gray-500 text-sm">{card.desc}</p>
                      {card.disabled && (
                        <span className="text-xs text-gray-600 mt-2">即将开放</span>
                      )}
                    </div>
                  </Card>
                </motion.div>
              </Col>
            ))}
          </Row>
        </motion.div>
        
        {/* 最近对话 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.3 }}
        >
          <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
            <MessageOutlined className="text-blue-400" />
            最近对话
          </h2>
          <Card className="glass-card">
            {conversations.length > 0 ? (
              <div className="space-y-2">
                {conversations.slice(0, 5).map((conv, index) => (
                  <motion.div
                    key={conv.id}
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: index * 0.05 }}
                    onClick={() => navigate(`/chat/${conv.id}`)}
                    className="flex items-center justify-between p-3 rounded-lg hover:bg-white/5 cursor-pointer transition-colors group"
                  >
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-blue-500/20 flex items-center justify-center">
                        <MessageOutlined className="text-blue-400 text-sm" />
                      </div>
                      <div>
                        <h4 className="text-white text-sm font-medium group-hover:text-blue-400 transition-colors">
                          {conv.title}
                        </h4>
                        <p className="text-gray-500 text-xs">
                          {conv.message_count || 0} 条消息 ·{' '}
                          {new Date(conv.updated_at).toLocaleDateString('zh-CN')}
                        </p>
                      </div>
                    </div>
                    <div className="text-gray-600 text-xs opacity-0 group-hover:opacity-100 transition-opacity">
                      点击继续 →
                    </div>
                  </motion.div>
                ))}
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span className="text-gray-500">暂无对话记录，开始你的第一次提问吧！</span>
                }
              >
                <Button type="primary" onClick={() => navigate('/chat')}>
                  开始对话
                </Button>
              </Empty>
            )}
          </Card>
        </motion.div>
      </div>
    </div>
  )
}

export default DashboardPage
