import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  BookOutlined,
  CodeOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  EyeOutlined,
  LinkOutlined,
  PlusOutlined,
  ProjectOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  chatApi,
  projectApi,
  type ResearchProject,
  type ResearchProjectExecutionSummary,
  type ResearchProjectResultSummary,
  type ResearchProjectWorkspaceOutputSummary,
  type ResearchProjectRuntimeCandidateSummary,
  type ResearchProjectRuntimeContextSummary,
  type ResearchProjectRuntimeOverview,
  type ResearchProjectStageSummary,
  type ResearchProjectWorkspaceRuntimeOverview,
} from '@/services/api'
import {
  buildPaperExecutionLaunch,
  buildPaperImplementationPrepLaunch,
  buildPaperPlanningLaunch,
  buildPaperRunDraftsLaunch,
  buildPaperTuningLaunch,
  type PaperWorkflowStage,
} from '@/utils/paperWorkflowPrompts'

const { Paragraph, Text, Title } = Typography
const { TextArea } = Input

type ProjectFormValues = {
  title?: string
  goal?: string
}

const statusColorMap: Record<string, string> = {
  draft: 'default',
  active: 'blue',
  archived: 'gold',
  running: 'processing',
  blocked: 'warning',
  completed: 'success',
}

const executionStatusColorMap: Record<string, string> = {
  draft: 'default',
  pending: 'gold',
  running: 'processing',
  completed: 'success',
  failed: 'error',
  blocked: 'warning',
  cancelled: 'default',
  completed_or_unknown: 'default',
  unknown: 'default',
}

const stageStatusColorMap: Record<string, string> = {
  missing: 'default',
  ready: 'blue',
  running: 'processing',
  blocked: 'warning',
  completed: 'success',
}

const stageActionLabelMap: Record<PaperWorkflowStage, string> = {
  planning: '继续规划',
  implementation_prep: '做实施准备',
  run_drafts: '生成运行草案',
  execution: '继续执行',
  tuning: '调参与对比',
}

