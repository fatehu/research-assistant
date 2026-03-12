import { Fragment, type CSSProperties, type ReactNode, useEffect, useState } from 'react'
import { Alert, Button, Card, Input, List, Space, Tag, Tooltip, Popover, Typography, message } from 'antd'
import { DownOutlined, DragOutlined, LinkOutlined, MoreOutlined, PlusOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'

import type {
  ReaderComponentAction,
  ReaderComponentNode,
  ReaderComponentSourceAnchor,
  ReaderComposeQualityReport,
} from '@/services/api'
import type { GenerativeStyleTokens } from '../generativeStyles'
import { isRegisteredReaderComponent, validateReaderComponentProps } from './registry'

const { Text, Title, Paragraph } = Typography

export type ReaderComponentRenderContext = {
  themeStyle?: GenerativeStyleTokens
  qualityReport?: ReaderComposeQualityReport | null
  readOnly?: boolean
  inlineQueryLoadingNodeId?: string | null
  resolveFigureImageUrl?: (imageUrl: string, node?: ReaderComponentNode) => string
  isActionableAnchor?: (anchor: ReaderComponentSourceAnchor) => boolean
  onJumpAnchor?: (
    anchors: ReaderComponentSourceAnchor[],
    options?: { pinPreview?: boolean; sourceBlockIds?: string[]; sourceAtomIds?: string[] },
  ) => void
  onPreviewAnchors?: (
    anchors: ReaderComponentSourceAnchor[],
    options?: { pinPreview?: boolean; sourceBlockIds?: string[]; sourceAtomIds?: string[] },
  ) => void
  onHidePreview?: () => void
  onInlineQuery?: (node: ReaderComponentNode, question: string) => Promise<void> | void
  onDropMarkdown?: (markdown: string, node?: ReaderComponentNode) => void
  onManualInsertSlot?: (nodeId: string) => void
  resolveAnchorPreviewImage?: (
    anchors: ReaderComponentSourceAnchor[],
    options?: { preferredPage?: number; segmentIndex?: number; sourceBlockIds?: string[]; sourceAtomIds?: string[] },
  ) => Promise<string | null>
}

function asString(value: unknown): string {
  return String(value ?? '').trim()
}

function asNumber(value: unknown, fallback: number): number {
  const num = Number(value)
  return Number.isFinite(num) ? num : fallback
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item || '').trim()).filter(Boolean)
}

