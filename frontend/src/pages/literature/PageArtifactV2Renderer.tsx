import { useMemo } from 'react'
import { Alert, Divider, Image, Layout, Space, Tag, Typography } from 'antd'
import { ProCard } from '@ant-design/pro-components'

import type { PageArtifactV2, PageArtifactV2ReadingBlock, PageArtifactV2SegmentKind } from '@/services/api'

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

const SUPPORT_SEGMENT_KINDS = new Set<PageArtifactV2SegmentKind>([
  'term_annotation',
  'external_resource',
  'aside_content',
])

function toClassToken(raw: string): string {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function compactText(raw: string, maxLength: number): string {
  const text = String(raw || '').trim().replace(/\s+/g, ' ')
  if (!text) return ''
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength - 1).trimEnd()}…`
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
    return String(meta.reader_title || meta.label || '旁注').trim() || '旁注'
  }
  return String(meta.reader_title || meta.label || getBlockLabel(block)).trim() || '补充说明'
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

function renderSupportCard(block: PageArtifactV2ReadingBlock, mode: 'reader' | 'workbench') {
  const meta = block.meta || {}
  if (block.segment_kind === 'term_annotation') {
    return (
      <ProCard key={block.segment_id} className="page-artifact-v2__aside-card" bodyStyle={{ padding: 16 }}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--term" />
            <span className="page-artifact-v2__block-label">术语注释</span>
          </div>
        ) : null}
        <Title level={5} className="page-artifact-v2__aside-title">
          {mode === 'reader' ? getReaderSupportTitle(block) : String(meta.term || getBlockLabel(block)).trim()}
        </Title>
        <Paragraph className="page-artifact-v2__support-note">{block.text}</Paragraph>
      </ProCard>
    )
  }

  if (block.segment_kind === 'external_resource') {
    const url = String(meta.url || '').trim()
    return (
      <ProCard key={block.segment_id} className="page-artifact-v2__aside-card" bodyStyle={{ padding: 16 }}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--resource" />
            <span className="page-artifact-v2__block-label">延伸阅读</span>
          </div>
        ) : null}
        <Title level={5} className="page-artifact-v2__aside-title">
          {mode === 'reader' ? getReaderSupportTitle(block) : compactText(block.text, 88) || '外部资源'}
        </Title>
        <Paragraph className="page-artifact-v2__support-note">
          {String(meta.note || meta.description || '在读到这里时补一层背景，不打断当前页主线。').trim()}
        </Paragraph>
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
    <ProCard key={block.segment_id} className="page-artifact-v2__aside-card" bodyStyle={{ padding: 16 }}>
      {mode === 'workbench' ? (
        <div className="page-artifact-v2__block-eyebrow">
          <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--support" />
          <span className="page-artifact-v2__block-label">{getBlockLabel(block)}</span>
        </div>
      ) : null}
      <Title level={5} className="page-artifact-v2__aside-title">
        {mode === 'reader' ? getReaderSupportTitle(block) : String(meta.label || '旁注').trim()}
      </Title>
      <Paragraph className="page-artifact-v2__support-note">{block.text}</Paragraph>
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
  const blockClassName = [
    'page-artifact-v2__main-block',
    `page-artifact-v2__main-block--${block.segment_kind}`,
    block.source_lane === 'current_page'
      ? 'page-artifact-v2__main-block--current-page'
      : 'page-artifact-v2__main-block--authored',
    isGuided ? 'page-artifact-v2__main-block--guided' : '',
    placement ? `page-artifact-v2__main-block--placement-${toClassToken(placement)}` : '',
  ]
    .filter(Boolean)
    .join(' ')

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
    return (
      <section key={block.segment_id} className={`${blockClassName} page-artifact-v2__excerpt`}>
        {mode === 'workbench' ? (
          <div className="page-artifact-v2__block-eyebrow">
            <span className="page-artifact-v2__block-dot page-artifact-v2__block-dot--excerpt" />
            <span className="page-artifact-v2__block-label">原文片段</span>
          </div>
        ) : null}
        <Paragraph className="page-artifact-v2__excerpt-text">{block.text}</Paragraph>
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

  const derived = useMemo(() => {
    const readingBlocks = artifact.reading_blocks || []
    const supportBlocks = readingBlocks.filter((block) => SUPPORT_SEGMENT_KINDS.has(block.segment_kind))
    const mainBlocks = readingBlocks.filter((block) => !SUPPORT_SEGMENT_KINDS.has(block.segment_kind))
    const headingBlocks = readingBlocks.filter((block) => block.segment_kind === 'heading')
    const paragraphBlocks = readingBlocks.filter((block) => block.segment_kind === 'paragraph')
    const explanationBlocks = readingBlocks.filter((block) => (
      block.segment_kind === 'paragraph' || block.segment_kind === 'authored_explanation'
    ))
    const excerptBlocks = readingBlocks.filter((block) => block.segment_kind === 'original_excerpt')
    const mediaBlocks = readingBlocks.filter((block) => (
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
    const useSideRail = (
      mode === 'workbench'
      || artifact.presentation_mode === 'mixed_layout'
      || artifact.layout_recipe.includes('interleave')
    ) && supportBlocks.length > 0

    const mainBlockGroups: MainBlockGroup[] = []
    let fallbackGroupIndex = 0
    let currentGroup: MainBlockGroup | null = null

    for (const block of mainBlocks) {
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
      mainBlocks,
      mainBlockGroups,
      supportBlocks,
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
  }, [artifact, mode])

  const heroTitle =
    cleanLeadCopy(derived.firstHeading?.text || '')
    || cleanLeadCopy(derived.firstParagraph?.text || '')
    || `第 ${artifact.focus_page} 页的阅读主线`
  const heroSubtitle = cleanLeadCopy(derived.firstParagraph?.text || derived.firstExplanation?.text || '')
  const heroQuote = String(derived.firstExcerpt?.text || '').trim()
  const continuityCount = artifact.provenance?.adjacent_context_pages?.length || 0
  const coverageRatio = Number(spineMeta.coverage_ratio || excerptCoverageMeta.coverage_ratio || 0)
  const presentationToken = toClassToken(artifact.presentation_mode)
  const templateToken = toClassToken(artifact.template_id)
  const layoutToken = toClassToken(artifact.layout_recipe)
  const useEditorialFlow = artifact.presentation_mode === 'editorial' || artifact.layout_recipe.includes('editorial')
  const useMixedLayout = artifact.presentation_mode === 'mixed_layout' || artifact.layout_recipe.includes('interleave')
  const useGuidedFlow = artifact.presentation_mode === 'guided_reading' || artifact.template_id.startsWith('guided_') || artifact.interaction_policy.includes('guided')
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
  const readerInlineSupportTitle = useEditorialFlow ? '页边补充' : '术语与补充'
  const readerHighlights = [
    derived.mediaBlocks.length ? '以图表证据带读当前页主线' : '以正文主线带读当前页',
    derived.excerptBlocks.length ? '关键原文片段跟随讲解穿插出现' : '以中文讲解为主，不堆砌原文摘录',
    derived.supportBlocks.length ? '术语与旁注压在主线旁边补足信息' : '本页尽量保持单线阅读，不额外拉出支线',
  ]

  return (
    <section className={rootClassName}>
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
            {heroQuote ? (
              <div className="page-artifact-v2__hero-quote">
                {mode === 'workbench' ? (
                  <span className="page-artifact-v2__hero-quote-label">页内锚点</span>
                ) : null}
                <Paragraph className="page-artifact-v2__hero-quote-text">{heroQuote}</Paragraph>
              </div>
            ) : null}
          </div>

          <aside className="page-artifact-v2__hero-rail">
            <span className="page-artifact-v2__hero-mode">
              {artifact.presentation_mode.replace(/_/g, ' ')}
            </span>
            <div className="page-artifact-v2__hero-highlights">
              {mode === 'reader' ? readerHighlights.map((item) => (
                <div key={item} className="page-artifact-v2__hero-pill page-artifact-v2__hero-pill--copy">
                  <span className="page-artifact-v2__hero-pill-label">{item}</span>
                </div>
              )) : (
                <>
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
                </>
              )}
              {mode === 'workbench' ? (
                <div className="page-artifact-v2__hero-pill">
                  <span className="page-artifact-v2__hero-pill-value">{continuityCount}</span>
                  <span className="page-artifact-v2__hero-pill-label">邻页上下文</span>
                </div>
              ) : null}
            </div>

            {mode === 'workbench' ? (
              <div className="page-artifact-v2__contract-tags">
                <Tag>{artifact.template_id}</Tag>
                <Tag>{artifact.layout_recipe}</Tag>
                <Tag>{artifact.widget_family}</Tag>
                <Tag>{artifact.motion_preset}</Tag>
                <Tag>{artifact.interaction_policy}</Tag>
              </div>
            ) : null}
          </aside>
        </div>
      </ProCard>

      <Layout className={`page-artifact-v2__layout ${derived.useSideRail ? 'page-artifact-v2__layout--with-side' : ''}`}>
        <Content className="page-artifact-v2__content-shell">
          <main className="page-artifact-v2__main">
            {derived.mainBlockGroups.map((group, index) => (
              <div key={group.groupId} className="page-artifact-v2__group-shell">
                <section className="page-artifact-v2__section-group">
                  {group.groupLabel ? (
                    <div className="page-artifact-v2__section-kicker">{group.groupLabel}</div>
                  ) : null}
                  {group.heading ? (
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
                  ) : null}

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

            {!derived.useSideRail && derived.supportBlocks.length ? (
              <ProCard className="page-artifact-v2__inline-support" bodyStyle={{ padding: 22 }}>
                <div className="page-artifact-v2__inline-support-head">
                  <Title level={4} className="page-artifact-v2__inline-support-title">
                    {mode === 'reader' ? readerInlineSupportTitle : useEditorialFlow ? '页边补注' : '边读边补的注释与资源'}
                  </Title>
                  {mode === 'workbench' ? (
                    <Paragraph className="page-artifact-v2__inline-support-copy">
                      {useEditorialFlow
                        ? '采用更像编辑页边栏的方式补术语、资源和旁注，不切断当前页主线。'
                        : '这些内容仍然服务当前页主线，只是换一种更轻的方式跟在正文旁边。'}
                    </Paragraph>
                  ) : null}
                </div>
                <div className="page-artifact-v2__inline-support-grid">
                  {derived.supportBlocks.map((block) => renderSupportCard(block, mode))}
                </div>
              </ProCard>
            ) : null}
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
                {derived.supportBlocks.map((block) => renderSupportCard(block, mode))}
              </div>
            </ProCard>
          </Sider>
        ) : null}
      </Layout>

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
