import { useState } from 'react'
import { Button } from 'antd'
import {
  BulbOutlined,
  LoadingOutlined,
  ExpandOutlined,
  CompressOutlined,
  ToolOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import type { IterationStep } from '@/stores/chatStore'
import { toolIcons, toolNames } from '../constants'

interface ReActPanelProps {
  steps: IterationStep[]
  currentIteration: number
  isThinking: boolean
  currentThought: string
  currentToolCall: { tool: string; input: Record<string, any> } | null
}

/** ReAct 推理过程面板 - 实时展示 */
const ReActPanel = ({
  steps,
  currentIteration,
  isThinking,
  currentThought,
  currentToolCall,
}: ReActPanelProps) => {
  const [expanded, setExpanded] = useState(true)

  // 如果没有任何内容，不显示
  if (steps.length === 0 && !isThinking && !currentToolCall) return null

  // 按迭代分组步骤 - 使用 observation 作为每轮结束的标志
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

  // 如果还有未完成的步骤（正在进行的轮次）
  if (currentGroup.length > 0) {
    iterations.push(currentGroup)
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: -5 }}
      animate={{ opacity: 1, y: 0 }}
      className="mb-3"
    >
      <div className="overflow-hidden rounded-2xl border border-white/[0.04] bg-[#13151A] px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
        <div
          className="flex cursor-pointer items-center justify-between gap-3 py-1 text-slate-400 transition-colors hover:text-slate-200"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div>
              <div className="flex items-center gap-2">
                <BulbOutlined className="text-emerald-300" />
                <span className="text-xs font-medium uppercase tracking-[0.16em] text-slate-400">推理过程</span>
              </div>
              <div className="mt-1 text-xs text-slate-500">
                {currentIteration > 0 ? `第 ${currentIteration} 轮推理` : '准备中'}
                {(isThinking || currentToolCall) && (
                  <span className="ml-2 text-emerald-300">
                    <LoadingOutlined className="animate-spin mr-1" />
                    {currentToolCall ? '执行工具中' : '思考中'}
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {steps.length > 0 && (
              <span className="rounded-full border border-white/[0.06] px-2 py-0.5 text-[11px] text-slate-500">
                {steps.length} 步
              </span>
            )}
            <Button
              type="text"
              size="small"
              icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
              className="text-slate-500 hover:text-slate-200"
            />
          </div>
        </div>

        {/* 内容 */}
        <AnimatePresence>
          {expanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="mt-2 max-h-96 space-y-4 overflow-y-auto border-l border-white/[0.08] pl-4 pt-2 scrollbar-thin scrollbar-thumb-slate-700">
                {/* 显示所有迭代 */}
                {iterations.map((iterSteps, iterIndex) => (
                  <div key={iterIndex}>
                    {/* 迭代分隔线 */}
                    {iterIndex > 0 && (
                      <div className="flex items-center gap-3 py-2 my-2">
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-700 to-transparent" />
                        <span className="text-xs text-slate-500 px-2">第 {iterIndex + 1} 轮推理</span>
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-700 to-transparent" />
                      </div>
                    )}

                    <div className="space-y-3">
                      {iterSteps.map((step) => (
                        <motion.div
                          key={step.timestamp}
                          initial={{ opacity: 0, x: -10 }}
                          animate={{ opacity: 1, x: 0 }}
                          className="relative pl-6"
                        >
                          {/* 时间线 */}
                          <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />

                          {step.type === 'thought' && (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-amber-500 border-2 border-slate-800" />
                              <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                                <div className="flex items-center gap-2 mb-2">
                                  <BulbOutlined className="text-amber-400" />
                                  <span className="text-xs font-medium text-amber-400">思考</span>
                                </div>
                                <p className="text-sm leading-relaxed text-slate-400">{step.content}</p>
                              </div>
                            </div>
                          )}

                          {step.type === 'action' && (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-blue-500 border-2 border-slate-800" />
                              <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                                <div className="flex items-center gap-2 mb-2">
                                  <span className="text-blue-400">
                                    {toolIcons[step.tool || ''] || <ToolOutlined />}
                                  </span>
                                  <span className="text-xs font-medium text-blue-400">
                                    调用 {toolNames[step.tool || ''] || step.tool}
                                  </span>
                                </div>
                                <code className="block overflow-x-auto rounded bg-slate-900/70 px-2 py-1 text-xs text-slate-400">
                                  {JSON.stringify(step.toolInput, null, 2)}
                                </code>
                              </div>
                            </div>
                          )}

                          {step.type === 'observation' && (
                            <div className="relative">
                              <div
                                className={`absolute -left-6 top-1 w-3 h-3 rounded-full border-2 border-slate-800 ${
                                  step.success ? 'bg-emerald-500' : 'bg-red-500'
                                }`}
                              />
                              <div
                                className={`rounded-lg p-3 border ${
                                  step.success
                                  ? 'bg-slate-950/40 border-white/[0.04]'
                                  : 'bg-slate-950/40 border-white/[0.04]'
                                }`}
                              >
                                <div className="flex items-center gap-2 mb-2">
                                  {step.success ? (
                                    <CheckCircleOutlined className="text-emerald-400" />
                                  ) : (
                                    <CloseCircleOutlined className="text-red-400" />
                                  )}
                                  <span
                                    className={`text-xs font-medium ${
                                      step.success ? 'text-emerald-400' : 'text-red-400'
                                    }`}
                                  >
                                    工具返回
                                  </span>
                                </div>
                                <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-400">
                                  {step.content}
                                </p>
                              </div>
                            </div>
                          )}
                        </motion.div>
                      ))}
                    </div>
                  </div>
                ))}

                {/* 当前正在进行的思考 */}
                {isThinking && (
                  <motion.div
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-amber-500 border-2 border-slate-800 animate-pulse" />
                    <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                      <div className="flex items-center gap-2 mb-2">
                        <BulbOutlined className="text-amber-400 animate-pulse" />
                        <span className="text-xs font-medium text-amber-400">思考中...</span>
                      </div>
                      <p className="text-sm leading-relaxed text-slate-400">
                        {currentThought || '正在分析问题并规划步骤...'}
                        <span className="inline-block w-2 h-4 bg-amber-400 animate-pulse ml-1 rounded-sm" />
                      </p>
                    </div>
                  </motion.div>
                )}

                {/* 当前正在执行的工具 */}
                {currentToolCall && (
                  <motion.div
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-blue-500 border-2 border-slate-800 animate-pulse" />
                    <div className="rounded-lg border border-white/[0.04] bg-slate-950/40 p-3">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-blue-400 animate-pulse">
                          {toolIcons[currentToolCall.tool] || <ToolOutlined />}
                        </span>
                        <span className="text-xs font-medium text-blue-400">
                          正在执行 {toolNames[currentToolCall.tool] || currentToolCall.tool}...
                        </span>
                        <LoadingOutlined className="text-blue-400 animate-spin" />
                      </div>
                      <code className="block rounded bg-slate-900/70 px-2 py-1 text-xs text-slate-400">
                        {JSON.stringify(currentToolCall.input)}
                      </code>
                    </div>
                  </motion.div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default ReActPanel
