import { useState } from 'react'
import { Button } from 'antd'
import {
  BulbOutlined,
  ExpandOutlined,
  CompressOutlined,
  ToolOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import { toolIcons, toolNames } from '../constants'

interface HistoryStep {
  type: string
  iteration: number
  content?: string
  tool?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
}

interface HistoryReActPanelProps {
  steps: HistoryStep[]
}

/** 历史消息的 ReAct 推理过程面板 */
const HistoryReActPanel = ({ steps }: HistoryReActPanelProps) => {
  const [expanded, setExpanded] = useState(false)

  if (!steps || steps.length === 0) return null

  // 统计信息
  const totalIterations = Math.max(...steps.map((s) => s.iteration || 1))
  const toolCalls = steps.filter((s) => s.type === 'action').length

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="mb-3">
      <div className="rounded-xl bg-gradient-to-br from-slate-800/60 to-slate-900/60 border border-slate-700/50 overflow-hidden">
        {/* 头部 */}
        <div
          className="flex items-center justify-between px-4 py-2.5 cursor-pointer hover:bg-slate-700/30 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-purple-500/80 to-blue-500/80 flex items-center justify-center">
              <BulbOutlined className="text-white text-xs" />
            </div>
            <div>
              <span className="text-sm font-medium text-slate-300">推理过程</span>
              <span className="ml-2 text-xs text-slate-500">
                {totalIterations} 轮迭代 · {toolCalls} 次工具调用
              </span>
            </div>
          </div>
          <Button
            type="text"
            size="small"
            icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
            className="text-slate-400 hover:text-white"
          />
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
              <div className="px-4 py-3 border-t border-slate-700/50 space-y-3 max-h-80 overflow-y-auto">
                {steps.map((step, index) => (
                  <div key={index} className="relative pl-5">
                    {/* 时间线 */}
                    <div className="absolute left-0 top-0 bottom-0 w-px bg-slate-700" />

                    {step.type === 'thought' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-amber-500 border-2 border-slate-800" />
                        <div className="bg-amber-500/10 rounded-lg p-2.5 border border-amber-500/20">
                          <div className="flex items-center gap-2 mb-1.5">
                            <BulbOutlined className="text-amber-400 text-xs" />
                            <span className="text-xs font-medium text-amber-400">
                              第 {step.iteration} 轮思考
                            </span>
                          </div>
                          <p className="text-xs text-slate-400 leading-relaxed">{step.content}</p>
                        </div>
                      </div>
                    )}

                    {step.type === 'action' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-blue-500 border-2 border-slate-800" />
                        <div className="bg-blue-500/10 rounded-lg p-2.5 border border-blue-500/20">
                          <div className="flex items-center gap-2 mb-1.5">
                            <span className="text-blue-400 text-xs">
                              {toolIcons[step.tool || ''] || <ToolOutlined />}
                            </span>
                            <span className="text-xs font-medium text-blue-400">
                              调用 {toolNames[step.tool || ''] || step.tool}
                            </span>
                          </div>
                          <code className="text-[10px] text-slate-500 bg-slate-800/60 px-2 py-1 rounded block overflow-x-auto">
                            {JSON.stringify(step.input)}
                          </code>
                        </div>
                      </div>
                    )}

                    {step.type === 'observation' && (
                      <div className="relative">
                        <div
                          className={`absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full border-2 border-slate-800 ${
                            step.success ? 'bg-emerald-500' : 'bg-red-500'
                          }`}
                        />
                        <div
                          className={`rounded-lg p-2.5 border ${
                            step.success
                              ? 'bg-emerald-500/10 border-emerald-500/20'
                              : 'bg-red-500/10 border-red-500/20'
                          }`}
                        >
                          <div className="flex items-center gap-2 mb-1.5">
                            {step.success ? (
                              <CheckCircleOutlined className="text-emerald-400 text-xs" />
                            ) : (
                              <CloseCircleOutlined className="text-red-400 text-xs" />
                            )}
                            <span
                              className={`text-xs font-medium ${
                                step.success ? 'text-emerald-400' : 'text-red-400'
                              }`}
                            >
                              工具返回
                            </span>
                          </div>
                          <p className="text-xs text-slate-400 leading-relaxed whitespace-pre-wrap">
                            {(step.output || '').length > 300
                              ? (step.output || '').slice(0, 300) + '...'
                              : step.output}
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default HistoryReActPanel
