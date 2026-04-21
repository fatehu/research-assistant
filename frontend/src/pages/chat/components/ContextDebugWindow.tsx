import { useMemo, useState, type ReactNode } from 'react'
import {
  BranchesOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  DownOutlined,
  HistoryOutlined,
  MessageOutlined,
  RobotOutlined,
  UpOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import type {
  ChatContextDebug,
  ConversationCompactedHistory,
  ConversationContextSnapshot,
  ConversationContextState,
  ConversationHistoryLog,
  ConversationItemStream,
  ConversationTurnStore,
  ConversationToolLedger,
} from '@/services/api'

interface ContextDebugWindowProps {
  contextDebug: ChatContextDebug | null
  conversationState?: ConversationContextState | null
  conversationCompactedHistory?: ConversationCompactedHistory | null
  conversationHistoryLog?: ConversationHistoryLog | null
  conversationTurnStore?: ConversationTurnStore | null
  conversationToolLedger?: ConversationToolLedger | null
  conversationItemStream?: ConversationItemStream | null
  conversationContextSnapshots?: ConversationContextSnapshot[]
  isSending?: boolean
  isCompacting?: boolean
  onManualCompact?: () => void | Promise<void>
}

interface SectionProps {
  title: string
  icon: ReactNode
  children: ReactNode
  emptyText?: string
}

const intentLabels: Record<string, string> = {
  general_chat: '通用对话',
  knowledge_query: '知识库问答',
  web_query: '网页检索',
  literature_task: '论文任务',
  code_task: '代码任务',
}

const formatIntentLabel = (intent: string): string =>
  intentLabels[String(intent || '').trim()] || String(intent || '').trim() || 'unknown'

const clipText = (value: string | undefined | null, limit = 120): string => {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (!text) return ''
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(0, limit - 1)).trim()}…`
}

const formatEvidenceLedgerSummary = (
  evidenceLedger: ConversationContextState['evidence_ledger'] | undefined,
): string => {
  if (!evidenceLedger || evidenceLedger.length === 0) return ''
  return evidenceLedger
    .map((item) => {
      const summary = String(item?.summary || '').trim()
      if (!summary) return ''
      const suffixParts: string[] = []
      if (item?.source_labels?.length) {
        suffixParts.push(`来源: ${item.source_labels.join('/')}`)
      }
      if (item?.source_kind) {
        suffixParts.push(`类型: ${item.source_kind}`)
      }
      if (item?.tool_names?.length) {
        suffixParts.push(`工具: ${item.tool_names.join('/')}`)
      }
      if (item?.provenance_hints?.length) {
        suffixParts.push(`线索: ${item.provenance_hints.join('/')}`)
      }
      if (item?.turn_ids?.length) {
        suffixParts.push(`回合: ${item.turn_ids.join('/')}`)
      }
      if (item?.status === 'provisional') {
        suffixParts.push('暂定')
      }
      return suffixParts.length ? `${summary}（${suffixParts.join('；')}）` : summary
    })
    .filter(Boolean)
    .join('；')
}

const formatDecisionStateSummary = (
  decisionState: ConversationContextState['decision_state'] | undefined,
): string => {
  if (!decisionState) return ''
  const parts: string[] = []
  if (decisionState.status) {
    parts.push(`状态: ${decisionState.status}`)
  }
  if (decisionState.evidence_status) {
    parts.push(`证据: ${decisionState.evidence_status}`)
  }
  if (decisionState.next_action) {
    parts.push(`下一步: ${decisionState.next_action}`)
  }
  if (decisionState.blocked_reason) {
    parts.push(`阻塞: ${decisionState.blocked_reason}`)
  }
  if (decisionState.allowed_actions?.length) {
    parts.push(`允许动作: ${decisionState.allowed_actions.join(' / ')}`)
  }
  if (typeof decisionState.repo_edit_allowed === 'boolean') {
    parts.push(`可改 repo/source: ${decisionState.repo_edit_allowed ? '是' : '否'}`)
  }
  return parts.join('；')
}

const Section = ({ title, icon, children, emptyText }: SectionProps) => {
  const hasContent = Boolean(children)
  return (
    <section className="rounded-2xl border border-white/[0.06] bg-white/[0.03] p-3.5">
      <div className="mb-2.5 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
        <span className="text-emerald-300">{icon}</span>
        {title}
      </div>
      {hasContent ? children : <div className="text-sm text-slate-500">{emptyText || '暂无数据'}</div>}
    </section>
  )
}

const SummaryBlock = ({ text }: { text?: string }) => {
  if (!text) {
    return <div className="text-sm leading-6 text-slate-500">当前没有这层上下文。</div>
  }
  return <div className="whitespace-pre-wrap text-sm leading-6 text-slate-200">{text}</div>
}

const FullTextBlock = ({ text, emptyText }: { text?: string; emptyText: string }) => {
  if (!text) {
    return <div className="text-sm text-slate-500">{emptyText}</div>
  }
  return (
    <pre className="max-h-72 overflow-auto rounded-xl border border-white/[0.06] bg-slate-900/72 p-3 text-xs leading-6 whitespace-pre-wrap break-words text-slate-200">
      {text}
    </pre>
  )
}

const RawObjectBlock = ({
  value,
  emptyText,
}: {
  value: Record<string, unknown> | null | undefined
  emptyText: string
}) => {
  if (!value || Object.keys(value).length === 0) {
    return <div className="text-sm text-slate-500">{emptyText}</div>
  }
  return (
    <pre className="max-h-72 overflow-auto rounded-xl border border-white/[0.06] bg-slate-900/72 p-3 text-xs leading-6 whitespace-pre-wrap break-words text-slate-200">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

const MessagePreviewList = ({
  messages,
  emptyText,
  lineClamp = 3,
}: {
  messages?: ChatContextDebug['recent_messages']
  emptyText: string
  lineClamp?: number
}) => {
  if (!messages || messages.length === 0) {
    return <div className="text-sm text-slate-500">{emptyText}</div>
  }

  return (
    <div className="space-y-2">
      {messages.map((item, index) => (
        <div key={`${item.role}-${index}`} className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
          <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-slate-500">
            <span className="rounded-full border border-white/[0.08] px-2 py-0.5 text-[10px] text-slate-300">
              {item.role}
            </span>
          </div>
          <div
            className="text-sm leading-6 text-slate-200"
            style={{
              display: '-webkit-box',
              WebkitLineClamp: lineClamp,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
            title={item.content}
          >
            {item.content}
          </div>
        </div>
      ))}
    </div>
  )
}

const SkillMatchList = ({
  items,
  emptyText,
  tone = 'neutral',
}: {
  items?: ChatContextDebug['active_skills']
  emptyText: string
  tone?: 'neutral' | 'active'
}) => {
  if (!items || items.length === 0) {
    return <div className="text-sm text-slate-500">{emptyText}</div>
  }

  const accentClass =
    tone === 'active'
      ? 'border-emerald-400/18 bg-emerald-500/10 text-emerald-200'
      : 'border-white/[0.08] bg-slate-950/70 text-slate-300'

  return (
    <div className="space-y-2">
      {items.map((item) => (
        <div
          key={`${item.name}-${item.path || item.activation_reason || 'skill'}`}
          className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full border px-2.5 py-1 text-[11px] ${accentClass}`}>
              {item.display_name || item.name}
            </span>
            {typeof item.score === 'number' ? (
              <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-[11px] text-cyan-200">
                score {item.score}
              </span>
            ) : null}
          </div>
          {item.description ? (
            <div className="mt-2 text-sm leading-6 text-slate-200">{item.description}</div>
          ) : null}
          {item.short_description ? (
            <div className="mt-1 text-xs leading-5 text-slate-400">{item.short_description}</div>
          ) : null}
          {item.when_to_use ? (
            <div className="mt-1 text-xs leading-5 text-slate-400">触发说明: {item.when_to_use}</div>
          ) : null}
          {item.stage_names && item.stage_names.length > 0 ? (
            <div className="mt-2 text-xs leading-5 text-slate-400">
              阶段: {item.stage_names.join(' -> ')}
            </div>
          ) : null}
          {item.stage_policies && item.stage_policies.length > 0 ? (
            <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-slate-400">
              {item.stage_policies.map((policy) => (
                <span
                  key={`${item.name}-stage-${policy}`}
                  className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2 py-0.5 text-cyan-200"
                >
                  {policy}
                </span>
              ))}
            </div>
          ) : null}
          {item.default_continue_policy ? (
            <div className="mt-1 text-xs leading-5 text-slate-400">
              默认继续策略: {item.default_continue_policy}
            </div>
          ) : null}
          {item.scripts && item.scripts.length > 0 ? (
            <div className="mt-1 text-xs leading-5 text-slate-400">
              辅助脚本: {item.scripts.join(', ')}
            </div>
          ) : null}
          {item.default_prompt ? (
            <div className="mt-1 text-xs leading-5 text-slate-400">默认入口: {item.default_prompt}</div>
          ) : null}
          <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-slate-500">
            {item.activation_reason ? (
              <span className="rounded-full border border-fuchsia-400/18 bg-fuchsia-500/10 px-2 py-0.5 text-fuchsia-200">
                {item.activation_reason}
              </span>
            ) : null}
            {item.execution_context ? (
              <span className="rounded-full border border-amber-400/18 bg-amber-500/10 px-2 py-0.5 text-amber-200">
                context {item.execution_context}
              </span>
            ) : null}
            {item.agent ? (
              <span className="rounded-full border border-sky-400/18 bg-sky-500/10 px-2 py-0.5 text-sky-200">
                agent {item.agent}
              </span>
            ) : null}
            {item.effort ? (
              <span className="rounded-full border border-lime-400/18 bg-lime-500/10 px-2 py-0.5 text-lime-200">
                effort {item.effort}
              </span>
            ) : null}
            {typeof item.user_invocable === 'boolean' ? (
              <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">
                {item.user_invocable ? '可显式调用' : '仅内部调用'}
              </span>
            ) : null}
            {typeof item.allow_implicit_invocation === 'boolean' ? (
              <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">
                {item.allow_implicit_invocation ? '允许隐式触发' : '仅显式触发'}
              </span>
            ) : null}
            {item.path ? (
              <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">{item.path}</span>
            ) : null}
            {item.config_path ? (
              <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">
                {item.config_path}
              </span>
            ) : null}
            {item.interface_path ? (
              <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">
                {item.interface_path}
              </span>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  )
}

