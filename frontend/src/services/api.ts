import axios, { AxiosError } from 'axios'

// API 鍩虹閰嶇疆
const VITE_ENV = ((import.meta as any).env || {}) as Record<string, string | undefined>
const API_BASE_URL = VITE_ENV.VITE_API_BASE_URL || 'http://localhost:8000'
export const SHOW_RAG_METRICS = VITE_ENV.VITE_SHOW_RAG_METRICS === 'true'

// 鍒涘缓 axios 瀹炰緥
const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,  // 30 绉掕秴鏃?
  headers: {
    'Content-Type': 'application/json',
  },
})

// 璇锋眰鎷︽埅鍣?- 娣诲姞 token
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

// 鍝嶅簲鎷︽埅鍣?- 澶勭悊閿欒
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

// 绫诲瀷瀹氫箟
export interface UserProfileData {
  title?: string
  department?: string
  research_area?: string
  bio?: string
  contact?: string
  [key: string]: unknown
}

export interface User {
  id: number
  email: string
  username: string
  full_name?: string
  avatar?: string
  bio?: string
  role?: UserRole
  mentor_id?: number
  department?: string
  research_direction?: string
  profile_data?: UserProfileData
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

export interface RagMetrics {
  knowledge_search_calls: number
  source_labels_count: number
  source_labels: string[]
  answer_citation_count: number
  citation_required: boolean
  citation_valid: boolean
  citation_repair_attempts: number
  citation_repair_successes: number
  compression_calls: number
  compression_success_chunks: number
  compression_fallback_chunks: number
}

export interface MessageMetadata extends Record<string, unknown> {
  rag_metrics?: RagMetrics
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
  metadata?: MessageMetadata
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

// ========== 鐭ヨ瘑搴撶被鍨?==========

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

export interface EmbeddingModel {
  id: string
  name: string
  dimension: number
  provider: string
  description: string
  max_tokens: number
  is_current: boolean
  compatible: boolean
}

export interface EmbeddingModelsResponse {
  models: EmbeddingModel[]
  current_model: string
  current_provider: string
  current_dimension: number
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
  // [Fix 12] 灞傜骇妫€绱㈡柊澧炲瓧娈?
  chunk_level?: string          // 鍒嗗潡灞傜骇: paragraph / section / document
  section_type?: string         // 绔犺妭绫诲瀷: abstract / methodology / results 绛?
  section_title?: string        // 绔犺妭鏍囬
  parent_context?: string       // 鐖剁骇 chunk 鐨勫唴瀹规憳瑕侊紙鍓?300 瀛楃锛?
}

export interface SearchResponse {
  query: string
  results: SearchResult[]
  total: number
  search_time_ms: number
}

export interface KnowledgeSearchOptions {
  useReranker?: boolean
  useHybrid?: boolean
  useQueryRewrite?: boolean
  rewriteMode?: 'auto' | 'force' | 'off'
  useContextualCompression?: boolean
  includeAdjacentChunks?: boolean
  adjacentWindow?: number
  queryRewriteStrategies?: string[]
  timeoutMs?: number
  signal?: AbortSignal
}

export const isApiCanceledError = (error: unknown): boolean => {
  return axios.isAxiosError(error) && error.code === 'ERR_CANCELED'
}

export const isApiTimeoutError = (error: unknown): boolean => {
  if (!axios.isAxiosError(error)) {
    return false
  }
  const code = String(error.code || '')
  const status = error.response?.status
  const msg = String(error.message || '').toLowerCase()
  return code === 'ECONNABORTED' || status === 504 || msg.includes('timeout')
}

export interface ProcessingStatus {
  document_id: number
  status: string
  progress: number
  message: string
  chunk_count: number
  error?: string
}

// 璁よ瘉 API
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

// 鐢ㄦ埛 API
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

// 鑱婂ぉ API
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

  // 娴佸紡鍙戦€佹秷鎭?
  sendMessageStream: async (
    message: string,
    conversationId?: number,
    onEvent?: (event: string, data: any) => void,
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
        throw new Error(error.detail || '请求失败')
      }

      const reader = response.body?.getReader()
      if (!reader) throw new Error('鏃犳硶璇诲彇鍝嶅簲')

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
              // 蹇界暐瑙ｆ瀽閿欒
            }
          }
        }
      }
    } catch (error) {
      // 濡傛灉鏄腑姝㈤敊璇紝瑙﹀彂 stopped 浜嬩欢
      if (error instanceof Error && error.name === 'AbortError') {
        onEvent?.('stopped', { aborted: true })
        return
      }
      throw error
    }
  },

  // 鎼滅储娑堟伅
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

  // 淇濆瓨鍋滄鐨勬秷鎭?
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

// ========== 鐭ヨ瘑搴?API ==========

