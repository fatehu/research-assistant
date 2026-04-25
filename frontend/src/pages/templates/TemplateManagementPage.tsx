import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Empty,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  DownOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import {
  docxTemplateApi,
  type DocxManagedFile,
  type DocxTemplate,
  type DocxTemplateOverview,
} from '@/services/api'

const { Paragraph, Text, Title } = Typography
const { TextArea } = Input

type FileRole = DocxManagedFile['file_role']

type TemplateFormValues = {
  name?: string
  description?: string
  md_constraints?: string
  docx_constraints?: string
}

const DRAFT_TEMPLATE_ID = '__draft_template__'

const darkButtonClass =
  '!border-slate-700 !bg-slate-950/60 !text-slate-200 hover:!border-emerald-400/60 hover:!bg-emerald-400/10 hover:!text-emerald-100 disabled:!border-slate-800 disabled:!bg-slate-900 disabled:!text-slate-600 transition-all duration-300'

const glassPanelClass =
  'border border-white/[0.08] bg-[#071322]/[0.82] shadow-[0_24px_70px_rgba(0,0,0,0.36),inset_0_1px_0_rgba(255,255,255,0.04)] backdrop-blur-xl transition-all duration-300'

const fieldClass =
  '!rounded-2xl !border-white/[0.09] !bg-[#061122] !text-slate-100 placeholder:!text-slate-500/60 focus:!border-emerald-400/50 focus:!shadow-[0_0_15px_rgba(52,211,153,0.15)] transition-all'

const editorClass =
  '!min-h-[320px] !resize-y !rounded-2xl !border-white/[0.08] !bg-[#020817] !px-4 !py-3 !font-mono !text-[13px] !leading-6 !text-slate-100 !shadow-[inset_0_18px_46px_rgba(0,0,0,0.28)] placeholder:!text-slate-500/60 focus:!border-emerald-400/50 focus:!shadow-[inset_0_18px_46px_rgba(0,0,0,0.28),0_0_15px_rgba(52,211,153,0.15)] transition-all'

const fileRoleOptions: Array<{ value: FileRole; label: string; help: string; color: string }> = [
  {
    value: 'sample_template',
    label: '成品/样例模板',
    help: '用于分析版式、标题层级、表格、页眉页脚和样例结构。',
    color: 'cyan',
  },
  {
    value: 'writing_guide',
    label: '撰写说明/填报指南',
    help: '用于提取章节要求、字数限制、填报口径和注意事项。',
    color: 'gold',
  },
  {
    value: 'reference',
    label: '普通参考附件',
    help: '只保存并在生成 DOCX 时交给 Claude 参考，不主动总结约束。',
    color: 'default',
  },
]

const roleByValue = Object.fromEntries(fileRoleOptions.map((item) => [item.value, item]))

