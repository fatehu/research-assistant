import { useRef, useEffect } from 'react'
import { Spin, Button } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { AnimatePresence } from 'framer-motion'
import type { Message, Conversation } from '@/services/api'
import type { IterationStep } from '@/stores/chatStore'
import MessageBubble from './MessageBubble'
import ReActPanel from './ReActPanel'
import EmptyState from './EmptyState'

interface ChatMessagesProps {
  messages: Message[]
  currentConversation: Conversation | null
  isLoading: boolean
  loadError: string | null
  isSending: boolean
  isThinking: boolean
  streamingContent: string
  streamingThought: string
  iterationSteps: IterationStep[]
  currentIteration: number
  currentToolCall: { tool: string; input: Record<string, any> } | null
  highlightedMessageId: number | null
  onQuickPrompt: (prompt: string) => void
  onReload: () => void
}

/** 消息列表区域 */
const ChatMessages = ({
  messages,
  currentConversation,
  isLoading,
  loadError,
  isSending,
  isThinking,
  streamingContent,
  streamingThought,
  iterationSteps,
  currentIteration,
  currentToolCall,
  highlightedMessageId,
  onQuickPrompt,
  onReload,
}: ChatMessagesProps) => {
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // 自动滚动
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  return (
    <div className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
      <div className="mx-auto max-w-[1040px] px-4 py-8 sm:px-6 lg:px-8">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center py-20">
            <Spin size="large" />
            <p className="text-slate-500 mt-4">加载对话中...</p>
          </div>
        ) : loadError ? (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="text-red-400 mb-4">{loadError}</div>
            <Button onClick={onReload} icon={<ReloadOutlined />}>
              重新加载
            </Button>
          </div>
        ) : messages.length === 0 ? (
          <EmptyState onQuickPrompt={onQuickPrompt} />
        ) : (
          <div className="space-y-7">
            <AnimatePresence mode="popLayout">
              {messages.map((msg, idx) => (
                <MessageBubble
                  key={msg.id || idx}
                  msg={msg}
                  isHighlighted={highlightedMessageId === msg.id}
                />
              ))}
            </AnimatePresence>

            {/* 流式响应 */}
            {isSending && (
              <div>
                {/* ReAct 推理过程面板 */}
                <ReActPanel
                  steps={iterationSteps}
                  currentIteration={currentIteration}
                  isThinking={isThinking}
                  currentThought={streamingThought}
                  currentToolCall={currentToolCall}
                />

                {/* 只有当有内容时才显示消息气泡 */}
                {(streamingContent ||
                  (!isThinking && !currentToolCall && iterationSteps.length === 0)) && (
                  <MessageBubble
                    msg={{
                      id: -1,
                      conversation_id: currentConversation?.id || 0,
                      role: 'assistant',
                      content: streamingContent,
                      message_type: 'text',
                      created_at: new Date().toISOString(),
                    }}
                    isStreaming={true}
                    streamingContent={streamingContent}
                    streamingThought=""
                    isThinking={false}
                  />
                )}
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>
    </div>
  )
}

export default ChatMessages