export const knowledgeApi = {
  // 鐭ヨ瘑搴?CRUD
  getKnowledgeBases: async (skip = 0, limit = 20): Promise<{ items: KnowledgeBase[]; total: number }> => {
    const response = await api.get('/api/knowledge/knowledge-bases', {
      params: { skip, limit },
    })
    return response.data
  },

  // 鑾峰彇鍙敤鐨勭煡璇嗗簱锛堣嚜宸辩殑 + 鍏变韩鐨勶級锛岀敤浜嶢I瀵硅瘽閫夋嫨
  getAvailableKnowledgeBases: async (): Promise<{
    own: { id: number; name: string; description?: string; document_count: number; total_chunks: number }[];
    shared: { id: number; name: string; description?: string; document_count: number; total_chunks: number; owner_id: number; owner_name: string }[];
    sharing_enabled: boolean;
  }> => {
    const response = await api.get('/api/knowledge/available')
    return response.data
  },

  createKnowledgeBase: async (data: KnowledgeBaseCreate): Promise<KnowledgeBase> => {
    const response = await api.post('/api/knowledge/knowledge-bases', data)
    return response.data
  },

  getEmbeddingModels: async (): Promise<EmbeddingModelsResponse> => {
    const response = await api.get('/api/knowledge/embedding-models')
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

  // 鏂囨。绠＄悊
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

  // 鍒嗙墖
  getChunks: async (kbId: number, docId: number, skip = 0, limit = 20): Promise<{ items: DocumentChunk[]; total: number }> => {
    const response = await api.get(`/api/knowledge/knowledge-bases/${kbId}/documents/${docId}/chunks`, {
      params: { skip, limit },
    })
    return response.data
  },

  // 鎼滅储锛堟敮鎸佸眰绾ц繃婊?+ 鐖剁骇涓婁笅鏂囧洖婧級
  search: async (
    query: string,
    knowledgeBaseIds?: number[],
    topK = 5,
    scoreThreshold = 0.5,
    includeShared = false,
    chunkLevel: string = 'paragraph',
    sectionType?: string,
    includeParentContext = false,
    options: KnowledgeSearchOptions = {},
  ): Promise<SearchResponse> => {
    const {
      useReranker = true,
      useHybrid = true,
      useQueryRewrite = true,
      rewriteMode = 'auto',
      useContextualCompression = true,
      includeAdjacentChunks = false,
      adjacentWindow = 1,
      queryRewriteStrategies,
      timeoutMs = 300000,
      signal,
    } = options
    const normalizedAdjacentWindow = Math.max(1, Math.min(3, adjacentWindow))

    const response = await api.post('/api/knowledge/search', {
      query,
      knowledge_base_ids: knowledgeBaseIds,
      top_k: topK,
      score_threshold: scoreThreshold,
      use_reranker: useReranker,
      use_hybrid: useHybrid,
      use_query_rewrite: useQueryRewrite,
      rewrite_mode: rewriteMode,
      use_contextual_compression: useContextualCompression,
      include_adjacent_chunks: includeAdjacentChunks,
      adjacent_window: normalizedAdjacentWindow,
      query_rewrite_strategies: queryRewriteStrategies,
      chunk_level: chunkLevel,
      section_type: sectionType || undefined,
      include_parent_context: includeParentContext,
    }, {
      params: { include_shared: includeShared },
      timeout: timeoutMs,
      signal,
    })
    return response.data
  },
}

// ========== 鏂囩尞绠＄悊绫诲瀷 ==========

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

export interface GraphNode {
  id: string
  title: string
  type: 'center' | 'citing' | 'referenced'
  level: number
  citations: number
  year?: number
  authors?: string[]
}

export interface GraphEdge {
  from: string
  to: string
}

export interface CitationGraph {
  center_id: string
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// ========== 鏂囩尞绠＄悊 API ==========

export const literatureApi = {
  // 鍒濆鍖?
  init: async (): Promise<{ message: string }> => {
    const response = await api.post('/api/literature/init')
    return response.data
  },

  // 鎼滅储璁烘枃
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

  // 鑾峰彇鎼滅储鍘嗗彶
  getSearchHistory: async (limit = 20): Promise<SearchHistory[]> => {
    const response = await api.get('/api/literature/search/history', {
      params: { limit },
    })
    return response.data
  },

  // 鑾峰彇璁烘枃鍒楄〃
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

  // 鑾峰彇璁烘枃璇︽儏
  getPaper: async (paperId: number): Promise<Paper> => {
    const response = await api.get(`/api/literature/papers/${paperId}`)
    return response.data
  },

  // 淇濆瓨璁烘枃
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

  // 鏇存柊璁烘枃
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

  // 鍒犻櫎璁烘枃
  deletePaper: async (paperId: number): Promise<void> => {
    await api.delete(`/api/literature/papers/${paperId}`)
  },

  // 涓嬭浇 PDF
  downloadPdf: async (
    paperId: number,
    knowledgeBaseId?: number
  ): Promise<{ message: string; pdf_path: string }> => {
    const response = await api.post(`/api/literature/papers/${paperId}/download-pdf`, null, {
      params: { knowledge_base_id: knowledgeBaseId },
    })
    return response.data
  },

  // 鏀惰棌澶圭鐞?
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

// ========== 浠ｇ爜瀹為獙瀹ょ被鍨?==========

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

// ========== 浠ｇ爜瀹為獙瀹?API ==========

export const codelabApi = {
  // 鑾峰彇 Notebook 鍒楄〃
  listNotebooks: async (): Promise<Notebook[]> => {
    const response = await api.get('/api/codelab/notebooks')
    return response.data
  },

  // 鍒涘缓 Notebook
  createNotebook: async (data: { title?: string; description?: string }): Promise<Notebook> => {
    const response = await api.post('/api/codelab/notebooks', data)
    return response.data
  },

  // 鑾峰彇 Notebook 璇︽儏
  getNotebook: async (notebookId: string): Promise<Notebook> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}`)
    return response.data
  },

  // 鏇存柊 Notebook
  updateNotebook: async (
    notebookId: string,
    data: { title?: string; description?: string; cells?: Cell[] }
  ): Promise<Notebook> => {
    const response = await api.patch(`/api/codelab/notebooks/${notebookId}`, data)
    return response.data
  },

  // 鍒犻櫎 Notebook
  deleteNotebook: async (notebookId: string): Promise<void> => {
    await api.delete(`/api/codelab/notebooks/${notebookId}`)
  },

  // 鎵ц浠ｇ爜鍗曞厓鏍?
  executeCell: async (notebookId: string, data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/execute`, data)
    return response.data
  },

  // 鐩存帴鎵ц浠ｇ爜锛堜笉淇濆瓨锛?
  executeCode: async (data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post('/api/codelab/execute', data)
    return response.data
  },

  // 娣诲姞鍗曞厓鏍?
  addCell: async (notebookId: string, cellType: 'code' | 'markdown', index?: number): Promise<Cell> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/cells`, null, {
      params: { cell_type: cellType, index },
    })
    return response.data
  },

  // 鍒犻櫎鍗曞厓鏍?
  deleteCell: async (notebookId: string, cellId: string): Promise<void> => {
    await api.delete(`/api/codelab/notebooks/${notebookId}/cells/${cellId}`)
  },

  // 杩愯鎵€鏈夊崟鍏冩牸
  runAll: async (notebookId: string): Promise<{ message: string; results: any[] }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/run-all`)
    return response.data
  },

  // 閲嶅惎鍐呮牳锛堟竻闄ゆ墍鏈夊彉閲忕姸鎬侊級
  restartKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/restart-kernel`)
    return response.data
  },

  // 鑾峰彇鍐呮牳鐘舵€?
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

  // 涓柇鍐呮牳鎵ц
  interruptKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/codelab/notebooks/${notebookId}/interrupt`)
    return response.data
  },
}

