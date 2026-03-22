import React, { useEffect, useState } from 'react'
import {
  BarChartOutlined,
  BookOutlined,
  CheckCircleOutlined,
  CodeOutlined,
  ClockCircleOutlined,
  CrownOutlined,
  DatabaseOutlined,
  DownloadOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  LinkOutlined,
  MessageOutlined,
  NotificationOutlined,
  ReadOutlined,
  ReloadOutlined,
  RobotOutlined,
  SearchOutlined,
  ShareAltOutlined,
  StopOutlined,
  SyncOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UserOutlined,
  UsergroupAddOutlined,
} from '@ant-design/icons'
import { Button, Card, Col, Empty, Input, List, Progress, Row, Segmented, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { motion } from 'framer-motion'

import { adminApi, type AdminAuditLogItem, type StatisticsDetailItem } from '@/services/api'
import { useRoleStore } from '@/stores/roleStore'

const { Paragraph, Text, Title } = Typography

const sectionCardClass =
  '!overflow-hidden !rounded-[28px] !border !border-white/[0.06] !bg-slate-900/50 !shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_28px_60px_rgba(2,6,23,0.34)] backdrop-blur-2xl'

const insetPanelClass =
  'rounded-[22px] border border-white/[0.06] bg-white/[0.03] shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]'

const actionButtonClass =
  '!h-10 !rounded-2xl !border-white/10 !bg-white/[0.04] !px-4 !text-slate-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] hover:!border-white/15 hover:!bg-white/[0.08] hover:!text-white'

const inputControlClass =
  '!rounded-2xl !border border-white/10 !bg-white/[0.04] !text-slate-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.02)] transition-all hover:!border-white/15 hover:!bg-white/[0.06] focus-within:!border-white/20 focus-within:!bg-white/[0.06] focus-within:!shadow-[0_0_0_2px_rgba(255,255,255,0.05)] [&_.ant-input]:!bg-transparent [&_.ant-input-clear-icon]:!text-slate-500 hover:[&_.ant-input-clear-icon]:!text-slate-300'

const sectionBodyStyle = {
  padding: 20,
}

const progressTrailColor = 'rgba(148, 163, 184, 0.12)'

const toneHex = {
  sky: '#38bdf8',
  cyan: '#22d3ee',
  emerald: '#34d399',
  teal: '#2dd4bf',
  slate: '#94a3b8',
  violet: '#a78bfa',
  rose: '#fb7185',
  amber: '#fbbf24',
  red: '#f87171',
} as const

type AccentTone = keyof typeof toneHex

const toneClassMap: Record<
  AccentTone,
  { shell: string; text: string; icon: string; badge: string; soft: string }
> = {
  sky: {
    shell: 'border-sky-400/15 bg-sky-400/10',
    text: 'text-sky-200',
    icon: 'text-sky-300',
    badge: 'border-sky-400/15 bg-sky-400/10 !text-sky-200',
    soft: 'from-sky-400/10 to-sky-400/[0.03]',
  },
  cyan: {
    shell: 'border-cyan-400/15 bg-cyan-400/10',
    text: 'text-cyan-200',
    icon: 'text-cyan-300',
    badge: 'border-cyan-400/15 bg-cyan-400/10 !text-cyan-200',
    soft: 'from-cyan-400/10 to-cyan-400/[0.03]',
  },
  emerald: {
    shell: 'border-emerald-400/15 bg-emerald-400/10',
    text: 'text-emerald-200',
    icon: 'text-emerald-300',
    badge: 'border-emerald-400/15 bg-emerald-400/10 !text-emerald-200',
    soft: 'from-emerald-400/10 to-emerald-400/[0.03]',
  },
  teal: {
    shell: 'border-teal-400/15 bg-teal-400/10',
    text: 'text-teal-200',
    icon: 'text-teal-300',
    badge: 'border-teal-400/15 bg-teal-400/10 !text-teal-200',
    soft: 'from-teal-400/10 to-teal-400/[0.03]',
  },
  slate: {
    shell: 'border-white/10 bg-white/[0.06]',
    text: 'text-slate-200',
    icon: 'text-slate-300',
    badge: 'border-white/10 bg-white/[0.06] !text-slate-300',
    soft: 'from-white/[0.08] to-white/[0.03]',
  },
  violet: {
    shell: 'border-violet-400/15 bg-violet-400/10',
    text: 'text-violet-200',
    icon: 'text-violet-300',
    badge: 'border-violet-400/15 bg-violet-400/10 !text-violet-200',
    soft: 'from-violet-400/10 to-violet-400/[0.03]',
  },
  rose: {
    shell: 'border-rose-400/15 bg-rose-400/10',
    text: 'text-rose-200',
    icon: 'text-rose-300',
    badge: 'border-rose-400/15 bg-rose-400/10 !text-rose-200',
    soft: 'from-rose-400/10 to-rose-400/[0.03]',
  },
  amber: {
    shell: 'border-amber-400/15 bg-amber-400/10',
    text: 'text-amber-200',
    icon: 'text-amber-300',
    badge: 'border-amber-400/15 bg-amber-400/10 !text-amber-200',
    soft: 'from-amber-400/10 to-amber-400/[0.03]',
  },
  red: {
    shell: 'border-red-400/15 bg-red-400/10',
    text: 'text-red-200',
    icon: 'text-red-300',
    badge: 'border-red-400/15 bg-red-400/10 !text-red-200',
    soft: 'from-red-400/10 to-red-400/[0.03]',
  },
}

const GlassTag = ({
  children,
  tone = 'slate',
  icon,
}: {
  children: React.ReactNode
  tone?: AccentTone
  icon?: React.ReactNode
}) => (
  <Tag
    bordered={false}
    icon={icon}
    className={`m-0 rounded-full border px-2.5 py-1 text-[11px] font-medium ${toneClassMap[tone].badge}`}
  >
    {children}
  </Tag>
)

const MiniMetric = ({
  label,
  value,
  tone,
  icon,
}: {
  label: string
  value: number
  tone: AccentTone
  icon: React.ReactNode
}) => {
  const toneClass = toneClassMap[tone]

  return (
    <div className={`h-full rounded-[22px] border border-white/[0.06] bg-gradient-to-b p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] ${toneClass.soft}`}>
      <div className={`flex items-center gap-2 text-sm ${toneClass.icon}`}>
        {icon}
        <span className="text-[11px] font-semibold uppercase tracking-[0.18em]">{label}</span>
      </div>
      <div className="mt-3 text-[1.75rem] font-semibold tracking-tight text-slate-50">{value}</div>
    </div>
  )
}

const percent = (value: number, total: number) => {
  if (total <= 0) return 0
  return Number(((value / total) * 100).toFixed(1))
}

const formatDayLabel = (date: string) => date.slice(5)
const formatDateTime = (value: string) => new Date(value).toLocaleString('zh-CN')

const DETAIL_ENTITY_OPTIONS = [
  { label: '研究组', value: 'groups' },
  { label: '共享', value: 'shares' },
  { label: '邀请', value: 'invitations' },
  { label: '公告', value: 'announcements' },
] as const

type DetailEntity = (typeof DETAIL_ENTITY_OPTIONS)[number]['value']

const AUDIT_ACTION_OPTIONS = [
  { label: '创建用户', value: 'create_user' },
  { label: '更新用户', value: 'update_user' },
  { label: '更新用户信息', value: 'update_user_info' },
  { label: '修改角色', value: 'update_user_role' },
  { label: '重置密码', value: 'update_user_password' },
  { label: '切换状态', value: 'toggle_user_active' },
  { label: '删除用户', value: 'delete_user' },
  { label: '导出总览', value: 'export_statistics_summary' },
  { label: '导出明细', value: 'export_statistics_details' },
  { label: '导出审计', value: 'export_statistics_audit' },
] as const

const getAuditActionLabel = (action: string) =>
  AUDIT_ACTION_OPTIONS.find((item) => item.value === action)?.label || action

const MetricCard = ({
  title,
  value,
  subtitle,
  tone,
  icon,
}: {
  title: string
  value: number | string
  subtitle: string
  tone: AccentTone
  icon: React.ReactNode
}) => {
  const toneClass = toneClassMap[tone]

  return (
    <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Text className="text-[11px] font-semibold uppercase tracking-[0.18em] !text-slate-500">{title}</Text>
          <div className="mt-3 text-[2rem] font-semibold tracking-tight text-slate-50">{value}</div>
          <Paragraph className="!mb-0 !mt-3 !text-sm !leading-6 !text-slate-400">{subtitle}</Paragraph>
        </div>
        <div
          className={`grid h-12 w-12 place-items-center rounded-2xl border shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] ${toneClass.shell} ${toneClass.icon}`}
        >
          <span className="text-lg">{icon}</span>
        </div>
      </div>
    </Card>
  )
}

const MetricChip = ({
  label,
  value,
  tone,
  icon,
}: {
  label: string
  value: number
  tone: AccentTone
  icon: React.ReactNode
}) => {
  const toneClass = toneClassMap[tone]

  return (
    <div className={`min-h-[92px] rounded-[20px] border bg-gradient-to-b px-4 py-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] ${toneClass.shell} ${toneClass.soft}`}>
      <div className={`flex items-center gap-2 ${toneClass.icon}`}>
        {icon}
        <Text className={`!text-[11px] !font-semibold !uppercase !tracking-[0.18em] ${toneClass.text}`}>{label}</Text>
      </div>
      <div className="mt-3 text-[1.65rem] font-semibold tracking-tight text-slate-50">{value}</div>
    </div>
  )
}

const TrendBars = ({
  title,
  tone,
  data,
  days,
}: {
  title: string
  tone: AccentTone
  data: Array<{ date: string; count: number }>
  days: number
}) => {
  const maxValue = Math.max(...data.map((item) => item.count), 1)
  const color = toneHex[tone]

  return (
    <div className={`${insetPanelClass} bg-slate-950/35 p-4`}>
      <div className="flex items-baseline justify-between gap-4">
        <Text className="!text-sm !font-semibold !text-slate-100">{title}</Text>
        <Text className="!text-[11px] !font-semibold !uppercase !tracking-[0.18em] !text-slate-500">近 {days} 天</Text>
      </div>
      <div
        className="mt-4 grid min-h-[118px] items-end gap-3"
        style={{
          gridTemplateColumns: `repeat(${Math.max(data.length, 1)}, minmax(0, 1fr))`,
        }}
      >
        {data.map((item, index) => (
          <div key={`${title}-${item.date}`} className="flex flex-col items-center gap-2">
            <Text className="!text-[11px] !text-slate-500">{item.count}</Text>
            <div className="flex h-[76px] w-full items-end">
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: Math.max(12, Math.round((item.count / maxValue) * 76)), opacity: 1 }}
                transition={{ type: 'spring', stiffness: 180, damping: 18, delay: index * 0.03 }}
                className="w-full rounded-full"
                style={{
                  background: `linear-gradient(180deg, ${color} 0%, ${color}8f 100%)`,
                  boxShadow: `0 10px 24px ${color}20`,
                }}
              />
            </div>
            <Text className="!text-[11px] !text-slate-600">{formatDayLabel(item.date)}</Text>
          </div>
        ))}
      </div>
    </div>
  )
}

