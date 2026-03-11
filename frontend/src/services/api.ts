import axios, { AxiosError } from 'axios'

// API base configuration
const VITE_ENV = ((import.meta as any).env || {}) as Record<string, string | undefined>
const API_BASE_URL = VITE_ENV.VITE_API_BASE_URL || 'http://localhost:8888'
export const SHOW_RAG_METRICS = VITE_ENV.VITE_SHOW_RAG_METRICS === 'true'
const LONG_RUNNING_READER_TIMEOUT_MS = 180000

export interface ApiErrorContract {
  code?: string
  message?: string
  details?: unknown
  request_id?: string
}

export type ApiErrorDetail = string | ApiErrorContract
export interface ApiErrorResponsePayload extends ApiErrorContract {
  detail?: ApiErrorDetail
}

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'timeout'
  | 'cancelled'

// Create axios instance
const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000, // 30s timeout
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor - attach token
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

export const extractApiErrorMessage = (detail: ApiErrorDetail | undefined, fallback = '请求失败'): string => {
  if (!detail) return fallback
  if (typeof detail === 'string') {
    const text = detail.trim()
    return text || fallback
  }
  if (typeof detail.message === 'string' && detail.message.trim()) {
    return detail.message.trim()
  }
  return fallback
}

const extractApiErrorDetail = (payload: ApiErrorResponsePayload | undefined): ApiErrorDetail | undefined => {
  if (!payload) return undefined
  if (typeof payload.detail !== 'undefined') {
    return payload.detail
  }
  if (typeof payload.message === 'string' || typeof payload.code === 'string') {
    return {
      code: payload.code,
      message: payload.message,
      details: payload.details,
      request_id: payload.request_id,
    }
  }
  return undefined
}

export const normalizeTaskStatus = (
  status: string | undefined | null,
): TaskStatus => {
  const normalized = String(status || '').trim().toLowerCase()
  if (normalized === 'completed' || normalized === 'complete') return 'completed'
  if (normalized === 'processing' || normalized === 'running') return 'running'
  if (normalized === 'ready' || normalized === 'success' || normalized === 'done') return 'completed'
  if (normalized === 'timeout') return 'timeout'
  if (normalized === 'cancelled' || normalized === 'canceled') return 'cancelled'
  if (normalized === 'failed' || normalized === 'error') return 'failed'
  if (normalized === 'pending' || normalized === 'queued') return 'pending'
  return 'failed'
}

// Response interceptor - normalize errors
api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorResponsePayload>) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('auth-storage')
      window.location.href = '/login'
    }
    const detail = extractApiErrorDetail(error.response?.data)
    const message = extractApiErrorMessage(detail, error.message || '请求失败')
    if (message) {
      error.message = message
    }
    throw error
  }
)

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
  status: TaskStatus
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
  // [Fix 12] Hierarchical retrieval fields
  chunk_level?: string // paragraph / section / document
  section_type?: string // abstract / methodology / results ...
  section_title?: string
  parent_context?: string // parent chunk preview
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
  if (!axios.isAxiosError(error)) {
    return false
  }
  return error.code === 'ERR_CANCELED' || error.response?.status === 499
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

type SseEventHandler<TEvent extends string = string> = (event: TEvent, data: any) => void

async function streamJsonSse<TEvent extends string = string>(
  url: string,
  onEvent?: SseEventHandler<TEvent>,
  abortController?: AbortController,
): Promise<void> {
  const response = await fetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${getToken()}`,
    },
    signal: abortController?.signal,
  })

  if (!response.ok) {
    let detail = `订阅失败 (${response.status})`
    try {
      const err = (await response.json()) as { detail?: ApiErrorDetail }
      detail = extractApiErrorMessage(err?.detail, detail)
    } catch {
      // ignore json parse error for non-json body
    }
    throw new Error(detail)
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new Error('无法读取状态流')
  }

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      const normalized = line.trim()
      if (!normalized.startsWith('data:')) continue
      const raw = normalized.slice(5).trim()
      if (!raw) continue
      try {
        const parsed = JSON.parse(raw) as { event?: string; data?: any }
        const event = String(parsed?.event || '')
        if (!event) continue
        onEvent?.(event as TEvent, parsed?.data)
      } catch {
        // ignore malformed chunk
      }
    }
  }
}

export interface ProcessingStatus {
  document_id: number
  status: string
  progress: number
  message: string
  chunk_count: number
  error?: string
}

export interface KnowledgeDocumentStatusEventData {
  kb_id: number
  document_id: number
  status: Document['status']
  chunk_count: number
  error_message?: string
  updated_at?: string
}

export interface PaperKnowledgeLinkStatusEventData {
  link_id: number
  paper_id: number
  knowledge_base_id: number
  document_id?: number
  status: PaperKnowledgeLink['status']
  error_message?: string
  updated_at?: string
}

export const normalizeDocumentStatus = (status: Document['status'] | string | null | undefined): TaskStatus =>
  normalizeTaskStatus(status)

export const normalizeKnowledgeLinkStatus = (
  status: PaperKnowledgeLink['status'] | string | null | undefined,
): TaskStatus => normalizeTaskStatus(status)

export const authApi = {
  login: async (email: string, password: string): Promise<AuthResponse> => {
    const response = await api.post('/api/v1/auth/login', { email, password })
    return response.data
  },

  register: async (
    email: string,
    username: string,
    password: string,
    fullName?: string
  ): Promise<AuthResponse> => {
    const response = await api.post('/api/v1/auth/register', {
      email,
      username,
      password,
      full_name: fullName,
    })
    return response.data
  },

  me: async (): Promise<User> => {
    const response = await api.get('/api/v1/auth/me')
    return response.data
  },

  logout: async (): Promise<void> => {
    await api.post('/api/v1/auth/logout')
  },
}

export const userApi = {
  getProfile: async (): Promise<User> => {
    const response = await api.get('/api/v1/users/profile')
    return response.data
  },

  updateProfile: async (data: Partial<User>): Promise<User> => {
    const response = await api.put('/api/v1/users/profile', data)
    return response.data
  },

  getLLMProviders: async (): Promise<{
    default: string
    providers: LLMProvider[]
  }> => {
    const response = await api.get('/api/v1/users/llm-providers')
    return response.data
  },
}

export const chatApi = {
  getConversations: async (
    skip = 0,
    limit = 20,
    archived = false
  ): Promise<Conversation[]> => {
    const response = await api.get('/api/v1/chat/conversations', {
      params: { skip, limit, archived },
    })
    return response.data
  },

  createConversation: async (title?: string): Promise<Conversation> => {
    const response = await api.post('/api/v1/chat/conversations', { title })
    return response.data
  },

  getConversation: async (conversationId: number): Promise<Conversation> => {
    const response = await api.get(`/api/v1/chat/conversations/${conversationId}`)
    return response.data
  },

  deleteConversation: async (conversationId: number): Promise<void> => {
    await api.delete(`/api/v1/chat/conversations/${conversationId}`)
  },

  getMessages: async (
    conversationId: number,
    skip = 0,
    limit = 50
  ): Promise<Message[]> => {
    const response = await api.get(
      `/api/v1/chat/conversations/${conversationId}/messages`,
      { params: { skip, limit } }
    )
    return response.data
  },

  sendMessageStream: async (
    message: string,
    conversationId?: number,
    onEvent?: (event: string, data: any) => void,
    abortController?: AbortController
  ): Promise<void> => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/chat/send`, {
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
        const error = (await response.json()) as { detail?: ApiErrorDetail }
        throw new Error(extractApiErrorMessage(error.detail, '请求失败'))
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
              // ignore malformed stream chunk
            }
          }
        }
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        onEvent?.('stopped', { aborted: true })
        return
      }
      throw error
    }
  },

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
    const response = await api.get('/api/v1/chat/messages/search', {
      params: { q: query, limit },
    })
    return response.data
  },

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
    const response = await api.post('/api/v1/chat/messages/stopped', data)
    return response.data
  },
}


