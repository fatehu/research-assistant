import { useEffect, useMemo, useRef, useState, forwardRef, isValidElement, type ReactNode } from 'react'
import { Button, Tooltip, Avatar, message } from 'antd'
import {
  RobotOutlined,
  UserOutlined,
  CopyOutlined,
  SearchOutlined,
  DatabaseOutlined,
  LinkOutlined,
  ThunderboltOutlined,
  DownOutlined,
  UpOutlined,
  EditOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  SHOW_RAG_METRICS,
  type Message,
  type MessageCitationSourceItem,
  type RagMetrics,
  type ConversationItemStream,
  type ToolWorkflowSummary,
  type ConversationToolLedger,
  type ConversationTurnStore,
  type MessageSpanRewriteResponse,
} from '@/services/api'
import CodeBlock from './CodeBlock'
import ThinkingPanel from './ThinkingPanel'
import HistoryReActPanel from './HistoryReActPanel'

interface MessageBubbleProps {
  msg: Message
  turnStore?: ConversationTurnStore
  itemStream?: ConversationItemStream
  toolLedger?: ConversationToolLedger
  showHistoryPrelude?: boolean
  isStreaming?: boolean
  streamingContent?: string
  streamingThought?: string
  isThinking?: boolean
  isHighlighted?: boolean
  onRewriteSpan?: (
    messageId: number,
    payload: {
      instruction: string
      selected_text: string
      before_context?: string
      after_context?: string
      occurrence_index?: number
    },
  ) => Promise<MessageSpanRewriteResponse>
}

interface CitationExplanationItem {
  id: string
  label: string
  sourceKind: string
  toolName?: string
  title?: string
  domain?: string
  url?: string
  knowledgeBase?: string
  document?: string
  sourceLabel?: string
  citationLabel?: string
  provider?: string
  providerRoute?: string
  contentPreview?: string
  retrievalScope?: Record<string, unknown>
  rank?: number
  chunkIndex?: number
  retrievalScore?: number
}

interface RagMetricCardItem {
  key: string
  label: string
  value: string
  icon: React.ReactNode
}

interface EnhancedTermParts {
  primary: string
  secondary: string
}

interface RewriteSelectionState {
  selectedText: string
  displayText: string
  beforeContext: string
  afterContext: string
  occurrenceIndex: number
  x: number
  y: number
}

interface RewriteAnimationState {
  oldContent: string
  newContent: string
  startOffset: number
  endOffset: number
  selectedText: string
  replacementText: string
}

const SPAN_REWRITE_OPTIONS = [
  {
    label: '更简洁',
    instruction: 'Make this selected span more concise while preserving facts and citation labels.',
  },
  {
    label: '更清楚',
    instruction: 'Make this selected span clearer and easier to read while preserving facts and citation labels.',
  },
  {
    label: '更正式',
    instruction: 'Make this selected span more formal while preserving facts and citation labels.',
  },
]

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

const findAllOccurrences = (content: string, selected: string): number[] => {
  const positions: number[] = []
  if (!selected) return positions
  let index = content.indexOf(selected)
  while (index >= 0) {
    positions.push(index)
    index = content.indexOf(selected, index + Math.max(selected.length, 1))
  }
  return positions
}

const countOccurrences = (content: string, selected: string): number => {
  return findAllOccurrences(content, selected).length
}

