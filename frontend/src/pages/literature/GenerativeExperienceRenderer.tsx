import { useMemo, type ReactNode } from 'react'
import {
  Card,
  Collapse,
  Empty,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'

import type {
  ReaderComponentNode,
  ReaderExperienceBlockRef,
  ReaderExperienceGuidedBeat,
  ReaderExperiencePlan,
  ReaderGenerativeInteractionModule,
  ReaderGenerativeJsWidgetPlan,
  ReaderGenerativeResourceModule,
  ReaderStoryClaim,
  ReaderTeachingManuscriptSegment,
} from '@/services/api'
import { renderReaderComponentTree, type ReaderComponentRenderContext } from './readerComponents'
import {
  getInteractionModuleDefinition,
  getResourceModuleDefinition,
  getWidgetDefinition,
  isQuestionStarterModule,
  type ExperienceBlockActionHandler,
  type ExperienceBlockActionResolver,
} from './experienceBlockRegistry'
import type { ExperienceUiEvent } from './useExperienceActionBus'

const { Title, Text, Paragraph } = Typography

export type ExperienceLayoutVariant =
  | 'focus_figure_split'
  | 'guided_story_stack'
  | 'explainer_first'
  | 'resource_augmented_reader'

export type ExperienceRenderMode = 'full' | 'final_manuscript'

type ExperienceContextCard = {
  key: string
  title: string
  body: ReactNode
}

type FallbackQuestionAnswer = {
  question: string
  answer: string
}

type GuidedReadingSegment = {
  primary: ReaderExperienceGuidedBeat
  support: ReaderExperienceGuidedBeat[]
}

type TeachingManuscriptSegmentView = {
  segmentId: string
  segmentType: string
  title: string
  teachingText: string
  displayFlowChunks: ManuscriptTextChunkView[]
  anchorExcerpt: string
  targetIds: string[]
  fullEvidenceTargetIds: string[]
  inlineSlots: TeachingManuscriptSlotView[]
  glossaryRows: Array<{ term: string; note: string }>
  adjacentBridge: string
  referenceLinks: Array<{ label: string; href: string; note: string }>
}

type ManuscriptSlotKind = 'figure' | 'body'

type TeachingManuscriptSlotView = {
  slotId: string
  slotKind: ManuscriptSlotKind
  title: string
  summary: string
  targetIds: string[]
  fullEvidenceTargetIds: string[]
  anchorExcerpt: string
}

type ManuscriptTextChunkView =
  | { kind: 'text'; text: string }
  | { kind: 'slot'; slot: TeachingManuscriptSlotView }

type ManuscriptRenderState = {
  seenSlotEvidenceKeys: Set<string>
  seenLongNarrativeKeys: Set<string>
}

export type AdjacentContinuityBridgeItem = {
  key: string
  relationLabel: string
  pageLabel: string
  summary: string
  hints: string[]
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function isWeakPlaceholderText(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return true
  const lowered = text.toLowerCase()
  if (lowered.includes('暂无') || lowered.includes('暂未') || lowered.includes('生成中')) return true
  if (lowered === 'n/a' || lowered === 'none' || lowered === 'null') return true
  return false
}

function normalizeKeyToken(raw: unknown): string {
  return String(raw || '').trim().toLowerCase().replace(/\s+/g, ' ')
}

function isNearDuplicateSentence(left: unknown, right: unknown): boolean {
  const leftText = String(left || '').trim()
  const rightText = String(right || '').trim()
  if (!leftText || !rightText) return false
  if (leftText === rightText) return true
  const compact = (value: string) => normalizeKeyToken(value).replace(/[^\p{L}\p{N}]+/gu, '')
  const compactLeft = compact(leftText)
  const compactRight = compact(rightText)
  if (!compactLeft || !compactRight) return false
  if (compactLeft === compactRight) return true
  const shorter = compactLeft.length <= compactRight.length ? compactLeft : compactRight
  const longer = compactLeft.length > compactRight.length ? compactLeft : compactRight
  if (shorter.length < 8) return false
  return longer.includes(shorter) && (shorter.length / longer.length >= 0.85)
}

function isSummaryPrefixDuplicate(prefixCandidate: unknown, fullSummary: unknown): boolean {
  const prefixText = String(prefixCandidate || '').trim()
  const summaryText = String(fullSummary || '').trim()
  if (!prefixText || !summaryText) return false
  const compact = (value: string) => normalizeKeyToken(value).replace(/[^\p{L}\p{N}]+/gu, '')
  const compactPrefix = compact(prefixText)
  const compactSummary = compact(summaryText)
  if (!compactPrefix || !compactSummary) return false
  if (compactPrefix === compactSummary) return true
  if (compactPrefix.length < 6) return false
  return compactSummary.startsWith(compactPrefix)
}

function looksLikeLowValueResourceHost(rawHref: unknown): boolean {
  const href = String(rawHref || '').trim()
  if (!href) return false
  try {
    const url = new URL(href)
    const host = normalizeKeyToken(url.hostname).replace(/^www\./, '')
    return host === 'medvily.com'
  } catch {
    return false
  }
}

function isReaderWorthyResourceLink(rawHref: unknown): boolean {
  const href = String(rawHref || '').trim()
  if (!href) return false
  try {
    const url = new URL(href)
    const host = normalizeKeyToken(url.hostname).replace(/^www\./, '')
    const path = normalizeKeyToken(url.pathname)
    if (host === 'medvily.com' || host === 'celap.org.cn' || host === 'lib.smu.edu.cn') return false
    if (host === 'doi.org' && /\.(?:g|f|t)\d+(?:$|[/?#])/i.test(path)) return false
    return true
  } catch {
    return false
  }
}

function isGenericSupportSummary(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return true
  return [
    '补充少量真正需要的外部背景，帮助理解正文。',
    '这组背景资料只负责补一层解释，帮助你把刚读过的正文放回上下文。',
    '这组背景资料只负责帮你读懂图里的比较对象和现实含义，不替代正文。',
  ].includes(text)
}

function dedupeTrimmedText(rows: unknown[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const row of rows) {
    const token = String(row || '').trim()
    if (!token || seen.has(token)) continue
    seen.add(token)
    result.push(token)
  }
  return result
}

function asRecord(raw: unknown): Record<string, unknown> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  return raw as Record<string, unknown>
}

function toUnknownRows(raw: unknown): unknown[] {
  if (Array.isArray(raw)) return raw
  const record = asRecord(raw)
  if (record) return Object.values(record)
  return []
}

function toStringRows(raw: unknown): string[] {
  if (Array.isArray(raw)) return dedupeTrimmedText(raw)
  const text = String(raw || '').trim()
  return text ? [text] : []
}

function resolveManuscriptSlotKind(raw: unknown): ManuscriptSlotKind | null {
  const token = normalizeKeyToken(raw)
  if (!token) return null
  if (/(^|[\s:_-])(figure|fig|visual|chart|image|table|图|图示)([\s:_-]|$)/i.test(token)) return 'figure'
  if (/(^|[\s:_-])(body|text|paragraph|prose|excerpt|quote|正文|原文|段落)([\s:_-]|$)/i.test(token)) return 'body'
  return null
}

function isInternalTargetToken(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  if (/^p\d+\s*:\s*[a-z0-9_-]+$/i.test(text)) return true
  if (/^(?:target|slot|node|block)[\s:_-]*[a-z0-9_-]+$/i.test(text)) return true
  if (/^[a-z]\d{1,2}$/i.test(text)) return true
  return false
}

function sanitizeReaderFacingSlotTitle(raw: unknown): string {
  const text = sanitizeManuscriptNarrative(raw)
  if (!text) return ''
  if (isInternalTargetToken(text)) return ''
  if (/^slot(?:\s+\d+)?$/i.test(text)) return ''
  return text
}

function sanitizeReaderFacingSlotSummary(raw: unknown): string {
  const text = sanitizeManuscriptNarrative(raw)
  if (!text) return ''
  if (isInternalTargetToken(text)) return ''
  if (isEnglishHeavyReaderCopy(text) && text.length > 180) return ''
  return text
}

function buildManuscriptSlotEvidenceKey(
  slot: Pick<TeachingManuscriptSlotView, 'slotId' | 'slotKind' | 'targetIds' | 'fullEvidenceTargetIds' | 'anchorExcerpt'>,
  fallbackTargetIds: string[] = [],
): string {
  const targetToken = dedupeTrimmedText([
    ...slot.targetIds,
    ...slot.fullEvidenceTargetIds,
    ...fallbackTargetIds,
  ])
    .map((item) => normalizeKeyToken(item))
    .filter(Boolean)
    .sort()
    .join('|')
  if (targetToken) return `${slot.slotKind}::target::${targetToken}`
  const anchorToken = normalizeKeyToken(slot.anchorExcerpt).replace(/[^\p{L}\p{N}]+/gu, '')
  if (anchorToken.length >= 24) return `${slot.slotKind}::anchor::${anchorToken.slice(0, 220)}`
  return `${slot.slotKind}::slot::${normalizeKeyToken(slot.slotId)}`
}

function normalizeManuscriptSlotBinding(
  raw: unknown,
  fallback: {
    slotIdBase: string
    fallbackKind: ManuscriptSlotKind | null
    targetIds: string[]
    fullEvidenceTargetIds: string[]
    anchorExcerpt: string
  },
): TeachingManuscriptSlotView | null {
  const record = asRecord(raw)
  if (!record) return null
  const slotId = String(
    record.slot_id
    || record.binding_id
    || record.id
    || record.key
    || record.name
    || record.ref_id
    || '',
  ).trim() || fallback.slotIdBase
  const slotKind = resolveManuscriptSlotKind(
    record.slot_kind
    || record.slot_type
    || record.kind
    || record.type
    || slotId
    || record.title
    || record.label,
  ) || fallback.fallbackKind
  if (!slotKind) return null
  const targetIds = dedupeTrimmedText([
    ...toStringRows(record.target_ids),
    ...toStringRows(record.source_target_ids),
    ...toStringRows(record.inline_target_ids),
    ...toStringRows(record.node_target_ids),
    ...fallback.targetIds,
  ])
  const fullEvidenceTargetIds = dedupeTrimmedText([
    ...toStringRows(record.full_evidence_target_ids),
    ...toStringRows(record.evidence_target_ids),
    ...toStringRows(record.target_ids),
    ...toStringRows(record.source_target_ids),
    ...fallback.fullEvidenceTargetIds,
    ...targetIds,
  ])
  const title = sanitizeReaderFacingSlotTitle(
    preferDisplayCopy(record.display_title, record.title || record.label || ''),
  )
  let summary = sanitizeReaderFacingSlotSummary(
    preferDisplayCopy(record.display_summary, record.summary || record.note || ''),
  )
  if (title && summary && isNearDuplicateSentence(title, summary)) summary = ''
  return {
    slotId,
    slotKind,
    title,
    summary,
    targetIds,
    fullEvidenceTargetIds,
    anchorExcerpt: String(record.anchor_excerpt || record.quote || record.anchor_quote || fallback.anchorExcerpt || '').trim(),
  }
}

function normalizeSlotList(
  rows: unknown[],
  fallback: {
    slotIdBase: string
    fallbackKind: ManuscriptSlotKind | null
    targetIds: string[]
    fullEvidenceTargetIds: string[]
    anchorExcerpt: string
  },
): TeachingManuscriptSlotView[] {
  const seen = new Set<string>()
  const result: TeachingManuscriptSlotView[] = []
  rows.forEach((row, index) => {
    const slot = normalizeManuscriptSlotBinding(row, {
      ...fallback,
      slotIdBase: `${fallback.slotIdBase}-${index + 1}`,
    })
    if (!slot) return
    const dedupeKey = buildManuscriptSlotEvidenceKey(slot, fallback.targetIds)
    if (seen.has(dedupeKey)) return
    seen.add(dedupeKey)
    result.push(slot)
  })
  return result
}

function stripSlotTokensFromCopy(rawText: string): string {
  const text = String(rawText || '')
  if (!text) return ''
  return text
    .replace(/<slot\b[^>]*\/?>/gi, ' ')
    .replace(/\{\{\s*(?:slot|figure_slot|body_slot)\s*:\s*[^}]+\s*\}\}/gi, ' ')
    .replace(/\[\[\s*(?:slot|figure_slot|body_slot)\s*:\s*[^\]]+\s*\]\]/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function tokenizeManuscriptCopyWithSlots(
  rawText: string,
  slots: TeachingManuscriptSlotView[],
): { chunks: ManuscriptTextChunkView[]; usedSlotIds: Set<string> } {
  const text = String(rawText || '').trim()
  if (!text) return { chunks: [], usedSlotIds: new Set<string>() }
  if (!slots.length) {
    const cleaned = stripSlotTokensFromCopy(text)
    return cleaned
      ? { chunks: [{ kind: 'text', text: cleaned }], usedSlotIds: new Set<string>() }
      : { chunks: [], usedSlotIds: new Set<string>() }
  }

  const normalizedText = text.replace(
    /<slot\b[^>]*(?:id|name)=["']?([^"'\s>]+)["']?[^>]*\/?>/gi,
    (_match, slotId: string) => `{{slot:${String(slotId || '').trim()}}`,
  )
  const usedSlotIds = new Set<string>()
  const slotLookup = new Map<string, TeachingManuscriptSlotView>()
  slots.forEach((slot) => {
    const key = normalizeKeyToken(slot.slotId)
    if (key) slotLookup.set(key, slot)
  })
  const pickSlot = (slotToken: string, slotIdToken: string) => {
    const normalizedId = normalizeKeyToken(slotIdToken)
    if (normalizedId && slotLookup.has(normalizedId)) return slotLookup.get(normalizedId) || null
    const kindHint = resolveManuscriptSlotKind(slotToken)
    const kindSlot = kindHint
      ? slots.find((slot) => slot.slotKind === kindHint && !usedSlotIds.has(normalizeKeyToken(slot.slotId)))
      : null
    if (kindSlot) return kindSlot
    return slots.find((slot) => !usedSlotIds.has(normalizeKeyToken(slot.slotId))) || null
  }

  const tokenPattern = /\{\{\s*([a-z_]+)(?:\s*:\s*([^}]+))?\s*\}\}|\[\[\s*([a-z_]+)(?:\s*:\s*([^\]]+))?\s*\]\]/gi
  const chunks: ManuscriptTextChunkView[] = []
  let cursor = 0
  let match: RegExpExecArray | null = tokenPattern.exec(normalizedText)
  while (match) {
    const tokenStart = match.index
    const tokenEnd = tokenPattern.lastIndex
    if (tokenStart > cursor) {
      const prose = normalizedText.slice(cursor, tokenStart).trim()
      if (prose) chunks.push({ kind: 'text', text: prose })
    }
    const tokenType = String(match[1] || match[3] || '').trim()
    const tokenId = String(match[2] || match[4] || '').trim()
    const slot = pickSlot(tokenType, tokenId)
    if (slot) {
      usedSlotIds.add(normalizeKeyToken(slot.slotId))
      chunks.push({ kind: 'slot', slot })
    } else {
      const normalizedType = normalizeKeyToken(tokenType)
      const isSlotToken = normalizedType === 'slot' || normalizedType === 'figure_slot' || normalizedType === 'body_slot'
      if (!isSlotToken) {
        const tokenRaw = String(match[0] || '').trim()
        if (tokenRaw) chunks.push({ kind: 'text', text: tokenRaw })
      }
    }
    cursor = tokenEnd
    match = tokenPattern.exec(normalizedText)
  }
  if (cursor < normalizedText.length) {
    const tail = normalizedText.slice(cursor).trim()
    if (tail) chunks.push({ kind: 'text', text: tail })
  }

  if (!chunks.length) return { chunks: [{ kind: 'text', text }], usedSlotIds }
  return { chunks, usedSlotIds }
}

function extractSlotReferenceIds(rawText: string): string[] {
  const text = String(rawText || '').trim()
  if (!text) return []
  const ids: string[] = []
  const tokenPattern = /\{\{\s*(?:slot|figure_slot|body_slot)\s*:\s*([^}]+)\s*\}\}|\[\[\s*(?:slot|figure_slot|body_slot)\s*:\s*([^\]]+)\s*\]\]/gi
  let match: RegExpExecArray | null = tokenPattern.exec(text)
  while (match) {
    const token = String(match[1] || match[2] || '').trim()
    if (token) ids.push(token)
    match = tokenPattern.exec(text)
  }
  return dedupeTrimmedText(ids)
}

