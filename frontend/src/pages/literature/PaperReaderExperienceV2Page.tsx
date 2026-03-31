import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Alert, ConfigProvider, Empty, Result, Space, Typography, message, theme } from 'antd'

import {
  literatureApi,
  type PageArtifactV2ReadingBlock,
  type ReaderExperienceV2Request,
  type ReaderExperienceV2Response,
} from '@/services/api'

import PageArtifactV2Renderer from './PageArtifactV2Renderer'
import './pageArtifactV2.css'

const { Text, Title } = Typography

const experienceV2ReaderTheme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#3d68b3',
    colorInfo: '#3d68b3',
    colorLink: '#3d68b3',
    colorSuccess: '#2f7f63',
    colorWarning: '#c58a2c',
    colorError: '#b4554f',
    colorText: '#182033',
    colorTextSecondary: 'rgba(24, 32, 51, 0.72)',
    colorTextTertiary: 'rgba(24, 32, 51, 0.52)',
    colorBgBase: '#f5efe4',
    colorBgLayout: '#f5efe4',
    colorBgContainer: 'rgba(255, 252, 246, 0.98)',
    colorBgElevated: '#ffffff',
    colorBorder: 'rgba(86, 110, 156, 0.18)',
    colorBorderSecondary: 'rgba(86, 110, 156, 0.12)',
    boxShadowSecondary: '0 18px 38px rgba(24, 34, 58, 0.08)',
    borderRadius: 16,
    borderRadiusLG: 24,
    borderRadiusSM: 10,
    fontFamily: '"IBM Plex Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
  },
  components: {
    Card: {
      colorBgContainer: 'rgba(255, 252, 246, 0.98)',
      headerBg: 'transparent',
    },
    Tag: {
      defaultBg: 'rgba(61, 104, 179, 0.08)',
      defaultColor: '#31589d',
    },
    Alert: {
      defaultPadding: 16,
    },
    Result: {
      titleFontSize: 30,
      subtitleFontSize: 15,
    },
  },
} as const

function toNumericParam(raw: string | null | undefined, fallback: number) {
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? value : fallback
}

function extractErrorMessage(error: unknown): string {
  const responseDetail = (error as any)?.response?.data?.detail
  if (typeof responseDetail === 'string' && responseDetail.trim()) return responseDetail.trim()
  const message = (error as Error | undefined)?.message
  return message && message.trim() ? message.trim() : 'experience-v2 route failed'
}

function buildRequest(searchParams: URLSearchParams): ReaderExperienceV2Request {
  const page = toNumericParam(searchParams.get('page'), 1)
  return {
    page,
    selected_kb_id: toNumericParam(searchParams.get('kb'), 0),
    reader_profile: String(searchParams.get('reader') || 'curious_generalist').trim() || 'curious_generalist',
    user_intent: String(searchParams.get('intent') || '').trim(),
    force_refresh: searchParams.get('refresh') === '1',
    regenerate: searchParams.get('regenerate') === '1',
  }
}

function buildExperienceV2LocalCacheKey(paperId: number, request: ReaderExperienceV2Request) {
  const stableRequest = {
    ...request,
    force_refresh: false,
    regenerate: false,
  }
  return `reader-experience-v2:${paperId}:${JSON.stringify(stableRequest)}`
}

function readLocalExperienceV2Snapshot(cacheKey: string): ReaderExperienceV2Response | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(cacheKey)
    if (!raw) return null
    const parsed = JSON.parse(raw) as ReaderExperienceV2Response
    if (parsed && typeof parsed === 'object' && parsed.status === 'ready' && parsed.artifact) {
      return parsed
    }
  } catch {
    return null
  }
  return null
}

function writeLocalExperienceV2Snapshot(cacheKey: string, response: ReaderExperienceV2Response) {
  if (typeof window === 'undefined') return
  if (response.status !== 'ready' || !response.artifact) return
  try {
    window.sessionStorage.setItem(cacheKey, JSON.stringify(response))
  } catch {
    // Ignore quota/storage errors and keep the in-memory UI path intact.
  }
}

