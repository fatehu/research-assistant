import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
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

function classifyReaderSurfaceState(params: {
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
      description: '当前页还没有可用的 PDF 渲染结果，暂时无法生成展开式体验。',
    }
  }
  if (composeToken.includes('no cached reader payload available') || composeToken.includes('暂无正文底座')) {
    return {
      title: '暂无正文底座',
      description: '当前页还没有可用的 compose payload，请先回到阅读器触发正文清洗或稍后重试。',
    }
  }
  if (!hasComposePayload && composeError) {
    return {
      title: '正文底座加载失败',
      description: composeError,
    }
  }
  if (hasComposePayload && !hasPlan && (planToken.includes('network error') || planToken.includes('加载体验计划失败'))) {
    return {
      title: '增强计划暂未就绪',
      description: '正文底座已可用，但体验计划还没有成功返回。你可以稍后刷新体验，或先回到阅读器继续阅读。',
    }
  }
  if (hasComposePayload && !hasPlan) {
    return {
      title: '增强计划暂未就绪',
      description: '当前已拿到底座内容，但更完整的体验计划还没准备好。',
    }
  }
  return null
}

function localizeReadingPathStep(step: string): string {
  const token = String(step || '').trim().toLowerCase()
  if (token === 'hero_summary') return '核心摘要'
  if (token === 'focus_evidence') return '聚焦证据'
  if (token === 'key_finding') return '关键发现'
  if (token === 'context_explainer') return '背景解释'
  if (token === 'supporting_resources') return '延伸资源'
  if (token === 'explore_questions') return '继续探索'
  if (token === 'reading_flow') return '正文阅读'
  if (token === 'hero') return '开场摘要'
  if (token === 'focus') return '重点内容'
  if (token === 'read') return '阅读正文'
  if (token === 'explore') return '扩展探索'
  return String(step || '').replace(/[_-]+/g, ' ')
}

