import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Typography,
  message,
} from 'antd'
import {
  BookOutlined,
  DeleteOutlined,
  PlusOutlined,
  ProjectOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  projectApi,
  type ResearchProject,
  type ResearchProjectFolderTree,
} from '@/services/api'
import { ProjectFolderBrowser } from '@/components/projects/ProjectFolderBrowser'

const { Paragraph, Title } = Typography
const { TextArea } = Input

type ProjectFormValues = {
  title?: string
  goal?: string
}

const glassPanelClass =
  'border border-white/[0.08] bg-[#071322]/[0.82] shadow-[0_24px_70px_rgba(0,0,0,0.36),inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-xl transition-all duration-300'

const darkButtonClass =
  '!border-slate-700 !bg-slate-950/60 !text-slate-200 hover:!border-cyan-400/60 hover:!bg-cyan-400/10 hover:!text-cyan-100 disabled:!border-slate-800 disabled:!bg-slate-900 disabled:!text-slate-600 transition-all duration-300'

const statusMetaMap: Record<string, { label: string; className: string; glowClass: string }> = {
  draft: {
    label: 'draft',
    className: 'border-slate-400/[0.22] bg-slate-300/[0.07] text-slate-200',
    glowClass: 'from-slate-300/[0.04]',
  },
  active: {
    label: 'active',
    className: 'border-cyan-300/[0.26] bg-cyan-300/[0.08] text-cyan-200',
    glowClass: 'from-cyan-300/[0.06]',
  },
  archived: {
    label: 'archived',
    className: 'border-amber-300/[0.24] bg-amber-300/[0.08] text-amber-200',
    glowClass: 'from-amber-300/[0.05]',
  },
}

const getStatusMeta = (status?: string | null) => statusMetaMap[String(status || 'draft')] || statusMetaMap.draft

