import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Collapse, Divider, Image, Input, Layout, Space, Tag, Typography } from 'antd'
import { ProCard } from '@ant-design/pro-components'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { literatureApi } from '@/services/api'
import type { PageArtifactV2, PageArtifactV2ReadingBlock, PageArtifactV2SegmentKind } from '@/services/api'

import PageArtifactV2ReaderOpening from './PageArtifactV2ReaderOpening'
import './pageArtifactV2.css'

const { Paragraph, Text, Title } = Typography
const { Content, Sider } = Layout

type PageArtifactV2RendererProps = {
  artifact: PageArtifactV2
  mode?: 'reader' | 'workbench'
  navigation?: {
    paperId: number
    readerProfile?: string
    selectedKbId?: number
    userIntent?: string
  }
  onRewriteBlockRequest?: (block: PageArtifactV2ReadingBlock) => void
  onRewriteBlockCancel?: () => void
  activeRewriteBlockId?: string | null
  rewriteDraft?: string
  rewritePromptPlaceholder?: string
  rewritePreviewText?: string
  onRewriteDraftChange?: (value: string) => void
  onRewriteSubmit?: () => void
  rewritingBlockId?: string | null
  rewriteDisabled?: boolean
  recentRewriteMarker?: { blockId: string; nonce: number } | null
}

type MediaBinding = {
  binding_kind: string
  binding_layout_id: string
  binding_source_ref: string
  page_asset_ref: string
  page_image_url: string
}

type MainBlockGroup = {
  groupId: string
  groupLabel: string
  heading: PageArtifactV2ReadingBlock | null
  blocks: PageArtifactV2ReadingBlock[]
}

type ReaderBridge = {
  page: number
  keyPoints: string[]
  bridgeText: string
}

type ReaderNeighborPreview = {
  page: number
  summary: string
  keyPoints: string[]
}

type ReaderActionChip = {
  key: string
  label: string
  href?: string
  kind: 'anchor' | 'preview'
  tone: 'focus' | 'navigate'
  previewKey?: 'previous' | 'next'
  previewKicker?: string
  previewSummary?: string
  previewPoints?: string[]
}

type ReaderBlockAskChip = {
  key: string
  label: string
  title: string
  question: string
  displayQuestion: string
  placeholder: string
  targetSegmentId: string
  explainKind: 'simplify' | 'figure'
  sourceExcerpt?: string
  sourceTranslationZh?: string
  explanationText?: string
  figureLabel?: string
  figureCaption?: string
  figureText?: string
  figureImageUrl?: string
}

type ReaderPreviewCue = {
  key: string
  label: string
  tone: 'visual' | 'structure' | 'continuity'
}

type ReaderAskMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

type ReaderAskThreadState = {
  messages: ReaderAskMessage[]
  draft: string
  seeded: boolean
}

type ReaderRewriteAnimationState = {
  blockId: string
  visibleText: string
  isTyping: boolean
  highlight: boolean
  nonce: number
}

const MAX_BLOCK_EXPLAIN_HISTORY_MESSAGES = 12
const READER_BLOCK_EXPLAIN_LOADING_HINTS = {
  simplify: [
    '正在解构长句结构…',
    '正在换成更贴近日常的说法…',
    '正在重组这一段的关键意思…',
  ],
  figure: [
    '正在提取图例与标签…',
    '正在对齐主要对比关系…',
    '正在整理图证与本页结论的对应…',
  ],
} as const
const READER_BLOCK_EXPLAIN_STREAMING_HINTS = {
  simplify: '正在把这段重新讲清楚…',
  figure: '正在把图里的证据串起来…',
} as const
const READER_BLOCK_EXPLAIN_CONTEXT_NOTES = {
  simplify: '只基于当前原文摘录、当前 AI 解读和本地追问历史。',
  figure: '只基于当前图块文本、真实图片 asset 和本地追问历史。',
} as const
const READER_BLOCK_EXPLAIN_LOADING_CUES = {
  simplify: ['拆句', '换说法', '保重点'],
  figure: ['锁定图例', '比较走势', '回扣结论'],
} as const
const READER_BLOCK_EXPLAIN_SKELETON_WIDTHS = {
  simplify: ['42%', '78%', '64%', '51%'],
  figure: ['30%', '84%', '58%', '69%'],
} as const

const SUPPORT_SEGMENT_KINDS = new Set<PageArtifactV2SegmentKind>([
  'term_annotation',
  'external_resource',
  'aside_content',
])

const REWRITABLE_SEGMENT_KINDS = new Set<PageArtifactV2SegmentKind>([
  'heading',
  'paragraph',
  'authored_explanation',
  'aside_content',
  'term_annotation',
])

const RAIL_HINTS = new Set([
  'rail',
  'side',
  'sidebar',
  'support',
  'side-rail',
  'support-rail',
  'support_rail',
])

const INLINE_HINTS = new Set([
  'inline',
  'block',
  'main',
  'main-flow',
  'main_flow',
])

function toClassToken(raw: string): string {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function normalizeTextKey(raw: string): string {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '')
    .replace(/[：:，,。.!！?？()（）【】[\]·\-—_]/g, '')
}

