import type {
  ReaderExperiencePlan,
  ReaderGenerativePlan,
  ReaderStoryClaim,
} from '@/services/api'

type FallbackQuestionAnswer = {
  question: string
  answer: string
}

export type ReaderExperiencePrimitives = {
  visibleClaims: ReaderStoryClaim[]
  terms: string[]
  backgroundTopics: string[]
  hooks: string[]
  fallbackQuestionAnswers: FallbackQuestionAnswer[]
}

type BuildReaderExperiencePrimitivesInput = {
  experiencePlan: ReaderExperiencePlan | null | undefined
  generativePlan: ReaderGenerativePlan | null | undefined
}

function preferDisplayCopy(primary: unknown, fallback: unknown): string {
  const primaryText = String(primary || '').trim()
  if (primaryText) return primaryText
  return String(fallback || '').trim()
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function asRecordList(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
}

function toStringRows(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || '').trim()).filter(Boolean)
  }
  const text = String(value || '').trim()
  return text ? [text] : []
}

function dedupeTrimmedRows(rows: unknown[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const row of rows) {
    const text = String(row || '').trim()
    if (!text || seen.has(text)) continue
    seen.add(text)
    result.push(text)
  }
  return result
}

function isEnglishHeavyReaderCopy(raw: unknown): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  const cjkMatches = text.match(/[\u3400-\u9fff]/g) || []
  const latinMatches = text.match(/[A-Za-z]/g) || []
  if (cjkMatches.length > 0) return false
  return latinMatches.length >= 24 && latinMatches.length > cjkMatches.length * 4
}

function readStorySubstrate(input: BuildReaderExperiencePrimitivesInput): Record<string, unknown> | null {
  if (input.generativePlan?.story_substrate && typeof input.generativePlan.story_substrate === 'object') {
    return input.generativePlan.story_substrate as unknown as Record<string, unknown>
  }
  const planMeta = asRecord(input.experiencePlan?.meta)
  const storySubstrate = asRecord(planMeta?.story_substrate)
  return storySubstrate
}

function readPageBrief(input: BuildReaderExperiencePrimitivesInput): Record<string, unknown> | null {
  if (input.generativePlan?.page_brief && typeof input.generativePlan.page_brief === 'object') {
    return input.generativePlan.page_brief as unknown as Record<string, unknown>
  }
  const planMeta = asRecord(input.experiencePlan?.meta)
  const pageBrief = asRecord(planMeta?.page_brief)
  return pageBrief
}

function normalizeStoryClaims(
  storySubstrate: Record<string, unknown> | null,
  maxCards: number,
): ReaderStoryClaim[] {
  if (!storySubstrate) return []
  const rows = asRecordList(storySubstrate.main_claims)
  const claims: ReaderStoryClaim[] = []
  for (const row of rows) {
    const text = preferDisplayCopy(row.display_text, row.text)
    if (!text || isEnglishHeavyReaderCopy(text)) continue
    claims.push({
      claim_id: String(row.claim_id || '').trim() || `claim-${claims.length + 1}`,
      text: String(row.text || '').trim() || text,
      display_text: text,
      source_target_ids: toStringRows(row.source_target_ids),
      strength: String(row.strength || '').trim() === 'supporting' ? 'supporting' : 'primary',
    })
    if (claims.length >= maxCards) break
  }
  return claims
}

function buildFallbackQuestionAnswers(input: {
  visibleClaims: ReaderStoryClaim[]
  terms: string[]
  hooks: string[]
  experiencePlan: ReaderExperiencePlan | null | undefined
  pageBrief: Record<string, unknown> | null
}): FallbackQuestionAnswer[] {
  const rows: FallbackQuestionAnswer[] = []
  const narrativeGoal = String(input.pageBrief?.page_goal || input.experiencePlan?.narrative_goal || '').trim()
  const heroSummary = preferDisplayCopy(input.experiencePlan?.hero?.display_summary, input.experiencePlan?.hero?.summary)

  if (narrativeGoal) {
    rows.push({
      question: '这页最值得先理解的目标是什么？',
      answer: narrativeGoal,
    })
  }
  const leadClaim = input.visibleClaims[0]
  if (leadClaim) {
    rows.push({
      question: '这一页最关键的结论是什么？',
      answer: preferDisplayCopy(leadClaim.display_text, leadClaim.text),
    })
  } else if (heroSummary) {
    rows.push({
      question: '这页最值得先看的内容是什么？',
      answer: heroSummary,
    })
  }
  if (input.terms.length) {
    rows.push({
      question: '先补哪些概念更容易读懂？',
      answer: `优先理解 ${input.terms.slice(0, 3).join('、')}。`,
    })
  }
  if (input.hooks.length) {
    rows.push({
      question: '读完当前页后可以继续追问什么？',
      answer: input.hooks[0],
    })
  }

  const deduped: FallbackQuestionAnswer[] = []
  const seen = new Set<string>()
  for (const row of rows) {
    const question = String(row.question || '').trim()
    const answer = String(row.answer || '').trim()
    if (!question || !answer) continue
    const key = `${question}::${answer}`
    if (seen.has(key)) continue
    seen.add(key)
    deduped.push({ question, answer })
    if (deduped.length >= 3) break
  }
  return deduped
}

export function buildReaderExperiencePrimitives(
  input: BuildReaderExperiencePrimitivesInput,
): ReaderExperiencePrimitives {
  const pageBrief = readPageBrief(input)
  const storySubstrate = readStorySubstrate(input)
  const contentBudget = asRecord(pageBrief?.content_budget)
  const maxClaimCards = Math.max(1, Number(contentBudget?.max_claim_cards || 2))
  const maxHooks = Math.max(1, Number(contentBudget?.max_hooks || 2))

  const visibleClaims = normalizeStoryClaims(storySubstrate, maxClaimCards)
  const terms = dedupeTrimmedRows(
    asRecordList(storySubstrate?.terms_to_explain)
      .map((item) => String(item.term || '').trim())
      .filter(Boolean),
  ).slice(0, 5)
  const backgroundTopics = dedupeTrimmedRows(
    asRecordList(storySubstrate?.background_gaps)
      .map((item) => String(item.topic || '').trim())
      .filter(Boolean),
  ).slice(0, 4)
  const hooks = dedupeTrimmedRows([
    ...toStringRows(pageBrief?.experience_hooks),
    ...toStringRows(input.experiencePlan?.reading_path),
  ]).slice(0, maxHooks)

  const fallbackQuestionAnswers = buildFallbackQuestionAnswers({
    visibleClaims,
    terms,
    hooks,
    experiencePlan: input.experiencePlan,
    pageBrief,
  })

  return {
    visibleClaims,
    terms,
    backgroundTopics,
    hooks,
    fallbackQuestionAnswers,
  }
}
