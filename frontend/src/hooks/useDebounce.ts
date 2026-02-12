import { useState, useEffect, useRef, useCallback } from 'react'

/**
 * useDebounce - 对值进行防抖处理
 * @param value 需要防抖的值
 * @param delay 延迟时间（毫秒），默认 300ms
 * @returns 防抖后的值
 */
export function useDebounce<T>(value: T, delay = 300): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value)

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedValue(value)
    }, delay)

    return () => clearTimeout(timer)
  }, [value, delay])

  return debouncedValue
}

/**
 * useDebouncedCallback - 对回调函数进行防抖处理
 * @param callback 需要防抖的回调函数
 * @param delay 延迟时间（毫秒），默认 300ms
 * @returns 防抖后的回调函数
 */
export function useDebouncedCallback<T extends (...args: any[]) => any>(
  callback: T,
  delay = 300
): T {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const callbackRef = useRef(callback)

  // 保持 callback 引用最新
  useEffect(() => {
    callbackRef.current = callback
  }, [callback])

  const debouncedFn = useCallback(
    (...args: Parameters<T>) => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
      }
      timerRef.current = setTimeout(() => {
        callbackRef.current(...args)
      }, delay)
    },
    [delay]
  ) as T

  // 清理
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
      }
    }
  }, [])

  return debouncedFn
}