// ========== Notebook Agent 绫诲瀷 ==========

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
  metadata: {
    rag_metrics?: RagMetrics
    [key: string]: any
  }
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
  user_authorized?: boolean  // 鏄惁鎺堟潈 AI 鎿嶄綔 Notebook
  stream?: boolean
}

export interface AgentChatEvent {
  type: 'content' | 'done' | 'error' | 'thought' | 'action' | 'observation' | 'answer' | 'start' | 'authorization_required'
  content?: string
  code_blocks?: AgentCodeBlock[]
  suggested_action?: string
  suggested_code?: string
  rag_metrics?: RagMetrics
  error?: string
  tool?: string
  input?: Record<string, any>
  success?: boolean
  output?: string
  action?: string  // 闇€瑕佹巿鏉冪殑鎿嶄綔
  provider?: string
  model?: string
  notebook_updated?: boolean  // Notebook 鏄惁鏈夋洿鏂帮紙鏂板 Cell锛?
  cell_id?: string  // 鏂板垱寤虹殑 Cell ID
  new_cell?: Cell   // 鏂板垱寤虹殑瀹屾暣 Cell 鏁版嵁
  updated_cell?: Cell  // 鏇存柊鐨?Cell 鏁版嵁
}

// ========== Notebook Agent API ==========

