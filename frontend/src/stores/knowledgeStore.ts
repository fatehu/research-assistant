import { create } from 'zustand'
import {
  knowledgeApi,
  KnowledgeBase,
  Document,
  DocumentUploadOptions,
  DocumentChunk,
  SearchResult,
  SearchResponse,
  KnowledgeSearchOptions,
  ProcessingStatus,
  isApiCanceledError,
  isApiTimeoutError,
} from '@/services/api'
import { handleApiError } from '@/utils/apiErrorHandler'

const inflightDocumentStatusRequests = new Map<string, Promise<ProcessingStatus | undefined>>()
const inflightKnowledgeBaseSummaryRequests = new Map<number, Promise<KnowledgeBase | undefined>>()

interface KnowledgeState {
  // 知识库列表
  knowledgeBases: KnowledgeBase[]
  currentKnowledgeBase: KnowledgeBase | null
  totalKnowledgeBases: number

  // 文档列表
  documents: Document[]
  currentDocument: Document | null
  totalDocuments: number

  // 分片列表
  chunks: DocumentChunk[]
  totalChunks: number

  // 搜索结果
  searchResults: SearchResult[]
  searchQuery: string
  searchTime: number

  // 加载状态
  isLoading: boolean
  isUploading: boolean
  isSearching: boolean

  // Actions
  fetchKnowledgeBases: () => Promise<void>
  createKnowledgeBase: (name: string, description?: string, embedding_model?: string) => Promise<KnowledgeBase>
  selectKnowledgeBase: (kbId: number) => Promise<void>
  updateKnowledgeBase: (kbId: number, data: Partial<KnowledgeBase>) => Promise<void>
  refreshKnowledgeBaseSummary: (kbId: number) => Promise<KnowledgeBase | undefined>
  deleteKnowledgeBase: (kbId: number) => Promise<void>

  fetchDocuments: (kbId: number) => Promise<void>
  uploadDocument: (kbId: number, file: File, options?: DocumentUploadOptions) => Promise<Document>
  selectDocument: (kbId: number, docId: number) => Promise<void>
  deleteDocument: (kbId: number, docId: number) => Promise<void>
  retryDocument: (kbId: number, docId: number) => Promise<ProcessingStatus>
  cancelDocument: (kbId: number, docId: number) => Promise<ProcessingStatus>
  refreshDocumentStatus: (kbId: number, docId: number) => Promise<ProcessingStatus | undefined>
  applyDocumentStatusPatch: (
    docId: number,
    patch: {
      status?: Document['status']
      processing_stage?: string
      processing_stage_label?: string
      processing_progress?: number
      processing_detail?: string
      chunk_count?: number
      error_message?: string
    },
  ) => void

  fetchChunks: (kbId: number, docId: number) => Promise<void>

  search: (
    query: string,
    knowledgeBaseIds?: number[],
    includeShared?: boolean,
    chunkLevel?: string,
    sectionType?: string,
    includeParentContext?: boolean,
    options?: KnowledgeSearchOptions,
  ) => Promise<SearchResponse>
  clearSearch: () => void

  clearCurrentKnowledgeBase: () => void
  clearCurrentDocument: () => void
}

