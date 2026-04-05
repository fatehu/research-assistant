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
  defaultExpanded?: boolean
  embedded?: boolean
}

/** 历史消息的 ReAct 推理过程面板 */
const HistoryReActPanel = ({
  steps,
  defaultExpanded = false,
  embedded = false,
}: HistoryReActPanelProps) => {
  const [expanded, setExpanded] = useState(defaultExpanded)

  if (!steps || steps.length === 0) return null

  // 统计信息
  const totalIterations = Math.max(...steps.map((s) => s.iteration || 1))
  const toolCalls = steps.filter((s) => s.type === 'action').length
  const shellClass = embedded
    ? 'relative overflow-hidden rounded-lg border border-white/[0.04] bg-white/[0.02] shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]'
    : 'relative rounded-xl border border-cyan-500/20 bg-slate-900/70 backdrop-blur-sm overflow-hidden'
  const contentBorderClass = embedded ? 'border-white/[0.08]' : 'border-slate-700/60'

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className={embedded ? '' : 'mb-3'}>
      <div className={shellClass}>
        {/* 头部 */}
        <div
          className={`relative z-10 flex cursor-pointer items-center justify-between transition-colors ${
            embedded ? 'gap-3 px-3 py-2 hover:bg-white/[0.04]' : 'px-4 py-2.5 hover:bg-white/5'
          }`}
          onClick={() => setExpanded(!expanded)}
        >
          <div className="flex items-center gap-3">
            <div className={embedded ? 'text-emerald-300' : 'flex h-7 w-7 items-center justify-center rounded-lg border border-cyan-300/30 bg-gradient-to-br from-cyan-500/80 to-blue-500/80 shadow-sm shadow-cyan-500/20'}>
              <BulbOutlined className={`text-[11px] ${embedded ? '' : 'text-white'}`} />
            </div>
            <div className="flex items-center gap-2">
              <span className={`font-medium ${embedded ? 'text-xs tracking-wide text-slate-400' : 'text-sm text-slate-100'}`}>推理过程</span>
              <span className={`rounded-full border px-2 py-0.5 text-[11px] ${
                embedded
                  ? 'border-white/[0.06] bg-transparent text-slate-500'
                  : 'border-cyan-500/20 bg-cyan-500/10 text-cyan-200/80'
              }`}>
                {totalIterations} 轮迭代
              </span>
              <span className={`rounded-full border px-2 py-0.5 text-[11px] ${
                embedded
                  ? 'border-white/[0.06] bg-transparent text-slate-500'
                  : 'border-slate-600/60 bg-slate-700/40 text-slate-300/80'
              }`}>
                {toolCalls} 次工具调用
              </span>
            </div>
          </div>
          <Button
            type="text"
            size="small"
            icon={expanded ? <CompressOutlined /> : <ExpandOutlined />}
            className="text-slate-300 hover:text-white"
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
              <div
                className={`relative z-10 max-h-80 overflow-y-auto space-y-3 ${
                  embedded
                    ? 'mx-3 mb-3 border-l border-white/[0.08] pl-4 pt-2'
                    : `border-t px-4 py-3 ${contentBorderClass}`
                }`}
              >
                {steps.map((step, index) => (
                  <div key={index} className="relative pl-5">
                    {/* 时间线 */}
                    <div className={`absolute bottom-0 left-0 top-0 w-px ${embedded ? 'bg-slate-800' : 'bg-slate-700/80'}`} />

                    {step.type === 'thought' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-amber-500 border-2 border-slate-800" />
                        <div className={`rounded-lg p-2.5 backdrop-blur-sm ${
                          embedded ? 'border border-white/[0.04] bg-slate-950/40' : 'border border-amber-500/18 bg-slate-950/70'
                        }`}>
                          <div className="flex items-center gap-2 mb-1.5">
                            <BulbOutlined className="text-amber-400 text-xs" />
                            <span className="text-xs font-medium text-amber-400">
                              第 {step.iteration} 轮过程
                            </span>
                          </div>
                          <p className={`text-xs leading-relaxed ${embedded ? 'text-slate-400' : 'text-slate-300'}`}>{step.content}</p>
                        </div>
                      </div>
                    )}

                    {step.type === 'action' && (
                      <div className="relative">
                        <div className="absolute -left-5 top-1.5 w-2.5 h-2.5 rounded-full bg-blue-500 border-2 border-slate-800" />
                        <div className={`rounded-lg p-2.5 backdrop-blur-sm ${
                          embedded ? 'border border-white/[0.04] bg-slate-950/40' : 'border border-sky-500/18 bg-slate-950/70'
                        }`}>
                          <div className="flex items-center gap-2 mb-1.5">
                            <span className="text-blue-400 text-xs">
                              {toolIcons[step.tool || ''] || <ToolOutlined />}
                            </span>
                            <span className="text-xs font-medium text-blue-400">
                              调用 {toolNames[step.tool || ''] || step.tool}
                            </span>
                          </div>
                          <code className="block overflow-x-auto rounded border border-slate-700/60 bg-slate-900/80 px-2 py-1 text-[10px] text-slate-300/80">
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
                              ? embedded
                                ? 'bg-slate-950/40 border-white/[0.04]'
                                : 'bg-slate-950/70 border-emerald-500/18'
                              : embedded
                                ? 'bg-slate-950/40 border-white/[0.04]'
                                : 'bg-slate-950/70 border-red-500/18'
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
                          <p className={`whitespace-pre-wrap text-xs leading-relaxed ${
                            embedded ? 'text-slate-400' : 'text-slate-300'
                          }`}>
                            {step.output}
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
