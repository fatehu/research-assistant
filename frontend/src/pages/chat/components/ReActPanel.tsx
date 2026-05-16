import { useMemo } from 'react'
import type { IterationStep } from '@/stores/chatStore'
import TurnProcessLanes, { type TurnLaneStep } from './TurnProcessLanes'

interface ReActPanelProps {
  steps: IterationStep[]
  currentIteration: number
  isThinking: boolean
  currentThought: string
  currentToolCall: { tool: string; input: Record<string, any>; output?: string } | null
}

const ReActPanel = ({
  steps,
  currentIteration,
  isThinking,
  currentThought,
  currentToolCall,
}: ReActPanelProps) => {
  const laneSteps = useMemo<TurnLaneStep[]>(() => {
    let inferredIteration = 1
    const normalized: TurnLaneStep[] = steps.map((step, index) => {
      const mapped: TurnLaneStep = {
        type: step.type,
        iteration: inferredIteration,
        content: step.content,
        tool: step.tool,
        input: step.toolInput,
        output: step.toolOutput,
        success: step.success,
      }
      if (step.type === 'observation' && index < steps.length - 1) {
        inferredIteration += 1
      }
      return mapped
    })

    if (isThinking && currentThought.trim()) {
      const lastThought = [...normalized].reverse().find((item) => item.type === 'thought')
      if (!lastThought || String(lastThought.content || '').trim() !== currentThought.trim()) {
        normalized.push({
          type: 'thought',
          iteration: Math.max(currentIteration, 1),
          content: currentThought.trim(),
        })
      }
    }

    if (currentToolCall) {
      const pendingActionIndex = [...normalized]
        .map((item, index) => ({ item, index }))
        .reverse()
        .find(({ item }) => item.type === 'action' && item.tool === currentToolCall.tool)?.index

      if (typeof pendingActionIndex === 'number') {
        const existing = normalized[pendingActionIndex]
        normalized[pendingActionIndex] = {
          ...existing,
          output: currentToolCall.output,
        }
      } else {
        normalized.push({
          type: 'action',
          iteration: Math.max(currentIteration, 1),
          tool: currentToolCall.tool,
          input: currentToolCall.input,
          output: currentToolCall.output,
          content: `调用工具: ${currentToolCall.tool}`,
        })
      }
    }

    return normalized
  }, [steps, currentIteration, isThinking, currentThought, currentToolCall])

  if (!laneSteps.length && !isThinking && !currentToolCall) {
    return null
  }

  return (
    <TurnProcessLanes
      steps={laneSteps}
      title="当前轨道"
      subtitle="只有最终 answer 会进入正文，过程与工具统一收在这条回合轨道里。"
      statusLabel={isThinking || currentToolCall ? '运行中' : '已收束'}
      active={isThinking || Boolean(currentToolCall)}
      defaultExpanded
    />
  )
}

export default ReActPanel
