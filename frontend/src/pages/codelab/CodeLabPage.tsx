/**
 * CodeLabPage - 代码实验室主页面
 * 
 * 优化内容:
 * 1. 恢复顶部功能介绍区域 (Feature Cards)
 * 2. 使用 React 18 并发特性优化性能
 * 3. 集成 NotebookAgentPanel AI 助手
 * 4. 增强 Agent 对 Notebook 的控制能力
 * 5. 使用 useCallback/useMemo 减少不必要的重渲染
 */

import { useState, useEffect, useRef, useCallback, useMemo, useTransition, useDeferredValue } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { 
  Button, 
  Input, 
  Dropdown, 
  Modal, 
  message, 
  Tooltip, 
  Empty,
  Spin,
  Badge,
} from 'antd'
import {
  PlayCircleOutlined,
  PlusOutlined,
  DeleteOutlined,
  CodeOutlined,
  FileMarkdownOutlined,
  SaveOutlined,
  CaretRightOutlined,
  MoreOutlined,
  DownOutlined,
  UpOutlined,
  CopyOutlined,
  ClearOutlined,
  ReloadOutlined,
  FolderOutlined,
  ClockCircleOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
  RocketOutlined,
  ExperimentOutlined,
  ThunderboltOutlined,
  RobotOutlined,
  CloudOutlined,
  BarChartOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { codelabApi, Notebook, Cell, CellOutput } from '@/services/api'
import NotebookAgentPanel from '@/components/NotebookAgentPanel'
import dayjs from 'dayjs'

// ========== 功能卡片组件 ==========

interface FeatureCardProps {
  icon: React.ReactNode
  title: string
  description: string
  color: string
  delay?: number
}

const FeatureCard: React.FC<FeatureCardProps> = ({ icon, title, description, color, delay = 0 }) => (
  <motion.div
    initial={{ opacity: 0, y: 20 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ delay }}
    className={`relative overflow-hidden rounded-2xl bg-gradient-to-br ${color} p-[1px]`}
  >
    <div className="h-full bg-slate-900/95 backdrop-blur-xl rounded-2xl p-5">
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-xl bg-white/10 flex items-center justify-center text-white text-xl">
          {icon}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-white font-semibold text-base mb-1">{title}</h3>
          <p className="text-slate-400 text-sm leading-relaxed">{description}</p>
        </div>
      </div>
    </div>
  </motion.div>
)

// ========== 统计卡片组件 ==========

interface StatCardProps {
  title: string
  value: number
  icon: React.ReactNode
  color: string
}

const StatCard: React.FC<StatCardProps> = ({ title, value, icon, color }) => (
  <div className="bg-slate-800/50 rounded-xl p-4 border border-slate-700/50">
    <div className="flex items-center gap-3">
      <div className={`w-10 h-10 rounded-lg ${color} flex items-center justify-center text-white`}>
        {icon}
      </div>
      <div>
        <div className="text-2xl font-bold text-white">{value}</div>
        <div className="text-slate-400 text-sm">{title}</div>
      </div>
    </div>
  </div>
)

// ========== Cell 输出渲染组件 ==========

const CellOutputRenderer = ({ output }: { output: CellOutput }) => {
  if (output.output_type === 'stream') {
    return (
      <pre className="text-sm text-slate-300 font-mono whitespace-pre-wrap bg-slate-900/50 p-3 rounded-lg overflow-x-auto">
        {output.content}
      </pre>
    )
  }

  if (output.output_type === 'execute_result') {
    return (
      <div className="flex items-start gap-3">
        <span className="text-emerald-500 font-mono text-sm mt-0.5">Out:</span>
        <pre className="text-sm text-amber-400 font-mono whitespace-pre-wrap flex-1 overflow-x-auto">
          {output.content}
        </pre>
      </div>
    )
  }

  if (output.output_type === 'display_data' && output.mime_type === 'image/png') {
    return (
      <div className="flex justify-center py-2">
        <img 
          src={output.content} 
          alt="Plot output" 
          className="max-w-full rounded-lg shadow-lg border border-slate-700/50"
          style={{ maxHeight: '500px' }}
        />
      </div>
    )
  }

  if (output.output_type === 'error') {
    const error = output.content as { ename: string; evalue: string; traceback: string[] }
    return (
      <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4">
        <div className="flex items-center gap-2 text-red-400 font-semibold mb-2">
          <ExclamationCircleOutlined />
          <span>{error.ename}: {error.evalue}</span>
        </div>
        {error.traceback && error.traceback.length > 0 && (
          <pre className="text-xs text-red-300/80 font-mono whitespace-pre-wrap overflow-x-auto">
            {error.traceback.join('\n')}
          </pre>
        )}
      </div>
    )
  }

  return null
}

// ========== Notebook Cell 组件 ==========

interface NotebookCellProps {
  cell: Cell
  index: number
  isSelected: boolean
  isRunning: boolean
  onSelect: () => void
  onRun: () => void
  onDelete: () => void
  onUpdate: (source: string) => void
  onToggleType: () => void
  onMoveUp: () => void
  onMoveDown: () => void
  onAddCellBelow: () => void
  isFirst: boolean
  isLast: boolean
}

const NotebookCell = ({
  cell,
  index,
  isSelected,
  isRunning,
  onSelect,
  onRun,
  onDelete,
  onUpdate,
  onToggleType,
  onMoveUp,
  onMoveDown,
  onAddCellBelow,
  isFirst,
  isLast,
}: NotebookCellProps) => {
  const [isEditing, setIsEditing] = useState(cell.cell_type === 'code' || !cell.source)
  const editorRef = useRef<any>(null)
  const [isPending, startTransition] = useTransition()

  const handleEditorMount = useCallback((editor: any) => {
    editorRef.current = editor
  }, [])

  const handleEditorChange = useCallback((value: string | undefined) => {
    startTransition(() => {
      onUpdate(value || '')
    })
  }, [onUpdate])

  useEffect(() => {
    if (isSelected && cell.cell_type === 'code') {
      editorRef.current?.focus()
    }
  }, [isSelected, cell.cell_type])

  const cellActions = useMemo(() => [
    { key: 'run', icon: <CaretRightOutlined />, label: '运行', onClick: onRun, disabled: cell.cell_type !== 'code' },
    { key: 'toggle', icon: cell.cell_type === 'code' ? <FileMarkdownOutlined /> : <CodeOutlined />, label: cell.cell_type === 'code' ? '转为 Markdown' : '转为代码', onClick: onToggleType },
    { type: 'divider' },
    { key: 'up', icon: <UpOutlined />, label: '上移', onClick: onMoveUp, disabled: isFirst },
    { key: 'down', icon: <DownOutlined />, label: '下移', onClick: onMoveDown, disabled: isLast },
    { type: 'divider' },
    { key: 'copy', icon: <CopyOutlined />, label: '复制', onClick: () => navigator.clipboard.writeText(cell.source) },
    { key: 'clear', icon: <ClearOutlined />, label: '清除输出', onClick: () => {} },
    { type: 'divider' },
    { key: 'delete', icon: <DeleteOutlined />, label: '删除', onClick: onDelete, danger: true },
  ], [cell.cell_type, cell.source, onRun, onToggleType, onMoveUp, onMoveDown, onDelete, isFirst, isLast])

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -20 }}
      className={`group relative rounded-xl transition-all duration-200 ${
        isSelected
          ? 'ring-2 ring-emerald-500/50 bg-slate-800/60'
          : 'bg-slate-800/30 hover:bg-slate-800/40'
      } ${isPending ? 'opacity-80' : ''}`}
      onClick={onSelect}
    >
      {/* Cell 侧边操作区 */}
      <div className="absolute -left-12 top-2 flex flex-col items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <Tooltip title="运行 (Shift+Enter)" placement="left">
          <Button
            type="text"
            size="small"
            icon={isRunning ? <LoadingOutlined spin /> : <CaretRightOutlined />}
            onClick={(e) => { e.stopPropagation(); onRun() }}
            disabled={cell.cell_type !== 'code' || isRunning}
            className="text-slate-500 hover:text-emerald-400"
          />
        </Tooltip>
        <Dropdown
          menu={{ items: cellActions as any }}
          trigger={['click']}
          placement="bottomLeft"
        >
          <Button
            type="text"
            size="small"
            icon={<MoreOutlined />}
            onClick={(e) => e.stopPropagation()}
            className="text-slate-500 hover:text-white"
          />
        </Dropdown>
      </div>

      {/* Cell 内容区 */}
      <div className="p-4">
        {/* Cell 头部 */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
              cell.cell_type === 'code'
                ? 'bg-emerald-500/20 text-emerald-400'
                : 'bg-blue-500/20 text-blue-400'
            }`}>
              {cell.cell_type === 'code' ? 'Python' : 'Markdown'}
            </span>
            {cell.execution_count && (
              <span className="text-slate-500 text-xs font-mono">
                [{cell.execution_count}]
              </span>
            )}
            {isRunning && (
              <span className="flex items-center gap-1 text-amber-400 text-xs">
                <LoadingOutlined spin />
                运行中...
              </span>
            )}
          </div>
          
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <Tooltip title="添加单元格">
              <Button
                type="text"
                size="small"
                icon={<PlusOutlined />}
                onClick={(e) => { e.stopPropagation(); onAddCellBelow() }}
                className="text-slate-500 hover:text-white"
              />
            </Tooltip>
          </div>
        </div>

        {/* Cell 编辑器/内容 */}
        {cell.cell_type === 'code' ? (
          <div className="rounded-lg overflow-hidden border border-slate-700/50">
            <Editor
              height={Math.max(100, Math.min(400, (cell.source.split('\n').length + 1) * 20))}
              defaultLanguage="python"
              value={cell.source}
              onChange={handleEditorChange}
              onMount={handleEditorMount}
              theme="vs-dark"
              options={{
                minimap: { enabled: false },
                fontSize: 14,
                lineNumbers: 'on',
                scrollBeyondLastLine: false,
                automaticLayout: true,
                tabSize: 4,
                wordWrap: 'on',
                padding: { top: 12, bottom: 12 },
                renderLineHighlight: 'none',
                overviewRulerLanes: 0,
                hideCursorInOverviewRuler: true,
                overviewRulerBorder: false,
                scrollbar: { vertical: 'hidden', horizontal: 'hidden' },
              }}
            />
          </div>
        ) : (
          <div className="min-h-[60px] cursor-text" onDoubleClick={() => setIsEditing(true)}>
            {isEditing ? (
              <div className="rounded-lg overflow-hidden border border-slate-700/50">
                <Editor
                  height={Math.max(100, Math.min(300, (cell.source.split('\n').length + 1) * 20))}
                  defaultLanguage="markdown"
                  value={cell.source}
                  onChange={handleEditorChange}
                  onMount={handleEditorMount}
                  theme="vs-dark"
                  options={{
                    minimap: { enabled: false },
                    fontSize: 14,
                    lineNumbers: 'off',
                    scrollBeyondLastLine: false,
                    automaticLayout: true,
                    wordWrap: 'on',
                    padding: { top: 12, bottom: 12 },
                  }}
                />
              </div>
            ) : (
              <div className="prose prose-invert prose-sm max-w-none px-2">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    code({ node, inline, className, children, ...props }: any) {
                      const match = /language-(\w+)/.exec(className || '')
                      return !inline && match ? (
                        <SyntaxHighlighter style={oneDark} language={match[1]} PreTag="div" {...props}>
                          {String(children).replace(/\n$/, '')}
                        </SyntaxHighlighter>
                      ) : (
                        <code className={className} {...props}>{children}</code>
                      )
                    },
                  }}
                >
                  {cell.source || '*双击编辑 Markdown...*'}
                </ReactMarkdown>
              </div>
            )}
          </div>
        )}

        {/* Cell 输出区 */}
        {cell.outputs && cell.outputs.length > 0 && (
          <div className="mt-4 space-y-2 border-t border-slate-700/30 pt-4">
            {cell.outputs.map((output, i) => (
              <CellOutputRenderer key={i} output={output} />
            ))}
          </div>
        )}
      </div>
    </motion.div>
  )
}

// ========== 主页面组件 ==========

const CodeLabPage = () => {
  const navigate = useNavigate()
  const { notebookId } = useParams<{ notebookId: string }>()
  
  const [notebooks, setNotebooks] = useState<Notebook[]>([])
  const [currentNotebook, setCurrentNotebook] = useState<Notebook | null>(null)
  const [selectedCellIndex, setSelectedCellIndex] = useState<number>(0)
  const [runningCells, setRunningCells] = useState<Set<string>>(new Set())
  const [isLoading, setIsLoading] = useState(false)
  const [isListLoading, setIsListLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  
  // Agent 面板状态
  const [showAgentPanel, setShowAgentPanel] = useState(false)
  const [isAgentExpanded, setIsAgentExpanded] = useState(false)

  // 性能优化: useTransition 和 useDeferredValue
  const [isPending, startTransition] = useTransition()
  const deferredNotebooks = useDeferredValue(notebooks)

  // 计算统计信息
  const stats = useMemo(() => ({
    totalNotebooks: notebooks.length,
    totalCells: notebooks.reduce((acc, nb) => acc + nb.cells.length, 0),
    totalExecutions: notebooks.reduce((acc, nb) => acc + nb.execution_count, 0),
  }), [notebooks])

  // 加载 Notebook 列表
  const loadNotebooks = useCallback(async () => {
    setIsListLoading(true)
    setLoadError(null)
    try {
      const data = await codelabApi.listNotebooks()
      startTransition(() => { setNotebooks(data) })
    } catch (error: any) {
      console.error('加载 Notebook 列表失败:', error)
      setLoadError(error.response?.status === 401 ? '登录已过期，请重新登录' : (error.message || '加载列表失败'))
    } finally {
      setIsListLoading(false)
    }
  }, [])

  // 加载单个 Notebook
  const loadNotebook = useCallback(async (id: string) => {
    setIsLoading(true)
    try {
      const data = await codelabApi.getNotebook(id)
      setCurrentNotebook(data)
      setSelectedCellIndex(0)
    } catch (error) {
      message.error('加载 Notebook 失败')
      navigate('/code')
    } finally {
      setIsLoading(false)
    }
  }, [navigate])

  // 创建新 Notebook
  const createNotebook = useCallback(async () => {
    try {
      const data = await codelabApi.createNotebook({ title: '未命名 Notebook' })
      setNotebooks(prev => [data, ...prev])
      navigate(`/code/${data.id}`)
    } catch (error) {
      message.error('创建 Notebook 失败')
    }
  }, [navigate])

  // 保存 Notebook
  const saveNotebook = useCallback(async () => {
    if (!currentNotebook) return
    setIsSaving(true)
    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells: currentNotebook.cells,
      })
      message.success('保存成功')
    } catch (error) {
      message.error('保存失败')
    } finally {
      setIsSaving(false)
    }
  }, [currentNotebook])

  // 删除 Notebook
  const deleteNotebook = useCallback(async (id: string) => {
    Modal.confirm({
      title: '删除 Notebook',
      content: '确定要删除这个 Notebook 吗？此操作不可撤销。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await codelabApi.deleteNotebook(id)
          setNotebooks(prev => prev.filter(n => n.id !== id))
          if (currentNotebook?.id === id) {
            setCurrentNotebook(null)
            navigate('/code')
          }
          message.success('Notebook 已删除')
        } catch (error) {
          message.error('删除失败')
        }
      },
    })
  }, [currentNotebook, navigate])

  // 运行单元格
  const runCell = useCallback(async (cellId: string, code: string) => {
    if (!currentNotebook) return
    
    setRunningCells(prev => new Set(prev).add(cellId))
    try {
      const result = await codelabApi.executeCell(currentNotebook.id, {
        code,
        cell_id: cellId,
        timeout: 30,
      })
      
      startTransition(() => {
        setCurrentNotebook(prev => {
          if (!prev) return prev
          return {
            ...prev,
            cells: prev.cells.map(cell =>
              cell.id === cellId
                ? { ...cell, outputs: result.outputs, execution_count: result.execution_count }
                : cell
            ),
          }
        })
      })
      
      if (!result.success) message.warning('代码执行出错')
    } catch (error) {
      message.error('执行失败')
    } finally {
      setRunningCells(prev => {
        const next = new Set(prev)
        next.delete(cellId)
        return next
      })
    }
  }, [currentNotebook])

  // 运行所有单元格
  const runAllCells = useCallback(async () => {
    if (!currentNotebook) return
    for (const cell of currentNotebook.cells) {
      if (cell.cell_type === 'code' && cell.source.trim()) {
        await runCell(cell.id, cell.source)
      }
    }
  }, [currentNotebook, runCell])

  // 重启内核
  const restartKernel = useCallback(async () => {
    if (!currentNotebook) return
    Modal.confirm({
      title: '重启内核',
      content: '重启内核将清除所有变量和执行状态。确定要继续吗？',
      okText: '重启',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await codelabApi.restartKernel(currentNotebook.id)
          startTransition(() => {
            setCurrentNotebook(prev => {
              if (!prev) return prev
              return {
                ...prev,
                cells: prev.cells.map(cell => ({ ...cell, outputs: [], execution_count: null })),
                execution_count: 0,
              }
            })
          })
          message.success('内核已重启')
        } catch (error) {
          message.error('重启内核失败')
        }
      },
    })
  }, [currentNotebook])

  // 更新单元格内容
  const updateCell = useCallback((cellId: string, source: string) => {
    startTransition(() => {
      setCurrentNotebook(prev => {
        if (!prev) return prev
        return {
          ...prev,
          cells: prev.cells.map(cell => cell.id === cellId ? { ...cell, source } : cell),
        }
      })
    })
  }, [])

  // 添加单元格
  const addCell = useCallback((index: number, cellType: 'code' | 'markdown' = 'code') => {
    const newCell: Cell = {
      id: crypto.randomUUID(),
      cell_type: cellType,
      source: '',
      outputs: [],
      execution_count: null,
      metadata: {},
    }
    
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cells = [...prev.cells]
      cells.splice(index + 1, 0, newCell)
      return { ...prev, cells }
    })
    
    setSelectedCellIndex(index + 1)
  }, [])

  // 删除单元格
  const deleteCell = useCallback((cellId: string) => {
    if (!currentNotebook || currentNotebook.cells.length <= 1) {
      message.warning('至少保留一个单元格')
      return
    }
    
    setCurrentNotebook(prev => {
      if (!prev) return prev
      return { ...prev, cells: prev.cells.filter(c => c.id !== cellId) }
    })
    
    setSelectedCellIndex(prev => Math.max(0, prev - 1))
  }, [currentNotebook])

  // 切换单元格类型
  const toggleCellType = useCallback((cellId: string) => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      return {
        ...prev,
        cells: prev.cells.map(cell =>
          cell.id === cellId
            ? { ...cell, cell_type: cell.cell_type === 'code' ? 'markdown' : 'code', outputs: [] }
            : cell
        ),
      }
    })
  }, [])

  // 移动单元格
  const moveCell = useCallback((cellId: string, direction: 'up' | 'down') => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cells = [...prev.cells]
      const index = cells.findIndex(c => c.id === cellId)
      if (index === -1) return prev
      
      const newIndex = direction === 'up' ? index - 1 : index + 1
      if (newIndex < 0 || newIndex >= cells.length) return prev
      
      [cells[index], cells[newIndex]] = [cells[newIndex], cells[index]]
      return { ...prev, cells }
    })
    
    setSelectedCellIndex(prev => {
      const delta = direction === 'up' ? -1 : 1
      return Math.max(0, Math.min((currentNotebook?.cells.length || 1) - 1, prev + delta))
    })
  }, [currentNotebook])

  // ========== Agent 回调函数 ==========

  const handleAgentInsertCode = useCallback((code: string) => {
    if (!currentNotebook) return
    
    const newCell: Cell = {
      id: crypto.randomUUID(),
      cell_type: 'code',
      source: code,
      outputs: [],
      execution_count: null,
      metadata: { from_agent: true },
    }
    
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cells = [...prev.cells]
      cells.splice(selectedCellIndex + 1, 0, newCell)
      return { ...prev, cells }
    })
    
    setSelectedCellIndex(selectedCellIndex + 1)
    message.success('代码已插入')
  }, [currentNotebook, selectedCellIndex])

  const handleAgentRunCode = useCallback(async (code: string) => {
    if (!currentNotebook) return
    
    const newCellId = crypto.randomUUID()
    const newCell: Cell = {
      id: newCellId,
      cell_type: 'code',
      source: code,
      outputs: [],
      execution_count: null,
      metadata: { from_agent: true },
    }
    
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cells = [...prev.cells]
      cells.splice(selectedCellIndex + 1, 0, newCell)
      return { ...prev, cells }
    })
    
    setSelectedCellIndex(selectedCellIndex + 1)
    setTimeout(() => { runCell(newCellId, code) }, 100)
  }, [currentNotebook, selectedCellIndex, runCell])

  const handleAgentFocusCell = useCallback((cellIndex: number) => {
    if (!currentNotebook) return
    setSelectedCellIndex(Math.max(0, Math.min(currentNotebook.cells.length - 1, cellIndex)))
  }, [currentNotebook])

  const handleAgentClearOutputs = useCallback(() => {
    if (!currentNotebook) return
    startTransition(() => {
      setCurrentNotebook(prev => {
        if (!prev) return prev
        return {
          ...prev,
          cells: prev.cells.map(cell => ({ ...cell, outputs: [], execution_count: null })),
        }
      })
    })
    message.success('所有输出已清除')
  }, [currentNotebook])

  // 初始化加载
  useEffect(() => { loadNotebooks() }, [loadNotebooks])

  useEffect(() => {
    if (notebookId) {
      loadNotebook(notebookId)
    } else {
      setCurrentNotebook(null)
    }
  }, [notebookId, loadNotebook])

  // 键盘快捷键
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        if (currentNotebook) saveNotebook()
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && currentNotebook) {
        e.preventDefault()
        const cell = currentNotebook.cells[selectedCellIndex]
        if (cell && cell.cell_type === 'code') runCell(cell.id, cell.source)
      }
    }
    
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [currentNotebook, selectedCellIndex, saveNotebook, runCell])

  // ========== 渲染 Notebook 列表视图 ==========
  
  if (!currentNotebook) {
    return (
      <div className="h-full flex flex-col bg-slate-950">
        {/* 渐变背景 */}
        <div className="absolute inset-0 overflow-hidden pointer-events-none">
          <div className="absolute top-0 left-1/4 w-96 h-96 bg-amber-500/10 rounded-full filter blur-3xl" />
          <div className="absolute bottom-1/4 right-1/4 w-80 h-80 bg-orange-500/10 rounded-full filter blur-3xl" />
        </div>

        {/* 头部 */}
        <div className="relative flex-shrink-0 h-16 px-6 flex items-center justify-between bg-slate-900/60 border-b border-slate-800 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-amber-500 to-orange-600 flex items-center justify-center shadow-lg shadow-amber-500/20">
              <ExperimentOutlined className="text-white text-2xl" />
            </div>
            <div>
              <h1 className="text-white font-bold text-xl">代码实验室</h1>
              <p className="text-slate-400 text-sm">Jupyter-style 交互式 Python 环境</p>
            </div>
          </div>
          
          <Button
            type="primary"
            size="large"
            icon={<PlusOutlined />}
            onClick={createNotebook}
            className="rounded-xl h-11 px-6 bg-gradient-to-r from-amber-500 to-orange-500 border-0 shadow-lg shadow-amber-500/20"
          >
            新建 Notebook
          </Button>
        </div>

        {/* 主内容区 */}
        <div className="relative flex-1 overflow-y-auto p-6">
          <div className="max-w-5xl mx-auto">
            {/* 欢迎区域和功能介绍 */}
            <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="mb-10">
              {/* 统计卡片 */}
              <div className="grid grid-cols-3 gap-4 mb-8">
                <StatCard title="Notebooks" value={stats.totalNotebooks} icon={<FolderOutlined />} color="bg-amber-500/80" />
                <StatCard title="总单元格" value={stats.totalCells} icon={<CodeOutlined />} color="bg-emerald-500/80" />
                <StatCard title="执行次数" value={stats.totalExecutions} icon={<ThunderboltOutlined />} color="bg-blue-500/80" />
              </div>

              {/* 功能卡片 */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
                <FeatureCard icon={<CodeOutlined />} title="Python 代码执行" description="支持完整的 Python 环境，包括 numpy、pandas、matplotlib 等科学计算库" color="from-emerald-500/50 to-teal-600/50" delay={0.1} />
                <FeatureCard icon={<BarChartOutlined />} title="数据可视化" description="内置图表渲染，支持 matplotlib、seaborn 等绑图库的实时可视化" color="from-blue-500/50 to-cyan-600/50" delay={0.15} />
                <FeatureCard icon={<RobotOutlined />} title="AI 助手" description="智能代码补全和错误分析，帮助你快速解决编程问题" color="from-purple-500/50 to-pink-600/50" delay={0.2} />
                <FeatureCard icon={<CloudOutlined />} title="云端同步" description="所有 Notebook 自动保存到云端，随时随地访问你的代码" color="from-amber-500/50 to-orange-600/50" delay={0.25} />
              </div>
            </motion.div>

            {/* Notebook 列表 */}
            <div className="space-y-4">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2 text-slate-400">
                  <FolderOutlined />
                  <span className="font-medium">我的 Notebooks</span>
                  <Badge count={notebooks.length} showZero color="#52525b" />
                </div>
                <Button type="text" icon={<ReloadOutlined />} onClick={loadNotebooks} loading={isListLoading} className="text-slate-400 hover:text-white">
                  刷新
                </Button>
              </div>

              {isListLoading ? (
                <div className="flex flex-col items-center justify-center py-16">
                  <Spin size="large" />
                  <p className="text-slate-500 mt-4">加载中...</p>
                </div>
              ) : loadError ? (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <ExclamationCircleOutlined className="text-4xl text-red-400 mb-4" />
                  <p className="text-red-400 mb-4">{loadError}</p>
                  <Button onClick={loadNotebooks} icon={<ReloadOutlined />} className="rounded-lg">重试</Button>
                </div>
              ) : deferredNotebooks.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<div className="text-slate-500"><p>还没有 Notebook</p><p className="text-sm mt-1">点击上方按钮创建你的第一个交互式代码笔记本</p></div>} />
              ) : (
                <div className="grid gap-4">
                  {deferredNotebooks.map((nb, i) => (
                    <motion.div key={nb.id} initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i * 0.03 }}
                      className="group bg-slate-800/40 rounded-xl p-5 border border-slate-700/50 hover:border-amber-500/30 hover:bg-slate-800/60 cursor-pointer transition-all"
                      onClick={() => navigate(`/code/${nb.id}`)}>
                      <div className="flex items-start justify-between">
                        <div className="flex items-start gap-4">
                          <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-amber-500/20 to-orange-500/20 flex items-center justify-center border border-amber-500/20">
                            <ExperimentOutlined className="text-amber-400 text-xl" />
                          </div>
                          <div>
                            <h3 className="text-white font-semibold text-lg group-hover:text-amber-400 transition-colors">{nb.title}</h3>
                            <p className="text-slate-500 text-sm mt-1">{nb.cells.length} 个单元格 · 执行 {nb.execution_count} 次</p>
                            <p className="text-slate-600 text-xs mt-2 flex items-center gap-1"><ClockCircleOutlined />更新于 {dayjs(nb.updated_at).format('YYYY-MM-DD HH:mm')}</p>
                          </div>
                        </div>
                        <Dropdown menu={{ items: [{ key: 'open', icon: <CodeOutlined />, label: '打开' }, { type: 'divider' }, { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true }], onClick: ({ key, domEvent }) => { domEvent.stopPropagation(); if (key === 'delete') deleteNotebook(nb.id); if (key === 'open') navigate(`/code/${nb.id}`) }}} trigger={['click']}>
                          <Button type="text" icon={<MoreOutlined />} onClick={(e) => e.stopPropagation()} className="text-slate-500 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity" />
                        </Dropdown>
                      </div>
                    </motion.div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ========== 渲染 Notebook 编辑视图 ==========
  
  return (
    <div className="h-full flex bg-slate-950">
      {/* 主内容区 */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* 工具栏 */}
        <div className="flex-shrink-0 h-14 px-4 flex items-center justify-between bg-slate-900/80 border-b border-slate-800 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <Button type="text" icon={<FolderOutlined />} onClick={() => navigate('/code')} className="text-slate-400 hover:text-white">返回列表</Button>
            <div className="h-6 w-px bg-slate-700" />
            <Input value={currentNotebook.title} onChange={(e) => setCurrentNotebook(prev => prev ? { ...prev, title: e.target.value } : prev)} variant="borderless" className="text-white font-semibold text-lg w-64 hover:bg-slate-800/50 rounded-lg px-2" placeholder="Notebook 标题" />
          </div>

          <div className="flex items-center gap-2">
            <Tooltip title="全部运行"><Button type="text" icon={<PlayCircleOutlined />} onClick={runAllCells} className="text-slate-400 hover:text-emerald-400">全部运行</Button></Tooltip>
            <Tooltip title="重启内核"><Button type="text" icon={<ReloadOutlined />} onClick={restartKernel} className="text-slate-400 hover:text-amber-400">重启内核</Button></Tooltip>
            <Tooltip title="添加代码单元格"><Button type="text" icon={<PlusOutlined />} onClick={() => addCell(currentNotebook.cells.length - 1, 'code')} className="text-slate-400 hover:text-white" /></Tooltip>
            <div className="h-6 w-px bg-slate-700 mx-2" />
            <Tooltip title="AI 助手">
              <Badge dot={showAgentPanel} offset={[-5, 5]}>
                <Button type={showAgentPanel ? 'primary' : 'text'} icon={<RobotOutlined />} onClick={() => setShowAgentPanel(!showAgentPanel)} className={showAgentPanel ? 'rounded-lg' : 'text-slate-400 hover:text-emerald-400'}>AI 助手</Button>
              </Badge>
            </Tooltip>
            <div className="h-6 w-px bg-slate-700 mx-2" />
            <Button type="primary" icon={isSaving ? <LoadingOutlined spin /> : <SaveOutlined />} onClick={saveNotebook} disabled={isSaving} className="rounded-lg">保存</Button>
          </div>
        </div>

        {/* Notebook 内容 */}
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-4xl mx-auto py-6 px-4 pl-16">
            {isLoading ? (
              <div className="flex items-center justify-center py-20"><Spin size="large" /></div>
            ) : (
              <AnimatePresence mode="popLayout">
                {currentNotebook.cells.map((cell, index) => (
                  <div key={cell.id} className="mb-4">
                    <NotebookCell cell={cell} index={index} isSelected={selectedCellIndex === index} isRunning={runningCells.has(cell.id)} onSelect={() => setSelectedCellIndex(index)} onRun={() => runCell(cell.id, cell.source)} onDelete={() => deleteCell(cell.id)} onUpdate={(source) => updateCell(cell.id, source)} onToggleType={() => toggleCellType(cell.id)} onMoveUp={() => moveCell(cell.id, 'up')} onMoveDown={() => moveCell(cell.id, 'down')} onAddCellBelow={() => addCell(index, 'code')} isFirst={index === 0} isLast={index === currentNotebook.cells.length - 1} />
                  </div>
                ))}
              </AnimatePresence>
            )}

            {/* 添加单元格按钮 */}
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center justify-center gap-4 py-8 border-2 border-dashed border-slate-800 rounded-xl hover:border-slate-700 transition-colors">
              <Button type="text" icon={<CodeOutlined />} onClick={() => addCell(currentNotebook.cells.length - 1, 'code')} className="text-slate-500 hover:text-emerald-400">+ 代码</Button>
              <Button type="text" icon={<FileMarkdownOutlined />} onClick={() => addCell(currentNotebook.cells.length - 1, 'markdown')} className="text-slate-500 hover:text-blue-400">+ Markdown</Button>
            </motion.div>
          </div>
        </div>
      </div>

      {/* Agent 面板 */}
      <NotebookAgentPanel
        notebookId={currentNotebook.id}
        isVisible={showAgentPanel}
        onClose={() => setShowAgentPanel(false)}
        onToggleExpand={() => setIsAgentExpanded(!isAgentExpanded)}
        isExpanded={isAgentExpanded}
        onInsertCode={handleAgentInsertCode}
        onRunCode={handleAgentRunCode}
        onFocusCell={handleAgentFocusCell}
        onClearOutputs={handleAgentClearOutputs}
        currentCellIndex={selectedCellIndex}
        cells={currentNotebook.cells}
      />
    </div>
  )
}

export default CodeLabPage
