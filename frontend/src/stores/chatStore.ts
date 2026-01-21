import { create } from 'zustand'
import { chatApi, Conversation, Message } from '@/services/api'

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

interface ChatState {
  // 对话列表
  conversations: Conversation[]
  currentConversation: Conversation | null
  messages: Message[]
  
  // 加载状态
  isLoading: boolean
  isSending: boolean
  isLoadingList: boolean  // 对话列表加载状态
  
  // 流式响应状态
  streamingContent: string
  streamingThought: string
  isThinking: boolean  // 是否正在思考中
  
  // ReAct 迭代状态
  iterationSteps: IterationStep[]  // 所有迭代步骤
  currentIteration: number  // 当前迭代次数
  toolCalls: ToolCall[]
  currentToolCall: ToolCall | null
  
  // 停止控制
  abortController: AbortController | null
  
  // Actions
  fetchConversations: () => Promise<void>
  createConversation: (title?: string) => Promise<Conversation>
  selectConversation: (conversationId: number) => Promise<void>
  deleteConversation: (conversationId: number) => Promise<void>
  archiveConversation: (conversationId: number) => Promise<void>
  sendMessage: (message: string) => Promise<number | undefined>  // 返回新对话ID（如果有）
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
  streamingContent: '',
  streamingThought: '',
  isThinking: false,
  iterationSteps: [],
  currentIteration: 0,
  toolCalls: [],
  currentToolCall: null,
  abortController: null,
  
  fetchConversations: async () => {
    // 防止重复加载
    if (get().isLoadingList) return
    
    set({ isLoadingList: true })
    try {
      const conversations = await chatApi.getConversations()
      set({ conversations, isLoadingList: false })
    } catch (error) {
      console.error('获取对话列表失败:', error)
      set({ isLoadingList: false })
    }
  },
  
  createConversation: async (title?: string) => {
    const conversation = await chatApi.createConversation(title || '新对话')
    set((state) => ({
      conversations: [conversation, ...state.conversations],
      currentConversation: conversation,
      messages: [],
    }))
    return conversation
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
        isLoading: false,
      })
    } catch (error) {
      console.error('加载对话失败:', error)
      set({ isLoading: false, currentConversation: null, messages: [] })
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
    }))
  },
  
  archiveConversation: async (conversationId: number) => {
    try {
      // 调用归档 API
      const response = await fetch(`/api/chat/conversations/${conversationId}/archive`, {
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
      console.error('归档失败:', error)
    }
  },
  
  sendMessage: async (message: string): Promise<number | undefined> => {
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
      streamingContent: '',
      streamingThought: '',
      iterationSteps: [],  // 重置迭代步骤
      currentIteration: 0,  // 重置为0，thinking_start时会变为1（表示第1轮）
      toolCalls: [],
      currentToolCall: null,
      abortController,  // 保存 AbortController
    }))
    
    let newConversationId: number | undefined = undefined
    
    try {
      let fullContent = ''
      let currentThought = ''  // 当前迭代的思考
      
      await chatApi.sendMessageStream(
        message,
        currentConversation?.id,
        (event, data) => {
          switch (event) {
            case 'start':
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
              
            case 'thinking_start':
              // 新一轮迭代开始
              set((state) => ({ 
                isThinking: true,
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
                isThinking: false 
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
                    streamingContent: '',
                    streamingThought: '',
                    iterationSteps: [],
                    currentIteration: 0,
                    toolCalls: [],
                    currentToolCall: null,
                    abortController: null,
                  }
                }
                return state
              })
              break
              
            case 'done':
              // 完成，添加助手消息
              const assistantMessage: Message = {
                id: Date.now() + 1,
                conversation_id: newConversationId || currentConversation?.id || 0,
                role: 'assistant',
                content: fullContent || data.answer || '',
                message_type: 'text',
                thought: currentThought || data.thought || undefined,
                react_steps: data.react_steps || undefined,  // 保存ReAct步骤
                created_at: new Date().toISOString(),
              }
              
              set((state) => ({
                messages: [...state.messages, assistantMessage],
                isSending: false,
                isThinking: false,
                streamingContent: '',
                streamingThought: '',
                iterationSteps: [],  // 清空迭代步骤
                currentIteration: 0,
                toolCalls: [],  // 清空工具调用记录
                currentToolCall: null,
                abortController: null,
              }))
              
              // 刷新对话列表（新对话或更新标题）
              fetchConversations()
              break
              
            case 'error':
              set({ 
                isSending: false, 
                isThinking: false, 
                iterationSteps: [],
                currentIteration: 0,
                toolCalls: [], 
                currentToolCall: null,
                abortController: null,
              })
              throw new Error(data)
          }
        },
        abortController
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
        iterationSteps: [],
        currentIteration: 0,
        toolCalls: [], 
        currentToolCall: null,
        abortController: null,
      })
      throw error
    }
  },
  
  stopGeneration: () => {
    const state = get()
    const { abortController, isSending, currentConversation, streamingContent, streamingThought, iterationSteps } = state
    
    if (!abortController || !isSending) {
      return
    }
    
    // 立即设置 isSending 为 false，防止重复调用和 race condition
    set({ isSending: false })
    
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
      const reactSteps = stoppedSteps.length > 0 ? stoppedSteps.map((step, idx) => ({
        type: step.type,
        iteration: Math.floor(idx / 3) + 1,
        content: step.content,
        tool: step.tool,
        input: step.toolInput,
        output: step.toolOutput,
        success: step.success,
      })) : undefined
      
      // 保存到数据库
      chatApi.saveStoppedMessage({
        conversation_id: conversationId,
        content: finalContent || '[已停止生成]',
        thought: stoppedThought || undefined,
        react_steps: reactSteps,
      }).then((savedMessage) => {
        // 使用数据库返回的消息更新 store
        set((currentState) => ({
          messages: [...currentState.messages, savedMessage],
          isThinking: false,
          streamingContent: '',
          streamingThought: '',
          iterationSteps: [],
          currentIteration: 0,
          toolCalls: [],
          currentToolCall: null,
          abortController: null,
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
          react_steps: reactSteps,
          created_at: new Date().toISOString(),
        }
        
        set((currentState) => ({
          messages: [...currentState.messages, localMessage],
          isThinking: false,
          streamingContent: '',
          streamingThought: '',
          iterationSteps: [],
          currentIteration: 0,
          toolCalls: [],
          currentToolCall: null,
          abortController: null,
        }))
      })
    } else {
      // 没有内容需要保存
      set({
        isThinking: false,
        streamingContent: '',
        streamingThought: '',
        iterationSteps: [],
        currentIteration: 0,
        toolCalls: [],
        currentToolCall: null,
        abortController: null,
      })
    }
    
    // 最后中止请求
    abortController.abort()
  },
  
  clearCurrentConversation: () => {
    set({
      currentConversation: null,
      messages: [],
      streamingContent: '',
      streamingThought: '',
      iterationSteps: [],
      currentIteration: 0,
      toolCalls: [],
      currentToolCall: null,
      abortController: null,
    })
  },
}))
