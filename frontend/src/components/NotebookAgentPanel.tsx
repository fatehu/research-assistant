/**
 * NotebookAgentPanel - Notebook AI 助手面板 (增强版)
 * 
 * 新增功能:
 * 1. onFocusCell - 聚焦到特定 cell
 * 2. onClearOutputs - 清除所有输出
 * 3. currentCellIndex - 当前选中的 cell 索引
 * 4. cells - notebook 的所有 cells
 */

import { useState, useEffect, useRef, useCallback } from 'react'
import { Button, Input, Tooltip, Spin, message, Popconfirm, Tag, Switch } from 'antd'
import {
  RobotOutlined, SendOutlined, CloseOutlined, DeleteOutlined, CopyOutlined,
  CodeOutlined, PlayCircleOutlined, ExpandOutlined, CompressOutlined,
  ReloadOutlined, BulbOutlined, QuestionCircleOutlined, BarChartOutlined,
  BugOutlined, ThunderboltOutlined, ClearOutlined, AimOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { agentApi, AgentMessage, AgentCodeBlock, Cell } from '@/services/api'

const { TextArea } = Input

interface NotebookAgentPanelProps {
  notebookId: string
  onInsertCode?: (code: string) => void
  onRunCode?: (code: string) => void
  onFocusCell?: (cellIndex: number) => void
  onClearOutputs?: () => void
  onRefreshNotebook?: () => void  // 刷新 Notebook 数据
  onAddCell?: (cell: Cell) => void  // 直接添加新 Cell（实时更新）
  onUpdateCell?: (cell: Cell) => void  // 更新 Cell（实时更新）
  isVisible: boolean
  onClose: () => void
  onToggleExpand?: () => void
  isExpanded?: boolean
  currentCellIndex?: number
  cells?: Cell[]
}

const quickActions = [
  { key: 'analyze', icon: <BarChartOutlined />, label: '分析数据', prompt: '请分析当前数据的特征和分布情况' },
  { key: 'visualize', icon: <BarChartOutlined />, label: '数据可视化', prompt: '请生成数据可视化代码' },
  { key: 'clean', icon: <BugOutlined />, label: '数据清洗', prompt: '请帮我清洗和预处理数据' },
  { key: 'model', icon: <ThunderboltOutlined />, label: '建模建议', prompt: '根据当前数据，推荐合适的机器学习模型' },
  { key: 'explain', icon: <QuestionCircleOutlined />, label: '解释代码', prompt: '请解释最近执行的代码' },
  { key: 'optimize', icon: <BulbOutlined />, label: '优化代码', prompt: '请优化最近的代码，提高性能' },
]

const NotebookAgentPanel: React.FC<NotebookAgentPanelProps> = ({
  notebookId, onInsertCode, onRunCode, onFocusCell, onClearOutputs, onRefreshNotebook, onAddCell, onUpdateCell,
  isVisible, onClose, onToggleExpand, isExpanded = false,
  currentCellIndex = 0, cells = [],
}) => {
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [inputValue, setInputValue] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const [isAuthorized, setIsAuthorized] = useState(false)  // 授权状态
  const [pendingAuthAction, setPendingAuthAction] = useState<string | null>(null)
  
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<any>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(() => { scrollToBottom() }, [messages, streamingContent, scrollToBottom])

  const loadHistory = useCallback(async () => {
    if (!notebookId) return
    setIsLoadingHistory(true)
    try {
      const data = await agentApi.getHistory(notebookId)
      setMessages(data.messages || [])
    } catch (error) {
      console.error('加载对话历史失败:', error)
    } finally {
      setIsLoadingHistory(false)
    }
  }, [notebookId])

  useEffect(() => {
    if (isVisible && notebookId) loadHistory()
  }, [isVisible, notebookId, loadHistory])

  const clearHistory = async () => {
    try {
      await agentApi.clearHistory(notebookId)
      setMessages([])
      message.success('对话已清空')
    } catch (error) {
      message.error('清空失败')
    }
  }

  const sendMessage = async (content: string, authorized: boolean = isAuthorized) => {
    if (!content.trim() || isLoading) return

    const userMessage: AgentMessage = {
      id: Date.now().toString(),
      role: 'user',
      content: content.trim(),
      code_blocks: [],
      timestamp: new Date().toISOString(),
      metadata: {},
    }

    setMessages(prev => [...prev, userMessage])
    setInputValue('')
    setIsLoading(true)
    setStreamingContent('')
    abortControllerRef.current = new AbortController()

    try {
      let fullContent = ''
      let codeBlocks: AgentCodeBlock[] = []

      await agentApi.chat(
        notebookId,
        { message: content.trim(), include_context: true, include_variables: true, user_authorized: authorized, stream: true },
        (event) => {
          if (event.type === 'content') {
            fullContent += event.content
            setStreamingContent(fullContent)
          } else if (event.type === 'thought') {
            // 可选：显示思考过程
            fullContent += `\n💭 *${event.content}*\n`
            setStreamingContent(fullContent)
          } else if (event.type === 'action') {
            // 显示工具调用
            fullContent += `\n🔧 调用工具: ${event.tool}\n`
            setStreamingContent(fullContent)
          } else if (event.type === 'observation') {
            // 显示工具结果
            const status = event.success ? '✅' : '❌'
            fullContent += `\n${status} 结果: ${event.output?.substring(0, 200)}${event.output && event.output.length > 200 ? '...' : ''}\n`
            setStreamingContent(fullContent)
            
            // 如果有新 Cell，直接添加到 Notebook（实时更新）
            if (event.notebook_updated && event.new_cell && onAddCell) {
              onAddCell(event.new_cell)
            } else if (event.notebook_updated && event.updated_cell && onUpdateCell) {
              // 更新已存在的 Cell
              onUpdateCell(event.updated_cell)
            } else if (event.notebook_updated && onRefreshNotebook) {
              // 降级：刷新整个 Notebook
              onRefreshNotebook()
            }
          } else if (event.type === 'answer') {
            fullContent = event.content || fullContent
            setStreamingContent(fullContent)
          } else if (event.type === 'authorization_required') {
            // 需要授权
            setPendingAuthAction(event.action || 'unknown')
            message.warning(`操作需要授权: ${event.action}`)
          } else if (event.type === 'done') {
            codeBlocks = event.code_blocks || []
            const assistantMessage: AgentMessage = {
              id: Date.now().toString(),
              role: 'assistant',
              content: fullContent,
              code_blocks: codeBlocks,
              timestamp: new Date().toISOString(),
              metadata: { suggested_action: event.suggested_action, suggested_code: event.suggested_code },
            }
            setMessages(prev => [...prev, assistantMessage])
            setStreamingContent('')
          } else if (event.type === 'error') {
            message.error(event.error || '请求失败')
          }
        },
        abortControllerRef.current
      )
    } catch (error: any) {
      if (error.name !== 'AbortError') {
        console.error('发送消息失败:', error)
        message.error('发送失败，请重试')
      }
    } finally {
      setIsLoading(false)
      abortControllerRef.current = null
    }
  }

  const stopGeneration = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      setIsLoading(false)
      setStreamingContent('')
    }
  }

  const copyCode = (code: string) => { navigator.clipboard.writeText(code); message.success('代码已复制') }
  const insertCode = (code: string) => { if (onInsertCode) onInsertCode(code) }
  const runCode = (code: string) => { if (onRunCode) onRunCode(code) }
  const handleQuickAction = (action: typeof quickActions[0]) => { sendMessage(action.prompt) }

  const getContextInfo = () => {
    if (cells.length === 0) return null
    const codeCount = cells.filter(c => c.cell_type === 'code').length
    const mdCount = cells.filter(c => c.cell_type === 'markdown').length
    const hasOutputs = cells.some(c => c.outputs && c.outputs.length > 0)
    return { codeCount, mdCount, hasOutputs, totalCells: cells.length }
  }

  const contextInfo = getContextInfo()

  const renderMessageContent = (msg: AgentMessage) => {
    const isUser = msg.role === 'user'
    
    return (
      <div className={`flex gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
        <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center ${isUser ? 'bg-blue-500' : 'bg-gradient-to-br from-emerald-500 to-teal-600'}`}>
          {isUser ? <span className="text-white text-sm font-medium">U</span> : <RobotOutlined className="text-white text-sm" />}
        </div>
        <div className={`flex-1 max-w-[85%] ${isUser ? 'text-right' : ''}`}>
          <div className={`inline-block rounded-2xl px-4 py-2 ${isUser ? 'bg-blue-500 text-white rounded-tr-sm' : 'bg-slate-800 text-slate-200 rounded-tl-sm'}`}>
            {isUser ? (
              <p className="whitespace-pre-wrap">{msg.content}</p>
            ) : (
              <div className="prose prose-invert prose-sm max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                  code({ node, className, children, ...props }: any) {
                    const match = /language-(\w+)/.exec(className || '')
                    const inline = !match
                    const codeString = String(children).replace(/\n$/, '')
                    
                    if (inline) return <code className="bg-slate-700/50 px-1.5 py-0.5 rounded text-emerald-400 text-sm" {...props}>{children}</code>
                    
                    return (
                      <div className="relative group my-2">
                        <div className="absolute right-2 top-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity z-10">
                          <Tooltip title="复制"><Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copyCode(codeString)} className="text-slate-400 hover:text-white" /></Tooltip>
                          {onInsertCode && <Tooltip title="插入到 Notebook"><Button type="text" size="small" icon={<CodeOutlined />} onClick={() => insertCode(codeString)} className="text-slate-400 hover:text-emerald-400" /></Tooltip>}
                          {onRunCode && <Tooltip title="运行代码"><Button type="text" size="small" icon={<PlayCircleOutlined />} onClick={() => runCode(codeString)} className="text-slate-400 hover:text-amber-400" /></Tooltip>}
                        </div>
                        <SyntaxHighlighter style={oneDark} language={match ? match[1] : 'python'} PreTag="div" customStyle={{ margin: 0, borderRadius: '0.5rem', fontSize: '0.85rem' }}>
                          {codeString}
                        </SyntaxHighlighter>
                      </div>
                    )
                  },
                  p({ children }) { return <p className="mb-2 last:mb-0">{children}</p> },
                  ul({ children }) { return <ul className="list-disc list-inside mb-2">{children}</ul> },
                  ol({ children }) { return <ol className="list-decimal list-inside mb-2">{children}</ol> },
                }}>{msg.content}</ReactMarkdown>
              </div>
            )}
          </div>
          <div className={`text-xs text-slate-500 mt-1 ${isUser ? 'text-right' : ''}`}>{new Date(msg.timestamp).toLocaleTimeString()}</div>
        </div>
      </div>
    )
  }

  const renderStreamingContent = () => {
    if (!streamingContent) return null
    return (
      <div className="flex gap-3">
        <div className="flex-shrink-0 w-8 h-8 rounded-full bg-gradient-to-br from-emerald-500 to-teal-600 flex items-center justify-center">
          <RobotOutlined className="text-white text-sm" />
        </div>
        <div className="flex-1 max-w-[85%]">
          <div className="inline-block rounded-2xl rounded-tl-sm px-4 py-2 bg-slate-800 text-slate-200">
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamingContent}</ReactMarkdown>
            </div>
            <span className="inline-block w-2 h-4 bg-emerald-400 animate-pulse ml-1" />
          </div>
        </div>
      </div>
    )
  }

  if (!isVisible) return null

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0, x: 50 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 50 }} className={`h-full flex flex-col bg-slate-900 border-l border-slate-800 ${isExpanded ? 'w-[600px]' : 'w-[400px]'}`}>
        {/* 头部 */}
        <div className="flex-shrink-0 h-14 px-4 flex items-center justify-between border-b border-slate-800 bg-slate-900/80 backdrop-blur-xl">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-emerald-500 to-teal-600 flex items-center justify-center"><RobotOutlined className="text-white" /></div>
            <div><h3 className="text-white font-semibold text-sm">AI 助手</h3><p className="text-slate-500 text-xs">智能编程辅助</p></div>
          </div>
          <div className="flex items-center gap-1">
            <Tooltip title="刷新上下文"><Button type="text" size="small" icon={<ReloadOutlined />} onClick={loadHistory} className="text-slate-400 hover:text-white" /></Tooltip>
            <Popconfirm title="确定要清空对话历史吗？" onConfirm={clearHistory} okText="确定" cancelText="取消">
              <Tooltip title="清空对话"><Button type="text" size="small" icon={<DeleteOutlined />} className="text-slate-400 hover:text-red-400" /></Tooltip>
            </Popconfirm>
            {onToggleExpand && <Tooltip title={isExpanded ? '收起' : '展开'}><Button type="text" size="small" icon={isExpanded ? <CompressOutlined /> : <ExpandOutlined />} onClick={onToggleExpand} className="text-slate-400 hover:text-white" /></Tooltip>}
            <Tooltip title="关闭"><Button type="text" size="small" icon={<CloseOutlined />} onClick={onClose} className="text-slate-400 hover:text-white" /></Tooltip>
          </div>
        </div>

        {/* Notebook 控制面板 */}
        {contextInfo && (
          <div className="flex-shrink-0 px-4 py-2 border-b border-slate-800 bg-slate-800/30">
            <div className="flex items-center justify-between mb-2">
              <span className="text-slate-400 text-xs">Notebook 上下文</span>
              <div className="flex gap-2">
                {onFocusCell && <Tooltip title="跳转到当前 Cell"><Button type="text" size="small" icon={<AimOutlined />} onClick={() => onFocusCell(currentCellIndex)} className="text-slate-400 hover:text-emerald-400 text-xs">Cell {currentCellIndex + 1}</Button></Tooltip>}
                {onClearOutputs && <Tooltip title="清除所有输出"><Button type="text" size="small" icon={<ClearOutlined />} onClick={onClearOutputs} className="text-slate-400 hover:text-amber-400" /></Tooltip>}
              </div>
            </div>
            <div className="flex items-center justify-between">
              <div className="flex gap-2 flex-wrap">
                <Tag color="green" className="text-xs">{contextInfo.codeCount} 代码</Tag>
                <Tag color="blue" className="text-xs">{contextInfo.mdCount} Markdown</Tag>
                {contextInfo.hasOutputs && <Tag color="orange" className="text-xs">有输出</Tag>}
              </div>
              <Tooltip title={isAuthorized ? 'AI 可以执行代码、安装包、操作单元格' : '开启后 AI 可以直接操作 Notebook'}>
                <div className="flex items-center gap-2">
                  <span className="text-slate-500 text-xs">允许 AI 操作</span>
                  <Switch 
                    size="small" 
                    checked={isAuthorized} 
                    onChange={setIsAuthorized}
                    className={isAuthorized ? 'bg-emerald-500' : ''}
                  />
                </div>
              </Tooltip>
            </div>
          </div>
        )}

        {/* 快捷操作 */}
        <div className="flex-shrink-0 px-4 py-2 border-b border-slate-800 bg-slate-900/50">
          <div className="flex flex-wrap gap-2">
            {quickActions.map(action => (
              <Button key={action.key} type="text" size="small" icon={action.icon} onClick={() => handleQuickAction(action)} disabled={isLoading} className="text-slate-400 hover:text-emerald-400 hover:bg-emerald-500/10 rounded-full text-xs">
                {action.label}
              </Button>
            ))}
          </div>
        </div>

        {/* 消息列表 */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {isLoadingHistory ? (
            <div className="flex items-center justify-center h-full"><Spin /></div>
          ) : messages.length === 0 && !streamingContent ? (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 flex items-center justify-center mb-4"><RobotOutlined className="text-3xl text-emerald-400" /></div>
              <h4 className="text-white font-medium mb-2">AI 编程助手</h4>
              <p className="text-slate-500 text-sm max-w-[250px]">我可以帮你分析数据、生成代码、解释错误等。试试上面的快捷操作吧！</p>
            </div>
          ) : (
            <>
              {messages.map(msg => <div key={msg.id}>{renderMessageContent(msg)}</div>)}
              {renderStreamingContent()}
            </>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* 输入区域 */}
        <div className="flex-shrink-0 p-4 border-t border-slate-800 bg-slate-900/80">
          <div className="relative">
            <TextArea ref={inputRef} value={inputValue} onChange={e => setInputValue(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(inputValue) }}} placeholder="输入问题或描述你想要的功能..." autoSize={{ minRows: 1, maxRows: 4 }} disabled={isLoading} className="bg-slate-800 border-slate-700 text-white resize-none pr-12 rounded-xl" />
            <div className="absolute right-2 bottom-2">
              {isLoading ? (
                <Button type="text" size="small" icon={<Spin size="small" />} onClick={stopGeneration} className="text-red-400 hover:text-red-300" />
              ) : (
                <Button type="text" size="small" icon={<SendOutlined />} onClick={() => sendMessage(inputValue)} disabled={!inputValue.trim()} className="text-emerald-400 hover:text-emerald-300 disabled:text-slate-600" />
              )}
            </div>
          </div>
          <p className="text-xs text-slate-600 mt-2 text-center">Enter 发送，Shift+Enter 换行</p>
        </div>
      </motion.div>
    </AnimatePresence>
  )
}

export default NotebookAgentPanel
