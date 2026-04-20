import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Empty, Space, Tag, Typography, message } from 'antd'
import {
  ArrowRightOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  FolderOpenOutlined,
  ProjectOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import {
  chatApi,
  literatureApi,
  projectApi,
  type Paper,
  type PaperExperimentWorkspace,
  type ResearchProject,
} from '@/services/api'
import {
  buildPaperExecutionLaunch,
  buildPaperImplementationPrepLaunch,
  buildPaperPlanningLaunch,
} from '@/utils/paperWorkflowPrompts'

const { Paragraph, Text } = Typography

interface PaperProjectLauncherCardProps {
  paper: Paper
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

const asObjectList = (value: unknown): Record<string, unknown>[] =>
  Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : []

const clipText = (value: unknown, limit = 120): string => {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trim()}…`
}

const formatMetricLabel = (item: Record<string, unknown>): string => {
  const name = String(item.name || '').trim()
  const direction = String(item.direction || '').trim()
  if (!name) return ''
  if (!direction || direction === 'unknown') return name
  return `${name} · ${direction === 'higher_is_better' ? '越高越好' : '越低越好'}`
}

const formatRunLabel = (item: Record<string, unknown>): string => {
  const label = String(item.label || '').trim()
  const goal = String(item.goal || '').trim()
  const expectedEffect = String(item.expected_effect || '').trim()
  const base = label || goal
  if (!base) return ''
  return expectedEffect ? `${base} · ${clipText(expectedEffect, 56)}` : base
}

export default function PaperProjectLauncherCard({ paper }: PaperProjectLauncherCardProps) {
  const navigate = useNavigate()
  const [projects, setProjects] = useState<ResearchProject[]>([])
  const [workspace, setWorkspace] = useState<PaperExperimentWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [openingChatAction, setOpeningChatAction] = useState<'planning' | 'implementation' | 'execution' | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const loadState = useCallback(async () => {
    setLoading(true)
    try {
      const [projectData, workspaceData] = await Promise.all([
        projectApi.listProjects({ paper_id: paper.id }),
        literatureApi
          .getPaperExperimentWorkspace(paper.id)
          .catch((error: unknown) => {
            const status = Number((error as { response?: { status?: number } })?.response?.status || 0)
            if (status === 404) return null
            throw error
          }),
      ])
      setProjects(projectData)
      setWorkspace(workspaceData)
    } catch (error) {
      message.error(String((error as Error)?.message || '加载论文规划状态失败'))
    } finally {
      setLoading(false)
    }
  }, [paper.id])

  useEffect(() => {
    void loadState()
  }, [loadState])

  const handleOpenPlanningChat = async () => {
    setOpeningChatAction('planning')
    try {
      const conversation = await chatApi.createConversation(`论文规划：${String(paper.title || '').slice(0, 40)}`)
      const { initialMessage, skillLaunch } = buildPaperPlanningLaunch({
        paperId: paper.id,
        projectId: projects[0]?.id || null,
        goal: projects[0]?.goal || null,
      })
      navigate(`/chat/${conversation.id}`, { state: { initialMessage, initialSkillLaunch: skillLaunch } })
    } catch (error) {
      message.error(String((error as Error)?.message || '打开论文规划流程失败'))
    } finally {
      setOpeningChatAction(null)
    }
  }

  const handleOpenImplementationPrepChat = async () => {
    setOpeningChatAction('implementation')
    try {
      const conversation = await chatApi.createConversation(`实施准备：${String(paper.title || '').slice(0, 40)}`)
      const primaryProject = projects[0] || null
      const { initialMessage, skillLaunch } = buildPaperImplementationPrepLaunch({
        paperId: paper.id,
        projectId: primaryProject?.id || null,
        goal: primaryProject?.goal || null,
      })
      navigate(`/chat/${conversation.id}`, { state: { initialMessage, initialSkillLaunch: skillLaunch } })
    } catch (error) {
      message.error(String((error as Error)?.message || '打开 implementation-prep 失败'))
    } finally {
      setOpeningChatAction(null)
    }
  }

  const handleOpenExecutionChat = async () => {
    setOpeningChatAction('execution')
    try {
      const conversation = await chatApi.createConversation(`执行复现：${String(paper.title || '').slice(0, 40)}`)
      const primaryProject = projects[0] || null
      const { initialMessage, skillLaunch } = buildPaperExecutionLaunch({
        paperId: paper.id,
        projectId: primaryProject?.id || null,
        goal: primaryProject?.goal || null,
      })
      navigate(`/chat/${conversation.id}`, { state: { initialMessage, initialSkillLaunch: skillLaunch } })
    } catch (error) {
      message.error(String((error as Error)?.message || '打开 execution 阶段失败'))
    } finally {
      setOpeningChatAction(null)
    }
  }

  const handleRefreshIntake = async () => {
    setRefreshing(true)
    try {
      const nextWorkspace = await literatureApi.refreshPaperExperimentWorkspaceIntake(paper.id)
      setWorkspace(nextWorkspace)
      message.success('规划输入已刷新')
    } catch (error) {
      message.error(String((error as Error)?.message || '刷新规划输入失败'))
    } finally {
      setRefreshing(false)
    }
  }

  const primaryProject = projects[0] || null
  const experimentSpec = useMemo(() => asRecord(workspace?.experiment_spec), [workspace?.experiment_spec])
  const optimizationBrief = useMemo(
    () => asRecord(experimentSpec.optimization_brief),
    [experimentSpec],
  )
  const intakeStatus = useMemo(() => asRecord(experimentSpec.intake_status), [experimentSpec])
  const intakeInput = useMemo(() => asRecord(intakeStatus.input), [intakeStatus])
  const baseline = useMemo(() => asRecord(experimentSpec.baseline), [experimentSpec])
  const task = useMemo(() => asRecord(experimentSpec.task), [experimentSpec])
  const metrics = useMemo(() => asObjectList(experimentSpec.metrics).slice(0, 6), [experimentSpec])
  const safeKnobs = useMemo(() => asObjectList(experimentSpec.safe_knobs).slice(0, 8), [experimentSpec])
  const modelSwaps = useMemo(() => asObjectList(experimentSpec.allowed_model_swaps).slice(0, 6), [experimentSpec])
  const firstRuns = useMemo(
    () => asObjectList(optimizationBrief.first_runs).slice(0, 4),
    [optimizationBrief],
  )

  const sourceMode = String(intakeInput.source_mode || '').trim()
  const sourceModeLabel =
    sourceMode === 'local_pdf_markdown'
      ? '真实 PDF -> Markdown'
      : sourceMode === 'metadata_abstract_fallback'
        ? '摘要回退'
        : '未生成'
  const extractor = String(intakeInput.extractor || '').trim()
  const hasIntake = Boolean(intakeStatus.has_llm_intake)
  const truncated = Boolean(intakeInput.truncated)
  const sentChars = Number(intakeInput.sent_chars || 0)
  const totalChars = Number(intakeInput.total_chars || 0)
  const intakeError = String(intakeStatus.error || '').trim()
  const notebookUrl = workspace?.notebook_id ? `/code/${workspace.notebook_id}` : ''

  return (
    <Card
      size="small"
      className="!border-slate-700/60 !bg-slate-900/55"
      title={(
        <div className="flex items-center gap-2 text-slate-100">
          <FileSearchOutlined className="text-cyan-400" />
          <span>复现 / 调优规划</span>
        </div>
      )}
      extra={
        primaryProject ? (
          <Button size="small" icon={<ProjectOutlined />} onClick={() => navigate(`/projects/${primaryProject.id}`)}>
            Project
          </Button>
        ) : null
      }
    >
      {loading ? (
        <div className="text-sm text-slate-400">正在检查这篇论文的规划状态...</div>
      ) : workspace ? (
        <Space direction="vertical" size={12} className="w-full">
          <Alert
            type={sourceMode === 'local_pdf_markdown' ? 'success' : 'warning'}
            showIcon
            message={hasIntake ? '已生成可复用的规划输入' : '已创建工作区，但 intake 结果不完整'}
            description={
              sourceMode === 'local_pdf_markdown'
                ? '当前规划来自真实 PDF -> markdown -> intake LLM 链路。后续继续规划时应复用这份结果。'
                : '当前结果不是完整 PDF 规划链路，可能只是摘要回退或 intake 不完整。继续使用前建议刷新 intake。'
            }
          />

          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            <div className="rounded-2xl border border-slate-700/60 bg-slate-950/35 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">规划来源</div>
              <div className="mt-1 text-sm font-medium text-slate-100">{sourceModeLabel}</div>
              <div className="mt-1 text-xs leading-5 text-slate-400">
                {extractor ? `extractor: ${extractor}` : '尚未记录 extractor'}
              </div>
            </div>
            <div className="rounded-2xl border border-slate-700/60 bg-slate-950/35 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Intake JSON</div>
              <div className="mt-1 text-sm font-medium text-slate-100">{hasIntake ? '已生成' : '缺失'}</div>
              <div className="mt-1 text-xs leading-5 text-slate-400">
                {totalChars > 0 ? `送入 ${sentChars}/${totalChars} chars${truncated ? ' · 已截断' : ''}` : '暂无 markdown 输入统计'}
              </div>
            </div>
          </div>

          <div className="rounded-2xl border border-slate-700/60 bg-slate-950/35 px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <Text className="!text-slate-200 !font-medium">
                {String(task.problem_statement || task.task_type || '当前还没有稳定任务摘要')}
              </Text>
              {String(task.domain || '').trim() ? <Tag color="blue">{String(task.domain)}</Tag> : null}
              {String(baseline.model_family || '').trim() ? <Tag color="purple">{String(baseline.model_family)}</Tag> : null}
            </div>
            <Paragraph className="!mb-0 !mt-2 !text-sm !leading-6 !text-slate-400">
              {String(
                baseline.entrypoint_hint ||
                  optimizationBrief.human_summary ||
                  '当前尚未形成稳定 baseline 入口提示，建议先刷新 intake 或回到 Chat 继续收束。',
              )}
            </Paragraph>
          </div>

          <div className="space-y-2">
            <div className="text-xs uppercase tracking-[0.14em] text-slate-500">规划摘要</div>
            <div className="flex flex-wrap gap-2">
              {metrics.length ? metrics.map((item) => {
                const label = formatMetricLabel(item)
                return label ? <Tag key={label} color="geekblue">{label}</Tag> : null
              }) : <Tag>暂无指标</Tag>}
            </div>
            <div className="flex flex-wrap gap-2">
              {safeKnobs.length ? safeKnobs.map((item) => {
                const key = String(item.key || item.label || '').trim()
                return key ? <Tag key={key} color="green">{key}</Tag> : null
              }) : <Tag>暂无 safe knobs</Tag>}
            </div>
            <div className="flex flex-wrap gap-2">
              {modelSwaps.length ? modelSwaps.map((item) => {
                const name = String(item.name || '').trim()
                return name ? <Tag key={name} color="magenta">{name}</Tag> : null
              }) : <Tag>暂无 model swaps</Tag>}
            </div>
            <div className="space-y-1">
              {firstRuns.length ? firstRuns.map((item, index) => {
                const label = formatRunLabel(item)
                return label ? (
                  <div key={`${label}-${index}`} className="text-sm leading-6 text-slate-300">
                    {index + 1}. {label}
                  </div>
                ) : null
              }) : <div className="text-sm leading-6 text-slate-500">当前没有 first runs 建议。</div>}
            </div>
          </div>

          <div className="rounded-2xl border border-dashed border-slate-700/60 bg-slate-950/25 px-4 py-3 text-xs leading-6 text-slate-500">
            中间产物已写入当前 workspace：
            <span className="text-slate-400"> `paper_intake_markdown.md`、`paper_intake_payload.json`、`paper_intake_result.json`、`experiment_spec.json`</span>
            {notebookUrl ? (
              <>
                {' '}· Notebook:
                <button
                  type="button"
                  onClick={() => navigate(notebookUrl)}
                  className="ml-1 text-cyan-300 transition hover:text-cyan-200"
                >
                  {notebookUrl}
                </button>
              </>
            ) : null}
          </div>

          {intakeError ? (
            <Alert
              type="warning"
              showIcon
              message="最近一次 intake 有异常"
              description={clipText(intakeError, 220)}
            />
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button
              type="primary"
              icon={<ExperimentOutlined />}
              loading={openingChatAction === 'planning'}
              onClick={() => void handleOpenPlanningChat()}
            >
              继续在 Chat 生成规划
            </Button>
            <Button
              icon={<ArrowRightOutlined />}
              loading={openingChatAction === 'implementation'}
              onClick={() => void handleOpenImplementationPrepChat()}
            >
              继续在 Chat 做实施准备
            </Button>
            <Button
              icon={<ExperimentOutlined />}
              loading={openingChatAction === 'execution'}
              onClick={() => void handleOpenExecutionChat()}
            >
              开始执行复现
            </Button>
            <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void handleRefreshIntake()}>
              刷新规划输入
            </Button>
            {workspace.notebook_id ? (
              <Button icon={<FolderOpenOutlined />} onClick={() => navigate(notebookUrl)}>
                打开 Notebook
              </Button>
            ) : null}
            {primaryProject ? (
              <Button icon={<ArrowRightOutlined />} onClick={() => navigate(`/projects/${primaryProject.id}`)}>
                打开 Project
              </Button>
            ) : null}
          </div>
        </Space>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="这篇论文还没有生成复现 / 调优规划"
        >
          <Space direction="vertical" size={10} className="items-start">
            <div className="text-sm leading-6 text-slate-500">
              从论文详情页显式触发固定 workflow。系统会基于已保存论文的 PDF {'->'} markdown {'->'} intake
              {' '}LLM 结果，生成复现/调优规划，并把中间产物落到对应 workspace。
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="primary"
                icon={<ExperimentOutlined />}
                loading={openingChatAction === 'planning'}
                onClick={() => void handleOpenPlanningChat()}
              >
                生成复现 / 调优规划
              </Button>
              <Button
                icon={<ArrowRightOutlined />}
                loading={openingChatAction === 'implementation'}
                onClick={() => void handleOpenImplementationPrepChat()}
              >
                直接进入实施准备
              </Button>
              {primaryProject ? (
                <Button icon={<ArrowRightOutlined />} onClick={() => navigate(`/projects/${primaryProject.id}`)}>
                  打开 Project
                </Button>
              ) : null}
            </div>
          </Space>
        </Empty>
      )}
    </Card>
  )
}
