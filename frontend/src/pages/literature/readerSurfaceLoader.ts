import { useEffect, useRef, useState } from 'react'

import {
  literatureApi,
  type ReaderComposePayload,
  type ReaderExperiencePlanResponse,
  type ReaderGenerativePlanResponse,
} from '@/services/api'

export type ReaderSurfaceLoadState =
  | 'idle'
  | 'loading_cached'
  | 'showing_seed'
  | 'refreshing_fresh'
  | 'ready'
  | 'partial_error'
  | 'hard_error'

export interface ReaderSurfaceCacheState {
  composeLayer: string
  composeHit: boolean
  planLayer: string
  planHit: boolean
  experienceLayer: string
  experienceHit: boolean
  isSeed: boolean
  isFresh: boolean
}

interface ReaderSurfaceBaseOptions {
  paperId: number
  page: number
  selectedKbId: number
  userIntent: string
}

interface ReaderExperienceLoaderOptions extends ReaderSurfaceBaseOptions {
  mode: 'experience'
  readerProfile: string
  reloadState: { nonce: number; forceFresh: boolean }
}

interface ReaderWorkbenchLoaderOptions extends ReaderSurfaceBaseOptions {
  mode: 'workbench'
  readerProfile: string
  reloadNonce: number
}

type ReaderSurfaceLoaderOptions = ReaderExperienceLoaderOptions | ReaderWorkbenchLoaderOptions

interface ReaderSurfaceLoaderResult {
  composePayload: ReaderComposePayload | null
  experienceResponse: ReaderExperiencePlanResponse | null
  generativePlanResponse: ReaderGenerativePlanResponse | null
  composeError: string | null
  planError: string | null
  composeLoading: boolean
  planLoading: boolean
  backgroundRefreshing: boolean
  surfaceLoadState: ReaderSurfaceLoadState
  cacheState: ReaderSurfaceCacheState
}

const EMPTY_CACHE_STATE: ReaderSurfaceCacheState = {
  composeLayer: 'none',
  composeHit: false,
  planLayer: 'none',
  planHit: false,
  experienceLayer: 'none',
  experienceHit: false,
  isSeed: false,
  isFresh: false,
}

const SURFACE_PERSISTENCE_VERSION = 'v3'

type PersistedExperienceSurface = {
  composePayload: ReaderComposePayload | null
  experienceResponse: ReaderExperiencePlanResponse | null
  cacheState: ReaderSurfaceCacheState
}

function hasUsableComposePayload(payload: ReaderComposePayload | null | undefined): payload is ReaderComposePayload {
  return Boolean(
    payload
    && typeof payload === 'object'
    && Object.keys(payload as unknown as Record<string, unknown>).length > 0,
  )
}

