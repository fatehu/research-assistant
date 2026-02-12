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
      className="mb-2"
    >
      <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 overflow-hidden">
        {/* 头部 - 可点击展开/收起 */}
        <div
          className="flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-amber-500/10 transition-colors"
          onClick={onToggle}
        >
          <div className="flex items-center gap-2">
            <BulbOutlined className="text-amber-400 text-sm" />
            <span className="text-amber-400/90 text-xs font-medium">最终思考</span>
            {isThinking && (
              <span className="flex items-center gap-1 text-xs text-amber-400/60">
                <LoadingOutlined className="animate-spin" />
                思考中
              </span>
            )}
          </div>
          <span className="text-amber-400/50 text-xs">
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
              <div className="px-3 py-2 border-t border-amber-500/10 max-h-40 overflow-y-auto">
                <p className="text-xs text-slate-400 whitespace-pre-wrap leading-relaxed">
                  {thought || '正在分析问题...'}
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
