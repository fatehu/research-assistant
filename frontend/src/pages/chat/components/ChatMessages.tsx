import { useRef, useEffect } from 'react'
import { Spin, Button } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { AnimatePresence } from 'framer-motion'
import type {
  Message,
  Conversation,
  ConversationItemStream,
  ConversationToolLedger,
  ConversationTurnStore,
  MessageSpanRewriteResponse,
} from '@/services/api'
import type { IterationStep, SendPhase } from '@/stores/chatStore'
import MessageBubble from './MessageBubble'
import EmptyState from './EmptyState'
import TurnTimeline from './TurnTimeline'

interface ChatMessagesProps {
  messages: Message[]
  currentConversation: Conversation | null
  isLoading: boolean
  loadError: string | null
  isSending: boolean
  sendPhase: SendPhase
  sendPhaseLabel?: string | null
  sendPhaseHint?: string | null
  isThinking: boolean
  streamingContent: string
  streamingThought: string
  iterationSteps: IterationStep[]
  currentIteration: number
  currentToolCall: { tool: string; input: Record<string, any> } | null
  currentTurnId: string | null
  highlightedMessageId: number | null
  onRewriteSpan: (
    messageId: number,
    payload: {
      instruction: string
      selected_text: string
      before_context?: string
      after_context?: string
      occurrence_index?: number
    },
  ) => Promise<MessageSpanRewriteResponse>
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
  sendPhase,
  sendPhaseLabel,
  sendPhaseHint,
  isThinking,
  streamingContent,
  streamingThought,
  iterationSteps,
  currentIteration,
  currentToolCall,
  currentTurnId,
  highlightedMessageId,
  onRewriteSpan,
  onQuickPrompt,
  onReload,
}: ChatMessagesProps) => {
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const lastAutoScrollStateRef = useRef<{ count: number; lastMessageId: number | null }>({
    count: 0,
    lastMessageId: null,
  })

  // 自动滚动
  useEffect(() => {
    const lastMessageId = messages.length > 0 ? messages[messages.length - 1].id : null
    const previous = lastAutoScrollStateRef.current
    const hasNewMessage = previous.count !== messages.length || previous.lastMessageId !== lastMessageId
    lastAutoScrollStateRef.current = {
      count: messages.length,
      lastMessageId,
    }

    if (hasNewMessage || isSending || streamingContent) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [isSending, messages, streamingContent])

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
            {currentConversation?.turn_store?.entries?.length || isSending ? (
              <TurnTimeline
                messages={messages}
                turnStore={currentConversation?.turn_store as ConversationTurnStore | undefined}
                itemStream={currentConversation?.item_stream as ConversationItemStream | undefined}
                toolLedger={currentConversation?.tool_ledger as ConversationToolLedger | undefined}
                highlightedMessageId={highlightedMessageId}
                activeTurnId={currentTurnId}
                isSending={isSending}
                sendPhase={sendPhase}
                sendPhaseLabel={sendPhaseLabel}
                sendPhaseHint={sendPhaseHint}
                isThinking={isThinking}
                streamingContent={streamingContent}
                streamingThought={streamingThought}
                iterationSteps={iterationSteps}
                currentIteration={currentIteration}
                currentToolCall={currentToolCall}
                onRewriteSpan={onRewriteSpan}
              />
            ) : (
              <AnimatePresence mode="popLayout">
                {messages.map((msg, idx) => (
                  <MessageBubble
                    key={msg.id || idx}
                    msg={msg}
                    turnStore={currentConversation?.turn_store as ConversationTurnStore | undefined}
                    itemStream={currentConversation?.item_stream as ConversationItemStream | undefined}
                    toolLedger={currentConversation?.tool_ledger as ConversationToolLedger | undefined}
                    isHighlighted={highlightedMessageId === msg.id}
                    onRewriteSpan={onRewriteSpan}
                  />
                ))}
              </AnimatePresence>
            )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>
    </div>
  )
}

export default ChatMessages
