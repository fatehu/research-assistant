import {
  ClockCircleOutlined,
  CompressOutlined,
} from '@ant-design/icons'
import type {
  ConversationItemStream,
  ConversationItemStreamEntry,
  ToolWorkflowSummary,
  ConversationToolLedger,
  ConversationTurnEntry,
  ConversationTurnStore,
  Message,
  MessageSpanRewriteResponse,
} from '@/services/api'
import type { IterationStep, SendPhase } from '@/stores/chatStore'
import MessageBubble from './MessageBubble'
import ReActPanel from './ReActPanel'
import HistoryReActPanel from './HistoryReActPanel'

interface TurnTimelineProps {
  messages: Message[]
  turnStore?: ConversationTurnStore | null
  itemStream?: ConversationItemStream | null
  toolLedger?: ConversationToolLedger | null
  highlightedMessageId?: number | null
  activeTurnId?: string | null
  isSending?: boolean
  sendPhase?: SendPhase
  sendPhaseLabel?: string | null
  sendPhaseHint?: string | null
  isThinking?: boolean
  streamingContent?: string
  streamingThought?: string
  iterationSteps?: IterationStep[]
  currentIteration?: number
  currentToolCall?: { tool: string; input: Record<string, any> } | null
  onRewriteSpan?: (
    messageId: number,
    payload: {
      instruction: string
      selected_text: string
      before_context?: string
      after_context?: string
      occurrence_index?: number
    },
  ) => Promise<MessageSpanRewriteResponse>
}

const getSendPhaseCopy = (
  phase: Exclude<SendPhase, 'idle'>,
  hasConversationHistory: boolean,
): { title: string; description: string } => {
  if (phase === 'submitting') {
    return hasConversationHistory
      ? { title: '消息已发送', description: '正在把这轮消息接入当前会话。' }
      : { title: '首条消息已发送', description: '正在建立连接，准备开始这轮处理。' }
  }
  if (phase === 'planning') {
    return hasConversationHistory
      ? { title: '正在整理本轮上下文', description: '系统正在读取会话状态、替代历史和偏好。' }
      : { title: '正在准备本轮请求', description: '首轮不会整理很多历史，正在判断回答路径并组装请求。' }
  }
  if (phase === 'loading_context') {
    return hasConversationHistory
      ? { title: '正在读取会话状态', description: '系统正在读取会话状态、替代历史和用户偏好。' }
      : { title: '正在准备本轮请求', description: '首轮不会整理很多历史，正在建立这次请求。' }
  }
  if (phase === 'routing') {
    return { title: '正在判断回答路径', description: '系统正在判断这轮是直接回答，还是需要进一步行动。' }
  }
  if (phase === 'waiting_model') {
    return { title: '回答路径已确定', description: '请求已经发给主模型，通常很快会开始返回内容。' }
  }
  if (phase === 'thinking') {
    return hasConversationHistory
      ? { title: '正在结合历史分析问题', description: '已经进入模型处理阶段，正在组织回答或规划下一步动作。' }
      : { title: '正在分析你的问题', description: '已经进入模型处理阶段，正在组织首轮回答。' }
  }
  if (phase === 'tool') {
    return { title: '正在调用工具', description: '需要外部信息或执行步骤，结果返回后会继续生成答案。' }
  }
  return { title: '正在输出回答', description: '已经开始回传内容，继续等待即可看到完整结果。' }
}

const formatTime = (value?: string): string => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

const sortTurns = (entries: ConversationTurnEntry[]): ConversationTurnEntry[] =>
  [...entries].sort((a, b) => {
    const aValue = new Date(a.started_at || a.completed_at || 0).getTime()
    const bValue = new Date(b.started_at || b.completed_at || 0).getTime()
    return aValue - bValue
  })

const sortItems = (entries: ConversationItemStreamEntry[]): ConversationItemStreamEntry[] =>
  [...entries].sort((a, b) => {
    const aTime = new Date(a.created_at || 0).getTime()
    const bTime = new Date(b.created_at || 0).getTime()
    if (aTime !== bTime) return aTime - bTime
    return (a.iteration || 0) - (b.iteration || 0)
  })

const turnStatusLabel = (value: string): string => {
  const normalized = String(value || '').trim().toLowerCase()
  if (normalized === 'completed') return '已完成'
  if (normalized === 'running') return '运行中'
  if (normalized === 'error') return '失败'
  if (normalized === 'stopped') return '已停止'
  return normalized || '未知'
}

