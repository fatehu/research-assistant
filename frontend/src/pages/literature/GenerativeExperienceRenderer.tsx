import { useMemo, type ReactNode } from 'react'
import {
  Card,
  Collapse,
  Empty,
  List,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'

import type {
  ReaderComponentNode,
  ReaderExperienceBlockRef,
  ReaderExperiencePlan,
  ReaderGenerativeInteractionModule,
  ReaderGenerativeJsWidgetPlan,
  ReaderGenerativeResourceModule,
  ReaderStoryClaim,
} from '@/services/api'
import { renderReaderComponentTree, type ReaderComponentRenderContext } from './readerComponents'
import {
  getInteractionModuleDefinition,
  getResourceModuleDefinition,
  getWidgetDefinition,
  isQuestionStarterModule,
  type ExperienceBlockActionHandler,
  type ExperienceBlockActionResolver,
} from './experienceBlockRegistry'
import type { ExperienceUiEvent } from './useExperienceActionBus'

const { Title, Text, Paragraph } = Typography

export type ExperienceLayoutVariant =
  | 'focus_figure_split'
  | 'guided_story_stack'
  | 'explainer_first'
  | 'resource_augmented_reader'

type ExperienceContextCard = {
  key: string
  title: string
  body: ReactNode
}

type FallbackQuestionAnswer = {
  question: string
  answer: string
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function buildIdLookup<T extends { module_id?: string; widget_id?: string }>(
  rows: T[],
  key: 'module_id' | 'widget_id',
): Map<string, T> {
  const lookup = new Map<string, T>()
  for (const row of rows) {
    const token = String(row?.[key] || '').trim()
    if (token) lookup.set(token, row)
  }
  return lookup
}

type GenerativeExperienceRendererProps = {
  layoutVariant: ExperienceLayoutVariant
  hero: ReaderExperiencePlan['hero'] | null
  focusHeading: string
  visibleClaims: ReaderStoryClaim[]
  contextCards: ExperienceContextCard[]
  narrativeSections: ReaderExperiencePlan['main_sections']
  focusNode: ReaderComponentNode | null
  readingFlowNodes: ReaderComponentNode[]
  renderCtx: ReaderComponentRenderContext
  composeLoading: boolean
  hasComposePayload: boolean
  backgroundRefreshing: boolean
  fallbackQuestionAnswers: FallbackQuestionAnswer[]
  resourceModules: ReaderGenerativeResourceModule[]
  interactionModules: ReaderGenerativeInteractionModule[]
  widgetBlocks: ReaderGenerativeJsWidgetPlan[]
  getBlockUiAction: ExperienceBlockActionResolver
  dispatchBlockAction: ExperienceBlockActionHandler
  lastUiEvent: ExperienceUiEvent
  topStatusText: string
}

export function GenerativeExperienceRenderer(props: GenerativeExperienceRendererProps) {
  const {
    layoutVariant,
    hero,
    focusHeading,
    visibleClaims,
    contextCards,
    narrativeSections,
    focusNode,
    readingFlowNodes,
    renderCtx,
    composeLoading,
    hasComposePayload,
    backgroundRefreshing,
    fallbackQuestionAnswers,
    resourceModules,
    interactionModules,
    widgetBlocks,
    getBlockUiAction,
    dispatchBlockAction,
    lastUiEvent,
    topStatusText,
  } = props

  const resourceModuleLookup = useMemo(() => buildIdLookup(resourceModules, 'module_id'), [resourceModules])
  const interactionModuleLookup = useMemo(() => buildIdLookup(interactionModules, 'module_id'), [interactionModules])
  const widgetLookup = useMemo(() => buildIdLookup(widgetBlocks, 'widget_id'), [widgetBlocks])

  const mainNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || 'main').trim() === 'main'),
    [narrativeSections],
  )
  const sidebarNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || '').trim() === 'sidebar'),
    [narrativeSections],
  )
  const footerNarrativeSections = useMemo(
    () => narrativeSections.filter((section) => String(section.section_region || '').trim() === 'footer'),
    [narrativeSections],
  )
  const heroContextCards = useMemo(() => {
    if (layoutVariant === 'guided_story_stack') return contextCards
    return contextCards.slice(0, 1)
  }, [contextCards, layoutVariant])
  const sidebarContextCards = useMemo(() => {
    if (layoutVariant === 'guided_story_stack') return []
    if (layoutVariant === 'explainer_first') return contextCards
    return contextCards.slice(1)
  }, [contextCards, layoutVariant])
  const hasSidebar = sidebarNarrativeSections.length > 0

  const resolveSectionBlockRefs = (
    section: ReaderExperiencePlan['main_sections'][number],
    blockType: 'resource_module' | 'interaction_module' | 'widget',
  ) => {
    const blockRefs = (section.blocks || [])
      .filter((block) => String(block.block_type || '').trim() === blockType)
      .slice()
      .sort((left, right) => {
        const leftPriority = Number(left.priority || 0)
        const rightPriority = Number(right.priority || 0)
        if (leftPriority !== rightPriority) return leftPriority - rightPriority
        return String(left.block_id || '').localeCompare(String(right.block_id || ''))
      })
      .map((block) => String(block.ref_id || '').trim())
      .filter(Boolean)
    if (blockRefs.length) return blockRefs
    if (blockType === 'resource_module') return section.resource_module_ids || []
    if (blockType === 'interaction_module') return section.interaction_module_ids || []
    return section.widget_ids || []
  }

  const resolveSectionBlocks = (section: ReaderExperiencePlan['main_sections'][number]) =>
    (section.blocks || []).slice().sort((left, right) => {
      const leftPriority = Number(left.priority || 0)
      const rightPriority = Number(right.priority || 0)
      if (leftPriority !== rightPriority) return leftPriority - rightPriority
      return String(left.block_id || '').localeCompare(String(right.block_id || ''))
    })

  const resolveSectionResourceModules = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'resource_module')
      .map((moduleId) => resourceModuleLookup.get(String(moduleId || '').trim()))
      .filter((module): module is NonNullable<typeof module> => Boolean(module))

  const resolveSectionInteractionModules = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'interaction_module')
      .map((moduleId) => interactionModuleLookup.get(String(moduleId || '').trim()))
      .filter((module): module is NonNullable<typeof module> => Boolean(module))

  const resolveSectionWidgets = (section: ReaderExperiencePlan['main_sections'][number]) =>
    resolveSectionBlockRefs(section, 'widget')
      .map((widgetId) => widgetLookup.get(String(widgetId || '').trim()))
      .filter((widget): widget is NonNullable<typeof widget> => Boolean(widget))

  const resolveReadingFlowNodes = (section: ReaderExperiencePlan['main_sections'][number]) => {
    const targetIds = (section.target_ids || []).map((item) => String(item || '').trim()).filter(Boolean)
    if (!targetIds.length) return readingFlowNodes
    const matched = readingFlowNodes.filter((node) => {
      const nodeId = String(node.id || '').trim()
      return targetIds.some((targetId) => targetId === nodeId || targetId.endsWith(`:${nodeId}`))
    })
    return matched.length ? matched : readingFlowNodes
  }

  const renderResourceModule = (module: ReaderGenerativeResourceModule, block: ReaderExperienceBlockRef | null) => {
    const definition = getResourceModuleDefinition(module.module_type)
    return definition.render({
      module,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderInteractionModule = (module: ReaderGenerativeInteractionModule, block: ReaderExperienceBlockRef | null) => {
    const definition = getInteractionModuleDefinition(module.module_type)
    return definition.render({
      module,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderWidget = (widget: ReaderGenerativeJsWidgetPlan, block: ReaderExperienceBlockRef | null) => {
    const definition = getWidgetDefinition(widget.widget_type)
    return definition.render({
      widget,
      block,
      getBlockUiAction,
      dispatchBlockAction,
      eyebrow: definition.eyebrow,
    })
  }

  const renderBlockState = (
    block: ReaderExperienceBlockRef | null,
    content: ReactNode,
    emptyLabel: string,
  ) => {
    const state = String(block?.state || 'ready').trim().toLowerCase()
    const fallbackPolicy = String(block?.fallback_policy || 'omit').trim().toLowerCase()
    if (state === 'loading') {
      return (
        <Card size="small" className="reader-experience-page__module-card reader-experience-page__module-card--soft">
          <div className="reader-experience-page__loading"><Spin /></div>
        </Card>
      )
    }
    if (state === 'error') {
      return (
        <Card size="small" className="reader-experience-page__module-card reader-experience-page__module-card--soft">
          <Empty description={emptyLabel || '该模块暂时不可用'} />
        </Card>
      )
    }
    if (state === 'empty') {
      if (fallbackPolicy === 'omit') return null
      return (
        <Card size="small" className="reader-experience-page__module-card reader-experience-page__module-card--soft">
          <Empty description={emptyLabel || '暂无内容'} />
        </Card>
      )
    }
    if (state === 'partial') {
      return (
        <div className="reader-experience-page__block-state-shell reader-experience-page__block-state-shell--partial">
          <div className="reader-experience-page__block-state-banner">
            <Tag color="gold">部分生成</Tag>
          </div>
          {content}
        </div>
      )
    }
    return content
  }

  const renderExperienceSection = (section: ReaderExperiencePlan['main_sections'][number]) => {
    const sectionType = String(section.section_type || '').trim()
    const sectionTitle = preferDisplayCopy(section.display_title, section.title)
    const sectionSummary = preferDisplayCopy(section.display_summary, section.summary)
    const sectionBlocks = resolveSectionBlocks(section)
    const sectionResourceModules = resolveSectionResourceModules(section)
    const sectionInteractionModules = resolveSectionInteractionModules(section)
    const sectionWidgets = resolveSectionWidgets(section)
    const resourceBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'resource_module').map((block) => [String(block.ref_id || '').trim(), block]))
    const interactionBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'interaction_module').map((block) => [String(block.ref_id || '').trim(), block]))
    const widgetBlockLookup = new Map(sectionBlocks.filter((block) => String(block.block_type || '').trim() === 'widget').map((block) => [String(block.ref_id || '').trim(), block]))
    const sectionQuestionModules = sectionInteractionModules.filter((module) => isQuestionStarterModule(module))
    const sectionExplainerModules = sectionInteractionModules.filter((module) => !isQuestionStarterModule(module))

    if (sectionType === 'hero' || sectionType === 'story_map') return null
    if (sectionType === 'focus_stage') {
      return (
        <Card key={section.section_id} title={sectionTitle || '焦点拆解'} className="reader-experience-page__panel">
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          <div className="reader-experience-page__focus-layout">
            <div className="reader-experience-page__focus-visual">
              {focusNode ? (
                <div className="reader-composed-surface reader-experience-page__surface reader-experience-page__surface--focus">
                  {renderReaderComponentTree([focusNode], renderCtx)}
                </div>
              ) : (
                <Empty description="暂无重点图示" />
              )}
            </div>
            <div className="reader-experience-page__focus-side">
              {preferDisplayCopy(hero?.display_summary, hero?.summary) ? (
                <Card size="small" className="reader-experience-page__module-card reader-experience-page__module-card--soft">
                  <Text className="reader-experience-page__eyebrow">如何阅读这张图</Text>
                  <Paragraph className="reader-experience-page__summary">{preferDisplayCopy(hero?.display_summary, hero?.summary)}</Paragraph>
                </Card>
              ) : null}
              {sectionExplainerModules.length ? sectionExplainerModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '术语解释暂未准备好')
              }) : null}
              {sectionWidgets.length ? sectionWidgets.map((widget) => {
                const block = widgetBlockLookup.get(String(widget.widget_id || '').trim()) || null
                return renderBlockState(block, renderWidget(widget, block), '图解控件暂未准备好')
              }) : null}
              {sectionResourceModules.length ? sectionResourceModules.map((module) => {
                const block = resourceBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderResourceModule(module, block), '延伸资源暂未准备好')
              }) : null}
              {!sectionExplainerModules.length && !sectionWidgets.length && !sectionResourceModules.length ? (
                backgroundRefreshing ? <div className="reader-experience-page__loading"><Spin /></div> : <Empty description="暂无聚焦增强内容" />
              ) : null}
            </div>
          </div>
        </Card>
      )
    }
    if (sectionType === 'reading_flow') {
      return (
        <Card key={section.section_id} title={sectionTitle || '正文阅读'} className="reader-experience-page__panel">
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {composeLoading && !hasComposePayload ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : resolveReadingFlowNodes(section).length ? (
            <div className="reader-composed-surface reader-experience-page__surface">
              {renderReaderComponentTree(resolveReadingFlowNodes(section), renderCtx)}
            </div>
          ) : (
            <Empty description="暂无正文内容" />
          )}
        </Card>
      )
    }
    if (sectionType === 'explainer_cluster') {
      return (
        <Card key={section.section_id} title={sectionTitle || '概念解释'} className="reader-experience-page__panel">
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {backgroundRefreshing && !sectionExplainerModules.length ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : sectionExplainerModules.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {sectionExplainerModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '解释模块暂未准备好')
              })}
            </Space>
          ) : (
            <Empty description="暂无解释模块" />
          )}
        </Card>
      )
    }
    if (sectionType === 'supporting_resources') {
      return (
        <Card key={section.section_id} title={sectionTitle || '延伸资源'} className="reader-experience-page__panel">
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {backgroundRefreshing && !sectionResourceModules.length ? (
            <div className="reader-experience-page__loading"><Spin /></div>
          ) : sectionResourceModules.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {sectionResourceModules.map((module) => {
                const block = resourceBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderResourceModule(module, block), '资源模块暂未准备好')
              })}
            </Space>
          ) : (
            <Empty description="暂无延伸资源" />
          )}
        </Card>
      )
    }
    if (sectionType === 'question_lab') {
      return (
        <Card key={section.section_id} title={sectionTitle || '继续探索'} className="reader-experience-page__panel">
          {sectionSummary ? <Paragraph className="reader-experience-page__summary reader-experience-page__section-summary">{sectionSummary}</Paragraph> : null}
          {sectionQuestionModules.length ? (
            <div className="reader-experience-page__question-grid">
              {sectionQuestionModules.map((module) => {
                const block = interactionBlockLookup.get(String(module.module_id || '').trim()) || null
                return renderBlockState(block, renderInteractionModule(module, block), '引导问题暂未准备好')
              })}
            </div>
          ) : fallbackQuestionAnswers.length ? (
            <Card size="small" className="reader-experience-page__module-card">
              <Text className="reader-experience-page__eyebrow">问题提示</Text>
              <Collapse
                bordered={false}
                className="reader-experience-page__qa-collapse"
                items={fallbackQuestionAnswers.map((item, index) => ({
                  key: `fallback-qa-${index}`,
                  label: item.question,
                  children: <Paragraph className="reader-experience-page__summary">{item.answer}</Paragraph>,
                }))}
              />
            </Card>
          ) : (
            <Empty description="暂无引导问题" />
          )}
          {sectionWidgets.length ? (
            <Space direction="vertical" size={12} style={{ width: '100%', marginTop: 16 }}>
              {sectionWidgets.map((widget) => {
                const block = widgetBlockLookup.get(String(widget.widget_id || '').trim()) || null
                return renderBlockState(block, renderWidget(widget, block), '控件暂未准备好')
              })}
            </Space>
          ) : null}
        </Card>
      )
    }
    return null
  }

  return (
    <>
      <div
        className={[
          'reader-experience-page__layout',
          `reader-experience-page__layout--${layoutVariant}`,
          !hasSidebar ? 'reader-experience-page__layout--solo' : '',
        ].filter(Boolean).join(' ')}
      >
        <main className="reader-experience-page__main">
          <Card className="reader-experience-page__hero-card">
            <div
              className={[
                'reader-experience-page__hero-grid',
                `reader-experience-page__hero-grid--${layoutVariant}`,
              ].join(' ')}
            >
              <div className="reader-experience-page__hero-summary">
                <div className="reader-experience-page__status-row">
                  <Text className="reader-experience-page__eyebrow">页面焦点</Text>
                  <span className="reader-experience-page__status-chip">{topStatusText}</span>
                </div>
                <Title level={3} style={{ marginTop: 0 }}>{focusHeading}</Title>
                {preferDisplayCopy(hero?.display_summary, hero?.summary) ? <Paragraph className="reader-experience-page__summary">{preferDisplayCopy(hero?.display_summary, hero?.summary)}</Paragraph> : null}
                {hero?.focus_label ? <Tag color="geekblue">{hero.focus_label}</Tag> : null}
                {visibleClaims.length ? (
                  <div className="reader-experience-page__claim-strip">
                    {visibleClaims.map((claim) => (
                      <div key={claim.claim_id} className="reader-experience-page__claim-card">
                        <Text className="reader-experience-page__eyebrow">关键发现</Text>
                        <Paragraph className="reader-experience-page__summary">{preferDisplayCopy(claim.display_text, claim.text)}</Paragraph>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="reader-experience-page__hero-claims">
                {preferDisplayCopy(hero?.display_subtitle, hero?.subtitle) ? (
                  <Card size="small" className="reader-experience-page__module-card reader-experience-page__hero-mini">
                    <Text className="reader-experience-page__eyebrow">阅读切入点</Text>
                    <Paragraph className="reader-experience-page__summary">{preferDisplayCopy(hero?.display_subtitle, hero?.subtitle)}</Paragraph>
                  </Card>
                ) : null}
                {mainNarrativeSections.length ? (
                  <Card size="small" className="reader-experience-page__module-card reader-experience-page__hero-mini">
                    <Text className="reader-experience-page__eyebrow">推荐阅读路径</Text>
                    <List
                      size="small"
                      dataSource={mainNarrativeSections.map((section) => preferDisplayCopy(section.display_title, section.title) || section.section_type)}
                      renderItem={(item) => <List.Item className="reader-experience-page__question-item"><Text>{item}</Text></List.Item>}
                    />
                  </Card>
                ) : null}
                {heroContextCards.length ? (
                  <div className="reader-experience-page__hero-mini-stack">
                    {heroContextCards.map((card) => (
                      <Card
                        key={`hero-context-${card.key}`}
                        size="small"
                        className="reader-experience-page__module-card reader-experience-page__hero-mini"
                      >
                        <Text className="reader-experience-page__eyebrow">{card.title}</Text>
                        {card.body}
                      </Card>
                    ))}
                  </div>
                ) : null}
                {lastUiEvent ? (
                  <Card size="small" className="reader-experience-page__module-card reader-experience-page__hero-mini">
                    <Text className="reader-experience-page__eyebrow">最近触发</Text>
                    <Paragraph className="reader-experience-page__summary">{lastUiEvent.label}</Paragraph>
                    <Text type="secondary">{lastUiEvent.targetRef ? `${lastUiEvent.eventName} · ${lastUiEvent.targetRef}` : lastUiEvent.eventName}</Text>
                  </Card>
                ) : null}
              </div>
            </div>
          </Card>
          {mainNarrativeSections.map((section) => renderExperienceSection(section))}
        </main>

        {hasSidebar ? (
          <aside className="reader-experience-page__sidebar">
            {sidebarContextCards.map((card) => (
              <Card key={card.key} size="small" title={card.title} className="reader-experience-page__module-card reader-experience-page__context-card">
                {card.body}
              </Card>
            ))}
            {sidebarNarrativeSections.map((section) => renderExperienceSection(section))}
          </aside>
        ) : null}
      </div>

      {footerNarrativeSections.length ? (
        <div className="reader-experience-page__footer-sections">
          {footerNarrativeSections.map((section) => renderExperienceSection(section))}
        </div>
      ) : null}
    </>
  )
}