export const agentApi = {
  // 鑾峰彇 Notebook 涓婁笅鏂?
  getContext: async (notebookId: string): Promise<AgentContextResponse> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}/agent/context`)
    return response.data
  },

  // 鑾峰彇瀵硅瘽鍘嗗彶
  getHistory: async (notebookId: string): Promise<{
    notebook_id: string
    messages: AgentMessage[]
    created_at: string
    updated_at: string
  }> => {
    const response = await api.get(`/api/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  // 娓呯┖瀵硅瘽鍘嗗彶
  clearHistory: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.delete(`/api/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  // 娴佸紡瀵硅瘽
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
      throw new Error(error.detail || '璇锋眰澶辫触')
    }

    const reader = response.body?.getReader()
    if (!reader) throw new Error('鏃犳硶璇诲彇鍝嶅簲')

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
            console.error('瑙ｆ瀽浜嬩欢澶辫触:', e)
          }
        }
      }
    }
  },

  // 闈炴祦寮忓璇?
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

  // 鐢熸垚浠ｇ爜寤鸿
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

  // 瑙ｉ噴閿欒
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

  // 鍒嗘瀽鏁版嵁
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

// 杈呭姪鍑芥暟
function getToken(): string {
  const authStorage = localStorage.getItem('auth-storage')
  if (authStorage) {
    const { state } = JSON.parse(authStorage)
    return state?.token || ''
  }
  return ''
}

// ========== 瑙掕壊绯荤粺绫诲瀷瀹氫箟 ==========

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

// 甯堢敓鍏崇郴鐘舵€?
export enum MentorshipStatus {
  NONE = 'none',
  PENDING = 'pending',
  ACTIVE = 'active',
  INVITED = 'invited',
  ARCHIVED = 'archived',
}

// 鐢ㄦ埛淇℃伅锛堟墿灞曚簡瑙掕壊瀛楁锛?
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

export interface UserBrief {
  id: number
  username: string
  full_name?: string
  avatar?: string
  role: UserRole
  profile_data?: UserProfileData
}

export interface Mentorship {
  id: number
  mentor_id: number
  student_id: number
  status: MentorshipStatus
  request_message?: string
  response_message?: string
  metadata?: Record<string, unknown>
  created_at: string
  updated_at: string
  approved_at?: string
  archived_at?: string
  mentor?: UserBrief
  student?: UserBrief
}

export interface MentorshipListResponse {
  items: Mentorship[]
  total: number
}

// 瀛︾敓璇︽儏锛堝惈缁熻锛?
export interface StudentDetail extends UserWithRole {
  conversation_count: number
  knowledge_base_count: number
  paper_count: number
  notebook_count: number
}

// 鐮旂┒缁?
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

// 缁勬垚鍛?
export interface GroupMember {
  id: number
  group_id: number
  user_id: number
  role: string
  joined_at: string
  user?: UserWithRole
}

// 閭€璇?
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

// 鍏变韩璧勬簮
export interface SharedResource {
  id: number
  resource_type: ShareType
  resource_id: number | string
  owner_id: number
  owner_name?: string
  owner_avatar?: string
  shared_with_type: 'user' | 'group' | 'all_students'
  shared_with_id?: number
  shared_with_name?: string
  group_name?: string
  permission: SharePermission
  created_at: string
  shared_at?: string
  expires_at?: string
  owner?: UserWithRole
  resource_name?: string
  resource_detail?: Record<string, unknown>
}

// 鍏憡
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

// 绯荤粺缁熻
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

// ========== 绠＄悊鍛?API ==========

export const adminApi = {
  // 鑾峰彇鎵€鏈夌敤鎴?
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

  // 鑾峰彇鐢ㄦ埛璇︽儏
  getUser: async (userId: number): Promise<UserWithRole> => {
    const response = await api.get(`/api/admin/users/${userId}`)
    return response.data
  },

  // 鏇存柊鐢ㄦ埛瑙掕壊
  updateUserRole: async (userId: number, role: UserRole): Promise<UserWithRole> => {
    const response = await api.put(`/api/admin/users/${userId}/role`, { role })
    return response.data
  },

  // 鍒囨崲鐢ㄦ埛婵€娲荤姸鎬?
  toggleUserActive: async (userId: number): Promise<{ is_active: boolean }> => {
    const response = await api.put(`/api/admin/users/${userId}/toggle-active`)
    return response.data
  },

  // 鍒犻櫎鐢ㄦ埛
  deleteUser: async (userId: number): Promise<void> => {
    await api.delete(`/api/admin/users/${userId}`)
  },

  // 鑾峰彇绯荤粺缁熻
  getStatistics: async (): Promise<SystemStatistics> => {
    const response = await api.get('/api/admin/statistics')
    return response.data
  },

  // 璁剧疆鐢ㄦ埛瀵煎笀
  setUserMentor: async (userId: number, mentorId: number | null): Promise<UserWithRole> => {
    const response = await api.put(`/api/admin/users/${userId}/mentor`, { mentor_id: mentorId })
    return response.data
  },
}

// ========== 瀵煎笀 API ==========

export const mentorApi = {
  // 鑾峰彇鎴戠殑瀛︾敓鍒楄〃
  getStudents: async (): Promise<StudentDetail[]> => {
    const response = await api.get('/api/mentor/students')
    return response.data
  },

  // 鑾峰彇瀛︾敓璇︽儏
  getStudentDetail: async (studentId: number): Promise<StudentDetail> => {
    const response = await api.get(`/api/mentor/students/${studentId}`)
    return response.data
  },

  // 閭€璇峰鐢?
  inviteStudent: async (email: string, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/mentor/students/invite', { email, message })
    return response.data
  },

  // 绉婚櫎瀛︾敓
  removeStudent: async (studentId: number): Promise<void> => {
    await api.delete(`/api/mentor/students/${studentId}`)
  },

  // 鑾峰彇鍙戝嚭鐨勯個璇?
  getSentInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/mentor/invitations/sent')
    return response.data
  },

  // 鍙栨秷閭€璇?
  cancelInvitation: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/mentor/invitations/${invitationId}`)
  },

  // 澶勭悊瀛︾敓鐢宠
  handleApplication: async (invitationId: number, accept: boolean): Promise<void> => {
    const response = await api.post(`/api/mentor/applications/${invitationId}/${accept ? 'accept' : 'reject'}`)
    return response.data
  },

  // 鑾峰彇鐮旂┒缁勫垪琛?
  getGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/mentor/groups')
    return response.data
  },

  // 鍒涘缓鐮旂┒缁?
  createGroup: async (data: {
    name: string
    description?: string
    max_members?: number
  }): Promise<ResearchGroup> => {
    const response = await api.post('/api/mentor/groups', data)
    return response.data
  },

  // 鏇存柊鐮旂┒缁?
  updateGroup: async (groupId: number, data: {
    name?: string
    description?: string
    max_members?: number
    is_active?: boolean
  }): Promise<ResearchGroup> => {
    const response = await api.put(`/api/mentor/groups/${groupId}`, data)
    return response.data
  },

  // 鍒犻櫎鐮旂┒缁?
  deleteGroup: async (groupId: number): Promise<void> => {
    await api.delete(`/api/mentor/groups/${groupId}`)
  },

  // 鑾峰彇缁勬垚鍛?
  getGroupMembers: async (groupId: number): Promise<GroupMember[]> => {
    const response = await api.get(`/api/mentor/groups/${groupId}/members`)
    return response.data
  },

  // 娣诲姞缁勬垚鍛?
  addGroupMember: async (groupId: number, userId: number): Promise<GroupMember> => {
    const response = await api.post(`/api/mentor/groups/${groupId}/members`, { user_id: userId })
    return response.data
  },

  // 绉婚櫎缁勬垚鍛?
  removeGroupMember: async (groupId: number, userId: number): Promise<void> => {
    await api.delete(`/api/mentor/groups/${groupId}/members/${userId}`)
  },
}

// ========== 瀛︾敓 API ==========

export const studentApi = {
  // 鑾峰彇鎴戠殑瀵煎笀
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

  // 鎼滅储瀵煎笀
  searchMentors: async (query: string): Promise<UserWithRole[]> => {
    const response = await api.get('/api/student/mentors/search', { params: { query } })
    return response.data
  },

  // 鐢宠瀵煎笀
  applyToMentor: async (mentorId: number, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/student/mentor/apply', { mentor_id: mentorId, message })
    return response.data
  },

  // 绂诲紑瀵煎笀
  leaveMentor: async (): Promise<void> => {
    await api.delete('/api/student/mentor/leave')
  },

  // 鑾峰彇鎴戠殑鐢宠
  getMyApplications: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/student/applications')
    return response.data
  },

  // 鍙栨秷鐢宠
  cancelApplication: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/student/applications/${invitationId}`)
  },

  // 鑾峰彇鏀跺埌鐨勯個璇?
  getReceivedInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/student/invitations')
    return response.data
  },

  // 鎺ュ彈閭€璇?
  acceptInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/student/invitations/${invitationId}/accept`)
  },

  // 鎷掔粷閭€璇?
  rejectInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/student/invitations/${invitationId}/reject`)
  },

  // 鑾峰彇鎴戝姞鍏ョ殑鐮旂┒缁?
  getMyGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/student/groups')
    return response.data
  },
}

// ========== 閭€璇?API ==========

export const invitationApi = {
  // 鑾峰彇鎴戠殑鎵€鏈夐個璇凤紙鏀跺埌鍜屽彂鍑虹殑锛?
  getAll: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/invitations')
    return response.data
  },

  // 鎺ュ彈閭€璇?
  accept: async (invitationId: number): Promise<void> => {
    await api.post(`/api/invitations/${invitationId}/accept`)
  },

  // 鎷掔粷閭€璇?
  reject: async (invitationId: number): Promise<void> => {
    await api.post(`/api/invitations/${invitationId}/reject`)
  },

  // 鍙栨秷閭€璇?
  cancel: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/invitations/${invitationId}`)
  },
}

