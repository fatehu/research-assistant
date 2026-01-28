/**
 * 导师仪表板组件
 * 包含：待处理申请、学生列表、学生活动
 */
import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Modal, message, Empty, Tooltip } from 'antd'
import {
  CheckOutlined,
  CloseOutlined,
  UserOutlined,
  ClockCircleOutlined,
  MessageOutlined,
  CodeOutlined,
  FileTextOutlined,
  BookOutlined,
  DatabaseOutlined,
  EyeOutlined,
  HistoryOutlined,
  LoadingOutlined,
} from '@ant-design/icons'
import { Mentorship, UserBrief } from '@/services/api'
import { useMentorshipStore, StudentActivity } from '@/stores/mentorshipStore'
import { UserAvatar, StatusBadge, ListSkeleton } from '@/components/ui'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

// 活动类型图标映射
const activityIcons: Record<string, React.ReactNode> = {
  conversation: <MessageOutlined className="text-blue-400" />,
  notebook: <FileTextOutlined className="text-emerald-400" />,
  knowledge: <DatabaseOutlined className="text-violet-400" />,
  literature: <BookOutlined className="text-amber-400" />,
  codelab: <CodeOutlined className="text-rose-400" />,
}

// ==================== 待处理申请列表 ====================

interface PendingRequestsProps {
  requests: Mentorship[]
  isLoading: boolean
  onRefresh: () => void
}