function isLowSignalNarrativeCopy(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return true
  return [
    '先抓住图里最值得注意的信息。',
    '先把当前内容放回前后文里。',
    '先补一层必要的方法背景。',
    '先补上理解当前内容需要的背景。',
    '先知道这一页为什么值得读，以及接下来按什么顺序理解它。',
    '先用图或关键证据建立抓手，再回到正文核对作者的解释。',
    '外部资源保留少量高相关来源，方便按需展开。',
  ].includes(text)
}

function isEnglishHeavyReaderCopy(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  const alphaChars = (text.match(/[A-Za-z]/g) || []).length
  const cjkChars = (text.match(/[\u3400-\u9fff]/g) || []).length
  if (alphaChars >= 80 && cjkChars <= 24) return true
  return alphaChars > cjkChars * 2.5 && alphaChars >= 48
}

function isLowSignalContinuityCopy(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return true
  if (isLowSignalNarrativeCopy(text)) return true
  if (isEnglishHeavyReaderCopy(text)) return true
  return [
    '这是整页阅读的主骨架，后续解释和资源都应附着在这条主干上。',
    '把注意力放在最强证据上，避免一开始就淹没在大段正文里。',
  ].includes(text)
}

function clampEvidenceSentence(raw: unknown, maxChars: number = 280): string {
  const text = String(raw || '').trim().replace(/\s+/g, ' ')
  if (!text) return ''
  const sentenceMatches = text.match(/[^。！？!?]+[。！？!?]?/g) || [text]
  const picked: string[] = []
  let lengthBudget = 0
  for (const sentence of sentenceMatches) {
    const token = sentence.trim()
    if (!token) continue
    if (picked.length >= 2) break
    if (lengthBudget > 0 && lengthBudget + token.length > maxChars) break
    picked.push(token)
    lengthBudget += token.length
    if (lengthBudget >= maxChars) break
  }
  const preview = picked.join(' ').trim() || text
  if (preview.length <= maxChars) return preview
  return `${preview.slice(0, Math.max(0, maxChars - 1)).trim()}…`
}

function buildEvidenceAnchorQuote(raw: unknown, maxChars: number = 140): string {
  const preview = clampEvidenceSentence(raw, maxChars)
  if (!preview) return ''
  const trimmed = preview.replace(/\s+/g, ' ').trim()
  if (!trimmed) return ''
  return `“${trimmed}”`
}

function buildReaderFacingClaimSnippet(raw: unknown, maxChars: number = 72): string {
  const preview = clampEvidenceSentence(raw, maxChars)
  if (!preview) return ''
  return preview.replace(/\s+/g, ' ').trim()
}

function buildEvidencePreviewNode(node: ReaderComponentNode): { node: ReaderComponentNode; truncated: boolean } {
  const nodeType = String(node.type || '').trim()
  if (nodeType === 'ParagraphProse') {
    const props = (node.props && typeof node.props === 'object')
      ? { ...(node.props as Record<string, unknown>) }
      : {}
    const rawParagraphs = Array.isArray(props.paragraphs)
      ? props.paragraphs
      : []
    const firstParagraph = rawParagraphs.find((row) => row && typeof row === 'object' && String((row as Record<string, unknown>).text || '').trim())
    const rawText = String(
      (firstParagraph && typeof firstParagraph === 'object' ? (firstParagraph as Record<string, unknown>).text : '')
      || props.text
      || '',
    ).trim()
    const previewText = clampEvidenceSentence(rawText, isEnglishHeavyReaderCopy(rawText) ? 220 : 280)
    if (!previewText || previewText === rawText) {
      return { node, truncated: false }
    }
    return {
      truncated: true,
      node: {
        ...node,
        props: {
          ...props,
          text: previewText,
          paragraphs: [{ text: previewText }],
        },
        children: [],
      },
    }
  }
  if (nodeType === 'FigurePanel') {
    const props = (node.props && typeof node.props === 'object')
      ? { ...(node.props as Record<string, unknown>) }
      : {}
    const caption = String(props.caption || '').trim()
    const previewCaption = isEnglishHeavyReaderCopy(caption)
      ? ''
      : clampEvidenceSentence(caption, 140)
    if (previewCaption === caption) return { node, truncated: false }
    return {
      truncated: true,
      node: {
        ...node,
        props: {
          ...props,
          caption: previewCaption,
        },
        children: [],
      },
    }
  }
  if (nodeType === 'ListBlock') {
    const props = (node.props && typeof node.props === 'object')
      ? { ...(node.props as Record<string, unknown>) }
      : {}
    const items = Array.isArray(props.items)
      ? props.items.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    const nextItems = items.slice(0, 3).map((item) => clampEvidenceSentence(item, 140))
    const truncated = nextItems.length < items.length || nextItems.some((item, index) => item !== items[index])
    if (!truncated) return { node, truncated: false }
    return {
      truncated: true,
      node: {
        ...node,
        props: {
          ...props,
          items: nextItems,
        },
        children: [],
      },
    }
  }
  return { node, truncated: false }
}

function extractPreviewLeadText(node: ReaderComponentNode): string {
  const props = (node.props && typeof node.props === 'object')
    ? node.props as Record<string, unknown>
    : {}
  if (node.type === 'ParagraphProse') {
    const paragraphs = Array.isArray(props.paragraphs) ? props.paragraphs : []
    const firstParagraph = paragraphs.find((row) => row && typeof row === 'object' && String((row as Record<string, unknown>).text || '').trim())
    return String(
      (firstParagraph && typeof firstParagraph === 'object' ? (firstParagraph as Record<string, unknown>).text : '')
      || props.text
      || '',
    ).trim()
  }
  if (node.type === 'ListBlock') {
    const items = Array.isArray(props.items) ? props.items : []
    return String(items[0] || '').trim()
  }
  return String(props.caption || props.title || props.text || '').trim()
}

