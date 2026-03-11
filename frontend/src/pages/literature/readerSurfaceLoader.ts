import { useEffect, useState } from 'react'

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

function hasNonDraftExperiencePlan(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  const plan = response?.plan
  return Boolean(plan && typeof plan === 'object' && plan.status && plan.status !== 'draft')
}

function isSeedExperiencePlan(response: ReaderExperiencePlanResponse | null | undefined): boolean {
  const plan = response?.plan
  const meta = plan && typeof plan === 'object'
    ? ((plan.meta as Record<string, unknown> | undefined) || undefined)
    : undefined
  return Boolean(meta?.seed_plan || response?.experience_cache_layer === 'derived_seed')
}

export function useReaderSurfaceLoader(options: ReaderSurfaceLoaderOptions): ReaderSurfaceLoaderResult {
  const paperId = options.paperId
  const page = options.page
  const selectedKbId = options.selectedKbId
  const userIntent = options.userIntent
  const mode = options.mode
  const readerProfile = options.mode === 'experience' ? options.readerProfile : ''
  const reloadNonce = options.mode === 'workbench' ? options.reloadNonce : 0
  const reloadState = options.mode === 'experience' ? options.reloadState : { nonce: 0, forceFresh: false }

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
      let hasComposeContent = false

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
        hasComposeContent = Boolean(nextComposePayload)
        setComposeLoading(false)
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(composeResp.cache_meta?.cache_layer || composeResp.payload?.cache_layer || 'none'),
          composeHit: Boolean(composeResp.payload?.cache_hit ?? composeResp.cache_meta?.cache_hit ?? false),
        }))
      } catch (error) {
        if (!alive) return
        setHardError(error instanceof Error ? error.message : '加载正文底座失败')
        return
      }

      try {
        const planResp = await literatureApi.getReaderGenerativePlan(paperId, {
          ...basePayload,
          style_intent: 'reader_workbench',
          force_refresh: false,
          regenerate: false,
          user_intent: userIntent.trim(),
        })
        if (!alive) return
        setGenerativePlanResponse(planResp)
        setPlanError(null)
        setPlanLoading(false)
        setSurfaceLoadState('ready')
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(planResp.cache_layer || prev.composeLayer || 'none'),
          composeHit: Boolean(planResp.cache_hit),
          planLayer: String(planResp.plan_cache_layer || 'none'),
          planHit: Boolean(planResp.plan_cache_hit),
          isFresh: !planResp.plan_cache_hit,
        }))
      } catch (error) {
        if (!alive) return
        const message = error instanceof Error ? error.message : '加载增强计划失败'
        setPlanError(message)
        setGenerativePlanResponse(null)
        setPlanLoading(false)
        setSurfaceLoadState(hasComposeContent ? 'partial_error' : 'hard_error')
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

      const schedulePoll = () => {
        if (!alive) return
        clearPolling()
        pollTimer = window.setTimeout(async () => {
          try {
            const cached = await literatureApi.getCachedReaderExperiencePlan(paperId, requestPayload)
            if (!alive) return
            const isSeed = isSeedExperiencePlan(cached)
            const hasCachedExperience = Boolean(cached?.experience_cache_hit && hasNonDraftExperiencePlan(cached))
            if (hasCachedExperience) {
              if (cached.compose_payload) setComposePayload(cached.compose_payload)
              setExperienceResponse(cached)
              setCacheState((prev) => ({
                ...prev,
                composeLayer: String(cached.cache_layer || prev.composeLayer || 'none'),
                composeHit: Boolean(cached.cache_hit),
                planLayer: String(cached.generative_plan_cache_layer || prev.planLayer || 'none'),
                planHit: Boolean(cached.generative_plan_cache_hit),
                experienceLayer: String(cached.experience_cache_layer || 'none'),
                experienceHit: Boolean(cached.experience_cache_hit),
                isSeed,
              }))
            }
            if (!forceFresh && hasCachedExperience && !isSeed) {
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

      setComposeLoading(true)
      setPlanLoading(true)
      setBackgroundRefreshing(false)
      setComposeError(null)
      setPlanError(null)
      setSurfaceLoadState('loading_cached')
      setCacheState(EMPTY_CACHE_STATE)
      let hasRecoverableContent = false

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
        setHardError(error instanceof Error ? error.message : '加载正文底座失败')
        return
      } finally {
        if (alive) setComposeLoading(false)
      }

      try {
        const cached = await literatureApi.getCachedReaderExperiencePlan(paperId, requestPayload)
        if (!alive) return
        const cachedComposePayload = cached.compose_payload || null
        if (cachedComposePayload) setComposePayload(cachedComposePayload)
        const isSeed = isSeedExperiencePlan(cached)
        const hasCachedExperience = Boolean(cached?.experience_cache_hit && hasNonDraftExperiencePlan(cached))
        if (hasCachedExperience || cachedComposePayload) {
          setExperienceResponse(cached)
          hasRecoverableContent = true
        }
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(cached.cache_layer || prev.composeLayer || 'none'),
          composeHit: Boolean(cached.cache_hit),
          planLayer: String(cached.generative_plan_cache_layer || 'none'),
          planHit: Boolean(cached.generative_plan_cache_hit),
          experienceLayer: String(cached.experience_cache_layer || 'none'),
          experienceHit: Boolean(cached.experience_cache_hit),
          isSeed,
        }))
        if (!forceFresh && hasCachedExperience && !isSeed) {
          setPlanLoading(false)
          setBackgroundRefreshing(false)
          setPlanError(null)
          setSurfaceLoadState('ready')
          return
        }

        setPlanLoading(false)
        setBackgroundRefreshing(true)
        setSurfaceLoadState(isSeed || hasCachedExperience ? 'showing_seed' : 'refreshing_fresh')
        setPlanError(
          isSeed
            ? '已先展示基础体验，后台继续生成更完整的增强内容。'
            : '正在生成更完整的体验内容，先展示正文底座。',
        )
        schedulePoll()

        const fresh = await literatureApi.getReaderExperiencePlan(paperId, requestPayload)
        if (!alive) return
        if (fresh.compose_payload) setComposePayload(fresh.compose_payload)
        setExperienceResponse(fresh)
        setBackgroundRefreshing(isSeedExperiencePlan(fresh))
        setPlanError(null)
        setSurfaceLoadState(isSeedExperiencePlan(fresh) ? 'showing_seed' : 'ready')
        setCacheState((prev) => ({
          ...prev,
          composeLayer: String(fresh.cache_layer || prev.composeLayer || 'none'),
          composeHit: Boolean(fresh.cache_hit),
          planLayer: String(fresh.generative_plan_cache_layer || prev.planLayer || 'none'),
          planHit: Boolean(fresh.generative_plan_cache_hit),
          experienceLayer: String(fresh.experience_cache_layer || prev.experienceLayer || 'none'),
          experienceHit: Boolean(fresh.experience_cache_hit),
          isSeed: isSeedExperiencePlan(fresh),
          isFresh: true,
        }))
      } catch (error) {
        if (!alive) return
        const message = error instanceof Error ? error.message : '加载体验计划失败'
        setPlanLoading(false)
        setBackgroundRefreshing(hasRecoverableContent)
        setPlanError(hasRecoverableContent ? '已先展示基础体验，后台继续生成更完整的增强内容。' : message)
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
