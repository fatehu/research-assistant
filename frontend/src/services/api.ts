import axios, { AxiosError } from 'axios'

// API 基础配置
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

// 创建 axios 实例
const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,  // 30 秒超时
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
    onEvent?: (event: string, data: unknown) => void,
    abortController?: AbortController
  ): Promise<void> => {
    try {
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
        signal: abortController?.signal,
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
    } catch (error) {
      // 如果是中止错误，触发 stopped 事件
      if (error instanceof Error && error.name === 'AbortError') {
        onEvent?.('stopped', { aborted: true })
        return
      }
      throw error
    }
  },
  
  // 搜索消息
  searchMessages: async (query: string, limit = 20): Promise<{
    query: string
    total: number
    results: Array<{
      message_id: number
      conversation_id: number
      conversation_title: string
      role: string
      content_snippet: string
      created_at: string
    }>
  }> => {
    const response = await api.get('/api/chat/messages/search', {
      params: { q: query, limit },
    })
    return response.data
  },
  
  // 保存停止的消息
  saveStoppedMessage: async (data: {
    conversation_id: number
    content: string
    thought?: string
    react_steps?: Array<{
      type: string
      iteration: number
      content?: string
      tool?: string
      input?: Record<string, unknown>
      output?: string
      success?: boolean
    }>
  }): Promise<Message> => {
    const response = await api.post('/api/chat/messages/stopped', data)
    return response.data
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

// ========== 代码实验室类型 ==========

export interface CellOutput {
  output_type: 'stream' | 'execute_result' | 'display_data' | 'error'
  content: any
  mime_type?: string
}

export interface Cell {
  id: string
  cell_type: 'code' | 'markdown'
  source: string
  outputs: CellOutput[]
  execution_count: number | null
  metadata: Record<string, any>
}

export interface Notebook {
  id: string
  user_id: number
  title: string
  description?: string
  cells: Cell[]
  created_at: string
  updated_at: string
  execution_count: number
}

export interface ExecuteRequest {
  code: string
  cell_id?: string
  timeout?: number
}

export interface ExecuteResponse {
  success: boolean
  outputs: CellOutput[]
  execution_count: number
  execution_time_ms: number
}

// ========== 代码实验室 API ==========

export const codelabApi = {
  // 获取 Notebook 列表
  listNotebooks: async (): Promise<Notebook[]> => {
    const response = await api.get('/api/codelab/notebooks')
    return response.data
  },

  // 创建 Notebook
  createNotebook: async (data: { title?: string; description?: string }): Promise<Notebook> => {
    const response = await api.post('/api/codelab/notebooks', data)
    return response.data
  },

  // 获取 Notebook 详情
  getNotebook: async (notebookId: string): Promise<Notebook> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}`)
    return response.data
  },

  // 更新 Notebook
  updateNotebook: async (
    notebookId: string,
    data: { title?: string; description?: string; cells?: Cell[] }
  ): Promise<Notebook> => {
    const response = await api.patch(`/api/codelab/notebooks/${notebookId}`, data)
    return response.data
  },

  // 删除 Notebook
  deleteNotebook: async (notebookId: string): Promise<void> => {
    await api.delete(`/api/codelab/notebooks/${notebookId}`)
  },

  // 执行代码单元格
  executeCell: async (notebookId: string, data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/execute`, data)
    return response.data
  },

  // 直接执行代码（不保存）
  executeCode: async (data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post('/api/codelab/execute', data)
    return response.data
  },

  // 添加单元格
  addCell: async (notebookId: string, cellType: 'code' | 'markdown', index?: number): Promise<Cell> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/cells`, null, {
      params: { cell_type: cellType, index },
    })
    return response.data
  },

  // 删除单元格
  deleteCell: async (notebookId: string, cellId: string): Promise<void> => {
    await api.delete(`/api/codelab/notebooks/${notebookId}/cells/${cellId}`)
  },

  // 运行所有单元格
  runAll: async (notebookId: string): Promise<{ message: string; results: any[] }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/run-all`)
    return response.data
  },

  // 重启内核（清除所有变量状态）
  restartKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/restart-kernel`)
    return response.data
  },

  // 获取内核状态
  getKernelStatus: async (notebookId: string): Promise<{
    status: 'running' | 'stopped'
    execution_count: number
    created_at?: string
    last_used_at?: string
    variables: Record<string, string>
  }> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}/kernel-status`)
    return response.data
  },

  // 中断内核执行
  interruptKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/interrupt`)
    return response.data
  },
}

// ========== Notebook Agent 类型 ==========

export interface AgentCodeBlock {
  id: string
  language: string
  code: string
}

export interface AgentMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  code_blocks: AgentCodeBlock[]
  timestamp: string
  metadata: Record<string, any>
}

export interface AgentContextResponse {
  notebook_id: string
  notebook_title: string
  cell_count: number
  execution_count: number
  variables: Record<string, string>
  recent_outputs: Array<{
    cell_id: string
    execution_count: number | null
    outputs: CellOutput[]
  }>
  code_summary: string
}

export interface AgentChatRequest {
  message: string
  include_context?: boolean
  include_variables?: boolean
  user_authorized?: boolean  // 是否授权 AI 操作 Notebook
  stream?: boolean
}

export interface AgentChatEvent {
  type: 'content' | 'done' | 'error' | 'thought' | 'action' | 'observation' | 'answer' | 'start' | 'authorization_required'
  content?: string
  code_blocks?: AgentCodeBlock[]
  suggested_action?: string
  suggested_code?: string
  error?: string
  tool?: string
  input?: Record<string, any>
  success?: boolean
  output?: string
  action?: string  // 需要授权的操作
  provider?: string
  model?: string
  notebook_updated?: boolean  // Notebook 是否有更新（新增 Cell）
  cell_id?: string  // 新创建的 Cell ID
  new_cell?: Cell   // 新创建的完整 Cell 数据
  updated_cell?: Cell  // 更新的 Cell 数据
}

// ========== Notebook Agent API ==========

export const agentApi = {
  // 获取 Notebook 上下文
  getContext: async (notebookId: string): Promise<AgentContextResponse> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}/agent/context`)
    return response.data
  },

  // 获取对话历史
  getHistory: async (notebookId: string): Promise<{
    notebook_id: string
    messages: AgentMessage[]
    created_at: string
    updated_at: string
  }> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  // 清空对话历史
  clearHistory: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.delete(`/api/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  // 流式对话
  chat: async (
    notebookId: string,
    request: AgentChatRequest,
    onEvent: (event: AgentChatEvent) => void,
    abortController?: AbortController
  ): Promise<void> => {
    const response = await fetch(
      `${API_BASE_URL}/api/codelab/notebooks/${notebookId}/agent/chat`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify(request),
        signal: abortController?.signal,
      }
    )

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || '请求失败')
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
            onEvent(data as AgentChatEvent)
          } catch (e) {
            console.error('解析事件失败:', e)
          }
        }
      }
    }
  },

  // 非流式对话
  chatSync: async (
    notebookId: string,
    request: Omit<AgentChatRequest, 'stream'>
  ): Promise<{
    message: AgentMessage
    suggested_code?: string
    suggested_action?: string
  }> => {
    const response = await api.post(
      `/api/codelab/notebooks/${notebookId}/agent/chat`,
      { ...request, stream: false }
    )
    return response.data
  },

  // 生成代码建议
  suggestCode: async (
    notebookId: string,
    description: string
  ): Promise<{
    description: string
    code: string
    full_response: string
  }> => {
    const response = await api.post(
      `/api/codelab/notebooks/${notebookId}/agent/suggest-code`,
      null,
      { params: { description } }
    )
    return response.data
  },

  // 解释错误
  explainError: async (
    notebookId: string,
    errorMessage: string,
    code?: string
  ): Promise<{
    explanation: string
    fix_code?: string
  }> => {
    const response = await api.post(
      `/api/codelab/notebooks/${notebookId}/agent/explain-error`,
      null,
      { params: { error_message: errorMessage, code } }
    )
    return response.data
  },

  // 分析数据
  analyzeData: async (
    notebookId: string,
    variableName: string,
    analysisType: 'overview' | 'statistics' | 'distribution' | 'correlation' = 'overview'
  ): Promise<{
    variable_name: string
    analysis_type: string
    suggested_code: string
    description: string
  }> => {
    const response = await api.post(
      `/api/codelab/notebooks/${notebookId}/agent/analyze-data`,
      null,
      { params: { variable_name: variableName, analysis_type: analysisType } }
    )
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

// ========== 角色系统类型定义 ==========

export enum UserRole {
  ADMIN = 'admin',
  MENTOR = 'mentor', 
  STUDENT = 'student',
}

export enum InvitationStatus {
  PENDING = 'pending',
  ACCEPTED = 'accepted',
  REJECTED = 'rejected',
  CANCELLED = 'cancelled',
}

export enum ShareType {
  KNOWLEDGE_BASE = 'knowledge_base',
  PAPER_COLLECTION = 'paper_collection',
  NOTEBOOK = 'notebook',
}

export enum SharePermission {
  READ = 'read',
  WRITE = 'write',
  ADMIN = 'admin',
}

// 师生关系状态
export enum MentorshipStatus {
  NONE = 'none',           // 无导师
  PENDING = 'pending',     // 申请中
  ACTIVE = 'active',       // 已建立关系
  INVITED = 'invited',     // 被邀请
}

// 用户信息（扩展了角色字段）
export interface UserWithRole {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  role: UserRole
  mentor_id?: number
  department?: string
  research_direction?: string
  joined_at?: string
  is_active: boolean
  created_at: string
  last_login?: string
}

// 学生详情（含统计）
export interface StudentDetail extends UserWithRole {
  conversation_count: number
  knowledge_base_count: number
  paper_count: number
  notebook_count: number
}

// 研究组
export interface ResearchGroup {
  id: number
  name: string
  description?: string
  mentor_id: number
  avatar?: string
  is_active: boolean
  max_members: number
  member_count?: number
  members?: GroupMember[]
  created_at: string
}

// 组成员
export interface GroupMember {
  id: number
  group_id: number
  user_id: number
  role: string
  joined_at: string
  user?: UserWithRole
}

// 邀请
export interface Invitation {
  id: number
  type: 'invite' | 'apply'
  from_user_id: number
  to_user_id: number
  group_id?: number
  message?: string
  status: InvitationStatus
  responded_at?: string
  created_at: string
  expires_at?: string
  from_user?: UserWithRole
  to_user?: UserWithRole
  group?: ResearchGroup
}

// 共享资源
export interface SharedResource {
  id: number
  resource_type: ShareType
  resource_id: number
  owner_id: number
  shared_with_type: 'user' | 'group' | 'all_students'
  shared_with_id?: number
  permission: SharePermission
  created_at: string
  expires_at?: string
  owner?: UserWithRole
  resource_name?: string
}

// 公告
export interface Announcement {
  id: number
  mentor_id: number
  group_id?: number
  title: string
  content: string
  is_pinned: boolean
  is_active: boolean
  created_at: string
  updated_at: string
  mentor?: UserWithRole
  group?: ResearchGroup
  is_read?: boolean
  read_count?: number
}

// 系统统计
export interface SystemStatistics {
  total_users: number
  admin_count: number
  mentor_count: number
  student_count: number
  active_users: number
  total_conversations: number
  total_knowledge_bases: number
  total_papers: number
  total_notebooks: number
}

// ========== 管理员 API ==========

export const adminApi = {
  // 获取所有用户
  getUsers: async (params?: {
    skip?: number
    limit?: number
    role?: UserRole
    search?: string
    is_active?: boolean
  }): Promise<UserWithRole[]> => {
    const response = await api.get('/api/admin/users', { params })
    return response.data
  },

  // 获取用户详情
  getUser: async (userId: number): Promise<UserWithRole> => {
    const response = await api.get(`/api/admin/users/${userId}`)
    return response.data
  },

  // 更新用户角色
  updateUserRole: async (userId: number, role: UserRole): Promise<UserWithRole> => {
    const response = await api.put(`/api/admin/users/${userId}/role`, { role })
    return response.data
  },

  // 切换用户激活状态
  toggleUserActive: async (userId: number): Promise<{ is_active: boolean }> => {
    const response = await api.put(`/api/admin/users/${userId}/toggle-active`)
    return response.data
  },

  // 删除用户
  deleteUser: async (userId: number): Promise<void> => {
    await api.delete(`/api/admin/users/${userId}`)
  },

  // 获取系统统计
  getStatistics: async (): Promise<SystemStatistics> => {
    const response = await api.get('/api/admin/statistics')
    return response.data
  },

  // 设置用户导师
  setUserMentor: async (userId: number, mentorId: number | null): Promise<UserWithRole> => {
    const response = await api.put(`/api/admin/users/${userId}/mentor`, { mentor_id: mentorId })
    return response.data
  },
}

// ========== 导师 API ==========

export const mentorApi = {
  // 获取我的学生列表
  getStudents: async (): Promise<StudentDetail[]> => {
    const response = await api.get('/api/mentor/students')
    return response.data
  },

  // 获取学生详情
  getStudentDetail: async (studentId: number): Promise<StudentDetail> => {
    const response = await api.get(`/api/mentor/students/${studentId}`)
    return response.data
  },

  // 邀请学生
  inviteStudent: async (email: string, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/mentor/students/invite', { email, message })
    return response.data
  },

  // 移除学生
  removeStudent: async (studentId: number): Promise<void> => {
    await api.delete(`/api/mentor/students/${studentId}`)
  },

  // 获取发出的邀请
  getSentInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/mentor/invitations/sent')
    return response.data
  },

  // 取消邀请
  cancelInvitation: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/mentor/invitations/${invitationId}`)
  },

  // 处理学生申请
  handleApplication: async (invitationId: number, accept: boolean): Promise<void> => {
    const response = await api.post(`/api/mentor/applications/${invitationId}/${accept ? 'accept' : 'reject'}`)
    return response.data
  },

  // 获取研究组列表
  getGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/mentor/groups')
    return response.data
  },

  // 创建研究组
  createGroup: async (data: {
    name: string
    description?: string
    max_members?: number
  }): Promise<ResearchGroup> => {
    const response = await api.post('/api/mentor/groups', data)
    return response.data
  },

  // 更新研究组
  updateGroup: async (groupId: number, data: {
    name?: string
    description?: string
    max_members?: number
    is_active?: boolean
  }): Promise<ResearchGroup> => {
    const response = await api.put(`/api/mentor/groups/${groupId}`, data)
    return response.data
  },

  // 删除研究组
  deleteGroup: async (groupId: number): Promise<void> => {
    await api.delete(`/api/mentor/groups/${groupId}`)
  },

  // 获取组成员
  getGroupMembers: async (groupId: number): Promise<GroupMember[]> => {
    const response = await api.get(`/api/mentor/groups/${groupId}/members`)
    return response.data
  },

  // 添加组成员
  addGroupMember: async (groupId: number, userId: number): Promise<GroupMember> => {
    const response = await api.post(`/api/mentor/groups/${groupId}/members`, { user_id: userId })
    return response.data
  },

  // 移除组成员
  removeGroupMember: async (groupId: number, userId: number): Promise<void> => {
    await api.delete(`/api/mentor/groups/${groupId}/members/${userId}`)
  },
}

