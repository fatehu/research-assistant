import { App as AntdApp, message } from 'antd'
import { useEffect } from 'react'

const MESSAGE_METHODS = ['open', 'success', 'error', 'info', 'warning', 'loading', 'destroy'] as const

export default function AntdMessageBridge() {
  const { message: appMessage } = AntdApp.useApp()

  useEffect(() => {
    const target = message as unknown as Record<string, (...args: unknown[]) => unknown>
    const source = appMessage as unknown as Record<string, (...args: unknown[]) => unknown>

    for (const method of MESSAGE_METHODS) {
      target[method] = (...args: unknown[]) => source[method](...args)
    }
  }, [appMessage])

  return null
}