const normalizeToolInput = (value: Record<string, any> | undefined | null): string => {
  try {
    return JSON.stringify(value || {})
  } catch {
    return ''
  }
}

const hasRenderableAssistantMessage = (message: Message | undefined): boolean => {
  if (!message) return false
  if (String(message.content || '').trim()) return true
  if (String(message.thought || '').trim()) return true
  return false
}

const buildHistorySteps = (
  items: ConversationItemStreamEntry[],
): Array<{
  type: string
  iteration: number
  content?: string
  tool?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  workflowSummary?: ToolWorkflowSummary
  rawContent?: string
}> => {
  const steps: Array<{
    type: string
    iteration: number
    content?: string
    tool?: string
    input?: Record<string, unknown>
    output?: string
    success?: boolean
    workflowSummary?: ToolWorkflowSummary
    rawContent?: string
  }> = []

  for (const item of items) {
    const kind = String(item.kind || '').trim().toLowerCase()
    if (kind === 'tool_use_summary') {
      steps.push({
        type: 'workflow',
        iteration: item.iteration || 0,
        content: item.summary || item.content || '',
        rawContent: item.content || item.summary || '',
        workflowSummary:
          item.metadata?.workflow_summary && typeof item.metadata.workflow_summary === 'object'
            ? (item.metadata.workflow_summary as ToolWorkflowSummary)
            : undefined,
      })
      continue
    }
    if (kind === 'permission_denial') {
      steps.push({
        type: 'workflow',
        iteration: item.iteration || 0,
        content: item.summary || item.content || '',
        rawContent: item.content || item.summary || '',
        workflowSummary: {
          version: 'tool_workflow_summary.v1',
          headline: '权限受限，流程已等待',
          status: 'waiting',
          highlights: [item.summary || item.content || '当前步骤需要额外授权后才能继续。'],
          next_action: '等待授权或调整执行路径',
        },
      })
      continue
    }
    if (kind === 'reasoning_summary') {
      steps.push({
        type: 'thought',
        iteration: item.iteration || 0,
        content: item.summary || item.content || '',
      })
      continue
    }
    if (kind === 'tool_call') {
      steps.push({
        type: 'action',
        iteration: item.iteration || 0,
        tool: item.tool_name,
        input: item.arguments,
      })
      continue
    }
    if (kind === 'tool_result') {
      steps.push({
        type: 'observation',
        iteration: item.iteration || 0,
        tool: item.tool_name,
        output: item.summary || item.error || '',
        success: item.success,
      })
    }
  }

  return steps
}

const CompactBoundaryPanel = ({ item }: { item: ConversationItemStreamEntry }) => {
  const replacementHistory = Array.isArray(item.metadata?.replacement_history)
    ? item.metadata.replacement_history
    : []
  const replacementCount = Number(replacementHistory.length || 0)

  return (
    <div className="relative overflow-hidden rounded-xl border border-cyan-500/20 bg-slate-900/70 backdrop-blur-sm">
      <div className="flex items-center justify-between gap-3 px-4 py-2.5">
        <div className="flex items-center gap-3">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg border border-cyan-300/30 bg-gradient-to-br from-cyan-500/80 to-blue-500/80 shadow-sm shadow-cyan-500/20">
            <CompressOutlined className="text-[11px] text-white" />
          </div>
          <div>
            <div className="text-sm font-medium text-slate-100">压缩边界</div>
            <div className="mt-0.5 text-xs text-cyan-200/80">
              {replacementCount > 0 ? `replacement history ${replacementCount} 条` : '历史已被压缩为替代历史'}
            </div>
          </div>
        </div>
      </div>
      <div className="border-t border-cyan-500/20 px-4 py-3">
        <div className="text-xs leading-relaxed text-slate-300">
          {item.summary || item.content || '历史已被压缩为替代历史。'}
        </div>
      </div>
    </div>
  )
}

const PendingTurnPanel = ({
  phase = 'submitting',
  phaseLabel = null,
  phaseHint = null,
  hasConversationHistory = false,
}: {
  phase?: Exclude<SendPhase, 'idle'>
  phaseLabel?: string | null
  phaseHint?: string | null
  hasConversationHistory?: boolean
}) => {
  const copy =
    phaseLabel && phaseHint
      ? { title: phaseLabel, description: phaseHint }
      : getSendPhaseCopy(phase, hasConversationHistory)

  return (
    <div className="overflow-hidden rounded-2xl border border-emerald-400/14 bg-[#13151A] px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)]">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex h-8 w-8 items-center justify-center rounded-xl border border-emerald-400/18 bg-emerald-500/10">
          <ClockCircleOutlined className="animate-pulse text-sm text-emerald-300" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-slate-100">{copy.title}</span>
            <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-100">
              处理中
            </span>
          </div>
          <div className="mt-1 text-sm leading-6 text-slate-400">{copy.description}</div>
        </div>
      </div>
    </div>
  )
}