const ContextDebugWindow = ({
  contextDebug,
  conversationState = null,
  conversationCompactedHistory = null,
  conversationHistoryLog = null,
  conversationTurnStore = null,
  conversationToolLedger = null,
  conversationItemStream = null,
  conversationContextSnapshots = [],
  isSending = false,
  isCompacting = false,
  onManualCompact,
}: ContextDebugWindowProps) => {
  const [expanded, setExpanded] = useState(true)
  const [detailsExpanded, setDetailsExpanded] = useState(false)
  const activeConversationState = conversationState || contextDebug?.conversation_state || null
  const activeCompactedHistory = conversationCompactedHistory || null
  const activeHistoryLog = conversationHistoryLog || null
  const activeTurnStore = conversationTurnStore || null
  const activeToolLedger = conversationToolLedger || null
  const activeItemStream = conversationItemStream || null
  const activeContextSnapshots = conversationContextSnapshots || []
  const hasRunDebug = Boolean(contextDebug)
  const activeEvidenceLedger = activeConversationState?.evidence_ledger || []
  const latestTurns = useMemo(
    () =>
      [...(activeTurnStore?.entries || [])].sort((a, b) => {
        const aTime = new Date(a.started_at || a.completed_at || 0).getTime()
        const bTime = new Date(b.started_at || b.completed_at || 0).getTime()
        return bTime - aTime
      }),
    [activeTurnStore?.entries],
  )
  const latestTurn = latestTurns[0]
  const selectedTools = contextDebug?.selected_tools || []
  const responsePathLabel = selectedTools.length
    ? `可能调用 ${selectedTools.slice(0, 3).join('、')}`
    : contextDebug
      ? '优先直接回答'
      : '基于当前会话状态继续回答'
  const historyCarryLines = [
    activeCompactedHistory?.replacement_history?.length
      ? `替代历史 ${activeCompactedHistory.replacement_history.length} 条`
      : '',
    contextDebug?.recently_slid_messages_count
      ? `刚滑出的原文 ${contextDebug.recently_slid_messages_count} 条`
      : '',
    activeCompactedHistory?.history_anchors
      ? `历史锚点：${clipText(activeCompactedHistory.history_anchors, 100)}`
      : '',
    activeCompactedHistory?.history_summary
      ? `历史摘要：${clipText(activeCompactedHistory.history_summary, 100)}`
      : '',
  ].filter(Boolean)

  const summaryChips = useMemo(
    () =>
      contextDebug
        ? [
            {
              key: 'tokens',
              label: '上下文预算',
              value: `${contextDebug.estimated_tokens}/${contextDebug.effective_budget || contextDebug.budget}`,
            },
            {
              key: 'messages',
              label: '送入消息',
              value: `${contextDebug.message_count_sent}/${contextDebug.message_count_before_trim}`,
            },
            ...(contextDebug.model_context_window
              ? [
                  {
                    key: 'model-window',
                    label: '模型窗口',
                    value: `${contextDebug.model_context_window}`,
                  },
                ]
              : []),
            {
              key: 'window',
              label: '最近窗口',
              value: `${contextDebug.window_turns} turns`,
            },
            {
              key: 'slid',
              label: '刚滑出',
              value: `${contextDebug.recently_slid_messages_count ?? 0} 条`,
            },
            {
              key: 'memory',
              label: '记忆',
              value: contextDebug.memory_enabled ? `${contextDebug.memory_count} 条` : '关闭',
            },
            {
              key: 'cache',
              label: '前缀缓存',
              value: contextDebug.stable_prefix_cache_active
                ? `${contextDebug.stable_prefix_cache_hits || 0}/${contextDebug.stable_prefix_cache_misses || 0}`
                : '未命中',
            },
          ]
        : [],
    [contextDebug],
  )

  if (
    !contextDebug &&
    !activeConversationState &&
    !activeCompactedHistory &&
    !activeHistoryLog &&
    !activeTurnStore &&
    !activeToolLedger &&
    !activeItemStream &&
    activeContextSnapshots.length === 0
  ) {
    return null
  }

  return (
    <div className="pointer-events-none fixed right-4 bottom-[104px] z-40 w-[min(420px,calc(100vw-1.5rem))]">
      <motion.div
        layout
        className="pointer-events-auto overflow-hidden rounded-[24px] border border-white/[0.08] bg-slate-950/88 shadow-[0_20px_60px_rgba(2,6,23,0.42)] backdrop-blur-2xl"
      >
        <div
          role="button"
          tabIndex={0}
          onClick={() => setExpanded((value) => !value)}
          onKeyDown={(event) => {
            if (event.target !== event.currentTarget) return
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              setExpanded((value) => !value)
            }
          }}
          className="flex w-full items-center justify-between gap-3 border-b border-white/[0.06] bg-[linear-gradient(135deg,rgba(15,23,42,0.96),rgba(17,24,39,0.88))] px-4 py-3.5 text-left"
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <div className="flex h-9 w-9 items-center justify-center rounded-2xl border border-emerald-400/18 bg-emerald-500/10 text-emerald-200">
                <BranchesOutlined />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-semibold text-slate-100">上下文窗口</span>
                  <span
                    className={`rounded-full border px-2 py-0.5 text-[11px] ${
                      isSending
                        ? 'border-emerald-400/20 bg-emerald-500/10 text-emerald-200'
                        : 'border-slate-600/70 bg-slate-800/80 text-slate-300'
                    }`}
                  >
                    {isSending ? '实时' : hasRunDebug ? '最近一次' : '会话级'}
                  </span>
                  {contextDebug?.context_truncated ? (
                    <span className="rounded-full border border-amber-400/20 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-200">
                      已裁剪
                    </span>
                  ) : null}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  {contextDebug ? (
                    <>
                      <span>{formatIntentLabel(contextDebug.intent)}</span>
                      <span className="text-slate-600">•</span>
                      <span>{contextDebug.tool_choice === 'required' ? '工具必用' : '工具自动'}</span>
                      <span className="text-slate-600">•</span>
                      <span>第 {Math.max(1, contextDebug.iteration)} 轮</span>
                    </>
                  ) : (
                    <span>当前仅展示会话级上下文状态</span>
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {onManualCompact ? (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation()
                  void onManualCompact()
                }}
                disabled={isCompacting || isSending}
                className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-3 py-1.5 text-[11px] font-medium text-cyan-100 transition hover:border-cyan-300/28 hover:bg-cyan-500/14 disabled:cursor-not-allowed disabled:border-slate-700/70 disabled:bg-slate-800/60 disabled:text-slate-500"
              >
                {isCompacting ? '压缩中…' : '压缩会话'}
              </button>
            ) : null}
            <span className="flex h-8 w-8 items-center justify-center rounded-full border border-white/[0.08] bg-white/[0.04] text-slate-300">
              {expanded ? <UpOutlined className="text-[11px]" /> : <DownOutlined className="text-[11px]" />}
            </span>
          </div>
        </div>

        <AnimatePresence initial={false}>
          {expanded && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
              className="overflow-hidden"
            >
              <div className="max-h-[min(68vh,760px)] space-y-3 overflow-y-auto px-4 py-4">
                <Section title="这轮会带入什么" icon={<BranchesOutlined />}>
                  <div className="space-y-3">
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                      <div className="rounded-2xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
                        <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">回答方式</div>
                        <div className="mt-1 text-sm font-semibold text-slate-100">
                          {responsePathLabel}
                        </div>
                        <div className="mt-1 text-xs leading-5 text-slate-400">
                          {contextDebug?.carry_over_previous_goal
                            ? '会延续上一轮主题。'
                            : '会从当前输入开始组织回答。'}
                        </div>
                      </div>
                      <div className="rounded-2xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
                        <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">当前主题 / 目标</div>
                        <div className="mt-1 text-sm font-semibold text-slate-100">
                          {activeConversationState?.active_topic || '当前未形成稳定主题'}
                        </div>
                        <div className="mt-1 text-xs leading-5 text-slate-400">
                          {activeConversationState?.user_goal || '当前没有显式用户目标摘要。'}
                        </div>
                      </div>
                    </div>

                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
                      <div className="mb-1.5 text-[11px] uppercase tracking-[0.14em] text-slate-500">会继承的历史</div>
                      {historyCarryLines.length ? (
                        <div className="space-y-1.5 text-sm leading-6 text-slate-200">
                          {historyCarryLines.map((line) => (
                            <div key={line}>{line}</div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm leading-6 text-slate-500">这轮主要依赖最近消息和当前输入，不会带入很多压缩历史。</div>
                      )}
                    </div>

                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
                      <div className="mb-1.5 text-[11px] uppercase tracking-[0.14em] text-slate-500">关键证据</div>
                      {activeEvidenceLedger.length ? (
                        <div className="space-y-2">
                          {activeEvidenceLedger.slice(0, 4).map((item) => (
                            <div key={item.entry_id} className="rounded-xl border border-white/[0.06] bg-black/10 px-3 py-2">
                              <div className="text-sm leading-6 text-slate-100">{item.summary}</div>
                              <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-slate-500">
                                <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-0.5">
                                  {item.status === 'provisional' ? '暂定' : '已确认'}
                                </span>
                                {item.source_labels?.length ? (
                                  <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2 py-0.5 text-cyan-200">
                                    {item.source_labels.slice(0, 3).join(' / ')}
                                  </span>
                                ) : null}
                                {item.source_kind ? (
                                  <span className="rounded-full border border-fuchsia-400/18 bg-fuchsia-500/10 px-2 py-0.5 text-fuchsia-200">
                                    {item.source_kind}
                                  </span>
                                ) : null}
                                {item.tool_names?.length ? (
                                  <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2 py-0.5 text-emerald-200">
                                    {item.tool_names.slice(0, 2).join(' / ')}
                                  </span>
                                ) : null}
                                {item.result_count ? (
                                  <span className="rounded-full border border-white/[0.08] bg-slate-950/70 px-2 py-0.5">
                                    结果 {item.result_count}
                                  </span>
                                ) : null}
                              </div>
                              {item.provenance_hints?.length ? (
                                <div className="mt-1.5 text-[12px] leading-5 text-slate-400">
                                  {item.provenance_hints.slice(0, 2).join('；')}
                                </div>
                              ) : null}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm leading-6 text-slate-500">当前没有稳定可复用的证据结论。</div>
                      )}
                    </div>

                    <div className="rounded-2xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5">
                      <div className="mb-1.5 text-[11px] uppercase tracking-[0.14em] text-slate-500">最近一轮发生了什么</div>
                      {latestTurn ? (
                        <div className="space-y-1.5 text-sm leading-6 text-slate-200">
                          {latestTurn.user_content ? <div><span className="text-slate-400">用户：</span>{clipText(latestTurn.user_content, 120)}</div> : null}
                          {latestTurn.assistant_summary ? <div><span className="text-slate-400">助手：</span>{clipText(latestTurn.assistant_summary, 140)}</div> : null}
                          <div className="text-xs text-slate-500">
                            状态 {latestTurn.status} · iter {latestTurn.iteration_count} · tool {latestTurn.tool_call_count}/{latestTurn.tool_result_count}
                          </div>
                        </div>
                      ) : (
                        <div className="text-sm leading-6 text-slate-500">当前还没有稳定的回合记录。</div>
                      )}
                    </div>
                  </div>
                </Section>

                <div className="flex items-center justify-between rounded-2xl border border-white/[0.06] bg-white/[0.03] px-3 py-2.5 text-xs text-slate-400">
                  <span className="flex items-center gap-2">
                    <ClockCircleOutlined className="text-emerald-300" />
                    默认先展示对用户真正有用的上下文摘要
                  </span>
                  <button
                    type="button"
                    onClick={() => setDetailsExpanded((value) => !value)}
                    className="rounded-full border border-white/[0.08] bg-slate-900/70 px-3 py-1 text-[11px] text-slate-300 transition hover:border-white/[0.16] hover:text-white"
                  >
                    {detailsExpanded ? '收起调试细节' : '查看调试细节'}
                  </button>
                </div>

                {detailsExpanded ? (
                  <>
                <div className="grid grid-cols-2 gap-2">
                  {summaryChips.map((item) => (
                    <div
                      key={item.key}
                      className="rounded-2xl border border-white/[0.06] bg-white/[0.03] px-3 py-2.5"
                    >
                      <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">{item.label}</div>
                      <div className="mt-1 text-sm font-semibold text-slate-100">{item.value}</div>
                    </div>
                  ))}
                </div>

                <Section title="调度意图" icon={<RobotOutlined />} emptyText="当前没有可展示的意图信息。">
                  {contextDebug ? (
                    <div className="space-y-2">
                      <div className="text-sm text-slate-200">
                        <span className="text-slate-400">意图：</span>
                        {formatIntentLabel(contextDebug.intent)}
                      </div>
                      <div className="flex flex-wrap gap-2 text-[11px] text-slate-500">
                        <span className="rounded-full border border-white/[0.06] bg-slate-900/70 px-2.5 py-1">
                          路由: {contextDebug.routing_source === 'llm' ? 'LLM' : '规则'}
                        </span>
                        {contextDebug.carry_over_previous_goal ? (
                          <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-cyan-200">
                            延续上一轮主题
                          </span>
                        ) : null}
                      </div>
                      {contextDebug.intent_user_text ? (
                        <div className="rounded-xl border border-white/[0.06] bg-slate-900/70 px-3 py-2 text-sm leading-6 text-slate-300">
                          {contextDebug.intent_user_text}
                        </div>
                      ) : null}
                      {contextDebug.routing_reason ? (
                        <div className="rounded-xl border border-white/[0.06] bg-white/[0.03] px-3 py-2 text-sm leading-6 text-slate-400">
                          {contextDebug.routing_reason}
                        </div>
                      ) : null}
                      <div className="flex flex-wrap gap-2">
                        {(contextDebug.selected_tools || []).length > 0 ? (
                          contextDebug.selected_tools.map((tool) => (
                            <span
                              key={tool}
                              className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-xs text-emerald-200"
                            >
                              {tool}
                            </span>
                          ))
                        ) : (
                          <span className="text-sm text-slate-500">这一轮没有限定工具集合。</span>
                        )}
                      </div>
                    </div>
                  ) : null}
                </Section>

                <Section title="Skills" icon={<DatabaseOutlined />}>
                  {contextDebug ? (
                    <div className="space-y-3">
                      <div>
                        <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          已激活
                        </div>
                        <SkillMatchList
                          items={contextDebug.active_skills}
                          emptyText="当前没有命中的 skill。"
                          tone="active"
                        />
                      </div>
                      <div>
                        <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          候选清单
                        </div>
                        <SkillMatchList
                          items={contextDebug.available_skills}
                          emptyText="当前没有可用的 skill。"
                        />
                      </div>
                      <div className="text-xs leading-5 text-slate-500">
                        注入提示词预算约 {contextDebug.skill_prompt_tokens_estimate ?? 0} tokens
                      </div>
                    </div>
                  ) : null}
                </Section>

                <Section title="上下文层" icon={<HistoryOutlined />}>
                  <div className="space-y-3">
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        会话上下文状态
                      </div>
                      <SummaryBlock
                        text={
                          contextDebug?.conversation_state_summary ||
                          (activeConversationState
                            ? [
                                activeConversationState.active_topic
                                  ? `当前主题: ${activeConversationState.active_topic}`
                                  : '',
                                activeConversationState.user_goal
                                  ? `当前目标: ${activeConversationState.user_goal}`
                                  : '',
                                activeConversationState.constraints?.length
                                  ? `约束: ${activeConversationState.constraints.join('；')}`
                                  : '',
                                activeConversationState.open_questions?.length
                                  ? `未解决: ${activeConversationState.open_questions.join('；')}`
                                  : '',
                                activeConversationState.resolved_facts?.length
                                  ? `已确认: ${activeConversationState.resolved_facts.join('；')}`
                                  : '',
                                activeConversationState.evidence_ledger?.length
                                  ? `证据账本: ${formatEvidenceLedgerSummary(activeConversationState.evidence_ledger)}`
                                  : '',
                                activeConversationState.decision_state
                                  ? `决策态: ${formatDecisionStateSummary(activeConversationState.decision_state)}`
                                  : '',
                                activeConversationState.last_reasoning_summary
                                  ? `最近推理摘要: ${activeConversationState.last_reasoning_summary}`
                                  : '',
                              ]
                                .filter(Boolean)
                                .join('\n')
                            : ''
                          )
                        }
                      />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        刚滑出的原文
                      </div>
                      <div className="mb-2 text-xs text-slate-500">
                        这些消息已离开最近窗口，但仍以原文形式参与上下文。
                      </div>
                      <MessagePreviewList
                        messages={contextDebug?.recently_slid_messages}
                        emptyText="当前没有刚滑出的原文消息。"
                        lineClamp={2}
                      />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        关键历史锚点
                      </div>
                      <SummaryBlock text={contextDebug?.anchor_summary} />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        持久历史锚点
                      </div>
                      <SummaryBlock text={contextDebug?.persisted_anchor_summary || activeCompactedHistory?.history_anchors} />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        推理摘要
                      </div>
                      <SummaryBlock text={contextDebug?.reasoning_summary} />
                      {contextDebug?.reasoning_summary ? (
                        <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
                          {contextDebug.reasoning_summary_provider ? (
                            <span className="rounded-full border border-white/[0.06] bg-slate-900/70 px-2.5 py-1">
                              provider: {contextDebug.reasoning_summary_provider}
                            </span>
                          ) : null}
                          {contextDebug.reasoning_summary_model ? (
                            <span className="rounded-full border border-white/[0.06] bg-slate-900/70 px-2.5 py-1">
                              model: {contextDebug.reasoning_summary_model}
                            </span>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        持久摘要
                      </div>
                      <SummaryBlock text={contextDebug?.persisted_summary || activeCompactedHistory?.history_summary} />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        压缩边界与替代历史
                      </div>
                      {activeCompactedHistory?.replacement_history?.length ? (
                        <div className="space-y-2">
                          {activeCompactedHistory.compact_boundary_message_id ? (
                            <div className="text-xs text-slate-500">
                              compact boundary message id: {activeCompactedHistory.compact_boundary_message_id}
                            </div>
                          ) : null}
                          <MessagePreviewList
                            messages={activeCompactedHistory.replacement_history.map((item) => ({
                              role: item.role,
                              content: item.content,
                            }))}
                            emptyText="当前没有替代历史。"
                            lineClamp={2}
                          />
                        </div>
                      ) : (
                        <div className="text-sm text-slate-500">当前没有替代历史。</div>
                      )}
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        更早历史摘要
                      </div>
                      <SummaryBlock text={contextDebug?.older_history_summary} />
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        用户已确认偏好
                      </div>
                      <SummaryBlock
                        text={
                          contextDebug?.user_chat_preferences
                            ? [
                                contextDebug.user_chat_preferences.response_language
                                  ? `语言: ${contextDebug.user_chat_preferences.response_language}`
                                  : '',
                                contextDebug.user_chat_preferences.response_verbosity
                                  ? `详细度: ${contextDebug.user_chat_preferences.response_verbosity}`
                                  : '',
                                contextDebug.user_chat_preferences.web_search
                                  ? `联网: ${contextDebug.user_chat_preferences.web_search}`
                                  : '',
                              ]
                                .filter(Boolean)
                                .join('\n')
                            : ''
                        }
                      />
                    </div>
                  </div>
                </Section>

                <Section title="记忆与证据" icon={<DatabaseOutlined />}>
                  <div className="space-y-3">
                    <div className="flex flex-wrap gap-2">
                      {(contextDebug?.source_labels || []).length > 0 ? (
                        (contextDebug?.source_labels || []).map((item) => (
                          <span
                            key={item}
                            className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-xs text-cyan-200"
                          >
                            {item}
                          </span>
                        ))
                      ) : (
                        <span className="text-sm text-slate-500">当前没有知识库来源标签。</span>
                      )}
                    </div>
                    {(contextDebug?.successful_knowledge_queries || []).length > 0 ? (
                      <div className="space-y-2">
                        <div className="text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          已成功检索的 Query
                        </div>
                        {(contextDebug?.successful_knowledge_queries || []).map((query) => (
                          <div
                            key={query}
                            className="rounded-xl border border-white/[0.06] bg-slate-900/70 px-3 py-2 text-sm leading-6 text-slate-300"
                          >
                            {query}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {(contextDebug?.memory_lines || []).length > 0 ? (
                      <div className="space-y-2">
                        <div className="text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          Memory Recall
                        </div>
                        {(contextDebug?.memory_lines || []).map((line, index) => (
                          <div
                            key={`${index}-${line}`}
                            className="rounded-xl border border-white/[0.06] bg-slate-900/70 px-3 py-2 text-sm leading-6 text-slate-300"
                          >
                            {line}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </Section>

                <Section title="工具账本" icon={<DatabaseOutlined />}>
                  {activeToolLedger?.entries?.length ? (
                    <div className="space-y-2">
                      {activeToolLedger.entries.slice(-8).reverse().map((entry, index) => (
                        <div
                          key={`${entry.entry_id}-${index}`}
                          className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
                        >
                          <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-slate-500">
                            <span className="rounded-full border border-white/[0.08] px-2 py-0.5 text-[10px] text-slate-300">
                              {entry.kind}
                            </span>
                            <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-200">
                              {entry.tool_name}
                            </span>
                            {entry.status ? <span>{entry.status}</span> : null}
                            {typeof entry.iteration === 'number' ? <span>iter {entry.iteration}</span> : null}
                          </div>
                          <div className="text-sm leading-6 text-slate-200">
                            {entry.summary || '当前账本项没有额外摘要。'}
                          </div>
                          <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
                            {entry.permission_required ? (
                              <span className="rounded-full border border-amber-400/18 bg-amber-500/10 px-2 py-1 text-amber-200">
                                需要授权
                              </span>
                            ) : null}
                            {typeof entry.success === 'boolean' ? (
                              <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                {entry.success ? 'success' : 'failed'}
                              </span>
                            ) : null}
                            {typeof entry.execution_time_ms === 'number' ? (
                              <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                {Math.round(entry.execution_time_ms)} ms
                              </span>
                            ) : null}
                            {entry.created_at ? (
                              <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                {entry.created_at}
                              </span>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-sm text-slate-500">当前会话还没有持久化的工具账本。</div>
                  )}
                </Section>

                <Section title="事件流" icon={<BranchesOutlined />}>
                  {activeItemStream?.entries?.length ? (
                    <div className="space-y-2">
                      {activeItemStream.entries.slice(-10).reverse().map((entry, index) => (
                        <div
                          key={`${entry.item_id}-${index}`}
                          className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
                        >
                          <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-slate-500">
                            <span className="rounded-full border border-white/[0.08] px-2 py-0.5 text-[10px] text-slate-300">
                              {entry.kind}
                            </span>
                            {entry.turn_id ? (
                              <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-0.5 text-[10px] text-slate-300">
                                {entry.turn_id}
                              </span>
                            ) : null}
                            {entry.tool_name ? (
                              <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-200">
                                {entry.tool_name}
                              </span>
                            ) : null}
                            {entry.status ? <span>{entry.status}</span> : null}
                            {typeof entry.iteration === 'number' ? <span>iter {entry.iteration}</span> : null}
                          </div>
                          <div className="text-sm leading-6 text-slate-200">
                            {entry.summary || entry.content || entry.thought || '当前事件没有额外摘要。'}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="text-sm text-slate-500">当前会话还没有 item 事件流。</div>
                  )}
                </Section>

                <Section title="会话演进" icon={<ClockCircleOutlined />}>
                  <div className="space-y-3">
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        最近回合
                      </div>
                      {activeTurnStore?.entries?.length ? (
                        <div className="space-y-2">
                          {activeTurnStore.entries.slice(-6).reverse().map((entry) => (
                            <div
                              key={entry.turn_id}
                              className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
                            >
                              <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-slate-500">
                                <span className="rounded-full border border-white/[0.08] px-2 py-0.5 text-[10px] text-slate-300">
                                  {entry.turn_id}
                                </span>
                                <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2 py-0.5 text-[10px] text-cyan-200">
                                  {entry.status}
                                </span>
                                <span>iter {entry.iteration_count}</span>
                                <span>call {entry.tool_call_count}</span>
                                <span>result {entry.tool_result_count}</span>
                              </div>
                              <div className="space-y-1 text-sm leading-6 text-slate-300">
                                {entry.user_content ? <div>用户: {entry.user_content}</div> : null}
                                {entry.assistant_summary ? <div>助手: {entry.assistant_summary}</div> : null}
                                {entry.error_message ? <div className="text-rose-300">错误: {entry.error_message}</div> : null}
                              </div>
                              <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
                                {entry.run_id ? (
                                  <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                    run: {entry.run_id}
                                  </span>
                                ) : null}
                                {entry.started_at ? (
                                  <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                    started: {entry.started_at}
                                  </span>
                                ) : null}
                                {entry.completed_at ? (
                                  <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                    completed: {entry.completed_at}
                                  </span>
                                ) : null}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm text-slate-500">当前还没有持久化的回合记录。</div>
                      )}
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        历史事件
                      </div>
                      {activeHistoryLog?.events?.length ? (
                        <div className="space-y-2">
                          {activeHistoryLog.events.slice(-6).reverse().map((event, index) => (
                            <div
                              key={`${event.title}-${event.created_at || index}`}
                              className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
                            >
                              <div className="mb-1 flex items-center justify-between gap-3">
                                <div className="text-sm font-medium text-slate-100">{event.title}</div>
                                {event.created_at ? (
                                  <div className="text-[11px] text-slate-500">{event.created_at}</div>
                                ) : null}
                              </div>
                              <div className="text-sm leading-6 text-slate-300">{event.detail}</div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm text-slate-500">当前还没有会话级历史事件。</div>
                      )}
                    </div>
                    <div>
                      <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                        最近快照
                      </div>
                      {activeContextSnapshots.length ? (
                        <div className="space-y-2">
                          {activeContextSnapshots.slice(-3).reverse().map((snapshot, index) => (
                            <div
                              key={`${snapshot.created_at || index}-${snapshot.mode || 'snapshot'}`}
                              className="rounded-xl border border-white/[0.06] bg-slate-900/72 px-3 py-2.5"
                            >
                              <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-slate-500">
                                <span className="rounded-full border border-white/[0.08] px-2 py-0.5 text-[10px] text-slate-300">
                                  {snapshot.mode || 'snapshot'}
                                </span>
                                {snapshot.created_at ? <span>{snapshot.created_at}</span> : null}
                              </div>
                              <div className="text-sm leading-6 text-slate-300">
                                {snapshot.summary_text || '当前快照没有额外摘要。'}
                              </div>
                              <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-500">
                                <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                  compacted: {snapshot.compacted_message_count}
                                </span>
                                {snapshot.up_to_message_id ? (
                                  <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2 py-1">
                                    up_to_message_id: {snapshot.up_to_message_id}
                                  </span>
                                ) : null}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="text-sm text-slate-500">当前还没有上下文快照。</div>
                      )}
                    </div>
                  </div>
                </Section>

                <Section title="送入模型的消息" icon={<MessageOutlined />}>
                  <div className="space-y-2">
                    <div className="text-xs text-slate-500">
                      {contextDebug
                        ? `实际送入模型 ${contextDebug.message_count_sent} 条；包含最近窗口、刚滑出的原文和压缩后的历史层。每条最多展示三行。`
                        : '当前没有最近一次运行快照，这里只展示会话级状态。'}
                    </div>
                    <MessagePreviewList
                      messages={contextDebug?.recent_messages}
                      emptyText="当前没有送入模型的消息快照。"
                    />
                  </div>
                </Section>

                <Section title="真实发送内容" icon={<MessageOutlined />}>
                  {contextDebug ? (
                    <div className="space-y-3">
                      <div className="flex flex-wrap gap-2 text-[11px] text-slate-500">
                        {contextDebug.model_request_mode ? (
                          <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-cyan-200">
                            mode: {contextDebug.model_request_mode}
                          </span>
                        ) : null}
                        <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2.5 py-1">
                          messages: {contextDebug.model_messages_raw?.length || 0}
                        </span>
                        <span className="rounded-full border border-white/[0.06] bg-slate-950/70 px-2.5 py-1">
                          tools: {contextDebug.model_tool_schemas_raw?.length || 0}
                        </span>
                      </div>

                      <div>
                        <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          System Prompt
                        </div>
                        <FullTextBlock
                          text={contextDebug.model_system_prompt}
                          emptyText="当前没有 system prompt 快照。"
                        />
                      </div>

                      <div>
                        <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          Messages
                        </div>
                        {contextDebug.model_messages_raw?.length ? (
                          <div className="space-y-2">
                            {contextDebug.model_messages_raw.map((message, index) => (
                              <div
                                key={`raw-message-${index}`}
                                className="rounded-xl border border-white/[0.06] bg-black/10 p-2.5"
                              >
                                <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">
                                  #{index + 1} {String(message.role || 'unknown')}
                                </div>
                                <RawObjectBlock value={message} emptyText="空消息" />
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="text-sm text-slate-500">当前没有 message payload 快照。</div>
                        )}
                      </div>

                      <div>
                        <div className="mb-1.5 text-xs font-medium uppercase tracking-[0.14em] text-slate-500">
                          Tool Schemas
                        </div>
                        {contextDebug.model_tool_schemas_raw?.length ? (
                          <div className="space-y-2">
                            {contextDebug.model_tool_schemas_raw.map((schema, index) => (
                              <div
                                key={`tool-schema-${index}`}
                                className="rounded-xl border border-white/[0.06] bg-black/10 p-2.5"
                              >
                                <div className="mb-1 text-[11px] uppercase tracking-[0.14em] text-slate-500">
                                  #{index + 1}{' '}
                                  {String(
                                    (schema.function as Record<string, unknown> | undefined)?.name ||
                                      schema.name ||
                                      'tool',
                                  )}
                                </div>
                                <RawObjectBlock value={schema} emptyText="空 schema" />
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="text-sm text-slate-500">当前请求没有附带 tool schema。</div>
                        )}
                      </div>
                    </div>
                  ) : null}
                </Section>
                  </>
                ) : null}

                <div className="flex items-center justify-between rounded-2xl border border-white/[0.06] bg-white/[0.03] px-3 py-2.5 text-xs text-slate-400">
                  <span className="flex items-center gap-2">
                    <ClockCircleOutlined className="text-emerald-300" />
                    {contextDebug ? '这个窗口展示的是本轮送入模型的上下文分层快照' : '这个窗口展示的是会话级上下文状态'}
                  </span>
                  <span className="text-slate-500">{contextDebug?.version || activeConversationState?.version || 'n/a'}</span>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

export default ContextDebugWindow
