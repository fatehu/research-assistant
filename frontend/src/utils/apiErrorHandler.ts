import { message } from 'antd'
import type { AxiosError } from 'axios'

/**
 * API 错误类型枚举
 */
export enum ApiErrorType {
  /** 网络错误 / 无法连接 */
  Network = 'NETWORK',
  /** 401 未认证 */
  Unauthorized = 'UNAUTHORIZED',
  /** 403 无权限 */
  Forbidden = 'FORBIDDEN',
  /** 404 资源不存在 */
  NotFound = 'NOT_FOUND',
  /** 422 参数校验失败 */
  Validation = 'VALIDATION',
  /** 429 请求过多 */
  RateLimited = 'RATE_LIMITED',
  /** 500+ 服务端错误 */
  Server = 'SERVER',
  /** 请求超时 */
  Timeout = 'TIMEOUT',
  /** 请求被取消 */
  Cancelled = 'CANCELLED',
  /** 其他未知错误 */
  Unknown = 'UNKNOWN',
  /** 明确的业务冲突 */
  Conflict = 'CONFLICT',
}

interface ApiErrorDetailObject {
  code?: string
  message?: string
  details?: unknown
  request_id?: string
}

export interface ParsedApiError {
  /** 错误类型 */
  type: ApiErrorType
  /** 用户友好的中文消息 */
  message: string
  /** HTTP 状态码（如果有） */
  status?: number
  /** 服务端返回的 detail（如果有） */
  detail?: string
  /** 服务端业务错误码（如果有） */
  code?: string
  /** 原始错误对象 */
  raw: unknown
}

function isDetailObject(value: unknown): value is ApiErrorDetailObject {
  return typeof value === 'object' && value !== null
}

function normalizeDetailPayload(data: any): {
  detailText?: string
  detailCode?: string
  detailDetails?: unknown
} {
  const rawDetail = data?.detail
  if (typeof rawDetail === 'string') {
    return { detailText: rawDetail }
  }
  if (isDetailObject(rawDetail)) {
    return {
      detailText: typeof rawDetail.message === 'string' ? rawDetail.message : undefined,
      detailCode: typeof rawDetail.code === 'string' ? rawDetail.code : undefined,
      detailDetails: rawDetail.details,
    }
  }
  if (typeof data?.message === 'string') {
    return { detailText: data.message }
  }
  if (typeof data === 'string') {
    return { detailText: data }
  }
  return {}
}

function formatDuplicateUploadMessage(detailDetails: unknown, fallback?: string): string {
  if (typeof detailDetails !== 'object' || detailDetails === null) {
    return fallback || '当前知识库中已存在相同文件'
  }
  const details = detailDetails as Record<string, unknown>
  const duplicateFilename = typeof details.duplicate_filename === 'string' ? details.duplicate_filename.trim() : ''
  const duplicateDocId = Number(details.duplicate_of_document_id || 0)
  const parts: string[] = ['当前知识库中已存在相同文件']
  if (duplicateFilename) {
    parts.push(`已存在文件：${duplicateFilename}`)
  }
  if (Number.isFinite(duplicateDocId) && duplicateDocId > 0) {
    parts.push(`文档 ID：${duplicateDocId}`)
  }
  return parts.join('，')
}

/**
 * 解析 API 错误，返回结构化的错误信息
 */
export function parseApiError(error: unknown): ParsedApiError {
  // Axios 错误
  if (isAxiosError(error)) {
    const status = error.response?.status
    const { detailText, detailCode, detailDetails } = normalizeDetailPayload(error.response?.data)
    const detail = detailText

    // 请求被取消
    if (error.code === 'ERR_CANCELED') {
      return { type: ApiErrorType.Cancelled, message: '请求已取消', raw: error }
    }

    // 超时
    if (error.code === 'ECONNABORTED') {
      return { type: ApiErrorType.Timeout, message: '请求超时，请稍后重试', raw: error }
    }

    // 无响应 - 网络错误
    if (!error.response) {
      return { type: ApiErrorType.Network, message: '网络连接失败，请检查网络后重试', raw: error }
    }

    if (status === 409 && detailCode === 'duplicate_file_upload') {
      return {
        type: ApiErrorType.Conflict,
        message: formatDuplicateUploadMessage(detailDetails, detail || '当前知识库中已存在相同文件'),
        status,
        detail,
        code: detailCode,
        raw: error,
      }
    }

    switch (status) {
      case 401:
        return { type: ApiErrorType.Unauthorized, message: '登录已过期，请重新登录', status, detail, code: detailCode, raw: error }
      case 403:
        return { type: ApiErrorType.Forbidden, message: '没有权限执行此操作', status, detail, code: detailCode, raw: error }
      case 404:
        return { type: ApiErrorType.NotFound, message: detail || '请求的资源不存在', status, detail, code: detailCode, raw: error }
      case 422:
        return { type: ApiErrorType.Validation, message: detail || '参数校验失败', status, detail, code: detailCode, raw: error }
      case 429:
        return { type: ApiErrorType.RateLimited, message: '请求过于频繁，请稍后再试', status, detail, code: detailCode, raw: error }
      default:
        if (status && status >= 500) {
          return {
            type: ApiErrorType.Server,
            message: detail || '服务器内部错误，请稍后重试',
            status,
            detail,
            code: detailCode,
            raw: error,
          }
        }
        return {
          type: ApiErrorType.Unknown,
          message: detail || `请求失败 (${status})`,
          status,
          detail,
          code: detailCode,
          raw: error,
        }
    }
  }

  // AbortError（fetch 取消）
  if (error instanceof Error && error.name === 'AbortError') {
    return { type: ApiErrorType.Cancelled, message: '请求已取消', raw: error }
  }

  // 普通 Error
  if (error instanceof Error) {
    return { type: ApiErrorType.Unknown, message: error.message || '操作失败', raw: error }
  }

  // 字符串
  if (typeof error === 'string') {
    return { type: ApiErrorType.Unknown, message: error, raw: error }
  }

  return { type: ApiErrorType.Unknown, message: '发生未知错误', raw: error }
}

/**
 * 处理 API 错误并显示消息提示
 *
 * @param error 原始错误
 * @param context 可选的上下文描述，用于日志
 * @returns 解析后的错误信息
 *
 * @example
 * ```ts
 * try {
 *   await api.deleteDocument(id)
 * } catch (error) {
 *   handleApiError(error, '删除文档')
 * }
 * ```
 */
export function handleApiError(error: unknown, context?: string): ParsedApiError {
  const parsed = parseApiError(error)

  // 取消的请求不需要提示
  if (parsed.type === ApiErrorType.Cancelled) {
    return parsed
  }

  // 401 由全局 axios 拦截器统一处理（跳转登录），这里不再重复弹窗
  if (parsed.type === ApiErrorType.Unauthorized) {
    return parsed
  }

  // 在控制台输出完整错误
  const prefix = context ? `[${context}]` : '[API]'
  console.error(`${prefix} ${parsed.type}:`, parsed.message, parsed.raw)

  // 弹出用户提示
  if (parsed.type === ApiErrorType.Conflict) {
    message.warning(parsed.message)
  } else {
    message.error(parsed.message)
  }

  return parsed
}

// 类型守卫
function isAxiosError(error: unknown): error is AxiosError<{ detail?: string; message?: string }> {
  return (
    typeof error === 'object' &&
    error !== null &&
    'isAxiosError' in error &&
    (error as any).isAxiosError === true
  )
}
