/**
 * CodeLabPage - 代码实验室主页面（重构版）
 *
 * 拆分策略:
 * - useNotebook hook: 全部 Notebook/Cell CRUD 操作 + Agent 回调
 * - NotebookListView: 无 Notebook 打开时的列表展示
 * - NotebookEditorView: 编辑器视图（工具栏 + Cell 列表 + Agent 面板）
 * - NotebookCell: 单个单元格（Monaco Editor + Markdown 渲染 + 输出）
 * - CellOutputRenderer: 渲染 Cell 输出（stream/result/image/error）
 * - FeatureCard / StatCard: 展示性卡片
 *
 * 本文件仅负责: 路由参数 → 视图切换 → 键盘快捷键 → 副作用编排
 */

import { useState, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { useNotebook } from './hooks/useNotebook'
import { NotebookListView, NotebookEditorView } from './components'

const CodeLabPage = () => {
  const { notebookId } = useParams<{ notebookId: string }>()

  const {
    // 列表
    deferredNotebooks, isListLoading, loadError,
    loadNotebooks, createNotebook, deleteNotebook,
    // 当前 Notebook
    currentNotebook, setCurrentNotebook,
    selectedCellIndex, setSelectedCellIndex,
    runningCells, isLoading, isSaving,
    loadNotebook, refreshNotebook, saveNotebook, setTitle,
    // Cell 操作
    runCell, runAllCells, restartKernel, interruptRunningExecution, cancelBackgroundExecution,
    updateCell, addCell, deleteCell, toggleCellType, moveCell,
    // Agent 回调
    handleAgentInsertCode, handleAgentRunCode, handleAgentFocusCell,
    handleAgentClearOutputs, handleAgentAddCell, handleAgentUpdateCell,
  } = useNotebook()

  // Agent 面板 UI 状态（仅与视图相关，保留在页面组件中）
  const [showAgentPanel, setShowAgentPanel] = useState(false)
  const [isAgentExpanded, setIsAgentExpanded] = useState(false)

  // ---- 副作用 ----

  // 初始化加载列表
  useEffect(() => { loadNotebooks() }, [loadNotebooks])

  // 路由变化 → 加载/卸载 Notebook
  useEffect(() => {
    if (notebookId) {
      loadNotebook(notebookId)
    } else {
      setCurrentNotebook(null)
      loadNotebooks() // 返回列表时刷新
    }
  }, [notebookId, loadNotebook, loadNotebooks, setCurrentNotebook])

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

  // ---- 渲染 ----

  if (!currentNotebook) {
    return (
      <NotebookListView
        notebooks={deferredNotebooks}
        isLoading={isListLoading}
        loadError={loadError}
        onCreateNotebook={createNotebook}
        onDeleteNotebook={deleteNotebook}
        onRefresh={loadNotebooks}
      />
    )
  }

  return (
    <NotebookEditorView
      notebook={currentNotebook}
      selectedCellIndex={selectedCellIndex}
      runningCells={runningCells}
      isLoading={isLoading}
      isSaving={isSaving}
      showAgentPanel={showAgentPanel}
      isAgentExpanded={isAgentExpanded}
      onSetTitle={setTitle}
      onSelectCell={setSelectedCellIndex}
      onRunCell={runCell}
      onRunAllCells={runAllCells}
      onInterruptRunningExecution={interruptRunningExecution}
      onCancelBackgroundExecution={cancelBackgroundExecution}
      onRestartKernel={restartKernel}
      onSave={saveNotebook}
      onDeleteCell={deleteCell}
      onUpdateCell={updateCell}
      onToggleCellType={toggleCellType}
      onMoveCell={moveCell}
      onAddCell={addCell}
      onToggleAgentPanel={() => setShowAgentPanel(!showAgentPanel)}
      onToggleAgentExpand={() => setIsAgentExpanded(!isAgentExpanded)}
      onAgentInsertCode={handleAgentInsertCode}
      onAgentRunCode={handleAgentRunCode}
      onAgentFocusCell={handleAgentFocusCell}
      onAgentClearOutputs={handleAgentClearOutputs}
      onAgentAddCell={handleAgentAddCell}
      onAgentUpdateCell={handleAgentUpdateCell}
      onRefreshNotebook={() => refreshNotebook(currentNotebook.id)}
    />
  )
}

export default CodeLabPage
