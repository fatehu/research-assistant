import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Empty, Space, Typography, message } from 'antd'
import {
  ArrowRightOutlined,
  FileSearchOutlined,
  ProjectOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'

import {
  chatApi,
  projectApi,
  type Paper,
  type ResearchProject,
} from '@/services/api'
import { buildPaperPlanningLaunch } from '@/utils/paperWorkflowPrompts'

const { Paragraph, Text } = Typography

interface PaperProjectLauncherCardProps {
  paper: Paper
}

export default function PaperProjectLauncherCard({ paper }: PaperProjectLauncherCardProps) {
  const navigate = useNavigate()
  const [projects, setProjects] = useState<ResearchProject[]>([])
  const [loading, setLoading] = useState(true)
  const [openingChat, setOpeningChat] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  const loadProjects = useCallback(async () => {
    try {
      setProjects(await projectApi.listProjects({ paper_id: paper.id }))
    } catch (error) {
      message.error(String((error as Error)?.message || '加载论文 project 失败'))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [paper.id])

  useEffect(() => {
    setLoading(true)
    void loadProjects()
  }, [loadProjects])

  const handleOpenChat = async () => {
    setOpeningChat(true)
    try {
      const primaryProject = projects[0] || null
      const conversation = await chatApi.createConversation(`论文复现：${String(paper.title || '').slice(0, 40)}`)
      const { initialMessage, skillLaunch } = buildPaperPlanningLaunch({
        paperId: paper.id,
        projectId: primaryProject?.id || null,
        goal: primaryProject?.goal || null,
      })
      navigate(`/chat/${conversation.id}`, {
        state: {
          initialMessage,
          initialSkillLaunch: skillLaunch,
        },
      })
    } catch (error) {
      message.error(String((error as Error)?.message || '打开论文复现对话失败'))
    } finally {
      setOpeningChat(false)
    }
  }

  const handleRefresh = async () => {
    setRefreshing(true)
    await loadProjects()
  }

  const primaryProject = projects[0] || null

  return (
    <Card
      size="small"
      className="!border-slate-700/60 !bg-slate-900/55"
      title={(
        <div className="flex items-center gap-2 text-slate-100">
          <FileSearchOutlined className="text-cyan-400" />
          <span>论文复现 Project</span>
        </div>
      )}
      extra={(
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={refreshing}
          onClick={() => void handleRefresh()}
        >
          刷新
        </Button>
      )}
    >
      {loading ? (
        <div className="text-sm text-slate-400">正在检查这篇论文关联的 project...</div>
      ) : primaryProject ? (
        <Space direction="vertical" size={12} className="w-full">
          <div className="rounded-2xl border border-slate-700/60 bg-slate-950/35 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Current Project</div>
            <div className="mt-1 text-sm font-medium text-slate-100">{primaryProject.title}</div>
            <div className="mt-1 text-xs leading-5 text-slate-400">
              {primaryProject.project_root || `/app/uploads/projects/${primaryProject.id}`}
            </div>
          </div>

          <Paragraph className="!mb-0 !text-sm !leading-6 !text-slate-400">
            这篇论文已经绑定到当前 project。继续进入 Chat 时，会先检查 `reference/`，缺失时自动运行 prepare builder。
          </Paragraph>

          {primaryProject.goal ? (
            <div className="rounded-2xl border border-slate-700/60 bg-slate-950/35 px-4 py-3">
              <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">Goal</div>
              <Text className="!text-slate-200">{primaryProject.goal}</Text>
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button
              type="primary"
              icon={<ArrowRightOutlined />}
              loading={openingChat}
              onClick={() => void handleOpenChat()}
            >
              在 Chat 中继续
            </Button>
            <Button
              icon={<ProjectOutlined />}
              onClick={() => navigate(`/projects/${primaryProject.id}`)}
            >
              打开 Project
            </Button>
          </div>
        </Space>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="这篇论文还没有绑定 project"
        >
          <Space direction="vertical" size={10} className="items-start">
            <Paragraph className="!mb-0 !text-sm !leading-6 !text-slate-500">
              直接进入 Chat 即可。系统会先解析论文，自动找或创建 project，再检查 `reference/` 是否需要 prepare。
            </Paragraph>
            <div className="flex flex-wrap gap-2">
              <Button
                type="primary"
                icon={<ArrowRightOutlined />}
                loading={openingChat}
                onClick={() => void handleOpenChat()}
              >
                开始论文复现
              </Button>
            </div>
          </Space>
        </Empty>
      )}
    </Card>
  )
}
