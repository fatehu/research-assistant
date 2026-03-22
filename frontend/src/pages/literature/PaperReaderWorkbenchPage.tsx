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
  ReaderAdjacentPageContext,
  ReaderComposeAsset,
  ReaderComposePayload,
  ReaderComponentNode,
  ReaderEnrichmentTarget,
  ReaderExperienceGuidedBeat,
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

function isSeedExperiencePlan(
  response: { plan?: { meta?: Record<string, unknown> | null } | null; experience_cache_layer?: string | null } | null | undefined,
): boolean {
  const meta = (response?.plan?.meta && typeof response.plan.meta === 'object')
    ? response.plan.meta
    : null
  return Boolean(meta?.seed_plan || response?.experience_cache_layer === 'derived_seed')
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
      description: '当前页还没有可用的 PDF 渲染结果，workbench 暂时无法构建验收视图。',
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

function renderPlanningBrief(brief: Record<string, unknown> | undefined | null) {
  if (!brief || !Object.keys(brief).length) return <Empty description="No planning brief" />
  const recommendedSections = Array.isArray(brief.recommended_sections) ? brief.recommended_sections : []
  const toolHints = Array.isArray(brief.tool_hints) ? brief.tool_hints : []
  const plannerNotes = Array.isArray(brief.planner_notes) ? brief.planner_notes : []
  const toolBudget = (brief.tool_budget && typeof brief.tool_budget === 'object')
    ? brief.tool_budget as Record<string, unknown>
    : {}
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        {String(brief.page_archetype_hint || '').trim() ? <Tag color="blue">{String(brief.page_archetype_hint)}</Tag> : null}
        {String(brief.continuity_mode || '').trim() ? <Tag>{String(brief.continuity_mode)}</Tag> : null}
        {String(brief.primary_focus_label || '').trim() ? <Tag color="geekblue">{String(brief.primary_focus_label)}</Tag> : null}
      </Space>
      {String(brief.summary || brief.reader_goal || '').trim() ? (
        <Paragraph className="reader-workbench-debug__summary">{String(brief.summary || brief.reader_goal)}</Paragraph>
      ) : null}
      {recommendedSections.length ? (
        <div className="reader-workbench-debug__path-strip">
          {recommendedSections.map((item, index) => (
            <div key={`planning-section-${index}`} className="reader-workbench-debug__path-step">
              <span className="reader-workbench-debug__path-step-index">{index + 1}</span>
              <span>{String(item)}</span>
            </div>
          ))}
        </div>
      ) : null}
      {toolHints.length ? (
        <Space wrap>
          {toolHints.map((item, index) => <Tag color="purple" key={`planning-tool-${index}`}>{String(item)}</Tag>)}
        </Space>
      ) : null}
      {Object.keys(toolBudget).length ? (
        <Space wrap>
          <Tag color="magenta">{`工具预算 ${String(toolBudget.max_tool_requests || 0)}`}</Tag>
          <Tag>{`原生 ${String(toolBudget.max_reader_native_requests || 0)}`}</Tag>
          <Tag>{`公网 ${String(toolBudget.max_public_web_requests || 0)}`}</Tag>
          <Tag>{`去重 ${String(toolBudget.duplicate_query_policy || '')}`}</Tag>
        </Space>
      ) : null}
      {plannerNotes.length ? (
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          {plannerNotes.map((item, index) => (
            <Paragraph key={`planning-note-${index}`} className="reader-workbench-debug__summary">{String(item)}</Paragraph>
          ))}
        </Space>
      ) : null}
    </Space>
  )
}

function renderPlannerOutput(payload: Record<string, unknown> | undefined | null) {
  if (!payload || !Object.keys(payload).length) return <Empty description="No planner output" />
  const toolRequests = Array.isArray(payload.tool_requests) ? payload.tool_requests as Array<Record<string, unknown>> : []
  const toolBudget = (payload.tool_budget && typeof payload.tool_budget === 'object')
    ? payload.tool_budget as Record<string, unknown>
    : {}
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        {String(payload.page_objective || '').trim() ? <Tag color="blue">{String(payload.page_objective)}</Tag> : null}
        {String(payload.widget_focus || '').trim() ? <Tag color="geekblue">{String(payload.widget_focus)}</Tag> : null}
      </Space>
      {String(payload.narrative_strategy || '').trim() ? (
        <Paragraph className="reader-workbench-debug__summary">{String(payload.narrative_strategy)}</Paragraph>
      ) : null}
      {Array.isArray(payload.section_strategy) && payload.section_strategy.length ? (
        <div className="reader-workbench-debug__path-strip">
          {payload.section_strategy.map((item, index) => (
            <div key={`planner-output-section-${index}`} className="reader-workbench-debug__path-step">
              <span className="reader-workbench-debug__path-step-index">{index + 1}</span>
              <span>{String(item)}</span>
            </div>
          ))}
        </div>
      ) : null}
      {toolRequests.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {toolRequests.map((item, index) => (
            <Card key={`planner-output-tool-${index}`} size="small">
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space wrap>
                  {String(item.beat_id || '').trim() ? <Tag color="cyan">{String(item.beat_id)}</Tag> : null}
                  {String(item.tool || '').trim() ? <Tag color="purple">{String(item.tool)}</Tag> : null}
                  {String(item.priority || '').trim() ? <Tag>{String(item.priority)}</Tag> : null}
                </Space>
                {String(item.reason || '').trim() ? <Paragraph className="reader-workbench-debug__summary">{String(item.reason)}</Paragraph> : null}
                {item.arguments && typeof item.arguments === 'object' ? (
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(item.arguments, null, 2)}</pre>
                ) : null}
              </Space>
            </Card>
          ))}
        </Space>
      ) : null}
      {Object.keys(toolBudget).length ? (
        <Space wrap>
          <Tag color="magenta">{`max ${String(toolBudget.max_tool_requests || 0)}`}</Tag>
          <Tag>{`reader ${String(toolBudget.max_reader_native_requests || 0)}`}</Tag>
          <Tag>{`web ${String(toolBudget.max_public_web_requests || 0)}`}</Tag>
        </Space>
      ) : null}
    </Space>
  )
}