function hasNonDraftExperiencePlan(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  const plan = response?.plan
  return Boolean(plan && typeof plan === 'object' && plan.status && plan.status !== 'draft')
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

function isCompletedFinalExperiencePlan(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  if (!response || !hasNonDraftExperiencePlan(response) || isSeedExperiencePlan(response)) return false
  if (!isDoneLikeStatus(response.plan?.status)) return false
  if (hasProvisionalStatusToken(response.compose_status)) return false
  if (hasProvisionalStatusToken(response.experience_cache_layer)) return false
  const plan = asRecord(response.plan)
  const meta = asRecord(plan?.meta) || {}
  if (meta.seed_plan || meta.provisional || meta.is_provisional || meta.final_surface_ready === false) return false
  const fallbackReason = String(meta.fallback_reason || '').trim().toLowerCase()
  if (fallbackReason) return false
  const manuscript = asRecord(plan?.teaching_manuscript)
  const manuscriptStatus = String(manuscript?.status || '').trim()
  if (manuscriptStatus && !isDoneLikeStatus(manuscriptStatus)) return false
  if (hasProvisionalStatusToken(manuscriptStatus)) return false
  return true
}

function hasMeaningfulExperienceText(value: unknown): boolean {
  const text = String(value || '').trim()
  if (!text) return false
  const lowered = text.toLowerCase()
  if (lowered.includes('暂无') || lowered.includes('暂未') || lowered.includes('生成中')) return false
  if (lowered === 'n/a' || lowered === 'none' || lowered === 'null') return false
  return true
}

function scorePrimaryExperienceContent(response: ReaderExperiencePlanResponse | null | undefined): number {
  if (!hasNonDraftExperiencePlan(response) || isSeedExperiencePlan(response)) return 0
  const plan = asRecord(response?.plan)
  const hero = asRecord(plan?.hero)
  const mainSections = asRecordList(plan?.main_sections)
  const guidedBeats = asRecordList(plan?.guided_beats)
  const supportingResources = asRecordList(plan?.supporting_resources)
  const interactiveBlocks = asRecordList(plan?.interactive_blocks)
  const widgetBlocks = asRecordList(plan?.widget_blocks)
  const manuscript = asRecord(plan?.teaching_manuscript)
  const readingPath = toStringRows(plan?.reading_path)

  let score = 0
  if (hasMeaningfulExperienceText(hero?.display_title || hero?.title)) score += 4
  if (hasMeaningfulExperienceText(hero?.display_summary || hero?.summary)) score += 8
  score += Math.min(mainSections.length, 8) * 6
  score += Math.min(guidedBeats.length, 8) * 4
  score += Math.min(supportingResources.length, 6) * 3
  score += Math.min(interactiveBlocks.length, 6) * 3
  score += Math.min(widgetBlocks.length, 6) * 3
  score += Math.min(readingPath.length, 6) * 2

  for (const section of mainSections) {
    if (hasMeaningfulExperienceText(section.display_title || section.title)) score += 2
    if (hasMeaningfulExperienceText(section.display_summary || section.summary)) score += 3
    score += Math.min(toStringRows(section.target_ids).length, 3)
    score += Math.min(asRecordList(section.blocks).length, 4)
  }

  // Manuscript presence is only a weak fallback signal; it should not dominate cache choice.
  if (manuscript) {
    if (hasMeaningfulExperienceText(manuscript.opening)) score += 1
    const segments = Array.isArray(manuscript.segments) ? manuscript.segments : []
    score += Math.min(segments.length, 4)
  }
  return score
}

function hasCompletedFinalPrimaryExperience(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  if (!isCompletedFinalExperiencePlan(response)) return false
  return scorePrimaryExperienceContent(response) > 0
}

function shouldKeepCurrentExperience(
  current: ReaderExperiencePlanResponse | null | undefined,
  incoming: ReaderExperiencePlanResponse | null | undefined,
): boolean {
  if (!hasCompletedFinalPrimaryExperience(current)) return false
  if (!hasCompletedFinalPrimaryExperience(incoming)) return true
  const currentScore = scorePrimaryExperienceContent(current)
  const incomingScore = scorePrimaryExperienceContent(incoming)
  const currentFresh = Boolean(current && !current.experience_cache_hit)
  const incomingCached = Boolean(incoming?.experience_cache_hit)
  if (currentFresh && incomingCached && currentScore >= incomingScore) return true
  return currentScore > incomingScore + 2
}

function isSeedExperiencePlan(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  const plan = response?.plan
  const meta = plan && typeof plan === 'object'
    ? ((plan.meta as Record<string, unknown> | undefined) || undefined)
    : undefined
  if (meta?.seed_plan) return true
  if (response?.experience_cache_layer !== 'derived_seed') return false
  return !hasNonDraftExperiencePlan(response)
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function toStringRows(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || '').trim()).filter(Boolean)
  }
  const text = String(value || '').trim()
  return text ? [text] : []
}

function buildExperienceSurfacePersistenceKey(options: ReaderExperienceLoaderOptions): string {
  return [
    'reader-surface',
    SURFACE_PERSISTENCE_VERSION,
    options.mode,
    String(options.paperId),
    String(options.page),
    String(options.selectedKbId),
    String(options.readerProfile || '').trim(),
    String(options.userIntent || '').trim(),
  ].join(':')
}

function readPersistedExperienceSurface(key: string): PersistedExperienceSurface | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedExperienceSurface | null
    if (!parsed || typeof parsed !== 'object') return null
    if (!hasCompletedFinalPrimaryExperience(parsed.experienceResponse)) return null
    return {
      composePayload: parsed.composePayload || null,
      experienceResponse: parsed.experienceResponse || null,
      cacheState: parsed.cacheState ? { ...EMPTY_CACHE_STATE, ...parsed.cacheState } : EMPTY_CACHE_STATE,
    }
  } catch {
    return null
  }
}

function persistExperienceSurface(
  key: string,
  composePayload: ReaderComposePayload | null,
  experienceResponse: ReaderExperiencePlanResponse | null,
  cacheState: ReaderSurfaceCacheState,
): void {
  if (typeof window === 'undefined') return
  if (!hasCompletedFinalPrimaryExperience(experienceResponse)) return
  try {
    window.sessionStorage.setItem(
      key,
      JSON.stringify({
        composePayload,
        experienceResponse,
        cacheState,
      }),
    )
  } catch {
    // Ignore storage failures and keep runtime-only state.
  }
}

