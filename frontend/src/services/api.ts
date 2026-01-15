import axios, { AxiosError } from 'axios'

// API 基础配置
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

// 创建 axios 实例
const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器 - 添加 token
api.interceptors.request.use((config) => {
  const authStorage = localStorage.getItem('auth-storage')
  if (authStorage) {
    const { state } = JSON.parse(authStorage)
    if (state?.token) {
      config.headers.Authorization = `Bearer ${state.token}`
    }
  }
  return config
})

// 响应拦截器 - 处理错误
api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ detail: string }>) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('auth-storage')
      window.location.href = '/login'
    }
    throw error
  }
)

// 类型定义
export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  is_active: boolean
  preferred_llm_provider: string
  preferences: Record<string, unknown>
  created_at: string
  last_login?: string
}

export interface AuthResponse {
  access_token: string
  token_type: string
  user: User
}

export interface Conversation {
  id: number
  user_id: number
  title: string
  llm_provider: string
  llm_model?: string
  is_archived: number
  created_at: string
  updated_at: string
  messages?: Message[]
  message_count?: number
  last_message?: string
}

export interface Message {
  id: number
  conversation_id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  message_type: string
  thought?: string
  action?: string
  action_input?: Record<string, unknown>
  observation?: string
  metadata?: Record<string, unknown>
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  created_at: string
}

export interface LLMProvider {
  id: string
  name: string
  model: string
  available: boolean
}

// ========== 知识库类型 ==========

export interface KnowledgeBase {
  id: number
  user_id: number
  name: string
  description?: string
  embedding_model: string
  embedding_dimension: number
  chunk_size: number
  chunk_overlap: number
  document_count: number
  total_chunks: number
  total_tokens: number
  is_public: boolean
  created_at: string
  updated_at: string
}

export interface KnowledgeBaseCreate {
  name: string
  description?: string
  embedding_model?: string
  chunk_size?: number
  chunk_overlap?: number
}

export interface Document {
  id: number
  knowledge_base_id: number
  filename: string
  original_filename: string
  file_size: number
  file_type: string
  status: 'pending' | 'processing' | 'completed' | 'failed'
  error_message?: string
  chunk_count: number
  token_count: number
  char_count: number
  created_at: string
  updated_at: string
  processed_at?: string
  content?: string
}

export interface DocumentChunk {
  id: number
  document_id: number
  chunk_index: number
  content: string
  start_char: number
  end_char: number
  token_count: number
  char_count: number
  created_at: string
}

export interface SearchResult {
  chunk_id: number
  document_id: number
  knowledge_base_id: number
  document_name: string
  knowledge_base_name: string
  content: string
  score: number
  chunk_index: number
  metadata: Record<string, unknown>
}

export interface SearchResponse {
  query: string
  results: SearchResult[]
  total: number
  search_time_ms: number
}

export interface ProcessingStatus {
  document_id: number
  status: string
  progress: number
  message: string
  chunk_count: number
  error?: string
}

// 认证 API
export const authApi = {
  login: async (email: string, password: string): Promise<AuthResponse> => {
    const response = await api.post('/api/auth/login', { email, password })
    return response.data
  },
  
  register: async (
    email: string,
    username: string,
    password: string,
    fullName?: string
  ): Promise<AuthResponse> => {
    const response = await api.post('/api/auth/register', {
      email,
      username,
      password,
      full_name: fullName,
    })
    return response.data
  },
  
  me: async (): Promise<User> => {
    const response = await api.get('/api/auth/me')
    return response.data
  },
  
  logout: async (): Promise<void> => {
    await api.post('/api/auth/logout')
  },
}

// 用户 API
export const userApi = {
  getProfile: async (): Promise<User> => {
    const response = await api.get('/api/users/profile')
    return response.data
  },
  
  updateProfile: async (data: Partial<User>): Promise<User> => {
    const response = await api.put('/api/users/profile', data)
    return response.data
  },
  
  getLLMProviders: async (): Promise<{
    default: string
    providers: LLMProvider[]
  }> => {
    const response = await api.get('/api/users/llm-providers')
    return response.data
  },
}

