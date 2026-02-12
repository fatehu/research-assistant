import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button } from 'antd'
import { ReloadOutlined, ExclamationCircleOutlined } from '@ant-design/icons'

interface ErrorBoundaryProps {
  /** 子组件 */
  children: ReactNode
  /** 自定义回退 UI；不传则使用内置默认 UI */
  fallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode)
  /** 错误上报回调 */
  onError?: (error: Error, errorInfo: ErrorInfo) => void
  /** 重置时回调（例如清理 store 状态） */
  onReset?: () => void
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
}

/**
 * ErrorBoundary - React 错误边界
 *
 * 用法 1: 包裹单个页面，防止一个页面崩溃导致整个应用白屏
 * ```tsx
 * <ErrorBoundary>
 *   <ChatPage />
 * </ErrorBoundary>
 * ```
 *
 * 用法 2: 自定义回退 UI
 * ```tsx
 * <ErrorBoundary fallback={<div>出错了</div>}>
 *   <SomeComponent />
 * </ErrorBoundary>
 * ```
 *
 * 用法 3: render prop 模式
 * ```tsx
 * <ErrorBoundary fallback={(error, reset) => (
 *   <div>
 *     <p>{error.message}</p>
 *     <button onClick={reset}>重试</button>
 *   </div>
 * )}>
 *   <SomeComponent />
 * </ErrorBoundary>
 * ```
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('[ErrorBoundary] 捕获到组件错误:', error, errorInfo)
    this.props.onError?.(error, errorInfo)
  }

  handleReset = (): void => {
    this.props.onReset?.()
    this.setState({ hasError: false, error: null })
  }

  render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children
    }

    const { fallback } = this.props
    const { error } = this.state

    // render prop 模式
    if (typeof fallback === 'function') {
      return fallback(error!, this.handleReset)
    }

    // 静态 ReactNode 回退
    if (fallback) {
      return fallback
    }

    // 内置默认 UI
    return (
      <div className="h-full flex items-center justify-center bg-slate-950">
        <div className="text-center max-w-md px-6">
          <div className="w-16 h-16 rounded-full bg-red-500/10 flex items-center justify-center mx-auto mb-4">
            <ExclamationCircleOutlined className="text-3xl text-red-400" />
          </div>
          <h2 className="text-xl font-semibold text-white mb-2">
            页面出现异常
          </h2>
          <p className="text-slate-400 mb-2 text-sm">
            该模块遇到了意外错误，不影响其他功能的正常使用。
          </p>
          {error && (
            <p className="text-xs text-red-400/60 font-mono mb-6 break-all">
              {error.message}
            </p>
          )}
          <div className="flex items-center justify-center gap-3">
            <Button
              type="primary"
              icon={<ReloadOutlined />}
              onClick={this.handleReset}
              className="rounded-lg"
            >
              重新加载
            </Button>
            <Button
              onClick={() => window.location.reload()}
              className="rounded-lg"
            >
              刷新页面
            </Button>
          </div>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
