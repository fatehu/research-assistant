import { useEffect, useState } from 'react'

import type { ReaderExperienceBlockRef } from '@/services/api'

export type ExperienceUiEvent = {
  label: string
  eventName: string
  targetRef: string
} | null

export type ReaderExperienceUiActionRef = ReaderExperienceBlockRef['ui_actions'][number] | null

function getBlockUiAction(block: ReaderExperienceBlockRef | null | undefined, actionType: string): ReaderExperienceUiActionRef {
  return (block?.ui_actions || []).find((action) => String(action.action_type || '').trim() === actionType) || null
}

export function useExperienceActionBus(params: {
  paperId: number
  focusPage: number
  primaryFocusTargetId: string
}) {
  const { paperId, focusPage, primaryFocusTargetId } = params
  const [activeTargetId, setActiveTargetId] = useState('')
  const [lastUiEvent, setLastUiEvent] = useState<ExperienceUiEvent>(null)

  useEffect(() => {
    // 页面切换时重置本地 focus 状态；否则上一页选中的 target 可能在跳转后
    // 继续高亮无关 block。
    setActiveTargetId('')
    setLastUiEvent(null)
  }, [focusPage, paperId])

  const dispatchBlockAction = (
    block: ReaderExperienceBlockRef,
    actionType: string,
    targetRefOverride?: string,
  ) => {
    const action = getBlockUiAction(block, actionType)
    const targetRef = String(targetRefOverride || action?.target_ref || block.target_ids?.[0] || '').trim()
    if (actionType === 'focus_target' && targetRef) {
      setActiveTargetId(targetRef)
    }
    if (actionType === 'return_to_reader') {
      // 返回时使用后端提供的 primary focus target，让手写 block 和生成组件
      // 落到同一个阅读锚点。
      setActiveTargetId(primaryFocusTargetId || '')
    }
    setLastUiEvent({
      label: String(action?.label || actionType).trim(),
      eventName: String(action?.event_name || `block.${actionType}`).trim(),
      targetRef,
    })
  }

  return {
    activeTargetId,
    lastUiEvent,
    dispatchBlockAction,
    getBlockUiAction,
  }
}
