import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  List,
  Space,
  Tag,
  Typography,
} from 'antd'

import type {
  ReaderComposeAsset,
  ReaderComponentNode,
  ReaderEnrichmentTarget,
  ReaderPageBrief,
  ReaderStorySubstrate,
} from '@/services/api'
import {
  mapComposeStyleIntentToKey,
  resolveGenerativeStyleTokens,
} from './generativeStyles'
import {
  GenerativeExperienceRenderer,
  type ExperienceLayoutVariant,
} from './GenerativeExperienceRenderer'
import { useReaderSurfaceLoader } from './readerSurfaceLoader'
import { useExperienceActionBus } from './useExperienceActionBus'
import type { ReaderComponentRenderContext } from './readerComponents'
import './composedReader.css'

const { Title, Text, Paragraph } = Typography
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

function hasNonDraftExperiencePlan(
  response: { plan?: { status?: string | null } | null } | null | undefined,
): boolean {
  const status = String(response?.plan?.status || '').trim().toLowerCase()
  return status === 'done' || status === 'fallback'
}

function classifyWorkbenchSurfaceState(params: {
  composeError: string | null
  planError: string | null
  hasComposePayload: boolean
  hasPlan: boolean
  composeLoading: boolean
  planLoading: boolean
  backgroundRefreshing: boolean
}): { title: string; description: string } | null {
  const {
    composeError,
    planError,
    hasComposePayload,
    hasPlan,
    composeLoading,
    planLoading,
    backgroundRefreshing,
  } = params
  const composeToken = String(composeError || '').trim().toLowerCase()
  const planToken = String(planError || '').trim().toLowerCase()
  if (composeLoading || planLoading || backgroundRefreshing) return null
  if (composeToken.includes('pdf')) {
    return {
      title: '论文 PDF 尚未就绪',
      description: '当前页还没有可用的 PDF 渲染结果，workbench 暂时无法构建调试视图。',
    }
  }
  if (composeToken.includes('no cached reader payload available') || composeToken.includes('暂无正文底座')) {
    return {
      title: '暂无正文底座缓存',
      description: '请先在阅读器打开这一页，触发 compose 底座生成后再回来查看 workbench。',
    }
  }
  if (!hasComposePayload && composeError) {
    return {
      title: '正文底座加载失败',
      description: composeError,
    }
  }
  if (hasComposePayload && !hasPlan && (planToken.includes('network error') || planToken.includes('增强计划暂未就绪'))) {
    return {
      title: '增强计划暂未就绪',
      description: '调试页已拿到底座内容，但完整体验计划还没返回。',
    }
  }
  if (hasComposePayload && !hasPlan) {
    return {
      title: '增强计划暂未就绪',
      description: '当前页已有正文底座，但还没有可展示的 experience plan。',
    }
  }
  return null
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function isEnglishHeavyReaderCopy(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  const cjkMatches = text.match(/[\u3400-\u9fff]/g) || []
  const latinMatches = text.match(/[A-Za-z]/g) || []
  if (cjkMatches.length > 0) return false
  return latinMatches.length >= 24 && latinMatches.length > cjkMatches.length * 4
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

function sanitizeWorkbenchNode(node: ReaderComponentNode): ReaderComponentNode {
  const nextNode: ReaderComponentNode = {
    ...node,
    props: { ...(node.props || {}) },
  }
  if (nextNode.type === 'FigurePanel' && nextNode.props && typeof nextNode.props === 'object') {
    delete (nextNode.props as Record<string, unknown>).ai_insight
  }
  if (Array.isArray(node.children) && node.children.length) {
    nextNode.children = node.children.map((child) => sanitizeWorkbenchNode(child))
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

function extractFirstFigureNode(nodes: ReaderComponentNode[]): ReaderComponentNode | null {
  return nodes.find((node) => String(node.type || '').trim() === 'FigurePanel') || null
}

function humanizeToken(token: string): string {
  const raw = String(token || '').trim()
  if (!raw) return ''
  const lower = raw.toLowerCase()
  if (lower === 'hero_summary') return '核心摘要'
  if (lower === 'focus_evidence') return '聚焦证据'
  if (lower === 'key_finding') return '关键发现'
  if (lower === 'context_explainer') return '背景解释'
  if (lower === 'explore_questions') return '继续追问'
  if (lower === 'reading_flow') return '正文阅读'
  if (lower === 'curious_generalist') return '泛读型读者'
  if (lower === 'paper_read') return '论文阅读工具'
  return raw
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function toAbsoluteApiUrl(rawUrl: string): string {
  const token = String(rawUrl || '').trim()
  if (!token) return ''
  if (/^https?:\/\//i.test(token) || token.startsWith('data:') || token.startsWith('blob:')) return token
  if (!token.startsWith('/')) return token
  return `${READER_API_BASE_URL}${token}`
}

function renderTarget(target: ReaderEnrichmentTarget) {
  return (
    <Card key={target.target_id} size="small" className="reader-workbench-debug__target-card">
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Space wrap>
          <Tag>{target.target_kind}</Tag>
          <Tag color="geekblue">{target.component_type}</Tag>
        </Space>
        <Text strong>{target.title || target.figure_label || target.section_label || target.target_id}</Text>
        {target.excerpt ? <Paragraph style={{ marginBottom: 0 }}>{target.excerpt}</Paragraph> : null}
        <Text type="secondary">Target ID: {target.target_id}</Text>
      </Space>
    </Card>
  )
}

function renderStorySubstrate(storySubstrate: ReaderStorySubstrate | undefined | null) {
  if (!storySubstrate) return <Empty description="No story substrate" />
  return (
    <Space direction="vertical" size={14} style={{ width: '100%' }}>
      {storySubstrate.main_claims?.length ? (
        <div className="reader-workbench-debug__story-block">
          <Text className="reader-workbench-debug__module-eyebrow">Main claims</Text>
          <List
            size="small"
            dataSource={storySubstrate.main_claims}
            renderItem={(item) => (
              <List.Item className="reader-workbench-debug__story-list-item">
                <Text>{String(item.display_text || item.text || '').trim()}</Text>
              </List.Item>
            )}
          />
        </div>
      ) : null}
      {storySubstrate.background_gaps?.length ? (
        <div className="reader-workbench-debug__story-block">
          <Text className="reader-workbench-debug__module-eyebrow">Background gaps</Text>
          <Space wrap>
            {storySubstrate.background_gaps.map((item, index) => (
              <Tag key={`gap-${index}`}>{item.topic}</Tag>
            ))}
          </Space>
        </div>
      ) : null}
      {storySubstrate.narrative_turns?.length ? (
        <div className="reader-workbench-debug__story-block">
          <Text className="reader-workbench-debug__module-eyebrow">Narrative turns</Text>
          <List
            size="small"
            dataSource={storySubstrate.narrative_turns}
            renderItem={(item) => (
              <List.Item className="reader-workbench-debug__story-list-item">
                <Text>{item.label || item.kind}</Text>
              </List.Item>
            )}
          />
        </div>
      ) : null}
    </Space>
  )
}

export default function PaperReaderWorkbenchPage() {
  const { paperId } = useParams<{ paperId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialPage = Number(searchParams.get('page') || '1')
  const initialKbId = Number(searchParams.get('kb') || '0')
  const initialIntent = searchParams.get('intent') || ''
  const initialReader = searchParams.get('reader') || 'curious_generalist'

  const [page, setPage] = useState(Number.isFinite(initialPage) && initialPage > 0 ? initialPage : 1)
  const [selectedKbId, setSelectedKbId] = useState(Number.isFinite(initialKbId) && initialKbId > 0 ? initialKbId : 0)
  const [userIntent, setUserIntent] = useState(initialIntent)
  const [readerProfile, setReaderProfile] = useState(initialReader)
  const [reloadState, setReloadState] = useState({ nonce: 0, forceFresh: false })

  const numericPaperId = Number(paperId || 0)

  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('page', String(page))
      if (selectedKbId > 0) next.set('kb', String(selectedKbId))
      else next.delete('kb')
      if (userIntent.trim()) next.set('intent', userIntent.trim())
      else next.delete('intent')
      if (readerProfile.trim()) next.set('reader', readerProfile.trim())
      else next.delete('reader')
      return next
    }, { replace: true })
  }, [page, readerProfile, selectedKbId, setSearchParams, userIntent])

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
    page,
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
  const generativePlan = experienceResponse?.generative_plan || null
  const layoutVariant = normalizeExperienceLayoutVariant(
    experiencePlan?.layout_variant || String(experiencePlan?.meta?.layout_variant || ''),
  )
  const enrichmentTargets = experienceResponse?.enrichment_bundle?.targets || effectiveComposePayload?.enrichment_bundle?.targets || []
  const storySubstrate = generativePlan?.story_substrate || null
  const pageBrief: ReaderPageBrief | null = generativePlan?.page_brief || null
  const mainSections = useMemo(() => experiencePlan?.main_sections || [], [experiencePlan?.main_sections])
  const experienceMeta = useMemo<Record<string, unknown>>(
    () => ((experiencePlan?.meta && typeof experiencePlan.meta === 'object') ? experiencePlan.meta : {}) as Record<string, unknown>,
    [experiencePlan?.meta],
  )
  const contractValidationStatus = String(
    (((experienceMeta.contract_validation as Record<string, unknown> | undefined) || {}).status as string | undefined) || '',
  ).trim()
  const displayCopyContract = String(experienceMeta.display_copy_contract || '').trim()
  const contentBudget = (pageBrief?.content_budget && typeof pageBrief.content_budget === 'object')
    ? pageBrief.content_budget as Record<string, number>
    : {}

  const composedAssets = useMemo<ReaderComposeAsset[]>(
    () => (Array.isArray(effectiveComposePayload?.assets) ? effectiveComposePayload.assets : []),
    [effectiveComposePayload?.assets],
  )
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
      return pages[0] || page
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
  }, [composedAssets, composedPageImageUrl, numericPaperId, page])

  const renderCtx: ReaderComponentRenderContext = useMemo(() => ({
    themeStyle,
    qualityReport: effectiveComposePayload?.quality_report || null,
    readOnly: true,
    resolveFigureImageUrl: (imageUrl, node) => resolveFigureImageUrl(imageUrl, node),
  }), [effectiveComposePayload?.quality_report, resolveFigureImageUrl, themeStyle])

  const mainComponents = useMemo(() => (
    (effectiveComposePayload?.ui_plan?.components || [])
      .filter((node) => getReaderNodePlacement(node) === 'main')
      .map((node) => sanitizeWorkbenchNode(node))
  ), [effectiveComposePayload])

  const hero = experiencePlan?.hero || null
  const storyTitle = (() => {
    const title = String(hero?.display_title || experiencePlan?.page_story_title || '').trim()
    if (title && title.toLowerCase() !== 'fig 3') return title
    return `论文 ${numericPaperId} 的体验页预览`
  })()
  const storySubtitle = preferDisplayCopy(
    hero?.display_subtitle,
    experiencePlan?.page_story_subtitle || 'workbench 预览与调试共用同一份 experience plan。',
  )
  const topSummary = pageBrief?.page_goal || generativePlan?.rationale?.[0] || '围绕正文补充公开资源，并生成页面级体验计划。'
  const composeMetaSummary = [
    experienceResponse?.compose_build_mode ? `compose ${experienceResponse.compose_build_mode}` : '',
    experienceResponse?.cache_layer ? `compose-cache ${experienceResponse.cache_layer}` : '',
    experienceResponse?.generative_plan_cache_layer ? `plan-cache ${experienceResponse.generative_plan_cache_layer}` : '',
    experienceResponse?.experience_cache_layer ? `experience-cache ${experienceResponse.experience_cache_layer}` : '',
  ].filter(Boolean).join(' · ')

  const readingPath = experiencePlan?.reading_path || []
  const primaryFocusTargetId = String(hero?.target_ids?.[0] || '').trim()
  const { activeTargetId, lastUiEvent, dispatchBlockAction, getBlockUiAction } = useExperienceActionBus({
    paperId: numericPaperId,
    focusPage: page,
    primaryFocusTargetId,
  })
  const effectiveFocusTargetId = activeTargetId || primaryFocusTargetId

  const mainClaims = generativePlan?.story_substrate?.main_claims || []
  const termsToExplain = generativePlan?.story_substrate?.terms_to_explain || []
  const backgroundGaps = generativePlan?.story_substrate?.background_gaps || []
  const resourceModules = experiencePlan?.supporting_resources || []
  const interactionModules = experiencePlan?.interactive_blocks || []
  const widgetBlocks = experiencePlan?.widget_blocks || []
  const questionModules = interactionModules.filter((module) => String(module.module_type || '').trim() === 'QuestionStarterPanel')

  const focusNode = useMemo(() => {
    if (!effectiveFocusTargetId) return extractFirstFigureNode(mainComponents)
    return mainComponents.find((node) => targetMatchesNodeId(effectiveFocusTargetId, node, page)) || extractFirstFigureNode(mainComponents)
  }, [effectiveFocusTargetId, mainComponents, page])

  const readingFlowNodes = useMemo(() => {
    if (!focusNode) return mainComponents
    const focusId = String(focusNode.id || '').trim()
    return mainComponents.filter((node) => String(node.id || '').trim() !== focusId)
  }, [focusNode, mainComponents])

  const focusHeading = (() => {
    const heroTitle = String(preferDisplayCopy(hero?.display_title, hero?.title)).trim()
    if (heroTitle) return heroTitle
    if (focusNode?.type === 'FigurePanel') {
      const props = (focusNode.props && typeof focusNode.props === 'object') ? focusNode.props as Record<string, unknown> : {}
      const sourceLabel = String(props.source_label || '').trim()
      const title = String(props.title || '').trim()
      if (sourceLabel && title) return `${sourceLabel} · ${title}`
      if (sourceLabel) return sourceLabel
      if (title) return title
    }
    return storyTitle
  })()

  const visibleClaims = mainClaims
    .filter((claim) => !isEnglishHeavyReaderCopy(preferDisplayCopy(claim?.display_text, claim?.text)))
    .slice(0, Math.max(1, Number(contentBudget.max_claim_cards || 2)))
  const visibleTerms = termsToExplain.slice(0, 5)
  const visibleBackgroundGaps = backgroundGaps.slice(0, 4)
  const visibleHooks = useMemo(() => (
    Array.isArray(pageBrief?.experience_hooks)
      ? pageBrief.experience_hooks.slice(0, Math.max(1, Number(contentBudget.max_hooks || 2)))
      : []
  ), [contentBudget.max_hooks, pageBrief?.experience_hooks])
  const fallbackQuestionAnswers = useMemo(() => {
    if (questionModules.length) return []
    return [
      {
        question: '这页最值得先看的证据是什么？',
        answer: preferDisplayCopy(hero?.display_summary, hero?.summary) || topSummary,
      },
      {
        question: '有哪些术语会卡住理解？',
        answer: visibleTerms.length
          ? `优先解释 ${visibleTerms.slice(0, 3).map((item) => item.term).join('、')}。`
          : '优先回到正文，确认评价指标和比较对象。',
      },
    ]
  }, [hero?.display_summary, hero?.summary, questionModules.length, topSummary, visibleTerms])
  const contextCards = useMemo(() => {
    const cards: Array<{ key: string; title: string; body: ReactNode }> = []
    if (visibleTerms.length || visibleBackgroundGaps.length) {
      cards.push({
        key: 'context',
        title: '阅读前需了解',
        body: (
          <div className="reader-experience-page__chip-cloud">
            {visibleTerms.map((item) => <Tag key={item.term}>{item.term}</Tag>)}
            {visibleBackgroundGaps.map((item) => <Tag key={item.topic} color="orange">{item.topic}</Tag>)}
          </div>
        ),
      })
    }
    if (visibleClaims.length || visibleHooks.length) {
      cards.push({
        key: 'signals',
        title: '本页信号',
        body: (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {visibleClaims.slice(0, 2).map((claim) => (
              <div key={claim.claim_id} className="reader-experience-page__signal-item">
                <Text>{preferDisplayCopy(claim.display_text, claim.text)}</Text>
              </div>
            ))}
            {visibleHooks.length ? (
              <div className="reader-experience-page__chip-cloud">
                {visibleHooks.map((hook, index) => <Tag key={`hook-${index}`} color="blue">{hook}</Tag>)}
              </div>
            ) : null}
          </Space>
        ),
      })
    }
    return cards
  }, [visibleBackgroundGaps, visibleClaims, visibleHooks, visibleTerms])

  const topStatusText = (() => {
    if ((composeLoading && !effectiveComposePayload) || (planLoading && !effectiveComposePayload && !experiencePlan)) return '正在准备调试预览'
    if (backgroundRefreshing) return '后台更新中'
    if (planError) return '基础内容已就绪'
    if (experienceResponse?.experience_cache_hit) return '已命中缓存'
    return '最新生成'
  })()
  const storyMapSection = useMemo(
    () => mainSections.find((section) => String(section.section_type || '').trim() === 'story_map') || null,
    [mainSections],
  )
  const storyMapMeta = useMemo(() => {
    if (!storyMapSection) return { rationaleRows: [] as string[], hookRows: [] as string[], toolRows: [] as string[] }
    const meta = (storyMapSection.meta && typeof storyMapSection.meta === 'object')
      ? storyMapSection.meta as Record<string, unknown>
      : {}
    return {
      rationaleRows: Array.isArray(meta.rationale) ? meta.rationale.map((item) => String(item || '').trim()).filter(Boolean) : [],
      hookRows: Array.isArray(meta.hooks) ? meta.hooks.map((item) => String(item || '').trim()).filter(Boolean) : [],
      toolRows: Array.isArray(meta.used_tools) ? meta.used_tools.map((item) => String(item || '').trim()).filter(Boolean) : [],
    }
  }, [storyMapSection])
  const surfaceState = classifyWorkbenchSurfaceState({
    composeError,
    planError,
    hasComposePayload: Boolean(effectiveComposePayload),
    hasPlan: Boolean(hasNonDraftExperiencePlan(experienceResponse)),
    composeLoading,
    planLoading,
    backgroundRefreshing,
  })

  return (
    <div className="reader-workbench-debug">
      <div className="reader-workbench-debug__header">
        <div className="reader-workbench-debug__hero">
          <Text className="reader-workbench-debug__hero-eyebrow">Generative Reader Workbench</Text>
          <Title level={2} style={{ margin: 0 }}>{storyTitle}</Title>
          <Paragraph className="reader-workbench-debug__hero-summary">{storySubtitle || topSummary}</Paragraph>
          <Space wrap className="reader-workbench-debug__hero-status">
            <Tag color={topStatusText === '已命中缓存' ? 'cyan' : (backgroundRefreshing ? 'gold' : 'blue')}>{topStatusText}</Tag>
            {pageBrief?.reader_type ? <Tag>{humanizeToken(pageBrief.reader_type)}</Tag> : null}
            {experienceResponse?.experience_cache_hit ? <Tag color="green">experience cached</Tag> : null}
            {experienceResponse?.generative_plan_cache_hit ? <Tag color="blue">plan cached</Tag> : null}
            {contractValidationStatus ? <Tag>{`contract ${contractValidationStatus}`}</Tag> : null}
          </Space>
          {composeMetaSummary ? <Text className="reader-workbench-debug__hero-meta">{composeMetaSummary}</Text> : null}
        </div>
        <Space wrap className="reader-workbench-debug__hero-actions">
          <Button
            onClick={() => setReloadState((prev) => ({ nonce: prev.nonce + 1, forceFresh: true }))}
            loading={composeLoading || planLoading || backgroundRefreshing}
          >
            刷新调试预览
          </Button>
          <Link to={`/literature/${numericPaperId}/experience?page=${page}${selectedKbId > 0 ? `&kb=${selectedKbId}` : ''}${readerProfile.trim() ? `&reader=${encodeURIComponent(readerProfile.trim())}` : ''}`}>打开体验页</Link>
          <Link to={`/literature/${numericPaperId}/read?page=${page}`}>返回阅读器</Link>
        </Space>
      </div>

      <details className="reader-workbench-debug__controls-details">
        <summary>参数与意图</summary>
        <Card size="small" className="reader-workbench-debug__controls">
          <Space wrap size={16} align="start">
            <div>
              <Text type="secondary">Page</Text>
              <div>
                <InputNumber min={1} value={page} onChange={(value) => setPage(Number(value || 1))} />
              </div>
            </div>
            <div>
              <Text type="secondary">Selected KB</Text>
              <div>
                <InputNumber min={0} value={selectedKbId} onChange={(value) => setSelectedKbId(Number(value || 0))} />
              </div>
            </div>
            <div style={{ minWidth: 220 }}>
              <Text type="secondary">Reader</Text>
              <Input value={readerProfile} onChange={(event) => setReaderProfile(event.target.value)} />
            </div>
            <div className="reader-workbench-debug__intent">
              <Text type="secondary">Intent</Text>
              <Input.TextArea value={userIntent} onChange={(event) => setUserIntent(event.target.value)} autoSize={{ minRows: 2, maxRows: 4 }} />
            </div>
          </Space>
        </Card>
      </details>

      {composeError ? <Alert type="error" showIcon message={composeError} className="reader-workbench-debug__alert" /> : null}
      {!composeError && planError ? (
        <Alert
          type="warning"
          showIcon
          message={planError}
          description="正文底座已加载，增强计划暂未完成。你仍可先检查当前 preview 和 debug 信息。"
          className="reader-workbench-debug__alert"
        />
      ) : null}
      {selectedKbId <= 0 ? (
        <Alert
          type="info"
          showIcon
          message="当前未绑定知识库"
          description="workbench 预览会继续基于正文与公开资源构建同一份 experience plan；如果你要验证知识库检索效果，可补充 kb。"
          className="reader-workbench-debug__alert"
        />
      ) : null}
      {surfaceState ? (
        <Card className="reader-workbench-debug__panel">
          <Empty
            description={(
              <Space direction="vertical" size={4}>
                <Text strong>{surfaceState.title}</Text>
                <Text type="secondary">{surfaceState.description}</Text>
              </Space>
            )}
          />
        </Card>
      ) : null}

      {!surfaceState ? (
        <>
          <Card className="reader-workbench-debug__panel">
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <div className="reader-workbench-debug__summary-block">
                <Text className="reader-workbench-debug__module-eyebrow">Shared Experience Preview</Text>
                <Paragraph className="reader-workbench-debug__module-summary" style={{ marginBottom: 0 }}>
                  workbench 现在直接预览和 `/experience` 同一份 experience plan，调试层只补 story、targets、cache 和 contract 信息。
                </Paragraph>
              </div>
              <GenerativeExperienceRenderer
                layoutVariant={layoutVariant}
                hero={hero}
                focusHeading={focusHeading}
                visibleClaims={visibleClaims}
                contextCards={contextCards}
                narrativeSections={mainSections.filter((section) => String(section.section_type || '').trim() !== 'hero')}
                focusNode={focusNode}
                readingFlowNodes={readingFlowNodes}
                renderCtx={renderCtx}
                composeLoading={composeLoading}
                hasComposePayload={Boolean(effectiveComposePayload)}
                backgroundRefreshing={backgroundRefreshing}
                fallbackQuestionAnswers={fallbackQuestionAnswers}
                resourceModules={resourceModules}
                interactionModules={interactionModules}
                widgetBlocks={widgetBlocks}
                getBlockUiAction={getBlockUiAction}
                dispatchBlockAction={dispatchBlockAction}
                lastUiEvent={lastUiEvent}
                topStatusText={topStatusText}
              />
            </Space>
          </Card>

          <div className="reader-workbench-debug__layout">
            <section className="reader-workbench-debug__main">
              <Card title="Story Map" className="reader-workbench-debug__panel">
                {renderStorySubstrate(storySubstrate)}
              </Card>

              <Card title="Enhancement Outline" className="reader-workbench-debug__panel">
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <div className="reader-workbench-debug__summary-block">
                    <Text className="reader-workbench-debug__module-eyebrow">Plan rationale</Text>
                    {generativePlan?.rationale?.length ? (
                      <List
                        size="small"
                        dataSource={generativePlan.rationale}
                        renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
                      />
                    ) : (
                      <Text type="secondary">No rationale supplied</Text>
                    )}
                  </div>
                  {storyMapMeta.rationaleRows.length ? (
                    <div className="reader-workbench-debug__summary-block">
                      <Text className="reader-workbench-debug__module-eyebrow">Storyboard rationale</Text>
                      <List
                        size="small"
                        dataSource={storyMapMeta.rationaleRows}
                        renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
                      />
                    </div>
                  ) : null}
                  {storyMapMeta.hookRows.length ? (
                    <div className="reader-workbench-debug__summary-block">
                      <Text className="reader-workbench-debug__module-eyebrow">Hooks</Text>
                      <Space wrap>
                        {storyMapMeta.hookRows.map((item, index) => <Tag key={`hook-${index}`}>{item}</Tag>)}
                      </Space>
                    </div>
                  ) : null}
                  <details className="reader-workbench-debug__details">
                    <summary>查看 grounded targets 和调试信息</summary>
                    <div className="reader-workbench-debug__details-body">
                      {enrichmentTargets.length ? (
                        <div className="reader-workbench-debug__grid">
                          {enrichmentTargets.map(renderTarget)}
                        </div>
                      ) : (
                        <Empty description="No enrichment targets" />
                      )}
                    </div>
                  </details>
                </Space>
              </Card>
            </section>

            <aside className="reader-workbench-debug__sidebar">
              <Card title="Plan Meta" className="reader-workbench-debug__panel">
                <Space wrap>
                  <Tag>{`layout ${layoutVariant}`}</Tag>
                  {pageBrief?.page_archetype ? <Tag>{pageBrief.page_archetype}</Tag> : null}
                  {displayCopyContract ? <Tag>{displayCopyContract}</Tag> : null}
                  {storyMapMeta.toolRows.length ? storyMapMeta.toolRows.map((tool) => <Tag key={tool} color="geekblue">{tool}</Tag>) : null}
                </Space>
                <Paragraph className="reader-workbench-debug__module-summary">
                  {topSummary}
                </Paragraph>
                {composeMetaSummary ? (
                  <Paragraph className="reader-workbench-debug__module-summary">
                    {composeMetaSummary}
                  </Paragraph>
                ) : null}
                {experienceResponse?.experience_cache_hit ? <Tag color="cyan">experience cache hit</Tag> : null}
                {experienceResponse?.generative_plan_cache_hit ? <Tag color="blue">generative cache hit</Tag> : null}
              </Card>

              <Card title="Reading Path" className="reader-workbench-debug__panel">
                {readingPath.length ? (
                  <div className="reader-workbench-debug__path-strip">
                    {readingPath.map((step, index) => (
                      <div key={`${step}-${index}`} className="reader-workbench-debug__path-step">
                        <span className="reader-workbench-debug__path-step-index">{index + 1}</span>
                        <span>{humanizeToken(step)}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty description="No reading path" />
                )}
              </Card>

              <Card title="Content Budget" className="reader-workbench-debug__panel">
                {Object.keys(contentBudget).length ? (
                  <Space wrap>
                    {Object.entries(contentBudget).map(([key, value]) => (
                      <Tag key={key}>{`${key}:${value}`}</Tag>
                    ))}
                  </Space>
                ) : (
                  <Empty description="No content budget" />
                )}
              </Card>
            </aside>
          </div>
        </>
      ) : null}
    </div>
  )
}