function renderToolEnrichmentPacket(packet: Record<string, unknown> | undefined | null) {
  if (!packet || !Object.keys(packet).length) return <Empty description="No tool enrichment packet" />
  const toolFindings = Array.isArray(packet.tool_findings) ? packet.tool_findings as Array<Record<string, unknown>> : []
  const publicLinks = Array.isArray(packet.public_links) ? packet.public_links as Array<Record<string, unknown>> : []
  const beatPackets = Array.isArray(packet.beat_packets) ? packet.beat_packets as Array<Record<string, unknown>> : []
  const budgetSummary = (packet.budget_summary && typeof packet.budget_summary === 'object')
    ? packet.budget_summary as Record<string, unknown>
    : {}
  const budgetEvents = Array.isArray(packet.budget_events) ? packet.budget_events as Array<Record<string, unknown>> : []
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {Array.isArray(packet.executed_tools) && packet.executed_tools.length ? (
        <Space wrap>
          {packet.executed_tools.map((item, index) => <Tag color="purple" key={`tool-enrichment-executed-${index}`}>{String(item)}</Tag>)}
        </Space>
      ) : null}
      {Object.keys(budgetSummary).length ? (
        <Space wrap>
          <Tag color="magenta">{`执行 ${String(budgetSummary.executed_tool_count || 0)}`}</Tag>
          <Tag>{`抑制 ${String(budgetSummary.suppressed_request_count || 0)}`}</Tag>
          <Tag>{`超时 ${String(budgetSummary.timeout_count || 0)}`}</Tag>
        </Space>
      ) : null}
      {toolFindings.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {toolFindings.map((item, index) => (
            <Card key={`tool-enrichment-finding-${index}`} size="small">
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space wrap>
                  {String(item.beat_id || '').trim() ? <Tag color="cyan">{String(item.beat_id)}</Tag> : null}
                  {String(item.tool || '').trim() ? <Tag color="geekblue">{String(item.tool)}</Tag> : null}
                  {'success' in item ? <Tag color={item.success ? 'green' : 'red'}>{item.success ? 'success' : 'failed'}</Tag> : null}
                </Space>
                {String(item.output_excerpt || item.error || '').trim() ? (
                  <Paragraph className="reader-workbench-debug__summary">{String(item.output_excerpt || item.error)}</Paragraph>
                ) : null}
              </Space>
            </Card>
          ))}
        </Space>
      ) : null}
      {beatPackets.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {beatPackets.map((item, index) => {
            const requestedTools = Array.isArray(item.requested_tools) ? item.requested_tools as Array<Record<string, unknown>> : []
            const findings = Array.isArray(item.tool_findings) ? item.tool_findings as Array<Record<string, unknown>> : []
            const objectives = Array.isArray(item.tool_objectives) ? item.tool_objectives as Array<unknown> : []
            return (
              <Card key={`tool-enrichment-beat-${index}`} size="small">
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  <Space wrap>
                    {String(item.beat_id || '').trim() ? <Tag color="cyan">{String(item.beat_id)}</Tag> : null}
                    {String(item.title || '').trim() ? <Tag>{String(item.title)}</Tag> : null}
                    {requestedTools.length ? <Tag color="purple">{`requests ${requestedTools.length}`}</Tag> : null}
                    {findings.length ? <Tag color="geekblue">{`findings ${findings.length}`}</Tag> : null}
                  </Space>
                  {String(item.reader_goal || '').trim() ? <Paragraph className="reader-workbench-debug__summary">{String(item.reader_goal)}</Paragraph> : null}
                  {objectives.length ? (
                    <Space wrap>
                      {objectives.map((objective, objectiveIndex) => (
                        <Tag key={`tool-enrichment-beat-objective-${index}-${objectiveIndex}`} color="blue">
                          {String(objective)}
                        </Tag>
                      ))}
                    </Space>
                  ) : null}
                </Space>
              </Card>
            )
          })}
        </Space>
      ) : null}
      {budgetEvents.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {budgetEvents.slice(0, 4).map((item, index) => (
            <Card key={`tool-enrichment-budget-${index}`} size="small">
              <Space wrap>
                <Tag color="orange">{String(item.type || 'budget')}</Tag>
                {String(item.tool || '').trim() ? <Tag>{String(item.tool)}</Tag> : null}
                {String(item.reason || '').trim() ? <Tag color="red">{String(item.reason)}</Tag> : null}
              </Space>
            </Card>
          ))}
        </Space>
      ) : null}
      {publicLinks.length ? (
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          {publicLinks.map((item, index) => (
            <Card key={`tool-enrichment-link-${index}`} size="small">
              <Space direction="vertical" size={4} style={{ width: '100%' }}>
                <Text strong>{String(item.label || item.href || `link-${index + 1}`)}</Text>
                {String(item.href || '').trim() ? <Text type="secondary">{String(item.href)}</Text> : null}
              </Space>
            </Card>
          ))}
        </Space>
      ) : null}
    </Space>
  )
}

