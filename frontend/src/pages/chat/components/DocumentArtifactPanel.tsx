import { useEffect, useMemo, useState, type SyntheticEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import {
  chatApi,
  docxTemplateApi,
  type DocumentArtifact,
  type DocumentArtifactBlock,
  type DocxTemplate,
} from '@/services/api'
import { Select, ConfigProvider, theme } from 'antd'
import { DownOutlined } from '@ant-design/icons'

type Props = {
  conversationId?: number
  artifact?: DocumentArtifact | null
  disabled?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
  selectedBlockIds: string[]
  onSelectedBlockIdsChange: (blockIds: string[]) => void
  onEnsureConversation: () => Promise<number | null>
  onRefresh: (conversationId?: number) => Promise<void>
}

const cloneArtifact = (value: DocumentArtifact): DocumentArtifact =>
  JSON.parse(JSON.stringify(value)) as DocumentArtifact

const emptyBlock = (index: number): DocumentArtifactBlock => ({
  block_id: `block-${index + 1}`,
  index,
  title: `章节 ${index + 1}`,
  heading_path: [`章节 ${index + 1}`],
  required: true,
  target_words: 0,
  block_constraints: '',
  markdown: '',
  status: 'empty',
})

type ArtifactRewriteSelection = {
  blockId: string
  selectedText: string
  startOffset: number
  endOffset: number
}

const ARTIFACT_REWRITE_OPTIONS = [
  {
    label: '更简洁',
    instruction: '让选中内容更简洁，保留原有事实、术语和 Markdown 结构。',
  },
  {
    label: '更清楚',
    instruction: '让选中内容更清楚、更顺畅，保留原有事实、术语和 Markdown 结构。',
  },
  {
    label: '更正式',
    instruction: '把选中内容改得更正式、更适合科研项目文档，保留原有事实、术语和 Markdown 结构。',
  },
]

const getApiErrorMessage = (err: any, fallback: string) => {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  return err?.message || fallback
}

const DocumentArtifactPanel = ({
  conversationId,
  artifact,
  disabled = false,
  open,
  onOpenChange,
  selectedBlockIds,
  onSelectedBlockIdsChange,
  onEnsureConversation,
  onRefresh,
}: Props) => {
  const [templates, setTemplates] = useState<DocxTemplate[]>([])
  const [templatesLoading, setTemplatesLoading] = useState(false)
  const [templatesLoaded, setTemplatesLoaded] = useState(false)
  const [templateConfigOpen, setTemplateConfigOpen] = useState(true)
  const [blockPickerOpen, setBlockPickerOpen] = useState(false)
  const [blockConstraintsOpen, setBlockConstraintsOpen] = useState(false)
  const [markdownMode, setMarkdownMode] = useState<'edit' | 'preview'>('edit')
  const [artifactRewriteSelection, setArtifactRewriteSelection] = useState<ArtifactRewriteSelection | null>(null)
  const [artifactRewriteOpen, setArtifactRewriteOpen] = useState(false)
  const [artifactRewriteInstruction, setArtifactRewriteInstruction] = useState('')
  const [artifactRewriteLoading, setArtifactRewriteLoading] = useState(false)
  const [templateId, setTemplateId] = useState('')
  const [title, setTitle] = useState('')
  const [notes, setNotes] = useState('')
  const [draftSchema, setDraftSchema] = useState<DocumentArtifact | null>(null)
  const [editableArtifact, setEditableArtifact] = useState<DocumentArtifact | null>(artifact ? cloneArtifact(artifact) : null)
  const [selectedBlockId, setSelectedBlockId] = useState('')
  const [isGenerating, setIsGenerating] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setEditableArtifact(artifact ? cloneArtifact(artifact) : null)
    if (artifact?.blocks?.length) {
      setSelectedBlockId((current) => current || artifact.blocks[0].block_id)
      setBlockPickerOpen(false)
    }
  }, [artifact])

  useEffect(() => {
    setArtifactRewriteSelection(null)
    setArtifactRewriteOpen(false)
    setArtifactRewriteInstruction('')
  }, [selectedBlockId])

  useEffect(() => {
    if (!open || templates.length > 0 || templatesLoaded) return
    let cancelled = false
    setTemplatesLoading(true)
    setTemplatesLoaded(false)
    docxTemplateApi
      .getOverview()
      .then((overview) => {
        if (cancelled) return
        setTemplates(overview.templates || [])
        const firstTemplateId = overview.templates?.[0]?.template_id || ''
        setTemplateId((current) => current || firstTemplateId)
      })
      .catch((err) => {
        console.error('加载模板失败:', err)
        if (!cancelled) setError('模板列表加载失败')
      })
      .finally(() => {
        if (!cancelled) {
          setTemplatesLoading(false)
          setTemplatesLoaded(true)
        }
      })
    return () => {
      cancelled = true
    }
  }, [open, templates.length, templatesLoaded])

  const selectedTemplate = useMemo(
    () => templates.find((item) => item.template_id === templateId) || null,
    [templateId, templates],
  )

  const selectedBlock = useMemo(() => {
    const source = editableArtifact?.blocks || []
    return source.find((item) => item.block_id === selectedBlockId) || source[0] || null
  }, [editableArtifact?.blocks, selectedBlockId])
  const selectedBlockIdSet = useMemo(() => new Set(selectedBlockIds), [selectedBlockIds])
  const selectedBlockIncluded = selectedBlock ? selectedBlockIdSet.has(selectedBlock.block_id) : false

  const toggleSendBlockSelection = (blockId: string) => {
    onSelectedBlockIdsChange(
      selectedBlockIdSet.has(blockId)
        ? selectedBlockIds.filter((item) => item !== blockId)
        : [...selectedBlockIds, blockId],
    )
  }

  const updateDraft = (patch: Partial<DocumentArtifact>) => {
    setDraftSchema((current) => (current ? { ...current, ...patch } : current))
  }

  const updateDraftBlock = (blockId: string, patch: Partial<DocumentArtifactBlock>) => {
    setDraftSchema((current) => {
      if (!current) return current
      return {
        ...current,
        blocks: current.blocks.map((block) => (block.block_id === blockId ? { ...block, ...patch } : block)),
      }
    })
  }

  const updateEditableBlock = (blockId: string, patch: Partial<DocumentArtifactBlock>) => {
    setEditableArtifact((current) => {
      if (!current) return current
      return {
        ...current,
        blocks: current.blocks.map((block) => (block.block_id === blockId ? { ...block, ...patch } : block)),
      }
    })
  }

  const handleGenerateSchema = async () => {
    if (!templateId || isGenerating || disabled) return
    setError(null)
    setIsGenerating(true)
    try {
      const ensuredConversationId = await onEnsureConversation()
      if (!ensuredConversationId) throw new Error('无法创建对话')
      const schema = await chatApi.generateDocumentArtifactSchema(ensuredConversationId, {
        template_id: templateId,
        title: title || selectedTemplate?.name || '文档草稿',
        user_notes: notes,
      })
      setDraftSchema(schema)
      setTemplateConfigOpen(false)
    } catch (err: any) {
      console.error('生成文档结构失败:', err)
      setError(err?.response?.data?.detail || err?.message || '生成文档结构失败')
    } finally {
      setIsGenerating(false)
    }
  }

  const handleReloadTemplates = async () => {
    if (templatesLoading) return
    setError(null)
    setTemplatesLoading(true)
    setTemplatesLoaded(false)
    try {
      const overview = await docxTemplateApi.getOverview()
      const nextTemplates = overview.templates || []
      setTemplates(nextTemplates)
      setTemplateId((current) => {
        if (nextTemplates.some((item) => item.template_id === current)) return current
        return nextTemplates[0]?.template_id || ''
      })
    } catch (err) {
      console.error('重新加载模板失败:', err)
      setError('模板列表加载失败')
    } finally {
      setTemplatesLoading(false)
      setTemplatesLoaded(true)
    }
  }

  const handleCreateArtifact = async () => {
    if (!draftSchema || !templateId || isSaving || disabled) return
    setError(null)
    setIsSaving(true)
    try {
      const ensuredConversationId = await onEnsureConversation()
      if (!ensuredConversationId) throw new Error('无法创建对话')
      await chatApi.createDocumentArtifact(ensuredConversationId, {
        template_id: templateId,
        schema: draftSchema,
      })
      setDraftSchema(null)
      await onRefresh(ensuredConversationId)
    } catch (err: any) {
      console.error('创建文档 Artifact 失败:', err)
      setError(err?.response?.data?.detail || err?.message || '创建文档结构失败')
    } finally {
      setIsSaving(false)
    }
  }

  const handleSaveSelectedBlock = async () => {
    if (!conversationId || !selectedBlock || isSaving || disabled) return
    setError(null)
    setIsSaving(true)
    try {
      await chatApi.updateDocumentArtifactBlock(conversationId, selectedBlock.block_id, {
        title: selectedBlock.title,
        block_constraints: selectedBlock.block_constraints,
        markdown: selectedBlock.markdown,
        status: selectedBlock.status || 'draft',
      })
      await onRefresh(conversationId)
    } catch (err: any) {
      console.error('保存文档块失败:', err)
      setError(err?.response?.data?.detail || err?.message || '保存文档块失败')
    } finally {
      setIsSaving(false)
    }
  }

  const handleArtifactMarkdownSelection = (event: SyntheticEvent<HTMLTextAreaElement>) => {
    if (!selectedBlock) return
    const textarea = event.currentTarget
    const startOffset = Number(textarea.selectionStart || 0)
    const endOffset = Number(textarea.selectionEnd || startOffset)
    if (endOffset <= startOffset) {
      setArtifactRewriteSelection(null)
      return
    }
    const selectedText = selectedBlock.markdown.slice(startOffset, endOffset)
    if (selectedText.trim().length < 2) {
      setArtifactRewriteSelection(null)
      return
    }
    setArtifactRewriteSelection({
      blockId: selectedBlock.block_id,
      selectedText,
      startOffset,
      endOffset,
    })
  }

  const handleOpenArtifactRewrite = () => {
    if (markdownMode !== 'edit') {
      setError('请切换到编辑模式后，在 Markdown 正文中选中需要改写的文字。')
      return
    }
    if (!selectedBlock || !artifactRewriteSelection || artifactRewriteSelection.blockId !== selectedBlock.block_id) {
      setError('请先在当前 Markdown 正文中选中需要局部改写的文字。')
      return
    }
    setError(null)
    setArtifactRewriteOpen(true)
  }

  const handleCloseArtifactRewrite = () => {
    setArtifactRewriteOpen(false)
    setArtifactRewriteInstruction('')
  }

  const runArtifactRewrite = async (instruction: string) => {
    if (!conversationId || !selectedBlock || !artifactRewriteSelection || artifactRewriteLoading || disabled) return
    const normalizedInstruction = instruction.trim()
    if (!normalizedInstruction) {
      setError('请输入局部改写要求。')
      return
    }
    if (artifactRewriteSelection.blockId !== selectedBlock.block_id) {
      setError('选区已变化，请重新选择需要改写的文字。')
      return
    }

    const currentMarkdown = selectedBlock.markdown || ''
    const selectedText = currentMarkdown.slice(artifactRewriteSelection.startOffset, artifactRewriteSelection.endOffset)
    if (selectedText !== artifactRewriteSelection.selectedText) {
      setError('选区内容已变化，请重新选择需要改写的文字。')
      setArtifactRewriteSelection(null)
      setArtifactRewriteOpen(false)
      return
    }

    setError(null)
    setArtifactRewriteLoading(true)
    try {
      await chatApi.updateDocumentArtifactBlock(conversationId, selectedBlock.block_id, {
        title: selectedBlock.title,
        block_constraints: selectedBlock.block_constraints,
        markdown: currentMarkdown,
        status: selectedBlock.status || 'draft',
      })
      const response = await chatApi.rewriteDocumentArtifactBlockSpan(conversationId, selectedBlock.block_id, {
        instruction: normalizedInstruction,
        selected_text: selectedText,
        before_context: currentMarkdown.slice(Math.max(0, artifactRewriteSelection.startOffset - 1200), artifactRewriteSelection.startOffset),
        after_context: currentMarkdown.slice(artifactRewriteSelection.endOffset, artifactRewriteSelection.endOffset + 1200),
        start_offset: artifactRewriteSelection.startOffset,
        end_offset: artifactRewriteSelection.endOffset,
      })
      setEditableArtifact(cloneArtifact(response.artifact))
      setArtifactRewriteSelection(null)
      setArtifactRewriteOpen(false)
      setArtifactRewriteInstruction('')
      await onRefresh(conversationId)
    } catch (err: any) {
      console.error('局部改写文档块失败:', err)
      setError(getApiErrorMessage(err, '局部改写失败'))
    } finally {
      setArtifactRewriteLoading(false)
    }
  }

  const addDraftBlock = () => {
    setDraftSchema((current) => {
      if (!current) return current
      const nextBlock = emptyBlock(current.blocks.length)
      return { ...current, blocks: [...current.blocks, nextBlock] }
    })
  }

  const removeDraftBlock = (blockId: string) => {
    setDraftSchema((current) => {
      if (!current || current.blocks.length <= 1) return current
      return {
        ...current,
        blocks: current.blocks
          .filter((block) => block.block_id !== blockId)
          .map((block, index) => ({ ...block, index })),
      }
    })
  }

  if (!open) {
    return (
      <div className="document-artifact-panel flex h-full min-h-[56px] flex-row items-center justify-between border-t border-[#1b314d] bg-[#020817] px-3 py-2 lg:min-h-0 lg:flex-col lg:border-l lg:border-t-0 lg:px-2 lg:py-3">
        <button
          type="button"
          className="flex h-10 w-10 items-center justify-center rounded-2xl border border-[#28506c] bg-[#0a1a2e] text-sm font-semibold text-cyan-100 shadow-[0_10px_26px_rgba(0,0,0,0.22)] transition hover:border-[#2dd4bf]/60 hover:bg-[#0d2536] outline-none"
          title="展开文档面板"
          onClick={() => onOpenChange(true)}
        >
          文
        </button>
        <div className="min-w-0 flex-1 px-3 text-left lg:flex-none lg:px-0 lg:py-3 lg:[writing-mode:vertical-rl]">
          <div className="truncate text-xs font-medium tracking-[0.18em] text-[#8ba0b7]">DOCX</div>
          <div className="mt-0.5 truncate text-xs text-[#5f7186] lg:hidden">
            {artifact ? `${artifact.blocks.length} 个块` : '模板与结构'}
          </div>
        </div>
        {artifact ? (
          <div className="rounded-full border border-[#1d3550] bg-[#071426] px-2 py-0.5 text-xs text-[#b8cadb] lg:mt-auto">
            {artifact.blocks.length}
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className="document-artifact-panel flex h-full min-h-0 flex-col border-t border-white/[0.07] bg-[#0a0f1c] lg:border-l lg:border-t-0">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-[radial-gradient(circle_at_top_right,rgba(45,212,191,0.10),transparent_30%),radial-gradient(circle_at_18%_0%,rgba(59,130,246,0.055),transparent_28%),linear-gradient(180deg,#0f1728_0%,#0a0f1c_100%)] lg:border-0">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-white/[0.07] bg-[#0f1728]/90 px-3 py-2.5 backdrop-blur-xl">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.22em] text-[#8ba0b7]">文档控制台</div>
            <div className="mt-1 truncate text-sm font-semibold text-[#f4f8ff]">
              {artifact ? artifact.title : '文档工作台'}
            </div>
            <div className="mt-0.5 truncate text-[11px] text-[#a3b7cd]">
              {artifact ? `${artifact.blocks.length} 个模块 · ${selectedBlockIds.length} 个待发送` : '选择模板，生成可编辑分块'}
            </div>
          </div>
          <div className="flex min-w-0 shrink-0 items-center gap-2">
            {artifact ? (
              <button
                type="button"
                className="document-artifact-switch-button max-w-[172px] truncate rounded-full px-3 py-1.5 text-xs font-medium transition border-none outline-none"
                onClick={() => setBlockPickerOpen((current) => !current)}
                title="切换当前模块"
              >
                {selectedBlock?.title || '选择模块'} {blockPickerOpen ? '↑' : '↓'}
              </button>
            ) : null}
            <button
              type="button"
              className="rounded-full border border-solid border-white/[0.08] bg-white/[0.045] px-3 py-1.5 text-xs font-medium text-[#d7e7f8] transition outline-none hover:border-[#2dd4bf]/45 hover:bg-[#0d2536]"
              onClick={() => onOpenChange(false)}
            >
              收起
            </button>
          </div>
        </div>

        {artifact && blockPickerOpen ? (
          <div className="document-artifact-picker docx-panel-scrollbar shrink-0 max-h-56 space-y-2 overflow-auto border-b border-white/[0.07] p-2.5">
            <div className="flex items-center justify-between gap-2 px-1 pb-1 text-xs text-[#a3b7cd]">
              <span>勾选后，下一次发送会带上模块 ID。</span>
              {selectedBlockIds.length ? (
                <button
                  type="button"
                  className="shrink-0 text-[#9ddbd3] bg-transparent border-none outline-none hover:text-[#e8fffb]"
                  onClick={() => onSelectedBlockIdsChange([])}
                >
                  清空
                </button>
              ) : null}
            </div>
            {(editableArtifact?.blocks || []).map((block) => (
              <div
                key={block.block_id}
                className={`group relative w-full overflow-hidden rounded-xl border py-2 pl-4 pr-3 text-left text-sm transition ${selectedBlock?.block_id === block.block_id
                    ? 'border-[#2dd4bf]/40 bg-[#2dd4bf]/10 text-[#f4f8ff] shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_8px_20px_rgba(45,212,191,0.08)]'
                    : 'border-white/[0.05] bg-white/[0.02] text-[#c9d8e8] hover:border-white/[0.1] hover:bg-white/[0.05]'
                  }`}
              >
                {selectedBlock?.block_id === block.block_id && (
                  <div className="absolute left-0 top-1/2 h-1/2 w-[3px] -translate-y-1/2 rounded-r-full bg-[#2dd4bf] shadow-[0_0_12px_rgba(45,212,191,0.6)]"></div>
                )}
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="document-artifact-block-checkbox mt-1 h-3.5 w-3.5 shrink-0 rounded"
                    checked={selectedBlockIdSet.has(block.block_id)}
                    onChange={() => toggleSendBlockSelection(block.block_id)}
                    title="发送消息时带上这个模块 ID"
                  />
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left bg-transparent border-none outline-none"
                    onClick={() => {
                      setSelectedBlockId(block.block_id)
                      setBlockPickerOpen(false)
                    }}
                  >
                    <div className="truncate font-medium">{block.title}</div>
                    <div className="mt-1 hidden truncate font-mono text-[11px] text-[#8ba0b7] group-hover:block">{block.block_id}</div>
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        <div className={artifact ? 'flex min-h-0 flex-1 flex-col overflow-hidden relative' : 'docx-panel-scrollbar min-h-0 flex-1 overflow-auto p-4'}>
          {error ? (
            <div className="mx-3 mt-3 mb-1 shrink-0 rounded-xl border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-100">
              {error}
            </div>
          ) : null}

          {!artifact ? (
            <div className="space-y-4">
              <div className="rounded-2xl border border-[#18304a] bg-[#071426]">
                <div className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left">
                  <div className="min-w-0">
                    <div className="text-xs font-medium uppercase tracking-[0.16em] text-[#6e8298]">生成配置</div>
                    <div className="mt-0.5 truncate text-sm text-[#e8f2ff]">
                      {selectedTemplate?.name || '选择模板'}
                      {title ? ` · ${title}` : ''}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="shrink-0 rounded-full border border-solid border-[#1d3550] bg-[#0a1a2e] px-2.5 py-1 text-xs text-[#b8cadb] outline-none transition hover:border-[#2f5f73] hover:text-[#e8f2ff]"
                    onClick={() => setTemplateConfigOpen((current) => !current)}
                  >
                    {templateConfigOpen ? '收起' : '展开'}
                  </button>
                </div>

                {templateConfigOpen ? (
                  <div className="space-y-3 border-t border-[#14243a] px-3 py-3">
                    <div className="grid gap-3">
                      <div className="space-y-1 text-sm text-[#b8cadb]">
                        <span className="flex items-center justify-between gap-2">
                          <span>模板</span>
                          <button
                            type="button"
                            className="rounded-full border border-solid border-[#1d3550] bg-transparent px-2 py-0.5 text-[11px] text-[#b8cadb] transition outline-none hover:border-[#2f5f73] hover:text-[#e8f2ff] disabled:cursor-not-allowed disabled:text-[#5f7186]"
                            disabled={templatesLoading}
                            onClick={handleReloadTemplates}
                          >
                            刷新
                          </button>
                        </span>
                        <ConfigProvider
                          theme={{
                            algorithm: theme.darkAlgorithm,
                            token: {
                              colorBgContainer: '#071426',
                              colorBorder: '#18304a',
                              colorPrimary: '#2dd4bf',
                              borderRadius: 12,
                            },
                          }}
                        >
                          <Select
                            className="w-full h-10 custom-premium-select"
                            popupClassName="premium-select-popup"
                            value={templateId}
                            onChange={(val) => setTemplateId(val)}
                            disabled={templatesLoading || disabled}
                            placeholder="选择模板"
                            suffixIcon={<DownOutlined className="text-slate-500 text-[10px]" />}
                            options={
                              templatesLoading
                                ? [{ value: '', label: '模板加载中...', disabled: true }]
                                : templates.length === 0
                                  ? [{ value: '', label: templatesLoaded ? '暂无模板' : '等待加载', disabled: true }]
                                  : templates.map((item) => ({
                                      value: item.template_id,
                                      label: item.name || item.template_id,
                                    }))
                            }
                          />
                        </ConfigProvider>
                        <span className="text-xs text-[#6e8298]">
                          {templatesLoading
                            ? '正在从模板管理读取模板列表'
                            : templates.length > 0
                              ? `已加载 ${templates.length} 个模板`
                              : '没有可用模板时，先到模板管理上传并保存模板。'}
                        </span>
                      </div>
                      <label className="space-y-1 text-sm text-[#b8cadb]">
                        <span>文档标题</span>
                        <input
                          className="w-full rounded-xl border border-[#18304a] bg-[#071426] px-3 py-2 text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                          value={title}
                          onChange={(event) => setTitle(event.target.value)}
                          placeholder={selectedTemplate?.name || '例如：面上项目申请书'}
                        />
                      </label>
                    </div>

                    <label className="space-y-1 text-sm text-[#b8cadb]">
                      <span>本次生成补充说明</span>
                      <textarea
                        className="min-h-[72px] w-full rounded-xl border border-[#18304a] bg-[#071426] px-3 py-2 text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                        value={notes}
                        onChange={(event) => setNotes(event.target.value)}
                        placeholder="例如：按国家自然科学基金面上项目正文结构拆分，先生成可逐块填写的草稿结构。"
                      />
                    </label>

                    <button
                      type="button"
                      className="rounded-full border border-solid border-[#2f5f73] bg-[#0d2536] px-4 py-2 text-sm font-medium text-cyan-50 transition outline-none hover:bg-[#113149] disabled:cursor-not-allowed disabled:border-[#18304a] disabled:bg-[#071426] disabled:text-[#5f7186]"
                      disabled={!templateId || isGenerating || disabled}
                      onClick={handleGenerateSchema}
                    >
                      {isGenerating ? '生成结构中...' : '生成 section/block 草稿'}
                    </button>
                  </div>
                ) : null}
              </div>

              {draftSchema ? (
                <div className="space-y-4 rounded-2xl border border-[#18304a] bg-[#071426] p-4">
                  <label className="space-y-1 text-sm text-[#b8cadb]">
                    <span>整体约束</span>
                    <textarea
                      className="min-h-[120px] w-full rounded-xl border border-[#18304a] bg-[#071426] px-3 py-2 text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                      value={draftSchema.global_constraints}
                      onChange={(event) => updateDraft({ global_constraints: event.target.value })}
                    />
                  </label>

                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm font-medium text-[#e8f2ff]">分块结构</div>
                    <button
                      type="button"
                      className="rounded-full border border-solid border-[#1d3550] bg-transparent px-3 py-1 text-xs text-[#b8cadb] hover:border-[#2f5f73] hover:text-[#e8f2ff] outline-none"
                      onClick={addDraftBlock}
                    >
                      新增块
                    </button>
                  </div>

                  <div className="space-y-3">
                    {draftSchema.blocks.map((block) => (
                      <div key={block.block_id} className="rounded-xl border border-[#18304a] bg-[#081626] p-3">
                        <div className="grid gap-2">
                          <div className="rounded-lg border border-[#14243a] bg-[#050d1a] px-3 py-2">
                            <div className="text-[10px] uppercase tracking-[0.16em] text-[#5f7186]">系统 ID</div>
                            <div className="mt-1 truncate font-mono text-xs text-[#9db1c6]" title={block.block_id}>
                              {block.block_id}
                            </div>
                          </div>
                          <label className="space-y-1">
                            <span className="text-[11px] font-medium text-[#8ba0b7]">中文标题</span>
                            <input
                              className="w-full rounded-lg border border-[#18304a] bg-[#071426] px-3 py-2 text-sm text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                              value={block.title}
                              onChange={(event) => updateDraftBlock(block.block_id, { title: event.target.value })}
                              placeholder="标题"
                            />
                          </label>
                          <label className="space-y-1">
                            <span className="text-[11px] font-medium text-[#8ba0b7]">目标字数</span>
                            <input
                              className="w-full rounded-lg border border-[#18304a] bg-[#071426] px-3 py-2 text-sm text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                              type="number"
                              min={0}
                              value={block.target_words}
                              onChange={(event) => updateDraftBlock(block.block_id, { target_words: Number(event.target.value || 0) })}
                              placeholder="0 表示不限制"
                            />
                          </label>
                        </div>
                        <label className="mt-2 block space-y-1">
                          <span className="text-[11px] font-medium text-[#8ba0b7]">分块约束</span>
                          <textarea
                            className="min-h-[80px] w-full rounded-lg border border-[#18304a] bg-[#071426] px-3 py-2 text-sm text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                            value={block.block_constraints}
                            onChange={(event) => updateDraftBlock(block.block_id, { block_constraints: event.target.value })}
                            placeholder="本块约束"
                          />
                        </label>
                        <label className="mt-2 block space-y-1">
                          <span className="text-[11px] font-medium text-[#8ba0b7]">Markdown 骨架</span>
                          <textarea
                            className="min-h-[80px] w-full rounded-lg border border-[#18304a] bg-[#071426] px-3 py-2 text-sm text-[#e8f2ff] outline-none focus:border-[#2f5f73]"
                            value={block.markdown}
                            onChange={(event) => updateDraftBlock(block.block_id, { markdown: event.target.value })}
                            placeholder="Markdown 骨架"
                          />
                        </label>
                        <div className="mt-2 flex items-center justify-between">
                          <label className="flex items-center gap-2 text-xs text-[#8ba0b7]">
                            <input
                              type="checkbox"
                              checked={block.required}
                              onChange={(event) => updateDraftBlock(block.block_id, { required: event.target.checked })}
                            />
                            必填
                          </label>
                          <button
                            type="button"
                            className="text-xs text-red-200 hover:text-red-100 disabled:text-[#5f7186]"
                            disabled={draftSchema.blocks.length <= 1}
                            onClick={() => removeDraftBlock(block.block_id)}
                          >
                            删除
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>

                  <button
                    type="button"
                    className="rounded-full border-none outline-none bg-[#2dd4bf] px-4 py-2 text-sm font-semibold text-[#021014] transition hover:bg-[#5eead4] disabled:cursor-not-allowed disabled:bg-[#18304a] disabled:text-[#5f7186]"
                    disabled={isSaving || disabled}
                    onClick={handleCreateArtifact}
                  >
                    {isSaving ? '保存中...' : '确认并创建文档结构'}
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden px-3 pb-[72px] pt-3">
              {selectedBlock ? (
                <>
                  <div className="flex shrink-0 items-start justify-between gap-2 px-0.5">
                    <input
                      className="document-artifact-title-input min-w-0 flex-1 border-0 bg-transparent px-0 py-1 text-xl font-semibold leading-tight tracking-[-0.02em] text-[#f4f8ff] outline-none"
                      value={selectedBlock.title}
                      onChange={(event) => updateEditableBlock(selectedBlock.block_id, { title: event.target.value })}
                    />
                    <button
                      type="button"
                      className={`mt-0.5 shrink-0 rounded-full border border-solid px-2.5 py-1 text-[11px] font-medium transition outline-none ${selectedBlockIncluded
                          ? 'border-[#2dd4bf]/55 bg-[#12382f] text-[#d8fff7] hover:bg-[#16483e]'
                          : 'border-white/[0.09] bg-transparent text-[#c9d8e8] hover:border-[#2dd4bf]/45 hover:text-[#e8fffb]'
                        }`}
                      onClick={() => toggleSendBlockSelection(selectedBlock.block_id)}
                      title="控制下一次发送消息是否携带当前模块 ID"
                    >
                      {selectedBlockIncluded ? '已加入发送' : '加入发送'}
                    </button>
                  </div>

                  <div className="shrink-0 overflow-hidden rounded-xl border border-white/[0.07] bg-[#111a2c]/72">
                    <div className="flex items-center justify-between gap-2 px-2.5 py-2">
                      <button
                        type="button"
                        className={`flex items-center rounded-full border border-solid px-2.5 py-1 text-left text-[11px] font-medium transition outline-none ${blockConstraintsOpen
                            ? 'bg-[#18304a] border-transparent text-[#e8f2ff]'
                            : 'bg-transparent border-white/[0.07] text-[#a3b7cd] hover:border-[#2dd4bf]/28 hover:text-[#f4f8ff]'
                          }`}
                        onClick={() => setBlockConstraintsOpen((current) => !current)}
                      >
                        分块约束
                        {!blockConstraintsOpen && selectedBlock.block_constraints ? (
                          <span className="ml-2 font-normal text-[#6e8298]">已收起</span>
                        ) : null}
                        <span className="ml-1 text-[#8ba0b7]">{blockConstraintsOpen ? '↑' : '↓'}</span>
                      </button>
                      <div className="flex shrink-0 items-center gap-1">
                        <button
                          type="button"
                          className={`rounded-full border border-solid px-2.5 py-1 text-[11px] font-medium transition outline-none ${artifactRewriteSelection?.blockId === selectedBlock.block_id
                              ? 'border-[#2dd4bf]/42 bg-[#12382f] text-[#d8fff7] hover:bg-[#16483e]'
                              : 'border-white/[0.07] bg-transparent text-[#a3b7cd] hover:border-[#2dd4bf]/28 hover:text-[#f4f8ff]'
                            }`}
                          disabled={artifactRewriteLoading}
                          title="先在 Markdown 正文中选中一段文字，再点击局部改写"
                          onClick={handleOpenArtifactRewrite}
                        >
                          局部改写
                        </button>
                      </div>
                      <div className="flex shrink-0 rounded-full border border-solid border-white/[0.07] bg-transparent p-0.5 shadow-inner">
                        <button
                          type="button"
                          className={`rounded-full border border-solid px-2.5 py-1 text-[11px] font-medium transition outline-none ${markdownMode === 'edit'
                              ? 'bg-[#2dd4bf] border-transparent text-[#021014]'
                              : 'bg-transparent border-transparent text-[#a3b7cd] hover:text-[#f4f8ff]'
                            }`}
                          onClick={() => setMarkdownMode('edit')}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className={`rounded-full border border-solid px-2.5 py-1 text-[11px] font-medium transition outline-none ${markdownMode === 'preview'
                              ? 'bg-[#2dd4bf] border-transparent text-[#021014]'
                              : 'bg-transparent border-transparent text-[#a3b7cd] hover:text-[#f4f8ff]'
                            }`}
                          onClick={() => setMarkdownMode('preview')}
                        >
                          预览
                        </button>
                      </div>
                    </div>
                    {blockConstraintsOpen ? (
                      <textarea
                        className="document-artifact-inset-input docx-panel-scrollbar min-h-[92px] w-full border-0 border-t border-white/[0.07] px-3.5 py-3 text-sm leading-6 text-[#e8f2ff] shadow-[inset_0_16px_38px_rgba(0,0,0,0.20)] outline-none focus:border-[#2f5f73]"
                        value={selectedBlock.block_constraints}
                        onChange={(event) =>
                          updateEditableBlock(selectedBlock.block_id, { block_constraints: event.target.value })
                        }
                      />
                    ) : null}
                    {artifactRewriteOpen ? (
                      <div className="border-t border-white/[0.07] bg-[#020817] px-3 py-3">
                        <div className="mb-2 max-h-14 overflow-hidden rounded-lg border border-white/[0.07] bg-white/[0.035] px-3 py-2 text-xs leading-5 text-[#c9d8e8]">
                          {artifactRewriteSelection?.selectedText || '未选择文字'}
                        </div>
                        <div className="mb-2 flex flex-wrap gap-2">
                          {ARTIFACT_REWRITE_OPTIONS.map((option) => (
                            <button
                              key={option.label}
                              type="button"
                              className="rounded-full border border-solid border-[#2dd4bf]/18 bg-[#2dd4bf]/10 px-3 py-1.5 text-xs text-[#d8fff7] transition outline-none hover:border-[#2dd4bf]/38 hover:bg-[#2dd4bf]/16 disabled:cursor-not-allowed disabled:opacity-50"
                              disabled={artifactRewriteLoading}
                              onClick={() => void runArtifactRewrite(option.instruction)}
                            >
                              {option.label}
                            </button>
                          ))}
                        </div>
                        <div className="flex gap-2">
                          <input
                            value={artifactRewriteInstruction}
                            onChange={(event) => setArtifactRewriteInstruction(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' && !event.shiftKey) {
                                event.preventDefault()
                                void runArtifactRewrite(artifactRewriteInstruction)
                              }
                            }}
                            placeholder="自定义要求，例如：更像国自然申请书表述"
                            className="min-w-0 flex-1 rounded-xl border border-white/[0.08] bg-[#0a0f1c] px-3 py-2 text-xs text-[#f4f8ff] outline-none placeholder:text-[#6e8298] focus:border-[#2dd4bf]/40"
                          />
                          <button
                            type="button"
                            className="rounded-xl bg-[#2dd4bf] px-3 py-2 text-xs font-semibold text-[#021014] transition hover:bg-[#5eead4] disabled:cursor-not-allowed disabled:opacity-50"
                            disabled={artifactRewriteLoading}
                            onClick={() => void runArtifactRewrite(artifactRewriteInstruction)}
                          >
                            {artifactRewriteLoading ? '改写中' : '应用'}
                          </button>
                          <button
                            type="button"
                            className="rounded-xl border border-white/[0.08] px-3 py-2 text-xs text-[#a3b7cd] transition hover:text-[#f4f8ff] disabled:cursor-not-allowed disabled:opacity-50"
                            disabled={artifactRewriteLoading}
                            onClick={handleCloseArtifactRewrite}
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </div>

                  <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
                    {markdownMode === 'edit' ? (
                      <textarea
                        className="document-artifact-inset-input docx-panel-scrollbar min-h-0 flex-1 resize-none rounded-2xl border border-white/[0.07] px-4 py-3 font-mono text-sm leading-7 text-[#f4f8ff] shadow-[inset_0_1px_0_rgba(255,255,255,0.025),inset_0_18px_42px_rgba(0,0,0,0.24)] outline-none focus:border-[#2f5f73]"
                        value={selectedBlock.markdown}
                        onSelect={handleArtifactMarkdownSelection}
                        onKeyUp={handleArtifactMarkdownSelection}
                        onChange={(event) => {
                          updateEditableBlock(selectedBlock.block_id, { markdown: event.target.value })
                          setArtifactRewriteSelection(null)
                          setArtifactRewriteOpen(false)
                        }}
                        placeholder="该 block 的 Markdown 内容"
                      />
                    ) : (
                      <div className="document-artifact-markdown-preview docx-panel-scrollbar min-h-0 flex-1 overflow-auto rounded-2xl border border-white/[0.07] bg-[#020817] px-4 py-3 text-sm leading-7 text-[#dce9f7] shadow-[inset_0_1px_0_rgba(255,255,255,0.025),inset_0_18px_42px_rgba(0,0,0,0.24)]">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {selectedBlock.markdown || '_暂无 Markdown 内容_'}
                        </ReactMarkdown>
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div className="rounded-2xl border border-white/[0.07] bg-[#071426]/72 p-4 text-sm text-[#a3b7cd]">
                  还没有可编辑模块。
                </div>
              )}

              <div className="absolute bottom-0 left-0 right-0 z-20 border-t border-white/[0.07] bg-[#0a0f1c]/94 px-4 py-3 backdrop-blur-xl">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-xs font-medium text-[#c9d8e8]">
                      {selectedBlockIds.length ? `发送将携带 ${selectedBlockIds.length} 个模块` : '未选择发送模块'}
                    </div>
                    <div className="mt-0.5 text-[11px] text-[#6e8298]">AI 可通过 document_artifact_read/update_block 读写这些块。</div>
                  </div>
                  <button
                    type="button"
                    className="shrink-0 rounded-xl border border-[#2f5f73] bg-[#2dd4bf] px-4 py-2 text-sm font-semibold text-[#021014] shadow-[0_10px_24px_rgba(45,212,191,0.16)] transition hover:bg-[#5eead4] disabled:cursor-not-allowed disabled:border-[#18304a] disabled:bg-[#071426] disabled:text-[#5f7186]"
                    disabled={isSaving || disabled || !selectedBlock}
                    onClick={handleSaveSelectedBlock}
                  >
                    {isSaving ? '保存中...' : '保存'}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default DocumentArtifactPanel
