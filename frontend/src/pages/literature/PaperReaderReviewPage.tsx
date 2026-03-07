import { type CSSProperties, useEffect, useMemo, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { Alert, Card, ConfigProvider, Empty, Space, Spin, Tag, Typography, theme } from 'antd'

import {
  type ReaderComponentNode,
  type ReaderComposeAsset,
  type ReaderComposeReviewSnapshot,
  type ReaderGenerativeStyleKey,
  literatureApi,
} from '@/services/api'
import {
  normalizeGenerativeStyleKey,
  resolveGenerativeStyleTokens,
  type ReaderThemeMode,
} from './generativeStyles'
import { renderReaderComponentTree } from './readerComponents'
import './composedReader.css'

const { Title, Text } = Typography

const READER_API_BASE_URL = String(
  ((import.meta as any).env?.VITE_API_BASE_URL as string) || 'http://localhost:8888',
).trim()

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

function mapComposeStyleIntentToKey(styleIntent: string | undefined): ReaderGenerativeStyleKey {
  const normalized = String(styleIntent || '').trim().toLowerCase()
  if (normalized === 'clinical' || normalized === 'clinical_brief') return 'clinical_brief'
  if (normalized === 'preprint' || normalized === 'preprint_modern') return 'preprint_modern'
  return 'journal_classic'
}

function pickTokenString(tokens: Record<string, unknown>, keys: string[], fallback: string): string {
  for (const key of keys) {
    const value = String(tokens[key] ?? '').trim()
    if (value) return value
  }
  return fallback
}

function pickTokenNumber(tokens: Record<string, unknown>, keys: string[], fallback: number): number {
  for (const key of keys) {
    const value = Number(tokens[key])
    if (Number.isFinite(value)) return value
  }
  return fallback
}

function toAbsoluteApiUrl(rawUrl: string): string {
  const token = String(rawUrl || '').trim()
  if (!token) return ''
  if (/^https?:\/\//i.test(token) || token.startsWith('data:') || token.startsWith('blob:')) return token
  if (!token.startsWith('/')) return token
  if (!READER_API_BASE_URL) return token
  return `${READER_API_BASE_URL}${token}`
}

export default function PaperReaderReviewPage() {
  const { paperId: paperIdParam } = useParams()
  const [searchParams] = useSearchParams()
  const [snapshot, setSnapshot] = useState<ReaderComposeReviewSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const paperId = Number(paperIdParam || 0)
  const sessionId = String(searchParams.get('sessionId') || '').trim()
  const snapshotId = String(searchParams.get('snapshotId') || '').trim()

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      if (!paperId || !sessionId) {
        setError('Missing paperId or sessionId for review snapshot.')
        setLoading(false)
        return
      }
      setLoading(true)
      setError('')
      try {
        const next = await literatureApi.getReaderComposeReviewSnapshot(
          paperId,
          sessionId,
          snapshotId || undefined,
        )
        if (!cancelled) {
          setSnapshot(next)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load review snapshot.')
          setSnapshot(null)
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [paperId, sessionId, snapshotId])

  const themeMode = useMemo<ReaderThemeMode>(() => {
    const traceMeta = (snapshot?.ui_plan?.trace_meta || {}) as Record<string, unknown>
    const raw = String(snapshot?.theme_mode || traceMeta.theme_mode || 'light').trim().toLowerCase()
    return raw === 'dark' ? 'dark' : 'light'
  }, [snapshot])

  const styleKey = useMemo<ReaderGenerativeStyleKey>(() => {
    const traceMeta = (snapshot?.ui_plan?.trace_meta || {}) as Record<string, unknown>
    const styleIntent = String(snapshot?.style_intent || traceMeta.style_intent || '').trim()
    return normalizeGenerativeStyleKey(mapComposeStyleIntentToKey(styleIntent))
  }, [snapshot])

  const activeStyle = useMemo(() => {
    const base = resolveGenerativeStyleTokens(styleKey, themeMode)
    const overlay = (snapshot?.ui_plan?.style_tokens || {}) as Record<string, unknown>
    return {
      pageBackground: pickTokenString(overlay, ['pageBackground', 'page_background'], base.pageBackground),
      panelBackground: pickTokenString(overlay, ['panelBackground', 'panel_background'], base.panelBackground),
      surfaceBackground: pickTokenString(overlay, ['surfaceBackground', 'surface_background'], base.surfaceBackground),
      railBackground: pickTokenString(overlay, ['railBackground', 'rail_background'], base.railBackground),
      overlayBackground: pickTokenString(overlay, ['overlayBackground', 'overlay_background'], base.overlayBackground),
      borderColor: pickTokenString(overlay, ['borderColor', 'border_color'], base.borderColor),
      headingColor: pickTokenString(overlay, ['headingColor', 'heading_color'], base.headingColor),
      bodyColor: pickTokenString(overlay, ['bodyColor', 'body_color'], base.bodyColor),
      mutedColor: pickTokenString(overlay, ['mutedColor', 'muted_color'], base.mutedColor),
      bodyFontFamily: pickTokenString(overlay, ['bodyFontFamily', 'body_font_family'], base.bodyFontFamily),
      headingFontFamily: pickTokenString(overlay, ['headingFontFamily', 'heading_font_family'], base.headingFontFamily),
      bodyFontSize: pickTokenNumber(overlay, ['bodyFontSize', 'body_font_size'], base.bodyFontSize),
      bodyLineHeight: pickTokenNumber(overlay, ['bodyLineHeight', 'body_line_height'], base.bodyLineHeight),
    }
  }, [snapshot, styleKey, themeMode])

  const contentMaxWidth = useMemo(() => {
    const layout = (snapshot?.ui_plan?.layout || {}) as Record<string, unknown>
    const width = Number(layout.content_max_width ?? layout.contentMaxWidth ?? 960)
    if (!Number.isFinite(width)) return 960
    return clamp(width, 680, 1280)
  }, [snapshot])

  const assets = useMemo<ReaderComposeAsset[]>(() => snapshot?.assets || [], [snapshot])
  const composedPageImageUrl = String(snapshot?.docmind_page_image_url || '').trim()

  const resolveFigureImageUrl = (rawUrl: string, node?: ReaderComponentNode): string => {
    const token = String(rawUrl || '').trim()
    if (!token) return ''
    const assetPage = (() => {
      const pages = Array.isArray(node?.source_anchor_refs)
        ? node.source_anchor_refs
          .map((item) => Number(item?.page || 0))
          .filter((item) => Number.isFinite(item) && item > 0)
        : []
      return pages[0] || Number(snapshot?.page || 1)
    })()
    const sourceBlockIds = Array.isArray(node?.source_block_ids)
      ? node.source_block_ids.map((item) => String(item || '').trim()).filter(Boolean)
      : []
    const pickImageHintUrl = (): string => {
      for (const asset of assets) {
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
      for (const asset of assets) {
        if (asset.kind !== 'image_hint') continue
        const meta = (asset.meta && typeof asset.meta === 'object') ? asset.meta as Record<string, unknown> : {}
        const candidateId = String(meta.asset_id || meta.layout_unique_id || meta.unique_id || '').trim()
        const candidateUrl = String(asset.href || meta.image_url || '').trim()
        if (candidateUrl.startsWith('data:image/')) continue
        if (assetId && candidateId && candidateId === assetId && candidateUrl) {
          return toAbsoluteApiUrl(candidateUrl)
        }
      }
      if (assetId) {
        return toAbsoluteApiUrl(`/api/v1/literature/reader/figure-assets/${paperId}/${assetPage}/${assetId}`)
      }
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
  }

  const isDark = themeMode === 'dark'

  return (
    <ConfigProvider theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
      <div
        className="reader-composed-surface reader-workbench reader-workbench--review-page"
        style={{
          '--reader-card-bg': activeStyle.panelBackground,
          '--reader-card-border': activeStyle.borderColor,
          '--reader-text': activeStyle.bodyColor,
          '--reader-heading': activeStyle.headingColor,
          '--reader-muted-text': activeStyle.mutedColor,
          '--reader-workbench-page-bg': activeStyle.pageBackground,
          '--reader-workbench-surface-bg': activeStyle.surfaceBackground,
          '--reader-workbench-rail-bg': activeStyle.railBackground,
          '--reader-workbench-overlay-bg': activeStyle.overlayBackground,
          '--reader-workbench-measure': `${contentMaxWidth}px`,
          '--reader-workbench-body-font': activeStyle.bodyFontFamily,
          '--reader-workbench-heading-font': activeStyle.headingFontFamily,
        } as CSSProperties}
      >
        <div className="reader-workbench__frame">
          <div className="reader-workbench__topbar">
            <div className="reader-workbench__meta">
              <div className="reader-workbench__eyebrow">
                <Tag color="blue">Compose Review</Tag>
                {snapshot ? <Tag color="purple">Revision {snapshot.revision}</Tag> : null}
                {snapshot?.scheme_choice?.scheme_id ? <Tag color="geekblue">{snapshot.scheme_choice.scheme_id}</Tag> : null}
                {snapshot?.status ? <Tag color={snapshot.status === 'done' ? 'green' : 'orange'}>{snapshot.status}</Tag> : null}
              </div>
              <Title level={2} className="reader-workbench__title">
                Review Workbench
              </Title>
              <Text className="reader-workbench__subtitle">
                在正式发布到阅读页之前，这里展示 AI 编排后的真实 React 渲染、意图说明和诊断信息。
              </Text>
            </div>
            <div className="reader-workbench__controls">
              {snapshot?.session_id ? <Text type="secondary">Session {snapshot.session_id.slice(0, 10)}</Text> : null}
              {snapshot?.snapshot_id ? <Text type="secondary">Snapshot {snapshot.snapshot_id}</Text> : null}
            </div>
          </div>

          <div className="reader-workbench__body">
            <div className="reader-workbench__canvas">
              <div className="reader-workbench__surface reader-workbench__surface--scroll">
                {error ? (
                  <Alert
                    type="error"
                    showIcon
                    message="Review snapshot unavailable"
                    description={error}
                    style={{ marginBottom: 16 }}
                  />
                ) : null}

                {loading ? (
                  <div style={{ minHeight: 360, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <Spin />
                  </div>
                ) : null}

                {!loading && snapshot ? (
                  <div className="reader-workbench__content" style={{ maxWidth: contentMaxWidth, margin: '0 auto' }}>
                    {renderReaderComponentTree(snapshot.ui_plan.components, {
                      themeStyle: activeStyle,
                      qualityReport: snapshot.quality_report,
                      resolveFigureImageUrl,
                      readOnly: true,
                    })}
                  </div>
                ) : null}

                {!loading && !snapshot && !error ? (
                  <Empty description="No review snapshot found." />
                ) : null}
              </div>
            </div>

            <div className="reader-workbench__rail">
              <Card size="small" title="Scheme">
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Text strong>{snapshot?.scheme_choice?.label || snapshot?.scheme_choice?.scheme_id || 'Unspecified'}</Text>
                  <Text className="reader-workbench__rail-note">
                    {snapshot?.scheme_choice?.rationale || 'No explicit scheme rationale.'}
                  </Text>
                </Space>
              </Card>

              <Card size="small" title="Decision Log">
                {snapshot?.decision_log?.length ? (
                  <div className="reader-workbench__decision-list">
                    {snapshot.decision_log.map((item, idx) => (
                      <div key={`decision-${idx}`} className="reader-workbench__decision-item">
                        <Text>{item}</Text>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No decision log." />
                )}
              </Card>

              <Card size="small" title="Omissions">
                {snapshot?.omission_decisions?.length ? (
                  <div className="reader-workbench__omission-list">
                    {snapshot.omission_decisions.map((item) => (
                      <div key={item.decision_id} className="reader-workbench__omission-item">
                        <Space size={6} wrap>
                          <Tag color={item.decision === 'hide' ? 'red' : (item.decision === 'collapse' ? 'gold' : 'blue')}>
                            {item.decision}
                          </Tag>
                          {item.recoverable ? <Tag color="green">recoverable</Tag> : null}
                        </Space>
                        <div style={{ marginTop: 6 }}>
                          <Text>{item.reason || 'No reason provided.'}</Text>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No intentional omissions." />
                )}
              </Card>

              <Card size="small" title="Observed Render">
                {snapshot?.render_image_url ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <img
                      src={snapshot.render_image_url}
                      alt="Observed review render"
                      style={{ width: '100%', borderRadius: 10, border: '1px solid rgba(120,145,170,0.18)' }}
                    />
                    {snapshot.observation_note ? <Text className="reader-workbench__rail-note">{snapshot.observation_note}</Text> : null}
                    {snapshot.observation_source ? <Text className="reader-workbench__rail-note">Source: {snapshot.observation_source}</Text> : null}
                  </Space>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No render observation." />
                )}
              </Card>

              <Card size="small" title="Diagnostics">
                {snapshot?.diagnostics?.length ? (
                  <Space direction="vertical" size={10} style={{ width: '100%' }}>
                    {snapshot.diagnostics.map((item) => (
                      <Alert
                        key={item.code}
                        type={item.severity === 'error' ? 'error' : (item.severity === 'warn' ? 'warning' : 'info')}
                        showIcon
                        message={item.code}
                        description={item.message}
                      />
                    ))}
                  </Space>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No diagnostics." />
                )}
              </Card>

              <Card size="small" title="Metadata">
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Text>Paper {snapshot?.paper_id || paperId}</Text>
                  <Text>Page {snapshot?.page || '-'}</Text>
                  <Text>Theme {snapshot?.theme_mode || themeMode}</Text>
                  <Text>Detail {snapshot?.detail_level || '-'}</Text>
                </Space>
              </Card>
            </div>
          </div>
        </div>
      </div>
    </ConfigProvider>
  )
}
