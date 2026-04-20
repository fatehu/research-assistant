import { create } from 'zustand'
import {
  chatApi,
  type ChatContextDebug,
  type ChatWorkflowControl,
  type ChatSkillLaunchRequest,
  type ChatRagOverrides,
  type ChatUserPreferences,
  Conversation,
  Message,
  type MessageMetadata,
  type ConversationHistoryLog,
  type ConversationContextSnapshot,
  type ConversationContextState,
  type ConversationCompactedHistory,
  type ConversationTurnStore,
  type ConversationToolLedger,
  type ConversationItemStream,
  type MessageSpanRewriteResponse,
} from '@/services/api'
import { handleApiError } from '@/utils/apiErrorHandler'

// 工具调用信息
export interface ToolCall {
  tool: string
  input: Record<string, any>
  output?: string
  success?: boolean
  timestamp: number
}

// 迭代步骤
export interface IterationStep {
  type: 'thought' | 'action' | 'observation'
  content: string
  tool?: string
  toolInput?: Record<string, any>
  toolOutput?: string
  success?: boolean
  timestamp: number
}

export type SendPhase =
  | 'idle'
  | 'submitting'
  | 'planning'
  | 'loading_context'
  | 'routing'
  | 'waiting_model'
  | 'thinking'
  | 'tool'
  | 'answering'

const assistantSummaryText = (content: string, limit = 160): string => {
  const normalized = String(content || '').replace(/\s+/g, ' ').trim()
  if (normalized.length <= limit) return normalized
  return `${normalized.slice(0, Math.max(1, limit - 1)).trimEnd()}…`
}

const resolveAssistantTurnEntry = (
  turnStore: ConversationTurnStore | undefined,
  currentTurnId: string | null,
) => {
  const entries = Array.isArray(turnStore?.entries) ? turnStore.entries : []
  if (!entries.length) return undefined
  if (currentTurnId) {
    const matched = entries.find((entry) => entry.turn_id === currentTurnId)
    if (matched) return matched
  }
  return entries[entries.length - 1]
}

const resolveAssistantItemEntry = (
  itemStream: ConversationItemStream | undefined,
  currentTurnId: string | null,
  assistantMessageId?: number,
) => {
  const entries = Array.isArray(itemStream?.entries) ? itemStream.entries : []
  if (!entries.length) return undefined

  if (assistantMessageId) {
    const byMessageId = entries.find(
      (entry) => entry.role === 'assistant' && entry.message_id === assistantMessageId,
    )
    if (byMessageId) return byMessageId
  }

  const assistantEntries = entries.filter((entry) => entry.role === 'assistant')
  if (!assistantEntries.length) return undefined

  if (currentTurnId) {
    const sameTurnEntries = assistantEntries.filter((entry) => entry.turn_id === currentTurnId)
    if (sameTurnEntries.length) return sameTurnEntries[sameTurnEntries.length - 1]
  }

  return assistantEntries[assistantEntries.length - 1]
}

const buildDoneAssistantMessage = ({
  itemStream,
  turnStore,
  currentTurnId,
  fallbackContent,
  fallbackThought,
  fallbackMetadata,
  conversationId,
}: {
  itemStream?: ConversationItemStream
  turnStore?: ConversationTurnStore
  currentTurnId: string | null
  fallbackContent: string
  fallbackThought?: string
  fallbackMetadata?: MessageMetadata
  conversationId: number
}): Message | null => {
  const turnEntry = resolveAssistantTurnEntry(turnStore, currentTurnId)
  const assistantMessageId =
    typeof turnEntry?.assistant_message_id === 'number' && Number.isFinite(turnEntry.assistant_message_id)
      ? turnEntry.assistant_message_id
      : undefined
  const itemEntry = resolveAssistantItemEntry(itemStream, currentTurnId, assistantMessageId)
  const content = String(itemEntry?.content || fallbackContent || '')
  const thought = String(itemEntry?.thought || fallbackThought || '').trim()
  const resolvedMessageId =
    typeof itemEntry?.message_id === 'number' && Number.isFinite(itemEntry.message_id)
      ? itemEntry.message_id
      : assistantMessageId

  if (!content.trim() && !thought) return null

  return {
    id: resolvedMessageId || Date.now() + 1,
    conversation_id: conversationId,
    role: 'assistant',
    content,
    message_type: 'text',
    thought: thought || undefined,
    metadata:
      itemEntry?.metadata && typeof itemEntry.metadata === 'object'
        ? (itemEntry.metadata as MessageMetadata)
        : fallbackMetadata,
    created_at: itemEntry?.created_at || new Date().toISOString(),
  }
}