export const knowledgeApi = {
  getKnowledgeBases: async (skip = 0, limit = 20): Promise<{ items: KnowledgeBase[]; total: number }> => {
    const response = await api.get('/api/v1/knowledge/knowledge-bases', {
      params: { skip, limit },
    })
    return response.data
  },

  getAvailableKnowledgeBases: async (): Promise<{
    own: { id: number; name: string; description?: string; document_count: number; total_chunks: number }[];
    shared: { id: number; name: string; description?: string; document_count: number; total_chunks: number; owner_id: number; owner_name: string }[];
    sharing_enabled: boolean;
  }> => {
    const response = await api.get('/api/v1/knowledge/available')
    return response.data
  },

  createKnowledgeBase: async (data: KnowledgeBaseCreate): Promise<KnowledgeBase> => {
    const response = await api.post('/api/v1/knowledge/knowledge-bases', data)
    return response.data
  },

  getEmbeddingModels: async (): Promise<EmbeddingModelsResponse> => {
    const response = await api.get('/api/v1/knowledge/embedding-models')
    return response.data
  },

  getKnowledgeBase: async (kbId: number): Promise<KnowledgeBase> => {
    const response = await api.get(`/api/v1/knowledge/knowledge-bases/${kbId}`)
    return response.data
  },

  updateKnowledgeBase: async (kbId: number, data: Partial<KnowledgeBaseCreate>): Promise<KnowledgeBase> => {
    const response = await api.put(`/api/v1/knowledge/knowledge-bases/${kbId}`, data)
    return response.data
  },

  deleteKnowledgeBase: async (kbId: number): Promise<void> => {
    await api.delete(`/api/v1/knowledge/knowledge-bases/${kbId}`)
  },

  getDocuments: async (kbId: number, skip = 0, limit = 20): Promise<{ items: Document[]; total: number }> => {
    const response = await api.get(`/api/v1/knowledge/knowledge-bases/${kbId}/documents`, {
      params: { skip, limit },
    })
    return response.data
  },

  uploadDocument: async (kbId: number, file: File): Promise<Document> => {
    const formData = new FormData()
    formData.append('file', file)

    const response = await api.post(
      `/api/v1/knowledge/knowledge-bases/${kbId}/documents/upload`,
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
    const response = await api.get(`/api/v1/knowledge/knowledge-bases/${kbId}/documents/${docId}`)
    return response.data
  },

  deleteDocument: async (kbId: number, docId: number): Promise<void> => {
    await api.delete(`/api/v1/knowledge/knowledge-bases/${kbId}/documents/${docId}`)
  },

  getDocumentStatus: async (kbId: number, docId: number): Promise<ProcessingStatus> => {
    const response = await api.get(`/api/v1/knowledge/knowledge-bases/${kbId}/documents/${docId}/status`)
    return response.data
  },

  streamStatusEvents: async (
    params: { kb_id?: number } | undefined,
    onEvent?: (event: 'connected' | 'heartbeat' | 'document_status', data: any) => void,
    abortController?: AbortController,
  ): Promise<void> => {
    const query = new URLSearchParams()
    if (params?.kb_id && params.kb_id > 0) {
      query.set('kb_id', String(params.kb_id))
    }
    const suffix = query.toString() ? `?${query.toString()}` : ''
    await streamJsonSse(`${API_BASE_URL}/api/v1/knowledge/events/stream${suffix}`, onEvent, abortController)
  },

  getChunks: async (kbId: number, docId: number, skip = 0, limit = 20): Promise<{ items: DocumentChunk[]; total: number }> => {
    const response = await api.get(`/api/v1/knowledge/knowledge-bases/${kbId}/documents/${docId}/chunks`, {
      params: { skip, limit },
    })
    return response.data
  },

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

    const response = await api.post('/api/v1/knowledge/search', {
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

export type CommentFilter = 'all' | 'same_group'
export type AnnotationType = 'highlight' | 'note'
export type LiteratureAskScope = 'paper' | 'collection'

export interface ReaderSession {
  page: number
  zoom: string
  scroll_y: number
  selected_kb_id?: number
  last_anchor: Record<string, unknown>
  updated_at: string
}

export interface ReaderSessionUpdate {
  page: number
  zoom: string
  scroll_y: number
  selected_kb_id?: number
  last_anchor?: Record<string, unknown>
}

export type ReaderGenerativeStyleKey = 'journal_classic' | 'clinical_brief' | 'preprint_modern'

export interface ReaderGenerativeSourceAnchor {
  page: number
  start_char: number
  end_char: number
}

export interface ReaderGenerativeRequest {
  page: number
  selected_kb_id?: number
  force_refresh?: boolean
  prefer_agent?: boolean
  style_hint?: ReaderGenerativeStyleKey | string
}

export interface ReaderGenerativeBlock {
  id: string
  kind: 'heading' | 'paragraph' | 'list_item' | 'caption'
  text: string
  order: number
  section_title?: string
  source_anchor: ReaderGenerativeSourceAnchor
}

export interface ReaderGenerativeSection {
  title: string
  level: number
  block_ids: string[]
  source_anchor?: ReaderGenerativeSourceAnchor
}

export interface ReaderGenerativeAsset {
  kind: 'link' | 'annotation' | 'image_hint'
  label: string
  source: 'metadata' | 'text' | 'annotation' | 'pdf'
  href?: string | null
  meta: Record<string, unknown>
}

export interface ReaderGenerativeStyleTuning {
  body_scale: number
  line_height: number
  heading_scale: number
}

export interface ReaderGenerativePagePayload {
  paper_id: number
  page: number
  parser_version: string
  source_signature: string
  style_key: ReaderGenerativeStyleKey | string
  build_mode: string
  structure_confidence: number
  summary: string
  style_tuning?: ReaderGenerativeStyleTuning
  sections: ReaderGenerativeSection[]
  blocks: ReaderGenerativeBlock[]
  assets: ReaderGenerativeAsset[]
  generated_at: string
  cache_hit?: boolean
  cache_layer?: 'redis' | 'db' | 'none' | string
}

export interface ReaderGenerativePrefetchRequest {
  pages: number[]
  selected_kb_id?: number
  style_hint?: ReaderGenerativeStyleKey | string
}

export interface ReaderGenerativePrefetchResponse {
  queued: number[]
  skipped: number[]
}

export interface ReaderComposeRequest {
  page: number
  selected_kb_id?: number
  force_refresh?: boolean
  regenerate?: boolean
  latency_budget_ms?: number
  quality_target?: number
  max_iterations?: number
  style_intent?: string
  theme_mode?: 'light' | 'dark'
  detail_level?: 'concise' | 'standard' | 'deep'
  compare_mode?: boolean
  citation_tldr?: boolean
}

export interface ReaderGenerativePlanRequest extends ReaderComposeRequest {
  user_intent?: string
}

export interface ReaderExperiencePlanRequest extends ReaderGenerativePlanRequest {
  focus_page?: number
  focus_section_ids?: string[]
  reader_profile?: string
}

export interface ReaderComposeSchemeChoice {
  scheme_id: string
  label: string
  rationale: string
  source: string
  candidate_ids: string[]
}

export interface ReaderComposeOmissionDecision {
  decision_id: string
  decision: 'hide' | 'collapse' | 'defer'
  reason: string
  recoverable: boolean
  target_layout_ids: string[]
  target_block_ids: string[]
  target_atom_ids: string[]
}

export interface ReaderComposeReviewDiagnostic {
  code: string
  severity: 'info' | 'warn' | 'error'
  message: string
  component_ids: string[]
  meta: Record<string, unknown>
}

export interface ReaderComposeReviewSessionRequest extends ReaderComposeRequest {
  snapshot_label?: string
  prefer_cache_clone?: boolean
  allow_recompute_on_cache_miss?: boolean
}

export interface ReaderComposeReviewImportRequest {
  snapshot_label?: string
  payload: ReaderComposePayload
}

export interface ReaderComposeReviewPatchRequest {
  snapshot_id?: string
  ui_ops: Record<string, unknown>[]
  decision_log_append?: string[]
  omission_decisions?: ReaderComposeOmissionDecision[] | null
  scheme_choice?: ReaderComposeSchemeChoice | null
  note?: string
}

export interface ReaderComposeReviewObservationRequest {
  snapshot_id?: string
  render_image_url?: string
  diagnostics?: ReaderComposeReviewDiagnostic[]
  note?: string
  source?: string
}

export interface ReaderComposeReviewObservationUploadOptions {
  snapshot_id?: string
  diagnostics?: ReaderComposeReviewDiagnostic[]
  note?: string
  source?: string
}

export interface ReaderComposeReviewAutoPatchRequest {
  snapshot_id?: string
  user_intent?: string
  note?: string
}

export interface ReaderComponentBBoxHint {
  x0: number
  x1: number
  top: number
  bottom: number
  page_width?: number | null
  page_height?: number | null
}

export interface ReaderAnchorPolygonPoint {
  x: number
  y: number
}

export interface ReaderAnchorPolygon {
  points: ReaderAnchorPolygonPoint[]
  source?: string | null
  component_id?: string | null
}

export interface ReaderAnchorGeometry {
  polygons: ReaderAnchorPolygon[]
  page_width?: number | null
  page_height?: number | null
}

export interface ReaderComponentAnchorV2 {
  coord_version: 'anchor_v2' | string
  canonical_block_id: string
  page: number
  start_char: number
  end_char: number
}

export interface ReaderComponentSourceAnchor {
  page: number
  start_char: number
  end_char: number
  quote?: string | null
  quote_text?: string | null
  anchor_id?: string | null
  segment_index?: number | null
  segment_total?: number | null
  bbox_hint?: ReaderComponentBBoxHint | null
  canonical_block_id?: string | null
  coord_version?: 'anchor_v2' | string | null
  anchor_confidence?: number | null
  anchor_v2?: ReaderComponentAnchorV2 | null
  geometry_version?: 'poly_v1' | string | null
  geometry?: ReaderAnchorGeometry | null
  source_word_ids?: string[]
  source_char_ranges?: Array<{ start_char_id: string; end_char_id: string }>
}

export interface ReaderComponentAction {
  key: string
  label: string
  kind?: 'primary' | 'default' | 'danger' | 'link'
  payload?: Record<string, unknown>
}

export interface ReaderComponentLayoutSlot {
  reserved_height?: number
  lock_height?: boolean
}

export interface ReaderComponentNode {
  id: string
  type:
    | 'PaperHeaderCard'
    | 'MetadataSidebarCard'
    | 'ContextRail'
    | 'SectionTOC'
    | 'SectionHeading'
    | 'ParagraphProse'
    | 'ListBlock'
    | 'FigurePanel'
    | 'TablePanel'
    | 'CitationLinks'
    | 'KeyTakeaways'
    | 'AnnotationRail'
    | 'QualityBadge'
    | 'QualityPanel'
    | 'InlineQuerySlot'
    | 'AnswerCard'
    | 'CompareInsightsCard'
    | 'InsightClusterCard'
    | 'SectionBridgeCard'
    | 'PdfSnippetCard'
    | 'CitationCard'
    | 'EquationBlock'
    | 'MethodologyCard'
    | 'CalloutBox'
    | 'AbstractCard'
    | string
  props: Record<string, unknown>
  children: ReaderComponentNode[]
  source_anchor_refs: ReaderComponentSourceAnchor[]
  source_block_ids: string[]
  source_atom_ids?: string[]
  zone_type?: 'main_body' | 'side_context' | 'figure_meta'
  column_id?: string
  region?: string
  display?: 'default' | 'collapsed' | 'pinned' | 'hidden_until_expand'
  order_key?: number
  heading_prob?: number
  capabilities?: string[]
  actions?: ReaderComponentAction[]
  layout_slot?: ReaderComponentLayoutSlot | null
}

export interface ReaderComposeQualityReport {
  overall: number
  structure_fidelity: number
  readability: number
  evidence_alignment: number
  layout_consistency: number
  cross_column_merge_ratio?: number
  duplicate_ratio?: number
  sidebar_recall?: number
  toc_quality?: number
  hard_constraints_passed: boolean
  sidebar_leak_detected: boolean
  title_integrity_ok: boolean
  anchors_valid: boolean
  mm_assist_used?: boolean
  mm_model?: string
  mm_fallback_used?: boolean
  anchor_coverage_ratio?: number
  evidence_image_ready?: number
  anchor_quote_hit_rate?: number
  anchor_bbox_iou?: number
  anchor_misjump_rate?: number
  anchor_gate_passed?: boolean
  validation_errors: string[]
  quality_target: number
  iterations: number
  degraded: boolean
  stop_reason: string
  latency_budget_ms: number
  deductions?: Array<Record<string, unknown>>
  fix_suggestions?: string[]
  iteration_trace_summary?: Array<Record<string, unknown>>
}

export interface ReaderUIPlan {
  plan_id: string
  components: ReaderComponentNode[]
  layout: Record<string, unknown>
  style_tokens: Record<string, unknown>
  trace_meta: Record<string, unknown>
  ui_ops?: ReaderComponentPatchOp[]
  agent_trace?: Array<Record<string, unknown>>
  agent_tool_calls?: Array<Record<string, unknown>>
}

export interface ReaderComponentPatchOp {
  op: 'insert_component' | 'update_component_props' | 'remove_component' | 'reorder_components' | string
  reason?: string
  ordered_component_ids?: string[]
  component_id?: string
  props_patch?: Record<string, unknown>
  after_component_id?: string | null
  component?: ReaderComponentNode
}

export interface SegmentPlan {
  segment_id: string
  kind: string
  ui_component: string
  component_hint?: string | null
  kind_hint?: string | null
  confidence?: number | null
  block_ids: string[]
  line_ids?: string[]
  evidence_line_ids?: string[]
  title?: string | null
  continuation?: string | null
  reason?: string | null
}

export interface LayoutPlanV2 {
  zones?: Array<Record<string, unknown>>
  headings?: Array<Record<string, unknown>>
  continuation?: Record<string, unknown>
  segments?: SegmentPlan[]
  ui_suggestions?: Array<Record<string, unknown>>
  notes?: string[]
}

export interface NodeGateReport {
  total_nodes?: number
  blocked_nodes?: number
  passed_nodes?: number
  rows?: Array<Record<string, unknown>>
}

export interface ReaderValidationGateResult {
  passed: boolean
  errors: string[]
}

export interface ReaderValidationGates {
  id_integrity: ReaderValidationGateResult
  full_coverage: ReaderValidationGateResult
  whitelist_only: ReaderValidationGateResult
  layout_contract: ReaderValidationGateResult
  ownership_unchanged: ReaderValidationGateResult
  non_empty_plan_for_non_empty_input: ReaderValidationGateResult
  source_text_immutable: ReaderValidationGateResult
}

export interface ReaderValidationReport {
  passed: boolean
  gates: ReaderValidationGates
  errors?: string[]
}

export interface ReaderComposeAsset {
  kind: 'link' | 'annotation' | 'image_hint' | 'external_image'
  label: string
  source: 'metadata' | 'text' | 'annotation' | 'pdf' | 'web'
  href?: string | null
  meta: Record<string, unknown>
  tldr?: string | null
}

export interface ReaderEnrichmentTarget {
  target_id: string
  node_id: string
  target_kind: 'section' | 'paragraph' | 'figure' | 'table' | 'equation' | 'structure'
  component_type: string
  title: string
  excerpt: string
  source_block_ids: string[]
  source_atom_ids?: string[]
  section_label?: string
  figure_label?: string
  suggested_resource_types?: string[]
  meta?: Record<string, unknown>
}

export interface ReaderEnrichmentBundle {
  version: string
  targets: ReaderEnrichmentTarget[]
  resource_modules: Array<Record<string, unknown>>
  interaction_modules: Array<Record<string, unknown>>
  meta: Record<string, unknown>
}

export interface ReaderStoryClaim {
  claim_id: string
  text: string
  display_text: string
  source_target_ids: string[]
  strength: 'primary' | 'supporting'
}

export interface ReaderStoryEvidenceUnit {
  evidence_id: string
  kind: 'figure' | 'paragraph' | 'table' | 'equation' | 'section'
  role: string
  title: string
  source_target_ids: string[]
}

export interface ReaderStoryTermGap {
  term: string
  reason: string
  source_target_ids: string[]
}

export interface ReaderStoryBackgroundGap {
  topic: string
  reason: string
  suggested_resource_type: string
}

export interface ReaderStoryNarrativeTurn {
  turn_id: string
  kind: string
  label: string
  target_ids: string[]
}

export interface ReaderStorySubstrate {
  version: string
  page_id: string
  main_claims: ReaderStoryClaim[]
  evidence_units: ReaderStoryEvidenceUnit[]
  terms_to_explain: ReaderStoryTermGap[]
  background_gaps: ReaderStoryBackgroundGap[]
  narrative_turns: ReaderStoryNarrativeTurn[]
  meta: Record<string, unknown>
}

export interface ReaderPageBrief {
  version: string
  page_goal: string
  reader_type: string
  page_archetype: 'figure_explainer' | 'finding_digest' | 'methods_decoder' | 'concept_decoder' | 'context_builder'
  hero_angle: string
  primary_focus_target_id: string
  secondary_support_target_ids: string[]
  reading_path: string[]
  interaction_opportunities: string[]
  resource_gaps: string[]
  experience_hooks: string[]
  resource_strategy: string
  storyboard: Array<Record<string, unknown>>
  content_budget: Record<string, number>
  meta: Record<string, unknown>
}

export interface ReaderGenerativeResourceModule {
  module_id: string
  module_type: string
  target_ids: string[]
  title: string
  display_title: string
  summary: string
  display_summary: string
  links: Array<Record<string, unknown>>
  source: 'agent' | 'paper_read' | 'knowledge_search' | 'web' | 'mcp' | 'fallback'
  interaction_mode: string
  meta: Record<string, unknown>
}

export interface ReaderGenerativeInteractionModule {
  module_id: string
  module_type: string
  target_ids: string[]
  title: string
  display_title: string
  display_summary: string
  props: Record<string, unknown>
  source: 'agent' | 'paper_read' | 'knowledge_search' | 'web' | 'mcp' | 'fallback'
  meta: Record<string, unknown>
}

export interface ReaderGenerativeJsWidgetPlan {
  widget_id: string
  widget_type: string
  target_ids: string[]
  title: string
  display_title: string
  display_summary: string
  data_requirements: string[]
  props: Record<string, unknown>
  meta: Record<string, unknown>
}

export interface ReaderGenerativePlan {
  version: string
  status: 'draft' | 'done' | 'fallback'
  shell_mode: string
  story_substrate: ReaderStorySubstrate
  page_brief: ReaderPageBrief
  rationale: string[]
  resource_modules: ReaderGenerativeResourceModule[]
  interaction_modules: ReaderGenerativeInteractionModule[]
  js_widgets: ReaderGenerativeJsWidgetPlan[]
  used_tools: string[]
  tool_trace: Array<Record<string, unknown>>
  meta: Record<string, unknown>
}

export interface ReaderExperienceHero {
  title: string
  display_title: string
  subtitle: string
  display_subtitle: string
  summary: string
  display_summary: string
  focus_label: string
  target_ids: string[]
  claim_ids: string[]
  meta: Record<string, unknown>
}

export interface ReaderExperienceUiAction {
  action_id: string
  action_type: string
  label: string
  target_ref: string
  payload: Record<string, unknown>
  event_name: string
  agent_handoff: boolean
  meta: Record<string, unknown>
}

export interface ReaderExperienceEventBinding {
  event_id: string
  event_name: string
  event_source: 'user' | 'agent' | 'system'
  event_type: string
  action_ids: string[]
  target_ref: string
  payload: Record<string, unknown>
  meta: Record<string, unknown>
}

export interface ReaderExperienceBlockRef {
  block_id: string
  block_type: 'resource_module' | 'interaction_module' | 'widget'
  version: string
  ref_id: string
  variant: string
  target_ids: string[]
  priority: number
  state: 'ready' | 'empty' | 'loading' | 'partial' | 'error'
  data_requirements: string[]
  fallback_policy: string
  user_actions: string[]
  agent_actions: string[]
  ui_actions: ReaderExperienceUiAction[]
  event_bindings: ReaderExperienceEventBinding[]
  meta: Record<string, unknown>
}

export interface ReaderExperienceSection {
  section_id: string
  section_type: 'hero' | 'focus_stage' | 'reading_flow' | 'explainer_cluster' | 'supporting_resources' | 'question_lab' | 'story_map'
  title: string
  display_title: string
  summary: string
  display_summary: string
  target_ids: string[]
  section_region: 'main' | 'sidebar' | 'footer'
  layout_variant: string
  blocks: ReaderExperienceBlockRef[]
  resource_module_ids: string[]
  interaction_module_ids: string[]
  widget_ids: string[]
  meta: Record<string, unknown>
}

export interface ReaderExperiencePlan {
  version: string
  status: 'draft' | 'done' | 'fallback'
  scope: 'paper' | 'section' | 'page_focus'
  focus_page: number
  reader_profile: string
  layout_variant: string
  page_story_title: string
  page_story_subtitle: string
  narrative_goal: string
  hero: ReaderExperienceHero
  main_sections: ReaderExperienceSection[]
  supporting_resources: ReaderGenerativeResourceModule[]
  interactive_blocks: ReaderGenerativeInteractionModule[]
  widget_blocks: ReaderGenerativeJsWidgetPlan[]
  reading_path: string[]
  used_tools: string[]
  meta: Record<string, unknown>
}

export interface ReaderComposePayload {
  paper_id: number
  page: number
  status: 'done' | 'fallback'
  degraded_reason: string
  pipeline_version?: string
  engine_version: string
  source_signature: string
  build_mode: string
  ui_plan: ReaderUIPlan
  assets: ReaderComposeAsset[]
  scheme_choice?: ReaderComposeSchemeChoice
  decision_log?: string[]
  omission_decisions?: ReaderComposeOmissionDecision[]
  quality_report: ReaderComposeQualityReport
  iteration_trace: Array<Record<string, unknown>>
  main_block_ids: string[]
  aux_block_ids: string[]
  validation_report: ReaderValidationReport
  asset_policy: Record<string, unknown>
  layout_channels?: Record<string, string[]>
  mm_assist_meta?: Record<string, unknown>
  parser_chain_meta?: Record<string, unknown>
  page_structure_v3?: Record<string, unknown>
  canonical_atoms?: Record<string, unknown>
  atom_semantics?: Record<string, unknown>
  deterministic_page_skeleton?: Record<string, unknown>
  stage2_style_plan?: Record<string, unknown>
  minimal_gate_report?: Record<string, unknown>
  candidate_ranking?: Record<string, unknown>
  repair_report?: Record<string, unknown>
  segment_id_map?: Record<string, unknown>
  stage1_structural_annotations?: Record<string, unknown>
  stage2_design_layout?: Record<string, unknown>
  pipeline_contract_meta?: Record<string, unknown>
  qwen_layout_plan_v2?: LayoutPlanV2 | null
  layout_advice_v3?: Record<string, unknown>
  qwen_plan_meta?: Record<string, unknown>
  assembly_meta?: Record<string, unknown>
  component_registry_version?: string
  segment_map?: Record<string, unknown>
  segment_map_meta?: Record<string, unknown>
  node_gate_report?: NodeGateReport | null
  toc_quality?: number
  phase1_compact_input?: Record<string, unknown>
  review_route_meta?: Record<string, unknown>
  enrichment_bundle?: ReaderEnrichmentBundle
  generative_reader_plan?: ReaderGenerativePlan
  generated_at: string
  cache_hit?: boolean
  cache_layer?: 'redis' | 'db' | 'none' | string
  overlay_applied?: boolean
  overlay_count?: number
}

export interface ReaderComposeReviewSnapshot {
  session_id: string
  snapshot_id: string
  paper_id: number
  page: number
  source_signature: string
  build_mode: string
  status: 'done' | 'fallback'
  ui_plan: ReaderUIPlan
  assets: ReaderComposeAsset[]
  quality_report: ReaderComposeQualityReport
  scheme_choice?: ReaderComposeSchemeChoice
  decision_log: string[]
  omission_decisions: ReaderComposeOmissionDecision[]
  diagnostics: ReaderComposeReviewDiagnostic[]
  phase1_compact_input?: Record<string, unknown>
  enrichment_bundle?: ReaderEnrichmentBundle
  generative_reader_plan?: ReaderGenerativePlan
  render_route: string
  render_image_url?: string
  observation_note?: string
  observation_source?: string
  observation_diagnostics?: ReaderComposeReviewDiagnostic[]
  observation_updated_at?: string | null
  docmind_page_image_url?: string
  style_intent?: string
  theme_mode?: string
  detail_level?: string
  parent_snapshot_id?: string | null
  revision: number
  created_at: string
}

export interface ReaderComposeReviewAutoPatchResponse {
  snapshot: ReaderComposeReviewSnapshot
  patch_applied: boolean
  ui_ops: Record<string, unknown>[]
  ui_ops_count: number
  fallback_reason?: string | null
  validation_errors: string[]
  agent_summary: string
}

export interface ReaderComposePrefetchRequest {
  pages: number[]
  selected_kb_id?: number
  style_intent?: string
  latency_budget_ms?: number
  quality_target?: number
  max_iterations?: number
  theme_mode?: 'light' | 'dark'
  detail_level?: 'concise' | 'standard' | 'deep'
  compare_mode?: boolean
  citation_tldr?: boolean
}

export interface ReaderComposePrefetchResponse {
  queued: number[]
  skipped: number[]
}

export interface ReaderComposeStreamEventMap {
  start: {
    cache_hit: boolean
    cache_layer?: 'redis' | 'db' | 'none' | string
    build_mode: string
    page: number
    engine_version?: string
    budget?: {
      latency_budget_ms?: number
      quality_target?: number
    }
  }
  plan_draft: {
    iteration: number
    ui_plan: ReaderUIPlan
    phase?: 'skeleton' | 'semantic' | 'enhance' | string
    layout_lock?: boolean
  }
  plan_patch: {
    iteration: number
    ui_plan: ReaderUIPlan
    phase?: 'skeleton' | 'semantic' | 'enhance' | string
    patch_type?: 'node_replace' | 'node_insert' | 'node_update' | string
  }
  component_patch: {
    iteration: number
    seq?: number
    source?: 'agent' | string
    ui_ops: ReaderComponentPatchOp[]
  }
  agent_trace: {
    iteration: number
    trace: Array<Record<string, unknown>>
    tool_calls?: Array<Record<string, unknown>>
  }
  component_error: {
    message: string
    errors?: string[]
    stage?: string
    code?: string
    details?: Record<string, unknown>
  }
  assets: {
    assets: ReaderComposeAsset[]
  }
  quality: {
    iteration: number
    quality_report: ReaderComposeQualityReport
    mm_assist_used?: boolean
    mm_fallback_used?: boolean
    cross_column_merge_ratio?: number
    sidebar_recall?: number
  }
  done: {
    status?: 'done' | 'fallback'
    degraded_reason?: string
    validation_report?: ReaderValidationReport
    payload: ReaderComposePayload
    cache_meta?: Record<string, unknown>
    iteration_stats?: Record<string, unknown>
    overlay_meta?: Record<string, unknown>
    qwen_plan_meta?: Record<string, unknown>
    parser_chain_meta?: Record<string, unknown>
    pipeline_contract_meta?: Record<string, unknown>
    stage1_structural_annotations?: Record<string, unknown>
    stage2_design_layout?: Record<string, unknown>
    canonical_atoms?: Record<string, unknown>
    atom_semantics?: Record<string, unknown>
    deterministic_page_skeleton?: Record<string, unknown>
    stage2_style_plan?: Record<string, unknown>
    minimal_gate_report?: Record<string, unknown>
    candidate_ranking?: Record<string, unknown>
    repair_report?: Record<string, unknown>
    segment_id_map?: Record<string, unknown>
    segment_stats?: Record<string, unknown>
    node_gate_stats?: Record<string, unknown>
  }
  error: {
    message: string
    stage?: string
    code?: string
  }
}

export type ReaderComposeStreamEvent = keyof ReaderComposeStreamEventMap

export interface ReaderComposeFetchResponse {
  payload: ReaderComposePayload
  cache_meta?: Record<string, unknown>
}

export interface ReaderGenerativePlanResponse {
  page: number
  plan: ReaderGenerativePlan
  enrichment_bundle: ReaderEnrichmentBundle
  scheme_choice: ReaderComposeSchemeChoice
  compose_status: 'done' | 'fallback'
  compose_build_mode: string
  compose_source_signature: string
  source_sig_hash: string
  cache_hit: boolean
  cache_layer: string
  plan_cache_hit: boolean
  plan_cache_layer: string
}

export interface ReaderExperiencePlanResponse {
  focus_page: number
  plan: ReaderExperiencePlan
  generative_plan: ReaderGenerativePlan
  compose_payload?: ReaderComposePayload | null
  enrichment_bundle: ReaderEnrichmentBundle
  compose_status: 'done' | 'fallback'
  compose_build_mode: string
  compose_source_signature: string
  source_sig_hash: string
  cache_hit: boolean
  cache_layer: string
  generative_plan_cache_hit: boolean
  generative_plan_cache_layer: string
  experience_cache_hit: boolean
  experience_cache_layer: string
}

export interface ReaderNodeActionRequest {
  page: number
  node_id: string
  action: 'regenerate' | 'degrade'
  reason?: string
  selected_kb_id?: number
  style_intent?: string
  theme_mode?: 'light' | 'dark'
  detail_level?: 'concise' | 'standard' | 'deep'
  compare_mode?: boolean
  citation_tldr?: boolean
}

export interface ReaderNodeActionResponse {
  patch_type: 'node_replace' | 'node_insert' | 'node_update'
  node_before?: ReaderComponentNode | null
  node_after?: ReaderComponentNode | null
  quality_delta: number
  overlay_saved: boolean
  message: string
  disabled?: boolean
  disabled_reason?: string | null
}

export interface ReaderInlineQueryRequest {
  page: number
  node_id: string
  question: string
  scope?: 'page' | 'section'
  selected_kb_id?: number
  style_intent?: string
  theme_mode?: 'light' | 'dark'
  detail_level?: 'concise' | 'standard' | 'deep'
  compare_mode?: boolean
  citation_tldr?: boolean
}

export interface ReaderInlineQuerySource {
  page: number
  start_char: number
  end_char: number
  quote?: string | null
  quote_text?: string | null
  canonical_block_id?: string | null
  coord_version?: 'anchor_v2' | string | null
  anchor_confidence?: number | null
}

export interface ReaderInlineQueryEventMap {
  start: {
    page: number
    node_id: string
  }
  token: {
    text: string
  }
  sources: ReaderInlineQuerySource[]
  disabled: {
    disabled: boolean
    disabled_reason?: string
    message?: string
  }
  done: {
    node?: ReaderComponentNode
    sources?: ReaderInlineQuerySource[]
    disabled?: boolean
    disabled_reason?: string
  }
  error: {
    message: string
  }
}

export type ReaderInlineQueryEvent = keyof ReaderInlineQueryEventMap

export interface ReaderGenerativeStreamEventMap {
  start: {
    cache_hit: boolean
    cache_layer?: 'redis' | 'db' | 'none' | string
    build_mode: string
    page: number
    parser_version?: string
  }
  skeleton: {
    sections: ReaderGenerativeSection[]
    summary: string
    style_recommendation?: ReaderGenerativeStyleKey | string
    style_tuning?: ReaderGenerativeStyleTuning
    structure_confidence?: number
  }
  chunk: {
    blocks: ReaderGenerativeBlock[]
  }
  assets: {
    assets: ReaderGenerativeAsset[]
  }
  done: {
    payload: ReaderGenerativePagePayload
    cache_meta?: Record<string, unknown>
  }
  error: {
    message: string
  }
}

export type ReaderGenerativeStreamEvent = keyof ReaderGenerativeStreamEventMap

export interface ReaderPageReadyEventData {
  paper_id: number
  page: number
  source_signature: string
  updated_at?: string
}

export interface PaperAnnotation {
  id: number
  user_id: number
  paper_id: number
  annotation_type: AnnotationType
  page: number
  quote_text?: string
  anchor: Record<string, unknown>
  content?: string
  color: string
  created_at: string
  updated_at: string
}

export interface PaperAnnotationCreate {
  annotation_type: AnnotationType
  page: number
  quote_text?: string
  anchor?: Record<string, unknown>
  content?: string
  color?: string
}

export interface PaperCommentAuthor {
  id: number
  username: string
  full_name?: string
  avatar?: string
}

export interface PaperComment {
  id: number
  paper_entity_id: number
  user_id: number
  parent_id?: number
  content: string
  created_at: string
  updated_at: string
  author: PaperCommentAuthor
}

export interface PaperRatingSummary {
  my_rating?: number
  global_avg?: number
  global_count: number
  same_group_avg?: number
  same_group_count: number
}

export interface PaperKnowledgeLink {
  id: number
  user_id: number
  paper_id: number
  knowledge_base_id: number
  document_id?: number
  status: TaskStatus
  error_message?: string
  created_at: string
  updated_at: string
}

export interface CollectionKnowledgeReadinessItem {
  paper_id: number
  title: string
  status: TaskStatus | 'missing'
  document_id?: number
  error_message?: string
  pdf_available: boolean
}

export interface CollectionKnowledgeReadiness {
  collection_id: number
  knowledge_base_id: number
  total_papers: number
  completed_papers: number
  running_papers: number
  pending_papers: number
  failed_papers: number
  timeout_papers: number
  cancelled_papers: number
  missing_papers: number
  can_cross_paper_answer: boolean
  papers: CollectionKnowledgeReadinessItem[]
}

export interface LiteratureAskRequest {
  scope: LiteratureAskScope
  paper_id?: number
  collection_id?: number
  knowledge_base_id: number
  mode?: 'agentic' | 'classic'
  question: string
  session_id?: number
}

export interface LiteratureAskSource {
  idx?: number
  document_id: number
  document_name: string
  page?: number
  page_source?: 'metadata' | 'estimated' | 'unknown'
  section_title?: string
  section_type?: string
  snippet: string
  score?: number | null
  score_source?: 'fts' | 'fallback' | 'paper_read'
  chunk_id?: number
}

export interface LiteratureAskEvent {
  event: 'start' | 'token' | 'sources' | 'done' | 'error'
  data: any
}

export interface LiteratureAskSession {
  id: number
  user_id: number
  scope: LiteratureAskScope
  paper_id?: number
  collection_id?: number
  knowledge_base_id: number
  title?: string
  created_at: string
  updated_at: string
}

export interface LiteratureAskMessage {
  id: number
  session_id: number
  role: 'user' | 'assistant'
  content: string
  sources: LiteratureAskSource[]
  created_at: string
}


export const literatureApi = {
  init: async (): Promise<{ message: string }> => {
    const response = await api.post('/api/v1/literature/init')
    return response.data
  },

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
    const response = await api.get('/api/v1/literature/search', { params })
    return response.data
  },

  getSearchHistory: async (limit = 20): Promise<SearchHistory[]> => {
    const response = await api.get('/api/v1/literature/search/history', {
      params: { limit },
    })
    return response.data
  },

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
    const response = await api.get('/api/v1/literature/papers', { params })
    return response.data
  },

  getPaper: async (paperId: number): Promise<Paper> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}`)
    return response.data
  },

  getPaperPdfBlob: async (paperId: number, timeoutMs = 180000): Promise<Blob> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/pdf`, {
      responseType: 'blob',
      timeout: timeoutMs,
    })
    return response.data as Blob
  },

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
    const response = await api.post('/api/v1/literature/papers', data)
    return response.data
  },

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
    const response = await api.patch(`/api/v1/literature/papers/${paperId}`, data)
    return response.data
  },

  deletePaper: async (paperId: number): Promise<void> => {
    await api.delete(`/api/v1/literature/papers/${paperId}`)
  },

  downloadPdf: async (
    paperId: number,
    knowledgeBaseId?: number,
    timeoutMs = 180000
  ): Promise<{ message: string; pdf_path: string }> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/download-pdf`, null, {
      params: { knowledge_base_id: knowledgeBaseId },
      timeout: timeoutMs,
    })
    return response.data
  },

  getCollections: async (): Promise<PaperCollection[]> => {
    const response = await api.get('/api/v1/literature/collections')
    return response.data
  },

  createCollection: async (data: {
    name: string
    description?: string
    color?: string
    icon?: string
    collection_type?: string
  }): Promise<PaperCollection> => {
    const response = await api.post('/api/v1/literature/collections', data)
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
    const response = await api.patch(`/api/v1/literature/collections/${collectionId}`, data)
    return response.data
  },

  deleteCollection: async (collectionId: number): Promise<void> => {
    await api.delete(`/api/v1/literature/collections/${collectionId}`)
  },

  addPaperToCollection: async (paperId: number, collectionIds: number[]): Promise<void> => {
    await api.post('/api/v1/literature/collections/add-paper', {
      paper_id: paperId,
      collection_ids: collectionIds,
    })
  },

  removePaperFromCollection: async (paperId: number, collectionId: number): Promise<void> => {
    await api.post('/api/v1/literature/collections/remove-paper', {
      paper_id: paperId,
      collection_id: collectionId,
    })
  },

  getReaderSession: async (paperId: number): Promise<ReaderSession> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/reader/session`)
    return response.data
  },

  updateReaderSession: async (paperId: number, data: ReaderSessionUpdate): Promise<ReaderSession> => {
    const response = await api.put(`/api/v1/literature/papers/${paperId}/reader/session`, data)
    return response.data
  },

  streamReaderGenerative: async (
    paperId: number,
    payload: ReaderGenerativeRequest,
    onEvent?: (
      event: ReaderGenerativeStreamEvent,
      data: ReaderGenerativeStreamEventMap[ReaderGenerativeStreamEvent],
    ) => void,
    abortController?: AbortController,
  ): Promise<void> => {
    const response = await fetch(`${API_BASE_URL}/api/v1/literature/papers/${paperId}/reader/generative/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify(payload),
      signal: abortController?.signal,
    })

    if (!response.ok) {
      let detail = '请求失败'
      try {
        const err = (await response.json()) as { detail?: ApiErrorDetail }
        detail = extractApiErrorMessage(err?.detail, detail)
      } catch {
        // ignore json parse error for non-json body
      }
      throw new Error(detail)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('无法读取响应流')
    }

    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const parsed = JSON.parse(line.slice(6)) as {
            event?: ReaderGenerativeStreamEvent
            data?: ReaderGenerativeStreamEventMap[ReaderGenerativeStreamEvent]
          }
          const event = parsed?.event
          if (!event) continue
          onEvent?.(event, parsed.data as ReaderGenerativeStreamEventMap[ReaderGenerativeStreamEvent])
        } catch {
          // ignore malformed stream chunk
        }
      }
    }
  },

  prefetchReaderGenerative: async (
    paperId: number,
    payload: ReaderGenerativePrefetchRequest,
  ): Promise<ReaderGenerativePrefetchResponse> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/generative/prefetch`, payload)
    return response.data
  },

  streamReaderComposed: async (
    paperId: number,
    payload: ReaderComposeRequest,
    onEvent?: (
      event: ReaderComposeStreamEvent,
      data: ReaderComposeStreamEventMap[ReaderComposeStreamEvent],
    ) => void,
    abortController?: AbortController,
  ): Promise<void> => {
    const response = await fetch(`${API_BASE_URL}/api/v1/literature/papers/${paperId}/reader/composed/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify(payload),
      signal: abortController?.signal,
    })

    if (!response.ok) {
      let detail = '请求失败'
      try {
        const err = (await response.json()) as { detail?: ApiErrorDetail }
        detail = extractApiErrorMessage(err?.detail, detail)
      } catch {
        // ignore json parse error for non-json body
      }
      throw new Error(detail)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('无法读取响应流')
    }

    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const parsed = JSON.parse(line.slice(6)) as {
            event?: ReaderComposeStreamEvent
            data?: ReaderComposeStreamEventMap[ReaderComposeStreamEvent]
          }
          const event = parsed?.event
          if (!event) continue
          onEvent?.(event, parsed.data as ReaderComposeStreamEventMap[ReaderComposeStreamEvent])
        } catch {
          // ignore malformed stream chunk
        }
      }
    }
  },

  getCachedReaderComposed: async (
    paperId: number,
    payload: ReaderComposeRequest,
  ): Promise<ReaderComposeFetchResponse> => {
    const response = await api.post(
      `/api/v1/literature/papers/${paperId}/reader/composed/cached`,
      payload,
      { timeout: 30000 },
    )
    return response.data
  },

  getReaderGenerativePlan: async (
    paperId: number,
    payload: ReaderGenerativePlanRequest,
  ): Promise<ReaderGenerativePlanResponse> => {
    const response = await api.post(
      `/api/v1/literature/papers/${paperId}/reader/composed/generative-plan`,
      payload,
      { timeout: LONG_RUNNING_READER_TIMEOUT_MS },
    )
    return response.data
  },

  getReaderExperiencePlan: async (
    paperId: number,
    payload: ReaderExperiencePlanRequest,
  ): Promise<ReaderExperiencePlanResponse> => {
    const response = await api.post(
      `/api/v1/literature/papers/${paperId}/experience/plan`,
      payload,
      { timeout: LONG_RUNNING_READER_TIMEOUT_MS },
    )
    return response.data
  },

  getCachedReaderExperiencePlan: async (
    paperId: number,
    payload: ReaderExperiencePlanRequest,
  ): Promise<ReaderExperiencePlanResponse> => {
    const response = await api.post(
      `/api/v1/literature/papers/${paperId}/experience/plan/cached`,
      payload,
      { timeout: 30000 },
    )
    return response.data
  },

  createReaderComposeReviewSession: async (
    paperId: number,
    payload: ReaderComposeReviewSessionRequest,
  ): Promise<ReaderComposeReviewSnapshot> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/review-session`, payload)
    return response.data
  },

  importReaderComposeReviewSession: async (
    paperId: number,
    payload: ReaderComposeReviewImportRequest,
  ): Promise<ReaderComposeReviewSnapshot> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/review-session/import`, payload)
    return response.data
  },

  getReaderComposeReviewSnapshot: async (
    paperId: number,
    sessionId: string,
    snapshotId?: string,
  ): Promise<ReaderComposeReviewSnapshot> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/reader/composed/review-session/${sessionId}`, {
      params: snapshotId ? { snapshot_id: snapshotId } : undefined,
    })
    return response.data
  },

  observeReaderComposeReviewSnapshot: async (
    paperId: number,
    sessionId: string,
    payload: ReaderComposeReviewObservationRequest,
  ): Promise<ReaderComposeReviewSnapshot> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/review-session/${sessionId}/observation`, payload)
    return response.data
  },

  uploadReaderComposeReviewObservationImage: async (
    paperId: number,
    sessionId: string,
    image: File | Blob,
    options: ReaderComposeReviewObservationUploadOptions = {},
  ): Promise<ReaderComposeReviewSnapshot> => {
    const formData = new FormData()
    formData.append('image', image)
    if (options.snapshot_id) formData.append('snapshot_id', options.snapshot_id)
    if (options.note) formData.append('note', options.note)
    if (options.source) formData.append('source', options.source)
    if (options.diagnostics?.length) {
      formData.append('diagnostics_json', JSON.stringify(options.diagnostics))
    }
    const response = await api.post(
      `/api/v1/literature/papers/${paperId}/reader/composed/review-session/${sessionId}/observation-image`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return response.data
  },

  patchReaderComposeReviewSnapshot: async (
    paperId: number,
    sessionId: string,
    payload: ReaderComposeReviewPatchRequest,
  ): Promise<ReaderComposeReviewSnapshot> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/review-session/${sessionId}/patch`, payload)
    return response.data
  },

  autoPatchReaderComposeReviewSnapshot: async (
    paperId: number,
    sessionId: string,
    payload: ReaderComposeReviewAutoPatchRequest,
  ): Promise<ReaderComposeReviewAutoPatchResponse> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/review-session/${sessionId}/auto-patch`, payload)
    return response.data
  },

  prefetchReaderComposed: async (
    paperId: number,
    payload: ReaderComposePrefetchRequest,
  ): Promise<ReaderComposePrefetchResponse> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/prefetch`, payload)
    return response.data
  },

  actionReaderComposedNode: async (
    paperId: number,
    payload: ReaderNodeActionRequest,
  ): Promise<ReaderNodeActionResponse> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/reader/composed/node/action`, payload)
    return response.data
  },

  streamReaderComposedInlineQuery: async (
    paperId: number,
    payload: ReaderInlineQueryRequest,
    onEvent?: (
      event: ReaderInlineQueryEvent,
      data: ReaderInlineQueryEventMap[ReaderInlineQueryEvent],
    ) => void,
    abortController?: AbortController,
  ): Promise<void> => {
    const response = await fetch(`${API_BASE_URL}/api/v1/literature/papers/${paperId}/reader/composed/inline-query/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify(payload),
      signal: abortController?.signal,
    })

    if (!response.ok) {
      let detail = '请求失败'
      try {
        const err = (await response.json()) as { detail?: ApiErrorDetail }
        detail = extractApiErrorMessage(err?.detail, detail)
      } catch {
        // ignore json parse error for non-json body
      }
      throw new Error(detail)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('无法读取响应流')
    }

    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const parsed = JSON.parse(line.slice(6)) as {
            event?: ReaderInlineQueryEvent
            data?: ReaderInlineQueryEventMap[ReaderInlineQueryEvent]
          }
          const event = parsed?.event
          if (!event) continue
          onEvent?.(event, parsed.data as ReaderInlineQueryEventMap[ReaderInlineQueryEvent])
        } catch {
          // ignore malformed stream chunk
        }
      }
    }
  },

  getAnnotations: async (
    paperId: number,
    params?: { page?: number; type?: AnnotationType }
  ): Promise<PaperAnnotation[]> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/annotations`, { params })
    return response.data
  },

  createAnnotation: async (paperId: number, data: PaperAnnotationCreate): Promise<PaperAnnotation> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/annotations`, data)
    return response.data
  },

  updateAnnotation: async (
    paperId: number,
    annotationId: number,
    data: Partial<PaperAnnotationCreate>
  ): Promise<PaperAnnotation> => {
    const response = await api.patch(`/api/v1/literature/papers/${paperId}/annotations/${annotationId}`, data)
    return response.data
  },

  deleteAnnotation: async (paperId: number, annotationId: number): Promise<void> => {
    await api.delete(`/api/v1/literature/papers/${paperId}/annotations/${annotationId}`)
  },

  getComments: async (paperId: number, filter: CommentFilter = 'all'): Promise<PaperComment[]> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/comments`, {
      params: { filter },
    })
    return response.data
  },

  createComment: async (
    paperId: number,
    data: { content: string; parent_id?: number }
  ): Promise<PaperComment> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/comments`, data)
    return response.data
  },

  updateComment: async (paperId: number, commentId: number, content: string): Promise<PaperComment> => {
    const response = await api.patch(`/api/v1/literature/papers/${paperId}/comments/${commentId}`, { content })
    return response.data
  },

  deleteComment: async (paperId: number, commentId: number): Promise<void> => {
    await api.delete(`/api/v1/literature/papers/${paperId}/comments/${commentId}`)
  },

  putRating: async (paperId: number, rating: number): Promise<PaperRatingSummary> => {
    const response = await api.put(`/api/v1/literature/papers/${paperId}/rating`, { rating })
    return response.data
  },

  getRatingSummary: async (paperId: number): Promise<PaperRatingSummary> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/ratings/summary`)
    return response.data
  },

  addToKnowledge: async (paperId: number, knowledgeBaseId: number): Promise<PaperKnowledgeLink> => {
    const response = await api.post(`/api/v1/literature/papers/${paperId}/add-to-knowledge`, {
      knowledge_base_id: knowledgeBaseId,
    })
    return response.data
  },

  getKnowledgeLinks: async (paperId: number): Promise<PaperKnowledgeLink[]> => {
    const response = await api.get(`/api/v1/literature/papers/${paperId}/knowledge-links`)
    return response.data
  },

  getCollectionKnowledgeReadiness: async (
    collectionId: number,
    knowledgeBaseId: number,
  ): Promise<CollectionKnowledgeReadiness> => {
    const response = await api.get(`/api/v1/literature/collections/${collectionId}/knowledge-readiness`, {
      params: { knowledge_base_id: knowledgeBaseId },
    })
    return response.data
  },

  streamStatusEvents: async (
    params: { paper_id?: number } | undefined,
    onEvent?: (event: 'connected' | 'heartbeat' | 'paper_link_status' | 'reader_page_ready', data: any) => void,
    abortController?: AbortController,
  ): Promise<void> => {
    const query = new URLSearchParams()
    if (params?.paper_id && params.paper_id > 0) {
      query.set('paper_id', String(params.paper_id))
    }
    const suffix = query.toString() ? `?${query.toString()}` : ''
    await streamJsonSse(`${API_BASE_URL}/api/v1/literature/events/stream${suffix}`, onEvent, abortController)
  },

  getAskSessions: async (params?: {
    scope?: LiteratureAskScope
    paper_id?: number
    collection_id?: number
    knowledge_base_id?: number
    limit?: number
    offset?: number
  }): Promise<LiteratureAskSession[]> => {
    const response = await api.get('/api/v1/literature/ask/sessions', { params })
    return response.data
  },

  getAskMessages: async (
    sessionId: number,
    params?: { limit?: number; offset?: number },
  ): Promise<LiteratureAskMessage[]> => {
    const response = await api.get(`/api/v1/literature/ask/sessions/${sessionId}/messages`, { params })
    return response.data
  },

  askStream: async (
    payload: LiteratureAskRequest,
    onEvent?: (event: LiteratureAskEvent['event'], data: any) => void,
    abortController?: AbortController
  ): Promise<void> => {
    const response = await fetch(`${API_BASE_URL}/api/v1/literature/ask`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify(payload),
      signal: abortController?.signal,
    })

    if (!response.ok) {
      let detail = '请求失败'
      try {
        const err = (await response.json()) as { detail?: ApiErrorDetail }
        detail = extractApiErrorMessage(err?.detail, detail)
      } catch {
        // ignore json parse error for non-json body
      }
      throw new Error(detail)
    }

    const reader = response.body?.getReader()
    if (!reader) {
      throw new Error('无法读取响应流')
    }

    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const parsed = JSON.parse(line.slice(6)) as LiteratureAskEvent
          onEvent?.(parsed.event, parsed.data)
        } catch {
          // ignore malformed chunk
        }
      }
    }
  },
}


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
  terminated_reason?: 'timeout' | 'policy_violation' | 'resource_limit' | 'none'
  policy_violation_code?: string | null
}


export const codelabApi = {
  listNotebooks: async (): Promise<Notebook[]> => {
    const response = await api.get('/api/v1/codelab/notebooks')
    return response.data
  },

  createNotebook: async (data: { title?: string; description?: string }): Promise<Notebook> => {
    const response = await api.post('/api/v1/codelab/notebooks', data)
    return response.data
  },

  getNotebook: async (notebookId: string): Promise<Notebook> => {
    const response = await api.get(`/api/v1/codelab/notebooks/${notebookId}`)
    return response.data
  },

  updateNotebook: async (
    notebookId: string,
    data: { title?: string; description?: string; cells?: Cell[] }
  ): Promise<Notebook> => {
    const response = await api.patch(`/api/v1/codelab/notebooks/${notebookId}`, data)
    return response.data
  },

  deleteNotebook: async (notebookId: string): Promise<void> => {
    await api.delete(`/api/v1/codelab/notebooks/${notebookId}`)
  },

  executeCell: async (notebookId: string, data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post(`/api/v1/codelab/notebooks/${notebookId}/execute`, data)
    return response.data
  },

  executeCode: async (data: ExecuteRequest): Promise<ExecuteResponse> => {
    const response = await api.post('/api/v1/codelab/execute', data)
    return response.data
  },

  addCell: async (notebookId: string, cellType: 'code' | 'markdown', index?: number): Promise<Cell> => {
    const response = await api.post(`/api/v1/codelab/notebooks/${notebookId}/cells`, null, {
      params: { cell_type: cellType, index },
    })
    return response.data
  },

  deleteCell: async (notebookId: string, cellId: string): Promise<void> => {
    await api.delete(`/api/v1/codelab/notebooks/${notebookId}/cells/${cellId}`)
  },

  runAll: async (notebookId: string): Promise<{ message: string; results: any[] }> => {
    const response = await api.post(`/api/v1/codelab/notebooks/${notebookId}/run-all`)
    return response.data
  },

  restartKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/v1/codelab/notebooks/${notebookId}/restart-kernel`)
    return response.data
  },

  getKernelStatus: async (notebookId: string): Promise<{
    status: 'running' | 'stopped'
    execution_count: number
    created_at?: string
    last_used_at?: string
    variables: Record<string, string>
  }> => {
    const response = await api.get(`/api/v1/codelab/notebooks/${notebookId}/kernel-status`)
    return response.data
  },

  interruptKernel: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.post(`/api/v1/codelab/notebooks/${notebookId}/interrupt`)
    return response.data
  },
}


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
    react_steps?: ReactStep[]
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
  user_authorized?: boolean
  stream?: boolean
}