export const useKnowledgeStore = create<KnowledgeState>((set, get) => ({
  knowledgeBases: [],
  currentKnowledgeBase: null,
  totalKnowledgeBases: 0,

  documents: [],
  currentDocument: null,
  totalDocuments: 0,

  chunks: [],
  totalChunks: 0,

  searchResults: [],
  searchQuery: '',
  searchTime: 0,

  isLoading: false,
  isUploading: false,
  isSearching: false,

  // ========== 知识库操作 ==========

  fetchKnowledgeBases: async () => {
    set({ isLoading: true })
    try {
      const { items, total } = await knowledgeApi.getKnowledgeBases()
      set({ knowledgeBases: items, totalKnowledgeBases: total, isLoading: false })
    } catch (error) {
      handleApiError(error, '获取知识库列表')
      set({ isLoading: false })
    }
  },

  createKnowledgeBase: async (name: string, description?: string, embedding_model?: string) => {
    const kb = await knowledgeApi.createKnowledgeBase({ name, description, embedding_model })
    set((state) => ({
      knowledgeBases: [kb, ...state.knowledgeBases],
      totalKnowledgeBases: state.totalKnowledgeBases + 1,
    }))
    return kb
  },

  selectKnowledgeBase: async (kbId: number) => {
    set({ isLoading: true })
    try {
      const kb = await knowledgeApi.getKnowledgeBase(kbId)
      set({ currentKnowledgeBase: kb, isLoading: false })
      // 同时获取文档列表
      await get().fetchDocuments(kbId)
    } catch (error) {
      handleApiError(error, '获取知识库详情')
      set({ isLoading: false })
      throw error
    }
  },

  updateKnowledgeBase: async (kbId: number, data: Partial<KnowledgeBase>) => {
    const kb = await knowledgeApi.updateKnowledgeBase(kbId, data)
    set((state) => ({
      knowledgeBases: state.knowledgeBases.map((k) => (k.id === kbId ? kb : k)),
      currentKnowledgeBase: state.currentKnowledgeBase?.id === kbId ? kb : state.currentKnowledgeBase,
    }))
  },

  refreshKnowledgeBaseSummary: async (kbId: number) => {
    const normalizedKbId = Number(kbId || 0)
    if (!Number.isFinite(normalizedKbId) || normalizedKbId <= 0) {
      return undefined
    }

    const existing = inflightKnowledgeBaseSummaryRequests.get(normalizedKbId)
    if (existing) {
      return existing
    }

    const request = (async () => {
      try {
        const kb = await knowledgeApi.getKnowledgeBase(normalizedKbId)
        set((state) => ({
          knowledgeBases: state.knowledgeBases.map((item) => (item.id === normalizedKbId ? kb : item)),
          currentKnowledgeBase: state.currentKnowledgeBase?.id === normalizedKbId ? kb : state.currentKnowledgeBase,
        }))
        return kb
      } catch (error) {
        handleApiError(error, '刷新知识库统计')
        return undefined
      } finally {
        inflightKnowledgeBaseSummaryRequests.delete(normalizedKbId)
      }
    })()

    inflightKnowledgeBaseSummaryRequests.set(normalizedKbId, request)
    return request
  },

  deleteKnowledgeBase: async (kbId: number) => {
    await knowledgeApi.deleteKnowledgeBase(kbId)
    const { currentKnowledgeBase } = get()
    set((state) => ({
      knowledgeBases: state.knowledgeBases.filter((k) => k.id !== kbId),
      totalKnowledgeBases: state.totalKnowledgeBases - 1,
      currentKnowledgeBase: currentKnowledgeBase?.id === kbId ? null : currentKnowledgeBase,
      documents: currentKnowledgeBase?.id === kbId ? [] : state.documents,
    }))
  },

  // ========== 文档操作 ==========

  fetchDocuments: async (kbId: number) => {
    set({ isLoading: true })
    try {
      const { items, total } = await knowledgeApi.getDocuments(kbId)
      set({ documents: items, totalDocuments: total, isLoading: false })
    } catch (error) {
      handleApiError(error, '获取文档列表')
      set({ isLoading: false })
    }
  },

  uploadDocument: async (kbId: number, file: File, options?: DocumentUploadOptions) => {
    set({ isUploading: true })
    try {
      const doc = await knowledgeApi.uploadDocument(kbId, file, options)
      set((state) => ({
        documents: [doc, ...state.documents],
        totalDocuments: state.totalDocuments + 1,
        knowledgeBases: state.knowledgeBases.map((kb) =>
          kb.id === kbId
            ? {
              ...kb,
              document_count: kb.document_count + 1,
            }
            : kb,
        ),
        currentKnowledgeBase: state.currentKnowledgeBase?.id === kbId
          ? {
            ...state.currentKnowledgeBase,
            document_count: state.currentKnowledgeBase.document_count + 1,
          }
          : state.currentKnowledgeBase,
        isUploading: false,
      }))
      return doc
    } catch (error) {
      set({ isUploading: false })
      throw error
    }
  },

  selectDocument: async (kbId: number, docId: number) => {
    set({ isLoading: true })
    try {
      const doc = await knowledgeApi.getDocument(kbId, docId)
      set({ currentDocument: doc, isLoading: false })
    } catch (error) {
      handleApiError(error, '获取文档详情')
      set({ isLoading: false })
      throw error
    }
  },

  deleteDocument: async (kbId: number, docId: number) => {
    await knowledgeApi.deleteDocument(kbId, docId)
    const { currentDocument } = get()
    set((state) => ({
      documents: state.documents.filter((d) => d.id !== docId),
      totalDocuments: state.totalDocuments - 1,
      currentDocument: currentDocument?.id === docId ? null : currentDocument,
    }))
    // 刷新知识库信息
    await get().selectKnowledgeBase(kbId)
  },

  retryDocument: async (kbId: number, docId: number) => {
    const status = await knowledgeApi.retryDocument(kbId, docId)
    get().applyDocumentStatusPatch(docId, {
      status: status.status as Document['status'],
      processing_stage: status.processing_stage,
      processing_stage_label: status.processing_stage_label,
      processing_progress: status.progress,
      processing_detail: status.processing_detail,
      chunk_count: status.chunk_count,
      error_message: status.error,
    })
    return status
  },

  cancelDocument: async (kbId: number, docId: number) => {
    const status = await knowledgeApi.cancelDocument(kbId, docId)
    get().applyDocumentStatusPatch(docId, {
      status: status.status as Document['status'],
      processing_stage: status.processing_stage,
      processing_stage_label: status.processing_stage_label,
      processing_progress: status.progress,
      processing_detail: status.processing_detail,
      chunk_count: status.chunk_count,
      error_message: status.error,
    })
    return status
  },

  refreshDocumentStatus: async (kbId: number, docId: number) => {
    const requestKey = `${kbId}:${docId}`
    const existing = inflightDocumentStatusRequests.get(requestKey)
    if (existing) {
      return existing
    }

    const request = (async () => {
      try {
        const status = await knowledgeApi.getDocumentStatus(kbId, docId)
        get().applyDocumentStatusPatch(docId, {
          status: status.status as Document['status'],
          processing_stage: status.processing_stage,
          processing_stage_label: status.processing_stage_label,
          processing_progress: status.progress,
          processing_detail: status.processing_detail,
          chunk_count: status.chunk_count,
          error_message: status.error,
        })
        return status
      } catch (error: any) {
        const statusCode = Number(error?.response?.status || 0)
        if (statusCode === 429) {
          console.warn(`[KnowledgeStore] 文档状态轮询触发限流，已静默退避: kb=${kbId}, doc=${docId}`)
          return undefined
        }
        handleApiError(error, '获取文档状态')
        return undefined
      } finally {
        inflightDocumentStatusRequests.delete(requestKey)
      }
    })()

    inflightDocumentStatusRequests.set(requestKey, request)
    return request
  },

  applyDocumentStatusPatch: (docId, patch) => {
    set((state) => ({
      documents: state.documents.map((d) =>
        d.id === docId
          ? {
            ...d,
            status: patch.status ?? d.status,
            processing_stage: patch.processing_stage ?? d.processing_stage,
            processing_stage_label: patch.processing_stage_label ?? d.processing_stage_label,
            processing_progress: patch.processing_progress ?? d.processing_progress,
            processing_detail: patch.processing_detail ?? d.processing_detail,
            chunk_count: patch.chunk_count ?? d.chunk_count,
            error_message: patch.error_message ?? d.error_message,
          }
          : d,
      ),
      currentDocument: state.currentDocument?.id === docId
        ? {
          ...state.currentDocument,
          status: patch.status ?? state.currentDocument.status,
          processing_stage: patch.processing_stage ?? state.currentDocument.processing_stage,
          processing_stage_label: patch.processing_stage_label ?? state.currentDocument.processing_stage_label,
          processing_progress: patch.processing_progress ?? state.currentDocument.processing_progress,
          processing_detail: patch.processing_detail ?? state.currentDocument.processing_detail,
          chunk_count: patch.chunk_count ?? state.currentDocument.chunk_count,
          error_message: patch.error_message ?? state.currentDocument.error_message,
        }
        : state.currentDocument,
    }))
  },

  // ========== 分片操作 ==========

  fetchChunks: async (kbId: number, docId: number) => {
    set({ isLoading: true })
    try {
      const { items, total } = await knowledgeApi.getChunks(kbId, docId)
      set({ chunks: items, totalChunks: total, isLoading: false })
    } catch (error) {
      handleApiError(error, '获取分片列表')
      set({ isLoading: false })
    }
  },

  // ========== 搜索操作 ==========

  search: async (
    query: string,
    knowledgeBaseIds?: number[],
    includeShared: boolean = true,
    chunkLevel: string = 'paragraph',
    sectionType?: string,
    includeParentContext: boolean = false,
    options: KnowledgeSearchOptions = {},
  ) => {
    set({ isSearching: true, searchQuery: query })
    try {
      // Keep all search option fields intact when forwarding to API.
      const searchOptions: KnowledgeSearchOptions = { ...options }
      const response = await knowledgeApi.search(
        query, knowledgeBaseIds, 5, 0.5, includeShared,
        chunkLevel, sectionType, includeParentContext, searchOptions,
      )
      set({
        searchResults: response.results,
        searchTime: response.search_time_ms,
        isSearching: false,
      })
      return response
    } catch (error) {
      if (!isApiCanceledError(error) && !isApiTimeoutError(error)) {
        handleApiError(error, '搜索')
      }
      set({ isSearching: false })
      throw error
    }
  },

  clearSearch: () => {
    set({ searchResults: [], searchQuery: '', searchTime: 0 })
  },

  // ========== 清理操作 ==========

  clearCurrentKnowledgeBase: () => {
    set({ currentKnowledgeBase: null, documents: [], totalDocuments: 0 })
  },

  clearCurrentDocument: () => {
    set({ currentDocument: null, chunks: [], totalChunks: 0 })
  },
}))
