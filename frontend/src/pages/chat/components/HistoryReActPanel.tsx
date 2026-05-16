import { useMemo } from 'react'
import type { ToolWorkflowSummary } from '@/services/api'
import TurnProcessLanes, { type TurnLaneStep } from './TurnProcessLanes'

interface HistoryStep {
  type: string
  iteration: number
  content?: string
  tool?: string
  toolCallId?: string
  input?: Record<string, unknown>
  output?: string
  success?: boolean
  workflowSummary?: ToolWorkflowSummary
  rawContent?: string
}

interface HistoryReActPanelProps {
  steps: HistoryStep[]
  defaultExpanded?: boolean
  embedded?: boolean
}

const HistoryReActPanel = ({
  steps,
  defaultExpanded = true,
  embedded = false,
}: HistoryReActPanelProps) => {
  const laneSteps = useMemo<TurnLaneStep[]>(
    () =>
      (Array.isArray(steps) ? steps : [])
        .filter((step): step is HistoryStep & { type: TurnLaneStep['type'] } =>
          step.type === 'workflow' ||
          step.type === 'thought' ||
          step.type === 'action' ||
          step.type === 'observation',
        )
        .map((step) => ({
          type: step.type,
          iteration: step.iteration || 0,
          content: step.content,
          tool: step.tool,
          toolCallId: step.toolCallId,
          input: step.input,
          output: step.output,
          success: step.success,
          workflowSummary: step.workflowSummary,
          rawContent: step.rawContent,
        })),
    [steps],
  )

  if (!laneSteps.length) {
    return null
  }

  return (
    <TurnProcessLanes
      steps={laneSteps}
      title="历史轨道"
      subtitle="这一回合的过程文本和工具调用都集中在这里，正文只保留最终结论。"
      statusLabel="已归档"
      defaultExpanded={defaultExpanded}
      embedded={embedded}
    />
  )
}

export default HistoryReActPanel