function buildExperienceCacheState(
  response: ReaderExperiencePlanResponse,
  options: { isSeed: boolean; isFresh?: boolean },
): ReaderSurfaceCacheState {
  return {
    composeLayer: String(response.cache_layer || 'none'),
    composeHit: Boolean(response.cache_hit),
    planLayer: String(response.generative_plan_cache_layer || 'none'),
    planHit: Boolean(response.generative_plan_cache_hit),
    experienceLayer: String(response.experience_cache_layer || 'none'),
    experienceHit: Boolean(response.experience_cache_hit),
    isSeed: options.isSeed,
    isFresh: Boolean(options.isFresh),
  }
}

function asRecordList(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
}

function countNonEmptyStrings(value: unknown): number {
  if (!Array.isArray(value)) return 0
  return value.filter((item) => Boolean(String(item || '').trim())).length
}

function hasOwnKeys(value: Record<string, unknown> | null): boolean {
  return Boolean(value && Object.keys(value).length > 0)
}

function summarizeWorkbenchGenerativePlan(plan: Record<string, unknown>): {
  moduleCount: number
  storySignalCount: number
  briefSignalCount: number
  planningSignalCount: number
  stageCount: number
  score: number
} {
  const storySubstrate = asRecord(plan.story_substrate) || {}
  const pageBrief = asRecord(plan.page_brief) || {}
  const meta = asRecord(plan.meta) || {}
  const planningBrief = asRecord(meta.planning_brief) || {}

  const moduleCount = (
    asRecordList(plan.resource_modules).length
    + asRecordList(plan.interaction_modules).length
    + asRecordList(plan.js_widgets).length
  )
  const storySignalCount = (
    asRecordList(storySubstrate.main_claims).length
    + asRecordList(storySubstrate.evidence_units).length
    + asRecordList(storySubstrate.terms_to_explain).length
    + asRecordList(storySubstrate.background_gaps).length
    + asRecordList(storySubstrate.narrative_turns).length
  )
  const briefSignalCount = (
    countNonEmptyStrings(pageBrief.reading_path)
    + countNonEmptyStrings(pageBrief.experience_hooks)
    + asRecordList(pageBrief.storyboard).length
    + countNonEmptyStrings(pageBrief.body_flow_target_ids)
    + (String(pageBrief.page_goal || '').trim() ? 1 : 0)
    + (String(pageBrief.hero_angle || '').trim() ? 1 : 0)
    + (String(pageBrief.primary_focus_target_id || '').trim() ? 1 : 0)
  )
  const planningSignalCount = (
    countNonEmptyStrings(planningBrief.recommended_sections)
    + countNonEmptyStrings(planningBrief.tool_hints)
    + asRecordList(planningBrief.guided_beat_seed).length
    + countNonEmptyStrings(planningBrief.body_flow_target_ids)
    + (String(planningBrief.summary || '').trim() ? 1 : 0)
  )
  const stageCount = asRecordList(meta.runtime_stage_trace).length
  const score = (
    moduleCount * 6
    + storySignalCount * 2
    + briefSignalCount * 2
    + planningSignalCount * 3
    + stageCount
  )

  return {
    moduleCount,
    storySignalCount,
    briefSignalCount,
    planningSignalCount,
    stageCount,
    score,
  }
}

function isScaffoldLikeWorkbenchGenerativePlan(plan: Record<string, unknown> | null): boolean {
  if (!plan || Object.keys(plan).length === 0) return true

  const status = String(plan.status || '').trim()
  const meta = asRecord(plan.meta) || {}
  const summary = summarizeWorkbenchGenerativePlan(plan)
  const fallbackReason = String(meta.fallback_reason || '').trim()

  if (status === '' || status === 'draft') return true
  if (meta.seed_plan) return true
  if (fallbackReason === 'seed_plan' || fallbackReason === 'empty_module_plan' || fallbackReason === 'agent_not_run') return true
  if (
    summary.moduleCount === 0
    && summary.storySignalCount <= 2
    && summary.briefSignalCount <= 3
    && summary.planningSignalCount === 0
    && summary.stageCount === 0
  ) {
    return true
  }
  if (
    summary.score <= 12
    && summary.moduleCount <= 1
    && summary.storySignalCount <= 2
    && summary.briefSignalCount <= 4
    && summary.planningSignalCount === 0
    && summary.stageCount === 0
  ) {
    return true
  }
  return false
}

