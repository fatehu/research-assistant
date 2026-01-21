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

export interface ReactStep {
  type: 'thought' | 'action' | 'observation'
  iteration: number
  content?: string
  tool?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
}

export interface Message {
  id: number
  conversation_id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  message_type: string
  thought?: string
  react_steps?: ReactStep[]
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

// ========== 文献管理类型 ==========

export interface PaperAuthor {
  name: string
  authorId?: string
  affiliations?: string[]
}

export interface Paper {
  id: number
  user_id: number
  semantic_scholar_id?: string
  arxiv_id?: string
  doi?: string
  title: string
  abstract?: string
  authors: PaperAuthor[]
  year?: number
  venue?: string
  citation_count: number
  reference_count: number
  influential_citation_count: number
  url?: string
  pdf_url?: string
  arxiv_url?: string
  pdf_path?: string
  pdf_downloaded: boolean
  knowledge_base_id?: number
  document_id?: number
  fields_of_study: string[]
  tags: string[]
  is_read: boolean
  read_at?: string
  notes?: string
  rating?: number
  source: string
  published_date?: string
  created_at: string
  updated_at: string
  collection_ids: number[]
}

export interface PaperSearchResult {
  source: string
  external_id: string
  title: string
  abstract?: string
  authors: PaperAuthor[]
  year?: number
  venue?: string
  citation_count: number
  reference_count: number
  url?: string
  pdf_url?: string
  arxiv_id?: string
  doi?: string
  fields_of_study: string[]
  is_saved: boolean
  saved_paper_id?: number
}

export interface PaperSearchResponse {
  total: number
  offset: number
  papers: PaperSearchResult[]
  query: string
  source: string
}

export interface PaperCollection {
  id: number
  user_id: number
  name: string
  description?: string
  color: string
  icon: string
  collection_type: string
  is_default: boolean
  paper_count: number
  created_at: string
  updated_at: string
}

export interface SearchHistory {
  id: number
  query: string
  source: string
  result_count: number
  filters: Record<string, unknown>
  created_at: string
}

// ========== 文献管理 API ==========

export const literatureApi = {
  // 初始化
  init: async (): Promise<{ message: string }> => {
    const response = await api.post('/api/literature/init')
    return response.data
  },

  // 搜索论文
  searchPapers: async (params: {
    query: string
    source?: string
    limit?: number
    offset?: number
    year_start?: number
    year_end?: number
    fields?: string
    open_access?: boolean
  }): Promise<PaperSearchResponse> => {
    const response = await api.get('/api/literature/search', { params })
    return response.data
  },

  // 获取搜索历史
  getSearchHistory: async (limit = 20): Promise<SearchHistory[]> => {
    const response = await api.get('/api/literature/search/history', {
      params: { limit },
    })
    return response.data
  },

  // 获取论文列表
  getPapers: async (params?: {
    collection_id?: number
    is_read?: boolean
    tag?: string
    search?: string
    sort_by?: string
    sort_order?: string
    limit?: number
    offset?: number
  }): Promise<Paper[]> => {
    const response = await api.get('/api/literature/papers', { params })
    return response.data
  },

  // 获取论文详情
  getPaper: async (paperId: number): Promise<Paper> => {
    const response = await api.get(`/api/literature/papers/${paperId}`)
    return response.data
  },

  // 保存论文
  savePaper: async (data: {
    source: string
    external_id: string
    title: string
    abstract?: string
    authors?: PaperAuthor[]
    year?: number
    venue?: string
    citation_count?: number
    reference_count?: number
    url?: string
    pdf_url?: string
    arxiv_id?: string
    doi?: string
    fields_of_study?: string[]
    raw_data?: Record<string, unknown>
    collection_ids?: number[]
  }): Promise<Paper> => {
    const response = await api.post('/api/literature/papers', data)
    return response.data
  },

  // 更新论文
  updatePaper: async (
    paperId: number,
    data: {
      title?: string
      abstract?: string
      notes?: string
      tags?: string[]
      rating?: number
      is_read?: boolean
    }
  ): Promise<Paper> => {
    const response = await api.patch(`/api/literature/papers/${paperId}`, data)
    return response.data
  },

  // 删除论文
  deletePaper: async (paperId: number): Promise<void> => {
    await api.delete(`/api/literature/papers/${paperId}`)
  },

  // 下载 PDF
  downloadPdf: async (
    paperId: number,
    knowledgeBaseId?: number
  ): Promise<{ message: string; pdf_path: string }> => {
    const response = await api.post(`/api/literature/papers/${paperId}/download-pdf`, null, {
      params: { knowledge_base_id: knowledgeBaseId },
    })
    return response.data
  },

  // 收藏夹管理
  getCollections: async (): Promise<PaperCollection[]> => {
    const response = await api.get('/api/literature/collections')
    return response.data
  },

  createCollection: async (data: {
    name: string
    description?: string
    color?: string
    icon?: string
    collection_type?: string
  }): Promise<PaperCollection> => {
    const response = await api.post('/api/literature/collections', data)
    return response.data
  },

  updateCollection: async (
    collectionId: number,
    data: {
      name?: string
      description?: string
      color?: string
      icon?: string
    }
  ): Promise<PaperCollection> => {
    const response = await api.patch(`/api/literature/collections/${collectionId}`, data)
    return response.data
  },

  deleteCollection: async (collectionId: number): Promise<void> => {
    await api.delete(`/api/literature/collections/${collectionId}`)
  },

  addPaperToCollection: async (paperId: number, collectionIds: number[]): Promise<void> => {
    await api.post('/api/literature/collections/add-paper', {
      paper_id: paperId,
      collection_ids: collectionIds,
    })
  },

  removePaperFromCollection: async (paperId: number, collectionId: number): Promise<void> => {
    await api.post('/api/literature/collections/remove-paper', {
      paper_id: paperId,
      collection_id: collectionId,
    })
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