export interface AgentChatEvent {
  type: 'content' | 'done' | 'error' | 'thought' | 'action' | 'observation' | 'answer' | 'start' | 'authorization_required'
  content?: string
  code_blocks?: AgentCodeBlock[]
  react_steps?: ReactStep[]
  suggested_action?: string
  suggested_code?: string
  rag_metrics?: RagMetrics
  error?: string
  tool?: string
  input?: Record<string, any>
  success?: boolean
  output?: string
  error_contract?: ApiErrorContract
  iteration?: number
  action?: string // action requiring approval
  provider?: string
  model?: string
  notebook_updated?: boolean // whether notebook content changed
  cell_id?: string // new cell id
  new_cell?: Cell // created cell payload
  updated_cell?: Cell // updated cell payload
}

// ========== Notebook Agent API ==========

export const agentApi = {
  // Get notebook context
  getContext: async (notebookId: string): Promise<AgentContextResponse> => {
    const response = await api.get(`/api/v1/codelab/notebooks/${notebookId}/agent/context`)
    return response.data
  },

  getHistory: async (notebookId: string): Promise<{
    notebook_id: string
    messages: AgentMessage[]
    created_at: string
    updated_at: string
  }> => {
    const response = await api.get(`/api/v1/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  clearHistory: async (notebookId: string): Promise<{ message: string }> => {
    const response = await api.delete(`/api/v1/codelab/notebooks/${notebookId}/agent/history`)
    return response.data
  },

  chat: async (
    notebookId: string,
    request: AgentChatRequest,
    onEvent: (event: AgentChatEvent) => void,
    abortController?: AbortController
  ): Promise<void> => {
    const response = await fetch(
      `${API_BASE_URL}/api/v1/codelab/notebooks/${notebookId}/agent/chat`,
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
      const error = (await response.json()) as { detail?: ApiErrorDetail }
      throw new Error(extractApiErrorMessage(error.detail, '请求失败'))
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

  chatSync: async (
    notebookId: string,
    request: Omit<AgentChatRequest, 'stream'>
  ): Promise<{
    message: AgentMessage
    suggested_code?: string
    suggested_action?: string
  }> => {
    const response = await api.post(
      `/api/v1/codelab/notebooks/${notebookId}/agent/chat`,
      { ...request, stream: false }
    )
    return response.data
  },

  suggestCode: async (
    notebookId: string,
    description: string
  ): Promise<{
    description: string
    code: string
    full_response: string
  }> => {
    const response = await api.post(
      `/api/v1/codelab/notebooks/${notebookId}/agent/suggest-code`,
      null,
      { params: { description } }
    )
    return response.data
  },

  explainError: async (
    notebookId: string,
    errorMessage: string,
    code?: string
  ): Promise<{
    explanation: string
    fix_code?: string
  }> => {
    const response = await api.post(
      `/api/v1/codelab/notebooks/${notebookId}/agent/explain-error`,
      null,
      { params: { error_message: errorMessage, code } }
    )
    return response.data
  },

  // Analyze data
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
      `/api/v1/codelab/notebooks/${notebookId}/agent/analyze-data`,
      null,
      { params: { variable_name: variableName, analysis_type: analysisType } }
    )
    return response.data
  },
}

function getToken(): string {
  const authStorage = localStorage.getItem('auth-storage')
  if (authStorage) {
    const { state } = JSON.parse(authStorage)
    return state?.token || ''
  }
  return ''
}


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

export enum MentorshipStatus {
  NONE = 'none',
  PENDING = 'pending',
  ACTIVE = 'active',
  INVITED = 'invited',
  ARCHIVED = 'archived',
}

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

export interface StudentDetail extends UserWithRole {
  conversation_count: number
  knowledge_base_count: number
  paper_count: number
  notebook_count: number
}

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

export interface GroupMember {
  id: number
  group_id?: number
  user_id: number
  role: string
  joined_at: string
  username?: string
  full_name?: string
  avatar?: string
  user?: UserWithRole
}

export interface MentorActivity {
  id: string
  type: 'conversation' | 'notebook' | 'knowledge' | 'literature' | 'codelab'
  title: string
  description?: string
  timestamp: string
  student: UserBrief
}

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

export interface SystemStatistics {
  time_window_days: number
  total_users: number
  admin_count: number
  mentor_count: number
  student_count: number
  active_users: number
  inactive_users: number
  total_conversations: number
  total_knowledge_bases: number
  total_documents: number
  total_papers: number
  total_notebooks: number
  total_groups: number
  pending_invitations: number
  total_shared_resources: number
  total_announcements: number
  students_with_mentor: number
  students_without_mentor: number
  activity: {
    new_users_last_7_days: number
    new_conversations_last_7_days: number
    new_knowledge_bases_last_7_days: number
    new_papers_last_7_days: number
    new_notebooks_last_7_days: number
  }
  collaboration: {
    total_groups: number
    active_groups: number
    total_group_members: number
    pending_invitations: number
    total_shared_resources: number
    total_announcements: number
    active_announcements: number
  }
  mentorship: {
    students_with_mentor: number
    students_without_mentor: number
  }
  document_pipeline: {
    total_documents: number
    completed_documents: number
    running_documents: number
    failed_documents: number
    pending_documents: number
    timeout_documents: number
    cancelled_documents: number
  }
  trends_7d: {
    users: Array<{ date: string; count: number }>
    conversations: Array<{ date: string; count: number }>
    knowledge_bases: Array<{ date: string; count: number }>
    papers: Array<{ date: string; count: number }>
    notebooks: Array<{ date: string; count: number }>
  }
  share_breakdown: Array<{ key: string; label: string; count: number }>
  invitation_breakdown: Array<{ key: string; label: string; count: number }>
  top_mentors: Array<{
    mentor_id: number
    username: string
    full_name?: string
    student_count: number
    group_count: number
  }>
  recent_activity: Array<{
    id: string
    type: string
    title: string
    owner_name: string
    owner_role: string
    created_at: string
  }>
  ai_rag: {
    assistant_messages_last_window: number
    rag_messages_last_window: number
    knowledge_search_calls_last_window: number
    citation_required_answers_last_window: number
    citation_valid_answers_last_window: number
    citation_repair_attempts_last_window: number
    citation_repair_successes_last_window: number
    compression_calls_last_window: number
    compression_fallback_chunks_last_window: number
    assistant_total_tokens_last_window: number
    agent_runs_last_window: number
    successful_agent_runs_last_window: number
  }
  codelab: {
    notebooks_active_last_window: number
    executed_notebooks: number
    total_execution_count: number
    code_cells: number
    executed_code_cells: number
    agent_runs_last_window: number
    agent_tokens_last_window: number
  }
  literature: {
    total_collections: number
    active_read_sessions_last_window: number
    annotations_last_window: number
    comments_last_window: number
    ratings_last_window: number
    qa_sessions_last_window: number
    qa_messages_last_window: number
    knowledge_links_total: number
    knowledge_link_breakdown: Array<{ key: string; label: string; count: number }>
  }
}

export interface StatisticsDetailItem {
  id: string
  entity: string
  title: string
  subtitle?: string
  status?: string
  category?: string
  owner_name?: string
  owner_role?: string
  target_name?: string
  permission?: string
  member_count?: number
  created_at?: string
  updated_at?: string
}

export interface StatisticsDetailResponse {
  entity: string
  total: number
  page: number
  page_size: number
  items: StatisticsDetailItem[]
}

export interface AdminAuditLogItem {
  id: number
  action: string
  target_type?: string
  target_id?: string
  admin_name: string
  summary: string
  created_at: string
}

export interface AdminAuditLogResponse {
  total: number
  page: number
  page_size: number
  items: AdminAuditLogItem[]
}


export const adminApi = {
  getUsers: async (params?: {
    skip?: number
    limit?: number
    role?: UserRole
    search?: string
    is_active?: boolean
  }): Promise<UserWithRole[]> => {
    const response = await api.get('/api/v1/admin/users', { params })
    return response.data
  },

  getUser: async (userId: number): Promise<UserWithRole> => {
    const response = await api.get(`/api/v1/admin/users/${userId}`)
    return response.data
  },

  updateUserRole: async (userId: number, role: UserRole): Promise<UserWithRole> => {
    const response = await api.put(`/api/v1/admin/users/${userId}/role`, { role })
    return response.data
  },

  toggleUserActive: async (userId: number): Promise<{ is_active: boolean }> => {
    const response = await api.put(`/api/v1/admin/users/${userId}/toggle-active`)
    return response.data
  },

  deleteUser: async (userId: number): Promise<void> => {
    await api.delete(`/api/v1/admin/users/${userId}`)
  },

  getStatistics: async (params?: { days?: number }): Promise<SystemStatistics> => {
    const response = await api.get('/api/v1/admin/statistics', { params })
    return response.data
  },

  getStatisticsDetails: async (params: {
    entity: 'groups' | 'shares' | 'invitations' | 'announcements'
    page?: number
    page_size?: number
    status?: string
    category?: string
    search?: string
  }): Promise<StatisticsDetailResponse> => {
    const response = await api.get('/api/v1/admin/statistics/details', { params })
    return response.data
  },

  getAuditLogs: async (params?: {
    page?: number
    page_size?: number
    action?: string
    search?: string
  }): Promise<AdminAuditLogResponse> => {
    const response = await api.get('/api/v1/admin/audit-logs', { params })
    return response.data
  },

  exportStatistics: async (params: {
    scope: 'summary' | 'details' | 'audit'
    days?: number
    entity?: 'groups' | 'shares' | 'invitations' | 'announcements'
    action?: string
    status?: string
    category?: string
    search?: string
  }): Promise<Blob> => {
    const response = await api.get('/api/v1/admin/statistics/export', {
      params,
      responseType: 'blob',
    })
    return response.data as Blob
  },

  setUserMentor: async (userId: number, mentorId: number | null): Promise<UserWithRole> => {
    const response = await api.put(`/api/v1/admin/users/${userId}/mentor`, { mentor_id: mentorId })
    return response.data
  },
}


export const mentorApi = {
  getStudents: async (): Promise<StudentDetail[]> => {
    const response = await api.get('/api/v1/mentor/students')
    return response.data
  },

  getStudentDetail: async (studentId: number): Promise<StudentDetail> => {
    const response = await api.get(`/api/v1/mentor/students/${studentId}`)
    return response.data
  },

  inviteStudent: async (email: string, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/v1/mentor/students/invite', { email, message })
    return response.data
  },

  removeStudent: async (studentId: number): Promise<void> => {
    await api.delete(`/api/v1/mentor/students/${studentId}`)
  },

  getSentInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/v1/mentor/invitations/sent')
    return response.data
  },

  cancelInvitation: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/v1/mentor/invitations/${invitationId}`)
  },

  handleApplication: async (invitationId: number, accept: boolean): Promise<void> => {
    const response = await api.post(`/api/v1/mentor/applications/${invitationId}/${accept ? 'accept' : 'reject'}`)
    return response.data
  },

  getGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/v1/mentor/groups')
    return response.data
  },

  createGroup: async (data: {
    name: string
    description?: string
    max_members?: number
  }): Promise<ResearchGroup> => {
    const response = await api.post('/api/v1/mentor/groups', data)
    return response.data
  },

  updateGroup: async (groupId: number, data: {
    name?: string
    description?: string
    max_members?: number
    is_active?: boolean
  }): Promise<ResearchGroup> => {
    const response = await api.put(`/api/v1/mentor/groups/${groupId}`, data)
    return response.data
  },

  deleteGroup: async (groupId: number): Promise<void> => {
    await api.delete(`/api/v1/mentor/groups/${groupId}`)
  },

  getGroupMembers: async (groupId: number): Promise<GroupMember[]> => {
    const response = await api.get(`/api/v1/mentor/groups/${groupId}/members`)
    return response.data
  },

  addGroupMember: async (groupId: number, userId: number): Promise<GroupMember> => {
    const response = await api.post(`/api/v1/mentor/groups/${groupId}/members`, { user_id: userId })
    return response.data
  },

  removeGroupMember: async (groupId: number, userId: number): Promise<void> => {
    await api.delete(`/api/v1/mentor/groups/${groupId}/members/${userId}`)
  },

  getActivities: async (skip = 0, limit = 20): Promise<MentorActivity[]> => {
    const response = await api.get('/api/v1/mentor/activities', {
      params: { skip, limit },
    })
    return response.data
  },
}


