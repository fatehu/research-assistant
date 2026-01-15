import { create } from 'zustand'
import { chatApi, Conversation, Message } from '@/services/api'

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
  
  // Actions
  fetchConversations: () => Promise<void>
  createConversation: (title?: string) => Promise<Conversation>
  selectConversation: (conversationId: number) => Promise<void>
  deleteConversation: (conversationId: number) => Promise<void>
  archiveConversation: (conversationId: number) => Promise<void>
  sendMessage: (message: string) => Promise<number | undefined>  // 返回新对话ID（如果有）
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
    const { currentConversation, fetchConversations } = get()
    
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
      isThinking: true,
      streamingContent: '',
      streamingThought: '',
    }))
    
    let newConversationId: number | undefined = undefined
    
    try {
      let fullContent = ''
      let fullThought = ''
      
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
              set({ isThinking: true })
              break
              
            case 'thinking':
              // 流式思考内容
              fullThought += data
              set({ streamingThought: fullThought })
              break
              
            case 'thought':
              // 思考完成
              fullThought = data
              set({ 
                streamingThought: fullThought,
                isThinking: false 
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
              
            case 'done':
              // 完成，添加助手消息
              const assistantMessage: Message = {
                id: Date.now() + 1,
                conversation_id: newConversationId || currentConversation?.id || 0,
                role: 'assistant',
                content: fullContent || data.answer || '',
                message_type: 'text',
                thought: fullThought || data.thought || undefined,
                created_at: new Date().toISOString(),
              }
              
              set((state) => ({
                messages: [...state.messages, assistantMessage],
                isSending: false,
                isThinking: false,
                streamingContent: '',
                streamingThought: '',
              }))
              
              // 刷新对话列表（新对话或更新标题）
              fetchConversations()
              break
              
            case 'error':
              set({ isSending: false, isThinking: false })
              throw new Error(data)
          }
        }
      )
      
      return newConversationId
    } catch (error) {
      set({ isSending: false, isThinking: false })
      throw error
    }
  },
  
  clearCurrentConversation: () => {
    set({
      currentConversation: null,
      messages: [],
      streamingContent: '',
      streamingThought: '',
    })
  },
}))
