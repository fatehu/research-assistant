import { motion } from 'framer-motion'

interface FeatureCardProps {
  icon: React.ReactNode
  title: string
  description: string
  color: string
  delay?: number
}

/** 功能介绍卡片 - 用于列表页展示 CodeLab 核心能力 */
const FeatureCard: React.FC<FeatureCardProps> = ({ icon, title, description, color, delay = 0 }) => (
  <motion.div
    initial={{ opacity: 0, y: 20 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ delay }}
    className={`relative overflow-hidden rounded-2xl bg-gradient-to-br ${color} p-[1px]`}
  >
    <div className="h-full bg-slate-900/95 backdrop-blur-xl rounded-2xl p-5">
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0 w-12 h-12 rounded-xl bg-white/10 flex items-center justify-center text-white text-xl">
          {icon}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-white font-semibold text-base mb-1">{title}</h3>
          <p className="text-slate-400 text-sm leading-relaxed">{description}</p>
        </div>
      </div>
    </div>
  </motion.div>
)

export default FeatureCard
