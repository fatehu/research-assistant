import { useState, useEffect, useRef, useCallback, useMemo, useTransition } from 'react'
import { Button, Tooltip, Dropdown } from 'antd'
import {
  CaretRightOutlined, DeleteOutlined, CodeOutlined,
  FileMarkdownOutlined, PlusOutlined, MoreOutlined,
  UpOutlined, DownOutlined, CopyOutlined, ClearOutlined,
  LoadingOutlined, StopOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'
import Editor from '@monaco-editor/react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { Cell } from '@/services/api'
import CellOutputRenderer from './CellOutputRenderer'

export interface NotebookCellProps {
  cell: Cell
  index: number
  isSelected: boolean
  isRunning: boolean
  onInterruptRunningExecution: () => void
  onCancelBackgroundExecution: (executionId: string) => void
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

/** Notebook 单元格 - 支持 Code / Markdown 编辑、运行、移动、删除 */
const NotebookCell = ({
  cell,
  index,
  isSelected,
  isRunning,
  onInterruptRunningExecution,
  onCancelBackgroundExecution,
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
  const backgroundExecution = cell.metadata?.background_execution as Record<string, any> | undefined
  const backgroundStatus = String(backgroundExecution?.status || '').trim().toLowerCase()
  const backgroundExecutionId = String(backgroundExecution?.execution_id || '').trim()
  const isBackgroundRunning = backgroundStatus === 'pending' || backgroundStatus === 'running'

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
            disabled={cell.cell_type !== 'code' || isRunning || isBackgroundRunning}
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
            {(isRunning || isBackgroundRunning) && (
              <span className="flex items-center gap-1 text-amber-400 text-xs">
                <LoadingOutlined spin />
                {isBackgroundRunning ? '后台运行中…' : '运行中...'}
              </span>
            )}
            {backgroundStatus === 'failed' && (
              <span className="text-rose-400 text-xs">后台任务失败</span>
            )}
            {backgroundStatus === 'cancelled' && (
              <span className="text-slate-400 text-xs">后台任务已停止</span>
            )}
          </div>

          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {isRunning && !isBackgroundRunning && (
              <Tooltip title="中断当前执行">
                <Button
                  type="text"
                  size="small"
                  icon={<StopOutlined />}
                  onClick={(e) => {
                    e.stopPropagation()
                    onInterruptRunningExecution()
                  }}
                  className="text-amber-400 hover:text-amber-300"
                />
              </Tooltip>
            )}
            {isBackgroundRunning && backgroundExecutionId && (
              <Tooltip title="停止后台任务">
                <Button
                  type="text"
                  size="small"
                  icon={<StopOutlined />}
                  onClick={(e) => {
                    e.stopPropagation()
                    onCancelBackgroundExecution(backgroundExecutionId)
                  }}
                  className="text-rose-400 hover:text-rose-300"
                />
              </Tooltip>
            )}
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

export default NotebookCell