function isFragmentaryBodyLeadText(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  const normalized = text.replace(/\s+/g, ' ')

  // Case 1: short lead text that clearly looks like a continuation fragment.
  if (
    normalized.length < 140
    && /^[a-z(]/.test(normalized)
    && !/[.!?。！？]$/.test(normalized)
  ) {
    return true
  }

  // Case 2: sentence-like tail fragment such as
  // "adjudicator, as a second-year medical student ... for Step ..."
  // that starts lowercase and carries continuation markers.
  if (normalized.length <= 260 && /^[a-z]/.test(normalized)) {
    const hasContinuationMarker = /,\s*as\s+an?\s+/i.test(normalized)
      || /\bfor\s+Step\s*[123]\b/i.test(normalized)
      || /\bpost-graduate\s+year\b/i.test(normalized)
    const commaCount = (normalized.match(/,/g) || []).length
    if (hasContinuationMarker && commaCount >= 1) return true
  }

  return false
}

function sanitizeReaderFacingNarrative(raw: unknown): string {
  const text = String(raw || '').trim()
  if (!text) return ''
  if (isLowSignalNarrativeCopy(text)) return ''
  if (isEnglishHeavyReaderCopy(text)) return ''
  if (text.includes('只在正文需要时补一层') || text.includes('而不是把外部资料变成主线')) return ''
  if (text.includes('primary anchor') || text.includes('Main text body')) return ''
  if (text.includes('建议结合原文逐句核对') && text.includes('是怎样被解释的')) {
    return '先抓住这段材料里最关键的比较，再回到正文看看作者如何解释这些结果。'
  }
  return text
    .replace(/“这一段正文包含本页的重要结论，建议结合原文逐句核对。”/g, '正文里的关键结果')
    .replace(/\b当前焦点\b/g, '当前页关键证据')
}

function sanitizeManuscriptNarrative(raw: unknown): string {
  const text = String(raw || '').trim()
  if (!text) return ''
  if (isWeakPlaceholderText(text)) return ''
  return text
}

function normalizeReaderFacingFocusLabel(raw: unknown): string {
  const text = String(raw || '').trim()
  if (!text) return ''
  if (['当前焦点', 'current focus', 'focus', 'page focus'].includes(text.toLowerCase())) return ''
  return text
}

function buildIdLookup<T extends { module_id?: string; widget_id?: string }>(
  rows: T[],
  key: 'module_id' | 'widget_id',
): Map<string, T> {
  const lookup = new Map<string, T>()
  for (const row of rows) {
    const token = String(row?.[key] || '').trim()
    if (token) lookup.set(token, row)
  }
  return lookup
}

const PRIMARY_GUIDED_BEAT_TYPES = new Set(['figure_walkthrough', 'body_segment'])
const ATTACHED_GUIDED_BEAT_TYPES = new Set(['concept_bridge', 'why_it_matters', 'context_bridge'])

type GenerativeExperienceRendererProps = {
  renderMode?: ExperienceRenderMode
  layoutVariant: ExperienceLayoutVariant
  hero: ReaderExperiencePlan['hero'] | null
  focusHeading: string
  visibleClaims: ReaderStoryClaim[]
  contextCards: ExperienceContextCard[]
  narrativeSections: ReaderExperiencePlan['main_sections']
  guidedBeats: ReaderExperienceGuidedBeat[]
  teachingManuscript?: ReaderExperiencePlan['teaching_manuscript'] | null
  toolEnrichmentPacket: Record<string, unknown>
  focusNode: ReaderComponentNode | null
  bodyFlowNodes: ReaderComponentNode[]
  readingFlowNodes: ReaderComponentNode[]
  renderCtx: ReaderComponentRenderContext
  composeLoading: boolean
  hasComposePayload: boolean
  backgroundRefreshing: boolean
  fallbackQuestionAnswers: FallbackQuestionAnswer[]
  resourceModules: ReaderGenerativeResourceModule[]
  interactionModules: ReaderGenerativeInteractionModule[]
  widgetBlocks: ReaderGenerativeJsWidgetPlan[]
  getBlockUiAction: ExperienceBlockActionResolver
  dispatchBlockAction: ExperienceBlockActionHandler
  lastUiEvent: ExperienceUiEvent
  topStatusText: string
  adjacentContinuityBridge?: AdjacentContinuityBridgeItem[]
  seedMode?: boolean
}

export function GenerativeExperienceRenderer(props: GenerativeExperienceRendererProps) {
  const {
    renderMode = 'full',
    layoutVariant,
    hero,
    focusHeading,
    visibleClaims,
    contextCards,
    narrativeSections,
    guidedBeats,
    teachingManuscript = null,
    toolEnrichmentPacket,
    focusNode,
    bodyFlowNodes,
    readingFlowNodes,
    renderCtx,
    composeLoading,
    hasComposePayload,
    backgroundRefreshing,
    fallbackQuestionAnswers,
    resourceModules,
    interactionModules,
    widgetBlocks,
    getBlockUiAction,
    dispatchBlockAction,
    lastUiEvent: _lastUiEvent,
    topStatusText,
    adjacentContinuityBridge = [],
    seedMode = false,
  } = props
  const isFinalManuscriptOnly = renderMode === 'final_manuscript'

  const resourceModuleLookup = useMemo(() => buildIdLookup(resourceModules, 'module_id'), [resourceModules])
  const interactionModuleLookup = useMemo(() => buildIdLookup(interactionModules, 'module_id'), [interactionModules])
  const widgetLookup = useMemo(() => buildIdLookup(widgetBlocks, 'widget_id'), [widgetBlocks])

  const mainNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || 'main').trim() === 'main'),
    [narrativeSections],
  )
  const sidebarNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || '').trim() === 'sidebar'),
    [narrativeSections],
  )
  const footerNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || '').trim() === 'footer'),
    [narrativeSections],
  )
  const hasGuidedBeats = guidedBeats.length > 0
  const beatPacketLookup = useMemo(() => {
    const rows = Array.isArray(toolEnrichmentPacket?.beat_packets)
      ? toolEnrichmentPacket.beat_packets as Array<Record<string, unknown>>
      : []
    const lookup = new Map<string, Record<string, unknown>>()
    for (const row of rows) {
      const beatId = String(row?.beat_id || '').trim()
      if (beatId) lookup.set(beatId, row)
    }
    return lookup
  }, [toolEnrichmentPacket])
  const manuscriptPayload = useMemo(() => {
    const manuscriptFromPlan = teachingManuscript && typeof teachingManuscript === 'object'
      ? teachingManuscript as unknown as Record<string, unknown>
      : null
    const manuscriptFallback = toolEnrichmentPacket && typeof toolEnrichmentPacket === 'object'
      ? (toolEnrichmentPacket as Record<string, unknown>).teaching_manuscript
      : null
    if (manuscriptFromPlan) return manuscriptFromPlan
    if (manuscriptFallback && typeof manuscriptFallback === 'object') {
      return manuscriptFallback as Record<string, unknown>
    }
    return null
  }, [teachingManuscript, toolEnrichmentPacket])
  const manuscriptSegments = useMemo(() => {
    if (!manuscriptPayload) return []
    const rootSlotRows = [
      ...toUnknownRows(manuscriptPayload.slot_bindings),
      ...toUnknownRows(manuscriptPayload.slots),
      ...toUnknownRows(manuscriptPayload.evidence_slots),
    ]
    const rootSlots = normalizeSlotList(rootSlotRows, {
      slotIdBase: 'ms-root-slot',
      fallbackKind: null,
      targetIds: [],
      fullEvidenceTargetIds: [],
      anchorExcerpt: '',
    })
    const rootSlotLookup = new Map<string, TeachingManuscriptSlotView>()
    rootSlots.forEach((slot) => {
      const key = normalizeKeyToken(slot.slotId)
      if (key) rootSlotLookup.set(key, slot)
    })
    const mergeSlotWithRoot = (slot: TeachingManuscriptSlotView): TeachingManuscriptSlotView => {
      const root = rootSlotLookup.get(normalizeKeyToken(slot.slotId))
      if (!root) return slot
      return {
        slotId: slot.slotId || root.slotId,
        slotKind: slot.slotKind || root.slotKind,
        title: slot.title || root.title,
        summary: slot.summary || root.summary,
        targetIds: slot.targetIds.length ? slot.targetIds : root.targetIds,
        fullEvidenceTargetIds: slot.fullEvidenceTargetIds.length ? slot.fullEvidenceTargetIds : root.fullEvidenceTargetIds,
        anchorExcerpt: slot.anchorExcerpt || root.anchorExcerpt,
      }
    }
    const normalizeCombinedSlots = (
      rawRows: unknown[],
      fallback: {
        slotIdBase: string
        fallbackKind: ManuscriptSlotKind | null
        targetIds: string[]
        fullEvidenceTargetIds: string[]
        anchorExcerpt: string
      },
      extraIds: string[] = [],
    ) => {
      const rows = [...rawRows]
      extraIds.forEach((slotId) => {
        const slot = rootSlotLookup.get(normalizeKeyToken(slotId))
        if (slot) rows.push(slot)
      })
      const slots = normalizeSlotList(rows, fallback).map((slot) => mergeSlotWithRoot(slot))
      const seen = new Set<string>()
      const result: TeachingManuscriptSlotView[] = []
      slots.forEach((slot) => {
        const key = buildManuscriptSlotEvidenceKey(slot, fallback.targetIds)
        if (seen.has(key)) return
        seen.add(key)
        result.push(slot)
      })
      return result
    }
    const rows: ReaderTeachingManuscriptSegment[] = Array.isArray(manuscriptPayload.segments)
      ? manuscriptPayload.segments as ReaderTeachingManuscriptSegment[]
      : []
    const result: TeachingManuscriptSegmentView[] = []
    for (const row of rows) {
      if (!row) continue
      const teachingText = sanitizeManuscriptNarrative(row.teaching_text)
      const title = sanitizeManuscriptNarrative(
        row.title || row.segment_type,
      )
      const targetIds = dedupeTrimmedText(
        Array.isArray(row.target_ids)
          ? row.target_ids as unknown[]
          : [],
      )
      const rowRecord = row as unknown as Record<string, unknown>
      const fullEvidenceTargetIds = dedupeTrimmedText([
        ...toStringRows(rowRecord.full_evidence_target_ids),
        ...targetIds,
      ])
      const glossaryRows = Array.isArray(row.glossary)
        ? row.glossary
          .map((item) => ({
            term: String(item?.term || '').trim(),
            note: sanitizeManuscriptNarrative(item?.note),
          }))
          .filter((item) => item.term && item.note)
          .slice(0, 3)
        : []
      const referenceLinks = Array.isArray(row.reference_links)
        ? row.reference_links
          .map((item) => ({
            label: String(item?.label || item?.href || '').trim(),
            href: String(item?.href || '').trim(),
            note: sanitizeManuscriptNarrative(item?.note),
          }))
          .filter((item) => item.href && !looksLikeLowValueResourceHost(item.href))
          .slice(0, 2)
        : []
      const adjacentBridge = sanitizeManuscriptNarrative(row.adjacent_bridge)
      const anchorExcerpt = String(row.anchor_excerpt || '').trim()
      const rowMeta = asRecord(rowRecord.meta)
      const inlineSlots = normalizeCombinedSlots(
        [
          ...toUnknownRows(rowRecord.slot_bindings),
          ...toUnknownRows(rowRecord.slots),
          ...toUnknownRows(rowRecord.inline_slots),
          ...toUnknownRows(rowMeta?.slot_bindings),
          ...toUnknownRows(rowMeta?.slots),
        ],
        {
          slotIdBase: `${String(row.segment_id || 'ms-segment').trim() || 'ms-segment'}-slot`,
          fallbackKind: resolveManuscriptSlotKind(row.segment_type || ''),
          targetIds,
          fullEvidenceTargetIds,
          anchorExcerpt,
        },
        extractSlotReferenceIds(teachingText),
      )
      const slotById = new Map<string, TeachingManuscriptSlotView>()
      inlineSlots.forEach((slot) => {
        const key = normalizeKeyToken(slot.slotId)
        if (!key) return
        if (!slotById.has(key)) {
          slotById.set(key, slot)
        }
      })
      const mergeSlotWithKnownBinding = (slot: TeachingManuscriptSlotView | null): TeachingManuscriptSlotView | null => {
        if (!slot) return null
        const known = slotById.get(normalizeKeyToken(slot.slotId))
        if (!known) return slot
        return {
          slotId: slot.slotId || known.slotId,
          slotKind: slot.slotKind || known.slotKind,
          title: slot.title || known.title,
          summary: slot.summary || known.summary,
          targetIds: slot.targetIds.length ? slot.targetIds : known.targetIds,
          fullEvidenceTargetIds: slot.fullEvidenceTargetIds.length ? slot.fullEvidenceTargetIds : known.fullEvidenceTargetIds,
          anchorExcerpt: slot.anchorExcerpt || known.anchorExcerpt,
        }
      }
      const displayFlowRows = [
        ...toUnknownRows(rowRecord.display_flow),
        ...toUnknownRows(rowRecord.manuscript_flow),
        ...toUnknownRows(rowRecord.flow),
        ...toUnknownRows(rowMeta?.display_flow),
      ]
      const displayFlowChunks: ManuscriptTextChunkView[] = []
      const displayFlowTextSeen = new Set<string>()
      const displayFlowSlotSeen = new Set<string>()
      displayFlowRows.forEach((flowRow, flowIndex) => {
        const flowRecord = asRecord(flowRow)
        if (!flowRecord) {
          const fallbackText = sanitizeManuscriptNarrative(flowRow)
          const compactText = normalizeKeyToken(fallbackText).replace(/[^\p{L}\p{N}]+/gu, '')
          if (
            fallbackText
            && !isInternalTargetToken(fallbackText)
            && (!compactText || !displayFlowTextSeen.has(compactText))
          ) {
            if (compactText) displayFlowTextSeen.add(compactText)
            displayFlowChunks.push({ kind: 'text', text: fallbackText })
          }
          return
        }
        const kindToken = normalizeKeyToken(
          flowRecord.kind
          || flowRecord.type
          || flowRecord.block_type
          || flowRecord.node_type
          || flowRecord.slot_type
          || '',
        )
        const flowText = sanitizeManuscriptNarrative(preferDisplayCopy(
          flowRecord.display_text,
          flowRecord.text || flowRecord.content || flowRecord.prose || flowRecord.teaching_text || '',
        ))
        const flowSlotId = String(
          flowRecord.slot_id
          || flowRecord.ref_id
          || flowRecord.binding_id
          || flowRecord.id
          || '',
        ).trim()
        const flowSlot = mergeSlotWithKnownBinding(
          normalizeManuscriptSlotBinding(flowRecord, {
            slotIdBase: `${String(row.segment_id || 'ms-segment').trim() || 'ms-segment'}-display-slot-${flowIndex + 1}`,
            fallbackKind: resolveManuscriptSlotKind(kindToken || flowSlotId || row.segment_type || ''),
            targetIds,
            fullEvidenceTargetIds,
            anchorExcerpt,
          }),
        ) || (flowSlotId ? slotById.get(normalizeKeyToken(flowSlotId)) || null : null)
        const isSlotFlow = Boolean(flowSlot)
          || kindToken.includes('slot')
          || kindToken === 'figure'
          || kindToken === 'body'
          || Boolean(flowSlotId)
        const isProseFlow = kindToken === 'prose'
          || kindToken === 'text'
          || kindToken === 'paragraph'
          || kindToken === 'narration'
          || kindToken === 'manuscript_copy'
        if (flowText && (!isSlotFlow || isProseFlow)) {
          const compactText = normalizeKeyToken(flowText).replace(/[^\p{L}\p{N}]+/gu, '')
          if (
            !isInternalTargetToken(flowText)
            && (!compactText || !displayFlowTextSeen.has(compactText))
          ) {
            if (compactText) displayFlowTextSeen.add(compactText)
            displayFlowChunks.push({ kind: 'text', text: flowText })
          }
        }
        if (flowSlot) {
          const slotKey = buildManuscriptSlotEvidenceKey(flowSlot, targetIds)
          if (!displayFlowSlotSeen.has(slotKey)) {
            displayFlowSlotSeen.add(slotKey)
            displayFlowChunks.push({ kind: 'slot', slot: flowSlot })
          }
        }
      })
      if (
        !teachingText
        && !title
        && !anchorExcerpt
        && !glossaryRows.length
        && !referenceLinks.length
        && !inlineSlots.length
        && !displayFlowChunks.length
      ) continue
      result.push({
        segmentId: String(row.segment_id || '').trim() || `ms-segment-${result.length + 1}`,
        segmentType: String(row.segment_type || '').trim(),
        title: title || `讲读段 ${result.length + 1}`,
        teachingText,
        displayFlowChunks,
        anchorExcerpt,
        targetIds,
        fullEvidenceTargetIds,
        inlineSlots,
        glossaryRows,
        adjacentBridge,
        referenceLinks,
      })
    }
    return result
  }, [manuscriptPayload])
  const manuscriptOpening = useMemo(() => {
    const opening = sanitizeManuscriptNarrative(manuscriptPayload?.opening)
    return opening
  }, [manuscriptPayload])
  const manuscriptHeroContinuity = useMemo(() => {
    if (!manuscriptSegments.length) {
      return {
        pathLine: '',
        leadAnchor: '',
        adjacentLine: '',
        resourceLine: '',
      }
    }
    const pathTitles = manuscriptSegments
      .map((item) => String(item.title || '').trim())
      .filter(Boolean)
      .slice(0, 3)
    const pathLine = pathTitles.length
      ? `讲读路径：${pathTitles.join(' -> ')}`
      : ''
    const leadAnchor = buildEvidenceAnchorQuote(
      manuscriptSegments
        .map((item) => item.anchorExcerpt)
        .find((item) => String(item || '').trim()) || '',
      120,
    )
    const adjacentLine = manuscriptSegments
      .map((item) => String(item.adjacentBridge || '').trim())
      .find(Boolean) || ''
    const resourceLabels = dedupeTrimmedText(
      manuscriptSegments
        .flatMap((item) => item.referenceLinks.map((link) => String(link.label || '').trim() || String(link.href || '').trim()))
        .filter(Boolean),
    ).slice(0, 2)
    const resourceLine = resourceLabels.length
      ? `外部延伸：${resourceLabels.join('、')}`
      : ''
    return { pathLine, leadAnchor, adjacentLine, resourceLine }
  }, [manuscriptSegments])
  const hasPrimaryContractContent = useMemo(() => {
    const heroSignal = Boolean(
      String(preferDisplayCopy(hero?.display_summary, hero?.summary)).trim()
      || String(preferDisplayCopy(hero?.display_title, hero?.title)).trim(),
    )
    return Boolean(
      heroSignal
      || mainNarrativeSections.length > 0
      || guidedBeats.length > 0
      || resourceModules.length > 0
      || interactionModules.length > 0
      || widgetBlocks.length > 0,
    )
  }, [
    guidedBeats.length,
    hero?.display_summary,
    hero?.display_title,
    hero?.summary,
    hero?.title,
    interactionModules.length,
    mainNarrativeSections.length,
    resourceModules.length,
    widgetBlocks.length,
  ])
  const isManuscriptPreferred = !seedMode && manuscriptSegments.length > 0 && !hasPrimaryContractContent
  const heroContextCards = useMemo(() => {
    if (seedMode) return contextCards
    if (isManuscriptPreferred) return []
    if (layoutVariant === 'guided_story_stack') return contextCards
    return contextCards.slice(0, 1)
  }, [contextCards, isManuscriptPreferred, layoutVariant, seedMode])
  const sidebarContextCards = useMemo(() => {
    if (seedMode) return []
    if (isManuscriptPreferred) return []
    if (layoutVariant === 'guided_story_stack') return []
    if (layoutVariant === 'explainer_first') return contextCards
    return contextCards.slice(1)
  }, [contextCards, isManuscriptPreferred, layoutVariant, seedMode])
  const effectiveSidebarSections = isManuscriptPreferred ? [] : sidebarNarrativeSections
  const hasSidebar = effectiveSidebarSections.length > 0

  const resolveSectionBlockRefs = (
    section: ReaderExperiencePlan['main_sections'][number],
    blockType: 'resource_module' | 'interaction_module' | 'widget',
  ) => {
    const blockRefs = (section.blocks || [])
      .filter((block) => String(block.block_type || '').trim() === blockType)
      .slice()
      .sort((left, right) => {
        const leftPriority = Number(left.priority || 0)
        const rightPriority = Number(right.priority || 0)
        if (leftPriority !== rightPriority) return leftPriority - rightPriority
        return String(left.block_id || '').localeCompare(String(right.block_id || ''))
      })
      .map((block) => String(block.ref_id || '').trim())
      .filter(Boolean)
    if (blockRefs.length) return blockRefs
    if (blockType === 'resource_module') return section.resource_module_ids || []
    if (blockType === 'interaction_module') return section.interaction_module_ids || []
    return section.widget_ids || []
  }

  const resolveSectionBlocks = (section: ReaderExperiencePlan['main_sections'][number]) =>
    (section.blocks || []).slice().sort((left, right) => {
      const leftPriority = Number(left.priority || 0)
      const rightPriority = Number(right.priority || 0)
      if (leftPriority !== rightPriority) return leftPriority - rightPriority
      return String(left.block_id || '').localeCompare(String(right.block_id || ''))
    })

  const resolveSectionResourceModules = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'resource_module')
      .map((moduleId) => resourceModuleLookup.get(String(moduleId || '').trim()))
      .filter((module): module is NonNullable<typeof module> => Boolean(module))

  const resolveSectionInteractionModules = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'interaction_module')
      .map((moduleId) => interactionModuleLookup.get(String(moduleId || '').trim()))
      .filter((module): module is NonNullable<typeof module> => Boolean(module))

  const resolveSectionWidgets = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'widget')
      .map((widgetId) => widgetLookup.get(String(widgetId || '').trim()))
      .filter((widget): widget is NonNullable<typeof widget> => Boolean(widget))

  const resolveReadingFlowNodes = (section: ReaderExperiencePlan['main_sections'][number]) => {
    const targetIds = (section.target_ids || []).map((item) => String(item || '').trim()).filter(Boolean)
    if (!targetIds.length) return readingFlowNodes
    const matched = readingFlowNodes.filter((node) => {
      const nodeId = String(node.id || '').trim()
      return targetIds.some((targetId) => targetId === nodeId || targetId.endsWith(`:${nodeId}`))
    })
    return matched.length ? matched : readingFlowNodes
  }

  const hasUsableResourceModule = (module: ReaderGenerativeResourceModule) => {
    const moduleType = String(module.module_type || '').trim()
    const title = preferDisplayCopy(module.display_title, module.title)
    const summary = preferDisplayCopy(module.display_summary, module.summary)
    const links = Array.isArray(module.links)
      ? module.links.map((item) => item && typeof item === 'object' ? item as Record<string, unknown> : {}).filter((item) => {
        const href = String(item.href || item.url || '').trim()
        const label = String(item.label || item.title || '').trim()
        return Boolean(href || label)
      })
      : []
    const usableLinks = links.filter((item) => isReaderWorthyResourceLink(item.href || item.url))
    if (moduleType === 'FigureSourceCard' || moduleType === 'RelatedResourceCard') {
      return usableLinks.length > 0
        || Boolean(summary && !isWeakPlaceholderText(summary) && !isGenericSupportSummary(summary) && moduleType !== 'FigureSourceCard')
    }
    return Boolean((title && !isWeakPlaceholderText(title)) || (summary && !isWeakPlaceholderText(summary)) || usableLinks.length)
  }

  const hasUsableInteractionModule = (module: ReaderGenerativeInteractionModule) => {
    const moduleType = String(module.module_type || '').trim()
    if (moduleType === 'GlossaryPanel') {
      const terms = Array.isArray(module.props?.terms) ? module.props.terms : []
      return terms.some((row) => {
        if (!row || typeof row !== 'object') return false
        const item = row as Record<string, unknown>
        const term = String(item.term || '').trim()
        const definition = String(item.definition || '').trim()
        return Boolean(term || definition)
      })
    }
    if (moduleType === 'QuestionStarterPanel') {
      const questions = Array.isArray(module.props?.questions) ? module.props.questions : []
      return questions.some((item) => String(item || '').trim())
    }
    const qaPairs = Array.isArray(module.props?.qa_pairs) ? module.props.qa_pairs : []
    return qaPairs.some((row) => {
      if (!row || typeof row !== 'object') return false
      const item = row as Record<string, unknown>
      const question = String(item.question || '').trim()
      const answer = String(item.answer || '').trim()
      return Boolean(question || answer)
    })
  }

  const hasUsableWidget = (widget: ReaderGenerativeJsWidgetPlan) => {
    const panels = Array.isArray(widget.props?.panels) ? widget.props.panels : []
    return panels.some((row) => {
      if (!row || typeof row !== 'object') return false
      const item = row as Record<string, unknown>
      const label = preferDisplayCopy(item.display_label, item.label || '')
      const panelSummary = preferDisplayCopy(item.display_summary, item.summary || '')
      return Boolean((panelSummary && !isWeakPlaceholderText(panelSummary)) || (label && !isWeakPlaceholderText(label) && !/^(panel|tab)\s*\d+$/i.test(label)))
    })
  }

  const resolveTargetNodes = (targetRows: unknown[]) => {
    const targetIds = dedupeTrimmedText(targetRows)
    if (!targetIds.length) return []
    const matched = bodyFlowNodes.filter((node) => {
      const nodeId = String(node.id || '').trim()
      return targetIds.some((targetId) => targetId === nodeId || targetId.endsWith(`:${nodeId}`))
    })
    return matched
  }

  const resolveBeatNodes = (beat: ReaderExperienceGuidedBeat) => resolveTargetNodes(beat.target_ids || [])

  const renderResourceModule = (module: ReaderGenerativeResourceModule, block: ReaderExperienceBlockRef | null) => {
    const sanitizedModule: ReaderGenerativeResourceModule = {
      ...module,
      links: Array.isArray(module.links)
        ? module.links.filter((item) => {
          const href = String(item?.href || item?.url || '').trim()
          return isReaderWorthyResourceLink(href)
        })
        : [],
    }
    const definition = getResourceModuleDefinition(sanitizedModule.module_type)
    return definition.render({
      module: sanitizedModule,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderInteractionModule = (module: ReaderGenerativeInteractionModule, block: ReaderExperienceBlockRef | null) => {
    const definition = getInteractionModuleDefinition(module.module_type)
    return definition.render({
      module,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderWidget = (widget: ReaderGenerativeJsWidgetPlan, block: ReaderExperienceBlockRef | null) => {
    const definition = getWidgetDefinition(widget.widget_type)
    return definition.render({
      widget,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderBlockState = (
    block: ReaderExperienceBlockRef | null,
    content: ReactNode,
    _emptyLabel: string,
  ) => {
    const state = String(block?.state || 'ready').trim().toLowerCase()
    const fallbackPolicy = String(block?.fallback_policy || 'omit').trim().toLowerCase()
    if (state === 'loading') {
      return (
        <Card size="small" className="reader-experience-page__module-card reader-experience-page__module-card--soft">
          <div className="reader-experience-page__loading"><Spin /></div>
        </Card>
      )
    }
    if (state === 'error') {
      return null
    }
    if (state === 'empty') {
      if (fallbackPolicy === 'omit') return null
      return null
    }
    if (state === 'partial') {
      return (
        <div className="reader-experience-page__block-state-shell reader-experience-page__block-state-shell--partial">
          <div className="reader-experience-page__block-state-banner">
            <Tag color="gold">部分生成</Tag>
          </div>
          {content}
        </div>
      )
    }
    return content
  }

  const renderEvidenceCollapse = (
    key: string,
    summaryCopy: string | null,
    content: ReactNode,
    badgeLabel: string = '原页',
  ) => (
    <Collapse
      ghost
      className="reader-experience-page__qa-collapse"
      items={[
        {
          key,
          label: (
            <Space wrap size={8}>
              <Text className="reader-experience-page__eyebrow">展开原页细节</Text>
              <Tag>{badgeLabel}</Tag>
              {summaryCopy ? <Text type="secondary">{summaryCopy}</Text> : null}
            </Space>
          ),
          children: (
            <div className="reader-composed-surface reader-experience-page__surface">
              {content}
            </div>
          ),
        },
      ]}
    />
  )

  const renderBeatBlocks = (beat: ReaderExperienceGuidedBeat) => {
    const beatType = String(beat.beat_type || '').trim()
    const blocks = (beat.block_stack || []).slice().sort((left, right) => {
      const leftPriority = Number(left.priority || 0)
      const rightPriority = Number(right.priority || 0)
      if (leftPriority !== rightPriority) return leftPriority - rightPriority
      return String(left.block_id || '').localeCompare(String(right.block_id || ''))
    })
    if (!blocks.length) return null
    const keepFigureExplainPanel = !(
      beatType === 'figure_walkthrough'
      && blocks.some((block) => String(block.block_type || '').trim() === 'widget')
    )
    const renderedBlocks = blocks.map((block) => {
      const refId = String(block.ref_id || '').trim()
      if (block.block_type === 'resource_module') {
        const module = resourceModuleLookup.get(refId)
        if (!module) return null
        if (!keepFigureExplainPanel && String(module.module_type || '').trim() === 'FigureExplainPanel') return null
        if (!hasUsableResourceModule(module)) return null
        return renderBlockState(block, renderResourceModule(module, block), '资源模块暂未准备好')
      }
      if (block.block_type === 'interaction_module') {
        const module = interactionModuleLookup.get(refId)
        if (!module) return null
        if (!hasUsableInteractionModule(module)) return null
        return renderBlockState(block, renderInteractionModule(module, block), '解释模块暂未准备好')
      }
      if (block.block_type === 'widget') {
        const widget = widgetLookup.get(refId)
        if (!widget) return null
        if (!hasUsableWidget(widget)) return null
        return renderBlockState(block, renderWidget(widget, block), '交互控件暂未准备好')
      }
      return null
    }).filter(Boolean)
    if (!renderedBlocks.length) return null
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {renderedBlocks}
      </Space>
    )
  }

  const renderBeatEnrichment = (
    beat: ReaderExperienceGuidedBeat,
    variant: 'inline' | 'side' = 'inline',
    primarySummary: string = '',
  ) => {
    const packet = beatPacketLookup.get(String(beat.beat_id || '').trim())
    const links = (packet && Array.isArray(packet.public_links)
      ? packet.public_links as Array<Record<string, unknown>>
      : []
    )
      .map((item) => ({
        label: String(item.label || item.href || '').trim(),
        href: String(item.href || '').trim(),
      }))
      .filter((item) => (item.label || item.href) && !looksLikeLowValueResourceHost(item.href))
      .slice(0, 2)
    const summary = sanitizeReaderFacingNarrative(packet?.summary)
    const supportPoints = dedupeTrimmedText(
      packet && Array.isArray(packet.supporting_points)
        ? packet.supporting_points
        : [],
    ).map((item) => sanitizeReaderFacingNarrative(item)).filter(Boolean).slice(0, 3)
    const readerFacingNotes = dedupeTrimmedText(
      packet && Array.isArray(packet.reader_facing_notes)
        ? packet.reader_facing_notes
        : [],
    ).map((item) => sanitizeReaderFacingNarrative(item)).filter(Boolean).slice(0, 2)
    const lead = [summary, supportPoints[0], readerFacingNotes[0]]
      .map((item) => String(item || '').trim())
      .find((item) => item) || ''
    const effectiveLead = (
      lead
      && lead !== String(primarySummary || '').trim()
      && !isLowSignalNarrativeCopy(lead)
    ) ? lead : ''
    const concisePoints = dedupeTrimmedText([
      ...supportPoints.filter((point) => point !== lead && !isLowSignalNarrativeCopy(point)),
      ...readerFacingNotes.filter((note) => note !== lead && !isLowSignalNarrativeCopy(note)),
    ]).slice(0, 2)

    if (!effectiveLead && !concisePoints.length && !links.length) return null
    const isInline = variant === 'inline'

    return (
      <div
        style={isInline
          ? {
            marginTop: 10,
            paddingLeft: 4,
          }
          : {
            marginTop: 8,
            paddingLeft: 4,
          }}
      >
        {effectiveLead ? (
          <Paragraph className="reader-experience-page__summary" style={{ marginBottom: concisePoints.length || links.length ? 8 : 0 }}>
            {effectiveLead}
          </Paragraph>
        ) : null}
        {concisePoints.length ? (
          <ul style={{ margin: 0, paddingInlineStart: 20, color: '#4c5f78' }}>
            {concisePoints.map((point, index) => (
              <li key={`${beat.beat_id}-concise-point-${index}`}>
                <Text type="secondary">{point}</Text>
              </li>
            ))}
          </ul>
        ) : null}
        {links.length ? (
          <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
            参考：
            {links.map((item, index) => (
              item.href ? (
                <a key={`${beat.beat_id}-compact-link-${index}`} href={item.href} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
                  {item.label || item.href}
                </a>
              ) : (
                <Text key={`${beat.beat_id}-compact-link-${index}`} type="secondary" style={{ marginLeft: 8 }}>
                  {item.label}
                </Text>
              )
            ))}
          </Paragraph>
        ) : null}
      </div>
    )
  }

  const resolveBeatReaderLabel = (beat: ReaderExperienceGuidedBeat) => {
    const beatType = String(beat.beat_type || '').trim()
    const beatTitle = preferDisplayCopy(beat.display_title, beat.title)
    return beatTitle || (beatType === 'figure_walkthrough'
      ? '先看图解'
      : beatType === 'body_segment'
        ? '读正文'
        : beatType === 'guide_intro'
          ? '先抓核心'
        : '停下来整理一下')
  }

  const resolveSectionNarrativeLead = (...candidates: unknown[]) => {
    for (const candidate of candidates) {
      const value = sanitizeReaderFacingNarrative(candidate)
      if (value) return value
    }
    return ''
  }

  const buildCompactReferenceTeasers = (nodes: ReaderComponentNode[], maxCount: number = 2): string[] => {
    const result: string[] = []
    const seen = new Set<string>()
    for (const node of nodes) {
      const previewNode = buildEvidencePreviewNode(node).node
      const rawLead = extractPreviewLeadText(previewNode)
      let teaser = clampEvidenceSentence(rawLead, isEnglishHeavyReaderCopy(rawLead) ? 88 : 120)
      if (!teaser || isInternalTargetToken(teaser)) continue
      if (isEnglishHeavyReaderCopy(teaser) && teaser.length > 96) {
        teaser = `${teaser.slice(0, 95).trim()}…`
      }
      const dedupeKey = normalizeKeyToken(teaser).replace(/[^\p{L}\p{N}]+/gu, '')
      if (!dedupeKey || seen.has(dedupeKey)) continue
      seen.add(dedupeKey)
      result.push(teaser)
      if (result.length >= maxCount) break
    }
    return result
  }

  const renderInlineSourceEvidence = (
    key: string,
    nodes: ReaderComponentNode[],
    options: {
      title: string
      evidenceLabel?: string
      summary?: string
      maxHeight?: number
      fullEvidence?: ReactNode
      fullEvidenceLabel?: string
      anchorQuote?: string
      mode?: 'surface' | 'quote'
      compactSecondary?: boolean
    },
  ) => {
    const hasSurface = options.mode !== 'quote' && nodes.length > 0
    if (!hasSurface && !options.anchorQuote) return null
    const isFigureEvidence = nodes.some((node) => String(node.type || '').trim() === 'FigurePanel')
    const summaryLabel = String(options.evidenceLabel || (isFigureEvidence ? '图示摘录' : '原页摘录')).trim()
    const summaryTitle = String(options.title || '').trim()
    const showSummaryTitle = Boolean(summaryTitle) && normalizeKeyToken(summaryTitle) !== normalizeKeyToken(summaryLabel)
    const shouldRenderInlinePreview = hasSurface && (options.mode === 'surface' || isFigureEvidence)
    const previewSurface = hasSurface ? (
      <div
        className="reader-composed-surface reader-experience-page__surface"
        style={{
          maxHeight: Math.min(options.maxHeight || (options.compactSecondary ? 220 : 300), options.compactSecondary ? 220 : 300),
          overflow: 'hidden',
          position: 'relative',
          paddingRight: 2,
        }}
      >
        {renderReaderComponentTree(nodes, renderCtx)}
        <div
          aria-hidden="true"
          style={{
            position: 'absolute',
            insetInline: 0,
            bottom: 0,
            height: 40,
            background: 'linear-gradient(180deg, rgba(255, 255, 255, 0) 0%, rgba(255, 255, 255, 0.98) 100%)',
            pointerEvents: 'none',
          }}
        />
      </div>
    ) : null
    return (
      <div
        key={key}
        className="reader-experience-page__source-control"
      >
        {shouldRenderInlinePreview ? (
          <Space direction="vertical" size={8} style={{ width: '100%', marginBottom: 8 }}>
            <Space wrap size={8}>
              <Text className="reader-experience-page__eyebrow">{summaryLabel}</Text>
              {showSummaryTitle ? <Text type="secondary">{summaryTitle}</Text> : null}
            </Space>
            {previewSurface}
          </Space>
        ) : null}
        <details className="reader-experience-page__source-toggle">
          <summary>
            <Text className="reader-experience-page__eyebrow">{shouldRenderInlinePreview ? '原页细节' : summaryLabel}</Text>
            {showSummaryTitle ? (
              <Text type="secondary">{shouldRenderInlinePreview ? `查看${summaryTitle}` : summaryTitle}</Text>
            ) : null}
          </summary>
          <div className="reader-experience-page__source-toggle-body">
            {options.summary ? (
              <Paragraph type="secondary" style={{ marginBottom: 8 }}>
                {options.summary}
              </Paragraph>
            ) : null}
            {options.anchorQuote ? (
              <blockquote className="reader-experience-page__source-quote">
                {options.anchorQuote}
              </blockquote>
            ) : null}
            {!shouldRenderInlinePreview && hasSurface ? previewSurface : null}
            {options.fullEvidence ? (
              <div style={{ marginTop: 8 }}>
                {renderEvidenceCollapse(
                  `${key}-full-evidence`,
                  options.fullEvidenceLabel || '完整原页内容',
                  options.fullEvidence,
                  '完整原页',
                )}
              </div>
            ) : null}
          </div>
        </details>
      </div>
    )
  }

  const buildSegmentBridgeCopy = (beat: ReaderExperienceGuidedBeat, index: number) => {
    const fragments = dedupeTrimmedText([
      sanitizeReaderFacingNarrative(beat.continuity_note),
      index === 0 && adjacentContinuityBridge.length
        ? `${adjacentContinuityBridge[0].relationLabel}（${adjacentContinuityBridge[0].pageLabel}）补充：${adjacentContinuityBridge[0].summary}`
        : '',
    ])
    return fragments.filter((item) => !isLowSignalContinuityCopy(item))
  }

  const humanizeBridgeCopy = (raw: string) => {
    const text = String(raw || '').trim()
    if (!text) return ''
    const adjacentMatch = text.match(/^邻页[^：]*补充：(.*)$/)
    if (adjacentMatch) {
      const body = String(adjacentMatch[1] || '').trim()
      if (!body) return ''
      return `和前后页对照看，这里可以补一句：${body}`
    }
    return text
  }

  const buildSegmentEvidence = (
    nodes: ReaderComponentNode[],
    variant: 'figure' | 'body',
  ) => {
    const filteredNodes = variant === 'body'
      ? nodes.filter((node) => {
        const nodeType = String(node.type || '').trim()
        const nodeId = String(node.id || '').trim()
        if (nodeType === 'FigurePanel') return false
        if (focusNode && nodeId && nodeId === String(focusNode.id || '').trim()) return false
        return true
      })
      : nodes
    const baseNodes = (() => {
      const candidateNodes = filteredNodes.length ? filteredNodes : nodes
      if (variant !== 'body' || candidateNodes.length <= 1) return candidateNodes
      const firstNode = candidateNodes[0]
      const firstText = String(
        ((firstNode.props && typeof firstNode.props === 'object')
          ? ((firstNode.props as Record<string, unknown>).text || '')
          : '') || '',
      ).trim()
      const looksFragmentary = firstNode.type === 'ParagraphProse'
        && isFragmentaryBodyLeadText(firstText)
      if (looksFragmentary) return candidateNodes.slice(1)
      return candidateNodes
    })()
    const previewRows = baseNodes
      .slice(0, variant === 'figure' ? 1 : 3)
      .map((node) => buildEvidencePreviewNode(node))
    const inlineNodes = previewRows.map((row) => row.node)
    const normalizedInlineNodes = (() => {
      if (variant !== 'body' || inlineNodes.length <= 1) return inlineNodes
      const leadText = extractPreviewLeadText(inlineNodes[0])
      const looksFragmentary = isFragmentaryBodyLeadText(leadText)
      if (!looksFragmentary) return inlineNodes
      return inlineNodes.slice(1)
    })()
    const truncated = previewRows.some((row) => row.truncated)
    const omitted = filteredNodes.length !== nodes.length
      || baseNodes.length > normalizedInlineNodes.length
      || normalizedInlineNodes.length !== inlineNodes.length
      || truncated
    const anchorQuote = variant === 'body'
      ? buildEvidenceAnchorQuote(extractPreviewLeadText(normalizedInlineNodes[0] || baseNodes[0] || nodes[0] || { type: '', props: {} }), 140)
      : ''
    return {
      inlineNodes: normalizedInlineNodes,
      anchorQuote,
      fullEvidence: omitted ? renderReaderComponentTree(baseNodes, renderCtx) : null,
    }
  }

  const renderSupportBeat = (beat: ReaderExperienceGuidedBeat, parentBeatId: string) => {
    const beatType = String(beat.beat_type || '').trim()
    const beatSummary = sanitizeReaderFacingNarrative(preferDisplayCopy(beat.display_summary, beat.summary))
    const beatBlocks = seedMode ? null : renderBeatBlocks(beat)
    const inlineBeatEnrichment = seedMode ? null : renderBeatEnrichment(beat, 'inline', beatSummary)
    const supportLead = beatType === 'why_it_matters' ? '延伸参考：' : '补充说明：'
    const isEmptySupportFallback = supportLead === '延伸参考：'
      && !inlineBeatEnrichment
      && !beatBlocks

    if ((!beatSummary && !inlineBeatEnrichment && !beatBlocks) || isEmptySupportFallback) return null

    return (
      <div
        key={`${parentBeatId}-${beat.beat_id}`}
        style={{
          marginTop: 10,
          paddingTop: 2,
        }}
      >
        {beatSummary ? (
          <Paragraph className="reader-experience-page__summary" style={{ marginBottom: inlineBeatEnrichment || beatBlocks ? 8 : 0 }}>
            <Text type="secondary">{supportLead}</Text>{beatSummary}
          </Paragraph>
        ) : null}
        {inlineBeatEnrichment}
        {beatBlocks ? (
          <div style={{ marginTop: inlineBeatEnrichment ? 12 : 0 }}>
            {beatBlocks}
          </div>
        ) : null}
      </div>
    )
  }

  const renderStandaloneBeat = (beat: ReaderExperienceGuidedBeat) => {
    const beatSummary = sanitizeReaderFacingNarrative(preferDisplayCopy(beat.display_summary, beat.summary))
    const heroSummary = sanitizeReaderFacingNarrative(preferDisplayCopy(hero?.display_summary, hero?.summary))
    const inlineBeatEnrichment = seedMode ? null : renderBeatEnrichment(beat, 'inline', beatSummary)
    const beatBlocks = seedMode ? null : renderBeatBlocks(beat)
    const beatReaderLabel = resolveBeatReaderLabel(beat)
    const beatType = String(beat.beat_type || '').trim()

    if (beatType === 'guide_intro') {
      if (seedMode) return null
      if (beatSummary && beatSummary === heroSummary) return null
      if (isLowSignalNarrativeCopy(beatSummary)) return null
    }
    if (seedMode && !PRIMARY_GUIDED_BEAT_TYPES.has(beatType)) return null
    if (!beatSummary && !inlineBeatEnrichment && !beatBlocks) return null

    return (
      <section
        key={beat.beat_id}
        className="reader-experience-page__guided-beat"
        style={{
          padding: '8px 0 2px',
          borderTop: '1px solid rgba(140, 152, 168, 0.18)',
        }}
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Space wrap size={8}>
            <Tag color="gold">阅读注释</Tag>
            <Text className="reader-experience-page__eyebrow">{beatReaderLabel}</Text>
          </Space>
          {beatSummary ? <Paragraph className="reader-experience-page__summary">{beatSummary}</Paragraph> : null}
          {inlineBeatEnrichment}
          {beatBlocks}
        </Space>
      </section>
    )
  }

  const renderGuidedSegment = (segment: GuidedReadingSegment, index: number) => {
    const beat = segment.primary
    const beatType = String(beat.beat_type || '').trim()
    const beatSummary = sanitizeReaderFacingNarrative(preferDisplayCopy(beat.display_summary, beat.summary))
    const beatNodes = resolveBeatNodes(beat)
    const beatBlocks = seedMode ? null : renderBeatBlocks(beat)
    const inlineBeatEnrichment = seedMode ? null : renderBeatEnrichment(beat, 'inline', beatSummary)
    const beatReaderLabel = resolveBeatReaderLabel(beat)
    const segmentNodes = beatType === 'figure_walkthrough'
      ? (focusNode ? [focusNode] : beatNodes)
      : beatNodes
    const evidence = buildSegmentEvidence(
      segmentNodes,
      beatType === 'figure_walkthrough' ? 'figure' : 'body',
    )
    const bridgeCopy = buildSegmentBridgeCopy(beat, index)
    const hasSourceEvidence = evidence.inlineNodes.length > 0
    const hasSupportContent = segment.support.length > 0 || beatBlocks

    if (!beatSummary && !inlineBeatEnrichment && !hasSourceEvidence && !hasSupportContent) return null

    return (
      <section
        key={beat.beat_id}
        className="reader-experience-page__guided-beat"
        style={{
          padding: index === 0 ? '4px 0 12px' : '18px 0 12px',
          borderTop: index === 0 ? 'none' : '1px solid rgba(140, 152, 168, 0.18)',
        }}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text className="reader-experience-page__eyebrow" style={{ color: '#6a7f99' }}>{beatReaderLabel}</Text>
          {beatSummary ? (
            <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">
              {beatSummary}
            </Paragraph>
          ) : null}
          {bridgeCopy.length ? (
            <div style={{ marginTop: -2 }}>
              {bridgeCopy.map((item, itemIndex) => (
                <Paragraph key={`${beat.beat_id}-bridge-${itemIndex}`} type="secondary" style={{ marginBottom: itemIndex === bridgeCopy.length - 1 ? 0 : 6 }}>
                  {humanizeBridgeCopy(item)}
                </Paragraph>
              ))}
            </div>
          ) : null}
          {composeLoading && !hasComposePayload ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : hasSourceEvidence ? (
            renderInlineSourceEvidence(`${beat.beat_id}-source`, evidence.inlineNodes, {
              title: beatType === 'figure_walkthrough' ? '本段图示与图注' : '本段原文摘录',
              maxHeight: beatType === 'figure_walkthrough' ? 420 : 260,
              anchorQuote: evidence.anchorQuote,
              mode: beatType === 'figure_walkthrough' ? 'surface' : 'quote',
              fullEvidence: evidence.fullEvidence,
              fullEvidenceLabel: beatType === 'figure_walkthrough' ? '展开完整图示与图注' : '展开本段完整原文',
            })
          ) : null}
          {inlineBeatEnrichment ? (
            <div style={{ marginTop: 2 }}>
              {inlineBeatEnrichment}
            </div>
          ) : null}
          {hasSupportContent ? (
            <div style={{ marginTop: 2 }}>
              {beatBlocks ? (
                <div style={{ marginTop: 4 }}>
                  {beatBlocks}
                </div>
              ) : null}
              {segment.support.map((supportBeat) => renderSupportBeat(supportBeat, beat.beat_id))}
            </div>
          ) : null}
        </Space>
      </section>
    )
  }

  const resolveManuscriptSlotEvidence = (
    slot: TeachingManuscriptSlotView,
    segment: TeachingManuscriptSegmentView,
  ) => {
    const isFigureNode = (node: ReaderComponentNode) => String(node.type || '').trim() === 'FigurePanel'
    const targetIds = dedupeTrimmedText([
      ...slot.targetIds,
      ...slot.fullEvidenceTargetIds,
      ...segment.targetIds,
    ])
    const targetNodes = resolveTargetNodes(targetIds)
    const figureTargetNodes = targetNodes.filter((node) => isFigureNode(node))
    const bodyTargetNodes = targetNodes.filter((node) => !isFigureNode(node))
    const fallbackFigureNodes = (() => {
      if (figureTargetNodes.length) return figureTargetNodes.slice(0, 1)
      if (focusNode && isFigureNode(focusNode)) return [focusNode]
      const bodyFigure = bodyFlowNodes.find((node) => isFigureNode(node))
      if (bodyFigure) return [bodyFigure]
      const readingFigure = readingFlowNodes.find((node) => isFigureNode(node))
      return readingFigure ? [readingFigure] : []
    })()
    const fallbackBodyNodes = (() => {
      if (bodyTargetNodes.length) return bodyTargetNodes
      const bodyFlowBodyNodes = bodyFlowNodes.filter((node) => !isFigureNode(node))
      if (bodyFlowBodyNodes.length) return bodyFlowBodyNodes
      return readingFlowNodes.filter((node) => !isFigureNode(node))
    })()
    const segmentNodes = slot.slotKind === 'figure'
      ? fallbackFigureNodes
      : fallbackBodyNodes
    const evidence = buildSegmentEvidence(segmentNodes, slot.slotKind)
    const slotAnchorQuote = buildEvidenceAnchorQuote(slot.anchorExcerpt, 160)
    return {
      evidence,
      slotAnchorQuote: slot.slotKind === 'body'
        ? (slotAnchorQuote || evidence.anchorQuote)
        : slotAnchorQuote,
    }
  }

  const renderManuscriptInlineSlot = (
    slot: TeachingManuscriptSlotView,
    segment: TeachingManuscriptSegmentView,
    key: string,
  ) => {
    const { evidence, slotAnchorQuote } = resolveManuscriptSlotEvidence(slot, segment)
    const hasSourceEvidence = evidence.inlineNodes.length > 0
    const fallbackQuote = slotAnchorQuote || buildEvidenceAnchorQuote(segment.anchorExcerpt, 140)
    const slotLead = [slot.summary, slot.title].map((item) => String(item || '').trim()).find(Boolean) || ''
    if (!hasSourceEvidence && !fallbackQuote && !slotLead) return null
    return (
      <div key={key}>
        {slotLead ? (
          <Paragraph type="secondary" className="reader-experience-page__manuscript-inline-slot-summary">
            {slotLead}
          </Paragraph>
        ) : null}
        {renderInlineSourceEvidence(`${key}-source`, evidence.inlineNodes, {
          title: slot.title || (slot.slotKind === 'figure' ? '本段图示与图注' : '本段正文摘录'),
          evidenceLabel: '原页参考',
          maxHeight: slot.slotKind === 'figure' ? 340 : 220,
          fullEvidence: evidence.fullEvidence,
          fullEvidenceLabel: '展开完整证据',
          mode: slot.slotKind === 'figure' ? 'surface' : 'quote',
          anchorQuote: fallbackQuote,
          compactSecondary: true,
        })}
      </div>
    )
  }

  const renderTeachingManuscriptSegment = (
    segment: TeachingManuscriptSegmentView,
    renderState?: ManuscriptRenderState,
  ) => {
    const declaredSlots = segment.inlineSlots.map((slot, index) => ({
      ...slot,
      slotId: slot.slotId || `${segment.segmentId}-slot-${index + 1}`,
      targetIds: slot.targetIds.length ? slot.targetIds : segment.targetIds,
      fullEvidenceTargetIds: slot.fullEvidenceTargetIds.length
        ? slot.fullEvidenceTargetIds
        : (segment.fullEvidenceTargetIds.length ? segment.fullEvidenceTargetIds : slot.targetIds),
      anchorExcerpt: slot.anchorExcerpt || segment.anchorExcerpt,
    }))

    const displayFlowSlots = segment.displayFlowChunks
      .filter((chunk): chunk is { kind: 'slot'; slot: TeachingManuscriptSlotView } => chunk.kind === 'slot')
      .map((chunk, index) => ({
        ...chunk.slot,
        slotId: chunk.slot.slotId || `${segment.segmentId}-display-slot-${index + 1}`,
        targetIds: chunk.slot.targetIds.length ? chunk.slot.targetIds : segment.targetIds,
        fullEvidenceTargetIds: chunk.slot.fullEvidenceTargetIds.length
          ? chunk.slot.fullEvidenceTargetIds
          : (segment.fullEvidenceTargetIds.length ? segment.fullEvidenceTargetIds : chunk.slot.targetIds),
        anchorExcerpt: chunk.slot.anchorExcerpt || segment.anchorExcerpt,
      }))

    const slotSeen = new Set<string>()
    const slotPool = [...declaredSlots, ...displayFlowSlots].filter((slot) => {
      const key = buildManuscriptSlotEvidenceKey(slot, segment.targetIds)
      if (slotSeen.has(key)) return false
      slotSeen.add(key)
      return true
    })

    const tokenizedTeachingText = tokenizeManuscriptCopyWithSlots(segment.teachingText, [])
    const teachingTextChunks = tokenizedTeachingText.chunks
      .filter((chunk): chunk is { kind: 'text'; text: string } => chunk.kind === 'text')
      .map((chunk) => ({ kind: 'text' as const, text: String(chunk.text || '').trim() }))
      .filter((chunk) => chunk.text && !isInternalTargetToken(chunk.text))
    const displayFlowTextChunks = segment.displayFlowChunks
      .filter((chunk): chunk is { kind: 'text'; text: string } => chunk.kind === 'text')
      .map((chunk) => ({ kind: 'text' as const, text: String(chunk.text || '').trim() }))
      .filter((chunk) => chunk.text && !isInternalTargetToken(chunk.text))

    const narrativeSeedChunks = teachingTextChunks.length ? teachingTextChunks : displayFlowTextChunks
    const localNarrativeSeen = new Set<string>()
    const narrativeChunks = narrativeSeedChunks.filter((chunk) => {
      const compactKey = normalizeKeyToken(chunk.text).replace(/[^\p{L}\p{N}]+/gu, '')
      const shouldDedupe = Boolean(compactKey) && (chunk.text.length >= 96 || isEnglishHeavyReaderCopy(chunk.text))
      if (!shouldDedupe) return true
      if (localNarrativeSeen.has(compactKey)) return false
      localNarrativeSeen.add(compactKey)
      if (!renderState) return true
      if (renderState.seenLongNarrativeKeys.has(compactKey)) return false
      renderState.seenLongNarrativeKeys.add(compactKey)
      return true
    })

    const slotCandidateSeen = new Set<string>()
    const slotCandidates: TeachingManuscriptSlotView[] = []
    const pushSlot = (slot: TeachingManuscriptSlotView) => {
      const key = buildManuscriptSlotEvidenceKey(slot, segment.targetIds)
      if (slotCandidateSeen.has(key)) return
      slotCandidateSeen.add(key)
      slotCandidates.push(slot)
    }
    slotPool.forEach((slot) => pushSlot(slot))

    const freshSlots = slotCandidates.filter((slot) => {
      if (!renderState) return true
      const key = buildManuscriptSlotEvidenceKey(slot, segment.targetIds)
      return !renderState.seenSlotEvidenceKeys.has(key)
    })
    const primarySlot = freshSlots[0] || null
    const secondarySlots = freshSlots.slice(1)
    if (primarySlot && renderState) {
      renderState.seenSlotEvidenceKeys.add(buildManuscriptSlotEvidenceKey(primarySlot, segment.targetIds))
    }

    const primaryEvidenceRow = primarySlot
      ? renderManuscriptInlineSlot(primarySlot, segment, `${segment.segmentId}-slot-primary`)
      : null
    const secondaryEvidenceRows = secondarySlots
      .map((slot, index) => {
        const { evidence, slotAnchorQuote } = resolveManuscriptSlotEvidence(slot, segment)
        const hasSourceEvidence = evidence.inlineNodes.length > 0
        const fallbackQuote = slotAnchorQuote || buildEvidenceAnchorQuote(segment.anchorExcerpt, 140)
        const slotLead = [slot.summary, slot.title].map((item) => String(item || '').trim()).find(Boolean) || ''
        if (!hasSourceEvidence && !fallbackQuote && !slotLead) return null
        if (renderState) {
          renderState.seenSlotEvidenceKeys.add(buildManuscriptSlotEvidenceKey(slot, segment.targetIds))
        }
        return (
          <div key={`${segment.segmentId}-supp-slot-${index}`}>
            {slotLead ? (
              <Paragraph type="secondary" className="reader-experience-page__manuscript-inline-slot-summary">
                {slotLead}
              </Paragraph>
            ) : null}
            {renderInlineSourceEvidence(`${segment.segmentId}-supp-slot-${index}-source`, evidence.inlineNodes, {
              title: slot.title || (slot.slotKind === 'figure' ? '本段图示与图注' : '本段正文摘录'),
              evidenceLabel: '补充参考',
              maxHeight: slot.slotKind === 'figure' ? 360 : 220,
              fullEvidence: evidence.fullEvidence,
              fullEvidenceLabel: '展开完整证据',
              mode: slot.slotKind === 'figure' ? 'surface' : 'quote',
              anchorQuote: fallbackQuote,
            })}
          </div>
        )
      })
      .filter((row): row is NonNullable<typeof row> => row !== null)
    const optionalEvidenceRows = [primaryEvidenceRow, ...secondaryEvidenceRows]
      .filter((row): row is NonNullable<typeof row> => row !== null)

    const segmentAnchorQuote = buildEvidenceAnchorQuote(segment.anchorExcerpt, 160)
    const showSegmentAnchorQuote = Boolean(segmentAnchorQuote) && !narrativeChunks.length
    const inlineAnnotations: Array<{ key: string; title: string; content: ReactNode }> = []
    if (segment.glossaryRows.length) {
      inlineAnnotations.push({
        key: `${segment.segmentId}-glossary`,
        title: '术语注解',
        content: segment.glossaryRows.map((item, itemIndex) => (
          <span key={`${segment.segmentId}-glossary-${itemIndex}`}>
            <Text strong>{item.term}</Text>
            <Text type="secondary">：{item.note}</Text>
            {itemIndex < segment.glossaryRows.length - 1 ? <Text type="secondary">；</Text> : null}
          </span>
        )),
      })
    }
    if (segment.adjacentBridge) {
      inlineAnnotations.push({
        key: `${segment.segmentId}-adjacent`,
        title: '邻页承接',
        content: (
          <Text type="secondary">{segment.adjacentBridge}</Text>
        ),
      })
    }
    if (segment.referenceLinks.length) {
      inlineAnnotations.push({
        key: `${segment.segmentId}-resources`,
        title: '外部延伸',
        content: segment.referenceLinks.map((item, itemIndex) => (
          <span key={`${segment.segmentId}-link-${itemIndex}`}>
            <a href={item.href} target="_blank" rel="noreferrer">
              {item.label || item.href}
            </a>
            {item.note ? <Text type="secondary">：{item.note}</Text> : null}
            {itemIndex < segment.referenceLinks.length - 1 ? <Text type="secondary">；</Text> : null}
          </span>
        )),
      })
    }

    const hasAnyEvidence = optionalEvidenceRows.length > 0
    if (
      !segment.teachingText
      && !segment.title
      && !segmentAnchorQuote
      && !segment.glossaryRows.length
      && !segment.referenceLinks.length
      && !narrativeChunks.length
      && !hasAnyEvidence
    ) {
      return null
    }
    return (
      <section
        key={segment.segmentId}
        className="reader-experience-page__guided-beat reader-experience-page__manuscript-segment"
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {segment.title ? (
            <Text className="reader-experience-page__eyebrow reader-experience-page__manuscript-segment-title">{segment.title}</Text>
          ) : null}
          {showSegmentAnchorQuote ? (
            <blockquote className="reader-experience-page__manuscript-anchor-quote">
              {segmentAnchorQuote}
            </blockquote>
          ) : null}
          {narrativeChunks.length ? (
            <div className="reader-experience-page__manuscript-copy-flow">
              {narrativeChunks.map((chunk, index) => (
                <Paragraph
                  key={`${segment.segmentId}-copy-${index}`}
                  className="reader-experience-page__summary reader-experience-page__section-summary reader-experience-page__manuscript-copy"
                >
                  {chunk.text}
                </Paragraph>
              ))}
            </div>
          ) : null}
          {optionalEvidenceRows.length ? (
            <div style={{ marginTop: 2 }}>
              {renderEvidenceCollapse(
                `${segment.segmentId}-supp-evidence`,
                optionalEvidenceRows.length > 1
                  ? `补充材料 ${optionalEvidenceRows.length} 条`
                  : '补充原页片段',
                <Space direction="vertical" size={12} style={{ width: '100%' }}>
                  {optionalEvidenceRows}
                </Space>,
                '原页片段',
              )}
            </div>
          ) : null}
          {inlineAnnotations.length ? (
            <aside className="reader-experience-page__manuscript-annotations" aria-label="边注">
              {inlineAnnotations.map((item) => (
                <div key={item.key} className="reader-experience-page__manuscript-annotation-row">
                  <Text strong className="reader-experience-page__manuscript-annotation-label">{item.title}：</Text>
                  <span className="reader-experience-page__manuscript-annotation-content">{item.content}</span>
                </div>
              ))}
            </aside>
          ) : null}
        </Space>
      </section>
    )
  }

  const renderExperienceSection = (section: ReaderExperiencePlan['main_sections'][number]) => {
    const sectionType = String(section.section_type || '').trim()
    const sectionTypeToken = normalizeKeyToken(sectionType)
    const sectionTitle = preferDisplayCopy(section.display_title, section.title)
    const sectionSummary = preferDisplayCopy(section.display_summary, section.summary)
    const sectionBlocks = resolveSectionBlocks(section)
    const sectionResourceModules = resolveSectionResourceModules(section).filter((module) => hasUsableResourceModule(module))
    const sectionInteractionModules = resolveSectionInteractionModules(section).filter((module) => hasUsableInteractionModule(module))
    const sectionWidgets = resolveSectionWidgets(section).filter((widget) => hasUsableWidget(widget))
    const resourceBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'resource_module').map((block) => [String(block.ref_id || '').trim(), block]))
    const interactionBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'interaction_module').map((block) => [String(block.ref_id || '').trim(), block]))
    const widgetBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'widget').map((block) => [String(block.ref_id || '').trim(), block]))
    const sectionQuestionModules = sectionInteractionModules.filter((module) => isQuestionStarterModule(module))
    const sectionExplainerModules = sectionInteractionModules.filter((module) => !isQuestionStarterModule(module))
    const glossaryModules = sectionExplainerModules.filter((module) => String(module.module_type || '').trim() === 'GlossaryPanel')
    const readingNodes = resolveReadingFlowNodes(section)
    if (seedMode && (sectionTypeToken === 'supporting_resources' || sectionTypeToken === 'question_lab')) return null
    if (seedMode && sectionTypeToken === 'explainer_cluster' && !glossaryModules.length) return null
    if (sectionTypeToken === 'hero') return null

    const renderSectionTitle = (fallback: string) => sectionTitle || fallback

    if (sectionTypeToken === 'focus_stage') {
      const focusExplainerModules = seedMode ? glossaryModules : sectionExplainerModules
      const focusWidgets = seedMode ? [] : sectionWidgets
      const focusResourceModules = seedMode ? [] : sectionResourceModules
      const heroFigureSummary = preferDisplayCopy(hero?.display_summary, hero?.summary)
      const focusLead = resolveSectionNarrativeLead(
        sectionSummary,
        heroFigureSummary,
      )
      const shouldShowHeroFigureSummary = Boolean(heroFigureSummary)
        && orderedMainNarrativeSections.length === 0
        && !isNearDuplicateSentence(heroFigureSummary, focusLead)
        && !isSummaryPrefixDuplicate(heroFigureSummary, focusLead)
        && !isNearDuplicateSentence(heroFigureSummary, sectionSummary)
        && !isSummaryPrefixDuplicate(heroFigureSummary, sectionSummary)
      const focusEvidence = focusNode ? buildSegmentEvidence([focusNode], 'figure') : null
      const focusTeasers = buildCompactReferenceTeasers(
        focusEvidence?.inlineNodes?.length ? focusEvidence.inlineNodes : (focusNode ? [focusNode] : []),
        1,
      )
      const focusReferenceNodes = focusEvidence?.inlineNodes?.length
        ? focusEvidence.inlineNodes
        : (focusNode ? [focusNode] : [])
      const focusFullEvidence = focusNode
        ? (
          focusEvidence?.fullEvidence
          || renderReaderComponentTree([focusNode], renderCtx)
        )
        : null
      return (
        <section key={section.section_id} className="reader-experience-page__guided-beat" style={{ padding: '8px 0 12px' }}>
          <Text className="reader-experience-page__eyebrow">{renderSectionTitle('页面聚焦')}</Text>
          <div className="reader-experience-page__focus-layout">
            <div className="reader-experience-page__focus-visual">
              {focusLead ? (
                <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary" style={{ marginBottom: focusTeasers.length || focusNode ? 10 : 0 }}>
                  {focusLead}
                </Paragraph>
              ) : null}
              {focusTeasers.length ? (
                <Paragraph type="secondary" style={{ marginBottom: 10 }}>
                  摘录预览：{focusTeasers[0]}
                </Paragraph>
              ) : null}
              {focusNode ? (
                renderInlineSourceEvidence(`${section.section_id}-focus-reference`, focusReferenceNodes, {
                  title: '图示与图注',
                  evidenceLabel: '图示摘录',
                  compactSecondary: true,
                  maxHeight: 220,
                  mode: 'surface',
                  fullEvidence: focusFullEvidence,
                  fullEvidenceLabel: '完整图示与图注',
                })
              ) : null}
            </div>
            <div className="reader-experience-page__focus-side">
              {!shouldRenderGuidedBeats && shouldShowHeroFigureSummary ? (
                <div style={{ padding: '2px 2px 4px' }}>
                  <Text className="reader-experience-page__eyebrow">图示解读</Text>
                  <Paragraph className="reader-experience-page__summary">{heroFigureSummary}</Paragraph>
                </div>
              ) : null}
              {focusExplainerModules.length ? focusExplainerModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '术语解释暂未准备好')
              }) : null}
              {focusWidgets.length ? focusWidgets.map((widget) => {
                const block = widgetBlockLookup.get(String(widget.widget_id || '').trim()) || null
                return renderBlockState(block, renderWidget(widget, block), '图解控件暂未准备好')
              }) : null}
              {focusResourceModules.length ? focusResourceModules.map((module) => {
                const block = resourceBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderResourceModule(module, block), '延伸资源暂未准备好')
              }) : null}
              {!focusExplainerModules.length && !focusWidgets.length && !focusResourceModules.length && backgroundRefreshing
                ? <div className="reader-experience-page__loading"><Spin /></div>
                : null}
            </div>
          </div>
        </section>
      )
    }
    if (sectionTypeToken === 'reading_flow') {
      const readingLead = resolveSectionNarrativeLead(
        sectionSummary,
        preferDisplayCopy(hero?.display_summary, hero?.summary),
      )
      const readingEvidence = buildSegmentEvidence(readingNodes, 'body')
      const readingTeasers = buildCompactReferenceTeasers(
        readingEvidence.inlineNodes.length ? readingEvidence.inlineNodes : readingNodes,
        2,
      )
      const readingReferenceNodes = readingEvidence.inlineNodes.length
        ? readingEvidence.inlineNodes
        : readingNodes.slice(0, 3)
      const readingFullEvidence = readingEvidence.fullEvidence || (
        readingNodes.length
          ? renderReaderComponentTree(readingNodes, renderCtx)
          : null
      )
      return (
        <section key={section.section_id} className="reader-experience-page__guided-beat" style={{ padding: '8px 0 12px' }}>
          <Text className="reader-experience-page__eyebrow">{renderSectionTitle('主线叙述')}</Text>
          {readingLead ? (
            <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">
              {readingLead}
            </Paragraph>
          ) : null}
          {readingTeasers.length ? (
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {readingTeasers.map((teaser, index) => (
                <Paragraph key={`${section.section_id}-teaser-${index}`} type="secondary" style={{ marginBottom: 0 }}>
                  摘录 {index + 1}：{teaser}
                </Paragraph>
              ))}
            </Space>
          ) : null}
          {composeLoading && !hasComposePayload ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : readingNodes.length ? (
            <div style={{ marginTop: readingTeasers.length ? 10 : 0 }}>
              {renderInlineSourceEvidence(`${section.section_id}-reading-flow-reference`, readingReferenceNodes, {
                title: '正文摘录',
                evidenceLabel: '正文摘录',
                anchorQuote: readingEvidence.anchorQuote,
                compactSecondary: true,
                maxHeight: 220,
                fullEvidence: readingFullEvidence,
                fullEvidenceLabel: '完整正文片段',
              })}
            </div>
          ) : null}
        </section>
      )
    }
    if (sectionTypeToken === 'explainer_cluster') {
      const visibleExplainerModules = seedMode ? glossaryModules : sectionExplainerModules
      if (!visibleExplainerModules.length && !backgroundRefreshing) return null
      return (
        <Card key={section.section_id} className="reader-experience-page__panel">
          <Text className="reader-experience-page__eyebrow">{renderSectionTitle('概念解释')}</Text>
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {backgroundRefreshing && !visibleExplainerModules.length ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : visibleExplainerModules.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {visibleExplainerModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '解释模块暂未准备好')
              })}
            </Space>
          ) : null}
        </Card>
      )
    }
    if (sectionTypeToken === 'supporting_resources') {
      if (!sectionResourceModules.length && !backgroundRefreshing) return null
      return (
        <Card key={section.section_id} className="reader-experience-page__panel">
          <Text className="reader-experience-page__eyebrow">{renderSectionTitle('延伸阅读')}</Text>
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {backgroundRefreshing && !sectionResourceModules.length ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : sectionResourceModules.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {sectionResourceModules.map((module) => {
                const block = resourceBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderResourceModule(module, block), '资源模块暂未准备好')
              })}
            </Space>
          ) : null}
        </Card>
      )
    }
    if (sectionTypeToken === 'question_lab') {
      const questionModules = sectionQuestionModules.filter((module) => hasUsableInteractionModule(module))
      const hasFallbackQuestions = fallbackQuestionAnswers.some((item) => {
        const question = String(item.question || '').trim()
        const answer = String(item.answer || '').trim()
        return Boolean(question && answer && !isWeakPlaceholderText(answer))
      })
      const hasQuestionWidgets = sectionWidgets.length > 0
      if (!questionModules.length && !hasFallbackQuestions && !hasQuestionWidgets && !backgroundRefreshing) return null
      return (
        <Card key={section.section_id} className="reader-experience-page__panel">
          <Text className="reader-experience-page__eyebrow">{renderSectionTitle('继续追问')}</Text>
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {questionModules.length ? (
            <div className="reader-experience-page__question-grid">
              {questionModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '引导问题暂未准备好')
              })}
            </div>
          ) : hasFallbackQuestions ? (
            <Card size="small" className="reader-experience-page__module-card">
              <Text className="reader-experience-page__eyebrow">问题提示</Text>
              <Collapse
                bordered={false}
                className="reader-experience-page__qa-collapse"
                items={fallbackQuestionAnswers.filter((item) => {
                  const question = String(item.question || '').trim()
                  const answer = String(item.answer || '').trim()
                  return Boolean(question && answer && !isWeakPlaceholderText(answer))
                }).map((item, index) => ({
                  key: `fallback-qa-${index}`,
                  label: item.question,
                  children: <Paragraph className="reader-experience-page__summary">{item.answer}</Paragraph>,
                }))}
              />
            </Card>
          ) : null}
          {sectionWidgets.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 16 }}>
              {sectionWidgets.map((widget) => {
                const block = widgetBlockLookup.get(String(widget.widget_id || '').trim()) || null
                return renderBlockState(block, renderWidget(widget, block), '控件暂未准备好')
              })}
            </Space>
          ) : null}
          {!questionModules.length && !hasFallbackQuestions && !hasQuestionWidgets && backgroundRefreshing
            ? <div className="reader-experience-page__loading"><Spin /></div>
            : null}
        </Card>
      )
    }

    const genericRenderRows: ReactNode[] = []
    sectionExplainerModules.forEach((module) => {
      const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
      const row = renderBlockState(block, renderInteractionModule(module, block), '解释模块暂未准备好')
      if (row) genericRenderRows.push(row)
    })
    sectionResourceModules.forEach((module) => {
      const block = resourceBlockLookup.get(String(module.module_id || '').trim()) || null
      const row = renderBlockState(block, renderResourceModule(module, block), '资源模块暂未准备好')
      if (row) genericRenderRows.push(row)
    })
    sectionWidgets.forEach((widget) => {
      const block = widgetBlockLookup.get(String(widget.widget_id || '').trim()) || null
      const row = renderBlockState(block, renderWidget(widget, block), '控件暂未准备好')
      if (row) genericRenderRows.push(row)
    })

    const hasReadableContent = Boolean(
      sectionSummary
      || genericRenderRows.length
      || readingNodes.length
      || backgroundRefreshing,
    )
    if (!hasReadableContent) return null

    return (
      <Card key={section.section_id} className="reader-experience-page__panel">
        <Text className="reader-experience-page__eyebrow">{sectionTitle || '延伸内容'}</Text>
        {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
        {genericRenderRows.length ? (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {genericRenderRows}
          </Space>
        ) : null}
        {!genericRenderRows.length && composeLoading && !hasComposePayload ? (
          <div className="reader-experience-page__loading"><Spin /></div>
        ) : null}
        {!genericRenderRows.length && !composeLoading && readingNodes.length ? (
          renderInlineSourceEvidence(`${section.section_id}-generic-reading`, readingNodes, {
            title: sectionTitle || '原页摘录',
            evidenceLabel: '原页摘录',
            compactSecondary: true,
            maxHeight: 220,
          })
        ) : null}
      </Card>
    )
  }

  const orderedMainNarrativeSections = useMemo(() => {
    const seen = new Set<string>()
    return mainNarrativeSections.filter((section, index) => {
      const sectionId = String(section.section_id || '').trim()
      const fallbackKey = `${String(section.section_type || '').trim()}::${String(section.display_title || section.title || '').trim()}::${index}`
      const key = sectionId || fallbackKey
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }, [mainNarrativeSections])

  const dedupedGuidedBeats = useMemo(() => {
    const seen = new Set<string>()
    const supportContextSeen = new Set<string>()
    const result: ReaderExperienceGuidedBeat[] = []
    for (const beat of guidedBeats) {
      const beatType = normalizeKeyToken(beat.beat_type || '')
      const beatTitle = normalizeKeyToken(preferDisplayCopy(beat.display_title, beat.title))
      const beatSummary = normalizeKeyToken(preferDisplayCopy(beat.display_summary, beat.summary))
      const dedupeKey = `${beatType}::${beatTitle || beatSummary}`
      if (dedupeKey !== `${beatType}::` && seen.has(dedupeKey)) continue
      if (ATTACHED_GUIDED_BEAT_TYPES.has(beatType) && beatTitle && beatSummary) {
        const supportContextKey = `${beatTitle}::${beatSummary}`
        if (supportContextSeen.has(supportContextKey)) continue
        supportContextSeen.add(supportContextKey)
      }
      if (dedupeKey !== `${beatType}::`) seen.add(dedupeKey)
      result.push(beat)
    }
    return result
  }, [guidedBeats])
  const groupedGuidedSegments = useMemo(() => {
    const segments: GuidedReadingSegment[] = []
    const orderedItems: Array<
      | { kind: 'standalone'; beat: ReaderExperienceGuidedBeat }
      | { kind: 'segment'; segment: GuidedReadingSegment }
    > = []

    for (const beat of dedupedGuidedBeats) {
      const beatType = normalizeKeyToken(beat.beat_type || '')
      if (PRIMARY_GUIDED_BEAT_TYPES.has(beatType)) {
        const segment = { primary: beat, support: [] as ReaderExperienceGuidedBeat[] }
        segments.push(segment)
        orderedItems.push({ kind: 'segment', segment })
        continue
      }
      if (ATTACHED_GUIDED_BEAT_TYPES.has(beatType) && segments.length) {
        segments[segments.length - 1].support.push(beat)
        continue
      }
      orderedItems.push({ kind: 'standalone', beat })
    }

    return { orderedItems, segments }
  }, [dedupedGuidedBeats])
  const primarySeedGuidedBeats = useMemo(
    () => dedupedGuidedBeats.filter((beat) => {
      const beatType = String(beat.beat_type || '').trim()
      return beatType === 'figure_walkthrough' || beatType === 'body_segment'
    }),
    [dedupedGuidedBeats],
  )
  const shouldRenderGuidedBeats = hasGuidedBeats && orderedMainNarrativeSections.length === 0 && (
    !seedMode
      ? groupedGuidedSegments.orderedItems.length > 0
      : primarySeedGuidedBeats.length > 0
  )
  const shouldRenderTeachingManuscript = isFinalManuscriptOnly
    ? manuscriptSegments.length > 0
    : isManuscriptPreferred

  const heroSummary = preferDisplayCopy(hero?.display_summary, hero?.summary)
  const heroSubtitle = preferDisplayCopy(hero?.display_subtitle, hero?.subtitle)
  const focusLabel = normalizeReaderFacingFocusLabel(hero?.focus_label)
  const focusStageSection = orderedMainNarrativeSections.find(
    (section) => normalizeKeyToken(section.section_type || '') === 'focus_stage',
  )
  const focusStageSummary = focusStageSection
    ? preferDisplayCopy(focusStageSection.display_summary, focusStageSection.summary)
    : ''
  const shouldShowHeroSubtitle = Boolean(heroSubtitle)
    && orderedMainNarrativeSections.length === 0
    && !isNearDuplicateSentence(heroSubtitle, heroSummary)
    && !isSummaryPrefixDuplicate(heroSubtitle, heroSummary)
    && !isNearDuplicateSentence(heroSubtitle, focusStageSummary)
    && !isSummaryPrefixDuplicate(heroSubtitle, focusStageSummary)
  const shouldShowManuscriptOpening = Boolean(manuscriptOpening)
    && !isNearDuplicateSentence(manuscriptOpening, heroSummary)
    && !isSummaryPrefixDuplicate(manuscriptOpening, heroSummary)
  const manuscriptMetaRows = [
    { key: 'adjacent', label: '邻页承接', value: manuscriptHeroContinuity.adjacentLine },
    { key: 'resource', label: '外部延伸', value: manuscriptHeroContinuity.resourceLine.replace(/^外部延伸：/, '') },
  ].filter((item) => String(item.value || '').trim())

  const renderManuscriptDocument = (statusLabel: string) => {
    const renderState: ManuscriptRenderState = {
      seenSlotEvidenceKeys: new Set<string>(),
      seenLongNarrativeKeys: new Set<string>(),
    }
    return (
      <article className="reader-experience-page__manuscript-document">
      <header className="reader-experience-page__manuscript-head">
        <div className="reader-experience-page__status-row">
          <Text className="reader-experience-page__eyebrow">{statusLabel}</Text>
          <span className="reader-experience-page__status-chip">{topStatusText}</span>
        </div>
        <Title level={3} style={{ marginTop: 0 }}>{focusHeading}</Title>
        {heroSummary ? <Paragraph className="reader-experience-page__summary">{heroSummary}</Paragraph> : null}
        {shouldShowManuscriptOpening ? (
          <Paragraph type="secondary" className="reader-experience-page__manuscript-intro">
            {manuscriptOpening}
          </Paragraph>
        ) : null}
        {shouldShowHeroSubtitle && !isFinalManuscriptOnly ? (
          <Paragraph type="secondary" className="reader-experience-page__manuscript-intro">
            {heroSubtitle}
          </Paragraph>
        ) : null}
        {focusLabel && !isFinalManuscriptOnly ? (
          <span className="reader-experience-page__manuscript-focus-chip">{focusLabel}</span>
        ) : null}
        {manuscriptMetaRows.length ? (
          <div className="reader-experience-page__manuscript-meta-list">
            {manuscriptMetaRows.map((item) => (
              <Paragraph key={item.key} type="secondary" className="reader-experience-page__manuscript-meta-item">
                <Text strong>{item.label}：</Text>{item.value}
              </Paragraph>
            ))}
          </div>
        ) : null}
      </header>
      <div className="reader-experience-page__manuscript-flow">
        {manuscriptSegments.map((segment) => renderTeachingManuscriptSegment(segment, renderState))}
      </div>
      </article>
    )
  }

  if (isFinalManuscriptOnly) {
    if (!shouldRenderTeachingManuscript) {
      return (
        <Card className="reader-experience-page__panel reader-experience-page__panel--empty">
          <Empty
            description={(
              <Space direction="vertical" size={4}>
                <Text strong>终稿讲读稿尚未生成完成</Text>
                <Text type="secondary">当前页体验内容尚未就绪，请稍后刷新。</Text>
              </Space>
            )}
          />
        </Card>
      )
    }
    return (
      <div
        className={[
          'reader-experience-page__layout',
          `reader-experience-page__layout--${layoutVariant}`,
          'reader-experience-page__layout--solo',
          'reader-experience-page__layout--manuscript',
        ].filter(Boolean).join(' ')}
      >
        <main className="reader-experience-page__main">
          {renderManuscriptDocument('体验内容（后备）')}
        </main>
      </div>
    )
  }

  return (
    <>
      <div
        className={[
          'reader-experience-page__layout',
          `reader-experience-page__layout--${layoutVariant}`,
          !hasSidebar ? 'reader-experience-page__layout--solo' : '',
          shouldRenderTeachingManuscript ? 'reader-experience-page__layout--manuscript' : '',
        ].filter(Boolean).join(' ')}
      >
        <main className="reader-experience-page__main">
          {shouldRenderTeachingManuscript
            ? (
              renderManuscriptDocument('页面焦点')
            )
            : (
              <>
                <Card className="reader-experience-page__hero-card">
                  <div
                    className={[
                      'reader-experience-page__hero-grid',
                      `reader-experience-page__hero-grid--${layoutVariant}`,
                    ].join(' ')}
                  >
                    <div className="reader-experience-page__hero-summary">
                      <div className="reader-experience-page__status-row">
                        <Text className="reader-experience-page__eyebrow">页面焦点</Text>
                        <span className="reader-experience-page__status-chip">{topStatusText}</span>
                      </div>
                      <Title level={3} style={{ marginTop: 0 }}>{focusHeading}</Title>
                      {heroSummary ? <Paragraph className="reader-experience-page__summary">{heroSummary}</Paragraph> : null}
                      {focusLabel ? <Tag color="geekblue">{focusLabel}</Tag> : null}
                      {!hasGuidedBeats && visibleClaims.length ? (
                        <div className="reader-experience-page__claim-strip">
                          {visibleClaims.map((claim) => (
                            <div key={claim.claim_id} className="reader-experience-page__claim-card">
                              <Text className="reader-experience-page__eyebrow">阅读线索</Text>
                              <Paragraph className="reader-experience-page__summary">
                                {buildReaderFacingClaimSnippet(preferDisplayCopy(claim.display_text, claim.text))}
                              </Paragraph>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <div className="reader-experience-page__hero-claims">
                      {!shouldRenderGuidedBeats && shouldShowHeroSubtitle ? (
                        <Card size="small" className="reader-experience-page__module-card reader-experience-page__hero-mini">
                          <Text className="reader-experience-page__eyebrow">阅读切入点</Text>
                          <Paragraph className="reader-experience-page__summary">{heroSubtitle}</Paragraph>
                        </Card>
                      ) : null}
                      {!hasGuidedBeats && heroContextCards.length ? (
                        <div className="reader-experience-page__hero-mini-stack">
                          {heroContextCards.map((card) => (
                            <section
                              key={`hero-context-${card.key}`}
                              className="reader-experience-page__hero-mini"
                              style={{ padding: '10px 12px' }}
                            >
                              <Text className="reader-experience-page__eyebrow">{card.title}</Text>
                              {card.body}
                            </section>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  </div>
                </Card>
                {shouldRenderGuidedBeats
                  ? (
                    seedMode
                      ? primarySeedGuidedBeats.map((beat, index) => renderGuidedSegment({ primary: beat, support: [] }, index))
                      : groupedGuidedSegments.orderedItems.map((item) => (
                        item.kind === 'standalone'
                          ? renderStandaloneBeat(item.beat)
                          : renderGuidedSegment(
                            item.segment,
                            groupedGuidedSegments.segments.indexOf(item.segment),
                          )
                      ))
                  )
                  : orderedMainNarrativeSections.map((section) => renderExperienceSection(section))}
              </>
            )}
        </main>

        {hasSidebar ? (
          <aside className="reader-experience-page__sidebar">
            {sidebarContextCards.map((card) => (
              <section
                key={card.key}
                style={{
                  padding: '4px 2px 10px 10px',
                  borderLeft: '2px solid rgba(120, 140, 168, 0.22)',
                }}
              >
                <Text className="reader-experience-page__eyebrow">{card.title}</Text>
                {card.body}
              </section>
            ))}
            {effectiveSidebarSections.map((section) => renderExperienceSection(section))}
          </aside>
        ) : null}
      </div>

      {footerNarrativeSections.length ? (
        <div className="reader-experience-page__footer-sections">
          {footerNarrativeSections.map((section) => renderExperienceSection(section))}
        </div>
      ) : null}
    </>
  )
}
