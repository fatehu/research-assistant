import { useMemo, useState, type ReactNode } from 'react'
import { Button } from 'antd'
import {
  BranchesOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CompressOutlined,
  ExpandOutlined,
  LoadingOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import type { ToolWorkflowSummary } from '@/services/api'
import { toolIcons, toolNames } from '../constants'

export interface TurnLaneStep {
  type: 'workflow' | 'thought' | 'action' | 'observation'
  iteration: number
  content?: string
  tool?: string
  toolCallId?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  workflowSummary?: ToolWorkflowSummary
  rawContent?: string
}

interface TurnProcessLanesProps {
  steps: TurnLaneStep[]
  title: string
  subtitle: string
  statusLabel?: string
  active?: boolean
  defaultExpanded?: boolean
  embedded?: boolean
}

interface ToolAttempt {
  key: string
  tool?: string
  toolCallId?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  pending: boolean
}

interface AttemptBlock {
  iteration: number
  processTexts: string[]
  toolAttempts: ToolAttempt[]
  workflowSteps: Array<{
    headline: string
    status?: string
    highlights: string[]
    nextAction?: string
    blockedReason?: string
    evidenceRefs: string[]
    rawContent?: string
  }>
}

const clipMultilineText = (value: string | undefined, maxChars = 320, maxLines = 6): string => {
  const text = String(value || '').trim()
  if (!text) return ''
  const lines = text.split('\n')
  const clippedByLines = lines.slice(0, maxLines).join('\n')
  const clipped =
    clippedByLines.length > maxChars ? `${clippedByLines.slice(0, maxChars).trimEnd()}…` : clippedByLines
  return clipped.length < text.length || lines.length > maxLines ? `${clipped}…` : clipped
}

const shouldCollapseText = (value: string | undefined, maxChars = 320, maxLines = 6): boolean => {
  const text = String(value || '').trim()
  if (!text) return false
  return text.length > maxChars || text.split('\n').length > maxLines
}

const ExpandableTextBlock = ({
  text,
  monospace = false,
  variant = 'default',
  expandLabel = '展开原文',
  collapseLabel = '收起原文',
}: {
  text?: string
  monospace?: boolean
  variant?: 'default' | 'evidence' | 'state'
  expandLabel?: string
  collapseLabel?: string
}) => {
  const normalized = String(text || '').trim()
  const [expanded, setExpanded] = useState(false)

  if (!normalized) return null

  const collapsible = shouldCollapseText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)
  const preview = clipMultilineText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)
  const lineCount = normalized.split('\n').length
  const charCount = normalized.length

  if (variant === 'evidence' && !monospace) {
    return (
      <div className="overflow-hidden rounded-[22px] border border-emerald-400/12 bg-[linear-gradient(180deg,rgba(6,78,59,0.16),rgba(2,6,23,0.74))] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-3 py-2.5">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-200/85">
              结果原文
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              {lineCount} 行 · {charCount} 字
            </div>
          </div>
          {collapsible ? (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex items-center gap-1 rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-100 transition hover:border-emerald-300/28 hover:bg-emerald-500/14"
            >
              {expanded ? collapseLabel : expandLabel}
            </button>
          ) : (
            <span className="rounded-full border border-white/[0.06] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
              已完整展示
            </span>
          )}
        </div>

        <div className="px-3 py-3">
          <div className="rounded-2xl border border-white/[0.06] bg-slate-950/55 px-3 py-3 text-sm leading-6 text-slate-200">
            <div className="whitespace-pre-wrap break-words">{expanded || !collapsible ? normalized : preview}</div>
          </div>
        </div>

        {expanded ? (
          <div className="border-t border-white/[0.06] px-3 py-2 text-[11px] leading-5 text-slate-500">
            原文保留给人工核查，主流程仍以摘要和状态结论为准。
          </div>
        ) : null}
      </div>
    )
  }

  if (variant === 'state' && !monospace) {
    return (
      <div className="overflow-hidden rounded-[20px] border border-cyan-400/12 bg-[linear-gradient(180deg,rgba(8,47,73,0.14),rgba(2,6,23,0.7))] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-3 py-2.5">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-cyan-200/85">
              状态细节
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              {lineCount} 行 · {charCount} 字
            </div>
          </div>
          {collapsible ? (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex items-center gap-1.5 rounded-full border border-cyan-400/16 bg-cyan-500/10 px-2.5 py-1 text-[11px] font-medium text-cyan-100 transition hover:border-cyan-300/26 hover:bg-cyan-500/14"
            >
              {expanded ? <CompressOutlined /> : <ExpandOutlined />}
              {expanded ? collapseLabel : expandLabel}
            </button>
          ) : (
            <span className="rounded-full border border-white/[0.06] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
              已完整展示
            </span>
          )}
        </div>

        <div className="px-3 py-3">
          <div className="rounded-2xl border border-white/[0.06] bg-slate-950/55 px-3 py-3 text-sm leading-6 text-slate-200">
            <div className="whitespace-pre-wrap break-words">{expanded || !collapsible ? normalized : preview}</div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {monospace ? (
        <pre className="overflow-x-auto rounded-2xl border border-white/[0.06] bg-slate-950/80 px-3 py-2.5 text-[11px] leading-5 text-slate-400">
          {collapsible && !expanded ? preview : normalized}
        </pre>
      ) : (
        <div className="rounded-2xl border border-white/[0.06] bg-slate-950/65 px-3 py-2.5 text-sm leading-6 text-slate-300">
          <div className="whitespace-pre-wrap break-words">{collapsible && !expanded ? preview : normalized}</div>
        </div>
      )}
      {collapsible ? (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="text-[11px] text-cyan-300 transition hover:text-cyan-100"
        >
          {expanded ? collapseLabel : expandLabel}
        </button>
      ) : null}
    </div>
  )
}