export const studentApi = {
  getMentor: async (): Promise<UserWithRole | null> => {
    try {
      const response = await api.get('/api/v1/student/mentor')
      return response.data
    } catch (error: any) {
      if (error.response?.status === 404) {
        return null
      }
      throw error
    }
  },

  searchMentors: async (query: string): Promise<UserWithRole[]> => {
    const response = await api.get('/api/v1/student/mentors/search', { params: { query } })
    return response.data
  },

  applyToMentor: async (mentorId: number, message?: string): Promise<Invitation> => {
    const response = await api.post('/api/v1/student/mentor/apply', { mentor_id: mentorId, message })
    return response.data
  },

  leaveMentor: async (): Promise<void> => {
    await api.delete('/api/v1/student/mentor/leave')
  },

  getMyApplications: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/v1/student/applications')
    return response.data
  },

  cancelApplication: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/v1/student/applications/${invitationId}`)
  },

  getReceivedInvitations: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/v1/student/invitations')
    return response.data
  },

  acceptInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/v1/student/invitations/${invitationId}/accept`)
  },

  rejectInvitation: async (invitationId: number): Promise<void> => {
    await api.post(`/api/v1/student/invitations/${invitationId}/reject`)
  },

  getMyGroups: async (): Promise<ResearchGroup[]> => {
    const response = await api.get('/api/v1/student/groups')
    return response.data
  },
}


