import { useState, useEffect, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { Input, Button, Spin, message, Tooltip, Avatar } from 'antd'
import {
  SendOutlined,
  RobotOutlined,
  UserOutlined,
  BulbOutlined,
  LoadingOutlined,
  CopyOutlined,
  ReloadOutlined,
  ExpandOutlined,
  CompressOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import remarkGfm from 'remark-gfm'
import { useChatStore } from '@/stores/chatStore'
import type { Message } from '@/services/api'

const { TextArea } = Input

// 代码块组件
const CodeBlock = ({ className, children }: { className?: string; children: React.ReactNode }) => {
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const code = String(children).replace(/\n$/, '')
  
  const handleCopy = () => {
    navigator.clipboard.writeText(code)
    message.success('代码已复制')
  }
  
  if (!match) {
    return (
      <code className="bg-slate-800 text-emerald-400 px-1.5 py-0.5 rounded text-sm font-mono">
        {children}
      </code>
    )
  }
  
  return (
    <div className="relative group my-4 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 bg-slate-800 border-b border-slate-700">
        <span className="text-xs text-slate-400 font-mono">{language}</span>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={handleCopy}
          className="text-slate-400 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity"
        />
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderRadius: 0,
          padding: '1rem',
          fontSize: '0.875rem',
          background: '#1e293b',
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  )
}

// 思考过程面板
const ThinkingPanel = ({ 
  thought, 
  isThinking,
  isExpanded,
  onToggle 
}: { 
  thought: string
  isThinking: boolean
  isExpanded: boolean
  onToggle: () => void
}) => {
  if (!thought && !isThinking) return null
  
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="mb-3"
    >
      <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 overflow-hidden">
        {/* 头部 */}
        <div 
          className="flex items-center justify-between px-4 py-2.5 bg-amber-500/10 cursor-pointer"
          onClick={onToggle}
        >
          <div className="flex items-center gap-2">
            <div className="w-6 h-6 rounded-lg bg-amber-500/20 flex items-center justify-center">
              <BulbOutlined className="text-amber-400 text-sm" />
            </div>
            <span className="text-amber-400 font-medium text-sm">思考过程</span>
            {isThinking && (
              <div className="flex items-center gap-1.5 ml-2">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                <span className="text-xs text-amber-400/70">思考中...</span>
              </div>
            )}
          </div>
          <Button
            type="text"
            size="small"
            icon={isExpanded ? <CompressOutlined /> : <ExpandOutlined />}
            className="text-amber-400/70 hover:text-amber-400"
          />
        </div>
        
        {/* 内容 */}
        <AnimatePresence>
          {isExpanded && (
            <motion.div
              initial={{ height: 0 }}
              animate={{ height: 'auto' }}
              exit={{ height: 0 }}
              className="overflow-hidden"
            >
              <div className="px-4 py-3 max-h-60 overflow-y-auto">
                <pre className="text-sm text-slate-300 whitespace-pre-wrap font-sans leading-relaxed">
                  {thought || '正在分析问题...'}
                </pre>
                {isThinking && (
                  <span className="inline-block w-2 h-4 bg-amber-400 animate-pulse ml-1" />
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

// 消息气泡
const MessageBubble = ({
  msg,
  isStreaming = false,
  streamingContent = '',
  streamingThought = '',
  isThinking = false,
}: {
  msg: Message
  isStreaming?: boolean
  streamingContent?: string
  streamingThought?: string
  isThinking?: boolean
}) => {
  const isUser = msg.role === 'user'
  const content = isStreaming ? streamingContent : msg.content
  const thought = isStreaming ? streamingThought : msg.thought
  const [thoughtExpanded, setThoughtExpanded] = useState(true)
  
  const handleCopy = () => {
    navigator.clipboard.writeText(content)
    message.success('已复制')
  }
  
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className={`flex gap-4 ${isUser ? 'flex-row-reverse' : ''}`}
    >
      {/* 头像 */}
      <div className="flex-shrink-0 pt-1">
        <Avatar
          size={40}
          icon={isUser ? <UserOutlined /> : <RobotOutlined />}
          className={isUser 
            ? 'bg-gradient-to-br from-blue-500 to-indigo-600' 
            : 'bg-gradient-to-br from-emerald-500 to-teal-600'
          }
        />
      </div>
      
      {/* 内容区 */}
      <div className={`flex-1 max-w-[85%] ${isUser ? 'flex flex-col items-end' : ''}`}>
        {/* 思考过程面板 (仅 AI 消息) */}
        {!isUser && (thought || isThinking) && (
          <ThinkingPanel
            thought={thought || ''}
            isThinking={isThinking}
            isExpanded={thoughtExpanded}
            onToggle={() => setThoughtExpanded(!thoughtExpanded)}
          />
        )}
        
        {/* 消息内容 */}
        <div
          className={`group relative rounded-2xl px-5 py-4 ${
            isUser
              ? 'bg-gradient-to-br from-blue-500 to-indigo-600 text-white'
              : 'bg-slate-800/80 border border-slate-700/50'
          }`}
        >
          {isUser ? (
            <p className="text-[15px] whitespace-pre-wrap leading-relaxed">{content}</p>
          ) : (
            <>
              <div className="prose prose-invert prose-slate max-w-none prose-p:my-2 prose-headings:my-3 prose-li:my-0.5 prose-pre:my-2">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    code: ({ className, children }) => (
                      <CodeBlock className={className}>{children}</CodeBlock>
                    ),
                  }}
                >
                  {content || (isStreaming ? '' : '')}
                </ReactMarkdown>
                {isStreaming && content && (
                  <span className="inline-block w-2 h-5 bg-emerald-400 animate-pulse ml-0.5 -mb-1" />
                )}
                {isStreaming && !content && isThinking && (
                  <span className="text-slate-500 italic">等待回答...</span>
                )}
              </div>
              
              {/* 操作按钮 */}
              {!isStreaming && content && (
                <div className="absolute -bottom-8 right-0 opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
                  <Tooltip title="复制">
                    <Button
                      type="text"
                      size="small"
                      icon={<CopyOutlined />}
                      onClick={handleCopy}
                      className="text-slate-500 hover:text-white"
                    />
                  </Tooltip>
                </div>
              )}
            </>
          )}
        </div>
        
        {/* 时间戳 */}
        <div className={`text-xs text-slate-500 mt-2 ${isUser ? 'text-right' : ''}`}>
          {new Date(msg.created_at).toLocaleTimeString('zh-CN', {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </div>
      </div>
    </motion.div>
  )
}

// 空状态欢迎页
const EmptyState = ({ onQuickPrompt }: { onQuickPrompt: (prompt: string) => void }) => {
  const prompts = [
    { icon: '🔬', text: '解释深度学习中的注意力机制' },
    { icon: '📊', text: '如何设计一个对照实验？' },
    { icon: '📝', text: '帮我写一段论文摘要' },
    { icon: '💡', text: 'Transformer 和 RNN 有什么区别？' },
  ]
  
  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center py-16 px-4"
    >
      {/* Logo */}
      <div className="relative mb-8">
        <div className="w-24 h-24 rounded-3xl bg-gradient-to-br from-emerald-400 via-teal-500 to-cyan-600 flex items-center justify-center shadow-2xl shadow-emerald-500/25">
          <RobotOutlined className="text-5xl text-white" />
        </div>
        <div className="absolute -bottom-1 -right-1 w-8 h-8 rounded-xl bg-amber-400 flex items-center justify-center">
          <BulbOutlined className="text-amber-900" />
        </div>
      </div>
      
      {/* 标题 */}
      <h1 className="text-3xl font-bold text-white mb-3">
        AI 科研助手
      </h1>
      <p className="text-slate-400 text-center max-w-md mb-10 leading-relaxed">
        我可以帮助你解答科研问题、分析实验数据、撰写学术论文。
        <br />
        <span className="text-emerald-400">你可以看到我的完整思考过程</span>
      </p>
      
      {/* 快捷提示 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-2xl">
        {prompts.map((prompt, index) => (
          <motion.button
            key={index}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 + index * 0.05 }}
            onClick={() => onQuickPrompt(prompt.text)}
            className="flex items-center gap-3 p-4 rounded-xl bg-slate-800/50 border border-slate-700/50 hover:bg-slate-700/50 hover:border-slate-600 transition-all text-left group"
          >
            <span className="text-2xl">{prompt.icon}</span>
            <span className="text-sm text-slate-300 group-hover:text-white transition-colors">
              {prompt.text}
            </span>
          </motion.button>
        ))}
      </div>
    </motion.div>
  )
}

// 主聊天页面
const ChatPage = () => {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const messagesEndRef = useRef<HTMLDivElement>(null)
  
  const {
    messages,
    currentConversation,
    isLoading,
    isSending,
    isThinking,
    streamingContent,
    streamingThought,
    selectConversation,
    sendMessage,
    clearCurrentConversation,
  } = useChatStore()
  
  const [inputValue, setInputValue] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  
  // 加载对话
  useEffect(() => {
    const loadConversation = async () => {
      if (conversationId) {
        setLoadError(null)
        try {
          await selectConversation(parseInt(conversationId))
          setLoadError(null)  // 成功后确保清除错误
        } catch (error: any) {
          console.error('加载对话失败:', error)
          // 提供更详细的错误信息
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
      }
    }
    
    loadConversation()
  }, [conversationId]) // 只依赖 conversationId
  
  // 重新加载对话
  const handleReload = async () => {
    if (conversationId) {
      setLoadError(null)
      try {
        await selectConversation(parseInt(conversationId))
      } catch (error) {
        console.error('重新加载对话失败:', error)
        setLoadError('加载对话失败，请刷新重试')
      }
    }
  }
  
  // 自动滚动
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])
  
  // 发送消息
  const handleSend = async (content?: string) => {
    const messageContent = content || inputValue.trim()
    if (!messageContent || isSending) return
    
    setInputValue('')
    
    try {
      // 直接发送消息，如果没有对话后端会自动创建
      const newConvId = await sendMessage(messageContent)
      
      // 如果是新创建的对话，更新 URL
      if (newConvId && !conversationId) {
        navigate(`/chat/${newConvId}`, { replace: true })
      }
    } catch (error) {
      message.error('发送失败，请重试')
    }
  }
  
  // 快捷提示
  const handleQuickPrompt = (prompt: string) => {
    setInputValue(prompt)
  }
  
  // 按键处理
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }
  
  return (
    <div className="h-full flex flex-col bg-gradient-to-b from-slate-900 to-slate-950">
      {/* 消息区域 */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-4 py-6">
          {isLoading ? (
            <div className="flex justify-center py-20">
              <Spin size="large" />
            </div>
          ) : loadError ? (
            <div className="flex flex-col items-center justify-center py-20">
              <div className="text-red-400 mb-4">{loadError}</div>
              <Button onClick={handleReload}>
                重新加载
              </Button>
            </div>
          ) : messages.length === 0 ? (
            <EmptyState onQuickPrompt={handleQuickPrompt} />
          ) : (
            <div className="space-y-8">
              <AnimatePresence mode="popLayout">
                {messages.map((msg, idx) => (
                  <MessageBubble key={msg.id || idx} msg={msg} />
                ))}
              </AnimatePresence>
              
              {/* 流式响应 */}
              {isSending && (
                <MessageBubble
                  msg={{
                    id: -1,
                    conversation_id: currentConversation?.id || 0,
                    role: 'assistant',
                    content: streamingContent,
                    message_type: 'text',
                    thought: streamingThought,
                    created_at: new Date().toISOString(),
                  }}
                  isStreaming={true}
                  streamingContent={streamingContent}
                  streamingThought={streamingThought}
                  isThinking={isThinking}
                />
              )}
              
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>
      </div>
      
      {/* 输入区域 */}
      <div className="border-t border-slate-800 bg-slate-900/80 backdrop-blur-xl">
        <div className="max-w-4xl mx-auto p-4">
          <div className="relative">
            <TextArea
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入问题，按 Enter 发送..."
              autoSize={{ minRows: 1, maxRows: 6 }}
              className="pr-28 text-base bg-slate-800 border-slate-700 rounded-xl resize-none"
              disabled={isSending}
            />
            <div className="absolute right-2 bottom-2 flex items-center gap-2">
              <Button
                type="primary"
                icon={isSending ? <LoadingOutlined /> : <SendOutlined />}
                onClick={() => handleSend()}
                disabled={!inputValue.trim() || isSending}
                className="bg-emerald-500 hover:bg-emerald-600 border-none rounded-lg"
              >
                发送
              </Button>
            </div>
          </div>
          
          {/* 底部信息 */}
          <div className="flex items-center justify-between mt-2 text-xs text-slate-500">
            <span className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-emerald-400" />
              {currentConversation?.llm_provider || 'DeepSeek'} · 
              {currentConversation?.llm_model || 'deepseek-chat'}
            </span>
            <span>Shift + Enter 换行</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default ChatPage