export const PendingRequests = ({ requests, isLoading, onRefresh }: PendingRequestsProps) => {
  const [selectedRequest, setSelectedRequest] = useState<Mentorship | null>(null)
  const [action, setAction] = useState<'approve' | 'reject' | null>(null)
  const [responseMessage, setResponseMessage] = useState('')
  const { approveMentorship, rejectMentorship, isSubmitting } = useMentorshipStore()

  const handleAction = async () => {
    if (!selectedRequest || !action) return
    
    try {
      if (action === 'approve') {
        await approveMentorship(selectedRequest.id, responseMessage || undefined)
        message.success('已批准申请')
      } else {
        await rejectMentorship(selectedRequest.id, responseMessage || undefined)
        message.success('已拒绝申请')
      }
      setSelectedRequest(null)
      setAction(null)
      setResponseMessage('')
      onRefresh()
    } catch {
      message.error('操作失败')
    }
  }

  if (isLoading) {
    return <ListSkeleton count={3} />
  }

  if (requests.length === 0) {
    return (
      <div className="text-center py-12">
        <CheckOutlined className="text-5xl text-emerald-500/50 mb-4" />
        <p className="text-slate-400">暂无待处理的申请</p>
        <p className="text-sm text-slate-500 mt-1">所有申请都已处理完毕</p>
      </div>
    )
  }

  return (
    <>
      <div className="space-y-4">
        <AnimatePresence>
          {requests.map((request, index) => {
            const student = request.student
            const profile = student?.profile_data || {}
            
            return (
              <motion.div
                key={request.id}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ delay: index * 0.05 }}
                className="group bg-gradient-to-r from-amber-500/10 to-orange-500/5 border border-amber-500/20 rounded-xl p-4 hover:border-amber-500/40 transition-all"
              >
                <div className="flex items-start gap-4">
                  <UserAvatar user={student} size="lg" />
                  
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-4">
                      <div>
                        <h4 className="font-medium text-white">
                          {student?.full_name || student?.username || '未知学生'}
                        </h4>
                        {profile.department && (
                          <p className="text-sm text-slate-400">{profile.department}</p>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 text-xs text-amber-400 whitespace-nowrap">
                        <ClockCircleOutlined />
                        {dayjs(request.created_at).fromNow()}
                      </div>
                    </div>
                    
                    {request.request_message && (
                      <div className="mt-3 p-3 bg-slate-800/50 rounded-lg text-sm text-slate-300">
                        "{request.request_message}"
                      </div>
                    )}
                    
                    <div className="flex items-center gap-2 mt-4">
                      <motion.button
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                        onClick={() => {
                          setSelectedRequest(request)
                          setAction('approve')
                        }}
                        className="flex-1 px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
                      >
                        <CheckOutlined />
                        批准
                      </motion.button>
                      <motion.button
                        whileHover={{ scale: 1.02 }}
                        whileTap={{ scale: 0.98 }}
                        onClick={() => {
                          setSelectedRequest(request)
                          setAction('reject')
                        }}
                        className="flex-1 px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white text-sm font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
                      >
                        <CloseOutlined />
                        拒绝
                      </motion.button>
                    </div>
                  </div>
                </div>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>

      {/* 审批确认弹窗 */}
      <Modal
        title={action === 'approve' ? '批准申请' : '拒绝申请'}
        open={!!selectedRequest && !!action}
        onCancel={() => {
          setSelectedRequest(null)
          setAction(null)
          setResponseMessage('')
        }}
        onOk={handleAction}
        confirmLoading={isSubmitting}
        okText={action === 'approve' ? '确认批准' : '确认拒绝'}
        okButtonProps={{ 
          danger: action === 'reject',
          className: action === 'approve' ? '!bg-emerald-600 hover:!bg-emerald-500' : ''
        }}
      >
        {selectedRequest && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 p-3 bg-slate-100 dark:bg-slate-800 rounded-lg">
              <UserAvatar user={selectedRequest.student} size="md" />
              <div>
                <p className="font-medium">
                  {selectedRequest.student?.full_name || selectedRequest.student?.username}
                </p>
                {selectedRequest.request_message && (
                  <p className="text-sm text-slate-500 mt-1">
                    "{selectedRequest.request_message}"
                  </p>
                )}
              </div>
            </div>
            
            <div>
              <label className="block text-sm font-medium mb-2">
                回复留言 (可选)
              </label>
              <textarea
                value={responseMessage}
                onChange={(e) => setResponseMessage(e.target.value)}
                placeholder={action === 'approve' 
                  ? '欢迎加入，期待我们的合作...' 
                  : '感谢您的申请，但...'
                }
                className="w-full h-24 px-3 py-2 border border-slate-300 dark:border-slate-600 rounded-lg bg-white dark:bg-slate-900 resize-none focus:outline-none focus:ring-2 focus:ring-violet-500"
              />
            </div>
          </div>
        )}
      </Modal>
    </>
  )
}

// ==================== 学生列表 ====================

interface StudentListProps {
  students: UserBrief[]
  isLoading: boolean
  onViewStudent?: (studentId: number) => void
}

export const StudentList = ({ students, isLoading, onViewStudent }: StudentListProps) => {
  if (isLoading) {
    return <ListSkeleton count={4} />
  }

  if (students.length === 0) {
    return (
      <div className="text-center py-12">
        <UserOutlined className="text-5xl text-slate-600 mb-4" />
        <p className="text-slate-400">暂无关联学生</p>
        <p className="text-sm text-slate-500 mt-1">批准学生申请后将显示在这里</p>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      {students.map((student, index) => {
        const profile = student.profile_data || {}
        
        return (
          <motion.div
            key={student.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: index * 0.05 }}
            className="group bg-slate-800/50 border border-slate-700/50 rounded-xl p-4 hover:border-violet-500/50 hover:bg-slate-800/80 transition-all cursor-pointer"
            onClick={() => onViewStudent?.(student.id)}
          >
            <div className="flex items-center gap-3 mb-3">
              <UserAvatar user={student} size="lg" />
              <div className="min-w-0 flex-1">
                <h4 className="font-medium text-white truncate group-hover:text-violet-300 transition-colors">
                  {student.full_name || student.username}
                </h4>
                {profile.department && (
                  <p className="text-xs text-slate-400 truncate">{profile.department}</p>
                )}
              </div>
            </div>
            
            {profile.research_area && (
              <p className="text-sm text-slate-400 line-clamp-2 mb-3">
                {profile.research_area}
              </p>
            )}
            
            <div className="flex items-center justify-between pt-3 border-t border-slate-700/50">
              <StatusBadge status="active" />
              <Tooltip title="查看详情">
                <button className="p-1.5 text-slate-400 hover:text-white hover:bg-slate-700 rounded-lg transition-all">
                  <EyeOutlined />
                </button>
              </Tooltip>
            </div>
          </motion.div>
        )
      })}
    </div>
  )
}

// ==================== 学生活动 ====================

interface StudentActivitiesProps {
  activities: StudentActivity[]
  isLoading?: boolean
}

export const StudentActivities = ({ activities, isLoading }: StudentActivitiesProps) => {
  if (isLoading) {
    return <ListSkeleton count={5} />
  }

  if (activities.length === 0) {
    return (
      <div className="text-center py-8">
        <HistoryOutlined className="text-4xl text-slate-600 mb-3" />
        <p className="text-slate-400 text-sm">暂无最近活动</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {activities.map((activity, index) => (
        <motion.div
          key={activity.id}
          initial={{ opacity: 0, x: -10 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: index * 0.03 }}
          className="flex items-start gap-3 p-3 bg-slate-800/30 rounded-lg hover:bg-slate-800/50 transition-colors"
        >
          <div className="w-8 h-8 rounded-lg bg-slate-700/50 flex items-center justify-center flex-shrink-0">
            {activityIcons[activity.type]}
          </div>
          
          <div className="flex-1 min-w-0">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm text-white truncate">{activity.title}</p>
                <p className="text-xs text-slate-500">
                  {activity.student.full_name || activity.student.username}
                </p>
              </div>
              <span className="text-xs text-slate-500 whitespace-nowrap">
                {dayjs(activity.timestamp).fromNow()}
              </span>
            </div>
            {activity.description && (
              <p className="text-xs text-slate-400 mt-1 line-clamp-1">
                {activity.description}
              </p>
            )}
          </div>
        </motion.div>
      ))}
    </div>
  )
}

// ==================== 导师仪表板主组件 ====================

interface MentorDashboardProps {
  activeTab: 'pending' | 'students' | 'activities'
  onTabChange: (tab: 'pending' | 'students' | 'activities') => void
}

export const MentorDashboard = ({ activeTab, onTabChange }: MentorDashboardProps) => {
  const {
    pendingRequests,
    myStudents,
    studentActivities,
    isLoading,
    pendingCount,
    fetchPendingRequests,
    fetchMyStudents,
  } = useMentorshipStore()

  const tabs = [
    { 
      key: 'pending' as const, 
      label: '待处理申请', 
      icon: <ClockCircleOutlined />,
      count: pendingCount,
    },
    { 
      key: 'students' as const, 
      label: '我的学生', 
      icon: <UserOutlined />,
      count: myStudents.length,
    },
    { 
      key: 'activities' as const, 
      label: '最近活动', 
      icon: <HistoryOutlined />,
    },
  ]

  const handleRefresh = () => {
    fetchPendingRequests()
    fetchMyStudents()
  }

  return (
    <div className="space-y-6">
      {/* Tab 导航 */}
      <div className="flex items-center gap-2 p-1 bg-slate-800/50 rounded-xl">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => onTabChange(tab.key)}
            className={`
              flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg
              text-sm font-medium transition-all
              ${activeTab === tab.key
                ? 'bg-violet-600 text-white shadow-lg shadow-violet-500/25'
                : 'text-slate-400 hover:text-white hover:bg-slate-700/50'
              }
            `}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.count !== undefined && tab.count > 0 && (
              <span className={`
                px-1.5 py-0.5 text-xs rounded-full
                ${activeTab === tab.key 
                  ? 'bg-white/20 text-white' 
                  : tab.key === 'pending' 
                    ? 'bg-amber-500/20 text-amber-400'
                    : 'bg-slate-600 text-slate-300'
                }
              `}>
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          transition={{ duration: 0.2 }}
        >
          {activeTab === 'pending' && (
            <PendingRequests
              requests={pendingRequests}
              isLoading={isLoading}
              onRefresh={handleRefresh}
            />
          )}
          {activeTab === 'students' && (
            <StudentList
              students={myStudents}
              isLoading={isLoading}
            />
          )}
          {activeTab === 'activities' && (
            <StudentActivities
              activities={studentActivities}
              isLoading={isLoading}
            />
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  )
}

export default MentorDashboard