// ========== 学生 API ==========

export const studentApi = {
  // 获取我的导师
  getMentor: async (): Promise<UserWithRole | null> => {
    try {
      const response = await api.get('/api/student/mentor')
      return response.data
    } catch (error: any) {
      if (error.response?.status === 404) {
        return null
      }
      throw error
    }
  },

  // 搜索导师
  searchMentors: async (query: string): Promise<UserWithRole[]> => {
    const response = await api.get('/api/student/mentors/search', { params: { query } })
    return response.data
  },

  // 申请导师
  applyToMentor: async (mentorId: number, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/student/mentor/apply', { mentor_id: mentorId, message })
    return response.data
  },

  // 离开导师
  leaveMentor: async (): Promise<void> => {
    await api.delete('/api/student/mentor/leave')
  },

  // 获取我的申请
  getMyApplications: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/student/applications')
    return response.data
  },

  // 取消申请
  cancelApplication: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/student/applications/${invitationId}`)
  },

  // 获取收到的邀请
  getReceivedInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/student/invitations')
    return response.data
  },

  // 接受邀请
  acceptInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/student/invitations/${invitationId}/accept`)
  },

  // 拒绝邀请
  rejectInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/student/invitations/${invitationId}/reject`)
  },

  // 获取我加入的研究组
  getMyGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/student/groups')
    return response.data
  },
}

// ========== 邀请 API ==========

export const invitationApi = {
  // 获取我的所有邀请（收到和发出的）
  getAll: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/invitations')
    return response.data
  },

  // 接受邀请
  accept: async (invitationId: number): Promise<void> => {
    await api.post(`/api/invitations/${invitationId}/accept`)
  },

  // 拒绝邀请
  reject: async (invitationId: number): Promise<void> => {
    await api.post(`/api/invitations/${invitationId}/reject`)
  },

  // 取消邀请
  cancel: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/invitations/${invitationId}`)
  },
}

