import {
  Button,
  Card,
  Collapse,
  List,
  Space,
  Tag,
  Typography,
} from 'antd'

import type {
  ReaderExperienceBlockRef,
  ReaderGenerativeInteractionModule,
  ReaderGenerativeJsWidgetPlan,
  ReaderGenerativeResourceModule,
} from '@/services/api'
import type { ReaderExperienceUiActionRef } from './useExperienceActionBus'

const { Title, Text, Paragraph } = Typography

export type ExperienceBlockActionHandler = (
  block: ReaderExperienceBlockRef,
  actionType: string,
  targetRefOverride?: string,
) => void

export type ExperienceBlockActionResolver = (
  block: ReaderExperienceBlockRef | null | undefined,
  actionType: string,
) => ReaderExperienceUiActionRef

type ResourceModuleRendererProps = {
  module: ReaderGenerativeResourceModule
  block: ReaderExperienceBlockRef | null
  getBlockUiAction: ExperienceBlockActionResolver
  dispatchBlockAction: ExperienceBlockActionHandler
  eyebrow: string
}

type InteractionModuleRendererProps = {
  module: ReaderGenerativeInteractionModule
  block: ReaderExperienceBlockRef | null
  getBlockUiAction: ExperienceBlockActionResolver
  dispatchBlockAction: ExperienceBlockActionHandler
  eyebrow: string
}

type WidgetRendererProps = {
  widget: ReaderGenerativeJsWidgetPlan
  block: ReaderExperienceBlockRef | null
  getBlockUiAction: ExperienceBlockActionResolver
  dispatchBlockAction: ExperienceBlockActionHandler
  eyebrow: string
}

type ResourceModuleDefinition = {
  eyebrow: string
  render: (props: ResourceModuleRendererProps) => JSX.Element
}

type InteractionModuleDefinition = {
  eyebrow: string
  role: 'glossary' | 'question' | 'generic'
  render: (props: InteractionModuleRendererProps) => JSX.Element
}

type WidgetDefinition = {
  eyebrow: string
  render: (props: WidgetRendererProps) => JSX.Element
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function shouldRenderModuleSummary(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  return ![
    '先补一层必要的方法背景。',
    '先补上理解当前内容需要的背景。',
    '补充少量真正需要的外部背景，帮助理解正文。',
    '先用图或关键证据建立抓手，再回到正文核对作者的解释。',
  ].includes(text)
}

function extractLinkDomain(href: string): string {
  try {
    const url = new URL(String(href || '').trim())
    return String(url.hostname || '').replace(/^www\./i, '')
  } catch {
    return ''
  }
}

function renderResourceModuleCard(props: ResourceModuleRendererProps) {
  const { module, block, getBlockUiAction, dispatchBlockAction, eyebrow } = props
  const links = Array.isArray(module.links) ? module.links : []
  const primaryDomain = links.length ? extractLinkDomain(String((links[0] as Record<string, unknown>).href || '')) : ''
  const title = preferDisplayCopy(module.display_title, module.title)
  const summary = preferDisplayCopy(module.display_summary, module.summary)
  const inspectSourceAction = getBlockUiAction(block, 'inspect_source')
  return (
    <Card key={module.module_id} size="small" className="reader-experience-page__module-card reader-experience-page__resource-card">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div className="reader-experience-page__module-head">
          <Text className="reader-experience-page__eyebrow">{eyebrow}</Text>
          {primaryDomain ? <Tag bordered={false} className="reader-experience-page__domain-chip">{primaryDomain}</Tag> : null}
        </div>
        <Title level={4} style={{ margin: 0 }}>{title}</Title>
        {shouldRenderModuleSummary(summary) ? <Paragraph className="reader-experience-page__summary">{summary}</Paragraph> : null}
        {links.length ? (
          <div className="reader-experience-page__resource-links">
            {links.map((item, index) => {
              const row = item as Record<string, unknown>
              const href = String(row.href || '').trim()
              const label = String(row.label || href || 'Link').trim()
              const domain = extractLinkDomain(href)
              if (!href) return null
              return (
                <a
                  key={`${module.module_id}-link-${index}`}
                  href={href}
                  target="_blank"
                  rel="noreferrer"
                  className="reader-experience-page__resource-link"
                  onClick={() => {
                    if (block) dispatchBlockAction(block, 'open_resource')
                  }}
                >
                  <span className="reader-experience-page__resource-link-title">{label}</span>
                  <span className="reader-experience-page__resource-link-meta">
                    <span>{domain || '外部来源'}</span>
                    <span aria-hidden="true">↗</span>
                  </span>
                </a>
              )
            })}
          </div>
        ) : null}
        {block && inspectSourceAction ? (
          <Button size="small" type="text" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'inspect_source')}>
            {inspectSourceAction.label || '查看来源'}
          </Button>
        ) : null}
      </Space>
    </Card>
  )
}

