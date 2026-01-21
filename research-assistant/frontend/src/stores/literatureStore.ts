import { create } from 'zustand'
import { 
  literatureApi, 
  Paper, 
  PaperSearchResult, 
  PaperCollection, 
  SearchHistory 
} from '@/services/api'

interface LiteratureState {
  // 论文列表
  papers: Paper[]
  papersLoading: boolean
  
  // 搜索结果
  searchResults: PaperSearchResult[]
  searchQuery: string
  searchSource: string
  searchTotal: number
  searchOffset: number
  searchLoading: boolean
  searchLoadingMore: boolean
  searchHasMore: boolean
  
  // 收藏夹
  collections: PaperCollection[]
  selectedCollectionId: number | null
  collectionsLoading: boolean
  
  // 当前选中的论文
  selectedPaper: Paper | null
  selectedPaperLoading: boolean
  
  // 搜索历史
  searchHistory: SearchHistory[]
  
  // 视图状态
  viewMode: 'list' | 'card'
  detailPanelOpen: boolean
  
  // Actions
  init: () => Promise<void>
  
  // 搜索
  searchPapers: (query: string, source?: string, options?: {
    limit?: number
    offset?: number
    year_start?: number
    year_end?: number
  }) => Promise<void>
  loadMoreSearchResults: () => Promise<void>
  clearSearch: () => void
  loadSearchHistory: () => Promise<void>
  
  // 论文管理
  loadPapers: (options?: {
    collection_id?: number
    is_read?: boolean
    search?: string
  }) => Promise<void>
  savePaper: (paper: PaperSearchResult, collectionIds?: number[]) => Promise<Paper>
  updatePaper: (paperId: number, data: Partial<Paper>) => Promise<void>
  deletePaper: (paperId: number) => Promise<void>
  selectPaper: (paper: Paper | null) => void
  loadPaperDetail: (paperId: number) => Promise<void>
  
  // 收藏夹
  loadCollections: () => Promise<void>
  createCollection: (data: { name: string; description?: string; color?: string }) => Promise<PaperCollection>
  updateCollection: (collectionId: number, data: Partial<PaperCollection>) => Promise<void>
  deleteCollection: (collectionId: number) => Promise<void>
  selectCollection: (collectionId: number | null) => void
  addToCollection: (paperId: number, collectionIds: number[]) => Promise<void>
  removeFromCollection: (paperId: number, collectionId: number) => Promise<void>
  
  // PDF
  downloadPdf: (paperId: number, knowledgeBaseId?: number) => Promise<void>
  
  // 视图
  setViewMode: (mode: 'list' | 'card') => void
  toggleDetailPanel: (open?: boolean) => void
}

