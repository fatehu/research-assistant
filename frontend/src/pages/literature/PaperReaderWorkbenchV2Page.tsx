import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Alert, Collapse, ConfigProvider, Empty, Result, Space, Spin, Tag, Typography, theme } from 'antd'
import { PageContainer, ProCard, ProDescriptions } from '@ant-design/pro-components'

import { literatureApi, type ReaderExperienceV2Request, type ReaderWorkbenchV2Response } from '@/services/api'

import PageArtifactV2Renderer from './PageArtifactV2Renderer'
import './pageArtifactV2.css'

const { Paragraph, Text, Title } = Typography

const workbenchV2Theme = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#355ea7',
    colorInfo: '#355ea7',
    colorLink: '#355ea7',
    colorWarning: '#c58a2c',
    colorError: '#b4554f',
    colorText: '#18263e',
    colorTextSecondary: 'rgba(24, 38, 62, 0.72)',
    colorBgBase: '#f3efe6',
    colorBgLayout: '#f3efe6',
    colorBgContainer: 'rgba(255, 252, 246, 0.98)',
    colorBgElevated: '#fffdfa',
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
  },
} as const

function toNumericParam(raw: string | null | undefined, fallback: number) {
  const value = Number(raw)
  return Number.isFinite(value) && value >= 0 ? value : fallback
}

function extractErrorMessage(error: unknown): string {
  const responseDetail = (error as any)?.response?.data?.detail
  if (typeof responseDetail === 'string' && responseDetail.trim()) return responseDetail.trim()
  const message = (error as Error | undefined)?.message
  return message && message.trim() ? message.trim() : 'workbench-v2 route failed'
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

function JsonPanel(props: { title: string; value: unknown }) {
  return (
    <ProCard bordered title={props.title}>
      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {JSON.stringify(props.value ?? null, null, 2)}
      </pre>
    </ProCard>
  )
}

export default function PaperReaderWorkbenchV2Page() {
  const { paperId } = useParams()
  const [searchParams] = useSearchParams()
  const numericPaperId = Number(paperId || 0)
  const request = useMemo(() => buildRequest(searchParams), [searchParams])
  const [response, setResponse] = useState<ReaderWorkbenchV2Response | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.body.classList.add('workbench-v2-route-active')
    return () => {
      document.body.classList.remove('workbench-v2-route-active')
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function run() {
      setLoading(true)
      setError(null)
      try {
        const next = await literatureApi.getReaderWorkbenchV2(numericPaperId, request)
        if (cancelled) return
        setResponse(next)
      } catch (err) {
        if (!cancelled) setError(extractErrorMessage(err))
      } finally {
        if (!cancelled) setLoading(false)
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
  }, [numericPaperId, request])

  const query = searchParams.toString()
  const experienceHref = `/literature/${numericPaperId}/experience-v2${query ? `?${query}` : ''}`

  return (
    <ConfigProvider theme={workbenchV2Theme}>
      <div className="workbench-v2-page">
        <PageContainer
          ghost
          className="workbench-v2-page__container"
          title="生成链检查台"
          subTitle="这里保留 dossier、session、narrative brief、artifact 与失败状态；reader-facing 页面默认不展示这些内部细节。"
          tags={(
            <Space wrap>
              <Tag color="blue">page {request.page}</Tag>
              <Tag>{request.reader_profile}</Tag>
              {response?.status ? <Tag>{response.status}</Tag> : null}
              {response?.artifact_cache_hit ? <Tag color="green">artifact cache</Tag> : null}
              {response?.session_cache_hit ? <Tag color="purple">session cache</Tag> : null}
            </Space>
          )}
          extra={[<Link key="experience" to={experienceHref}>打开 /experience-v2</Link>]}
        >
          <Space direction="vertical" size={20} className="workbench-v2-page__stack">
            {!loading && !error && response ? (
              <ProCard bordered className="workbench-v2-page__summary">
                <ProDescriptions
                  column={2}
                  dataSource={{
                    status: response.status,
                    artifactCache: response.artifact_cache_hit ? '命中' : '未命中',
                    sessionCache: response.session_cache_hit ? '命中' : '未命中',
                    focusPage: response.focus_page,
                    readerProfile: request.reader_profile,
                    hasArtifact: response.artifact ? 'yes' : 'no',
                  }}
                  columns={[
                    { title: '状态', dataIndex: 'status' },
                    { title: '成品缓存', dataIndex: 'artifactCache' },
                    { title: '会话缓存', dataIndex: 'sessionCache' },
                    { title: '页码', dataIndex: 'focusPage' },
                    { title: '读者档案', dataIndex: 'readerProfile' },
                    { title: 'artifact', dataIndex: 'hasArtifact' },
                  ]}
                />
              </ProCard>
            ) : null}

            {loading ? (
              <ProCard bordered>
                <Space direction="vertical" size={12} style={{ width: '100%', alignItems: 'center' }}>
                  <Spin size="large" />
                  <Text type="secondary">正在载入 /workbench-v2 runtime snapshot</Text>
                </Space>
              </ProCard>
            ) : null}

            {!loading && error ? (
              <Result status="error" title="/workbench-v2 未能载入" subTitle={error} />
            ) : null}

            {!loading && !error && response ? (
              <div style={{ display: 'grid', gap: 20 }}>
                {response.failure_detail ? <Alert type="warning" showIcon message={response.failure_detail} /> : null}

                {response.artifact ? <PageArtifactV2Renderer artifact={response.artifact} mode="workbench" /> : <Empty description="artifact snapshot unavailable" />}

                <Collapse
                  items={[
                    {
                      key: 'dossier',
                      label: 'Reading Dossier V2',
                      children: <JsonPanel title="reading_dossier_v2" value={response.reading_dossier} />,
                    },
                    {
                      key: 'session',
                      label: 'Experience Session V2',
                      children: <JsonPanel title="experience_session_v2" value={response.session} />,
                    },
                    {
                      key: 'artifact',
                      label: 'Artifact Validation',
                      children: <JsonPanel title="page_artifact_v2 validation" value={response.artifact_validation} />,
                    },
                    {
                      key: 'compose',
                      label: 'Compose Payload Snapshot',
                      children: <JsonPanel title="compose payload" value={response.compose_payload} />,
                    },
                  ]}
                />
              </div>
            ) : null}
          </Space>
        </PageContainer>
      </div>
    </ConfigProvider>
  )
}