// ========== 鍏变韩璧勬簮 API ==========

export const shareApi = {
  // 鑾峰彇鍏变韩缁欐垜鐨勮祫婧?
  getSharedWithMe: async (resourceType?: string): Promise<SharedResource[]> => {
    const params = resourceType ? { resource_type: resourceType } : {}
    const response = await api.get('/api/share/shared-with-me', { params })
    return response.data
  },

  // 鑾峰彇鍏变韩缁欐垜鐨勮祫婧愭暟閲忕粺璁?
  getSharedCount: async (): Promise<{ paper: number; paper_collection: number; knowledge_base: number; notebook: number; total: number }> => {
    const response = await api.get('/api/share/shared-with-me/count')
    return response.data
  },

  // 鑾峰彇鎴戝叡浜嚭鍘荤殑璧勬簮
  getMyShares: async (resourceType?: string): Promise<SharedResource[]> => {
    const params = resourceType ? { resource_type: resourceType } : {}
    const response = await api.get('/api/share/my-shares', { params })
    return response.data
  },

  // 鑾峰彇鍙叡浜殑鐮旂┒缁?
  getMyGroups: async (): Promise<{ id: number; name: string; role: string }[]> => {
    const response = await api.get('/api/share/my-groups')
    return response.data
  },

  // 鑾峰彇鎴戠殑璁烘枃鍒楄〃锛堢敤浜庡叡浜€夋嫨锛?
  getMyPapers: async (search?: string): Promise<{ id: number; title: string; authors: string[]; year: number; venue: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/share/my-papers', { params })
    return response.data
  },

  // 鑾峰彇鎴戠殑鏂囩尞闆嗗垪琛紙鐢ㄤ簬鍏变韩閫夋嫨锛?
  getMyCollections: async (search?: string): Promise<{ id: number; name: string; description: string; paper_count: number; color: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/share/my-collections', { params })
    return response.data
  },

  // 鑾峰彇鎴戠殑鐭ヨ瘑搴撳垪琛紙鐢ㄤ簬鍏变韩閫夋嫨锛?
  getMyKnowledgeBases: async (search?: string): Promise<{ id: number; name: string; description: string; document_count: number }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/share/my-knowledge-bases', { params })
    return response.data
  },

  // 鑾峰彇鎴戠殑绗旇鏈垪琛紙鐢ㄤ簬鍏变韩閫夋嫨锛?
  getMyNotebooks: async (search?: string): Promise<{ id: string; title: string; description: string; cell_count: number; updated_at: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/share/my-notebooks', { params })
    return response.data
  },

  // 鍏变韩璧勬簮
  shareResource: async (data: {
    resource_type: string
    resource_id: number | string  // 鏀寔鏁存暟鍜屽瓧绗︿覆锛堝notebook UUID锛?
    shared_with_type: 'user' | 'group' | 'all_students'
    shared_with_id?: number
    permission?: string
    message?: string
  }): Promise<SharedResource> => {
    const response = await api.post('/api/share/', data)
    return response.data
  },

  // 鎵归噺鍏变韩
  batchShare: async (data: {
    resource_type: string
    resource_ids: (number | string)[]  // 鏀寔鏁存暟鍜屽瓧绗︿覆
    shared_with_type: 'user' | 'group' | 'all_students'
    shared_with_id?: number
    permission?: string
  }): Promise<{ success_count: number; skip_count: number; message: string }> => {
    const response = await api.post('/api/share/batch', data)
    return response.data
  },

  // 灏嗗叡浜鏂囨坊鍔犲埌鎴戠殑搴?
  copyToLibrary: async (shareId: number, collectionId?: number): Promise<{ message: string; paper_id: number }> => {
    const params = collectionId ? { collection_id: collectionId } : {}
    const response = await api.post(`/api/share/copy-to-library/${shareId}`, null, { params })
    return response.data
  },

  // 鑾峰彇鍏变韩璧勬簮璇︽儏锛堝寘鍚畬鏁村唴瀹癸級
  getSharedDetail: async (shareId: number): Promise<any> => {
    const response = await api.get(`/api/share/detail/${shareId}`)
    return response.data
  },

  // 鎵归噺澶嶅埗鏂囩尞闆嗕腑鐨勮鏂?
  copyCollectionPapers: async (shareId: number, paperIds?: number[], targetCollectionId?: number): Promise<{ success_count: number; skip_count: number; message: string }> => {
    const response = await api.post(`/api/share/copy-collection-papers/${shareId}`, {
      paper_ids: paperIds,
      target_collection_id: targetCollectionId,
    })
    return response.data
  },

  // 鍙栨秷鍏变韩
  removeShare: async (shareId: number): Promise<void> => {
    await api.delete(`/api/share/${shareId}`)
  },
}

// ========== 鍏憡 API ==========

