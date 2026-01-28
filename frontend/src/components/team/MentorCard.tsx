/**
 * 导师卡片组件
 * 用于展示导师信息，支持申请指导
 */
import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Modal, Input, Button, message } from 'antd'
import {
  UserOutlined,
  MailOutlined,
  BankOutlined,
  ExperimentOutlined,
  SendOutlined,
  StarOutlined,
} from '@ant-design/icons'
import { UserBrief } from '@/services/api'
import { UserAvatar, RoleBadge } from '@/components/ui'
import { useMentorshipStore } from '@/stores/mentorshipStore'

const { TextArea } = Input

interface MentorCardProps {
  mentor: UserBrief
  onApplySuccess?: () => void
  disabled?: boolean
}

export const MentorCard = ({ mentor, onApplySuccess, disabled = false }: MentorCardProps) => {
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [applicationMessage, setApplicationMessage] = useState('')
  const { applyMentorship, isSubmitting } = useMentorshipStore()

  const handleApply = async () => {
    try {
      await applyMentorship(mentor.id, applicationMessage || undefined)
      message.success('申请已提交，请等待导师审批')
      setIsModalOpen(false)
      setApplicationMessage('')
      onApplySuccess?.()
    } catch (error) {
      message.error('申请失败，请稍后重试')
    }
  }

  const profile = mentor.profile_data || {}

  return (
    <>
      <motion.div
        whileHover={{ y: -4, scale: 1.01 }}
        transition={{ duration: 0.2 }}
        className={`
          group relative bg-gradient-to-br from-slate-800/80 to-slate-900/80
          border border-slate-700/50 rounded-2xl p-6
          hover:border-violet-500/50 hover:shadow-lg hover:shadow-violet-500/10
          transition-all duration-300
          ${disabled ? 'opacity-60 pointer-events-none' : ''}
        `}
      >
        {/* 头部信息 */}
        <div className="flex items-start gap-4 mb-4">
          <UserAvatar user={mentor} size="lg" />
          <div className="flex-1 min-w-0">
            <h3 className="text-lg font-semibold text-white truncate">
              {mentor.full_name || mentor.username}
            </h3>
            {profile.title && (
              <p className="text-sm text-slate-400 truncate">{profile.title}</p>
            )}
            <RoleBadge role={mentor.role} size="sm" />
          </div>
        </div>

        {/* 详细信息 */}
        <div className="space-y-2 mb-4">
          {profile.department && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <BankOutlined className="text-violet-400" />
              <span className="truncate">{profile.department}</span>
            </div>
          )}
          {profile.research_area && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <ExperimentOutlined className="text-emerald-400" />
              <span className="truncate">{profile.research_area}</span>
            </div>
          )}
          {profile.contact && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <MailOutlined className="text-blue-400" />
              <span className="truncate">{profile.contact}</span>
            </div>
          )}
        </div>

        {/* 简介 */}
        {profile.bio && (
          <p className="text-sm text-slate-500 line-clamp-2 mb-4">
            {profile.bio}
          </p>
        )}

        {/* 申请按钮 */}
        <motion.button
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          onClick={() => setIsModalOpen(true)}
          disabled={disabled}
          className={`
            w-full py-2.5 rounded-xl font-medium text-sm
            flex items-center justify-center gap-2
            bg-gradient-to-r from-violet-600 to-purple-600
            hover:from-violet-500 hover:to-purple-500
            text-white shadow-lg shadow-violet-500/20
            transition-all duration-200
            disabled:opacity-50 disabled:cursor-not-allowed
          `}
        >
          <SendOutlined />
          申请成为学生
        </motion.button>

        {/* 悬浮光效 */}
        <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-violet-500/0 to-purple-500/0 group-hover:from-violet-500/5 group-hover:to-purple-500/5 transition-all duration-300 pointer-events-none" />
      </motion.div>

      {/* 申请弹窗 */}
      <Modal
        title={
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-violet-500/20 flex items-center justify-center">
              <StarOutlined className="text-xl text-violet-400" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-white">申请导师指导</h3>
              <p className="text-sm text-slate-400">
                向 {mentor.full_name || mentor.username} 发送申请
              </p>
            </div>
          </div>
        }
        open={isModalOpen}
        onCancel={() => setIsModalOpen(false)}
        footer={null}
        centered
        className="mentorship-modal"
        styles={{
          content: {
            background: 'linear-gradient(135deg, rgb(30, 41, 59) 0%, rgb(15, 23, 42) 100%)',
            border: '1px solid rgba(100, 116, 139, 0.3)',
            borderRadius: '1rem',
          },
          header: {
            background: 'transparent',
            borderBottom: '1px solid rgba(100, 116, 139, 0.2)',
            paddingBottom: '1rem',
          },
        }}
      >
        <div className="py-4">
          <label className="block text-sm font-medium text-slate-300 mb-2">
            申请留言 <span className="text-slate-500">(可选)</span>
          </label>
          <TextArea
            value={applicationMessage}
            onChange={(e) => setApplicationMessage(e.target.value)}
            placeholder="介绍一下自己，为什么想要选择这位导师..."
            rows={4}
            maxLength={500}
            showCount
            className="bg-slate-800/50 border-slate-700 text-white placeholder:text-slate-500"
          />
        </div>

        <div className="flex gap-3 pt-4 border-t border-slate-700/50">
          <Button
            onClick={() => setIsModalOpen(false)}
            className="flex-1 bg-slate-700 border-slate-600 text-slate-300 hover:bg-slate-600 hover:border-slate-500"
          >
            取消
          </Button>
          <Button
            type="primary"
            onClick={handleApply}
            loading={isSubmitting}
            className="flex-1 bg-gradient-to-r from-violet-600 to-purple-600 border-none hover:from-violet-500 hover:to-purple-500"
          >
            提交申请
          </Button>
        </div>
      </Modal>
    </>
  )
}

// 导师网格组件
interface MentorGridProps {
  mentors: UserBrief[]
  onApplySuccess?: () => void
  disabled?: boolean
}

export const MentorGrid = ({ mentors, onApplySuccess, disabled }: MentorGridProps) => {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
      <AnimatePresence mode="popLayout">
        {mentors.map((mentor, index) => (
          <motion.div
            key={mentor.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            transition={{ delay: index * 0.05 }}
          >
            <MentorCard
              mentor={mentor}
              onApplySuccess={onApplySuccess}
              disabled={disabled}
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
