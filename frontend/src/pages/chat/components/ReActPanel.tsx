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
      className="mb-4"
    >
      <div className="rounded-xl bg-gradient-to-br from-slate-800/80 to-slate-900/80 border border-slate-700/50 overflow-hidden shadow-lg">
        {/* 头部 - 渐变背景 */}
        <div
          className="flex items-center justify-between px-4 py-3 bg-gradient-to-r from-purple-500/10 via-blue-500/10 to-emerald-500/10 cursor-pointer"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple-500 to-blue-500 flex items-center justify-center shadow-lg">
              <BulbOutlined className="text-white text-sm" />
            </div>
            <div>
              <div className="text-sm font-medium text-white">推理过程</div>
              <div className="text-xs text-slate-400">
                {currentIteration > 0 ? `第 ${currentIteration} 轮推理` : '准备中'}
                {(isThinking || currentToolCall) && (
                  <span className="ml-2 text-emerald-400">
                    <LoadingOutlined className="animate-spin mr-1" />
                    {currentToolCall ? '执行工具中' : '思考中'}
                  </span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {steps.length > 0 && (
              <span className="text-xs text-slate-400 bg-slate-700/50 px-2 py-1 rounded-full">
                {steps.length} 步
              </span>
            )}
            <Button
              type="text"
              size="small"
              icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
              className="text-slate-400 hover:text-white"
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
              <div className="px-4 py-3 space-y-4 max-h-96 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-700">
                {/* 显示所有迭代 */}
                {iterations.map((iterSteps, iterIndex) => (
                  <div key={iterIndex}>
                    {/* 迭代分隔线 */}
                    {iterIndex > 0 && (
                      <div className="flex items-center gap-3 py-2 my-2">
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-600 to-transparent" />
                        <span className="text-xs text-slate-500 px-2">第 {iterIndex + 1} 轮推理</span>
                        <div className="flex-1 h-px bg-gradient-to-r from-transparent via-slate-600 to-transparent" />
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
                              <div className="bg-amber-500/10 rounded-lg p-3 border border-amber-500/20">
                                <div className="flex items-center gap-2 mb-2">
                                  <BulbOutlined className="text-amber-400" />
                                  <span className="text-xs font-medium text-amber-400">思考</span>
                                </div>
                                <p className="text-sm text-slate-300 leading-relaxed">{step.content}</p>
                              </div>
                            </div>
                          )}

                          {step.type === 'action' && (
                            <div className="relative">
                              <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-blue-500 border-2 border-slate-800" />
                              <div className="bg-blue-500/10 rounded-lg p-3 border border-blue-500/20">
                                <div className="flex items-center gap-2 mb-2">
                                  <span className="text-blue-400">
                                    {toolIcons[step.tool || ''] || <ToolOutlined />}
                                  </span>
                                  <span className="text-xs font-medium text-blue-400">
                                    调用 {toolNames[step.tool || ''] || step.tool}
                                  </span>
                                </div>
                                <code className="text-xs text-slate-400 bg-slate-800/80 px-2 py-1 rounded block overflow-x-auto">
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
                                    ? 'bg-emerald-500/10 border-emerald-500/20'
                                    : 'bg-red-500/10 border-red-500/20'
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
                                <p className="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">
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
                {isThinking && currentThought && (
                  <motion.div
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    className="relative pl-6"
                  >
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />
                    <div className="absolute -left-6 top-1 w-3 h-3 rounded-full bg-amber-500 border-2 border-slate-800 animate-pulse" />
                    <div className="bg-amber-500/10 rounded-lg p-3 border border-amber-500/20">
                      <div className="flex items-center gap-2 mb-2">
                        <BulbOutlined className="text-amber-400 animate-pulse" />
                        <span className="text-xs font-medium text-amber-400">思考中...</span>
                      </div>
                      <p className="text-sm text-slate-300 leading-relaxed">
                        {currentThought}
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
                    <div className="bg-blue-500/10 rounded-lg p-3 border border-blue-500/20">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-blue-400 animate-pulse">
                          {toolIcons[currentToolCall.tool] || <ToolOutlined />}
                        </span>
                        <span className="text-xs font-medium text-blue-400">
                          正在执行 {toolNames[currentToolCall.tool] || currentToolCall.tool}...
                        </span>
                        <LoadingOutlined className="text-blue-400 animate-spin" />
                      </div>
                      <code className="text-xs text-slate-400 bg-slate-800/80 px-2 py-1 rounded block">
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
