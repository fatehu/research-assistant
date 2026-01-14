import { useState, useEffect, useRef } from 'react'
import { useParams, useLocation } from 'react-router-dom'
import { Input, Button, Spin, Empty, Collapse, Tag, message, Tooltip } from 'antd'
import {
  SendOutlined,
  RobotOutlined,
  UserOutlined,
  BulbOutlined,
  ThunderboltOutlined,
  EyeOutlined,
  LoadingOutlined,
  CopyOutlined,
  ReloadOutlined,
  DeleteOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { useChatStore } from '@/stores/chatStore'
import type { Message } from '@/services/api'

const { TextArea } = Input

// Markdown 代码块渲染组件
const CodeBlock = ({ className, children }: { className?: string; children: React.ReactNode }) => {
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const code = String(children).replace(/\n$/, '')
  
  const handleCopy = () => {
    navigator.clipboard.writeText(code)
    message.success('代码已复制')
  }
  
  return match ? (
    <div className="relative group my-4">
      <div className="absolute right-2 top-2 opacity-0 group-hover:opacity-100 transition-opacity z-10">
        <Button
          size="small"
          icon={<CopyOutlined />}
          onClick={handleCopy}
          className="text-gray-400 hover:text-white"
        />
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{
          borderRadius: '0.5rem',
          padding: '1rem',
          fontSize: '0.875rem',
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  ) : (
    <code className="bg-gray-800 px-1.5 py-0.5 rounded text-blue-300 text-sm">
      {children}
    </code>
  )
}

// ReAct 过程展示组件
const ReActProcess = ({
  thought,
  action,
  observation,
  isStreaming = false,
}: {
  thought?: string
  action?: string
  observation?: string
  isStreaming?: boolean
}) => {
  if (!thought && !action && !observation) return null
  
  const items = []
  
  if (thought) {
    items.push({
      key: 'thought',
      label: (
        <div className="flex items-center gap-2 text-yellow-400">
          <BulbOutlined />
          <span>思考过程</span>
          {isStreaming && <LoadingOutlined className="text-xs" />}
        </div>
      ),
      children: (
        <div className="text-gray-300 text-sm whitespace-pre-wrap">
          {thought}
        </div>
      ),
    })
  }
  
  if (action) {
    items.push({
      key: 'action',
      label: (
        <div className="flex items-center gap-2 text-blue-400">
          <ThunderboltOutlined />
          <span>执行动作</span>
        </div>
      ),
      children: (
        <div className="text-gray-300 text-sm">
          <Tag color="blue">{action}</Tag>
        </div>
      ),
    })
  }
  
  if (observation) {
    items.push({
      key: 'observation',
      label: (
        <div className="flex items-center gap-2 text-green-400">
          <EyeOutlined />
          <span>观察结果</span>
        </div>
      ),
      children: (
        <div className="text-gray-300 text-sm whitespace-pre-wrap">
          {observation}
        </div>
      ),
    })
  }
  
  return (
    <Collapse
      items={items}
      defaultActiveKey={isStreaming ? ['thought'] : []}
      ghost
      size="small"
      className="react-process-collapse mb-3"
    />
  )
}

// 消息气泡组件
const MessageBubble = ({
  message,
  isStreaming = false,
  streamingContent = '',
  streamingThought = '',
  streamingAction = '',
}: {
  message: Message
  isStreaming?: boolean
  streamingContent?: string
  streamingThought?: string
  streamingAction?: string
}) => {
  const isUser = message.role === 'user'
  const content = isStreaming ? streamingContent : message.content
  const thought = isStreaming ? streamingThought : message.thought
  const action = isStreaming ? streamingAction : message.action
  
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex gap-3 ${isUser ? 'flex-row-reverse' : ''}`}
    >
      {/* 头像 */}
      <div
        className={`w-8 h-8 rounded-lg flex-shrink-0 flex items-center justify-center ${
          isUser
            ? 'bg-gradient-to-br from-blue-500 to-purple-500'
            : 'bg-gradient-to-br from-green-500 to-teal-500'
        }`}
      >
        {isUser ? (
          <UserOutlined className="text-white text-sm" />
        ) : (
          <RobotOutlined className="text-white text-sm" />
        )}
      </div>
      
      {/* 消息内容 */}
      <div
        className={`max-w-[80%] ${isUser ? 'items-end' : 'items-start'}`}
      >
        {/* ReAct 过程 */}
        {!isUser && (thought || action || message.observation) && (
          <ReActProcess
            thought={thought}
            action={action}
            observation={message.observation}
            isStreaming={isStreaming}
          />
        )}
        
        {/* 消息内容 */}
        <div
          className={`rounded-2xl px-4 py-3 ${
            isUser
              ? 'bg-gradient-to-br from-blue-500 to-blue-600 text-white'
              : 'glass-card'
          }`}
        >
          {isUser ? (
            <p className="text-sm whitespace-pre-wrap">{content}</p>
          ) : (
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown
                components={{
                  code: ({ className, children }) => (
                    <CodeBlock className={className}>{children}</CodeBlock>
                  ),
                }}
              >
                {content || (isStreaming ? '正在思考...' : '')}
              </ReactMarkdown>
              {isStreaming && content && (
                <span className="inline-block w-2 h-4 bg-blue-400 animate-pulse ml-1" />
              )}
            </div>
          )}
        </div>
        
        {/* 时间戳 */}
        <div
          className={`text-xs text-gray-600 mt-1 ${
            isUser ? 'text-right' : 'text-left'
          }`}
        >
          {new Date(message.created_at).toLocaleTimeString('zh-CN', {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </div>
      </div>
    </motion.div>
  )
}

// 主聊天页面
const ChatPage = () => {
  const { conversationId } = useParams()
  const location = useLocation()
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  
  const {
    messages,
    currentConversation,
    isLoading,
    isSending,
    streamingContent,
    streamingThought,
    streamingAction,
    selectConversation,
    createConversation,
    sendMessage,
  } = useChatStore()
  
  const [inputValue, setInputValue] = useState('')
  
  // 加载对话
  useEffect(() => {
    if (conversationId) {
      selectConversation(parseInt(conversationId))
    }
  }, [conversationId, selectConversation])
  
  // 处理初始消息（从 Dashboard 快速输入跳转）
  useEffect(() => {
    const initialMessage = location.state?.initialMessage
    if (initialMessage && conversationId) {
      handleSend(initialMessage)
      // 清除 state 防止重复发送
      window.history.replaceState({}, document.title)
    }
  }, [conversationId, location.state])
  
  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])
  
  // 发送消息
  const handleSend = async (content?: string) => {
    const messageContent = content || inputValue.trim()
    if (!messageContent || isSending) return
    
    setInputValue('')
    
    try {
      await sendMessage(messageContent)
    } catch (error) {
      message.error('发送失败，请重试')
    }
  }
  
  // 处理按键
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }
  
  // 复制消息
  const handleCopyMessage = (content: string) => {
    navigator.clipboard.writeText(content)
    message.success('已复制到剪贴板')
  }
  
  return (
    <div className="h-full flex flex-col">
      {/* 消息列表区域 */}
      <div className="flex-1 overflow-y-auto px-4 py-6">
        <div className="max-w-4xl mx-auto space-y-6">
          {isLoading ? (
            <div className="flex justify-center py-12">
              <Spin size="large" tip="加载中..." />
            </div>
          ) : messages.length === 0 ? (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex flex-col items-center justify-center py-20"
            >
              <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-blue-500 to-purple-500 flex items-center justify-center mb-6 shadow-lg shadow-blue-500/25">
                <RobotOutlined className="text-4xl text-white" />
              </div>
              <h2 className="text-2xl font-bold text-white mb-2">
                开始新对话
              </h2>
              <p className="text-gray-400 text-center max-w-md mb-6">
                我是你的科研助手，可以帮助你解答问题、分析数据、撰写论文。
                试着问我任何问题吧！
              </p>
              
              {/* 快捷提问 */}
              <div className="grid grid-cols-2 gap-3 max-w-lg">
                {[
                  '解释什么是注意力机制？',
                  '如何提高模型训练效率？',
                  '帮我设计一个实验方案',
                  '综述深度学习最新进展',
                ].map((prompt, index) => (
                  <motion.button
                    key={index}
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ delay: index * 0.1 }}
                    onClick={() => setInputValue(prompt)}
                    className="p-3 rounded-xl glass-card text-sm text-gray-300 hover:text-white hover:border-blue-500/50 transition-all text-left"
                  >
                    {prompt}
                  </motion.button>
                ))}
              </div>
            </motion.div>
          ) : (
            <>
              <AnimatePresence>
                {messages.map((msg, index) => (
                  <MessageBubble key={msg.id || index} message={msg} />
                ))}
              </AnimatePresence>
              
              {/* 流式响应 */}
              {isSending && (
                <MessageBubble
                  message={{
                    id: -1,
                    conversation_id: currentConversation?.id || 0,
                    role: 'assistant',
                    content: streamingContent,
                    message_type: 'text',
                    thought: streamingThought,
                    action: streamingAction,
                    created_at: new Date().toISOString(),
                  }}
                  isStreaming={true}
                  streamingContent={streamingContent}
                  streamingThought={streamingThought}
                  streamingAction={streamingAction}
                />
              )}
            </>
          )}
          <div ref={messagesEndRef} />
        </div>
      </div>
      
      {/* 输入区域 */}
      <div className="border-t border-white/5 bg-gradient-to-t from-slate-900/50 to-transparent backdrop-blur-xl">
        <div className="max-w-4xl mx-auto p-4">
          <div className="relative">
            <TextArea
              ref={inputRef as any}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入你的问题... (Enter 发送，Shift+Enter 换行)"
              autoSize={{ minRows: 1, maxRows: 6 }}
              className="pr-24 text-base resize-none"
              disabled={isSending}
            />
            <div className="absolute right-2 bottom-2 flex items-center gap-2">
              {inputValue.trim() && (
                <Tooltip title="清空">
                  <Button
                    type="text"
                    size="small"
                    icon={<DeleteOutlined />}
                    onClick={() => setInputValue('')}
                    className="text-gray-500 hover:text-white"
                  />
                </Tooltip>
              )}
              <Button
                type="primary"
                icon={isSending ? <LoadingOutlined /> : <SendOutlined />}
                onClick={() => handleSend()}
                disabled={!inputValue.trim() || isSending}
              >
                发送
              </Button>
            </div>
          </div>
          
          {/* 提示信息 */}
          <div className="flex items-center justify-between mt-2 text-xs text-gray-600">
            <span>
              当前模型: {currentConversation?.llm_model || 'DeepSeek V3'}
            </span>
            <span>
              {messages.length > 0 && `共 ${messages.length} 条消息`}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default ChatPage