const resolveConversationWorkflowControl = (messages: Message[] | undefined): ChatWorkflowControl | null => {
  const candidates = Array.isArray(messages) ? messages : []
  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const message = candidates[index]
    const workflowControl =
      message?.role === 'assistant' &&
      message.metadata &&
      typeof message.metadata === 'object' &&
      (message.metadata.workflow_control as ChatWorkflowControl | undefined)
    if (workflowControl && typeof workflowControl === 'object') {
      return workflowControl
    }
  }
  return null
}

const applySpanRewriteToConversation = (
  conversation: Conversation | null,
  response: MessageSpanRewriteResponse,
): Conversation | null => {
  if (!conversation) return conversation
  const messageId = response.message.id
  const nextItemStream = conversation.item_stream
    ? {
        ...conversation.item_stream,
        entries: conversation.item_stream.entries.map((entry) =>
          entry.message_id === messageId && entry.role === 'assistant'
            ? {
                ...entry,
                content: response.new_content,
                metadata: response.message.metadata || entry.metadata,
              }
            : entry,
        ),
      }
    : conversation.item_stream
  const nextTurnStore = conversation.turn_store
    ? {
        ...conversation.turn_store,
        entries: conversation.turn_store.entries.map((entry) =>
          entry.assistant_message_id === messageId
            ? {
                ...entry,
                assistant_summary: assistantSummaryText(response.new_content),
              }
            : entry,
        ),
      }
    : conversation.turn_store

  return {
    ...conversation,
    ...(nextItemStream ? { item_stream: nextItemStream } : {}),
    ...(nextTurnStore ? { turn_store: nextTurnStore } : {}),
  }
}

interface ChatState {
  // 对话列表
  conversations: Conversation[]
  currentConversation: Conversation | null
  messages: Message[]

  // 加载状态
  isLoading: boolean
  isSending: boolean
  isLoadingList: boolean  // 对话列表加载状态
  isCompactingContext: boolean

  // 流式响应状态
  sendPhase: SendPhase
  sendPhaseLabel: string | null
  sendPhaseHint: string | null
  streamingContent: string
  streamingThought: string
  streamingContextDebug: ChatContextDebug | null
  lastRunContextDebug: ChatContextDebug | null
  workflowControl: ChatWorkflowControl | null
  isThinking: boolean  // 是否正在思考中

  // ReAct 迭代状态
  iterationSteps: IterationStep[]  // 所有迭代步骤
  currentIteration: number  // 当前迭代次数
  toolCalls: ToolCall[]
  currentToolCall: ToolCall | null
  currentTurnId: string | null

  // 停止控制
  abortController: AbortController | null
  currentBackgroundRunId: string | null

