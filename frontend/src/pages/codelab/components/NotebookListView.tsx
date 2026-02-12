import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Spin, Empty, Badge, Dropdown, Tooltip } from 'antd'
import {
  PlusOutlined, DeleteOutlined, CodeOutlined,
  FolderOutlined, ClockCircleOutlined,
  ExclamationCircleOutlined, ReloadOutlined,
  ExperimentOutlined, ThunderboltOutlined,
  RobotOutlined, CloudOutlined, BarChartOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import { motion } from 'framer-motion'
import { Notebook } from '@/services/api'
import dayjs from 'dayjs'
import FeatureCard from './FeatureCard'
import StatCard from './StatCard'

interface NotebookListViewProps {
  notebooks: Notebook[]
  isLoading: boolean
  loadError: string | null
  onCreateNotebook: () => void
  onDeleteNotebook: (id: string) => void
  onRefresh: () => void
}

/** Notebook 列表视图 - 展示所有 Notebooks、统计和功能介绍 */
const NotebookListView = ({
  notebooks,
  isLoading,
  loadError,
  onCreateNotebook,
  onDeleteNotebook,
  onRefresh,
}: NotebookListViewProps) => {
  const navigate = useNavigate()

  const stats = useMemo(() => ({
    totalNotebooks: notebooks.length,
    totalCells: notebooks.reduce((acc, nb) => acc + nb.cells.length, 0),
    totalExecutions: notebooks.reduce((acc, nb) => acc + nb.execution_count, 0),
  }), [notebooks])

  return (
    <div className="h-full flex flex-col bg-slate-950">
      {/* 渐变背景 */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className="absolute top-0 left-1/4 w-96 h-96 bg-amber-500/10 rounded-full filter blur-3xl" />
        <div className="absolute bottom-1/4 right-1/4 w-80 h-80 bg-orange-500/10 rounded-full filter blur-3xl" />
      </div>

      {/* 头部 */}
      <div className="relative flex-shrink-0 h-16 px-6 flex items-center justify-between bg-slate-900/60 border-b border-slate-800 backdrop-blur-xl">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-amber-500 to-orange-600 flex items-center justify-center shadow-lg shadow-amber-500/20">
            <ExperimentOutlined className="text-white text-2xl" />
          </div>
          <div>
            <h1 className="text-white font-bold text-xl">代码实验室</h1>
            <p className="text-slate-400 text-sm">Jupyter-style 交互式 Python 环境</p>
          </div>
        </div>

        <Button
          type="primary"
          size="large"
          icon={<PlusOutlined />}
          onClick={onCreateNotebook}
          className="rounded-xl h-11 px-6 bg-gradient-to-r from-amber-500 to-orange-500 border-0 shadow-lg shadow-amber-500/20"
        >
          新建 Notebook
        </Button>
      </div>

      {/* 主内容区 */}
      <div className="relative flex-1 overflow-y-auto p-6">
        <div className="max-w-5xl mx-auto">
          {/* 统计卡片 + 功能介绍 */}
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="mb-10">
            <div className="grid grid-cols-3 gap-4 mb-8">
              <StatCard title="Notebooks" value={stats.totalNotebooks} icon={<FolderOutlined />} color="bg-amber-500/80" />
              <StatCard title="总单元格" value={stats.totalCells} icon={<CodeOutlined />} color="bg-emerald-500/80" />
              <StatCard title="执行次数" value={stats.totalExecutions} icon={<ThunderboltOutlined />} color="bg-blue-500/80" />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
              <FeatureCard icon={<CodeOutlined />} title="Python 代码执行" description="支持完整的 Python 环境，包括 numpy、pandas、matplotlib 等科学计算库" color="from-emerald-500/50 to-teal-600/50" delay={0.1} />
              <FeatureCard icon={<BarChartOutlined />} title="数据可视化" description="内置图表渲染，支持 matplotlib、seaborn 等绑图库的实时可视化" color="from-blue-500/50 to-cyan-600/50" delay={0.15} />
              <FeatureCard icon={<RobotOutlined />} title="AI 助手" description="智能代码补全和错误分析，帮助你快速解决编程问题" color="from-purple-500/50 to-pink-600/50" delay={0.2} />
              <FeatureCard icon={<CloudOutlined />} title="云端同步" description="所有 Notebook 自动保存到云端，随时随地访问你的代码" color="from-amber-500/50 to-orange-600/50" delay={0.25} />
            </div>
          </motion.div>

          {/* Notebook 列表 */}
          <div className="space-y-4">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2 text-slate-400">
                <FolderOutlined />
                <span className="font-medium">我的 Notebooks</span>
                <Badge count={notebooks.length} showZero color="#52525b" />
              </div>
              <Button type="text" icon={<ReloadOutlined />} onClick={onRefresh} loading={isLoading} className="text-slate-400 hover:text-white">
                刷新
              </Button>
            </div>

            {isLoading ? (
              <div className="flex flex-col items-center justify-center py-16">
                <Spin size="large" />
                <p className="text-slate-500 mt-4">加载中...</p>
              </div>
            ) : loadError ? (
              <div className="flex flex-col items-center justify-center py-16 text-center">
                <ExclamationCircleOutlined className="text-4xl text-red-400 mb-4" />
                <p className="text-red-400 mb-4">{loadError}</p>
                <Button onClick={onRefresh} icon={<ReloadOutlined />} className="rounded-lg">重试</Button>
              </div>
            ) : notebooks.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <div className="text-slate-500">
                    <p>还没有 Notebook</p>
                    <p className="text-sm mt-1">点击上方按钮创建你的第一个交互式代码笔记本</p>
                  </div>
                }
              />
            ) : (
              <div className="grid gap-4">
                {notebooks.map((nb, i) => (
                  <motion.div
                    key={nb.id}
                    initial={{ opacity: 0, x: -20 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: i * 0.03 }}
                    className="group bg-slate-800/40 rounded-xl p-5 border border-slate-700/50 hover:border-amber-500/30 hover:bg-slate-800/60 cursor-pointer transition-all"
                    onClick={() => navigate(`/code/${nb.id}`)}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex items-start gap-4">
                        <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-amber-500/20 to-orange-500/20 flex items-center justify-center border border-amber-500/20">
                          <ExperimentOutlined className="text-amber-400 text-xl" />
                        </div>
                        <div>
                          <h3 className="text-white font-semibold text-lg group-hover:text-amber-400 transition-colors">{nb.title}</h3>
                          <p className="text-slate-500 text-sm mt-1">{nb.cells.length} 个单元格 · 执行 {nb.execution_count} 次</p>
                          <p className="text-slate-600 text-xs mt-2 flex items-center gap-1">
                            <ClockCircleOutlined />
                            更新于 {dayjs(nb.updated_at).format('YYYY-MM-DD HH:mm')}
                          </p>
                        </div>
                      </div>
                      <Dropdown
                        menu={{
                          items: [
                            { key: 'open', icon: <CodeOutlined />, label: '打开' },
                            { type: 'divider' },
                            { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
                          ],
                          onClick: ({ key, domEvent }) => {
                            domEvent.stopPropagation()
                            if (key === 'delete') onDeleteNotebook(nb.id)
                            if (key === 'open') navigate(`/code/${nb.id}`)
                          },
                        }}
                        trigger={['click']}
                      >
                        <Button
                          type="text"
                          icon={<MoreOutlined />}
                          onClick={(e) => e.stopPropagation()}
                          className="text-slate-500 hover:text-white opacity-0 group-hover:opacity-100 transition-opacity"
                        />
                      </Dropdown>
                    </div>
                  </motion.div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default NotebookListView
