import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Empty,
  Space,
  Tag,
  Typography,
} from 'antd'

import type {
  ReaderComposeAsset,
  ReaderComponentNode,
} from '@/services/api'
import {
  mapComposeStyleIntentToKey,
  resolveGenerativeStyleTokens,
} from './generativeStyles'
import {
  GenerativeExperienceRenderer,
  type ExperienceLayoutVariant,
} from './GenerativeExperienceRenderer'
import { buildReaderExperiencePrimitives } from './experienceReaderPrimitives'
import { useReaderSurfaceLoader } from './readerSurfaceLoader'
import { useExperienceActionBus } from './useExperienceActionBus'
import type { ReaderComponentRenderContext } from './readerComponents'
import './composedReader.css'

const { Text } = Typography
const READER_API_BASE_URL = String(
  ((import.meta as any).env?.VITE_API_BASE_URL as string) || 'http://localhost:8888',
)

const READING_FLOW_COMPONENT_TYPES = new Set([
  'SectionHeading',
  'Separator',
  'ParagraphProse',
  'ListBlock',
  'FigurePanel',
  'TablePanel',
  'EquationBlock',
  'AbstractCard',
  'MethodologyCard',
  'CalloutBox',
  'CompareInsightsCard',
  'InsightClusterCard',
  'SectionBridgeCard',
  'InlineQuerySlot',
  'AnswerCard',
])

const CONTEXT_ONLY_COMPONENT_TYPES = new Set([
  'PaperHeaderCard',
  'MetadataSidebarCard',
  'ContextRail',
  'SectionTOC',
  'CitationLinks',
  'CitationCard',
  'PdfSnippetCard',
  'KeyTakeaways',
  'AnnotationRail',
  'QualityBadge',
  'QualityPanel',
])

function normalizeExperienceLayoutVariant(raw: string): ExperienceLayoutVariant {
  const token = String(raw || '').trim()
  if (token === 'focus_figure_split' || token === 'guided_story_stack' || token === 'explainer_first') {
    return token
  }
  return 'resource_augmented_reader'
}

function isDoneLikeStatus(value: unknown): boolean {
  const token = String(value || '').trim().toLowerCase()
  return token === 'done' || token === 'completed' || token === 'complete'
}

function hasProvisionalStatusToken(value: unknown): boolean {
  const token = String(value || '').trim().toLowerCase()
  if (!token) return false
  return (
    token.includes('seed')
    || token.includes('draft')
    || token.includes('fallback')
    || token.includes('provisional')
  )
}

function isSeedExperiencePlan(
  response: { plan?: { meta?: Record<string, unknown> | null } | null; experience_cache_layer?: string | null } | null | undefined,
): boolean {
  const meta = (response?.plan?.meta && typeof response.plan.meta === 'object')
    ? response.plan.meta
    : null
  return Boolean(meta?.seed_plan || response?.experience_cache_layer === 'derived_seed')
}

function isCompletedFinalExperiencePlan(
  response: {
    plan?: { status?: string | null; meta?: Record<string, unknown> | null; teaching_manuscript?: { status?: string | null } | null } | null
    compose_status?: string | null
    experience_cache_layer?: string | null
  } | null | undefined,
): boolean {
  if (!response || !response.plan) return false
  if (isSeedExperiencePlan(response)) return false
  if (!isDoneLikeStatus(response.plan.status)) return false
  if (hasProvisionalStatusToken(response.compose_status)) return false
  if (hasProvisionalStatusToken(response.experience_cache_layer)) return false
  const meta = (response.plan.meta && typeof response.plan.meta === 'object')
    ? response.plan.meta
    : null
  if (meta?.seed_plan || meta?.provisional || meta?.is_provisional || meta?.final_surface_ready === false) return false
  const fallbackReason = String(meta?.fallback_reason || '').trim().toLowerCase()
  if (fallbackReason) return false
  const manuscriptStatus = String(response.plan.teaching_manuscript?.status || '').trim()
  if (manuscriptStatus && !isDoneLikeStatus(manuscriptStatus)) return false
  if (hasProvisionalStatusToken(manuscriptStatus)) return false
  return true
}