const DetailTitle = ({ title, subtitle }: { title: string; subtitle?: string | null }) => (
  <div>
    <Text className="!text-slate-100">{title}</Text>
    <Paragraph className="!m-0 !mt-1 !text-slate-500">{subtitle || '-'}</Paragraph>
  </div>
)

const toneTagFromStatus = (value?: string | null): AccentTone => {
  if (value === 'active' || value === 'accepted' || value === 'completed') return 'emerald'
  if (value === 'pending' || value === 'pinned') return 'amber'
  if (value === 'rejected' || value === 'failed' || value === 'inactive') return 'red'
  if (value === 'invite') return 'cyan'
  return 'slate'
}

const toneTagFromIndex = (index: number): AccentTone => {
  if (index === 0) return 'amber'
  if (index === 1) return 'sky'
  if (index === 2) return 'slate'
  return 'slate'
}

const renderStatusTag = (label: string, tone: AccentTone) => <GlassTag tone={tone}>{label}</GlassTag>

const renderTableCellText = (value?: string | null, tone: 'primary' | 'secondary' | 'muted' = 'secondary') => {
  const className =
    tone === 'primary'
      ? '!text-slate-100'
      : tone === 'muted'
        ? '!text-slate-500'
        : '!text-slate-400'

  return <Text className={className}>{value || '-'}</Text>
}