const formatDateTime = (value?: string) => {
  if (!value) return 'unknown'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export default function ProjectsPage() {
  const navigate = useNavigate()
  const { projectId } = useParams<{ projectId: string }>()
  const [form] = Form.useForm<ProjectFormValues>()

  const [projects, setProjects] = useState<ResearchProject[]>([])
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [folderTreeLoading, setFolderTreeLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [deletingProject, setDeletingProject] = useState(false)
  const [selectedProject, setSelectedProject] = useState<ResearchProject | null>(null)
  const [folderTree, setFolderTree] = useState<ResearchProjectFolderTree | null>(null)

  const numericProjectId = useMemo(() => {
    const parsed = Number(projectId || 0)
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null
  }, [projectId])

  const loadProjects = useCallback(async () => {
    setLoading(true)
    try {
      const data = await projectApi.listProjects()
      setProjects(data)
    } catch (error) {
      message.error(String((error as Error)?.message || '加载项目失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadProjectDetail = useCallback(async (id: number) => {
    setDetailLoading(true)
    try {
      const data = await projectApi.getProject(id)
      setSelectedProject(data)
    } catch (error) {
      setSelectedProject(null)
      message.error(String((error as Error)?.message || '加载项目详情失败'))
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const loadProjectFolderTree = useCallback(async (id: number, options?: { silent?: boolean }) => {
    if (!options?.silent) setFolderTreeLoading(true)
    try {
      const data = await projectApi.getProjectFolderTree(id)
      setFolderTree(data)
    } catch (error) {
      setFolderTree(null)
      if (!options?.silent) {
        message.error(String((error as Error)?.message || '加载项目文件夹失败'))
      }
    } finally {
      if (!options?.silent) setFolderTreeLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  useEffect(() => {
    if (!numericProjectId) {
      setSelectedProject(null)
      setFolderTree(null)
      return
    }
    void loadProjectDetail(numericProjectId)
    void loadProjectFolderTree(numericProjectId)
  }, [loadProjectDetail, loadProjectFolderTree, numericProjectId])

  useEffect(() => {
    if (!numericProjectId) return undefined
    const timer = window.setInterval(() => {
      void loadProjectFolderTree(numericProjectId, { silent: true })
    }, 15000)
    return () => window.clearInterval(timer)
  }, [loadProjectFolderTree, numericProjectId])

  const handleCreateProject = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      const created = await projectApi.createProject({
        title: values.title,
        goal: values.goal,
        status: 'draft',
      })
      setCreateOpen(false)
      form.resetFields()
      await loadProjects()
      navigate(`/projects/${created.id}`)
      message.success('项目已创建')
    } catch (error) {
      message.error(String((error as Error)?.message || '创建项目失败'))
    } finally {
      setCreating(false)
    }
  }

  const handleDeleteProject = (project: ResearchProject) => {
    Modal.confirm({
      title: '删除项目',
      content: `将删除项目“${project.title}”和它的 project 文件夹。此操作不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true, loading: deletingProject },
      cancelText: '取消',
      onOk: async () => {
        setDeletingProject(true)
        try {
          await projectApi.deleteProject(project.id)
          message.success('项目已删除')
          await loadProjects()
          if (numericProjectId === project.id) {
            setSelectedProject(null)
            setFolderTree(null)
            navigate('/projects')
          }
        } catch (error) {
          message.error(String((error as Error)?.message || '删除项目失败'))
        } finally {
          setDeletingProject(false)
        }
      },
    })
  }

  const selectedStatusMeta = getStatusMeta(selectedProject?.status)
  const selectedProjectRoot = selectedProject
    ? folderTree?.project_root || selectedProject.project_root || `/app/uploads/projects/${selectedProject.id}`
    : ''
  const selectedProjectFolderExists = Boolean(folderTree?.exists || selectedProject?.project_root_exists)

  return (
    <>
      <div className="premium-command-page relative min-h-full space-y-6 overflow-hidden text-slate-200">
        <div className="pointer-events-none absolute -left-24 top-0 h-72 w-72 rounded-full bg-cyan-300/[0.03] blur-3xl" />
        <div className="pointer-events-none absolute right-0 top-40 h-96 w-96 rounded-full bg-emerald-300/[0.03] blur-3xl" />

        <div className={`relative overflow-hidden rounded-[32px] ${glassPanelClass} p-6 hover:shadow-[0_24px_60px_rgba(34,211,238,0.06)]`}>
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_0%,rgba(34,211,238,0.12),transparent_34%),linear-gradient(135deg,rgba(2,6,23,0.1),rgba(15,23,42,0.42))]" />
          <div className="relative flex flex-wrap items-start justify-between gap-5">
            <div className="max-w-4xl space-y-3">
              <div className="inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-300/[0.08] px-3 py-1 text-xs uppercase tracking-[0.22em] text-cyan-200 shadow-[0_0_15px_rgba(34,211,238,0.1)]">
                <ProjectOutlined />
                Research Command Center
              </div>
              <Title level={3} className="!mb-0 !text-slate-50">
                研究项目
              </Title>
              <Paragraph className="!mb-0 !text-slate-400">
                这里聚焦 project 基础信息、根目录和当前文件树，用作论文复现、文献整理和后续交付物的工程化工作台。
              </Paragraph>
            </div>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setCreateOpen(true)}
              className="!border-cyan-400/50 !bg-gradient-to-r !from-cyan-500/20 !to-blue-500/20 !text-cyan-50 hover:!from-cyan-500/40 hover:!to-blue-500/40 hover:!text-white shadow-[0_0_20px_rgba(34,211,238,0.25)] hover:shadow-[0_0_30px_rgba(34,211,238,0.4)] transition-all duration-300"
            >
              新建项目
            </Button>
          </div>
          <div className="relative mt-5 grid gap-3 sm:grid-cols-3">
            <div className="group rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3 transition-all duration-300 hover:border-cyan-400/30 hover:bg-[#020817]/70">
              <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80 transition-colors group-hover:text-cyan-200">Projects</div>
              <div className="mt-1 text-2xl font-semibold text-slate-50">{projects.length}</div>
            </div>
            <div className="group rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3 transition-all duration-300 hover:border-cyan-400/30 hover:bg-[#020817]/70">
              <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80 transition-colors group-hover:text-cyan-200">Selected</div>
              <div className="mt-1 truncate text-sm font-semibold text-slate-200">{selectedProject?.title || '未选择'}</div>
            </div>
            <div className="group rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3 transition-all duration-300 hover:border-cyan-400/30 hover:bg-[#020817]/70">
              <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80 transition-colors group-hover:text-cyan-200">Folder</div>
              <div className="mt-1 text-sm font-semibold text-slate-200">
                <span className={`inline-flex items-center gap-1.5 ${selectedProjectFolderExists ? 'text-emerald-400' : 'text-amber-400/80'}`}>
                  {selectedProjectFolderExists && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />}
                  {selectedProjectFolderExists ? 'READY' : 'PENDING'}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div className="relative grid gap-5 lg:grid-cols-[360px_minmax(0,1fr)]">
          <aside className={`rounded-[30px] ${glassPanelClass} p-3`}>
            <div className="mb-3 flex items-center justify-between px-2">
              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400/70">Project Index</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">{projects.length} 个项目</div>
              </div>
              <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void loadProjects()} className="!border-white/[0.1] !bg-white/[0.03] !text-slate-300 hover:!border-cyan-400/50 hover:!bg-cyan-400/10 hover:!text-cyan-200 transition-all">
                刷新
              </Button>
            </div>
            {loading ? (
              <div className="flex min-h-[320px] items-center justify-center">
                <Spin />
              </div>
            ) : projects.length > 0 ? (
              <div className="space-y-2">
                {projects.map((item) => {
                  const active = numericProjectId === item.id
                  const statusMeta = getStatusMeta(item.status)
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`group relative w-full overflow-hidden rounded-3xl border px-4 py-3 text-left transition-all ${
                        active
                          ? `border-cyan-400/[0.2] bg-gradient-to-r ${statusMeta.glowClass} via-[#0a1a2e] to-[#061122] shadow-[0_8px_24px_rgba(34,211,238,0.04)]`
                          : 'border-white/[0.07] bg-[#061122]/[0.72] hover:border-cyan-300/[0.18] hover:bg-[#08182c]'
                      }`}
                      onClick={() => navigate(`/projects/${item.id}`)}
                    >
                      {active ? (
                        <>
                          <span className="absolute inset-y-4 left-0 w-px rounded-r-full bg-cyan-200/25" />
                          <span className="pointer-events-none absolute -left-8 top-1/2 h-20 w-20 -translate-y-1/2 rounded-full bg-teal-200/[0.05] blur-2xl" />
                        </>
                      ) : null}
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="truncate text-base font-semibold text-slate-100">{item.title}</div>
                          <div className="mt-1 text-xs text-slate-400/80">{item.paper_count} papers</div>
                        </div>
                        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] ${statusMeta.className}`}>
                          {statusMeta.label}
                        </span>
                      </div>
                      <div className="mt-3 line-clamp-2 text-sm leading-6 text-slate-400">
                        {String(item.goal || '尚未设置项目目标')}
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : (
              <div className="rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55 px-5 py-8">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-500">还没有项目</span>}>
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                    创建第一个项目
                  </Button>
                </Empty>
              </div>
            )}
          </aside>

          <main className={`min-h-[520px] rounded-[30px] ${glassPanelClass} p-5`}>
            {detailLoading ? (
              <div className="flex min-h-[320px] items-center justify-center">
                <Spin />
              </div>
            ) : selectedProject ? (
              <div className="space-y-5">
                <section className="relative overflow-hidden rounded-[28px] border border-white/[0.08] bg-[#061122]/[0.76] p-5">
                  <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_18%_0%,rgba(34,211,238,0.12),transparent_35%)]" />
                  <div className="relative flex flex-wrap items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <span className={`rounded-full border px-2.5 py-1 text-xs ${selectedStatusMeta.className}`}>
                          {selectedStatusMeta.label}
                        </span>
                        <span className="text-xs text-slate-400/70">Updated {formatDateTime(selectedProject.updated_at)}</span>
                      </div>
                      <Title level={4} className="!mb-0 !text-slate-50">
                        {selectedProject.title}
                      </Title>
                    </div>
                    <Space wrap>
                        {selectedProject.primary_paper ? (
                          <Button
                            icon={<BookOutlined />}
                            onClick={() => navigate(`/literature/${selectedProject.primary_paper?.id}/read`)}
                            className={darkButtonClass}
                          >
                            打开主论文
                          </Button>
                        ) : null}
                        <Button
                          icon={<ReloadOutlined />}
                          loading={detailLoading || folderTreeLoading}
                          className={darkButtonClass}
                          onClick={() => {
                            if (!numericProjectId) return
                            void loadProjectDetail(numericProjectId)
                            void loadProjectFolderTree(numericProjectId)
                          }}
                        >
                          刷新
                        </Button>
                        <Button
                          danger
                          icon={<DeleteOutlined />}
                          loading={deletingProject}
                          className="!border-rose-500/30 !bg-rose-500/10 !text-rose-200 hover:!border-rose-400/60 hover:!bg-rose-400/20 hover:!text-rose-100 transition-all duration-300"
                          onClick={() => handleDeleteProject(selectedProject)}
                        >
                          删除项目
                        </Button>
                      </Space>
                    </div>
                  </section>

                  <div className="grid gap-3 md:grid-cols-3">
                    <div className="group relative overflow-hidden rounded-3xl border border-white/[0.08] bg-[#020817]/55 p-4 transition-all hover:border-cyan-400/30 hover:bg-[#020817]/80 hover:shadow-[0_8px_24px_rgba(34,211,238,0.06)]">
                      <div className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full bg-cyan-400/5 blur-2xl transition-opacity group-hover:opacity-100 opacity-0" />
                      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-400/80 transition-colors group-hover:text-cyan-200/80">关联论文</div>
                      <div className="mt-2 text-2xl font-semibold text-slate-50">{selectedProject.paper_count}</div>
                      <div className="mt-1 text-xs text-slate-400/80">papers</div>
                    </div>
                    <div className="group relative overflow-hidden rounded-3xl border border-white/[0.08] bg-[#020817]/55 p-4 transition-all hover:border-emerald-400/30 hover:bg-[#020817]/80 hover:shadow-[0_8px_24px_rgba(52,211,153,0.06)]">
                      <div className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full bg-emerald-400/5 blur-2xl transition-opacity group-hover:opacity-100 opacity-0" />
                      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-400/80 transition-colors group-hover:text-emerald-200/80">目录状态</div>
                      <div className={`mt-2 text-lg font-semibold ${selectedProjectFolderExists ? 'text-emerald-400' : 'text-slate-50'}`}>
                        {selectedProjectFolderExists ? 'READY' : 'MISSING'}
                      </div>
                      <div className="mt-1 text-xs text-slate-400/80">project folder</div>
                    </div>
                    <div className="group relative overflow-hidden rounded-3xl border border-white/[0.08] bg-[#020817]/55 p-4 transition-all hover:border-indigo-400/30 hover:bg-[#020817]/80 hover:shadow-[0_8px_24px_rgba(129,140,248,0.06)]">
                      <div className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full bg-indigo-400/5 blur-2xl transition-opacity group-hover:opacity-100 opacity-0" />
                      <div className="text-[11px] uppercase tracking-[0.18em] text-slate-400/80 transition-colors group-hover:text-indigo-200/80">主论文</div>
                      <div className="mt-2 truncate text-sm font-semibold text-slate-100">
                        {selectedProject.primary_paper?.title || '未绑定'}
                      </div>
                      <div className="mt-1 text-xs text-slate-400/80">primary paper</div>
                    </div>
                  </div>

                <section className="group relative rounded-[28px] border border-white/[0.08] bg-[#061122]/[0.62] p-5 transition-all hover:border-white/[0.12] hover:bg-[#061122]/80">
                  <div className="pointer-events-none absolute -left-12 -top-12 h-32 w-32 rounded-full bg-cyan-400/5 blur-3xl transition-opacity group-hover:bg-cyan-400/10" />
                  <div className="relative">
                    <div className="text-xs uppercase tracking-[0.18em] text-slate-400/80">Goal</div>
                    <div className="mt-3 max-w-5xl text-sm leading-7 text-slate-300">
                      {selectedProject.goal || '尚未设置目标'}
                    </div>
                  </div>
                </section>

                <section className="rounded-[28px] border border-white/[0.08] bg-[#061122]/[0.62] p-5">
                  <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-xs uppercase tracking-[0.18em] text-slate-400/80">Project Folder</div>
                      <div className="mt-2 break-all font-mono text-sm text-slate-300">{selectedProjectRoot}</div>
                    </div>
                    <span
                      className={`shrink-0 rounded-full border px-2.5 py-1 text-xs ${
                        selectedProjectFolderExists
                          ? 'border-emerald-300/[0.24] bg-emerald-300/[0.08] text-emerald-200'
                          : 'border-slate-400/[0.22] bg-slate-300/[0.07] text-slate-300'
                      }`}
                    >
                      {selectedProjectFolderExists ? 'exists' : 'missing'}
                    </span>
                  </div>
                  {selectedProjectFolderExists ? null : (
                    <Alert
                      type="info"
                      showIcon
                      message="当前 project 文件夹还不存在"
                      description="prepare 或后续 project 文件写入后，这里会显示实际目录树。"
                    />
                  )}
                  <div className="mt-4">
                    <div className="mb-2 text-xs uppercase tracking-[0.18em] text-slate-400/80">Folder Tree</div>
                    {folderTreeLoading ? (
                      <div className="flex min-h-[120px] items-center justify-center">
                        <Spin size="small" />
                      </div>
                    ) : (
                      <ProjectFolderBrowser tree={folderTree?.tree || '.'} />
                    )}
                  </div>
                </section>
              </div>
            ) : (
              <div className="flex min-h-[420px] items-center justify-center rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-500">从左侧选择一个项目，或先创建新项目</span>} />
              </div>
            )}
          </main>
        </div>
      </div>

      <Modal
        title={<span className="text-slate-100 text-lg font-semibold">新建项目</span>}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void handleCreateProject()}
        confirmLoading={creating}
        okText="创建"
        rootClassName="premium-command-page"
        className="premium-command-modal"
        okButtonProps={{ className: '!border-cyan-400/50 !bg-gradient-to-r !from-cyan-500/20 !to-blue-500/20 !text-cyan-50 hover:!from-cyan-500/40 hover:!to-blue-500/40 hover:!text-white shadow-[0_0_20px_rgba(34,211,238,0.25)] hover:shadow-[0_0_30px_rgba(34,211,238,0.4)] transition-all' }}
        cancelButtonProps={{ className: darkButtonClass }}
      >
        <div className="py-4">
          <Form form={form} layout="vertical">
            <Form.Item name="title" label={<span className="text-slate-300">项目标题</span>}>
              <Input placeholder="例如：fastText reproduction" className="!rounded-xl !bg-[#061122] !border-white/[0.09] !text-slate-100 placeholder:!text-slate-500/60 focus:!border-cyan-400/50 focus:!shadow-[0_0_15px_rgba(34,211,238,0.15)]" />
            </Form.Item>
            <Form.Item name="goal" label={<span className="text-slate-300">项目目标</span>}>
              <TextArea rows={5} placeholder="例如：准备论文与仓库 reference，并为后续复现整理 project 工作目录" className="!rounded-xl !bg-[#061122] !border-white/[0.09] !text-slate-100 placeholder:!text-slate-500/60 focus:!border-cyan-400/50 focus:!shadow-[0_0_15px_rgba(34,211,238,0.15)]" />
            </Form.Item>
          </Form>
        </div>
      </Modal>
    </>
  )
}
