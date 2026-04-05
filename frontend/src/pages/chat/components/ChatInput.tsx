import { useEffect, useRef, useState } from 'react'
import { Input, Button, Select } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import type {
  AvailableKnowledgeBaseSummary,
  ChatPreferenceCandidate,
  ChatPreferenceKey,
  ChatContextPreviewResponse,
  ChatRagOverrides,
  ChatRagScopeMode,
  ChatUserPreferences,
  ConversationCompactedHistory,
  ConversationContextState,
  Document,
  SharedKnowledgeBaseSummary,
} from '@/services/api'
import { knowledgeApi } from '@/services/api'
import type { SendPhase } from '@/stores/chatStore'

const { TextArea } = Input

interface ChatInputProps {
  inputValue: string
  isSending: boolean
  sendPhase: SendPhase
  sendPhaseLabel?: string | null
  sendPhaseHint?: string | null
  hasConversationHistory?: boolean
  contextPreview?: ChatContextPreviewResponse | null
  isPreviewLoading?: boolean
  previewError?: string | null
  conversationState?: ConversationContextState | null
  compactedHistory?: ConversationCompactedHistory | null
  chatPreferences?: ChatUserPreferences | null
  effectiveChatPreferences?: ChatUserPreferences | null
  chatPreferenceCandidates?: ChatPreferenceCandidate[]
  chatPreferenceOverrides?: Partial<ChatUserPreferences>
  ragOverrides?: ChatRagOverrides | null
  effectiveRagOverrides?: ChatRagOverrides | null
  ragResetToken?: number
  llmProvider?: string
  onInputChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  onRequestPreview?: () => void
  onChatPreferenceOverrideChange?: (
    key: ChatPreferenceKey,
    value: ChatUserPreferences[ChatPreferenceKey],
  ) => void
  onChatPreferenceOverrideClear?: (key: ChatPreferenceKey) => void
  onIgnoreChatPreferenceCandidate?: (candidateId: string) => void
  onRagOverridesChange?: (overrides: ChatRagOverrides | null) => void
}

const normalizePreviewScalar = (value: unknown): string => String(value || '').trim()
type RagFeatureKey =
  | 'use_reranker'
  | 'use_hybrid'
  | 'use_query_rewrite'
  | 'use_contextual_compression'
type RagFeatureMode = 'inherit' | 'on' | 'off'

const RAG_FEATURE_LABELS: Record<RagFeatureKey, string> = {
  use_reranker: 'Reranker',
  use_hybrid: 'Hybrid',
  use_query_rewrite: 'Rewrite',
  use_contextual_compression: 'Compact',
}

const buildFeatureModesFromOverrides = (
  overrides: ChatRagOverrides | null | undefined,
): Record<RagFeatureKey, RagFeatureMode> => ({
  use_reranker:
    overrides?.use_reranker == null ? 'inherit' : overrides.use_reranker ? 'on' : 'off',
  use_hybrid:
    overrides?.use_hybrid == null ? 'inherit' : overrides.use_hybrid ? 'on' : 'off',
  use_query_rewrite:
    overrides?.use_query_rewrite == null ? 'inherit' : overrides.use_query_rewrite ? 'on' : 'off',
  use_contextual_compression:
    overrides?.use_contextual_compression == null
      ? 'inherit'
      : overrides.use_contextual_compression
        ? 'on'
        : 'off',
})

const serializeRagOverrides = (overrides: ChatRagOverrides | null | undefined): string =>
  JSON.stringify(overrides ?? null)

const DOCUMENT_PAGE_SIZE = 30

const mergeDocumentOptions = (base: Document[], incoming: Document[]): Document[] => {
  const merged = new Map<number, Document>()
  base.forEach((item) => {
    merged.set(item.id, item)
  })
  incoming.forEach((item) => {
    merged.set(item.id, item)
  })
  return Array.from(merged.values())
}

type RagKnowledgeBaseOption = AvailableKnowledgeBaseSummary | SharedKnowledgeBaseSummary

