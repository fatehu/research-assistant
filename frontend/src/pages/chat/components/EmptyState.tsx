import {
  RobotOutlined,
  BulbOutlined,
  SearchOutlined,
  GlobalOutlined,
  CalculatorOutlined,
  ClockCircleOutlined,
  SwapOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'

interface EmptyStateProps {
  onQuickPrompt: (prompt: string) => void
}

/** 空状态欢迎页 */
const EmptyState = ({ onQuickPrompt }: EmptyStateProps) => {
  const prompts = [
    { icon: '🔬', text: '解释深度学习中的注意力机制' },
    { icon: '📊', text: '计算 sin(45°) + cos(60°) 的值' },
    { icon: '📝', text: '搜索我知识库中关于机器学习的内容' },
    { icon: '💡', text: '帮我把 100 华氏度转换成摄氏度' },
  ]

  const tools = [
    { icon: <SearchOutlined />, name: '知识库搜索', desc: '检索上传的文档' },
    { icon: <GlobalOutlined />, name: '网络搜索', desc: '搜索互联网' },
    { icon: <CalculatorOutlined />, name: '计算器', desc: '数学运算' },
    { icon: <ClockCircleOutlined />, name: '日期时间', desc: '获取当前时间' },
    { icon: <SwapOutlined />, name: '单位转换', desc: '长度/重量/温度' },
  ]

  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center py-12 px-4"
    >
      {/* Logo */}
      <div className="relative mb-6">
        <div className="w-20 h-20 rounded-3xl bg-gradient-to-br from-emerald-400 via-teal-500 to-cyan-600 flex items-center justify-center shadow-2xl shadow-emerald-500/25">
          <RobotOutlined className="text-4xl text-white" />
        </div>
        <div className="absolute -bottom-1 -right-1 w-7 h-7 rounded-xl bg-amber-400 flex items-center justify-center">
          <BulbOutlined className="text-amber-900 text-sm" />
        </div>
      </div>

      {/* 标题 */}
      <h1 className="text-2xl font-bold text-white mb-2">AI 科研助手</h1>
      <p className="text-slate-400 text-center max-w-md mb-6 text-sm leading-relaxed">
        我可以帮助你解答科研问题、分析数据、检索知识库
        <br />
        <span className="text-emerald-400">支持工具调用，可以看到过程与工具轨道</span>
      </p>

      {/* 可用工具 */}
      <div className="flex flex-wrap justify-center gap-2 mb-8">
        {tools.map((tool, index) => (
          <motion.div
            key={index}
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: 0.05 * index }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-slate-800/50 border border-slate-700/50 text-xs"
          >
            <span className="text-blue-400">{tool.icon}</span>
            <span className="text-slate-300">{tool.name}</span>
          </motion.div>
        ))}
      </div>

      {/* 快捷提示 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-2xl">
        {prompts.map((prompt, index) => (
          <motion.button
            key={index}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 + index * 0.05 }}
            onClick={() => onQuickPrompt(prompt.text)}
            className="flex items-center gap-3 p-4 rounded-xl bg-slate-800/50 border border-slate-700/50 hover:bg-slate-700/50 hover:border-slate-600 transition-all text-left group"
          >
            <span className="text-2xl">{prompt.icon}</span>
            <span className="text-sm text-slate-300 group-hover:text-white transition-colors">
              {prompt.text}
            </span>
          </motion.button>
        ))}
      </div>
    </motion.div>
  )
}

export default EmptyState
