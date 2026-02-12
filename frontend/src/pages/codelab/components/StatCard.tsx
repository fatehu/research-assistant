interface StatCardProps {
  title: string
  value: number
  icon: React.ReactNode
  color: string
}

/** 统计数据卡片 - 展示 Notebooks / 单元格 / 执行次数 */
const StatCard: React.FC<StatCardProps> = ({ title, value, icon, color }) => (
  <div className="bg-slate-800/50 rounded-xl p-4 border border-slate-700/50">
    <div className="flex items-center gap-3">
      <div className={`w-10 h-10 rounded-lg ${color} flex items-center justify-center text-white`}>
        {icon}
      </div>
      <div>
        <div className="text-2xl font-bold text-white">{value}</div>
        <div className="text-slate-400 text-sm">{title}</div>
      </div>
    </div>
  </div>
)

export default StatCard