function hasFullWorkbenchInspectPayload(plan: Record<string, unknown> | null): boolean {
  if (!plan || isScaffoldLikeWorkbenchGenerativePlan(plan)) return false

  const storySubstrate = asRecord(plan.story_substrate)
  const pageBrief = asRecord(plan.page_brief)
  const meta = asRecord(plan.meta) || {}
  const planningBrief = asRecord(meta.planning_brief)
  const plannerOutput = asRecord(meta.planner_output)
  const toolEnrichmentPacket = asRecord(meta.tool_enrichment_packet)
  const contractValidation = asRecord(meta.contract_validation)
  const runtimeStageTrace = asRecordList(meta.runtime_stage_trace)
  const guidedBeats = asRecordList(plan.guided_beats)
  const plannerGuidedBeats = plannerOutput ? asRecordList(plannerOutput.guided_beats) : []
  const toolTraceRows = Array.isArray(plan.tool_trace) ? plan.tool_trace : null

  return Boolean(
    hasOwnKeys(storySubstrate)
    && hasOwnKeys(pageBrief)
    && hasOwnKeys(planningBrief)
    && hasOwnKeys(plannerOutput)
    && hasOwnKeys(toolEnrichmentPacket)
    && hasOwnKeys(contractValidation)
    && runtimeStageTrace.length > 0
    && toolTraceRows !== null
    && (guidedBeats.length > 0 || plannerGuidedBeats.length > 0),
  )
}

function getWorkbenchInspectableGenerativePlan(
  planResponse: ReaderGenerativePlanResponse | null | undefined,
  experienceResponse: ReaderExperiencePlanResponse | null | undefined,
): Record<string, unknown> | null {
  const directPlan = asRecord(planResponse?.plan)
  if (hasFullWorkbenchInspectPayload(directPlan)) {
    return directPlan
  }
  const experiencePlan = asRecord(experienceResponse?.generative_plan)
  if (hasFullWorkbenchInspectPayload(experiencePlan)) {
    return experiencePlan
  }
  return null
}

function hasWorkbenchInspectablePlan(
  planResponse: ReaderGenerativePlanResponse | null | undefined,
  experienceResponse: ReaderExperiencePlanResponse | null | undefined = null,
): boolean {
  const plan = getWorkbenchInspectableGenerativePlan(planResponse, experienceResponse)
  if (!plan) return false
  const pageDossier = asRecord(planResponse?.page_dossier) || asRecord(experienceResponse?.page_dossier)
  const adjacentPageContext = Array.isArray(planResponse?.adjacent_page_context)
    ? planResponse?.adjacent_page_context
    : (Array.isArray(experienceResponse?.adjacent_page_context) ? experienceResponse?.adjacent_page_context : null)
  return Boolean(pageDossier && adjacentPageContext)
}