function renderRuntimeStages(rows: Array<Record<string, unknown>> | undefined | null) {
  if (!rows?.length) return <Empty description="No runtime stages" />
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {rows.map((row, index) => {
        const meta = (row.meta && typeof row.meta === 'object') ? row.meta as Record<string, unknown> : {}
        const stageId = String(row.stage_id || '').trim()
        const stageKind = String(row.stage_kind || '').trim()
        const status = String(row.status || '').trim()
        const summary = String(row.summary || '').trim()
        return (
          <Card key={`runtime-stage-${index}`} size="small">
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space wrap>
                {stageId ? <Tag color="blue">{stageId}</Tag> : null}
                {stageKind ? <Tag>{stageKind}</Tag> : null}
                {status ? <Tag color={status.includes('done') || status === 'ready' ? 'green' : status.includes('fallback') || status.includes('timeout') ? 'orange' : 'default'}>{status}</Tag> : null}
              </Space>
              {summary ? <Paragraph className="reader-workbench-debug__summary">{summary}</Paragraph> : null}
              {Object.keys(meta).length ? (
                <Space wrap>
                  {Object.entries(meta).slice(0, 6).map(([key, value]) => (
                    <Tag key={`${stageId}-${key}`}>{`${key}:${Array.isArray(value) ? value.length : String(value)}`}</Tag>
                  ))}
                </Space>
              ) : null}
            </Space>
          </Card>
        )
      })}
    </Space>
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

function renderAdjacentPageContext(items: ReaderAdjacentPageContext[] | undefined | null) {
  if (!items?.length) return <Empty description="No adjacent-page context" />
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {items.map((item) => (
        <Card key={`${item.relation}-${item.page}`} size="small">
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color="blue">{item.relation === 'previous_page' ? '上一页' : '下一页'}</Tag>
              <Tag>第 {item.page} 页</Tag>
              {item.source ? <Tag color="geekblue">{item.source}</Tag> : null}
            </Space>
            {item.summary ? <Text strong>{item.summary}</Text> : null}
            {item.body_text ? <Paragraph style={{ marginBottom: 0 }}>{item.body_text}</Paragraph> : null}
            {item.continuation_hints?.length ? (
              <div>
                <Text type="secondary">承接提示</Text>
                <div className="reader-experience-page__chip-cloud">
                  {item.continuation_hints.map((hint, index) => <Tag key={`${item.page}-hint-${index}`}>{hint}</Tag>)}
                </div>
              </div>
            ) : null}
            {item.figures?.length ? (
              <div>
                <Text type="secondary">图片描述</Text>
                <List
                  size="small"
                  dataSource={item.figures}
                  renderItem={(row) => <List.Item>{`${row.label || 'Figure'}: ${row.description}`}</List.Item>}
                />
              </div>
            ) : null}
            {item.tables?.length ? (
              <div>
                <Text type="secondary">表格描述</Text>
                <List
                  size="small"
                  dataSource={item.tables}
                  renderItem={(row) => <List.Item>{`${row.label || 'Table'}: ${row.description}`}</List.Item>}
                />
              </div>
            ) : null}
            {item.equations?.length ? (
              <div>
                <Text type="secondary">公式描述</Text>
                <List
                  size="small"
                  dataSource={item.equations}
                  renderItem={(row) => <List.Item>{`${row.label || 'Equation'}: ${row.description}`}</List.Item>}
                />
              </div>
            ) : null}
          </Space>
        </Card>
      ))}
    </Space>
  )
}

function renderPageBriefSummary(pageBrief: ReaderPageBrief | null | undefined) {
  if (!pageBrief) return <Empty description="No page brief" />
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        {String(pageBrief.page_archetype || '').trim() ? <Tag color="blue">{String(pageBrief.page_archetype)}</Tag> : null}
        {String(pageBrief.reader_type || '').trim() ? <Tag>{String(pageBrief.reader_type)}</Tag> : null}
        {String(pageBrief.primary_focus_target_id || '').trim() ? <Tag color="geekblue">{String(pageBrief.primary_focus_target_id)}</Tag> : null}
      </Space>
      {String(pageBrief.page_goal || '').trim() ? (
        <Paragraph className="reader-workbench-debug__summary">{String(pageBrief.page_goal)}</Paragraph>
      ) : null}
      {Array.isArray(pageBrief.experience_hooks) && pageBrief.experience_hooks.length ? (
        <div className="reader-workbench-debug__path-strip">
          {pageBrief.experience_hooks.slice(0, 4).map((hook, index) => (
            <div key={`page-brief-hook-${index}`} className="reader-workbench-debug__path-step">
              <span className="reader-workbench-debug__path-step-index">{index + 1}</span>
              <span>{String(hook)}</span>
            </div>
          ))}
        </div>
      ) : null}
      <details className="reader-workbench-debug__details">
        <summary>查看原始 page_brief JSON</summary>
        <div className="reader-workbench-debug__details-body">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(pageBrief, null, 2)}</pre>
        </div>
      </details>
    </Space>
  )
}

function renderPageDossierSummary(pageDossier: Record<string, unknown>) {
  if (!Object.keys(pageDossier || {}).length) return <Empty description="No page dossier" />
  const currentPage = (pageDossier.current_page && typeof pageDossier.current_page === 'object')
    ? pageDossier.current_page as Record<string, unknown>
    : {}
  const targets = Array.isArray(currentPage.targets) ? currentPage.targets : []
  const assets = Array.isArray(currentPage.assets) ? currentPage.assets : []
  const adjacent = Array.isArray(pageDossier.adjacent_page_context) ? pageDossier.adjacent_page_context as Array<Record<string, unknown>> : []
  const quality = (currentPage.quality && typeof currentPage.quality === 'object')
    ? currentPage.quality as Record<string, unknown>
    : {}
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        {currentPage.page ? <Tag>{`page ${currentPage.page}`}</Tag> : null}
        {currentPage.build_mode ? <Tag color="blue">{String(currentPage.build_mode)}</Tag> : null}
        {currentPage.pipeline_version ? <Tag color="geekblue">{String(currentPage.pipeline_version)}</Tag> : null}
        {currentPage.status ? <Tag color="cyan">{String(currentPage.status)}</Tag> : null}
        {quality.stop_reason ? <Tag>{`stop ${String(quality.stop_reason)}`}</Tag> : null}
      </Space>
      <Space wrap>
        <Tag>{`targets ${targets.length}`}</Tag>
        <Tag>{`assets ${assets.length}`}</Tag>
        <Tag>{`adjacent ${adjacent.length}`}</Tag>
      </Space>
      {String(currentPage.degraded_reason || '').trim() ? (
        <Paragraph style={{ marginBottom: 0 }}>{String(currentPage.degraded_reason || '').trim()}</Paragraph>
      ) : null}
      <details className="reader-workbench-debug__details">
        <summary>查看原始 dossier JSON</summary>
        <div className="reader-workbench-debug__details-body">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(pageDossier, null, 2)}</pre>
        </div>
      </details>
    </Space>
  )
}

function renderToolTraceRows(rows: Array<Record<string, unknown>> | undefined | null) {
  if (!rows?.length) return <Empty description="No tool trace" />
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {rows.map((row, index) => {
        const data = (row.data && typeof row.data === 'object') ? row.data as Record<string, unknown> : {}
        const toolName = String(data.tool || row.tool || '').trim()
        const traceType = String(row.type || '').trim()
        const success = typeof data.success === 'boolean' ? data.success : undefined
        const input = (data.input && typeof data.input === 'object') ? data.input as Record<string, unknown> : {}
        const query = String(input.query || input.q || input.url || input.href || '').trim()
        const outputText = String(data.output_excerpt || data.output || '').trim()
        return (
          <Card key={`tool-trace-${index}`} size="small">
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space wrap>
                {traceType ? <Tag>{traceType}</Tag> : null}
                {toolName ? <Tag color="geekblue">{toolName}</Tag> : null}
                {typeof success === 'boolean' ? <Tag color={success ? 'green' : 'red'}>{success ? 'success' : 'failed'}</Tag> : null}
              </Space>
              {query ? <Text strong>{query}</Text> : null}
              {outputText ? <Paragraph style={{ marginBottom: 0 }}>{outputText}</Paragraph> : null}
            </Space>
          </Card>
        )
      })}
    </Space>
  )
}

