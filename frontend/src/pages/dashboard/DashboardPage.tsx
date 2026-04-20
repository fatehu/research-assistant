import { useEffect, useState } from 'react'
import type { MouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Card, Input, Button, Row, Col, Empty, Modal, message } from 'antd'
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
} from '@ant-design/icons'
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useKnowledgeStore } from '@/stores/knowledgeStore'
import { useMentorshipStore } from '@/stores/mentorshipStore'
import { RoleBadge } from '@/components/ui'
import { StudentActivities } from '@/components/team/MentorDashboard'
import RetrievalRuntimeStatusStrip from '@/components/system/RetrievalRuntimeStatusStrip'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

const { TextArea } = Input

const sectionCardClass =
  '!overflow-hidden !rounded-[28px] !border !border-white/[0.06] !bg-slate-900/50 !shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_28px_60px_rgba(2,6,23,0.34)] backdrop-blur-2xl'

const touchFeedbackClass =
  'cursor-pointer touch-manipulation focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-300/25 focus-visible:ring-offset-0 active:transition-none'

const floatingListItemClass =
  `group flex appearance-none border border-transparent bg-transparent w-full items-center justify-between gap-4 rounded-2xl px-4 py-4 text-left transition-all duration-200 hover:bg-slate-800/55 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_14px_28px_rgba(2,6,23,0.28)] active:scale-[0.988] active:border-emerald-400/22 active:bg-slate-800/72 active:shadow-[inset_0_1px_0_rgba(255,255,255,0.07),0_10px_20px_rgba(2,6,23,0.24)] ${touchFeedbackClass}`

