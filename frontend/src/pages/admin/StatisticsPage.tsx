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
  MailOutlined,
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
import { Button, Card, Col, Empty, Input, List, Progress, Row, Segmented, Select, Space, Spin, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { adminApi, type AdminAuditLogItem, type StatisticsDetailItem } from '@/services/api'
import { useRoleStore } from '@/stores/roleStore'

const { Paragraph, Text, Title } = Typography

const panelStyle: React.CSSProperties = {
  backgroundColor: '#161B22',
  borderColor: '#30363D',
  borderRadius: 18,
  boxShadow: '0 18px 40px rgba(0, 0, 0, 0.24)',
}

const panelBodyStyle = {
  padding: 20,
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
  color,
  icon,
}: {
  title: string
  value: number | string
  subtitle: string
  color: string
  icon: React.ReactNode
}) => (
  <Card bordered style={{ ...panelStyle, height: '100%', overflow: 'hidden' }} styles={{ body: panelBodyStyle }}>
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'flex-start',
        gap: 12,
      }}
    >
      <div>
        <Text style={{ color: '#8B949E', fontSize: 12, letterSpacing: 0.6 }}>{title}</Text>
        <div style={{ marginTop: 8, fontSize: 34, fontWeight: 700, color }}>{value}</div>
        <Paragraph style={{ marginTop: 10, marginBottom: 0, color: '#9FB0C3' }}>{subtitle}</Paragraph>
      </div>
      <div
        style={{
          width: 46,
          height: 46,
          borderRadius: 14,
          display: 'grid',
          placeItems: 'center',
          color,
          background: `${color}18`,
          border: `1px solid ${color}33`,
          fontSize: 20,
        }}
      >
        {icon}
      </div>
    </div>
  </Card>
)

const MetricChip = ({
  label,
  value,
  color,
  icon,
}: {
  label: string
  value: number
  color: string
  icon: React.ReactNode
}) => (
  <div
    style={{
      minHeight: 88,
      padding: '14px 16px',
      borderRadius: 16,
      border: `1px solid ${color}33`,
      background: `linear-gradient(180deg, ${color}14 0%, rgba(22,27,34,0.9) 100%)`,
    }}
  >
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, color }}>
      {icon}
      <Text style={{ color, fontSize: 12 }}>{label}</Text>
    </div>
    <div style={{ marginTop: 10, fontSize: 26, fontWeight: 700, color: '#F0F6FC' }}>{value}</div>
  </div>
)

