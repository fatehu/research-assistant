import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Alert, ConfigProvider, Empty, Result, Space, Spin, Tag, Typography, theme } from 'antd'
import { PageContainer, ProCard } from '@ant-design/pro-components'

import { literatureApi, type ReaderExperienceV2Request, type ReaderExperienceV2Response } from '@/services/api'

import PageArtifactV2Renderer from './PageArtifactV2Renderer'
import './pageArtifactV2.css'

const { Paragraph, Text, Title } = Typography

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
  return `reader-experience-v2:${paperId}:${JSON.stringify(request)}`
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

  useEffect(() => {
    document.body.classList.add('experience-v2-route-active')
    return () => {
      document.body.classList.remove('experience-v2-route-active')
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function run() {
      const optimistic = readLocalExperienceV2Snapshot(localCacheKey)
      if (optimistic && !cancelled) {
        setResponse(optimistic)
      }
      setLoading(!optimistic?.artifact)
      setBuilding(false)
      setError(null)
      try {
        const cached = await literatureApi.getCachedReaderExperienceV2(numericPaperId, request)
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

        setResponse((previous) => previous?.artifact ? previous : cached)
        setBuilding(true)
        const live = await literatureApi.getReaderExperienceV2(numericPaperId, request)
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
  }, [cacheOnly, numericPaperId, request, localCacheKey])

  const query = searchParams.toString()
  const workbenchHref = `/literature/${numericPaperId}/workbench-v2${query ? `?${query}` : ''}`
  const readerHref = `/literature/${numericPaperId}/read${query ? `?${query}` : ''}`

  const hasRenderableArtifact = Boolean(response?.artifact)

  return (
    <ConfigProvider theme={experienceV2ReaderTheme}>
      <div className="experience-v2-page">
        <PageContainer
          ghost
          className="experience-v2-page__container"
          title="当前页阅读体验"
          subTitle="按当前页主线组织解释、原文与图证，不在 reader 模式暴露运行细节。"
          tags={(
            <Space wrap>
              <Tag color="blue">page {request.page}</Tag>
              <Tag>{request.reader_profile}</Tag>
              {cacheOnly ? <Tag>cache only</Tag> : null}
              {response?.artifact_cache_hit ? <Tag color="green">artifact cache</Tag> : <Tag color="gold">fresh build</Tag>}
              {response?.status ? <Tag>{response.status}</Tag> : null}
            </Space>
          )}
          extra={[
            <Link key="workbench" to={workbenchHref}>打开 /workbench-v2</Link>,
            <Link key="reader" to={readerHref}>返回 /read</Link>,
          ]}
        >
          <div className="experience-v2-page__frame">
            <Space direction="vertical" size={20} style={{ width: '100%' }}>
              {hasRenderableArtifact && (loading || building) ? (
                <Alert
                  type="info"
                  showIcon
                  message={building ? '正在后台刷新这一页，先显示最近一次完成稿。' : '正在检查是否有更新的完成稿。'}
                />
              ) : null}

              {(loading || building) && !hasRenderableArtifact ? (
                <ProCard bordered className="experience-v2-page__shell" bodyStyle={{ padding: 28 }}>
                  <Space direction="vertical" size={12} style={{ width: '100%', alignItems: 'center' }}>
                    <Spin size="large" />
                    <Title level={4} className="experience-v2-page__shell-title">正在组织这一页的阅读体验</Title>
                    <Text type="secondary" className="experience-v2-page__shell-copy">
                      {cacheOnly
                        ? '正在检查是否已有可直接复用的完成稿，不会自动重新生成。'
                        : building
                          ? '系统正在整理当前页主线、图表位点和必要补充。'
                          : '正在检查是否已有可直接复用的完成稿。'}
                    </Text>
                  </Space>
                </ProCard>
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
        </PageContainer>
      </div>
    </ConfigProvider>
  )
}
