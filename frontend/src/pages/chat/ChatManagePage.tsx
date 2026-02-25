import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Empty, Input, Modal, Spin, message } from 'antd'
import {
  ArrowRightOutlined,
  CalendarOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  FireOutlined,
  MessageOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'
import { useChatStore } from '@/stores/chatStore'
import type { Conversation } from '@/services/api'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

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

const getPreviewText = (value?: string | null, maxLength = 86) => {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

const formatAbsoluteTime = (value?: string | null) => {
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm') : '--'
}

const formatRelativeTime = (value?: string | null) => {
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed.fromNow() : '时间未知'
}

type ConversationGroup = {
  key: 'today' | 'yesterday' | 'week' | 'older'
  title: string
  hint: string
  items: Conversation[]
}

const ChatManagePage = () => {
  const navigate = useNavigate()
  const { conversations, isLoadingList, fetchConversations, deleteConversation } = useChatStore()

  const [keyword, setKeyword] = useState('')
  const [deletingId, setDeletingId] = useState<number | null>(null)

  useEffect(() => {
    fetchConversations()
  }, [fetchConversations])

  const orderedConversations = useMemo(
    () =>
      [...conversations].sort(
        (a, b) => dayjs(b.updated_at).valueOf() - dayjs(a.updated_at).valueOf()
      ),
    [conversations]
  )

  const filteredConversations = useMemo(() => {
    const text = keyword.trim().toLowerCase()
    if (!text) return orderedConversations
    return orderedConversations.filter((conv) =>
      safeConversationTitle(conv.title).toLowerCase().includes(text)
    )
  }, [keyword, orderedConversations])

  const overview = useMemo(() => {
    const totalConversations = orderedConversations.length
    const totalMessages = orderedConversations.reduce(
      (sum, conv) => sum + Number(conv.message_count || 0),
      0
    )
    const weekBaseline = dayjs().subtract(7, 'day')
    const weekActiveCount = orderedConversations.filter((conv) => {
      const updated = dayjs(conv.updated_at)
      return updated.isValid() && (updated.isAfter(weekBaseline) || updated.isSame(weekBaseline))
    }).length
    const latestUpdated =
      orderedConversations.length > 0 ? formatAbsoluteTime(orderedConversations[0].updated_at) : '--'
    return {
      totalConversations,
      totalMessages,
      weekActiveCount,
      latestUpdated,
    }
  }, [orderedConversations])

  const groupedConversations = useMemo<ConversationGroup[]>(() => {
    const todayStart = dayjs().startOf('day')
    const yesterdayStart = todayStart.subtract(1, 'day')
    const weekStart = todayStart.subtract(7, 'day')

    const groups: ConversationGroup[] = [
      { key: 'today', title: '今天', hint: '最近 24 小时', items: [] },
      { key: 'yesterday', title: '昨天', hint: '前 1 天', items: [] },
      { key: 'week', title: '近 7 天', hint: '最近一周', items: [] },
      { key: 'older', title: '更早', hint: '历史会话', items: [] },
    ]

    filteredConversations.forEach((conv) => {
      const updated = dayjs(conv.updated_at)
      if (!updated.isValid()) {
        groups[3].items.push(conv)
        return
      }
      if (updated.isAfter(todayStart) || updated.isSame(todayStart)) {
        groups[0].items.push(conv)
        return
      }
      if (updated.isAfter(yesterdayStart) || updated.isSame(yesterdayStart)) {
        groups[1].items.push(conv)
        return
      }
      if (updated.isAfter(weekStart) || updated.isSame(weekStart)) {
        groups[2].items.push(conv)
        return
      }
      groups[3].items.push(conv)
    })

    return groups.filter((group) => group.items.length > 0)
  }, [filteredConversations])

  const handleDelete = (conversationId: number, title?: string | null) => {
    Modal.confirm({
      title: '删除对话',
      content: `确定删除「${safeConversationTitle(title)}」吗？此操作不可撤销。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          setDeletingId(conversationId)
          await deleteConversation(conversationId)
          message.success('对话已删除')
        } catch {
          message.error('删除失败，请稍后重试')
        } finally {
          setDeletingId(null)
        }
      },
    })
  }

  return (
    <div className="relative h-full overflow-y-auto bg-slate-950">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(110%_80%_at_50%_0%,rgba(16,185,129,0.08),transparent_58%)]" />

      <div className="relative mx-auto w-full max-w-6xl space-y-5 px-4 py-5 sm:px-6 sm:py-6 lg:px-8">
        <div className="animate-fade-in rounded-2xl border border-slate-700/70 bg-slate-900/70 p-5 shadow-[0_10px_26px_rgba(2,6,23,0.4)] sm:p-6">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex items-start gap-4">
              <span className="mt-0.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-emerald-400/30 bg-emerald-500/10">
                <MessageOutlined className="text-lg text-emerald-100" />
              </span>
              <div>
                <h1 className="text-2xl font-semibold tracking-tight text-slate-50">聊天管理</h1>
                <p className="mt-1 text-sm text-slate-300">
                  统一查看全部历史会话，快速检索并清理无效对话。
                </p>
              </div>
            </div>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => navigate('/chat')}
              className="!h-11 !rounded-xl !border-0 !px-5 !font-medium !bg-gradient-to-r !from-emerald-500 !to-emerald-600 hover:!from-emerald-400 hover:!to-emerald-500"
            >
              新建对话
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <div className="animate-slide-up rounded-2xl border border-slate-700/70 bg-slate-900/70 px-4 py-4 backdrop-blur-sm transition-all duration-300 hover:-translate-y-0.5 hover:border-emerald-400/35 hover:shadow-[0_14px_28px_rgba(16,185,129,0.14)]">
            <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
              <MessageOutlined className="text-emerald-300" />
              总会话数
            </p>
            <p className="mt-2 text-2xl font-semibold text-slate-50">{overview.totalConversations}</p>
          </div>
          <div className="animate-slide-up rounded-2xl border border-slate-700/70 bg-slate-900/70 px-4 py-4 backdrop-blur-sm transition-all duration-300 hover:-translate-y-0.5 hover:border-emerald-400/35 hover:shadow-[0_14px_28px_rgba(16,185,129,0.14)]">
            <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
              <FireOutlined className="text-cyan-300" />
              消息总量
            </p>
            <p className="mt-2 text-2xl font-semibold text-slate-50">{overview.totalMessages}</p>
          </div>
          <div className="animate-slide-up rounded-2xl border border-slate-700/70 bg-slate-900/70 px-4 py-4 backdrop-blur-sm transition-all duration-300 hover:-translate-y-0.5 hover:border-emerald-400/35 hover:shadow-[0_14px_28px_rgba(16,185,129,0.14)]">
            <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
              <ClockCircleOutlined className="text-emerald-300" />
              近 7 天活跃
            </p>
            <p className="mt-2 text-2xl font-semibold text-slate-50">{overview.weekActiveCount}</p>
          </div>
          <div className="animate-slide-up rounded-2xl border border-slate-700/70 bg-slate-900/70 px-4 py-4 backdrop-blur-sm transition-all duration-300 hover:-translate-y-0.5 hover:border-emerald-400/35 hover:shadow-[0_14px_28px_rgba(16,185,129,0.14)]">
            <p className="flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
              <CalendarOutlined className="text-cyan-300" />
              最近更新时间
            </p>
            <p className="mt-2 truncate text-sm font-semibold text-slate-100">
              {overview.latestUpdated}
            </p>
          </div>
        </div>

        <div className="rounded-3xl border border-slate-700/70 bg-slate-900/70 p-4 backdrop-blur-sm">
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            prefix={<SearchOutlined className="text-slate-500" />}
            placeholder="搜索会话标题"
            allowClear
            className="!h-11 !rounded-xl !border-slate-600/70 !bg-slate-950/70 !text-slate-100 hover:!border-emerald-400/30"
          />
          <p className="mt-3 text-xs text-slate-400">
            {keyword.trim()
              ? `匹配 ${filteredConversations.length} / ${orderedConversations.length} 条会话`
              : `共 ${orderedConversations.length} 条会话`}
          </p>
        </div>

        <div className="rounded-3xl border border-slate-700/70 bg-slate-900/70 p-3 sm:p-4 backdrop-blur-sm">
          {isLoadingList ? (
            <div className="py-16 text-center">
              <Spin />
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="py-14">
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  keyword.trim() ? (
                    <span className="text-slate-500">没有匹配的对话</span>
                  ) : (
                    <span className="text-slate-500">暂无历史对话</span>
                  )
                }
              />
            </div>
          ) : (
            <div className="space-y-5">
              {groupedConversations.map((group) => (
                <section key={group.key} className="space-y-2">
                  <div className="flex items-center justify-between px-2">
                    <div className="flex items-center gap-2">
                      <span className="rounded-full border border-emerald-500/25 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-200">
                        {group.title}
                      </span>
                      <span className="text-xs text-slate-500">{group.hint}</span>
                    </div>
                    <span className="text-xs text-slate-500">{group.items.length} 条</span>
                  </div>

                  <div className="space-y-2">
                    {group.items.map((conv, index) => (
                      <article
                        key={conv.id}
                        style={{ animationDelay: `${Math.min(index, 10) * 35}ms` }}
                        className="group animate-slide-up rounded-2xl border border-slate-700/70 bg-slate-900/65 px-4 py-3 transition-all duration-300 hover:-translate-y-0.5 hover:border-emerald-400/35 hover:bg-slate-800/75 hover:shadow-[0_12px_24px_rgba(2,6,23,0.45)]"
                      >
                        <div className="flex items-start gap-3">
                          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-emerald-500/30 bg-emerald-500/10">
                            <MessageOutlined className="text-sm text-emerald-200" />
                          </span>

                          <button
                            type="button"
                            onClick={() => navigate(`/chat/${conv.id}`)}
                            className="m-0 flex-1 min-w-0 cursor-pointer appearance-none border-0 bg-transparent p-0 text-left"
                          >
                            <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
                              <p className="truncate text-sm font-semibold text-slate-100">
                                {safeConversationTitle(conv.title)}
                              </p>
                              <p className="whitespace-nowrap text-xs text-slate-500">
                                {formatRelativeTime(conv.updated_at)}
                              </p>
                            </div>
                            <p className="mt-1 truncate text-xs text-slate-400">
                              {getPreviewText(conv.last_message) || '暂无消息摘要'}
                            </p>
                            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                              <span className="rounded-full border border-slate-600/70 bg-slate-900/80 px-2.5 py-1 text-slate-300">
                                {conv.message_count || 0} 条消息
                              </span>
                              <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1 text-slate-400">
                                {formatAbsoluteTime(conv.updated_at)}
                              </span>
                            </div>
                          </button>

                          <div className="flex shrink-0 items-center gap-1">
                            <Button
                              type="text"
                              size="small"
                              icon={<ArrowRightOutlined />}
                              onClick={() => navigate(`/chat/${conv.id}`)}
                              className="!text-emerald-300 hover:!text-emerald-200 hover:!bg-emerald-500/10"
                            />
                            <Button
                              type="text"
                              size="small"
                              danger
                              loading={deletingId === conv.id}
                              icon={<DeleteOutlined />}
                              onClick={() => handleDelete(conv.id, conv.title)}
                              className="hover:!bg-red-500/10"
                            />
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default ChatManagePage