function renderContractValidation(payload: Record<string, unknown> | undefined | null) {
  if (!payload || !Object.keys(payload).length) return <Empty description="No contract validation" />
  const status = String(payload.status || '').trim()
  const contract = String(payload.contract || '').trim()
  const errorCount = Number(payload.error_count || 0)
  const errorsPreview = Array.isArray(payload.errors_preview)
    ? payload.errors_preview.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  const isFallback = status === 'fallback'
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        {status ? <Tag color={isFallback ? 'orange' : 'green'}>{status}</Tag> : null}
        {contract ? <Tag>{contract}</Tag> : null}
        {Number.isFinite(errorCount) && errorCount > 0 ? <Tag color="red">{`errors ${errorCount}`}</Tag> : null}
      </Space>
      {isFallback ? (
        <Alert
          type="warning"
          showIcon
          message="当前 generative/experience plan 走了 contract fallback"
          description="这通常意味着 richer plan 被回退成更保守结果，页面质量和 workbench inspectability 都会被削弱。"
        />
      ) : null}
      {errorsPreview.length ? (
        <List
          size="small"
          dataSource={errorsPreview}
          renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
        />
      ) : null}
    </Space>
  )
}

function renderComposeCritic(payload: ReaderComposePayload | null | undefined) {
  if (!payload) return <Empty description="No compose critic snapshot" />
  const quality = payload.quality_report
  const validation = payload.validation_report
  const validationErrors = Array.isArray(quality.validation_errors)
    ? quality.validation_errors.map((item) => String(item || '').trim()).filter(Boolean)
    : []
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space wrap>
        <Tag color={quality.hard_constraints_passed ? 'green' : 'orange'}>{quality.hard_constraints_passed ? 'hard constraints passed' : 'hard constraints failed'}</Tag>
        <Tag>{`overall ${Number(quality.overall || 0).toFixed(3)}`}</Tag>
        <Tag>{`target ${Number(quality.quality_target || 0).toFixed(3)}`}</Tag>
        <Tag>{`structure ${Number(quality.structure_fidelity || 0).toFixed(3)}`}</Tag>
        <Tag>{`evidence ${Number(quality.evidence_alignment || 0).toFixed(3)}`}</Tag>
        <Tag>{`readability ${Number(quality.readability || 0).toFixed(3)}`}</Tag>
        <Tag>{`iterations ${Number(quality.iterations || 0)}`}</Tag>
        <Tag color={validation?.passed ? 'green' : 'red'}>{validation?.passed ? 'validation passed' : 'validation failed'}</Tag>
      </Space>
      {quality.stop_reason ? (
        <Paragraph className="reader-workbench-debug__summary">{`stop reason: ${quality.stop_reason}`}</Paragraph>
      ) : null}
      {validationErrors.length ? (
        <List
          size="small"
          dataSource={validationErrors.slice(0, 6)}
          renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
        />
      ) : null}
      <details className="reader-workbench-debug__details">
        <summary>查看 quality / validation JSON</summary>
        <div className="reader-workbench-debug__details-body">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify({ quality, validation }, null, 2)}</pre>
        </div>
      </details>
    </Space>
  )
}

