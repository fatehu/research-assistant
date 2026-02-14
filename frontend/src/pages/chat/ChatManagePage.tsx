import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Empty, Input, Modal, Spin, message } from 'antd'
import {
  ArrowRightOutlined,
  DeleteOutlined,
  MessageOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'
import { useChatStore } from '@/stores/chatStore'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

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
    <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-900 to-slate-950">
      <div className="max-w-5xl mx-auto p-6 space-y-6">
        <div className="rounded-2xl border border-emerald-500/20 bg-slate-900/70 backdrop-blur-sm p-5">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-3">
              <span className="w-10 h-10 rounded-xl bg-emerald-500/20 border border-emerald-400/30 flex items-center justify-center">
                <MessageOutlined className="text-emerald-300 text-lg" />
              </span>
              <div>
                <h1 className="text-xl font-semibold text-slate-100">聊天管理</h1>
                <p className="text-sm text-slate-400">查看、检索并管理全部历史对话</p>
              </div>
            </div>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => navigate('/chat')}
              className="!h-10 !rounded-xl !border-0 !bg-gradient-to-r !from-emerald-500 !to-cyan-500 hover:!from-emerald-400 hover:!to-cyan-400"
            >
              新建对话
            </Button>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-700/70 bg-slate-900/65 p-4">
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            prefix={<SearchOutlined className="text-slate-500" />}
            placeholder="搜索会话标题"
            allowClear
            className="!h-11 !rounded-xl !bg-slate-900/80 !border-slate-700/80 !text-slate-200"
          />
        </div>

        <div className="rounded-2xl border border-slate-700/70 bg-slate-900/65 p-3">
          {isLoadingList ? (
            <div className="py-16 text-center">
              <Spin />
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="py-12">
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
            <div className="space-y-2">
              {filteredConversations.map((conv) => (
                <div
                  key={conv.id}
                  className="group flex items-center justify-between gap-3 rounded-xl border border-slate-700/70 bg-slate-900/75 hover:border-emerald-400/35 hover:bg-slate-800/90 transition-all px-4 py-3"
                >
                  <button
                    type="button"
                    onClick={() => navigate(`/chat/${conv.id}`)}
                    className="flex-1 min-w-0 text-left"
                  >
                    <p className="text-sm font-medium text-slate-100 truncate">
                      {safeConversationTitle(conv.title)}
                    </p>
                    <p className="text-xs text-slate-400 mt-1">
                      {conv.message_count || 0} 条消息 · {dayjs(conv.updated_at).fromNow()}
                    </p>
                  </button>
                  <div className="flex items-center gap-2">
                    <Button
                      type="text"
                      icon={<ArrowRightOutlined />}
                      onClick={() => navigate(`/chat/${conv.id}`)}
                      className="!text-emerald-300 hover:!text-emerald-200 hover:!bg-emerald-500/10"
                    />
                    <Button
                      type="text"
                      danger
                      loading={deletingId === conv.id}
                      icon={<DeleteOutlined />}
                      onClick={() => handleDelete(conv.id, conv.title)}
                      className="hover:!bg-red-500/10"
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default ChatManagePage