function renderGlossaryModule(props: InteractionModuleRendererProps) {
  const { module, block, getBlockUiAction, dispatchBlockAction, eyebrow } = props
  const terms = Array.isArray(module.props?.terms) ? module.props.terms : []
  const title = preferDisplayCopy(module.display_title, module.title)
  const summary = preferDisplayCopy(module.display_summary, '')
  const expandDefinitionAction = getBlockUiAction(block, 'expand_definition')
  const returnToReaderAction = getBlockUiAction(block, 'return_to_reader')
  return (
    <Card key={module.module_id} size="small" className="reader-experience-page__module-card">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div className="reader-experience-page__module-head">
          <Text className="reader-experience-page__eyebrow">{eyebrow}</Text>
        </div>
        <Title level={4} style={{ margin: 0 }}>{title}</Title>
        {shouldRenderModuleSummary(summary) ? <Paragraph className="reader-experience-page__summary">{summary}</Paragraph> : null}
        {terms.length ? (
          <div className="reader-experience-page__term-list">
            {terms.map((item, index) => {
              if (!item || typeof item !== 'object') return null
              const row = item as Record<string, unknown>
              const term = String(row.term || '').trim()
              const definition = String(row.definition || '').trim()
              if (!term && !definition) return null
              return (
                <div key={`${module.module_id}-term-${index}`} className="reader-experience-page__term-card">
                  <Text strong>{term || `术语 ${index + 1}`}</Text>
                  {definition ? <Paragraph className="reader-experience-page__summary">{definition}</Paragraph> : null}
                  {block && expandDefinitionAction ? (
                    <Button size="small" type="text" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'expand_definition')}>
                      {expandDefinitionAction.label || '展开术语解释'}
                    </Button>
                  ) : null}
                </div>
              )
            })}
          </div>
        ) : (
          <Paragraph className="reader-experience-page__summary">暂无术语解释。</Paragraph>
        )}
        {block && returnToReaderAction ? (
          <Button size="small" type="text" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'return_to_reader')}>
            {returnToReaderAction.label || '回到正文'}
          </Button>
        ) : null}
      </Space>
    </Card>
  )
}

