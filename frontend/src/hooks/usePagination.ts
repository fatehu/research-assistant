import { useState, useCallback, useMemo } from 'react'

export interface PaginationOptions {
  /** 每页数量，默认 20 */
  pageSize?: number
  /** 初始页码，默认 1 */
  initialPage?: number
}

export interface PaginationResult<T> {
  /** 当前页数据 */
  currentData: T[]
  /** 当前页码 */
  currentPage: number
  /** 总页数 */
  totalPages: number
  /** 总条数 */
  total: number
  /** 是否有下一页 */
  hasNextPage: boolean
  /** 是否有上一页 */
  hasPrevPage: boolean
  /** 跳转到指定页 */
  goToPage: (page: number) => void
  /** 下一页 */
  nextPage: () => void
  /** 上一页 */
  prevPage: () => void
  /** 重置到第一页 */
  reset: () => void
  /** 每页条数 */
  pageSize: number
}

/**
 * usePagination - 客户端分页 hook
 * 适合数据量不大、已全部加载到前端的场景
 *
 * @param data 完整数据数组
 * @param options 分页选项
 */
export function usePagination<T>(
  data: T[],
  options: PaginationOptions = {}
): PaginationResult<T> {
  const { pageSize = 20, initialPage = 1 } = options
  const [currentPage, setCurrentPage] = useState(initialPage)

  const total = data.length
  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  // 确保页码在有效范围内
  const safePage = Math.max(1, Math.min(currentPage, totalPages))

  const currentData = useMemo(() => {
    const start = (safePage - 1) * pageSize
    return data.slice(start, start + pageSize)
  }, [data, safePage, pageSize])

  const goToPage = useCallback(
    (page: number) => {
      setCurrentPage(Math.max(1, Math.min(page, totalPages)))
    },
    [totalPages]
  )

  const nextPage = useCallback(() => {
    setCurrentPage((prev) => Math.min(prev + 1, totalPages))
  }, [totalPages])

  const prevPage = useCallback(() => {
    setCurrentPage((prev) => Math.max(prev - 1, 1))
  }, [])

  const reset = useCallback(() => {
    setCurrentPage(1)
  }, [])

  return {
    currentData,
    currentPage: safePage,
    totalPages,
    total,
    hasNextPage: safePage < totalPages,
    hasPrevPage: safePage > 1,
    goToPage,
    nextPage,
    prevPage,
    reset,
    pageSize,
  }
}

/**
 * useLoadMorePagination - 加载更多模式的分页 hook
 * 适合"点击加载更多"或"无限滚动"的场景
 */
export interface LoadMoreOptions {
  /** 每次加载数量，默认 20 */
  pageSize?: number
}

export interface LoadMoreResult {
  /** 当前偏移量 */
  offset: number
  /** 每页大小 */
  pageSize: number
  /** 是否还有更多 */
  hasMore: boolean
  /** 是否正在加载 */
  isLoading: boolean
  /** 加载更多 */
  loadMore: () => Promise<void>
  /** 重置 */
  reset: () => void
}

export function useLoadMorePagination(
  fetchFn: (offset: number, limit: number) => Promise<{ total: number; data: any[] }>,
  options: LoadMoreOptions = {}
): LoadMoreResult {
  const { pageSize = 20 } = options
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(false)

  const hasMore = offset < total

  const loadMore = useCallback(async () => {
    if (isLoading || !hasMore) return
    setIsLoading(true)
    try {
      const result = await fetchFn(offset, pageSize)
      setTotal(result.total)
      setOffset((prev) => prev + result.data.length)
    } finally {
      setIsLoading(false)
    }
  }, [offset, pageSize, isLoading, hasMore, fetchFn])

  const reset = useCallback(() => {
    setOffset(0)
    setTotal(0)
  }, [])

  return { offset, pageSize, hasMore, isLoading, loadMore, reset }
}
