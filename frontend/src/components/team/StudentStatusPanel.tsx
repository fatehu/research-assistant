/**
 * 学生状态面板组件
 * 显示学生的师生关系状态（待审批/已关联）
 */
import { useState } from 'react'
import { motion } from 'framer-motion'
import { Button, Modal, message, Tooltip } from 'antd'
import {
  ClockCircleOutlined,
  CheckCircleOutlined,
  UserOutlined,
  MailOutlined,
  BankOutlined,
  ExperimentOutlined,
  DeleteOutlined,
  CalendarOutlined,
  MessageOutlined,
} from '@ant-design/icons'
import { Mentorship, MentorshipStatus } from '@/services/api'
import { UserAvatar, StatusBadge, Timeline } from '@/components/ui'
import { useMentorshipStore } from '@/stores/mentorshipStore'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

interface StudentStatusPanelProps {
  mentorship: Mentorship
  onStatusChange?: () => void
}

export const StudentStatusPanel = ({ mentorship, onStatusChange }: StudentStatusPanelProps) => {
  const [cancelModalOpen, setCancelModalOpen] = useState(false)
  const { cancelApplication, isSubmitting } = useMentorshipStore()

  const mentor = mentorship.mentor
  const profile = mentor?.profile_data || {}
  const isPending = mentorship.status === MentorshipStatus.PENDING
  const isActive = mentorship.status === MentorshipStatus.ACTIVE

  const handleCancel = async () => {
    try {
      await cancelApplication(mentorship.id)
      message.success('已取消申请')
      setCancelModalOpen(false)
      onStatusChange?.()
    } catch {
      message.error('操作失败')
    }
  }

  // 构建时间线数据
  const timelineItems = [
    {
      status: 'completed' as const,
      label: '提交申请',
      time: dayjs(mentorship.created_at).format('MM/DD'),
    },
    {
      status: isPending ? 'current' as const : 'completed' as const,
      label: '等待审批',
      time: isPending ? '进行中' : dayjs(mentorship.approved_at || mentorship.updated_at).format('MM/DD'),
    },
    {
      status: isActive ? 'completed' as const : 'pending' as const,
      label: '建立关系',
      time: isActive ? dayjs(mentorship.approved_at).format('MM/DD') : '',
    },
  ]

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-gradient-to-br from-slate-800/80 to-slate-900/80 border border-slate-700/50 rounded-2xl overflow-hidden"
    >
      {/* 头部状态条 */}
      <div className={`
        px-6 py-4 border-b border-slate-700/50
        ${isPending 
          ? 'bg-gradient-to-r from-amber-500/10 to-orange-500/10' 
          : 'bg-gradient-to-r from-emerald-500/10 to-teal-500/10'
        }
      `}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            {isPending ? (
              <ClockCircleOutlined className="text-2xl text-amber-400" />
            ) : (
              <CheckCircleOutlined className="text-2xl text-emerald-400" />
            )}
            <div>
              <h3 className="text-lg font-semibold text-white">
                {isPending ? '申请审批中' : '已建立师生关系'}
              </h3>
              <p className="text-sm text-slate-400">
                {isPending 
                  ? `提交于 ${dayjs(mentorship.created_at).fromNow()}`
                  : `建立于 ${dayjs(mentorship.approved_at).format('YYYY年MM月DD日')}`
                }
              </p>
            </div>
          </div>
          <StatusBadge status={mentorship.status} />
        </div>
      </div>

      {/* 时间线 */}
      <div className="px-6 py-4 border-b border-slate-700/50">
        <Timeline items={timelineItems} />
      </div>

      {/* 导师信息 */}
      {mentor && (
        <div className="p-6">
          <h4 className="text-sm font-medium text-slate-400 mb-4">导师信息</h4>
          <div className="flex items-start gap-4">
            <UserAvatar user={mentor} size="xl" />
            <div className="flex-1 min-w-0">
              <h3 className="text-xl font-semibold text-white mb-1">
                {mentor.full_name || mentor.username}
              </h3>
              {profile.title && (
                <p className="text-sm text-slate-400 mb-3">{profile.title}</p>
              )}
              
              <div className="space-y-2">
                {profile.department && (
                  <div className="flex items-center gap-2 text-sm text-slate-400">
                    <BankOutlined className="text-violet-400" />
                    <span>{profile.department}</span>
                  </div>
                )}
                {profile.research_area && (
                  <div className="flex items-center gap-2 text-sm text-slate-400">
                    <ExperimentOutlined className="text-emerald-400" />
                    <span>{profile.research_area}</span>
                  </div>
                )}
                {profile.contact && (
                  <div className="flex items-center gap-2 text-sm text-slate-400">
                    <MailOutlined className="text-blue-400" />
                    <span>{profile.contact}</span>
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* 申请留言 */}
          {mentorship.request_message && (
            <div className="mt-4 p-4 bg-slate-800/50 rounded-xl">
              <div className="flex items-center gap-2 text-sm text-slate-400 mb-2">
                <MessageOutlined />
                <span>我的申请留言</span>
              </div>
              <p className="text-slate-300">{mentorship.request_message}</p>
            </div>
          )}

          {/* 导师回复 */}
          {mentorship.response_message && (
            <div className="mt-4 p-4 bg-emerald-500/10 border border-emerald-500/20 rounded-xl">
              <div className="flex items-center gap-2 text-sm text-emerald-400 mb-2">
                <MessageOutlined />
                <span>导师回复</span>
              </div>
              <p className="text-slate-300">{mentorship.response_message}</p>
            </div>
          )}
        </div>
      )}

      {/* 操作按钮 */}
      {isPending && (
        <div className="px-6 pb-6">
          <Tooltip title="取消后可重新申请其他导师">
            <Button
              danger
              icon={<DeleteOutlined />}
              onClick={() => setCancelModalOpen(true)}
              className="w-full"
            >
              取消申请
            </Button>
          </Tooltip>
        </div>
      )}

      {/* 取消确认弹窗 */}
      <Modal
        title="确认取消申请"
        open={cancelModalOpen}
        onCancel={() => setCancelModalOpen(false)}
        onOk={handleCancel}
        confirmLoading={isSubmitting}
        okText="确认取消"
        cancelText="再想想"
        okButtonProps={{ danger: true }}
      >
        <p>确定要取消对 <strong>{mentor?.full_name || mentor?.username}</strong> 的指导申请吗？</p>
        <p className="text-slate-400 text-sm mt-2">取消后您可以重新申请其他导师。</p>
      </Modal>
    </motion.div>
  )
}
