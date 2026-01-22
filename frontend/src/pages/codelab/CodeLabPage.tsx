import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { 
  Button, 
  Input, 
  Dropdown, 
  Modal, 
  message, 
  Tooltip, 
  Empty,
  Spin
} from 'antd'
import {
  PlayCircleOutlined,
  PlusOutlined,
  DeleteOutlined,
  CodeOutlined,
  FileMarkdownOutlined,
  SaveOutlined,
  CaretRightOutlined,
  StopOutlined,
  MoreOutlined,
  DownOutlined,
  UpOutlined,
  CopyOutlined,
  ClearOutlined,
  ReloadOutlined,
  FileAddOutlined,
  FolderOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
  RocketOutlined,
  ExperimentOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence, Reorder } from 'framer-motion'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { codelabApi, Notebook, Cell, CellOutput } from '@/services/api'
import dayjs from 'dayjs'

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

  const handleEditorMount = (editor: any) => {
    editorRef.current = editor
  }

  const handleKeyDown = (e: KeyboardEvent) => {
    // Shift + Enter 运行当前 cell
    if (e.shiftKey && e.key === 'Enter') {
      e.preventDefault()
      onRun()
    }
  }

  useEffect(() => {
    if (isSelected && cell.cell_type === 'code') {
      editorRef.current?.focus()
    }
  }, [isSelected, cell.cell_type])

  const cellActions = [
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
  ]

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
      }`}
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
          menu={{
            items: cellActions.filter(a => !a.type).map(a => ({
              key: a.key!,
              icon: a.icon,
              label: a.label,
              onClick: a.onClick,
              disabled: a.disabled,
              danger: a.danger,
            })),
          }}
          trigger={['click']}
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

      {/* Cell 头部 */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-700/30">
        <div className="flex items-center gap-3">
          {/* 执行计数或类型标识 */}
          <div className={`w-16 text-xs font-mono ${
            cell.cell_type === 'code' 
              ? cell.execution_count 
                ? 'text-emerald-400' 
                : 'text-slate-500'
              : 'text-blue-400'
          }`}>
            {cell.cell_type === 'code' 
              ? cell.execution_count ? `In [${cell.execution_count}]:` : 'In [ ]:'
              : 'Markdown'}
          </div>
          {/* 运行状态 */}
          {isRunning && (
            <div className="flex items-center gap-2 text-amber-400 text-xs">
              <LoadingOutlined spin />
              <span>运行中...</span>
            </div>
          )}
        </div>
        
        {/* 右侧操作按钮 */}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <Tooltip title="添加单元格">
            <Button
              type="text"
              size="small"
              icon={<PlusOutlined />}
              onClick={(e) => { e.stopPropagation(); onAddCellBelow() }}
              className="text-slate-500 hover:text-emerald-400"
            />
          </Tooltip>
        </div>
      </div>

      {/* Cell 内容区 */}
      <div className="p-4">
        {cell.cell_type === 'code' ? (
          <div className="rounded-lg overflow-hidden border border-slate-700/50">
            <Editor
              height={Math.max(80, Math.min(400, cell.source.split('\n').length * 20 + 20))}
              language="python"
              theme="vs-dark"
              value={cell.source}
              onChange={(value) => onUpdate(value || '')}
              onMount={handleEditorMount}
              options={{
                minimap: { enabled: false },
                scrollBeyondLastLine: false,
                lineNumbers: 'on',
                glyphMargin: false,
                folding: false,
                lineDecorationsWidth: 10,
                lineNumbersMinChars: 3,
                renderLineHighlight: 'line',
                scrollbar: {
                  vertical: 'auto',
                  horizontal: 'auto',
                  verticalScrollbarSize: 8,
                  horizontalScrollbarSize: 8,
                },
                fontSize: 13,
                fontFamily: "'JetBrains Mono', 'Fira Code', Menlo, monospace",
                padding: { top: 10, bottom: 10 },
                wordWrap: 'on',
              }}
            />
          </div>
        ) : (
          <div
            className="min-h-[60px] cursor-text"
            onClick={() => setIsEditing(true)}
          >
            {isEditing || !cell.source ? (
              <Editor
                height={Math.max(80, Math.min(300, cell.source.split('\n').length * 20 + 20))}
                language="markdown"
                theme="vs-dark"
                value={cell.source}
                onChange={(value) => onUpdate(value || '')}
                onMount={handleEditorMount}
                options={{
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  lineNumbers: 'off',
                  glyphMargin: false,
                  folding: false,
                  renderLineHighlight: 'none',
                  scrollbar: { vertical: 'auto', horizontal: 'auto' },
                  fontSize: 14,
                  wordWrap: 'on',
                  padding: { top: 10, bottom: 10 },
                }}
              />
            ) : (
              <div 
                className="prose prose-invert prose-sm max-w-none px-2"
                onDoubleClick={() => setIsEditing(true)}
              >
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    code({ node, className, children, ...props }: any) {
                      const match = /language-(\w+)/.exec(className || '')
                      const inline = !match
                      return inline ? (
                        <code className="bg-slate-700/50 px-1.5 py-0.5 rounded text-emerald-400" {...props}>
                          {children}
                        </code>
                      ) : (
                        <SyntaxHighlighter
                          style={oneDark}
                          language={match[1]}
                          PreTag="div"
                        >
                          {String(children).replace(/\n$/, '')}
                        </SyntaxHighlighter>
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
  const [showNotebookList, setShowNotebookList] = useState(!notebookId)
  const [loadError, setLoadError] = useState<string | null>(null)

  // 加载 Notebook 列表
  const loadNotebooks = useCallback(async () => {
    setIsListLoading(true)
    setLoadError(null)
    try {
      const data = await codelabApi.listNotebooks()
      setNotebooks(data)
    } catch (error: any) {
      console.error('加载 Notebook 列表失败:', error)
      if (error.response?.status === 401) {
        setLoadError('登录已过期，请重新登录')
        message.error('登录已过期')
      } else {
        setLoadError(error.message || '加载列表失败，请检查网络连接')
      }
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
      setShowNotebookList(false)
    } catch (error) {
      message.error('加载 Notebook 失败')
      navigate('/code')
    } finally {
      setIsLoading(false)
    }
  }, [navigate])

  // 创建新 Notebook
  const createNotebook = async () => {
    try {
      const data = await codelabApi.createNotebook({ title: '未命名 Notebook' })
      setNotebooks(prev => [data, ...prev])
      navigate(`/code/${data.id}`)
    } catch (error) {
      message.error('创建 Notebook 失败')
    }
  }

  // 保存 Notebook
  const saveNotebook = async () => {
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
  }

  // 删除 Notebook
  const deleteNotebook = async (id: string) => {
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
  }

  // 运行单元格
  const runCell = async (cellId: string, code: string) => {
    if (!currentNotebook) return
    
    setRunningCells(prev => new Set(prev).add(cellId))
    try {
      const result = await codelabApi.executeCell(currentNotebook.id, {
        code,
        cell_id: cellId,
        timeout: 30,
      })
      
      // 更新单元格输出
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
      
      if (!result.success) {
        message.warning('代码执行出错')
      }
    } catch (error) {
      message.error('执行失败')
    } finally {
      setRunningCells(prev => {
        const next = new Set(prev)
        next.delete(cellId)
        return next
      })
    }
  }

  // 运行所有单元格
  const runAllCells = async () => {
    if (!currentNotebook) return
    
    for (const cell of currentNotebook.cells) {
      if (cell.cell_type === 'code' && cell.source.trim()) {
        await runCell(cell.id, cell.source)
      }
    }
  }

  // 重启内核（清除所有变量状态）
  const restartKernel = async () => {
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
          // 清除所有 cell 的输出
          setCurrentNotebook(prev => {
            if (!prev) return prev
            return {
              ...prev,
              cells: prev.cells.map(cell => ({
                ...cell,
                outputs: [],
                execution_count: null,
              })),
              execution_count: 0,
            }
          })
          message.success('内核已重启，所有变量已清除')
        } catch (error) {
          message.error('重启内核失败')
        }
      },
    })
  }

  // 更新单元格内容
  const updateCell = (cellId: string, source: string) => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      return {
        ...prev,
        cells: prev.cells.map(cell =>
          cell.id === cellId ? { ...cell, source } : cell
        ),
      }
    })
  }

  // 添加单元格
  const addCell = (index: number, cellType: 'code' | 'markdown' = 'code') => {
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
  }

  // 删除单元格
  const deleteCell = (cellId: string) => {
    if (!currentNotebook || currentNotebook.cells.length <= 1) {
      message.warning('至少保留一个单元格')
      return
    }
    
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cellIndex = prev.cells.findIndex(c => c.id === cellId)
      const cells = prev.cells.filter(c => c.id !== cellId)
      
      if (selectedCellIndex >= cells.length) {
        setSelectedCellIndex(cells.length - 1)
      } else if (selectedCellIndex > cellIndex) {
        setSelectedCellIndex(selectedCellIndex - 1)
      }
      
      return { ...prev, cells }
    })
  }

  // 切换单元格类型
  const toggleCellType = (cellId: string) => {
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
  }

  // 移动单元格
  const moveCell = (cellId: string, direction: 'up' | 'down') => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cells = [...prev.cells]
      const index = cells.findIndex(c => c.id === cellId)
      const newIndex = direction === 'up' ? index - 1 : index + 1
      
      if (newIndex < 0 || newIndex >= cells.length) return prev
      
      ;[cells[index], cells[newIndex]] = [cells[newIndex], cells[index]]
      setSelectedCellIndex(newIndex)
      
      return { ...prev, cells }
    })
  }

  // 初始化加载
  useEffect(() => {
    loadNotebooks()
  }, [loadNotebooks])

  useEffect(() => {
    if (notebookId) {
      loadNotebook(notebookId)
    } else {
      setCurrentNotebook(null)
      setShowNotebookList(true)
    }
  }, [notebookId, loadNotebook])

  // 键盘快捷键
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!currentNotebook) return
      
      // Ctrl/Cmd + S 保存
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        saveNotebook()
      }
      
      // Ctrl/Cmd + Enter 运行当前单元格
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault()
        const cell = currentNotebook.cells[selectedCellIndex]
        if (cell && cell.cell_type === 'code') {
          runCell(cell.id, cell.source)
        }
      }
    }
    
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [currentNotebook, selectedCellIndex])

  // Notebook 列表视图
  if (showNotebookList || !currentNotebook) {
    return (
      <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-900 to-slate-950">
        <div className="max-w-5xl mx-auto p-8">
          {/* 头部 */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="mb-8"
          >
            <div className="flex items-center gap-4 mb-6">
              <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-amber-500 to-orange-600 flex items-center justify-center shadow-lg shadow-amber-500/20">
                <ExperimentOutlined className="text-2xl text-white" />
              </div>
              <div>
                <h1 className="text-3xl font-bold text-white">代码实验室</h1>
                <p className="text-slate-400 mt-1">Jupyter-style 交互式 Python 环境</p>
              </div>
            </div>

            {/* 功能介绍 */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
              {[
                { icon: <CodeOutlined />, title: '多单元格编辑', desc: '支持代码和 Markdown' },
                { icon: <ThunderboltOutlined />, title: '实时执行', desc: 'numpy, pandas, matplotlib' },
                { icon: <RocketOutlined />, title: '可视化输出', desc: '图表、数据表格' },
              ].map((item, i) => (
                <motion.div
                  key={i}
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.1 + i * 0.1 }}
                  className="bg-slate-800/40 rounded-xl p-4 border border-slate-700/50"
                >
                  <div className="text-amber-400 text-xl mb-2">{item.icon}</div>
                  <div className="text-white font-medium">{item.title}</div>
                  <div className="text-slate-500 text-sm">{item.desc}</div>
                </motion.div>
              ))}
            </div>

            {/* 创建按钮 */}
            <Button
              type="primary"
              size="large"
              icon={<FileAddOutlined />}
              onClick={createNotebook}
              className="rounded-xl h-12 px-8 bg-gradient-to-r from-amber-500 to-orange-600 border-0 shadow-lg shadow-amber-500/20"
            >
              新建 Notebook
            </Button>
          </motion.div>

          {/* Notebook 列表 */}
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-slate-400 mb-4">
              <FolderOutlined />
              <span>我的 Notebooks</span>
              <span className="text-slate-600">({notebooks.length})</span>
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
                <Button 
                  onClick={loadNotebooks}
                  icon={<ReloadOutlined />}
                  className="rounded-lg"
                >
                  重试
                </Button>
              </div>
            ) : notebooks.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div className="text-slate-500">
                    <p>还没有 Notebook</p>
                    <p className="text-sm mt-1">创建你的第一个交互式代码笔记本吧</p>
                  </div>
                }
              />
            ) : (
              <div className="grid gap-4">
                {notebooks.map((nb, i) => (
                  <motion.div
                    key={nb.id}
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: i * 0.05 }}
                    className="group bg-slate-800/40 rounded-xl p-5 border border-slate-700/50 hover:border-amber-500/30 hover:bg-slate-800/60 cursor-pointer transition-all"
                    onClick={() => navigate(`/code/${nb.id}`)}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex items-start gap-4">
                        <div className="w-10 h-10 rounded-xl bg-amber-500/20 flex items-center justify-center">
                          <ExperimentOutlined className="text-amber-400" />
                        </div>
                        <div>
                          <h3 className="text-white font-semibold group-hover:text-amber-400 transition-colors">
                            {nb.title}
                          </h3>
                          <p className="text-slate-500 text-sm mt-1">
                            {nb.cells.length} 个单元格 · 执行 {nb.execution_count} 次
                          </p>
                          <p className="text-slate-600 text-xs mt-2 flex items-center gap-1">
                            <ClockCircleOutlined />
                            更新于 {dayjs(nb.updated_at).format('YYYY-MM-DD HH:mm')}
                          </p>
                        </div>
                      </div>
                      <Dropdown
                        menu={{
                          items: [
                            { key: 'open', icon: <CodeOutlined />, label: '打开' },
                            { type: 'divider' },
                            { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
                          ],
                          onClick: ({ key, domEvent }) => {
                            domEvent.stopPropagation()
                            if (key === 'delete') deleteNotebook(nb.id)
                            if (key === 'open') navigate(`/code/${nb.id}`)
                          },
                        }}
                        trigger={['click']}
                      >
                        <Button
                          type="text"
                          icon={<MoreOutlined />}
                          onClick={(e) => e.stopPropagation()}
                          className="text-slate-500 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity"
                        />
                      </Dropdown>
                    </div>
                  </motion.div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  // Notebook 编辑视图
  return (
    <div className="h-full flex flex-col bg-slate-950">
      {/* 工具栏 */}
      <div className="flex-shrink-0 h-14 px-4 flex items-center justify-between bg-slate-900/80 border-b border-slate-800 backdrop-blur-xl">
        <div className="flex items-center gap-4">
          <Button
            type="text"
            icon={<FolderOutlined />}
            onClick={() => navigate('/code')}
            className="text-slate-400 hover:text-white"
          >
            返回列表
          </Button>
          
          <div className="h-6 w-px bg-slate-700" />
          
          <Input
            value={currentNotebook.title}
            onChange={(e) => setCurrentNotebook(prev => prev ? { ...prev, title: e.target.value } : prev)}
            variant="borderless"
            className="text-white font-semibold text-lg w-64 hover:bg-slate-800/50 rounded-lg px-2"
            placeholder="Notebook 标题"
          />
        </div>

        <div className="flex items-center gap-2">
          <Tooltip title="全部运行">
            <Button
              type="text"
              icon={<PlayCircleOutlined />}
              onClick={runAllCells}
              className="text-slate-400 hover:text-emerald-400"
            >
              全部运行
            </Button>
          </Tooltip>

          <Tooltip title="重启内核 (清除所有变量)">
            <Button
              type="text"
              icon={<ReloadOutlined />}
              onClick={restartKernel}
              className="text-slate-400 hover:text-amber-400"
            >
              重启内核
            </Button>
          </Tooltip>
          
          <Tooltip title="添加代码单元格">
            <Button
              type="text"
              icon={<PlusOutlined />}
              onClick={() => addCell(currentNotebook.cells.length - 1, 'code')}
              className="text-slate-400 hover:text-white"
            />
          </Tooltip>

          <div className="h-6 w-px bg-slate-700 mx-2" />

          <Button
            type="primary"
            icon={isSaving ? <LoadingOutlined spin /> : <SaveOutlined />}
            onClick={saveNotebook}
            disabled={isSaving}
            className="rounded-lg"
          >
            保存
          </Button>
        </div>
      </div>

      {/* Notebook 内容 */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto py-6 px-4 pl-16">
          {isLoading ? (
            <div className="flex items-center justify-center py-20">
              <Spin size="large" />
            </div>
          ) : (
            <AnimatePresence mode="popLayout">
              {currentNotebook.cells.map((cell, index) => (
                <div key={cell.id} className="mb-4">
                  <NotebookCell
                    cell={cell}
                    index={index}
                    isSelected={selectedCellIndex === index}
                    isRunning={runningCells.has(cell.id)}
                    onSelect={() => setSelectedCellIndex(index)}
                    onRun={() => runCell(cell.id, cell.source)}
                    onDelete={() => deleteCell(cell.id)}
                    onUpdate={(source) => updateCell(cell.id, source)}
                    onToggleType={() => toggleCellType(cell.id)}
                    onMoveUp={() => moveCell(cell.id, 'up')}
                    onMoveDown={() => moveCell(cell.id, 'down')}
                    onAddCellBelow={() => addCell(index, 'code')}
                    isFirst={index === 0}
                    isLast={index === currentNotebook.cells.length - 1}
                  />
                </div>
              ))}
            </AnimatePresence>
          )}

          {/* 添加单元格按钮 */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="flex items-center justify-center gap-4 py-8 border-2 border-dashed border-slate-800 rounded-xl hover:border-slate-700 transition-colors"
          >
            <Button
              type="text"
              icon={<CodeOutlined />}
              onClick={() => addCell(currentNotebook.cells.length - 1, 'code')}
              className="text-slate-500 hover:text-emerald-400"
            >
              + 代码
            </Button>
            <Button
              type="text"
              icon={<FileMarkdownOutlined />}
              onClick={() => addCell(currentNotebook.cells.length - 1, 'markdown')}
              className="text-slate-500 hover:text-blue-400"
            >
              + Markdown
            </Button>
          </motion.div>
        </div>
      </div>
    </div>
  )
}

export default CodeLabPage