export const invitationApi = {
  getAll: async (): Promise<Invitation[]> => {
    const response = await api.get('/api/v1/invitations')
    return response.data
  },

  accept: async (invitationId: number): Promise<void> => {
    await api.post(`/api/v1/invitations/${invitationId}/accept`)
  },

  reject: async (invitationId: number): Promise<void> => {
    await api.post(`/api/v1/invitations/${invitationId}/reject`)
  },

  cancel: async (invitationId: number): Promise<void> => {
    await api.delete(`/api/v1/invitations/${invitationId}`)
  },
}


export const shareApi = {
  getSharedWithMe: async (resourceType?: string): Promise<SharedResource[]> => {
    const params = resourceType ? { resource_type: resourceType } : {}
    const response = await api.get('/api/v1/share/shared-with-me', { params })
    return response.data
  },

  getSharedCount: async (): Promise<{ paper: number; paper_collection: number; knowledge_base: number; notebook: number; total: number }> => {
    const response = await api.get('/api/v1/share/shared-with-me/count')
    return response.data
  },

  getMyShares: async (resourceType?: string): Promise<SharedResource[]> => {
    const params = resourceType ? { resource_type: resourceType } : {}
    const response = await api.get('/api/v1/share/my-shares', { params })
    return response.data
  },

  getMyGroups: async (): Promise<{ id: number; name: string; role: string }[]> => {
    const response = await api.get('/api/v1/share/my-groups')
    return response.data
  },

  getMyPapers: async (search?: string): Promise<{ id: number; title: string; authors: string[]; year: number; venue: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/v1/share/my-papers', { params })
    return response.data
  },

  getMyCollections: async (search?: string): Promise<{ id: number; name: string; description: string; paper_count: number; color: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/v1/share/my-collections', { params })
    return response.data
  },

  getMyKnowledgeBases: async (search?: string): Promise<{ id: number; name: string; description: string; document_count: number }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/v1/share/my-knowledge-bases', { params })
    return response.data
  },

  getMyNotebooks: async (search?: string): Promise<{ id: string; title: string; description: string; cell_count: number; updated_at: string }[]> => {
    const params = search ? { search } : {}
    const response = await api.get('/api/v1/share/my-notebooks', { params })
    return response.data
  },

  // Share a resource
  shareResource: async (data: {
    resource_type: string
    resource_id: number | string // supports numeric id or string id (e.g. notebook UUID)
    shared_with_type: 'user' | 'group' | 'all_students'
    shared_with_id?: number
    permission?: string
    message?: string
  }): Promise<SharedResource> => {
    const response = await api.post('/api/v1/share/', data)
    return response.data
  },

  // Batch share
  batchShare: async (data: {
    resource_type: string
    resource_ids: (number | string)[] // supports numeric id or string id
    shared_with_type: 'user' | 'group' | 'all_students'
    shared_with_id?: number
    permission?: string
  }): Promise<{ success_count: number; skip_count: number; message: string }> => {
    const response = await api.post('/api/v1/share/batch', data)
    return response.data
  },

  copyToLibrary: async (shareId: number, collectionId?: number): Promise<{ message: string; paper_id: number }> => {
    const params = collectionId ? { collection_id: collectionId } : {}
    const response = await api.post(`/api/v1/share/copy-to-library/${shareId}`, null, { params })
    return response.data
  },

  getSharedDetail: async (shareId: number): Promise<any> => {
    const response = await api.get(`/api/v1/share/detail/${shareId}`)
    return response.data
  },

  copyCollectionPapers: async (shareId: number, paperIds?: number[], targetCollectionId?: number): Promise<{ success_count: number; skip_count: number; message: string }> => {
    const response = await api.post(`/api/v1/share/copy-collection-papers/${shareId}`, {
      paper_ids: paperIds,
      target_collection_id: targetCollectionId,
    })
    return response.data
  },

  removeShare: async (shareId: number): Promise<void> => {
    await api.delete(`/api/v1/share/${shareId}`)
  },
}


