/**
 * 团队/师生关系页面
 * 根据用户角色展示不同内容：
 * - 学生: 导师列表/申请状态
 * - 导师: 学生管理仪表板
 * - 管理员: 所有关系管理
 */
import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { message, Input, Spin } from 'antd'
import {
  TeamOutlined,
  SearchOutlined,
  ReloadOutlined,
  UserOutlined,
  CrownOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '@/stores/authStore'
import { useMentorshipStore } from '@/stores/mentorshipStore'
import { MentorGrid } from '@/components/team/MentorCard'
import { StudentStatusPanel } from '@/components/team/StudentStatusPanel'
import { MentorDashboard } from '@/components/team/MentorDashboard'
import { StatCard, CardSkeleton } from '@/components/ui'
import { UserRole } from '@/types'

const { Search } = Input

export const TeamPage = () => {
  const { user, isMentor, isStudent, isAdmin } = useAuthStore()
  const {
    mentors,
    myMentorship,
    pendingRequests,
    myStudents,
    isLoading,
    isSubmitting,
    error,
    fetchMentors,
    fetchMyMentorship,
    fetchPendingRequests,
    fetchMyStudents,
    applyMentorship,
    clearError,
  } = useMentorshipStore()

  const [searchQuery, setSearchQuery] = useState('')
  const [mentorTab, setMentorTab] = useState<'pending' | 'students' | 'activities'>('pending')

  // 根据角色加载数据
  useEffect(() => {
    if (isStudent()) {
      fetchMyMentorship()
      if (!myMentorship) {
        fetchMentors()
      }
    } else if (isMentor()) {
      fetchPendingRequests()
      fetchMyStudents()
    }
  }, [user?.role])

  // 刷新数据
  const handleRefresh = () => {
    if (isStudent()) {
      fetchMyMentorship()
      fetchMentors()
    } else if (isMentor()) {
      fetchPendingRequests()
      fetchMyStudents()
    }
  }

  // 学生申请导师
  const handleApply = async (mentorId: number, requestMessage: string) => {
    try {
      await applyMentorship(mentorId, requestMessage)
      message.success('申请已提交，等待导师审批')
    } catch (err) {
      message.error(error || '申请失败')
    }
  }

  // 显示错误信息
  useEffect(() => {
    if (error) {
      message.error(error)
      clearError()
    }
  }, [error])

  // 过滤导师列表
  const filteredMentors = mentors.filter(mentor => {
    const query = searchQuery.toLowerCase()
    const name = (mentor.full_name || mentor.username || '').toLowerCase()
    const area = (mentor.profile_data?.research_area || '').toLowerCase()
    return name.includes(query) || area.includes(query)
  })

  // 渲染学生视图
  const renderStudentView = () => {
    // 已有师生关系，显示状态面板
    if (myMentorship) {
      return (
        <div className="max-w-2xl mx-auto">
          <StudentStatusPanel 
            mentorship={myMentorship} 
            onStatusChange={() => {
              fetchMyMentorship()
              fetchMentors()
            }}
          />
        </div>
      )
    }

    // 无师生关系，显示导师选择
    return (
      <div className="space-y-6">
        {/* 搜索和筛选 */}
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-4">
          <Search
            placeholder="搜索导师姓名或研究方向..."
            prefix={<SearchOutlined className="text-slate-400" />}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            allowClear
            className="flex-1 max-w-md"
            size="large"
          />
          <button
            onClick={handleRefresh}
            disabled={isLoading}
            className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
          >
            <ReloadOutlined className={isLoading ? 'animate-spin' : ''} />
            刷新
          </button>
        </div>

        {/* 导师网格 */}
        {isLoading && mentors.length === 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {[1, 2, 3, 4, 5, 6].map(i => (
              <CardSkeleton key={i} />
            ))}
          </div>
        ) : (
          <MentorGrid
            mentors={filteredMentors}
            onApply={handleApply}
            isSubmitting={isSubmitting}
            emptyMessage={searchQuery ? '没有找到匹配的导师' : '暂无可用导师'}
          />
        )}
      </div>
    )
  }

  // 渲染导师视图
  const renderMentorView = () => {
    return (
      <div className="space-y-6">
        {/* 统计卡片 */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <StatCard
            label="待处理申请"
            value={pendingRequests.length}
            icon={<ClockCircleOutlined className="text-2xl" />}
            gradient="from-amber-600/80 to-orange-600/80"
          />
          <StatCard
            label="我的学生"
            value={myStudents.length}
            icon={<UserOutlined className="text-2xl" />}
            gradient="from-emerald-600/80 to-teal-600/80"
          />
          <StatCard
            label="总活动数"
            value="42"
            icon={<CheckCircleOutlined className="text-2xl" />}
            gradient="from-blue-600/80 to-indigo-600/80"
          />
        </div>

        {/* 导师仪表板 */}
        <div className="bg-slate-800/30 border border-slate-700/50 rounded-2xl p-6">
          <MentorDashboard
            activeTab={mentorTab}
            onTabChange={setMentorTab}
          />
        </div>
      </div>
    )
  }

  // 渲染管理员视图（预留）
  const renderAdminView = () => {
    return (
      <div className="text-center py-16">
        <CrownOutlined className="text-5xl text-amber-400 mb-4" />
        <h3 className="text-xl font-semibold text-white mb-2">管理员控制台</h3>
        <p className="text-slate-400">师生关系管理功能开发中...</p>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-slate-900 to-slate-800 p-6">
      <div className="max-w-7xl mx-auto">
        {/* 页面头部 */}
        <motion.div
          initial={{ opacity: 0, y: -20 }}
          animate={{ opacity: 1, y: 0 }}
          className="mb-8"
        >
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div className="flex items-center gap-4">
              <div className="w-12 h-12 bg-gradient-to-br from-violet-500 to-blue-500 rounded-xl flex items-center justify-center shadow-lg shadow-violet-500/25">
                <TeamOutlined className="text-2xl text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-bold text-white">
                  {isMentor() ? '学生管理' : '我的团队'}
                </h1>
                <p className="text-slate-400">
                  {isStudent() 
                    ? (myMentorship ? '查看您的师生关系状态' : '选择导师建立指导关系')
                    : '管理您的学生和指导申请'
                  }
                </p>
              </div>
            </div>
            
            {isMentor() && (
              <button
                onClick={handleRefresh}
                disabled={isLoading}
                className="px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg transition-colors flex items-center gap-2 disabled:opacity-50"
              >
                <ReloadOutlined className={isLoading ? 'animate-spin' : ''} />
                刷新数据
              </button>
            )}
          </div>
        </motion.div>

        {/* 主要内容区域 */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
        >
          {isAdmin() && renderAdminView()}
          {isMentor() && !isAdmin() && renderMentorView()}
          {isStudent() && renderStudentView()}
        </motion.div>
      </div>
    </div>
  )
}

export default TeamPage
