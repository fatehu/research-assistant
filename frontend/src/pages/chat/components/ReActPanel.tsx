import { useState } from 'react'
import { Button } from 'antd'
import {
  BulbOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CompressOutlined,
  ExpandOutlined,
  LoadingOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import type { IterationStep } from '@/stores/chatStore'
import { toolIcons, toolNames } from '../constants'

interface ReActPanelProps {
  steps: IterationStep[]
  currentIteration: number
  isThinking: boolean
  currentThought: string
  currentToolCall: { tool: string; input: Record<string, any> } | null
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
  expandLabel = '展开原文',
  collapseLabel = '收起原文',
}: {
  text?: string
  monospace?: boolean
  expandLabel?: string
  collapseLabel?: string
}) => {
  const normalized = String(text || '').trim()
  const [expanded, setExpanded] = useState(false)

  if (!normalized) return null

  const collapsible = shouldCollapseText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)
  const preview = clipMultilineText(normalized, monospace ? 420 : 320, monospace ? 8 : 6)

  return (
    <div className="space-y-2">
      {monospace ? (
        <pre className="overflow-x-auto rounded-xl border border-white/[0.06] bg-slate-900/70 px-2.5 py-2 text-[11px] leading-5 text-slate-400">
          {collapsible && !expanded ? preview : normalized}
        </pre>
      ) : (
        <div className="rounded-xl border border-white/[0.06] bg-slate-950/55 px-2.5 py-2 text-sm leading-6 text-slate-400">
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

const ReActPanel = ({
  steps,
  currentIteration,
  isThinking,
  currentThought,
  currentToolCall,
}: ReActPanelProps) => {
  const [expanded, setExpanded] = useState(true)

  if (steps.length === 0 && !isThinking && !currentToolCall) return null

  const iterations: IterationStep[][] = []
  let currentGroup: IterationStep[] = []

  steps.forEach((step) => {
    currentGroup.push(step)
    if (step.type === 'observation') {
      if (currentGroup.length > 0) {
        iterations.push([...currentGroup])
        currentGroup = []
      }
    }
  })

  if (currentGroup.length > 0) {
    iterations.push(currentGroup)
  }

  return (
    <motion.div initial={{ opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} className="mb-3">
      <div className="overflow-hidden rounded-2xl border border-white/[0.04] bg-[#13151A] px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div
          className="flex cursor-pointer items-center justify-between gap-3 py-1 text-slate-400 transition-colors hover:text-slate-200"
          onClick={() => setExpanded((value) => !value)}
        >
          <div className="flex items-center gap-3">
            <div>
              <div className="flex items-center gap-2">
                <BulbOutlined className="text-emerald-300" />
                <span className="text-xs font-medium uppercase tracking-[0.16em] text-slate-400">推理过程</span>
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {currentIteration > 0 ? `第 ${currentIteration} 轮推理` : '准备中'}
                {isThinking || currentToolCall ? (
                  <span className="ml-2 text-emerald-300">
                    <LoadingOutlined className="mr-1 animate-spin" />
                    {currentToolCall ? '执行工具中' : '思考中'}
                  </span>
                ) : null}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {steps.length > 0 ? (
              <span className="rounded-full border border-white/[0.06] px-2 py-0.5 text-[11px] text-slate-500">
                {steps.length} 步
              </span>
            ) : null}
            <Button
              type="text"
              size="small"
              icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
              className="text-slate-500 hover:text-slate-200"
            />
          </div>
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
              <div className="mt-2 max-h-96 space-y-4 overflow-y-auto border-l border-white/[0.08] pl-4 pt-2 scrollbar-thin scrollbar-thumb-slate-700">
                {iterations.map((iterSteps, iterIndex) => (
                  <div key={iterIndex}>
                    {iterIndex > 0 ? (
                      <div className="my-2 flex items-center gap-3 py-2">
                        <div className="h-px flex-1 bg-gradient-to-r from-transparent via-slate-700 to-transparent" />
                        <span className="px-2 text-xs text-slate-500">第 {iterIndex + 1} 轮推理</span>
                        <div className="h-px flex-1 bg-gradient-to-r from-transparent via-slate-700 to-transparent" />
                      </div>
                    ) : null}

                    <div className="space-y-3">
                      {iterSteps.map((step) => (
                        <motion.div
                          key={step.timestamp}
                          initial={{ opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          className="relative pl-6"
                        >
                          <div className="absolute bottom-0 left-0 top-0 w-px bg-slate-700" />

                          {step.type === 'thought' ? (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 h-3 w-3 rounded-full border-2 border-slate-800 bg-amber-500" />
                              <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                                <div className="mb-2 flex items-center gap-2">
                                  <BulbOutlined className="text-amber-400" />
                                  <span className="text-xs font-medium text-amber-400">过程摘要</span>
                                </div>
                                <ExpandableTextBlock
                                  text={step.content}
                                  expandLabel="展开过程原文"
                                  collapseLabel="收起过程原文"
                                />
                              </div>
                            </div>
                          ) : null}

                          {step.type === 'action' ? (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 h-3 w-3 rounded-full border-2 border-slate-800 bg-blue-500" />
                              <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                                <div className="mb-2 flex items-center gap-2">
                                  <span className="text-blue-400">{toolIcons[step.tool || ''] || <ToolOutlined />}</span>
                                  <span className="text-xs font-medium text-blue-400">
                                    调用 {toolNames[step.tool || ''] || step.tool}
                                  </span>
                                </div>
                                <ExpandableTextBlock
                                  text={JSON.stringify(step.toolInput || {}, null, 2)}
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
                                className={`absolute -left-6 top-1 h-3 w-3 rounded-full border-2 border-slate-800 ${
                                  step.success ? 'bg-emerald-500' : 'bg-red-500'
                                }`}
                              />
                              <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                                <div className="mb-2 flex items-center gap-2">
                                  {step.success ? (
                                    <CheckCircleOutlined className="text-emerald-400" />
                                  ) : (
                                    <CloseCircleOutlined className="text-red-400" />
                                  )}
                                  <span className={`text-xs font-medium ${step.success ? 'text-emerald-400' : 'text-red-400'}`}>
                                    工具证据
                                  </span>
                                </div>
                                <ExpandableTextBlock
                                  text={step.toolOutput || step.content}
                                  expandLabel="展开证据原文"
                                  collapseLabel="收起证据原文"
                                />
                              </div>
                            </div>
                          ) : null}
                        </motion.div>
                      ))}
                    </div>
                  </div>
                ))}

                {isThinking ? (
                  <motion.div
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute bottom-0 left-0 top-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 h-3 w-3 animate-pulse rounded-full border-2 border-slate-800 bg-amber-500" />
                    <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                      <div className="mb-2 flex items-center gap-2">
                        <BulbOutlined className="animate-pulse text-amber-400" />
                        <span className="text-xs font-medium text-amber-400">处理中...</span>
                      </div>
                      <ExpandableTextBlock
                        text={currentThought || '正在分析问题并规划步骤...'}
                        expandLabel="展开过程原文"
                        collapseLabel="收起过程原文"
                      />
                    </div>
                  </motion.div>
                ) : null}

                {currentToolCall ? (
                  <motion.div
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute bottom-0 left-0 top-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 h-3 w-3 animate-pulse rounded-full border-2 border-slate-800 bg-blue-500" />
                    <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                      <div className="mb-2 flex items-center gap-2">
                        <span className="animate-pulse text-blue-400">
                          {toolIcons[currentToolCall.tool] || <ToolOutlined />}
                        </span>
                        <span className="text-xs font-medium text-blue-400">
                          正在执行 {toolNames[currentToolCall.tool] || currentToolCall.tool}...
                        </span>
                        <LoadingOutlined className="animate-spin text-blue-400" />
                      </div>
                      <ExpandableTextBlock
                        text={JSON.stringify(currentToolCall.input || {}, null, 2)}
                        monospace
                        expandLabel="展开工具参数"
                        collapseLabel="收起工具参数"
                      />
                    </div>
                  </motion.div>
                ) : null}
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default ReActPanel