export const announcementApi = {
  // 鑾峰彇鍏憡鍒楄〃锛堝鐢熺湅鍒扮殑锛?
  getAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/announcements')
    return response.data
  },

  // 鑾峰彇鎴戝彂甯冪殑鍏憡锛堝甯堬級
  getMyAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/announcements/my')
    return response.data
  },

  // 鍒涘缓鍏憡
  createAnnouncement: async (data: {
    title: string
    content: string
    group_id?: number
    is_pinned?: boolean
  }): Promise<Announcement> => {
    const response = await api.post('/api/announcements', data)
    return response.data
  },

  // 鏇存柊鍏憡
  updateAnnouncement: async (announcementId: number, data: {
    title?: string
    content?: string
    is_pinned?: boolean
    is_active?: boolean
  }): Promise<Announcement> => {
    const response = await api.put(`/api/announcements/${announcementId}`, data)
    return response.data
  },

  // 鍒犻櫎鍏憡
  deleteAnnouncement: async (announcementId: number): Promise<void> => {
    await api.delete(`/api/announcements/${announcementId}`)
  },

  // 鏍囪宸茶
  markAsRead: async (announcementId: number): Promise<void> => {
    await api.post(`/api/announcements/${announcementId}/read`)
  },

  // 鑾峰彇鍏憡闃呰缁熻
  getReadStats: async (announcementId: number): Promise<{
    total_count: number
    read_count: number
    readers: Array<{ user_id: number; username: string; read_at: string }>
  }> => {
    const response = await api.get(`/api/announcements/${announcementId}/stats`)
    return response.data
  },
}

// ========== 甯堢敓鍏崇郴 API ==========

const toUserBrief = (
  user: UserWithRole | null | undefined,
  fallbackRole: UserRole = UserRole.STUDENT,
): UserBrief | undefined => {
  if (!user) {
    return undefined
  }
  return {
    id: user.id,
    username: user.username,
    full_name: user.full_name,
    avatar: user.avatar,
    role: (user.role || fallbackRole) as UserRole,
    profile_data: {
      department: user.department,
      research_area: user.research_direction,
      bio: user.bio,
    },
  }
}

const invitationToMentorship = (inv: Invitation): Mentorship => {
  const isApply = inv.type === 'apply'
  const mentorId = isApply ? inv.to_user_id : inv.from_user_id
  const studentId = isApply ? inv.from_user_id : inv.to_user_id

  let status: MentorshipStatus
  if (inv.status === InvitationStatus.ACCEPTED) {
    status = MentorshipStatus.ACTIVE
  } else if (inv.status === InvitationStatus.PENDING) {
    status = isApply ? MentorshipStatus.PENDING : MentorshipStatus.INVITED
  } else {
    status = MentorshipStatus.ARCHIVED
  }

  return {
    id: inv.id,
    mentor_id: mentorId,
    student_id: studentId,
    status,
    request_message: inv.message,
    created_at: inv.created_at,
    updated_at: inv.responded_at || inv.created_at,
    approved_at: inv.status === InvitationStatus.ACCEPTED ? inv.responded_at : undefined,
    archived_at: inv.status !== InvitationStatus.PENDING && inv.status !== InvitationStatus.ACCEPTED ? inv.responded_at : undefined,
    mentor: isApply
      ? toUserBrief(inv.to_user, UserRole.MENTOR)
      : toUserBrief(inv.from_user, UserRole.MENTOR),
    student: isApply
      ? toUserBrief(inv.from_user, UserRole.STUDENT)
      : toUserBrief(inv.to_user, UserRole.STUDENT),
  }
}

const activeMentorToMentorship = (mentor: UserWithRole): Mentorship => {
  const now = new Date().toISOString()
  return {
    id: -(mentor.id + 100000),
    mentor_id: mentor.id,
    student_id: 0,
    status: MentorshipStatus.ACTIVE,
    created_at: now,
    updated_at: now,
    mentor: toUserBrief(mentor, UserRole.MENTOR),
  }
}