const renderPreviewMessagePayload = (message: Record<string, any>) => {
  const content = typeof message.content === 'string'
    ? message.content.trim()
    : message.content != null
      ? JSON.stringify(message.content, null, 2)
      : ''
  const thought = normalizePreviewScalar(message.thought)
  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls : []
  const hasBody = Boolean(content || thought || toolCalls.length)

  if (!hasBody) {
    return <div className="text-slate-500">这条消息没有正文内容。</div>
  }

  return (
    <div className="space-y-2">
      {content ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-slate-500">content</div>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs leading-6 text-slate-300">
            {content}
          </pre>
        </div>
      ) : null}
      {thought ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-cyan-300">thought</div>
          <pre className="max-h-28 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-cyan-400/12 bg-cyan-500/5 px-2 py-1.5 text-xs leading-6 text-cyan-100/90">
            {thought}
          </pre>
        </div>
      ) : null}
      {toolCalls.length ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-emerald-300">tool_calls</div>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-emerald-400/12 bg-emerald-500/5 px-2 py-1.5 text-xs leading-6 text-emerald-100/90">
            {JSON.stringify(toolCalls, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  )
}

/** 聊天输入区域 */
const ChatInput = ({
  inputValue,
  isSending,
  sendPhase,
  sendPhaseLabel = null,
  sendPhaseHint = null,
  hasConversationHistory = false,
  contextPreview = null,
  isPreviewLoading = false,
  previewError = null,
  conversationState = null,
  compactedHistory = null,
  chatPreferences = null,
  effectiveChatPreferences = null,
  chatPreferenceCandidates = [],
  chatPreferenceOverrides = {},
  ragOverrides = null,
  effectiveRagOverrides = null,
  ragResetToken = 0,
  llmProvider,
  onInputChange,
  onSend,
  onStop,
  onRequestPreview,
  onChatPreferenceOverrideChange,
  onChatPreferenceOverrideClear,
  onIgnoreChatPreferenceCandidate,
  onRagOverridesChange,
}: ChatInputProps) => {
  const [isPreviewCollapsed, setIsPreviewCollapsed] = useState(false)
  const ragOverridesRef = useRef(ragOverrides)
  const ragDocumentIdsRef = useRef<number[]>(ragOverrides?.document_ids || [])
  const [isRagPanelOpen, setIsRagPanelOpen] = useState(Boolean(ragOverrides?.enabled))
  const [ragEnabled, setRagEnabled] = useState(Boolean(ragOverrides?.enabled))
  const [ragScopeMode, setRagScopeMode] = useState<ChatRagScopeMode>(ragOverrides?.scope_mode || 'all')
  const [ragKnowledgeBaseIds, setRagKnowledgeBaseIds] = useState<number[]>(ragOverrides?.knowledge_base_ids || [])
  const [ragDocumentKnowledgeBaseId, setRagDocumentKnowledgeBaseId] = useState<number | null>(
    ragOverrides?.knowledge_base_ids?.[0] || null,
  )
  const [ragDocumentIds, setRagDocumentIds] = useState<number[]>(ragOverrides?.document_ids || [])
  const [ragFeatureModes, setRagFeatureModes] = useState<Record<RagFeatureKey, RagFeatureMode>>(
    buildFeatureModesFromOverrides(ragOverrides),
  )
  const [knowledgeBaseOptions, setKnowledgeBaseOptions] = useState<RagKnowledgeBaseOption[]>([])
  const [documentOptions, setDocumentOptions] = useState<Document[]>([])
  const [documentSearchInput, setDocumentSearchInput] = useState('')
  const [debouncedDocumentSearch, setDebouncedDocumentSearch] = useState('')
  const [documentPage, setDocumentPage] = useState(0)
  const [documentTotal, setDocumentTotal] = useState(0)
  const [documentLoadedCount, setDocumentLoadedCount] = useState(0)
  const [isKnowledgeBaseLoading, setIsKnowledgeBaseLoading] = useState(false)
  const [isDocumentLoading, setIsDocumentLoading] = useState(false)
  const [ragLoadError, setRagLoadError] = useState<string | null>(null)

  useEffect(() => {
    ragOverridesRef.current = ragOverrides
  }, [ragOverrides])

  useEffect(() => {
    ragDocumentIdsRef.current = ragDocumentIds
  }, [ragDocumentIds])

  useEffect(() => {
    const nextOverrides = ragOverridesRef.current || null
    setRagEnabled(Boolean(nextOverrides?.enabled))
    setRagScopeMode(nextOverrides?.scope_mode || 'all')
    setRagKnowledgeBaseIds(nextOverrides?.knowledge_base_ids || [])
    setRagDocumentKnowledgeBaseId(nextOverrides?.knowledge_base_ids?.[0] || null)
    setRagDocumentIds(nextOverrides?.document_ids || [])
    setRagFeatureModes(buildFeatureModesFromOverrides(nextOverrides))
    setIsRagPanelOpen(Boolean(nextOverrides?.enabled))
    setDocumentOptions([])
    setDocumentSearchInput('')
    setDebouncedDocumentSearch('')
    setDocumentPage(0)
    setDocumentTotal(0)
    setDocumentLoadedCount(0)
    setRagLoadError(null)
  }, [ragResetToken])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const nextSearch = documentSearchInput.trim()
      setDocumentPage(0)
      setDebouncedDocumentSearch(nextSearch)
    }, 250)
    return () => {
      window.clearTimeout(timer)
    }
  }, [documentSearchInput])

  useEffect(() => {
    setDocumentOptions([])
    setDocumentSearchInput('')
    setDebouncedDocumentSearch('')
    setDocumentPage(0)
    setDocumentTotal(0)
    setDocumentLoadedCount(0)
  }, [ragDocumentKnowledgeBaseId, ragScopeMode])

  useEffect(() => {
    let cancelled = false
    if (!isRagPanelOpen) {
      return () => {
        cancelled = true
      }
    }

    setIsKnowledgeBaseLoading(true)
    setRagLoadError(null)
    knowledgeApi
      .getAvailableKnowledgeBases()
      .then((payload) => {
        if (cancelled) return
        const merged = [...(payload.own || []), ...(payload.shared || [])]
        setKnowledgeBaseOptions(merged)
      })
      .catch((error) => {
        console.error('加载知识库列表失败:', error)
        if (!cancelled) {
          setRagLoadError('暂时无法读取知识库列表')
          setKnowledgeBaseOptions([])
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsKnowledgeBaseLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [isRagPanelOpen])

  useEffect(() => {
    let cancelled = false
    if (!isRagPanelOpen || ragScopeMode !== 'document' || !ragDocumentKnowledgeBaseId) {
      setDocumentOptions([])
      setDocumentTotal(0)
      setDocumentLoadedCount(0)
      return () => {
        cancelled = true
      }
    }

    const skip = documentPage * DOCUMENT_PAGE_SIZE
    setIsDocumentLoading(true)
    setRagLoadError(null)
    knowledgeApi
      .getDocuments(
        ragDocumentKnowledgeBaseId,
        skip,
        DOCUMENT_PAGE_SIZE,
        debouncedDocumentSearch,
      )
      .then((payload) => {
        if (cancelled) return
        const items = payload.items || []
        const total = Number(payload.total || 0)
        setDocumentTotal(total)
        setDocumentLoadedCount((current) => (documentPage === 0 ? items.length : current + items.length))
        setDocumentOptions((current) => {
          if (documentPage === 0) {
            const preserved = current.filter((item) => ragDocumentIdsRef.current.includes(item.id))
            return mergeDocumentOptions(preserved, items)
          }
          return mergeDocumentOptions(current, items)
        })
      })
      .catch((error) => {
        console.error('加载文档列表失败:', error)
        if (!cancelled) {
          setRagLoadError('暂时无法读取文档列表')
          setDocumentOptions([])
          setDocumentTotal(0)
          setDocumentLoadedCount(0)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsDocumentLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [debouncedDocumentSearch, documentPage, isRagPanelOpen, ragDocumentKnowledgeBaseId, ragScopeMode])

  useEffect(() => {
    if (!onRagOverridesChange) return
    if (!ragEnabled) {
      if (serializeRagOverrides(ragOverridesRef.current) !== serializeRagOverrides(null)) {
        onRagOverridesChange(null)
      }
      return
    }

    const next: ChatRagOverrides = {
      enabled: true,
      scope_mode: ragScopeMode,
    }
    if (ragScopeMode === 'knowledge_base' && ragKnowledgeBaseIds.length) {
      next.knowledge_base_ids = ragKnowledgeBaseIds
    } else if (ragScopeMode === 'document') {
      if (ragDocumentKnowledgeBaseId) {
        next.knowledge_base_ids = [ragDocumentKnowledgeBaseId]
      }
      if (ragDocumentIds.length) {
        next.document_ids = ragDocumentIds
      }
    }
    (Object.keys(ragFeatureModes) as RagFeatureKey[]).forEach((key) => {
      const mode = ragFeatureModes[key]
      if (mode === 'inherit') return
      next[key] = mode === 'on'
    })
    if (serializeRagOverrides(ragOverridesRef.current) !== serializeRagOverrides(next)) {
      onRagOverridesChange(next)
    }
  }, [
    onRagOverridesChange,
    ragDocumentIds,
    ragDocumentKnowledgeBaseId,
    ragEnabled,
    ragFeatureModes,
    ragKnowledgeBaseIds,
    ragScopeMode,
  ])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  const canLoadMoreDocuments =
    Boolean(ragDocumentKnowledgeBaseId) && documentLoadedCount < documentTotal && !isDocumentLoading

  const previewDebug = contextPreview?.context_debug
  const previewState = contextPreview?.context_state || conversationState
  const previewAnchor =
    contextPreview?.compacted_history?.history_anchors ||
    compactedHistory?.history_anchors ||
    ''
  const previewHistorySummary =
    contextPreview?.compacted_history?.history_summary ||
    compactedHistory?.history_summary ||
    ''
  const previewReplacementHistory =
    contextPreview?.compacted_history?.replacement_history ||
    compactedHistory?.replacement_history ||
    []
  const selectedTools = previewDebug?.selected_tools || []
  const exactModelMessages = previewDebug?.model_messages_raw || []
  const assembledModelMessages = previewDebug?.model_messages_assembled_raw || []
  const exactSystemPrompt = String(previewDebug?.model_system_prompt || '').trim()
  const exactToolSchemas = previewDebug?.model_tool_schemas_raw || []
  const recentlySlid = previewDebug?.recently_slid_messages || []
  const inheritedRecentMessages = (() => {
    const raw = [...(previewDebug?.recent_messages || [])]
    if (!raw.length) return []
    const draft = inputValue.trim()
    if (!draft) return raw.slice(0, 3)
    let lastIndex = -1
    for (let index = raw.length - 1; index >= 0; index -= 1) {
      const item = raw[index]
      if (item.role === 'user' && String(item.content || '').trim() === draft) {
        lastIndex = index
        break
      }
    }
    if (lastIndex >= 0) {
      raw.splice(lastIndex, 1)
    }
    return raw.slice(-3)
  })()
  const hasInheritedHistoryDetails = Boolean(
    previewAnchor ||
      previewHistorySummary ||
      previewReplacementHistory.length ||
      recentlySlid.length ||
      inheritedRecentMessages.length,
  )
  const hasLightPreview = Boolean(previewState || previewAnchor || previewHistorySummary || previewReplacementHistory.length)
  const hasInput = Boolean(inputValue.trim())
  const shouldRenderPreview = hasInput && !isSending
  const isFirstTurn = !hasConversationHistory
  const sendPhaseCopy = (() => {
    if (sendPhaseLabel && sendPhaseHint) {
      return { label: sendPhaseLabel, hint: sendPhaseHint }
    }
    const phase = sendPhase === 'idle' ? 'submitting' : sendPhase
    if (phase === 'submitting') {
      return isFirstTurn
        ? { label: '正在发送首条消息…', hint: '消息已发出，正在建立连接' }
        : { label: '正在提交这轮消息…', hint: '消息已发出，正在更新当前会话' }
    }
    if (phase === 'planning') {
      return isFirstTurn
        ? { label: '正在准备本轮请求…', hint: '首轮不会整理很多历史，正在判断回答路径并组装请求' }
        : { label: '正在整理本轮上下文…', hint: '正在读取会话状态、替代历史和偏好，准备这轮请求' }
    }
    if (phase === 'loading_context') {
      return isFirstTurn
        ? { label: '正在准备本轮请求…', hint: '首轮不会整理很多历史，正在建立这次请求' }
        : { label: '正在读取会话状态…', hint: '正在读取会话状态、替代历史和用户偏好' }
    }
    if (phase === 'routing') {
      return { label: '正在判断回答路径…', hint: '正在判断这轮是直接回答，还是需要进一步行动' }
    }
    if (phase === 'waiting_model') {
      return { label: '回答路径已确定…', hint: '请求已经发给主模型，通常很快会开始返回内容' }
    }
    if (phase === 'thinking') {
      return isFirstTurn
        ? { label: '正在分析你的问题…', hint: '已经进入模型处理阶段，正在组织首轮回答' }
        : { label: '正在结合历史分析问题…', hint: '已经进入模型处理阶段，正在组织回答或规划下一步' }
    }
    if (phase === 'tool') {
      return { label: '正在调用工具…', hint: '需要外部信息或执行步骤，结果返回后会继续回答' }
    }
    return { label: '正在输出回答…', hint: '已经开始返回结果，可以随时停止' }
  })()
  const previewBadgeText = contextPreview
    ? contextPreview.preview_mode === 'direct'
      ? '完整预演 · 直答流式'
      : '完整预演 · Agent 路径'
    : hasLightPreview
      ? '本地轻预览'
      : '轻预览'
  const fallbackPreviewText = isFirstTurn
    ? '首条消息不会继承历史上下文，将主要基于你当前输入直接生成。'
    : '当前会话暂未形成可复用的持久上下文，这轮将主要使用最近原始消息和当前输入。'
  const preferenceKeyLabels: Record<ChatPreferenceKey, string> = {
    response_language: '语言',
    response_verbosity: '详细度',
    web_search: '联网策略',
  }
  const preferenceValueLabels: Record<ChatPreferenceKey, Record<string, string>> = {
    response_language: {
      auto: '自动',
      'zh-CN': '中文',
      'en-US': '英文',
    },
    response_verbosity: {
      concise: '简洁',
      balanced: '平衡',
      detailed: '详细',
    },
    web_search: {
      ask: '先询问',
      avoid: '避免联网',
      allow_when_needed: '需要时联网',
    },
  }
  const chatPreferenceOptions: Record<ChatPreferenceKey, Array<{ value: string; label: string }>> = {
    response_language: [
      { value: 'zh-CN', label: '中文' },
      { value: 'en-US', label: '英文' },
      { value: 'auto', label: '自动' },
    ],
    response_verbosity: [
      { value: 'concise', label: '简洁' },
      { value: 'balanced', label: '平衡' },
      { value: 'detailed', label: '详细' },
    ],
    web_search: [
      { value: 'ask', label: '先询问' },
      { value: 'avoid', label: '避免联网' },
      { value: 'allow_when_needed', label: '需要时联网' },
    ],
  }
  const preferenceBadges = [
    chatPreferences?.response_language === 'zh-CN'
      ? '默认中文'
      : chatPreferences?.response_language === 'en-US'
        ? '默认英文'
        : '',
    chatPreferences?.response_verbosity === 'concise'
      ? '默认简洁'
      : chatPreferences?.response_verbosity === 'detailed'
        ? '默认详细'
        : '',
    chatPreferences?.web_search === 'avoid'
      ? '默认避免联网'
      : chatPreferences?.web_search === 'allow_when_needed'
        ? '需要时可联网'
      : '',
  ].filter(Boolean)
  const effectivePreferenceBadges = effectiveChatPreferences
    ? [
        effectiveChatPreferences.response_language !== chatPreferences?.response_language
          ? `本轮语言: ${preferenceValueLabels.response_language[effectiveChatPreferences.response_language]}`
          : '',
        effectiveChatPreferences.response_verbosity !== chatPreferences?.response_verbosity
          ? `本轮详细度: ${preferenceValueLabels.response_verbosity[effectiveChatPreferences.response_verbosity]}`
          : '',
        effectiveChatPreferences.web_search !== chatPreferences?.web_search
          ? `本轮联网: ${preferenceValueLabels.web_search[effectiveChatPreferences.web_search]}`
          : '',
      ].filter(Boolean)
    : []
  const effectiveRagBadges = effectiveRagOverrides?.enabled
    ? [
        effectiveRagOverrides.scope_mode === 'knowledge_base' && effectiveRagOverrides.knowledge_base_ids?.length
          ? `本轮 RAG: 知识库 ${effectiveRagOverrides.knowledge_base_ids.join(', ')}`
          : effectiveRagOverrides.scope_mode === 'document' && effectiveRagOverrides.document_ids?.length
            ? `本轮 RAG: 文档 ${effectiveRagOverrides.document_ids.join(', ')}`
            : '本轮 RAG: 全部知识库',
        effectiveRagOverrides.use_reranker != null
          ? `Reranker ${effectiveRagOverrides.use_reranker ? '开启' : '关闭'}`
          : '',
        effectiveRagOverrides.use_hybrid != null
          ? `Hybrid ${effectiveRagOverrides.use_hybrid ? '开启' : '关闭'}`
          : '',
        effectiveRagOverrides.use_query_rewrite != null
          ? `Rewrite ${effectiveRagOverrides.use_query_rewrite ? '开启' : '关闭'}`
          : '',
        effectiveRagOverrides.use_contextual_compression != null
          ? `Compact ${effectiveRagOverrides.use_contextual_compression ? '开启' : '关闭'}`
          : '',
      ].filter(Boolean)
    : []
  const toolSchemaLabels = exactToolSchemas
    .map((schema, index) => {
      const name =
        (typeof schema?.name === 'string' && schema.name) ||
        (typeof (schema as Record<string, unknown>)?.function === 'object' &&
        typeof ((schema as Record<string, unknown>).function as Record<string, unknown>)?.name === 'string'
          ? (((schema as Record<string, unknown>).function as Record<string, unknown>).name as string)
          : '') ||
        (typeof schema?.title === 'string' && schema.title) ||
        `tool_${index + 1}`
      return String(name).trim()
    })
    .filter(Boolean)

  return (
    <div className="border-t border-white/[0.06] bg-slate-950/88 backdrop-blur-2xl">
      <div className="mx-auto max-w-[1040px] px-4 py-4 sm:px-6 lg:px-8">
        <div className="relative flex items-end gap-3">
          <div className="relative flex-1 rounded-[24px] border border-slate-700/60 bg-slate-800/78 p-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_14px_28px_rgba(2,6,23,0.2)] transition-all duration-200 focus-within:border-emerald-400/30 focus-within:shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_0_0_1px_rgba(16,185,129,0.06)]">
            <TextArea
              value={inputValue}
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入问题，按 Enter 发送..."
              autoSize={{ minRows: 1, maxRows: 6 }}
              className="!m-0 !rounded-[18px] !border-0 !bg-transparent !px-4 !py-3 !text-base !leading-7 !text-slate-100 !shadow-none resize-none placeholder:!text-slate-500 focus:!shadow-none"
              disabled={isSending}
            />
          </div>
          {isSending ? (
            <Button
              type="primary"
              size="large"
              danger
              icon={<StopOutlined />}
              onClick={onStop}
              className="bg-red-500 hover:bg-red-600 border-none rounded-2xl h-[52px] px-5
                shadow-lg shadow-red-500/20"
            >
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              size="large"
              icon={<SendOutlined />}
              onClick={onSend}
              disabled={!inputValue.trim()}
              className="bg-emerald-500 hover:bg-emerald-600 border-none rounded-2xl h-[52px] px-5
                shadow-lg shadow-emerald-500/20 disabled:opacity-50"
            >
              发送
            </Button>
          )}
        </div>

        <div className="mt-3 overflow-hidden rounded-[22px] border border-emerald-400/12 bg-[linear-gradient(135deg,rgba(6,78,59,0.18),rgba(15,23,42,0.72))] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-[0.16em] text-emerald-200/90">
                本轮 RAG 注入
              </span>
              {ragEnabled ? (
                <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-[11px] text-emerald-100">
                  已启用
                </span>
              ) : (
                <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-300">
                  未启用
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="small"
                onClick={() => setIsRagPanelOpen((value) => !value)}
                className="rounded-xl border-white/10 bg-white/[0.04] text-slate-200 hover:border-emerald-400/30 hover:text-emerald-100"
              >
                {isRagPanelOpen ? '收起设置' : '展开设置'}
              </Button>
              <Button
                size="small"
                type={ragEnabled ? 'primary' : 'default'}
                onClick={() => setRagEnabled((value) => !value)}
                className={
                  ragEnabled
                    ? 'rounded-xl border-none bg-emerald-500 text-slate-950 shadow-none hover:!bg-emerald-400'
                    : 'rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-emerald-400/30 hover:text-emerald-100'
                }
              >
                {ragEnabled ? '关闭本轮 RAG' : '开启本轮 RAG'}
              </Button>
            </div>
          </div>

          <AnimatePresence initial={false}>
            {isRagPanelOpen ? (
              <motion.div
                key="rag-panel"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2, ease: 'easeOut' }}
                className="overflow-hidden"
              >
                <div className="space-y-3 px-4 py-3.5">
                  <div className="text-xs leading-6 text-slate-400">
                    这是一次性注入，只影响当前这条消息。发送完成后会自动清空，不会写入长期偏好。
                  </div>

                  {ragLoadError ? (
                    <div className="text-sm text-amber-200/90">{ragLoadError}</div>
                  ) : null}

                  <div className="space-y-2">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">检索范围</div>
                    <div className="flex flex-wrap gap-2">
                      {[
                        { value: 'all' as const, label: '全部知识库' },
                        { value: 'knowledge_base' as const, label: '指定知识库' },
                        { value: 'document' as const, label: '直达文档' },
                      ].map((option) => (
                        <Button
                          key={option.value}
                          size="small"
                          type={ragScopeMode === option.value ? 'primary' : 'default'}
                          onClick={() => setRagScopeMode(option.value)}
                          className={
                            ragScopeMode === option.value
                              ? 'rounded-xl border-none bg-emerald-500 text-slate-950 shadow-none hover:!bg-emerald-400'
                              : 'rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-emerald-400/30 hover:text-emerald-100'
                          }
                        >
                          {option.label}
                        </Button>
                      ))}
                    </div>
                  </div>

                  {ragScopeMode === 'knowledge_base' ? (
                    <div className="space-y-2">
                      <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">指定知识库</div>
                      <Select
                        mode="multiple"
                        allowClear
                        value={ragKnowledgeBaseIds}
                        loading={isKnowledgeBaseLoading}
                        placeholder="选择这轮可检索的知识库"
                        onChange={(value) => setRagKnowledgeBaseIds((value as number[]) || [])}
                        options={knowledgeBaseOptions.map((item) => ({
                          value: item.id,
                          label:
                            'owner_name' in item && item.owner_name
                              ? `${item.name} · ${item.owner_name}`
                              : item.name,
                        }))}
                        className="w-full"
                      />
                    </div>
                  ) : null}

                  {ragScopeMode === 'document' ? (
                    <div className="grid gap-3 md:grid-cols-2">
                      <div className="space-y-2">
                        <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">文档所属知识库</div>
                        <Select
                          allowClear
                          value={ragDocumentKnowledgeBaseId ?? undefined}
                          loading={isKnowledgeBaseLoading}
                          placeholder="先选一个知识库"
                          onChange={(value) => {
                            setRagDocumentKnowledgeBaseId(typeof value === 'number' ? value : null)
                            setRagDocumentIds([])
                            setDocumentSearchInput('')
                            setDebouncedDocumentSearch('')
                            setDocumentPage(0)
                          }}
                          options={knowledgeBaseOptions.map((item) => ({
                            value: item.id,
                            label:
                              'owner_name' in item && item.owner_name
                                ? `${item.name} · ${item.owner_name}`
                                : item.name,
                          }))}
                          className="w-full"
                        />
                      </div>
                      <div className="space-y-2">
                        <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">直达文档</div>
                        <Select
                          mode="multiple"
                          allowClear
                          showSearch
                          filterOption={false}
                          searchValue={documentSearchInput}
                          value={ragDocumentIds}
                          loading={isDocumentLoading}
                          disabled={!ragDocumentKnowledgeBaseId}
                          placeholder="搜索并选择这轮直达的文档"
                          onChange={(value) => setRagDocumentIds((value as number[]) || [])}
                          onSearch={(value) => setDocumentSearchInput(value)}
                          options={documentOptions.map((item) => ({
                            value: item.id,
                            label: item.original_filename || item.filename,
                          }))}
                          dropdownRender={(menu) => (
                            <>
                              {menu}
                              {ragDocumentKnowledgeBaseId ? (
                                <div className="border-t border-white/[0.06] px-3 py-2 text-xs text-slate-400">
                                  <div className="flex items-center justify-between gap-3">
                                    <span>
                                      已加载 {Math.min(documentLoadedCount, documentTotal)} / {documentTotal || 0}
                                    </span>
                                    {canLoadMoreDocuments ? (
                                      <Button
                                        size="small"
                                        onMouseDown={(event) => event.preventDefault()}
                                        onClick={() => setDocumentPage((current) => current + 1)}
                                        className="rounded-lg border-white/10 bg-white/[0.04] text-slate-200 hover:border-emerald-400/30 hover:text-emerald-100"
                                      >
                                        加载更多
                                      </Button>
                                    ) : documentTotal > 0 ? (
                                      <span>已加载全部</span>
                                    ) : null}
                                  </div>
                                </div>
                              ) : null}
                            </>
                          )}
                          className="w-full"
                        />
                        {!isDocumentLoading && ragDocumentKnowledgeBaseId && !documentOptions.length ? (
                          <div className="text-xs leading-5 text-slate-500">
                            {debouncedDocumentSearch
                              ? '没有匹配的文档，试试更短的关键词。'
                              : '这个知识库当前没有可选文档，或你暂时没有可读文档权限。'}
                          </div>
                        ) : null}
                        {!isDocumentLoading && ragDocumentKnowledgeBaseId && documentTotal > 0 ? (
                          <div className="text-xs leading-5 text-slate-500">
                            {canLoadMoreDocuments
                              ? `共 ${documentTotal} 篇文档，可搜索文件名并分批加载。`
                              : `共 ${documentTotal} 篇文档，可直接搜索文件名。`}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  ) : null}

                  <div className="space-y-2">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">检索策略</div>
                    <div className="space-y-2">
                      {(Object.keys(RAG_FEATURE_LABELS) as RagFeatureKey[]).map((key) => (
                        <div
                          key={key}
                          className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-3"
                        >
                          <div className="mb-2 text-sm font-medium text-slate-100">{RAG_FEATURE_LABELS[key]}</div>
                          <div className="flex flex-wrap gap-2">
                            {[
                              { value: 'inherit' as const, label: '继承默认' },
                              { value: 'on' as const, label: '强制开启' },
                              { value: 'off' as const, label: '强制关闭' },
                            ].map((option) => (
                              <Button
                                key={option.value}
                                size="small"
                                type={ragFeatureModes[key] === option.value ? 'primary' : 'default'}
                                onClick={() =>
                                  setRagFeatureModes((current) => ({
                                    ...current,
                                    [key]: option.value,
                                  }))
                                }
                                className={
                                  ragFeatureModes[key] === option.value
                                    ? 'rounded-xl border-none bg-emerald-500 text-slate-950 shadow-none hover:!bg-emerald-400'
                                    : 'rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-emerald-400/30 hover:text-emerald-100'
                                }
                              >
                                {option.label}
                              </Button>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {effectiveRagBadges.length ? (
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-emerald-200/90">
                      {effectiveRagBadges.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              </motion.div>
            ) : null}
          </AnimatePresence>
        </div>

        {shouldRenderPreview ? (
          <div className="mt-3 overflow-hidden rounded-[22px] border border-cyan-400/12 bg-[linear-gradient(135deg,rgba(8,47,73,0.22),rgba(15,23,42,0.72))] shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
            <div className="border-b border-white/[0.06] px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold uppercase tracking-[0.16em] text-cyan-200/90">
                    发送前上下文预览
                  </span>
                  <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-300">
                    {previewBadgeText}
                  </span>
                  {contextPreview && previewDebug?.carry_over_previous_goal ? (
                    <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-[11px] text-cyan-100">
                      延续上一轮主题
                    </span>
                  ) : null}
                  {contextPreview?.send_plan?.reusable ? (
                    <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-[11px] text-emerald-100">
                      已生成可复用发送草案
                    </span>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    size="small"
                    onClick={() => setIsPreviewCollapsed((value) => !value)}
                    className="rounded-xl border-white/10 bg-white/[0.04] text-slate-200 hover:border-cyan-400/30 hover:text-cyan-100"
                  >
                    {isPreviewCollapsed ? '展开预览' : '收起预览'}
                  </Button>
                  {onRequestPreview ? (
                    <Button
                      size="small"
                      onClick={onRequestPreview}
                      loading={isPreviewLoading}
                      disabled={!inputValue.trim()}
                      className="rounded-xl border-white/10 bg-white/[0.04] text-slate-200 hover:border-cyan-400/30 hover:text-cyan-100"
                    >
                      {contextPreview ? '刷新完整预演' : '查看完整预演'}
                    </Button>
                  ) : null}
                </div>
              </div>
            </div>

            <AnimatePresence initial={false} mode="wait">
              {isPreviewCollapsed ? (
                <motion.div
                  key="preview-collapsed"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2, ease: 'easeOut' }}
                  className="overflow-hidden"
                >
                  <div className="flex flex-wrap items-center gap-2 px-4 py-3 text-[11px] text-slate-400">
                    <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                      {contextPreview ? '已生成完整预演' : '当前显示轻预览'}
                    </span>
                    {contextPreview ? (
                      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                        Provider Messages {exactModelMessages.length} 条
                      </span>
                    ) : null}
                    {contextPreview && exactToolSchemas.length ? (
                      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                        Tools {exactToolSchemas.length} 个
                      </span>
                    ) : null}
                    <span className="text-slate-500">
                      预览已收起，不影响发送。点“展开预览”可继续查看完整内容。
                    </span>
                  </div>
                </motion.div>
              ) : (
                <motion.div
                  key="preview-expanded"
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.2, ease: 'easeOut' }}
                  className="overflow-hidden"
                >
            <div className="space-y-3 px-4 py-3.5">
              {previewError ? (
                <div className="text-sm text-amber-200/90">{previewError}</div>
              ) : isPreviewLoading && !contextPreview ? (
                <div className="space-y-2">
                  <div className="h-3 w-40 rounded-full bg-white/[0.06]" />
                  <div className="h-3 w-3/4 rounded-full bg-white/[0.05]" />
                  <div className="h-3 w-2/3 rounded-full bg-white/[0.04]" />
                </div>
              ) : contextPreview ? (
                <>
                  <div className="grid gap-3 md:grid-cols-2">
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5">
                      <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">当前主题</div>
                      <div className="text-sm leading-6 text-slate-100">
                        {previewState?.active_topic || '当前未形成稳定主题'}
                      </div>
                    </div>
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5">
                      <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">本轮调度</div>
                      <div className="text-sm leading-6 text-slate-100">
                        {previewDebug?.intent || '基于当前会话状态'}
                        {selectedTools.length ? ` · ${selectedTools.slice(0, 3).join(', ')}` : ''}
                      </div>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">这次会输入给模型的内容</div>
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5 text-sm leading-6 text-slate-200">
                      <div className="space-y-3">
                        <div>
                          <div className="mb-1 text-slate-400">System Prompt</div>
                          {exactSystemPrompt ? (
                            <pre className="max-h-44 overflow-auto rounded-xl border border-white/[0.05] bg-black/10 px-2.5 py-2 text-xs leading-6 whitespace-pre-wrap break-words text-slate-300">
                              {exactSystemPrompt}
                            </pre>
                          ) : (
                            <div className="text-slate-500">当前完整预演没有返回 system prompt。</div>
                          )}
                        </div>

                        <div>
                          <div className="mb-1 text-slate-400">Provider Messages（{exactModelMessages.length} 条）</div>
                          {exactModelMessages.length ? (
                            <div className="space-y-1">
                              {exactModelMessages.map((item, index) => (
                                <div
                                  key={`${String(item.role || 'unknown')}-${index}`}
                                  className="rounded-xl border border-white/[0.05] bg-black/10 px-2.5 py-2 text-slate-300"
                                >
                                  <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-slate-500">
                                    {String(item.role || 'unknown')}
                                  </div>
                                  {renderPreviewMessagePayload(item)}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="text-slate-500">当前完整预演没有返回实际发送给 provider 的消息清单。</div>
                          )}
                        </div>

                        <div>
                          <div className="mb-1 text-slate-400">Tools（{exactToolSchemas.length} 个）</div>
                          {toolSchemaLabels.length ? (
                            <div className="flex flex-wrap gap-2">
                              {toolSchemaLabels.map((label) => (
                                <span
                                  key={label}
                                  className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-300"
                                >
                                  {label}
                                </span>
                              ))}
                            </div>
                          ) : (
                            <div className="text-slate-500">这次不会向模型暴露工具 schema。</div>
                          )}
                        </div>

                        <div className="rounded-xl border border-cyan-400/12 bg-cyan-500/5 px-2.5 py-2 text-xs leading-6 text-cyan-100/90">
                          上面三块是这次真正会发给 provider 的内容。下面这些用于解释这些输入从哪里来，以及内部上下文是怎么组出来的。
                        </div>

                        <div>
                          <div className="mb-1 text-slate-400">内部组装上下文（{assembledModelMessages.length} 条）</div>
                          {assembledModelMessages.length ? (
                            <div className="space-y-1">
                              {assembledModelMessages.map((item, index) => (
                                <div
                                  key={`assembled-${String(item.role || 'unknown')}-${index}`}
                                  className="rounded-xl border border-cyan-400/12 bg-cyan-500/5 px-2.5 py-2 text-slate-200"
                                >
                                  <div className="mb-1 text-[11px] uppercase tracking-[0.12em] text-cyan-300">
                                    {String(item.role || 'unknown')}
                                  </div>
                                  {renderPreviewMessagePayload(item)}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="text-slate-500">当前完整预演没有返回内部组装上下文。</div>
                          )}
                        </div>

                        {previewAnchor || previewHistorySummary || previewReplacementHistory.length ? (
                          <div className="space-y-2 border-t border-white/[0.05] pt-3">
                            <div className="text-slate-400">压缩 / 替代历史说明</div>
                            <div className="text-xs leading-6 text-slate-500">
                              上面的 Provider Messages 已经把这次真正会发送的原始消息列出来了。这里仅解释压缩摘要和替代历史，不再重复原始消息。
                            </div>
                            {previewAnchor ? <div><span className="text-slate-500">历史锚点：</span>{previewAnchor}</div> : null}
                            {previewHistorySummary ? <div><span className="text-slate-500">压缩摘要：</span>{previewHistorySummary}</div> : null}
                            {previewReplacementHistory.length ? (
                              <div>
                                <div className="text-slate-500">压缩后替代历史：</div>
                                <div className="mt-1 space-y-1">
                                  {previewReplacementHistory.slice(0, 3).map((item, index) => (
                                    <div
                                      key={`${item.role}-${index}`}
                                      className="line-clamp-2 rounded-xl border border-white/[0.05] bg-black/10 px-2.5 py-1.5 text-slate-300"
                                    >
                                      <span className="mr-2 text-slate-500">{item.role}</span>
                                      {item.content}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </div>

                  {preferenceBadges.length ? (
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                      {preferenceBadges.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {effectivePreferenceBadges.length ? (
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-cyan-200/90">
                      {effectivePreferenceBadges.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {chatPreferenceCandidates.length ? (
                    <div className="space-y-2">
                      <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">候选偏好</div>
                      <div className="space-y-2">
                        {chatPreferenceCandidates.map((candidate) => {
                          const key = candidate.key
                          const selectedValue =
                            (chatPreferenceOverrides?.[key] as string | undefined) || candidate.suggested_value
                          const hasOverride = Boolean(chatPreferenceOverrides?.[key])
                          return (
                            <div
                              key={candidate.candidate_id}
                              className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-3"
                            >
                              <div className="flex flex-wrap items-start justify-between gap-2">
                                <div>
                                  <div className="text-sm font-medium text-slate-100">
                                    {preferenceKeyLabels[key]}
                                    <span className="ml-2 rounded-full border border-white/[0.08] bg-white/[0.04] px-2 py-0.5 text-[11px] text-slate-300">
                                      建议: {preferenceValueLabels[key][candidate.suggested_value]}
                                    </span>
                                  </div>
                                  {candidate.reason ? (
                                    <div className="mt-1 text-xs leading-5 text-slate-400">{candidate.reason}</div>
                                  ) : null}
                                  {candidate.source_excerpt ? (
                                    <div className="mt-1 line-clamp-2 text-xs leading-5 text-slate-500">
                                      “{candidate.source_excerpt}”
                                    </div>
                                  ) : null}
                                </div>
                                {onIgnoreChatPreferenceCandidate ? (
                                  <Button
                                    size="small"
                                    onClick={() => onIgnoreChatPreferenceCandidate(candidate.candidate_id)}
                                    className="rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-white/20 hover:text-white"
                                  >
                                    忽略
                                  </Button>
                                ) : null}
                              </div>

                              <div className="mt-3 flex flex-wrap gap-2">
                                {chatPreferenceOptions[key].map((option) => (
                                  <Button
                                    key={option.value}
                                    size="small"
                                    type={selectedValue === option.value ? 'primary' : 'default'}
                                    onClick={() =>
                                      onChatPreferenceOverrideChange?.(
                                        key,
                                        option.value as ChatUserPreferences[ChatPreferenceKey],
                                      )
                                    }
                                    className={
                                      selectedValue === option.value
                                        ? 'rounded-xl border-none bg-cyan-500 text-slate-950 shadow-none hover:!bg-cyan-400'
                                        : 'rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-cyan-400/30 hover:text-cyan-100'
                                    }
                                  >
                                    {option.label}
                                  </Button>
                                ))}
                                {hasOverride && onChatPreferenceOverrideClear ? (
                                  <Button
                                    size="small"
                                    onClick={() => onChatPreferenceOverrideClear(key)}
                                    className="rounded-xl border-white/10 bg-white/[0.04] text-slate-300 hover:border-white/20 hover:text-white"
                                  >
                                    恢复默认
                                  </Button>
                                ) : null}
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    {contextPreview ? (
                      <>
                        <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                          送入模型 {previewDebug?.message_count_sent ?? 0} 条
                        </span>
                        {previewDebug?.context_truncated ? (
                          <span className="rounded-full border border-amber-400/18 bg-amber-500/10 px-2.5 py-1 text-amber-200">
                            上下文会裁剪
                          </span>
                        ) : null}
                      </>
                    ) : (
                      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                        默认不自动请求完整预演
                      </span>
                    )}
                    {previewState?.open_questions?.length ? (
                      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                        未解问题 {previewState.open_questions.length}
                      </span>
                    ) : null}
                  </div>
                </>
              ) : (
                <>
                  <div className="grid gap-3 md:grid-cols-2">
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5">
                      <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">当前主题</div>
                      <div className="text-sm leading-6 text-slate-100">
                        {previewState?.active_topic || '当前未形成稳定主题'}
                      </div>
                    </div>
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5">
                      <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">发送方式</div>
                      <div className="text-sm leading-6 text-slate-100">
                        {isFirstTurn ? '首条消息直发' : '基于当前会话状态发送'}
                      </div>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">这次会输入给模型的内容</div>
                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-3 py-2.5 text-sm leading-6 text-slate-200">
                      <div className="space-y-2">
                        <div>{fallbackPreviewText}</div>
                        <div className="rounded-xl border border-cyan-400/12 bg-cyan-500/5 px-2.5 py-2 text-xs leading-6 text-cyan-100/90">
                          轻预览只会告诉你大致会继承哪些信息。要查看真正发给模型的 system prompt、messages 和 tools，请点“查看完整预演”。
                        </div>
                        {hasInheritedHistoryDetails ? (
                          <div className="space-y-2 border-t border-white/[0.05] pt-3">
                            {previewAnchor ? <div><span className="text-slate-500">历史锚点：</span>{previewAnchor}</div> : null}
                            {previewHistorySummary ? <div><span className="text-slate-500">压缩摘要：</span>{previewHistorySummary}</div> : null}
                            {inheritedRecentMessages.length ? (
                              <div>
                                <div className="text-slate-500">最近会继承的原始消息：</div>
                                <div className="mt-1 space-y-1">
                                  {inheritedRecentMessages.map((item, index) => (
                                    <div
                                      key={`${item.role}-${index}`}
                                      className="line-clamp-2 rounded-xl border border-white/[0.05] bg-black/10 px-2.5 py-1.5 text-slate-300"
                                    >
                                      <span className="mr-2 text-slate-500">{item.role}</span>
                                      {item.content}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                            {previewReplacementHistory.length ? (
                              <div>
                                <div className="text-slate-500">压缩后替代历史：</div>
                                <div className="mt-1 space-y-1">
                                  {previewReplacementHistory.slice(0, 3).map((item, index) => (
                                    <div
                                      key={`${item.role}-${index}`}
                                      className="line-clamp-2 rounded-xl border border-white/[0.05] bg-black/10 px-2.5 py-1.5 text-slate-300"
                                    >
                                      <span className="mr-2 text-slate-500">{item.role}</span>
                                      {item.content}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </div>

                  {preferenceBadges.length ? (
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                      {preferenceBadges.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {effectivePreferenceBadges.length ? (
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-cyan-200/90">
                      {effectivePreferenceBadges.map((item) => (
                        <span
                          key={item}
                          className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                      默认不自动请求完整预演
                    </span>
                    {effectivePreferenceBadges.length ? (
                      <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-cyan-100">
                        本轮偏好已覆盖默认设置
                      </span>
                    ) : null}
                    {previewState?.open_questions?.length ? (
                      <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1">
                        未解问题 {previewState.open_questions.length}
                      </span>
                    ) : null}
                  </div>
                </>
              )}
            </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        ) : null}

        {/* 底部信息 */}
        <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
          <span className="flex items-center gap-2">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                isSending ? 'bg-amber-400' : 'bg-emerald-400'
              } animate-pulse`}
            />
            <span className="text-slate-400">
              {isSending ? sendPhaseCopy.label : llmProvider || 'DeepSeek'}
            </span>
          </span>
          <span className="text-slate-600">
            {isSending
              ? `${sendPhaseCopy.hint} · 点击停止可中止`
              : 'Shift + Enter 换行 · Enter 发送'}
          </span>
        </div>
      </div>
    </div>
  )
}

export default ChatInput