export const announcementApi = {
  getAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/v1/announcements')
    return response.data
  },

  getMyAnnouncements: async (): Promise<Announcement[]> => {
    const response = await api.get('/api/v1/announcements/my')
    return response.data
  },

  createAnnouncement: async (data: {
    title: string
    content: string
    group_id?: number
    is_pinned?: boolean
  }): Promise<Announcement> => {
    const response = await api.post('/api/v1/announcements', data)
    return response.data
  },

  updateAnnouncement: async (announcementId: number, data: {
    title?: string
    content?: string
    is_pinned?: boolean
    is_active?: boolean
  }): Promise<Announcement> => {
    const response = await api.put(`/api/v1/announcements/${announcementId}`, data)
    return response.data
  },

  deleteAnnouncement: async (announcementId: number): Promise<void> => {
    await api.delete(`/api/v1/announcements/${announcementId}`)
  },

  markAsRead: async (announcementId: number): Promise<void> => {
    await api.post(`/api/v1/announcements/${announcementId}/read`)
  },

  getReadStats: async (announcementId: number): Promise<{
    total_count: number
    read_count: number
    readers: Array<{ user_id: number; username: string; read_at: string }>
  }> => {
    const response = await api.get(`/api/v1/announcements/${announcementId}/stats`)
    return response.data
  },
}


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

  getActivities: async (skip = 0, limit = 20): Promise<MentorActivity[]> => {
    return mentorApi.getActivities(skip, limit)
  },

  deleteMentorship: async (mentorshipId: number): Promise<void> => {
    await mentorshipApi.updateMentorshipStatus(mentorshipId, MentorshipStatus.ARCHIVED)
  },

  getPendingCount: async (): Promise<number> => {
    const invitations = await invitationApi.getAll().catch(() => [])
    return invitations.filter((inv) => inv.status === InvitationStatus.PENDING).length
  },
}


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
    const response = await api.get('/api/v1/mcp/templates')
    return response.data
  },

  getConfig: async (): Promise<MCPConfigResponse> => {
    const response = await api.get('/api/v1/mcp/config')
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
    const response = await api.post('/api/v1/mcp/config/validate', payload)
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
    const response = await api.put('/api/v1/mcp/config', payload)
    return response.data
  },

  refreshStatus: async (forceRefresh = true): Promise<{
    server_count: number
    tool_count: number
    servers: MCPServerStatusItem[]
  }> => {
    const response = await api.post('/api/v1/mcp/status/refresh', { force_refresh: forceRefresh })
    return response.data
  },
}