  // Actions
  fetchConversations: () => Promise<void>
  createConversation: (title?: string) => Promise<Conversation>
  selectConversation: (conversationId: number) => Promise<void>
  deleteConversation: (conversationId: number) => Promise<void>
  archiveConversation: (conversationId: number) => Promise<void>
  compactConversationContext: () => Promise<void>
  rewriteMessageSpan: (
    messageId: number,
    payload: {
      instruction: string
      selected_text: string
      before_context?: string
      after_context?: string
      occurrence_index?: number
    },
  ) => Promise<MessageSpanRewriteResponse>
  sendMessage: (
    message: string,
    options?: {
      sendPlanId?: string
      chatPreferenceOverrides?: Partial<ChatUserPreferences>
      ragOverrides?: ChatRagOverrides | null
      skillLaunch?: ChatSkillLaunchRequest | null
    },
  ) => Promise<number | undefined>  // 返回新对话ID（如果有）
  stopGeneration: () => void  // 停止生成
  clearCurrentConversation: () => void
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  currentConversation: null,
  messages: [],
  isLoading: false,
  isSending: false,
  isLoadingList: false,
  isCompactingContext: false,
  sendPhase: 'idle',
  sendPhaseLabel: null,
  sendPhaseHint: null,
  streamingContent: '',
  streamingThought: '',
  streamingContextDebug: null,
  lastRunContextDebug: null,
  workflowControl: null,
  isThinking: false,
  iterationSteps: [],
  currentIteration: 0,
  toolCalls: [],
  currentToolCall: null,
  currentTurnId: null,
  abortController: null,
  currentBackgroundRunId: null,

  fetchConversations: async () => {
    // 防止重复加载
    if (get().isLoadingList) return

    set({ isLoadingList: true })
    try {
      const conversations = await chatApi.getConversations()
      set({ conversations, isLoadingList: false })
    } catch (error) {
      handleApiError(error, '获取对话列表')
      set({ isLoadingList: false })
    }
  },

  createConversation: async (title?: string) => {
    try {
      const conversation = await chatApi.createConversation(title || '新对话')
      set((state) => ({
        conversations: [conversation, ...state.conversations],
        currentConversation: conversation,
        messages: [],
        lastRunContextDebug: null,
        workflowControl: null,
        sendPhase: 'idle',
        sendPhaseLabel: null,
        sendPhaseHint: null,
      }))
      return conversation
    } catch (error) {
      handleApiError(error, '创建对话')
      throw error
    }
  },