// 聊天 API
export const chatApi = {
  getConversations: async (
    skip = 0,
    limit = 20,
    archived = false
  ): Promise<Conversation[]> => {
    const response = await api.get('/api/chat/conversations', {
      params: { skip, limit, archived },
    })
    return response.data
  },
  
  createConversation: async (title?: string): Promise<Conversation> => {
    const response = await api.post('/api/chat/conversations', { title })
    return response.data
  },
  
  getConversation: async (conversationId: number): Promise<Conversation> => {
    const response = await api.get(`/api/chat/conversations/${conversationId}`)
    return response.data
  },
  
  deleteConversation: async (conversationId: number): Promise<void> => {
    await api.delete(`/api/chat/conversations/${conversationId}`)
  },
  
  getMessages: async (
    conversationId: number,
    skip = 0,
    limit = 50
  ): Promise<Message[]> => {
    const response = await api.get(
      `/api/chat/conversations/${conversationId}/messages`,
      { params: { skip, limit } }
    )
    return response.data
  },
  
  // 流式发送消息
  sendMessageStream: async (
    message: string,
    conversationId?: number,
    onEvent?: (event: string, data: unknown) => void
  ): Promise<void> => {
    const response = await fetch(`${API_BASE_URL}/api/chat/send`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({
        message,
        conversation_id: conversationId,
        stream: true,
      }),
    })
    
    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || '发送失败')
    }
    
    const reader = response.body?.getReader()
    if (!reader) throw new Error('无法读取响应')
    
    const decoder = new TextDecoder()
    let buffer = ''
    
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))
            onEvent?.(data.event, data.data)
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
  },
}

// ========== 知识库 API ==========

export const knowledgeApi = {
  // 知识库 CRUD
  getKnowledgeBases: async (skip = 0, limit = 20): Promise<{ items: KnowledgeBase[]; total: number }> => {
    const response = await api.get('/api/knowledge/knowledge-bases', {
      params: { skip, limit },
    })
    return response.data
  },
  
  createKnowledgeBase: async (data: KnowledgeBaseCreate): Promise<KnowledgeBase> => {
    const response = await api.post('/api/knowledge/knowledge-bases', data)
    return response.data
  },
  
  getKnowledgeBase: async (kbId: number): Promise<KnowledgeBase> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}`)
    return response.data
  },
  
  updateKnowledgeBase: async (kbId: number, data: Partial<KnowledgeBaseCreate>): Promise<KnowledgeBase> => {
    const response = await api.put(`/api/knowledge/knowledge-bases/${kbId}`, data)
    return response.data
  },
  
  deleteKnowledgeBase: async (kbId: number): Promise<void> => {
    await api.delete(`/api/knowledge/knowledge-bases/${kbId}`)
  },
  
  // 文档管理
  getDocuments: async (kbId: number, skip = 0, limit = 20): Promise<{ items: Document[]; total: number }> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}/documents`, {
      params: { skip, limit },
    })
    return response.data
  },
  
  uploadDocument: async (kbId: number, file: File): Promise<Document> => {
    const formData = new FormData()
    formData.append('file', file)
    
    const response = await api.post(
      `/api/knowledge/knowledge-bases/${kbId}/documents/upload`,
      formData,
      {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      }
    )
    return response.data
  },
  
  getDocument: async (kbId: number, docId: number): Promise<Document> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}/documents/${docId}`)
    return response.data
  },
  
  deleteDocument: async (kbId: number, docId: number): Promise<void> => {
    await api.delete(`/api/knowledge/knowledge-bases/${kbId}/documents/${docId}`)
  },
  
  getDocumentStatus: async (kbId: number, docId: number): Promise<ProcessingStatus> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}/documents/${docId}/status`)
    return response.data
  },
  
  // 分片
  getChunks: async (kbId: number, docId: number, skip = 0, limit = 20): Promise<{ items: DocumentChunk[]; total: number }> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}/documents/${docId}/chunks`, {
      params: { skip, limit },
    })
    return response.data
  },
  
  // 搜索
  search: async (
    query: string,
    knowledgeBaseIds?: number[],
    topK = 5,
    scoreThreshold = 0.5
  ): Promise<SearchResponse> => {
    const response = await api.post('/api/knowledge/search', {
      query,
      knowledge_base_ids: knowledgeBaseIds,
      top_k: topK,
      score_threshold: scoreThreshold,
    })
    return response.data
  },
}

// 辅助函数
function getToken(): string {
  const authStorage = localStorage.getItem('auth-storage')
  if (authStorage) {
    const { state } = JSON.parse(authStorage)
    return state?.token || ''
  }
  return ''
}

export default api
