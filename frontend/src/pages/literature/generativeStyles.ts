import { ReaderGenerativeStyleKey } from '@/services/api'

export type GenerativeStyleTokens = {
  pageBackground: string
  panelBackground: string
  surfaceBackground: string
  railBackground: string
  overlayBackground: string
  borderColor: string
  headingColor: string
  bodyColor: string
  mutedColor: string
  bodyFontFamily: string
  bodyFontSize: number
  bodyLineHeight: number
  bodyLetterSpacing: string
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
    pageBackground: 'linear-gradient(180deg, #f4f0e8 0%, #ede8de 100%)',
    panelBackground: '#f8f4ec',
    surfaceBackground: '#f2efe6',
    railBackground: '#f5f0e7',
    overlayBackground: '#f2efe6',
    borderColor: '#d6deea',
    headingColor: '#102a50',
    bodyColor: '#14223b',
    mutedColor: '#516075',
    bodyFontFamily: '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
    bodyFontSize: 18,
    bodyLineHeight: 1.95,
    bodyLetterSpacing: '0em',
    headingFontFamily: '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
  },
  clinical_brief: {
    pageBackground: 'linear-gradient(180deg, #eef5f0 0%, #e6eee8 100%)',
    panelBackground: '#f7fbf8',
    surfaceBackground: '#fcfffd',
    railBackground: '#edf5f0',
    overlayBackground: '#f9fdf9',
    borderColor: '#c9ddd3',
    headingColor: '#0f5a47',
    bodyColor: '#15382f',
    mutedColor: '#4c665d',
    bodyFontFamily: '"IBM Plex Sans","Noto Sans SC","Segoe UI",sans-serif',
    bodyFontSize: 17,
    bodyLineHeight: 1.85,
    bodyLetterSpacing: '0em',
    headingFontFamily: '"IBM Plex Sans","Noto Sans SC","Segoe UI",sans-serif',
  },
  preprint_modern: {
    pageBackground: 'linear-gradient(180deg, #eff1f8 0%, #e7e9f2 100%)',
    panelBackground: '#f7f8fc',
    surfaceBackground: '#fcfcff',
    railBackground: '#eef0f6',
    overlayBackground: '#f9faff',
    borderColor: '#cfd6ea',
    headingColor: '#273288',
    bodyColor: '#1f2758',
    mutedColor: '#5b6486',
    bodyFontFamily: '"Space Grotesk","Noto Sans SC","Segoe UI",sans-serif',
    bodyFontSize: 17,
    bodyLineHeight: 1.85,
    bodyLetterSpacing: '0em',
    headingFontFamily: '"Space Grotesk","Noto Sans SC","Segoe UI",sans-serif',
  },
}

export function normalizeGenerativeStyleKey(raw: string | undefined): ReaderGenerativeStyleKey {
  const value = String(raw || '').trim().toLowerCase()
  if (value === 'clinical_brief') return 'clinical_brief'
  if (value === 'preprint_modern') return 'preprint_modern'
  return 'journal_classic'
}

export function mapComposeStyleIntentToKey(styleIntent: string | undefined): ReaderGenerativeStyleKey {
  const normalized = String(styleIntent || '').trim().toLowerCase()
  if (normalized === 'clinical' || normalized === 'clinical_brief') return 'clinical_brief'
  if (normalized === 'preprint' || normalized === 'preprint_modern') return 'preprint_modern'
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
    pageBackground: 'linear-gradient(180deg, #11161e 0%, #0c1118 100%)',
    panelBackground: '#202733',
    surfaceBackground: '#171d26',
    railBackground: '#202733',
    overlayBackground: '#262e3a',
    borderColor: 'rgba(141, 160, 195, 0.28)',
    headingColor: '#eef3ff',
    bodyColor: '#d6def0',
    mutedColor: '#aab4c8',
  }
}