function renderGuidedBeats(
  beats: ReaderExperienceGuidedBeat[] | undefined | null,
  plannerOutput?: Record<string, unknown> | null,
  toolEnrichmentPacket?: Record<string, unknown> | null,
) {
  if (!beats?.length) return <Empty description="No guided beats" />
  const beatPackets = Array.isArray(toolEnrichmentPacket?.beat_packets)
    ? toolEnrichmentPacket?.beat_packets as Array<Record<string, unknown>>
    : []
  const beatPacketLookup = new Map(
    beatPackets
      .map((item) => [String(item.beat_id || '').trim(), item] as const)
      .filter(([beatId]) => Boolean(beatId)),
  )
  const plannerToolRequests = Array.isArray(plannerOutput?.tool_requests)
    ? plannerOutput.tool_requests as Array<Record<string, unknown>>
    : []
  const packetFindings = Array.isArray(toolEnrichmentPacket?.tool_findings)
    ? toolEnrichmentPacket.tool_findings as Array<Record<string, unknown>>
    : []
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {beats.map((beat, index) => {
        const beatId = String(beat.beat_id || '').trim()
        const beatPacket = beatPacketLookup.get(beatId)
        const plannerRequests = plannerToolRequests.filter((item) => String(item.beat_id || '').trim() === beatId)
        const requestedTools = Array.isArray(beatPacket?.requested_tools)
          ? beatPacket?.requested_tools as Array<Record<string, unknown>>
          : plannerRequests
        const findings = Array.isArray(beatPacket?.tool_findings)
          ? beatPacket?.tool_findings as Array<Record<string, unknown>>
          : packetFindings.filter((item) => String(item.beat_id || '').trim() === beatId)
        const publicLinks = Array.isArray(beatPacket?.public_links)
          ? beatPacket?.public_links as Array<Record<string, unknown>>
          : []
        const supportingPoints = Array.isArray(beatPacket?.supporting_points)
          ? beatPacket.supporting_points.map((item) => String(item || '').trim()).filter(Boolean)
          : []
        const readerFacingNotes = Array.isArray(beatPacket?.reader_facing_notes)
          ? beatPacket.reader_facing_notes.map((item) => String(item || '').trim()).filter(Boolean)
          : []
        const beatSummary = String(beatPacket?.summary || '').trim()
        const hasChainDetails = requestedTools.length > 0 || findings.length > 0 || publicLinks.length > 0
        const chainSummary = `${requestedTools.length} request${requestedTools.length === 1 ? '' : 's'} → ${findings.length} finding${findings.length === 1 ? '' : 's'} → ${publicLinks.length} link${publicLinks.length === 1 ? '' : 's'}`
        const visibleTargets = (beat.target_ids || []).slice(0, 8)
        const hiddenTargetCount = Math.max(0, (beat.target_ids?.length || 0) - visibleTargets.length)
        return (
          <Card key={beatId || `guided-beat-${index}`} size="small">
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space wrap>
                <Tag color="blue">{beat.beat_type || 'guided_beat'}</Tag>
                {beat.section_type_hint ? <Tag>{beat.section_type_hint}</Tag> : null}
                {Number.isFinite(beat.importance) ? <Tag>{`priority ${beat.importance}`}</Tag> : null}
                {beat.target_ids?.length ? <Tag color="geekblue">{`targets ${beat.target_ids.length}`}</Tag> : null}
                {beat.block_stack?.length ? <Tag color="purple">{`blocks ${beat.block_stack.length}`}</Tag> : null}
                {findings.length ? <Tag color="green">{`findings ${findings.length}`}</Tag> : null}
                {publicLinks.length ? <Tag color="gold">{`links ${publicLinks.length}`}</Tag> : null}
              </Space>
              {preferDisplayCopy(beat.display_title, beat.title) ? <Text strong>{preferDisplayCopy(beat.display_title, beat.title)}</Text> : null}
              {preferDisplayCopy(beat.display_summary, beat.summary) ? <Paragraph style={{ marginBottom: 0 }}>{preferDisplayCopy(beat.display_summary, beat.summary)}</Paragraph> : null}
              {beat.reader_goal ? <Paragraph style={{ marginBottom: 0 }}>{beat.reader_goal}</Paragraph> : null}
              {beat.continuity_note ? <Text type="secondary">{beat.continuity_note}</Text> : null}
              {beat.tool_objectives?.length ? (
                <Space wrap size={[6, 6]}>
                  {beat.tool_objectives.map((objective) => (
                    <Tag key={`${beatId}-${objective}`} color="cyan">
                      {objective}
                    </Tag>
                  ))}
                </Space>
              ) : null}
              {beat.target_ids?.length ? (
                <div className="reader-workbench-debug__meta-row">
                  <Text type="secondary">Coverage</Text>
                  <div className="reader-experience-page__chip-cloud">
                    {visibleTargets.map((targetId) => <Tag key={`${beatId}-${targetId}`}>{targetId}</Tag>)}
                    {hiddenTargetCount > 0 ? <Tag>{`+${hiddenTargetCount} more`}</Tag> : null}
                  </div>
                </div>
              ) : null}
              {beat.block_stack?.length ? (
                <div className="reader-workbench-debug__meta-row">
                  <Text type="secondary">Planned blocks</Text>
                  <div className="reader-experience-page__chip-cloud">
                    {beat.block_stack.map((block, blockIndex) => {
                      const blockType = String(block?.block_type || '').trim()
                      const refId = String(block?.ref_id || '').trim()
                      const label = blockType || refId || `block-${blockIndex + 1}`
                      return (
                        <Tag color="purple" key={`${beatId}-block-${blockIndex}-${label}`}>
                          {label}
                        </Tag>
                      )
                    })}
                  </div>
                </div>
              ) : null}
              {beatSummary ? <Paragraph className="reader-workbench-debug__summary">{beatSummary}</Paragraph> : null}
              {supportingPoints.length ? (
                <List
                  size="small"
                  dataSource={supportingPoints}
                  renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
                />
              ) : null}
              {readerFacingNotes.length ? (
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  {readerFacingNotes.map((item, noteIndex) => (
                    <Text type="secondary" key={`${beatId}-reader-note-${noteIndex}`}>{item}</Text>
                  ))}
                </Space>
              ) : null}
              <div className="reader-workbench-debug__meta-row">
                <Text type="secondary">Tool chain</Text>
                <Space wrap>
                  <Tag color={hasChainDetails ? 'green' : 'default'}>{chainSummary}</Tag>
                  {plannerRequests.length ? <Tag color="purple">{`planner ${plannerRequests.length}`}</Tag> : null}
                  {beat.drop_notes?.length ? <Tag color="orange">{`drops ${beat.drop_notes.length}`}</Tag> : null}
                </Space>
              </div>
              {beat.drop_notes?.length ? (
                <details className="reader-workbench-debug__details">
                  <summary>查看 drop rationale</summary>
                  <div className="reader-workbench-debug__details-body">
                    <List
                      size="small"
                      dataSource={beat.drop_notes}
                      renderItem={(item) => <List.Item style={{ paddingInline: 0 }}>{item}</List.Item>}
                    />
                  </div>
                </details>
              ) : null}
              {hasChainDetails ? (
                <details className="reader-workbench-debug__details">
                  <summary>查看 tool chain</summary>
                  <div className="reader-workbench-debug__details-body">
                    <Space direction="vertical" size={10} style={{ width: '100%' }}>
                      {requestedTools.length ? (
                        <div>
                          <Text type="secondary">Tool requests</Text>
                          <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 6 }}>
                            {requestedTools.map((item, requestIndex) => (
                              <Card key={`${beatId}-request-${requestIndex}`} size="small">
                                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                  <Space wrap>
                                    {String(item.tool || '').trim() ? <Tag color="purple">{String(item.tool)}</Tag> : null}
                                    {String(item.priority || '').trim() ? <Tag>{String(item.priority)}</Tag> : null}
                                  </Space>
                                  {String(item.reason || '').trim() ? <Paragraph className="reader-workbench-debug__summary">{String(item.reason)}</Paragraph> : null}
                                  {item.arguments && typeof item.arguments === 'object' ? (
                                    <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(item.arguments, null, 2)}</pre>
                                  ) : null}
                                </Space>
                              </Card>
                            ))}
                          </Space>
                        </div>
                      ) : null}
                      {findings.length ? (
                        <div>
                          <Text type="secondary">Tool findings</Text>
                          <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 6 }}>
                            {findings.map((item, findingIndex) => (
                              <Card key={`${beatId}-finding-${findingIndex}`} size="small">
                                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                  <Space wrap>
                                    {String(item.tool || '').trim() ? <Tag color="geekblue">{String(item.tool)}</Tag> : null}
                                    {'success' in item ? <Tag color={item.success ? 'green' : 'red'}>{item.success ? 'success' : 'failed'}</Tag> : null}
                                  </Space>
                                  {String(item.output_excerpt || item.error || '').trim() ? (
                                    <Paragraph className="reader-workbench-debug__summary">{String(item.output_excerpt || item.error)}</Paragraph>
                                  ) : null}
                                </Space>
                              </Card>
                            ))}
                          </Space>
                        </div>
                      ) : null}
                      {publicLinks.length ? (
                        <div>
                          <Text type="secondary">Public links</Text>
                          <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 6 }}>
                            {publicLinks.map((item, linkIndex) => (
                              <Card key={`${beatId}-link-${linkIndex}`} size="small">
                                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                                  <Text strong>{String(item.label || item.href || `link-${linkIndex + 1}`)}</Text>
                                  {String(item.href || '').trim() ? <Text type="secondary">{String(item.href)}</Text> : null}
                                </Space>
                              </Card>
                            ))}
                          </Space>
                        </div>
                      ) : null}
                    </Space>
                  </div>
                </details>
              ) : (
                <details className="reader-workbench-debug__details">
                  <summary>查看 coverage / rationale</summary>
                  <div className="reader-workbench-debug__details-body">
                    <Space direction="vertical" size={10} style={{ width: '100%' }}>
                      {plannerRequests.length ? (
                        <div>
                          <Text type="secondary">Planner requests</Text>
                          <Space direction="vertical" size={8} style={{ width: '100%', marginTop: 6 }}>
                            {plannerRequests.map((item, requestIndex) => (
                              <Card key={`${beatId}-planner-request-${requestIndex}`} size="small">
                                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                  <Space wrap>
                                    {String(item.tool || '').trim() ? <Tag color="purple">{String(item.tool)}</Tag> : null}
                                    {String(item.priority || '').trim() ? <Tag>{String(item.priority)}</Tag> : null}
                                  </Space>
                                  {String(item.reason || '').trim() ? <Paragraph className="reader-workbench-debug__summary">{String(item.reason)}</Paragraph> : null}
                                  {item.arguments && typeof item.arguments === 'object' ? (
                                    <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(item.arguments, null, 2)}</pre>
                                  ) : null}
                                </Space>
                              </Card>
                            ))}
                          </Space>
                        </div>
                      ) : null}
                    </Space>
                  </div>
                </details>
              )}
            </Space>
          </Card>
        )
      })}
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
    generativePlanResponse,
    composeError,
    planError,
    composeLoading,
    planLoading,
    backgroundRefreshing,
    surfaceLoadState,
    cacheState,
  } = useReaderSurfaceLoader({
    mode: 'workbench',
    paperId: numericPaperId,
    page,
    selectedKbId,
    userIntent,
    readerProfile,
    reloadNonce: reloadState.nonce,
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
  const generativePlan = generativePlanResponse?.plan || experienceResponse?.generative_plan || null
  const adjacentPageContext = generativePlanResponse?.adjacent_page_context || experienceResponse?.adjacent_page_context || []
  const pageDossier = (
    (generativePlanResponse?.page_dossier && typeof generativePlanResponse.page_dossier === 'object')
      ? generativePlanResponse.page_dossier
      : ((experienceResponse?.page_dossier && typeof experienceResponse.page_dossier === 'object')
        ? experienceResponse.page_dossier
        : {})
  ) as Record<string, unknown>
  const planningBrief = (generativePlan?.meta?.planning_brief && typeof generativePlan.meta.planning_brief === 'object')
    ? generativePlan.meta.planning_brief as Record<string, unknown>
    : ((experiencePlan?.meta?.planning_brief && typeof experiencePlan.meta.planning_brief === 'object')
      ? experiencePlan.meta.planning_brief as Record<string, unknown>
      : {})
  const plannerOutput = (generativePlan?.meta?.planner_output && typeof generativePlan.meta.planner_output === 'object')
    ? generativePlan.meta.planner_output as Record<string, unknown>
    : ((experiencePlan?.meta?.planner_output && typeof experiencePlan.meta.planner_output === 'object')
      ? experiencePlan.meta.planner_output as Record<string, unknown>
      : {})
  const toolEnrichmentPacket = (generativePlan?.meta?.tool_enrichment_packet && typeof generativePlan.meta.tool_enrichment_packet === 'object')
    ? generativePlan.meta.tool_enrichment_packet as Record<string, unknown>
    : ((experiencePlan?.meta?.tool_enrichment_packet && typeof experiencePlan.meta.tool_enrichment_packet === 'object')
      ? experiencePlan.meta.tool_enrichment_packet as Record<string, unknown>
      : {})
  const runtimeStageTrace = Array.isArray(generativePlan?.meta?.runtime_stage_trace)
    ? generativePlan.meta.runtime_stage_trace as Array<Record<string, unknown>>
    : (Array.isArray(experiencePlan?.meta?.runtime_stage_trace)
      ? experiencePlan.meta.runtime_stage_trace as Array<Record<string, unknown>>
      : [])
  const toolTraceRows = Array.isArray(generativePlan?.tool_trace)
    ? generativePlan.tool_trace.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    : ((Array.isArray(experiencePlan?.meta?.tool_trace)
      ? experiencePlan.meta.tool_trace
      : []
    ).filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')))
  const layoutVariant = normalizeExperienceLayoutVariant(
    experiencePlan?.layout_variant || String(experiencePlan?.meta?.layout_variant || ''),
  )
  const enrichmentTargets = experienceResponse?.enrichment_bundle?.targets || effectiveComposePayload?.enrichment_bundle?.targets || []
  const storySubstrate = generativePlan?.story_substrate || null
  const pageBrief: ReaderPageBrief | null = generativePlan?.page_brief || null
  const mainSections = useMemo(() => experiencePlan?.main_sections || [], [experiencePlan?.main_sections])
  const guidedBeats = useMemo(
    () => (
      experiencePlan?.guided_beats?.length
        ? experiencePlan.guided_beats
        : (Array.isArray(plannerOutput.guided_beats) ? plannerOutput.guided_beats as ReaderExperienceGuidedBeat[] : [])
    ),
    [experiencePlan?.guided_beats, plannerOutput.guided_beats],
  )
  const experienceMeta = useMemo<Record<string, unknown>>(
    () => ((experiencePlan?.meta && typeof experiencePlan.meta === 'object') ? experiencePlan.meta : {}) as Record<string, unknown>,
    [experiencePlan?.meta],
  )
  const generativeMeta = useMemo<Record<string, unknown>>(
    () => ((generativePlan?.meta && typeof generativePlan.meta === 'object') ? generativePlan.meta : {}) as Record<string, unknown>,
    [generativePlan?.meta],
  )
  const contractValidation = useMemo<Record<string, unknown>>(() => {
    const fromGenerative = (generativeMeta.contract_validation && typeof generativeMeta.contract_validation === 'object')
      ? generativeMeta.contract_validation as Record<string, unknown>
      : {}
    if (Object.keys(fromGenerative).length) return fromGenerative
    const fromExperience = (experienceMeta.contract_validation && typeof experienceMeta.contract_validation === 'object')
      ? experienceMeta.contract_validation as Record<string, unknown>
      : {}
    return fromExperience
  }, [experienceMeta.contract_validation, generativeMeta.contract_validation])
  const contractValidationStatus = String(
    ((contractValidation.status as string | undefined) || ''),
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
    experiencePlan?.page_story_subtitle || 'workbench 用于验收最终讲读稿与中间产物，不作为第二个体验页。',
  )
  const topSummary = pageBrief?.page_goal || generativePlan?.rationale?.[0] || '围绕正文补充公开资源，并生成最终讲读稿。'
  const composeMetaSummary = [
    experienceResponse?.compose_build_mode ? `compose ${experienceResponse.compose_build_mode}` : (generativePlanResponse?.compose_build_mode ? `compose ${generativePlanResponse.compose_build_mode}` : ''),
    experienceResponse?.cache_layer ? `compose-cache ${experienceResponse.cache_layer}` : (generativePlanResponse?.cache_layer ? `compose-cache ${generativePlanResponse.cache_layer}` : ''),
    experienceResponse?.generative_plan_cache_layer ? `plan-cache ${experienceResponse.generative_plan_cache_layer}` : (generativePlanResponse?.plan_cache_layer ? `plan-cache ${generativePlanResponse.plan_cache_layer}` : ''),
    experienceResponse?.experience_cache_layer ? `experience-cache ${experienceResponse.experience_cache_layer}` : '',
  ].filter(Boolean).join(' · ')
  const isSeedExperience = isSeedExperiencePlan(experienceResponse)
  const hasFinalManuscript = Boolean(
    Array.isArray(experiencePlan?.teaching_manuscript?.segments)
    && experiencePlan.teaching_manuscript.segments.length > 0,
  ) && !isSeedExperience

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
    if (experienceResponse?.experience_cache_hit || generativePlanResponse?.plan_cache_hit) return '已命中缓存'
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
    hasPlan: Boolean(hasNonDraftExperiencePlan(experienceResponse) || ['done', 'fallback'].includes(String(generativePlan?.status || '').trim())),
    composeLoading,
    planLoading,
    backgroundRefreshing,
  })
  const acceptanceChecks = [
    { key: 'dossier', label: 'dossier', ready: Object.keys(pageDossier).length > 0 },
    { key: 'draft', label: 'draft', ready: Object.keys(planningBrief).length > 0 || Object.keys(plannerOutput).length > 0 || Boolean(generativePlan) },
    { key: 'critic', label: 'critic', ready: Object.keys(contractValidation).length > 0 || Boolean(effectiveComposePayload?.quality_report) },
    {
      key: 'provenance',
      label: 'provenance',
      ready: runtimeStageTrace.length > 0 || toolTraceRows.length > 0 || Object.keys(toolEnrichmentPacket).length > 0 || adjacentPageContext.length > 0,
    },
    {
      key: 'cache',
      label: 'cache state',
      ready: cacheState.composeLayer !== 'none' || cacheState.planLayer !== 'none' || cacheState.experienceLayer !== 'none',
    },
    { key: 'final', label: 'final manuscript', ready: hasFinalManuscript },
  ]

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
            {(experienceResponse?.generative_plan_cache_hit || generativePlanResponse?.plan_cache_hit) ? <Tag color="blue">plan cached</Tag> : null}
            {contractValidationStatus ? <Tag>{`contract ${contractValidationStatus}`}</Tag> : null}
          </Space>
          {composeMetaSummary ? <Text className="reader-workbench-debug__hero-meta">{composeMetaSummary}</Text> : null}
        </div>
        <Space wrap className="reader-workbench-debug__hero-actions">
          <Button
            onClick={() => setReloadState((prev) => ({ nonce: prev.nonce + 1, forceFresh: false }))}
            loading={composeLoading || planLoading || backgroundRefreshing}
          >
            刷新验收面
          </Button>
          <Link to={`/literature/${numericPaperId}/experience-v2?page=${page}${selectedKbId > 0 ? `&kb=${selectedKbId}` : ''}${readerProfile.trim() ? `&reader=${encodeURIComponent(readerProfile.trim())}` : ''}`}>打开体验页</Link>
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
          description="正文底座已加载，增强计划暂未完成。你仍可先检查当前验收与检视信息。"
          className="reader-workbench-debug__alert"
        />
      ) : null}
      {selectedKbId <= 0 ? (
        <Alert
          type="info"
          showIcon
          message="当前未绑定知识库"
          description="workbench 会继续基于正文与公开资源构建体验计划；如果你要验证知识库检索效果，可补充 kb。"
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
        <div className="reader-workbench-debug__layout">
          <section className="reader-workbench-debug__main">
            <Card title="Acceptance Plane" className="reader-workbench-debug__panel">
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Paragraph className="reader-workbench-debug__module-summary" style={{ marginBottom: 0 }}>
                  workbench 负责验收中间产物与 provenance，并在同一页核对最终讲读稿是否达到发布边界。
                </Paragraph>
                <Space wrap>
                  {acceptanceChecks.map((item) => (
                    <Tag key={item.key} color={item.ready ? 'green' : 'orange'}>
                      {item.ready ? `ready · ${item.label}` : `pending · ${item.label}`}
                    </Tag>
                  ))}
                </Space>
                <Space wrap>
                  <Tag>{`compose-layer ${cacheState.composeLayer}`}</Tag>
                  <Tag>{`plan-layer ${cacheState.planLayer}`}</Tag>
                  <Tag>{`experience-layer ${cacheState.experienceLayer}`}</Tag>
                  <Tag>{`compose-hit ${cacheState.composeHit ? 'yes' : 'no'}`}</Tag>
                  <Tag>{`plan-hit ${cacheState.planHit ? 'yes' : 'no'}`}</Tag>
                  <Tag>{`experience-hit ${cacheState.experienceHit ? 'yes' : 'no'}`}</Tag>
                </Space>
              </Space>
            </Card>

            <Card title="Final Manuscript (Reader-Facing)" className="reader-workbench-debug__panel">
              {hasFinalManuscript ? (
                <GenerativeExperienceRenderer
                  renderMode="final_manuscript"
                  layoutVariant={layoutVariant}
                  hero={hero}
                  focusHeading={String(preferDisplayCopy(hero?.display_title, hero?.title)).trim() || storyTitle}
                  visibleClaims={[]}
                  contextCards={[]}
                  narrativeSections={[]}
                  guidedBeats={[]}
                  teachingManuscript={experiencePlan?.teaching_manuscript || null}
                  toolEnrichmentPacket={{}}
                  focusNode={null}
                  bodyFlowNodes={[]}
                  readingFlowNodes={[]}
                  renderCtx={renderCtx}
                  composeLoading={composeLoading}
                  hasComposePayload={Boolean(effectiveComposePayload)}
                  backgroundRefreshing={backgroundRefreshing}
                  fallbackQuestionAnswers={[]}
                  resourceModules={[]}
                  interactionModules={[]}
                  widgetBlocks={[]}
                  getBlockUiAction={getBlockUiAction}
                  dispatchBlockAction={dispatchBlockAction}
                  lastUiEvent={lastUiEvent}
                  topStatusText={topStatusText}
                  seedMode={false}
                />
              ) : (
                <Alert
                  type="warning"
                  showIcon
                  message="最终讲读稿尚未就绪"
                  description="当前仍处于草稿或 seed 计划阶段；请继续查看右侧 draft / critic / provenance 信息。"
                />
              )}
            </Card>

            <Card title="Story & Draft Outline" className="reader-workbench-debug__panel">
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <div className="reader-workbench-debug__summary-block">
                  <Text className="reader-workbench-debug__module-eyebrow">Story substrate</Text>
                  {renderStorySubstrate(storySubstrate)}
                </div>
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
                  <summary>查看 grounded targets</summary>
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
            <Card title="Cache State" className="reader-workbench-debug__panel">
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                <Space wrap>
                  <Tag>{`layout ${layoutVariant}`}</Tag>
                  {pageBrief?.page_archetype ? <Tag>{pageBrief.page_archetype}</Tag> : null}
                  {displayCopyContract ? <Tag>{displayCopyContract}</Tag> : null}
                  {storyMapMeta.toolRows.length ? storyMapMeta.toolRows.map((tool) => <Tag key={tool} color="geekblue">{tool}</Tag>) : null}
                </Space>
                <Paragraph className="reader-workbench-debug__module-summary" style={{ marginBottom: 0 }}>
                  {topSummary}
                </Paragraph>
                {composeMetaSummary ? (
                  <Paragraph className="reader-workbench-debug__module-summary" style={{ marginBottom: 0 }}>
                    {composeMetaSummary}
                  </Paragraph>
                ) : null}
                <Space wrap>
                  <Tag>{`compose ${cacheState.composeLayer}`}</Tag>
                  <Tag>{`plan ${cacheState.planLayer}`}</Tag>
                  <Tag>{`experience ${cacheState.experienceLayer}`}</Tag>
                  <Tag>{`fresh ${cacheState.isFresh ? 'yes' : 'no'}`}</Tag>
                  <Tag>{`seed ${cacheState.isSeed ? 'yes' : 'no'}`}</Tag>
                </Space>
              </Space>
            </Card>

            <Card title="Draft Meta" className="reader-workbench-debug__panel">
              <Space direction="vertical" size={10} style={{ width: '100%' }}>
                <div>
                  <Text className="reader-workbench-debug__module-eyebrow">Reading path</Text>
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
                </div>
                <div>
                  <Text className="reader-workbench-debug__module-eyebrow">Content budget</Text>
                  {Object.keys(contentBudget).length ? (
                    <Space wrap>
                      {Object.entries(contentBudget).map(([key, value]) => (
                        <Tag key={key}>{`${key}:${value}`}</Tag>
                      ))}
                    </Space>
                  ) : (
                    <Empty description="No content budget" />
                  )}
                </div>
              </Space>
            </Card>

            <Card title="Page Dossier" className="reader-workbench-debug__panel">
              {renderPageDossierSummary(pageDossier)}
            </Card>

            <Card title="Page Brief" className="reader-workbench-debug__panel">
              {renderPageBriefSummary(pageBrief)}
            </Card>

            <Card title="Planning Brief (Draft)" className="reader-workbench-debug__panel">
              {renderPlanningBrief(planningBrief)}
            </Card>

            <Card title="Planner Output (Draft)" className="reader-workbench-debug__panel">
              {renderPlannerOutput(plannerOutput)}
            </Card>

            <Card title="Critic / Contract" className="reader-workbench-debug__panel">
              <Space direction="vertical" size={14} style={{ width: '100%' }}>
                {renderComposeCritic(effectiveComposePayload)}
                {renderContractValidation(contractValidation)}
              </Space>
            </Card>

            <Card title="Provenance · Guided Beats" className="reader-workbench-debug__panel">
              {renderGuidedBeats(guidedBeats, plannerOutput, toolEnrichmentPacket)}
            </Card>

            <Card title="Provenance · Adjacent Context" className="reader-workbench-debug__panel">
              {renderAdjacentPageContext(adjacentPageContext)}
            </Card>

            <Card title="Provenance · Tool Enrichment Packet" className="reader-workbench-debug__panel">
              {renderToolEnrichmentPacket(toolEnrichmentPacket)}
            </Card>

            <Card title="Provenance · Runtime Stages" className="reader-workbench-debug__panel">
              {renderRuntimeStages(runtimeStageTrace)}
            </Card>

            <Card title="Provenance · Tool Trace" className="reader-workbench-debug__panel">
              {renderToolTraceRows(toolTraceRows)}
            </Card>
          </aside>
        </div>
      ) : null}
    </div>
  )
}
