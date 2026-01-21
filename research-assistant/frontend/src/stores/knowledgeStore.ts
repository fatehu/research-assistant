import { create } from 'zustand'
import { knowledgeApi, KnowledgeBase, Document, DocumentChunk, SearchResult, SearchResponse } from '@/services/api'

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
  createKnowledgeBase: (name: string, description?: string) => Promise<KnowledgeBase>
  selectKnowledgeBase: (kbId: number) => Promise<void>
  updateKnowledgeBase: (kbId: number, data: Partial<KnowledgeBase>) => Promise<void>
  deleteKnowledgeBase: (kbId: number) => Promise<void>
  
  fetchDocuments: (kbId: number) => Promise<void>
  uploadDocument: (kbId: number, file: File) => Promise<Document>
  selectDocument: (kbId: number, docId: number) => Promise<void>
  deleteDocument: (kbId: number, docId: number) => Promise<void>
  refreshDocumentStatus: (kbId: number, docId: number) => Promise<void>
  
  fetchChunks: (kbId: number, docId: number) => Promise<void>
  
  search: (query: string, knowledgeBaseIds?: number[]) => Promise<SearchResponse>
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
      console.error('获取知识库列表失败:', error)
      set({ isLoading: false })
    }
  },
  
  createKnowledgeBase: async (name: string, description?: string) => {
    const kb = await knowledgeApi.createKnowledgeBase({ name, description })
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
      console.error('获取知识库详情失败:', error)
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
      console.error('获取文档列表失败:', error)
      set({ isLoading: false })
    }
  },
  
  uploadDocument: async (kbId: number, file: File) => {
    set({ isUploading: true })
    try {
      const doc = await knowledgeApi.uploadDocument(kbId, file)
      set((state) => ({
        documents: [doc, ...state.documents],
        totalDocuments: state.totalDocuments + 1,
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
      console.error('获取文档详情失败:', error)
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
  
  refreshDocumentStatus: async (kbId: number, docId: number) => {
    try {
      const status = await knowledgeApi.getDocumentStatus(kbId, docId)
      set((state) => ({
        documents: state.documents.map((d) =>
          d.id === docId
            ? { ...d, status: status.status as Document['status'], chunk_count: status.chunk_count }
            : d
        ),
      }))
      return status
    } catch (error) {
      console.error('获取文档状态失败:', error)
    }
  },
  
  // ========== 分片操作 ==========
  
  fetchChunks: async (kbId: number, docId: number) => {
    set({ isLoading: true })
    try {
      const { items, total } = await knowledgeApi.getChunks(kbId, docId)
      set({ chunks: items, totalChunks: total, isLoading: false })
    } catch (error) {
      console.error('获取分片列表失败:', error)
      set({ isLoading: false })
    }
  },
  
  // ========== 搜索操作 ==========
  
  search: async (query: string, knowledgeBaseIds?: number[]) => {
    set({ isSearching: true, searchQuery: query })
    try {
      const response = await knowledgeApi.search(query, knowledgeBaseIds)
      set({
        searchResults: response.results,
        searchTime: response.search_time_ms,
        isSearching: false,
      })
      return response
    } catch (error) {
      console.error('搜索失败:', error)
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