function renderQuestionStarterModule(props: InteractionModuleRendererProps) {
  const { module, block, getBlockUiAction, dispatchBlockAction, eyebrow } = props
  const questions = Array.isArray(module.props?.questions) ? module.props.questions : []
  const title = preferDisplayCopy(module.display_title, module.title)
  const summary = preferDisplayCopy(module.display_summary, '')
  const returnToReaderAction = getBlockUiAction(block, 'return_to_reader')
  const startFollowupAction = getBlockUiAction(block, 'start_followup')
  return (
    <Card key={module.module_id} size="small" className="reader-experience-page__module-card">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div className="reader-experience-page__module-head">
          <Text className="reader-experience-page__eyebrow">{eyebrow}</Text>
        </div>
        <Title level={4} style={{ margin: 0 }}>{title}</Title>
        {summary ? <Paragraph className="reader-experience-page__summary">{summary}</Paragraph> : null}
        {questions.length ? (
          <List
            size="small"
            className="reader-experience-page__question-list"
            dataSource={questions.map((item) => String(item || '').trim()).filter(Boolean)}
            renderItem={(item) => (
              <List.Item className="reader-experience-page__question-item" onClick={() => { if (block) dispatchBlockAction(block, 'start_followup') }}>
                <Text>{item}</Text>
              </List.Item>
            )}
          />
        ) : (
          <Paragraph className="reader-experience-page__summary">暂无引导问题。</Paragraph>
        )}
        {block && returnToReaderAction ? (
          <Button size="small" type="text" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'return_to_reader')}>
            {returnToReaderAction.label || '回到正文'}
          </Button>
        ) : null}
        {block && startFollowupAction ? (
          <Button size="small" type="default" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'start_followup')}>
            {startFollowupAction.label || '继续追问'}
          </Button>
        ) : null}
      </Space>
    </Card>
  )
}

function renderGenericInteractionModule(props: InteractionModuleRendererProps) {
  const { module, block, getBlockUiAction, dispatchBlockAction, eyebrow } = props
  const qaPairs = Array.isArray(module.props?.qa_pairs) ? module.props.qa_pairs : []
  const title = preferDisplayCopy(module.display_title, module.title)
  const summary = preferDisplayCopy(module.display_summary, '')
  const returnToReaderAction = getBlockUiAction(block, 'return_to_reader')
  const startFollowupAction = getBlockUiAction(block, 'start_followup')
  return (
    <Card key={module.module_id} size="small" className="reader-experience-page__module-card">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div className="reader-experience-page__module-head">
          <Text className="reader-experience-page__eyebrow">{eyebrow}</Text>
        </div>
        <Title level={4} style={{ margin: 0 }}>{title}</Title>
        {summary ? <Paragraph className="reader-experience-page__summary">{summary}</Paragraph> : null}
        {qaPairs.length ? (
          <Collapse
            bordered={false}
            className="reader-experience-page__qa-collapse"
            items={qaPairs.map((item, index) => {
              const row = (item && typeof item === 'object') ? item as Record<string, unknown> : {}
              const question = String(row.question || `问题 ${index + 1}`).trim()
              const answer = String(row.answer || '').trim()
              const confidence = String(row.confidence || '').trim()
              return {
                key: `${module.module_id}-qa-${index}`,
                label: question,
                children: (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    <Paragraph className="reader-experience-page__summary">{answer || '回答生成中。'}</Paragraph>
                    {confidence ? <Tag bordered={false} className="reader-experience-page__domain-chip">{confidence}</Tag> : null}
                  </Space>
                ),
              }
            })}
          />
        ) : (
          <Paragraph className="reader-experience-page__summary">暂无交互细节。</Paragraph>
        )}
        {block && returnToReaderAction ? (
          <Button size="small" type="text" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'return_to_reader')}>
            {returnToReaderAction.label || '回到正文'}
          </Button>
        ) : null}
        {block && startFollowupAction ? (
          <Button size="small" type="default" className="reader-experience-page__inline-action" onClick={() => dispatchBlockAction(block, 'start_followup')}>
            {startFollowupAction.label || '继续追问'}
          </Button>
        ) : null}
      </Space>
    </Card>
  )
}