const StatisticsPage: React.FC = () => {
  const { statistics, statisticsLoading, fetchStatistics } = useRoleStore()
  const [windowDays, setWindowDays] = useState<number>(7)
  const [detailEntity, setDetailEntity] = useState<DetailEntity>('groups')
  const [detailPage, setDetailPage] = useState(1)
  const [detailSearch, setDetailSearch] = useState('')
  const [detailStatus, setDetailStatus] = useState<string | undefined>(undefined)
  const [detailCategory, setDetailCategory] = useState<string | undefined>(undefined)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailTotal, setDetailTotal] = useState(0)
  const [detailItems, setDetailItems] = useState<StatisticsDetailItem[]>([])
  const [auditPage, setAuditPage] = useState(1)
  const [auditAction, setAuditAction] = useState<string | undefined>(undefined)
  const [auditSearch, setAuditSearch] = useState('')
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditItems, setAuditItems] = useState<AdminAuditLogItem[]>([])
  const [exportingKey, setExportingKey] = useState<string | null>(null)

  useEffect(() => {
    fetchStatistics(windowDays)
  }, [fetchStatistics, windowDays])

  useEffect(() => {
    setDetailPage(1)
    setDetailStatus(undefined)
    setDetailCategory(undefined)
    setDetailSearch('')
  }, [detailEntity])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setDetailLoading(true)
      try {
        const response = await adminApi.getStatisticsDetails({
          entity: detailEntity,
          page: detailPage,
          page_size: 10,
          search: detailSearch || undefined,
          status: detailStatus,
          category: detailCategory,
        })
        if (!cancelled) {
          setDetailItems(response.items)
          setDetailTotal(response.total)
        }
      } catch (error) {
        if (!cancelled) {
          setDetailItems([])
          setDetailTotal(0)
        }
        // eslint-disable-next-line no-console
        console.error('加载统计明细失败:', error)
      } finally {
        if (!cancelled) {
          setDetailLoading(false)
        }
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [detailCategory, detailEntity, detailPage, detailSearch, detailStatus])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setAuditLoading(true)
      try {
        const response = await adminApi.getAuditLogs({
          page: auditPage,
          page_size: 10,
          action: auditAction,
          search: auditSearch || undefined,
        })
        if (!cancelled) {
          setAuditItems(response.items)
          setAuditTotal(response.total)
        }
      } catch (error) {
        if (!cancelled) {
          setAuditItems([])
          setAuditTotal(0)
        }
        // eslint-disable-next-line no-console
        console.error('加载审计日志失败:', error)
      } finally {
        if (!cancelled) {
          setAuditLoading(false)
        }
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [auditAction, auditPage, auditSearch])

  const downloadExport = async (
    key: string,
    params: Parameters<typeof adminApi.exportStatistics>[0],
    filename: string,
  ) => {
    setExportingKey(key)
    try {
      const blob = await adminApi.exportStatistics(params)
      const objectUrl = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(objectUrl)
    } catch (error) {
      // eslint-disable-next-line no-console
      console.error('导出统计失败:', error)
    } finally {
      setExportingKey((current) => (current === key ? null : current))
    }
  }

  if (statisticsLoading && !statistics) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950">
        <Spin size="large" />
      </div>
    )
  }

  if (!statistics) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950">
        <Empty description="暂无统计数据" />
      </div>
    )
  }

  const activity = statistics.activity ?? {
    new_users_last_7_days: 0,
    new_conversations_last_7_days: 0,
    new_knowledge_bases_last_7_days: 0,
    new_papers_last_7_days: 0,
    new_notebooks_last_7_days: 0,
  }
  const mentorship = statistics.mentorship ?? {
    students_with_mentor: statistics.students_with_mentor || 0,
    students_without_mentor: statistics.students_without_mentor || 0,
  }
  const collaboration = statistics.collaboration ?? {
    total_groups: statistics.total_groups || 0,
    active_groups: 0,
    total_group_members: 0,
    pending_invitations: statistics.pending_invitations || 0,
    total_shared_resources: statistics.total_shared_resources || 0,
    total_announcements: statistics.total_announcements || 0,
    active_announcements: 0,
  }
  const documentPipeline = statistics.document_pipeline ?? {
    total_documents: statistics.total_documents || 0,
    completed_documents: 0,
    running_documents: 0,
    failed_documents: 0,
    pending_documents: 0,
    timeout_documents: 0,
    cancelled_documents: 0,
  }
  const trends = statistics.trends_7d ?? {
    users: [],
    conversations: [],
    knowledge_bases: [],
    papers: [],
    notebooks: [],
  }
  const shareBreakdown = statistics.share_breakdown ?? []
  const invitationBreakdown = statistics.invitation_breakdown ?? []
  const topMentors = statistics.top_mentors ?? []
  const recentActivity = statistics.recent_activity ?? []
  const aiRag = statistics.ai_rag ?? {
    assistant_messages_last_window: 0,
    rag_messages_last_window: 0,
    knowledge_search_calls_last_window: 0,
    citation_required_answers_last_window: 0,
    citation_valid_answers_last_window: 0,
    citation_repair_attempts_last_window: 0,
    citation_repair_successes_last_window: 0,
    compression_calls_last_window: 0,
    compression_fallback_chunks_last_window: 0,
    assistant_total_tokens_last_window: 0,
    agent_runs_last_window: 0,
    successful_agent_runs_last_window: 0,
  }
  const codelab = statistics.codelab ?? {
    notebooks_active_last_window: 0,
    executed_notebooks: 0,
    total_execution_count: 0,
    code_cells: 0,
    executed_code_cells: 0,
    agent_runs_last_window: 0,
    agent_tokens_last_window: 0,
  }
  const literature = statistics.literature ?? {
    total_collections: 0,
    active_read_sessions_last_window: 0,
    annotations_last_window: 0,
    comments_last_window: 0,
    ratings_last_window: 0,
    qa_sessions_last_window: 0,
    qa_messages_last_window: 0,
    knowledge_links_total: 0,
    knowledge_link_breakdown: [],
  }
  const knowledgeLinkBreakdown = literature.knowledge_link_breakdown ?? []
  const detailStatusOptions =
    detailEntity === 'groups'
      ? [
          { label: '启用', value: 'active' },
          { label: '停用', value: 'inactive' },
        ]
      : detailEntity === 'invitations'
        ? invitationBreakdown.map((item) => ({ label: item.label, value: item.key }))
        : detailEntity === 'announcements'
          ? [
              { label: '启用', value: 'active' },
              { label: '停用', value: 'inactive' },
            ]
          : []
  const detailCategoryOptions =
    detailEntity === 'shares'
      ? shareBreakdown.map((item) => ({ label: item.label, value: item.key }))
      : detailEntity === 'invitations'
        ? [
            { label: '导师邀请', value: 'invite' },
            { label: '学生申请', value: 'apply' },
          ]
        : detailEntity === 'announcements'
          ? [
              { label: '置顶', value: 'pinned' },
              { label: '普通', value: 'normal' },
            ]
          : []

  const detailColumns: ColumnsType<StatisticsDetailItem> =
    detailEntity === 'groups'
      ? [
          {
            title: '研究组',
            dataIndex: 'title',
            key: 'title',
            render: (_, record) => <DetailTitle title={record.title} subtitle={record.subtitle} />,
          },
          {
            title: '导师',
            dataIndex: 'owner_name',
            key: 'owner_name',
            render: (value) => renderTableCellText(value),
          },
          {
            title: '成员数',
            dataIndex: 'member_count',
            key: 'member_count',
          },
          {
            title: '状态',
            dataIndex: 'status',
            key: 'status',
            render: (value) => renderStatusTag(value === 'active' ? '启用' : '停用', toneTagFromStatus(value)),
          },
          {
            title: '更新时间',
            dataIndex: 'updated_at',
            key: 'updated_at',
            render: (value) => renderTableCellText(value ? formatDateTime(value) : '-', 'muted'),
          },
        ]
      : detailEntity === 'shares'
        ? [
            {
              title: '资源',
              dataIndex: 'title',
              key: 'title',
              render: (_, record) => <DetailTitle title={record.title} subtitle={record.subtitle} />,
            },
            {
              title: '所有者',
              dataIndex: 'owner_name',
              key: 'owner_name',
              render: (value) => renderTableCellText(value),
            },
            {
              title: '共享目标',
              dataIndex: 'target_name',
              key: 'target_name',
              render: (value) => renderTableCellText(value),
            },
            {
              title: '权限',
              dataIndex: 'permission',
              key: 'permission',
              render: (value) => renderStatusTag(value || '-', 'sky'),
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              key: 'created_at',
              render: (value) => renderTableCellText(value ? formatDateTime(value) : '-', 'muted'),
            },
          ]
        : detailEntity === 'invitations'
          ? [
              {
                title: '邀请链路',
                dataIndex: 'title',
                key: 'title',
                render: (_, record) => <DetailTitle title={record.title} subtitle={record.subtitle} />,
              },
              {
                title: '类型',
                dataIndex: 'category',
                key: 'category',
                render: (value) => renderStatusTag(value === 'invite' ? '邀请' : '申请', toneTagFromStatus(value)),
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                render: (value) => renderStatusTag(value || '-', toneTagFromStatus(value)),
              },
              {
                title: '发起人',
                dataIndex: 'owner_name',
                key: 'owner_name',
                render: (value) => renderTableCellText(value),
              },
              {
                title: '目标',
                dataIndex: 'target_name',
                key: 'target_name',
                render: (value) => renderTableCellText(value),
              },
              {
                title: '创建时间',
                dataIndex: 'created_at',
                key: 'created_at',
                render: (value) => renderTableCellText(value ? formatDateTime(value) : '-', 'muted'),
              },
            ]
          : [
              {
                title: '公告',
                dataIndex: 'title',
                key: 'title',
                render: (_, record) => <DetailTitle title={record.title} subtitle={record.subtitle} />,
              },
              {
                title: '导师',
                dataIndex: 'owner_name',
                key: 'owner_name',
                render: (value) => renderTableCellText(value),
              },
              {
                title: '类别',
                dataIndex: 'category',
                key: 'category',
                render: (value) => renderStatusTag(value === 'pinned' ? '置顶' : '普通', toneTagFromStatus(value)),
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                render: (value) => renderStatusTag(value === 'active' ? '启用' : '停用', toneTagFromStatus(value)),
              },
              {
                title: '更新时间',
                dataIndex: 'updated_at',
                key: 'updated_at',
                render: (value) => renderTableCellText(value ? formatDateTime(value) : '-', 'muted'),
              },
            ]

  const auditColumns: ColumnsType<AdminAuditLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 190,
      render: (value) => renderTableCellText(formatDateTime(value), 'muted'),
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 150,
      render: (value) => renderStatusTag(getAuditActionLabel(value), 'sky'),
    },
    {
      title: '管理员',
      dataIndex: 'admin_name',
      key: 'admin_name',
      width: 140,
      render: (value) => renderTableCellText(value, 'primary'),
    },
    {
      title: '对象',
      key: 'target',
      width: 180,
      render: (_, record) => (
        <Text className="!text-slate-400">
          {[record.target_type, record.target_id].filter(Boolean).join(' / ') || '-'}
        </Text>
      ),
    },
    {
      title: '摘要',
      dataIndex: 'summary',
      key: 'summary',
      render: (value) => renderTableCellText(value, 'primary'),
    },
  ]

  const activeRate = percent(statistics.active_users, statistics.total_users)
  const mentorCoverage = percent(mentorship.students_with_mentor, statistics.student_count)
  const documentCompletionRate = percent(
    documentPipeline.completed_documents,
    documentPipeline.total_documents,
  )
  const aiRunSuccessRate = percent(aiRag.successful_agent_runs_last_window, aiRag.agent_runs_last_window)
  const citationValidityRate = percent(
    aiRag.citation_valid_answers_last_window,
    aiRag.citation_required_answers_last_window,
  )
  const citationRepairRate = percent(
    aiRag.citation_repair_successes_last_window,
    aiRag.citation_repair_attempts_last_window,
  )
  const codelabCellCoverage = percent(codelab.executed_code_cells, codelab.code_cells)
  const completedKnowledgeLinks =
    knowledgeLinkBreakdown.find((item) => item.key === 'completed')?.count ?? 0
  const knowledgeLinkCompletionRate = percent(completedKnowledgeLinks, literature.knowledge_links_total)

  return (
    <div className="statistics-page min-h-full bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.14),transparent_28%),radial-gradient(circle_at_top_right,rgba(34,211,238,0.12),transparent_24%),linear-gradient(180deg,#020617_0%,#030712_48%,#0f172a_100%)] p-4 sm:p-6">
      <Card bordered={false} className={`${sectionCardClass} mb-6`} styles={{ body: { padding: 24 } }}>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 items-start gap-4">
            <div className="grid h-12 w-12 place-items-center rounded-[18px] border border-emerald-400/15 bg-gradient-to-br from-emerald-400/12 via-cyan-400/10 to-transparent text-xl text-emerald-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
              <BarChartOutlined />
            </div>
            <div className="min-w-0">
              <Text className="!text-[11px] !font-semibold !uppercase !tracking-[0.22em] !text-slate-500">Operations Pulse</Text>
              <Title level={3} className="!mb-0 !mt-2 !text-slate-50">
                系统统计
              </Title>
              <Paragraph className="!mb-0 !mt-2 !max-w-3xl !text-slate-400">
                管理员视角的运营概览，覆盖用户、资源、协作链路、导师分布和近期活动。
              </Paragraph>
            </div>
          </div>
          <Space wrap>
          <Segmented
            className="statistics-page__segmented"
            value={windowDays}
            options={[
              { label: '近 7 天', value: 7 },
              { label: '近 30 天', value: 30 },
              { label: '近 90 天', value: 90 },
            ]}
            onChange={(value) => setWindowDays(Number(value))}
          />
          <Button className={actionButtonClass} icon={<ReloadOutlined />} onClick={() => fetchStatistics(windowDays)}>
            刷新数据
          </Button>
          <Button
            className={actionButtonClass}
            icon={<DownloadOutlined />}
            loading={exportingKey === 'summary'}
            onClick={() =>
              downloadExport(
                'summary',
                { scope: 'summary', days: windowDays },
                `system-statistics-summary-${windowDays}d.csv`,
              )
            }
          >
            导出总览
          </Button>
          </Space>
        </div>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title="用户总量"
            value={statistics.total_users}
            subtitle={`活跃 ${statistics.active_users} / 非活跃 ${statistics.inactive_users}`}
            tone="sky"
            icon={<TeamOutlined />}
          />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title="资源总量"
            value={
              statistics.total_knowledge_bases +
              statistics.total_documents +
              statistics.total_papers +
              statistics.total_notebooks
            }
            subtitle={`知识库 ${statistics.total_knowledge_bases}，文档 ${statistics.total_documents}`}
            tone="cyan"
            icon={<DatabaseOutlined />}
          />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title="协作链路"
            value={statistics.total_groups + statistics.total_shared_resources + statistics.total_announcements}
            subtitle={`研究组 ${statistics.total_groups}，共享 ${statistics.total_shared_resources}`}
            tone="emerald"
            icon={<ShareAltOutlined />}
          />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title={`近 ${statistics.time_window_days} 天新增`}
            value={activity.new_users_last_7_days}
            subtitle={`对话 ${activity.new_conversations_last_7_days}，Notebook ${activity.new_notebooks_last_7_days}`}
            tone="teal"
            icon={<ThunderboltOutlined />}
          />
        </Col>

        <Col xs={24} xl={10}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                用户与导师制
              </Title>
              <GlassTag tone="sky">活跃率 {activeRate}%</GlassTag>
            </div>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div>
                <div className="mb-2 flex justify-between">
                  <Text className="!text-slate-400">管理员</Text>
                  <Text className="!text-sky-300">{statistics.admin_count}</Text>
                </div>
                <Progress percent={percent(statistics.admin_count, statistics.total_users)} showInfo={false} strokeColor={toneHex.sky} trailColor={progressTrailColor} />
              </div>
              <div>
                <div className="mb-2 flex justify-between">
                  <Text className="!text-slate-400">导师</Text>
                  <Text className="!text-cyan-300">{statistics.mentor_count}</Text>
                </div>
                <Progress percent={percent(statistics.mentor_count, statistics.total_users)} showInfo={false} strokeColor={toneHex.cyan} trailColor={progressTrailColor} />
              </div>
              <div>
                <div className="mb-2 flex justify-between">
                  <Text className="!text-slate-400">学生</Text>
                  <Text className="!text-emerald-300">{statistics.student_count}</Text>
                </div>
                <Progress percent={percent(statistics.student_count, statistics.total_users)} showInfo={false} strokeColor={toneHex.emerald} trailColor={progressTrailColor} />
              </div>
            </Space>
            <Row gutter={[12, 12]} className="mt-[18px]">
              <Col span={12}>
                <MetricChip
                  label="已绑定导师学生"
                  value={mentorship.students_with_mentor}
                  tone="emerald"
                  icon={<LinkOutlined />}
                />
              </Col>
              <Col span={12}>
                <MetricChip
                  label="待分配学生"
                  value={mentorship.students_without_mentor}
                  tone="teal"
                  icon={<UserOutlined />}
                />
              </Col>
            </Row>
            <Paragraph className="!mb-0 !mt-4 !text-slate-500">
              学生导师覆盖率 {mentorCoverage}%。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              资源总览
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="对话" value={statistics.total_conversations} tone="sky" icon={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="知识库" value={statistics.total_knowledge_bases} tone="cyan" icon={<DatabaseOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="文档" value={statistics.total_documents} tone="teal" icon={<FileTextOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="论文" value={statistics.total_papers} tone="violet" icon={<BookOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="Notebook" value={statistics.total_notebooks} tone="slate" icon={<ReadOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="共享资源" value={statistics.total_shared_resources} tone="emerald" icon={<ShareAltOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              协作运营
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12}>
                <MetricChip label="研究组" value={collaboration.total_groups} tone="sky" icon={<UsergroupAddOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="活跃研究组" value={collaboration.active_groups} tone="emerald" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="组成员关系" value={collaboration.total_group_members} tone="violet" icon={<TeamOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="待处理邀请" value={collaboration.pending_invitations} tone="teal" icon={<ClockCircleOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="公告总数" value={collaboration.total_announcements} tone="slate" icon={<NotificationOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="启用公告" value={collaboration.active_announcements} tone="cyan" icon={<NotificationOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                文档处理状态
              </Title>
              <GlassTag tone="emerald">完成率 {documentCompletionRate}%</GlassTag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="已完成" value={documentPipeline.completed_documents} tone="emerald" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="处理中" value={documentPipeline.running_documents} tone="sky" icon={<SyncOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="待处理" value={documentPipeline.pending_documents} tone="slate" icon={<ClockCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="失败" value={documentPipeline.failed_documents} tone="red" icon={<StopOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="超时" value={documentPipeline.timeout_documents} tone="rose" icon={<ThunderboltOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="取消" value={documentPipeline.cancelled_documents} tone="slate" icon={<StopOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              近 {statistics.time_window_days} 天新增
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8} xl={4}>
                <MiniMetric label="新增用户" value={activity.new_users_last_7_days} tone="sky" icon={<TeamOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <MiniMetric label="新增对话" value={activity.new_conversations_last_7_days} tone="cyan" icon={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <MiniMetric label="新增知识库" value={activity.new_knowledge_bases_last_7_days} tone="emerald" icon={<DatabaseOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <MiniMetric label="新增论文" value={activity.new_papers_last_7_days} tone="violet" icon={<BookOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <MiniMetric label="新增 Notebook" value={activity.new_notebooks_last_7_days} tone="slate" icon={<ReadOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              近 {statistics.time_window_days} 天趋势
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="用户" tone="sky" data={trends.users} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="对话" tone="cyan" data={trends.conversations} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="知识库" tone="emerald" data={trends.knowledge_bases} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="论文" tone="violet" data={trends.papers} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="Notebook" tone="slate" data={trends.notebooks} days={statistics.time_window_days} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={10}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              共享与邀请分布
            </Title>
            <Text className="!mb-[10px] !block !text-slate-400">共享资源类型</Text>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {shareBreakdown.length > 0 ? shareBreakdown.map((item) => (
                <div key={`share-${item.key}`}>
                  <div className="mb-1.5 flex justify-between">
                    <Text className="!text-slate-200">{item.label}</Text>
                    <Text className="!text-emerald-300">{item.count}</Text>
                  </div>
                  <Progress percent={percent(item.count, statistics.total_shared_resources)} showInfo={false} strokeColor={toneHex.emerald} trailColor={progressTrailColor} />
                </div>
              )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无共享资源" />}
            </Space>
            <Text className="!mb-[10px] !mt-[18px] !block !text-slate-400">邀请状态</Text>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {invitationBreakdown.length > 0 ? invitationBreakdown.map((item) => (
                <div key={`invitation-${item.key}`}>
                  <div className="mb-1.5 flex justify-between">
                    <Text className="!text-slate-200">{item.label}</Text>
                    <Text className="!text-cyan-300">{item.count}</Text>
                  </div>
                  <Progress percent={percent(item.count, invitationBreakdown.reduce((sum, current) => sum + current.count, 0))} showInfo={false} strokeColor={toneHex.cyan} trailColor={progressTrailColor} />
                </div>
              )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无邀请记录" />}
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              导师排行
            </Title>
            <List
              split={false}
              dataSource={topMentors}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无导师数据" /> }}
              renderItem={(item, index) => (
                <List.Item className="!my-1 !rounded-[22px] !border !border-transparent !px-4 !py-3 transition-all duration-200 hover:!border-white/[0.06] hover:!bg-white/[0.04]">
                  <div className="flex w-full items-center justify-between gap-3">
                    <div>
                      <Space size="middle">
                        <GlassTag tone={toneTagFromIndex(index)} icon={<CrownOutlined />}>
                          TOP {index + 1}
                        </GlassTag>
                        <div>
                          <Text className="!text-[15px] !text-slate-100">
                            {item.full_name || item.username}
                          </Text>
                          <Paragraph className="!m-0 !mt-1 !text-slate-500">
                            @{item.username}
                          </Paragraph>
                        </div>
                      </Space>
                    </div>
                    <Space size="large">
                      <MiniMetric label="学生" value={item.student_count} tone="sky" icon={<TeamOutlined />} />
                      <MiniMetric label="研究组" value={item.group_count} tone="emerald" icon={<FolderOpenOutlined />} />
                    </Space>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <Title level={5} className="!mb-[18px] !mt-0 !text-slate-50">
              最近活动
            </Title>
            <List
              split={false}
              dataSource={recentActivity}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无近期活动" /> }}
              renderItem={(item) => (
                <List.Item className="!my-1 !rounded-[22px] !border !border-transparent !px-4 !py-3 transition-all duration-200 hover:!border-white/[0.06] hover:!bg-white/[0.04]">
                  <div className="flex w-full items-center justify-between gap-3">
                    <div>
                      <Space wrap>
                        <GlassTag tone="sky">{item.type}</GlassTag>
                        <Text className="!text-[15px] !text-slate-100">{item.title}</Text>
                      </Space>
                      <Paragraph className="!m-0 !mt-1.5 !text-slate-500">
                        {item.owner_name} · {item.owner_role}
                      </Paragraph>
                    </div>
                    <Text className="!text-slate-500">{formatDateTime(item.created_at)}</Text>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                AI / RAG 专项
              </Title>
              <GlassTag tone="sky">运行成功率 {aiRunSuccessRate}%</GlassTag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="Agent 调用" value={aiRag.agent_runs_last_window} tone="sky" icon={<RobotOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="RAG 回复" value={aiRag.rag_messages_last_window} tone="emerald" icon={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="检索调用" value={aiRag.knowledge_search_calls_last_window} tone="cyan" icon={<SearchOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="引用有效" value={aiRag.citation_valid_answers_last_window} tone="teal" icon={<LinkOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="压缩回退块" value={aiRag.compression_fallback_chunks_last_window} tone="rose" icon={<SyncOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="回复 Tokens" value={aiRag.assistant_total_tokens_last_window} tone="violet" icon={<ThunderboltOutlined />} />
              </Col>
            </Row>
            <Paragraph className="!mb-0 !mt-4 !text-slate-500">
              近 {statistics.time_window_days} 天引用有效率 {citationValidityRate}% ，修复成功率 {citationRepairRate}% ，压缩调用 {aiRag.compression_calls_last_window} 次。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered={false} className={`${sectionCardClass} h-full`} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                CodeLab 专项
              </Title>
              <GlassTag tone="emerald">代码单元覆盖率 {codelabCellCoverage}%</GlassTag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="活跃 Notebook" value={codelab.notebooks_active_last_window} tone="sky" icon={<ReadOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="已执行 Notebook" value={codelab.executed_notebooks} tone="emerald" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="总执行次数" value={codelab.total_execution_count} tone="cyan" icon={<ThunderboltOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="代码单元" value={codelab.code_cells} tone="violet" icon={<CodeOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="已执行单元" value={codelab.executed_code_cells} tone="teal" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="Agent Tokens" value={codelab.agent_tokens_last_window} tone="slate" icon={<RobotOutlined />} />
              </Col>
            </Row>
            <Paragraph className="!mb-0 !mt-4 !text-slate-500">
              近 {statistics.time_window_days} 天 CodeLab Agent 调用 {codelab.agent_runs_last_window} 次。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex flex-wrap items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                文献阅读专项
              </Title>
              <GlassTag tone="slate">知识链路完成率 {knowledgeLinkCompletionRate}%</GlassTag>
            </div>
            <Row gutter={[16, 16]}>
              <Col xs={24} xl={14}>
                <Row gutter={[12, 12]}>
                  <Col xs={12} md={8}>
                    <MetricChip label="文献集" value={literature.total_collections} tone="sky" icon={<FolderOpenOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="活跃阅读会话" value={literature.active_read_sessions_last_window} tone="emerald" icon={<ReadOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="批注" value={literature.annotations_last_window} tone="cyan" icon={<FileTextOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="评论" value={literature.comments_last_window} tone="violet" icon={<MessageOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="评分" value={literature.ratings_last_window} tone="slate" icon={<CrownOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="QA 会话" value={literature.qa_sessions_last_window} tone="teal" icon={<BookOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="QA 消息" value={literature.qa_messages_last_window} tone="slate" icon={<MessageOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="知识链路" value={literature.knowledge_links_total} tone="slate" icon={<LinkOutlined />} />
                  </Col>
                </Row>
              </Col>
              <Col xs={24} xl={10}>
                <Text className="!mb-[10px] !block !text-slate-400">论文入知识库状态</Text>
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  {knowledgeLinkBreakdown.length > 0 ? knowledgeLinkBreakdown.map((item) => (
                    <div key={`paper-link-${item.key}`}>
                      <div className="mb-1.5 flex justify-between">
                        <Text className="!text-slate-200">{item.label}</Text>
                        <Text className="!text-cyan-300">{item.count}</Text>
                      </div>
                      <Progress
                        percent={percent(item.count, literature.knowledge_links_total)}
                        showInfo={false}
                        strokeColor={toneHex.cyan}
                        trailColor={progressTrailColor}
                      />
                    </div>
                  )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无知识链路记录" />}
                </Space>
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex flex-wrap items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                明细下钻
              </Title>
              <Space wrap>
                <Button
                  className={actionButtonClass}
                  icon={<DownloadOutlined />}
                  loading={exportingKey === 'details'}
                  onClick={() =>
                    downloadExport(
                      'details',
                      {
                        scope: 'details',
                        entity: detailEntity,
                        status: detailStatus,
                        category: detailCategory,
                        search: detailSearch || undefined,
                      },
                      `system-statistics-details-${detailEntity}.csv`,
                    )
                  }
                >
                  导出当前明细
                </Button>
                <Segmented
                  className="statistics-page__segmented"
                  value={detailEntity}
                  options={DETAIL_ENTITY_OPTIONS.map((item) => ({ label: item.label, value: item.value }))}
                  onChange={(value) => setDetailEntity(value as DetailEntity)}
                />
                <Input
                  className={inputControlClass}
                  allowClear
                  value={detailSearch}
                  onChange={(event) => {
                    setDetailPage(1)
                    setDetailSearch(event.target.value)
                  }}
                  prefix={<SearchOutlined />}
                  placeholder="搜索标题、负责人或说明"
                  style={{ width: 240 }}
                />
                {detailStatusOptions.length > 0 && (
                  <Select
                    className="statistics-page__select"
                    allowClear
                    placeholder="状态"
                    value={detailStatus}
                    onChange={(value) => {
                      setDetailPage(1)
                      setDetailStatus(value)
                    }}
                    style={{ width: 140 }}
                    options={detailStatusOptions}
                  />
                )}
                {detailCategoryOptions.length > 0 && (
                  <Select
                    className="statistics-page__select"
                    allowClear
                    placeholder="类别"
                    value={detailCategory}
                    onChange={(value) => {
                      setDetailPage(1)
                      setDetailCategory(value)
                    }}
                    style={{ width: 160 }}
                    options={detailCategoryOptions}
                  />
                )}
              </Space>
            </div>
            <Table
              className="statistics-table"
              rowClassName={() => 'statistics-table__row'}
              rowKey="id"
              columns={detailColumns}
              dataSource={detailItems}
              loading={detailLoading}
              pagination={{
                current: detailPage,
                pageSize: 10,
                total: detailTotal,
                showSizeChanger: false,
                onChange: (nextPage) => setDetailPage(nextPage),
                showTotal: (total) => `共 ${total} 条`,
              }}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无明细数据" />,
              }}
              scroll={{ x: 960 }}
            />
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered={false} className={sectionCardClass} styles={{ body: sectionBodyStyle }}>
            <div className="mb-[18px] flex flex-wrap items-center justify-between gap-3">
              <Title level={5} className="!m-0 !text-slate-50">
                管理员审计
              </Title>
              <Space wrap>
                <Button
                  className={actionButtonClass}
                  icon={<DownloadOutlined />}
                  loading={exportingKey === 'audit'}
                  onClick={() =>
                    downloadExport(
                      'audit',
                      {
                        scope: 'audit',
                        action: auditAction,
                        search: auditSearch || undefined,
                      },
                      'system-statistics-audit.csv',
                    )
                  }
                >
                  导出审计
                </Button>
                <Input
                  className={inputControlClass}
                  allowClear
                  value={auditSearch}
                  onChange={(event) => {
                    setAuditPage(1)
                    setAuditSearch(event.target.value)
                  }}
                  prefix={<SearchOutlined />}
                  placeholder="搜索管理员、对象或摘要"
                  style={{ width: 240 }}
                />
                <Select
                  className="statistics-page__select"
                  allowClear
                  placeholder="操作类型"
                  value={auditAction}
                  onChange={(value) => {
                    setAuditPage(1)
                    setAuditAction(value)
                  }}
                  style={{ width: 180 }}
                  options={AUDIT_ACTION_OPTIONS.map((item) => ({ label: item.label, value: item.value }))}
                />
              </Space>
            </div>
            <Table
              className="statistics-table"
              rowClassName={() => 'statistics-table__row'}
              rowKey="id"
              columns={auditColumns}
              dataSource={auditItems}
              loading={auditLoading}
              pagination={{
                current: auditPage,
                pageSize: 10,
                total: auditTotal,
                showSizeChanger: false,
                onChange: (nextPage) => setAuditPage(nextPage),
                showTotal: (total) => `共 ${total} 条`,
              }}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无审计日志" />,
              }}
              scroll={{ x: 960 }}
            />
          </Card>
        </Col>
      </Row>
      <style>{`
        .statistics-page .ant-empty-description {
          color: rgb(100, 116, 139) !important;
        }
        .statistics-page__segmented {
          background: rgba(255, 255, 255, 0.05) !important;
          border: 1px solid rgba(255, 255, 255, 0.08) !important;
          border-radius: 16px !important;
          padding: 4px !important;
        }
        .statistics-page__segmented .ant-segmented-item {
          color: rgb(148, 163, 184) !important;
          border-radius: 12px !important;
          transition: all 0.2s ease !important;
        }
        .statistics-page__segmented .ant-segmented-item-selected {
          color: rgb(248, 250, 252) !important;
          background: rgba(255, 255, 255, 0.08) !important;
          box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05) !important;
        }
        .statistics-page .ant-input-affix-wrapper,
        .statistics-page__select .ant-select-selector {
          background: rgba(255, 255, 255, 0.04) !important;
          border: 1px solid rgba(255, 255, 255, 0.1) !important;
          border-radius: 16px !important;
          color: rgb(248, 250, 252) !important;
          box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        }
        .statistics-page .ant-input-affix-wrapper:hover,
        .statistics-page__select .ant-select-selector:hover {
          border-color: rgba(255, 255, 255, 0.14) !important;
        }
        .statistics-page .ant-input-affix-wrapper-focused,
        .statistics-page .ant-input-affix-wrapper-focused:hover,
        .statistics-page__select.ant-select-focused .ant-select-selector {
          border-color: rgba(255, 255, 255, 0.2) !important;
          background: rgba(255, 255, 255, 0.06) !important;
          box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 0 0 2px rgba(255, 255, 255, 0.04) !important;
        }
        .statistics-page .ant-input,
        .statistics-page .ant-input-prefix,
        .statistics-page__select .ant-select-selection-item,
        .statistics-page__select .ant-select-arrow {
          color: rgb(248, 250, 252) !important;
          background: transparent !important;
        }
        .statistics-page .ant-input::placeholder,
        .statistics-page__select .ant-select-selection-placeholder {
          color: rgb(100, 116, 139) !important;
        }
        .statistics-page .ant-progress-bg {
          box-shadow: 0 8px 18px rgba(15, 23, 42, 0.22) !important;
        }
        .statistics-table .ant-table {
          background: transparent !important;
        }
        .statistics-table .ant-table-container::before,
        .statistics-table .ant-table-container::after {
          display: none !important;
        }
        .statistics-table .ant-table-thead > tr > th {
          background: transparent !important;
          border-bottom: none !important;
          color: rgb(100, 116, 139) !important;
          font-size: 10px !important;
          font-weight: 700 !important;
          letter-spacing: 0.18em !important;
          padding: 0 16px 12px !important;
          text-transform: uppercase !important;
        }
        .statistics-table .ant-table-tbody > tr > td {
          background: transparent !important;
          border-bottom: none !important;
          padding: 14px 16px !important;
          transition: background-color 0.2s ease, box-shadow 0.2s ease !important;
        }
        .statistics-table .ant-table-tbody > tr > td:first-child {
          border-radius: 18px 0 0 18px !important;
        }
        .statistics-table .ant-table-tbody > tr > td:last-child {
          border-radius: 0 18px 18px 0 !important;
        }
        .statistics-table .ant-table-tbody > tr.statistics-table__row:hover > td {
          background: rgba(255, 255, 255, 0.05) !important;
          box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04) !important;
        }
        .statistics-table .ant-table-placeholder > td {
          background: transparent !important;
        }
        .statistics-table .ant-pagination {
          margin-top: 20px !important;
        }
        .statistics-table .ant-pagination .ant-pagination-item,
        .statistics-table .ant-pagination .ant-pagination-prev,
        .statistics-table .ant-pagination .ant-pagination-next {
          border-color: rgba(255, 255, 255, 0.08) !important;
          background: rgba(255, 255, 255, 0.04) !important;
          border-radius: 12px !important;
        }
        .statistics-table .ant-pagination .ant-pagination-item a,
        .statistics-table .ant-pagination .ant-pagination-prev button,
        .statistics-table .ant-pagination .ant-pagination-next button,
        .statistics-table .ant-pagination .ant-pagination-total-text {
          color: rgb(148, 163, 184) !important;
        }
        .statistics-table .ant-pagination .ant-pagination-item-active {
          background: rgba(255, 255, 255, 0.08) !important;
          border-color: rgba(255, 255, 255, 0.12) !important;
        }
        .statistics-table .ant-pagination .ant-pagination-item-active a {
          color: rgb(248, 250, 252) !important;
        }
        .statistics-table .ant-spin-nested-loading,
        .statistics-table .ant-spin-container {
          background: transparent !important;
        }
      `}</style>
    </div>
  )
}

export default StatisticsPage
