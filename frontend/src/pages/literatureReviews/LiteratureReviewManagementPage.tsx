import { useCallback, useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Alert, Button, Empty, Space, Spin, Tag, Typography, message } from 'antd'
import {
  BookOutlined,
  CodeOutlined,
  DownloadOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  literatureReviewWorkspaceApi,
  type LiteratureReviewFileContent,
  type LiteratureReviewWorkspace,
  type LiteratureReviewWorkspaceFile,
  type LiteratureReviewWorkspaceOverview,
} from '@/services/api'

const { Paragraph, Text, Title } = Typography

const glassPanelClass =
  'border border-white/[0.08] bg-[#071322]/[0.82] shadow-[0_24px_70px_rgba(0,0,0,0.36),inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-xl transition-all duration-300'

const darkButtonClass =
  '!border-white/[0.1] !bg-[linear-gradient(180deg,rgba(55,65,81,0.72)_0%,rgba(31,41,55,0.82)_100%)] !text-slate-300 hover:!border-cyan-200/30 hover:!text-slate-100 hover:!shadow-[0_6px_10px_-2px_rgba(0,0,0,0.34)] transition-all duration-200'

const statusMetaMap: Record<string, { label: string; className: string }> = {
  final_ready: { label: 'final ready', className: 'border-emerald-300/[0.24] bg-emerald-300/[0.08] text-emerald-200' },
  reviewing: { label: 'reviewing', className: 'border-cyan-300/[0.24] bg-cyan-300/[0.08] text-cyan-200' },
  reading: { label: 'reading', className: 'border-blue-300/[0.22] bg-blue-300/[0.07] text-blue-200' },
  downloaded: { label: 'downloaded', className: 'border-amber-300/[0.24] bg-amber-300/[0.08] text-amber-200' },
  created: { label: 'created', className: 'border-slate-300/[0.18] bg-slate-300/[0.06] text-slate-300' },
}

const groupLabels: Record<string, string> = {
  root: 'Root',
  pdf: 'PDF',
  md: 'Markdown / JSON',
  review: 'Reviews',
  searches: 'Searches',
  other: 'Other',
}

