import { useEffect, useState } from 'react'

import {
  literatureApi,
  type RetrievalRuntimeComponentStatus,
  type RetrievalRuntimeStatusResponse,
} from '@/services/api'

function isTerminal(status: string | null | undefined): boolean {
  return ['ready', 'degraded', 'disabled', 'skipped'].includes(String(status || '').trim())
}

function nextPollDelay(status: RetrievalRuntimeStatusResponse | null): number | null {
  if (!status) return 15000
  if (status.status === 'unavailable') return 30000
  if (status.background_task_running) return 15000
  if (!isTerminal(status.status)) return 15000
  return null
}

function formatComponentLabel(component: string): string {
  if (component === 'embedding') return 'Embedding'
  if (component === 'reranker') return 'Reranker'
  return component
}

function formatStatusLabel(status: string): string {
  const normalized = String(status || '').trim()
  if (normalized === 'warming') return '预热中'
  if (normalized === 'queued') return '排队中'
  if (normalized === 'warmed' || normalized === 'ready') return '就绪'
  if (normalized === 'disabled') return '已关闭'
  if (normalized === 'skipped') return '已跳过'
  if (normalized === 'timeout') return '超时'
  if (normalized === 'failed') return '失败'
  if (normalized === 'idle') return '未启动'
  if (normalized === 'unavailable') return '重试中'
  return normalized || '未知'
}

function statusTone(status: string): string {
  const normalized = String(status || '').trim()
  if (normalized === 'warmed' || normalized === 'ready') {
    return 'border-emerald-400/24 bg-emerald-500/10 text-emerald-100'
  }
  if (normalized === 'warming' || normalized === 'queued') {
    return 'border-cyan-400/22 bg-cyan-500/10 text-cyan-100'
  }
  if (normalized === 'failed' || normalized === 'timeout' || normalized === 'degraded') {
    return 'border-amber-400/24 bg-amber-500/10 text-amber-100'
  }
  return 'border-white/[0.08] bg-white/[0.04] text-slate-200'
}

function overviewCopy(status: RetrievalRuntimeStatusResponse | null): string {
  if (!status) return '检索运行时状态暂不可用，正在重试。'
  if (!status.enabled) return '检索模型预热已关闭，首轮检索会按需加载。'
  if (status.status === 'unavailable') return '检索运行时状态暂不可用，正在重试。'
  if (status.status === 'warming') return '检索模型正在后台预热，首轮知识检索可能略慢。'
  if (status.status === 'degraded') return '检索模型预热未完全成功，首轮知识检索可能触发补加载。'
  if (status.status === 'ready') return '检索模型已就绪。'
  if (status.status === 'skipped') return '检索模型未执行预热。'
  return '检索运行时状态已更新。'
}

export default function RetrievalRuntimeStatusStrip() {
  const [status, setStatus] = useState<RetrievalRuntimeStatusResponse | null>(null)

  useEffect(() => {
    let cancelled = false
    let timerId: number | null = null
    let inFlight = false

    const scheduleNext = (delayMs: number | null) => {
      if (cancelled || delayMs == null) return
      if (timerId != null) {
        window.clearTimeout(timerId)
      }
      timerId = window.setTimeout(() => {
        void poll()
      }, delayMs)
    }

    const poll = async (force = false) => {
      if (cancelled || inFlight) return
      if (!force && typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        return
      }
      inFlight = true
      try {
        const nextStatus = await literatureApi.getRetrievalRuntimeStatus()
        if (cancelled) return
        setStatus(nextStatus)
        scheduleNext(nextPollDelay(nextStatus))
      } catch {
        if (cancelled) return
        const fallbackStatus = {
          enabled: true,
          status: 'unavailable',
          timeout_seconds: 0,
          duration_ms: 0,
          started_at: null,
          completed_at: null,
          background_task_running: false,
          components: [],
        } satisfies RetrievalRuntimeStatusResponse
        setStatus((previous) => previous || fallbackStatus)
        scheduleNext(nextPollDelay(fallbackStatus))
      } finally {
        inFlight = false
      }
    }

    const handleVisibilityChange = () => {
      if (!cancelled && document.visibilityState === 'visible') {
        void poll(true)
      }
    }

    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', handleVisibilityChange)
    }

    void poll(true)

    return () => {
      cancelled = true
      if (timerId != null) {
        window.clearTimeout(timerId)
      }
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', handleVisibilityChange)
      }
    }
  }, [])

  const components: RetrievalRuntimeComponentStatus[] = status?.components || []
  const overallTone = status?.status === 'ready'
    ? 'bg-emerald-400'
    : status?.status === 'degraded'
      ? 'bg-amber-400'
      : 'bg-cyan-400'

  return (
    <div className="mb-6 flex flex-wrap items-center gap-3 rounded-[24px] border border-white/[0.08] bg-slate-950/68 px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_18px_36px_rgba(2,6,23,0.26)] backdrop-blur-xl">
      <div className="flex min-w-[240px] flex-1 items-center gap-3">
        <span className={`h-2.5 w-2.5 rounded-full shadow-[0_0_16px_currentColor] ${overallTone}`} />
        <div className="min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-slate-500">
            retrieval runtime
          </div>
          <div className="text-sm text-slate-200">
            {overviewCopy(status)}
          </div>
        </div>
      </div>

      {components.map((component) => (
        <div
          key={component.component}
          className={`inline-flex min-h-8 items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${statusTone(component.status)}`}
          title={component.detail || undefined}
        >
          <span className="font-mono uppercase tracking-[0.16em] text-[10px] opacity-80">
            {formatComponentLabel(component.component)}
          </span>
          <span>{formatStatusLabel(component.status)}</span>
        </div>
      ))}
    </div>
  )
}