export const mentorshipApi = {
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

      const invitations = await invitationApi.getAll()
      const pendingInvitations = invitations.filter(
        (i) => i.type === 'invite' && i.status === InvitationStatus.PENDING,
      )
      const pendingApplications = invitations.filter(
        (i) => i.type === 'apply' && i.status === InvitationStatus.PENDING,
      )

      if (pendingInvitations.length > 0) {
        return { status: MentorshipStatus.INVITED, pendingInvitations }
      }
      if (pendingApplications.length > 0) {
        return { status: MentorshipStatus.PENDING, pendingApplications }
      }

      return { status: MentorshipStatus.NONE }
    } catch {
      return { status: MentorshipStatus.NONE }
    }
  },

  getMentor: async (): Promise<UserWithRole | null> => {
    return studentApi.getMentor()
  },

  searchMentors: async (query: string): Promise<UserWithRole[]> => {
    return studentApi.searchMentors(query)
  },

  applyToMentor: async (mentorId: number, message?: string): Promise<Invitation> => {
    return studentApi.applyToMentor(mentorId, message)
  },

  leaveMentor: async (): Promise<void> => {
    return studentApi.leaveMentor()
  },

  acceptInvitation: async (invitationId: number): Promise<void> => {
    return studentApi.acceptInvitation(invitationId)
  },

  rejectInvitation: async (invitationId: number): Promise<void> => {
    return studentApi.rejectInvitation(invitationId)
  },

  cancelApplication: async (invitationId: number): Promise<void> => {
    return studentApi.cancelApplication(invitationId)
  },

  getMyApplications: async (): Promise<Invitation[]> => {
    return studentApi.getMyApplications()
  },

  getReceivedInvitations: async (): Promise<Invitation[]> => {
    return studentApi.getReceivedInvitations()
  },

  // Compatibility methods used by Team/Mentorship stores
  getMentors: async (query: string = ''): Promise<UserBrief[]> => {
    const mentors = await studentApi.searchMentors(query)
    return mentors
      .map((mentor) => toUserBrief(mentor, UserRole.MENTOR))
      .filter((mentor): mentor is UserBrief => Boolean(mentor))
  },

  getMentorships: async (
    status?: MentorshipStatus,
    scope: 'as_student' | 'as_mentor' | 'all' = 'all',
  ): Promise<MentorshipListResponse> => {
    const items: Mentorship[] = []

    if (scope !== 'as_mentor') {
      const mentor = await studentApi.getMentor().catch(() => null)
      if (mentor) {
        items.push(activeMentorToMentorship(mentor))
      }
    }

    const invitations = await invitationApi.getAll().catch(() => [])
    for (const inv of invitations) {
      if (scope === 'as_mentor' && inv.type !== 'apply') {
        continue
      }
      if (scope === 'as_student' && inv.type !== 'apply' && inv.type !== 'invite') {
        continue
      }
      items.push(invitationToMentorship(inv))
    }

    const filtered = status ? items.filter((item) => item.status === status) : items
    return { items: filtered, total: filtered.length }
  },

  applyMentorship: async (mentorId: number, message?: string): Promise<Mentorship> => {
    const created = await studentApi.applyToMentor(mentorId, message)
    const createdId = (created as unknown as { invitation_id?: number; id?: number }).invitation_id
      ?? (created as unknown as { id?: number }).id

    if (createdId) {
      const invitations = await invitationApi.getAll().catch(() => [])
      const found = invitations.find((inv) => inv.id === createdId)
      if (found) {
        return invitationToMentorship(found)
      }
    }

    const now = new Date().toISOString()
    return {
      id: createdId || Date.now(),
      mentor_id: mentorId,
      student_id: 0,
      status: MentorshipStatus.PENDING,
      request_message: message,
      created_at: now,
      updated_at: now,
    }
  },

  updateMentorshipStatus: async (
    mentorshipId: number,
    status: MentorshipStatus,
    _message?: string,
  ): Promise<void> => {
    if (status === MentorshipStatus.ACTIVE) {
      await invitationApi.accept(mentorshipId)
      return
    }

    if (status === MentorshipStatus.ARCHIVED) {
      try {
        await invitationApi.cancel(mentorshipId)
      } catch {
        await invitationApi.reject(mentorshipId)
      }
      return
    }

    if (status === MentorshipStatus.INVITED) {
      await invitationApi.accept(mentorshipId)
      return
    }
  },

  getMyStudents: async (status: MentorshipStatus = MentorshipStatus.ACTIVE): Promise<UserBrief[]> => {
    if (status !== MentorshipStatus.ACTIVE) {
      return []
    }
    const students = await mentorApi.getStudents().catch(() => [])
    return students.map((student) => ({
      id: student.id,
      username: student.username,
      full_name: student.full_name,
      avatar: student.avatar,
      role: UserRole.STUDENT,
      profile_data: {
        department: student.department,
        research_area: student.research_direction,
      },
    }))
  },

  deleteMentorship: async (mentorshipId: number): Promise<void> => {
    await mentorshipApi.updateMentorshipStatus(mentorshipId, MentorshipStatus.ARCHIVED)
  },

  getPendingCount: async (): Promise<number> => {
    const invitations = await invitationApi.getAll().catch(() => [])
    return invitations.filter((inv) => inv.status === InvitationStatus.PENDING).length
  },
}

// ========== MCP 绠＄悊绫诲瀷涓?API ==========

export interface MCPServerConfigItem {
  name: string
  transport: 'stdio' | 'sse' | 'streamable_http'
  enabled: boolean
  command?: string
  args?: string[]
  env?: Record<string, string>
  cwd?: string
  url?: string
  headers?: Record<string, string>
  timeout_seconds?: number
  sse_read_timeout_seconds?: number
}

export interface MCPServerTemplate {
  id: string
  title: string
  description: string
  claude_desktop_config: Record<string, unknown>
  recommended_routes?: Record<string, string[]>
}

export interface MCPServerStatusItem {
  name: string
  transport: string
  enabled: boolean
  reachable: boolean | null
  discovered_tools: number
  last_checked_at?: string | null
  last_error?: string | null
  tools: string[]
}

export interface MCPConfigResponse {
  enabled: boolean
  tool_prefix: string
  call_timeout_seconds: number
  config_path: string
  tool_routes: Record<string, string[]>
  servers: MCPServerConfigItem[]
  claude_desktop_config: Record<string, unknown>
}

export const mcpApi = {
  getTemplates: async (): Promise<{ templates: MCPServerTemplate[] }> => {
    const response = await api.get('/api/mcp/templates')
    return response.data
  },

  getConfig: async (): Promise<MCPConfigResponse> => {
    const response = await api.get('/api/mcp/config')
    return response.data
  },

  validateConfig: async (payload: {
    raw_json?: string
    claude_desktop_config?: Record<string, unknown>
    servers?: Record<string, unknown>[]
  }): Promise<{
    valid: boolean
    server_count: number
    servers: MCPServerConfigItem[]
    claude_desktop_config: Record<string, unknown>
  }> => {
    const response = await api.post('/api/mcp/config/validate', payload)
    return response.data
  },

  saveConfig: async (payload: {
    raw_json?: string
    claude_desktop_config?: Record<string, unknown>
    servers?: Record<string, unknown>[]
  }): Promise<{
    message: string
    path: string
    server_count: number
    servers: MCPServerConfigItem[]
    claude_desktop_config: Record<string, unknown>
  }> => {
    const response = await api.put('/api/mcp/config', payload)
    return response.data
  },

  refreshStatus: async (forceRefresh = true): Promise<{
    server_count: number
    tool_count: number
    servers: MCPServerStatusItem[]
  }> => {
    const response = await api.post('/api/mcp/status/refresh', { force_refresh: forceRefresh })
    return response.data
  },
}

export default api

// ========== 鏅鸿兘鍒嗗潡绫诲瀷瀹氫箟 ==========

export enum ChunkingStrategy {
  FIXED = 'fixed',
  SEMANTIC = 'semantic',
  HIERARCHICAL = 'hierarchical',
  ACADEMIC = 'academic',
  HYBRID = 'hybrid',
}

export enum ChunkLevel {
  PARAGRAPH = 'paragraph',
  SECTION = 'section',
  DOCUMENT = 'document',
}

export enum ChunkingPreset {
  DEFAULT = 'default',
  FAST = 'fast',
  PRECISE = 'precise',
  ACADEMIC = 'academic',
  DEEP = 'deep',
}