// ========== 共享资源 API ==========

export const shareApi = {
  // 获取共享给我的资源
  getSharedWithMe: async (): Promise<SharedResource[]> => {
    const response = await api.get('/api/share/received')
    return response.data
  },

  // 获取我共享出去的资源
  getMyShares: async (): Promise<SharedResource[]> => {
    const response = await api.get('/api/share/sent')
    return response.data
  },

  // 共享资源
  shareResource: async (data: {
    resource_type: ShareType
    resource_id: number
    shared_with_type: 'user' | 'group' | 'all_students'
    shared_with_id?: number
    permission?: SharePermission
    expires_at?: string
  }): Promise<SharedResource> => {
    const response = await api.post('/api/share', data)
    return response.data
  },

  // 更新共享权限
  updatePermission: async (shareId: number, permission: SharePermission): Promise<SharedResource> => {
    const response = await api.put(`/api/share/${shareId}`, { permission })
    return response.data
  },

  // 取消共享
  removeShare: async (shareId: number): Promise<void> => {
    await api.delete(`/api/share/${shareId}`)
  },
}

// ========== 公告 API ==========

export const announcementApi = {
  // 获取公告列表（学生看到的）
  getAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/announcements')
    return response.data
  },

  // 获取我发布的公告（导师）
  getMyAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/announcements/my')
    return response.data
  },

  // 创建公告
  createAnnouncement: async (data: {
    title: string
    content: string
    group_id?: number
    is_pinned?: boolean
  }): Promise<Announcement> => {
    const response = await api.post('/api/announcements', data)
    return response.data
  },

  // 更新公告
  updateAnnouncement: async (announcementId: number, data: {
    title?: string
    content?: string
    is_pinned?: boolean
    is_active?: boolean
  }): Promise<Announcement> => {
    const response = await api.put(`/api/announcements/${announcementId}`, data)
    return response.data
  },

  // 删除公告
  deleteAnnouncement: async (announcementId: number): Promise<void> => {
    await api.delete(`/api/announcements/${announcementId}`)
  },

  // 标记已读
  markAsRead: async (announcementId: number): Promise<void> => {
    await api.post(`/api/announcements/${announcementId}/read`)
  },

  // 获取公告阅读统计
  getReadStats: async (announcementId: number): Promise<{
    total_count: number
    read_count: number
    readers: Array<{ user_id: number; username: string; read_at: string }>
  }> => {
    const response = await api.get(`/api/announcements/${announcementId}/stats`)
    return response.data
  },
}