export default api


export enum ChunkingStrategy {
  FIXED = 'fixed',
  SEMANTIC = 'semantic',
  HIERARCHICAL = 'hierarchical',
  ACADEMIC = 'academic',
  HYBRID = 'hybrid',
  LLM = 'llm',
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
  LLM = 'llm',
}

export interface ChunkingConfig {
  strategy: ChunkingStrategy
  use_token_based: boolean
  base_chunk_tokens: number
  overlap_tokens: number
  min_semantic_tokens: number
  max_semantic_tokens: number
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
  estimated_chunks?: number
  language?: string
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


export const chunkingApi = {
  getPresets: async (): Promise<{ presets: PresetDescription[] }> => {
    const response = await api.get('/api/v1/chunking/presets')
    return response.data
  },

  getPreset: async (presetName: ChunkingPreset): Promise<ChunkingConfigResponse> => {
    const response = await api.get(`/api/v1/chunking/presets/${presetName}`)
    return response.data
  },

  previewChunking: async (
    text: string,
    config?: Partial<ChunkingConfig>,
    preset?: ChunkingPreset,
    fileType = 'txt'
  ): Promise<ChunkingResult> => {
    const response = await api.post('/api/v1/chunking/preview', {
      text,
      config,
      preset,
      file_type: fileType,
    })
    return response.data
  },

  analyzeDocument: async (
    text: string,
    fileType = 'txt'
  ): Promise<DocumentAnalysis> => {
    const response = await api.post('/api/v1/chunking/analyze', {
      text,
      file_type: fileType,
    })
    return response.data
  },

  compareStrategies: async (
    text: string,
    strategies: ChunkingPreset[] = [ChunkingPreset.FAST, ChunkingPreset.PRECISE, ChunkingPreset.LLM],
    fileType = 'txt'
  ): Promise<StrategyComparison> => {
    const params = new URLSearchParams()
    strategies.forEach(s => params.append('strategies', s))

    const response = await api.post(`/api/v1/chunking/compare?${params.toString()}`, {
      text,
      file_type: fileType,
    })
    return response.data
  },

  getKnowledgeBaseConfig: async (kbId: number): Promise<ChunkingConfigResponse | null> => {
    try {
      const response = await api.get(`/api/v1/chunking/knowledge-base/${kbId}/config`)
      return response.data
    } catch {
      return null
    }
  },

  updateKnowledgeBaseConfig: async (
    kbId: number,
    config: Partial<ChunkingConfig> | { preset: ChunkingPreset }
  ): Promise<ChunkingConfigResponse> => {
    const response = await api.put(`/api/v1/chunking/knowledge-base/${kbId}/config`, config)
    return response.data
  },

  applyPresetToKnowledgeBase: async (
    kbId: number,
    preset: ChunkingPreset
  ): Promise<{ message: string; knowledge_base_id: number; preset: string }> => {
    const response = await api.post(`/api/v1/chunking/knowledge-base/${kbId}/apply-preset?preset=${preset}`)
    return response.data
  },
}




