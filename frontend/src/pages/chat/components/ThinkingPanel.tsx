import { useState } from 'react'
import { BulbOutlined, LoadingOutlined } from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'

interface ThinkingPanelProps {
  thought: string
  isThinking: boolean
  isExpanded: boolean
  onToggle: () => void
}

/** 思考过程面板（仅显示最终思考） */
const ThinkingPanel = ({ thought, isThinking, isExpanded, onToggle }: ThinkingPanelProps) => {
  if (!thought && !isThinking) return null

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.2 }}
      className="mb-3"
    >
      <div className="relative overflow-hidden rounded-xl border border-amber-400/20 bg-slate-900/70 backdrop-blur-sm">
        <div className="absolute inset-0 bg-gradient-to-r from-amber-500/10 via-transparent to-orange-400/10 pointer-events-none" />

        {/* 头部 - 可点击展开/收起 */}
        <div
          className="relative z-10 flex items-center justify-between px-3 py-2.5 cursor-pointer hover:bg-white/5 transition-colors"
          onClick={onToggle}
        >
          <div className="flex items-center gap-2">
            <span className="w-5 h-5 rounded-md bg-amber-500/20 border border-amber-400/30 flex items-center justify-center">
              <BulbOutlined className="text-amber-300 text-[11px]" />
            </span>
            <span className="text-amber-100 text-xs font-medium tracking-wide">最终思考</span>
            {isThinking && (
              <span className="flex items-center gap-1 text-[11px] text-amber-200/70">
                <LoadingOutlined className="animate-spin" />
                思考中
              </span>
            )}
          </div>
          <span className="text-[11px] text-amber-200/70 bg-amber-500/10 border border-amber-500/20 px-2 py-0.5 rounded-md">
            {isExpanded ? '收起' : '展开'}
          </span>
        </div>

        {/* 内容 */}
        <AnimatePresence>
          {isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="relative z-10 px-3 pb-3 pt-2.5 border-t border-amber-500/20 max-h-44 overflow-y-auto">
                <p className="text-xs text-slate-300 whitespace-pre-wrap leading-relaxed">
                  {thought || '正在分析问题并整理结论...'}
                </p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

export default ThinkingPanel
