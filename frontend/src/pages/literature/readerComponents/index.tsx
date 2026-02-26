import { Fragment, type CSSProperties, type ReactNode, useState } from 'react'
import { Alert, Button, Card, Input, List, Space, Tag, Tooltip, Popover, Typography, message } from 'antd'
import { DownOutlined, DragOutlined, LinkOutlined, ReloadOutlined, ShrinkOutlined, PlusOutlined } from '@ant-design/icons'

import type {
  ReaderComponentNode,
  ReaderComponentSourceAnchor,
  ReaderComposeQualityReport,
} from '@/services/api'
import type { GenerativeStyleTokens } from '../generativeStyles'

const { Text, Title, Paragraph } = Typography

export type ReaderComponentRenderContext = {
  themeStyle?: GenerativeStyleTokens
  qualityReport?: ReaderComposeQualityReport | null
  inlineQueryLoadingNodeId?: string | null
  onJumpAnchor?: (anchors: ReaderComponentSourceAnchor[], options?: { pinPreview?: boolean }) => void
  onPreviewAnchors?: (anchors: ReaderComponentSourceAnchor[], options?: { pinPreview?: boolean }) => void
  onHidePreview?: () => void
  onNodeAction?: (node: ReaderComponentNode, action: 'regenerate' | 'degrade') => void
  onInlineQuery?: (node: ReaderComponentNode, question: string) => Promise<void> | void
  onDropMarkdown?: (markdown: string, node?: ReaderComponentNode) => void
  onManualInsertSlot?: (nodeId: string) => void
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

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
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
      quote_text: typeof row.quote_text === 'string' ? row.quote_text : undefined,
      bbox_hint: row.bbox_hint as ReaderComponentSourceAnchor['bbox_hint'],
    })
  }
  return rows
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
  if (node.type === 'ParagraphProse') return text('text')
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

function ActionBar(props: {
  node: ReaderComponentNode
  ctx: ReaderComponentRenderContext
  extraActions?: ReactNode
}): ReactNode {
  const { node, ctx, extraActions } = props
  const [hovered, setHovered] = useState(false)
  const markdown = componentToMarkdown(node)
  const canJump = Array.isArray(node.source_anchor_refs) && node.source_anchor_refs.length > 0
  const darkTheme = isDarkTheme(ctx)
  const idleOpacity = darkTheme ? 0.62 : 0.9
  const actionBtnStyle: CSSProperties = {
    color: ctx?.themeStyle?.bodyColor,
    borderColor: ctx?.themeStyle?.borderColor,
  }
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
        <Button
          size="small"
          icon={<ReloadOutlined />}
          style={actionBtnStyle}
          onClick={() => ctx.onNodeAction?.(node, 'regenerate')}
        >
          修复
        </Button>
        <Button
          size="small"
          icon={<ShrinkOutlined />}
          style={actionBtnStyle}
          onClick={() => ctx.onNodeAction?.(node, 'degrade')}
        >
          降级
        </Button>
        <Button
          size="small"
          icon={<LinkOutlined />}
          style={actionBtnStyle}
          disabled={!canJump}
          onClick={() => ctx.onJumpAnchor?.(node.source_anchor_refs || [], { pinPreview: true })}
        >
          定位到证据
        </Button>
        <Button
          size="small"
          style={actionBtnStyle}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(markdown)
              message.success('已复制为 Markdown')
            } catch {
              message.warning('复制失败，请检查浏览器权限')
            }
          }}
        >
          复制Markdown
        </Button>
        {extraActions}
      </Space>
    </div>
  )
}
function DraggableContainer(props: {
  node: ReaderComponentNode
  children: ReactNode
}): ReactNode {
  const { node, children } = props
  const markdown = componentToMarkdown(node)
  return (
    <div
      draggable
      onDragStart={(event) => {
        const payload = JSON.stringify({ node, markdown })
        event.dataTransfer.setData('application/x-reader-component+json', payload)
        event.dataTransfer.setData('text/markdown', markdown)
        event.dataTransfer.setData('text/plain', markdown)
      }}
      style={{ cursor: 'grab' }}
    >
      {children}
      <div style={{ marginTop: 6 }}>
        <Tag icon={<DragOutlined />} color="default">可拖拽到右侧工作区</Tag>
      </div>
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
  const [hovered, setHovered] = useState(false)

  return withAnchorPreview(
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ position: 'relative', marginBottom: 14 }}
    >
      <DraggableContainer node={node}>
        <div>
          <ActionBar node={node} ctx={ctx} />
          <p
            style={{
              margin: 0,
              lineHeight: ctx.themeStyle?.bodyLineHeight || 1.95,
              fontSize: ctx.themeStyle?.bodyFontSize || 18,
              textAlign: 'justify',
              color: ctx.themeStyle?.bodyColor,
              fontFamily: ctx.themeStyle?.bodyFontFamily,
            }}
          >
            {text}
            {renderChildren(node.children || [], ctx)}
          </p>
        </div>
      </DraggableContainer>

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
    </div>
  )
}

export function renderReaderNode(node: ReaderComponentNode, ctx: ReaderComponentRenderContext): ReactNode {
  const props = node.props || {}
  const anchorRefs = normalizeAnchorRows(node.source_anchor_refs)

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
          anchor: normalizeAnchorRows(row.anchor),
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
      const imageUrl = asString(props.image_url)
      const sourceLabel = asString(props.source_label)
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
              <img
                src={imageUrl}
                alt={caption || 'figure'}
                style={{ width: '100%', maxHeight: 360, objectFit: 'cover', borderRadius: 10 }}
              />
            ) : null}
            {caption ? <Text type="secondary" style={{ display: 'block', marginTop: 10, color: ctx?.themeStyle?.bodyColor, opacity: 0.85 }}>{caption}</Text> : null}
            {sourceLabel ? <Tag style={{ marginTop: 8 }}>{sourceLabel}</Tag> : null}
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
