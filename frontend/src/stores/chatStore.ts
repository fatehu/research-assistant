import { create } from 'zustand'
import { chatApi, Conversation, Message } from '@/services/api'

interface ChatState {
  conversations: Conversation[]
  currentConversation: Conversation | null
  messages: Message[]
  isLoading: boolean
  isSending: boolean
  streamingContent: string
  streamingThought: string
  streamingAction: string
  
  // Actions
  fetchConversations: () => Promise<void>
  createConversation: (title?: string) => Promise<Conversation>
  selectConversation: (conversationId: number) => Promise<void>
  deleteConversation: (conversationId: number) => Promise<void>
  sendMessage: (message: string, onChunk?: (chunk: string) => void) => Promise<void>
  clearStreaming: () => void
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  currentConversation: null,
  messages: [],
  isLoading: false,
  isSending: false,
  streamingContent: '',
  streamingThought: '',
  streamingAction: '',
  
  fetchConversations: async () => {
    set({ isLoading: true })
    try {
      const conversations = await chatApi.getConversations()
      set({ conversations, isLoading: false })
    } catch (error) {
      set({ isLoading: false })
      throw error
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
      set({
        currentConversation: conversation,
        messages: conversation.messages || [],
        isLoading: false,
      })
    } catch (error) {
      set({ isLoading: false })
      throw error
    }
  },
  
  deleteConversation: async (conversationId: number) => {
    await chatApi.deleteConversation(conversationId)
    const currentConversation = get().currentConversation
    set((state) => ({
      conversations: state.conversations.filter((c) => c.id !== conversationId),
      currentConversation: currentConversation?.id === conversationId ? null : currentConversation,
      messages: currentConversation?.id === conversationId ? [] : state.messages,
    }))
  },
  
  sendMessage: async (message: string, onChunk?: (chunk: string) => void) => {
    const { currentConversation } = get()
    
    // 添加用户消息到列表
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
      streamingContent: '',
      streamingThought: '',
      streamingAction: '',
    }))
    
    try {
      let fullContent = ''
      let thought = ''
      let action = ''
      let newConversationId = currentConversation?.id
      
      await chatApi.sendMessageStream(
        message,
        currentConversation?.id,
        (event, data) => {
          if (event === 'start' && data.conversation_id) {
            newConversationId = data.conversation_id
          } else if (event === 'content') {
            fullContent += data
            set({ streamingContent: fullContent })
            onChunk?.(data)
          } else if (event === 'thought') {
            thought = data
            set({ streamingThought: thought })
          } else if (event === 'action') {
            action = data
            set({ streamingAction: action })
          } else if (event === 'done') {
            // 添加助手消息
            const assistantMessage: Message = {
              id: Date.now() + 1,
              conversation_id: newConversationId || 0,
              role: 'assistant',
              content: fullContent,
              message_type: 'text',
              thought: thought || undefined,
              action: action || undefined,
              created_at: new Date().toISOString(),
            }
            
            set((state) => ({
              messages: [...state.messages, assistantMessage],
              isSending: false,
              streamingContent: '',
              streamingThought: '',
              streamingAction: '',
            }))
            
            // 如果是新对话，更新对话列表
            if (!currentConversation && newConversationId) {
              get().fetchConversations()
            }
          } else if (event === 'error') {
            set({ isSending: false })
            throw new Error(data)
          }
        }
      )
    } catch (error) {
      set({ isSending: false })
      throw error
    }
  },
  
  clearStreaming: () => {
    set({
      streamingContent: '',
      streamingThought: '',
      streamingAction: '',
    })
  },
}))
