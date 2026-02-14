import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'

import { useChatStore } from '@/stores/chatStore'
import { ChatMessages, ChatInput } from './components'

/**
 * ChatPage - 聊天主页面（重构版）
 *
 * 原 1104 行单体组件已拆分为:
 *   - ChatMessages   消息列表区域（含流式、ReAct 面板）
 *   - ChatInput      输入区域
 *   - MessageBubble  消息气泡
 *   - ReActPanel     实时推理面板
 *   - HistoryReActPanel 历史推理面板
 *   - ThinkingPanel  思考面板
 *   - CodeBlock      代码块
 *   - EmptyState     空状态欢迎页
 *
 * 本文件仅保留路由参数处理、store 连接、页面级副作用。
 */
const ChatPage = () => {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const initialMessageSent = useRef(false)

  const {
    messages,
    currentConversation,
    isLoading,
    isSending,
    isThinking,
    streamingContent,
    streamingThought,
    iterationSteps,
    currentIteration,
    currentToolCall,
    selectConversation,
    sendMessage,
    stopGeneration,
    clearCurrentConversation,
  } = useChatStore()

  const [inputValue, setInputValue] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [highlightedMessageId, setHighlightedMessageId] = useState<number | null>(null)
  const [conversationLoaded, setConversationLoaded] = useState(false)

  // ─── 加载对话 ───────────────────────────────────
  useEffect(() => {
    const loadConversation = async () => {
      if (isSending) {
        setConversationLoaded(true)
        return
      }

      setConversationLoaded(false)
      if (conversationId) {
        setLoadError(null)
        try {
          await selectConversation(parseInt(conversationId))
          setLoadError(null)
          setConversationLoaded(true)
        } catch (error: any) {
          console.error('加载对话失败:', error)
          if (error?.response?.status === 404) {
            setLoadError('对话不存在或已被删除')
          } else if (error?.response?.status === 401) {
            setLoadError('登录已过期，请重新登录')
          } else {
            setLoadError('加载对话失败，请刷新重试')
          }
        }
      } else {
        setLoadError(null)
        clearCurrentConversation()
        setConversationLoaded(true)
      }
    }

    loadConversation()
  }, [clearCurrentConversation, conversationId, isSending, selectConversation])

  // ─── 处理首页传来的初始消息 / 消息高亮 ──────────
  useEffect(() => {
    const state = location.state as {
      initialMessage?: string
      highlightMessageId?: number
    } | null

    // 处理初始消息
    if (
      state?.initialMessage &&
      conversationId &&
      conversationLoaded &&
      !initialMessageSent.current &&
      !isSending
    ) {
      initialMessageSent.current = true
      sendMessage(state.initialMessage).catch((err) => {
        console.error('发送初始消息失败:', err)
        // Error handled by store
        initialMessageSent.current = false
      })
      navigate(location.pathname, { replace: true, state: {} })
    }

    // 处理消息高亮
    if (state?.highlightMessageId && conversationLoaded && messages.length > 0) {
      setHighlightedMessageId(state.highlightMessageId)
      navigate(location.pathname, { replace: true, state: {} })

      setTimeout(() => {
        const el = document.getElementById(`message-${state.highlightMessageId}`)
        el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 100)

      setTimeout(() => setHighlightedMessageId(null), 3000)
    }
  }, [conversationId, conversationLoaded, isSending, location.pathname, location.state, messages.length, navigate, sendMessage])

  // 重置 initialMessageSent 当 conversationId 改变时
  useEffect(() => {
    initialMessageSent.current = false
  }, [conversationId])

  // ─── 操作回调 ──────────────────────────────────
  const handleReload = async () => {
    if (conversationId) {
      setLoadError(null)
      try {
        await selectConversation(parseInt(conversationId))
      } catch {
        setLoadError('加载对话失败，请刷新重试')
      }
    }
  }

  const handleSend = async (content?: string) => {
    const messageContent = content || inputValue.trim()
    if (!messageContent || isSending) return

    setInputValue('')

    try {
      const newConvId = await sendMessage(messageContent)
      if (newConvId && !conversationId) {
        navigate(`/chat/${newConvId}`, { replace: true })
      }
    } catch {
      // Error handled by store
    }
  }

  const handleQuickPrompt = (prompt: string) => {
    setInputValue(prompt)
  }

  // ─── 渲染 ──────────────────────────────────────
  return (
    <div className="h-full flex flex-col bg-gradient-to-b from-slate-900 via-slate-900 to-slate-950">
      <ChatMessages
        messages={messages}
        currentConversation={currentConversation}
        isLoading={isLoading}
        loadError={loadError}
        isSending={isSending}
        isThinking={isThinking}
        streamingContent={streamingContent}
        streamingThought={streamingThought}
        iterationSteps={iterationSteps}
        currentIteration={currentIteration}
        currentToolCall={currentToolCall}
        highlightedMessageId={highlightedMessageId}
        onQuickPrompt={handleQuickPrompt}
        onReload={handleReload}
      />

      <ChatInput
        inputValue={inputValue}
        isSending={isSending}
        llmProvider={currentConversation?.llm_provider}
        onInputChange={setInputValue}
        onSend={() => handleSend()}
        onStop={stopGeneration}
      />
    </div>
  )
}

export default ChatPage