function normalizeDoiHref(value: unknown): string {
  const doi = asString(value)
  if (!doi) return ''
  if (/^https?:\/\//i.test(doi)) return doi
  const trimmed = doi.replace(/^doi:\s*/i, '')
  return `https://doi.org/${trimmed}`
}

function deriveFigureSourceLabel(caption: string, sourceLabel: string): string {
  const explicit = asString(sourceLabel)
  if (explicit) return explicit
  const text = asString(caption)
  if (!text) return ''
  const matched = text.match(/^(Fig(?:ure)?\s*\d+[A-Za-z]?)/i)
  return matched ? matched[1] : ''
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
}

function asStringMatrix(value: unknown): string[][] {
  if (!Array.isArray(value)) return []
  return value
    .map((row) => (Array.isArray(row) ? row.map((cell) => String(cell ?? '').trim()) : []))
    .filter((row) => row.length > 0)
}

type TableRowEvidence = {
  rowIndex: number
  label: string
  sourceAtomIds: string[]
  anchor: ReaderComponentSourceAnchor | null
}

type TableCellShape = {
  cellId: number
  rowStart: number
  rowEnd: number
  colStart: number
  colEnd: number
  rowspan: number
  colspan: number
  text: string
  layoutIds: string[]
}

function asTableRowEvidence(value: unknown): TableRowEvidence[] {
  if (!Array.isArray(value)) return []
  const rows: TableRowEvidence[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const row = item as Record<string, unknown>
    const rowIndex = asNumber(row.row_index, -1)
    if (rowIndex < 0) continue
    rows.push({
      rowIndex,
      label: asString(row.label) || `Row ${rowIndex + 1}`,
      sourceAtomIds: asStringArray(row.source_atom_ids),
      anchor: row.anchor && typeof row.anchor === 'object' ? (row.anchor as ReaderComponentSourceAnchor) : null,
    })
  }
  return rows
}

function asTableCells(value: unknown): TableCellShape[] {
  if (!Array.isArray(value)) return []
  const cells: TableCellShape[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const row = item as Record<string, unknown>
    const rowStart = asNumber(row.row_start, -1)
    const rowEnd = asNumber(row.row_end, rowStart)
    const colStart = asNumber(row.col_start, -1)
    const colEnd = asNumber(row.col_end, colStart)
    if (rowStart < 0 || colStart < 0) continue
    cells.push({
      cellId: asNumber(row.cell_id, cells.length),
      rowStart,
      rowEnd: Math.max(rowStart, rowEnd),
      colStart,
      colEnd: Math.max(colStart, colEnd),
      rowspan: Math.max(1, asNumber(row.rowspan, Math.max(1, rowEnd - rowStart + 1))),
      colspan: Math.max(1, asNumber(row.colspan, Math.max(1, colEnd - colStart + 1))),
      text: asString(row.text),
      layoutIds: asStringArray(row.layout_ids),
    })
  }
  return cells
}

function buildEquationMarkdown(value: string): string {
  const latex = asString(value)
  if (!latex) return '$$x = y$$'
  if (
    (latex.startsWith('$$') && latex.endsWith('$$'))
    || (latex.startsWith('\\[') && latex.endsWith('\\]'))
  ) {
    return latex
  }
  return ['$$', latex, '$$'].join('\n')
}

type ParagraphSegment = {
  text: string
  source_char_ranges?: Array<{ start_char_id: string; end_char_id: string }>
}

function normalizeParagraphSegments(value: unknown): ParagraphSegment[] {
  if (!Array.isArray(value)) return []
  const rows: ParagraphSegment[] = []
  for (const item of value) {
    if (typeof item === 'string') {
      const text = asString(item)
      if (!text) continue
      rows.push({ text })
      continue
    }
    if (!item || typeof item !== 'object') continue
    const row = item as Record<string, unknown>
    const text = asString(row.text || row.content)
    if (!text) continue
    const segment: ParagraphSegment = { text }
    const ranges = row.source_char_ranges
    if (Array.isArray(ranges)) {
      const normalizedRanges = ranges
        .filter((rng): rng is { start_char_id: string; end_char_id: string } => (
          Boolean(rng)
          && typeof rng === 'object'
          && typeof (rng as any).start_char_id === 'string'
          && typeof (rng as any).end_char_id === 'string'
          && String((rng as any).start_char_id).trim().length > 0
          && String((rng as any).end_char_id).trim().length > 0
        ))
        .map((rng) => ({
          start_char_id: String(rng.start_char_id).trim(),
          end_char_id: String(rng.end_char_id).trim(),
        }))
      if (normalizedRanges.length > 0) {
        segment.source_char_ranges = normalizedRanges
      }
    }
    rows.push(segment)
  }
  return rows
}

function normalizeAnchorRows(value: unknown): ReaderComponentSourceAnchor[] {
  if (!Array.isArray(value)) return []
  const rows: ReaderComponentSourceAnchor[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const row = item as Record<string, unknown>
    const page = Number(row.page || 0)
    const startChar = Number(row.start_char || 0)
    const endChar = Number(row.end_char || 0)
    if (!Number.isFinite(page) || page <= 0) continue
    if (!Number.isFinite(startChar) || !Number.isFinite(endChar) || endChar <= startChar) continue
    rows.push({
      page,
      start_char: startChar,
      end_char: endChar,
      quote: typeof row.quote === 'string' ? row.quote : (typeof row.quote_text === 'string' ? row.quote_text : undefined),
      quote_text: typeof row.quote_text === 'string' ? row.quote_text : undefined,
      anchor_id: typeof row.anchor_id === 'string' ? row.anchor_id : undefined,
      segment_index: Number.isFinite(Number(row.segment_index)) ? Number(row.segment_index) : undefined,
      segment_total: Number.isFinite(Number(row.segment_total)) ? Number(row.segment_total) : undefined,
      bbox_hint: row.bbox_hint as ReaderComponentSourceAnchor['bbox_hint'],
      canonical_block_id: typeof row.canonical_block_id === 'string' ? row.canonical_block_id : undefined,
      source_layout_id: typeof row.source_layout_id === 'string' ? row.source_layout_id : undefined,
      coord_version: typeof row.coord_version === 'string' ? row.coord_version : undefined,
      anchor_confidence: Number.isFinite(Number(row.anchor_confidence)) ? Number(row.anchor_confidence) : undefined,
      anchor_v2: row.anchor_v2 as ReaderComponentSourceAnchor['anchor_v2'],
      geometry_version: typeof row.geometry_version === 'string' ? row.geometry_version : undefined,
      geometry: row.geometry as ReaderComponentSourceAnchor['geometry'],
      source_word_ids: Array.isArray(row.source_word_ids) ? row.source_word_ids.map((item) => String(item || '')).filter(Boolean) : undefined,
      source_char_ranges: Array.isArray(row.source_char_ranges)
        ? row.source_char_ranges
          .filter((item): item is { start_char_id: string; end_char_id: string } => (
            Boolean(item)
            && typeof item === 'object'
            && typeof (item as any).start_char_id === 'string'
            && typeof (item as any).end_char_id === 'string'
          ))
        : undefined,
    })
  }
  return rows
}

const ACTIONABLE_ANCHOR_MIN_CONFIDENCE = 0.78

function isNodeGatePassed(node: ReaderComponentNode): boolean {
  const props = (node?.props && typeof node.props === 'object')
    ? node.props as Record<string, unknown>
    : {}
  return props.node_gate_passed !== false
}

function isJumpableAnchor(
  anchor: ReaderComponentSourceAnchor,
  customPredicate?: (anchor: ReaderComponentSourceAnchor) => boolean,
): boolean {
  if (typeof customPredicate === 'function') {
    return customPredicate(anchor)
  }
  const start = Number(anchor.start_char || 0)
  const end = Number(anchor.end_char || 0)
  if (end <= start) return false
  if (Number(anchor.segment_index || 0) > 0 || Number(anchor.segment_total || 0) > 0) return false
  const canonicalBlockId = String(anchor.canonical_block_id || '').trim()
  if (!canonicalBlockId) return false
  const coordVersion = String(anchor.coord_version || anchor.anchor_v2?.coord_version || '').trim()
  if (coordVersion !== 'anchor_v2') return false
  const confidence = Number(anchor.anchor_confidence || 0)
  if (confidence > 0 && confidence < ACTIONABLE_ANCHOR_MIN_CONFIDENCE) return false
  return true
}

function baseCardStyle(ctx?: ReaderComponentRenderContext): CSSProperties {
  const isDark = ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44')
  return {
    borderRadius: 16,
    border: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
    boxShadow: isDark ? '0 8px 32px rgba(0, 0, 0, 0.4)' : '0 12px 32px rgba(11, 18, 32, 0.05)',
    background: ctx?.themeStyle?.panelBackground || '#ffffff',
    overflow: 'hidden',
  }
}

function isDarkTheme(ctx?: ReaderComponentRenderContext): boolean {
  return Boolean(ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44'))
}

function cardSurfaceStyles(
  ctx?: ReaderComponentRenderContext,
  options?: { bodyPadding?: number | string; headerPadding?: number | string; emphasis?: 'default' | 'muted' },
): { header: CSSProperties; body: CSSProperties } {
  const dark = isDarkTheme(ctx)
  const emphasis = options?.emphasis || 'default'
  const headerPadding = options?.headerPadding ?? '14px 18px'
  const bodyPadding = options?.bodyPadding ?? '18px 18px'
  const borderColor = ctx?.themeStyle?.borderColor || (dark ? 'rgba(226, 232, 240, 0.14)' : 'rgba(9, 30, 66, 0.08)')
  const headerBg = dark
    ? (emphasis === 'muted' ? 'rgba(255, 255, 255, 0.03)' : 'rgba(10, 18, 34, 0.9)')
    : (emphasis === 'muted' ? 'rgba(15, 23, 42, 0.025)' : 'rgba(255, 255, 255, 0.96)')
  const bodyBg = dark
    ? (emphasis === 'muted' ? 'rgba(255, 255, 255, 0.02)' : 'rgba(11, 20, 38, 0.86)')
    : '#ffffff'
  return {
    header: {
      padding: headerPadding,
      minHeight: 0,
      borderBottom: `1px solid ${borderColor}`,
      background: headerBg,
      color: ctx?.themeStyle?.headingColor || (dark ? '#f5f8ff' : '#17263c'),
    },
    body: {
      padding: bodyPadding,
      background: bodyBg,
      color: ctx?.themeStyle?.bodyColor || (dark ? '#eef2ff' : '#17263c'),
    },
  }
}

export function componentToMarkdown(node: ReaderComponentNode): string {
  const props = node.props || {}
  const text = (key: string) => asString((props as Record<string, unknown>)[key])
  if (node.type === 'ParagraphProse') {
    const paragraphs = normalizeParagraphSegments((props as Record<string, unknown>).paragraphs)
    if (paragraphs.length > 0) {
      return paragraphs.map((item) => item.text).join('\n\n')
    }
    return text('text')
  }
  if (node.type === 'SectionHeading') return `## ${text('text')}`
  if (node.type === 'Separator') return '---'
  if (node.type === 'KeyTakeaways') {
    const items = asRecordArray((props as Record<string, unknown>).items)
    if (items.length > 0) {
      return items.map((item) => `- ${asString(item.text || item.title || item.value)}`).join('\n')
    }
    return asStringArray((props as Record<string, unknown>).items).map((item) => `- ${item}`).join('\n')
  }
  if (node.type === 'TablePanel') {
    const title = text('title') || '表格'
    const matrix = asStringMatrix((props as Record<string, unknown>).matrix)
    const headerRowCount = asNumber((props as Record<string, unknown>).header_row_count, 0)
    const rows = asRecordArray((props as Record<string, unknown>).rows)
    const rawMarkdown = text('raw_markdown')
    if (matrix.length > 0) {
      const normalized = matrix.map((row) => row.filter((cell, idx) => idx < row.length))
      const header = normalized[0] || []
      const body = normalized.slice(Math.max(1, headerRowCount || 1))
      const headerRow = `| ${header.join(' | ')} |`
      const sepRow = `| ${header.map(() => '---').join(' | ')} |`
      const bodyRows = body.map((row) => `| ${row.join(' | ')} |`)
      return [`### ${title}`, '', headerRow, sepRow, ...bodyRows].join('\n')
    }
    if (!rows.length) return rawMarkdown ? `### ${title}\n\n${rawMarkdown}` : `### ${title}\n\n(暂无结构化行数据)`
    const headers = asStringArray((props as Record<string, unknown>).headers)
    const orderedHeaders = headers.length > 0 ? headers : Object.keys(rows[0] || {})
    const rowKeys = orderedHeaders.map((_, index) => `col_${index + 1}`)
    const headerRow = `| ${orderedHeaders.join(' | ')} |`
    const sepRow = `| ${orderedHeaders.map(() => '---').join(' | ')} |`
    const bodyRows = rows.map((row) => `| ${rowKeys.map((key) => asString(row[key] ?? row[key.replace(/^col_/, '')] ?? '')).join(' | ')} |`)
    return [`### ${title}`, '', headerRow, sepRow, ...bodyRows].join('\n')
  }
  if (node.type === 'FigurePanel') {
    const caption = text('caption')
    const insight = text('ai_insight')
    return [`### 图表`, caption ? `- 图注：${caption}` : '', insight ? `- AI解读：${insight}` : ''].filter(Boolean).join('\n')
  }
  if (node.type === 'AnswerCard') {
    return [`### 问答`, `- 问题：${text('question')}`, `- 回答：${text('answer')}`].join('\n')
  }
  if (node.type === 'InsightClusterCard') {
    const items = asStringArray((props as Record<string, unknown>).items)
    return [`### ${text('title') || '关键洞察'}`, ...items.map((item) => `- ${item}`)].join('\n')
  }
  if (node.type === 'SectionBridgeCard') {
    return [`### ${text('title') || '章节承接'}`, text('text')].filter(Boolean).join('\n')
  }
  if (node.type === 'ContextRail') {
    const rows = asRecordArray((props as Record<string, unknown>).items)
    const lines = rows
      .map((row) => asString(row.text || row.label || row.value))
      .filter(Boolean)
      .slice(0, 12)
    return [`### 侧栏信息`, ...lines.map((line) => `- ${line}`)].join('\n')
  }
  if (node.type === 'CitationCard') {
    return [`### 引用文献: ${text('title')}`, `- 作者: ${asStringArray((props as Record<string, unknown>).authors).join(', ')}`, `- 年份: ${text('year')}`, `- 期刊: ${text('journal')}`, `- DOI: ${text('doi')}`, text('abstract_tldr') ? `- TL;DR: ${text('abstract_tldr')}` : ''].filter(Boolean).join('\n')
  }
  if (node.type === 'EquationBlock') {
    return [`$$`, text('latex'), `$$`, text('description') ? `*注: ${text('description')}*` : ''].filter(Boolean).join('\n')
  }
  if (node.type === 'MethodologyCard') {
    const steps = asStringArray((props as Record<string, unknown>).steps)
    return [`### 研究方法: ${text('title') || '实验设计'}`, ...steps.map((s, i) => `${i + 1}. ${s}`), text('participants') ? `*参与对象: ${text('participants')}*` : '', text('tools') ? `*工具: ${asStringArray((props as Record<string, unknown>).tools).join(', ')}*` : ''].filter(Boolean).join('\n')
  }
  if (node.type === 'CalloutBox') {
    const emoji = { info: 'ℹ️', warning: '⚠️', success: '✅', tip: '💡' }[asString((props as Record<string, unknown>).type)] || 'ℹ️'
    return [`> ${emoji} **${text('title') || '提示'}**`, `> ${text('content')}`].join('\n')
  }
  if (node.type === 'AbstractCard') {
    return [`### 摘要`, text('text')].join('\n')
  }
  return JSON.stringify(node.props || {}, null, 2)
}

function renderChildren(children: ReaderComponentNode[], ctx: ReaderComponentRenderContext): ReactNode {
  if (!children.length) return null
  return (
    <Fragment>
      {children.map((child) => (
        <Fragment key={child.id}>{renderReaderNode(child, ctx)}</Fragment>
      ))}
    </Fragment>
  )
}

function buildFallbackActions(node: ReaderComponentNode, ctx?: ReaderComponentRenderContext): ReaderComponentAction[] {
  const capabilities = new Set(
    asStringArray(node.capabilities)
      .map((item) => item.toLowerCase())
      .filter(Boolean),
  )
  const hasCapabilityFilter = capabilities.size > 0
  const nodeGatePassed = isNodeGatePassed(node)
  const anchors = normalizeAnchorRows(node.source_anchor_refs)
    .filter((row) => nodeGatePassed && isJumpableAnchor(row, ctx?.isActionableAnchor))
  const allowByCapability = (keys: string[], defaultAllow = true): boolean => {
    if (!hasCapabilityFilter) return defaultAllow
    return keys.some((key) => capabilities.has(key))
  }

  const fallback: ReaderComponentAction[] = []
  if (
    node.type !== 'KeyTakeaways'
    && anchors.length > 0
    && allowByCapability(['jump_anchor', 'jump_to_anchor', 'locate_evidence'])
  ) {
    fallback.push({ key: 'jump_anchor', label: '定位到证据', kind: 'default' })
  }
  if (allowByCapability(['copy', 'copy_markdown', 'drag_markdown'])) {
    fallback.push({ key: 'copy_markdown', label: '复制Markdown', kind: 'default' })
  }
  return fallback
}

function canonicalActionKey(rawKey: string): string {
  const key = asString(rawKey).toLowerCase()
  if (key === 'jump_to_anchor' || key === 'locate_evidence') return 'jump_anchor'
  if (key === 'copy_markdown') return 'copy'
  return key
}

function mergeActionRows(
  rawActions: ReaderComponentAction[],
  fallbackActions: ReaderComponentAction[],
): ReaderComponentAction[] {
  const merged: ReaderComponentAction[] = []
  const seen = new Set<string>()
  for (const row of [...rawActions, ...fallbackActions]) {
    const key = canonicalActionKey(asString(row?.key))
    if (!key || seen.has(key)) continue
    seen.add(key)
    merged.push(row)
  }
  return merged
}

async function copyNodeMarkdown(markdown: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(markdown)
    message.success('已复制为 Markdown')
  } catch {
    message.warning('复制失败，请检查浏览器权限')
  }
}

function ActionBar(props: {
  node: ReaderComponentNode
  ctx: ReaderComponentRenderContext
  extraActions?: ReactNode
  placement?: 'default' | 'outer-left'
}): ReactNode {
  const { node, ctx, extraActions, placement = 'default' } = props
  const [open, setOpen] = useState(false)
  if (ctx.readOnly) return null
  const markdown = componentToMarkdown(node)
  const nodeGatePassed = isNodeGatePassed(node)
  const anchorRefs = normalizeAnchorRows(node.source_anchor_refs)
    .filter((row) => nodeGatePassed && isJumpableAnchor(row, ctx?.isActionableAnchor))
  const rawActions = Array.isArray(node.actions) ? node.actions : []
  const actionRows = mergeActionRows(rawActions, buildFallbackActions(node, ctx))
    .filter((row) => !(node.type === 'KeyTakeaways' && canonicalActionKey(asString(row.key)) === 'jump_anchor'))
    .filter((row) => !(canonicalActionKey(asString(row.key)) === 'jump_anchor' && anchorRefs.length === 0))
    .filter((row) => !(asString(row.key).toLowerCase() === 'preview_anchor' && anchorRefs.length === 0))
  const canJump = anchorRefs.length > 0
  const actionBtnStyle: CSSProperties = {
    color: ctx?.themeStyle?.bodyColor,
    borderColor: ctx?.themeStyle?.borderColor,
  }
  const onDragMarkdown = (event: React.DragEvent<HTMLElement>): void => {
    event.stopPropagation()
    const payload = JSON.stringify({ node, markdown })
    event.dataTransfer.setData('application/x-reader-component+json', payload)
    event.dataTransfer.setData('text/markdown', markdown)
    event.dataTransfer.setData('text/plain', markdown)
  }
  if (actionRows.length === 0 && !extraActions) return null
  const sourceBlockIds = Array.isArray(node.source_block_ids)
    ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  const sourceAtomIds = Array.isArray(node.source_atom_ids)
    ? node.source_atom_ids.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  const compactActionLabel = (key: string, fallback: string): string => {
    const normalized = canonicalActionKey(key)
    if (normalized === 'repair') return '修复'
    if (normalized === 'degrade') return '降级'
    if (normalized === 'copy') return '复制'
    if (normalized === 'jump_anchor') return '证据'
    if (normalized === 'preview_anchor') return '预览'
    if (normalized === 'open' || normalized === 'open_link') return '打开链接'
    return fallback
  }
  const actionMenu = (
    <div className="reader-action-menu">
      {actionRows.map((row, idx) => {
        const key = canonicalActionKey(asString(row.key))
        const label = compactActionLabel(key, asString(row.label) || key || `action-${idx + 1}`)
        const payload = (row.payload && typeof row.payload === 'object')
          ? row.payload as Record<string, unknown>
          : {}
        if (!key) return null
        if (key === 'jump_anchor') {
          return (
            <Button
              key={`${node.id}:jump:${idx}`}
              size="small"
              icon={<LinkOutlined />}
              style={actionBtnStyle}
              disabled={!canJump}
              onClick={() => ctx.onJumpAnchor?.(anchorRefs, { pinPreview: true, sourceBlockIds, sourceAtomIds })}
            >
              {label}
            </Button>
          )
        }
        if (key === 'copy') {
          return (
            <Button
              key={`${node.id}:copy:${idx}`}
              size="small"
              style={actionBtnStyle}
              onClick={() => copyNodeMarkdown(markdown)}
            >
              {label}
            </Button>
          )
        }
        if (key === 'preview_anchor') {
          return (
            <Button
              key={`${node.id}:preview:${idx}`}
              size="small"
              style={actionBtnStyle}
              disabled={!canJump}
              onClick={() => ctx.onPreviewAnchors?.(anchorRefs, { pinPreview: true, sourceBlockIds, sourceAtomIds })}
            >
              {label}
            </Button>
          )
        }
        const href = asString(payload.href)
        return (
          <Button
            key={`${node.id}:${key}:${idx}`}
            size="small"
            style={actionBtnStyle}
            disabled={!href}
            onClick={() => {
              if (!href) return
              window.open(href, '_blank', 'noopener,noreferrer')
            }}
          >
            {label}
          </Button>
        )
      })}
      <span draggable onDragStart={onDragMarkdown} style={{ display: 'inline-flex', cursor: 'grab' }}>
        <Button
          size="small"
          icon={<DragOutlined />}
          style={actionBtnStyle}
        >
          拖拽
        </Button>
      </span>
      {extraActions ? <div>{extraActions}</div> : null}
    </div>
  )
  const actionOverlayClassName = [
    'reader-composed-popover',
    'reader-node-action-popover',
    isDarkTheme(ctx) ? 'reader-node-action-popover--dark' : 'reader-node-action-popover--light',
  ].join(' ')
  return (
    <div className={`reader-action-bar${placement === 'outer-left' ? ' reader-action-bar--outer-left' : ''}${open ? ' reader-action-bar--open' : ''}`}>
      <Popover
        trigger="click"
        placement={placement === 'outer-left' ? 'rightTop' : 'rightTop'}
        overlayClassName={actionOverlayClassName}
        open={open}
        onOpenChange={setOpen}
        content={actionMenu}
      >
        <Button
          size="small"
          shape="circle"
          className="reader-action-trigger"
          icon={<MoreOutlined />}
          aria-label="更多段落操作"
        />
      </Popover>
    </div>
  )
}
function DraggableContainer(props: {
  node: ReaderComponentNode
  children: ReactNode
}): ReactNode {
  const { children } = props
  return (
    <div style={{ userSelect: 'text' }}>
      {children}
    </div>
  )
}

function InlineQuerySlotNode(props: {
  node: ReaderComponentNode
  ctx: ReaderComponentRenderContext
}): ReactNode {
  const { node, ctx } = props
  const [expanded, setExpanded] = useState(false)
  const [value, setValue] = useState('')
  if (!ctx.onInlineQuery) return null
  const loading = ctx.inlineQueryLoadingNodeId === node.id
  return (
    <Card size="small" style={{ ...baseCardStyle(ctx), margin: '8px 0' }}>
      {!expanded ? (
        <Button size="small" type="dashed" icon={<DownOutlined />} onClick={() => setExpanded(true)}>
          + 段落追问
        </Button>
      ) : (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Input.TextArea
            value={value}
            onChange={(event) => setValue(event.target.value)}
            rows={2}
            placeholder={asString((node.props || {}).placeholder) || '输入你的问题'}
          />
          <Space size={8}>
            <Button
              size="small"
              type="primary"
              loading={loading}
              disabled={!value.trim()}
              onClick={async () => {
                const question = value.trim()
                if (!question) return
                await ctx.onInlineQuery?.(node, question)
                setValue('')
                setExpanded(false)
              }}
            >
              发送
            </Button>
            <Button size="small" onClick={() => setExpanded(false)}>收起</Button>
          </Space>
        </Space>
      )}
    </Card>
  )
}

function EquationBlockNode(props: {
  node: ReaderComponentNode
  ctx: ReaderComponentRenderContext
  withAnchorPreview: (child: ReactNode) => ReactNode
}): ReactNode {
  const { node, ctx, withAnchorPreview } = props
  const latex = asString((node.props || {}).latex)
  const label = asString((node.props || {}).label)
  const description = asString((node.props || {}).description)
  const resolveAnchorPreviewImage = ctx.resolveAnchorPreviewImage
  const [evidenceImage, setEvidenceImage] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const anchorRefs = normalizeAnchorRows(node.source_anchor_refs)
    const sourceBlockIds = Array.isArray(node.source_block_ids)
      ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    const sourceAtomIds = Array.isArray(node.source_atom_ids)
      ? node.source_atom_ids.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    if (!resolveAnchorPreviewImage || anchorRefs.length === 0) {
      setEvidenceImage(null)
      return () => {
        cancelled = true
      }
    }
    void resolveAnchorPreviewImage(anchorRefs, {
      preferredPage: Number(anchorRefs[0]?.page || 0) || undefined,
      sourceBlockIds,
      sourceAtomIds,
    }).then((imageUrl) => {
      if (!cancelled) setEvidenceImage(imageUrl || null)
    }).catch(() => {
      if (!cancelled) setEvidenceImage(null)
    })
    return () => {
      cancelled = true
    }
  }, [
    resolveAnchorPreviewImage,
    node.source_anchor_refs,
    node.source_block_ids,
    node.source_atom_ids,
  ])

  return withAnchorPreview(
    <div style={{
      margin: '24px 0',
      padding: '16px',
      textAlign: 'center',
      backgroundColor: isDarkTheme(ctx) ? 'rgba(255,255,255,0.02)' : 'rgba(0,0,0,0.01)',
      borderRadius: 12,
      position: 'relative',
    }}>
      <ActionBar node={node} ctx={ctx} />
      {evidenceImage ? (
        <div
          style={{
            marginBottom: 12,
            padding: '10px 12px',
            borderRadius: 10,
            border: `1px solid ${ctx.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
            background: isDarkTheme(ctx) ? 'rgba(255,255,255,0.02)' : 'rgba(15, 23, 42, 0.025)',
          }}
        >
          <Text strong style={{ display: 'block', marginBottom: 8, color: ctx.themeStyle?.headingColor }}>
            公式证据
          </Text>
          <img
            src={evidenceImage}
            alt={label || 'equation evidence'}
            style={{ maxWidth: '100%', height: 'auto', borderRadius: 8 }}
          />
        </div>
      ) : null}
      <div style={{
        fontSize: 20,
        overflowX: 'auto',
        padding: '10px 0',
        color: ctx.themeStyle?.bodyColor,
      }}>
        <div style={{ display: 'inline-block', verticalAlign: 'middle', textAlign: 'left' }}>
          <ReactMarkdown
            remarkPlugins={[remarkMath]}
            rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: 'ignore' }] as any]}
            components={{
              p: ({ children }) => <>{children}</>,
            }}
          >
            {buildEquationMarkdown(latex)}
          </ReactMarkdown>
        </div>
        {label && (
          <span style={{
            position: 'absolute',
            right: 20,
            top: '50%',
            transform: 'translateY(-50%)',
            fontWeight: 'bold',
            color: ctx.themeStyle?.bodyColor,
            opacity: 0.6,
          }}>
            ({label})
          </span>
        )}
      </div>
      {description && (
        <div style={{
          marginTop: 12,
          fontSize: 13,
          color: ctx.themeStyle?.bodyColor,
          opacity: 0.78,
          lineHeight: 1.7,
          textAlign: 'left',
        }}>
          {description}
        </div>
      )}
      {renderChildren(node.children || [], ctx)}
    </div>,
  )
}

function ParagraphProseNode(props: {
  node: ReaderComponentNode
  ctx: ReaderComponentRenderContext
  withAnchorPreview: (child: ReactNode) => ReactNode
}): ReactNode {
  const { node, ctx, withAnchorPreview } = props
  const text = asString(node.props?.text)
  const paragraphs = normalizeParagraphSegments((node.props as Record<string, unknown>)?.paragraphs)
  const [hovered, setHovered] = useState(false)
  const paragraphStyle: CSSProperties = {
    margin: 0,
    lineHeight: ctx.themeStyle?.bodyLineHeight || 1.95,
    fontSize: ctx.themeStyle?.bodyFontSize || 18,
    textAlign: 'justify',
    color: ctx.themeStyle?.bodyColor,
    fontFamily: ctx.themeStyle?.bodyFontFamily,
  }
  const paragraphRows = paragraphs.length > 0
    ? paragraphs
    : (() => {
      if (!text) return []
      const blocks = text
        .split(/\n\s*\n+/)
        .map((item) => item.trim())
        .filter((item) => item.length > 0)
      if (blocks.length >= 2) {
        return blocks.map((item) => ({ text: item }))
      }
      return [{ text }]
    })()

  return withAnchorPreview(
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ position: 'relative', marginBottom: 14 }}
    >
      <span className="reader-node-hover-bridge" aria-hidden="true" />
      <ActionBar node={node} ctx={ctx} placement="outer-left" />
      <DraggableContainer node={node}>
        <>
          {paragraphRows.map((item, idx) => (
            <p
              key={`${node.id}-p-${idx}`}
              style={{
                ...paragraphStyle,
                margin: idx === 0 ? 0 : '10px 0 0 0',
              }}
            >
              {item.text}
            </p>
          ))}
          {renderChildren(node.children || [], ctx)}
        </>
      </DraggableContainer>

      {!ctx.readOnly ? (
        <div
          style={{
            position: 'absolute',
            bottom: -18,
            left: '50%',
            transform: hovered ? 'translate(-50%, 0) scale(1)' : 'translate(-50%, -10px) scale(0.9)',
            opacity: hovered ? 1 : 0,
            transition: 'all 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275)',
            zIndex: 10,
            pointerEvents: hovered ? 'auto' : 'none',
          }}
        >
          <Button
            type="primary"
            shape="circle"
            size="small"
            icon={<PlusOutlined />}
            onClick={() => ctx.onManualInsertSlot?.(String(node.id))}
            title="在此段落后插入段落追问"
            style={{ boxShadow: '0 4px 12px rgba(22, 119, 255, 0.35)' }}
          />
        </div>
      ) : null}
    </div>
  )
}

export function renderReaderNode(node: ReaderComponentNode, ctx: ReaderComponentRenderContext): ReactNode {
  if (!isRegisteredReaderComponent(String(node.type || ''))) {
    return (
      <Alert
        showIcon
        type="warning"
        message={`Unknown component: ${node.type}`}
        description="Component is not registered in reader registry."
      />
    )
  }
  const propsValidation = validateReaderComponentProps(String(node.type || ''), node.props || {})
  if (!propsValidation.ok) {
    return (
      <Alert
        showIcon
        type="warning"
        message={`Invalid props for ${node.type}`}
        description={propsValidation.error}
      />
    )
  }
  const props = propsValidation.props || {}
  const nodeGatePassed = isNodeGatePassed(node)
  const anchorRefs = normalizeAnchorRows(node.source_anchor_refs)
    .filter((row) => nodeGatePassed && isJumpableAnchor(row, ctx?.isActionableAnchor))

  const layoutStyle: React.CSSProperties = {}
  const minHeightOnlyNodeTypes = new Set([
    'FigurePanel',
    'TablePanel',
    'PdfSnippetCard',
  ])
  const currentNodeType = String(node.type || '').trim()
  if (node.layout_slot?.reserved_height && minHeightOnlyNodeTypes.has(currentNodeType)) {
    layoutStyle.minHeight = node.layout_slot.reserved_height
    if (node.layout_slot.lock_height && currentNodeType !== 'FigurePanel') {
      layoutStyle.height = node.layout_slot.reserved_height
      layoutStyle.overflow = 'hidden'
    }
  }

  const withAnchorPreview = (child: ReactNode): ReactNode => (
    <div
      className="reader-node-shell"
      data-reader-node-type={String(node.type || '')}
      style={layoutStyle}
      onMouseEnter={() => {
        if (anchorRefs.length > 0) {
          ctx.onPreviewAnchors?.(anchorRefs, {
            pinPreview: false,
            sourceBlockIds: Array.isArray(node.source_block_ids)
              ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
              : [],
            sourceAtomIds: Array.isArray(node.source_atom_ids)
              ? node.source_atom_ids.map((item) => String(item || '').trim()).filter(Boolean)
              : [],
          })
        } else {
          ctx.onHidePreview?.()
        }
      }}
      onMouseLeave={() => {
        ctx.onHidePreview?.()
      }}
    >
      {child}
    </div>
  )

  switch (node.type) {
    case 'PaperHeaderCard': {
      const title = asString(props.title)
      const venue = asString(props.venue)
      const year = asString(props.year)
      const authors = asStringArray(props.authors)
      return withAnchorPreview(
        <Card size="small" style={{ ...baseCardStyle(ctx), border: 'none' }}>
          <Title level={2} style={{ marginBottom: 10, color: ctx.themeStyle?.headingColor }}>{title || 'Untitled Paper'}</Title>
          <Space wrap>
            {venue ? <Tag color="geekblue">{venue}</Tag> : null}
            {year ? <Tag>{year}</Tag> : null}
            {authors.length > 0 ? <Tag color="blue">作者：{authors.slice(0, 4).join('、')}</Tag> : null}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>,
      )
    }

    case 'MetadataSidebarCard': {
      const items = asRecordArray(props.items)
      return (
        <Card size="small" title="元数据" style={baseCardStyle(ctx)} styles={cardSurfaceStyles(ctx, { bodyPadding: '16px 18px', emphasis: 'muted' })}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {items.map((item, idx) => (
              <div key={`meta-${idx}`}>
                <Text strong>{asString(item.label)}：</Text>
                <Text>{asString(item.value)}</Text>
              </div>
            ))}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'ContextRail': {
      const title = asString(props.title) || '侧栏信息'
      const rows = asRecordArray(props.items)
      const items = rows
        .map((row) => ({
          text: asString(row.text || row.label || row.value),
          anchor: normalizeAnchorRows(row.anchor)
            .filter((item) => isJumpableAnchor(item, ctx?.isActionableAnchor)),
        }))
        .filter((item) => item.text)
      const defaultCollapsed = props.default_collapsed !== false
      return (
        <Card size="small" title={title} style={baseCardStyle(ctx)} styles={cardSurfaceStyles(ctx, { bodyPadding: '14px 16px', emphasis: 'muted' })}>
          <details open={!defaultCollapsed}>
            <summary style={{ cursor: 'pointer', marginBottom: 10, color: ctx.themeStyle?.bodyColor }}>
              点击展开/收起侧栏上下文
            </summary>
            <List
              size="small"
              dataSource={items}
              renderItem={(item, idx) => (
                <List.Item key={`ctx-${idx}`}>
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    <Text style={{ color: ctx.themeStyle?.bodyColor }}>{item.text}</Text>
                    {item.anchor.length > 0 ? (
                      <Button
                        type="default"
                        size="small"
                        onClick={() => ctx.onJumpAnchor?.(item.anchor, { pinPreview: true })}
                        style={{
                          alignSelf: 'flex-start',
                          borderRadius: 999,
                          paddingInline: 10,
                          borderColor: isDarkTheme(ctx) ? 'rgba(149, 177, 255, 0.28)' : 'rgba(29, 78, 216, 0.18)',
                          background: isDarkTheme(ctx) ? 'rgba(88, 130, 255, 0.14)' : 'rgba(29, 78, 216, 0.06)',
                          color: isDarkTheme(ctx) ? '#e7efff' : '#1d4ed8',
                        }}
                      >
                        定位到证据
                      </Button>
                    ) : null}
                  </Space>
                </List.Item>
              )}
            />
          </details>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'SectionTOC': {
      // 按页目录卡已下线。兼容旧缓存时直接跳过渲染，避免出现“空目录占位”。
      return null
    }

    case 'SectionHeading': {
      const text = asString(props.text)
      const level = Math.max(1, Math.min(4, asNumber(props.level, 2)))
      const levelToSize = { 1: 34, 2: 30, 3: 24, 4: 20 }
      return withAnchorPreview(
        <div style={{ margin: '20px 0 8px', position: 'relative' }}>
          <span className="reader-node-hover-bridge" aria-hidden="true" />
          <ActionBar node={node} ctx={ctx} placement="outer-left" />
          <Title
            level={Math.min(5, level + 1) as 1 | 2 | 3 | 4 | 5}
            style={{
              marginBottom: 8,
              fontSize: levelToSize[level as 1 | 2 | 3 | 4],
              lineHeight: 1.2,
              letterSpacing: 0.2,
              color: ctx.themeStyle?.headingColor,
              fontFamily: ctx.themeStyle?.headingFontFamily,
            }}
          >
            {text}
          </Title>
          {renderChildren(node.children || [], ctx)}
        </div>,
      )
    }

    case 'Separator': {
      const tone = asString(props.tone).toLowerCase()
      const borderColor = tone === 'strong'
        ? (isDarkTheme(ctx) ? 'rgba(226, 232, 240, 0.32)' : 'rgba(15, 23, 42, 0.18)')
        : tone === 'muted'
          ? (isDarkTheme(ctx) ? 'rgba(226, 232, 240, 0.12)' : 'rgba(15, 23, 42, 0.08)')
          : (ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)')
      const label = asString(props.label)
      return (
        <div style={{ margin: '18px 0 14px' }}>
          <div
            style={{
              borderTop: `1px solid ${borderColor}`,
              position: 'relative',
            }}
          >
            {label ? (
              <span
                style={{
                  position: 'absolute',
                  top: -11,
                  left: 0,
                  paddingRight: 10,
                  background: ctx?.themeStyle?.pageBackground || '#fdfbf7',
                  color: ctx?.themeStyle?.bodyColor || 'rgba(15, 23, 42, 0.48)',
                  fontSize: 12,
                  letterSpacing: 0.4,
                  textTransform: 'uppercase',
                }}
              >
                {label}
              </span>
            ) : null}
          </div>
        </div>
      )
    }

    case 'ParagraphProse': {
      return <ParagraphProseNode key={node.id} node={node} ctx={ctx} withAnchorPreview={withAnchorPreview} />
    }

    case 'ListBlock': {
      const items = asStringArray(props.items)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <div style={{ position: 'relative' }}>
            <span className="reader-node-hover-bridge" aria-hidden="true" />
            <ActionBar node={node} ctx={ctx} placement="outer-left" />
            <ul
              style={{
                marginBottom: 14,
                paddingInlineStart: 24,
                lineHeight: 1.9,
                color: ctx.themeStyle?.bodyColor,
                fontFamily: ctx.themeStyle?.bodyFontFamily,
              }}
            >
              {items.map((item, idx) => <li key={`li-${idx}`}>{item}</li>)}
              {renderChildren(node.children || [], ctx)}
            </ul>
          </div>
        </DraggableContainer>,
      )
    }

    case 'FigurePanel': {
      const caption = asString(props.caption)
      const rawImageUrl = asString(props.image_url)
      const imageUrl = asString(
        typeof ctx.resolveFigureImageUrl === 'function'
          ? ctx.resolveFigureImageUrl(rawImageUrl, node)
          : rawImageUrl,
      )
      const imageFit = asString(props.image_fit).toLowerCase() === 'cover' ? 'cover' : 'contain'
      const preferContain = imageFit !== 'cover'
      const sourceLabel = deriveFigureSourceLabel(caption, asString(props.source_label))
      const aiInsight = asString(props.ai_insight)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <Card size="small" style={{ ...baseCardStyle(ctx), marginBottom: 14 }} styles={cardSurfaceStyles(ctx, { bodyPadding: '16px 16px' })}>
            <ActionBar
              node={node}
              ctx={ctx}
              extraActions={(
                <Button
                  size="small"
                  onClick={async () => {
                    const markdown = componentToMarkdown(node)
                    try {
                      await navigator.clipboard.writeText(markdown)
                      message.success('图表 Markdown 已复制')
                    } catch {
                      message.warning('复制失败')
                    }
                  }}
                >
                  导出/复制Markdown
                </Button>
              )}
            />
            {imageUrl ? (
              <div
                style={{
                  background: preferContain ? 'rgba(15, 23, 42, 0.03)' : 'transparent',
                  borderRadius: 12,
                  padding: preferContain ? 12 : 0,
                }}
              >
                <img
                  src={imageUrl}
                  alt={caption || 'figure'}
                  style={{
                    width: preferContain ? 'auto' : '100%',
                    maxWidth: '100%',
                    height: 'auto',
                    objectFit: imageFit,
                    borderRadius: 10,
                    display: 'block',
                    margin: preferContain ? '0 auto' : undefined,
                  }}
                />
              </div>
            ) : null}
            {(caption || sourceLabel) ? (
              <div
                style={{
                  marginTop: 12,
                  padding: '12px 14px',
                  borderRadius: 12,
                  background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44')
                    ? 'rgba(255, 255, 255, 0.04)'
                    : 'rgba(15, 23, 42, 0.03)',
                  border: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
                }}
              >
                {sourceLabel ? <Tag style={{ marginBottom: 8 }}>{sourceLabel}</Tag> : null}
                {caption ? (
                  <Paragraph
                    style={{
                      marginBottom: 0,
                      color: ctx?.themeStyle?.bodyColor,
                      opacity: 0.88,
                      lineHeight: 1.72,
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {caption}
                  </Paragraph>
                ) : null}
              </div>
            ) : null}
            {aiInsight ? (
              <div style={{
                marginTop: 14, padding: '12px 16px',
                background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(22, 119, 255, 0.05)' : 'rgba(23, 119, 255, 0.04)',
                borderRadius: 10, borderLeft: '4px solid #1677ff'
              }}>
                <Text strong style={{ color: '#1677ff', display: 'block', marginBottom: 6, fontSize: 13, letterSpacing: 0.5 }}>✨ AI 深度洞察</Text>
                <Text style={{ color: ctx?.themeStyle?.bodyColor, lineHeight: 1.7 }}>{aiInsight}</Text>
              </div>
            ) : null}
            <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
          </Card>
        </DraggableContainer>,
      )
    }

    case 'TablePanel': {
      const title = asString(props.title)
      const headers = asStringArray(props.headers)
      const matrix = asStringMatrix(props.matrix)
      const tableCells = asTableCells(props.table_cells)
      const headerRowCount = asNumber(props.header_row_count, 0)
      const rows = asRecordArray(props.rows)
      const caption = asString(props.caption)
      const notes = asStringArray(props.notes)
      const rawMarkdown = asString(props.raw_markdown)
      const rowEvidence = asTableRowEvidence(props.row_evidence)
      const aiInsight = asString(props.ai_insight)
      const nodeAnchorRefs = normalizeAnchorRows(node.source_anchor_refs)
      const nodeSourceBlockIds = Array.isArray(node.source_block_ids)
        ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
        : []
      const nodeSourceAtomIds = Array.isArray(node.source_atom_ids)
        ? node.source_atom_ids.map((item) => String(item || '').trim()).filter(Boolean)
        : []
      const fallbackMatrix = rows.length
        ? [
            headers.length ? headers : Object.keys(rows[0] || {}),
            ...rows.map((row) => {
              const keys = headers.length ? headers.map((_, index) => `col_${index + 1}`) : Object.keys(row)
              return keys.map((key) => asString(row[key] ?? ''))
            }),
          ]
        : []
      const effectiveMatrix = matrix.length > 0 ? matrix : fallbackMatrix
      const effectiveHeaderRowCount = matrix.length > 0 ? Math.max(0, Math.min(headerRowCount, matrix.length - 1)) : (headers.length ? 1 : 0)
      const columnCount = effectiveMatrix.reduce((max, row) => Math.max(max, row.length), 0)
      const paddedMatrix = effectiveMatrix.map((row) => [...row, ...Array(Math.max(0, columnCount - row.length)).fill('')])
      const rowEvidenceMap = new Map(rowEvidence.map((item) => [item.rowIndex, item]))
      const maxStructuredRow = tableCells.reduce((max, cell) => Math.max(max, cell.rowEnd), -1)
      const structuredRows = Array.from({ length: maxStructuredRow + 1 }, (_, rowIndex) => ({
        rowIndex,
        cells: tableCells
          .filter((cell) => cell.rowStart === rowIndex)
          .sort((left, right) => left.colStart - right.colStart || left.cellId - right.cellId),
      })).filter((entry) => entry.cells.length > 0)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <Card size="small" title={title || '表格'} style={baseCardStyle(ctx)}>
            <ActionBar
              node={node}
              ctx={ctx}
              extraActions={(
                <Space wrap size={8}>
                  <Button
                    size="small"
                    disabled={nodeAnchorRefs.length === 0}
                    onClick={() => ctx.onJumpAnchor?.(nodeAnchorRefs, {
                      pinPreview: true,
                      sourceBlockIds: nodeSourceBlockIds,
                      sourceAtomIds: nodeSourceAtomIds,
                    })}
                  >
                    证据
                  </Button>
                  <Button
                    size="small"
                    disabled={nodeAnchorRefs.length === 0}
                    onClick={() => ctx.onPreviewAnchors?.(nodeAnchorRefs, {
                      pinPreview: true,
                      sourceBlockIds: nodeSourceBlockIds,
                      sourceAtomIds: nodeSourceAtomIds,
                    })}
                  >
                    预览
                  </Button>
                  <Button
                    size="small"
                    onClick={async () => {
                      const markdown = componentToMarkdown(node)
                      try {
                        await navigator.clipboard.writeText(markdown)
                        message.success('表格 Markdown 已复制')
                      } catch {
                        message.warning('复制失败')
                      }
                    }}
                  >
                    导出CSV/Markdown
                  </Button>
                </Space>
              )}
            />
            {structuredRows.length > 0 || paddedMatrix.length > 0 ? (
              <div style={{ overflowX: 'auto', borderRadius: 10, border: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}` }}>
                {rowEvidence.length > 0 ? (
                  <div style={{ padding: '10px 12px', borderBottom: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`, background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255,255,255,0.02)' : 'rgba(15, 23, 42, 0.025)' }}>
                    <Text style={{ fontSize: 12, color: ctx?.themeStyle?.bodyColor, opacity: 0.72 }}>
                      悬停表格行可预览证据，点击行可定位到 PDF 高光。
                    </Text>
                  </div>
                ) : null}
                <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: `${Math.max(420, (structuredRows.length > 0 ? Math.max(...tableCells.map((cell) => cell.colEnd + 1), 0) : columnCount) * 140)}px` }}>
                  {structuredRows.length > 0 ? (
                    <>
                      {structuredRows.some((entry) => entry.rowIndex < effectiveHeaderRowCount) ? (
                        <thead>
                          {structuredRows.filter((entry) => entry.rowIndex < effectiveHeaderRowCount).map((entry) => {
                            const evidence = rowEvidenceMap.get(entry.rowIndex)
                            return (
                              <tr
                                key={`thead-structured-${entry.rowIndex}`}
                                onMouseEnter={() => {
                                  if (evidence?.anchor) {
                                    ctx.onPreviewAnchors?.([evidence.anchor], { sourceAtomIds: evidence.sourceAtomIds })
                                  }
                                }}
                                onClick={() => {
                                  if (evidence?.anchor) {
                                    ctx.onJumpAnchor?.([evidence.anchor], { pinPreview: true, sourceAtomIds: evidence.sourceAtomIds })
                                  }
                                }}
                                style={{ cursor: evidence?.anchor ? 'pointer' : 'default' }}
                              >
                                {entry.cells.map((cell) => (
                                  <th
                                    key={`thead-structured-${entry.rowIndex}-${cell.cellId}`}
                                    rowSpan={cell.rowspan > 1 ? cell.rowspan : undefined}
                                    colSpan={cell.colspan > 1 ? cell.colspan : undefined}
                                    scope="col"
                                    style={{
                                      padding: '10px 12px',
                                      textAlign: 'left',
                                      fontWeight: 700,
                                      fontSize: 13,
                                      color: ctx?.themeStyle?.headingColor,
                                      background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255,255,255,0.04)' : 'rgba(15, 23, 42, 0.04)',
                                      borderBottom: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
                                    }}
                                  >
                                    {cell.text || '—'}
                                  </th>
                                ))}
                              </tr>
                            )
                          })}
                        </thead>
                      ) : null}
                      <tbody>
                        {structuredRows.filter((entry) => entry.rowIndex >= effectiveHeaderRowCount).map((entry) => {
                          const evidence = rowEvidenceMap.get(entry.rowIndex)
                          return (
                            <tr
                              key={`tbody-structured-${entry.rowIndex}`}
                              onMouseEnter={() => {
                                if (evidence?.anchor) {
                                  ctx.onPreviewAnchors?.([evidence.anchor], { sourceAtomIds: evidence.sourceAtomIds })
                                }
                              }}
                              onClick={() => {
                                if (evidence?.anchor) {
                                  ctx.onJumpAnchor?.([evidence.anchor], { pinPreview: true, sourceAtomIds: evidence.sourceAtomIds })
                                }
                              }}
                              style={{
                                cursor: evidence?.anchor ? 'pointer' : 'default',
                                background: evidence?.anchor
                                  ? (ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255,255,255,0.015)' : 'rgba(15, 23, 42, 0.015)')
                                  : undefined,
                              }}
                            >
                              {entry.cells.map((cell) => (
                                <td
                                  key={`tbody-structured-${entry.rowIndex}-${cell.cellId}`}
                                  rowSpan={cell.rowspan > 1 ? cell.rowspan : undefined}
                                  colSpan={cell.colspan > 1 ? cell.colspan : undefined}
                                  style={{
                                    padding: '10px 12px',
                                    verticalAlign: 'top',
                                    borderBottom: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
                                    color: ctx?.themeStyle?.bodyColor,
                                    fontSize: 13,
                                    lineHeight: 1.55,
                                    whiteSpace: 'pre-wrap',
                                  }}
                                >
                                  {cell.text || '—'}
                                </td>
                              ))}
                            </tr>
                          )
                        })}
                      </tbody>
                    </>
                  ) : (
                    <>
                      {effectiveHeaderRowCount > 0 ? (
                        <thead>
                          {paddedMatrix.slice(0, effectiveHeaderRowCount).map((row, rowIndex) => {
                            const evidence = rowEvidenceMap.get(rowIndex)
                            return (
                              <tr
                                key={`thead-${rowIndex}`}
                                onMouseEnter={() => {
                                  if (evidence?.anchor) {
                                    ctx.onPreviewAnchors?.([evidence.anchor], { sourceAtomIds: evidence.sourceAtomIds })
                                  }
                                }}
                                onClick={() => {
                                  if (evidence?.anchor) {
                                    ctx.onJumpAnchor?.([evidence.anchor], { pinPreview: true, sourceAtomIds: evidence.sourceAtomIds })
                                  }
                                }}
                                style={{ cursor: evidence?.anchor ? 'pointer' : 'default' }}
                              >
                                {row.map((cell, cellIndex) => (
                                  <th
                                    key={`thead-${rowIndex}-${cellIndex}`}
                                    style={{
                                      padding: '10px 12px',
                                      textAlign: 'left',
                                      fontWeight: 700,
                                      fontSize: 13,
                                      color: ctx?.themeStyle?.headingColor,
                                      background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255,255,255,0.04)' : 'rgba(15, 23, 42, 0.04)',
                                      borderBottom: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
                                    }}
                                  >
                                    {cell || '—'}
                                  </th>
                                ))}
                              </tr>
                            )
                          })}
                        </thead>
                      ) : null}
                      <tbody>
                        {paddedMatrix.slice(effectiveHeaderRowCount).map((row, rowIndex) => {
                          const absoluteRowIndex = rowIndex + effectiveHeaderRowCount
                          const evidence = rowEvidenceMap.get(absoluteRowIndex)
                          return (
                            <tr
                              key={`tbody-${rowIndex}`}
                              onMouseEnter={() => {
                                if (evidence?.anchor) {
                                  ctx.onPreviewAnchors?.([evidence.anchor], { sourceAtomIds: evidence.sourceAtomIds })
                                }
                              }}
                              onClick={() => {
                                if (evidence?.anchor) {
                                  ctx.onJumpAnchor?.([evidence.anchor], { pinPreview: true, sourceAtomIds: evidence.sourceAtomIds })
                                }
                              }}
                              style={{
                                cursor: evidence?.anchor ? 'pointer' : 'default',
                                background: evidence?.anchor
                                  ? (ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255,255,255,0.015)' : 'rgba(15, 23, 42, 0.015)')
                                  : undefined,
                              }}
                            >
                              {row.map((cell, cellIndex) => (
                                <td
                                  key={`tbody-${rowIndex}-${cellIndex}`}
                                  style={{
                                    padding: '10px 12px',
                                    verticalAlign: 'top',
                                    borderBottom: `1px solid ${ctx?.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
                                    color: ctx?.themeStyle?.bodyColor,
                                    fontSize: 13,
                                    lineHeight: 1.55,
                                    whiteSpace: 'pre-wrap',
                                  }}
                                >
                                  {cell || '—'}
                                </td>
                              ))}
                            </tr>
                          )
                        })}
                      </tbody>
                    </>
                  )}
                </table>
              </div>
            ) : rawMarkdown ? (
              <Paragraph style={{ marginBottom: 0, color: ctx?.themeStyle?.bodyColor, whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                {rawMarkdown}
              </Paragraph>
            ) : (
              <List
                size="small"
                dataSource={rows}
                renderItem={(row, idx) => (
                  <List.Item key={`row-${idx}`}>
                    <Text style={{ color: ctx?.themeStyle?.bodyColor }}>{Object.values(row).map((item) => asString(item)).join(' | ')}</Text>
                  </List.Item>
                )}
              />
            )}
            {caption ? (
              <Paragraph style={{ marginTop: 12, marginBottom: 0, color: ctx?.themeStyle?.bodyColor, opacity: 0.82, lineHeight: 1.65 }}>
                {caption}
              </Paragraph>
            ) : null}
            {notes.length > 0 ? (
              <div style={{ marginTop: 10 }}>
                {notes.map((note, idx) => (
                  <Paragraph key={`table-note-${idx}`} style={{ marginBottom: idx === notes.length - 1 ? 0 : 6, color: ctx?.themeStyle?.bodyColor, opacity: 0.72, fontSize: 12, lineHeight: 1.55 }}>
                    {note}
                  </Paragraph>
                ))}
              </div>
            ) : null}
            {aiInsight ? (
              <div style={{
                marginTop: 14, padding: '12px 16px',
                background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(22, 119, 255, 0.05)' : 'rgba(23, 119, 255, 0.04)',
                borderRadius: 10, borderLeft: '4px solid #1677ff'
              }}>
                <Text strong style={{ color: '#1677ff', display: 'block', marginBottom: 6, fontSize: 13, letterSpacing: 0.5 }}>📊 AI 数据解读</Text>
                <Text style={{ color: ctx?.themeStyle?.bodyColor, lineHeight: 1.7 }}>{aiInsight}</Text>
              </div>
            ) : null}
            <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
          </Card>
        </DraggableContainer>,
      )
    }

    case 'CitationLinks': {
      const links = asRecordArray(props.links)
      return (
        <Card size="small" title="文献资源链接" style={baseCardStyle(ctx)} styles={cardSurfaceStyles(ctx, { bodyPadding: '14px 16px', emphasis: 'muted' })}>
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {links.map((link, idx) => {
              const href = asString(link.href)
              const label = asString(link.label) || href
              const tldr = asString(link.tldr || (link.meta as Record<string, unknown> | undefined)?.tldr)
              if (!href) return null
              return (
                <Popover
                  key={`link-${idx}`}
                  overlayClassName="reader-composed-popover"
                  overlayStyle={
                    {
                      '--reader-card-bg': ctx?.themeStyle?.panelBackground,
                      '--reader-card-border': ctx?.themeStyle?.borderColor,
                      '--reader-text': ctx?.themeStyle?.bodyColor,
                    } as CSSProperties
                  }
                  title="文献智能摘要 (TL;DR)"
                  placement="topLeft"
                  content={
                    <div style={{ maxWidth: 320, whiteSpace: 'normal' }}>
                      <Text
                        style={{
                          color: tldr ? ctx?.themeStyle?.bodyColor : ctx?.themeStyle?.bodyColor,
                          lineHeight: 1.6,
                          opacity: tldr ? 1 : 0.72,
                        }}
                      >
                        {tldr || '暂无该文献的核心摘要...（可由 Web Search Agent 异步挂载）'}
                      </Text>
                    </div>
                  }
                >
                  <a
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                  style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 4,
                      padding: '6px 10px',
                      background: isDarkTheme(ctx) ? 'rgba(98, 170, 255, 0.14)' : 'rgba(22, 119, 255, 0.08)',
                      borderRadius: 999,
                      border: isDarkTheme(ctx) ? '1px solid rgba(131, 188, 255, 0.35)' : '1px solid rgba(22, 119, 255, 0.2)',
                      color: isDarkTheme(ctx) ? '#e6eeff' : '#1858d0',
                      fontWeight: 600,
                      lineHeight: 1.3,
                      textDecoration: 'none',
                    }}
                  >
                    <LinkOutlined /> {label}
                  </a>
                </Popover>
              )
            })}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'KeyTakeaways': {
      const rows = asRecordArray(props.items)
      const itemRows = rows.length > 0
        ? rows.map((row) => ({
          text: asString(row.text || row.title || row.value),
        }))
        : asStringArray(props.items).map((text) => ({ text }))
      return (
        <Card size="small" title="关键要点" style={baseCardStyle(ctx)}>
          <ActionBar node={node} ctx={ctx} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {itemRows.map((item, idx) => {
              return (
                <div
                  key={`take-${idx}`}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 12,
                    padding: '12px 14px',
                    background: ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44')
                      ? 'rgba(255, 255, 255, 0.03)'
                      : 'rgba(22, 119, 255, 0.03)',
                    borderRadius: 10,
                    border: `1px solid ${ctx?.themeStyle?.panelBackground?.includes('rgba(18, 26, 44') ? 'rgba(255, 255, 255, 0.06)' : 'rgba(22, 119, 255, 0.08)'}`,
                    transition: 'all 0.3s ease',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.transform = 'translateY(-2px)'
                    e.currentTarget.style.boxShadow = '0 6px 16px rgba(0,0,0,0.06)'
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.transform = 'translateY(0)'
                    e.currentTarget.style.boxShadow = 'none'
                  }}
                >
                  <div style={{
                    width: 24, height: 24, borderRadius: '50%', background: 'linear-gradient(135deg, #1677ff 0%, #0958d9 100%)',
                    color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 13, fontWeight: 'bold', flexShrink: 0, marginTop: 2,
                    boxShadow: '0 2px 6px rgba(22, 119, 255, 0.4)'
                  }}>
                    {idx + 1}
                  </div>
                  <div style={{ flex: 1, lineHeight: 1.8 }}>
                    <Text style={{ fontSize: 15, color: ctx?.themeStyle?.bodyColor }}>{item.text}</Text>
                  </div>
                </div>
              )
            })}
          </div>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'AnnotationRail': {
      const items = asStringArray(props.items)
      return (
        <Card size="small" title="页内批注" style={baseCardStyle(ctx)}>
          <List
            size="small"
            dataSource={items}
            renderItem={(item, idx) => <List.Item key={`anno-${idx}`}>{item}</List.Item>}
          />
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'InlineQuerySlot':
      return <InlineQuerySlotNode node={node} ctx={ctx} />

    case 'AnswerCard': {
      const question = asString(props.question)
      const answer = asString(props.answer)
      const foldable = props.foldable !== false
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <Card size="small" title="内联问答" style={baseCardStyle(ctx)}>
            <ActionBar node={node} ctx={ctx} />
            <Paragraph style={{ marginBottom: 8 }}>
              <Text strong>问题：</Text>
              <Text>{question}</Text>
            </Paragraph>
            <Paragraph style={{ marginBottom: 0 }} ellipsis={foldable ? { rows: 6, expandable: true, symbol: '展开' } : false}>
              <Text strong>回答：</Text>
              <Text>{answer}</Text>
            </Paragraph>
          </Card>
        </DraggableContainer>,
      )
    }

    case 'CompareInsightsCard': {
      const items = asRecordArray(props.items)
      return withAnchorPreview(
        <Card size="small" title="跨论文对比洞察" style={baseCardStyle(ctx)} styles={cardSurfaceStyles(ctx, { bodyPadding: '16px 18px', emphasis: 'muted' })}>
          <ActionBar node={node} ctx={ctx} />
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {items.map((item, idx) => (
              <div
                key={`cmp-${idx}`}
                style={{
                  padding: '10px 12px',
                  borderRadius: 12,
                  background: isDarkTheme(ctx) ? 'rgba(255,255,255,0.04)' : 'rgba(15, 23, 42, 0.035)',
                }}
              >
                <Text strong style={{ display: 'block', marginBottom: 4 }}>{asString(item.title || `洞察${idx + 1}`)}</Text>
                <Text>{asString(item.content)}</Text>
              </div>
            ))}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>,
      )
    }

    case 'InsightClusterCard': {
      const title = asString(props.title) || 'Key insight cluster'
      const tone = asString(props.tone).toLowerCase()
      const items = asStringArray(props.items)
      const toneAccent = {
        finding: { border: '#1677ff', bg: isDarkTheme(ctx) ? 'rgba(22, 119, 255, 0.08)' : '#f0f7ff', tag: '洞察' },
        claim: { border: '#722ed1', bg: isDarkTheme(ctx) ? 'rgba(114, 46, 209, 0.08)' : '#f7f1ff', tag: '论点' },
        implication: { border: '#08979c', bg: isDarkTheme(ctx) ? 'rgba(8, 151, 156, 0.08)' : '#eefbfb', tag: '启示' },
      }[tone as 'finding' | 'claim' | 'implication'] || { border: '#1677ff', bg: isDarkTheme(ctx) ? 'rgba(22, 119, 255, 0.08)' : '#f0f7ff', tag: '洞察' }

      return withAnchorPreview(
        <Card
          size="small"
          style={{
            ...baseCardStyle(ctx),
            borderLeft: `4px solid ${toneAccent.border}`,
            background: toneAccent.bg,
            marginBottom: 16,
          }}
          styles={cardSurfaceStyles(ctx, { bodyPadding: '16px 18px', emphasis: 'muted' })}
          title={(
            <Space size={8}>
              <Tag color="blue">{toneAccent.tag}</Tag>
              <Text strong>{title}</Text>
            </Space>
          )}
        >
          <ActionBar node={node} ctx={ctx} />
          <List
            size="small"
            dataSource={items}
            renderItem={(item) => (
              <List.Item style={{ border: 'none', padding: '6px 0' }}>
                <Space align="start" size={8}>
                  <span style={{ color: toneAccent.border, fontSize: 16, lineHeight: 1 }}>•</span>
                  <Text style={{ fontSize: 14, lineHeight: 1.7 }}>{item}</Text>
                </Space>
              </List.Item>
            )}
          />
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>,
      )
    }

    case 'SectionBridgeCard': {
      const title = asString(props.title) || '章节承接'
      const text = asString(props.text)
      return withAnchorPreview(
        <div
          style={{
            margin: '18px 0',
            padding: '14px 18px',
            borderRadius: 14,
            border: `1px solid ${ctx.themeStyle?.borderColor || 'rgba(9, 30, 66, 0.08)'}`,
            background: isDarkTheme(ctx) ? 'rgba(255,255,255,0.03)' : 'rgba(8, 15, 30, 0.025)',
          }}
        >
          <ActionBar node={node} ctx={ctx} />
          <Text
            strong
            style={{
              display: 'block',
              fontSize: 12,
              letterSpacing: '0.08em',
              textTransform: 'uppercase',
              color: ctx.themeStyle?.mutedColor,
              marginBottom: 8,
            }}
          >
            {title}
          </Text>
          <Paragraph style={{ marginBottom: 0, fontSize: 14, lineHeight: 1.75, color: ctx.themeStyle?.bodyColor }}>
            {text}
          </Paragraph>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </div>,
      )
    }

    case 'QualityPanel':
    case 'QualityBadge': {
      const report = ctx.qualityReport
      if (!report) return null
      const deductions = Array.isArray(report.deductions) ? report.deductions : []
      const suggestions = Array.isArray(report.fix_suggestions) ? report.fix_suggestions : []
      return (
        <Alert
          showIcon
          type={report.hard_constraints_passed ? 'success' : 'warning'}
          message={`质量分：${Math.round((report.overall || 0) * 100)}/100`}
          description={(
            <Space direction="vertical" size={6}>
              <Text>迭代：{report.iterations || 0} 轮；停止原因：{report.stop_reason || 'unknown'}</Text>
              <Space size={8} wrap>
                {typeof report.cross_column_merge_ratio === 'number' ? (
                  <Tag color={report.cross_column_merge_ratio <= 0.08 ? 'green' : 'gold'}>
                    跨栏拼接率 {(report.cross_column_merge_ratio * 100).toFixed(1)}%
                  </Tag>
                ) : null}
                {typeof report.sidebar_recall === 'number' ? (
                  <Tag color={report.sidebar_recall >= 0.75 ? 'green' : 'gold'}>
                    侧栏保留率 {(report.sidebar_recall * 100).toFixed(1)}%
                  </Tag>
                ) : null}
                {typeof report.toc_quality === 'number' ? (
                  <Tag color={report.toc_quality >= 0.55 ? 'blue' : 'orange'}>
                    目录质量 {(report.toc_quality * 100).toFixed(0)}%
                  </Tag>
                ) : null}
                {report.mm_assist_used ? (
                  <Tag color="purple">
                    多模态辅助：{report.mm_model || '已启用'}
                    {report.mm_fallback_used ? '（fallback）' : ''}
                  </Tag>
                ) : (
                  <Tag>多模态辅助：未触发</Tag>
                )}
              </Space>
              {deductions.length > 0 ? (
                <div>
                  <Text strong>扣分项：</Text>
                  <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                    {deductions.slice(0, 5).map((item, idx) => (
                      <li key={`ded-${idx}`}>
                        {asString((item as Record<string, unknown>).item)} - {asString((item as Record<string, unknown>).reason)}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {suggestions.length > 0 ? (
                <div>
                  <Text strong>补救建议：</Text>
                  <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                    {suggestions.slice(0, 5).map((item, idx) => <li key={`sug-${idx}`}>{item}</li>)}
                  </ul>
                </div>
              ) : null}
            </Space>
          )}
        />
      )
    }

    case 'PdfSnippetCard': {
      const title = asString(props.title) || '原文片段'
      const description = asString(props.description)
      const page = asNumber(props.page, 0)
      return (
        <Card size="small" title={title} style={baseCardStyle(ctx)}>
          <Paragraph style={{ marginBottom: 8 }}>{description}</Paragraph>
          {page > 0 ? <Tag color="blue">第 {page} 页</Tag> : null}
        </Card>
      )
    }

    case 'CitationCard': {
      const title = asString(props.title)
      const authors = asStringArray(props.authors)
      const year = asString(props.year)
      const journal = asString(props.journal)
      const doi = asString(props.doi)
      const doiHref = normalizeDoiHref(doi)
      const abstractTldr = asString(props.abstract_tldr)
      const citationKey = asString(props.citation_key)

      return withAnchorPreview(
        <Card
          size="small"
          style={{
            ...baseCardStyle(ctx),
            borderLeft: '4px solid #faad14',
            marginBottom: 16,
          }}
          title={
            <Space>
              <Tag color="warning">{citationKey || 'REF'}</Tag>
              <Text strong>{title}</Text>
            </Space>
          }
        >
          <ActionBar node={node} ctx={ctx} />
          <div style={{ marginBottom: 8 }}>
            <Text type="secondary" style={{ fontSize: 13 }}>
              {authors.join(', ')} {year ? `(${year})` : ''}
            </Text>
            {journal && (
              <div style={{ marginTop: 2 }}>
                <Text italic style={{ fontSize: 13 }}>{journal}</Text>
              </div>
            )}
          </div>
          {doi && (
            <div style={{ marginBottom: 10 }}>
              <Tag icon={<LinkOutlined />} color="blue">
                <a href={doiHref} target="_blank" rel="noreferrer" style={{ color: 'inherit' }}>
                  {doi}
                </a>
              </Tag>
            </div>
          )}
          {abstractTldr && (
            <div style={{
              padding: '8px 12px',
              backgroundColor: isDarkTheme(ctx) ? 'rgba(250, 173, 20, 0.05)' : '#fffbe6',
              borderRadius: 8,
              fontSize: 14,
              lineHeight: 1.6,
              color: ctx.themeStyle?.bodyColor,
            }}>
              <Text strong style={{ display: 'block', marginBottom: 4, fontSize: 12, color: '#d48806' }}>
                文献简评 / TL;DR
              </Text>
              {abstractTldr}
            </div>
          )}
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'EquationBlock': {
      return <EquationBlockNode node={node} ctx={ctx} withAnchorPreview={withAnchorPreview} />
    }

    case 'MethodologyCard': {
      const title = asString(props.title) || '实验设计与方法'
      const steps = asStringArray(props.steps)
      const participants = asString(props.participants)
      const tools = asStringArray(props.tools)

      return withAnchorPreview(
        <Card
          size="small"
          title={<Title level={5} style={{ margin: 0, color: ctx.themeStyle?.headingColor }}>🔬 {title}</Title>}
          style={{ ...baseCardStyle(ctx), borderLeft: '4px solid #722ed1', marginBottom: 16 }}
        >
          <ActionBar node={node} ctx={ctx} />
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {participants && (
              <div>
                <Text strong style={{ color: '#722ed1' }}>参与对象: </Text>
                <Text>{participants}</Text>
              </div>
            )}
            <div>
              <Text strong style={{ color: '#722ed1' }}>关键步骤: </Text>
              <List
                size="small"
                dataSource={steps}
                renderItem={(item, index) => (
                  <List.Item style={{ border: 'none', padding: '4px 0' }}>
                    <Space align="start">
                      <div style={{
                        width: 20, height: 20, borderRadius: '50%',
                        backgroundColor: '#f9f0ff', color: '#722ed1',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontSize: 11, fontWeight: 'bold', flexShrink: 0, marginTop: 2
                      }}>
                        {index + 1}
                      </div>
                      <Text style={{ fontSize: 14 }}>{item}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
            {tools.length > 0 && (
              <div>
                <Text strong style={{ color: '#722ed1' }}>研究工具: </Text>
                <Space wrap size={4}>
                  {tools.map((t, i) => <Tag key={i} color="purple">{t}</Tag>)}
                </Space>
              </div>
            )}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    case 'CalloutBox': {
      const type = asString(props.type) as 'info' | 'warning' | 'success' | 'tip'
      const title = asString(props.title)
      const content = asString(props.content)
      const colorMap = {
        info: { border: '#1677ff', bg: 'rgba(22, 119, 255, 0.05)', icon: 'ℹ️' },
        warning: { border: '#faad14', bg: 'rgba(250, 173, 20, 0.05)', icon: '⚠️' },
        success: { border: '#52c41a', bg: 'rgba(82, 196, 26, 0.05)', icon: '✅' },
        tip: { border: '#13c2c2', bg: 'rgba(19, 194, 194, 0.05)', icon: '💡' },
      }
      const style = colorMap[type] || colorMap.info

      return (
        <div style={{
          margin: '16px 0',
          padding: '16px 20px',
          backgroundColor: style.bg,
          borderLeft: `4px solid ${style.border}`,
          borderRadius: '0 12px 12px 0',
          position: 'relative'
        }}>
          <ActionBar node={node} ctx={ctx} />
          <Space align="start" size={10}>
            <span style={{ fontSize: 18 }}>{style.icon}</span>
            <div>
              {title && <Text strong style={{ display: 'block', marginBottom: 4, fontSize: 15 }}>{title}</Text>}
              <Text style={{ fontSize: 14, lineHeight: 1.6 }}>{content}</Text>
            </div>
          </Space>
          {renderChildren(node.children || [], ctx)}
        </div>
      )
    }

    case 'AbstractCard': {
      const text = asString(props.text)
      return withAnchorPreview(
        <Card
          size="small"
          title={<Title level={5} style={{ margin: 0, color: ctx.themeStyle?.headingColor }}>📝 Abstract / 摘要</Title>}
          style={{
            ...baseCardStyle(ctx),
            backgroundColor: isDarkTheme(ctx) ? 'rgba(22, 119, 255, 0.03)' : 'rgba(22, 119, 255, 0.01)',
            border: `1px dashed ${ctx.themeStyle?.borderColor || '#1677ff'}`,
            marginBottom: 20
          }}
        >
          <ActionBar node={node} ctx={ctx} />
          <Paragraph style={{
            fontSize: 15,
            lineHeight: 1.8,
            textAlign: 'justify',
            color: ctx.themeStyle?.bodyColor,
            marginBottom: 0
          }}>
            {text}
          </Paragraph>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
      )
    }

    default:
      return (
        <Alert
          showIcon
          type="warning"
          message={`未知组件：${node.type}`}
          description="该组件未在白名单注册，已安全忽略。"
        />
      )
  }
}

export function renderReaderComponentTree(
  components: ReaderComponentNode[],
  ctx: ReaderComponentRenderContext,
): ReactNode {
  const resolveReaderBandKind = (node: ReaderComponentNode): 'prose' | 'feature' | 'break' => {
    switch (String(node.type || '').trim()) {
      case 'FigurePanel':
      case 'AbstractCard':
      case 'MethodologyCard':
      case 'CitationCard':
      case 'CompareInsightsCard':
      case 'InsightClusterCard':
      case 'KeyTakeaways':
      case 'EquationBlock':
        return 'feature'
      case 'CalloutBox':
      case 'SectionBridgeCard':
      case 'AnnotationRail':
      case 'QualityPanel':
      case 'QualityBadge':
      case 'AnswerCard':
      case 'Separator':
      case 'InlineQuerySlot':
        return 'break'
      default:
        return 'prose'
    }
  }

  return (
    <Fragment>
      {components.map((node) => {
        const bandKind = resolveReaderBandKind(node)
        return (
          <div
            key={node.id}
            className={`reader-band reader-band--${bandKind}`}
            data-reader-node-type={node.type}
          >
            {renderReaderNode(node, ctx)}
          </div>
        )
      })}
    </Fragment>
  )
}