const hasBrokenTitle = (value?: string | null) => {
  const text = String(value || '').trim()
  if (!text) return true
  const brokenCount = (text.match(/\?/g) || []).length + (text.match(/\uFFFD/g) || []).length
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

  const heroMouseX = useMotionValue(0)
  const heroMouseY = useMotionValue(0)
  const smoothHeroMouseX = useSpring(heroMouseX, { stiffness: 120, damping: 18, mass: 0.7 })
  const smoothHeroMouseY = useSpring(heroMouseY, { stiffness: 120, damping: 18, mass: 0.7 })
  const primaryGlowX = useTransform(smoothHeroMouseX, (value) => value * 0.18)
  const primaryGlowY = useTransform(smoothHeroMouseY, (value) => value * 0.18)
  const secondaryGlowX = useTransform(smoothHeroMouseX, (value) => value * -0.14)
  const secondaryGlowY = useTransform(smoothHeroMouseY, (value) => value * -0.12)

  useEffect(() => {
    document.body.classList.add('dashboard-route-active')

    return () => {
      document.body.classList.remove('dashboard-route-active')
    }
  }, [])

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
  }, [
    fetchKnowledgeBases,
    fetchMyMentorship,
    fetchMyStudents,
    fetchPendingCount,
    fetchPendingRequests,
    isMentor,
    isStudent,
    user?.role,
  ])

  // 计算知识库总文档数
  const totalDocuments = knowledgeBases.reduce((sum, kb) => sum + kb.document_count, 0)

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
      const conversation = await createConversation(quickInput.slice(0, 30))
      navigate(`/chat/${conversation.id}`, { state: { initialMessage: quickInput } })
    } catch {
      message.error('创建对话失败')
    }
  }

  const handleHeroMouseMove = (event: MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const offsetX = event.clientX - rect.left - rect.width / 2
    const offsetY = event.clientY - rect.top - rect.height / 2
    heroMouseX.set(offsetX)
    heroMouseY.set(offsetY)
  }

  const handleHeroMouseLeave = () => {
    heroMouseX.set(0)
    heroMouseY.set(0)
  }

  // 快捷入口卡片（根据角色调整）
  const quickAccessCards = [
    {
      icon: <MessageOutlined className="text-[26px]" />,
      kicker: 'dialog',
      title: 'AI 对话',
      desc: '智能问答与分析',
      path: '/chat',
      iconShell: 'border-emerald-400/20 bg-[linear-gradient(135deg,rgba(16,185,129,0.26),rgba(20,184,166,0.14))]',
      iconColor: 'text-emerald-100',
      accentLine: 'via-emerald-400/70',
      hoverBorder: 'hover:border-emerald-400/45',
      hoverGlow: 'hover:shadow-[0_0_24px_rgba(16,185,129,0.16),0_24px_42px_rgba(2,6,23,0.34)]',
      hoverArrow: 'group-hover:text-emerald-300',
      statusShell: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100/85',
    },
    ...(!isAdmin()
      ? [
          {
            icon: <TeamOutlined className="text-[26px]" />,
            kicker: 'team',
            title: isMentor() ? '学生管理' : '我的团队',
            desc: isMentor() ? '管理指导关系' : '选择导师',
            path: isMentor() ? '/mentor/students' : '/student/mentor',
            badge: isMentor() && pendingCount > 0 ? pendingCount : undefined,
            iconShell: 'border-violet-400/20 bg-[linear-gradient(135deg,rgba(139,92,246,0.28),rgba(124,58,237,0.14))]',
            iconColor: 'text-violet-100',
            accentLine: 'via-violet-400/70',
            hoverBorder: 'hover:border-violet-400/45',
            hoverGlow: 'hover:shadow-[0_0_24px_rgba(139,92,246,0.16),0_24px_42px_rgba(2,6,23,0.34)]',
            hoverArrow: 'group-hover:text-violet-300',
            statusShell: 'border-violet-400/20 bg-violet-500/10 text-violet-100/85',
          },
        ]
      : []),
    {
      icon: <DatabaseOutlined className="text-[26px]" />,
      kicker: 'memory',
      title: '知识库',
      desc: '文档管理与检索',
      path: '/knowledge',
      iconShell: 'border-blue-400/20 bg-[linear-gradient(135deg,rgba(59,130,246,0.28),rgba(37,99,235,0.14))]',
      iconColor: 'text-blue-100',
      accentLine: 'via-blue-400/70',
      hoverBorder: 'hover:border-blue-400/45',
      hoverGlow: 'hover:shadow-[0_0_24px_rgba(59,130,246,0.16),0_24px_42px_rgba(2,6,23,0.34)]',
      hoverArrow: 'group-hover:text-blue-300',
      statusShell: 'border-blue-400/20 bg-blue-500/10 text-blue-100/85',
    },
    {
      icon: <BookOutlined className="text-[26px]" />,
      kicker: 'reader',
      title: '文献管理',
      desc: '论文搜索与收藏',
      path: '/literature',
      iconShell: 'border-amber-400/20 bg-[linear-gradient(135deg,rgba(245,158,11,0.28),rgba(249,115,22,0.14))]',
      iconColor: 'text-amber-100',
      accentLine: 'via-amber-300/70',
      hoverBorder: 'hover:border-amber-400/45',
      hoverGlow: 'hover:shadow-[0_0_24px_rgba(245,158,11,0.16),0_24px_42px_rgba(2,6,23,0.34)]',
      hoverArrow: 'group-hover:text-amber-200',
      statusShell: 'border-amber-400/20 bg-amber-500/10 text-amber-100/85',
    },
  ]

  const statsOverview = [
    {
      title: '对话总数',
      value: conversations.length,
      unit: '会话',
      icon: <MessageOutlined />,
      iconShell: 'border-emerald-400/18 bg-emerald-500/12',
      iconColor: 'text-emerald-300',
      valueColor: 'text-emerald-100',
      accentLine: 'via-emerald-300/75',
    },
    {
      title: '知识文档',
      value: totalDocuments,
      unit: '篇',
      icon: <FileTextOutlined />,
      iconShell: 'border-violet-400/18 bg-violet-500/12',
      iconColor: 'text-violet-300',
      valueColor: 'text-violet-100',
      accentLine: 'via-violet-300/75',
    },
    {
      title: isMentor() ? '我的学生' : '知识库',
      value: isMentor() ? myStudents.length : totalKnowledgeBases,
      unit: isMentor() ? '人' : '个',
      icon: isMentor() ? <UserOutlined /> : <DatabaseOutlined />,
      iconShell: 'border-blue-400/18 bg-blue-500/12',
      iconColor: 'text-blue-300',
      valueColor: 'text-blue-100',
      accentLine: 'via-blue-300/75',
    },
    {
      title: isMentor() ? '待处理申请' : '代码运行',
      value: isMentor() ? pendingCount : 0,
      unit: isMentor() ? '份' : '次',
      icon: isMentor() ? <ClockCircleOutlined /> : <ExperimentOutlined />,
      iconShell: 'border-amber-400/18 bg-amber-500/12',
      iconColor: 'text-amber-300',
      valueColor: 'text-amber-100',
      accentLine: 'via-amber-300/75',
    },
  ]

  const heroSignals = isMentor()
    ? [
        {
          label: 'threads',
          value: conversations.length,
          shell: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100/85',
          dot: 'bg-emerald-400',
        },
        {
          label: 'students',
          value: myStudents.length,
          shell: 'border-violet-400/20 bg-violet-500/10 text-violet-100/85',
          dot: 'bg-violet-400',
        },
        {
          label: 'pending',
          value: pendingCount,
          shell: 'border-amber-400/20 bg-amber-500/10 text-amber-100/85',
          dot: 'bg-amber-400',
        },
      ]
    : isAdmin()
      ? [
          {
            label: 'threads',
            value: conversations.length,
            shell: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100/85',
            dot: 'bg-emerald-400',
          },
          {
            label: 'knowledge bases',
            value: totalKnowledgeBases,
            shell: 'border-blue-400/20 bg-blue-500/10 text-blue-100/85',
            dot: 'bg-blue-400',
          },
          {
            label: 'admin access',
            value: 'ON',
            shell: 'border-amber-400/20 bg-amber-500/10 text-amber-100/85',
            dot: 'bg-amber-400',
          },
        ]
    : [
        {
          label: 'threads',
          value: conversations.length,
          shell: 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100/85',
          dot: 'bg-emerald-400',
        },
        {
          label: 'docs synced',
          value: totalDocuments,
          shell: 'border-cyan-400/20 bg-cyan-500/10 text-cyan-100/85',
          dot: 'bg-cyan-400',
        },
        {
          label: myMentorship ? 'mentor linked' : 'mentor needed',
          value: myMentorship ? 'ON' : 'OFF',
          shell: 'border-violet-400/20 bg-violet-500/10 text-violet-100/85',
          dot: myMentorship ? 'bg-violet-400' : 'bg-slate-500',
        },
      ]

  // 快速提问建议
  const quickPrompts = [
    { tag: 'RAG', text: '深度学习最新进展' },
    { tag: 'ARCH', text: 'Transformer 原理解析' },
    { tag: 'LAB', text: '如何设计实验方案' },
    { tag: 'STACK', text: 'PyTorch vs TensorFlow' },
  ]

  const orderedConversations = [...conversations].sort(
    (a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf()
  )
  const recentConversations = orderedConversations.slice(0, 5)
  const mentorshipTarget = isMentor() ? '/mentor/students' : '/student/mentor'
  const mentorshipTargetLabel = isMentor() ? '前往导师中心' : '前往我的导师'

  return (
    <div className="relative h-full overflow-y-auto text-slate-100">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(16,185,129,0.14),transparent_30%),radial-gradient(circle_at_88%_10%,rgba(139,92,246,0.16),transparent_24%),linear-gradient(180deg,rgba(2,6,23,0.08)_0%,rgba(2,6,23,0.46)_50%,rgba(2,6,23,0.72)_100%)]" />
      <div className="pointer-events-none absolute inset-0 opacity-[0.16] [background-image:linear-gradient(rgba(148,163,184,0.16)_1px,transparent_1px),linear-gradient(90deg,rgba(148,163,184,0.16)_1px,transparent_1px)] [background-size:96px_96px]" />

      <div className="relative mx-auto max-w-6xl space-y-8 p-6">
        {/* 欢迎卡片 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <Card className={`${sectionCardClass} [&_.ant-card-body]:!p-0`}>
            <div
              className="relative overflow-hidden px-6 py-6 sm:px-8 sm:py-8"
              onMouseMove={handleHeroMouseMove}
              onMouseLeave={handleHeroMouseLeave}
            >
              <motion.div
                style={{ x: primaryGlowX, y: primaryGlowY }}
                className="pointer-events-none absolute -right-16 -top-24 h-80 w-80 rounded-full bg-[radial-gradient(circle,rgba(16,185,129,0.22)_0%,rgba(16,185,129,0.10)_28%,transparent_72%)] blur-3xl"
              />
              <motion.div
                style={{ x: secondaryGlowX, y: secondaryGlowY }}
                className="pointer-events-none absolute -bottom-24 -left-12 h-72 w-72 rounded-full bg-[radial-gradient(circle,rgba(99,102,241,0.20)_0%,rgba(139,92,246,0.12)_32%,transparent_72%)] blur-3xl"
              />
              <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(120deg,rgba(255,255,255,0.08),transparent_24%,transparent_72%,rgba(255,255,255,0.04)_100%)] opacity-80" />

              <div className="relative z-10">
                <div className="mb-5 flex flex-wrap items-center gap-3">
                  <div className="inline-flex items-center gap-2 rounded-full border border-emerald-400/18 bg-emerald-500/10 px-3 py-1.5">
                    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/20">
                      <ThunderboltOutlined className="text-emerald-300" />
                    </div>
                    <span className="font-mono text-[11px] uppercase tracking-[0.28em] text-emerald-100/75">
                      command center
                    </span>
                  </div>
                  {user?.role && <RoleBadge role={user.role} size="sm" />}
                </div>

                <div className="max-w-4xl">
                  <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.28em] text-slate-500">
                    Research Assistant Workspace
                  </p>
                  <h1 className="mb-3 text-3xl font-semibold tracking-tight text-white sm:text-[2.1rem]">
                    {getGreeting()}，{user?.full_name || user?.username}
                  </h1>
                  <p className="mb-6 max-w-3xl text-base leading-7 text-slate-300/88">
                    {isMentor() && myStudents.length > 0 ? (
                      <>
                        你当前正在跟进 <span className="text-violet-300">{myStudents.length}</span> 名学生的研究进展，
                        {pendingCount > 0 ? (
                          <span className="text-amber-300">还有 {pendingCount} 份申请等待决策。</span>
                        ) : (
                          <span className="text-emerald-300">当前没有待处理申请。</span>
                        )}
                      </>
                    ) : isStudent() && !myMentorship ? (
                      <>
                        当前还未建立导师关系，前往
                        <span
                          className="mx-1 cursor-pointer text-violet-300 transition-colors hover:text-violet-200 hover:underline"
                          onClick={() => navigate('/student/mentor')}
                        >
                          我的导师
                        </span>
                        以接入更稳定的学术协作支持。
                      </>
                    ) : (
                      <>
                        在这里快速发起研究问题、数据分析或文献探索任务。
                        {totalDocuments > 0 && (
                          <span className="text-emerald-300"> 当前已同步 {totalDocuments} 篇知识文档。</span>
                        )}
                      </>
                    )}
                  </p>

                  <div className="mb-6 flex flex-wrap gap-2.5">
                    {heroSignals.map((signal) => (
                      <div
                        key={signal.label}
                        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 ${signal.shell}`}
                      >
                        <span className={`h-2 w-2 rounded-full ${signal.dot}`} />
                        <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-slate-400">
                          {signal.label}
                        </span>
                        <span className="text-sm font-semibold text-white">{signal.value}</span>
                      </div>
                    ))}
                  </div>

                  <RetrievalRuntimeStatusStrip />

                  {/* 快速输入框 */}
                  <div className="relative max-w-4xl">
                    <div className="relative overflow-hidden rounded-[28px] border border-white/10 bg-slate-950/76 p-3 shadow-[0_8px_32px_rgba(0,0,0,0.4),inset_0_1px_0_rgba(255,255,255,0.08)] ring-1 ring-white/10 backdrop-blur-2xl transition-all duration-300 focus-within:border-emerald-400/28 focus-within:ring-emerald-200/12">
                      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(255,255,255,0.12),transparent_34%),linear-gradient(135deg,rgba(16,185,129,0.10),transparent_52%,rgba(139,92,246,0.10)_100%)] opacity-90" />

                      <div className="relative z-10">
                        <div className="mb-2 flex items-center justify-between px-3">
                          <span className="font-mono text-[11px] uppercase tracking-[0.28em] text-emerald-100/72">
                            query engine
                          </span>
                          <span className="text-xs text-slate-500">Enter 发送，Shift + Enter 换行</span>
                        </div>

                        <TextArea
                          value={quickInput}
                          onChange={(e) => setQuickInput(e.target.value)}
                          placeholder="输入研究问题、数据任务或文献线索，我会直接展开完整工作流..."
                          autoSize={{ minRows: 3, maxRows: 6 }}
                          className="dashboard-command-input !rounded-[24px] !bg-transparent !text-base !text-slate-100"
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              e.preventDefault()
                              handleQuickSend()
                            }
                          }}
                        />

                        <div className="absolute inset-x-3 bottom-3 flex items-center justify-between gap-3">
                          <div className="hidden items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.03] px-3 py-1.5 text-xs text-slate-400 sm:inline-flex">
                            <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_12px_rgba(16,185,129,0.65)]" />
                            深度检索与知识问答已就绪
                          </div>
                          <Button
                            type="primary"
                            icon={isSending ? <LoadingOutlined /> : <SendOutlined />}
                            onClick={handleQuickSend}
                            disabled={!quickInput.trim() || isSending}
                            className={`!h-11 !cursor-pointer !rounded-2xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 !px-6 !font-medium !text-white !shadow-[0_12px_24px_rgba(16,185,129,0.28)] hover:!from-emerald-400 hover:!to-cyan-400 active:!translate-y-px active:!scale-[0.985] active:!from-emerald-500 active:!to-cyan-500 active:!shadow-[0_8px_16px_rgba(16,185,129,0.22)] disabled:!cursor-not-allowed disabled:!shadow-none ${touchFeedbackClass}`}
                          >
                            发起对话
                          </Button>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* 快速提问建议 */}
                  <div className="mt-5 flex flex-wrap gap-2">
                    {quickPrompts.map((prompt, index) => (
                      <motion.button
                        key={prompt.text}
                        type="button"
                        initial={{ opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ delay: 0.2 + index * 0.05 }}
                        whileTap={{ scale: 0.97, y: 1 }}
                        onClick={() => setQuickInput(prompt.text)}
                        className={`group flex items-center gap-3 rounded-full border border-white/[0.08] bg-white/[0.03] px-4 py-2 text-sm text-slate-300 transition-all hover:border-emerald-400/28 hover:bg-emerald-500/10 hover:text-white active:border-emerald-400/36 active:bg-emerald-500/16 active:text-white ${touchFeedbackClass}`}
                      >
                        <span className="rounded-full border border-white/[0.08] bg-slate-900/70 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-slate-400 transition-colors group-hover:text-emerald-200">
                          {prompt.tag}
                        </span>
                        <span>{prompt.text}</span>
                      </motion.button>
                    ))}
                  </div>
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
          <Row gutter={[16, 16]}>
            {statsOverview.map((stat, index) => (
              <Col xs={12} sm={6} key={stat.title}>
                <motion.div
                  initial={{ opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.15 + index * 0.05 }}
                >
                  <div className="relative overflow-hidden rounded-[24px] bg-slate-900/72 px-5 py-5 shadow-[inset_0_1px_1px_rgba(255,255,255,0.08),0_18px_36px_rgba(2,6,23,0.28)] transition-all duration-300 hover:bg-slate-900/82">
                    <div className={`absolute inset-x-6 top-0 h-px bg-gradient-to-r from-transparent ${stat.accentLine} to-transparent`} />

                    <div className="flex items-start justify-between gap-4">
                      <div
                        className={`flex h-11 w-11 items-center justify-center rounded-[14px] border ${stat.iconShell} ${stat.iconColor} shadow-[0_10px_24px_rgba(2,6,23,0.18)]`}
                      >
                        {stat.icon}
                      </div>
                      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-slate-600">
                        live
                      </span>
                    </div>

                    <div className="mt-6">
                      <p className="text-sm text-slate-400">{stat.title}</p>
                      <div className="mt-2 flex items-end gap-2">
                        <span className={`text-3xl font-semibold tracking-tight ${stat.valueColor}`}>
                          {stat.value}
                        </span>
                        <span className="mb-1 text-xs font-mono uppercase tracking-[0.18em] text-slate-500">
                          {stat.unit}
                        </span>
                      </div>
                    </div>
                  </div>
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
          <div className="mb-5 flex items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-emerald-400/16 bg-emerald-500/10">
                <RocketOutlined className="text-emerald-300" />
              </div>
              <div>
                <p className="font-mono text-[11px] uppercase tracking-[0.26em] text-slate-500">launch pads</p>
                <h2 className="text-lg font-semibold text-white">快捷入口</h2>
              </div>
            </div>
          </div>

          <Row gutter={[16, 16]}>
            {quickAccessCards.map((card, index) => (
              <Col xs={24} sm={12} lg={6} key={card.title}>
                <motion.button
                  type="button"
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.25 + index * 0.05 }}
                  whileTap={{ scale: 0.986, y: 1 }}
                  onClick={() => navigate(card.path)}
                  className={`group relative flex h-full w-full cursor-pointer flex-col overflow-hidden rounded-[24px] border border-white/[0.06] bg-slate-900/60 p-5 text-left backdrop-blur-xl transition-all duration-300 active:bg-slate-900/82 active:shadow-[0_10px_18px_rgba(2,6,23,0.24)] ${card.hoverBorder} ${card.hoverGlow} ${touchFeedbackClass}`}
                >
                  <div className={`absolute inset-x-6 top-0 h-px bg-gradient-to-r from-transparent ${card.accentLine} to-transparent`} />

                  {card.badge && (
                    <div className="absolute right-4 top-4 flex h-7 min-w-7 items-center justify-center rounded-full bg-amber-400 px-2 text-xs font-bold text-slate-950 shadow-[0_10px_24px_rgba(245,158,11,0.35)]">
                      {card.badge}
                    </div>
                  )}

                  <div className="flex items-start justify-between gap-4">
                    <div
                      className={`flex h-14 w-14 items-center justify-center rounded-[18px] border ${card.iconShell} ${card.iconColor} shadow-[0_16px_28px_rgba(2,6,23,0.22)] transition-all duration-300 group-hover:-translate-y-[3px] group-hover:brightness-110`}
                    >
                      {card.icon}
                    </div>
                    <span className="font-mono text-[11px] uppercase tracking-[0.22em] text-slate-600">
                      {card.kicker}
                    </span>
                  </div>

                  <div className="mt-9 flex flex-1 flex-col justify-end">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <h3 className="text-base font-semibold text-slate-100">{card.title}</h3>
                        <p className="mt-1.5 text-sm leading-6 text-slate-400">{card.desc}</p>
                      </div>
                      <ArrowRightOutlined
                        className={`mt-1 flex-shrink-0 text-slate-600 transition-all duration-300 group-hover:translate-x-1 ${card.hoverArrow}`}
                      />
                    </div>

                    <div
                      className={`mt-5 inline-flex items-center gap-2 self-start rounded-full border px-3 py-1.5 text-[11px] ${card.statusShell}`}
                    >
                      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-75" />
                      <span className="font-mono uppercase tracking-[0.18em]">jump in</span>
                    </div>
                  </div>
                </motion.button>
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
            <div className="mb-5 flex items-center justify-between gap-4">
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-violet-400/16 bg-violet-500/10">
                  <UserOutlined className="text-violet-300" />
                </div>
                <div>
                  <p className="font-mono text-[11px] uppercase tracking-[0.26em] text-slate-500">team signal</p>
                  <h2 className="text-lg font-semibold text-white">学生动态</h2>
                </div>
              </div>
              <Button
                type="text"
                className={`!h-10 !cursor-pointer !rounded-full !border !border-white/[0.08] !bg-white/[0.03] !px-4 !text-slate-300 hover:!border-violet-400/30 hover:!bg-violet-500/10 hover:!text-white active:!scale-[0.985] active:!border-violet-400/36 active:!bg-violet-500/16 ${touchFeedbackClass}`}
                onClick={() => setShowAllActivities(true)}
              >
                查看全部 <ArrowRightOutlined />
              </Button>
            </div>

            <Card className={`${sectionCardClass} [&_.ant-card-body]:!p-4 sm:[&_.ant-card-body]:!p-5`}>
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
          <div className="mb-5 flex items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-emerald-400/16 bg-emerald-500/10">
                <ClockCircleOutlined className="text-emerald-300" />
              </div>
              <div>
                <p className="font-mono text-[11px] uppercase tracking-[0.26em] text-slate-500">recent threads</p>
                <h2 className="text-lg font-semibold text-white">最近对话</h2>
              </div>
            </div>
            {conversations.length > 0 && (
              <Button
                type="text"
                className={`!h-10 !cursor-pointer !rounded-full !border !border-white/[0.08] !bg-white/[0.03] !px-4 !text-slate-300 hover:!border-emerald-400/30 hover:!bg-emerald-500/10 hover:!text-white active:!scale-[0.985] active:!border-emerald-400/36 active:!bg-emerald-500/16 ${touchFeedbackClass}`}
                onClick={() => setShowAllConversations(true)}
              >
                查看全部 <ArrowRightOutlined />
              </Button>
            )}
          </div>

          <Card className={`${sectionCardClass} [&_.ant-card-body]:!p-3 sm:[&_.ant-card-body]:!p-4`}>
            {conversations.length > 0 ? (
              <div className="space-y-1.5">
                {recentConversations.map((conv, index) => (
                  <motion.button
                    key={conv.id}
                    type="button"
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.35 + index * 0.05 }}
                    whileTap={{ scale: 0.988, y: 1 }}
                    onClick={() => navigate(`/chat/${conv.id}`)}
                    className={floatingListItemClass}
                  >
                    <div className="flex min-w-0 items-center gap-4">
                      <div className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-2xl border border-emerald-400/14 bg-emerald-500/10">
                        <MessageOutlined className="text-emerald-300" />
                      </div>
                      <div className="min-w-0">
                        <h4 className="truncate text-base font-medium text-slate-100 transition-colors group-hover:text-emerald-200">
                          {safeConversationTitle(conv.title)}
                        </h4>
                        <p className="mt-1 text-sm text-slate-500">
                          {conv.message_count || 0} 条消息 · {dayjs(conv.updated_at).fromNow()}
                        </p>
                      </div>
                    </div>
                    <ArrowRightOutlined className="flex-shrink-0 text-slate-600 transition-all duration-200 group-hover:translate-x-1 group-hover:text-emerald-300" />
                  </motion.button>
                ))}
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div className="text-slate-500">
                    <p>暂无对话记录</p>
                    <p className="mt-1 text-sm">开始你的第一次 AI 对话吧</p>
                  </div>
                }
                className="py-10"
              >
                <Button
                  type="primary"
                  onClick={() => navigate('/chat')}
                  className={`!mt-2 !h-10 !cursor-pointer !rounded-2xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 !px-5 hover:!from-emerald-400 hover:!to-cyan-400 active:!translate-y-px active:!scale-[0.985] active:!shadow-[0_8px_16px_rgba(16,185,129,0.22)] ${touchFeedbackClass}`}
                >
                  开始对话
                </Button>
              </Empty>
            )}
          </Card>
        </motion.div>

        <Modal
          title={
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-lg border border-violet-400/30 bg-violet-500/20">
                <UserOutlined className="text-sm text-violet-300" />
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
                className="!h-10 !cursor-pointer !rounded-xl !border-violet-400/40 !bg-violet-500/10 !text-violet-100 hover:!border-violet-300 hover:!text-white"
              >
                {mentorshipTargetLabel}
              </Button>
              <Button
                key="close"
                type="primary"
                onClick={() => setShowAllActivities(false)}
                className="!h-10 !cursor-pointer !rounded-xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 hover:!from-emerald-400 hover:!to-cyan-400"
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
              <span className="flex h-7 w-7 items-center justify-center rounded-lg border border-emerald-400/30 bg-emerald-500/20">
                <MessageOutlined className="text-sm text-emerald-300" />
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
                  navigate('/chat/manage')
                }}
                className="!h-10 !cursor-pointer !rounded-xl !border-emerald-400/40 !bg-emerald-500/10 !text-emerald-100 hover:!border-emerald-300 hover:!text-white"
              >
                前往聊天管理
              </Button>
              <Button
                key="close"
                type="primary"
                onClick={() => setShowAllConversations(false)}
                className="!h-10 !cursor-pointer !rounded-xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 hover:!from-emerald-400 hover:!to-cyan-400"
              >
                关闭
              </Button>
            </div>
          }
        >
          <div className="mb-3 rounded-xl border border-emerald-400/25 bg-emerald-500/10 px-3 py-2.5 text-xs text-emerald-100/90">
            可直接点击任一会话进入详情，或进入聊天管理页面统一维护历史会话。
          </div>
          {orderedConversations.length > 0 ? (
            <div className="max-h-[62vh] space-y-1.5 overflow-y-auto pr-1">
              {orderedConversations.map((conv) => (
                <button
                  key={conv.id}
                  type="button"
                  onClick={() => {
                    setShowAllConversations(false)
                    navigate(`/chat/${conv.id}`)
                  }}
                  className={`group flex w-full items-center justify-between rounded-2xl border border-white/[0.06] bg-slate-950/45 px-4 py-3 text-left transition-all duration-200 hover:border-emerald-400/25 hover:bg-slate-800/70 hover:shadow-[0_12px_24px_rgba(2,6,23,0.28)] active:scale-[0.988] active:border-emerald-400/28 active:bg-slate-800/78 active:shadow-[0_10px_20px_rgba(2,6,23,0.24)] ${touchFeedbackClass}`}
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium text-slate-100 transition-colors group-hover:text-emerald-200">
                      {safeConversationTitle(conv.title)}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      {conv.message_count || 0} 条消息 · {dayjs(conv.updated_at).fromNow()}
                    </p>
                  </div>
                  <ArrowRightOutlined className="ml-4 flex-shrink-0 text-slate-600 transition-all duration-200 group-hover:translate-x-1 group-hover:text-emerald-300" />
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