const TurnTimeline = ({
  messages,
  turnStore,
  itemStream,
  toolLedger,
  highlightedMessageId,
  activeTurnId,
  isSending = false,
  sendPhase = 'idle',
  sendPhaseLabel = null,
  sendPhaseHint = null,
  isThinking = false,
  streamingContent = '',
  streamingThought = '',
  iterationSteps = [],
  currentIteration = 0,
  currentToolCall = null,
  onRewriteSpan,
}: TurnTimelineProps) => {
  const persistedTurns = turnStore?.entries || []
  const trimmedActiveTurnId = String(activeTurnId || '').trim()
  const hasPersistedActiveTurn = Boolean(
    trimmedActiveTurnId && persistedTurns.some((turn) => String(turn.turn_id || '').trim() === trimmedActiveTurnId),
  )
  const latestUserMessage = [...messages].reverse().find((msg) => msg.role === 'user')
  const shouldRenderActiveTurn = Boolean(
    isSending && latestUserMessage && (!trimmedActiveTurnId || !hasPersistedActiveTurn),
  )
  const liveStepCount = iterationSteps.length + (isThinking ? 1 : 0) + (currentToolCall ? 1 : 0)

  if (!persistedTurns.length && !shouldRenderActiveTurn) {
    return null
  }

  const messageMap = new Map<number, Message>()
  for (const msg of messages) {
    messageMap.set(msg.id, msg)
  }

  const turns = sortTurns(persistedTurns)
  const itemsByTurn = new Map<string, ConversationItemStreamEntry[]>()
  for (const entry of itemStream?.entries || []) {
    if (!entry.turn_id) continue
    const current = itemsByTurn.get(entry.turn_id) || []
    current.push(entry)
    itemsByTurn.set(entry.turn_id, current)
  }

  return (
    <div className="space-y-6">
      {turns.map((turn) => {
        const turnItems = sortItems(itemsByTurn.get(turn.turn_id) || []).filter((entry) => {
          const kind = String(entry.kind || '').trim().toLowerCase()
          return !['user_message', 'assistant_message', 'stopped_assistant_message', 'message', 'system_message'].includes(kind)
        })
        const compactBoundaryItems = turnItems.filter(
          (entry) => String(entry.kind || '').trim().toLowerCase() === 'compact_boundary',
        )
        const reactHistorySteps = buildHistorySteps(
          turnItems.filter((entry) => String(entry.kind || '').trim().toLowerCase() !== 'compact_boundary'),
        )
        const userMessage = turn.user_message_id ? messageMap.get(turn.user_message_id) : undefined
        const assistantMessage = turn.assistant_message_id ? messageMap.get(turn.assistant_message_id) : undefined
        const renderableAssistantMessage = hasRenderableAssistantMessage(assistantMessage)
        const turnTime = formatTime(turn.started_at || turn.completed_at)
        const isRunningTurn = String(turn.status || '').trim().toLowerCase() === 'running'
        const shouldRenderTurnPending =
          isRunningTurn &&
          !assistantMessage &&
          !turn.assistant_summary &&
          reactHistorySteps.length === 0 &&
          compactBoundaryItems.length === 0

        return (
          <div
            key={turn.turn_id}
            className="rounded-[28px] border border-white/[0.05] bg-[linear-gradient(180deg,rgba(15,23,42,0.82),rgba(2,6,23,0.72))] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03),0_14px_30px_rgba(2,6,23,0.18)]"
          >
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] uppercase tracking-[0.14em] text-slate-300">
                回合
              </span>
              <span className="rounded-full border border-cyan-400/18 bg-cyan-500/10 px-2.5 py-1 text-[11px] text-cyan-100">
                {turnStatusLabel(turn.status)}
              </span>
              {turnTime ? (
                <span className="inline-flex items-center gap-1 rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
                  <ClockCircleOutlined />
                  {turnTime}
                </span>
              ) : null}
              <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
                items {turnItems.length}
              </span>
              <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
                tool {turn.tool_call_count}/{turn.tool_result_count}
              </span>
            </div>

            <div className="space-y-4">
              {userMessage ? (
                <MessageBubble
                  msg={userMessage}
                  turnStore={turnStore || undefined}
                  itemStream={itemStream || undefined}
                  toolLedger={toolLedger || undefined}
                  showHistoryPrelude={false}
                  isHighlighted={highlightedMessageId === userMessage.id}
                  onRewriteSpan={onRewriteSpan}
                />
              ) : turn.user_content ? (
                <div className="rounded-2xl border border-white/[0.06] bg-slate-800/60 px-4 py-3 text-sm leading-6 text-slate-100">
                  {turn.user_content}
                </div>
              ) : null}

              {reactHistorySteps.length ? <HistoryReActPanel steps={reactHistorySteps} defaultExpanded embedded /> : null}

              {compactBoundaryItems.length ? (
                <div className="space-y-3">
                  {compactBoundaryItems.map((item) => (
                    <CompactBoundaryPanel key={item.item_id} item={item} />
                  ))}
                </div>
              ) : null}

              {shouldRenderTurnPending ? (
                <PendingTurnPanel
                  phase={
                    trimmedActiveTurnId && turn.turn_id === trimmedActiveTurnId && sendPhase !== 'idle'
                      ? sendPhase
                      : 'planning'
                  }
                  phaseLabel={trimmedActiveTurnId && turn.turn_id === trimmedActiveTurnId ? sendPhaseLabel : null}
                  phaseHint={trimmedActiveTurnId && turn.turn_id === trimmedActiveTurnId ? sendPhaseHint : null}
                  hasConversationHistory={turns.length > 1}
                />
              ) : null}

              {renderableAssistantMessage && assistantMessage ? (
                <MessageBubble
                  msg={assistantMessage}
                  turnStore={turnStore || undefined}
                  itemStream={itemStream || undefined}
                  toolLedger={toolLedger || undefined}
                  showHistoryPrelude={false}
                  isHighlighted={highlightedMessageId === assistantMessage.id}
                  onRewriteSpan={onRewriteSpan}
                />
              ) : turn.assistant_summary ? (
                <div className="rounded-2xl border border-white/[0.06] bg-slate-900/60 px-4 py-3 text-sm leading-6 text-slate-200">
                  {turn.assistant_summary}
                </div>
              ) : null}
            </div>
          </div>
        )
      })}

      {shouldRenderActiveTurn && latestUserMessage ? (
        <div className="rounded-[28px] border border-emerald-400/14 bg-[linear-gradient(180deg,rgba(15,23,42,0.82),rgba(2,6,23,0.72))] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.03),0_14px_30px_rgba(2,6,23,0.18)]">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] uppercase tracking-[0.14em] text-slate-300">
              当前回合
            </span>
            <span className="rounded-full border border-emerald-400/18 bg-emerald-500/10 px-2.5 py-1 text-[11px] text-emerald-100">
              {isThinking || currentToolCall ? '运行中' : '生成中'}
            </span>
            <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
              items {liveStepCount}
            </span>
            {currentIteration > 0 ? (
              <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-[11px] text-slate-400">
                第 {currentIteration} 轮
              </span>
            ) : null}
          </div>

          <div className="space-y-4">
            <MessageBubble
              msg={latestUserMessage}
              turnStore={turnStore || undefined}
              itemStream={itemStream || undefined}
              toolLedger={toolLedger || undefined}
              showHistoryPrelude={false}
              isHighlighted={highlightedMessageId === latestUserMessage.id}
              onRewriteSpan={onRewriteSpan}
            />

            <ReActPanel
              steps={iterationSteps}
              currentIteration={currentIteration}
              isThinking={isThinking}
              currentThought={streamingThought}
              currentToolCall={currentToolCall}
            />

            {!iterationSteps.length && !isThinking && !currentToolCall && !streamingContent ? (
              <PendingTurnPanel
                phase={sendPhase === 'idle' ? 'submitting' : sendPhase}
                phaseLabel={sendPhaseLabel}
                phaseHint={sendPhaseHint}
                hasConversationHistory={persistedTurns.length > 0}
              />
            ) : null}

            {streamingContent ? (
              <MessageBubble
                msg={{
                  id: -1,
                  conversation_id: latestUserMessage.conversation_id,
                  role: 'assistant',
                  content: streamingContent,
                  message_type: 'text',
                  created_at: new Date().toISOString(),
                }}
                isStreaming={true}
                streamingContent={streamingContent}
                streamingThought=""
                isThinking={false}
                showHistoryPrelude={false}
                onRewriteSpan={onRewriteSpan}
              />
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}

export default TurnTimeline