export const useLiteratureStore = create<LiteratureState>((set, get) => ({
  // Initial state
  papers: [],
  papersLoading: false,
  searchResults: [],
  searchQuery: '',
  searchSource: 'semantic_scholar',
  searchTotal: 0,
  searchOffset: 0,
  searchLoading: false,
  searchLoadingMore: false,
  searchHasMore: false,
  collections: [],
  selectedCollectionId: null,
  collectionsLoading: false,
  selectedPaper: null,
  selectedPaperLoading: false,
  searchHistory: [],
  viewMode: 'card',
  detailPanelOpen: false,

  // 初始化
  init: async () => {
    try {
      await literatureApi.init()
      await get().loadCollections()
      await get().loadPapers()
    } catch (error) {
      console.error('Failed to init literature:', error)
    }
  },

  // 搜索论文
  searchPapers: async (query, source = 'semantic_scholar', options = {}) => {
    const limit = options.limit || 20
    set({ searchLoading: true, searchQuery: query, searchSource: source, searchOffset: 0 })
    try {
      const response = await literatureApi.searchPapers({
        query,
        source,
        limit,
        offset: 0,
        year_start: options.year_start,
        year_end: options.year_end,
      })
      const newOffset = response.papers.length
      // 只要还有更多数据且本次返回了数据就允许继续加载
      const hasMore = newOffset < response.total && response.papers.length > 0
      set({
        searchResults: response.papers,
        searchTotal: response.total,
        searchOffset: newOffset,
        searchHasMore: hasMore,
        searchLoading: false,
      })
    } catch (error) {
      console.error('Search failed:', error)
      set({ searchLoading: false })
      throw error
    }
  },

  // 加载更多搜索结果
  loadMoreSearchResults: async () => {
    const { searchQuery, searchSource, searchOffset, searchTotal, searchLoadingMore, searchHasMore } = get()
    if (searchLoadingMore || !searchHasMore) return
    
    const limit = 20
    set({ searchLoadingMore: true })
    try {
      const response = await literatureApi.searchPapers({
        query: searchQuery,
        source: searchSource,
        limit,
        offset: searchOffset,
      })
      const newOffset = searchOffset + response.papers.length
      // 如果返回0条数据或已达到total，则没有更多
      const hasMore = response.papers.length > 0 && newOffset < searchTotal
      set(state => ({
        searchResults: [...state.searchResults, ...response.papers],
        searchOffset: newOffset,
        searchHasMore: hasMore,
        searchLoadingMore: false,
      }))
    } catch (error) {
      console.error('Load more failed:', error)
      set({ searchLoadingMore: false })
    }
  },

  clearSearch: () => {
    set({ searchResults: [], searchQuery: '', searchTotal: 0, searchOffset: 0, searchHasMore: false })
  },

  loadSearchHistory: async () => {
    try {
      const history = await literatureApi.getSearchHistory(20)
      set({ searchHistory: history })
    } catch (error) {
      console.error('Failed to load search history:', error)
    }
  },

  // 加载论文列表
  loadPapers: async (options = {}) => {
    set({ papersLoading: true })
    try {
      const papers = await literatureApi.getPapers({
        collection_id: options.collection_id || get().selectedCollectionId || undefined,
        is_read: options.is_read,
        search: options.search,
        limit: 100,
      })
      set({ papers, papersLoading: false })
    } catch (error) {
      console.error('Failed to load papers:', error)
      set({ papersLoading: false })
    }
  },

  // 保存论文
  savePaper: async (paper, collectionIds = []) => {
    try {
      const saved = await literatureApi.savePaper({
        source: paper.source,
        external_id: paper.external_id,
        title: paper.title,
        abstract: paper.abstract,
        authors: paper.authors,
        year: paper.year,
        venue: paper.venue,
        citation_count: paper.citation_count,
        reference_count: paper.reference_count,
        url: paper.url,
        pdf_url: paper.pdf_url,
        arxiv_id: paper.arxiv_id,
        doi: paper.doi,
        fields_of_study: paper.fields_of_study,
        collection_ids: collectionIds,
      })
      
      // 更新搜索结果中的状态
      set(state => ({
        searchResults: state.searchResults.map(p =>
          p.external_id === paper.external_id
            ? { ...p, is_saved: true, saved_paper_id: saved.id }
            : p
        ),
        papers: [...state.papers, saved],
      }))
      
      // 更新收藏夹计数
      await get().loadCollections()
      
      return saved
    } catch (error) {
      console.error('Failed to save paper:', error)
      throw error
    }
  },

  updatePaper: async (paperId, data) => {
    try {
      const updated = await literatureApi.updatePaper(paperId, data)
      set(state => ({
        papers: state.papers.map(p => p.id === paperId ? updated : p),
        selectedPaper: state.selectedPaper?.id === paperId ? updated : state.selectedPaper,
      }))
      
      // 如果更新了阅读状态或评分，刷新收藏夹计数和论文列表
      if ('is_read' in data || 'rating' in data) {
        await get().loadCollections()
        // 如果当前在特定收藏夹视图，也刷新论文列表
        const collectionId = get().selectedCollectionId
        if (collectionId) {
          await get().loadPapers({ collection_id: collectionId })
        }
      }
    } catch (error) {
      console.error('Failed to update paper:', error)
      throw error
    }
  },

  deletePaper: async (paperId) => {
    try {
      await literatureApi.deletePaper(paperId)
      set(state => ({
        papers: state.papers.filter(p => p.id !== paperId),
        selectedPaper: state.selectedPaper?.id === paperId ? null : state.selectedPaper,
        detailPanelOpen: state.selectedPaper?.id === paperId ? false : state.detailPanelOpen,
      }))
      await get().loadCollections()
    } catch (error) {
      console.error('Failed to delete paper:', error)
      throw error
    }
  },

  selectPaper: (paper) => {
    set({ selectedPaper: paper, detailPanelOpen: paper !== null })
  },

  loadPaperDetail: async (paperId) => {
    set({ selectedPaperLoading: true })
    try {
      const paper = await literatureApi.getPaper(paperId)
      set({ selectedPaper: paper, selectedPaperLoading: false, detailPanelOpen: true })
    } catch (error) {
      console.error('Failed to load paper detail:', error)
      set({ selectedPaperLoading: false })
    }
  },

  // 收藏夹管理
  loadCollections: async () => {
    set({ collectionsLoading: true })
    try {
      const collections = await literatureApi.getCollections()
      set({ collections, collectionsLoading: false })
    } catch (error) {
      console.error('Failed to load collections:', error)
      set({ collectionsLoading: false })
    }
  },

  createCollection: async (data) => {
    try {
      const collection = await literatureApi.createCollection(data)
      set(state => ({ collections: [...state.collections, collection] }))
      return collection
    } catch (error) {
      console.error('Failed to create collection:', error)
      throw error
    }
  },

  updateCollection: async (collectionId, data) => {
    try {
      const updated = await literatureApi.updateCollection(collectionId, data)
      set(state => ({
        collections: state.collections.map(c => c.id === collectionId ? updated : c),
      }))
    } catch (error) {
      console.error('Failed to update collection:', error)
      throw error
    }
  },

  deleteCollection: async (collectionId) => {
    try {
      await literatureApi.deleteCollection(collectionId)
      set(state => ({
        collections: state.collections.filter(c => c.id !== collectionId),
        selectedCollectionId: state.selectedCollectionId === collectionId ? null : state.selectedCollectionId,
      }))
    } catch (error) {
      console.error('Failed to delete collection:', error)
      throw error
    }
  },

  selectCollection: (collectionId) => {
    set({ selectedCollectionId: collectionId })
    get().loadPapers({ collection_id: collectionId || undefined })
  },

  addToCollection: async (paperId, collectionIds) => {
    try {
      await literatureApi.addPaperToCollection(paperId, collectionIds)
      await get().loadCollections()
      if (get().selectedPaper?.id === paperId) {
        await get().loadPaperDetail(paperId)
      }
    } catch (error) {
      console.error('Failed to add to collection:', error)
      throw error
    }
  },

  removeFromCollection: async (paperId, collectionId) => {
    try {
      await literatureApi.removePaperFromCollection(paperId, collectionId)
      await get().loadCollections()
      // 如果当前在该收藏夹视图，重新加载
      if (get().selectedCollectionId === collectionId) {
        await get().loadPapers()
      }
      if (get().selectedPaper?.id === paperId) {
        await get().loadPaperDetail(paperId)
      }
    } catch (error) {
      console.error('Failed to remove from collection:', error)
      throw error
    }
  },

  // PDF 下载
  downloadPdf: async (paperId, knowledgeBaseId) => {
    try {
      await literatureApi.downloadPdf(paperId, knowledgeBaseId)
      // 重新加载论文以更新状态
      if (get().selectedPaper?.id === paperId) {
        await get().loadPaperDetail(paperId)
      }
      await get().loadPapers()
    } catch (error) {
      console.error('Failed to download PDF:', error)
      throw error
    }
  },

  // 视图
  setViewMode: (mode) => {
    set({ viewMode: mode })
  },

  toggleDetailPanel: (open) => {
    set(state => ({ detailPanelOpen: open ?? !state.detailPanelOpen }))
  },
}))