const TrendBars = ({
  title,
  color,
  data,
  days,
}: {
  title: string
  color: string
  data: Array<{ date: string; count: number }>
  days: number
}) => {
  const maxValue = Math.max(...data.map((item) => item.count), 1)

  return (
    <div
      style={{
        padding: 16,
        borderRadius: 16,
        border: '1px solid #30363D',
        background: 'linear-gradient(180deg, rgba(13,17,23,0.92) 0%, rgba(22,27,34,0.96) 100%)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <Text style={{ color: '#E6EDF3', fontSize: 14, fontWeight: 600 }}>{title}</Text>
        <Text style={{ color: '#8B949E', fontSize: 12 }}>近 {days} 天</Text>
      </div>
      <div
        style={{
          marginTop: 16,
          display: 'grid',
          gridTemplateColumns: `repeat(${Math.max(data.length, 1)}, minmax(0, 1fr))`,
          gap: 10,
          alignItems: 'end',
          minHeight: 110,
        }}
      >
        {data.map((item) => (
          <div key={`${title}-${item.date}`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
            <Text style={{ color: '#8B949E', fontSize: 11 }}>{item.count}</Text>
            <div
              style={{
                width: '100%',
                height: Math.max(12, Math.round((item.count / maxValue) * 68)),
                borderRadius: 999,
                background: `linear-gradient(180deg, ${color} 0%, ${color}88 100%)`,
                boxShadow: `0 10px 20px ${color}22`,
              }}
            />
            <Text style={{ color: '#6E7681', fontSize: 11 }}>{formatDayLabel(item.date)}</Text>
          </div>
        ))}
      </div>
    </div>
  )
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
            render: (_, record) => (
              <div>
                <Text style={{ color: '#F0F6FC' }}>{record.title}</Text>
                <Paragraph style={{ margin: '4px 0 0', color: '#8B949E' }}>{record.subtitle || '-'}</Paragraph>
              </div>
            ),
          },
          {
            title: '导师',
            dataIndex: 'owner_name',
            key: 'owner_name',
            render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
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
            render: (value) => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag>,
          },
          {
            title: '更新时间',
            dataIndex: 'updated_at',
            key: 'updated_at',
            render: (value) => <Text style={{ color: '#6E7681' }}>{value ? formatDateTime(value) : '-'}</Text>,
          },
        ]
      : detailEntity === 'shares'
        ? [
            {
              title: '资源',
              dataIndex: 'title',
              key: 'title',
              render: (_, record) => (
                <div>
                  <Text style={{ color: '#F0F6FC' }}>{record.title}</Text>
                  <Paragraph style={{ margin: '4px 0 0', color: '#8B949E' }}>{record.subtitle || '-'}</Paragraph>
                </div>
              ),
            },
            {
              title: '所有者',
              dataIndex: 'owner_name',
              key: 'owner_name',
              render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
            },
            {
              title: '共享目标',
              dataIndex: 'target_name',
              key: 'target_name',
              render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
            },
            {
              title: '权限',
              dataIndex: 'permission',
              key: 'permission',
              render: (value) => <Tag color="blue">{value || '-'}</Tag>,
            },
            {
              title: '创建时间',
              dataIndex: 'created_at',
              key: 'created_at',
              render: (value) => <Text style={{ color: '#6E7681' }}>{value ? formatDateTime(value) : '-'}</Text>,
            },
          ]
        : detailEntity === 'invitations'
          ? [
              {
                title: '邀请链路',
                dataIndex: 'title',
                key: 'title',
                render: (_, record) => (
                  <div>
                    <Text style={{ color: '#F0F6FC' }}>{record.title}</Text>
                    <Paragraph style={{ margin: '4px 0 0', color: '#8B949E' }}>{record.subtitle || '-'}</Paragraph>
                  </div>
                ),
              },
              {
                title: '类型',
                dataIndex: 'category',
                key: 'category',
                render: (value) => <Tag color="blue">{value === 'invite' ? '邀请' : '申请'}</Tag>,
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                render: (value) => <Tag color={value === 'accepted' ? 'green' : value === 'pending' ? 'gold' : value === 'rejected' ? 'red' : 'default'}>{value || '-'}</Tag>,
              },
              {
                title: '发起人',
                dataIndex: 'owner_name',
                key: 'owner_name',
                render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
              },
              {
                title: '目标',
                dataIndex: 'target_name',
                key: 'target_name',
                render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
              },
              {
                title: '创建时间',
                dataIndex: 'created_at',
                key: 'created_at',
                render: (value) => <Text style={{ color: '#6E7681' }}>{value ? formatDateTime(value) : '-'}</Text>,
              },
            ]
          : [
              {
                title: '公告',
                dataIndex: 'title',
                key: 'title',
                render: (_, record) => (
                  <div>
                    <Text style={{ color: '#F0F6FC' }}>{record.title}</Text>
                    <Paragraph style={{ margin: '4px 0 0', color: '#8B949E' }}>{record.subtitle || '-'}</Paragraph>
                  </div>
                ),
              },
              {
                title: '导师',
                dataIndex: 'owner_name',
                key: 'owner_name',
                render: (value) => <Text style={{ color: '#9FB0C3' }}>{value || '-'}</Text>,
              },
              {
                title: '类别',
                dataIndex: 'category',
                key: 'category',
                render: (value) => <Tag color={value === 'pinned' ? 'gold' : 'default'}>{value === 'pinned' ? '置顶' : '普通'}</Tag>,
              },
              {
                title: '状态',
                dataIndex: 'status',
                key: 'status',
                render: (value) => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag>,
              },
              {
                title: '更新时间',
                dataIndex: 'updated_at',
                key: 'updated_at',
                render: (value) => <Text style={{ color: '#6E7681' }}>{value ? formatDateTime(value) : '-'}</Text>,
              },
            ]

  const auditColumns: ColumnsType<AdminAuditLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 190,
      render: (value) => <Text style={{ color: '#6E7681' }}>{formatDateTime(value)}</Text>,
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 150,
      render: (value) => <Tag color="blue">{getAuditActionLabel(value)}</Tag>,
    },
    {
      title: '管理员',
      dataIndex: 'admin_name',
      key: 'admin_name',
      width: 140,
      render: (value) => <Text style={{ color: '#E6EDF3' }}>{value}</Text>,
    },
    {
      title: '对象',
      key: 'target',
      width: 180,
      render: (_, record) => (
        <Text style={{ color: '#9FB0C3' }}>
          {[record.target_type, record.target_id].filter(Boolean).join(' / ') || '-'}
        </Text>
      ),
    },
    {
      title: '摘要',
      dataIndex: 'summary',
      key: 'summary',
      render: (value) => <Text style={{ color: '#F0F6FC' }}>{value}</Text>,
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
    <div
      style={{
        minHeight: '100%',
        padding: '24px',
        background:
          'radial-gradient(circle at top left, rgba(74,144,217,0.18), transparent 32%), radial-gradient(circle at top right, rgba(212,175,55,0.14), transparent 24%), linear-gradient(180deg, #0D1117 0%, #111827 100%)',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 16,
          flexWrap: 'wrap',
          marginBottom: 24,
        }}
      >
        <div>
          <Space align="center" size="middle">
            <div
              style={{
                width: 48,
                height: 48,
                borderRadius: 16,
                display: 'grid',
                placeItems: 'center',
                background: 'linear-gradient(135deg, rgba(74,144,217,0.22), rgba(212,175,55,0.22))',
                border: '1px solid rgba(148,163,184,0.22)',
                color: '#D4AF37',
                fontSize: 24,
              }}
            >
              <BarChartOutlined />
            </div>
            <div>
              <Title level={3} style={{ margin: 0, color: '#F0F6FC' }}>
                系统统计
              </Title>
              <Paragraph style={{ margin: '6px 0 0', color: '#9FB0C3' }}>
                管理员视角的运营概览，覆盖用户、资源、协作链路、导师分布和近期活动。
              </Paragraph>
            </div>
          </Space>
        </div>
        <Space wrap>
          <Segmented
            value={windowDays}
            options={[
              { label: '近 7 天', value: 7 },
              { label: '近 30 天', value: 30 },
              { label: '近 90 天', value: 90 },
            ]}
            onChange={(value) => setWindowDays(Number(value))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => fetchStatistics(windowDays)}>
            刷新数据
          </Button>
          <Button
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

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title="用户总量"
            value={statistics.total_users}
            subtitle={`活跃 ${statistics.active_users} / 非活跃 ${statistics.inactive_users}`}
            color="#4A90D9"
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
            color="#D4AF37"
            icon={<DatabaseOutlined />}
          />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title="协作链路"
            value={statistics.total_groups + statistics.total_shared_resources + statistics.total_announcements}
            subtitle={`研究组 ${statistics.total_groups}，共享 ${statistics.total_shared_resources}`}
            color="#52C41A"
            icon={<ShareAltOutlined />}
          />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricCard
            title={`近 ${statistics.time_window_days} 天新增`}
            value={activity.new_users_last_7_days}
            subtitle={`对话 ${activity.new_conversations_last_7_days}，Notebook ${activity.new_notebooks_last_7_days}`}
            color="#F97316"
            icon={<ThunderboltOutlined />}
          />
        </Col>

        <Col xs={24} xl={10}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18 }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                用户与导师制
              </Title>
              <Tag color="blue">活跃率 {activeRate}%</Tag>
            </div>
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                  <Text style={{ color: '#9FB0C3' }}>管理员</Text>
                  <Text style={{ color: '#D4AF37' }}>{statistics.admin_count}</Text>
                </div>
                <Progress percent={percent(statistics.admin_count, statistics.total_users)} showInfo={false} strokeColor="#D4AF37" trailColor="#2B313A" />
              </div>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                  <Text style={{ color: '#9FB0C3' }}>导师</Text>
                  <Text style={{ color: '#4A90D9' }}>{statistics.mentor_count}</Text>
                </div>
                <Progress percent={percent(statistics.mentor_count, statistics.total_users)} showInfo={false} strokeColor="#4A90D9" trailColor="#2B313A" />
              </div>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
                  <Text style={{ color: '#9FB0C3' }}>学生</Text>
                  <Text style={{ color: '#8B9FB2' }}>{statistics.student_count}</Text>
                </div>
                <Progress percent={percent(statistics.student_count, statistics.total_users)} showInfo={false} strokeColor="#8B9FB2" trailColor="#2B313A" />
              </div>
            </Space>
            <Row gutter={[12, 12]} style={{ marginTop: 18 }}>
              <Col span={12}>
                <MetricChip
                  label="已绑定导师学生"
                  value={mentorship.students_with_mentor}
                  color="#52C41A"
                  icon={<LinkOutlined />}
                />
              </Col>
              <Col span={12}>
                <MetricChip
                  label="待分配学生"
                  value={mentorship.students_without_mentor}
                  color="#F97316"
                  icon={<UserOutlined />}
                />
              </Col>
            </Row>
            <Paragraph style={{ marginTop: 16, marginBottom: 0, color: '#8B949E' }}>
              学生导师覆盖率 {mentorCoverage}%。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              资源总览
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="对话" value={statistics.total_conversations} color="#4A90D9" icon={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="知识库" value={statistics.total_knowledge_bases} color="#00B894" icon={<DatabaseOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="文档" value={statistics.total_documents} color="#F97316" icon={<FileTextOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="论文" value={statistics.total_papers} color="#8B5CF6" icon={<BookOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="Notebook" value={statistics.total_notebooks} color="#E11D48" icon={<ReadOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="共享资源" value={statistics.total_shared_resources} color="#22C55E" icon={<ShareAltOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              协作运营
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12}>
                <MetricChip label="研究组" value={collaboration.total_groups} color="#4A90D9" icon={<UsergroupAddOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="活跃研究组" value={collaboration.active_groups} color="#52C41A" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="组成员关系" value={collaboration.total_group_members} color="#A855F7" icon={<TeamOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="待处理邀请" value={collaboration.pending_invitations} color="#F97316" icon={<ClockCircleOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="公告总数" value={collaboration.total_announcements} color="#FACC15" icon={<NotificationOutlined />} />
              </Col>
              <Col xs={12}>
                <MetricChip label="启用公告" value={collaboration.active_announcements} color="#14B8A6" icon={<NotificationOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18 }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                文档处理状态
              </Title>
              <Tag color="green">完成率 {documentCompletionRate}%</Tag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="已完成" value={documentPipeline.completed_documents} color="#22C55E" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="处理中" value={documentPipeline.running_documents} color="#4A90D9" icon={<SyncOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="待处理" value={documentPipeline.pending_documents} color="#FACC15" icon={<ClockCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="失败" value={documentPipeline.failed_documents} color="#EF4444" icon={<StopOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="超时" value={documentPipeline.timeout_documents} color="#FB7185" icon={<ThunderboltOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="取消" value={documentPipeline.cancelled_documents} color="#94A3B8" icon={<StopOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              近 {statistics.time_window_days} 天新增
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8} xl={4}>
                <Statistic title="新增用户" value={activity.new_users_last_7_days} valueStyle={{ color: '#4A90D9' }} prefix={<TeamOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <Statistic title="新增对话" value={activity.new_conversations_last_7_days} valueStyle={{ color: '#0EA5E9' }} prefix={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <Statistic title="新增知识库" value={activity.new_knowledge_bases_last_7_days} valueStyle={{ color: '#00B894' }} prefix={<DatabaseOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <Statistic title="新增论文" value={activity.new_papers_last_7_days} valueStyle={{ color: '#8B5CF6' }} prefix={<BookOutlined />} />
              </Col>
              <Col xs={12} md={8} xl={4}>
                <Statistic title="新增 Notebook" value={activity.new_notebooks_last_7_days} valueStyle={{ color: '#E11D48' }} prefix={<ReadOutlined />} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              近 {statistics.time_window_days} 天趋势
            </Title>
            <Row gutter={[12, 12]}>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="用户" color="#4A90D9" data={trends.users} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="对话" color="#0EA5E9" data={trends.conversations} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="知识库" color="#00B894" data={trends.knowledge_bases} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="论文" color="#8B5CF6" data={trends.papers} days={statistics.time_window_days} />
              </Col>
              <Col xs={24} lg={12} xl={8}>
                <TrendBars title="Notebook" color="#E11D48" data={trends.notebooks} days={statistics.time_window_days} />
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24} xl={10}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              共享与邀请分布
            </Title>
            <Text style={{ color: '#9FB0C3', display: 'block', marginBottom: 10 }}>共享资源类型</Text>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {shareBreakdown.length > 0 ? shareBreakdown.map((item) => (
                <div key={`share-${item.key}`}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <Text style={{ color: '#E6EDF3' }}>{item.label}</Text>
                    <Text style={{ color: '#22C55E' }}>{item.count}</Text>
                  </div>
                  <Progress percent={percent(item.count, statistics.total_shared_resources)} showInfo={false} strokeColor="#22C55E" trailColor="#2B313A" />
                </div>
              )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无共享资源" />}
            </Space>
            <Text style={{ color: '#9FB0C3', display: 'block', marginTop: 18, marginBottom: 10 }}>邀请状态</Text>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {invitationBreakdown.length > 0 ? invitationBreakdown.map((item) => (
                <div key={`invitation-${item.key}`}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <Text style={{ color: '#E6EDF3' }}>{item.label}</Text>
                    <Text style={{ color: '#F97316' }}>{item.count}</Text>
                  </div>
                  <Progress percent={percent(item.count, invitationBreakdown.reduce((sum, current) => sum + current.count, 0))} showInfo={false} strokeColor="#F97316" trailColor="#2B313A" />
                </div>
              )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无邀请记录" />}
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={14}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              导师排行
            </Title>
            <List
              dataSource={topMentors}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无导师数据" /> }}
              renderItem={(item, index) => (
                <List.Item style={{ borderBlockEnd: '1px solid #2B313A', paddingInline: 0 }}>
                  <div style={{ width: '100%', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                    <div>
                      <Space size="middle">
                        <Tag color={index === 0 ? 'gold' : index === 1 ? 'blue' : 'default'} icon={<CrownOutlined />}>
                          TOP {index + 1}
                        </Tag>
                        <div>
                          <Text style={{ color: '#E6EDF3', fontSize: 15 }}>
                            {item.full_name || item.username}
                          </Text>
                          <Paragraph style={{ margin: '4px 0 0', color: '#8B949E' }}>
                            @{item.username}
                          </Paragraph>
                        </div>
                      </Space>
                    </div>
                    <Space size="large">
                      <Statistic title="学生" value={item.student_count} valueStyle={{ color: '#4A90D9', fontSize: 22 }} prefix={<TeamOutlined />} />
                      <Statistic title="研究组" value={item.group_count} valueStyle={{ color: '#22C55E', fontSize: 22 }} prefix={<FolderOpenOutlined />} />
                    </Space>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <Title level={5} style={{ margin: '0 0 18px', color: '#F0F6FC' }}>
              最近活动
            </Title>
            <List
              dataSource={recentActivity}
              locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无近期活动" /> }}
              renderItem={(item) => (
                <List.Item style={{ borderBlockEnd: '1px solid #2B313A', paddingInline: 0 }}>
                  <div style={{ width: '100%', display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                    <div>
                      <Space wrap>
                        <Tag color="blue">{item.type}</Tag>
                        <Text style={{ color: '#F0F6FC', fontSize: 15 }}>{item.title}</Text>
                      </Space>
                      <Paragraph style={{ margin: '6px 0 0', color: '#8B949E' }}>
                        {item.owner_name} · {item.owner_role}
                      </Paragraph>
                    </div>
                    <Text style={{ color: '#6E7681' }}>{formatDateTime(item.created_at)}</Text>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18 }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                AI / RAG 专项
              </Title>
              <Tag color="blue">运行成功率 {aiRunSuccessRate}%</Tag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="Agent 调用" value={aiRag.agent_runs_last_window} color="#4A90D9" icon={<RobotOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="RAG 回复" value={aiRag.rag_messages_last_window} color="#22C55E" icon={<MessageOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="检索调用" value={aiRag.knowledge_search_calls_last_window} color="#F97316" icon={<SearchOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="引用有效" value={aiRag.citation_valid_answers_last_window} color="#D4AF37" icon={<LinkOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="压缩回退块" value={aiRag.compression_fallback_chunks_last_window} color="#FB7185" icon={<SyncOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="回复 Tokens" value={aiRag.assistant_total_tokens_last_window} color="#8B5CF6" icon={<ThunderboltOutlined />} />
              </Col>
            </Row>
            <Paragraph style={{ marginTop: 16, marginBottom: 0, color: '#8B949E' }}>
              近 {statistics.time_window_days} 天引用有效率 {citationValidityRate}% ，修复成功率 {citationRepairRate}% ，压缩调用 {aiRag.compression_calls_last_window} 次。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card bordered style={{ ...panelStyle, height: '100%' }} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18 }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                CodeLab 专项
              </Title>
              <Tag color="green">代码单元覆盖率 {codelabCellCoverage}%</Tag>
            </div>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={8}>
                <MetricChip label="活跃 Notebook" value={codelab.notebooks_active_last_window} color="#4A90D9" icon={<ReadOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="已执行 Notebook" value={codelab.executed_notebooks} color="#22C55E" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="总执行次数" value={codelab.total_execution_count} color="#F97316" icon={<ThunderboltOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="代码单元" value={codelab.code_cells} color="#8B5CF6" icon={<CodeOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="已执行单元" value={codelab.executed_code_cells} color="#E11D48" icon={<CheckCircleOutlined />} />
              </Col>
              <Col xs={12} md={8}>
                <MetricChip label="Agent Tokens" value={codelab.agent_tokens_last_window} color="#14B8A6" icon={<RobotOutlined />} />
              </Col>
            </Row>
            <Paragraph style={{ marginTop: 16, marginBottom: 0, color: '#8B949E' }}>
              近 {statistics.time_window_days} 天 CodeLab Agent 调用 {codelab.agent_runs_last_window} 次。
            </Paragraph>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 18, gap: 12, flexWrap: 'wrap' }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                文献阅读专项
              </Title>
              <Tag color="gold">知识链路完成率 {knowledgeLinkCompletionRate}%</Tag>
            </div>
            <Row gutter={[16, 16]}>
              <Col xs={24} xl={14}>
                <Row gutter={[12, 12]}>
                  <Col xs={12} md={8}>
                    <MetricChip label="文献集" value={literature.total_collections} color="#4A90D9" icon={<FolderOpenOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="活跃阅读会话" value={literature.active_read_sessions_last_window} color="#22C55E" icon={<ReadOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="批注" value={literature.annotations_last_window} color="#F97316" icon={<FileTextOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="评论" value={literature.comments_last_window} color="#8B5CF6" icon={<MessageOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="评分" value={literature.ratings_last_window} color="#FACC15" icon={<CrownOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="QA 会话" value={literature.qa_sessions_last_window} color="#14B8A6" icon={<BookOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="QA 消息" value={literature.qa_messages_last_window} color="#E11D48" icon={<MessageOutlined />} />
                  </Col>
                  <Col xs={12} md={8}>
                    <MetricChip label="知识链路" value={literature.knowledge_links_total} color="#94A3B8" icon={<LinkOutlined />} />
                  </Col>
                </Row>
              </Col>
              <Col xs={24} xl={10}>
                <Text style={{ color: '#9FB0C3', display: 'block', marginBottom: 10 }}>论文入知识库状态</Text>
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  {knowledgeLinkBreakdown.length > 0 ? knowledgeLinkBreakdown.map((item) => (
                    <div key={`paper-link-${item.key}`}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                        <Text style={{ color: '#E6EDF3' }}>{item.label}</Text>
                        <Text style={{ color: '#D4AF37' }}>{item.count}</Text>
                      </div>
                      <Progress
                        percent={percent(item.count, literature.knowledge_links_total)}
                        showInfo={false}
                        strokeColor="#D4AF37"
                        trailColor="#2B313A"
                      />
                    </div>
                  )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无知识链路记录" />}
                </Space>
              </Col>
            </Row>
          </Card>
        </Col>

        <Col xs={24}>
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 18, flexWrap: 'wrap' }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                明细下钻
              </Title>
              <Space wrap>
                <Button
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
                  value={detailEntity}
                  options={DETAIL_ENTITY_OPTIONS.map((item) => ({ label: item.label, value: item.value }))}
                  onChange={(value) => setDetailEntity(value as DetailEntity)}
                />
                <Input
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
          <Card bordered style={panelStyle} styles={{ body: panelBodyStyle }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 18, flexWrap: 'wrap' }}>
              <Title level={5} style={{ margin: 0, color: '#F0F6FC' }}>
                管理员审计
              </Title>
              <Space wrap>
                <Button
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
    </div>
  )
}

export default StatisticsPage