function renderFigureFocusAccordion(props: WidgetRendererProps) {
  const { widget, block, getBlockUiAction, dispatchBlockAction, eyebrow } = props
  const panels = Array.isArray(widget.props?.panels) ? widget.props.panels : []
  const title = preferDisplayCopy(widget.display_title, widget.title)
  const summary = preferDisplayCopy(widget.display_summary, '')
  const expandPanelAction = getBlockUiAction(block, 'expand_panel')
  const focusTargetAction = getBlockUiAction(block, 'focus_target')
  return (
    <Card key={widget.widget_id} size="small" className="reader-experience-page__module-card reader-experience-page__widget-card">
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <div className="reader-experience-page__module-head">
          <Text className="reader-experience-page__eyebrow">{eyebrow}</Text>
        </div>
        <Title level={4} style={{ margin: 0 }}>{title}</Title>
        {summary ? <Paragraph className="reader-experience-page__summary">{summary}</Paragraph> : null}
        {panels.length ? (
          <div className="reader-experience-page__widget-panels">
            {panels.map((item, index) => {
              if (!item || typeof item !== 'object') return null
              const row = item as Record<string, unknown>
              const label = preferDisplayCopy(row.display_label, row.label || `Panel ${index + 1}`)
              const panelSummary = preferDisplayCopy(row.display_summary, row.summary || '')
              return (
                <div
                  key={`${widget.widget_id}-panel-${index}`}
                  className="reader-experience-page__widget-panel"
                  onClick={() => {
                    if (!block) return
                    if (expandPanelAction) dispatchBlockAction(block, 'expand_panel')
                    const focusRef = String(row.focus_target_id || row.target_id || block.target_ids?.[0] || '').trim()
                    if (focusTargetAction) dispatchBlockAction(block, 'focus_target', focusRef)
                  }}
                >
                  <Text strong>{label}</Text>
                  <Paragraph className="reader-experience-page__summary">{panelSummary || '焦点说明生成中。'}</Paragraph>
                </div>
              )
            })}
          </div>
        ) : (
          <Paragraph className="reader-experience-page__summary">暂无交互细节。</Paragraph>
        )}
      </Space>
    </Card>
  )
}

function renderGenericWidget(props: WidgetRendererProps) {
  return renderFigureFocusAccordion(props)
}

const RESOURCE_MODULE_REGISTRY: Record<string, ResourceModuleDefinition> = {
  FigureExplainPanel: {
    eyebrow: '图解导读',
    render: renderResourceModuleCard,
  },
  RelatedResourceCard: {
    eyebrow: '延伸阅读',
    render: renderResourceModuleCard,
  },
}

const DEFAULT_RESOURCE_MODULE_DEFINITION: ResourceModuleDefinition = {
  eyebrow: '资源模块',
  render: renderResourceModuleCard,
}

const INTERACTION_MODULE_REGISTRY: Record<string, InteractionModuleDefinition> = {
  GlossaryPanel: {
    eyebrow: '概念解释',
    role: 'glossary',
    render: renderGlossaryModule,
  },
  QuestionStarterPanel: {
    eyebrow: '继续探索',
    role: 'question',
    render: renderQuestionStarterModule,
  },
}

const DEFAULT_INTERACTION_MODULE_DEFINITION: InteractionModuleDefinition = {
  eyebrow: '交互模块',
  role: 'generic',
  render: renderGenericInteractionModule,
}

const WIDGET_REGISTRY: Record<string, WidgetDefinition> = {
  'figure-focus-accordion': {
    eyebrow: '交互图解',
    render: renderFigureFocusAccordion,
  },
}

const DEFAULT_WIDGET_DEFINITION: WidgetDefinition = {
  eyebrow: '交互区块',
  render: renderGenericWidget,
}

export function getResourceModuleDefinition(moduleType: string): ResourceModuleDefinition {
  return RESOURCE_MODULE_REGISTRY[String(moduleType || '').trim()] || DEFAULT_RESOURCE_MODULE_DEFINITION
}

export function getInteractionModuleDefinition(moduleType: string): InteractionModuleDefinition {
  return INTERACTION_MODULE_REGISTRY[String(moduleType || '').trim()] || DEFAULT_INTERACTION_MODULE_DEFINITION
}

export function getWidgetDefinition(widgetType: string): WidgetDefinition {
  return WIDGET_REGISTRY[String(widgetType || '').trim()] || DEFAULT_WIDGET_DEFINITION
}

export function isQuestionStarterModule(module: ReaderGenerativeInteractionModule): boolean {
  return getInteractionModuleDefinition(module.module_type).role === 'question'
}
