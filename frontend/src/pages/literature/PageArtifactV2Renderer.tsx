import { useMemo } from 'react'
import { Alert, Collapse, Divider, Image, Layout, Space, Tag, Typography } from 'antd'
import { ProCard } from '@ant-design/pro-components'

import type { PageArtifactV2, PageArtifactV2ReadingBlock, PageArtifactV2SegmentKind } from '@/services/api'

import PageArtifactV2ReaderOpening from './PageArtifactV2ReaderOpening'
import './pageArtifactV2.css'

const { Paragraph, Text, Title } = Typography
const { Content, Sider } = Layout

type PageArtifactV2RendererProps = {
  artifact: PageArtifactV2
  mode?: 'reader' | 'workbench'
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

const SUPPORT_SEGMENT_KINDS = new Set<PageArtifactV2SegmentKind>([
  'term_annotation',
  'external_resource',
  'aside_content',
])

const RAIL_HINTS = new Set([
  'rail',
  'side',
  'sidebar',
  'side-rail',
  'support-rail',
  'support_rail',
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

function trimTrailingSentencePunctuation(raw: string): string {
  return String(raw || '').trim().replace(/[。.!！?？:：;；、，,\s]+$/g, '')
}

function trimLeadingSentencePunctuation(raw: string): string {
  return String(raw || '').trim().replace(/^[。.!！?？:：;；、，,\s]+/g, '')
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
  return RAIL_HINTS.has(placement) || RAIL_HINTS.has(lane)
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
      <ProCard key={block.segment_id} className={cardClassName} bodyStyle={{ padding: 16 }}>
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
    )
  }

  if (block.segment_kind === 'external_resource') {
    const url = String(meta.url || '').trim()
    return (
      <ProCard key={block.segment_id} className={cardClassName} bodyStyle={{ padding: 16 }}>
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
    )
  }

  return (
    <ProCard key={block.segment_id} className={cardClassName} bodyStyle={{ padding: 16 }}>
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
      <section key={block.segment_id} className={`page-artifact-v2__media ${blockClassName}`}>
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
      <section key={block.segment_id} className={`${blockClassName} page-artifact-v2__excerpt`}>
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
      <section key={block.segment_id} className={`${blockClassName} page-artifact-v2__heading-block`}>
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

  if (block.segment_kind === 'paragraph') {
    return (
      <section key={block.segment_id} className={blockClassName}>
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
    <section key={block.segment_id} className={blockClassName}>
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
  const spineMeta = (artifact.current_page_spine?.meta || {}) as Record<string, unknown>
  const excerptCoverageMeta = (spineMeta.excerpt_coverage || {}) as Record<string, unknown>
  const artifactMeta = (artifact.meta || {}) as Record<string, unknown>
  const readerOpeningMeta = ((artifactMeta.reader_opening || {}) as Record<string, unknown>)
  const readerOutroMeta = ((artifactMeta.reader_outro || {}) as Record<string, unknown>)

  const derived = useMemo(() => {
    const readingBlocks = artifact.reading_blocks || []
    const supportBlocks = readingBlocks.filter((block) => SUPPORT_SEGMENT_KINDS.has(block.segment_kind))
    const railBlocks = readingBlocks.filter((block) => shouldRenderInRail(block))
    const flowBlocks = readingBlocks.filter((block) => !shouldRenderInRail(block))
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

    for (const block of flowBlocks) {
      const meta = block.meta || {}
      const explicitGroupId = String(meta.group_id || meta.section_id || '').trim()
      const explicitGroupLabel = String(meta.group_label || meta.section_label || '').trim()
      const shouldStartNewGroup = block.segment_kind === 'heading' || Boolean(explicitGroupId) || currentGroup === null

      if (shouldStartNewGroup) {
        const nextGroupId = explicitGroupId || `group-${++fallbackGroupIndex}`
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
      useSideRail,
    }
  }, [artifact])

  const heroTitle =
    cleanLeadCopy(derived.firstHeading?.text || '')
    || cleanLeadCopy(derived.firstParagraph?.text || '')
    || `第 ${artifact.focus_page} 页的阅读主线`
  const heroSubtitle = cleanLeadCopy(String(readerOpeningMeta.summary || derived.firstParagraph?.text || derived.firstExplanation?.text || ''))
  const readerOpeningPoints = getStringList(readerOpeningMeta.key_points, 4, 180)
  const previousPageBridge = getReaderBridge(readerOpeningMeta.previous_page_bridge)
  const nextPageBridge = getReaderBridge(readerOutroMeta.next_page_bridge)
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
                    return group.heading && !shouldHideHeadingAsHeroDuplicate ? (
                    <header className="page-artifact-v2__section-heading">
                      {mode === 'workbench' ? (
                        <div className="page-artifact-v2__block-eyebrow">
                          <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--support" />
                          <span className="page-artifact-v2__block-label">阅读引导</span>
                        </div>
                      ) : null}
                      <Title level={3} className="page-artifact-v2__heading-text">
                        {group.heading.text}
                      </Title>
                    </header>
                    ) : null
                  })()}

                  <div className="page-artifact-v2__section-body">
                    {group.blocks.map((block) => (
                      <div key={block.segment_id} className="page-artifact-v2__main-item">
                        {renderMainBlock(block, derived.mainSegmentIds.has(block.segment_id), mode)}
                      </div>
                    ))}
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
                {derived.railBlocks.map((block) => renderSupportCard(block, mode, 'rail'))}
              </div>
            </ProCard>
          </Sider>
        ) : null}
      </Layout>

      {mode === 'reader' && nextPageBridge ? (
        <ProCard className="page-artifact-v2__outro" bodyStyle={{ padding: 18 }}>
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