function isEnglishHeavyReaderCopy(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  const cjkMatches = text.match(/[\u3400-\u9fff]/g) || []
  const latinMatches = text.match(/[A-Za-z]/g) || []
  if (cjkMatches.length > 0) return false
  return latinMatches.length >= 24 && latinMatches.length > cjkMatches.length * 4
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
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

  const [focusPage, setFocusPage] = useState(Number.isFinite(initialPage) && initialPage > 0 ? initialPage : 1)
  const [selectedKbId, setSelectedKbId] = useState(Number.isFinite(initialKbId) && initialKbId > 0 ? initialKbId : 0)
  const [userIntent, setUserIntent] = useState(initialIntent)
  const [readerProfile, setReaderProfile] = useState(initialReader)
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
  const generativePlan = experienceResponse?.generative_plan || null
  const layoutVariant = normalizeExperienceLayoutVariant(experiencePlan?.layout_variant || String(experiencePlan?.meta?.layout_variant || ''))

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
  const hero = experiencePlan?.hero || null
  const storyTitle = (() => {
    const title = String(hero?.display_title || experiencePlan?.page_story_title || '').trim()
    if (title && title.toLowerCase() !== 'fig 3') return title
    return `论文 ${numericPaperId} 展开阅读`
  })()
  const storySubtitle = preferDisplayCopy(hero?.display_subtitle, experiencePlan?.page_story_subtitle || '基于清洗后正文内容生成的展开式阅读体验。')
  const storyGoal = preferDisplayCopy(hero?.display_summary, experiencePlan?.narrative_goal || '')
  const mainSections = useMemo(() => experiencePlan?.main_sections || [], [experiencePlan?.main_sections])
  const pageBrief = generativePlan?.page_brief || null
  const contentBudget = (pageBrief?.content_budget && typeof pageBrief.content_budget === 'object')
    ? pageBrief.content_budget as Record<string, number>
    : {}
  const readingPath = experiencePlan?.reading_path || []
  const primaryFocusTargetId = String(hero?.target_ids?.[0] || '').trim()
  const { activeTargetId, lastUiEvent, dispatchBlockAction, getBlockUiAction } = useExperienceActionBus({
    paperId: numericPaperId,
    focusPage,
    primaryFocusTargetId,
  })
  const effectiveFocusTargetId = activeTargetId || primaryFocusTargetId
  const mainClaims = generativePlan?.story_substrate?.main_claims || []
  const termsToExplain = generativePlan?.story_substrate?.terms_to_explain || []
  const backgroundGaps = generativePlan?.story_substrate?.background_gaps || []
  const resourceModules = experiencePlan?.supporting_resources || []
  const interactionModules = experiencePlan?.interactive_blocks || []
  const widgetBlocks = experiencePlan?.widget_blocks || []

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

  const questionModules = interactionModules.filter((module) => String(module.module_type || '').trim() === 'QuestionStarterPanel')

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
  const fallbackQuestions = useMemo(() => {
    if (questionModules.length) return []
    return [
      '图里的哪一部分最能改变你对这页结论的理解？',
      '这页里哪些专业术语需要先解释，才能真正看懂结果？',
      '如果读者不是专业人士，最需要补充的背景信息是什么？',
    ]
  }, [questionModules.length])
  const fallbackQuestionAnswers = useMemo(() => {
    if (!fallbackQuestions.length) return []
    return [
      {
        question: fallbackQuestions[0],
        answer: preferDisplayCopy(hero?.display_summary, hero?.summary) || storyGoal || '先看主图，再回到正文核对哪一个变化最关键。',
      },
      {
        question: fallbackQuestions[1],
        answer: visibleTerms.length
          ? `优先要解释的是 ${visibleTerms.slice(0, 3).map((item) => item.term).join('、')}，因为它们直接决定你能不能正确读懂这页的图和结论。`
          : '最容易卡住理解的通常是页面里的评价指标和基准名称。',
      },
      {
        question: fallbackQuestions[2],
        answer: visibleBackgroundGaps.length
          ? `最值得补的背景是 ${visibleBackgroundGaps[0].topic}，因为它能帮助非专业读者理解这页在比较什么。`
          : '最值得补的外部背景通常是这页引用的考试体系或评价框架。',
      },
    ]
  }, [fallbackQuestions, hero?.display_summary, hero?.summary, storyGoal, visibleTerms, visibleBackgroundGaps])
  const sectionMap = useMemo<Record<string, typeof mainSections[number]>>(
    () => Object.fromEntries(mainSections.map((section) => [String(section.section_type || '').trim(), section])),
    [mainSections],
  )
  const topStatusText = (() => {
    if ((composeLoading && !effectiveComposePayload) || (planLoading && !effectiveComposePayload && !experiencePlan)) return '正在准备体验页'
    if (backgroundRefreshing) return '后台更新中'
    if (planError) return '基础内容已就绪'
    if (experienceResponse?.experience_cache_hit) return '已就绪'
    return '最新生成'
  })()
  const narrativeSections = useMemo(
    () => mainSections.filter((section) => String(section.section_type || '').trim() !== 'hero'),
    [mainSections],
  )
  const storyMapSection = sectionMap.story_map
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
  const surfaceState = classifyReaderSurfaceState({
    composeError,
    planError,
    hasComposePayload: Boolean(effectiveComposePayload),
    hasPlan: Boolean(hasNonDraftExperiencePlan(experienceResponse)),
    composeLoading,
    planLoading,
    backgroundRefreshing,
  })

  const contextCards = useMemo(() => {
    const cards: Array<{ key: string; title: string; body: ReactNode }> = []
    if (visibleTerms.length || visibleBackgroundGaps.length) {
      cards.push({
        key: 'context',
        title: '阅读前需了解',
        body: (
          <div className="reader-experience-page__chip-cloud">
            {visibleTerms.map((item) => (
              <Tag key={item.term}>{item.term}</Tag>
            ))}
            {visibleBackgroundGaps.map((item) => (
              <Tag key={item.topic} color="orange">{item.topic}</Tag>
            ))}
          </div>
        ),
      })
    }
    if (visibleClaims.length || visibleHooks.length) {
      const compactClaims = visibleClaims.slice(0, 2)
      const hiddenClaimCount = Math.max(0, visibleClaims.length - compactClaims.length)
      cards.push({
        key: 'signals',
        title: '本页信号',
        body: (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {compactClaims.length ? (
              <div className="reader-experience-page__signal-list">
                {compactClaims.map((claim) => (
                  <div key={claim.claim_id} className="reader-experience-page__signal-item">
                    <Text>{preferDisplayCopy(claim.display_text, claim.text)}</Text>
                  </div>
                ))}
              </div>
            ) : null}
            {hiddenClaimCount > 0 ? (
              <Text type="secondary" className="reader-experience-page__summary">+{hiddenClaimCount} 条更多信号</Text>
            ) : null}
            {visibleHooks.length ? (
              <div className="reader-experience-page__chip-cloud">
                {visibleHooks.map((hook, index) => (
                  <Tag key={`hook-${index}`} color="blue">{hook}</Tag>
                ))}
              </div>
            ) : null}
          </Space>
        ),
      })
    }
    return cards
  }, [visibleBackgroundGaps, visibleClaims, visibleHooks, visibleTerms])

  return (
    <div className="reader-experience-page">
      <div className="reader-experience-page__header">
        <div className="reader-experience-page__hero-copy">
          <div className="reader-experience-page__status-row">
            <Text className="reader-experience-page__eyebrow">展开阅读页</Text>
            <span className="reader-experience-page__status-chip">{topStatusText}</span>
          </div>
          <Title level={1} className="reader-experience-page__title">{storyTitle}</Title>
          <Paragraph className="reader-experience-page__subtitle">{storySubtitle}</Paragraph>
          {storyGoal ? <Paragraph className="reader-experience-page__goal">{storyGoal}</Paragraph> : null}
          <div className="reader-experience-page__path">
            {(readingPath.length ? readingPath : ['hero', 'focus', 'read', 'explore']).map((step, index) => (
              <div key={`${step}-${index}`} className="reader-experience-page__path-step">
                <span className="reader-experience-page__path-index">{index + 1}</span>
                <span>{localizeReadingPathStep(step)}</span>
              </div>
            ))}
          </div>
          {visibleHooks.length ? (
            <div className="reader-experience-page__path reader-experience-page__path--hooks">
              {visibleHooks.map((hook, index) => (
                <div key={`hook-${index}`} className="reader-experience-page__path-step reader-experience-page__path-step--hook">
                  <span>{hook}</span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
        <div className="reader-experience-page__header-actions">
          <Space wrap size={10}>
            <Button
              onClick={() => setReloadState((prev) => ({ nonce: prev.nonce + 1, forceFresh: true }))}
              loading={composeLoading || planLoading || backgroundRefreshing}
            >
              刷新体验
            </Button>
            <Link to={`/literature/${numericPaperId}/read?page=${focusPage}`}>返回阅读器</Link>
          </Space>
        </div>
      </div>

      <details className="reader-experience-page__details">
        <summary>页面参数</summary>
        <div className="reader-experience-page__details-body">
          <Space wrap size={16} align="start">
            <div>
              <Text type="secondary">页码</Text>
              <div>
                <InputNumber min={1} value={focusPage} onChange={(value) => setFocusPage(Number(value || 1))} />
              </div>
            </div>
            <div>
              <Text type="secondary">知识库</Text>
              <div>
                <InputNumber min={0} value={selectedKbId} onChange={(value) => setSelectedKbId(Number(value || 0))} />
              </div>
            </div>
            <div style={{ minWidth: 220 }}>
              <Text type="secondary">读者画像</Text>
              <Input value={readerProfile} onChange={(event) => setReaderProfile(event.target.value)} />
            </div>
            <div style={{ minWidth: 320, flex: '1 1 320px' }}>
              <Text type="secondary">阅读意图</Text>
              <Input.TextArea value={userIntent} onChange={(event) => setUserIntent(event.target.value)} autoSize={{ minRows: 2, maxRows: 4 }} />
            </div>
          </Space>
        </div>
      </details>

      {composeError ? <Alert type="error" showIcon message={composeError} className="reader-experience-page__alert" /> : null}
      {!composeError && planError ? (
        <Alert
          type="warning"
          showIcon
          message={planError}
          description="正文底座已加载，体验计划暂未完成。"
          className="reader-experience-page__alert"
        />
      ) : null}
      {selectedKbId <= 0 ? (
        <Alert
          type="info"
          showIcon
          message="当前未绑定知识库"
          description="本页会优先基于论文正文与公开资源生成体验；如果你希望引入知识库检索，可在上方参数里填入 kb。"
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

      {!surfaceState ? (
        <GenerativeExperienceRenderer
          layoutVariant={layoutVariant}
          hero={hero}
          focusHeading={focusHeading}
          visibleClaims={visibleClaims}
          contextCards={contextCards}
          narrativeSections={narrativeSections}
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
      ) : null}

      <details className="reader-experience-page__details">
        <summary>页面生成细节</summary>
        <div className="reader-experience-page__details-body">
          <Space wrap>
            <Tag>{`焦点页 ${focusPage}`}</Tag>
            <Tag>{`读者 ${readerProfile}`}</Tag>
            {experienceResponse?.cache_layer ? <Tag>{`compose ${experienceResponse.cache_layer}`}</Tag> : null}
            {experienceResponse?.experience_cache_layer ? <Tag>{`experience ${experienceResponse.experience_cache_layer}`}</Tag> : null}
            {experienceResponse?.generative_plan_cache_layer ? <Tag>{`plan ${experienceResponse.generative_plan_cache_layer}`}</Tag> : null}
            {generativePlan?.used_tools?.length ? <Tag>{`工具:${generativePlan.used_tools.join(', ')}`}</Tag> : null}
            {experienceResponse?.experience_cache_hit ? <Tag color="cyan">体验缓存命中</Tag> : null}
            {experienceResponse?.generative_plan_cache_hit ? <Tag color="blue">计划缓存命中</Tag> : null}
          </Space>
          <Paragraph className="reader-experience-page__summary">
            {String(generativePlan?.meta?.notes || experiencePlan?.meta?.derived_from || '').trim() || '当前体验页建立在 compose 底座与 generative plan 之上。'}
          </Paragraph>
          {storyMapMeta.rationaleRows.length ? (
            <div className="reader-experience-page__details-section">
              <Text className="reader-experience-page__eyebrow">页面规划依据</Text>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {storyMapMeta.rationaleRows.map((row, index) => (
                  <Paragraph key={`details-rationale-${index}`} className="reader-experience-page__summary">{row}</Paragraph>
                ))}
              </Space>
            </div>
          ) : null}
          {storyMapMeta.hookRows.length ? (
            <div className="reader-experience-page__details-section">
              <Text className="reader-experience-page__eyebrow">阅读钩子</Text>
              <div className="reader-experience-page__chip-cloud">
                {storyMapMeta.hookRows.map((hook, index) => (
                  <Tag key={`details-hook-${index}`}>{hook}</Tag>
                ))}
              </div>
            </div>
          ) : null}
          {storyMapMeta.toolRows.length ? (
            <div className="reader-experience-page__details-section">
              <Text className="reader-experience-page__eyebrow">使用工具</Text>
              <div className="reader-experience-page__chip-cloud">
                {storyMapMeta.toolRows.map((tool) => (
                  <Tag key={tool} color="geekblue">{tool}</Tag>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </details>
    </div>
  )
}
