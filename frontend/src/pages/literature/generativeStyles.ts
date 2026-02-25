import { ReaderGenerativeStyleKey } from '@/services/api'

export type GenerativeStyleTokens = {
  pageBackground: string
  panelBackground: string
  borderColor: string
  headingColor: string
  bodyColor: string
  bodyFontFamily: string
  bodyFontSize: number
  bodyLineHeight: number
  headingFontFamily: string
}

export type ReaderThemeMode = 'light' | 'dark'

export const GENERATIVE_STYLE_LABELS: Record<ReaderGenerativeStyleKey, string> = {
  journal_classic: 'Journal Classic',
  clinical_brief: 'Clinical Brief',
  preprint_modern: 'Preprint Modern',
}

export const GENERATIVE_STYLE_TOKENS: Record<ReaderGenerativeStyleKey, GenerativeStyleTokens> = {
  journal_classic: {
    pageBackground: 'linear-gradient(180deg, rgba(249, 251, 255, 0.98) 0%, rgba(241, 246, 255, 0.98) 100%)',
    panelBackground: 'rgba(255, 255, 255, 0.88)',
    borderColor: 'rgba(101, 154, 244, 0.22)',
    headingColor: '#102a50',
    bodyColor: '#14223b',
    bodyFontFamily: '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
    bodyFontSize: 18,
    bodyLineHeight: 1.95,
    headingFontFamily: '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
  },
  clinical_brief: {
    pageBackground: 'linear-gradient(180deg, rgba(246, 251, 250, 0.98) 0%, rgba(236, 246, 243, 0.98) 100%)',
    panelBackground: 'rgba(252, 255, 254, 0.9)',
    borderColor: 'rgba(75, 168, 138, 0.24)',
    headingColor: '#0f5a47',
    bodyColor: '#15382f',
    bodyFontFamily: '"IBM Plex Sans","Noto Sans SC","Segoe UI",sans-serif',
    bodyFontSize: 17,
    bodyLineHeight: 1.85,
    headingFontFamily: '"IBM Plex Sans","Noto Sans SC","Segoe UI",sans-serif',
  },
  preprint_modern: {
    pageBackground: 'linear-gradient(180deg, rgba(247, 248, 255, 0.98) 0%, rgba(236, 238, 252, 0.98) 100%)',
    panelBackground: 'rgba(251, 252, 255, 0.9)',
    borderColor: 'rgba(117, 130, 230, 0.24)',
    headingColor: '#273288',
    bodyColor: '#1f2758',
    bodyFontFamily: '"Space Grotesk","Noto Sans SC","Segoe UI",sans-serif',
    bodyFontSize: 17,
    bodyLineHeight: 1.85,
    headingFontFamily: '"Space Grotesk","Noto Sans SC","Segoe UI",sans-serif',
  },
}

export function normalizeGenerativeStyleKey(raw: string | undefined): ReaderGenerativeStyleKey {
  const value = String(raw || '').trim().toLowerCase()
  if (value === 'clinical_brief') return 'clinical_brief'
  if (value === 'preprint_modern') return 'preprint_modern'
  return 'journal_classic'
}

export function resolveGenerativeStyleTokens(
  styleKey: ReaderGenerativeStyleKey,
  themeMode: ReaderThemeMode,
): GenerativeStyleTokens {
  const base = GENERATIVE_STYLE_TOKENS[styleKey]
  if (themeMode !== 'dark') return base
  return {
    ...base,
    pageBackground: 'linear-gradient(180deg, rgba(13, 18, 33, 0.98) 0%, rgba(8, 12, 24, 0.98) 100%)',
    panelBackground: 'rgba(18, 26, 44, 0.85)',
    borderColor: 'rgba(109, 143, 206, 0.3)',
    headingColor: '#e6eeff',
    bodyColor: '#cdd8f3',
  }
}