export function useReaderSurfaceLoader(options: ReaderSurfaceLoaderOptions): ReaderSurfaceLoaderResult {
  const paperId = options.paperId
  const page = options.page
  const selectedKbId = options.selectedKbId
  const userIntent = options.userIntent
  const mode = options.mode
  const readerProfile = options.readerProfile
  const reloadNonce = options.mode === 'workbench' ? options.reloadNonce : 0
  const reloadState = options.mode === 'experience' ? options.reloadState : { nonce: 0, forceFresh: false }
  const experiencePersistKey = mode === 'experience' ? buildExperienceSurfacePersistenceKey(options) : ''

  const [composePayload, setComposePayload] = useState<ReaderComposePayload | null>(null)
  const [experienceResponse, setExperienceResponse] = useState<ReaderExperiencePlanResponse | null>(null)
  const [generativePlanResponse, setGenerativePlanResponse] = useState<ReaderGenerativePlanResponse | null>(null)
  const [composeError, setComposeError] = useState<string | null>(null)
  const [planError, setPlanError] = useState<string | null>(null)
  const [composeLoading, setComposeLoading] = useState(false)
  const [planLoading, setPlanLoading] = useState(false)
  const [backgroundRefreshing, setBackgroundRefreshing] = useState(false)
  const [surfaceLoadState, setSurfaceLoadState] = useState<ReaderSurfaceLoadState>('idle')
  const [cacheState, setCacheState] = useState<ReaderSurfaceCacheState>(EMPTY_CACHE_STATE)
  const composePayloadRef = useRef<ReaderComposePayload | null>(null)
  const experienceResponseRef = useRef<ReaderExperiencePlanResponse | null>(null)

  useEffect(() => {
    composePayloadRef.current = composePayload
  }, [composePayload])

  useEffect(() => {
    experienceResponseRef.current = experienceResponse
  }, [experienceResponse])

  useEffect(() => {
    if (!paperId) return
    let alive = true
    let pollTimer: number | null = null
    const basePayload = {
      page,
      selected_kb_id: selectedKbId > 0 ? selectedKbId : undefined,
    }

    const clearPolling = () => {
      if (pollTimer) {
        window.clearTimeout(pollTimer)
        pollTimer = null
      }
    }

    const setHardError = (message: string) => {
      if (!alive) return
      setComposeError(message)
      setPlanError(null)
      setComposePayload(null)
      setExperienceResponse(null)
      setGenerativePlanResponse(null)
      setComposeLoading(false)
      setPlanLoading(false)
      setBackgroundRefreshing(false)
      setSurfaceLoadState('hard_error')
      setCacheState(EMPTY_CACHE_STATE)
    }

    const loadWorkbench = async () => {
      setComposeLoading(true)
      setPlanLoading(true)
      setBackgroundRefreshing(false)
      setComposeError(null)
      setPlanError(null)
      setSurfaceLoadState('loading_cached')
      setCacheState(EMPTY_CACHE_STATE)
      let hasRecoverableContent = false
      const experienceRequestPayload = {
        ...basePayload,
        focus_page: page,
        reader_profile: readerProfile.trim() || 'curious_generalist',
        user_intent: userIntent.trim(),
        force_refresh: false,
        regenerate: false,
      }
      const planRequestPayload = {
        ...basePayload,
        style_intent: 'reader_workbench',
        force_refresh: false,
        regenerate: false,
        user_intent: userIntent.trim(),
      }

      const applyWorkbenchExperienceResponse = (experienceResp: ReaderExperiencePlanResponse) => {
        const recoveredExperienceContent = Boolean(
          experienceResp.compose_payload
          || hasNonDraftExperiencePlan(experienceResp)
          || hasWorkbenchInspectablePlan(null, experienceResp),
        )
        if (experienceResp.compose_payload) {
          setComposePayload(experienceResp.compose_payload)
        }
        if (recoveredExperienceContent) {
          setComposeError(null)
        }
        setExperienceResponse(experienceResp)
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(experienceResp.cache_layer || prev.composeLayer || 'none'),
          composeHit: Boolean(experienceResp.cache_hit),
          planLayer: String(experienceResp.generative_plan_cache_layer || prev.planLayer || 'none'),
          planHit: Boolean(experienceResp.generative_plan_cache_hit),
          experienceLayer: String(experienceResp.experience_cache_layer || prev.experienceLayer || 'none'),
          experienceHit: Boolean(experienceResp.experience_cache_hit),
        }))
        return recoveredExperienceContent
      }

      const scheduleWorkbenchPoll = () => {
        if (!alive) return
        clearPolling()
        pollTimer = window.setTimeout(async () => {
          try {
            const nextPlan = await literatureApi.getReaderGenerativePlan(paperId, planRequestPayload)
            if (!alive) return
            hasRecoverableContent = hasRecoverableContent || Boolean(nextPlan.plan)
            setGenerativePlanResponse(nextPlan)
            setCacheState((prev) => ({
              ...prev,
              composeLayer: String(nextPlan.cache_layer || prev.composeLayer || 'none'),
              composeHit: Boolean(nextPlan.cache_hit),
              planLayer: String(nextPlan.plan_cache_layer || prev.planLayer || 'none'),
              planHit: Boolean(nextPlan.plan_cache_hit),
              isFresh: !nextPlan.plan_cache_hit,
            }))
            if (hasWorkbenchInspectablePlan(nextPlan, experienceResponseRef.current)) {
              setPlanError(null)
              setBackgroundRefreshing(false)
              setSurfaceLoadState('ready')
              return
            }
            try {
              const cachedExperience = await literatureApi.getCachedReaderExperiencePlan(paperId, experienceRequestPayload)
              if (!alive) return
              const recovered = applyWorkbenchExperienceResponse(cachedExperience)
              hasRecoverableContent = hasRecoverableContent || recovered
              if (hasWorkbenchInspectablePlan(nextPlan, cachedExperience)) {
                setPlanError(null)
                setBackgroundRefreshing(false)
                setSurfaceLoadState('ready')
                return
              }
            } catch {
              if (!alive) return
            }
            setPlanError('正在生成完整的 staged runtime 检视数据。')
            setSurfaceLoadState(hasRecoverableContent ? 'showing_seed' : 'refreshing_fresh')
            scheduleWorkbenchPoll()
          } catch {
            if (!alive) return
            setSurfaceLoadState(hasRecoverableContent ? 'showing_seed' : 'refreshing_fresh')
            scheduleWorkbenchPoll()
          }
        }, 6000)
      }

      try {
        const composeResp = await literatureApi.getCachedReaderComposed(paperId, {
          ...basePayload,
          style_intent: 'reader_workbench',
          force_refresh: false,
          regenerate: false,
        })
        if (!alive) return
        const nextComposePayload = composeResp.payload || null
        setComposePayload(nextComposePayload)
        hasRecoverableContent = Boolean(nextComposePayload)
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(composeResp.cache_meta?.cache_layer || composeResp.payload?.cache_layer || 'none'),
          composeHit: Boolean(composeResp.payload?.cache_hit ?? composeResp.cache_meta?.cache_hit ?? false),
        }))
      } catch (error) {
        if (!alive) return
        setComposeError(error instanceof Error ? error.message : '加载正文底座失败')
      } finally {
        if (alive) setComposeLoading(false)
      }

      try {
        const planResp = await literatureApi.getReaderGenerativePlan(paperId, planRequestPayload)
        if (!alive) return
        hasRecoverableContent = hasRecoverableContent || Boolean(planResp.plan)
        setGenerativePlanResponse(planResp)
        if (planResp.plan) {
          setComposeError(null)
        }
        setPlanError(null)
        setPlanLoading(false)
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(planResp.cache_layer || prev.composeLayer || 'none'),
          composeHit: Boolean(planResp.cache_hit),
          planLayer: String(planResp.plan_cache_layer || 'none'),
          planHit: Boolean(planResp.plan_cache_hit),
          isFresh: !planResp.plan_cache_hit,
        }))
        if (hasWorkbenchInspectablePlan(planResp, null)) {
          setSurfaceLoadState('ready')
        } else {
          setBackgroundRefreshing(true)
          setSurfaceLoadState(hasRecoverableContent ? 'showing_seed' : 'refreshing_fresh')
          setPlanError('正在生成完整的 staged runtime 检视数据。')
          scheduleWorkbenchPoll()
        }
        void (async () => {
          try {
            const experienceResp = await literatureApi.getCachedReaderExperiencePlan(paperId, experienceRequestPayload)
            if (!alive) return
            const recoveredExperienceContent = applyWorkbenchExperienceResponse(experienceResp)
            hasRecoverableContent = hasRecoverableContent || recoveredExperienceContent
            if (hasWorkbenchInspectablePlan(planResp, experienceResp)) {
              setPlanError(null)
              setBackgroundRefreshing(false)
              setSurfaceLoadState('ready')
            } else if (!hasWorkbenchInspectablePlan(planResp, null)) {
              setSurfaceLoadState(hasRecoverableContent ? 'showing_seed' : 'refreshing_fresh')
            }
          } catch {
            if (!alive) return
          }
        })()
      } catch (error) {
        if (!alive) return
        let recoveredExperienceContent = false
        try {
          const experienceResp = await literatureApi.getCachedReaderExperiencePlan(paperId, experienceRequestPayload)
          if (!alive) return
          recoveredExperienceContent = applyWorkbenchExperienceResponse(experienceResp)
          if (recoveredExperienceContent) {
            hasRecoverableContent = true
          }
        } catch {
          if (!alive) return
        }
        const message = error instanceof Error ? error.message : '加载增强计划失败'
        setPlanError(message)
        setGenerativePlanResponse(null)
        setPlanLoading(false)
        if (recoveredExperienceContent) {
          setBackgroundRefreshing(true)
          setSurfaceLoadState(hasRecoverableContent ? 'showing_seed' : 'refreshing_fresh')
          scheduleWorkbenchPoll()
          return
        }
        setSurfaceLoadState(hasRecoverableContent ? 'partial_error' : 'hard_error')
      }
    }

    const loadExperience = async () => {
      const requestPayload = {
        ...basePayload,
        focus_page: page,
        reader_profile: readerProfile.trim(),
        user_intent: userIntent.trim(),
        force_refresh: reloadState.forceFresh,
        regenerate: reloadState.forceFresh,
      }
      const forceFresh = reloadState.forceFresh
      const persistedExperience = !forceFresh && experiencePersistKey
        ? readPersistedExperienceSurface(experiencePersistKey)
        : null

      const schedulePoll = () => {
        if (!alive) return
        clearPolling()
        pollTimer = window.setTimeout(async () => {
          try {
            const cached = await literatureApi.getCachedReaderExperiencePlan(paperId, requestPayload)
            if (!alive) return
            const isSeed = isSeedExperiencePlan(cached)
            const hasCachedExperience = Boolean(cached?.experience_cache_hit && hasNonDraftExperiencePlan(cached))
            const hasCompletedCachedExperience = Boolean(cached?.experience_cache_hit && hasCompletedFinalPrimaryExperience(cached))
            const cachedComposePayload = hasUsableComposePayload(cached.compose_payload) ? cached.compose_payload : null
            const needsComposeHydration = Boolean(hasCachedExperience && !cachedComposePayload)
            const keepCurrent = shouldKeepCurrentExperience(experienceResponseRef.current, cached)
            if (hasCachedExperience) {
              if (cachedComposePayload) setComposePayload(cachedComposePayload)
              if (!keepCurrent) {
                setExperienceResponse(cached)
                const nextCacheState = buildExperienceCacheState(cached, { isSeed, isFresh: false })
                setCacheState(nextCacheState)
                persistExperienceSurface(
                  experiencePersistKey,
                  cachedComposePayload || composePayloadRef.current,
                  cached,
                  nextCacheState,
                )
              }
            }
            if (needsComposeHydration && !keepCurrent) {
              try {
                const composed = await literatureApi.getCachedReaderComposed(paperId, requestPayload)
                if (!alive) return
                const payload = composed.payload || null
                if (payload) {
                  setComposePayload(payload)
                  setCacheState((prev) => ({
                    ...prev,
                    composeLayer: String(composed.cache_meta?.cache_layer || payload.cache_layer || 'none'),
                    composeHit: Boolean(payload.cache_hit ?? composed.cache_meta?.cache_hit ?? false),
                  }))
                  persistExperienceSurface(
                    experiencePersistKey,
                    payload,
                    cached,
                    buildExperienceCacheState(cached, { isSeed, isFresh: false }),
                  )
                }
              } catch {
                // Keep polling; a later pass can still hydrate compose.
              }
            }
            if (!forceFresh && hasCompletedCachedExperience && !needsComposeHydration) {
              setPlanError(null)
              setBackgroundRefreshing(false)
              setSurfaceLoadState('ready')
              return
            }
            schedulePoll()
          } catch {
            if (!alive) return
            schedulePoll()
          }
        }, 6000)
      }

      if (persistedExperience) {
        setComposePayload(persistedExperience.composePayload)
        setExperienceResponse(persistedExperience.experienceResponse)
        setCacheState(persistedExperience.cacheState)
        setComposeLoading(false)
        setPlanLoading(false)
        setBackgroundRefreshing(false)
        setComposeError(null)
        setPlanError(null)
        setSurfaceLoadState('ready')
      } else {
        setComposeLoading(true)
        setPlanLoading(true)
        setBackgroundRefreshing(false)
        setComposeError(null)
        setPlanError(null)
        setSurfaceLoadState('loading_cached')
        setCacheState(EMPTY_CACHE_STATE)
      }
      let hasRecoverableContent = Boolean(persistedExperience?.experienceResponse || persistedExperience?.composePayload)

      let hasComposeReadyFromExperience = hasUsableComposePayload(persistedExperience?.composePayload)
      let cachedPlanIsSeed = false
      let hasCompletedFinalCachedExperience = hasCompletedFinalPrimaryExperience(persistedExperience?.experienceResponse)

      try {
        const cached = await literatureApi.getCachedReaderExperiencePlan(paperId, requestPayload)
        if (!alive) return
        const cachedComposePayload = hasUsableComposePayload(cached.compose_payload) ? cached.compose_payload : null
        const isSeed = isSeedExperiencePlan(cached)
        const hasRecoverablePlan = isSeed || hasNonDraftExperiencePlan(cached)
        const hasCachedExperience = Boolean(cached?.experience_cache_hit && hasNonDraftExperiencePlan(cached))
        const hasCompletedCachedExperience = Boolean(cached?.experience_cache_hit && hasCompletedFinalPrimaryExperience(cached))
        const cachedNeedsComposeHydration = Boolean(hasCachedExperience && !cachedComposePayload)
        cachedPlanIsSeed = isSeed
        hasCompletedFinalCachedExperience = hasCompletedCachedExperience

        if (cachedComposePayload) {
          setComposePayload(cachedComposePayload)
          hasComposeReadyFromExperience = true
        }
        if (hasRecoverablePlan || cachedComposePayload) {
          setComposeError(null)
          setExperienceResponse(cached)
          hasRecoverableContent = true
        }
        const cachedCacheState = buildExperienceCacheState(cached, { isSeed, isFresh: false })
        setCacheState(cachedCacheState)
        if (hasRecoverablePlan) {
          persistExperienceSurface(
            experiencePersistKey,
            cachedComposePayload,
            cached,
            cachedCacheState,
          )
        }

        if (!forceFresh && hasCompletedFinalCachedExperience) {
          if (cachedNeedsComposeHydration) {
            try {
              const composed = await literatureApi.getCachedReaderComposed(paperId, requestPayload)
              if (!alive) return
              const payload = hasUsableComposePayload(composed.payload) ? composed.payload : null
              if (payload) {
                setComposePayload(payload)
                setComposeError(null)
                const hydratedCacheState = buildExperienceCacheState(cached, { isSeed, isFresh: false })
                persistExperienceSurface(
                  experiencePersistKey,
                  payload,
                  cached,
                  hydratedCacheState,
                )
              } else {
                setComposeError('正文证据暂未就绪')
              }
            } catch {
              if (!alive) return
              setComposeError('正文证据暂未就绪')
            }
          }
          setComposeLoading(false)
          setPlanLoading(false)
          setBackgroundRefreshing(false)
          setPlanError(null)
          setSurfaceLoadState('ready')
          return
        }
      } catch {
        if (!alive) return
      }

      if (!hasComposeReadyFromExperience) {
        try {
          const composed = await literatureApi.getCachedReaderComposed(paperId, requestPayload)
          if (!alive) return
          const payload = composed.payload || null
          if (payload) {
            setComposePayload(payload)
            hasRecoverableContent = true
            setCacheState((prev) => ({
              ...prev,
              composeLayer: String(composed.cache_meta?.cache_layer || payload.cache_layer || 'none'),
              composeHit: Boolean(payload.cache_hit ?? composed.cache_meta?.cache_hit ?? false),
            }))
            setComposeError(null)
          } else {
            setComposeError('暂无正文底座')
          }
        } catch (error) {
          if (!alive) return
          setComposeError(error instanceof Error ? error.message : '加载正文底座失败')
        } finally {
          if (alive) setComposeLoading(false)
        }
      } else if (alive) {
        setComposeLoading(false)
      }

      try {
        if (!hasRecoverableContent && !hasComposeReadyFromExperience) {
          setComposeError('暂无正文底座')
        }

        setPlanLoading(false)
        setBackgroundRefreshing(true)
        setSurfaceLoadState(cachedPlanIsSeed || !hasCompletedFinalCachedExperience ? 'showing_seed' : 'refreshing_fresh')
        setPlanError(
          cachedPlanIsSeed || !hasCompletedFinalCachedExperience
            ? '体验内容仍在生成，当前先展示基础阅读内容。'
            : null,
        )

        const fresh = await literatureApi.getReaderExperiencePlan(paperId, requestPayload)
        if (!alive) return
        const freshIsSeed = isSeedExperiencePlan(fresh)
        const freshHasCompletedFinalPlan = hasCompletedFinalPrimaryExperience(fresh)
        const keepCurrent = shouldKeepCurrentExperience(experienceResponseRef.current, fresh)
        if (fresh.compose_payload) {
          setComposePayload(fresh.compose_payload)
        }
        if (fresh.compose_payload || freshIsSeed || freshHasCompletedFinalPlan) {
          setComposeError(null)
        }
        if (!keepCurrent) {
          setExperienceResponse(fresh)
        }
        const freshCacheState = buildExperienceCacheState(fresh, { isSeed: freshIsSeed, isFresh: true })
        setCacheState(freshCacheState)
        if (!keepCurrent && (fresh.compose_payload || freshHasCompletedFinalPlan)) {
          persistExperienceSurface(
            experiencePersistKey,
            fresh.compose_payload || composePayloadRef.current,
            fresh,
            freshCacheState,
          )
        }
        if (freshIsSeed || !freshHasCompletedFinalPlan) {
          setBackgroundRefreshing(true)
          setPlanError('体验内容仍在生成，当前先展示基础阅读内容。')
          setSurfaceLoadState('showing_seed')
          schedulePoll()
        } else {
          clearPolling()
          setBackgroundRefreshing(false)
          setPlanError(null)
          setSurfaceLoadState('ready')
        }
      } catch (error) {
        if (!alive) return
        const message = error instanceof Error ? error.message : '加载体验计划失败'
        setPlanLoading(false)
        setBackgroundRefreshing(hasRecoverableContent)
        setPlanError(hasRecoverableContent ? '体验内容仍在生成，当前先展示基础阅读内容。' : message)
        setSurfaceLoadState(hasRecoverableContent ? 'partial_error' : 'hard_error')
        if (hasRecoverableContent) schedulePoll()
      }
    }

    void (mode === 'experience' ? loadExperience() : loadWorkbench())

    return () => {
      alive = false
      clearPolling()
    }
  }, [
    mode,
    page,
    paperId,
    reloadNonce,
    reloadState.forceFresh,
    reloadState.nonce,
    experiencePersistKey,
    readerProfile,
    selectedKbId,
    userIntent,
  ])

  return {
    composePayload,
    experienceResponse,
    generativePlanResponse,
    composeError,
    planError,
    composeLoading,
    planLoading,
    backgroundRefreshing,
    surfaceLoadState,
    cacheState,
  }
}
