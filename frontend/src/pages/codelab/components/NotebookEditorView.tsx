import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Input, Tooltip, Badge, Spin,
} from 'antd'
import {
  PlayCircleOutlined, PlusOutlined, CodeOutlined,
  FileMarkdownOutlined, SaveOutlined, ReloadOutlined,
  FolderOutlined, LoadingOutlined, RobotOutlined, FileTextOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import { Notebook, Cell } from '@/services/api'
import NotebookAgentPanel from '@/components/NotebookAgentPanel'
import NotebookCell from './NotebookCell'
import NotebookFilesDrawer from './NotebookFilesDrawer'

interface NotebookEditorViewProps {
  notebook: Notebook
  selectedCellIndex: number
  runningCells: Set<string>
  isLoading: boolean
  isSaving: boolean
  showAgentPanel: boolean
  isAgentExpanded: boolean
  onSetTitle: (title: string) => void
  onSelectCell: (index: number) => void
  onRunCell: (cellId: string, code: string) => void
  onRunAllCells: () => void
  onInterruptRunningExecution: () => void
  onCancelBackgroundExecution: (executionId: string) => void
  onRestartKernel: () => void
  onSave: () => void
  onDeleteCell: (cellId: string) => void
  onUpdateCell: (cellId: string, source: string) => void
  onToggleCellType: (cellId: string) => void
  onMoveCell: (cellId: string, direction: 'up' | 'down') => void
  onAddCell: (index: number, type?: 'code' | 'markdown') => void
  onToggleAgentPanel: () => void
  onToggleAgentExpand: () => void
  // Agent 回调
  onAgentInsertCode: (code: string) => void
  onAgentRunCode: (code: string) => void
  onAgentFocusCell: (index: number) => void
  onAgentClearOutputs: () => void
  onAgentAddCell: (cell: Cell) => void
  onAgentUpdateCell: (cell: Cell) => void
  onRefreshNotebook: () => void
}

/** Notebook 编辑视图 - 工具栏 + Cell 列表 + Agent 面板 */
const NotebookEditorView = ({
  notebook,
  selectedCellIndex,
  runningCells,
  isLoading,
  isSaving,
  showAgentPanel,
  isAgentExpanded,
  onSetTitle,
  onSelectCell,
  onRunCell,
  onRunAllCells,
  onInterruptRunningExecution,
  onCancelBackgroundExecution,
  onRestartKernel,
  onSave,
  onDeleteCell,
  onUpdateCell,
  onToggleCellType,
  onMoveCell,
  onAddCell,
  onToggleAgentPanel,
  onToggleAgentExpand,
  onAgentInsertCode,
  onAgentRunCode,
  onAgentFocusCell,
  onAgentClearOutputs,
  onAgentAddCell,
  onAgentUpdateCell,
  onRefreshNotebook,
}: NotebookEditorViewProps) => {
  const navigate = useNavigate()
  const [isFilesDrawerOpen, setIsFilesDrawerOpen] = useState(false)

  return (
    <div className="h-full flex bg-slate-950">
      {/* 主内容区 */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* 工具栏 */}
        <div className="flex-shrink-0 h-14 px-4 flex items-center justify-between bg-slate-900/80 border-b border-slate-800 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <Button type="text" icon={<FolderOutlined />} onClick={() => navigate('/code')} className="text-slate-400 hover:text-white">
              返回列表
            </Button>
            <div className="h-6 w-px bg-slate-700" />
            <Input
              value={notebook.title}
              onChange={(e) => onSetTitle(e.target.value)}
              variant="borderless"
              className="text-white font-semibold text-lg w-64 hover:bg-slate-800/50 rounded-lg px-2"
              placeholder="Notebook 标题"
            />
          </div>

          <div className="flex items-center gap-2">
            <Tooltip title="全部运行">
              <Button type="text" icon={<PlayCircleOutlined />} onClick={onRunAllCells} className="text-slate-400 hover:text-emerald-400">
                全部运行
              </Button>
            </Tooltip>
            <Tooltip title="重启内核">
              <Button type="text" icon={<ReloadOutlined />} onClick={onRestartKernel} className="text-slate-400 hover:text-amber-400">
                重启内核
              </Button>
            </Tooltip>
            <Tooltip title="Notebook 文件">
              <Button type="text" icon={<FileTextOutlined />} onClick={() => setIsFilesDrawerOpen(true)} className="text-slate-400 hover:text-sky-400">
                文件
              </Button>
            </Tooltip>
            <Tooltip title="添加代码单元格">
              <Button type="text" icon={<PlusOutlined />} onClick={() => onAddCell(notebook.cells.length - 1, 'code')} className="text-slate-400 hover:text-white" />
            </Tooltip>
            <div className="h-6 w-px bg-slate-700 mx-2" />
            <Tooltip title="AI 助手">
              <Badge dot={showAgentPanel} offset={[-5, 5]}>
                <Button
                  type={showAgentPanel ? 'primary' : 'text'}
                  icon={<RobotOutlined />}
                  onClick={onToggleAgentPanel}
                  className={showAgentPanel ? 'rounded-lg' : 'text-slate-400 hover:text-emerald-400'}
                >
                  AI 助手
                </Button>
              </Badge>
            </Tooltip>
            <div className="h-6 w-px bg-slate-700 mx-2" />
            <Button
              type="primary"
              icon={isSaving ? <LoadingOutlined spin /> : <SaveOutlined />}
              onClick={onSave}
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
                {notebook.cells.map((cell, index) => (
                  <div key={cell.id} className="mb-4">
                    <NotebookCell
                      cell={cell}
                      index={index}
                      isSelected={selectedCellIndex === index}
                      isRunning={runningCells.has(cell.id)}
                      onInterruptRunningExecution={onInterruptRunningExecution}
                      onCancelBackgroundExecution={onCancelBackgroundExecution}
                      onSelect={() => onSelectCell(index)}
                      onRun={() => onRunCell(cell.id, cell.source)}
                      onDelete={() => onDeleteCell(cell.id)}
                      onUpdate={(source) => onUpdateCell(cell.id, source)}
                      onToggleType={() => onToggleCellType(cell.id)}
                      onMoveUp={() => onMoveCell(cell.id, 'up')}
                      onMoveDown={() => onMoveCell(cell.id, 'down')}
                      onAddCellBelow={() => onAddCell(index, 'code')}
                      isFirst={index === 0}
                      isLast={index === notebook.cells.length - 1}
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
              <Button type="text" icon={<CodeOutlined />} onClick={() => onAddCell(notebook.cells.length - 1, 'code')} className="text-slate-500 hover:text-emerald-400">
                + 代码
              </Button>
              <Button type="text" icon={<FileMarkdownOutlined />} onClick={() => onAddCell(notebook.cells.length - 1, 'markdown')} className="text-slate-500 hover:text-blue-400">
                + Markdown
              </Button>
            </motion.div>
          </div>
        </div>
      </div>

      {/* Agent 面板 */}
      <NotebookAgentPanel
        notebookId={notebook.id}
        isVisible={showAgentPanel}
        onClose={() => onToggleAgentPanel()}
        onToggleExpand={onToggleAgentExpand}
        isExpanded={isAgentExpanded}
        onInsertCode={onAgentInsertCode}
        onRunCode={onAgentRunCode}
        onFocusCell={onAgentFocusCell}
        onClearOutputs={onAgentClearOutputs}
        onAddCell={onAgentAddCell}
        onUpdateCell={onAgentUpdateCell}
        onRefreshNotebook={onRefreshNotebook}
        currentCellIndex={selectedCellIndex}
        cells={notebook.cells}
      />

      <NotebookFilesDrawer
        notebookId={notebook.id}
        open={isFilesDrawerOpen}
        onClose={() => setIsFilesDrawerOpen(false)}
      />
    </div>
  )
}

export default NotebookEditorView