const expandMarkdownSelectionScaffold = (
  source: string,
  startOffset: number,
  endOffset: number,
): { startOffset: number; endOffset: number } => {
  let nextStart = startOffset
  let nextEnd = endOffset
  const lineStart = source.lastIndexOf('\n', Math.max(0, startOffset - 1)) + 1
  const leading = source.slice(lineStart, startOffset)
  const isLeadingScaffold = /^\s{0,3}(?:(?:#{1,6}\s+)|(?:[-*+]\s+)|(?:\d+[.)]\s+)|(?:>\s+))*[*_`~\s]*$/.test(leading)

  if (leading && isLeadingScaffold) {
    nextStart = lineStart
  }

  for (const token of ['***', '___', '**', '__', '~~', '`', '*', '_']) {
    if (source.startsWith(token, nextEnd)) {
      nextEnd += token.length
      break
    }
  }

  return { startOffset: nextStart, endOffset: nextEnd }
}

const shouldUseCharacterRewriteAnimation = (selectedText: string, replacementText: string): boolean => {
  const combined = `${selectedText}\n${replacementText}`
  if (combined.length > 360) return false
  if (/[\n\r]/.test(combined)) return false
  if (/(?:^|\n)\s{0,3}(?:#{1,6}|[-*+]|\d+[.)]|>)\s/.test(combined)) return false
  if (/[*_`[\]]/.test(combined)) return false
  return true
}

interface MarkdownVisibleTextIndex {
  text: string
  sourceOffsets: number[]
}

interface ResolvedRewriteSelection {
  selectedText: string
  displayText: string
  beforeContext: string
  afterContext: string
  occurrenceIndex: number
}

const normalizeVisibleText = (value: string): string => {
  return String(value || '').replace(/\s+/g, ' ').trim()
}

const appendVisibleIndexChar = (
  chars: string[],
  offsets: number[],
  char: string,
  sourceOffset: number,
) => {
  if (/\s/.test(char)) {
    if (chars.length > 0 && chars[chars.length - 1] !== ' ') {
      chars.push(' ')
      offsets.push(sourceOffset)
    }
    return
  }
  chars.push(char)
  offsets.push(sourceOffset)
}

const findLineSyntaxEnd = (source: string, offset: number): number => {
  const lineEnd = source.indexOf('\n', offset)
  const end = lineEnd >= 0 ? lineEnd : source.length
  const prefix = source.slice(offset, end)
  const match = prefix.match(/^\s{0,3}(?:(?:#{1,6}|[-*+]|\d+[.)]|>)\s+|\[[ xX]\]\s+)/)
  return match ? offset + match[0].length : offset
}

const buildMarkdownVisibleTextIndex = (source: string): MarkdownVisibleTextIndex => {
  const chars: string[] = []
  const sourceOffsets: number[] = []
  let index = 0
  let lineStart = true

  while (index < source.length) {
    if (lineStart) {
      const nextIndex = findLineSyntaxEnd(source, index)
      if (nextIndex > index) {
        index = nextIndex
      }
      lineStart = false
      if (index >= source.length) break
    }

    const char = source[index]
    if (char === '\n') {
      appendVisibleIndexChar(chars, sourceOffsets, ' ', index)
      index += 1
      lineStart = true
      continue
    }

    if (source.startsWith('![', index)) {
      index += 2
      continue
    }

    if (char === '[') {
      const closeBracket = source.indexOf(']', index + 1)
      if (closeBracket >= 0 && source[closeBracket + 1] === '(') {
        index += 1
        continue
      }
    }

    if (char === ']' && source[index + 1] === '(') {
        const closeIndex = source.indexOf(')', index + 2)
        index = closeIndex >= 0 ? closeIndex + 1 : index + 1
        continue
    }

    if (
      source.startsWith('***', index) ||
      source.startsWith('___', index)
    ) {
      index += 3
      continue
    }
    if (
      source.startsWith('**', index) ||
      source.startsWith('__', index) ||
      source.startsWith('~~', index)
    ) {
      index += 2
      continue
    }
    if (char === '*' || char === '_' || char === '`') {
      index += 1
      continue
    }

    appendVisibleIndexChar(chars, sourceOffsets, char, index)
    index += 1
  }

  while (chars.length > 0 && chars[chars.length - 1] === ' ') {
    chars.pop()
    sourceOffsets.pop()
  }

  return {
    text: chars.join(''),
    sourceOffsets,
  }
}

const resolveRewriteSelectionFromMarkdown = (
  source: string,
  renderedSelectedText: string,
  renderedBeforeText: string,
): ResolvedRewriteSelection | null => {
  const exactOccurrences = findAllOccurrences(source, renderedSelectedText)
  if (exactOccurrences.length > 0) {
    const renderedOccurrenceIndex = countOccurrences(renderedBeforeText, renderedSelectedText)
    const occurrenceIndex = Math.min(renderedOccurrenceIndex, exactOccurrences.length - 1)
    const sourceStart = exactOccurrences[occurrenceIndex]
    const sourceEnd = sourceStart + renderedSelectedText.length
    return {
      selectedText: renderedSelectedText,
      displayText: renderedSelectedText,
      beforeContext: source.slice(Math.max(0, sourceStart - 600), sourceStart),
      afterContext: source.slice(sourceEnd, sourceEnd + 600),
      occurrenceIndex,
    }
  }

  const normalizedSelected = normalizeVisibleText(renderedSelectedText)
  if (!normalizedSelected) return null

  const visibleIndex = buildMarkdownVisibleTextIndex(source)
  const matchPositions = findAllOccurrences(visibleIndex.text, normalizedSelected)
  if (matchPositions.length === 0) {
    return null
  }

  const normalizedBeforeLength = normalizeVisibleText(renderedBeforeText).length
  const visibleStart = matchPositions.reduce((best, current) =>
    Math.abs(current - normalizedBeforeLength) < Math.abs(best - normalizedBeforeLength)
      ? current
      : best,
  )
  const visibleEnd = visibleStart + normalizedSelected.length
  const sourceStart = visibleIndex.sourceOffsets[visibleStart]
  const sourceEnd = (visibleIndex.sourceOffsets[visibleEnd - 1] ?? sourceStart) + 1
  if (sourceStart == null || sourceEnd <= sourceStart) {
    return null
  }

  const expanded = expandMarkdownSelectionScaffold(source, sourceStart, sourceEnd)
  const selectedText = source.slice(expanded.startOffset, expanded.endOffset)
  const occurrenceIndex = Math.max(0, countOccurrences(source.slice(0, expanded.startOffset), selectedText))
  return {
    selectedText,
    displayText: renderedSelectedText,
    beforeContext: source.slice(Math.max(0, expanded.startOffset - 600), expanded.startOffset),
    afterContext: source.slice(expanded.endOffset, expanded.endOffset + 600),
    occurrenceIndex,
  }
}

const parseRagMetrics = (value: unknown): RagMetrics | null => {
  if (!value || typeof value !== 'object') {
    return null
  }

  const metrics = value as Partial<RagMetrics>
  if (typeof metrics.knowledge_search_calls !== 'number') {
    return null
  }

  const normalized: RagMetrics = {
    knowledge_search_calls: metrics.knowledge_search_calls,
    source_labels_count: Number(metrics.source_labels_count || 0),
    source_labels: Array.isArray(metrics.source_labels) ? metrics.source_labels : [],
    answer_citation_count: Number(metrics.answer_citation_count || 0),
    citation_required: Boolean(metrics.citation_required),
    citation_valid: Boolean(metrics.citation_valid),
    citation_repair_attempts: Number(metrics.citation_repair_attempts || 0),
    citation_repair_successes: Number(metrics.citation_repair_successes || 0),
    compression_calls: Number(metrics.compression_calls || 0),
    compression_success_chunks: Number(metrics.compression_success_chunks || 0),
    compression_fallback_chunks: Number(metrics.compression_fallback_chunks || 0),
  }

  const ragUsed =
    normalized.knowledge_search_calls > 0 ||
    normalized.source_labels_count > 0 ||
    normalized.answer_citation_count > 0 ||
    normalized.compression_calls > 0 ||
    normalized.citation_repair_attempts > 0 ||
    normalized.citation_repair_successes > 0

  return ragUsed ? normalized : null
}

const extractCitationLabels = (value: string): string[] => {
  const labels: string[] = []
  const seen = new Set<string>()
  const matches = value.matchAll(/\[(来源\d+|网页\d+)\]/g)
  for (const match of matches) {
    const label = String(match[1] || '').trim()
    if (!label || seen.has(label)) {
      continue
    }
    seen.add(label)
    labels.push(label)
  }
  return labels
}

const normalizeCitationSourceItem = (
  value: unknown,
  fallbackLabel?: string,
): CitationExplanationItem | null => {
  if (!value || typeof value !== 'object') {
    return null
  }
  const payload = value as Record<string, unknown>
  const label = String(payload.label || fallbackLabel || '').trim()
  if (!label) {
    return null
  }

  const toOptionalString = (key: string): string | undefined => {
    const raw = payload[key]
    const text = typeof raw === 'string' ? raw.trim() : String(raw || '').trim()
    return text || undefined
  }
  const toOptionalNumber = (key: string): number | undefined => {
    const raw = payload[key]
    if (typeof raw === 'number' && Number.isFinite(raw)) {
      return raw
    }
    if (typeof raw === 'string' && raw.trim()) {
      const parsed = Number(raw)
      return Number.isFinite(parsed) ? parsed : undefined
    }
    return undefined
  }

  return {
    id: label,
    label,
    sourceKind: toOptionalString('source_kind') || (label.startsWith('网页') ? 'public_web_search' : 'knowledge_base_search'),
    toolName: toOptionalString('tool_name'),
    title: toOptionalString('title'),
    domain: toOptionalString('domain'),
    url: toOptionalString('url'),
    knowledgeBase: toOptionalString('knowledge_base'),
    document: toOptionalString('document'),
    sourceLabel: toOptionalString('source_label'),
    citationLabel: toOptionalString('citation_label'),
    provider: toOptionalString('provider'),
    providerRoute: toOptionalString('provider_route'),
    contentPreview: toOptionalString('content_preview'),
    retrievalScope:
      payload.retrieval_scope && typeof payload.retrieval_scope === 'object'
        ? (payload.retrieval_scope as Record<string, unknown>)
        : undefined,
    rank: toOptionalNumber('rank'),
    chunkIndex: toOptionalNumber('chunk_index'),
    retrievalScore: toOptionalNumber('retrieval_score'),
  }
}

const buildCitationIndexFromMessage = (
  msg: Message,
): Map<string, CitationExplanationItem> => {
  const items = new Map<string, CitationExplanationItem>()
  const rawIndex = msg.metadata?.citation_index as Record<string, MessageCitationSourceItem> | undefined
  if (!rawIndex || typeof rawIndex !== 'object') {
    return items
  }
  for (const [label, value] of Object.entries(rawIndex)) {
    const item = normalizeCitationSourceItem(value, label)
    if (item) {
      items.set(item.label, item)
    }
  }
  return items
}

const buildCitationIndexFromLedger = (
  toolLedger: ConversationToolLedger | undefined,
  turnId: string | undefined,
): Map<string, CitationExplanationItem> => {
  const items = new Map<string, CitationExplanationItem>()
  if (!toolLedger?.entries?.length || !turnId) {
    return items
  }

  for (const entry of toolLedger.entries) {
    if (entry.kind !== 'tool_result' || entry.turn_id !== turnId) {
      continue
    }
    const metadata = entry.metadata
    if (!metadata || typeof metadata !== 'object') {
      continue
    }
    const sourceItems = Array.isArray((metadata as Record<string, unknown>).source_items)
      ? ((metadata as Record<string, unknown>).source_items as unknown[])
      : []
    for (const rawItem of sourceItems) {
      const item = normalizeCitationSourceItem(rawItem)
      if (item && !items.has(item.label)) {
        items.set(item.label, item)
      }
    }
  }
  return items
}

const parseCitationExplanationItems = (
  msg: Message,
  toolLedger: ConversationToolLedger | undefined,
  turnId: string | undefined,
): CitationExplanationItem[] => {
  const labels = extractCitationLabels(String(msg.content || ''))
  if (!labels.length) {
    return []
  }
  const messageIndex = buildCitationIndexFromMessage(msg)
  const ledgerIndex = buildCitationIndexFromLedger(toolLedger, turnId)
  return labels.map((label) => {
    const item = messageIndex.get(label) || ledgerIndex.get(label)
    if (item) {
      return item
    }
    return {
      id: label,
      label,
      sourceKind: 'unresolved_citation',
      sourceLabel: '该引用标签在当前消息中没有对应来源记录',
    }
  })
}

const renderCitationSourceKind = (item: CitationExplanationItem): string => {
  if (item.sourceKind === 'unresolved_citation') {
    return '未解析引用'
  }
  if (item.sourceKind === 'public_web_search') {
    return '公网搜索'
  }
  if (item.sourceKind === 'knowledge_base_search') {
    return '知识库检索'
  }
  return item.sourceKind || '引用来源'
}

const renderCitationScope = (scope: Record<string, unknown> | undefined): string => {
  if (!scope) {
    return ''
  }
  const scopeMode = String(scope.scope_mode || '').trim()
  const knowledgeBaseIds = Array.isArray(scope.knowledge_base_ids)
    ? scope.knowledge_base_ids.map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0)
    : []
  const documentIds = Array.isArray(scope.document_ids)
    ? scope.document_ids.map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0)
    : []
  if (documentIds.length > 0) {
    return `直达文档 ${documentIds.length} 个`
  }
  if (knowledgeBaseIds.length > 0) {
    return `指定知识库 ${knowledgeBaseIds.length} 个`
  }
  if (scopeMode === 'knowledge_base') {
    return '指定知识库'
  }
  if (scopeMode === 'document') {
    return '直达文档'
  }
  return '全部知识库'
}

const deriveMessageTurnId = (
  msg: Message,
  turnStore: ConversationTurnStore | undefined,
): string | undefined => {
  if (!turnStore?.entries?.length) return undefined
  if (msg.role === 'assistant') {
    return turnStore.entries.find((entry) => entry.assistant_message_id === msg.id)?.turn_id
  }
  if (msg.role === 'user') {
    return turnStore.entries.find((entry) => entry.user_message_id === msg.id)?.turn_id
  }
  return undefined
}

const deriveHistorySteps = (
  itemStream: ConversationItemStream | undefined,
  turnId: string | undefined,
): Array<{
  type: string
  iteration: number
  content?: string
  tool?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  workflowSummary?: ToolWorkflowSummary
  rawContent?: string
}> => {
  if (!itemStream?.entries?.length || !turnId) return []
  const steps: Array<{
    type: string
    iteration: number
    content?: string
    tool?: string
    input?: Record<string, unknown>
    output?: string
    success?: boolean
    workflowSummary?: ToolWorkflowSummary
    rawContent?: string
  }> = []
  itemStream.entries
    .filter((entry) => entry.turn_id === turnId)
    .forEach((entry) => {
      if (entry.kind === 'tool_use_summary') {
        steps.push({
          type: 'workflow',
          iteration: entry.iteration || 0,
          content: entry.summary || entry.content || '',
          rawContent: entry.content || entry.summary || '',
          workflowSummary:
            entry.metadata?.workflow_summary && typeof entry.metadata.workflow_summary === 'object'
              ? (entry.metadata.workflow_summary as ToolWorkflowSummary)
              : undefined,
        })
        return
      }
      if (entry.kind === 'permission_denial') {
        steps.push({
          type: 'workflow',
          iteration: entry.iteration || 0,
          content: entry.summary || entry.content || '',
          rawContent: entry.content || entry.summary || '',
          workflowSummary: {
            version: 'tool_workflow_summary.v1',
            headline: '权限受限，流程已等待',
            status: 'waiting',
            highlights: [entry.summary || entry.content || '当前步骤需要额外授权后才能继续。'],
            next_action: '等待授权或调整执行路径',
          },
        })
        return
      }
      if (entry.kind === 'reasoning_summary') {
        steps.push({
          type: 'thought',
          iteration: entry.iteration || 0,
          content: entry.summary || entry.content || '',
        })
        return
      }
      if (entry.kind === 'tool_call') {
        steps.push({
          type: 'action',
          iteration: entry.iteration || 0,
          tool: entry.tool_name,
          input: entry.arguments,
        })
        return
      }
      if (entry.kind === 'tool_result') {
        steps.push({
          type: 'observation',
          iteration: entry.iteration || 0,
          tool: entry.tool_name,
          output: entry.summary || entry.error || '',
          success: entry.success,
        })
      }
    })
  return steps
}

const flattenMarkdownText = (value: ReactNode): string => {
  if (value == null || typeof value === 'boolean') {
    return ''
  }
  if (typeof value === 'string' || typeof value === 'number') {
    return String(value)
  }
  if (Array.isArray(value)) {
    return value.map((item) => flattenMarkdownText(item)).join('')
  }
  if (isValidElement(value)) {
    return flattenMarkdownText(value.props.children)
  }
  return ''
}

const parseEnhancedTermParts = (value: string): EnhancedTermParts | null => {
  const normalized = String(value || '').replace(/\s+/g, ' ').trim()
  if (!normalized) {
    return null
  }
  const match = normalized.match(/^([A-Za-z][A-Za-z0-9/+:#&'’., -]{1,80}?)\s*[（(]\s*([^()（）\n]{1,40})\s*[）)]$/)
  if (!match) {
    return null
  }
  const primary = String(match[1] || '').trim()
  const secondary = String(match[2] || '').trim()
  if (!primary || !secondary) {
    return null
  }
  return { primary, secondary }
}

const EnhancedTermChip = ({ primary, secondary }: EnhancedTermParts) => (
  <span className="mx-0.5 inline-flex max-w-full items-center gap-2 rounded-full border border-emerald-400/18 bg-emerald-500/10 px-3 py-1 align-middle shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
    <span className="truncate text-[0.92em] font-semibold tracking-[0.01em] text-emerald-100">
      {primary}
    </span>
    <span className="h-3.5 w-px bg-white/[0.08]" />
    <span className="truncate text-[0.82em] font-medium text-cyan-200">
      {secondary}
    </span>
  </span>
)

/** 消息气泡 - 美化版 */
const MessageBubble = forwardRef<HTMLDivElement, MessageBubbleProps>(
  (
    {
      msg,
      turnStore,
      itemStream,
      toolLedger,
      showHistoryPrelude = true,
      isStreaming = false,
      streamingContent = '',
      isThinking = false,
      isHighlighted = false,
      onRewriteSpan,
    },
    ref
  ) => {
    const isUser = msg.role === 'user'
    const baseContent = isStreaming ? streamingContent : msg.content
    const contentRef = useRef<HTMLDivElement | null>(null)
    const [rewriteSelection, setRewriteSelection] = useState<RewriteSelectionState | null>(null)
    const [customRewriteInstruction, setCustomRewriteInstruction] = useState('')
    const [rewriteLoading, setRewriteLoading] = useState(false)
    const [rewriteAnimation, setRewriteAnimation] = useState<RewriteAnimationState | null>(null)
    const [animatedContent, setAnimatedContent] = useState<string | null>(null)
    const content = animatedContent ?? baseContent
    const turnId = useMemo(() => deriveMessageTurnId(msg, turnStore), [msg, turnStore])
    const historySteps = useMemo(
      () => (isStreaming ? [] : deriveHistorySteps(itemStream, turnId)),
      [isStreaming, itemStream, turnId],
    )
    const derivedThought = useMemo(
      () => historySteps.find((step) => step.type === 'thought')?.content || '',
      [historySteps],
    )
    const thought = isStreaming ? '' : derivedThought || msg.thought || ''
    const hasReasoningSummary = Boolean(derivedThought || msg.thought)
    const [thoughtExpanded, setThoughtExpanded] = useState(false)
    const [ragExpanded, setRagExpanded] = useState(false)
    const [evidenceExpanded, setEvidenceExpanded] = useState(false)
    const ragMetrics = !isStreaming && !isUser ? parseRagMetrics(msg.metadata?.rag_metrics) : null
    const citationItems = useMemo(
      () => (!isStreaming && !isUser ? parseCitationExplanationItems(msg, toolLedger, turnId) : []),
      [isStreaming, isUser, msg, toolLedger, turnId]
    )
    const ragMetricCards = useMemo<RagMetricCardItem[]>(
      () =>
        ragMetrics
          ? [
              {
                key: 'search',
                label: '检索调用',
                value: String(ragMetrics.knowledge_search_calls),
                icon: <SearchOutlined />,
              },
              {
                key: 'sources',
                label: '来源数',
                value: String(ragMetrics.source_labels_count),
                icon: <DatabaseOutlined />,
              },
              {
                key: 'citations',
                label: '答案引用',
                value: String(ragMetrics.answer_citation_count),
                icon: <LinkOutlined />,
              },
              {
                key: 'compression',
                label: '压缩调用',
                value: String(ragMetrics.compression_calls),
                icon: <ThunderboltOutlined />,
              },
            ]
          : [],
      [ragMetrics]
    )

    useEffect(() => {
      if (!rewriteAnimation) return
      let cancelled = false

      const runAnimation = async () => {
        const {
          oldContent,
          newContent,
          startOffset,
          endOffset,
          selectedText,
          replacementText,
        } = rewriteAnimation
        const prefix = oldContent.slice(0, startOffset)
        const suffix = oldContent.slice(endOffset)
        if (!shouldUseCharacterRewriteAnimation(selectedText, replacementText)) {
          setAnimatedContent(oldContent)
          await sleep(90)
          if (cancelled) return
          setAnimatedContent(newContent)
          await sleep(180)
          if (cancelled) return
          setAnimatedContent(null)
          setRewriteAnimation(null)
          return
        }

        const deleteStep = Math.max(1, Math.ceil(selectedText.length / 18))
        const typeStep = Math.max(1, Math.ceil(replacementText.length / 30))

        for (let length = selectedText.length; length > 0; length -= deleteStep) {
          if (cancelled) return
          const nextSpan = selectedText.slice(0, Math.max(0, length - deleteStep))
          setAnimatedContent(prefix + nextSpan + suffix)
          await sleep(16)
        }

        await sleep(90)

        for (let length = 0; length < replacementText.length; length += typeStep) {
          if (cancelled) return
          const nextSpan = replacementText.slice(0, Math.min(replacementText.length, length + typeStep))
          setAnimatedContent(prefix + nextSpan + suffix)
          await sleep(18)
        }

        if (cancelled) return
        setAnimatedContent(null)
        setRewriteAnimation(null)
      }

      void runAnimation()
      return () => {
        cancelled = true
      }
    }, [rewriteAnimation])

    const handleCopy = () => {
      navigator.clipboard.writeText(content)
      message.success('已复制到剪贴板')
    }

    const handleCaptureRewriteSelection = () => {
      if (isUser || isStreaming || rewriteLoading || rewriteAnimation || !onRewriteSpan) return
      const selection = window.getSelection()
      if (!selection || selection.rangeCount <= 0 || selection.isCollapsed) return
      const range = selection.getRangeAt(0)
      const container = contentRef.current
      if (!container || !container.contains(range.commonAncestorContainer)) return

      const selectedText = selection.toString()
      if (selectedText.trim().length < 2) return
      if (selectedText.length > 4000) {
        message.warning('选区太长，请缩小后再改写')
        return
      }

      const preRange = range.cloneRange()
      preRange.selectNodeContents(container)
      preRange.setEnd(range.startContainer, range.startOffset)
      const renderedBefore = preRange.toString()
      const currentContent = String(baseContent || '')
      const resolvedSelection = resolveRewriteSelectionFromMarkdown(
        currentContent,
        selectedText,
        renderedBefore,
      )
      if (!resolvedSelection) {
        message.warning('这个选区暂时无法映射到原始 Markdown，请缩小选区后重试')
        return
      }

      const rect = range.getBoundingClientRect()

      setRewriteSelection({
        ...resolvedSelection,
        x: Math.min(Math.max(rect.left + rect.width / 2, 180), window.innerWidth - 180),
        y: Math.max(rect.top - 12, 72),
      })
    }

    const runRewrite = async (instruction: string) => {
      if (!rewriteSelection || !onRewriteSpan) return
      const normalizedInstruction = instruction.trim()
      if (!normalizedInstruction) {
        message.warning('请输入改写要求')
        return
      }
      setRewriteLoading(true)
      try {
        const response = await onRewriteSpan(msg.id, {
          instruction: normalizedInstruction,
          selected_text: rewriteSelection.selectedText,
          before_context: rewriteSelection.beforeContext,
          after_context: rewriteSelection.afterContext,
          occurrence_index: rewriteSelection.occurrenceIndex,
        })
        setRewriteSelection(null)
        setCustomRewriteInstruction('')
        setAnimatedContent(response.old_content)
        setRewriteAnimation({
          oldContent: response.old_content,
          newContent: response.new_content,
          startOffset: response.start_offset,
          endOffset: response.end_offset,
          selectedText: response.selected_text,
          replacementText: response.replacement_text,
        })
      } finally {
        setRewriteLoading(false)
      }
    }
    const timeLabel = new Date(msg.created_at).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    })

    const bubbleColumnClass = isUser
      ? 'flex min-w-0 flex-1 flex-col items-end'
      : 'flex min-w-0 flex-1 flex-col'
    const userBubbleShellClass = 'inline-flex max-w-[min(76%,720px)] flex-col items-end'
    const assistantBubbleWidthClass = 'w-full max-w-[min(100%,920px)]'
    const hasAssistantPrelude =
      showHistoryPrelude && !isUser && !isStreaming && (historySteps.length > 0 || String(thought || '').trim().length > 0)
    const normalizedContent = String(content || '').trim()
    const normalizedThought = String(thought || '').trim()
    const shouldHideEmptyAssistantBubble =
      !isUser && !isStreaming && !normalizedContent && !normalizedThought && historySteps.length === 0

    if (shouldHideEmptyAssistantBubble) {
      return null
    }

    return (
      <motion.div
        ref={ref}
        id={`message-${msg.id}`}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: 'easeOut' }}
        className={`flex items-start gap-3.5 ${isUser ? 'flex-row-reverse' : ''} ${
          isHighlighted ? 'relative' : ''
        }`}
      >
        {/* 高亮效果 */}
        {isHighlighted && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 -mx-4 -my-2 rounded-xl bg-emerald-500/10 border border-emerald-500/30 pointer-events-none"
            style={{ zIndex: -1 }}
          />
        )}

        {/* 头像 */}
        <div className="flex-shrink-0">
          {isUser ? (
            <Avatar
              size={30}
              icon={<UserOutlined />}
              className="border border-slate-700/60 bg-slate-800 text-white shadow-[0_10px_24px_rgba(2,6,23,0.14)]"
            />
          ) : (
            <div className="flex h-[30px] w-[30px] items-center justify-center rounded-full border border-emerald-400/18 bg-slate-900/72 shadow-[0_10px_24px_rgba(2,6,23,0.14)]">
              <RobotOutlined className="text-sm text-emerald-300" />
            </div>
          )}
        </div>

        {/* 内容区 */}
        <div className={bubbleColumnClass}>
          <div className={`mb-2 flex items-center gap-2 ${isUser ? 'justify-end' : ''}`}>
            <span className={`text-xs font-medium tracking-wide ${isUser ? 'text-slate-300' : 'text-emerald-300'}`}>
              {isUser ? '你' : 'AI 助手'}
            </span>
            <span className="text-xs text-slate-500">{timeLabel}</span>
          </div>

          {SHOW_RAG_METRICS && !isUser && !isStreaming && ragMetrics && (
            <div className="mb-3 flex w-full max-w-[min(100%,860px)] flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setRagExpanded(!ragExpanded)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/[0.08] bg-slate-900/75 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:border-emerald-400/20 hover:text-white"
                >
                  <SearchOutlined className="text-[11px] text-emerald-300" />
                  检索质量
                  {ragExpanded ? <UpOutlined className="text-[10px]" /> : <DownOutlined className="text-[10px]" />}
                </button>
                <span
                  className={`rounded-full border px-2.5 py-1 text-[11px] ${
                    ragMetrics.citation_valid
                      ? 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200'
                      : 'border-amber-400/20 bg-amber-500/10 text-amber-200'
                  }`}
                >
                  {ragMetrics.citation_valid ? '引用有效' : '引用待修正'}
                </span>
                {ragMetricCards.map((item) => (
                  <span
                    key={item.key}
                    className="inline-flex items-center gap-1.5 rounded-full border border-white/[0.08] bg-slate-900/65 px-2.5 py-1 text-[11px] text-slate-300"
                  >
                    <span className="text-emerald-300">{item.icon}</span>
                    {item.label} {item.value}
                  </span>
                ))}
              </div>

              <AnimatePresence initial={false}>
                {ragExpanded && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    transition={{ duration: 0.18, ease: 'easeOut' }}
                    className="overflow-hidden"
                  >
                    <div className="rounded-2xl border border-white/[0.08] bg-slate-900/72 px-4 py-3 text-xs text-slate-300 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
                      <div className="flex flex-wrap gap-2">
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          需引用: {ragMetrics.citation_required ? '是' : '否'}
                        </span>
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          修复成功/尝试: {ragMetrics.citation_repair_successes}/{ragMetrics.citation_repair_attempts}
                        </span>
                        <span className="rounded-full border border-slate-700/80 bg-slate-950/70 px-2.5 py-1">
                          压缩命中/回退: {ragMetrics.compression_success_chunks}/{ragMetrics.compression_fallback_chunks}
                        </span>
                      </div>
                      <div className="mt-3 text-[11px] uppercase tracking-[0.18em] text-slate-500">来源标签</div>
                      <div className="mt-2 text-sm leading-6 text-slate-200">
                        {ragMetrics.source_labels.length > 0 ? ragMetrics.source_labels.join(' · ') : '-'}
                      </div>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* 消息内容 */}
          {isUser ? (
            <div className={userBubbleShellClass}>
              <div className="w-fit max-w-full rounded-2xl rounded-tr-md bg-slate-800 px-5 py-3 text-white shadow-[0_12px_24px_rgba(15,23,42,0.16)]">
                <p className="whitespace-pre-wrap text-base leading-7 text-white">{content}</p>
              </div>
            </div>
          ) : (
            <div className={assistantBubbleWidthClass}>
              <div className="overflow-hidden rounded-[24px] rounded-tl-md border border-white/[0.04] bg-[#13151A] px-8 pt-7 pb-8 shadow-[inset_0_1px_0_rgba(255,255,255,0.03),0_14px_40px_rgba(2,6,23,0.22)]">
                  {hasAssistantPrelude && (
                    <div className="mb-3 space-y-3 border-b border-white/[0.04] pb-3">
                      {historySteps.length > 0 && (
                        <HistoryReActPanel steps={historySteps} embedded />
                      )}
                      {thought && (
                        <ThinkingPanel
                          thought={thought}
                          isThinking={false}
                          isExpanded={thoughtExpanded}
                          onToggle={() => setThoughtExpanded(!thoughtExpanded)}
                          embedded
                          label={hasReasoningSummary ? '推理摘要' : '最终思考'}
                        />
                      )}
                    </div>
                  )}

                  {content ? (
                    <>
                      <div
                        ref={contentRef}
                        onMouseUp={handleCaptureRewriteSelection}
                        className="prose prose-invert prose-slate max-w-none
                        [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_li>p]:my-1
                        prose-p:my-6 prose-p:text-[16px] prose-p:leading-[1.95] prose-p:tracking-[0.004em] prose-p:text-slate-200/90
                        prose-headings:text-white prose-headings:font-semibold prose-headings:tracking-[-0.03em]
                        prose-pre:my-6 prose-pre:bg-slate-950/90 prose-pre:border prose-pre:border-slate-700/60 prose-pre:rounded-xl
                        prose-code:text-emerald-200 prose-code:bg-white/[0.06] prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-sm prose-code:font-mono
                        prose-strong:text-white prose-strong:font-bold
                        prose-em:text-slate-200 prose-em:italic
                        prose-a:text-emerald-300 prose-a:no-underline hover:prose-a:text-emerald-200 hover:prose-a:underline
                        prose-blockquote:border-l-4 prose-blockquote:border-emerald-500/30 prose-blockquote:bg-emerald-500/5 prose-blockquote:py-3 prose-blockquote:px-5 prose-blockquote:not-italic prose-blockquote:rounded-r-lg
                        prose-hr:border-white/[0.06] prose-hr:my-8
                        prose-table:border prose-table:border-white/[0.06] prose-th:bg-slate-800/50 prose-th:px-4 prose-th:py-3 prose-td:px-4 prose-td:py-3 prose-td:border-t prose-td:border-white/[0.06]
                        text-base leading-8"
                      >
                        <ReactMarkdown
                          remarkPlugins={[remarkGfm]}
                          components={{
                            code: ({ className, children }) => (
                              <CodeBlock className={className}>{children}</CodeBlock>
                            ),
                            h1: ({ children, ...props }) => (
                              <h1
                                {...props}
                                className="mt-12 mb-7 text-[30px] font-semibold leading-[1.14] tracking-[-0.045em] text-white"
                              >
                                {children}
                              </h1>
                            ),
                            h2: ({ children, ...props }) => (
                              <h2
                                {...props}
                                className="relative mt-12 mb-7 pl-4 text-[25px] font-semibold leading-[1.22] tracking-[-0.04em] text-white before:absolute before:left-0 before:top-1 before:h-[calc(100%-0.35rem)] before:w-1 before:rounded-full before:bg-emerald-300/70 before:shadow-[0_0_18px_rgba(110,231,183,0.45)] before:content-['']"
                              >
                                {children}
                              </h2>
                            ),
                            h3: ({ children, ...props }) => (
                              <h3
                                {...props}
                                className="mt-10 mb-5 text-[20px] font-semibold leading-[1.32] tracking-[-0.025em] text-slate-50"
                              >
                                {children}
                              </h3>
                            ),
                            p: ({ children, ...props }) => (
                              <p
                                {...props}
                                className="my-6 text-[16px] leading-[1.95] tracking-[0.004em] text-slate-200/90"
                              >
                                {children}
                              </p>
                            ),
                            ul: ({ children, ...props }) => (
                              <ul
                                {...props}
                                className="my-6 list-outside list-disc space-y-3 pl-7 text-slate-100 [padding-inline-start:1.75rem]"
                              >
                                {children}
                              </ul>
                            ),
                            ol: ({ children, ...props }) => (
                              <ol
                                {...props}
                                className="my-6 list-outside list-decimal space-y-3 pl-7 text-slate-100 [padding-inline-start:1.75rem]"
                              >
                                {children}
                              </ol>
                            ),
                            li: ({ children, ...props }) => (
                              <li
                                {...props}
                                className="my-2.5 pl-2 text-[16px] leading-[1.9] text-slate-100 marker:font-semibold marker:text-emerald-300/70"
                              >
                                {children}
                              </li>
                            ),
                            strong: ({ children, ...props }) => {
                              const flattened = flattenMarkdownText(children)
                              const enhancedTerm = parseEnhancedTermParts(flattened)
                              if (enhancedTerm) {
                                return <EnhancedTermChip {...enhancedTerm} />
                              }
                              return (
                                <strong {...props} className="font-semibold text-white">
                                  {children}
                                </strong>
                              )
                            },
                          }}
                        >
                          {content}
                        </ReactMarkdown>
                      </div>

                      {citationItems.length > 0 && (
                        <div className="mt-6 overflow-hidden rounded-xl border border-white/[0.05] bg-slate-950/75 shadow-sm">
                          <button
                            type="button"
                            onClick={() => setEvidenceExpanded((value) => !value)}
                            className="flex w-full items-center justify-between gap-3 bg-slate-950/70 px-5 py-3.5 text-left text-slate-200 transition-colors hover:bg-slate-900/80"
                          >
                            <div>
                              <div className="text-sm font-semibold text-slate-100">引用说明</div>
                              <div className="text-xs text-slate-500">
                                按本条回答实际使用的引用标签展开来源解释
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <div className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2.5 py-1 text-[11px] text-slate-300">
                                {citationItems.length} 条
                              </div>
                              <span className="flex h-7 w-7 items-center justify-center rounded-full border border-white/[0.08] bg-slate-900/80 text-slate-300">
                                {evidenceExpanded ? <UpOutlined className="text-[11px]" /> : <DownOutlined className="text-[11px]" />}
                              </span>
                            </div>
                          </button>

                          <AnimatePresence initial={false}>
                            {evidenceExpanded && (
                              <motion.div
                                initial={{ opacity: 0, height: 0 }}
                                animate={{ opacity: 1, height: 'auto' }}
                                exit={{ opacity: 0, height: 0 }}
                                transition={{ duration: 0.2, ease: 'easeOut' }}
                                className="overflow-hidden"
                              >
                                <div className="bg-slate-950/75 px-5 pb-4">
                                  <div className="space-y-3 pt-0.5">
                                  {citationItems.map((item) => (
                                    <div
                                      key={item.id}
                                      className="rounded-xl border border-slate-700/70 bg-slate-950/70 px-3.5 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]"
                                    >
                                      <div className="flex flex-wrap items-center gap-2 text-xs">
                                        <span className="rounded-full border border-emerald-400/20 bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-200">
                                          {item.label}
                                        </span>
                                        <span className="rounded-full border border-slate-600/70 bg-slate-800 px-2 py-0.5 text-slate-300">
                                          {renderCitationSourceKind(item)}
                                        </span>
                                        {item.retrievalScore !== undefined ? (
                                          <span className="rounded-full border border-slate-600/70 bg-slate-800 px-2 py-0.5 text-slate-300">
                                            检索 {item.retrievalScore}%
                                          </span>
                                        ) : null}
                                        {renderCitationScope(item.retrievalScope) ? (
                                          <span className="rounded-full border border-cyan-400/15 bg-cyan-500/10 px-2 py-0.5 text-cyan-200">
                                            {renderCitationScope(item.retrievalScope)}
                                          </span>
                                        ) : null}
                                      </div>

                                      <div className="mt-2 text-sm font-medium leading-6 text-slate-100">
                                        {item.title || [item.knowledgeBase, item.document].filter(Boolean).join(' / ') || item.sourceLabel || '未解析到更详细来源'}
                                      </div>
                                      {(item.citationLabel || item.domain || item.provider) && (
                                        <div className="mt-1 text-xs text-slate-500">
                                          {[item.citationLabel, item.domain, item.provider].filter(Boolean).join(' · ')}
                                        </div>
                                      )}
                                      {item.url ? (
                                        <a
                                          href={item.url}
                                          target="_blank"
                                          rel="noreferrer"
                                          className="mt-2 block truncate text-xs text-emerald-300 hover:text-emerald-200 hover:underline"
                                        >
                                          {item.url}
                                        </a>
                                      ) : null}
                                      {item.contentPreview ? (
                                        <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100">
                                          {item.contentPreview}
                                        </div>
                                      ) : null}
                                    </div>
                                  ))}
                                  </div>
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </div>
                      )}

                      {/* 流式输出光标 */}
                      {isStreaming && (
                        <span className="inline-block w-2 h-5 bg-emerald-400 animate-pulse ml-1 -mb-1 rounded-sm" />
                      )}
                    </>
                  ) : isStreaming ? (
                    <div className="flex items-center gap-3 py-2">
                      <div className="flex gap-1">
                        <span
                          className="w-2 h-2 bg-emerald-400 rounded-full animate-bounce"
                          style={{ animationDelay: '0ms' }}
                        />
                        <span
                          className="w-2 h-2 bg-teal-400 rounded-full animate-bounce"
                          style={{ animationDelay: '150ms' }}
                        />
                        <span
                          className="w-2 h-2 bg-cyan-400 rounded-full animate-bounce"
                          style={{ animationDelay: '300ms' }}
                        />
                      </div>
                      <span className="text-sm text-slate-400">
                        {isThinking ? '正在思考...' : '正在生成回答...'}
                      </span>
                    </div>
                  ) : null}

                  {/* 操作栏 */}
                      {!isStreaming && content && (
                    <div className="mt-5 flex items-center gap-3 border-t border-white/[0.04] pt-4">
                      {!isUser && onRewriteSpan && (
                        <Tooltip title="选中回复中的一段文字后改写">
                          <Button
                            type="text"
                            size="small"
                            icon={<EditOutlined />}
                            onClick={() => message.info('请先在这条回复里选中需要改写的一段文字')}
                            className="rounded-lg text-slate-400 transition-all hover:bg-white/[0.04] hover:text-cyan-300"
                          >
                            局部改写
                          </Button>
                        </Tooltip>
                      )}
                      <Tooltip title="复制内容">
                        <Button
                          type="text"
                          size="small"
                          icon={<CopyOutlined />}
                          onClick={handleCopy}
                          className="rounded-lg text-slate-400 transition-all hover:bg-white/[0.04] hover:text-emerald-300"
                        >
                          复制
                        </Button>
                      </Tooltip>

                      <div className="flex-1" />
                      <span className="text-xs text-slate-600">AI 生成内容，仅供参考</span>
                    </div>
                  )}
              </div>
            </div>
          )}
        </div>

        <AnimatePresence>
          {rewriteSelection && !isUser && !isStreaming && (
            <motion.div
              initial={{ opacity: 0, y: 8, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: 0.96 }}
              transition={{ duration: 0.16, ease: 'easeOut' }}
              className="fixed z-[80] w-[360px] rounded-2xl border border-cyan-300/20 bg-slate-950/95 p-3 shadow-[0_22px_70px_rgba(2,6,23,0.58)] backdrop-blur-xl"
              style={{
                left: Math.min(Math.max(rewriteSelection.x - 180, 16), window.innerWidth - 376),
                top: Math.max(rewriteSelection.y - 178, 16),
              }}
            >
              <div className="mb-2 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 text-xs font-medium text-cyan-100">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-cyan-400/10 text-cyan-200">
                    <EditOutlined />
                  </span>
                  局部改写选区
                </div>
                <button
                  type="button"
                  onClick={() => setRewriteSelection(null)}
                  className="rounded-full px-2 py-1 text-xs text-slate-500 transition-colors hover:bg-white/[0.06] hover:text-slate-200"
                >
                  取消
                </button>
              </div>
              <div className="mb-2 max-h-16 overflow-hidden rounded-xl border border-white/[0.06] bg-white/[0.04] px-3 py-2 text-xs leading-5 text-slate-300">
                {rewriteSelection.displayText}
              </div>
              <div className="mb-2 flex flex-wrap gap-2">
                {SPAN_REWRITE_OPTIONS.map((option) => (
                  <button
                    key={option.label}
                    type="button"
                    disabled={rewriteLoading}
                    onClick={() => void runRewrite(option.instruction)}
                    className="rounded-full border border-cyan-300/15 bg-cyan-400/10 px-3 py-1.5 text-xs text-cyan-100 transition-all hover:border-cyan-300/35 hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  value={customRewriteInstruction}
                  onChange={(event) => setCustomRewriteInstruction(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault()
                      void runRewrite(customRewriteInstruction)
                    }
                  }}
                  placeholder="自定义要求，例如：更像论文摘要"
                  className="min-w-0 flex-1 rounded-xl border border-white/[0.08] bg-slate-900/90 px-3 py-2 text-xs text-slate-100 outline-none transition-colors placeholder:text-slate-600 focus:border-cyan-300/40"
                />
                <button
                  type="button"
                  disabled={rewriteLoading}
                  onClick={() => void runRewrite(customRewriteInstruction)}
                  className="rounded-xl bg-cyan-300 px-3 py-2 text-xs font-semibold text-slate-950 transition-all hover:bg-cyan-200 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {rewriteLoading ? '改写中' : '应用'}
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    )
  }
)

MessageBubble.displayName = 'MessageBubble'

export default MessageBubble