export interface ChunkingConfig {
  strategy: ChunkingStrategy
  // V3 Token 璁￠噺鏂板
  use_token_based: boolean
  base_chunk_tokens: number
  overlap_tokens: number
  min_semantic_tokens: number
  max_semantic_tokens: number
  // 瀛楃璁￠噺锛堟棫锛?
  base_chunk_size: number
  chunk_overlap: number
  semantic_threshold: number
  min_semantic_chunk: number
  max_semantic_chunk: number
  enable_hierarchical: boolean
  hierarchy_levels: ChunkLevel[]
  detect_academic_structure: boolean
  preserve_citations: boolean
  breakpoint_percentile: number
}

export interface ChunkingConfigResponse extends ChunkingConfig {
  id?: number
  user_id?: number
  name?: string
  is_default: boolean
  created_at?: string
  updated_at?: string
}

export interface PresetDescription {
  name: string
  description: string
  strategy: string
  recommended_for: string[]
}

export interface ChunkMetadata {
  level: ChunkLevel
  section_type?: string
  section_title?: string
  parent_id?: string
  child_ids: string[]
  has_citations: boolean
  position_ratio: number
  keywords: string[]
  token_count?: number
}

export interface SmartChunk {
  id: string
  content: string
  start_char: number
  end_char: number
  metadata: ChunkMetadata
}

export interface ChunkingStats {
  total_chunks: number
  total_chars: number
  avg_chunk_size: number
  min_chunk_size: number
  max_chunk_size: number
  chunks_with_citations: number
  total_tokens?: number
  avg_chunk_tokens?: number
  min_chunk_tokens?: number
  max_chunk_tokens?: number
}

export interface ChunkingResult {
  strategy: string
  chunks: SmartChunk[]
  hierarchy?: Record<string, Array<Record<string, unknown>>>
  metadata: Record<string, unknown>
  stats: ChunkingStats
}

export interface DocumentAnalysis {
  is_academic: boolean
  detected_sections: Array<{
    title: string
    type: string
    start: number
    end: number
    length: number
  }>
  has_citations: boolean
  recommended_strategy: string
  recommended_reason: string
  document_stats: {
    total_chars: number
    total_sentences: number
    total_paragraphs: number
    avg_sentence_length: number
    section_count: number
    total_tokens?: number
  }
  // [Fix 7] 鏂板瀛楁
  estimated_chunks?: number      // 棰勪及鍒嗗潡鏁伴噺
  language?: string              // 妫€娴嬪埌鐨勮瑷€ (zh / en)
}

export interface StrategyComparison {
  document_length: number
  comparisons: Record<string, {
    strategy: string
    stats?: ChunkingStats
    sample_chunks?: Array<{
      content: string
      length: number
      has_citations: boolean
    }>
    total_chunks?: number
    error?: string
  }>
  recommendation: {
    best_strategy: string
    reason: string
  }
}

// ========== 鏅鸿兘鍒嗗潡API ==========

export const chunkingApi = {
  // 鑾峰彇鎵€鏈夐璁?
  getPresets: async (): Promise<{ presets: PresetDescription[] }> => {
    const response = await api.get('/api/chunking/presets')
    return response.data
  },

  // 鑾峰彇鎸囧畾棰勮璇︽儏
  getPreset: async (presetName: ChunkingPreset): Promise<ChunkingConfigResponse> => {
    const response = await api.get(`/api/chunking/presets/${presetName}`)
    return response.data
  },

  // 棰勮鍒嗗潡鏁堟灉
  previewChunking: async (
    text: string,
    config?: Partial<ChunkingConfig>,
    preset?: ChunkingPreset,
    fileType = 'txt'
  ): Promise<ChunkingResult> => {
    const response = await api.post('/api/chunking/preview', {
      text,
      config,
      preset,
      file_type: fileType,
    })
    return response.data
  },

  // 鍒嗘瀽鏂囨。缁撴瀯
  analyzeDocument: async (
    text: string,
    fileType = 'txt'
  ): Promise<DocumentAnalysis> => {
    const response = await api.post('/api/chunking/analyze', {
      text,
      file_type: fileType,
    })
    return response.data
  },

  // 姣旇緝涓嶅悓绛栫暐
  compareStrategies: async (
    text: string,
    strategies: ChunkingPreset[] = [ChunkingPreset.FAST, ChunkingPreset.PRECISE, ChunkingPreset.DEEP],
    fileType = 'txt'
  ): Promise<StrategyComparison> => {
    const params = new URLSearchParams()
    strategies.forEach(s => params.append('strategies', s))

    const response = await api.post(`/api/chunking/compare?${params.toString()}`, {
      text,
      file_type: fileType,
    })
    return response.data
  },

  // 鑾峰彇鐭ヨ瘑搴撶殑鍒嗗潡閰嶇疆
  getKnowledgeBaseConfig: async (kbId: number): Promise<ChunkingConfigResponse | null> => {
    try {
      const response = await api.get(`/api/chunking/knowledge-base/${kbId}/config`)
      return response.data
    } catch {
      return null
    }
  },

  // 鏇存柊鐭ヨ瘑搴撶殑鍒嗗潡閰嶇疆
  updateKnowledgeBaseConfig: async (
    kbId: number,
    config: Partial<ChunkingConfig> | { preset: ChunkingPreset }
  ): Promise<ChunkingConfigResponse> => {
    const response = await api.put(`/api/chunking/knowledge-base/${kbId}/config`, config)
    return response.data
  },

  // 灏嗛璁惧簲鐢ㄥ埌鐭ヨ瘑搴?
  applyPresetToKnowledgeBase: async (
    kbId: number,
    preset: ChunkingPreset
  ): Promise<{ message: string; knowledge_base_id: number; preset: string }> => {
    const response = await api.post(`/api/chunking/knowledge-base/${kbId}/apply-preset?preset=${preset}`)
    return response.data
  },
}




