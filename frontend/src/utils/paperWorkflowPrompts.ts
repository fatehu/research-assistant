import type { ChatSkillLaunchRequest } from '@/services/api'

export type PaperWorkflowStage = 'planning' | 'execution' | 'tuning'

type PaperWorkflowLaunchArgs = {
  paperId: number
  projectId?: number | null
  goal?: string | null
  preferredDraftId?: string | null
}

const PAPER_WORKFLOW_SKILL = 'paper-reproduction'

const stageMessageLabel = (stage: PaperWorkflowStage): string => {
  if (stage === 'planning') return '规划阶段'
  if (stage === 'tuning') return '调参与对比阶段'
  return '执行阶段'
}

export const buildPaperWorkflowSkillLaunch = (
  stage: PaperWorkflowStage,
  args: PaperWorkflowLaunchArgs,
): ChatSkillLaunchRequest => ({
  skill_name: PAPER_WORKFLOW_SKILL,
  stage,
  paper_id: args.paperId,
  ...(args.projectId ? { project_id: args.projectId } : {}),
  ...(args.goal ? { goal: args.goal } : {}),
  ...(args.preferredDraftId ? { preferred_draft_id: args.preferredDraftId } : {}),
})

export const buildPaperWorkflowInitialMessage = (
  stage: PaperWorkflowStage,
  args: PaperWorkflowLaunchArgs,
): string => {
  const label = stageMessageLabel(stage)
  return args.paperId
    ? `继续论文${label}（paper_id=${args.paperId}）`
    : `继续论文${label}`
}

export const buildPaperPlanningLaunch = (args: PaperWorkflowLaunchArgs) => ({
  initialMessage: buildPaperWorkflowInitialMessage('planning', args),
  skillLaunch: buildPaperWorkflowSkillLaunch('planning', args),
})

export const buildPaperExecutionLaunch = (args: PaperWorkflowLaunchArgs) => ({
  initialMessage: buildPaperWorkflowInitialMessage('execution', args),
  skillLaunch: buildPaperWorkflowSkillLaunch('execution', args),
})

export const buildPaperTuningLaunch = (args: PaperWorkflowLaunchArgs) => ({
  initialMessage: buildPaperWorkflowInitialMessage('tuning', args),
  skillLaunch: buildPaperWorkflowSkillLaunch('tuning', args),
})
