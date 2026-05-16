import { useState, useEffect, useMemo, useRef } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'

import { useChatStore } from '@/stores/chatStore'
import {
  chatApi,
  type ChatContextPreviewResponse,
  type ChatPreferenceCandidate,
  type ChatPreferenceKey,
  type ChatRagOverrides,
  type ChatWorkflowAction,
  type ChatSkillLaunchRequest,
  type ChatUserPreferences,
} from '@/services/api'
import { useAuthStore } from '@/stores/authStore'
import { ChatMessages, ChatInput, ContextDebugWindow, DocumentArtifactPanel } from './components'

/**
 * ChatPage - 聊天主页面（重构版）
 *
 * 原 1104 行单体组件已拆分为:
 *   - ChatMessages   消息列表区域（含流式、ReAct 面板）
 *   - ChatInput      输入区域
 *   - MessageBubble  消息气泡
 *   - ReActPanel     实时推理面板
 *   - HistoryReActPanel 历史推理面板
 *   - ThinkingPanel  思考面板
 *   - CodeBlock      代码块
 *   - EmptyState     空状态欢迎页
 *
 * 本文件仅保留路由参数处理、store 连接、页面级副作用。
 */
const ChatPage = () => {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const initialMessageSent = useRef(false)
  const { user } = useAuthStore()

  const {
    messages,
    currentConversation,
    isLoading,
    isSending,
    isCompactingContext,
    isThinking,
    sendPhase,
    sendPhaseLabel,
    sendPhaseHint,
    streamingContent,
    streamingThought,
    streamingContextDebug,
    lastRunContextDebug,
    workflowControl,
    iterationSteps,
    currentIteration,
    currentToolCall,
    currentTurnId,
    selectConversation,
    branchConversation,
    sendMessage,
    rewriteMessageSpan,
    compactConversationContext,
    stopGeneration,
    clearCurrentConversation,
  } = useChatStore()

  const [inputValue, setInputValue] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [highlightedMessageId, setHighlightedMessageId] = useState<number | null>(null)
  const [conversationLoaded, setConversationLoaded] = useState(false)
  const [isBranching, setIsBranching] = useState(false)
  const [contextPreview, setContextPreview] = useState<ChatContextPreviewResponse | null>(null)
  const [isPreviewLoading, setIsPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [chatPreferenceOverrides, setChatPreferenceOverrides] = useState<Partial<ChatUserPreferences>>({})
  const [ragOverrides, setRagOverrides] = useState<ChatRagOverrides | null>(null)
  const [ignoredCandidateIds, setIgnoredCandidateIds] = useState<string[]>([])
  const [ragResetToken, setRagResetToken] = useState(0)
  const [documentPanelOpen, setDocumentPanelOpen] = useState(false)
  const [selectedDocumentBlockIds, setSelectedDocumentBlockIds] = useState<string[]>([])
  const previewRequestIdRef = useRef(0)
  const loadedConversationIdRef = useRef<number | null>(null)
  const serializedPreferenceOverrides = useMemo(
    () => JSON.stringify(chatPreferenceOverrides || {}),
    [chatPreferenceOverrides],
  )
  const serializedRagOverrides = useMemo(
    () => JSON.stringify(ragOverrides || {}),
    [ragOverrides],
  )
  const activeContextDebug = useMemo(
    () => streamingContextDebug || contextPreview?.context_debug || lastRunContextDebug || null,
    [contextPreview?.context_debug, lastRunContextDebug, streamingContextDebug],
  )
  const confirmedChatPreferences = useMemo(
    () =>
      user?.preferences && typeof user.preferences.chat_preferences === 'object'
        ? (user.preferences.chat_preferences as ChatUserPreferences)
        : null,
    [user?.preferences],
  )
  const effectiveChatPreferences = useMemo(() => {
    const base = confirmedChatPreferences || {
      version: 'chat_preferences.v1',
      response_language: 'auto' as const,
      response_verbosity: 'balanced' as const,
      web_search: 'ask' as const,
    }
    if (contextPreview?.effective_chat_preferences) {
      return contextPreview.effective_chat_preferences
    }
    return {
      ...base,
      ...chatPreferenceOverrides,
    }
  }, [chatPreferenceOverrides, confirmedChatPreferences, contextPreview?.effective_chat_preferences])
  const visibleChatPreferenceCandidates = useMemo(
    () =>
      (contextPreview?.chat_preference_candidates || []).filter(
        (item): item is ChatPreferenceCandidate =>
          Boolean(item?.candidate_id) && !ignoredCandidateIds.includes(item.candidate_id),
      ),
    [contextPreview?.chat_preference_candidates, ignoredCandidateIds],
  )
  const hasPendingFirstTurnMessage = useMemo(
    () => messages.some((message) => Number(message.conversation_id || 0) === 0),
    [messages],
  )

  useEffect(() => {
    previewRequestIdRef.current += 1
    setContextPreview(null)
    setIsPreviewLoading(false)
    setPreviewError(null)
    setIgnoredCandidateIds([])
  }, [conversationId, inputValue, isSending, serializedPreferenceOverrides, serializedRagOverrides])

  useEffect(() => {
    setChatPreferenceOverrides({})
    setRagOverrides(null)
    setIgnoredCandidateIds([])
    setRagResetToken((current) => current + 1)
    setSelectedDocumentBlockIds([])
    loadedConversationIdRef.current = null
  }, [conversationId])

  useEffect(() => {
    const validBlockIds = new Set((currentConversation?.document_artifact?.blocks || []).map((block) => block.block_id))
    setSelectedDocumentBlockIds((current) => current.filter((blockId) => validBlockIds.has(blockId)))
  }, [currentConversation?.document_artifact?.blocks])

  // ─── 加载对话 ───────────────────────────────────
  useEffect(() => {
    let cancelled = false

    const loadConversation = async () => {
      if (conversationId) {
        const parsedConversationId = parseInt(conversationId, 10)
        if (isSending && currentConversation?.id === parsedConversationId) {
          setLoadError(null)
          setConversationLoaded(true)
          return
        }
        if (
          currentConversation?.id === parsedConversationId &&
          messages.length > 0 &&
          loadedConversationIdRef.current === parsedConversationId
        ) {
          setLoadError(null)
          setConversationLoaded(true)
          return
        }
        setConversationLoaded(false)
        if (!cancelled) {
          setLoadError(null)
        }
        try {
          await selectConversation(parsedConversationId)
          loadedConversationIdRef.current = parsedConversationId
          if (!cancelled) {
            setLoadError(null)
            setConversationLoaded(true)
          }
        } catch (error: any) {
          console.error('加载对话失败:', error)
          if (!cancelled) {
            if (error?.response?.status === 404) {
              setLoadError('对话不存在或已被删除')
            } else if (error?.response?.status === 401) {
              setLoadError('登录已过期，请重新登录')
            } else {
              setLoadError('加载对话失败，请刷新重试')
            }
          }
        }
      } else {
        const hasPendingFirstTurn = isSending || hasPendingFirstTurnMessage

        setConversationLoaded(false)
        if (!cancelled) {
          setLoadError(null)
          if (hasPendingFirstTurn) {
            setConversationLoaded(true)
            return
          }
          clearCurrentConversation()
          setConversationLoaded(true)
        }
      }
    }

    loadConversation()

    return () => {
      cancelled = true
    }
  }, [
    clearCurrentConversation,
    conversationId,
    currentConversation?.id,
    hasPendingFirstTurnMessage,
    isSending,
    messages.length,
    selectConversation,
  ])

  // ─── 处理首页传来的初始消息 / 消息高亮 ──────────
  useEffect(() => {
    const state = location.state as {
      initialMessage?: string
      initialSkillLaunch?: ChatSkillLaunchRequest
      highlightMessageId?: number
    } | null

    // 处理初始消息
    if (
      state?.initialMessage &&
      conversationId &&
      conversationLoaded &&
      !initialMessageSent.current &&
      !isSending
    ) {
      initialMessageSent.current = true
      sendMessage(state.initialMessage, {
        skillLaunch: state.initialSkillLaunch || null,
      }).catch((err) => {
        console.error('发送初始消息失败:', err)
        // Error handled by store
        initialMessageSent.current = false
      })
      navigate(location.pathname, { replace: true, state: {} })
    }

    // 处理消息高亮
    if (state?.highlightMessageId && conversationLoaded && messages.length > 0) {
      setHighlightedMessageId(state.highlightMessageId)
      navigate(location.pathname, { replace: true, state: {} })

      setTimeout(() => {
        const el = document.getElementById(`message-${state.highlightMessageId}`)
        el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 100)

      setTimeout(() => setHighlightedMessageId(null), 3000)
    }
  }, [conversationId, conversationLoaded, isSending, location.pathname, location.state, messages.length, navigate, sendMessage])

  // 重置 initialMessageSent 当 conversationId 改变时
  useEffect(() => {
    initialMessageSent.current = false
  }, [conversationId])

  // ─── 操作回调 ──────────────────────────────────
  const handleReload = async () => {
    if (conversationId) {
      setLoadError(null)
      try {
        await selectConversation(parseInt(conversationId))
      } catch {
        setLoadError('加载对话失败，请刷新重试')
      }
    }
  }

  const handleBranchConversation = async () => {
    if (!currentConversation?.id || isSending || isBranching) return
    setIsBranching(true)
    try {
      const branchConversationId = await branchConversation(currentConversation.id)
      if (branchConversationId) {
        navigate(`/chat/${branchConversationId}`)
      }
    } catch {
      // Error handled by store
    } finally {
      setIsBranching(false)
    }
  }

  const handleSend = async (content?: string) => {
    const messageContent = content || inputValue.trim()
    if (!messageContent || isSending) return

    const sendPlanId =
      contextPreview?.send_plan &&
      contextPreview.send_plan.reusable &&
      contextPreview.send_plan.draft_message === messageContent
        ? contextPreview.send_plan.plan_id
        : undefined

    setInputValue('')

    try {
      const newConvId = await sendMessage(messageContent, {
        sendPlanId,
        chatPreferenceOverrides,
        ragOverrides,
        documentArtifactBlockIds: selectedDocumentBlockIds,
      })
      setChatPreferenceOverrides({})
      setRagOverrides(null)
      setIgnoredCandidateIds([])
      setRagResetToken((current) => current + 1)
      if (newConvId && !conversationId) {
        navigate(`/chat/${newConvId}`, { replace: true })
      }
    } catch {
      // Error handled by store
    }
  }

  const handleQuickPrompt = (prompt: string) => {
    setInputValue(prompt)
  }

  const handleWorkflowAction = async (action: ChatWorkflowAction) => {
    const messageContent = String(action.message || '').trim()
    if (!messageContent || isSending) return

    setInputValue('')
    setContextPreview(null)
    setPreviewError(null)
    setIsPreviewLoading(false)

    try {
      const newConvId = await sendMessage(messageContent, {
        skillLaunch: action.skill_launch || null,
        documentArtifactBlockIds: selectedDocumentBlockIds,
      })
      setChatPreferenceOverrides({})
      setRagOverrides(null)
      setIgnoredCandidateIds([])
      setRagResetToken((current) => current + 1)
      if (newConvId && !conversationId) {
        navigate(`/chat/${newConvId}`, { replace: true })
      }
    } catch {
      // Error handled by store
    }
  }

  const handleRequestPreview = async () => {
    const trimmed = inputValue.trim()
    if (!trimmed || isSending) return

    const requestId = previewRequestIdRef.current + 1
    previewRequestIdRef.current = requestId
    setIsPreviewLoading(true)
    setPreviewError(null)

    try {
      const preview = await chatApi.previewContext(
        trimmed,
        conversationId ? parseInt(conversationId, 10) : undefined,
        undefined,
        chatPreferenceOverrides,
        ragOverrides,
      )
      if (previewRequestIdRef.current !== requestId) return
      setContextPreview(preview)
      setPreviewError(null)
    } catch (error) {
      if (previewRequestIdRef.current !== requestId) return
      console.error('上下文预演失败:', error)
      setContextPreview(null)
      setPreviewError('完整预演暂不可用')
    } finally {
      if (previewRequestIdRef.current === requestId) {
        setIsPreviewLoading(false)
      }
    }
  }

  const handleChatPreferenceOverrideChange = (
    key: ChatPreferenceKey,
    value: ChatUserPreferences[ChatPreferenceKey],
  ) => {
    setChatPreferenceOverrides((current) => ({
      ...current,
      [key]: value,
    }))
  }

  const handleChatPreferenceOverrideClear = (key: ChatPreferenceKey) => {
    setChatPreferenceOverrides((current) => {
      const next = { ...current }
      delete next[key]
      return next
    })
  }

  const handleIgnoreCandidate = (candidateId: string) => {
    setIgnoredCandidateIds((current) => (current.includes(candidateId) ? current : [...current, candidateId]))
  }

  const handleRagOverridesChange = (next: ChatRagOverrides | null) => {
    setRagOverrides(next && next.enabled ? next : null)
  }

  const handleEnsureDocumentArtifactConversation = async () => {
    if (currentConversation?.id) return currentConversation.id
    const created = await chatApi.createConversation('文档生成')
    await selectConversation(created.id)
    navigate(`/chat/${created.id}`)
    return created.id
  }

  const handleRefreshDocumentArtifactConversation = async (targetConversationId?: number) => {
    const id = targetConversationId || currentConversation?.id
    if (!id) return
    await selectConversation(id)
  }

  // ─── 渲染 ──────────────────────────────────────
  return (
    <div className="flex h-full flex-col bg-slate-950">
      {currentConversation ? (
        <div className="border-b border-slate-800/80 bg-slate-950/95 px-4 py-3">
          <div className="mx-auto flex max-w-[1040px] items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">当前对话</div>
              <div className="truncate text-sm font-medium text-slate-200">{currentConversation.title || '新对话'}</div>
            </div>
            <button
              type="button"
              className="shrink-0 rounded-full border border-cyan-400/35 bg-cyan-400/10 px-3 py-1.5 text-sm font-medium text-cyan-100 transition hover:border-cyan-300/70 hover:bg-cyan-400/20 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-slate-900 disabled:text-slate-500"
              disabled={isSending || isBranching || isLoading}
              title={isSending ? '当前 AI 仍在生成，结束后才能创建分支' : '复制当前会话到一个新的对话'}
              onClick={handleBranchConversation}
            >
              {isBranching ? '创建中...' : '创建分支'}
            </button>
          </div>
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1 flex-col bg-slate-950 lg:flex-row">
        <div className="flex min-w-0 flex-1 flex-col">
          <ChatMessages
            messages={messages}
            currentConversation={currentConversation}
            isLoading={isLoading}
            loadError={loadError}
            isSending={isSending}
            sendPhase={sendPhase}
            sendPhaseLabel={sendPhaseLabel}
            sendPhaseHint={sendPhaseHint}
            isThinking={isThinking}
            streamingContent={streamingContent}
            streamingThought={streamingThought}
            iterationSteps={iterationSteps}
            currentIteration={currentIteration}
            currentToolCall={currentToolCall}
            currentTurnId={currentTurnId}
            highlightedMessageId={highlightedMessageId}
            onRewriteSpan={rewriteMessageSpan}
            onQuickPrompt={handleQuickPrompt}
            onReload={handleReload}
          />

          <ChatInput
            inputValue={inputValue}
            isSending={isSending}
            sendPhase={sendPhase}
            sendPhaseLabel={sendPhaseLabel}
            sendPhaseHint={sendPhaseHint}
            hasConversationHistory={messages.length > 0 || Boolean(currentConversation?.message_count)}
            contextPreview={contextPreview}
            isPreviewLoading={isPreviewLoading}
            previewError={previewError}
            conversationState={currentConversation?.context_state || null}
            compactedHistory={currentConversation?.compacted_history || null}
            chatPreferences={confirmedChatPreferences}
            effectiveChatPreferences={effectiveChatPreferences}
            chatPreferenceCandidates={visibleChatPreferenceCandidates}
            chatPreferenceOverrides={chatPreferenceOverrides}
            ragOverrides={ragOverrides}
            effectiveRagOverrides={contextPreview?.effective_rag_overrides || ragOverrides}
            ragResetToken={ragResetToken}
            workflowControl={workflowControl}
            llmProvider={currentConversation?.llm_provider}
            onInputChange={setInputValue}
            onSend={() => handleSend()}
            onStop={stopGeneration}
            onWorkflowAction={handleWorkflowAction}
            onRequestPreview={handleRequestPreview}
            onChatPreferenceOverrideChange={handleChatPreferenceOverrideChange}
            onChatPreferenceOverrideClear={handleChatPreferenceOverrideClear}
            onIgnoreChatPreferenceCandidate={handleIgnoreCandidate}
            onRagOverridesChange={handleRagOverridesChange}
          />
        </div>

        <aside
          className={`min-h-0 shrink-0 transition-[width,height] duration-200 ${
            documentPanelOpen
              ? 'h-[48vh] lg:h-auto lg:w-[400px] xl:w-[440px]'
              : 'h-14 lg:h-auto lg:w-16'
          }`}
        >
          <DocumentArtifactPanel
            conversationId={currentConversation?.id}
            artifact={currentConversation?.document_artifact || null}
            disabled={isSending || isLoading}
            open={documentPanelOpen}
            onOpenChange={setDocumentPanelOpen}
            selectedBlockIds={selectedDocumentBlockIds}
            onSelectedBlockIdsChange={setSelectedDocumentBlockIds}
            onEnsureConversation={handleEnsureDocumentArtifactConversation}
            onRefresh={handleRefreshDocumentArtifactConversation}
          />
        </aside>
      </div>

      <ContextDebugWindow
        contextDebug={isLoading ? null : activeContextDebug}
        conversationState={currentConversation?.context_state || null}
        conversationCompactedHistory={currentConversation?.compacted_history || null}
        conversationHistoryLog={currentConversation?.history_log || null}
        conversationTurnStore={currentConversation?.turn_store || null}
        conversationToolLedger={currentConversation?.tool_ledger || null}
        conversationItemStream={currentConversation?.item_stream || null}
        conversationContextSnapshots={currentConversation?.context_snapshots || []}
        isSending={isSending}
        isCompacting={isCompactingContext}
        onManualCompact={compactConversationContext}
      />
    </div>
  )
}

export default ChatPage