const workflowStatusClass = (status: string | undefined): string => {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'blocked') return 'border-red-400/18 bg-red-500/10 text-red-100'
  if (normalized === 'waiting') return 'border-amber-400/18 bg-amber-500/10 text-amber-100'
  if (normalized === 'ready' || normalized === 'progressed') {
    return 'border-emerald-400/18 bg-emerald-500/10 text-emerald-100'
  }
  return 'border-cyan-400/18 bg-cyan-500/10 text-cyan-100'
}

const toolStatusClass = (attempt: ToolAttempt): string => {
  if (attempt.pending) return 'border-amber-400/18 bg-amber-500/10 text-amber-100'
  if (attempt.success === false) return 'border-red-400/18 bg-red-500/10 text-red-100'
  return 'border-emerald-400/18 bg-emerald-500/10 text-emerald-100'
}

const buildAttemptBlocks = (steps: TurnLaneStep[]): AttemptBlock[] => {
  const grouped = new Map<number, TurnLaneStep[]>()

  steps.forEach((step, index) => {
    const iteration = Math.max(step.iteration || 1, 1)
    const current = grouped.get(iteration) || []
    current.push({ ...step, rawContent: step.rawContent, content: step.content, iteration })
    grouped.set(iteration, current)
    void index
  })

  return [...grouped.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([iteration, iterationSteps]) => {
      const processTexts = iterationSteps
        .filter((step) => step.type === 'thought')
        .map((step) => String(step.content || '').trim())
        .filter(Boolean)

      const workflowSteps = iterationSteps
        .filter((step) => step.type === 'workflow')
        .map((step) => ({
          headline: String(step.workflowSummary?.headline || step.content || '流程状态已更新').trim(),
          status: String(step.workflowSummary?.status || '').trim() || undefined,
          highlights: Array.isArray(step.workflowSummary?.highlights)
            ? step.workflowSummary?.highlights.filter(Boolean).slice(0, 4)
            : [],
          nextAction: String(step.workflowSummary?.next_action || '').trim() || undefined,
          blockedReason: String(step.workflowSummary?.decision_state?.blocked_reason || '').trim() || undefined,
          evidenceRefs: Array.isArray(step.workflowSummary?.evidence_refs)
            ? step.workflowSummary?.evidence_refs.filter(Boolean).slice(0, 6)
            : [],
          rawContent: String(step.rawContent || '').trim() || undefined,
        }))

      const toolAttempts: ToolAttempt[] = []
      const pendingAttempts: ToolAttempt[] = []

      const findPendingAttemptIndex = (toolCallId?: string, toolName?: string): number => {
        if (toolCallId) {
          const byId = pendingAttempts.findIndex((attempt) => attempt.toolCallId === toolCallId)
          if (byId >= 0) return byId
        }
        if (toolName) {
          const byTool = pendingAttempts.findIndex((attempt) => attempt.tool === toolName)
          if (byTool >= 0) return byTool
        }
        return pendingAttempts.length ? 0 : -1
      }

      iterationSteps.forEach((step, index) => {
        if (step.type === 'action') {
          pendingAttempts.push({
            key: `action-${iteration}-${step.tool || 'unknown'}-${index}`,
            tool: step.tool,
            toolCallId: step.toolCallId,
            input: step.input,
            pending: true,
          })
          return
        }

        if (step.type === 'observation') {
          const observation: ToolAttempt = {
            key: `observation-${iteration}-${step.tool || 'unknown'}-${index}`,
            tool: step.tool,
            toolCallId: step.toolCallId,
            output: step.output || step.content,
            success: step.success,
            pending: false,
          }

          const pendingAttemptIndex = findPendingAttemptIndex(observation.toolCallId, observation.tool)
          if (pendingAttemptIndex >= 0) {
            const pendingAttempt = pendingAttempts.splice(pendingAttemptIndex, 1)[0]
            toolAttempts.push({
              ...pendingAttempt,
              output: observation.output,
              success: observation.success,
              pending: false,
            })
            return
          }

          toolAttempts.push(observation)
        }
      })

      if (pendingAttempts.length) {
        toolAttempts.push(...pendingAttempts)
      }

      return {
        iteration,
        processTexts,
        toolAttempts,
        workflowSteps,
      }
    })
}