// ========== 师生关系 API ==========

export const mentorshipApi = {
  // 获取当前师生关系状态
  getStatus: async (): Promise<{
    status: MentorshipStatus
    mentor?: UserWithRole
    pendingInvitations?: Invitation[]
    pendingApplications?: Invitation[]
  }> => {
    try {
      const mentor = await studentApi.getMentor()
      if (mentor) {
        return { status: MentorshipStatus.ACTIVE, mentor }
      }
      
      // 检查是否有待处理的邀请或申请
      const invitations = await invitationApi.getAll()
      const pendingInvitations = invitations.filter(i => i.type === 'invite' && i.status === 'pending')
      const pendingApplications = invitations.filter(i => i.type === 'apply' && i.status === 'pending')
      
      if (pendingInvitations.length > 0) {
        return { status: MentorshipStatus.INVITED, pendingInvitations }
      }
      if (pendingApplications.length > 0) {
        return { status: MentorshipStatus.PENDING, pendingApplications }
      }
      
      return { status: MentorshipStatus.NONE }
    } catch (error) {
      return { status: MentorshipStatus.NONE }
    }
  },

  // 获取导师信息
  getMentor: async (): Promise<UserWithRole | null> => {
    return studentApi.getMentor()
  },

  // 搜索可用导师
  searchMentors: async (query: string): Promise<UserWithRole[]> => {
    return studentApi.searchMentors(query)
  },

  // 申请成为某导师的学生
  applyToMentor: async (mentorId: number, message?: string): Promise<Invitation> => {
    return studentApi.applyToMentor(mentorId, message)
  },

  // 离开当前导师
  leaveMentor: async (): Promise<void> => {
    return studentApi.leaveMentor()
  },

  // 接受导师邀请
  acceptInvitation: async (invitationId: number): Promise<void> => {
    return studentApi.acceptInvitation(invitationId)
  },

  // 拒绝导师邀请
  rejectInvitation: async (invitationId: number): Promise<void> => {
    return studentApi.rejectInvitation(invitationId)
  },

  // 取消申请
  cancelApplication: async (invitationId: number): Promise<void> => {
    return studentApi.cancelApplication(invitationId)
  },

  // 获取我的申请列表
  getMyApplications: async (): Promise<Invitation[]> => {
    return studentApi.getMyApplications()
  },

  // 获取收到的邀请
  getReceivedInvitations: async (): Promise<Invitation[]> => {
    return studentApi.getReceivedInvitations()
  },
}

export default api
