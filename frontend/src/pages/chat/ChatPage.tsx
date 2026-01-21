import { useState, useEffect, useRef, forwardRef } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
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
  ToolOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SearchOutlined,
  CalculatorOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  SwapOutlined,
  GlobalOutlined,
  StopOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import remarkGfm from 'remark-gfm'
import { useChatStore, IterationStep } from '@/stores/chatStore'
import type { Message } from '@/services/api'

const { TextArea } = Input

// 工具图标映射
const toolIcons: Record<string, React.ReactNode> = {
  knowledge_search: <SearchOutlined />,
  web_search: <GlobalOutlined />,
  calculator: <CalculatorOutlined />,
  datetime: <ClockCircleOutlined />,
  text_analysis: <FileTextOutlined />,
  unit_converter: <SwapOutlined />,
}

// 工具名称映射
const toolNames: Record<string, string> = {
  knowledge_search: '知识库搜索',
  web_search: '网络搜索',
  calculator: '计算器',
  datetime: '日期时间',
  text_analysis: '文本分析',
  unit_converter: '单位转换',
}

// ReAct 推理过程面板 - 更精美的设计
const ReActPanel = ({ 
  steps,
  currentIteration,
  isThinking,
  currentThought,
  currentToolCall,
}: { 
  steps: IterationStep[]
  currentIteration: number
  isThinking: boolean
  currentThought: string
  currentToolCall: { tool: string; input: Record<string, any> } | null
}) => {
  const [expanded, setExpanded] = useState(true)
  
  // 如果没有任何内容，不显示
  if (steps.length === 0 && !isThinking && !currentToolCall) return null
  
  // 按迭代分组步骤 - 使用 observation 作为每轮结束的标志
  const iterations: IterationStep[][] = []
  let currentGroup: IterationStep[] = []
  
  steps.forEach((step, index) => {
    currentGroup.push(step)
    // 当遇到 observation 时，结束当前轮
    if (step.type === 'observation') {
      if (currentGroup.length > 0) {
        iterations.push([...currentGroup])
        currentGroup = []
      }
    }
  })
  
  // 如果还有未完成的步骤（正在进行的轮次）
  if (currentGroup.length > 0) {
    iterations.push(currentGroup)
  }
  
  return (
    <motion.div
      initial={{ opacity: 0, y: -5 }}
      animate={{ opacity: 1, y: 0 }}
      className="mb-4"
    >
      <div className="rounded-xl bg-gradient-to-br from-slate-800/80 to-slate-900/80 border border-slate-700/50 overflow-hidden shadow-lg">
        {/* 头部 - 渐变背景 */}
        <div 
          className="flex items-center justify-between px-4 py-3 bg-gradient-to-r from-purple-500/10 via-blue-500/10 to-emerald-500/10 cursor-pointer"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500 to-blue-500 flex items-center justify-center shadow-lg">
              <BulbOutlined className="text-white text-sm" />
            </div>
            <div>
              <div className="text-sm font-medium text-white">推理过程</div>
              <div className="text-xs text-slate-400">
                {currentIteration > 0 ? `第 ${currentIteration} 轮推理` : '准备中'}
                {(isThinking || currentToolCall) && (
                  <span className="ml-2 text-emerald-400">
                    <LoadingOutlined className="animate-spin mr-1" />
                    {currentToolCall ? '执行工具中' : '思考中'}
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {steps.length > 0 && (
              <span className="text-xs text-slate-400 bg-slate-700/50 px-2 py-1 rounded-full">
                {steps.length} 步
              </span>
            )}
            <Button
              type="text"
              size="small"
              icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
              className="text-slate-400 hover:text-white"
            />
          </div>
        </div>
        
        {/* 内容 */}
        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-4 py-3 space-y-4 max-h-96 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700">
                {/* 显示所有迭代 */}
                {iterations.map((iterSteps, iterIndex) => (
                  <div key={iterIndex}>
                    {/* 迭代分隔线 - 从第二轮开始显示分隔线 */}
                    {iterIndex > 0 && (
                      <div className="flex items-center gap-3 py-2 my-2">
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-600 to-transparent" />
                        <span className="text-xs text-slate-500 px-2">第 {iterIndex + 1} 轮推理</span>
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-600 to-transparent" />
                      </div>
                    )}
                    
                    <div className="space-y-3">
                      {iterSteps.map((step) => (
                        <motion.div 
                          key={step.timestamp}
                          initial={{ opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          className="relative pl-6"
                        >
                          {/* 时间线 */}
                          <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                          
                          {step.type === 'thought' && (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-amber-500 border-2 border-slate-800" />
                              <div className="bg-amber-500/10 rounded-lg p-3 border border-amber-500/20">
                                <div className="flex items-center gap-2 mb-2">
                                  <BulbOutlined className="text-amber-400" />
                                  <span className="text-xs font-medium text-amber-400">思考</span>
                                </div>
                                <p className="text-sm text-slate-300 leading-relaxed">
                                  {step.content}
                                </p>
                              </div>
                            </div>
                          )}
                          
                          {step.type === 'action' && (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-blue-500 border-2 border-slate-800" />
                              <div className="bg-blue-500/10 rounded-lg p-3 border border-blue-500/20">
                                <div className="flex items-center gap-2 mb-2">
                                  <span className="text-blue-400">
                                    {toolIcons[step.tool || ''] || <ToolOutlined />}
                                  </span>
                                  <span className="text-xs font-medium text-blue-400">
                                    调用 {toolNames[step.tool || ''] || step.tool}
                                  </span>
                                </div>
                                <code className="text-xs text-slate-400 bg-slate-800/80 px-2 py-1 rounded block overflow-x-auto">
                                  {JSON.stringify(step.toolInput, null, 2)}
                                </code>
                              </div>
                            </div>
                          )}
                          
                          {step.type === 'observation' && (
                            <div className="relative">
                              <div className={`absolute -left-6 top-1 w-3 h-3 rounded-full border-2 border-slate-800 ${step.success ? 'bg-emerald-500' : 'bg-red-500'}`} />
                              <div className={`rounded-lg p-3 border ${step.success ? 'bg-emerald-500/10 border-emerald-500/20' : 'bg-red-500/10 border-red-500/20'}`}>
                                <div className="flex items-center gap-2 mb-2">
                                  {step.success ? (
                                    <CheckCircleOutlined className="text-emerald-400" />
                                  ) : (
                                    <CloseCircleOutlined className="text-red-400" />
                                  )}
                                  <span className={`text-xs font-medium ${step.success ? 'text-emerald-400' : 'text-red-400'}`}>
                                    工具返回
                                  </span>
                                </div>
                                <p className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">
                                  {step.content.length > 300 ? step.content.slice(0, 300) + '...' : step.content}
                                </p>
                              </div>
                            </div>
                          )}
                        </motion.div>
                      ))}
                    </div>
                  </div>
                ))}
                
                {/* 当前正在进行的思考 */}
                {isThinking && currentThought && (
                  <motion.div 
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-amber-500 border-2 border-slate-800 animate-pulse" />
                    <div className="bg-amber-500/10 rounded-lg p-3 border border-amber-500/20">
                      <div className="flex items-center gap-2 mb-2">
                        <BulbOutlined className="text-amber-400 animate-pulse" />
                        <span className="text-xs font-medium text-amber-400">思考中...</span>
                      </div>
                      <p className="text-sm text-slate-300 leading-relaxed">
                        {currentThought}
                        <span className="inline-block w-2 h-4 bg-amber-400 animate-pulse ml-1 rounded-sm" />
                      </p>
                    </div>
                  </motion.div>
                )}
                
                {/* 当前正在执行的工具 */}
                {currentToolCall && (
                  <motion.div 
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-blue-500 border-2 border-slate-800 animate-pulse" />
                    <div className="bg-blue-500/10 rounded-lg p-3 border border-blue-500/20">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-blue-400 animate-pulse">
                          {toolIcons[currentToolCall.tool] || <ToolOutlined />}
                        </span>
                        <span className="text-xs font-medium text-blue-400">
                          正在执行 {toolNames[currentToolCall.tool] || currentToolCall.tool}...
                        </span>
                        <LoadingOutlined className="text-blue-400 animate-spin" />
                      </div>
                      <code className="text-xs text-slate-400 bg-slate-800/80 px-2 py-1 rounded block">
                        {JSON.stringify(currentToolCall.input)}
                      </code>
                    </div>
                  </motion.div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

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

// 思考过程面板（仅显示最终思考）
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
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.2 }}
      className="mb-2"
    >
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 overflow-hidden">
        {/* 头部 - 可点击展开/收起 */}
        <div 
          className="flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-amber-500/10 transition-colors"
          onClick={onToggle}
        >
          <div className="flex items-center gap-2">
            <BulbOutlined className="text-amber-400 text-sm" />
            <span className="text-amber-400/90 text-xs font-medium">最终思考</span>
            {isThinking && (
              <span className="flex items-center gap-1 text-xs text-amber-400/60">
                <LoadingOutlined className="animate-spin" />
                思考中
              </span>
            )}
          </div>
          <span className="text-amber-400/50 text-xs">
            {isExpanded ? '收起' : '展开'}
          </span>
        </div>
        
        {/* 内容 */}
        <AnimatePresence>
          {isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-3 py-2 border-t border-amber-500/10 max-h-40 overflow-y-auto">
                <p className="text-xs text-slate-400 whitespace-pre-wrap leading-relaxed">
                  {thought || '正在分析问题...'}
                </p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

// 历史消息的 ReAct 推理过程面板
const HistoryReActPanel = ({ 
  steps 
}: { 
  steps: Array<{
    type: string
    iteration: number
    content?: string
    tool?: string
    input?: Record<string, unknown>
    output?: string
    success?: boolean
  }>
}) => {
  const [expanded, setExpanded] = useState(false)
  
  if (!steps || steps.length === 0) return null
  
  // 统计信息
  const totalIterations = Math.max(...steps.map(s => s.iteration || 1))
  const toolCalls = steps.filter(s => s.type === 'action').length
  
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="mb-3"
    >
      <div className="rounded-xl bg-gradient-to-br from-slate-800/60 to-slate-900/60 border border-slate-700/50 overflow-hidden">
        {/* 头部 */}
        <div 
          className="flex items-center justify-between px-4 py-2.5 cursor-pointer hover:bg-slate-700/30 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-purple-500/80 to-blue-500/80 flex items-center justify-center">
              <BulbOutlined className="text-white text-xs" />
            </div>
            <div>
              <span className="text-sm font-medium text-slate-300">推理过程</span>
              <span className="ml-2 text-xs text-slate-500">
                {totalIterations} 轮迭代 · {toolCalls} 次工具调用
              </span>
            </div>
          </div>
          <Button
            type="text"
            size="small"
            icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
            className="text-slate-400 hover:text-white"
          />
        </div>
        
        {/* 内容 */}
        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-4 py-3 border-t border-slate-700/50 space-y-3 max-h-80 overflow-y-auto">
                {steps.map((step, index) => (
                  <div key={index} className="relative pl-5">
                    {/* 时间线 */}
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    
                    {step.type === 'thought' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-amber-500 border-2 border-slate-800" />
                        <div className="bg-amber-500/10 rounded-lg p-2.5 border border-amber-500/20">
                          <div className="flex items-center gap-2 mb-1.5">
                            <BulbOutlined className="text-amber-400 text-xs" />
                            <span className="text-xs font-medium text-amber-400">
                              第 {step.iteration} 轮思考
                            </span>
                          </div>
                          <p className="text-xs text-slate-400 leading-relaxed">
                            {step.content}
                          </p>
                        </div>
                      </div>
                    )}
                    
                    {step.type === 'action' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-blue-500 border-2 border-slate-800" />
                        <div className="bg-blue-500/10 rounded-lg p-2.5 border border-blue-500/20">
                          <div className="flex items-center gap-2 mb-1.5">
                            <span className="text-blue-400 text-xs">
                              {toolIcons[step.tool || ''] || <ToolOutlined />}
                            </span>
                            <span className="text-xs font-medium text-blue-400">
                              调用 {toolNames[step.tool || ''] || step.tool}
                            </span>
                          </div>
                          <code className="text-[10px] text-slate-500 bg-slate-800/60 px-2 py-1 rounded block overflow-x-auto">
                            {JSON.stringify(step.input)}
                          </code>
                        </div>
                      </div>
                    )}
                    
                    {step.type === 'observation' && (
                      <div className="relative">
                        <div className={`absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full border-2 border-slate-800 ${step.success ? 'bg-emerald-500' : 'bg-red-500'}`} />
                        <div className={`rounded-lg p-2.5 border ${step.success ? 'bg-emerald-500/10 border-emerald-500/20' : 'bg-red-500/10 border-red-500/20'}`}>
                          <div className="flex items-center gap-2 mb-1.5">
                            {step.success ? (
                              <CheckCircleOutlined className="text-emerald-400 text-xs" />
                            ) : (
                              <CloseCircleOutlined className="text-red-400 text-xs" />
                            )}
                            <span className={`text-xs font-medium ${step.success ? 'text-emerald-400' : 'text-red-400'}`}>
                              工具返回
                            </span>
                          </div>
                          <p className="text-xs text-slate-400 leading-relaxed whitespace-pre-wrap">
                            {(step.output || '').length > 300 ? (step.output || '').slice(0, 300) + '...' : step.output}
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

// 消息气泡 - 美化版
const MessageBubble = forwardRef<HTMLDivElement, {
  msg: Message
  isStreaming?: boolean
  streamingContent?: string
  streamingThought?: string
  isThinking?: boolean
  isHighlighted?: boolean
}>(({
  msg,
  isStreaming = false,
  streamingContent = '',
  streamingThought = '',
  isThinking = false,
  isHighlighted = false,
}, ref) => {
  const isUser = msg.role === 'user'
  const content = isStreaming ? streamingContent : msg.content
  const thought = isStreaming ? '' : msg.thought
  const reactSteps = isStreaming ? undefined : msg.react_steps
  const [thoughtExpanded, setThoughtExpanded] = useState(false)
  
  const handleCopy = () => {
    navigator.clipboard.writeText(content)
    message.success('已复制到剪贴板')
  }
  
  return (
    <motion.div
      ref={ref}
      id={`message-${msg.id}`}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: 'easeOut' }}
      className={`flex gap-4 ${isUser ? 'flex-row-reverse' : ''} ${
        isHighlighted ? 'relative' : ''
      }`}
    >
      {/* 高亮效果 */}
      {isHighlighted && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="absolute inset-0 -mx-4 -my-2 rounded-xl bg-emerald-500/10 border border-emerald-500/30 pointer-events-none"
          style={{ zIndex: -1 }}
        />
      )}
      
      {/* 头像 */}
      <div className="flex-shrink-0">
        {isUser ? (
          <Avatar
            size={40}
            icon={<UserOutlined />}
            className="bg-gradient-to-br from-blue-500 to-indigo-600 shadow-lg shadow-blue-500/30"
          />
        ) : (
          <div className="w-10 h-10 rounded-full bg-gradient-to-br from-emerald-500 via-teal-500 to-cyan-500 flex items-center justify-center shadow-lg shadow-emerald-500/30">
            <RobotOutlined className="text-white text-lg" />
          </div>
        )}
      </div>
      
      {/* 内容区 */}
      <div className={`flex-1 max-w-[85%] ${isUser ? 'flex flex-col items-end' : ''}`}>
        {/* 角色标签 */}
        <div className={`flex items-center gap-2 mb-2 ${isUser ? 'flex-row-reverse' : ''}`}>
          <span className={`text-sm font-medium ${isUser ? 'text-blue-400' : 'text-emerald-400'}`}>
            {isUser ? '你' : 'AI 助手'}
          </span>
          <span className="text-xs text-slate-500">
            {new Date(msg.created_at).toLocaleTimeString('zh-CN', {
              hour: '2-digit',
              minute: '2-digit',
            })}
          </span>
        </div>
        
        {/* ReAct 推理过程面板 (历史 AI 消息) */}
        {!isUser && !isStreaming && reactSteps && reactSteps.length > 0 && (
          <HistoryReActPanel steps={reactSteps} />
        )}
        
        {/* 最终思考面板 (与推理过程并行显示) */}
        {!isUser && !isStreaming && thought && (
          <ThinkingPanel
            thought={thought}
            isThinking={false}
            isExpanded={thoughtExpanded}
            onToggle={() => setThoughtExpanded(!thoughtExpanded)}
          />
        )}
        
        {/* 消息内容 */}
        {isUser ? (
          // 用户消息 - 简洁风格
          <div className="bg-gradient-to-r from-blue-600 to-indigo-600 text-white px-5 py-3 rounded-2xl rounded-tr-md shadow-lg shadow-blue-500/20">
            <p className="text-[15px] whitespace-pre-wrap leading-relaxed">{content}</p>
          </div>
        ) : (
          // AI消息 - 精美卡片风格
          <div className="relative">
            {/* 渐变边框效果 */}
            <div className="absolute -inset-0.5 bg-gradient-to-r from-emerald-500/20 via-teal-500/20 to-cyan-500/20 rounded-2xl blur-sm" />
            
            <div className="relative bg-slate-800/90 backdrop-blur-xl border border-slate-700/50 rounded-2xl rounded-tl-md overflow-hidden">
              {/* 顶部渐变装饰 */}
              <div className="h-1 bg-gradient-to-r from-emerald-500 via-teal-500 to-cyan-500" />
              
              <div className="p-5">
                {content ? (
                  <>
                    <div className="prose prose-invert prose-slate max-w-none
                      prose-p:my-3 prose-p:leading-relaxed prose-p:text-slate-200
                      prose-headings:mt-6 prose-headings:mb-3 prose-headings:text-white prose-headings:font-semibold
                      prose-h1:text-xl prose-h2:text-lg prose-h3:text-base
                      prose-li:my-1 prose-li:text-slate-200
                      prose-ul:my-3 prose-ol:my-3
                      prose-pre:my-4 prose-pre:bg-slate-900 prose-pre:border prose-pre:border-slate-700/50 prose-pre:rounded-xl
                      prose-code:text-emerald-400 prose-code:bg-slate-900/80 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-sm prose-code:font-mono
                      prose-strong:text-white prose-strong:font-semibold
                      prose-em:text-slate-300 prose-em:italic
                      prose-a:text-blue-400 prose-a:no-underline hover:prose-a:text-blue-300 hover:prose-a:underline
                      prose-blockquote:border-l-4 prose-blockquote:border-emerald-500/50 prose-blockquote:bg-slate-900/50 prose-blockquote:py-2 prose-blockquote:px-4 prose-blockquote:not-italic prose-blockquote:rounded-r-lg
                      prose-hr:border-slate-700 prose-hr:my-6
                      prose-table:border prose-table:border-slate-700 prose-th:bg-slate-800 prose-th:px-3 prose-th:py-2 prose-td:px-3 prose-td:py-2 prose-td:border-t prose-td:border-slate-700
                      text-[15px] leading-relaxed">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          code: ({ className, children }) => (
                            <CodeBlock className={className}>{children}</CodeBlock>
                          ),
                        }}
                      >
                        {content}
                      </ReactMarkdown>
                    </div>
                    
                    {/* 流式输出光标 */}
                    {isStreaming && (
                      <span className="inline-block w-2 h-5 bg-emerald-400 animate-pulse ml-1 -mb-1 rounded-sm" />
                    )}
                  </>
                ) : isStreaming ? (
                  <div className="flex items-center gap-3 py-2">
                    <div className="flex gap-1">
                      <span className="w-2 h-2 bg-emerald-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                      <span className="w-2 h-2 bg-teal-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                      <span className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                    </div>
                    <span className="text-sm text-slate-400">
                      {isThinking ? '正在思考...' : '正在生成回答...'}
                    </span>
                  </div>
                ) : null}
                
                {/* 操作栏 */}
                {!isStreaming && content && (
                  <div className="flex items-center gap-3 mt-4 pt-4 border-t border-slate-700/50">
                    <Tooltip title="复制内容">
                      <Button
                        type="text"
                        size="small"
                        icon={<CopyOutlined />}
                        onClick={handleCopy}
                        className="text-slate-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded-lg transition-all"
                      >
                        复制
                      </Button>
                    </Tooltip>
                    
                    {/* 可以添加更多操作按钮 */}
                    <div className="flex-1" />
                    <span className="text-xs text-slate-600">
                      AI 生成内容，仅供参考
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </motion.div>
  )
})

// 添加displayName以便调试
MessageBubble.displayName = 'MessageBubble'

// 空状态欢迎页
const EmptyState = ({ onQuickPrompt }: { onQuickPrompt: (prompt: string) => void }) => {
  const prompts = [
    { icon: '🔬', text: '解释深度学习中的注意力机制' },
    { icon: '📊', text: '计算 sin(45°) + cos(60°) 的值' },
    { icon: '📝', text: '搜索我知识库中关于机器学习的内容' },
    { icon: '💡', text: '帮我把 100 华氏度转换成摄氏度' },
  ]
  
  const tools = [
    { icon: <SearchOutlined />, name: '知识库搜索', desc: '检索上传的文档' },
    { icon: <GlobalOutlined />, name: '网络搜索', desc: '搜索互联网' },
    { icon: <CalculatorOutlined />, name: '计算器', desc: '数学运算' },
    { icon: <ClockCircleOutlined />, name: '日期时间', desc: '获取当前时间' },
    { icon: <SwapOutlined />, name: '单位转换', desc: '长度/重量/温度' },
  ]
  
  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center py-12 px-4"
    >
      {/* Logo */}
      <div className="relative mb-6">
        <div className="w-20 h-20 rounded-3xl bg-gradient-to-br from-emerald-400 via-teal-500 to-cyan-600 flex items-center justify-center shadow-2xl shadow-emerald-500/25">
          <RobotOutlined className="text-4xl text-white" />
        </div>
        <div className="absolute -bottom-1 -right-1 w-7 h-7 rounded-xl bg-amber-400 flex items-center justify-center">
          <BulbOutlined className="text-amber-900 text-sm" />
        </div>
      </div>
      
      {/* 标题 */}
      <h1 className="text-2xl font-bold text-white mb-2">
        AI 科研助手
      </h1>
      <p className="text-slate-400 text-center max-w-md mb-6 text-sm leading-relaxed">
        我可以帮助你解答科研问题、分析数据、检索知识库
        <br />
        <span className="text-emerald-400">支持工具调用，可以看到完整思考过程</span>
      </p>
      
      {/* 可用工具 */}
      <div className="flex flex-wrap justify-center gap-2 mb-8">
        {tools.map((tool, index) => (
          <motion.div
            key={index}
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.05 * index }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-slate-800/50 border border-slate-700/50 text-xs"
          >
            <span className="text-blue-400">{tool.icon}</span>
            <span className="text-slate-300">{tool.name}</span>
          </motion.div>
        ))}
      </div>
      
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
  const location = useLocation()
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const initialMessageSent = useRef(false) // 跟踪是否已发送初始消息
  
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
    toolCalls,
    currentToolCall,
    selectConversation,
    sendMessage,
    stopGeneration,
    clearCurrentConversation,
  } = useChatStore()
  
  const [inputValue, setInputValue] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  
  // 处理从首页传来的初始消息或从搜索结果跳转
  const [highlightedMessageId, setHighlightedMessageId] = useState<number | null>(null)
  const [conversationLoaded, setConversationLoaded] = useState(false)  // 追踪对话是否已加载
  
  // 加载对话
  useEffect(() => {
    const loadConversation = async () => {
      // 如果正在发送消息，不重新加载（防止覆盖本地消息）
      if (isSending) {
        setConversationLoaded(true)
        return
      }
      
      setConversationLoaded(false)  // 开始加载时重置
      if (conversationId) {
        setLoadError(null)
        try {
          await selectConversation(parseInt(conversationId))
          setLoadError(null)
          setConversationLoaded(true)  // 加载完成
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
        setConversationLoaded(true)  // 新对话也算加载完成
      }
    }
    
    loadConversation()
  }, [conversationId])
  
  // 处理从首页传来的初始消息 - 必须在对话加载完成后执行
  useEffect(() => {
    const state = location.state as { initialMessage?: string; highlightMessageId?: number } | null
    
    // 处理初始消息 - 只有当对话加载完成且没有发送过初始消息时才发送
    if (state?.initialMessage && conversationId && conversationLoaded && !initialMessageSent.current && !isSending) {
      initialMessageSent.current = true
      // 发送初始消息
      sendMessage(state.initialMessage).catch(err => {
        console.error('发送初始消息失败:', err)
        message.error('发送失败，请重试')
        initialMessageSent.current = false  // 失败时允许重试
      })
      // 清除 location state，防止刷新页面时重复发送
      navigate(location.pathname, { replace: true, state: {} })
    }
    
    // 处理消息高亮
    if (state?.highlightMessageId && conversationLoaded && messages.length > 0) {
      setHighlightedMessageId(state.highlightMessageId)
      // 清除 location state
      navigate(location.pathname, { replace: true, state: {} })
      
      // 滚动到对应消息
      setTimeout(() => {
        const messageElement = document.getElementById(`message-${state.highlightMessageId}`)
        if (messageElement) {
          messageElement.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }
      }, 100)
      
      // 3秒后取消高亮
      setTimeout(() => {
        setHighlightedMessageId(null)
      }, 3000)
    }
  }, [conversationId, location.state, conversationLoaded, messages.length, isSending])
  
  // 重置 initialMessageSent 当 conversationId 改变时
  useEffect(() => {
    initialMessageSent.current = false
  }, [conversationId])
  
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
    <div className="h-full flex flex-col bg-gradient-to-b from-slate-900 via-slate-900 to-slate-950">
      {/* 消息区域 */}
      <div className="flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700 scrollbar-track-transparent">
        <div className="max-w-3xl mx-auto px-4 py-6">
          {isLoading ? (
            <div className="flex flex-col items-center justify-center py-20">
              <Spin size="large" />
              <p className="text-slate-500 mt-4">加载对话中...</p>
            </div>
          ) : loadError ? (
            <div className="flex flex-col items-center justify-center py-20">
              <div className="text-red-400 mb-4">{loadError}</div>
              <Button onClick={handleReload} icon={<ReloadOutlined />}>
                重新加载
              </Button>
            </div>
          ) : messages.length === 0 ? (
            <EmptyState onQuickPrompt={handleQuickPrompt} />
          ) : (
            <div className="space-y-6">
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
                  {(streamingContent || (!isThinking && !currentToolCall && iterationSteps.length === 0)) && (
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
      
      {/* 输入区域 */}
      <div className="border-t border-slate-800/50 bg-slate-900/90 backdrop-blur-xl">
        <div className="max-w-3xl mx-auto p-4">
          <div className="relative flex items-end gap-3">
            <div className="flex-1 relative">
              <TextArea
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="输入问题，按 Enter 发送..."
                autoSize={{ minRows: 1, maxRows: 6 }}
                className="text-base bg-slate-800/80 border-slate-700/50 rounded-xl resize-none 
                  focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/20
                  placeholder:text-slate-500"
                disabled={isSending}
              />
            </div>
            {isSending ? (
              <Button
                type="primary"
                size="large"
                danger
                icon={<StopOutlined />}
                onClick={stopGeneration}
                className="bg-red-500 hover:bg-red-600 border-none rounded-xl h-10 px-5
                  shadow-lg shadow-red-500/20"
              >
                停止
              </Button>
            ) : (
              <Button
                type="primary"
                size="large"
                icon={<SendOutlined />}
                onClick={() => handleSend()}
                disabled={!inputValue.trim()}
                className="bg-emerald-500 hover:bg-emerald-600 border-none rounded-xl h-10 px-5
                  shadow-lg shadow-emerald-500/20 disabled:opacity-50"
              >
                发送
              </Button>
            )}
          </div>
          
          {/* 底部信息 */}
          <div className="flex items-center justify-between mt-3 text-xs text-slate-500">
            <span className="flex items-center gap-2">
              <span className={`w-1.5 h-1.5 rounded-full ${isSending ? 'bg-amber-400' : 'bg-emerald-400'} animate-pulse`} />
              <span className="text-slate-400">
                {isSending ? '正在生成...' : (currentConversation?.llm_provider || 'DeepSeek')}
              </span>
            </span>
            <span className="text-slate-600">
              {isSending ? '点击停止按钮可中止生成' : 'Shift + Enter 换行 · Enter 发送'}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default ChatPage