const formatDateTime = (value?: string) => {
  if (!value) return 'unknown'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const formatBytes = (value?: number) => {
  const size = Number(value || 0)
  if (size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

const fileIcon = (file: LiteratureReviewWorkspaceFile) => {
  if (file.suffix === '.pdf') return <FilePdfOutlined className="text-red-300" />
  if (file.suffix === '.md') return <FileMarkdownOutlined className="text-cyan-300" />
  if (file.suffix === '.json') return <CodeOutlined className="text-amber-300" />
  return <FileTextOutlined className="text-slate-400" />
}

const pickDefaultFilePath = (files: LiteratureReviewWorkspaceFile[]) => {
  return (
    files.find((item) => item.relative_path === 'review/final.md')?.relative_path ||
    files.find((item) => item.relative_path === 'manifest.json')?.relative_path ||
    files.find((item) => item.relative_path.startsWith('review/') && item.suffix === '.md')?.relative_path ||
    files.find((item) => item.relative_path.startsWith('md/') && item.suffix === '.md')?.relative_path ||
    files.find((item) => item.suffix === '.json')?.relative_path ||
    ''
  )
}

export default function LiteratureReviewManagementPage() {
  const [overview, setOverview] = useState<LiteratureReviewWorkspaceOverview | null>(null)
  const [selectedReviewId, setSelectedReviewId] = useState('')
  const [detail, setDetail] = useState<LiteratureReviewWorkspace | null>(null)
  const [selectedPath, setSelectedPath] = useState('')
  const [content, setContent] = useState<LiteratureReviewFileContent | null>(null)
  const [selectedFileGroup, setSelectedFileGroup] = useState('')
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [contentLoading, setContentLoading] = useState(false)

  const loadOverview = useCallback(async () => {
    setLoading(true)
    try {
      const data = await literatureReviewWorkspaceApi.getOverview()
      setOverview(data)
      setSelectedReviewId((current) => {
        if (current && data.workspaces.some((item) => item.literature_review_id === current)) return current
        return data.workspaces[0]?.literature_review_id || ''
      })
    } catch (error) {
      message.error(String((error as Error)?.message || '加载文献综述工作区失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadWorkspace = useCallback(async (reviewId: string) => {
    if (!reviewId) {
      setDetail(null)
      setSelectedPath('')
      setSelectedFileGroup('')
      return
    }
    setDetailLoading(true)
    try {
      const data = await literatureReviewWorkspaceApi.getWorkspace(reviewId)
      setDetail(data)
      setSelectedPath((current) => {
        if (current && data.files.some((item) => item.relative_path === current)) return current
        return pickDefaultFilePath(data.files)
      })
    } catch (error) {
      setDetail(null)
      setSelectedPath('')
      setSelectedFileGroup('')
      message.error(String((error as Error)?.message || '加载文献综述详情失败'))
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  useEffect(() => {
    void loadWorkspace(selectedReviewId)
  }, [loadWorkspace, selectedReviewId])

  useEffect(() => {
    setSelectedFileGroup('')
  }, [selectedReviewId])

  const selectedFile = useMemo(
    () => detail?.files.find((item) => item.relative_path === selectedPath) || null,
    [detail?.files, selectedPath],
  )

  useEffect(() => {
    let cancelled = false
    if (!selectedReviewId || !selectedFile?.previewable) {
      setContent(null)
      return () => {
        cancelled = true
      }
    }

    setContentLoading(true)
    literatureReviewWorkspaceApi
      .getFileContent(selectedReviewId, selectedFile.relative_path)
      .then((data) => {
        if (!cancelled) setContent(data)
      })
      .catch((error) => {
        if (!cancelled) {
          setContent(null)
          message.error(String((error as Error)?.message || '读取文件内容失败'))
        }
      })
      .finally(() => {
        if (!cancelled) setContentLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedFile, selectedReviewId])

  const selectedWorkspaceSummary = useMemo(
    () => overview?.workspaces.find((item) => item.literature_review_id === selectedReviewId) || null,
    [overview?.workspaces, selectedReviewId],
  )

  const filesByGroup = useMemo(() => {
    const grouped = new Map<string, LiteratureReviewWorkspaceFile[]>()
    for (const file of detail?.files || []) {
      const group = file.group || 'other'
      grouped.set(group, [...(grouped.get(group) || []), file])
    }
    return ['root', 'pdf', 'md', 'review', 'searches', 'other']
      .map((group) => ({ group, files: grouped.get(group) || [] }))
      .filter((item) => item.files.length)
  }, [detail?.files])

  useEffect(() => {
    if (!filesByGroup.length) {
      setSelectedFileGroup('')
      return
    }
    const selectedGroup = detail?.files.find((file) => file.relative_path === selectedPath)?.group || ''
    setSelectedFileGroup((current) => {
      if (current && filesByGroup.some((item) => item.group === current)) return current
      return selectedGroup || filesByGroup[0]?.group || ''
    })
  }, [detail?.files, filesByGroup, selectedPath])

  const activeFileGroup = useMemo(
    () => filesByGroup.find((item) => item.group === selectedFileGroup) || filesByGroup[0] || null,
    [filesByGroup, selectedFileGroup],
  )

  const handleDownload = async (file: LiteratureReviewWorkspaceFile) => {
    if (!selectedReviewId) return
    try {
      const blob = await literatureReviewWorkspaceApi.downloadFile(selectedReviewId, file.relative_path)
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = file.name
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (error) {
      message.error(String((error as Error)?.message || '下载文件失败'))
    }
  }

  return (
    <div className="premium-command-page relative min-h-full space-y-6 overflow-hidden text-slate-200">
      <div className="pointer-events-none absolute -left-24 top-0 h-72 w-72 rounded-full bg-cyan-300/[0.03] blur-3xl" />
      <div className="pointer-events-none absolute right-0 top-40 h-96 w-96 rounded-full bg-emerald-300/[0.03] blur-3xl" />

      <div className={`relative overflow-hidden rounded-[32px] ${glassPanelClass} p-6`}>
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_0%,rgba(34,211,238,0.11),transparent_34%),linear-gradient(135deg,rgba(2,6,23,0.1),rgba(15,23,42,0.42))]" />
        <div className="relative flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-4xl space-y-3">
            <div className="inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-300/[0.08] px-3 py-1 text-xs uppercase tracking-[0.22em] text-cyan-200">
              <BookOutlined />
              Literature Review Workspace
            </div>
            <Title level={3} className="!mb-0 !text-slate-50">
              文献综述管理
            </Title>
            <Paragraph className="!mb-0 !text-slate-400">
              专门浏览 literature-review skill 的文件工作区：PDF、全文 Markdown、解析 JSON 和单篇/最终综述 Markdown。
            </Paragraph>
          </div>
          <Button icon={<ReloadOutlined />} loading={loading} className={darkButtonClass} onClick={() => void loadOverview()}>
            刷新
          </Button>
        </div>

        <div className="relative mt-5 grid gap-3 sm:grid-cols-4">
          <div className="rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80">Workspaces</div>
            <div className="mt-1 text-2xl font-semibold text-slate-50">{overview?.workspaces.length || 0}</div>
          </div>
          <div className="rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80">PDF</div>
            <div className="mt-1 text-2xl font-semibold text-slate-50">{selectedWorkspaceSummary?.counts.pdf || 0}</div>
          </div>
          <div className="rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80">Markdown</div>
            <div className="mt-1 text-2xl font-semibold text-slate-50">{selectedWorkspaceSummary?.counts.md || 0}</div>
          </div>
          <div className="rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80">Reviews</div>
            <div className="mt-1 text-2xl font-semibold text-slate-50">{selectedWorkspaceSummary?.counts.review || 0}</div>
          </div>
        </div>
      </div>

      {loading && !overview ? (
        <div className={`relative flex min-h-[420px] items-center justify-center rounded-[30px] ${glassPanelClass}`}>
          <Spin />
        </div>
      ) : (
        <div className="relative grid min-h-[640px] gap-5 xl:grid-cols-[340px_360px_minmax(0,1fr)]">
          <aside className={`rounded-[30px] ${glassPanelClass} p-3`}>
            <div className="mb-3 flex items-center justify-between px-2">
              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400/70">Review Index</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">{overview?.workspaces.length || 0} 个任务</div>
              </div>
              <Tag color="cyan">skill</Tag>
            </div>
            {overview?.workspaces.length ? (
              <div className="space-y-2">
                {overview.workspaces.map((workspace) => {
                  const active = workspace.literature_review_id === selectedReviewId
                  const statusMeta = statusMetaMap[workspace.status] || statusMetaMap.created
                  return (
                    <button
                      key={workspace.literature_review_id}
                      type="button"
                      className={`group relative w-full overflow-hidden rounded-3xl border px-4 py-3 text-left transition-all duration-300 ${active
                        ? 'translate-x-1 border-white/[0.10] bg-slate-700/45 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]'
                        : 'border-white/[0.07] bg-[#061122]/[0.72] hover:translate-x-1 hover:border-cyan-400/[0.16] hover:bg-[#08182c]'
                        }`}
                      onClick={() => setSelectedReviewId(workspace.literature_review_id)}
                    >
                      {active ? (
                        <>
                          <span className="pointer-events-none absolute inset-y-4 left-0 w-[2px] rounded-r-full bg-slate-400/70" />
                        </>
                      ) : null}
                      <div className="relative z-10 flex items-start gap-3">
                        <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-2xl border ${active ? 'border-cyan-300/35 bg-cyan-300/10 text-cyan-200' : 'border-white/[0.08] bg-white/[0.04] text-slate-400'}`}>
                          <BookOutlined />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-semibold text-slate-100">
                            {workspace.topic || workspace.literature_review_id}
                          </span>
                          <span className="mt-1 block truncate font-mono text-[11px] text-slate-500">
                            {workspace.literature_review_id}
                          </span>
                          <span className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400/80">
                            <span>{workspace.paper_count} papers</span>
                            <span>{formatDateTime(workspace.modified_at)}</span>
                          </span>
                          <span className={`mt-2 inline-flex rounded-full border px-2 py-0.5 text-[11px] ${statusMeta.className}`}>
                            {statusMeta.label}
                          </span>
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : (
              <div className="flex min-h-[360px] items-center justify-center rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">暂无文献综述工作区</span>} />
              </div>
            )}
          </aside>

          <section className={`flex min-h-0 flex-col rounded-[30px] ${glassPanelClass} p-3`}>
            <div className="mb-3 flex items-center justify-between px-2">
              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400/70">Workspace Files</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">{detail?.files.length || 0} 个文件</div>
              </div>
              {detailLoading ? <Spin size="small" /> : <FolderOpenOutlined className="text-cyan-200" />}
            </div>

            {detail ? (
              <div className="flex max-h-[calc(100vh-330px)] min-h-0 flex-1 flex-col gap-3 overflow-hidden">
                <Alert
                  type="info"
                  showIcon
                  className="!border-cyan-300/15 !bg-cyan-300/[0.06] !text-slate-300"
                  message={detail.topic || '未记录主题'}
                  description={<span className="text-xs text-slate-400">{detail.root_path}</span>}
                />
                <div className="grid shrink-0 grid-cols-2 gap-2">
                  {filesByGroup.map(({ group, files }) => {
                    const active = group === activeFileGroup?.group
                    return (
                      <button
                        key={group}
                        type="button"
                        className={`group relative overflow-hidden rounded-2xl border px-3 py-2.5 text-left transition-all duration-300 ${active
                          ? 'border-white/[0.10] bg-slate-700/40 shadow-[inset_0_1px_0_rgba(255,255,255,0.035)]'
                          : 'border-white/[0.07] bg-[#020817]/45 hover:border-cyan-300/[0.16] hover:bg-[#061122]'
                          }`}
                        onClick={() => setSelectedFileGroup(group)}
                      >
                        {active ? <span className="pointer-events-none absolute inset-y-3 left-0 w-[2px] rounded-r-full bg-slate-400/70" /> : null}
                        <span className="relative z-10 flex items-center justify-between gap-2">
                          <span className="flex min-w-0 items-center gap-2">
                            <FolderOpenOutlined className={active ? 'text-teal-300/80' : 'text-slate-500'} />
                            <span className="truncate text-xs font-semibold text-slate-200">{groupLabels[group] || group}</span>
                          </span>
                          <span className="shrink-0 rounded-full border border-white/[0.08] bg-white/[0.04] px-2 py-0.5 text-[11px] text-slate-400">
                            {files.length}
                          </span>
                        </span>
                      </button>
                    )
                  })}
                </div>

                <div className="min-h-0 flex-1 overflow-hidden rounded-3xl border border-white/[0.07] bg-[#020817]/35 shadow-[inset_0_1px_0_rgba(255,255,255,0.035)]">
                  <div className="flex items-center justify-between border-b border-white/[0.06] bg-[linear-gradient(135deg,rgba(15,23,42,0.88),rgba(8,24,44,0.58))] px-3 py-2.5">
                    <span className="text-[11px] uppercase tracking-[0.18em] text-cyan-100/70">
                      {activeFileGroup ? groupLabels[activeFileGroup.group] || activeFileGroup.group : 'Files'}
                    </span>
                    <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2 py-0.5 text-[11px] text-slate-400">
                      {activeFileGroup?.files.length || 0}
                    </span>
                  </div>
                  <div className="h-full min-h-0 space-y-1.5 overflow-auto p-2 pb-12">
                    {(activeFileGroup?.files || []).map((file) => {
                      const active = file.relative_path === selectedPath
                      return (
                        <button
                          key={file.relative_path}
                          type="button"
                          className={`group relative flex w-full items-start gap-3 overflow-hidden rounded-2xl border px-3 py-2.5 text-left transition-all duration-300 ${active
                            ? 'border-white/[0.10] bg-slate-700/40'
                            : 'border-white/[0.07] bg-[#020817]/45 hover:border-cyan-300/[0.18] hover:bg-[#061122]'
                            }`}
                          onClick={() => setSelectedPath(file.relative_path)}
                        >
                          {active ? <span className="pointer-events-none absolute inset-y-3 left-0 w-[2px] rounded-r-full bg-slate-400/70" /> : null}
                          <span className="relative z-10 mt-0.5">{fileIcon(file)}</span>
                          <span className="relative z-10 min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium text-slate-200">{file.name}</span>
                            <span className="mt-1 block truncate font-mono text-[11px] text-slate-500">{file.relative_path}</span>
                            <span className="mt-1 block text-[11px] text-slate-500">{formatBytes(file.size)}</span>
                          </span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              </div>
            ) : (
              <div className="flex min-h-[360px] items-center justify-center rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55">
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">请选择一个综述任务</span>} />
              </div>
            )}
          </section>

          <section className={`flex min-h-0 flex-col overflow-hidden rounded-[30px] ${glassPanelClass}`}>
            <div className="border-b border-white/[0.07] bg-[#061122]/[0.78] px-5 py-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[11px] uppercase tracking-[0.2em] text-cyan-200/80">Preview</div>
                  <div className="mt-1 truncate text-lg font-semibold text-slate-50">
                    {selectedFile?.relative_path || '未选择文件'}
                  </div>
                  {selectedFile ? (
                    <div className="mt-1 text-xs text-slate-500">
                      {formatBytes(selectedFile.size)} · {formatDateTime(selectedFile.modified_at)}
                    </div>
                  ) : null}
                </div>
                {selectedFile ? (
                  <Button
                    icon={<DownloadOutlined />}
                    className={darkButtonClass}
                    onClick={() => void handleDownload(selectedFile)}
                  >
                    下载
                  </Button>
                ) : null}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto bg-[#020817]/55 p-5">
              {!selectedFile ? (
                <div className="flex h-full min-h-[420px] items-center justify-center">
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">选择左侧 JSON 或 Markdown 文件查看内容</span>} />
                </div>
              ) : selectedFile.suffix === '.pdf' ? (
                <div className="rounded-3xl border border-white/[0.08] bg-[#061122]/70 p-5">
                  <Space direction="vertical" size={12}>
                    <FilePdfOutlined className="text-3xl text-red-300" />
                    <Text className="!text-slate-100">{selectedFile.name}</Text>
                    <Text className="!font-mono !text-xs !text-slate-500">{selectedFile.relative_path}</Text>
                    <Text className="!text-sm !text-slate-400">PDF 文件暂不内嵌预览，可直接下载或打开对应 Markdown 解析结果。</Text>
                  </Space>
                </div>
              ) : contentLoading ? (
                <div className="flex h-full min-h-[420px] items-center justify-center">
                  <Spin />
                </div>
              ) : content ? (
                <>
                  {content.truncated ? (
                    <Alert
                      type="warning"
                      showIcon
                      className="!mb-4 !border-amber-300/20 !bg-amber-300/[0.08] !text-slate-200"
                      message="文件较大，当前预览已截断。"
                    />
                  ) : null}
                  {content.suffix === '.json' ? (
                    <pre className="min-h-[420px] overflow-auto rounded-3xl border border-white/[0.08] bg-slate-950/80 p-4 font-mono text-xs leading-6 text-slate-300">
                      {content.content}
                    </pre>
                  ) : (
                    <div className="min-h-[420px] rounded-3xl border border-white/[0.08] bg-slate-950/60 px-5 py-4 text-sm leading-7 text-slate-200 [&_a]:text-cyan-300 [&_code]:rounded [&_code]:bg-white/[0.08] [&_code]:px-1 [&_h1]:mt-2 [&_h1]:text-2xl [&_h1]:font-semibold [&_h2]:mt-6 [&_h2]:text-xl [&_h2]:font-semibold [&_h3]:mt-5 [&_h3]:text-lg [&_h3]:font-semibold [&_li]:my-1 [&_ol]:pl-6 [&_p]:my-3 [&_pre]:overflow-auto [&_pre]:rounded-2xl [&_pre]:bg-black/40 [&_pre]:p-3 [&_strong]:text-slate-50 [&_table]:my-4 [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-white/[0.08] [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-white/[0.08] [&_th]:px-2 [&_th]:py-1">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content.content}</ReactMarkdown>
                    </div>
                  )}
                </>
              ) : (
                <div className="flex h-full min-h-[420px] items-center justify-center">
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">该文件没有可显示内容</span>} />
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