const formatDateTime = (value?: string) => {
  if (!value) return 'unknown'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const formatSize = (bytes?: number) => {
  const value = Number(bytes || 0)
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

const templateNameFromFile = (file: File) => {
  const stem = String(file.name || '')
    .replace(/\.[^.]+$/, '')
    .trim()
  return stem || `template-${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}`
}

const createDraftTemplate = (): DocxTemplate => ({
  template_id: DRAFT_TEMPLATE_ID,
  name: '未保存模板',
  description: '',
  created_at: '',
  updated_at: '',
  root_path: '',
  files_path: '',
  md_constraints: '',
  docx_constraints: '',
  files: [],
})

const saveBlob = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export default function TemplateManagementPage() {
  const [form] = Form.useForm<TemplateFormValues>()
  const [overview, setOverview] = useState<DocxTemplateOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [savingDefaultPrompt, setSavingDefaultPrompt] = useState(false)
  const [uploadRole, setUploadRole] = useState<FileRole>('writing_guide')
  const [analysisNotes, setAnalysisNotes] = useState('')
  const [analysisResultNotes, setAnalysisResultNotes] = useState('')
  const [defaultDocxStylePrompt, setDefaultDocxStylePrompt] = useState('')
  const [defaultPromptOpen, setDefaultPromptOpen] = useState(false)
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>('')
  const [draftTemplate, setDraftTemplate] = useState<DocxTemplate | null>(null)

  const templateList = useMemo(
    () => (draftTemplate ? [draftTemplate, ...(overview?.templates || [])] : overview?.templates || []),
    [draftTemplate, overview?.templates],
  )

  const selectedTemplate = useMemo<DocxTemplate | null>(() => {
    if (!selectedTemplateId) return null
    return templateList.find((item) => item.template_id === selectedTemplateId) || null
  }, [selectedTemplateId, templateList])

  const isDraftTemplateSelected = selectedTemplateId === DRAFT_TEMPLATE_ID

  const selectedTemplateWorkspaces = useMemo(() => {
    if (!selectedTemplate || isDraftTemplateSelected) return []
    return (overview?.workspaces || []).filter((workspace) => workspace.template_id === selectedTemplate.template_id)
  }, [isDraftTemplateSelected, overview?.workspaces, selectedTemplate])

  const unboundWorkspaceCount = useMemo(
    () => (overview?.workspaces || []).filter((workspace) => !workspace.template_id).length,
    [overview?.workspaces],
  )

  const applyTemplateToForm = useCallback((template: DocxTemplate | null) => {
    setAnalysisResultNotes('')
    form.setFieldsValue({
      name: template?.template_id === DRAFT_TEMPLATE_ID ? '' : template?.name || '',
      description: template?.description || '',
      md_constraints: template?.md_constraints || '',
      docx_constraints: template?.docx_constraints || '',
    })
  }, [form])

  const loadOverview = useCallback(async (options?: { keepSelection?: boolean }) => {
    setLoading(true)
    try {
      const data = await docxTemplateApi.getOverview()
      setOverview(data)
      setDefaultDocxStylePrompt(data.default_docx_style_prompt || '')
      if (!options?.keepSelection) {
        setSelectedTemplateId((current) => current || data.templates[0]?.template_id || '')
      } else {
        setSelectedTemplateId((current) => (
          current === DRAFT_TEMPLATE_ID
            ? current
            : current && data.templates.some((item) => item.template_id === current)
              ? current
              : data.templates[0]?.template_id || ''
        ))
      }
    } catch (error) {
      message.error(String((error as Error)?.message || '加载模板失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadOverview()
  }, [loadOverview])

  useEffect(() => {
    applyTemplateToForm(selectedTemplate)
  }, [applyTemplateToForm, selectedTemplate])

  const handleNewTemplate = () => {
    const draft = createDraftTemplate()
    setDraftTemplate(draft)
    setSelectedTemplateId(DRAFT_TEMPLATE_ID)
    applyTemplateToForm(draft)
  }

  const handleSelectTemplate = (template: DocxTemplate) => {
    setSelectedTemplateId(template.template_id)
    applyTemplateToForm(template)
  }

  const handleFormValuesChange = (_: Partial<TemplateFormValues>, values: TemplateFormValues) => {
    if (!isDraftTemplateSelected) return
    setDraftTemplate((current) => (
      current
        ? {
            ...current,
            name: String(values.name || '') || '未保存模板',
            description: values.description || '',
            md_constraints: values.md_constraints || '',
            docx_constraints: values.docx_constraints || '',
            updated_at: '',
          }
        : current
    ))
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const saved = await docxTemplateApi.saveTemplate({
        template_id: isDraftTemplateSelected ? undefined : selectedTemplate?.template_id,
        name: values.name || '',
        description: values.description || '',
        md_constraints: values.md_constraints || '',
        docx_constraints: values.docx_constraints || '',
      })
      setDraftTemplate(null)
      setSelectedTemplateId(saved.template_id)
      await loadOverview({ keepSelection: true })
      setSelectedTemplateId(saved.template_id)
      message.success('模板已保存')
    } catch (error) {
      message.error(String((error as Error)?.message || '保存模板失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleSaveDefaultPrompt = async () => {
    setSavingDefaultPrompt(true)
    try {
      const saved = await docxTemplateApi.updateDefaultDocxStylePrompt(defaultDocxStylePrompt)
      setDefaultDocxStylePrompt(saved.default_docx_style_prompt || '')
      await loadOverview({ keepSelection: true })
      message.success('默认 DOCX 样式提示词已保存')
    } catch (error) {
      message.error(String((error as Error)?.message || '保存默认提示词失败'))
    } finally {
      setSavingDefaultPrompt(false)
    }
  }

  const ensureTemplateForUpload = async (file: File): Promise<DocxTemplate> => {
    if (selectedTemplate && !isDraftTemplateSelected) return selectedTemplate
    const values = form.getFieldsValue()
    const saved = await docxTemplateApi.saveTemplate({
      name: values.name || templateNameFromFile(file),
      description: values.description || '',
      md_constraints: values.md_constraints || '',
      docx_constraints: values.docx_constraints || '',
    })
    setDraftTemplate(null)
    setSelectedTemplateId(saved.template_id)
    return saved
  }

  const handleUpload = async (file: File) => {
    setUploading(true)
    try {
      const targetTemplate = await ensureTemplateForUpload(file)
      await docxTemplateApi.uploadTemplateFile(targetTemplate.template_id, file, uploadRole)
      await loadOverview({ keepSelection: true })
      setSelectedTemplateId(targetTemplate.template_id)
      message.success('文件已上传')
    } catch (error) {
      message.error(String((error as Error)?.message || '上传文件失败'))
    } finally {
      setUploading(false)
    }
    return false
  }

  const handleFileRoleChange = async (file: DocxManagedFile, fileRole: FileRole) => {
    if (!selectedTemplate) return
    try {
      await docxTemplateApi.updateTemplateFileRole(selectedTemplate.template_id, templateFileOperationName(file), fileRole)
      await loadOverview({ keepSelection: true })
      message.success('文件类型已更新')
    } catch (error) {
      message.error(String((error as Error)?.message || '更新文件类型失败'))
    }
  }

  const handleAnalyze = async () => {
    if (!selectedTemplate) {
      message.warning('请先保存或选择一个模板')
      return
    }
    setAnalyzing(true)
    try {
      const result = await docxTemplateApi.analyzeTemplate(selectedTemplate.template_id, analysisNotes)
      form.setFieldsValue({
        md_constraints: result.md_constraints || '',
        docx_constraints: result.docx_constraints || '',
      })
      setAnalysisResultNotes(result.notes || '')
      message.success('已生成约束草稿，请检查后保存')
    } catch (error) {
      message.error(String((error as Error)?.message || '分析生成失败'))
    } finally {
      setAnalyzing(false)
    }
  }

  const handleDownload = async (file: DocxManagedFile) => {
    try {
      const blob = await docxTemplateApi.downloadFile(file.download_path || file.relative_path)
      saveBlob(blob, file.name)
    } catch (error) {
      message.error(String((error as Error)?.message || '下载文件失败'))
    }
  }

  const templateFileOperationName = (file: DocxManagedFile) => file.stored_name || file.name

  const handleTemplateFileDelete = async (file: DocxManagedFile) => {
    if (!selectedTemplate || isDraftTemplateSelected) {
      message.warning('请先选择已保存的模板')
      return
    }
    try {
      await docxTemplateApi.deleteTemplateFile(selectedTemplate.template_id, templateFileOperationName(file))
      message.success('已删除模板附件')
      await loadOverview({ keepSelection: true })
    } catch (error) {
      message.error(String((error as Error)?.message || '删除文件失败'))
    }
  }

  const renderFileList = (
    files: DocxManagedFile[],
    emptyText: string,
    options?: { editableRole?: boolean },
  ) => {
    if (!files.length) {
      return (
        <div className="group relative flex flex-col items-center justify-center overflow-hidden rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55 px-5 py-12 transition-all hover:border-emerald-500/30 hover:bg-[#020817]/80">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_50%,rgba(52,211,153,0.05),transparent_60%)] opacity-0 transition-opacity group-hover:opacity-100" />
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">{emptyText}</span>} />
        </div>
      )
    }
    return (
      <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
        {files.map((file) => {
          const role = file.file_role || 'reference'
          const roleOption = roleByValue[role] || roleByValue.reference
          return (
            <div
              key={`${file.relative_path}-${file.name}`}
              className="group relative overflow-hidden rounded-3xl border border-white/[0.08] bg-[#061122]/[0.88] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.035)] transition-all hover:-translate-y-0.5 hover:border-emerald-400/[0.2] hover:bg-[#08182c]"
            >
              <div className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-emerald-400/[0.04] blur-2xl transition-opacity group-hover:opacity-100" />
              <div className="relative flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border border-white/[0.08] bg-white/[0.04] text-emerald-200">
                  <FileTextOutlined />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold text-slate-100">{file.name}</div>
                  <div className="mt-1 truncate font-mono text-[11px] text-slate-400/60">{file.relative_path}</div>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400/70">
                    <span className="rounded-full border border-white/[0.07] bg-white/[0.035] px-2 py-0.5">{formatSize(file.size)}</span>
                    <span className="rounded-full border border-white/[0.07] bg-white/[0.035] px-2 py-0.5">{formatDateTime(file.modified_at)}</span>
                  </div>
                </div>
              </div>
              <div className="relative mt-4 flex flex-wrap items-center justify-between gap-2">
                {options?.editableRole ? (
                  <Select<FileRole>
                    key="role"
                    size="small"
                    value={file.file_role || 'reference'}
                    className="min-w-[170px]"
                    popupClassName="premium-command-select-dropdown"
                    options={fileRoleOptions.map((item) => ({ value: item.value, label: item.label }))}
                    onChange={(value) => void handleFileRoleChange(file, value)}
                  />
                ) : (
                  <Tag color={roleOption.color}>{roleOption.label}</Tag>
                )}
                <Space size={8}>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    className={darkButtonClass}
                    onClick={() => void handleDownload(file)}
                  >
                    下载
                  </Button>
                  {options?.editableRole ? (
                    <Popconfirm
                      title="删除模板附件"
                      description="只删除这个模板下的附件文件，不影响已生成的 DOCX 工作区。"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => void handleTemplateFileDelete(file)}
                    >
                      <Button
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        className="!border-red-400/30 !bg-red-500/[0.08] !text-red-100 hover:!border-red-300/60 hover:!bg-red-500/[0.18] transition-all"
                      >
                        删除
                      </Button>
                    </Popconfirm>
                  ) : null}
                </Space>
              </div>
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className="premium-command-page relative min-h-full space-y-6 overflow-hidden text-slate-200">
      <div className="pointer-events-none absolute -left-24 top-0 h-72 w-72 rounded-full bg-emerald-300/[0.03] blur-3xl" />
      <div className="pointer-events-none absolute right-0 top-40 h-96 w-96 rounded-full bg-cyan-300/[0.03] blur-3xl" />

      <div className={`relative overflow-hidden rounded-[32px] ${glassPanelClass} p-6 hover:shadow-[0_24px_60px_rgba(52,211,153,0.06)]`}>
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_0%,rgba(45,212,191,0.08),transparent_38%),linear-gradient(135deg,rgba(2,6,23,0.1),rgba(15,23,42,0.42))]" />
        <div className="relative flex flex-wrap items-start justify-between gap-5">
          <div className="max-w-4xl space-y-3">
            <div className="inline-flex items-center gap-2 rounded-full border border-emerald-300/20 bg-emerald-300/[0.08] px-3 py-1 text-xs uppercase tracking-[0.22em] text-emerald-200 shadow-[0_0_15px_rgba(52,211,153,0.1)]">
              <FileTextOutlined />
              Template Command Center
            </div>
            <Title level={3} className="!mb-0 !text-slate-50">
              DOCX 模板管理
            </Title>
            <Paragraph className="!mb-0 !text-slate-400">
              上传样例、指南或附件后，点击分析生成 MD/DOCX 约束草稿；AI 只产出可编辑草稿，最终规则以用户保存内容为准。
            </Paragraph>
          </div>
          <Space wrap>
            <Button
              icon={<ReloadOutlined />}
              className="!border-white/[0.1] !bg-white/[0.03] !text-slate-300 hover:!border-emerald-400/50 hover:!bg-emerald-400/10 hover:!text-emerald-200 transition-all"
              onClick={() => void loadOverview({ keepSelection: true })}
            >
              刷新
            </Button>
            <Button
              type="primary"
              onClick={handleNewTemplate}
              className="!border-emerald-400/50 !bg-gradient-to-r !from-emerald-500/20 !to-teal-500/20 !text-emerald-50 hover:!from-emerald-500/40 hover:!to-teal-500/40 hover:!text-white shadow-[0_0_20px_rgba(52,211,153,0.25)] hover:shadow-[0_0_30px_rgba(52,211,153,0.4)] transition-all duration-300"
            >
              新建模板
            </Button>
          </Space>
        </div>
        <div className="relative mt-5 grid gap-3 md:grid-cols-2">
          <div className="group rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3 transition-all duration-300 hover:border-emerald-400/30 hover:bg-[#020817]/70">
            <div className="text-[11px] uppercase tracking-[0.18em] text-emerald-200/80 transition-colors group-hover:text-emerald-200">DOCX Root</div>
            <div className="mt-1 break-all font-mono text-xs text-slate-300">{overview?.docx_root || '/app/uploads/docx'}</div>
          </div>
          <div className="group rounded-2xl border border-white/[0.07] bg-[#020817]/45 px-4 py-3 transition-all duration-300 hover:border-emerald-400/30 hover:bg-[#020817]/70">
            <div className="text-[11px] uppercase tracking-[0.18em] text-emerald-200/80 transition-colors group-hover:text-emerald-200">Templates Root</div>
            <div className="mt-1 break-all font-mono text-xs text-slate-300">{overview?.templates_root || '/app/uploads/docx/templates'}</div>
          </div>
        </div>
      </div>

      <div className="group relative mb-8 border-b border-white/[0.08] pb-5 transition-all duration-300 hover:bg-white/[0.01]">
        <div className="flex flex-wrap items-center justify-between gap-4 px-2">
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-4 text-left !bg-transparent border-none outline-none cursor-pointer group/btn"
            onClick={() => setDefaultPromptOpen((open) => !open)}
          >
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border border-white/[0.1] bg-white/[0.04] text-emerald-200 group-hover/btn:border-emerald-400/40 group-hover/btn:bg-emerald-400/10 transition-all shadow-sm">
              <SettingOutlined />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-semibold text-slate-50 group-hover/btn:text-emerald-50 transition-colors">平台默认 DOCX 样式提示词</span>
              <span className="block mt-0.5 text-xs text-slate-300/70 leading-relaxed">
                自动追加到 docx_generate_with_claude 的 requirements.md，模板 DOCX 约束会继续覆盖或细化。
              </span>
            </span>
            <DownOutlined className={`text-slate-500 transition-transform group-hover/btn:text-emerald-300 ${defaultPromptOpen ? 'rotate-180' : ''}`} />
          </button>
          <Button
            icon={<SaveOutlined />}
            loading={savingDefaultPrompt}
            className="!border-white/[0.1] !bg-white/[0.03] !text-slate-300 hover:!border-emerald-400/50 hover:!bg-emerald-400/10 hover:!text-emerald-200 transition-all"
            onClick={() => void handleSaveDefaultPrompt()}
          >
            保存默认提示词
          </Button>
        </div>
        {defaultPromptOpen ? (
          <div className="mt-4">
            <TextArea
              rows={8}
              className={editorClass}
              value={defaultDocxStylePrompt}
              onChange={(event) => setDefaultDocxStylePrompt(event.target.value)}
              placeholder="平台默认 DOCX 样式与结构要求"
            />
          </div>
        ) : null}
      </div>

      {loading && !overview ? (
        <div className={`relative flex min-h-[420px] items-center justify-center rounded-[30px] ${glassPanelClass}`}>
          <Spin />
        </div>
      ) : (
        <div className="relative grid gap-5 xl:grid-cols-[360px_minmax(0,1fr)]">
          <aside className={`rounded-[30px] ${glassPanelClass} p-3`}>
            <div className="mb-3 flex items-center justify-between px-2">
              <div>
                <div className="text-xs uppercase tracking-[0.2em] text-slate-400/70">Templates</div>
                <div className="mt-1 text-sm font-semibold text-slate-100">{templateList.length} 个模板</div>
              </div>
              <span className="rounded-full border border-emerald-300/20 bg-emerald-300/[0.08] px-2.5 py-1 text-xs text-emerald-200">
                library
              </span>
            </div>
            {templateList.length ? (
              <div className="space-y-2">
                {templateList.map((template) => {
                  const active = template.template_id === selectedTemplateId
                  const isDraft = template.template_id === DRAFT_TEMPLATE_ID
                  return (
                    <button
                      key={template.template_id}
                      type="button"
                      className={`group relative w-full overflow-hidden rounded-3xl border px-4 py-3 text-left transition-all duration-300 ${active
                        ? 'border-emerald-400/[0.2] bg-gradient-to-r from-emerald-400/[0.06] via-[#0a1a2e] to-[#061122] shadow-[0_8px_24px_rgba(16,185,129,0.04)] translate-x-1'
                        : 'border-white/[0.07] bg-[#061122]/[0.72] hover:border-emerald-400/[0.15] hover:bg-[#08182c] hover:translate-x-1 hover:shadow-[0_4px_16px_rgba(16,185,129,0.04)]'
                        }`}
                      onClick={() => handleSelectTemplate(template)}
                    >
                      {active ? <span className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-emerald-400" /> : null}
                      <div className="flex items-start gap-3">
                        <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-2xl border transition-colors duration-300 ${active ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-300' : 'border-white/[0.08] bg-white/[0.04] text-emerald-200 group-hover:border-emerald-400/30 group-hover:bg-emerald-400/5 group-hover:text-emerald-300'}`}>
                          <FileTextOutlined />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className={`block truncate text-sm font-semibold transition-colors duration-300 ${active ? 'text-emerald-50' : 'text-slate-100 group-hover:text-emerald-100'}`}>{template.name}</span>
                          <span className="mt-1 block truncate font-mono text-[11px] text-slate-500">
                            {isDraft ? 'draft · 保存后创建目录' : template.template_id}
                          </span>
                          <span className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400/70 transition-colors group-hover:text-slate-300">
                            <span>{template.files.length} files</span>
                            <span>{isDraft ? '未保存' : formatDateTime(template.updated_at)}</span>
                          </span>
                        </span>
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : (
              <div className="group relative flex flex-col items-center justify-center overflow-hidden rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55 px-5 py-12 transition-all hover:border-emerald-500/30 hover:bg-[#020817]/80">
                <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_50%,rgba(52,211,153,0.05),transparent_60%)] opacity-0 transition-opacity group-hover:opacity-100" />
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">暂无模板，先新建一个</span>} />
              </div>
            )}
          </aside>

          <div className="space-y-5">
            <section className={`overflow-hidden rounded-[30px] ${glassPanelClass}`}>
              <div className="border-b border-white/[0.07] bg-[#061122]/[0.78] px-5 py-4">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div>
                    <div className="text-[11px] uppercase tracking-[0.2em] text-emerald-200/80">
                      {selectedTemplate && !isDraftTemplateSelected ? 'Editing Template' : 'New Template'}
                    </div>
                    <div className="mt-1 text-lg font-semibold text-slate-50">
                      {selectedTemplate ? selectedTemplate.name : '新建模板'}
                    </div>
                  </div>
                  <Space wrap>
                    <Select<FileRole>
                      size="middle"
                      value={uploadRole}
                      className="min-w-[190px]"
                      popupClassName="premium-command-select-dropdown"
                      options={fileRoleOptions.map((item) => ({ value: item.value, label: item.label }))}
                      onChange={setUploadRole}
                    />
                    <Upload
                      showUploadList={false}
                      beforeUpload={(file) => {
                        void handleUpload(file as File)
                        return false
                      }}
                    >
                      <Tooltip title={isDraftTemplateSelected || !selectedTemplate ? '未保存草稿会先自动创建模板，再上传文件' : roleByValue[uploadRole].help}>
                        <Button
                          icon={<UploadOutlined />}
                          disabled={uploading}
                          loading={uploading}
                          className={darkButtonClass}
                        >
                          上传文件
                        </Button>
                      </Tooltip>
                    </Upload>
                    <Button
                      icon={<ThunderboltOutlined />}
                      loading={analyzing}
                      disabled={!selectedTemplate || !(selectedTemplate.files || []).length}
                      className={`${darkButtonClass} ${selectedTemplate?.files?.length ? 'shadow-[0_0_24px_rgba(16,185,129,0.12)]' : ''}`}
                      onClick={() => void handleAnalyze()}
                    >
                      分析生成约束
                    </Button>
                    <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={() => void handleSave()} className="!border-emerald-400/50 !bg-gradient-to-r !from-emerald-500/20 !to-teal-500/20 !text-emerald-50 hover:!from-emerald-500/40 hover:!to-teal-500/40 hover:!text-white shadow-[0_0_15px_rgba(52,211,153,0.2)] hover:shadow-[0_0_25px_rgba(52,211,153,0.35)] transition-all">
                      保存模板
                    </Button>
                  </Space>
                </div>
              </div>

              <div className="p-5">
                <Form form={form} layout="vertical" requiredMark={false} onValuesChange={handleFormValuesChange}>
                  <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(280px,0.42fr)]">
                    <Form.Item
                      name="name"
                      label={<span className="text-slate-300">模板名称</span>}
                      rules={[{ required: true, message: '请输入模板名称' }]}
                    >
                      <Input className={fieldClass} placeholder="例如：国自然面上项目 2026" />
                    </Form.Item>
                    <Form.Item name="description" label={<span className="text-slate-300">说明</span>}>
                      <TextArea
                        rows={2}
                        className={fieldClass}
                        placeholder="这个模板适用于什么文档，给用户和模型看的简短说明。"
                      />
                    </Form.Item>
                  </div>

                  <div className="mb-5 rounded-3xl border border-white/[0.08] bg-[#020817]/55 p-4">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                      <Text className="!text-sm !font-semibold !text-slate-200">分析补充说明</Text>
                      {analysisResultNotes ? (
                        <Text className="!text-xs !text-amber-300">分析备注：{analysisResultNotes}</Text>
                      ) : null}
                    </div>
                    <TextArea
                      rows={2}
                      className={fieldClass}
                      value={analysisNotes}
                      onChange={(event) => setAnalysisNotes(event.target.value)}
                      placeholder="可选：告诉模型这个模板面向什么场景，比如“按国自然面上项目申请书生成”。"
                    />
                  </div>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <div className="group rounded-3xl border border-white/[0.08] bg-[#061122]/[0.58] p-4 transition-all hover:border-cyan-400/30 hover:bg-[#061122]/80 hover:shadow-[0_8px_24px_rgba(34,211,238,0.06)] relative overflow-hidden">
                      <div className="pointer-events-none absolute -left-10 -top-10 h-24 w-24 rounded-full bg-cyan-400/10 blur-2xl transition-opacity group-hover:opacity-100 opacity-0" />
                      <div className="mb-3 flex items-center justify-between gap-2 relative">
                        <Tooltip title="给平台默认模型使用，要求它按固定章节、字段和证据格式生成 Markdown。">
                          <span className="text-sm font-semibold text-slate-100 transition-colors group-hover:text-cyan-100">MD 生成约束</span>
                        </Tooltip>
                        <span className="rounded-full border border-cyan-300/20 bg-cyan-300/[0.08] px-2 py-0.5 text-[11px] text-cyan-200 shadow-[0_0_8px_rgba(34,211,238,0.1)]">
                          model prompt
                        </span>
                      </div>
                      <Form.Item name="md_constraints" noStyle>
                        <TextArea className={editorClass} placeholder="点击“分析生成约束”后会填入草稿，也可以手动编辑。" />
                      </Form.Item>
                    </div>
                    <div className="group rounded-3xl border border-white/[0.08] bg-[#061122]/[0.58] p-4 transition-all hover:border-emerald-400/30 hover:bg-[#061122]/80 hover:shadow-[0_8px_24px_rgba(52,211,153,0.06)] relative overflow-hidden">
                      <div className="pointer-events-none absolute -right-10 -top-10 h-24 w-24 rounded-full bg-emerald-400/10 blur-2xl transition-opacity group-hover:opacity-100 opacity-0" />
                      <div className="mb-3 flex items-center justify-between gap-2 relative">
                        <Tooltip title="交给 Claude Code 使用，描述版式、标题层级、图表、页眉页脚、参考模板文件等要求。">
                          <span className="text-sm font-semibold text-slate-100 transition-colors group-hover:text-emerald-100">DOCX 生成约束</span>
                        </Tooltip>
                        <span className="rounded-full border border-emerald-300/20 bg-emerald-300/[0.08] px-2 py-0.5 text-[11px] text-emerald-200 shadow-[0_0_8px_rgba(52,211,153,0.1)]">
                          claude prompt
                        </span>
                      </div>
                      <Form.Item name="docx_constraints" noStyle>
                        <TextArea className={editorClass} placeholder="点击“分析生成约束”后会填入草稿，也可以手动编辑。" />
                      </Form.Item>
                    </div>
                  </div>
                </Form>
              </div>
            </section>

            <section className={`rounded-[30px] ${glassPanelClass} p-5`}>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-slate-100">模板附件</div>
                  <div className="mt-1 text-xs text-slate-400/80">
                    上传时选择文件类型；样例偏版式，指南偏内容约束，普通附件只给 Claude 参考。
                  </div>
                </div>
                <Tag color="green">template_files</Tag>
              </div>
              {renderFileList(selectedTemplate?.files || [], '这个模板还没有上传文件', { editableRole: true })}
            </section>

            <section className={`rounded-[30px] ${glassPanelClass} p-5`}>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                <div>
                  <Space className="text-slate-100">
                    <FolderOpenOutlined />
                    <span className="font-semibold">DOCX 生成工作区</span>
                  </Space>
                  <div className="mt-1 text-xs text-slate-400/80">
                    当前只展示这个模板产出的 DOCX/PDF；历史未绑定目录暂不混入当前模板。
                  </div>
                </div>
                <Space size={8}>
                  <Tag>{selectedTemplateWorkspaces.length} current</Tag>
                  {unboundWorkspaceCount ? <Tag color="default">{unboundWorkspaceCount} unbound</Tag> : null}
                </Space>
              </div>
              {selectedTemplateWorkspaces.length ? (
                <div className="space-y-4">
                  {selectedTemplateWorkspaces.map((workspace) => (
                    <div key={workspace.docx_id} className="rounded-3xl border border-white/[0.08] bg-[#020817]/45 p-4">
                      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                        <Space direction="vertical" size={2}>
                          <Text className="!text-slate-100">{workspace.docx_id}</Text>
                          <Text className="!font-mono !text-xs !text-slate-500">{workspace.path}</Text>
                          <Text className="!text-xs !text-slate-500">
                            {workspace.artifact_id ? `artifact: ${workspace.artifact_id}` : 'artifact: 未记录'}
                            {workspace.conversation_id ? ` · chat: ${workspace.conversation_id}` : ''}
                          </Text>
                        </Space>
                        <Space size={8}>
                          {workspace.status ? <Tag color={workspace.status === 'completed' ? 'green' : workspace.status === 'failed' ? 'red' : 'blue'}>{workspace.status}</Tag> : null}
                          {workspace.validation_status ? <Tag>{workspace.validation_status}</Tag> : null}
                          <Tag>{workspace.files.length} files</Tag>
                        </Space>
                      </div>
                      {renderFileList(workspace.files, '这个工作区暂无可下载文件')}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="group relative flex flex-col items-center justify-center overflow-hidden rounded-3xl border border-dashed border-white/[0.09] bg-[#020817]/55 px-5 py-12 transition-all hover:border-blue-500/30 hover:bg-[#020817]/80">
                  <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_50%,rgba(59,130,246,0.05),transparent_60%)] opacity-0 transition-opacity group-hover:opacity-100" />
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-400">当前模板暂无 DOCX 生成工作区</span>} />
                </div>
              )}
            </section>
          </div>
        </div>
      )}
    </div>
  )
}