function classifyFinalManuscriptSurfaceState(params: {
  planError: string | null
  hasPlan: boolean
  hasPrimaryExperience: boolean
  composeLoading: boolean
  planLoading: boolean
  backgroundRefreshing: boolean
  seedPlan: boolean
}): { title: string; description: string } | null {
  const {
    planError,
    hasPlan,
    hasPrimaryExperience,
    composeLoading,
    planLoading,
    backgroundRefreshing,
    seedPlan,
  } = params
  if (composeLoading || planLoading || backgroundRefreshing) return null
  if (hasPrimaryExperience) return null
  if (seedPlan || hasPlan) {
    return {
      title: '体验内容生成中',
      description: '当前页面正在完善阅读体验内容，请稍后刷新。',
    }
  }
  if (String(planError || '').trim()) {
    return {
      title: '体验内容暂未就绪',
      description: String(planError || '').trim(),
    }
  }
  return {
    title: '体验内容暂未就绪',
    description: '请稍后刷新，或先返回阅读器继续阅读。',
  }
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function buildReaderSignalSnippet(raw: unknown, maxChars: number = 52): string {
  const text = String(raw || '').trim().replace(/\s+/g, ' ')
  if (!text) return ''
  const sentence = (text.match(/[^。！？!?]+[。！？!?]?/) || [text])[0].trim()
  if (sentence.length <= maxChars) return sentence
  return `${sentence.slice(0, Math.max(0, maxChars - 1)).trim()}…`
}

function toAbsoluteApiUrl(rawUrl: string): string {
  const token = String(rawUrl || '').trim()
  if (!token) return ''
  if (/^https?:\/\//i.test(token) || token.startsWith('data:') || token.startsWith('blob:')) return token
  if (!token.startsWith('/')) return token
  return `${READER_API_BASE_URL}${token}`
}

function collectReaderNodeTextHints(node: ReaderComponentNode): string[] {
  const props = ((node.props && typeof node.props === 'object') ? node.props : {}) as Record<string, unknown>
  const hints = [
    props.text,
    props.title,
    props.caption,
    props.content,
    props.description,
    props.doi,
    props.label,
    props.subtitle,
  ]
  return hints.map((item) => String(item || '').trim()).filter(Boolean)
}

function isLikelyContextOnlyText(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  if (/^(?:research article|open access|corresponding author|supplementary material)$/i.test(text)) return true
  if (text.length <= 240 && /(?:https?:\/\/)?(?:dx\.)?doi\.org\/\S+/i.test(text)) return true
  if (text.length <= 180 && /^doi:\s*10\.\S+/i.test(text)) return true
  if (text.length <= 140 && /\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b/i.test(text)) return true
  return false
}

function getReaderNodePlacement(node: ReaderComponentNode): 'main' | 'context' {
  const type = String(node.type || '').trim()
  if (CONTEXT_ONLY_COMPONENT_TYPES.has(type)) return 'context'
  if (READING_FLOW_COMPONENT_TYPES.has(type)) return 'main'

  const zoneType = String(node.zone_type || '').trim().toLowerCase()
  const columnId = String(node.column_id || '').trim().toLowerCase()
  const region = String(node.region || '').trim().toLowerCase()
  if (zoneType === 'side_context') return 'context'
  if (columnId === 'sidebar' || region === 'sidebar') return 'context'

  if (type === 'ParagraphProse' || type === 'ListBlock' || type === 'CalloutBox' || type === 'SectionHeading') {
    const hints = collectReaderNodeTextHints(node)
    if (hints.some((item) => isLikelyContextOnlyText(item))) return 'context'
  }
  return 'main'
}

function sanitizeExperienceNode(node: ReaderComponentNode): ReaderComponentNode {
  const nextNode: ReaderComponentNode = {
    ...node,
    props: { ...(node.props || {}) },
  }
  if (nextNode.type === 'FigurePanel' && nextNode.props && typeof nextNode.props === 'object') {
    delete (nextNode.props as Record<string, unknown>).ai_insight
  }
  if (Array.isArray(node.children) && node.children.length) {
    nextNode.children = node.children.map((child) => sanitizeExperienceNode(child))
  }
  return nextNode
}

function buildNodeTargetId(page: number, node: ReaderComponentNode): string {
  return `p${page}:${String(node.id || '').trim()}`
}

function targetMatchesNodeId(targetId: string, node: ReaderComponentNode, page: number): boolean {
  const token = String(targetId || '').trim()
  const nodeId = String(node.id || '').trim()
  if (!token || !nodeId) return false
  return token === nodeId || token === buildNodeTargetId(page, node) || token.endsWith(`:${nodeId}`)
}

export default function PaperReaderExperiencePage() {
  const { paperId } = useParams<{ paperId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialPage = Number(searchParams.get('page') || '1')
  const initialKbId = Number(searchParams.get('kb') || '0')
  const initialIntent = searchParams.get('intent') || ''
  const initialReader = searchParams.get('reader') || 'curious_generalist'

  const [focusPage] = useState(Number.isFinite(initialPage) && initialPage > 0 ? initialPage : 1)
  const [selectedKbId] = useState(Number.isFinite(initialKbId) && initialKbId > 0 ? initialKbId : 0)
  const [userIntent] = useState(initialIntent)
  const [readerProfile] = useState(initialReader)
  const [reloadState, setReloadState] = useState({ nonce: 0, forceFresh: false })

  const numericPaperId = Number(paperId || 0)

  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('page', String(focusPage))
      if (selectedKbId > 0) next.set('kb', String(selectedKbId))
      else next.delete('kb')
      if (userIntent.trim()) next.set('intent', userIntent.trim())
      else next.delete('intent')
      if (readerProfile.trim()) next.set('reader', readerProfile.trim())
      else next.delete('reader')
      return next
    }, { replace: true })
  }, [focusPage, readerProfile, selectedKbId, setSearchParams, userIntent])

  const {
    composePayload,
    experienceResponse,
    composeError,
    planError,
    composeLoading,
    planLoading,
    backgroundRefreshing,
    surfaceLoadState,
  } = useReaderSurfaceLoader({
    mode: 'experience',
    paperId: numericPaperId,
    page: focusPage,
    selectedKbId,
    userIntent,
    readerProfile,
    reloadState,
  })

  useEffect(() => {
    if (!reloadState.forceFresh) return
    if (surfaceLoadState === 'ready' || surfaceLoadState === 'partial_error' || surfaceLoadState === 'hard_error') {
      setReloadState((prev) => ({ nonce: prev.nonce, forceFresh: false }))
    }
  }, [reloadState.forceFresh, surfaceLoadState])

  const themeStyle = useMemo(() => {
    const key = mapComposeStyleIntentToKey('reader_workbench')
    return resolveGenerativeStyleTokens(key, 'light')
  }, [])

  const effectiveComposePayload = composePayload || experienceResponse?.compose_payload || null
  const experiencePlan = experienceResponse?.plan || null
  const generativePlan = experienceResponse?.generative_plan || effectiveComposePayload?.generative_reader_plan || null
  const layoutVariant = normalizeExperienceLayoutVariant(experiencePlan?.layout_variant || String(experiencePlan?.meta?.layout_variant || ''))
  const manuscript = experiencePlan?.teaching_manuscript || null
  const hasPrimaryExperienceSignals = Boolean(
    (experiencePlan?.hero && (String(experiencePlan.hero.display_summary || experiencePlan.hero.summary || '').trim() || String(experiencePlan.hero.display_title || experiencePlan.hero.title || '').trim()))
    || (Array.isArray(experiencePlan?.main_sections) && experiencePlan.main_sections.length > 0)
    || (Array.isArray(experiencePlan?.guided_beats) && experiencePlan.guided_beats.length > 0)
    || (Array.isArray(experiencePlan?.supporting_resources) && experiencePlan.supporting_resources.length > 0)
    || (Array.isArray(experiencePlan?.interactive_blocks) && experiencePlan.interactive_blocks.length > 0)
    || (Array.isArray(experiencePlan?.widget_blocks) && experiencePlan.widget_blocks.length > 0),
  )
  const hasCompletedFinalExperience = isCompletedFinalExperiencePlan(experienceResponse)
  const hasPrimaryExperience = hasCompletedFinalExperience && hasPrimaryExperienceSignals

  const composedAssets = useMemo<ReaderComposeAsset[]>(
    () => (Array.isArray(effectiveComposePayload?.assets) ? effectiveComposePayload.assets : []),
    [effectiveComposePayload?.assets],
  )
  const readerPrimitives = useMemo(
    () => buildReaderExperiencePrimitives({ experiencePlan, generativePlan }),
    [experiencePlan, generativePlan],
  )
  const contextCards = useMemo(() => {
    const cards: Array<{ key: string; title: string; body: ReactNode }> = []
    if (readerPrimitives.terms.length || readerPrimitives.backgroundTopics.length) {
      cards.push({
        key: 'context',
        title: '阅读前需了解',
        body: (
          <div className="reader-experience-page__chip-cloud">
            {readerPrimitives.terms.map((item) => <Tag key={`term-${item}`}>{item}</Tag>)}
            {readerPrimitives.backgroundTopics.map((item) => <Tag key={`topic-${item}`} color="orange">{item}</Tag>)}
          </div>
        ),
      })
    }
    if (readerPrimitives.visibleClaims.length || readerPrimitives.hooks.length) {
      const claimSignalRows = readerPrimitives.visibleClaims
        .slice(0, 2)
        .map((claim) => ({
          id: claim.claim_id,
          compact: buildReaderSignalSnippet(preferDisplayCopy(claim.display_text, claim.text)),
        }))
        .filter((item) => item.compact)
      cards.push({
        key: 'signals',
        title: '阅读线索',
        body: (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {claimSignalRows.length ? (
              <details className="reader-experience-page__source-toggle">
                <summary>
                  <Text className="reader-experience-page__eyebrow">查看本页线索</Text>
                  <Text type="secondary">{`${claimSignalRows.length} 条`}</Text>
                </summary>
                <div className="reader-experience-page__source-toggle-body">
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    {claimSignalRows.map((item) => (
                      <Text key={item.id} type="secondary">{item.compact}</Text>
                    ))}
                  </Space>
                </div>
              </details>
            ) : null}
            {readerPrimitives.hooks.length ? (
              <div className="reader-experience-page__chip-cloud">
                {readerPrimitives.hooks.map((hook, index) => <Tag key={`hook-${index}`} color="blue">{hook}</Tag>)}
              </div>
            ) : null}
          </Space>
        ),
      })
    }
    return cards
  }, [readerPrimitives.backgroundTopics, readerPrimitives.hooks, readerPrimitives.terms, readerPrimitives.visibleClaims])
  const composedPageImageUrl = String(
    ((effectiveComposePayload as unknown as { docmind_structure?: { page_image_url?: unknown } })?.docmind_structure?.page_image_url)
    || '',
  ).trim()

  const resolveFigureImageUrl = useCallback((rawUrl: string, node?: ReaderComponentNode): string => {
    const token = String(rawUrl || '').trim()
    if (!token) return ''
    const assetPage = (() => {
      const pages = Array.isArray(node?.source_anchor_refs)
        ? node.source_anchor_refs
          .map((item) => Number(item?.page || 0))
          .filter((item) => Number.isFinite(item) && item > 0)
        : []
      return pages[0] || focusPage
    })()
    const sourceBlockIds = Array.isArray(node?.source_block_ids)
      ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    const pickImageHintUrl = (): string => {
      for (const asset of composedAssets) {
        if (asset.kind !== 'image_hint') continue
        const meta = (asset.meta && typeof asset.meta === 'object') ? asset.meta as Record<string, unknown> : {}
        const candidateUrl = String(asset.href || meta.image_url || '').trim()
        if (!candidateUrl || candidateUrl.startsWith('data:image/')) continue
        const layoutUniqueId = String(meta.layout_unique_id || meta.unique_id || '').trim()
        if (!sourceBlockIds.length || (layoutUniqueId && sourceBlockIds.includes(layoutUniqueId))) {
          return toAbsoluteApiUrl(candidateUrl)
        }
      }
      return ''
    }
    if (token.startsWith('data:image/')) return token
    if (token.startsWith('asset:')) {
      const assetId = token.slice('asset:'.length).trim()
      for (const asset of composedAssets) {
        if (asset.kind !== 'image_hint') continue
        const meta = (asset.meta && typeof asset.meta === 'object') ? asset.meta as Record<string, unknown> : {}
        const candidateId = String(meta.asset_id || meta.layout_unique_id || meta.unique_id || '').trim()
        const candidateUrl = String(asset.href || meta.image_url || '').trim()
        if (candidateUrl.startsWith('data:image/')) continue
        if (assetId && candidateId && candidateId === assetId && candidateUrl) {
          return toAbsoluteApiUrl(candidateUrl)
        }
      }
      if (assetId) return toAbsoluteApiUrl(`/api/v1/literature/reader/figure-assets/${numericPaperId}/${assetPage}/${assetId}`)
      return composedPageImageUrl ? toAbsoluteApiUrl(composedPageImageUrl) : ''
    }
    if (/^https?:\/\/(?:dx\.)?doi\.org\//i.test(token) && composedPageImageUrl) {
      return toAbsoluteApiUrl(composedPageImageUrl)
    }
    if (!token.startsWith('/') && !/^https?:\/\//i.test(token) && !token.startsWith('blob:') && !token.startsWith('data:')) {
      const hinted = pickImageHintUrl()
      if (hinted) return hinted
    }
    return toAbsoluteApiUrl(token)
  }, [composedAssets, composedPageImageUrl, focusPage, numericPaperId])

  const renderCtx: ReaderComponentRenderContext = useMemo(() => ({
    themeStyle,
    qualityReport: effectiveComposePayload?.quality_report || null,
    readOnly: true,
    resolveFigureImageUrl: (imageUrl, node) => resolveFigureImageUrl(imageUrl, node),
  }), [effectiveComposePayload?.quality_report, resolveFigureImageUrl, themeStyle])

  const mainComponents = useMemo(() => (
    (effectiveComposePayload?.ui_plan?.components || [])
      .filter((node) => getReaderNodePlacement(node) === 'main')
      .map((node) => sanitizeExperienceNode(node))
  ), [effectiveComposePayload])

  const primaryFocusTargetId = String(experiencePlan?.hero?.target_ids?.[0] || '').trim()
  const { activeTargetId, lastUiEvent, dispatchBlockAction, getBlockUiAction } = useExperienceActionBus({
    paperId: numericPaperId,
    focusPage,
    primaryFocusTargetId,
  })
  const effectiveFocusTargetId = activeTargetId || primaryFocusTargetId

  const focusNode = useMemo(() => {
    if (!effectiveFocusTargetId) {
      return mainComponents.find((node) => String(node.type || '').trim() === 'FigurePanel') || null
    }
    return mainComponents.find((node) => targetMatchesNodeId(effectiveFocusTargetId, node, focusPage)) || null
  }, [effectiveFocusTargetId, focusPage, mainComponents])

  const readingFlowNodes = useMemo(() => {
    if (!focusNode) return mainComponents
    const focusId = String(focusNode.id || '').trim()
    return mainComponents.filter((node) => String(node.id || '').trim() !== focusId)
  }, [focusNode, mainComponents])

  const storyTitle = (() => {
    const title = String(experiencePlan?.hero?.display_title || experiencePlan?.page_story_title || '').trim()
    if (title && title.toLowerCase() !== 'fig 3') return title
    return `论文 ${numericPaperId} 阅读体验`
  })()

  const focusHeading = String(preferDisplayCopy(experiencePlan?.hero?.display_title, experiencePlan?.hero?.title)).trim() || storyTitle

  const isSeedState = surfaceLoadState === 'showing_seed'
    || isSeedExperiencePlan(experienceResponse)
    || (Boolean(experienceResponse) && !hasCompletedFinalExperience)
  const surfaceState = classifyFinalManuscriptSurfaceState({
    planError,
    hasPlan: Boolean(experienceResponse?.plan),
    hasPrimaryExperience,
    composeLoading,
    planLoading,
    backgroundRefreshing,
    seedPlan: isSeedState,
  })

  const topStatusText = (() => {
    if ((composeLoading || planLoading) && !hasPrimaryExperience) return '体验生成中'
    if (hasPrimaryExperience && backgroundRefreshing) return '体验已就绪（后台更新）'
    if (hasPrimaryExperience && experienceResponse?.experience_cache_hit) return '体验已就绪'
    if (hasPrimaryExperience) return '体验已更新'
    if (backgroundRefreshing || isSeedState) return '体验收敛中'
    if (planError) return '体验暂未就绪'
    return '等待体验'
  })()

  const queryToken = useMemo(() => {
    const next = new URLSearchParams()
    next.set('page', String(focusPage))
    if (selectedKbId > 0) next.set('kb', String(selectedKbId))
    if (readerProfile.trim()) next.set('reader', readerProfile.trim())
    if (userIntent.trim()) next.set('intent', userIntent.trim())
    return next.toString()
  }, [focusPage, readerProfile, selectedKbId, userIntent])

  return (
    <div className="reader-experience-page">
      <div className="reader-experience-page__header">
        <div className="reader-experience-page__hero-copy">
          <div className="reader-experience-page__status-row">
            <Text className="reader-experience-page__eyebrow">阅读体验</Text>
            <span className="reader-experience-page__status-chip">{topStatusText}</span>
          </div>
          <Text type="secondary">{`论文 ${numericPaperId} · 第 ${focusPage} 页`}</Text>
        </div>
        <div className="reader-experience-page__header-actions">
          <Space wrap size={10}>
            <Button
              onClick={() => setReloadState((prev) => ({ nonce: prev.nonce + 1, forceFresh: true }))}
              loading={composeLoading || planLoading || backgroundRefreshing}
            >
              刷新体验
            </Button>
            <Link to={`/literature/${numericPaperId}/workbench-v2?${queryToken}`}>打开 Workbench</Link>
            <Link to={`/literature/${numericPaperId}/read?page=${focusPage}`}>返回阅读器</Link>
          </Space>
        </div>
      </div>

      {composeError ? <Alert type="error" showIcon message={composeError} className="reader-experience-page__alert" /> : null}
      {!composeError && planError && !hasCompletedFinalExperience ? (
        <Alert
          type="warning"
          showIcon
          message="体验内容还在生成"
          description="系统已在后台继续完善内容。"
          className="reader-experience-page__alert"
        />
      ) : null}
      {surfaceState ? (
        <Card className="reader-experience-page__panel reader-experience-page__panel--empty">
          <Empty
            description={
              <Space direction="vertical" size={4}>
                <Text strong>{surfaceState.title}</Text>
                <Text type="secondary">{surfaceState.description}</Text>
              </Space>
            }
          />
        </Card>
      ) : null}

      {!surfaceState && hasPrimaryExperience ? (
        <GenerativeExperienceRenderer
          renderMode="full"
          layoutVariant={layoutVariant}
          hero={experiencePlan?.hero || null}
          focusHeading={focusHeading}
          visibleClaims={readerPrimitives.visibleClaims}
          contextCards={contextCards}
          narrativeSections={experiencePlan?.main_sections || []}
          guidedBeats={experiencePlan?.guided_beats || []}
          teachingManuscript={manuscript}
          toolEnrichmentPacket={(
            (experiencePlan?.meta as Record<string, unknown> | undefined)?.tool_enrichment_packet as Record<string, unknown> | undefined
          ) || ({} as Record<string, unknown>)}
          focusNode={focusNode}
          bodyFlowNodes={mainComponents}
          readingFlowNodes={readingFlowNodes}
          renderCtx={renderCtx}
          composeLoading={composeLoading}
          hasComposePayload={Boolean(effectiveComposePayload)}
          backgroundRefreshing={backgroundRefreshing}
          fallbackQuestionAnswers={readerPrimitives.fallbackQuestionAnswers}
          resourceModules={experiencePlan?.supporting_resources || []}
          interactionModules={experiencePlan?.interactive_blocks || []}
          widgetBlocks={experiencePlan?.widget_blocks || []}
          getBlockUiAction={getBlockUiAction}
          dispatchBlockAction={dispatchBlockAction}
          lastUiEvent={lastUiEvent}
          topStatusText={topStatusText}
          seedMode={false}
        />
      ) : null}
    </div>
  )
}