function compactText(raw: string, maxLength: number): string {
  const text = String(raw || '').trim().replace(/\s+/g, ' ')
  if (!text) return ''
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength - 1).trimEnd()}…`
}

function getRewriteInlineTitle(block: PageArtifactV2ReadingBlock): string {
  const kind = String(block.segment_kind || '').trim()
  if (kind === 'heading') return '重写当前标题'
  if (kind === 'term_annotation') return '重写当前术语注释'
  if (kind === 'aside_content') return '重写当前页边提示'
  return '重写当前块'
}

function trimTrailingSentencePunctuation(raw: string): string {
  return String(raw || '').trim().replace(/[。.!！?？:：;；、，,\s]+$/g, '')
}

function trimLeadingSentencePunctuation(raw: string): string {
  return String(raw || '').trim().replace(/^[。.!！?？:：;；、，,\s]+/g, '')
}

function normalizeAskMarkdown(raw: string): string {
  return String(raw || '').replace(/\r\n/g, '\n').trim()
}

function renderAskAssistantMarkdown(content: string, explainKind: 'simplify' | 'figure') {
  const normalized = normalizeAskMarkdown(content)
  return (
    <div
      className={[
        'page-artifact-v2__block-query-markdown',
        `page-artifact-v2__block-query-markdown--${explainKind}`,
      ].join(' ')}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  )
}

function getMetaToken(block: PageArtifactV2ReadingBlock, key: string): string {
  return String(block.meta?.[key] || '').trim()
}

function getPlacementHint(block: PageArtifactV2ReadingBlock): string {
  return getMetaToken(block, 'placement').toLowerCase()
}

function getLaneHint(block: PageArtifactV2ReadingBlock): string {
  return getMetaToken(block, 'lane').toLowerCase()
}

function getReaderRole(block: PageArtifactV2ReadingBlock): string {
  return getMetaToken(block, 'reader_role').toLowerCase()
}

function shouldRenderInRail(block: PageArtifactV2ReadingBlock): boolean {
  const placement = getPlacementHint(block)
  const lane = getLaneHint(block)
  if (RAIL_HINTS.has(placement) || RAIL_HINTS.has(lane)) return true
  if (!SUPPORT_SEGMENT_KINDS.has(block.segment_kind)) return false
  // 辅助 blocks 默认放在 rail，除非后端显式标记为 inline；
  // 这样生成的旁注不会打断主正文。
  if (INLINE_HINTS.has(placement) || INLINE_HINTS.has(lane)) return false
  return true
}

function canRewriteExperienceBlock(block: PageArtifactV2ReadingBlock): boolean {
  return REWRITABLE_SEGMENT_KINDS.has(block.segment_kind)
}

function cleanLeadCopy(raw: string): string {
  return String(raw || '')
    .replace(/^先顺着当前页的主线往下读[:：]\s*/, '')
    .replace(/^先看清[^:：]+[:：]\s*/, '')
    .trim()
}

function getBlockLabel(block: PageArtifactV2ReadingBlock): string {
  const meta = block.meta || {}
  const kind = block.segment_kind
  if (kind === 'heading') return '章节引导'
  if (kind === 'paragraph') return '讲解段落'
  if (kind === 'original_excerpt') return '原文锚点'
  if (kind === 'authored_explanation') return '讲解'
  if (kind === 'term_annotation') return String(meta.term || '术语注释').trim()
  if (kind === 'external_resource') return String(meta.resource_type || '延伸资源').trim() || '延伸资源'
  if (kind === 'aside_content') return String(meta.label || '旁注').trim() || '旁注'
  return String(meta.label || kind).trim() || kind
}

function getReaderSupportTitle(block: PageArtifactV2ReadingBlock): string {
  const meta = block.meta || {}
  if (block.segment_kind === 'term_annotation') {
    return String(meta.display_term || meta.reader_title || '术语补充').trim() || '术语补充'
  }
  if (block.segment_kind === 'external_resource') {
    return String(meta.reader_title || block.text || '延伸阅读').trim() || '延伸阅读'
  }
  if (block.segment_kind === 'aside_content') {
    const raw = String(meta.reader_title || meta.label || '').trim()
    const lowered = raw.toLowerCase()
    if (!raw || lowered === 'aside' || raw === '旁注') {
      const role = getReaderRole(block)
      if (role === 'continuity_bridge') return '衔接提示'
      return '页边提示'
    }
    return raw
  }
  return String(meta.reader_title || meta.label || getBlockLabel(block)).trim() || '补充说明'
}

function getReaderSupportCopy(block: PageArtifactV2ReadingBlock): string {
  const meta = block.meta || {}
  const raw = String(
    block.segment_kind === 'external_resource'
      ? meta.note || meta.description || ''
      : block.text || meta.reader_copy || '',
  ).trim()
  if (
    raw === '在读到这里时补一层背景，不打断当前页主线。'
    || raw === '这些内容仍然服务当前页主线，只是换一种更轻的方式跟在正文旁边。'
  ) {
    return ''
  }
  return raw
}

function getExcerptTranslation(block: PageArtifactV2ReadingBlock): string {
  const direct = String(block.meta?.translation_zh || '').trim()
  if (direct) return direct
  return String(block.meta?.reader_translation_zh || '').trim()
}

function getStringList(raw: unknown, maxItems: number, maxLength = 180): string[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => compactText(String(item || '').trim(), maxLength))
    .filter(Boolean)
    .slice(0, maxItems)
}

function getReaderBridge(raw: unknown): ReaderBridge | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const record = raw as Record<string, unknown>
  const page = Number(record.page || 0)
  const keyPoints = getStringList(record.key_points, 3, 160)
  const bridgeText = compactText(String(record.bridge_text || '').trim(), 220)
  if (!page && !keyPoints.length && !bridgeText) return null
  return {
    page: Number.isFinite(page) ? page : 0,
    keyPoints,
    bridgeText,
  }
}

function getReaderNeighborPreview(raw: unknown): ReaderNeighborPreview | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const record = raw as Record<string, unknown>
  const page = Number(record.page || 0)
  const summary = compactText(String(record.summary || '').trim(), 240)
  const keyPoints = getStringList(record.key_points, 4, 150)
  if (!page && !summary && !keyPoints.length) return null
  return {
    page: Number.isFinite(page) ? page : 0,
    summary,
    keyPoints,
  }
}

function buildReaderBridgeSummary(bridge: ReaderBridge | null, mode: 'previous' | 'next'): string {
  if (!bridge) return ''
  const prefix = bridge.page > 0
    ? (mode === 'previous' ? `承接第 ${bridge.page} 页` : `接到第 ${bridge.page} 页`)
    : (mode === 'previous' ? '承接上一页' : '往下一页')
  const lead = bridge.keyPoints[0] || ''
  const bridgeText = bridge.bridgeText || ''
  if (bridgeText) return `${trimTrailingSentencePunctuation(prefix)}：${trimLeadingSentencePunctuation(bridgeText)}`
  if (lead) return `${trimTrailingSentencePunctuation(prefix)}：${trimLeadingSentencePunctuation(lead)}`
  return prefix
}

function splitReaderPreviewPoints(points: string[]): { cues: ReaderPreviewCue[]; notes: string[] } {
  const cues: ReaderPreviewCue[] = []
  const notes: string[] = []

  for (const raw of points) {
    const item = String(raw || '').trim()
    if (!item) continue

    const cueSpecs: Array<[prefix: string, tone: ReaderPreviewCue['tone']]> = [
      ['Figure 焦点：', 'visual'],
      ['Table 焦点：', 'visual'],
      ['Equation 焦点：', 'visual'],
      ['图注线索：', 'visual'],
      ['章节落点：', 'structure'],
    ]

    const matched = cueSpecs.find(([prefix]) => item.startsWith(prefix))
    if (matched) {
      const [prefix, tone] = matched
      const label = item.slice(prefix.length).trim() || item
      cues.push({
        key: `${tone}:${label}`,
        label,
        tone,
      })
      continue
    }

    if (item.includes('继续') || item.includes('承接') || item.includes('延伸') || item.includes('过渡')) {
      cues.push({
        key: `continuity:${item}`,
        label: item,
        tone: 'continuity',
      })
      continue
    }

    notes.push(item)
  }

  return {
    cues: cues.slice(0, 3),
    notes: notes.slice(0, 3),
  }
}

function getMediaBinding(block: PageArtifactV2ReadingBlock): MediaBinding | null {
  const meta = block.meta || {}
  const raw = (meta.media_binding || meta.figure_binding || {}) as Record<string, unknown>
  const pageAssetRef = String(raw.page_asset_ref || meta.page_asset_ref || raw.page_image_url || meta.page_image_url || '').trim()
  if (!pageAssetRef) return null
  return {
    binding_kind: String(raw.binding_kind || meta.binding_kind || '').trim(),
    binding_layout_id: String(raw.binding_layout_id || meta.binding_layout_id || '').trim(),
    binding_source_ref: String(raw.binding_source_ref || meta.binding_source_ref || '').trim(),
    page_asset_ref: pageAssetRef,
    page_image_url: String(raw.page_image_url || meta.page_image_url || '').trim(),
  }
}

function getResourceHost(rawUrl: string): string {
  try {
    return new URL(rawUrl).hostname.replace(/^www\./, '')
  } catch {
    return rawUrl
  }
}

function buildReaderBlockAnchorId(segmentId: string): string {
  return `reader-block-${segmentId}`
}

function buildExperienceV2PageHref(
  navigation: NonNullable<PageArtifactV2RendererProps['navigation']>,
  page: number,
  options?: { cacheOnly?: boolean },
): string {
  const params = new URLSearchParams()
  params.set('page', String(page))
  if (navigation.readerProfile) params.set('reader', navigation.readerProfile)
  if ((navigation.selectedKbId || 0) > 0) params.set('kb', String(navigation.selectedKbId))
  if (navigation.userIntent) params.set('intent', navigation.userIntent)
  if (options?.cacheOnly) params.set('cache_only', '1')
  return `/literature/${navigation.paperId}/experience-v2?${params.toString()}`
}

function getReaderMediaChipLabel(block: PageArtifactV2ReadingBlock | undefined): string {
  if (!block) return '只看图证'
  const label = String(block.meta?.label || block.text || '').trim()
  if (!label) return '只看图证'
  if (/^(fig(?:ure)?\s*\d+[a-z]?|图\s*\d+)/i.test(label)) {
    return `只看 ${compactText(label, 18)}`
  }
  return '只看图证'
}

function getReaderSupportChipLabel(block: PageArtifactV2ReadingBlock | undefined): string {
  if (!block) return '查看页边补充'
  const title = compactText(getReaderSupportTitle(block), 14)
  if (!title || title === '页边提示' || title === '补充说明' || title === '衔接提示') {
    return '查看页边补充'
  }
  return `查看${title}`
}

function buildReaderAskDisplayQuestion(kind: 'simplify' | 'figure', mediaBlock?: PageArtifactV2ReadingBlock): string {
  if (kind === 'simplify') return '请把这一段讲得更通俗一点'
  if (kind === 'figure') {
    const mediaLabel = String(mediaBlock?.meta?.label || mediaBlock?.text || '').trim()
    return mediaLabel ? `请只解释 ${mediaLabel}` : '请只解释这张图'
  }
  return ''
}

function buildReaderAskTitle(kind: 'simplify' | 'figure', mediaBlock?: PageArtifactV2ReadingBlock): string {
  if (kind === 'simplify') return '更通俗地解释这一段'
  if (kind === 'figure') {
    const mediaLabel = String(mediaBlock?.meta?.label || mediaBlock?.text || '').trim()
    return mediaLabel ? `只解释 ${mediaLabel}` : '只解释这张图'
  }
  return ''
}

function getInitialAskThreadState(): ReaderAskThreadState {
  return {
    messages: [],
    draft: '',
    seeded: false,
  }
}

function getBlockExplainLoadingHint(kind: 'simplify' | 'figure', index: number): string {
  const hints = READER_BLOCK_EXPLAIN_LOADING_HINTS[kind]
  return hints[index % hints.length]
}

function getBlockExplainStreamingHint(kind: 'simplify' | 'figure'): string {
  return READER_BLOCK_EXPLAIN_STREAMING_HINTS[kind]
}

function getBlockExplainContextNote(kind: 'simplify' | 'figure'): string {
  return READER_BLOCK_EXPLAIN_CONTEXT_NOTES[kind]
}

function renderMediaVisual(block: PageArtifactV2ReadingBlock) {
  const meta = block.meta || {}
  const binding = getMediaBinding(block)
  const assetRef = binding?.page_asset_ref || ''
  const tableRows = Array.isArray(meta.table_rows) ? (meta.table_rows as unknown[][]) : []
  const equationText = String(meta.normalized_text || block.text || '').trim()

  if (block.segment_kind === 'table_slot' && tableRows.length > 0) {
    return (
      <div className="page-artifact-v2__media-table">
        <table className="page-artifact-v2__table">
          <tbody>
            {tableRows.map((row, rowIndex) => (
              <tr key={`${block.segment_id}-row-${rowIndex}`}>
                {row.map((cell, cellIndex) => (
                  <td key={`${block.segment_id}-cell-${rowIndex}-${cellIndex}`}>{String(cell || '').trim() || '—'}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  if (block.segment_kind === 'equation_slot' && equationText) {
    return <pre className="page-artifact-v2__equation">{equationText}</pre>
  }

  if (!assetRef) {
    return (
      <Alert
        type="error"
        showIcon
        message={`media/resource binding unresolved: ${block.segment_kind}`}
      />
    )
  }

  return (
    <Image
      className="page-artifact-v2__image"
      src={assetRef}
      alt={String(meta.label || block.text || block.segment_kind).trim()}
      preview={false}
    />
  )
}

function renderSupportCard(
  block: PageArtifactV2ReadingBlock,
  mode: 'reader' | 'workbench',
  surface: 'inline' | 'rail',
) {
  const meta = block.meta || {}
  const cardClassName = [
    'page-artifact-v2__support-card',
    surface === 'rail' ? 'page-artifact-v2__support-card--rail' : 'page-artifact-v2__support-card--inline',
    `page-artifact-v2__support-card--${block.segment_kind}`,
    getReaderRole(block) ? `page-artifact-v2__support-card--role-${toClassToken(getReaderRole(block))}` : '',
  ]
    .filter(Boolean)
    .join(' ')

  if (block.segment_kind === 'term_annotation') {
    return (
      <div key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className="page-artifact-v2__anchor-target">
      <ProCard className={cardClassName} bodyStyle={{ padding: 16 }}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--term" />
            <span className="page-artifact-v2__block-label">术语注释</span>
          </div>
        ) : null}
        <Title level={5} className="page-artifact-v2__aside-title">
          {mode === 'reader' ? getReaderSupportTitle(block) : String(meta.term || getBlockLabel(block)).trim()}
        </Title>
        <Paragraph className="page-artifact-v2__support-note">{getReaderSupportCopy(block) || block.text}</Paragraph>
      </ProCard>
      </div>
    )
  }

  if (block.segment_kind === 'external_resource') {
    const url = String(meta.url || '').trim()
    return (
      <div key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className="page-artifact-v2__anchor-target">
      <ProCard className={cardClassName} bodyStyle={{ padding: 16 }}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--resource" />
            <span className="page-artifact-v2__block-label">延伸阅读</span>
          </div>
        ) : null}
        <Title level={5} className="page-artifact-v2__aside-title">
          {mode === 'reader' ? getReaderSupportTitle(block) : compactText(block.text, 88) || '外部资源'}
        </Title>
        {getReaderSupportCopy(block) ? (
          <Paragraph className="page-artifact-v2__support-note">{getReaderSupportCopy(block)}</Paragraph>
        ) : null}
        {url ? (
          <a className="page-artifact-v2__support-link" href={url} target="_blank" rel="noreferrer">
            打开 {getResourceHost(url)}
          </a>
        ) : (
          <Alert type="error" showIcon message="external resource binding unresolved" />
        )}
      </ProCard>
      </div>
    )
  }

  return (
    <div key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className="page-artifact-v2__anchor-target">
    <ProCard className={cardClassName} bodyStyle={{ padding: 16 }}>
      {mode === 'workbench' ? (
        <div className="page-artifact-v2__block-eyebrow">
          <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--support" />
          <span className="page-artifact-v2__block-label">{getBlockLabel(block)}</span>
        </div>
      ) : null}
      <Title level={5} className="page-artifact-v2__aside-title">
        {mode === 'reader' ? getReaderSupportTitle(block) : String(meta.label || '旁注').trim()}
      </Title>
      {getReaderSupportCopy(block) ? (
        <Paragraph className="page-artifact-v2__support-note">{getReaderSupportCopy(block)}</Paragraph>
      ) : null}
    </ProCard>
    </div>
  )
}

function renderMainBlock(
  block: PageArtifactV2ReadingBlock,
  isGuided: boolean,
  mode: 'reader' | 'workbench',
) {
  const meta = block.meta || {}
  const placement = String(meta.placement || '').trim()
  const lane = getLaneHint(block)
  const readerRole = getReaderRole(block)
  const prominence = getMetaToken(block, 'prominence')
  const blockClassName = [
    'page-artifact-v2__main-block',
    `page-artifact-v2__main-block--${block.segment_kind}`,
    block.source_lane === 'current_page'
      ? 'page-artifact-v2__main-block--current-page'
      : 'page-artifact-v2__main-block--authored',
    isGuided ? 'page-artifact-v2__main-block--guided' : '',
    placement ? `page-artifact-v2__main-block--placement-${toClassToken(placement)}` : '',
    lane ? `page-artifact-v2__main-block--lane-${toClassToken(lane)}` : '',
    readerRole ? `page-artifact-v2__main-block--role-${toClassToken(readerRole)}` : '',
    prominence ? `page-artifact-v2__main-block--prominence-${toClassToken(prominence)}` : '',
  ]
    .filter(Boolean)
    .join(' ')

  if (SUPPORT_SEGMENT_KINDS.has(block.segment_kind)) {
    return renderSupportCard(block, mode, 'inline')
  }

  if (
    block.segment_kind === 'figure_slot'
    || block.segment_kind === 'media_slot'
    || block.segment_kind === 'table_slot'
    || block.segment_kind === 'equation_slot'
  ) {
    return (
      <section key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className={`page-artifact-v2__media ${blockClassName} page-artifact-v2__anchor-target`}>
        <div className="page-artifact-v2__media-frame">
          <div className="page-artifact-v2__block-eyebrow">
            <span className={`page-artifact-v2__block-dot page-artifact-v2__block-dot--${block.segment_kind.includes('equation') ? 'media' : 'figure'}`} />
            <span className="page-artifact-v2__block-label">{getBlockLabel(block)}</span>
          </div>
          <div className="page-artifact-v2__media-copy">
            <Title level={4} className="page-artifact-v2__media-title">
              {String(meta.label || block.text || getBlockLabel(block)).trim()}
            </Title>
            <Paragraph className="page-artifact-v2__media-description">
              {String(meta.caption || meta.description || block.text).trim()}
            </Paragraph>
          </div>
          <div className="page-artifact-v2__media-visual">{renderMediaVisual(block)}</div>
        </div>
      </section>
    )
  }

  if (block.segment_kind === 'original_excerpt') {
    const translationZh = getExcerptTranslation(block)
    return (
      <section key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className={`${blockClassName} page-artifact-v2__excerpt page-artifact-v2__anchor-target`}>
        {mode === 'reader' ? (
          <div className="page-artifact-v2__source-strip">
            <span className="page-artifact-v2__source-pill">SOURCE</span>
            <span className="page-artifact-v2__source-note">原文摘录</span>
          </div>
        ) : null}
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--excerpt" />
            <span className="page-artifact-v2__block-label">原文片段</span>
          </div>
        ) : null}
        <Paragraph className="page-artifact-v2__excerpt-text">{block.text}</Paragraph>
        {translationZh ? (
          <Collapse
            ghost
            size="small"
            className="page-artifact-v2__excerpt-translation"
            items={[
              {
                key: `${block.segment_id}-translation-zh`,
                label: '中文译文',
                children: (
                  <Paragraph className="page-artifact-v2__excerpt-translation-text">
                    {translationZh}
                  </Paragraph>
                ),
              },
            ]}
          />
        ) : null}
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__context-tags">
            <Tag>page {block.page}</Tag>
            {block.source_layout_ids.slice(0, 3).map((layoutId) => (
              <Tag key={`${block.segment_id}-${layoutId}`}>{layoutId}</Tag>
            ))}
          </div>
        ) : null}
      </section>
    )
  }

  if (block.segment_kind === 'heading') {
    return (
      <section key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className={`${blockClassName} page-artifact-v2__heading-block page-artifact-v2__anchor-target`}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--support" />
            <span className="page-artifact-v2__block-label">阅读引导</span>
          </div>
        ) : null}
        <Title level={3} className="page-artifact-v2__heading-text">{block.text}</Title>
      </section>
    )
  }

  if (block.segment_kind === 'paragraph' || block.segment_kind === 'authored_explanation') {
    return (
      <section key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className={`${blockClassName} page-artifact-v2__paragraph page-artifact-v2__anchor-target`}>
        {mode === 'reader' ? (
          <div className="page-artifact-v2__teaching-cue">
            <span className="page-artifact-v2__teaching-badge">AI</span>
            <span className="page-artifact-v2__teaching-label">讲读拆解</span>
          </div>
        ) : null}
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot" />
            <span className="page-artifact-v2__block-label">讲解推进</span>
          </div>
        ) : null}
        <Paragraph className="page-artifact-v2__explanation-text">{block.text}</Paragraph>
      </section>
    )
  }

  return (
    <section key={block.segment_id} id={buildReaderBlockAnchorId(block.segment_id)} className={`${blockClassName} page-artifact-v2__anchor-target`}>
      <div className="page-artifact-v2__block-eyebrow">
        <span className="page-artifact-v2__block-dot" />
        <span className="page-artifact-v2__block-label">讲解推进</span>
      </div>
      <Paragraph className="page-artifact-v2__explanation-text">{block.text}</Paragraph>
      {mode === 'workbench' && Object.keys(meta).length ? (
        <div className="page-artifact-v2__context-tags">
          {block.source_lane === 'authoring_plan' ? <Tag>authoring_plan</Tag> : null}
          {meta.from ? <Tag>{String(meta.from)}</Tag> : null}
        </div>
      ) : null}
    </section>
  )
}

export default function PageArtifactV2Renderer(props: PageArtifactV2RendererProps) {
  const mode = props.mode || 'reader'
  const artifact = props.artifact
  const [activePreviewKey, setActivePreviewKey] = useState<'previous' | 'next' | null>(null)
  const [activeAskKey, setActiveAskKey] = useState<string | null>(null)
  const [askThreads, setAskThreads] = useState<Record<string, ReaderAskThreadState>>({})
  const [askLoadingKey, setAskLoadingKey] = useState<string | null>(null)
  const [askStreamingAnswer, setAskStreamingAnswer] = useState('')
  const [askError, setAskError] = useState('')
  const [askLoadingHintIndex, setAskLoadingHintIndex] = useState(0)
  const [rewriteAnimation, setRewriteAnimation] = useState<ReaderRewriteAnimationState | null>(null)
  const askAbortRef = useRef<AbortController | null>(null)
  const spineMeta = (artifact.current_page_spine?.meta || {}) as Record<string, unknown>
  const excerptCoverageMeta = (spineMeta.excerpt_coverage || {}) as Record<string, unknown>
  const artifactMeta = (artifact.meta || {}) as Record<string, unknown>
  const readerOpeningMeta = ((artifactMeta.reader_opening || {}) as Record<string, unknown>)
  const readerOutroMeta = ((artifactMeta.reader_outro || {}) as Record<string, unknown>)

  const derived = useMemo(() => {
    const readingBlocks = artifact.reading_blocks || []
    const supportBlocks = readingBlocks.filter((block) => SUPPORT_SEGMENT_KINDS.has(block.segment_kind))
    const explicitRailBlocks = readingBlocks.filter((block) => shouldRenderInRail(block))
    const shouldPromoteSupportBlocksToRail = mode === 'reader' && explicitRailBlocks.length === 0 && supportBlocks.length > 0
    const railBlocks = shouldPromoteSupportBlocksToRail ? supportBlocks : explicitRailBlocks
    const flowBlocks = readingBlocks.filter((block) => {
      if (shouldPromoteSupportBlocksToRail && SUPPORT_SEGMENT_KINDS.has(block.segment_kind)) {
        return false
      }
      return !shouldRenderInRail(block)
    })
    const headingBlocks = flowBlocks.filter((block) => block.segment_kind === 'heading')
    const paragraphBlocks = flowBlocks.filter((block) => block.segment_kind === 'paragraph')
    const explanationBlocks = flowBlocks.filter((block) => (
      block.segment_kind === 'paragraph' || block.segment_kind === 'authored_explanation'
    ))
    const excerptBlocks = flowBlocks.filter((block) => block.segment_kind === 'original_excerpt')
    const mediaBlocks = flowBlocks.filter((block) => (
      block.segment_kind === 'figure_slot'
      || block.segment_kind === 'table_slot'
      || block.segment_kind === 'equation_slot'
      || block.segment_kind === 'media_slot'
    ))
    const firstHeading = headingBlocks[0]
    const firstExplanation = explanationBlocks[0]
    const firstExcerpt = excerptBlocks[0]
    const firstParagraph = paragraphBlocks[0]
    const mainSegmentIds = new Set(artifact.current_page_spine?.main_segment_ids || [])
    const useSideRail = railBlocks.length > 0

    const mainBlockGroups: MainBlockGroup[] = []
    let fallbackGroupIndex = 0
    let currentGroup: MainBlockGroup | null = null
    const groupIdCounts = new Map<string, number>()

    for (const block of flowBlocks) {
      // 后端可能提供 heading 或显式 group ID；两者都不存在时，
      // 将后续正文保留在当前 group，保证渲染稳定。
      const meta = block.meta || {}
      const explicitGroupId = String(meta.group_id || meta.section_id || '').trim()
      const explicitGroupLabel = String(meta.group_label || meta.section_label || '').trim()
      const shouldStartNewGroup = block.segment_kind === 'heading' || Boolean(explicitGroupId) || currentGroup === null

      if (shouldStartNewGroup) {
        const baseGroupId = explicitGroupId || `group-${++fallbackGroupIndex}`
        const seenCount = groupIdCounts.get(baseGroupId) || 0
        groupIdCounts.set(baseGroupId, seenCount + 1)
        const nextGroupId = seenCount === 0 ? baseGroupId : `${baseGroupId}-${seenCount + 1}`
        currentGroup = {
          groupId: nextGroupId,
          groupLabel: explicitGroupLabel,
          heading: block.segment_kind === 'heading' ? block : null,
          blocks: block.segment_kind === 'heading' ? [] : [block],
        }
        mainBlockGroups.push(currentGroup)
        continue
      }

      if (!currentGroup) {
        continue
      }
      currentGroup.blocks.push(block)
    }

    const localExcerptBySegmentId: Record<string, PageArtifactV2ReadingBlock | null> = {}
    for (const group of mainBlockGroups) {
      const groupFirstExcerpt = group.blocks.find((item) => item.segment_kind === 'original_excerpt') || null
      let lastExcerpt: PageArtifactV2ReadingBlock | null = null
      for (const block of group.blocks) {
        // 段落级 ask 操作应引用最近的 source excerpt；当顺序噪声较大时，
        // 回退到 group 中的第一个 excerpt。
        if (block.segment_kind === 'original_excerpt') {
          lastExcerpt = block
          continue
        }
        if (block.segment_kind === 'paragraph' || block.segment_kind === 'authored_explanation') {
          localExcerptBySegmentId[block.segment_id] = lastExcerpt || groupFirstExcerpt
        }
      }
    }

    return {
      flowBlocks,
      mainBlockGroups,
      supportBlocks,
      railBlocks,
      headingBlocks,
      paragraphBlocks,
      explanationBlocks,
      excerptBlocks,
      mediaBlocks,
      firstHeading,
      firstParagraph,
      firstExplanation,
      firstExcerpt,
      mainSegmentIds,
      localExcerptBySegmentId,
      useSideRail,
    }
  }, [artifact, mode])

  useEffect(() => () => {
    askAbortRef.current?.abort()
  }, [])

  useEffect(() => {
    const marker = props.recentRewriteMarker
    if (!marker) return
    const target = (artifact.reading_blocks || []).find((item) => item.segment_id === marker.blockId)
    if (!target || !canRewriteExperienceBlock(target)) return
    const fullText = String(target.text || '').trim()
    if (!fullText) return

    // 只动画刚改写的 block。nonce 用来防止上一次改写的延迟 timer
    // 覆盖更新后的渲染文本。
    let frameIndex = 0
    const step = Math.max(1, Math.ceil(fullText.length / 36))
    setRewriteAnimation({
      blockId: marker.blockId,
      visibleText: '',
      isTyping: true,
      highlight: true,
      nonce: marker.nonce,
    })

    const intervalId = window.setInterval(() => {
      frameIndex = Math.min(fullText.length, frameIndex + step)
      const nextText = fullText.slice(0, frameIndex)
      setRewriteAnimation((current) => {
        if (!current || current.nonce !== marker.nonce) return current
        return {
          ...current,
          visibleText: nextText,
          isTyping: frameIndex < fullText.length,
        }
      })
      if (frameIndex >= fullText.length) {
        window.clearInterval(intervalId)
      }
    }, 24)

    const settleTimer = window.setTimeout(() => {
      setRewriteAnimation((current) => {
        if (!current || current.nonce !== marker.nonce) return current
        return {
          ...current,
          visibleText: fullText,
          isTyping: false,
          highlight: false,
        }
      })
    }, Math.max(1100, Math.min(2600, fullText.length * 22)))

    return () => {
      window.clearInterval(intervalId)
      window.clearTimeout(settleTimer)
    }
  }, [artifact.reading_blocks, props.recentRewriteMarker])

  useEffect(() => {
    if (!askLoadingKey) {
      setAskLoadingHintIndex(0)
      return
    }
    setAskLoadingHintIndex(0)
    const timerId = window.setInterval(() => {
      setAskLoadingHintIndex((current) => current + 1)
    }, 1500)
    return () => window.clearInterval(timerId)
  }, [askLoadingKey])

  const getDisplayedBlock = (block: PageArtifactV2ReadingBlock): PageArtifactV2ReadingBlock => {
    if (!rewriteAnimation || rewriteAnimation.blockId !== block.segment_id) return block
    return {
      ...block,
      text: rewriteAnimation.isTyping
        ? (rewriteAnimation.visibleText || ' ')
        : String(block.text || ''),
    }
  }

  const isRewriteHighlighted = (block: PageArtifactV2ReadingBlock | null | undefined): boolean => {
    if (!block || !rewriteAnimation) return false
    return rewriteAnimation.blockId === block.segment_id && rewriteAnimation.highlight
  }

  const heroTitle =
    cleanLeadCopy(derived.firstHeading?.text || '')
    || cleanLeadCopy(derived.firstParagraph?.text || '')
    || `第 ${artifact.focus_page} 页的阅读主线`
  const heroSubtitle = cleanLeadCopy(String(readerOpeningMeta.summary || derived.firstParagraph?.text || derived.firstExplanation?.text || ''))
  const readerOpeningPoints = getStringList(readerOpeningMeta.key_points, 4, 180)
  const previousPageBridge = getReaderBridge(readerOpeningMeta.previous_page_bridge)
  const nextPageBridge = getReaderBridge(readerOutroMeta.next_page_bridge)
  const previousPagePreview = getReaderNeighborPreview(readerOpeningMeta.previous_page_preview)
  const nextPagePreview = getReaderNeighborPreview(readerOutroMeta.next_page_preview)
  const heroContext = buildReaderBridgeSummary(previousPageBridge, 'previous')
  const previousBridgeLabel = previousPageBridge?.page
    ? `承接第 ${previousPageBridge.page} 页`
    : '承接上一页'
  const outroSummary = buildReaderBridgeSummary(nextPageBridge, 'next')
  const heroQuote = mode === 'reader' && readerOpeningPoints.length
    ? ''
    : String(derived.firstExcerpt?.text || '').trim()
  const continuityCount = artifact.provenance?.adjacent_context_pages?.length || 0
  const coverageRatio = Number(spineMeta.coverage_ratio || excerptCoverageMeta.coverage_ratio || 0)
  const presentationToken = toClassToken(artifact.presentation_mode)
  const templateToken = toClassToken(artifact.template_id)
  const layoutToken = toClassToken(artifact.layout_recipe)
  const useEditorialFlow = artifact.presentation_mode === 'editorial' || artifact.layout_recipe.includes('editorial')
  const useMixedLayout = artifact.presentation_mode === 'mixed_layout' || artifact.layout_recipe.includes('interleave')
  const useGuidedFlow = artifact.presentation_mode === 'guided_reading' || artifact.template_id.startsWith('guided_') || artifact.interaction_policy.includes('guided')
  const readerHeroNotes = (heroContext || readerOpeningPoints.length) ? [] : [
    derived.mediaBlocks[0] ? `先抓住${String(derived.mediaBlocks[0].meta?.label || derived.mediaBlocks[0].text || '当前页图证').trim()}` : '',
    derived.excerptBlocks[0] ? '顺着短原文片段读解释，不再把摘录堆成整块' : '',
    derived.railBlocks.length ? '补充说明压在侧边，不打断正文推进' : '',
  ].filter(Boolean)
  const hasHeroRail = mode === 'workbench' || readerHeroNotes.length > 0
  const normalizedHeroTitle = normalizeTextKey(heroTitle)
  const rootClassName = [
    'page-artifact-v2',
    `page-artifact-v2--mode-${mode}`,
    `page-artifact-v2--template-${templateToken}`,
    `page-artifact-v2--layout-${layoutToken}`,
    `page-artifact-v2--presentation-${presentationToken}`,
    `page-artifact-v2--widget-${toClassToken(artifact.widget_family)}`,
    `page-artifact-v2--motion-${toClassToken(artifact.motion_preset)}`,
    `page-artifact-v2--interaction-${toClassToken(artifact.interaction_policy)}`,
    useEditorialFlow ? 'page-artifact-v2--editorial-flow' : '',
    useMixedLayout ? 'page-artifact-v2--mixed-flow' : '',
    useGuidedFlow ? 'page-artifact-v2--guided-flow' : '',
      ].join(' ')
  const heroEyebrow = mode === 'workbench' ? 'Artifact Snapshot' : ''
  const readerActionChips = useMemo(() => {
    if (mode !== 'reader') return [] as ReaderActionChip[]

    const chips: ReaderActionChip[] = []
    const seen = new Set<string>()
    const pushChip = (chip: ReaderActionChip | null) => {
      if (!chip) return
      const dedupeKey = chip.href || chip.key
      if (seen.has(dedupeKey)) return
      seen.add(dedupeKey)
      chips.push(chip)
    }

    pushChip({
      key: 'opening',
      label: '回到本页要点',
      href: '#reader-opening',
      kind: 'anchor',
      tone: 'focus',
    })

    if (derived.firstExplanation) {
      pushChip({
        key: 'teaching',
        label: '进入讲读正文',
        href: `#${buildReaderBlockAnchorId(derived.firstExplanation.segment_id)}`,
        kind: 'anchor',
        tone: 'focus',
      })
    }

    if (derived.mediaBlocks[0]) {
      pushChip({
        key: 'media',
        label: getReaderMediaChipLabel(derived.mediaBlocks[0]),
        href: `#${buildReaderBlockAnchorId(derived.mediaBlocks[0].segment_id)}`,
        kind: 'anchor',
        tone: 'focus',
      })
    }

    if (derived.firstExcerpt) {
      pushChip({
        key: 'excerpt',
        label: '查看关键原文',
        href: `#${buildReaderBlockAnchorId(derived.firstExcerpt.segment_id)}`,
        kind: 'anchor',
        tone: 'focus',
      })
    }

    if (derived.railBlocks[0] || derived.supportBlocks[0]) {
      const supportBlock = derived.railBlocks[0] || derived.supportBlocks[0]
      pushChip({
        key: 'support',
        label: getReaderSupportChipLabel(supportBlock),
        href: `#${buildReaderBlockAnchorId(supportBlock.segment_id)}`,
        kind: 'anchor',
        tone: 'focus',
      })
    }

    const previousTargetPage = previousPagePreview?.page || previousPageBridge?.page || 0
    if (props.navigation && previousTargetPage) {
      pushChip({
        key: 'previous-page',
        label: `回看第 ${previousTargetPage} 页`,
        previewKey: 'previous',
        previewKicker: previousTargetPage ? `第 ${previousTargetPage} 页页面快照` : '上一页页面快照',
        previewSummary: previousPagePreview?.summary || buildReaderBridgeSummary(previousPageBridge, 'previous'),
        previewPoints: previousPagePreview?.keyPoints?.length ? previousPagePreview.keyPoints : previousPageBridge?.keyPoints,
        href: buildExperienceV2PageHref(props.navigation, previousTargetPage, { cacheOnly: true }),
        kind: 'preview',
        tone: 'navigate',
      })
    }

    const nextTargetPage = nextPagePreview?.page || nextPageBridge?.page || 0
    if (props.navigation && nextTargetPage) {
      pushChip({
        key: 'next-page',
        label: `预看第 ${nextTargetPage} 页`,
        previewKey: 'next',
        previewKicker: nextTargetPage ? `第 ${nextTargetPage} 页页面快照` : '下一页页面快照',
        previewSummary: nextPagePreview?.summary || outroSummary,
        previewPoints: nextPagePreview?.keyPoints?.length ? nextPagePreview.keyPoints : nextPageBridge?.keyPoints,
        href: buildExperienceV2PageHref(props.navigation, nextTargetPage, { cacheOnly: true }),
        kind: 'preview',
        tone: 'navigate',
      })
    }

    return chips.slice(0, 10)
  }, [derived.firstExcerpt, derived.firstExplanation, derived.mediaBlocks, derived.railBlocks, derived.supportBlocks, mode, nextPageBridge, nextPagePreview, outroSummary, previousPageBridge, previousPagePreview, props.navigation])
  const activePreviewChip = useMemo(
    () => readerActionChips.find((chip) => chip.kind === 'preview' && chip.previewKey === activePreviewKey) || null,
    [activePreviewKey, readerActionChips],
  )
  const activePreviewContent = useMemo(
    () => splitReaderPreviewPoints(activePreviewChip?.previewPoints || []),
    [activePreviewChip],
  )
  const activeAskThread = activeAskKey ? (askThreads[activeAskKey] || getInitialAskThreadState()) : null
  const canUseAskActions = mode === 'reader' && Number(props.navigation?.paperId || 0) > 0
  const canUseRewriteActions = mode === 'reader' && typeof props.onRewriteBlockRequest === 'function'

  const getBlockAskChip = (block: PageArtifactV2ReadingBlock): ReaderBlockAskChip | null => {
    if (!canUseAskActions) return null

    if (block.segment_kind === 'paragraph' || block.segment_kind === 'authored_explanation') {
      const excerptBlock = derived.localExcerptBySegmentId[block.segment_id] || null
      return {
        key: `ask-simplify-${block.segment_id}`,
        label: '更通俗地解释',
        title: '更通俗地解释这一段',
        question: buildReaderAskDisplayQuestion('simplify', block),
        displayQuestion: '请把这一段讲得更通俗一点',
        placeholder: '继续追问这段里哪里还不够通俗…',
        targetSegmentId: block.segment_id,
        explainKind: 'simplify',
        sourceExcerpt: excerptBlock?.text || '',
        sourceTranslationZh: excerptBlock ? getExcerptTranslation(excerptBlock) : '',
        explanationText: String(block.text || '').trim(),
      }
    }

    if (
      block.segment_kind === 'figure_slot'
      || block.segment_kind === 'media_slot'
      || block.segment_kind === 'table_slot'
      || block.segment_kind === 'equation_slot'
    ) {
      const mediaBinding = getMediaBinding(block)
      return {
        key: `ask-figure-${block.segment_id}`,
        label: block.segment_kind === 'table_slot' ? '只解释这个表' : '只解释这张图',
        title: buildReaderAskTitle('figure', block),
        question: buildReaderAskDisplayQuestion('figure', block),
        displayQuestion: buildReaderAskDisplayQuestion('figure', block),
        placeholder: '继续追问这个图表里某个细节…',
        targetSegmentId: block.segment_id,
        explainKind: 'figure',
        figureLabel: String(block.meta?.label || block.text || '').trim(),
        figureCaption: String(block.meta?.caption || block.meta?.description || '').trim(),
        figureText: String(block.text || '').trim(),
        figureImageUrl: String(mediaBinding?.page_asset_ref || mediaBinding?.page_image_url || '').trim(),
      }
    }

    return null
  }

  const updateAskThread = (chipKey: string, updater: (prev: ReaderAskThreadState) => ReaderAskThreadState) => {
    setAskThreads((prev) => {
      const current = prev[chipKey] || getInitialAskThreadState()
      return {
        ...prev,
        [chipKey]: updater(current),
      }
    })
  }

  const allReaderBlocks = useMemo(
    () => derived.mainBlockGroups.flatMap((group) => group.blocks).concat(derived.railBlocks),
    [derived.mainBlockGroups, derived.railBlocks],
  )
  const activeAskChip = activeAskKey
    ? allReaderBlocks
      .map((block) => getBlockAskChip(block))
      .find((chip) => chip?.key === activeAskKey) || null
    : null
  const isFigureFocusMode = Boolean(
    activeAskChip
    && activeAskKey === activeAskChip.key
    && activeAskChip.explainKind === 'figure',
  )

  const runAskChip = async (
    chip: ReaderBlockAskChip,
    question: string,
    displayQuestion: string,
  ) => {
    const paperId = Number(props.navigation?.paperId || 0)
    if (!chip.question || paperId <= 0) {
      setAskError('当前页面尚未就绪，暂时无法发起局部讲解。')
      return
    }
    if (chip.explainKind === 'figure' && !String(chip.figureImageUrl || '').trim()) {
      setAskError('当前图块没有可用图片 asset，无法只解释这张图。')
      return
    }

    const previousMessages = [...(askThreads[chip.key]?.messages || [])]
    const outboundHistory = previousMessages.slice(-MAX_BLOCK_EXPLAIN_HISTORY_MESSAGES)
    askAbortRef.current?.abort()
    const controller = new AbortController()
    askAbortRef.current = controller
    setAskError('')
    setAskStreamingAnswer('')
    setAskLoadingKey(chip.key)

    updateAskThread(chip.key, (prev) => ({
      ...prev,
      seeded: true,
      draft: '',
      messages: [
        ...prev.messages,
        {
          id: `user-${Date.now()}`,
          role: 'user',
          content: displayQuestion,
        },
      ],
    }))

    let aggregatedAnswer = ''

    try {
      const figureImageUrl = chip.explainKind === 'figure'
        ? (chip.figureImageUrl || undefined)
        : undefined
      await literatureApi.explainExperienceBlockStream(
        paperId,
        {
          page: Number(artifact.focus_page || 1),
          block_id: chip.targetSegmentId,
          explain_kind: chip.explainKind,
          question,
          source_excerpt: chip.sourceExcerpt || undefined,
          source_translation_zh: chip.sourceTranslationZh || undefined,
          explanation_text: chip.explanationText || undefined,
          figure_label: chip.figureLabel || undefined,
          figure_caption: chip.figureCaption || undefined,
          figure_text: chip.figureText || undefined,
          figure_image_url: figureImageUrl,
          history: outboundHistory.map((item) => ({
            role: item.role,
            content: item.content,
          })),
        },
        (event, data) => {
          if (event === 'token') {
            aggregatedAnswer += String(data?.text || '')
            setAskStreamingAnswer(aggregatedAnswer)
            return
          }
          if (event === 'done') {
            const answer = aggregatedAnswer.trim() || String((data as { answer?: string })?.answer || '').trim() || '暂无回答，请稍后重试。'
            updateAskThread(chip.key, (prev) => ({
              ...prev,
              messages: [
                ...prev.messages,
                {
                  id: `assistant-${Date.now()}`,
                  role: 'assistant',
                  content: answer,
                },
              ],
            }))
            setAskStreamingAnswer('')
            setAskLoadingKey((current) => (current === chip.key ? null : current))
            return
          }
          if (event === 'error') {
            const msg = String(data?.message || '局部讲解失败')
            setAskError(msg)
            setAskStreamingAnswer('')
            setAskLoadingKey((current) => (current === chip.key ? null : current))
          }
        },
        controller,
      )
    } catch (error: unknown) {
      if ((error as { name?: string })?.name === 'AbortError') return
      setAskError(error instanceof Error ? error.message : '局部讲解失败')
      setAskStreamingAnswer('')
      setAskLoadingKey((current) => (current === chip.key ? null : current))
    }
  }

  const handleAskChipClick = (chip: ReaderBlockAskChip) => {
    setActivePreviewKey(null)
    props.onRewriteBlockCancel?.()
    if (activeAskKey === chip.key) {
      setActiveAskKey(null)
      return
    }
    setActiveAskKey(chip.key)
    setAskError('')
    const thread = askThreads[chip.key]
      if (!thread?.seeded && chip.question) {
        void runAskChip(chip, chip.question, chip.displayQuestion || chip.label)
      }
  }

  const handleAskDraftChange = (chipKey: string, nextValue: string) => {
    updateAskThread(chipKey, (prev) => ({
      ...prev,
      draft: nextValue,
    }))
  }

  const handleAskFollowup = () => {
    if (!activeAskKey || !activeAskThread) return
    const nextDraft = String(activeAskThread.draft || '').trim()
    if (!nextDraft || askLoadingKey === activeAskKey) return
    const followupChip = derived.mainBlockGroups
      .flatMap((group) => group.blocks)
      .concat(derived.railBlocks)
      .map((block) => getBlockAskChip(block))
      .find((chip) => chip?.key === activeAskKey) || null
    if (!followupChip) return
    void runAskChip(followupChip, nextDraft, nextDraft)
  }

  const renderBlockActionSurface = (
    block: PageArtifactV2ReadingBlock,
    chip: ReaderBlockAskChip | null,
  ) => {
    const isRewritable = canUseRewriteActions && canRewriteExperienceBlock(block)
    if (!chip && !isRewritable) return null
    const isActive = Boolean(chip && activeAskKey === chip.key)
    const isLoading = Boolean(chip && askLoadingKey === chip.key)
    const isStreaming = isLoading && Boolean(askStreamingAnswer)
    const hasAssistantReply = Boolean(activeAskThread?.messages.some((item) => item.role === 'assistant'))
    const queryStage = isLoading ? (isStreaming ? 'streaming' : 'loading') : (hasAssistantReply ? 'settled' : 'ready')
    const loadingHint = chip ? getBlockExplainLoadingHint(chip.explainKind, askLoadingHintIndex) : ''
    const streamingHint = chip ? getBlockExplainStreamingHint(chip.explainKind) : ''
    const contextNote = chip ? getBlockExplainContextNote(chip.explainKind) : ''
    const loadingCues = chip ? READER_BLOCK_EXPLAIN_LOADING_CUES[chip.explainKind] : []
    const skeletonWidths = chip ? READER_BLOCK_EXPLAIN_SKELETON_WIDTHS[chip.explainKind] : []
    const isRewriteActive = props.activeRewriteBlockId === block.segment_id
    const isRewriting = props.rewritingBlockId === block.segment_id
    const normalizedRewriteDraft = String(props.rewriteDraft || '')
    return (
      <div
        className={[
          'page-artifact-v2__block-actions',
          chip ? `page-artifact-v2__block-actions--${chip.explainKind}` : '',
          isActive || isRewriteActive ? 'page-artifact-v2__block-actions--active' : '',
        ].filter(Boolean).join(' ')}
      >
        <div className="page-artifact-v2__block-actions-row">
          {chip ? (
            <button
              type="button"
              aria-pressed={isActive}
              onClick={() => handleAskChipClick(chip)}
              className={[
                'page-artifact-v2__inline-chip',
                `page-artifact-v2__inline-chip--${chip.explainKind}`,
                isLoading ? 'page-artifact-v2__inline-chip--busy' : '',
              ].filter(Boolean).join(' ')}
            >
              {chip.label}
            </button>
          ) : null}
          {isRewritable ? (
            <button
              type="button"
              aria-pressed={isRewriteActive}
              aria-busy={isRewriting}
              disabled={Boolean(props.rewriteDisabled || isRewriting)}
              onClick={() => {
                if (isRewriteActive) {
                  props.onRewriteBlockCancel?.()
                  return
                }
                setActiveAskKey(null)
                setAskError('')
                props.onRewriteBlockRequest?.(block)
              }}
              className={[
                'page-artifact-v2__inline-chip',
                'page-artifact-v2__inline-chip--simplify',
                'page-artifact-v2__inline-chip--rewrite',
                isRewriting ? 'page-artifact-v2__inline-chip--busy' : '',
              ].filter(Boolean).join(' ')}
            >
              {isRewriting ? '重写中' : '重写当前块'}
            </button>
          ) : null}
        </div>
        {isRewritable && isRewriteActive ? (
          <div
            className={[
              'page-artifact-v2__block-query',
              'page-artifact-v2__block-query--rewrite',
              isRewriting ? 'page-artifact-v2__block-query--streaming' : '',
            ].filter(Boolean).join(' ')}
          >
            <div className="page-artifact-v2__block-query-head">
              <div className="page-artifact-v2__block-query-kicker">{getRewriteInlineTitle(block)}</div>
              <div className="page-artifact-v2__block-query-status">
                只会改写当前块并覆盖当前 artifact cache。重新生成整页后，这次改写可能会被覆盖。
              </div>
            </div>
            {String(props.rewritePreviewText || '').trim() ? (
              <div className="page-artifact-v2__rewrite-preview">
                <div className="page-artifact-v2__rewrite-preview-label">当前内容</div>
                <div className="page-artifact-v2__rewrite-preview-copy">{props.rewritePreviewText}</div>
              </div>
            ) : null}
            <div className="page-artifact-v2__block-query-composer">
              <Input.TextArea
                value={normalizedRewriteDraft}
                onChange={(event) => props.onRewriteDraftChange?.(event.target.value)}
                autoSize={{ minRows: 3, maxRows: 6 }}
                maxLength={2000}
                showCount
                placeholder={props.rewritePromptPlaceholder || '例如：把这一段讲得更通俗，但保留关键概念和当前页语境。'}
                disabled={isRewriting}
              />
              <div className="page-artifact-v2__block-query-actions page-artifact-v2__block-query-actions--rewrite">
                <Button onClick={props.onRewriteBlockCancel} disabled={isRewriting}>
                  取消
                </Button>
                <Button
                  type="primary"
                  onClick={props.onRewriteSubmit}
                  loading={isRewriting}
                  disabled={!normalizedRewriteDraft.trim()}
                >
                  应用改写
                </Button>
              </div>
            </div>
          </div>
        ) : null}
        {chip && isActive ? (
          <div
            className={[
              'page-artifact-v2__block-query',
              `page-artifact-v2__block-query--${chip.explainKind}`,
              `page-artifact-v2__block-query--${queryStage}`,
            ].join(' ')}
          >
            <div className="page-artifact-v2__block-query-head">
              <div className="page-artifact-v2__block-query-kicker">{chip.title}</div>
              <div className="page-artifact-v2__block-query-status">
                {isLoading ? (isStreaming ? streamingHint : loadingHint) : contextNote}
              </div>
            </div>
            {askError ? (
              <Alert type="warning" showIcon message={askError} />
            ) : null}
            {activeAskThread?.messages.length ? (
              <div className="page-artifact-v2__block-query-messages">
                {activeAskThread.messages.map((messageItem) => (
                  <div
                    key={messageItem.id}
                    className={[
                      'page-artifact-v2__block-query-message',
                      `page-artifact-v2__block-query-message--${messageItem.role}`,
                      messageItem.role === 'assistant' ? `page-artifact-v2__block-query-message--assistant-${chip.explainKind}` : '',
                    ].filter(Boolean).join(' ')}
                  >
                    <div className="page-artifact-v2__block-query-message-role">
                      {messageItem.role === 'user' ? '你' : '讲读助手'}
                    </div>
                    {messageItem.role === 'assistant'
                      ? renderAskAssistantMarkdown(messageItem.content, chip.explainKind)
                      : (
                        <Paragraph className="page-artifact-v2__block-query-message-text">
                          {messageItem.content}
                        </Paragraph>
                      )}
                  </div>
                ))}
                {isLoading ? (
                  <div
                    className={[
                      'page-artifact-v2__block-query-message',
                      'page-artifact-v2__block-query-message--assistant',
                      'page-artifact-v2__block-query-message--pending',
                      `page-artifact-v2__block-query-message--assistant-${chip.explainKind}`,
                    ].join(' ')}
                  >
                    <div className="page-artifact-v2__block-query-message-role">讲读助手</div>
                    {isStreaming ? (
                      <Paragraph
                        className={[
                          'page-artifact-v2__block-query-message-text',
                          'page-artifact-v2__block-query-message-text--streaming',
                          `page-artifact-v2__block-query-message-text--${chip.explainKind}`,
                        ].join(' ')}
                      >
                        {askStreamingAnswer}
                      </Paragraph>
                    ) : (
                      <>
                        <div className="page-artifact-v2__block-query-phase">{loadingHint}</div>
                        <div className="page-artifact-v2__block-query-skeleton" aria-hidden="true">
                          {skeletonWidths.map((width, index) => (
                            <span
                              key={`${chip.key}-skeleton-${index}`}
                              className="page-artifact-v2__block-query-skeleton-line"
                              style={{ width }}
                            />
                          ))}
                        </div>
                        <div className="page-artifact-v2__block-query-cues" aria-hidden="true">
                          {loadingCues.map((cue) => (
                            <span key={`${chip.key}-${cue}`} className="page-artifact-v2__block-query-cue">
                              {cue}
                            </span>
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                ) : null}
              </div>
            ) : null}
            <div className="page-artifact-v2__block-query-composer">
              <Input.TextArea
                value={activeAskThread?.draft || ''}
                onChange={(event) => handleAskDraftChange(chip.key, event.target.value)}
                autoSize={{ minRows: 2, maxRows: 4 }}
                placeholder={chip.placeholder}
                disabled={askLoadingKey === chip.key}
              />
              <div className="page-artifact-v2__block-query-actions">
                <Button
                  type="primary"
                  onClick={handleAskFollowup}
                  loading={askLoadingKey === chip.key}
                  disabled={!String(activeAskThread?.draft || '').trim()}
                >
                  继续追问
                </Button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    )
  }
  return (
    <section className={rootClassName}>
      {mode === 'reader' && !hasHeroRail ? (
        <PageArtifactV2ReaderOpening
          title={heroTitle}
          summary={heroSubtitle}
          points={readerOpeningPoints}
          previousBridgeLabel={previousBridgeLabel}
          previousBridgeSummary={heroContext}
          previousBridgePoints={previousPageBridge?.keyPoints || []}
          quote={heroQuote}
          pageNumber={artifact.focus_page}
        />
      ) : (
        <ProCard className="page-artifact-v2__hero" bodyStyle={{ padding: 0 }}>
          <div className="page-artifact-v2__hero-surface">
            <div className="page-artifact-v2__hero-copy">
              {heroEyebrow ? (
                <span className="page-artifact-v2__eyebrow">{heroEyebrow}</span>
              ) : null}
              <Title className="page-artifact-v2__title">
                {mode === 'reader' ? heroTitle : `page_artifact_v2 · page ${artifact.focus_page}`}
              </Title>
              {heroSubtitle ? (
                <Paragraph className="page-artifact-v2__subtitle">{heroSubtitle}</Paragraph>
              ) : null}
              {mode === 'reader' && heroContext ? (
                <Paragraph className="page-artifact-v2__hero-context">{heroContext}</Paragraph>
              ) : null}
              {mode === 'reader' && readerOpeningPoints.length ? (
                <div className="page-artifact-v2__hero-points">
                  {readerOpeningPoints.map((item) => (
                    <div key={item} className="page-artifact-v2__hero-point">
                      <span className="page-artifact-v2__hero-point-dot" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              {heroQuote ? (
                <div className="page-artifact-v2__hero-quote">
                  {mode === 'workbench' ? (
                    <span className="page-artifact-v2__hero-quote-label">页内锚点</span>
                  ) : null}
                  <Paragraph className="page-artifact-v2__hero-quote-text">{heroQuote}</Paragraph>
                </div>
              ) : null}
            </div>

            {hasHeroRail ? (
              <aside className="page-artifact-v2__hero-rail">
                {mode === 'reader' ? (
                  <div className="page-artifact-v2__hero-notes">
                    {readerHeroNotes.map((item) => (
                      <div key={item} className="page-artifact-v2__hero-note">
                        <span className="page-artifact-v2__hero-note-dot" />
                        <span>{item}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <>
                    <span className="page-artifact-v2__hero-mode">
                      {artifact.presentation_mode.replace(/_/g, ' ')}
                    </span>
                    <div className="page-artifact-v2__hero-highlights">
                      <div className="page-artifact-v2__hero-pill">
                        <span className="page-artifact-v2__hero-pill-value">{derived.excerptBlocks.length}</span>
                        <span className="page-artifact-v2__hero-pill-label">原文锚点</span>
                      </div>
                      <div className="page-artifact-v2__hero-pill">
                        <span className="page-artifact-v2__hero-pill-value">{derived.mediaBlocks.length}</span>
                        <span className="page-artifact-v2__hero-pill-label">媒体位点</span>
                      </div>
                      <div className="page-artifact-v2__hero-pill">
                        <span className="page-artifact-v2__hero-pill-value">{derived.supportBlocks.length}</span>
                        <span className="page-artifact-v2__hero-pill-label">补充支撑</span>
                      </div>
                      <div className="page-artifact-v2__hero-pill">
                        <span className="page-artifact-v2__hero-pill-value">{continuityCount}</span>
                        <span className="page-artifact-v2__hero-pill-label">邻页上下文</span>
                      </div>
                    </div>
                    <div className="page-artifact-v2__contract-tags">
                      <Tag>{artifact.template_id}</Tag>
                      <Tag>{artifact.layout_recipe}</Tag>
                      <Tag>{artifact.widget_family}</Tag>
                      <Tag>{artifact.motion_preset}</Tag>
                      <Tag>{artifact.interaction_policy}</Tag>
                    </div>
                  </>
                )}
              </aside>
            ) : null}
          </div>
        </ProCard>
      )}

      <Layout className={`page-artifact-v2__layout ${derived.useSideRail ? 'page-artifact-v2__layout--with-side' : ''}`}>
        <Content className="page-artifact-v2__content-shell">
          <main className="page-artifact-v2__main">
            {derived.mainBlockGroups.map((group, index) => (
              <div key={group.groupId} className="page-artifact-v2__group-shell">
                <section className="page-artifact-v2__section-group">
                  {(() => {
                    const previousGroupLabel = normalizeTextKey(String(derived.mainBlockGroups[index - 1]?.groupLabel || ''))
                    const normalizedLabel = normalizeTextKey(String(group.groupLabel || ''))
                    const headingText = normalizeTextKey(String(group.heading?.text || ''))
                    const showGroupLabel = Boolean(
                      normalizedLabel
                      && normalizedLabel !== previousGroupLabel
                      && normalizedLabel !== headingText
                    )
                    return showGroupLabel ? (
                    <div className="page-artifact-v2__section-kicker">{group.groupLabel}</div>
                    ) : null
                  })()}
                  {(() => {
                    const headingText = normalizeTextKey(String(group.heading?.text || ''))
                    const shouldHideHeadingAsHeroDuplicate = Boolean(
                      mode === 'reader'
                      && group.heading
                      && index === 0
                      && headingText
                      && headingText === normalizedHeroTitle
                    )
                    const displayedHeading = group.heading ? getDisplayedBlock(group.heading) : null
                    return group.heading && !shouldHideHeadingAsHeroDuplicate ? (
                    <>
                      <header className={[
                        'page-artifact-v2__section-heading',
                        isRewriteHighlighted(group.heading) ? 'page-artifact-v2__section-heading--rewrite-flash' : '',
                      ].filter(Boolean).join(' ')}>
                        {mode === 'workbench' ? (
                          <div className="page-artifact-v2__block-eyebrow">
                            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--support" />
                            <span className="page-artifact-v2__block-label">阅读引导</span>
                          </div>
                        ) : null}
                        <Title level={3} className="page-artifact-v2__heading-text">
                          {displayedHeading?.text || group.heading.text}
                        </Title>
                      </header>
                      {mode === 'reader' ? renderBlockActionSurface(group.heading, null) : null}
                    </>
                    ) : null
                  })()}

                  <div className="page-artifact-v2__section-body">
                    {group.blocks.map((block) => {
                      const displayedBlock = getDisplayedBlock(block)
                      const askChip = mode === 'reader' ? getBlockAskChip(block) : null
                      const isFigureTarget = Boolean(
                        isFigureFocusMode
                        && block.segment_id === activeAskChip?.targetSegmentId,
                      )
                      return (
                        <div
                          key={block.segment_id}
                          className={[
                            'page-artifact-v2__main-item',
                            isFigureFocusMode && !isFigureTarget ? 'page-artifact-v2__main-item--figure-muted' : '',
                            isFigureTarget ? 'page-artifact-v2__main-item--figure-focus' : '',
                            isFigureTarget && askLoadingKey === activeAskChip?.key ? 'page-artifact-v2__main-item--figure-scanning' : '',
                            isRewriteHighlighted(block) ? 'page-artifact-v2__main-item--rewrite-flash' : '',
                          ].filter(Boolean).join(' ')}
                        >
                          {renderMainBlock(displayedBlock, derived.mainSegmentIds.has(block.segment_id), mode)}
                          {mode === 'reader' ? renderBlockActionSurface(block, askChip) : null}
                        </div>
                      )
                    })}
                  </div>
                </section>
                {useEditorialFlow && index < derived.mainBlockGroups.length - 1 ? <Divider className="page-artifact-v2__section-divider" /> : null}
              </div>
            ))}

          </main>
        </Content>

        {derived.useSideRail ? (
          <Sider width={320} theme="light" className="page-artifact-v2__side">
            <ProCard className="page-artifact-v2__side-shell" bodyStyle={{ padding: 18 }}>
              {mode === 'workbench' ? (
                <div className="page-artifact-v2__side-intro">
                  <span className="page-artifact-v2__side-kicker">inspection side rail</span>
                  <Text className="page-artifact-v2__side-copy">
                    这里保留支撑块、presentation contract 和 continuity 可视线索，便于检查 artifact 组成。
                  </Text>
                </div>
              ) : null}
              <div className="page-artifact-v2__side-stack">
                {derived.railBlocks.map((block) => {
                  const displayedBlock = getDisplayedBlock(block)
                  const askChip = mode === 'reader' ? getBlockAskChip(block) : null
                  return (
                    <div
                      key={block.segment_id}
                      className={[
                        'page-artifact-v2__side-item',
                        isRewriteHighlighted(block) ? 'page-artifact-v2__side-item--rewrite-flash' : '',
                      ].filter(Boolean).join(' ')}
                    >
                      {renderSupportCard(displayedBlock, mode, 'rail')}
                      {mode === 'reader' ? renderBlockActionSurface(block, askChip) : null}
                    </div>
                  )
                })}
              </div>
            </ProCard>
          </Sider>
        ) : null}
      </Layout>

      {mode === 'reader' && (nextPageBridge || readerActionChips.length) ? (
        <ProCard className="page-artifact-v2__outro" bodyStyle={{ padding: 18 }}>
          {nextPageBridge ? (
            <>
              <div className="page-artifact-v2__outro-kicker">
                {nextPageBridge.page ? `下一页 · 第 ${nextPageBridge.page} 页` : '下一页接续'}
              </div>
              {outroSummary ? (
                <Paragraph className="page-artifact-v2__outro-text">{outroSummary}</Paragraph>
              ) : null}
              {nextPageBridge.keyPoints.length ? (
                <div className="page-artifact-v2__outro-points">
                  {nextPageBridge.keyPoints.map((item) => (
                    <div key={item} className="page-artifact-v2__hero-note">
                      <span className="page-artifact-v2__hero-note-dot" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
          {readerActionChips.length ? (
            <div className="page-artifact-v2__outro-actions">
              {readerActionChips.map((chip) => (
                chip.kind === 'preview' ? (
                  <button
                    key={chip.key}
                    type="button"
                    aria-pressed={activePreviewKey === chip.previewKey}
                    onClick={() => {
                      setActiveAskKey(null)
                      setActivePreviewKey((current) => current === chip.previewKey ? null : (chip.previewKey || null))
                    }}
                    className={`page-artifact-v2__action-chip page-artifact-v2__action-chip--${chip.tone}`}
                  >
                    {chip.label}
                  </button>
                ) : (
                  <a
                    key={chip.key}
                    href={chip.href || '#'}
                    className={`page-artifact-v2__action-chip page-artifact-v2__action-chip--${chip.tone}`}
                  >
                    {chip.label}
                  </a>
                )
              ))}
            </div>
          ) : null}
          {activePreviewChip ? (
            <div className="page-artifact-v2__outro-preview">
              <div className="page-artifact-v2__outro-preview-kicker">
                {activePreviewChip.previewKicker}
              </div>
              {activePreviewChip.previewSummary ? (
                <Paragraph className="page-artifact-v2__outro-preview-text">
                  {activePreviewChip.previewSummary}
                </Paragraph>
              ) : null}
              {activePreviewContent.cues.length ? (
                <div className="page-artifact-v2__outro-preview-cues">
                  {activePreviewContent.cues.map((cue) => (
                    <span
                      key={cue.key}
                      className={`page-artifact-v2__outro-preview-cue page-artifact-v2__outro-preview-cue--${cue.tone}`}
                    >
                      {cue.label}
                    </span>
                  ))}
                </div>
              ) : null}
              {activePreviewContent.notes.length ? (
                <div className="page-artifact-v2__outro-preview-points">
                  {activePreviewContent.notes.map((item) => (
                    <div key={item} className="page-artifact-v2__hero-note">
                      <span className="page-artifact-v2__hero-note-dot" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              {activePreviewChip.href ? (
                <Link to={activePreviewChip.href} className="page-artifact-v2__outro-preview-link">
                  打开这一页（只读缓存）
                </Link>
              ) : null}
            </div>
              ) : null}
        </ProCard>
      ) : null}

      {mode === 'workbench' ? (
        <ProCard className="page-artifact-v2__inspector" bodyStyle={{ padding: '18px 20px' }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Title level={5} style={{ margin: 0 }}>Artifact inspector</Title>
            <Space wrap>
              <Tag>continuity: {artifact.provenance.continuity_mode}</Tag>
              <Tag>adjacent: {artifact.provenance.adjacent_context_pages.join(', ') || 'none'}</Tag>
              <Tag>main ids: {artifact.current_page_spine.main_segment_ids.length}</Tag>
              <Tag>coverage: {coverageRatio ? Math.round(coverageRatio * 100) : 0}%</Tag>
            </Space>
            <Paragraph style={{ margin: 0 }}>
              Reader route consumes the same artifact without exposing provenance by default; workbench keeps these checks visible.
            </Paragraph>
          </Space>
        </ProCard>
      ) : null}
    </section>
  )
}