  selectConversation: async (conversationId: number) => {
    set({ isLoading: true })
    try {
      const conversation = await chatApi.getConversation(conversationId)

      // 确保消息按时间排序
      const sortedMessages = (conversation.messages || []).sort(
        (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
      )

      set({
        currentConversation: conversation,
        messages: sortedMessages,
        streamingContextDebug: null,
        lastRunContextDebug: null,
        workflowControl: resolveConversationWorkflowControl(sortedMessages),
        sendPhase: 'idle',
        sendPhaseLabel: null,
        sendPhaseHint: null,
        isLoading: false,
      })
    } catch (error) {
      console.error('加载对话失败:', error)
      set({ isLoading: false, currentConversation: null, messages: [], workflowControl: null })
      throw error
    }
  },

  deleteConversation: async (conversationId: number) => {
    await chatApi.deleteConversation(conversationId)
    const { currentConversation } = get()
    set((state) => ({
      conversations: state.conversations.filter((c) => c.id !== conversationId),
      currentConversation: currentConversation?.id === conversationId ? null : currentConversation,
      messages: currentConversation?.id === conversationId ? [] : state.messages,
      lastRunContextDebug: currentConversation?.id === conversationId ? null : state.lastRunContextDebug,
      workflowControl: currentConversation?.id === conversationId ? null : state.workflowControl,
      sendPhase: currentConversation?.id === conversationId ? 'idle' : state.sendPhase,
      sendPhaseLabel: currentConversation?.id === conversationId ? null : state.sendPhaseLabel,
      sendPhaseHint: currentConversation?.id === conversationId ? null : state.sendPhaseHint,
    }))
  },

  archiveConversation: async (conversationId: number) => {
    try {
      // 调用归档 API
      const response = await fetch(`/api/v1/chat/conversations/${conversationId}/archive`, {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('token')}`,
        },
      })
      if (response.ok) {
        // 刷新对话列表
        get().fetchConversations()
      }
    } catch (error) {
      handleApiError(error, '归档失败')
    }
  },

  compactConversationContext: async () => {
    const { currentConversation } = get()
    if (!currentConversation?.id) return

    set({ isCompactingContext: true })
    try {
      const payload = await chatApi.compactConversation(currentConversation.id)
      const nextContextState =
        payload.context_state && typeof payload.context_state === 'object'
          ? (payload.context_state as ConversationContextState)
          : undefined
      const nextCompactedHistory =
        payload.compacted_history && typeof payload.compacted_history === 'object'
          ? (payload.compacted_history as ConversationCompactedHistory)
          : undefined
      const nextHistoryLog =
        payload.history_log && typeof payload.history_log === 'object'
          ? (payload.history_log as ConversationHistoryLog)
          : undefined
      const nextTurnStore =
        payload.turn_store && typeof payload.turn_store === 'object'
          ? (payload.turn_store as ConversationTurnStore)
          : undefined
      const nextToolLedger =
        payload.tool_ledger && typeof payload.tool_ledger === 'object'
          ? (payload.tool_ledger as ConversationToolLedger)
          : undefined
      const nextItemStream =
        payload.item_stream && typeof payload.item_stream === 'object'
          ? (payload.item_stream as ConversationItemStream)
          : undefined
      const nextContextSnapshots = Array.isArray(payload.context_snapshots)
        ? (payload.context_snapshots as ConversationContextSnapshot[])
        : undefined

      set((state) => ({
        currentConversation: state.currentConversation
          ? {
              ...state.currentConversation,
              ...(nextContextState ? { context_state: nextContextState } : {}),
              ...(nextCompactedHistory ? { compacted_history: nextCompactedHistory } : {}),
              ...(nextHistoryLog ? { history_log: nextHistoryLog } : {}),
              ...(nextTurnStore ? { turn_store: nextTurnStore } : {}),
              ...(nextToolLedger ? { tool_ledger: nextToolLedger } : {}),
              ...(nextItemStream ? { item_stream: nextItemStream } : {}),
              ...(nextContextSnapshots ? { context_snapshots: nextContextSnapshots } : {}),
            }
          : state.currentConversation,
        isCompactingContext: false,
      }))
    } catch (error) {
      set({ isCompactingContext: false })
      handleApiError(error, '压缩会话上下文')
      throw error
    }
  },

  rewriteMessageSpan: async (messageId, payload) => {
    try {
      const response = await chatApi.rewriteMessageSpan(messageId, payload)
      set((state) => ({
        messages: state.messages.map((message) =>
          message.id === response.message.id ? response.message : message,
        ),
        currentConversation: applySpanRewriteToConversation(state.currentConversation, response),
      }))
      return response
    } catch (error) {
      handleApiError(error, '改写消息片段')
      throw error
    }
  },

  sendMessage: async (
    message: string,
    options?: {
      sendPlanId?: string
      chatPreferenceOverrides?: Partial<ChatUserPreferences>
      ragOverrides?: ChatRagOverrides | null
      skillLaunch?: ChatSkillLaunchRequest | null
    },
  ): Promise<number | undefined> => {
    const { currentConversation, fetchConversations, isSending } = get()

    // 防止重复发送
    if (isSending) {
      return undefined
    }

    // 创建 AbortController
    const abortController = new AbortController()

    // 创建用户消息
    const userMessage: Message = {
      id: Date.now(),
      conversation_id: currentConversation?.id || 0,
      role: 'user',
      content: message,
      message_type: 'text',
      created_at: new Date().toISOString(),
    }

    set((state) => ({
      messages: [...state.messages, userMessage],
      isSending: true,
      isThinking: false,  // 初始不是思考状态，等待 thinking_start 事件
      sendPhase: 'submitting',
      sendPhaseLabel: null,
      sendPhaseHint: null,
      streamingContent: '',
      streamingThought: '',
      streamingContextDebug: null,
      workflowControl: null,
      iterationSteps: [],  // 重置迭代步骤
      currentIteration: 0,  // 重置为0，thinking_start时会变为1（表示第1轮）
      toolCalls: [],
      currentToolCall: null,
      currentTurnId: null,
      abortController,  // 保存 AbortController
      currentBackgroundRunId: null,
    }))

    let newConversationId: number | undefined = undefined

    try {
      let fullContent = ''
      let currentThought = ''  // 当前迭代的思考

      const handleStreamEvent = (event: string, data: any) => {
          switch (event) {
            case 'run_status':
              if (data && typeof data === 'object' && typeof data.run_id === 'string') {
                set({ currentBackgroundRunId: data.run_id })
              }
              break

            case 'start':
              set({
                sendPhase: 'planning',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                currentTurnId:
                  data && typeof data === 'object' && typeof data.turn_id === 'string' && data.turn_id.trim()
                    ? data.turn_id.trim()
                    : null,
              })
              if (data.conversation_id && !currentConversation) {
                // 新创建的对话
                newConversationId = data.conversation_id
                // 更新 currentConversation
                set({
                  currentConversation: {
                    id: data.conversation_id,
                    user_id: 0,
                    title: message.slice(0, 30),
                    llm_provider: 'deepseek',
                    is_archived: 0,
                    created_at: new Date().toISOString(),
                    updated_at: new Date().toISOString(),
                  }
                })
              }
              break

            case 'phase':
              if (data && typeof data === 'object') {
                const phaseKey = typeof data.key === 'string' ? data.key : ''
                const nextPhase: SendPhase =
                  phaseKey === 'loading_context' ||
                  phaseKey === 'routing' ||
                  phaseKey === 'waiting_model' ||
                  phaseKey === 'planning' ||
                  phaseKey === 'thinking' ||
                  phaseKey === 'tool' ||
                  phaseKey === 'answering'
                    ? phaseKey
                    : 'planning'
                set({
                  sendPhase: nextPhase,
                  sendPhaseLabel: typeof data.label === 'string' && data.label.trim() ? data.label.trim() : null,
                  sendPhaseHint: typeof data.hint === 'string' && data.hint.trim() ? data.hint.trim() : null,
                })
              }
              break

            case 'thinking_start':
              // 新一轮迭代开始
              set((state) => ({
                isThinking: true,
                sendPhase: 'thinking',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                currentIteration: state.currentIteration + 1,
              }))
              currentThought = ''  // 重置当前思考
              break

            case 'thinking':
              // 流式思考内容
              currentThought += data
              set({ streamingThought: currentThought })
              break

            case 'thought':
              // 思考完成，记录到迭代步骤
              currentThought = data
              set((state) => ({
                streamingThought: currentThought,
                isThinking: false,
                sendPhase: 'thinking',
                iterationSteps: [...state.iterationSteps, {
                  type: 'thought',
                  content: data,
                  timestamp: Date.now(),
                }]
              }))
              break

            case 'action':
              // 工具调用开始
              const toolCall = {
                tool: data.tool,
                input: data.input,
                timestamp: Date.now(),
              }
              set((state) => ({
                currentToolCall: toolCall,
                toolCalls: [...state.toolCalls, toolCall],
                isThinking: false,
                sendPhase: 'tool',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                iterationSteps: [...state.iterationSteps, {
                  type: 'action',
                  content: `调用工具: ${data.tool}`,
                  tool: data.tool,
                  toolInput: data.input,
                  timestamp: Date.now(),
                }]
              }))
              break

            case 'observation':
              // 工具调用结果
              set((state) => {
                const updatedToolCalls = [...state.toolCalls]
                const lastIndex = updatedToolCalls.length - 1
                if (lastIndex >= 0) {
                  updatedToolCalls[lastIndex] = {
                    ...updatedToolCalls[lastIndex],
                    output: data.output,
                    success: data.success,
                  }
                }
                return {
                  toolCalls: updatedToolCalls,
                  currentToolCall: null,
                  isThinking: true,  // 继续思考
                  sendPhase: 'thinking',
                  sendPhaseLabel: null,
                  sendPhaseHint: null,
                  iterationSteps: [...state.iterationSteps, {
                    type: 'observation',
                    content: data.output,
                    tool: data.tool,
                    toolOutput: data.output,
                    success: data.success,
                    timestamp: Date.now(),
                  }]
                }
              })
              break

            case 'content':
              // 流式回答内容
              fullContent += data
              set({
                streamingContent: fullContent,
                sendPhase: 'answering',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                isThinking: false
              })
              break

            case 'context_debug':
              set({
                streamingContextDebug:
                  data && typeof data === 'object' ? (data as ChatContextDebug) : null,
              })
              break

            case 'stopped':
              // 停止事件 - 由 stopGeneration 处理，这里只是确保状态被清理
              set((state) => {
                // 只有当还在发送状态时才重置（防止覆盖 stopGeneration 的处理结果）
                if (state.isSending) {
                  return {
                    isSending: false,
                    isThinking: false,
                    sendPhase: 'idle',
                    sendPhaseLabel: null,
                    sendPhaseHint: null,
                    streamingContent: '',
                    streamingThought: '',
                    streamingContextDebug: null,
                    workflowControl: null,
                    iterationSteps: [],
                    currentIteration: 0,
                    toolCalls: [],
                    currentToolCall: null,
                    abortController: null,
                    currentBackgroundRunId: null,
                  }
                }
                return state
              })
              break

            case 'done':
              // 完成，添加助手消息
              const doneData = (data && typeof data === 'object') ? data as Record<string, any> : {}
              const ragMetrics = doneData.rag_metrics
              const citationIndex =
                doneData.citation_index && typeof doneData.citation_index === 'object'
                  ? doneData.citation_index
                  : null
              const contextDebug = get().streamingContextDebug
              const reasoningSummary =
                typeof doneData.reasoning_summary === 'string' && doneData.reasoning_summary.trim()
                  ? doneData.reasoning_summary.trim()
                  : ''
              const conversationContextState =
                doneData.context_state && typeof doneData.context_state === 'object'
                  ? (doneData.context_state as ConversationContextState)
                  : undefined
              const conversationTurnStore =
                doneData.turn_store && typeof doneData.turn_store === 'object'
                  ? (doneData.turn_store as ConversationTurnStore)
                  : undefined
              const conversationToolLedger =
                doneData.tool_ledger && typeof doneData.tool_ledger === 'object'
                  ? (doneData.tool_ledger as ConversationToolLedger)
                  : undefined
              const conversationItemStream =
                doneData.item_stream && typeof doneData.item_stream === 'object'
                  ? (doneData.item_stream as ConversationItemStream)
                  : undefined
              const workflowControl =
                doneData.workflow_control && typeof doneData.workflow_control === 'object'
                  ? (doneData.workflow_control as ChatWorkflowControl)
                  : null
              const metadata: MessageMetadata | undefined =
                ragMetrics || reasoningSummary || citationIndex
                  ? {
                      ...(ragMetrics ? { rag_metrics: ragMetrics } : {}),
                      ...(reasoningSummary ? { reasoning_summary: { summary: reasoningSummary } } : {}),
                      ...(citationIndex ? { citation_index: citationIndex } : {}),
                    }
                  : undefined
              const resolvedMetadata: MessageMetadata | undefined =
                workflowControl
                  ? {
                      ...(metadata || {}),
                      workflow_control: workflowControl,
                    }
                  : metadata
              const finalAssistantContent = String(fullContent || doneData.answer || '')
              const finalAssistantThought =
                typeof doneData.thought === 'string' && doneData.thought.trim()
                  ? doneData.thought.trim()
                  : currentThought || undefined
              const resolvedAssistantMessage = buildDoneAssistantMessage({
                itemStream: conversationItemStream,
                turnStore: conversationTurnStore,
                currentTurnId: get().currentTurnId,
                fallbackContent: finalAssistantContent,
                fallbackThought: finalAssistantThought,
                fallbackMetadata: resolvedMetadata,
                conversationId: newConversationId || currentConversation?.id || 0,
              })
              const shouldAppendLocalAssistantMessage = Boolean(resolvedAssistantMessage)

              set((state) => ({
                messages: shouldAppendLocalAssistantMessage
                  ? [
                      ...state.messages.filter((message) =>
                        resolvedAssistantMessage?.id ? message.id !== resolvedAssistantMessage.id : true,
                      ),
                      resolvedAssistantMessage as Message,
                    ]
                  : state.messages,
                currentConversation: state.currentConversation
                  ? {
                      ...state.currentConversation,
                      ...(conversationContextState ? { context_state: conversationContextState } : {}),
                      ...(conversationTurnStore ? { turn_store: conversationTurnStore } : {}),
                      ...(conversationToolLedger ? { tool_ledger: conversationToolLedger } : {}),
                      ...(conversationItemStream ? { item_stream: conversationItemStream } : {}),
                    }
                  : state.currentConversation,
                isSending: false,
                isThinking: false,
                sendPhase: 'idle',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                streamingContent: '',
                streamingThought: '',
                streamingContextDebug: null,
                lastRunContextDebug: contextDebug || state.streamingContextDebug,
                workflowControl,
                iterationSteps: [],  // 清空迭代步骤
                currentIteration: 0,
                toolCalls: [],  // 清空工具调用记录
                currentToolCall: null,
                currentTurnId: null,
                abortController: null,
                currentBackgroundRunId: null,
              }))

              // 刷新对话列表（新对话或更新标题）
              fetchConversations()
              break

            case 'error':
              set({
                isSending: false,
                isThinking: false,
                sendPhase: 'idle',
                sendPhaseLabel: null,
                sendPhaseHint: null,
                streamingContextDebug: null,
                workflowControl: null,
                iterationSteps: [],
                currentIteration: 0,
                toolCalls: [],
                currentToolCall: null,
                lastRunContextDebug: null,
                currentTurnId: null,
                abortController: null,
                currentBackgroundRunId: null,
              })
              throw new Error(data)
          }
        }

      const run = await chatApi.createChatRun(
        message,
        currentConversation?.id,
        options?.sendPlanId,
        options?.chatPreferenceOverrides,
        options?.ragOverrides,
        options?.skillLaunch,
      )
      set({ currentBackgroundRunId: run.run_id })
      if (abortController.signal.aborted) {
        void chatApi.cancelChatRun(run.run_id).catch((error) => {
          console.error('[ChatStore] 取消后台对话任务失败:', error)
        })
        set({ currentBackgroundRunId: null })
        return newConversationId
      }

      await chatApi.streamChatRun(
        run.run_id,
        handleStreamEvent,
        abortController,
      )

      return newConversationId
    } catch (error) {
      // 如果是中止错误，不需要额外处理（已由 stopGeneration 处理）
      if (error instanceof Error && error.name === 'AbortError') {
        return newConversationId
      }

      // 其他错误，重置状态
      set({
        isSending: false,
        isThinking: false,
        sendPhase: 'idle',
        sendPhaseLabel: null,
        sendPhaseHint: null,
        streamingContextDebug: null,
        workflowControl: null,
        iterationSteps: [],
        currentIteration: 0,
        toolCalls: [],
        currentToolCall: null,
        currentTurnId: null,
        abortController: null,
        currentBackgroundRunId: null,
      })
      throw error
    }
  },

  stopGeneration: () => {
    const state = get()
    const { abortController, isSending, currentConversation, streamingContent, streamingThought, iterationSteps, streamingContextDebug, currentTurnId, currentBackgroundRunId } = state

    if (!abortController || !isSending) {
      return
    }

    // 立即设置 isSending 为 false，防止重复调用和 race condition
    set({ isSending: false, sendPhase: 'idle', sendPhaseLabel: null, sendPhaseHint: null })
    if (currentBackgroundRunId) {
      void chatApi.cancelChatRun(currentBackgroundRunId).catch((error) => {
        console.error('[ChatStore] 取消后台对话任务失败:', error)
      })
    }

    // 保存当前内容
    const stoppedContent = streamingContent || ''
    const stoppedThought = streamingThought || ''
    const stoppedSteps = [...(iterationSteps || [])]

    // 构建停止消息
    let finalContent = ''
    if (stoppedContent) {
      finalContent = stoppedContent + '\n\n[已停止生成]'
    } else if (stoppedThought) {
      finalContent = '[思考中被停止]\n\n' + stoppedThought
    } else if (stoppedSteps.length > 0) {
      finalContent = '[推理过程中被停止]'
    }

    // 只有在有内容且有对话ID时才保存
    const conversationId = currentConversation?.id
    if ((finalContent || stoppedSteps.length > 0) && conversationId) {
      // 保存到数据库
      chatApi.saveStoppedMessage({
        conversation_id: conversationId,
        content: finalContent || '[已停止生成]',
        thought: stoppedThought || undefined,
        metadata: {
          ...(currentTurnId ? { turn_id: currentTurnId } : {}),
        },
      }).then((savedMessage) => {
        // 使用数据库返回的消息更新 store
        set((currentState) => ({
          messages: [...currentState.messages, savedMessage],
          isThinking: false,
          sendPhase: 'idle',
          sendPhaseLabel: null,
          sendPhaseHint: null,
          streamingContent: '',
          streamingThought: '',
          streamingContextDebug: null,
          lastRunContextDebug: streamingContextDebug || currentState.lastRunContextDebug,
          workflowControl: null,
          iterationSteps: [],
          currentIteration: 0,
          toolCalls: [],
          currentToolCall: null,
          currentTurnId: null,
          abortController: null,
          currentBackgroundRunId: null,
        }))
      }).catch((error) => {
        console.error('[ChatStore] 保存消息到数据库失败:', error)
        // 即使保存失败，也在本地添加消息
        const localMessage: Message = {
          id: Date.now() + 1,
          conversation_id: conversationId,
          role: 'assistant',
          content: finalContent || '[已停止生成]',
          message_type: 'text',
          thought: stoppedThought || undefined,
          metadata: undefined,
          created_at: new Date().toISOString(),
        }

        set((currentState) => ({
          messages: [...currentState.messages, localMessage],
          isThinking: false,
          sendPhase: 'idle',
          sendPhaseLabel: null,
          sendPhaseHint: null,
          streamingContent: '',
          streamingThought: '',
          streamingContextDebug: null,
          lastRunContextDebug: streamingContextDebug || currentState.lastRunContextDebug,
          workflowControl: null,
          iterationSteps: [],
          currentIteration: 0,
          toolCalls: [],
          currentToolCall: null,
          currentTurnId: null,
          abortController: null,
          currentBackgroundRunId: null,
        }))
      })
    } else {
      // 没有内容需要保存
      set({
        isThinking: false,
        sendPhase: 'idle',
        sendPhaseLabel: null,
        sendPhaseHint: null,
        streamingContent: '',
        streamingThought: '',
        streamingContextDebug: null,
        lastRunContextDebug: streamingContextDebug || state.lastRunContextDebug,
        workflowControl: null,
        iterationSteps: [],
        currentIteration: 0,
        toolCalls: [],
        currentToolCall: null,
        currentTurnId: null,
        abortController: null,
        currentBackgroundRunId: null,
      })
    }

    // 最后中止请求
    abortController.abort()
  },

  clearCurrentConversation: () => {
    set({
      currentConversation: null,
      messages: [],
      sendPhase: 'idle',
      sendPhaseLabel: null,
      sendPhaseHint: null,
      streamingContent: '',
      streamingThought: '',
      streamingContextDebug: null,
      lastRunContextDebug: null,
      workflowControl: null,
      iterationSteps: [],
      currentIteration: 0,
      toolCalls: [],
      currentToolCall: null,
      currentTurnId: null,
      abortController: null,
      currentBackgroundRunId: null,
    })
  },
}))
