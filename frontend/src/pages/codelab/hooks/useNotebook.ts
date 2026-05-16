import { useState, useCallback, useTransition, useDeferredValue, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { message, Modal } from 'antd'
import { codelabApi, Notebook, Cell } from '@/services/api'
import { handleApiError } from '@/utils/apiErrorHandler'

const CELL_EXECUTION_TIMEOUT_SECONDS = 0
const BACKGROUND_RUNNING_STATUSES = new Set(['pending', 'running'])
const BACKGROUND_TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/**
 * useNotebook - 封装 Notebook 的全部 CRUD 操作和 Cell 管理
 *
 * 职责:
 * - Notebook 列表加载、创建、删除
 * - 单个 Notebook 加载、保存、标题更新
 * - Cell 增删改查、运行、移动、类型切换
 * - 内核重启
 * - Agent 操作回调 (insertCode, runCode, focusCell, clearOutputs, addCell, updateCell)
 *
 * 使用 React 18 useTransition 优化大量 Cell 更新的渲染性能
 */
export function useNotebook() {
  const navigate = useNavigate()

  // ---- 列表状态 ----
  const [notebooks, setNotebooks] = useState<Notebook[]>([])
  const [isListLoading, setIsListLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // ---- 当前 Notebook 状态 ----
  const [currentNotebook, setCurrentNotebook] = useState<Notebook | null>(null)
  const [selectedCellIndex, setSelectedCellIndex] = useState<number>(0)
  const [runningCells, setRunningCells] = useState<Set<string>>(new Set())
  const [isLoading, setIsLoading] = useState(false)
  const [isSaving, setIsSaving] = useState(false)

  // React 18 并发优化
  const [, startTransition] = useTransition()
  const deferredNotebooks = useDeferredValue(notebooks)
  const terminalBackgroundRefreshes = useRef<Set<string>>(new Set())
  const backgroundExecutionPollKey = useMemo(() => {
    if (!currentNotebook) return ''
    return currentNotebook.cells
      .map((cell) => {
        const execution = cell.metadata?.background_execution
        const executionId = String(execution?.execution_id || '').trim()
        const status = String(execution?.status || '').trim().toLowerCase()
        if (!executionId || !BACKGROUND_RUNNING_STATUSES.has(status)) return ''
        return `${executionId}:${status}`
      })
      .filter(Boolean)
      .join('|')
  }, [currentNotebook])

  // ========== Notebook 列表操作 ==========

  const loadNotebooks = useCallback(async () => {
    setIsListLoading(true)
    setLoadError(null)
    try {
      const data = await codelabApi.listNotebooks()
      startTransition(() => { setNotebooks(data) })
    } catch (error: any) {
      handleApiError(error, '加载列表失败')
      setLoadError(error.message || '加载列表失败')
    } finally {
      setIsListLoading(false)
    }
  }, [])

  const loadNotebook = useCallback(async (id: string, options?: { preserveSelection?: boolean }) => {
    setIsLoading(true)
    try {
      const data = await codelabApi.getNotebook(id)
      setCurrentNotebook(data)
      if (!options?.preserveSelection) {
        setSelectedCellIndex(0)
      } else {
        setSelectedCellIndex(prev => Math.max(0, Math.min(prev, Math.max(0, data.cells.length - 1))))
      }
    } catch (error) {
      handleApiError(error, '加载 Notebook 失败')
      navigate('/code')
    } finally {
      setIsLoading(false)
    }
  }, [navigate])

  const refreshNotebook = useCallback(async (id: string) => {
    await loadNotebook(id, { preserveSelection: true })
  }, [loadNotebook])

  const createNotebook = useCallback(async () => {
    try {
      const data = await codelabApi.createNotebook({ title: '未命名 Notebook' })
      setNotebooks(prev => [data, ...prev])
      navigate(`/code/${data.id}`)
    } catch (error) {
      handleApiError(error, '创建 Notebook 失败')
    }
  }, [navigate])

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
          handleApiError(error, '删除失败')
        }
      },
    })
  }, [currentNotebook, navigate])

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

  const setTitle = useCallback((title: string) => {
    setCurrentNotebook(prev => prev ? { ...prev, title } : prev)
  }, [])

  // ========== Cell 操作 ==========

  const runCell = useCallback(async (cellId: string, code: string) => {
    if (!currentNotebook) return

    setRunningCells(prev => new Set(prev).add(cellId))
    try {
      const result = await codelabApi.executeCell(currentNotebook.id, {
        code,
        cell_id: cellId,
        timeout: CELL_EXECUTION_TIMEOUT_SECONDS,
      })

      startTransition(() => {
        setCurrentNotebook(prev => {
          if (!prev) return prev
          return {
            ...prev,
            cells: prev.cells.map(cell => {
              if (cell.id !== cellId) return cell
              const metadata = { ...(cell.metadata || {}) }
              delete metadata.background_execution
              return {
                ...cell,
                outputs: result.outputs,
                execution_count: result.execution_count,
                metadata,
              }
            }),
          }
        })
      })

      if (!result.success) {
        const errorOutput = result.outputs.find(output => output.output_type === 'error')
        const errorDetail = typeof errorOutput?.content === 'object' && errorOutput?.content && 'evalue' in errorOutput.content
          ? String(errorOutput.content.evalue ?? '')
          : ''

        if (result.terminated_reason === 'timeout') {
          message.warning(errorDetail || '代码执行超时')
        } else if (result.terminated_reason === 'policy_violation') {
          message.warning(errorDetail || '代码触发沙箱限制')
        } else if (result.terminated_reason === 'resource_limit') {
          message.warning(errorDetail || '当前执行任务过多，请稍后重试')
        } else {
          message.warning(errorDetail || '代码执行出错')
        }
      }
    } catch (error) {
      handleApiError(error, '执行失败')
    } finally {
      setRunningCells(prev => {
        const next = new Set(prev)
        next.delete(cellId)
        return next
      })
    }
  }, [currentNotebook])

  const runAllCells = useCallback(async () => {
    if (!currentNotebook) return
    for (const cell of currentNotebook.cells) {
      if (cell.cell_type === 'code' && cell.source.trim()) {
        await runCell(cell.id, cell.source)
      }
    }
  }, [currentNotebook, runCell])

  const interruptRunningExecution = useCallback(async () => {
    if (!currentNotebook) return
    try {
      await codelabApi.interruptKernel(currentNotebook.id)
      setRunningCells(new Set())
      await refreshNotebook(currentNotebook.id)
      message.success('已请求中断当前执行')
    } catch (error) {
      handleApiError(error, '中断执行失败')
    }
  }, [currentNotebook, refreshNotebook])

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
          handleApiError(error, '重启内核失败')
        }
      },
    })
  }, [currentNotebook])

  const cancelBackgroundExecution = useCallback(async (executionId: string) => {
    if (!currentNotebook || !executionId) return
    try {
      await codelabApi.cancelBackgroundExecution(currentNotebook.id, executionId)
      await refreshNotebook(currentNotebook.id)
      message.success('已请求停止后台任务')
    } catch (error) {
      handleApiError(error, '停止后台任务失败')
    }
  }, [currentNotebook, refreshNotebook])

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

  const addCell = useCallback(async (index: number, cellType: 'code' | 'markdown' = 'code') => {
    if (!currentNotebook) return
    try {
      const newCell = await codelabApi.addCell(currentNotebook.id, cellType, index + 1)
      setCurrentNotebook(prev => {
        if (!prev) return prev
        const cells = [...prev.cells]
        cells.splice(index + 1, 0, newCell)
        return { ...prev, cells }
      })
      setSelectedCellIndex(index + 1)
    } catch (error) {
      handleApiError(error, '添加单元格失败')
    }
  }, [currentNotebook])

  const deleteCell = useCallback(async (cellId: string) => {
    if (!currentNotebook || currentNotebook.cells.length <= 1) {
      message.warning('至少保留一个单元格')
      return
    }
    try {
      await codelabApi.deleteCell(currentNotebook.id, cellId)
      setCurrentNotebook(prev => {
        if (!prev) return prev
        return { ...prev, cells: prev.cells.filter(c => c.id !== cellId) }
      })
      setSelectedCellIndex(prev => Math.max(0, prev - 1))
    } catch (error) {
      handleApiError(error, '删除单元格失败')
    }
  }, [currentNotebook])

  const toggleCellType = useCallback(async (cellId: string) => {
    if (!currentNotebook) return
    const newCells = currentNotebook.cells.map(cell =>
      cell.id === cellId
        ? { ...cell, cell_type: cell.cell_type === 'code' ? 'markdown' : 'code', outputs: [] } as Cell
        : cell
    )
    setCurrentNotebook(prev => {
      if (!prev) return prev
      return { ...prev, cells: newCells }
    })
    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells: newCells,
      })
    } catch (error) {
      handleApiError(error, '保存失败')
    }
  }, [currentNotebook])

  const moveCell = useCallback(async (cellId: string, direction: 'up' | 'down') => {
    if (!currentNotebook) return
    const cells = [...currentNotebook.cells]
    const index = cells.findIndex(c => c.id === cellId)
    if (index === -1) return

    const newIndex = direction === 'up' ? index - 1 : index + 1
    if (newIndex < 0 || newIndex >= cells.length) return

      ;[cells[index], cells[newIndex]] = [cells[newIndex], cells[index]]

    setCurrentNotebook(prev => {
      if (!prev) return prev
      return { ...prev, cells }
    })
    setSelectedCellIndex(prev => {
      const delta = direction === 'up' ? -1 : 1
      return Math.max(0, Math.min(cells.length - 1, prev + delta))
    })

    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells,
      })
    } catch (error) {
      handleApiError(error, '保存失败')
    }
  }, [currentNotebook])

  // ========== Agent 回调 ==========

  const handleAgentInsertCode = useCallback(async (code: string) => {
    if (!currentNotebook) return

    const newCell: Cell = {
      id: crypto.randomUUID(),
      cell_type: 'code',
      source: code,
      outputs: [],
      execution_count: null,
      metadata: { from_agent: true },
    }

    const newCells = [...currentNotebook.cells]
    newCells.splice(selectedCellIndex + 1, 0, newCell)

    setCurrentNotebook(prev => {
      if (!prev) return prev
      return { ...prev, cells: newCells }
    })
    setSelectedCellIndex(selectedCellIndex + 1)
    message.success('代码已插入')

    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells: newCells,
      })
    } catch (error) {
      handleApiError(error, '保存失败')
    }
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

    const newCells = [...currentNotebook.cells]
    newCells.splice(selectedCellIndex + 1, 0, newCell)

    setCurrentNotebook(prev => {
      if (!prev) return prev
      return { ...prev, cells: newCells }
    })
    setSelectedCellIndex(selectedCellIndex + 1)

    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells: newCells,
      })
    } catch (error) {
      handleApiError(error, '保存失败')
    }

    setTimeout(() => { runCell(newCellId, code) }, 100)
  }, [currentNotebook, selectedCellIndex, runCell])

  const handleAgentFocusCell = useCallback((cellIndex: number) => {
    if (!currentNotebook) return
    setSelectedCellIndex(Math.max(0, Math.min(currentNotebook.cells.length - 1, cellIndex)))
  }, [currentNotebook])

  const handleAgentClearOutputs = useCallback(async () => {
    if (!currentNotebook) return

    const newCells = currentNotebook.cells.map(cell => ({ ...cell, outputs: [], execution_count: null }))

    startTransition(() => {
      setCurrentNotebook(prev => {
        if (!prev) return prev
        return { ...prev, cells: newCells }
      })
    })
    message.success('所有输出已清除')

    try {
      await codelabApi.updateNotebook(currentNotebook.id, {
        title: currentNotebook.title,
        cells: newCells,
      })
    } catch (error) {
      handleApiError(error, '保存失败')
    }
  }, [currentNotebook])

  const handleAgentAddCell = useCallback((newCell: Cell) => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const exists = prev.cells.some(c => c.id === newCell.id)
      if (exists) return prev
      return { ...prev, cells: [...prev.cells, newCell] }
    })
    setTimeout(() => {
      setSelectedCellIndex(prev => prev + 1)
    }, 100)
  }, [])

  const handleAgentUpdateCell = useCallback((updatedCell: Cell) => {
    setCurrentNotebook(prev => {
      if (!prev) return prev
      const cellExists = prev.cells.some(c => c.id === updatedCell.id)
      if (cellExists) {
        return {
          ...prev,
          cells: prev.cells.map(c =>
            c.id === updatedCell.id ? { ...c, ...updatedCell } : c
          ),
        }
      } else {
        return {
          ...prev,
          cells: [...prev.cells, updatedCell],
        }
      }
    })
  }, [])

  useEffect(() => {
    const notebookId = currentNotebook?.id
    if (!notebookId || !backgroundExecutionPollKey) return

    const runningExecutionIds = new Set(
      backgroundExecutionPollKey
        .split('|')
        .map((item) => item.split(':')[0])
        .filter(Boolean)
    )
    let disposed = false

    const pollBackgroundExecutions = async () => {
      try {
        const executions = await codelabApi.listBackgroundExecutions(notebookId)
        if (disposed) return

        const executionsById = new Map(
          executions
            .filter((execution) => execution.execution_id)
            .map((execution) => [execution.execution_id, execution])
        )
        const terminalExecutionIds = executions
          .filter((execution) => {
            const executionId = String(execution.execution_id || '').trim()
            const status = String(execution.status || '').trim().toLowerCase()
            return (
              executionId &&
              runningExecutionIds.has(executionId) &&
              BACKGROUND_TERMINAL_STATUSES.has(status) &&
              !terminalBackgroundRefreshes.current.has(executionId)
            )
          })
          .map((execution) => String(execution.execution_id))

        terminalExecutionIds.forEach((executionId) => {
          terminalBackgroundRefreshes.current.add(executionId)
        })

        setCurrentNotebook(prev => {
          if (!prev || prev.id !== notebookId) return prev
          let changed = false
          const cells = prev.cells.map((cell) => {
            const currentBackground = cell.metadata?.background_execution
            const executionId = String(currentBackground?.execution_id || '').trim()
            if (!executionId) return cell

            const execution = executionsById.get(executionId)
            if (!execution) return cell

            const nextBackground = {
              ...(currentBackground || {}),
              execution_id: execution.execution_id,
              status: execution.status,
              description: execution.description || currentBackground?.description,
              created_at: execution.created_at || currentBackground?.created_at,
              started_at: execution.started_at ?? currentBackground?.started_at,
              completed_at: execution.completed_at ?? currentBackground?.completed_at,
              cancel_requested: Boolean(execution.cancel_requested),
              success: execution.success ?? currentBackground?.success,
              terminated_reason: execution.terminated_reason ?? currentBackground?.terminated_reason,
              policy_violation_code: execution.policy_violation_code ?? currentBackground?.policy_violation_code,
              execution_count: execution.execution_count ?? currentBackground?.execution_count,
              error: execution.error ?? currentBackground?.error,
            }
            const nextStatus = String(nextBackground.status || '').trim().toLowerCase()
            const currentStatus = String(currentBackground?.status || '').trim().toLowerCase()
            const nextExecutionCount = execution.execution_count ?? cell.execution_count
            if (
              nextStatus === currentStatus &&
              nextBackground.completed_at === currentBackground?.completed_at &&
              nextBackground.cancel_requested === Boolean(currentBackground?.cancel_requested) &&
              nextExecutionCount === cell.execution_count
            ) {
              return cell
            }
            changed = true
            return {
              ...cell,
              execution_count: nextExecutionCount,
              metadata: {
                ...(cell.metadata || {}),
                background_execution: nextBackground,
              },
            }
          })
          return changed ? { ...prev, cells } : prev
        })

        if (terminalExecutionIds.length > 0) {
          await refreshNotebook(notebookId)
        }
      } catch {
        // Keep polling quietly; the visible notebook state remains the last confirmed snapshot.
      }
    }

    void pollBackgroundExecutions()
    const timer = window.setInterval(() => {
      void pollBackgroundExecutions()
    }, 3000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [backgroundExecutionPollKey, currentNotebook?.id, refreshNotebook])

  return {
    // 列表
    notebooks, deferredNotebooks, isListLoading, loadError,
    loadNotebooks, createNotebook, deleteNotebook,
    // 当前 Notebook
    currentNotebook, setCurrentNotebook,
    selectedCellIndex, setSelectedCellIndex,
    runningCells, isLoading, isSaving,
    loadNotebook, saveNotebook, setTitle,
    refreshNotebook,
    // Cell 操作
    runCell, runAllCells, restartKernel, interruptRunningExecution,
    cancelBackgroundExecution,
    updateCell, addCell, deleteCell, toggleCellType, moveCell,
    // Agent 回调
    handleAgentInsertCode, handleAgentRunCode, handleAgentFocusCell,
    handleAgentClearOutputs, handleAgentAddCell, handleAgentUpdateCell,
  }
}
