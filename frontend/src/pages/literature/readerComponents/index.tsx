import { Fragment, type CSSProperties, type ReactNode, useState } from 'react'
import { Alert, Button, Card, Input, List, Space, Tag, Tooltip, Popover, Typography, message } from 'antd'
import { DownOutlined, DragOutlined, LinkOutlined, PlusOutlined } from '@ant-design/icons'

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
  onJumpAnchor?: (anchors: ReaderComponentSourceAnchor[], options?: { pinPreview?: boolean }) => void
  onPreviewAnchors?: (anchors: ReaderComponentSourceAnchor[], options?: { pinPreview?: boolean }) => void
  onHidePreview?: () => void
  onInlineQuery?: (node: ReaderComponentNode, question: string) => Promise<void> | void
  onDropMarkdown?: (markdown: string, node?: ReaderComponentNode) => void
  onManualInsertSlot?: (nodeId: string) => void
  resolveAnchorPreviewImage?: (
    anchors: ReaderComponentSourceAnchor[],
    options?: { preferredPage?: number; segmentIndex?: number },
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
  if (node.type === 'KeyTakeaways') {
    const items = asRecordArray((props as Record<string, unknown>).items)
    if (items.length > 0) {
      return items.map((item) => `- ${asString(item.text || item.title || item.value)}`).join('\n')
    }
    return asStringArray((props as Record<string, unknown>).items).map((item) => `- ${item}`).join('\n')
  }
  if (node.type === 'TablePanel') {
    const title = text('title') || '表格'
    const rows = asRecordArray((props as Record<string, unknown>).rows)
    if (!rows.length) return `### ${title}\n\n(暂无结构化行数据)`
    const headers = Object.keys(rows[0] || {})
    const headerRow = `| ${headers.join(' | ')} |`
    const sepRow = `| ${headers.map(() => '---').join(' | ')} |`
    const bodyRows = rows.map((row) => `| ${headers.map((h) => asString(row[h])).join(' | ')} |`)
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
}): ReactNode {
  const { node, ctx, extraActions } = props
  if (ctx.readOnly) return null
  const [hovered, setHovered] = useState(false)
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
  const darkTheme = isDarkTheme(ctx)
  const idleOpacity = darkTheme ? 0.62 : 0.9
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
  return (
    <div
      className="reader-action-bar"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        opacity: hovered ? 1 : idleOpacity,
        transition: 'opacity 0.25s',
        marginBottom: 8,
        display: 'flex',
        justifyContent: 'flex-end', // 靠右对齐更不影响阅读
      }}
    >
      <Space size={6} wrap>
        {actionRows.map((row, idx) => {
          const key = canonicalActionKey(asString(row.key))
          const label = asString(row.label) || key || `action-${idx + 1}`
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
                onClick={() => ctx.onJumpAnchor?.(anchorRefs, { pinPreview: true })}
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
                onClick={() => ctx.onPreviewAnchors?.(anchorRefs, { pinPreview: true })}
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
            拖拽Markdown
          </Button>
        </span>
        {extraActions}
      </Space>
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
  if (!ctx.onInlineQuery) return null
  const [expanded, setExpanded] = useState(false)
  const [value, setValue] = useState('')
  const loading = ctx.inlineQueryLoadingNodeId === node.id
  return (
    <Card size="small" style={{ ...baseCardStyle(ctx), margin: '8px 0' }}>
      {!expanded ? (
        <Button size="small" type="dashed" icon={<DownOutlined />} onClick={() => setExpanded(true)}>
          + 在这里提问
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
      <DraggableContainer node={node}>
        <div>
          <ActionBar node={node} ctx={ctx} />
          <div>
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
          </div>
        </div>
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
            title="在此段落后插入提问"
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
  if (node.layout_slot?.reserved_height) {
    layoutStyle.minHeight = node.layout_slot.reserved_height
    if (node.layout_slot.lock_height) {
      layoutStyle.height = node.layout_slot.reserved_height
      layoutStyle.overflow = 'hidden'
    }
  }

  const withAnchorPreview = (child: ReactNode): ReactNode => (
    <div
      style={layoutStyle}
      onMouseEnter={() => {
        if (anchorRefs.length > 0) {
          ctx.onPreviewAnchors?.(anchorRefs, { pinPreview: false })
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
        <Card size="small" title="元数据" style={baseCardStyle(ctx)}>
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
        <Card size="small" title={title} style={baseCardStyle(ctx)}>
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
                        type="link"
                        size="small"
                        onClick={() => ctx.onJumpAnchor?.(item.anchor, { pinPreview: true })}
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
        <div style={{ margin: '20px 0 8px' }}>
          <ActionBar node={node} ctx={ctx} />
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

    case 'ParagraphProse': {
      return <ParagraphProseNode key={node.id} node={node} ctx={ctx} withAnchorPreview={withAnchorPreview} />
    }

    case 'ListBlock': {
      const items = asStringArray(props.items)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <ActionBar node={node} ctx={ctx} />
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
      const preferContain = rawImageUrl.startsWith('asset:') || /^https?:\/\/(?:dx\.)?doi\.org\//i.test(rawImageUrl)
      const sourceLabel = deriveFigureSourceLabel(caption, asString(props.source_label))
      const aiInsight = asString(props.ai_insight)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <Card size="small" style={{ ...baseCardStyle(ctx), marginBottom: 14 }}>
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
                    width: '100%',
                    maxHeight: 520,
                    objectFit: preferContain ? 'contain' : 'cover',
                    borderRadius: 10,
                    display: 'block',
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
      const rows = asRecordArray(props.rows)
      const aiInsight = asString(props.ai_insight)
      return withAnchorPreview(
        <DraggableContainer node={node}>
          <Card size="small" title={title || '表格'} style={baseCardStyle(ctx)}>
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
                      message.success('表格 Markdown 已复制')
                    } catch {
                      message.warning('复制失败')
                    }
                  }}
                >
                  导出CSV/Markdown
                </Button>
              )}
            />
            <List
              size="small"
              dataSource={rows}
              renderItem={(row, idx) => (
                <List.Item key={`row-${idx}`}>
                  <Text style={{ color: ctx?.themeStyle?.bodyColor }}>{Object.values(row).map((item) => asString(item)).join(' | ')}</Text>
                </List.Item>
              )}
            />
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
        <Card size="small" title="文献资源链接" style={baseCardStyle(ctx)}>
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
                      padding: '4px 8px',
                      background: isDarkTheme(ctx) ? 'rgba(98, 170, 255, 0.14)' : 'rgba(22, 119, 255, 0.08)',
                      borderRadius: 6,
                      border: isDarkTheme(ctx) ? '1px solid rgba(131, 188, 255, 0.35)' : '1px solid rgba(22, 119, 255, 0.2)',
                      color: '#1677ff',
                      fontWeight: 500,
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
      return (
        <Card size="small" title="跨论文对比洞察" style={baseCardStyle(ctx)}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            {items.map((item, idx) => (
              <div key={`cmp-${idx}`}>
                <Text strong>{asString(item.title || `洞察${idx + 1}`)}：</Text>
                <Text>{asString(item.content)}</Text>
              </div>
            ))}
          </Space>
          <div style={{ marginTop: 10 }}>{renderChildren(node.children || [], ctx)}</div>
        </Card>
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
      const latex = asString(props.latex)
      const label = asString(props.label)
      const description = asString(props.description)

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
          <div style={{
            fontSize: 20,
            fontFamily: 'serif',
            overflowX: 'auto',
            padding: '10px 0',
            color: ctx.themeStyle?.bodyColor,
          }}>
            {/* Simple centered display for LaTeX, assuming frontend handles MathJax/KaTeX globally or we just show it nicely */}
            <div style={{ display: 'inline-block', verticalAlign: 'middle' }}>
              {latex}
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
              opacity: 0.7,
              fontStyle: 'italic'
            }}>
              {description}
            </div>
          )}
          {renderChildren(node.children || [], ctx)}
        </div>
      )
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
  return (
    <Fragment>
      {components.map((node) => (
        <Fragment key={node.id}>{renderReaderNode(node, ctx)}</Fragment>
      ))}
    </Fragment>
  )
}
