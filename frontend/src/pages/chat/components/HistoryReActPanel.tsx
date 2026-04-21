import { useMemo, useState } from 'react'
import { Button } from 'antd'
import {
  BranchesOutlined,
  BulbOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CompressOutlined,
  ExpandOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import type { ConversationDecisionState, ToolWorkflowSummary } from '@/services/api'
import { toolIcons, toolNames } from '../constants'

interface HistoryStep {
  type: string
  iteration: number
  content?: string
  tool?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  workflowSummary?: ToolWorkflowSummary
  rawContent?: string
}

interface HistoryReActPanelProps {
  steps: HistoryStep[]
  defaultExpanded?: boolean
  embedded?: boolean
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

const statusChipClass = (status: string | undefined, embedded: boolean): string => {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'blocked') {
    return embedded
      ? 'border-red-400/18 bg-red-500/10 text-red-200'
      : 'border-red-400/20 bg-red-500/10 text-red-100'
  }
  if (normalized === 'waiting') {
    return embedded
      ? 'border-amber-400/18 bg-amber-500/10 text-amber-200'
      : 'border-amber-400/20 bg-amber-500/10 text-amber-100'
  }
  if (normalized === 'ready' || normalized === 'progressed') {
    return embedded
      ? 'border-emerald-400/18 bg-emerald-500/10 text-emerald-200'
      : 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100'
  }
  return embedded
    ? 'border-cyan-400/18 bg-cyan-500/10 text-cyan-200'
    : 'border-cyan-400/20 bg-cyan-500/10 text-cyan-100'
}

const decisionToneClass = (decisionState: ConversationDecisionState | undefined, embedded: boolean): string => {
  const status = String(decisionState?.status || '').trim().toLowerCase()
  if (status === 'blocked') {
    return embedded
      ? 'border-red-400/18 bg-red-500/10 text-red-200'
      : 'border-red-400/20 bg-red-500/10 text-red-100'
  }
  if (status === 'ready') {
    return embedded
      ? 'border-emerald-400/18 bg-emerald-500/10 text-emerald-200'
      : 'border-emerald-400/20 bg-emerald-500/10 text-emerald-100'
  }
  if (status === 'waiting') {
    return embedded
      ? 'border-amber-400/18 bg-amber-500/10 text-amber-200'
      : 'border-amber-400/20 bg-amber-500/10 text-amber-100'
  }
  return embedded
    ? 'border-white/[0.08] bg-slate-950/70 text-slate-300'
    : 'border-white/[0.08] bg-slate-900/70 text-slate-200'
}

const ExpandableTextBlock = ({
  text,
  embedded,
  monospace = false,
  expandLabel = '展开原文',
  collapseLabel = '收起原文',
}: {
  text?: string
  embedded: boolean
  monospace?: boolean
  expandLabel?: string
  collapseLabel?: string
}) => {
  const normalized = String(text || '').trim()
  const [expanded, setExpanded] = useState(false)

  if (!normalized) return null

  const collapsible = shouldCollapseText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)
  const preview = clipMultilineText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)
  const bodyClass = monospace
    ? `overflow-x-auto rounded-xl border px-2.5 py-2 text-[11px] leading-5 ${
        embedded
          ? 'border-white/[0.06] bg-slate-950/70 text-slate-400'
          : 'border-white/[0.06] bg-slate-950/80 text-slate-300'
      }`
    : `rounded-xl border px-2.5 py-2 text-xs leading-6 ${
        embedded
          ? 'border-white/[0.06] bg-slate-950/55 text-slate-400'
          : 'border-white/[0.06] bg-slate-950/70 text-slate-300'
      }`

  return (
    <div className="space-y-2">
      {monospace ? (
        <pre className={bodyClass}>{collapsible && !expanded ? preview : normalized}</pre>
      ) : (
        <div className={bodyClass}>
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

const WorkflowSummaryCard = ({
  iteration,
  summary,
  rawContent,
  embedded,
}: {
  iteration: number
  summary?: ToolWorkflowSummary
  rawContent?: string
  embedded: boolean
}) => {
  const headline = String(summary?.headline || '流程状态已更新').trim()
  const status = String(summary?.status || 'observed').trim()
  const highlights = Array.isArray(summary?.highlights) ? summary?.highlights.filter(Boolean).slice(0, 4) : []
  const evidenceRefs = Array.isArray(summary?.evidence_refs)
    ? summary?.evidence_refs.filter(Boolean).slice(0, 6)
    : []
  const nextAction = String(summary?.next_action || '').trim()
  const decisionState = summary?.decision_state

  return (
    <div className="relative">
      <div className="absolute -left-5 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-800 bg-cyan-400" />
      <div
        className={`rounded-xl border p-3 ${
          embedded ? 'border-white/[0.06] bg-slate-950/55' : 'border-cyan-500/18 bg-slate-950/72'
        }`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <BranchesOutlined className="text-cyan-300" />
          <span className="text-xs font-semibold text-cyan-200">流程卡 · 第 {Math.max(iteration, 1)} 轮</span>
          <span className={`rounded-full border px-2 py-0.5 text-[11px] ${statusChipClass(status, embedded)}`}>
            {status}
          </span>
          {decisionState?.status ? (
            <span
              className={`rounded-full border px-2 py-0.5 text-[11px] ${decisionToneClass(decisionState, embedded)}`}
            >
              决策 {decisionState.status}
            </span>
          ) : null}
        </div>
        <div className={`mt-2 text-sm font-medium ${embedded ? 'text-slate-100' : 'text-white'}`}>{headline}</div>
        {highlights.length ? (
          <div className="mt-2 space-y-1.5">
            {highlights.map((item) => (
              <div key={item} className={`text-xs leading-6 ${embedded ? 'text-slate-300' : 'text-slate-200'}`}>
                {item}
              </div>
            ))}
          </div>
        ) : null}
        {nextAction ? (
          <div className="mt-2 rounded-xl border border-emerald-400/14 bg-emerald-500/8 px-3 py-2 text-xs leading-6 text-emerald-100">
            下一步：{nextAction}
          </div>
        ) : null}
        {decisionState?.blocked_reason ? (
          <div className="mt-2 text-xs leading-6 text-red-200">阻塞原因：{decisionState.blocked_reason}</div>
        ) : null}
        {evidenceRefs.length ? (
          <div className="mt-2 flex flex-wrap gap-2">
            {evidenceRefs.map((item) => (
              <span
                key={item}
                className={`rounded-full border px-2 py-0.5 text-[11px] ${
                  embedded
                    ? 'border-white/[0.08] bg-slate-950/70 text-slate-400'
                    : 'border-white/[0.08] bg-slate-900/70 text-slate-300'
                }`}
              >
                {item}
              </span>
            ))}
          </div>
        ) : null}
        {rawContent ? (
          <div className="mt-3 border-t border-white/[0.06] pt-3">
            <ExpandableTextBlock
              text={rawContent}
              embedded={embedded}
              expandLabel="展开流程原文"
              collapseLabel="收起流程原文"
            />
          </div>
        ) : null}
      </div>
    </div>
  )
}

const HistoryReActPanel = ({
  steps,
  defaultExpanded = false,
  embedded = false,
}: HistoryReActPanelProps) => {
  const [expanded, setExpanded] = useState(defaultExpanded)

  const metrics = useMemo(() => {
    const normalizedSteps = Array.isArray(steps) ? steps : []
    return {
      totalIterations: normalizedSteps.length
        ? Math.max(...normalizedSteps.map((step) => step.iteration || 1))
        : 0,
      toolCalls: normalizedSteps.filter((step) => step.type === 'action').length,
      workflowCards: normalizedSteps.filter((step) => step.type === 'workflow').length,
    }
  }, [steps])

  if (!steps || steps.length === 0) return null

  const shellClass = embedded
    ? 'relative overflow-hidden rounded-lg border border-white/[0.04] bg-white/[0.02] shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]'
    : 'relative overflow-hidden rounded-xl border border-cyan-500/20 bg-slate-900/70 backdrop-blur-sm'
  const contentBorderClass = embedded ? 'border-white/[0.08]' : 'border-slate-700/60'

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className={embedded ? '' : 'mb-3'}>
      <div className={shellClass}>
        <div
          className={`relative z-10 flex cursor-pointer items-center justify-between transition-colors ${
            embedded ? 'gap-3 px-3 py-2 hover:bg-white/[0.04]' : 'px-4 py-2.5 hover:bg-white/5'
          }`}
          onClick={() => setExpanded((value) => !value)}
        >
          <div className="flex items-center gap-3">
            <div
              className={
                embedded
                  ? 'text-emerald-300'
                  : 'flex h-7 w-7 items-center justify-center rounded-lg border border-cyan-300/30 bg-gradient-to-br from-cyan-500/80 to-blue-500/80 shadow-sm shadow-cyan-500/20'
              }
            >
              <BulbOutlined className={`text-[11px] ${embedded ? '' : 'text-white'}`} />
            </div>
            <div className="flex items-center gap-2">
              <span className={`font-medium ${embedded ? 'text-xs tracking-wide text-slate-400' : 'text-sm text-slate-100'}`}>
                推理过程
              </span>
              <span
                className={`rounded-full border px-2 py-0.5 text-[11px] ${
                  embedded
                    ? 'border-white/[0.06] bg-transparent text-slate-500'
                    : 'border-cyan-500/20 bg-cyan-500/10 text-cyan-200/80'
                }`}
              >
                {metrics.totalIterations} 轮迭代
              </span>
              <span
                className={`rounded-full border px-2 py-0.5 text-[11px] ${
                  embedded
                    ? 'border-white/[0.06] bg-transparent text-slate-500'
                    : 'border-slate-600/60 bg-slate-700/40 text-slate-300/80'
                }`}
              >
                {metrics.toolCalls} 次工具调用
              </span>
              {metrics.workflowCards ? (
                <span
                  className={`rounded-full border px-2 py-0.5 text-[11px] ${
                    embedded
                      ? 'border-cyan-400/18 bg-cyan-500/10 text-cyan-300'
                      : 'border-cyan-400/18 bg-cyan-500/10 text-cyan-100'
                  }`}
                >
                  {metrics.workflowCards} 张流程卡
                </span>
              ) : null}
            </div>
          </div>
          <Button
            type="text"
            size="small"
            icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
            className="text-slate-300 hover:text-white"
          />
        </div>

        <AnimatePresence>
          {expanded ? (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div
                className={`relative z-10 max-h-80 space-y-3 overflow-y-auto ${
                  embedded
                    ? 'mx-3 mb-3 border-l border-white/[0.08] pl-4 pt-2'
                    : `border-t px-4 py-3 ${contentBorderClass}`
                }`}
              >
                {steps.map((step, index) => (
                  <div key={`${step.type}-${step.iteration}-${index}`} className="relative pl-5">
                    <div className={`absolute bottom-0 left-0 top-0 w-px ${embedded ? 'bg-slate-800' : 'bg-slate-700/80'}`} />

                    {step.type === 'workflow' ? (
                      <WorkflowSummaryCard
                        iteration={step.iteration}
                        summary={step.workflowSummary}
                        rawContent={step.rawContent || step.content}
                        embedded={embedded}
                      />
                    ) : null}

                    {step.type === 'thought' ? (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-800 bg-amber-500" />
                        <div
                          className={`rounded-lg p-2.5 backdrop-blur-sm ${
                            embedded
                              ? 'border border-white/[0.04] bg-slate-950/40'
                              : 'border border-amber-500/18 bg-slate-950/70'
                          }`}
                        >
                          <div className="mb-1.5 flex items-center gap-2">
                            <BulbOutlined className="text-xs text-amber-400" />
                            <span className="text-xs font-medium text-amber-400">第 {step.iteration} 轮过程</span>
                          </div>
                          <ExpandableTextBlock
                            text={step.content}
                            embedded={embedded}
                            expandLabel="展开过程原文"
                            collapseLabel="收起过程原文"
                          />
                        </div>
                      </div>
                    ) : null}

                    {step.type === 'action' ? (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-800 bg-blue-500" />
                        <div
                          className={`rounded-lg p-2.5 backdrop-blur-sm ${
                            embedded ? 'border border-white/[0.04] bg-slate-950/40' : 'border border-sky-500/18 bg-slate-950/70'
                          }`}
                        >
                          <div className="mb-1.5 flex items-center gap-2">
                            <span className="text-xs text-blue-400">{toolIcons[step.tool || ''] || <ToolOutlined />}</span>
                            <span className="text-xs font-medium text-blue-400">
                              调用 {toolNames[step.tool || ''] || step.tool}
                            </span>
                          </div>
                          <ExpandableTextBlock
                            text={JSON.stringify(step.input || {}, null, 2)}
                            embedded={embedded}
                            monospace
                            expandLabel="展开工具参数"
                            collapseLabel="收起工具参数"
                          />
                        </div>
                      </div>
                    ) : null}

                    {step.type === 'observation' ? (
                      <div className="relative">
                        <div
                          className={`absolute -left-5 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-slate-800 ${
                            step.success ? 'bg-emerald-500' : 'bg-red-500'
                          }`}
                        />
                        <div
                          className={`rounded-lg border p-2.5 ${
                            embedded
                              ? 'border-white/[0.04] bg-slate-950/40'
                              : step.success
                                ? 'border-emerald-500/18 bg-slate-950/70'
                                : 'border-red-500/18 bg-slate-950/70'
                          }`}
                        >
                          <div className="mb-1.5 flex items-center gap-2">
                            {step.success ? (
                              <CheckCircleOutlined className="text-xs text-emerald-400" />
                            ) : (
                              <CloseCircleOutlined className="text-xs text-red-400" />
                            )}
                            <span className={`text-xs font-medium ${step.success ? 'text-emerald-400' : 'text-red-400'}`}>
                              工具证据
                            </span>
                          </div>
                          <ExpandableTextBlock
                            text={step.output || step.content}
                            embedded={embedded}
                            expandLabel="展开证据原文"
                            collapseLabel="收起证据原文"
                          />
                        </div>
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default HistoryReActPanel
