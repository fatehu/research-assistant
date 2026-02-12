import { useState, useRef, useCallback } from 'react'

/**
 * useStreamResponse - 流式响应状态管理 hook
 *
 * 封装了流式 SSE 响应常见的状态管理逻辑：
 * - 累积内容
 * - 加载/思考状态切换
 * - AbortController 取消控制
 * - 内容重置
 *
 * 注意：此 hook 管理的是 UI 层的临时展示状态，
 * 不涉及消息持久化（持久化仍由各页面的 store 负责）。
 */
export interface StreamResponseState {
  /** 流式累积的内容文本 */
  streamingContent: string
  /** 流式累积的思考文本 */
  streamingThought: string
  /** 是否正在生成中 */
  isStreaming: boolean
  /** 是否处于"思考"阶段 */
  isThinking: boolean
}

export interface StreamResponseActions {
  /** 开始新的流式会话，返回 AbortController */
  startStream: () => AbortController
  /** 追加内容文本 */
  appendContent: (text: string) => void
  /** 追加思考文本 */
  appendThought: (text: string) => void
  /** 设置思考状态 */
  setThinking: (thinking: boolean) => void
  /** 停止当前流式会话 */
  stopStream: () => void
  /** 重置全部状态 */
  reset: () => void
  /** 获取当前的 AbortController（如果正在流式中） */
  getAbortController: () => AbortController | null
}

export function useStreamResponse(): [StreamResponseState, StreamResponseActions] {
  const [streamingContent, setStreamingContent] = useState('')
  const [streamingThought, setStreamingThought] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [isThinking, setIsThinking] = useState(false)
  const abortControllerRef = useRef<AbortController | null>(null)

  const startStream = useCallback(() => {
    // 如果上一个还在跑，先中止
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
    }
    const controller = new AbortController()
    abortControllerRef.current = controller
    setStreamingContent('')
    setStreamingThought('')
    setIsStreaming(true)
    setIsThinking(false)
    return controller
  }, [])

  const appendContent = useCallback((text: string) => {
    setStreamingContent((prev) => prev + text)
  }, [])

  const appendThought = useCallback((text: string) => {
    setStreamingThought((prev) => prev + text)
  }, [])

  const setThinkingState = useCallback((thinking: boolean) => {
    setIsThinking(thinking)
  }, [])

  const stopStream = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    setIsStreaming(false)
    setIsThinking(false)
  }, [])

  const reset = useCallback(() => {
    stopStream()
    setStreamingContent('')
    setStreamingThought('')
  }, [stopStream])

  const getAbortController = useCallback(() => {
    return abortControllerRef.current
  }, [])

  return [
    { streamingContent, streamingThought, isStreaming, isThinking },
    {
      startStream,
      appendContent,
      appendThought,
      setThinking: setThinkingState,
      stopStream,
      reset,
      getAbortController,
    },
  ]
}