function formatReaderProfileLabel(profile: string): string {
  const normalized = String(profile || '').trim()
  if (!normalized) return '探索视角'
  if (normalized === 'curious_generalist') return '探索视角'
  return normalized.replace(/[_-]+/g, ' ')
}

function getRewritePromptPlaceholder(block: PageArtifactV2ReadingBlock | null): string {
  const kind = String(block?.segment_kind || '').trim()
  if (kind === 'heading') return '例如：让标题更聚焦这一页的主线，但不要变成长句。'
  if (kind === 'term_annotation') return '例如：把这个术语说明得更清楚一些，但保持页边注释口吻。'
  if (kind === 'aside_content') return '例如：把这条页边提示改得更简洁，直接服务当前页主线。'
  return '例如：把这一段讲得更通俗，但保留关键概念和当前页语境。'
}

function compactRewritePreview(raw: string, maxLength = 220): string {
  const text = String(raw || '').trim().replace(/\s+/g, ' ')
  if (!text) return ''
  if (text.length <= maxLength) return text
  return `${text.slice(0, maxLength - 1).trimEnd()}…`
}

export default function PaperReaderExperienceV2Page() {
  const { paperId } = useParams()
  const [searchParams] = useSearchParams()
  const numericPaperId = Number(paperId || 0)
  const request = useMemo(() => buildRequest(searchParams), [searchParams])
  const cacheOnly = searchParams.get('cache_only') === '1'
  const localCacheKey = useMemo(
    () => buildExperienceV2LocalCacheKey(numericPaperId, request),
    [numericPaperId, request],
  )
  const [response, setResponse] = useState<ReaderExperienceV2Response | null>(null)
  const [loading, setLoading] = useState(true)
  const [building, setBuilding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rewriteTarget, setRewriteTarget] = useState<PageArtifactV2ReadingBlock | null>(null)
  const [rewritePrompt, setRewritePrompt] = useState('')
  const [rewriteSubmitting, setRewriteSubmitting] = useState(false)
  const [recentRewriteMarker, setRecentRewriteMarker] = useState<{ blockId: string; nonce: number } | null>(null)
  const pendingRunOptionsRef = useRef<{ regenerate: boolean }>({ regenerate: false })
  const [runSeed, setRunSeed] = useState(0)

  useEffect(() => {
    document.body.classList.add('experience-v2-route-active')
    return () => {
      document.body.classList.remove('experience-v2-route-active')
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function run() {
      const pendingRunOptions = pendingRunOptionsRef.current
      pendingRunOptionsRef.current = { regenerate: false }
      const runRequest: ReaderExperienceV2Request = {
        ...request,
        regenerate: request.regenerate || pendingRunOptions.regenerate,
      }
      const shouldBypassCached = Boolean(runRequest.force_refresh || runRequest.regenerate)
      const optimistic = readLocalExperienceV2Snapshot(localCacheKey)
      if (optimistic && !cancelled) {
        setResponse(optimistic)
      }
      setLoading(!optimistic?.artifact)
      setBuilding(false)
      setError(null)
      try {
        let cached: ReaderExperienceV2Response | null = null
        if (!shouldBypassCached) {
          cached = await literatureApi.getCachedReaderExperienceV2(numericPaperId, runRequest)
          if (cancelled) return
          if (cached.status === 'ready' && cached.artifact) {
            setResponse(cached)
            writeLocalExperienceV2Snapshot(localCacheKey, cached)
            setLoading(false)
            return
          }

          if (cacheOnly) {
            setResponse((previous) => previous?.artifact ? previous : cached)
            setError(cached.failure_detail || '当前只读取缓存，未触发重新生成。')
            setLoading(false)
            return
          }
        }

        setResponse((previous) => previous?.artifact ? previous : cached)
        setBuilding(true)
        const live = await literatureApi.getReaderExperienceV2(numericPaperId, runRequest)
        if (cancelled) return
        setResponse(live)
        writeLocalExperienceV2Snapshot(localCacheKey, live)
        if (live.status !== 'ready' || !live.artifact) {
          setError(live.failure_detail || 'completed page_artifact_v2 not available')
        }
      } catch (err) {
        if (!cancelled) setError(extractErrorMessage(err))
      } finally {
        if (!cancelled) {
          setLoading(false)
          setBuilding(false)
        }
      }
    }

    if (numericPaperId > 0) {
      void run()
    } else {
      setLoading(false)
      setError('invalid paper id')
    }

    return () => {
      cancelled = true
    }
  }, [cacheOnly, numericPaperId, request, localCacheKey, runSeed])

  const handleRegenerate = () => {
    pendingRunOptionsRef.current = { regenerate: true }
    setRunSeed((previous) => previous + 1)
  }

  const handleOpenRewrite = (block: PageArtifactV2ReadingBlock) => {
    if (rewriteSubmitting) return
    if (rewriteTarget?.segment_id === block.segment_id) {
      setRewriteTarget(null)
      setRewritePrompt('')
      return
    }
    setRewriteTarget(block)
    setRewritePrompt('')
  }

  const handleCloseRewrite = () => {
    if (rewriteSubmitting) return
    setRewriteTarget(null)
    setRewritePrompt('')
  }

  const handleRewriteSubmit = async () => {
    if (!rewriteTarget) return
    const normalizedPrompt = String(rewritePrompt || '').trim()
    if (!normalizedPrompt) return
    try {
      setRewriteSubmitting(true)
      const result = await literatureApi.rewriteReaderExperienceV2Block(numericPaperId, {
        page: request.page,
        block_id: rewriteTarget.segment_id,
        rewrite_prompt: normalizedPrompt,
        selected_kb_id: request.selected_kb_id,
        reader_profile: request.reader_profile,
        user_intent: request.user_intent,
      })
      const nextResponse: ReaderExperienceV2Response = {
        ...(response || { focus_page: result.focus_page }),
        focus_page: result.focus_page,
        status: 'ready',
        artifact: result.artifact,
        failure_detail: '',
      }
      setResponse(nextResponse)
      writeLocalExperienceV2Snapshot(localCacheKey, nextResponse)
      setError(null)
      setRecentRewriteMarker({ blockId: result.rewritten_block.segment_id, nonce: Date.now() })
      message.success(result.message || '当前块已重写')
      setRewriteTarget(null)
      setRewritePrompt('')
    } catch (err) {
      message.error(extractErrorMessage(err))
    } finally {
      setRewriteSubmitting(false)
    }
  }

  const query = searchParams.toString()
  const workbenchHref = `/literature/${numericPaperId}/workbench-v2${query ? `?${query}` : ''}`
  const readerHref = `/literature/${numericPaperId}/read${query ? `?${query}` : ''}`

  const hasRenderableArtifact = Boolean(response?.artifact)
  const readerProfileLabel = formatReaderProfileLabel(request.reader_profile || '')
  const ambientStatusCopy = building
    ? '正在后台刷新这一页，先显示最近一次完成稿。'
    : '正在检查是否有更新的完成稿。'
  const loadingCopy = cacheOnly
    ? '正在检查是否已有可直接复用的完成稿，不会自动重新生成。'
    : building
      ? '系统正在整理当前页主线、图表位点和必要补充。'
      : '正在检查是否已有可直接复用的完成稿。'

  return (
    <ConfigProvider theme={experienceV2ReaderTheme}>
      <div className="experience-v2-page">
        <div className="experience-v2-page__frame">
          <div className="experience-v2-page__header">
            <div className="experience-v2-page__header-main">
              <Text type="secondary" className="experience-v2-page__eyebrow">
                {`Page ${request.page} • ${readerProfileLabel}`}
              </Text>
              <Title level={2} className="experience-v2-page__title">{`第 ${request.page} 页的阅读体验`}</Title>
              <Text className="experience-v2-page__subtitle">
                按当前页主线组织解释、原文与图证，把视觉重心留给正文。
              </Text>
            </div>
            <div className="experience-v2-page__header-actions">
              <Link className="page-artifact-v2__action-chip page-artifact-v2__action-chip--navigate" to={readerHref}>
                返回 /read
              </Link>
              {!cacheOnly ? (
                <button
                  type="button"
                  className="page-artifact-v2__action-chip page-artifact-v2__action-chip--focus experience-v2-page__action-chip"
                  disabled={loading || building}
                  aria-busy={building}
                  onClick={handleRegenerate}
                >
                  {building ? '重新生成中' : '重新生成'}
                </button>
              ) : null}
              <Link className="experience-v2-page__utility-link" to={workbenchHref}>
                查看 workbench
              </Link>
            </div>
          </div>

          <Space direction="vertical" size={20} style={{ width: '100%' }}>
            {hasRenderableArtifact && (loading || building) ? (
              <div className="experience-v2-page__status-strip" role="status" aria-live="polite">
                <span className="experience-v2-page__status-dot" />
                <span className="experience-v2-page__status-copy">{ambientStatusCopy}</span>
              </div>
            ) : null}

            {(loading || building) && !hasRenderableArtifact ? (
              <div className="experience-v2-page__shell experience-v2-page__shell--loading">
                <div className="experience-v2-page__shell-kicker">
                  {cacheOnly ? 'Cache check' : building ? 'Fresh composition' : 'Artifact lookup'}
                </div>
                <Title level={4} className="experience-v2-page__shell-title">正在组织这一页的阅读体验</Title>
                <Text type="secondary" className="experience-v2-page__shell-copy">{loadingCopy}</Text>
                <div className="experience-v2-page__shell-skeleton" aria-hidden="true">
                  <span className="experience-v2-page__shell-line experience-v2-page__shell-line--wide" />
                  <span className="experience-v2-page__shell-line experience-v2-page__shell-line--mid" />
                  <span className="experience-v2-page__shell-line experience-v2-page__shell-line--short" />
                </div>
              </div>
            ) : null}

            {!loading && !building && error && !hasRenderableArtifact ? (
              <Result
                status="error"
                title="experience-v2 未能完成"
                subTitle={error}
                extra={<Link to={workbenchHref}>去 /workbench-v2 查看 dossier、session 与失败状态</Link>}
              />
            ) : null}

            {response?.artifact ? (
              <PageArtifactV2Renderer
                artifact={response.artifact}
                mode="reader"
                navigation={{
                  paperId: numericPaperId,
                  readerProfile: request.reader_profile,
                  selectedKbId: request.selected_kb_id,
                  userIntent: request.user_intent,
                }}
                onRewriteBlockRequest={handleOpenRewrite}
                onRewriteBlockCancel={handleCloseRewrite}
                activeRewriteBlockId={rewriteTarget?.segment_id || null}
                rewriteDraft={rewritePrompt}
                rewritePromptPlaceholder={getRewritePromptPlaceholder(rewriteTarget)}
                rewritePreviewText={compactRewritePreview(rewriteTarget?.text || '')}
                onRewriteDraftChange={setRewritePrompt}
                onRewriteSubmit={handleRewriteSubmit}
                rewritingBlockId={rewriteSubmitting ? rewriteTarget?.segment_id || null : null}
                rewriteDisabled={!response?.artifact || rewriteSubmitting}
                recentRewriteMarker={recentRewriteMarker}
              />
            ) : null}

            {!loading && !building && !error && !response?.artifact ? (
              <Empty description="completed page_artifact_v2 not available" />
            ) : null}

            {response?.failure_detail && !error && !hasRenderableArtifact ? (
              <Alert type="warning" showIcon message={response.failure_detail} />
            ) : null}
          </Space>
        </div>
      </div>
    </ConfigProvider>
  )
}