const formatDateTime = (value?: string) => {
  if (!value) return 'unknown'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const formatSeconds = (value?: number) => {
  if (!Number.isFinite(value || 0) || !value || value <= 0) return null
  const total = Math.round(value)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

const formatMetricValue = (value: unknown) => {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(4)
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

const artifactKindLabel = (kind: string) => {
  if (kind === 'json') return 'JSON'
  if (kind === 'markdown') return 'MD'
  if (kind === 'log') return 'LOG'
  if (kind === 'db_record') return 'DB'
  return kind.toUpperCase()
}

const outputCategoryLabel = (category: string) => {
  if (category === 'planning') return 'Paper Analysis'
  if (category === 'repo_metadata') return 'Repo Analysis'
  if (category === 'specs') return 'Specs'
  if (category === 'drafts') return 'Run Drafts'
  if (category === 'executions') return 'Execution Outputs'
  if (category === 'results') return 'Reports'
  return 'Workspace Output'
}

const formatBytes = (value?: number) => {
  const size = Number(value || 0)
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

const openExternal = (url?: string) => {
  if (!url) return
  window.open(url, '_blank', 'noopener,noreferrer')
}

const buildStageLaunch = (stage: PaperWorkflowStage, args: { paperId: number; projectId: number; goal?: string | null }) => {
  if (stage === 'planning') return buildPaperPlanningLaunch(args)
  if (stage === 'implementation_prep') return buildPaperImplementationPrepLaunch(args)
  if (stage === 'run_drafts') return buildPaperRunDraftsLaunch(args)
  if (stage === 'tuning') return buildPaperTuningLaunch(args)
  return buildPaperExecutionLaunch(args)
}

function StageLedgerCard({ stages }: { stages: ResearchProjectStageSummary[] }) {
  return (
    <Card className="!border-slate-700/60 !bg-slate-950/30" title={<span className="text-slate-100">Stage Ledger</span>}>
      <div className="space-y-3">
        {stages.map((stage) => (
          <div key={stage.stage} className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-slate-100">{stage.label}</div>
                <div className="mt-1 text-xs text-slate-500">{stage.updated_at ? `Updated ${formatDateTime(stage.updated_at)}` : '尚无时间戳'}</div>
              </div>
              <Tag color={stageStatusColorMap[String(stage.status || 'missing')] || 'default'}>{stage.status}</Tag>
            </div>
            {stage.summary ? (
              <div className="mt-3 text-sm text-slate-300">{stage.summary}</div>
            ) : null}
            {stage.blockers.length > 0 ? (
              <div className="mt-3 rounded-xl border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                <div className="font-medium">Blockers</div>
                <div className="mt-1 space-y-1">
                  {stage.blockers.map((blocker) => (
                    <div key={blocker}>{blocker}</div>
                  ))}
                </div>
              </div>
            ) : null}
            {stage.artifacts.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {stage.artifacts.map((artifact) => (
                  <Tag key={`${stage.stage}:${artifact.relative_path}`} color={artifact.present ? 'cyan' : 'default'}>
                    {artifact.label} · {artifactKindLabel(artifact.kind)}
                  </Tag>
                ))}
              </div>
            ) : null}
            {stage.artifacts.length > 0 ? (
              <div className="mt-2 space-y-1 text-[11px] text-slate-500">
                {stage.artifacts.map((artifact) => (
                  <div key={`${stage.stage}:path:${artifact.relative_path}`} className="truncate">
                    <Text code className="!text-[11px]">{artifact.relative_path}</Text>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </Card>
  )
}

function RuntimeContextCard({
  runtimeContext,
  notebookId,
  onOpenNotebook,
}: {
  runtimeContext: ResearchProjectRuntimeContextSummary
  notebookId?: string
  onOpenNotebook?: () => void
}) {
  return (
    <Card className="!border-slate-700/60 !bg-slate-950/30" title={<span className="text-slate-100">Runtime Context</span>}>
      <div className="space-y-4 text-sm text-slate-300">
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
            <div className="text-xs uppercase tracking-[0.18em] text-slate-500">Mode</div>
            <div className="mt-2 space-y-2">
              <div>execution_mode: <Text code>{runtimeContext.execution_mode || 'unknown'}</Text></div>
              <div>repo_root: <Text code>{runtimeContext.repo_root_relative_path || 'repo/source'}</Text></div>
              <div>repo_files: {runtimeContext.repo_file_count}</div>
              <div>runtime_worker: {runtimeContext.runtime_worker_available ? 'available' : runtimeContext.runtime_worker_enabled ? 'enabled but unavailable' : 'disabled'}</div>
            </div>
          </div>
          <div className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
            <div className="text-xs uppercase tracking-[0.18em] text-slate-500">Workspace</div>
            <div className="mt-2 space-y-2">
              <div>repo: {runtimeContext.repo_available ? 'ready' : 'missing'}</div>
              <div>history candidates: {runtimeContext.repo_history_candidate_count}</div>
              {runtimeContext.repo_reference_url ? (
                <Button size="small" icon={<LinkOutlined />} onClick={() => openExternal(runtimeContext.repo_reference_url)}>
                  打开官方仓库
                </Button>
              ) : null}
              {runtimeContext.notebook_asset_relative_path && notebookId && onOpenNotebook ? (
                <Button size="small" icon={<CodeOutlined />} onClick={onOpenNotebook}>
                  查看 Notebook
                </Button>
              ) : null}
            </div>
          </div>
        </div>

        {runtimeContext.entrypoint_hints.length > 0 ? (
          <div>
            <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Entrypoints</div>
            <div className="flex flex-wrap gap-2">
              {runtimeContext.entrypoint_hints.map((item) => (
                <Tag key={item} color="blue">{item}</Tag>
              ))}
            </div>
          </div>
        ) : null}

        <div>
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Runtime Candidates</div>
          {runtimeContext.runtime_candidates.length > 0 ? (
            <div className="space-y-3">
              {runtimeContext.runtime_candidates.map((candidate: ResearchProjectRuntimeCandidateSummary) => (
                <div key={`${candidate.runtime_type}:${candidate.priority}`} className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium text-slate-100">{candidate.runtime_type}</div>
                      <div className="mt-1 text-xs text-slate-500">{candidate.reason || '无额外说明'}</div>
                    </div>
                    <Space wrap>
                      <Tag color={stageStatusColorMap[candidate.status] || 'default'}>{candidate.status}</Tag>
                      {candidate.requires_explicit_user_confirm ? <Tag color="gold">confirm</Tag> : null}
                      {candidate.requires_runtime_worker ? <Tag color="purple">worker</Tag> : null}
                    </Space>
                  </div>
                  {candidate.entrypoints.length > 0 ? (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {candidate.entrypoints.map((entrypoint) => (
                        <Tag key={entrypoint} color="cyan">{entrypoint}</Tag>
                      ))}
                    </div>
                  ) : null}
                  {candidate.blockers.length > 0 ? (
                    <div className="mt-3 text-xs text-amber-200">
                      {candidate.blockers.join(' · ')}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前 workspace 还没有 runtime candidate" />
          )}
        </div>

        <div>
          <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-500">Tool Availability</div>
          <div className="flex flex-wrap gap-2">
            {runtimeContext.tools.map((tool) => (
              <Tag key={tool.tool_key} color={tool.available ? 'green' : 'default'}>
                {tool.tool_key}: {tool.available ? 'ready' : 'missing'}
              </Tag>
            ))}
          </div>
        </div>
      </div>
    </Card>
  )
}

function ResultsCard({ results }: { results: ResearchProjectResultSummary }) {
  const baselineMetricEntries = Object.entries(results.baseline_metrics || {})
  const tuningMetricEntries = Object.entries(results.tuning_metrics || {})
  return (
    <Card className="!border-slate-700/60 !bg-slate-950/30" title={<span className="text-slate-100">Results</span>}>
      <div className="space-y-4">
        <div className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-sm font-medium text-slate-100">Baseline</div>
              <div className="mt-1 text-xs text-slate-500">
                {results.baseline_execution_id ? `execution=${results.baseline_execution_id}` : '尚无 baseline execution'}
              </div>
            </div>
            <Tag color={stageStatusColorMap[results.baseline_status] || executionStatusColorMap[results.baseline_status] || 'default'}>
              {results.baseline_status}
            </Tag>
          </div>
          {baselineMetricEntries.length > 0 ? (
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              {baselineMetricEntries.map(([key, value]) => (
                <div key={key} className="rounded-xl border border-cyan-400/20 bg-cyan-400/10 px-3 py-2">
                  <div className="text-[11px] uppercase tracking-[0.14em] text-cyan-200">{key}</div>
                  <div className="mt-1 text-sm font-medium text-slate-100">{formatMetricValue(value)}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-3 text-xs text-slate-500">暂无结构化 baseline 指标。</div>
          )}
          {results.baseline_completed_at ? (
            <div className="mt-2 text-xs text-slate-500">Completed {formatDateTime(results.baseline_completed_at)}</div>
          ) : null}
        </div>

        <div className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-sm font-medium text-slate-100">Tuning / Compare</div>
              <div className="mt-1 text-xs text-slate-500">
                {results.tuning_execution_id ? `tuning=${results.tuning_execution_id}` : '尚无 tuning execution'}
              </div>
            </div>
            <Space wrap>
              <Tag color={stageStatusColorMap[results.tuning_status] || executionStatusColorMap[results.tuning_status] || 'default'}>{results.tuning_status}</Tag>
              <Tag color={stageStatusColorMap[results.compare_status] || 'default'}>{results.compare_status}</Tag>
            </Space>
          </div>
          {tuningMetricEntries.length > 0 ? (
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              {tuningMetricEntries.map(([key, value]) => (
                <div key={key} className="rounded-xl border border-violet-400/20 bg-violet-400/10 px-3 py-2">
                  <div className="text-[11px] uppercase tracking-[0.14em] text-violet-200">{key}</div>
                  <div className="mt-1 text-sm font-medium text-slate-100">{formatMetricValue(value)}</div>
                </div>
              ))}
            </div>
          ) : null}
          {results.compare_summary ? (
            <div className="mt-3 rounded-xl border border-slate-800/80 bg-slate-950/70 px-3 py-2 text-sm text-slate-300">
              {results.compare_summary}
            </div>
          ) : null}
          {results.highlights.length > 0 ? (
            <div className="mt-3 space-y-1 text-xs text-slate-400">
              {results.highlights.map((item) => (
                <div key={item}>{item}</div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </Card>
  )
}

function ExecutionsCard({
  executions,
  workspaceId,
  onCancel,
  cancelingExecutionId,
}: {
  executions: ResearchProjectExecutionSummary[]
  workspaceId: number
  onCancel: (workspaceId: number, executionId: string) => Promise<void>
  cancelingExecutionId?: string | null
}) {
  return (
    <Card className="!border-slate-700/60 !bg-slate-950/30" title={<span className="text-slate-100">Executions</span>}>
      {executions.length > 0 ? (
        <div className="space-y-3">
          {executions.map((execution) => {
            const canCancel = ['pending', 'running'].includes(String(execution.status || '').toLowerCase())
            return (
              <div key={execution.execution_id} className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-100">
                      {execution.label || execution.execution_id}
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      {[execution.runtime_type, execution.draft_id, execution.command_preview].filter(Boolean).join(' · ') || '未提供执行元信息'}
                    </div>
                  </div>
                  <Space wrap>
                    <Tag color={executionStatusColorMap[String(execution.status || 'unknown')] || 'default'}>{execution.status}</Tag>
                    {execution.latest_loss !== undefined && execution.latest_loss !== null ? (
                      <Tag color="cyan">loss {execution.latest_loss.toFixed(4)}</Tag>
                    ) : null}
                    {execution.latest_elapsed_sec ? (
                      <Tag color="blue">{formatSeconds(execution.latest_elapsed_sec)}</Tag>
                    ) : null}
                    {canCancel ? (
                      <Button
                        size="small"
                        danger
                        icon={<StopOutlined />}
                        loading={cancelingExecutionId === execution.execution_id}
                        onClick={() => void onCancel(workspaceId, execution.execution_id)}
                      >
                        停止
                      </Button>
                    ) : null}
                  </Space>
                </div>
                <div className="mt-3 grid gap-2 text-xs text-slate-400 md:grid-cols-2 xl:grid-cols-4">
                  <div>Execution ID: <Text code className="!text-[11px]">{execution.execution_id}</Text></div>
                  <div>Result: {execution.result_exists ? 'ready' : 'pending'}</div>
                  <div>Log: {execution.log_exists ? `${execution.log_total_chars} chars` : 'none'}</div>
                  <div>
                    Updated: {formatDateTime(execution.completed_at || execution.started_at || execution.created_at)}
                  </div>
                </div>
                {execution.error || execution.message || execution.last_log_line ? (
                  <div className="mt-3 rounded-xl border border-slate-800/70 bg-slate-950/70 px-3 py-2 text-xs text-slate-300">
                    <div className="font-medium text-slate-200">Latest signal</div>
                    <div className="mt-1 whitespace-pre-wrap break-words text-slate-400">
                      {execution.error || execution.message || execution.last_log_line}
                    </div>
                  </div>
                ) : null}
                <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-500">
                  {execution.spec_relative_path ? <Text code className="!text-[11px]">{execution.spec_relative_path}</Text> : null}
                  {execution.result_relative_path ? <Text code className="!text-[11px]">{execution.result_relative_path}</Text> : null}
                  {execution.log_relative_path ? <Text code className="!text-[11px]">{execution.log_relative_path}</Text> : null}
                </div>
                {execution.log_tail ? (
                  <details className="mt-3">
                    <summary className="cursor-pointer text-xs text-slate-400">查看日志尾部</summary>
                    <pre className="mt-2 max-h-64 overflow-auto rounded-xl border border-slate-800/70 bg-slate-950/60 p-3 text-[11px] leading-5 text-slate-300">
                      {execution.log_tail}
                    </pre>
                  </details>
                ) : null}
              </div>
            )
          })}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="当前 workspace 还没有 execution 产物"
        />
      )}
    </Card>
  )
}

function WorkspaceOutputsCard({
  projectId,
  workspaceId,
  onOutputsChanged,
}: {
  projectId: number
  workspaceId: number
  onOutputsChanged?: () => Promise<void> | void
}) {
  const [outputs, setOutputs] = useState<ResearchProjectWorkspaceOutputSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [modalLoading, setModalLoading] = useState(false)
  const [cleanupLoading, setCleanupLoading] = useState(false)
  const [scopeCleanupLoading, setScopeCleanupLoading] = useState<string | null>(null)
  const [selectedPath, setSelectedPath] = useState('')
  const [outputContent, setOutputContent] = useState('')

  const outputScopes = useMemo(() => {
    const grouped = new Map<string, { label: string; items: ResearchProjectWorkspaceOutputSummary[] }>()
    for (const output of outputs) {
      const scope = String(output.scope || 'planning')
      const existing = grouped.get(scope)
      if (existing) {
        existing.items.push(output)
        continue
      }
      grouped.set(scope, { label: output.scope_label || scope, items: [output] })
    }
    const scopeOrder = ['planning', 'repo_analysis', 'grounding', 'implementation', 'run_drafts', 'executions', 'results']
    return scopeOrder
      .map((scope) => ({ scope, ...(grouped.get(scope) || { label: scope, items: [] as ResearchProjectWorkspaceOutputSummary[] }) }))
      .filter((entry) => entry.items.length > 0)
  }, [outputs])

  const loadOutputs = useCallback(async (options?: { silent?: boolean }) => {
    if (!options?.silent) setLoading(true)
    try {
      const data = await projectApi.listWorkspaceOutputs(projectId, workspaceId)
      setOutputs(data)
    } catch (error) {
      if (!options?.silent) {
        message.error(String((error as Error)?.message || '加载项目产物失败'))
      }
    } finally {
      if (!options?.silent) setLoading(false)
    }
  }, [projectId, workspaceId])

  useEffect(() => {
    void loadOutputs()
  }, [loadOutputs])

  const openOutput = async (output: ResearchProjectWorkspaceOutputSummary) => {
    setModalLoading(true)
    setModalOpen(true)
    setSelectedPath(output.relative_path)
    try {
      const payload = await projectApi.readWorkspaceOutput(projectId, workspaceId, output.relative_path)
      setOutputContent(payload.content)
    } catch (error) {
      message.error(String((error as Error)?.message || '读取项目产物失败'))
      setModalOpen(false)
    } finally {
      setModalLoading(false)
    }
  }

  const handleDelete = async (output: ResearchProjectWorkspaceOutputSummary) => {
    try {
      await projectApi.deleteWorkspaceOutput(projectId, workspaceId, output.relative_path)
      message.success(`已删除 ${output.relative_path}`)
      await loadOutputs({ silent: true })
      await onOutputsChanged?.()
    } catch (error) {
      message.error(String((error as Error)?.message || '删除项目产物失败'))
    }
  }

  const handleCleanup = () => {
    Modal.confirm({
      title: '一键清空项目产物',
      content: '将删除当前 workspace 下除 repo/source 以外的报告、分析结果、spec、draft、执行结果与 compare report，并清空运行记录。是否继续？',
      okText: '清空',
      okButtonProps: { danger: true, loading: cleanupLoading },
      cancelText: '取消',
      onOk: async () => {
        setCleanupLoading(true)
        try {
          const result = await projectApi.cleanupWorkspaceOutputs(projectId, workspaceId, { preserve_repo: true })
          message.success(`已清理 ${result.deleted_file_count} 个文件、${result.deleted_dir_count} 个目录、${result.deleted_run_count} 条运行记录`)
          await loadOutputs({ silent: true })
          await onOutputsChanged?.()
        } catch (error) {
          message.error(String((error as Error)?.message || '清空项目产物失败'))
        } finally {
          setCleanupLoading(false)
        }
      },
    })
  }

  const handleScopeCleanup = (scope: string, label: string) => {
    Modal.confirm({
      title: `清空 ${label}`,
      content: `将删除当前 workspace 下属于 ${label} 的产物。是否继续？`,
      okText: '清空',
      okButtonProps: { danger: true, loading: scopeCleanupLoading === scope },
      cancelText: '取消',
      onOk: async () => {
        setScopeCleanupLoading(scope)
        try {
          const result = await projectApi.cleanupWorkspaceOutputScope(projectId, workspaceId, {
            scope: scope as 'planning' | 'repo_analysis' | 'grounding' | 'implementation' | 'run_drafts' | 'executions' | 'results',
          })
          message.success(`已清空 ${label}：${result.deleted_file_count} 个文件、${result.deleted_dir_count} 个目录、${result.deleted_run_count} 条运行记录`)
          await loadOutputs({ silent: true })
          await onOutputsChanged?.()
        } catch (error) {
          message.error(String((error as Error)?.message || `清空 ${label} 失败`))
        } finally {
          setScopeCleanupLoading(null)
        }
      },
    })
  }

  return (
    <>
      <Card
        className="!border-slate-700/60 !bg-slate-950/30"
        title={<span className="text-slate-100">Project Outputs</span>}
        extra={(
          <Space wrap>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void loadOutputs()}>刷新</Button>
            <Button size="small" danger icon={<DeleteOutlined />} loading={cleanupLoading} onClick={handleCleanup}>一键清空产物</Button>
          </Space>
        )}
      >
        {outputScopes.length > 0 ? (
          <div className="mb-4 flex flex-wrap gap-2">
            {outputScopes.map((entry) => (
              <Button
                key={`scope:${entry.scope}`}
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={scopeCleanupLoading === entry.scope}
                onClick={() => handleScopeCleanup(entry.scope, entry.label)}
              >
                清空 {entry.label}
              </Button>
            ))}
          </div>
        ) : null}
        {loading ? (
          <div className="flex min-h-[220px] items-center justify-center"><Spin /></div>
        ) : outputs.length > 0 ? (
          <div className="space-y-3">
            {outputScopes.map((entry) => (
              <div key={`group:${entry.scope}`} className="space-y-3">
                <div className="flex items-center gap-2">
                  <Text className="!text-xs !font-medium !uppercase !tracking-[0.16em] !text-slate-500">{entry.label}</Text>
                  <Tag>{entry.items.length}</Tag>
                </div>
                {entry.items.map((output) => (
                  <div key={output.relative_path} className="rounded-2xl border border-slate-800/80 bg-slate-950/50 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-slate-100">{output.label}</div>
                        <div className="mt-1 text-xs text-slate-500">{output.relative_path}</div>
                      </div>
                      <Space wrap>
                        <Tag color="blue">{outputCategoryLabel(output.category)}</Tag>
                        <Tag color={output.storage === 'db_record' ? 'purple' : 'cyan'}>{artifactKindLabel(output.kind)}</Tag>
                      </Space>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400">
                      <div>size: {formatBytes(output.size_bytes)}</div>
                      <div>updated: {formatDateTime(output.updated_at)}</div>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button size="small" icon={<EyeOutlined />} onClick={() => void openOutput(output)}>查看</Button>
                      <Button size="small" danger icon={<DeleteOutlined />} disabled={!output.deletable} onClick={() => void handleDelete(output)}>删除</Button>
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前 workspace 还没有可管理的项目产物" />
        )}
      </Card>

      <Modal
        title="查看项目产物"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => setModalOpen(false)}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
        width={900}
      >
        {modalLoading ? (
          <div className="flex min-h-[280px] items-center justify-center"><Spin /></div>
        ) : (
          <div className="space-y-3">
            <Input
              value={selectedPath}
              disabled
            />
            <TextArea
              rows={18}
              value={outputContent}
              readOnly
              spellCheck={false}
              className="font-mono"
            />
          </div>
        )}
      </Modal>
    </>
  )
}

export default function ProjectsPage() {
  const navigate = useNavigate()
  const { projectId } = useParams()
  const [projects, setProjects] = useState<ResearchProject[]>([])
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [selectedProject, setSelectedProject] = useState<ResearchProject | null>(null)
  const [runtimeOverview, setRuntimeOverview] = useState<ResearchProjectRuntimeOverview | null>(null)
  const [runtimeLoading, setRuntimeLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [openingChat, setOpeningChat] = useState(false)
  const [cancelingExecutionId, setCancelingExecutionId] = useState<string | null>(null)
  const [form] = Form.useForm<ProjectFormValues>()

  const numericProjectId = useMemo(() => {
    const parsed = Number(projectId || 0)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }, [projectId])

  const relatedPapers = useMemo(
    () => selectedProject?.papers.filter((paper) => String(paper.role || 'related') !== 'primary') || [],
    [selectedProject],
  )

  const workspaceOverviews = useMemo(() => {
    const items = [...(runtimeOverview?.workspaces || [])]
    const primaryWorkspaceId = runtimeOverview?.primary_workspace_id || selectedProject?.primary_workspace_id
    items.sort((left, right) => {
      if (left.workspace_id === primaryWorkspaceId) return -1
      if (right.workspace_id === primaryWorkspaceId) return 1
      return (right.running_execution_count || 0) - (left.running_execution_count || 0)
    })
    return items
  }, [runtimeOverview, selectedProject])

  const unifiedChatStage = useMemo<PaperWorkflowStage>(() => {
    const stage = String(runtimeOverview?.recommended_chat_stage || 'planning').trim()
    if (stage === 'implementation_prep' || stage === 'run_drafts' || stage === 'execution' || stage === 'tuning') return stage
    return 'planning'
  }, [runtimeOverview])

  const loadProjects = async () => {
    setLoading(true)
    try {
      const data = await projectApi.listProjects()
      setProjects(data)
    } catch (error) {
      message.error(String((error as Error)?.message || '加载项目失败'))
    } finally {
      setLoading(false)
    }
  }

  const loadProjectDetail = async (id: number) => {
    setDetailLoading(true)
    try {
      const data = await projectApi.getProject(id)
      setSelectedProject(data)
    } catch (error) {
      setSelectedProject(null)
      message.error(String((error as Error)?.message || '加载项目详情失败'))
    } finally {
      setDetailLoading(false)
    }
  }

  const loadRuntimeOverview = async (id: number, options?: { silent?: boolean }) => {
    if (!options?.silent) setRuntimeLoading(true)
    try {
      const data = await projectApi.getProjectRuntimeOverview(id, { recent_execution_limit: 8, max_log_chars: 8000 })
      setRuntimeOverview(data)
    } catch (error) {
      if (!options?.silent) {
        message.error(String((error as Error)?.message || '加载项目运行状态失败'))
      }
      setRuntimeOverview(null)
    } finally {
      if (!options?.silent) setRuntimeLoading(false)
    }
  }

  useEffect(() => {
    void loadProjects()
  }, [])

  useEffect(() => {
    if (numericProjectId) {
      void loadProjectDetail(numericProjectId)
      void loadRuntimeOverview(numericProjectId)
      return
    }
    setSelectedProject(null)
    setRuntimeOverview(null)
  }, [numericProjectId])

  useEffect(() => {
    if (!numericProjectId) return undefined
    const timer = window.setInterval(() => {
      void loadRuntimeOverview(numericProjectId, { silent: true })
    }, 15000)
    return () => window.clearInterval(timer)
  }, [numericProjectId])

  const handleCreateProject = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      const created = await projectApi.createProject({
        title: values.title,
        goal: values.goal,
        status: 'draft',
      })
      setCreateOpen(false)
      form.resetFields()
      await loadProjects()
      navigate(`/projects/${created.id}`)
      message.success('项目已创建')
    } catch (error) {
      message.error(String((error as Error)?.message || '创建项目失败'))
    } finally {
      setCreating(false)
    }
  }

  const handleContinueInChat = async (project: ResearchProject) => {
    if (!project.primary_paper?.id) {
      message.warning('当前项目还没有主论文，无法继续 paper workflow')
      return
    }
    setOpeningChat(true)
    try {
      const conversation = await chatApi.createConversation(`研究推进：${String(project.title || '').slice(0, 40)}`)
      const { initialMessage, skillLaunch } = buildStageLaunch(unifiedChatStage, {
        paperId: project.primary_paper.id,
        projectId: project.id,
        goal: project.goal || null,
      })
      navigate(`/chat/${conversation.id}`, { state: { initialMessage, initialSkillLaunch: skillLaunch } })
    } catch (error) {
      message.error(String((error as Error)?.message || '打开 Chat 失败'))
    } finally {
      setOpeningChat(false)
    }
  }

  const handleCancelExecution = async (workspaceId: number, executionId: string) => {
    if (!selectedProject) return
    setCancelingExecutionId(executionId)
    try {
      await projectApi.cancelProjectExecution(selectedProject.id, workspaceId, executionId)
      message.success(`已请求停止 ${executionId}`)
      await loadRuntimeOverview(selectedProject.id, { silent: true })
    } catch (error) {
      message.error(String((error as Error)?.message || '停止 execution 失败'))
    } finally {
      setCancelingExecutionId(null)
    }
  }

  return (
    <>
      <div className="space-y-6 text-slate-200">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-2">
            <div className="inline-flex items-center gap-2 rounded-full border border-cyan-400/20 bg-cyan-400/10 px-3 py-1 text-xs uppercase tracking-[0.22em] text-cyan-300">
              <ProjectOutlined />
              研究项目
            </div>
            <Title level={3} className="!mb-0 !text-slate-100">
              Project Observatory
            </Title>
            <Paragraph className="!mb-0 !max-w-4xl !text-slate-400">
              Project 页只做观测，不再承担 notebook 编辑或第二套 agent 编排。Chat 负责推进 skill 流程；这里负责看阶段落库产物、runtime 形态、执行状态和结果。
            </Paragraph>
          </div>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建项目
          </Button>
        </div>

        <div className="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
          <Card className="!border-slate-700/60 !bg-slate-900/55">
            {loading ? (
              <div className="flex min-h-[320px] items-center justify-center">
                <Spin />
              </div>
            ) : projects.length > 0 ? (
              <List
                itemLayout="vertical"
                dataSource={projects}
                renderItem={(item) => {
                  const active = numericProjectId === item.id
                  return (
                    <List.Item
                      key={item.id}
                      className={`cursor-pointer rounded-2xl border px-4 py-3 transition-all ${active ? 'border-cyan-400/30 bg-cyan-400/10' : 'border-slate-700/60 bg-slate-950/30 hover:border-slate-600/80'}`}
                      onClick={() => navigate(`/projects/${item.id}`)}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-base font-medium text-slate-100">{item.title}</div>
                          <div className="mt-1 text-xs text-slate-500">
                            {item.paper_count} papers · {item.workspace_count} workspaces
                          </div>
                        </div>
                        <Tag color={statusColorMap[String(item.status || 'draft')] || 'default'}>{item.status}</Tag>
                      </div>
                      <div className="mt-3 text-sm text-slate-400">
                        {String(item.goal || item.summary?.['entry_mode'] || '尚未设置项目目标')}
                      </div>
                    </List.Item>
                  )
                }}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有研究项目">
                <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                  创建第一个项目
                </Button>
              </Empty>
            )}
          </Card>

          <Card className="!border-slate-700/60 !bg-slate-900/55">
            {detailLoading ? (
              <div className="flex min-h-[320px] items-center justify-center">
                <Spin />
              </div>
            ) : selectedProject ? (
              <div className="space-y-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <Title level={4} className="!mb-1 !text-slate-100">
                      {selectedProject.title}
                    </Title>
                    <div className="text-sm text-slate-500">
                      Updated {formatDateTime(selectedProject.updated_at)}
                    </div>
                  </div>
                  <Space wrap>
                    <Tag color={statusColorMap[String(selectedProject.status || 'draft')] || 'default'}>{selectedProject.status}</Tag>
                    {runtimeOverview ? (
                      <>
                        <Tag color={statusColorMap[String(runtimeOverview.current_status || 'draft')] || 'default'}>
                          {runtimeOverview.current_status}
                        </Tag>
                        <Tag color="cyan">{runtimeOverview.current_stage}</Tag>
                      </>
                    ) : null}
                  </Space>
                </div>

                <Card className="!border-slate-700/60 !bg-slate-950/30">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div className="space-y-3">
                      <div>
                        <div className="text-xs uppercase tracking-[0.18em] text-slate-500">Goal</div>
                        <div className="mt-2 max-w-4xl text-sm leading-7 text-slate-300">
                          {selectedProject.goal || '尚未设置目标'}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2 text-xs text-slate-400">
                        <Tag color="blue">{runtimeOverview?.workspace_count || selectedProject.workspace_count} workspaces</Tag>
                        <Tag color={runtimeOverview?.running_execution_count ? 'processing' : 'default'}>
                          {runtimeOverview?.running_execution_count || 0} running
                        </Tag>
                        <Tag color="gold">{runtimeOverview?.execution_count || 0} tracked executions</Tag>
                      </div>
                      {runtimeOverview?.continue_reason ? (
                        <Alert
                          type="info"
                          showIcon
                          message="当前阶段判断"
                          description={runtimeOverview.continue_reason}
                        />
                      ) : null}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {selectedProject.primary_paper ? (
                        <Button
                          icon={<BookOutlined />}
                          onClick={() => navigate(`/literature/${selectedProject.primary_paper?.id}/read`)}
                        >
                          打开主论文
                        </Button>
                      ) : null}
                      <Button
                        icon={<ExperimentOutlined />}
                        loading={openingChat}
                        onClick={() => void handleContinueInChat(selectedProject)}
                      >
                        {stageActionLabelMap[unifiedChatStage]}（Chat）
                      </Button>
                      <Button
                        icon={<ReloadOutlined />}
                        loading={runtimeLoading}
                        onClick={() => numericProjectId && void loadRuntimeOverview(numericProjectId)}
                      >
                        刷新状态
                      </Button>
                    </div>
                  </div>
                </Card>

                {workspaceOverviews.length > 0 ? (
                  <div className="space-y-5">
                    {workspaceOverviews.map((workspace) => (
                      <Card key={workspace.workspace_id} className="!border-slate-700/60 !bg-slate-950/25">
                        <div className="space-y-4">
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="truncate text-base font-semibold text-slate-100">{workspace.title}</div>
                              <div className="mt-1 text-xs text-slate-500">
                                {[workspace.paper_title, workspace.role, `${workspace.run_count} runs`].filter(Boolean).join(' · ')}
                              </div>
                            </div>
                            <Space wrap>
                              <Tag color={statusColorMap[String(workspace.current_status || workspace.status || 'draft')] || 'default'}>
                                {workspace.current_status || workspace.status}
                              </Tag>
                              <Tag color="cyan">{workspace.current_stage}</Tag>
                              <Tag color={workspace.running_execution_count ? 'processing' : 'default'}>
                                {workspace.running_execution_count} running
                              </Tag>
                            </Space>
                          </div>

                          <div className="flex flex-wrap gap-2">
                            {workspace.paper_id ? (
                              <Button size="small" icon={<BookOutlined />} onClick={() => navigate(`/literature/${workspace.paper_id}/read`)}>
                                打开论文
                              </Button>
                            ) : null}
                            {workspace.runtime_context.notebook_asset_relative_path && workspace.notebook_id ? (
                              <Button size="small" icon={<CodeOutlined />} onClick={() => navigate(`/code/${workspace.notebook_id}`)}>
                                查看 Notebook
                              </Button>
                            ) : null}
                            {workspace.runtime_context.repo_reference_url ? (
                              <Button size="small" icon={<LinkOutlined />} onClick={() => openExternal(workspace.runtime_context.repo_reference_url)}>
                                打开仓库
                              </Button>
                            ) : null}
                          </div>

                          <div className="grid gap-4 xl:grid-cols-2">
                            <StageLedgerCard stages={workspace.stage_ledger} />
                            <RuntimeContextCard
                              runtimeContext={workspace.runtime_context}
                              notebookId={workspace.notebook_id}
                              onOpenNotebook={workspace.notebook_id ? () => navigate(`/code/${workspace.notebook_id}`) : undefined}
                            />
                            <ResultsCard results={workspace.results} />
                            <ExecutionsCard
                              executions={workspace.recent_executions}
                              workspaceId={workspace.workspace_id}
                              onCancel={handleCancelExecution}
                              cancelingExecutionId={cancelingExecutionId}
                            />
                          </div>
                          <WorkspaceOutputsCard
                            projectId={selectedProject.id}
                            workspaceId={workspace.workspace_id}
                            onOutputsChanged={async () => {
                              await loadRuntimeOverview(selectedProject.id, { silent: true })
                              await loadProjectDetail(selectedProject.id)
                            }}
                          />
                        </div>
                      </Card>
                    ))}
                  </div>
                ) : (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="当前项目还没有 workspace；从论文页或 Chat 启动 paper workflow 后会出现在这里"
                  />
                )}

                <Card className="!border-slate-700/60 !bg-slate-950/30" title={<span className="text-slate-100">Related Papers</span>}>
                  {relatedPapers.length > 0 ? (
                    <div className="space-y-3">
                      {relatedPapers.map((paper) => (
                        <div key={paper.id} className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-700/60 bg-slate-950/30 px-4 py-3">
                          <div className="min-w-0">
                            <div className="truncate text-sm font-medium text-slate-100">{paper.title}</div>
                            <div className="mt-1 text-xs text-slate-500">
                              {[paper.venue, paper.year, paper.role].filter(Boolean).join(' · ') || '未标注文献信息'}
                            </div>
                          </div>
                          <Button size="small" icon={<BookOutlined />} onClick={() => navigate(`/literature/${paper.id}/read`)}>
                            阅读
                          </Button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前项目还没有相关论文" />
                  )}
                </Card>
              </div>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择一个项目，或先创建新项目" />
            )}
          </Card>
        </div>
      </div>

      <Modal
        title="新建研究项目"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void handleCreateProject()}
        confirmLoading={creating}
        okText="创建"
      >
        <Form form={form} layout="vertical">
          <Form.Item name="title" label="项目标题">
            <Input placeholder="例如：Tabular DL reproduction and extension" />
          </Form.Item>
          <Form.Item name="goal" label="项目目标">
            <TextArea rows={5} placeholder="例如：复现论文 baseline，并结合相关 arXiv 工作提出并验证改进方向" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
