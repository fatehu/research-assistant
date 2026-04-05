import { BulbOutlined, LoadingOutlined } from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'

interface ThinkingPanelProps {
  thought: string
  isThinking: boolean
  isExpanded: boolean
  onToggle: () => void
  embedded?: boolean
  label?: string
}

/** 思考过程面板（仅显示最终思考） */
const ThinkingPanel = ({
  thought,
  isThinking,
  isExpanded,
  onToggle,
  embedded = false,
  label = '最终思考',
}: ThinkingPanelProps) => {
  if (!thought && !isThinking) return null

  const shellClass = embedded
    ? 'relative overflow-hidden rounded-lg border border-white/[0.04] bg-white/[0.02] shadow-[inset_0_1px_0_rgba(255,255,255,0.02)]'
    : 'relative overflow-hidden rounded-xl border border-amber-400/20 bg-slate-900/70 backdrop-blur-sm'
  const separatorClass = embedded ? 'border-white/[0.08]' : 'border-amber-500/20'
  const containerClass = embedded ? '' : 'mb-3'

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.2 }}
      className={containerClass}
    >
      <div className={shellClass}>
        {/* 头部 - 可点击展开/收起 */}
        <div
          className={`relative z-10 flex cursor-pointer items-center justify-between transition-colors ${
            embedded ? 'gap-3 px-3 py-2 hover:bg-white/[0.04]' : 'px-3 py-2.5 hover:bg-white/5'
          }`}
          onClick={onToggle}
        >
          <div className="flex items-center gap-2">
            <span className={embedded ? 'text-emerald-300' : 'flex h-5 w-5 items-center justify-center rounded-md border border-amber-400/30 bg-amber-500/20'}>
              <BulbOutlined className={`text-[11px] ${embedded ? '' : 'text-amber-300'}`} />
            </span>
            <span className={`text-xs font-medium tracking-wide ${
              embedded ? 'text-slate-400' : 'text-amber-100'
            }`}>
              {label}
            </span>
            {isThinking && (
              <span className={`flex items-center gap-1 text-[11px] ${
                embedded ? 'text-slate-400' : 'text-amber-200/70'
              }`}>
                <LoadingOutlined className="animate-spin" />
                思考中
              </span>
            )}
          </div>
          <span className={`rounded-md border px-2 py-0.5 text-[11px] ${
            embedded
              ? 'border-white/[0.06] bg-transparent text-slate-500'
              : 'border-amber-500/20 bg-amber-500/10 text-amber-200/70'
          }`}>
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
              <div
                className={`relative z-10 max-h-44 overflow-y-auto ${
                  embedded
                    ? 'mx-3 mb-3 border-l border-white/[0.08] pl-4 pt-1'
                    : `border-t px-3 pb-3 pt-2.5 ${separatorClass}`
                }`}
              >
                <p className={`whitespace-pre-wrap text-xs leading-relaxed ${
                  embedded ? 'text-slate-400' : 'text-slate-300'
                }`}>
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