const SlotCard = ({
  label,
  tone,
  icon,
  collapsible = false,
  defaultCollapsed = false,
  collapsedSummary,
  children,
}: {
  label: string
  tone: 'process' | 'tool' | 'result' | 'state'
  icon: ReactNode
  collapsible?: boolean
  defaultCollapsed?: boolean
  collapsedSummary?: string
  children: ReactNode
}) => {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  const toneClass =
    tone === 'process'
      ? 'border-amber-400/14 bg-[linear-gradient(180deg,rgba(120,53,15,0.14),rgba(2,6,23,0.72))]'
      : tone === 'tool'
        ? 'border-sky-400/14 bg-[linear-gradient(180deg,rgba(12,74,110,0.14),rgba(2,6,23,0.72))]'
        : tone === 'result'
          ? 'border-emerald-400/14 bg-[linear-gradient(180deg,rgba(6,78,59,0.14),rgba(2,6,23,0.72))]'
          : 'border-cyan-400/14 bg-[linear-gradient(180deg,rgba(8,47,73,0.16),rgba(2,6,23,0.72))]'

  return (
    <div className={`rounded-3xl border p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)] ${toneClass}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-slate-300">
          {icon}
          {label}
        </div>
        {collapsible ? (
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] font-medium text-slate-300 transition hover:border-white/[0.14] hover:text-slate-100"
          >
            {collapsed ? <ExpandOutlined /> : <CompressOutlined />}
            {collapsed ? '展开' : '收起'}
          </button>
        ) : null}
      </div>
      {collapsed ? (
        <div className="mt-3 rounded-2xl border border-dashed border-white/[0.08] bg-slate-950/45 px-3 py-2.5 text-sm leading-6 text-slate-500">
          {collapsedSummary || '这一栏已折叠。'}
        </div>
      ) : (
        <div className="mt-3 space-y-3">{children}</div>
      )}
    </div>
  )
}

const EmptySlot = ({ text }: { text: string }) => (
  <div className="rounded-2xl border border-dashed border-white/[0.08] bg-slate-950/45 px-3 py-2.5 text-sm leading-6 text-slate-500">
    {text}
  </div>
)

const AttemptBlockCard = ({
  block,
  active = false,
}: {
  block: AttemptBlock
  active?: boolean
}) => (
  <div className="rounded-[28px] border border-white/[0.05] bg-[linear-gradient(180deg,rgba(15,23,42,0.82),rgba(2,6,23,0.72))] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03),0_14px_30px_rgba(2,6,23,0.18)]">
    <div className="mb-4 flex flex-wrap items-center gap-2">
      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] uppercase tracking-[0.14em] text-slate-300">
        第 {block.iteration} 轮
      </span>
      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
        Process {block.processTexts.length}
      </span>
      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
        Tool {block.toolAttempts.length}
      </span>
      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
        State {block.workflowSteps.length}
      </span>
      {active ? (
        <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-[11px] text-emerald-100">
          当前进行中
        </span>
      ) : null}
    </div>

    <div className="grid gap-4 xl:grid-cols-2">
      <SlotCard label="Process" tone="process" icon={<BulbOutlined className="text-amber-300" />}>
        {block.processTexts.length ? (
          block.processTexts.map((text, index) => (
            <ExpandableTextBlock
              key={`process-${block.iteration}-${index}`}
              text={text}
              expandLabel="展开过程原文"
              collapseLabel="收起过程原文"
            />
          ))
        ) : (
          <EmptySlot text="这一轮没有额外过程文本。" />
        )}
      </SlotCard>

      <SlotCard label="Tool" tone="tool" icon={<ToolOutlined className="text-sky-300" />}>
        {block.toolAttempts.length ? (
          block.toolAttempts.map((attempt) => {
            const toolLabel = toolNames[attempt.tool || ''] || attempt.tool || '工具调用'
            return (
              <div key={attempt.key} className="rounded-2xl border border-white/[0.06] bg-slate-950/55 px-3 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sky-300">{toolIcons[attempt.tool || ''] || <ToolOutlined />}</span>
                  <span className="text-sm font-medium text-slate-100">{toolLabel}</span>
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] ${toolStatusClass(attempt)}`}>
                    {attempt.pending ? '执行中' : attempt.success === false ? '失败' : '已执行'}
                  </span>
                  {attempt.pending ? <LoadingOutlined className="text-xs text-amber-300" /> : null}
                </div>
                {attempt.input && Object.keys(attempt.input).length ? (
                  <div className="mt-3">
                    <div className="mb-2 text-[11px] uppercase tracking-[0.14em] text-slate-500">参数</div>
                    <ExpandableTextBlock
                      text={JSON.stringify(attempt.input, null, 2)}
                      monospace
                      expandLabel="展开工具参数"
                      collapseLabel="收起工具参数"
                    />
                  </div>
                ) : null}
              </div>
            )
          })
        ) : (
          <EmptySlot text="这一轮没有工具调用。" />
        )}
      </SlotCard>

      <SlotCard label="Result" tone="result" icon={<CheckCircleOutlined className="text-emerald-300" />}>
        {block.toolAttempts.length ? (
          block.toolAttempts.map((attempt) => (
            <div key={`result-${attempt.key}`} className="rounded-2xl border border-white/[0.06] bg-slate-950/55 px-3 py-3">
              <div className="flex flex-wrap items-center gap-2">
                {attempt.pending ? (
                  <LoadingOutlined className="text-xs text-amber-300" />
                ) : attempt.success === false ? (
                  <CloseCircleOutlined className="text-xs text-red-300" />
                ) : (
                  <CheckCircleOutlined className="text-xs text-emerald-300" />
                )}
                <span className="text-sm font-medium text-slate-100">
                  {toolNames[attempt.tool || ''] || attempt.tool || '工具结果'}
                </span>
                <span className={`rounded-full border px-2 py-0.5 text-[11px] ${toolStatusClass(attempt)}`}>
                  {attempt.pending ? '等待返回' : attempt.success === false ? '失败' : '成功'}
                </span>
              </div>
              {attempt.output ? (
                <div className="mt-3">
                  <ExpandableTextBlock
                    text={attempt.output}
                    variant="evidence"
                    expandLabel="查看完整原文"
                    collapseLabel="收起原文"
                  />
                </div>
              ) : (
                <div className="mt-3 rounded-2xl border border-dashed border-white/[0.08] bg-slate-950/45 px-3 py-2.5 text-sm leading-6 text-slate-500">
                  {attempt.pending ? '工具已发出，正在等待 observation 返回。' : '这一轮没有记录到结果文本。'}
                </div>
              )}
            </div>
          ))
        ) : (
          <EmptySlot text="没有工具结果可展示。" />
        )}
      </SlotCard>

      <SlotCard
        label="状态变化"
        tone="state"
        icon={<BranchesOutlined className="text-cyan-300" />}
        collapsible
        defaultCollapsed
        collapsedSummary={
          block.workflowSteps.length
            ? `收纳 ${block.workflowSteps.length} 条流程状态、下一步和阻塞信息。`
            : '这一轮没有额外状态变化。'
        }
      >
        {block.workflowSteps.length ? (
          block.workflowSteps.map((workflow, index) => (
            <div key={`state-${block.iteration}-${index}`} className="rounded-2xl border border-white/[0.06] bg-slate-950/55 px-3 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-slate-100">{workflow.headline}</span>
                {workflow.status ? (
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] ${workflowStatusClass(workflow.status)}`}>
                    {workflow.status}
                  </span>
                ) : null}
              </div>
              {workflow.highlights.length ? (
                <div className="mt-3 space-y-1.5 text-sm leading-6 text-slate-300">
                  {workflow.highlights.map((item) => (
                    <div key={item}>{item}</div>
                  ))}
                </div>
              ) : null}
              {workflow.nextAction ? (
                <div className="mt-3 rounded-2xl border border-emerald-400/14 bg-emerald-500/8 px-3 py-2 text-xs leading-6 text-emerald-100">
                  下一步：{workflow.nextAction}
                </div>
              ) : null}
              {workflow.blockedReason ? (
                <div className="mt-2 text-xs leading-6 text-red-200">阻塞原因：{workflow.blockedReason}</div>
              ) : null}
              {workflow.evidenceRefs.length ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {workflow.evidenceRefs.map((item) => (
                    <span
                      key={item}
                      className="rounded-full border border-white/[0.08] bg-slate-900/70 px-2 py-0.5 text-[11px] text-slate-300"
                    >
                      {item}
                    </span>
                  ))}
                </div>
              ) : null}
              {workflow.rawContent && workflow.rawContent !== workflow.headline ? (
                <div className="mt-3 border-t border-white/[0.06] pt-3">
                  <ExpandableTextBlock
                    text={workflow.rawContent}
                    variant="state"
                    expandLabel="查看细节"
                    collapseLabel="收起细节"
                  />
                </div>
              ) : null}
            </div>
          ))
        ) : (
          <EmptySlot text="这一轮没有额外状态变化。" />
        )}
      </SlotCard>
    </div>
  </div>
)

const TurnProcessLanes = ({
  steps,
  title,
  subtitle,
  statusLabel,
  active = false,
  defaultExpanded = true,
  embedded = false,
}: TurnProcessLanesProps) => {
  const [expanded, setExpanded] = useState(defaultExpanded)
  void embedded

  const blocks = useMemo(() => buildAttemptBlocks(steps), [steps])
  const iterationCount = blocks.length
  const processCount = blocks.reduce((sum, block) => sum + block.processTexts.length, 0)
  const toolCount = blocks.reduce((sum, block) => sum + block.toolAttempts.length, 0)

  if (!steps.length && !active) return null

  return (
    <motion.div initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} className={embedded ? '' : 'mb-3'}>
      <div className="overflow-hidden rounded-[26px] border border-white/[0.05] bg-[linear-gradient(180deg,rgba(15,23,42,0.78),rgba(2,6,23,0.76))] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div
          className="flex cursor-pointer flex-wrap items-center justify-between gap-3 px-4 py-3 text-slate-400 transition-colors hover:text-slate-200"
          onClick={() => setExpanded((value) => !value)}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">{title}</span>
              {statusLabel ? (
                <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-100">
                  {statusLabel}
                </span>
              ) : null}
            </div>
            <div className="mt-1 text-sm text-slate-300">{subtitle}</div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {iterationCount ? (
              <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
                {iterationCount} 轮
              </span>
            ) : null}
            <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
              Process {processCount}
            </span>
            <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
              Tool {toolCount}
            </span>
            <Button
              type="text"
              size="small"
              icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
              className="text-slate-500 hover:text-slate-200"
            />
          </div>
        </div>

        <AnimatePresence initial={false}>
          {expanded ? (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
              className="overflow-hidden"
            >
              <div className="border-t border-white/[0.05] px-4 py-4">
                <div className="space-y-4">
                  {blocks.length ? (
                    blocks.map((block, index) => (
                      <AttemptBlockCard
                        key={`attempt-${block.iteration}-${index}`}
                        block={block}
                        active={active && index === blocks.length - 1}
                      />
                    ))
                  ) : (
                    <div className="rounded-[28px] border border-dashed border-white/[0.08] bg-slate-950/55 px-4 py-4 text-sm leading-6 text-slate-500">
                      本轮还没有收集到过程或工具事件。
                    </div>
                  )}
                </div>
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default TurnProcessLanes
