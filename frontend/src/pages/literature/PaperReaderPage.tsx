import { type CSSProperties, type DragEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ArrowLeftOutlined,
  LeftOutlined,
  LinkOutlined,
  PushpinOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Collapse,
  Col,
  Empty,
  Input,
  List,
  message,
  Popconfirm,
  Popover,
  Radio,
  Rate,
  Row,
  Select,
  Slider,
  Space,
  Spin,
  Tabs,
  Tag,
  ConfigProvider,
  theme,
  Tooltip,
  Typography,
} from 'antd'
import { Document as PdfDocument, Page as PdfPage, pdfjs } from 'react-pdf'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  AnnotationType,
  CommentFilter,
  CollectionKnowledgeReadiness,
  isApiTimeoutError,
  KnowledgeBase,
  knowledgeApi,
  LiteratureAskMessage,
  LiteratureAskScope,
  LiteratureAskSource,
  LiteratureAskSession,
  literatureApi,
  normalizeKnowledgeLinkStatus,
  Paper,
  PaperAnnotation,
  PaperCollection,
  PaperComment,
  PaperKnowledgeLink,
  PaperKnowledgeLinkStatusEventData,
  PaperRatingSummary,
  ReaderComposeAsset,
  ReaderComponentPatchOp,
  ReaderComponentNode,
  ReaderComponentSourceAnchor,
  ReaderInlineQueryEvent,
  ReaderInlineQuerySource,
  ReaderNodeActionRequest,
  ReaderComposePayload,
  ReaderComposeQualityReport,
  ReaderUIPlan,
  ReaderGenerativeAsset,
  ReaderGenerativeBlock,
  ReaderGenerativePagePayload,
  ReaderGenerativeSection,
  ReaderGenerativeStyleKey,
  ReaderGenerativeStyleTuning,
  ReaderPageGrounding,
  ReaderPageReadyEventData,
  ReaderSession,
} from '@/services/api'
import {
  GENERATIVE_STYLE_LABELS,
  GENERATIVE_STYLE_TOKENS,
  normalizeGenerativeStyleKey,
  type GenerativeStyleTokens,
  type ReaderThemeMode,
  resolveGenerativeStyleTokens,
} from './generativeStyles'
import { renderReaderComponentTree } from './readerComponents'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import './composedReader.css'

const { Title, Text, Paragraph } = Typography
const { TextArea } = Input

pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()
const READER_API_BASE_URL = String(
  ((import.meta as any).env?.VITE_API_BASE_URL as string) || 'http://localhost:8888',
).trim()

function parseZoomPercent(zoom: string | undefined): number {
  if (!zoom) return 120
  const value = Number(String(zoom).replace('%', '').trim())
  if (!Number.isFinite(value) || value <= 0) return 120
  return Math.max(60, Math.min(240, Math.round(value)))
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

function parsePositiveSearchParam(value: string | null): number | undefined {
  const parsed = Number(value || 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : undefined
}

function normalizeComposePipelineVersion(value: string | null): string | undefined {
  const token = String(value || '').trim().toLowerCase()
  if (token === 'layout_uid_v1') return 'layout_uid_v1'
  return undefined
}

function readJsonCache<T>(key: string | undefined): T | null {
  if (!key) return null
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

function writeJsonCache(key: string | undefined, payload: unknown): void {
  if (!key) return
  try {
    localStorage.setItem(key, JSON.stringify(payload))
  } catch {
    // ignore storage errors
  }
}

function getCurrentUserIdFromAuthStorage(): number | undefined {
  try {
    const authStorage = localStorage.getItem('auth-storage')
    if (!authStorage) return undefined
    const parsed = JSON.parse(authStorage) as { state?: { user?: { id?: number } } }
    const id = Number(parsed?.state?.user?.id || 0)
    return Number.isFinite(id) && id > 0 ? id : undefined
  } catch {
    return undefined
  }
}

type AcademicTextBlock = {
  kind: 'heading' | 'paragraph'
  text: string
}

type PdfTextItemLike = {
  str?: unknown
  transform?: unknown
  width?: unknown
}

type NormalizedPdfTextItem = {
  text: string
  x: number
  y: number
  width: number
  height: number
}

type ExtractedTextLine = {
  text: string
  xMin: number
  xMax: number
  y: number
  height: number
  column: 'single' | 'wide' | 'left' | 'right'
}

type PageResourceLink = {
  label: string
  href: string
  source: 'metadata' | 'text'
}

const DEFAULT_READER_STYLE_TUNING: ReaderGenerativeStyleTuning = {
  body_scale: 1,
  line_height: 1.9,
  heading_scale: 1,
}
const DEFAULT_COMPOSE_MAX_ITERATIONS = 16

function pickStyleTokenString(tokens: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = String(tokens[key] ?? '').trim()
    if (value) return value
  }
  return ''
}

function pickStyleTokenNumber(tokens: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = Number(tokens[key])
    if (Number.isFinite(value)) return value
  }
  return null
}

function mapComposeStyleIntentToKey(styleIntent: string, fallback: ReaderGenerativeStyleKey): ReaderGenerativeStyleKey {
  const normalized = String(styleIntent || '').trim().toLowerCase()
  if (normalized === 'clinical' || normalized === 'clinical_brief') return 'clinical_brief'
  if (normalized === 'preprint' || normalized === 'preprint_modern') return 'preprint_modern'
  if (normalized === 'journal' || normalized === 'journal_classic' || normalized === 'auto') return 'journal_classic'
  return fallback
}

function normalizeReaderStyleTuning(
  raw: unknown,
  fallbackLineHeight: number,
): ReaderGenerativeStyleTuning {
  const source = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const readNumber = (key: string, fallback: number): number => {
    const value = Number(source[key])
    return Number.isFinite(value) ? value : fallback
  }
  return {
    body_scale: Math.max(0.9, Math.min(1.25, readNumber('body_scale', DEFAULT_READER_STYLE_TUNING.body_scale))),
    line_height: Math.max(1.55, Math.min(2.2, readNumber('line_height', fallbackLineHeight))),
    heading_scale: Math.max(0.95, Math.min(1.35, readNumber('heading_scale', DEFAULT_READER_STYLE_TUNING.heading_scale))),
  }
}

const ACADEMIC_SECTION_KEYWORDS = [
  'abstract',
  'introduction',
  'background',
  'related work',
  'method',
  'methods',
  'approach',
  'experiments',
  'results',
  'discussion',
  'conclusion',
  'limitations',
  'references',
]

function isLikelySectionHeading(text: string): boolean {
  const value = text.trim()
  if (!value || value.length > 96) return false

  const normalized = value
    .replace(/^[\d.\-()ivxIVX\s]+/, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()

  if (!normalized) return false
  if (ACADEMIC_SECTION_KEYWORDS.includes(normalized)) return true

  const latinOnly = normalized.replace(/[^a-z]/g, '')
  const uppercaseLetters = value.replace(/[^A-Z]/g, '').length
  const alphaLetters = value.replace(/[^A-Za-z]/g, '').length
  const uppercaseRatio = alphaLetters > 0 ? uppercaseLetters / alphaLetters : 0

  const hasSentencePunctuation = /[。！？!?]/.test(value)
  return Boolean(
    !hasSentencePunctuation &&
    latinOnly.length >= 4 &&
    (uppercaseRatio >= 0.72 || /^[\d.\-()ivxIVX]+\s+[A-Za-z]/.test(value)),
  )
}

function getMedian(values: number[]): number {
  if (!values.length) return 0
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  if (sorted.length % 2 === 1) return sorted[mid]
  return (sorted[mid - 1] + sorted[mid]) / 2
}

function splitCompactedNumberedLine(line: string): string[] {
  const value = line.trim()
  if (!value) return []
  const markerMatches = value.match(/\b\d+\.\s+[A-Z]/g)
  if (!markerMatches || markerMatches.length < 2) return [value]

  const parts = value
    .split(/(?<=\S)\s+(?=\d+\.\s+[A-Z])/g)
    .map((item) => item.trim())
    .filter(Boolean)
  return parts.length > 1 ? parts : [value]
}

function normalizeAcademicArtifacts(text: string): string {
  return String(text || '')
    .replace(/([A-Za-z]{2,})-\s+([a-z]{2,})/g, '$1$2')
    .replace(/\s+([,.;:!?])/g, '$1')
    .replace(/([([{])\s+/g, '$1')
    .replace(/\s+([)\]}])/g, '$1')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

function normalizeAbsoluteLink(raw: string | undefined): string | null {
  const value = String(raw || '').trim().replace(/[)\].,;:]+$/g, '')
  if (!value) return null
  if (/^https?:\/\//i.test(value)) return value
  if (/^(?:dx\.)?doi\.org\//i.test(value)) return `https://${value}`
  if (/^10\.\d{4,9}\/\S+/i.test(value)) return `https://doi.org/${value}`
  if (/^www\./i.test(value)) return `https://${value}`
  return null
}

function collectPageResourceLinks(paper: Paper | null, pageText: string): PageResourceLink[] {
  const linkMap = new Map<string, PageResourceLink>()
  const pushLink = (label: string, rawHref: string | undefined, source: PageResourceLink['source']) => {
    const href = normalizeAbsoluteLink(rawHref)
    if (!href) return
    const key = href.toLowerCase()
    if (linkMap.has(key)) return
    linkMap.set(key, { label, href, source })
  }

  if (paper) {
    pushLink('论文主页', paper.url, 'metadata')
    pushLink('PDF原链接', paper.pdf_url, 'metadata')
    if (paper.arxiv_url) {
      pushLink('arXiv', paper.arxiv_url, 'metadata')
    } else if (paper.arxiv_id) {
      pushLink('arXiv', `https://arxiv.org/abs/${paper.arxiv_id}`, 'metadata')
    }
    if (paper.doi) {
      const doiLabel = `DOI: ${paper.doi}`
      pushLink(doiLabel, paper.doi, 'metadata')
    }
  }

  const text = String(pageText || '')
  const doiMatches = text.match(/\b10\.\d{4,9}\/[^\s"'<>]+/gi) || []
  doiMatches.slice(0, 8).forEach((doi) => {
    pushLink(`DOI: ${doi}`, doi, 'text')
  })

  const doiOrgMatches = text.match(/\b(?:https?:\/\/)?(?:dx\.)?doi\.org\/[^\s"'<>]+/gi) || []
  doiOrgMatches.slice(0, 8).forEach((url) => {
    pushLink('DOI链接', url, 'text')
  })

  const urlMatches = text.match(/\bhttps?:\/\/[^\s"'<>]+/gi) || []
  urlMatches.slice(0, 12).forEach((url) => {
    if (/doi\.org\//i.test(url)) return
    pushLink('页内链接', url, 'text')
  })

  return Array.from(linkMap.values()).slice(0, 12)
}

const READING_FLOW_COMPONENT_TYPES = new Set([
  'SectionHeading',
  'Separator',
  'ParagraphProse',
  'ListBlock',
  'FigurePanel',
  'TablePanel',
  'EquationBlock',
  'AbstractCard',
  'MethodologyCard',
  'CalloutBox',
  'CompareInsightsCard',
  'InsightClusterCard',
  'SectionBridgeCard',
  'InlineQuerySlot',
  'AnswerCard',
])

const CONTEXT_ONLY_COMPONENT_TYPES = new Set([
  'PaperHeaderCard',
  'MetadataSidebarCard',
  'ContextRail',
  'SectionTOC',
  'CitationLinks',
  'CitationCard',
  'PdfSnippetCard',
  'KeyTakeaways',
  'AnnotationRail',
  'QualityBadge',
  'QualityPanel',
])

function getReaderNodePlacement(node: ReaderComponentNode): 'main' | 'context' {
  const type = String(node.type || '').trim()
  if (CONTEXT_ONLY_COMPONENT_TYPES.has(type)) return 'context'
  if (READING_FLOW_COMPONENT_TYPES.has(type)) return 'main'

  const zoneType = String(node.zone_type || '').trim().toLowerCase()
  const columnId = String(node.column_id || '').trim().toLowerCase()
  const region = String(node.region || '').trim().toLowerCase()
  if (zoneType === 'side_context') return 'context'
  if (columnId === 'sidebar' || region === 'sidebar') return 'context'

  if (type === 'ParagraphProse' || type === 'ListBlock' || type === 'CalloutBox' || type === 'SectionHeading') {
    const hints = collectReaderNodeTextHints(node)
    if (hints.some((item) => isLikelyContextOnlyText(item))) return 'context'
  }

  return 'main'
}

function collectReaderNodeTextHints(node: ReaderComponentNode): string[] {
  const props = ((node.props && typeof node.props === 'object') ? node.props : {}) as Record<string, unknown>
  const hints = [
    props.text,
    props.title,
    props.caption,
    props.content,
    props.description,
    props.doi,
    props.label,
    props.subtitle,
  ]
  if (Array.isArray(props.items)) {
    for (const item of props.items.slice(0, 6)) {
      if (!item || typeof item !== 'object') continue
      const row = item as Record<string, unknown>
      hints.push(row.text, row.label, row.value)
    }
  }
  return hints.map((item) => String(item || '').trim()).filter(Boolean)
}

function isLikelyContextOnlyText(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  if (/^(?:research article|open access|corresponding author|supplementary material)$/i.test(text)) return true
  if (text.length <= 240 && /(?:https?:\/\/)?(?:dx\.)?doi\.org\/\S+/i.test(text)) return true
  if (text.length <= 180 && /^doi:\s*10\.\S+/i.test(text)) return true
  if (text.length <= 140 && /\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b/i.test(text)) return true
  if (
    text.length <= 420
    && /\b(?:Department of|School of Medicine|University|Hospital|Medical Center|Inc\b|LLC\b|Institute)\b/i.test(text)
    && (text.match(/\b\d+\b/g) || []).length >= 2
  ) {
    return true
  }
  if (
    text.length <= 360
    && (text.match(/,/g) || []).length >= 5
    && /\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\d/.test(text)
  ) {
    return true
  }
  if (text.length <= 220 && /\b(?:received|accepted|published|copyright|pmid|pmcid)\b/i.test(text)) return true
  if (
    text.length <= 220
    && /\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b/i.test(text)
    && /\b\d+\s*\/\s*\d+\b/.test(text)
  ) {
    return true
  }
  return false
}

function isContextOnlyReaderNode(node: ReaderComponentNode): boolean {
  return getReaderNodePlacement(node) === 'context'
}

function isLikelyStandalonePageNumber(text: string): boolean {
  const value = text.trim()
  if (!value) return false
  if (/^\d{1,4}$/.test(value)) return true
  if (/^[ivxlcdmIVXLCDM]{1,8}$/.test(value)) return true
  return false
}

function stripLikelyPageNumberSuffix(text: string): string {
  const value = text.trim()
  if (!value) return ''
  const match = value.match(/^(.*?)([。！？!?;；.:])\s+(\d{1,4})$/)
  if (!match) return value

  const prefix = match[1].trim()
  const number = Number(match[3])
  if (!Number.isFinite(number) || number <= 0 || number > 3000) return value
  if (prefix.length < 20) return value
  return `${prefix}${match[2]}`
}

function buildAcademicTextBlocks(rawText: string): AcademicTextBlock[] {
  const lines = (rawText || '')
    .replace(/\u00a0/g, ' ')
    .split(/\r?\n/)
    .map((line) => line.replace(/\s+/g, ' ').trim())
    .flatMap((line) => splitCompactedNumberedLine(line))
    .map((line) => normalizeAcademicArtifacts(stripLikelyPageNumberSuffix(line)))
    .filter((line) => Boolean(line) && !isLikelyStandalonePageNumber(line))

  if (lines.length === 0) return []

  const blocks: AcademicTextBlock[] = []
  const paragraphLines: string[] = []
  const paragraphMaxLength = 340

  const flushParagraph = () => {
    const merged = paragraphLines.join(' ').replace(/\s+/g, ' ').trim()
    paragraphLines.length = 0
    if (!merged) return

    if (merged.length <= paragraphMaxLength) {
      blocks.push({ kind: 'paragraph', text: merged })
      return
    }

    const chunks = merged
      .replace(/([。！？!?;；])\s+/g, '$1\n')
      .split('\n')
      .map((item) => item.trim())
      .filter(Boolean)

    let buffer = ''
    for (const sentence of chunks) {
      if (!buffer) {
        buffer = sentence
        continue
      }
      if (buffer.length + sentence.length + 1 > paragraphMaxLength) {
        blocks.push({ kind: 'paragraph', text: buffer.trim() })
        buffer = sentence
      } else {
        buffer = `${buffer} ${sentence}`
      }
    }
    if (buffer.trim()) {
      blocks.push({ kind: 'paragraph', text: buffer.trim() })
    }
  }

  for (const line of lines) {
    if (!line) {
      flushParagraph()
      continue
    }

    if (isLikelySectionHeading(line)) {
      flushParagraph()
      blocks.push({ kind: 'heading', text: line })
      continue
    }

    if (paragraphLines.length === 0) {
      paragraphLines.push(line)
      continue
    }

    const prevLine = paragraphLines[paragraphLines.length - 1]
    const shouldBreakBySentence =
      /[。！？!?]$/.test(prevLine) &&
      /^[A-Z][a-z]/.test(line) &&
      prevLine.length >= 72

    if (shouldBreakBySentence) {
      flushParagraph()
      paragraphLines.push(line)
      continue
    }

    paragraphLines.push(line)
  }
  flushParagraph()
  return blocks
}

function extractAcademicPageText(textContent: any): string {
  const rawItems = Array.isArray(textContent?.items) ? (textContent.items as PdfTextItemLike[]) : []
  if (rawItems.length === 0) return ''

  const normalizedItems = rawItems
    .map((item) => {
      const text = typeof item?.str === 'string' ? item.str.replace(/\s+/g, ' ').trim() : ''
      if (!text) return null

      const transform = Array.isArray(item?.transform) ? item.transform : []
      const x = Number(transform?.[4] ?? 0)
      const y = Number(transform?.[5] ?? 0)
      const width = Number(item?.width ?? text.length * 4)
      const sx = Math.abs(Number(transform?.[0] ?? 0))
      const sy = Math.abs(Number(transform?.[3] ?? 0))
      const inferredHeight = Math.max(sx, sy)
      return {
        text,
        x: Number.isFinite(x) ? x : 0,
        y: Number.isFinite(y) ? y : 0,
        width: Number.isFinite(width) && width > 0 ? width : text.length * 4,
        height: Number.isFinite(inferredHeight) && inferredHeight > 0 ? inferredHeight : 10,
      }
    })
    .filter((item): item is NormalizedPdfTextItem => Boolean(item))

  if (normalizedItems.length === 0) return ''

  normalizedItems.sort((a, b) => {
    const yDiff = b.y - a.y
    if (Math.abs(yDiff) > 0.8) return yDiff
    return a.x - b.x
  })

  const medianHeight = getMedian(normalizedItems.map((item) => item.height)) || 10
  const lineYThreshold = clamp(medianHeight * 0.45, 2, 4.8)
  const inlineSpaceThreshold = clamp(medianHeight * 0.22, 1.8, 9)
  const paragraphGapThreshold = Math.max(12, medianHeight * 1.4)
  const lineItems: ExtractedTextLine[] = []

  let currentItems: NormalizedPdfTextItem[] = []
  let currentY = normalizedItems[0].y

  const pushCurrentLine = () => {
    if (currentItems.length === 0) return
    const sorted = [...currentItems].sort((a, b) => a.x - b.x)
    let text = sorted[0].text
    let lastRight = sorted[0].x + sorted[0].width
    for (let i = 1; i < sorted.length; i += 1) {
      const token = sorted[i]
      const gap = token.x - lastRight
      if (gap > inlineSpaceThreshold) {
        text += ' '
      }
      text += token.text
      lastRight = Math.max(lastRight, token.x + token.width)
    }

    const normalizedText = text.replace(/\s+/g, ' ').trim()
    if (normalizedText) {
      lineItems.push({
        text: normalizedText,
        xMin: sorted[0].x,
        xMax: Math.max(...sorted.map((item) => item.x + item.width)),
        y: getMedian(sorted.map((item) => item.y)),
        height: getMedian(sorted.map((item) => item.height)) || medianHeight,
        column: 'single',
      })
    }
    currentItems = []
  }

  normalizedItems.forEach((item) => {
    if (currentItems.length === 0) {
      currentItems = [item]
      currentY = item.y
      return
    }

    const yDiff = Math.abs(item.y - currentY)
    if (yDiff > lineYThreshold) {
      pushCurrentLine()
      currentItems = [item]
      currentY = item.y
      return
    }

    currentItems.push(item)
    currentY = getMedian(currentItems.map((lineItem) => lineItem.y))
  })
  pushCurrentLine()

  if (lineItems.length === 0) return ''

  lineItems.sort((a, b) => {
    const yDiff = b.y - a.y
    if (Math.abs(yDiff) > 0.8) return yDiff
    return a.xMin - b.xMin
  })

  const minX = Math.min(...lineItems.map((line) => line.xMin))
  const maxX = Math.max(...lineItems.map((line) => line.xMax))
  const pageWidth = Math.max(maxX - minX, 1)
  const splitX = minX + pageWidth * 0.5
  const wideSpanThreshold = pageWidth * 0.72

  const wideLines: ExtractedTextLine[] = []
  const leftLines: ExtractedTextLine[] = []
  const rightLines: ExtractedTextLine[] = []

  for (const line of lineItems) {
    const span = line.xMax - line.xMin
    if (span >= wideSpanThreshold) {
      wideLines.push({ ...line, column: 'wide' })
      continue
    }
    const centerX = (line.xMin + line.xMax) / 2
    if (centerX <= splitX) {
      leftLines.push({ ...line, column: 'left' })
    } else {
      rightLines.push({ ...line, column: 'right' })
    }
  }

  const likelyTwoColumns =
    leftLines.length >= 6 &&
    rightLines.length >= 6 &&
    getMedian(rightLines.map((line) => line.xMin)) - getMedian(leftLines.map((line) => line.xMin)) >
    pageWidth * 0.18

  let orderedLines: ExtractedTextLine[] = []
  if (!likelyTwoColumns) {
    orderedLines = lineItems.map((line) => ({ ...line, column: 'single' }))
  } else {
    const sortLines = (items: ExtractedTextLine[]) =>
      items.sort((a, b) => {
        const yDiff = b.y - a.y
        if (Math.abs(yDiff) > 0.8) return yDiff
        return a.xMin - b.xMin
      })

    sortLines(wideLines)
    sortLines(leftLines)
    sortLines(rightLines)

    const topColumnY = Math.max(leftLines[0]?.y ?? 0, rightLines[0]?.y ?? 0)
    const headerWideLines = wideLines.filter((line) => line.y > topColumnY + lineYThreshold)
    const footerWideLines = wideLines.filter((line) => line.y <= topColumnY + lineYThreshold)
    orderedLines = [...headerWideLines, ...leftLines, ...rightLines, ...footerWideLines]
  }

  const output: string[] = []
  let prevLine: ExtractedTextLine | null = null
  for (const line of orderedLines) {
    const normalizedLineText = normalizeAcademicArtifacts(line.text)
    if (!normalizedLineText || isLikelyStandalonePageNumber(normalizedLineText)) {
      continue
    }
    if (prevLine) {
      const yGap = Math.abs(prevLine.y - line.y)
      const isColumnSwitch =
        prevLine.column !== line.column &&
        prevLine.column !== 'wide' &&
        line.column !== 'wide' &&
        prevLine.column !== 'single' &&
        line.column !== 'single'
      if (isColumnSwitch || yGap > paragraphGapThreshold) {
        output.push('')
      }
    }
    output.push(normalizedLineText)
    prevLine = line
  }

  return output.join('\n').trim()
}

type PendingSectionJump = {
  sectionTitle: string
  expectedPage?: number
}

type ReaderDetailLevel = 'concise' | 'standard' | 'deep'
type ComposedBackendOptions = {
  detailLevel: ReaderDetailLevel
  compareMode: boolean
  citationTldr: boolean
}
type PendingComposedRun = {
  regenerate: boolean
  applyCurrentOptions: boolean
}
type AnchorMatchMethod = 'polygon' | 'bbox_hint' | 'quote_exact' | 'quote_fuzzy' | 'char_range' | 'fallback'

type AnchorPreviewState = {
  visible: boolean
  pinned: boolean
  loading: boolean
  preview_key?: string
  page: number
  text: string
  title: string
  anchors: ReaderComponentSourceAnchor[]
  anchor_index: number
  anchor_count: number
  image_data_url?: string | null
  match_method?: AnchorMatchMethod
  match_confidence?: number
  fallback_used?: boolean
}

type ReaderAnchorPreviewOptions = {
  pinPreview?: boolean
  segmentIndex?: number
  sourceBlockIds?: string[]
  sourceAtomIds?: string[]
}

type PageStructureBlockSpatialRow = {
  blockId: string
  text: string
  bbox?: ReaderComponentSourceAnchor['bbox_hint']
  polygon?: Array<{ x: number; y: number }>
}

type PageStructureLayoutSpatialRow = {
  layoutId: string
  text: string
  bbox?: ReaderComponentSourceAnchor['bbox_hint']
  polygons?: Array<Array<{ x: number; y: number }>>
}

type PageStructureSpatialDimensions = {
  pageWidth?: number
  pageHeight?: number
}

function normalizeAnswerMarkdown(answer: string): string {
  const value = String(answer || '').replace(/\r\n?/g, '\n').trim()
  if (!value) return ''

  const normalizedLines: string[] = []
  const lines = value.split('\n')
  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/g, '')
    const trimmed = line.trim()
    const isHeading = /^#{1,6}\s+\S/.test(trimmed)
    const isList = /^([-*+]|\d+\.)\s+\S/.test(trimmed)
    if ((isHeading || isList) && normalizedLines.length > 0 && normalizedLines[normalizedLines.length - 1] !== '') {
      normalizedLines.push('')
    }
    normalizedLines.push(line)
    if (isHeading && normalizedLines[normalizedLines.length - 1] !== '') {
      normalizedLines.push('')
    }
  }

  return normalizedLines
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/\[(?:来源)?(\d{1,3})\]/g, (_, indexText: string) => `[来源${indexText}](source://${indexText})`)
    .trim()
}

function normalizeSectionKey(value: string | undefined): string {
  const raw = String(value || '').trim().toLowerCase()
  if (!raw) return ''
  return raw
    .replace(/[\u3000\s]+/g, '')
    .replace(/[，。！？；：、,.!?;:()[\]{}【】<>《》"'`~!@#$%^&*+=|\\/]/g, '')
}

function extractSectionNumber(value: string | undefined): string {
  const text = String(value || '').trim()
  const match = text.match(/^(\d+(?:\.\d+){0,5})/)
  return match ? match[1] : ''
}

function computeSectionMatchScore(target: string, candidate: string): number {
  const targetKey = normalizeSectionKey(target)
  const candidateKey = normalizeSectionKey(candidate)
  if (!targetKey || !candidateKey) return 0

  if (targetKey === candidateKey) return 150

  const targetNo = extractSectionNumber(target)
  const candidateNo = extractSectionNumber(candidate)
  let score = 0
  if (targetNo && candidateNo) {
    if (targetNo === candidateNo) score += 80
    else if (targetNo.startsWith(candidateNo) || candidateNo.startsWith(targetNo)) score += 45
  }

  if (candidateKey.includes(targetKey)) score += 70
  else if (targetKey.includes(candidateKey)) score += 50

  const targetTokens = Array.from(new Set((target || '').toLowerCase().split(/[\s\-_:：，,.;；。()]+/).filter(Boolean)))
  const candidateTokens = new Set((candidate || '').toLowerCase().split(/[\s\-_:：，,.;；。()]+/).filter(Boolean))
  const overlap = targetTokens.filter((item) => candidateTokens.has(item)).length
  if (overlap > 0) score += overlap * 8
  return score
}

function findBestSectionHeadingIndex(blocks: AcademicTextBlock[], sectionTitle: string): number | null {
  let bestIdx: number | null = null
  let bestScore = 0
  blocks.forEach((block, idx) => {
    if (block.kind !== 'heading') return
    const score = computeSectionMatchScore(sectionTitle, block.text)
    if (score > bestScore) {
      bestScore = score
      bestIdx = idx
    }
  })
  return bestScore >= 60 ? bestIdx : null
}

function replaceNodeInTree(
  nodes: ReaderComponentNode[],
  nodeId: string,
  nodeAfter: ReaderComponentNode,
): ReaderComponentNode[] {
  return nodes.map((node) => {
    if (node.id === nodeId) {
      return nodeAfter
    }
    const children = Array.isArray(node.children) ? node.children : []
    if (children.length === 0) return node
    return {
      ...node,
      children: replaceNodeInTree(children, nodeId, nodeAfter),
    }
  })
}

function insertNodeAfterInTree(
  nodes: ReaderComponentNode[],
  nodeId: string,
  nodeAfter: ReaderComponentNode,
): ReaderComponentNode[] {
  const output: ReaderComponentNode[] = []
  for (const node of nodes) {
    output.push(node)
    if (node.id === nodeId) {
      output.push(nodeAfter)
      continue
    }
    const children = Array.isArray(node.children) ? node.children : []
    if (children.length > 0) {
      output[output.length - 1] = {
        ...node,
        children: insertNodeAfterInTree(children, nodeId, nodeAfter),
      }
    }
  }
  return output
}

function findNodeInTree(
  nodes: ReaderComponentNode[],
  nodeId: string,
): ReaderComponentNode | null {
  for (const node of nodes) {
    if (String(node.id) === String(nodeId)) return node
    const children = Array.isArray(node.children) ? node.children : []
    if (children.length > 0) {
      const found = findNodeInTree(children, nodeId)
      if (found) return found
    }
  }
  return null
}

function normalizeAnchorMatchText(value: string): string {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
}

function findAllOccurrences(source: string, query: string, limit = 64): number[] {
  if (!source || !query) return []
  const output: number[] = []
  let cursor = 0
  while (cursor >= 0 && cursor < source.length && output.length < limit) {
    const idx = source.indexOf(query, cursor)
    if (idx < 0) break
    output.push(idx)
    cursor = idx + Math.max(1, query.length)
  }
  return output
}

function removeNodeInTree(
  nodes: ReaderComponentNode[],
  nodeId: string,
): ReaderComponentNode[] {
  const output: ReaderComponentNode[] = []
  for (const node of nodes) {
    if (node.id === nodeId) continue
    const children = Array.isArray(node.children) ? node.children : []
    output.push({
      ...node,
      children: children.length > 0 ? removeNodeInTree(children, nodeId) : children,
    })
  }
  return output
}

function updateNodePropsInTree(
  nodes: ReaderComponentNode[],
  nodeId: string,
  propsPatch: Record<string, unknown>,
): ReaderComponentNode[] {
  return nodes.map((node) => {
    if (node.id === nodeId) {
      return {
        ...node,
        props: {
          ...(node.props || {}),
          ...(propsPatch || {}),
        },
      }
    }
    const children = Array.isArray(node.children) ? node.children : []
    if (children.length === 0) return node
    return {
      ...node,
      children: updateNodePropsInTree(children, nodeId, propsPatch),
    }
  })
}

function reorderTopLevelNodes(
  nodes: ReaderComponentNode[],
  orderedNodeIds: string[],
): ReaderComponentNode[] {
  const idToNode = new Map(nodes.map((node) => [String(node.id), node]))
  const ordered: ReaderComponentNode[] = []
  const seen = new Set<string>()
  for (const nodeId of orderedNodeIds) {
    const key = String(nodeId || '').trim()
    if (!key || seen.has(key)) continue
    const node = idToNode.get(key)
    if (!node) continue
    ordered.push(node)
    seen.add(key)
  }
  for (const node of nodes) {
    const key = String(node.id || '')
    if (seen.has(key)) continue
    ordered.push(node)
  }
  return ordered
}

function applyComponentPatchOps(
  nodes: ReaderComponentNode[],
  ops: ReaderComponentPatchOp[],
): ReaderComponentNode[] {
  let next = [...nodes]
  for (const op of Array.isArray(ops) ? ops : []) {
    const kind = String(op?.op || '').trim()
    if (kind === 'reorder_components') {
      next = reorderTopLevelNodes(next, Array.isArray(op.ordered_component_ids) ? op.ordered_component_ids : [])
      continue
    }
    if (kind === 'remove_component') {
      const nodeId = String(op.component_id || '').trim()
      if (!nodeId) continue
      next = removeNodeInTree(next, nodeId)
      continue
    }
    if (kind === 'update_component_props') {
      const nodeId = String(op.component_id || '').trim()
      if (!nodeId || !op.props_patch || typeof op.props_patch !== 'object') continue
      next = updateNodePropsInTree(next, nodeId, op.props_patch)
      continue
    }
    if (kind === 'insert_component') {
      const node = op.component
      if (!node || typeof node !== 'object') continue
      const afterId = String(op.after_component_id || '').trim()
      next = afterId ? insertNodeAfterInTree(next, afterId, node) : [...next, node]
    }
  }
  return next
}

const ACTIONABLE_ANCHOR_MIN_CONFIDENCE = 0.78

function isActionableAnchor(anchor: ReaderComponentSourceAnchor): boolean {
  const start = Number(anchor.start_char || 0)
  const end = Number(anchor.end_char || 0)
  if (end <= start) return false
  const canonicalBlockId = String(anchor.canonical_block_id || '').trim()
  const sourceLayoutId = String(anchor.source_layout_id || '').trim()
  const coordVersion = String(anchor.coord_version || anchor.anchor_v2?.coord_version || '').trim()
  const hasGeometryPolygons = Array.isArray(anchor.geometry?.polygons) && anchor.geometry.polygons.length > 0
  if (coordVersion === 'layout_uid_v1') {
    if (!sourceLayoutId && !hasGeometryPolygons) return false
  } else if (coordVersion === 'anchor_v2') {
    if (!canonicalBlockId) return false
  } else if (coordVersion !== 'anchor_v2') {
    return false
  }
  const confidence = Number(anchor.anchor_confidence || 0)
  if (confidence > 0 && confidence < ACTIONABLE_ANCHOR_MIN_CONFIDENCE) return false
  return true
}

function sortAnchorsForPreview(
  anchors: ReaderComponentSourceAnchor[],
  preferredPage?: number,
): ReaderComponentSourceAnchor[] {
  const validAnchors = Array.isArray(anchors)
    ? anchors.filter((item) => Number.isFinite(item.page) && Number(item.page) > 0 && isActionableAnchor(item))
    : []
  return [...validAnchors].sort((left, right) => {
    const leftOnPage = Number(preferredPage || 0) > 0 && Number(left.page) === Number(preferredPage)
    const rightOnPage = Number(preferredPage || 0) > 0 && Number(right.page) === Number(preferredPage)
    if (leftOnPage !== rightOnPage) return leftOnPage ? -1 : 1
    const leftSegment = Number(left.segment_index || 0)
    const rightSegment = Number(right.segment_index || 0)
    if (leftSegment > 0 && rightSegment > 0 && leftSegment !== rightSegment) {
      return leftSegment - rightSegment
    }
    const leftStart = Number(left.start_char || 0)
    const rightStart = Number(right.start_char || 0)
    if (leftStart !== rightStart) return leftStart - rightStart
    return Number(left.end_char || 0) - Number(right.end_char || 0)
  })
}

function pickPrimaryAnchor(
  anchors: ReaderComponentSourceAnchor[],
  preferredPage?: number,
): ReaderComponentSourceAnchor | null {
  if (!Array.isArray(anchors) || anchors.length === 0) return null
  const validAnchors = sortAnchorsForPreview(anchors, preferredPage)
  if (validAnchors.length === 0) return null
  const scored = validAnchors.map((item, idx) => {
    const span = Math.max(0, Number(item.end_char || 0) - Number(item.start_char || 0))
    const quoteLength = String(item.quote_text || '').trim().length
    const hasBbox = Boolean(item.bbox_hint && Number(item.bbox_hint.x1) > Number(item.bbox_hint.x0))
    let score = 0
    if (Number(preferredPage || 0) > 0 && Number(item.page) === Number(preferredPage)) score += 1000
    if (hasBbox) score += 220
    if (quoteLength > 0) score += 80 + Math.min(220, quoteLength) * 0.3
    if (span >= 24 && span <= 1800) score += 48
    else if (span > 0) score += 16
    score -= idx * 0.01
    return { item, score }
  })
  scored.sort((a, b) => b.score - a.score)
  return scored[0]?.item || null
}

function mergeAnchorsForPreview(
  primary: ReaderComponentSourceAnchor,
  anchors: ReaderComponentSourceAnchor[],
): ReaderComponentSourceAnchor {
  const cluster = (Array.isArray(anchors) ? anchors : [])
    .filter((item) => Number.isFinite(Number(item.page || 0)) && Number(item.page || 0) > 0)
    .sort((left, right) => Number(left.start_char || 0) - Number(right.start_char || 0))
  if (cluster.length <= 1) return primary

  const page = Number(primary.page || cluster[0]?.page || 1)
  const startChar = cluster.reduce((minValue, item) => Math.min(minValue, Number(item.start_char || minValue)), Number(primary.start_char || 0))
  const endChar = cluster.reduce((maxValue, item) => Math.max(maxValue, Number(item.end_char || maxValue)), Number(primary.end_char || 0))
  const confidence = cluster.reduce((maxValue, item) => Math.max(maxValue, Number(item.anchor_confidence || 0)), Number(primary.anchor_confidence || 0))

  const bboxHints = cluster
    .map((item) => item.bbox_hint)
    .filter((hint) => hint && Number.isFinite(Number(hint.x0)) && Number.isFinite(Number(hint.x1)))
  let mergedBbox = primary.bbox_hint || undefined
  if (bboxHints.length > 0) {
    const pageWidth = Number(bboxHints[0]?.page_width || primary.bbox_hint?.page_width || 0) || undefined
    const pageHeight = Number(bboxHints[0]?.page_height || primary.bbox_hint?.page_height || 0) || undefined
    mergedBbox = {
      x0: Math.min(...bboxHints.map((hint) => Number(hint?.x0 || 0))),
      x1: Math.max(...bboxHints.map((hint) => Number(hint?.x1 || 0))),
      top: Math.min(...bboxHints.map((hint) => Number(hint?.top || 0))),
      bottom: Math.max(...bboxHints.map((hint) => Number(hint?.bottom || 0))),
      page_width: pageWidth,
      page_height: pageHeight,
    }
  }

  const polygons = cluster.flatMap((item) => (
    Array.isArray(item.geometry?.polygons) ? item.geometry!.polygons : []
  ))
  const geometry = polygons.length > 0
    ? {
      polygons,
      page_width: Number(cluster[0]?.geometry?.page_width || primary.geometry?.page_width || 0) || undefined,
      page_height: Number(cluster[0]?.geometry?.page_height || primary.geometry?.page_height || 0) || undefined,
    }
    : primary.geometry

  return {
    ...primary,
    page,
    start_char: startChar,
    end_char: Math.max(startChar + 1, endChar),
    quote: cluster.length <= 1 ? primary.quote : undefined,
    quote_text: cluster.length <= 1 ? primary.quote_text : undefined,
    anchor_confidence: confidence > 0 ? confidence : primary.anchor_confidence,
    segment_index: 0,
    segment_total: cluster.length,
    bbox_hint: mergedBbox,
    geometry_version: geometry ? 'poly_v1' : primary.geometry_version,
    geometry,
    anchor_v2: primary.anchor_v2
      ? {
        ...primary.anchor_v2,
        page,
        start_char: startChar,
        end_char: Math.max(startChar + 1, endChar),
      }
      : primary.anchor_v2,
  }
}

function isTextLikeAnchor(anchor: ReaderComponentSourceAnchor): boolean {
  const bbox = anchor.bbox_hint
  if (!bbox) return false
  const width = Math.max(0, Number(bbox.x1 || 0) - Number(bbox.x0 || 0))
  const height = Math.max(0, Number(bbox.bottom || 0) - Number(bbox.top || 0))
  if (width <= 0 || height <= 0) return false
  return height <= 96 && width >= 48
}

function horizontalOverlapRatio(
  left: ReaderComponentSourceAnchor,
  right: ReaderComponentSourceAnchor,
): number {
  const leftBox = left.bbox_hint
  const rightBox = right.bbox_hint
  if (!leftBox || !rightBox) return 0
  const leftWidth = Math.max(0, Number(leftBox.x1 || 0) - Number(leftBox.x0 || 0))
  const rightWidth = Math.max(0, Number(rightBox.x1 || 0) - Number(rightBox.x0 || 0))
  if (leftWidth <= 0 || rightWidth <= 0) return 0
  const overlap = Math.max(0, Math.min(Number(leftBox.x1 || 0), Number(rightBox.x1 || 0)) - Math.max(Number(leftBox.x0 || 0), Number(rightBox.x0 || 0)))
  return overlap / Math.max(1, Math.min(leftWidth, rightWidth))
}

function canClusterAdjacentTextAnchors(
  left: ReaderComponentSourceAnchor,
  right: ReaderComponentSourceAnchor,
): boolean {
  if (!isTextLikeAnchor(left) || !isTextLikeAnchor(right)) return false
  if (Number(left.page || 0) !== Number(right.page || 0)) return false
  const leftBox = left.bbox_hint!
  const rightBox = right.bbox_hint!
  const leftHeight = Math.max(1, Number(leftBox.bottom || 0) - Number(leftBox.top || 0))
  const rightHeight = Math.max(1, Number(rightBox.bottom || 0) - Number(rightBox.top || 0))
  const verticalGap = Math.max(0, Number(rightBox.top || 0) - Number(leftBox.bottom || 0))
  const maxGap = Math.max(18, Math.max(leftHeight, rightHeight) * 1.8)
  if (verticalGap > maxGap) return false
  const overlap = horizontalOverlapRatio(left, right)
  if (overlap >= 0.45) return true
  const leftX0 = Number(leftBox.x0 || 0)
  const rightX0 = Number(rightBox.x0 || 0)
  return Math.abs(leftX0 - rightX0) <= Math.max(24, Math.min(leftHeight, rightHeight) * 3)
}

function clusterAdjacentTextAnchorsForPreview(
  primary: ReaderComponentSourceAnchor,
  anchors: ReaderComponentSourceAnchor[],
): ReaderComponentSourceAnchor[] {
  const sorted = [...anchors]
    .filter((item) => Number(item.page || 0) === Number(primary.page || 0))
    .sort((left, right) => {
      const leftTop = Number(left.bbox_hint?.top || 0)
      const rightTop = Number(right.bbox_hint?.top || 0)
      if (leftTop !== rightTop) return leftTop - rightTop
      return Number(left.start_char || 0) - Number(right.start_char || 0)
    })
  if (sorted.length <= 1 || !isTextLikeAnchor(primary)) return [primary]
  const primaryKey = buildPreviewKey(primary)
  const primaryIndex = sorted.findIndex((item) => buildPreviewKey(item) === primaryKey)
  if (primaryIndex < 0) return [primary]
  let start = primaryIndex
  let end = primaryIndex
  while (start > 0 && canClusterAdjacentTextAnchors(sorted[start - 1], sorted[start])) {
    start -= 1
  }
  while (end < sorted.length - 1 && canClusterAdjacentTextAnchors(sorted[end], sorted[end + 1])) {
    end += 1
  }
  return sorted.slice(start, end + 1)
}

function buildAnchorPreviewTarget(
  anchors: ReaderComponentSourceAnchor[],
  preferredPage?: number,
): { previewAnchor: ReaderComponentSourceAnchor; previewAnchors: ReaderComponentSourceAnchor[] } | null {
  const orderedAnchors = sortAnchorsForPreview(anchors, preferredPage)
  if (orderedAnchors.length === 0) return null
  const primary = pickPrimaryAnchor(orderedAnchors, preferredPage) || orderedAnchors[0]
  if (!primary) return null
  const page = Number(primary.page || preferredPage || 0)
  const primaryCanonicalBlockId = String(
    primary.canonical_block_id || primary.anchor_v2?.canonical_block_id || '',
  ).trim()
  const pageAnchors = orderedAnchors.filter((item) => Number(item.page || 0) === page)
  const sameBlockAnchors = primaryCanonicalBlockId
    ? pageAnchors.filter((item) => {
      const blockId = String(item.canonical_block_id || item.anchor_v2?.canonical_block_id || '').trim()
      return blockId === primaryCanonicalBlockId
    })
    : []
  const textClusterAnchors = clusterAdjacentTextAnchorsForPreview(primary, pageAnchors)
  const previewAnchors = sameBlockAnchors.length > 1
    ? sameBlockAnchors
    : textClusterAnchors.length > 1
      ? textClusterAnchors
      : sameBlockAnchors.length > 0
        ? sameBlockAnchors
        : [primary]
  const previewAnchor = mergeAnchorsForPreview(primary, previewAnchors)
  return { previewAnchor, previewAnchors }
}

function buildPreviewKey(anchor: ReaderComponentSourceAnchor): string {
  const bbox = anchor.bbox_hint
  const bboxKey = bbox
    ? [bbox.x0, bbox.x1, bbox.top, bbox.bottom, bbox.page_width, bbox.page_height].map((n) => Number(n || 0)).join(':')
    : 'none'
  const geometry = anchor.geometry
  const polygonKey = geometry?.polygons
    ? geometry.polygons
      .map((poly) => (Array.isArray(poly?.points) ? poly.points : [])
        .map((pt) => `${Number((pt as any)?.x || 0).toFixed(2)},${Number((pt as any)?.y || 0).toFixed(2)}`)
        .join(';'))
      .join('|')
    : 'none'
  return [
    Number(anchor.page || 0),
    Number(anchor.start_char || 0),
    Number(anchor.end_char || 0),
    String(anchor.canonical_block_id || anchor.anchor_v2?.canonical_block_id || ''),
    String(anchor.coord_version || anchor.anchor_v2?.coord_version || ''),
    String(anchor.anchor_id || ''),
    Number(anchor.segment_index || 0),
    Number(anchor.segment_total || 0),
    normalizeAnchorMatchText(String(anchor.quote_text || '')).slice(0, 320),
    bboxKey,
    polygonKey.slice(0, 1024),
  ].join('|')
}

function normalizeSourceBlockIds(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item || '').trim()).filter(Boolean)
}

function inferPageFromCanonicalBlockId(blockId: string): number | null {
  const token = String(blockId || '').trim()
  if (!token) return null
  const match = token.match(/(?:^|_)p(\d+)(?:_|$)/i)
  if (!match) return null
  const value = Number(match[1] || 0)
  return Number.isFinite(value) && value > 0 ? value : null
}

function canonicalizePageStructureBlockId(blockId: string): string {
  const token = String(blockId || '').trim()
  if (!token) return ''
  if (/^p\d+_/i.test(token)) return token
  const page = inferPageFromCanonicalBlockId(token)
  if (page && !token.startsWith(`p${page}_`)) {
    return `p${page}_${token}`
  }
  return token
}

function normalizeSpatialPolygon(value: unknown): Array<{ x: number; y: number }> {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => ({
      x: Number((item as any)?.x || 0),
      y: Number((item as any)?.y || 0),
    }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
}

function collectAnchorSpatialDimensions(
  node: unknown,
  state: { pageWidth: number; pageHeight: number },
): void {
  if (!node || typeof node !== 'object') return
  const row = node as Record<string, unknown>
  const anchors = Array.isArray(row.source_anchor_refs) ? row.source_anchor_refs : []
  for (const rawAnchor of anchors) {
    if (!rawAnchor || typeof rawAnchor !== 'object') continue
    const anchor = rawAnchor as Record<string, unknown>
    const bbox = (anchor.bbox_hint && typeof anchor.bbox_hint === 'object')
      ? anchor.bbox_hint as Record<string, unknown>
      : {}
    const geometry = (anchor.geometry && typeof anchor.geometry === 'object')
      ? anchor.geometry as Record<string, unknown>
      : {}
    const bboxWidth = Number(bbox.page_width || 0)
    const bboxHeight = Number(bbox.page_height || 0)
    const geometryWidth = Number(geometry.page_width || 0)
    const geometryHeight = Number(geometry.page_height || 0)
    if (Number.isFinite(bboxWidth) && bboxWidth > state.pageWidth) state.pageWidth = bboxWidth
    if (Number.isFinite(bboxHeight) && bboxHeight > state.pageHeight) state.pageHeight = bboxHeight
    if (Number.isFinite(geometryWidth) && geometryWidth > state.pageWidth) state.pageWidth = geometryWidth
    if (Number.isFinite(geometryHeight) && geometryHeight > state.pageHeight) state.pageHeight = geometryHeight
  }
  const children = Array.isArray(row.children) ? row.children : []
  for (const child of children) {
    collectAnchorSpatialDimensions(child, state)
  }
}

function inferPageStructureSpatialDimensions(
  payload: Record<string, unknown> | null | undefined,
): PageStructureSpatialDimensions {
  const state = { pageWidth: 0, pageHeight: 0 }
  const uiPlan = (payload?.ui_plan && typeof payload.ui_plan === 'object')
    ? payload.ui_plan as Record<string, unknown>
    : {}
  const components = Array.isArray(uiPlan.components) ? uiPlan.components : []
  for (const node of components) {
    collectAnchorSpatialDimensions(node, state)
  }
  return {
    pageWidth: state.pageWidth > 0 ? state.pageWidth : undefined,
    pageHeight: state.pageHeight > 0 ? state.pageHeight : undefined,
  }
}

function buildPageStructureSpatialIndex(
  pageStructure: Record<string, unknown> | null | undefined,
  spatialDimensions?: PageStructureSpatialDimensions,
  grounding?: ReaderPageGrounding | null,
): {
  pageWidth: number
  pageHeight: number
  blockMap: Record<string, PageStructureBlockSpatialRow>
  layoutMap: Record<string, PageStructureLayoutSpatialRow>
} {
  const rows = Array.isArray(pageStructure?.block_groups) ? pageStructure!.block_groups : []
  const blockMap: Record<string, PageStructureBlockSpatialRow> = {}
  const layoutMap: Record<string, PageStructureLayoutSpatialRow> = {}
  let pageWidth = 0
  let pageHeight = 0
  for (const raw of rows) {
    if (!raw || typeof raw !== 'object') continue
    const row = raw as Record<string, unknown>
    const blockId = String(row.block_id || '').trim()
    if (!blockId) continue
    const layout = (row.layout_bbox_or_polygon && typeof row.layout_bbox_or_polygon === 'object')
      ? row.layout_bbox_or_polygon as Record<string, unknown>
      : {}
    const bboxRaw = (layout.bbox && typeof layout.bbox === 'object') ? layout.bbox as Record<string, unknown> : {}
    const x0 = Number(bboxRaw.x0 || 0)
    const x1 = Number(bboxRaw.x1 || 0)
    const top = Number(bboxRaw.top || 0)
    const bottom = Number(bboxRaw.bottom || 0)
    const bbox = Number.isFinite(x0) && Number.isFinite(x1) && Number.isFinite(top) && Number.isFinite(bottom) && x1 > x0 && bottom > top
      ? {
        x0,
        x1,
        top,
        bottom,
        page_width: undefined,
        page_height: undefined,
      }
      : undefined
    const polygon = normalizeSpatialPolygon(layout.polygon)
    if (bbox) {
      pageWidth = Math.max(pageWidth, Number(bbox.x1 || 0))
      pageHeight = Math.max(pageHeight, Number(bbox.bottom || 0))
    }
    for (const point of polygon) {
      pageWidth = Math.max(pageWidth, Number(point.x || 0))
      pageHeight = Math.max(pageHeight, Number(point.y || 0))
    }
    const spatialRow: PageStructureBlockSpatialRow = {
      blockId,
      text: String(row.text || '').trim(),
      bbox,
      polygon: polygon.length >= 3 ? polygon : undefined,
    }
    blockMap[blockId] = spatialRow
    const canonicalBlockId = canonicalizePageStructureBlockId(blockId)
    if (canonicalBlockId && canonicalBlockId !== blockId) {
      blockMap[canonicalBlockId] = spatialRow
    }
  }
  const groundingEvidenceRows = Array.isArray(grounding?.evidence_map) ? grounding.evidence_map : []
  const groundingAtomRows = Array.isArray(grounding?.layout_atoms) ? grounding.layout_atoms : []
  const atomTextMap: Record<string, string> = {}
  for (const atom of groundingAtomRows) {
    const layoutId = String(atom?.layout_id || '').trim()
    if (!layoutId) continue
    atomTextMap[layoutId] = String(atom?.clean_text || atom?.raw_text || '').trim()
  }
  for (const entry of groundingEvidenceRows) {
    const layoutId = String(entry?.source_layout_id || '').trim()
    if (!layoutId) continue
    const polygons = Array.isArray(entry?.block_positions)
      ? entry.block_positions
        .map((poly) => normalizeSpatialPolygon(poly))
        .filter((poly) => poly.length >= 3)
      : []
    let bbox: PageStructureLayoutSpatialRow['bbox']
    if (polygons.length > 0) {
      const flat = polygons.flatMap((poly) => poly)
      const xs = flat.map((point) => Number(point.x || 0))
      const ys = flat.map((point) => Number(point.y || 0))
      const x0 = Math.min(...xs)
      const x1 = Math.max(...xs)
      const top = Math.min(...ys)
      const bottom = Math.max(...ys)
      if (Number.isFinite(x0) && Number.isFinite(x1) && Number.isFinite(top) && Number.isFinite(bottom) && x1 > x0 && bottom > top) {
        bbox = { x0, x1, top, bottom, page_width: undefined, page_height: undefined }
      }
      for (const point of flat) {
        pageWidth = Math.max(pageWidth, Number(point.x || 0))
        pageHeight = Math.max(pageHeight, Number(point.y || 0))
      }
    } else {
      const layoutPolygon = normalizeSpatialPolygon(entry?.layout_pos)
      if (layoutPolygon.length >= 3) {
        const xs = layoutPolygon.map((point) => Number(point.x || 0))
        const ys = layoutPolygon.map((point) => Number(point.y || 0))
        const x0 = Math.min(...xs)
        const x1 = Math.max(...xs)
        const top = Math.min(...ys)
        const bottom = Math.max(...ys)
        if (Number.isFinite(x0) && Number.isFinite(x1) && Number.isFinite(top) && Number.isFinite(bottom) && x1 > x0 && bottom > top) {
          bbox = { x0, x1, top, bottom, page_width: undefined, page_height: undefined }
        }
        for (const point of layoutPolygon) {
          pageWidth = Math.max(pageWidth, Number(point.x || 0))
          pageHeight = Math.max(pageHeight, Number(point.y || 0))
        }
      }
    }
    layoutMap[layoutId] = {
      layoutId,
      text: atomTextMap[layoutId] || '',
      bbox,
      polygons: polygons.length > 0
        ? polygons
        : (() => {
          const layoutPolygon = normalizeSpatialPolygon(entry?.layout_pos)
          return layoutPolygon.length >= 3 ? [layoutPolygon] : []
        })(),
    }
  }
  const resolvedPageWidth = Number(spatialDimensions?.pageWidth || 0) > 0
    ? Number(spatialDimensions?.pageWidth || 0)
    : pageWidth
  const resolvedPageHeight = Number(spatialDimensions?.pageHeight || 0) > 0
    ? Number(spatialDimensions?.pageHeight || 0)
    : pageHeight
  if (resolvedPageWidth > 0 || resolvedPageHeight > 0) {
    for (const row of Object.values(blockMap)) {
      if (row.bbox) {
        row.bbox = {
          ...row.bbox,
          page_width: resolvedPageWidth || undefined,
          page_height: resolvedPageHeight || undefined,
        }
      }
    }
    for (const row of Object.values(layoutMap)) {
      if (row.bbox) {
        row.bbox = {
          ...row.bbox,
          page_width: resolvedPageWidth || undefined,
          page_height: resolvedPageHeight || undefined,
        }
      }
    }
  }
  return { pageWidth: resolvedPageWidth, pageHeight: resolvedPageHeight, blockMap, layoutMap }
}

function buildAnchorFromPageStructureBlocks(params: {
  anchors: ReaderComponentSourceAnchor[]
  sourceBlockIds?: string[]
  sourceAtomIds?: string[]
  preferredPage?: number
  pageStructureIndex: {
    pageWidth: number
    pageHeight: number
    blockMap: Record<string, PageStructureBlockSpatialRow>
    layoutMap: Record<string, PageStructureLayoutSpatialRow>
  }
}): { previewAnchor: ReaderComponentSourceAnchor; previewAnchors: ReaderComponentSourceAnchor[] } | null {
  const { anchors, sourceBlockIds, sourceAtomIds, preferredPage, pageStructureIndex } = params
  const blockMap = pageStructureIndex.blockMap || {}
  const layoutMap = pageStructureIndex.layoutMap || {}
  const normalizedLayoutIds = [
    ...((Array.isArray(sourceAtomIds) ? sourceAtomIds : []).map((item) => String(item || '').trim()).filter(Boolean)),
    ...((Array.isArray(anchors) ? anchors : [])
      .map((item) => String(item?.source_layout_id || '').trim())
      .filter(Boolean)),
  ]
  const uniqueLayoutIds = Array.from(new Set(normalizedLayoutIds))
  const layoutRows = uniqueLayoutIds
    .map((layoutId) => layoutMap[String(layoutId || '').trim()])
    .filter((item): item is PageStructureLayoutSpatialRow => Boolean(item) && (!!item.bbox || (Array.isArray(item.polygons) && item.polygons.length > 0)))

  const normalizedIds = [
    ...normalizeSourceBlockIds(sourceBlockIds),
    ...((Array.isArray(anchors) ? anchors : [])
      .map((item) => String(item?.canonical_block_id || item?.anchor_v2?.canonical_block_id || '').trim())
      .filter(Boolean)),
  ]
  const uniqueIds = Array.from(new Set(normalizedIds))
  const spatialRows = uniqueIds
    .map((blockId) => blockMap[String(blockId || '').trim()])
    .filter((item): item is PageStructureBlockSpatialRow => Boolean(item) && (!!item.bbox || !!item.polygon))
  if (layoutRows.length === 0 && spatialRows.length === 0) return null

  const page = (() => {
    const preferred = Number(preferredPage || 0)
    if (preferred > 0) return preferred
    const anchorPage = Number((anchors || [])[0]?.page || 0)
    if (anchorPage > 0) return anchorPage
    if (layoutRows.length > 0) return 1
    for (const row of spatialRows) {
      const inferred = inferPageFromCanonicalBlockId(row.blockId)
      if (inferred) return inferred
    }
    return 1
  })()

  if (layoutRows.length > 0) {
    const bboxRows = layoutRows
      .map((item) => item.bbox)
      .filter((item): item is NonNullable<PageStructureLayoutSpatialRow['bbox']> => Boolean(item))
    const mergedBbox = bboxRows.length > 0
      ? {
        x0: Math.min(...bboxRows.map((item) => Number(item.x0 || 0))),
        x1: Math.max(...bboxRows.map((item) => Number(item.x1 || 0))),
        top: Math.min(...bboxRows.map((item) => Number(item.top || 0))),
        bottom: Math.max(...bboxRows.map((item) => Number(item.bottom || 0))),
        page_width: pageStructureIndex.pageWidth || bboxRows[0]?.page_width || undefined,
        page_height: pageStructureIndex.pageHeight || bboxRows[0]?.page_height || undefined,
      }
      : undefined
    const polygons = layoutRows.flatMap((item) => (
      Array.isArray(item.polygons) && item.polygons.length > 0
        ? item.polygons
          .filter((poly) => Array.isArray(poly) && poly.length >= 3)
          .map((poly, idx) => ({ points: poly, source: 'page_grounding_v1', component_id: `${item.layoutId}:${idx + 1}` }))
        : item.bbox
          ? [{
            points: [
              { x: Number(item.bbox.x0 || 0), y: Number(item.bbox.top || 0) },
              { x: Number(item.bbox.x1 || 0), y: Number(item.bbox.top || 0) },
              { x: Number(item.bbox.x1 || 0), y: Number(item.bbox.bottom || 0) },
              { x: Number(item.bbox.x0 || 0), y: Number(item.bbox.bottom || 0) },
            ],
            source: 'page_grounding_v1',
            component_id: item.layoutId,
          }]
          : []
    ))
    const quoteText = layoutRows
      .map((item) => String(item.text || '').trim())
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
    const startChar = 0
    const endChar = Math.max(startChar + 1, quoteText.length || uniqueLayoutIds.length)
    const maxConfidence = Array.isArray(anchors) && anchors.length > 0
      ? Math.max(...anchors.map((item) => Number(item.anchor_confidence || 0)), 0.98)
      : 0.98
    const previewAnchor: ReaderComponentSourceAnchor = {
      ...(Array.isArray(anchors) && anchors.length > 0 ? anchors[0] : {
        page,
        start_char: startChar,
        end_char: endChar,
      }),
      page,
      start_char: startChar,
      end_char: endChar,
      canonical_block_id: String((Array.isArray(anchors) && anchors[0]?.canonical_block_id) || ''),
      source_layout_id: uniqueLayoutIds[0],
      quote: quoteText || undefined,
      quote_text: quoteText || undefined,
      anchor_id: `page_grounding_v1:${uniqueLayoutIds.join(',')}`,
      anchor_confidence: maxConfidence,
      bbox_hint: mergedBbox,
      geometry_version: polygons.length > 0 ? 'poly_v1' : undefined,
      geometry: polygons.length > 0
        ? {
          polygons,
          page_width: pageStructureIndex.pageWidth || undefined,
          page_height: pageStructureIndex.pageHeight || undefined,
        }
        : undefined,
      segment_index: 0,
      segment_total: uniqueLayoutIds.length,
      anchor_v2: {
        coord_version: 'layout_uid_v1',
        canonical_block_id: String((Array.isArray(anchors) && anchors[0]?.canonical_block_id) || uniqueLayoutIds[0] || ''),
        page,
        start_char: startChar,
        end_char: endChar,
      },
      source_word_ids: [],
      source_char_ranges: [],
    }
    return {
      previewAnchor,
      previewAnchors: [previewAnchor],
    }
  }

  const bboxRows = spatialRows
    .map((item) => item.bbox)
    .filter((item): item is NonNullable<PageStructureBlockSpatialRow['bbox']> => Boolean(item))
  const mergedBbox = bboxRows.length > 0
    ? {
      x0: Math.min(...bboxRows.map((item) => Number(item.x0 || 0))),
      x1: Math.max(...bboxRows.map((item) => Number(item.x1 || 0))),
      top: Math.min(...bboxRows.map((item) => Number(item.top || 0))),
      bottom: Math.max(...bboxRows.map((item) => Number(item.bottom || 0))),
      page_width: pageStructureIndex.pageWidth || bboxRows[0]?.page_width || undefined,
      page_height: pageStructureIndex.pageHeight || bboxRows[0]?.page_height || undefined,
    }
    : undefined

  const polygons = spatialRows.flatMap((item) => (
    Array.isArray(item.polygon) && item.polygon.length >= 3
      ? [{ points: item.polygon, source: 'page_structure_v3', component_id: item.blockId }]
      : item.bbox
        ? [{
          points: [
            { x: Number(item.bbox.x0 || 0), y: Number(item.bbox.top || 0) },
            { x: Number(item.bbox.x1 || 0), y: Number(item.bbox.top || 0) },
            { x: Number(item.bbox.x1 || 0), y: Number(item.bbox.bottom || 0) },
            { x: Number(item.bbox.x0 || 0), y: Number(item.bbox.bottom || 0) },
          ],
          source: 'page_structure_v3',
          component_id: item.blockId,
        }]
        : []
  ))

  const startChar = (Array.isArray(anchors) && anchors.length > 0)
    ? Math.min(...anchors.map((item) => Number(item.start_char || 0)))
    : 0
  const endChar = (Array.isArray(anchors) && anchors.length > 0)
    ? Math.max(...anchors.map((item) => Number(item.end_char || 0)))
    : Math.max(startChar + 1, startChar + spatialRows.reduce((acc, item) => acc + Math.max(1, String(item.text || '').length), 0))
  const quoteText = spatialRows
    .map((item) => String(item.text || '').trim())
    .filter(Boolean)
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim()
  const maxConfidence = Array.isArray(anchors) && anchors.length > 0
    ? Math.max(...anchors.map((item) => Number(item.anchor_confidence || 0)), 0.92)
    : 0.92

  const previewAnchor: ReaderComponentSourceAnchor = {
    ...(Array.isArray(anchors) && anchors.length > 0 ? anchors[0] : {
      page,
      start_char: startChar,
      end_char: Math.max(startChar + 1, endChar),
    }),
    page,
    start_char: startChar,
    end_char: Math.max(startChar + 1, endChar),
    canonical_block_id: uniqueIds[0],
    quote: quoteText || undefined,
    quote_text: quoteText || undefined,
    anchor_id: `page_structure_v3:${uniqueIds.join(',')}`,
    anchor_confidence: maxConfidence,
    bbox_hint: mergedBbox,
    geometry_version: polygons.length > 0 ? 'poly_v1' : undefined,
    geometry: polygons.length > 0
      ? {
        polygons,
        page_width: pageStructureIndex.pageWidth || undefined,
        page_height: pageStructureIndex.pageHeight || undefined,
      }
      : undefined,
    segment_index: 0,
    segment_total: uniqueIds.length,
    anchor_v2: {
      coord_version: 'anchor_v2',
      canonical_block_id: uniqueIds[0],
      page,
      start_char: startChar,
      end_char: Math.max(startChar + 1, endChar),
    },
    source_word_ids: [],
    source_char_ranges: [],
  }
  return {
    previewAnchor,
    previewAnchors: [previewAnchor],
  }
}

function clampRect(
  rect: { x: number; y: number; width: number; height: number },
  canvasWidth: number,
  canvasHeight: number,
): { x: number; y: number; width: number; height: number } | null {
  const x = Math.max(0, Math.min(canvasWidth - 1, rect.x))
  const y = Math.max(0, Math.min(canvasHeight - 1, rect.y))
  const maxWidth = Math.max(1, canvasWidth - x)
  const maxHeight = Math.max(1, canvasHeight - y)
  const width = Math.max(1, Math.min(maxWidth, rect.width))
  const height = Math.max(1, Math.min(maxHeight, rect.height))
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(width) || !Number.isFinite(height)) return null
  return { x, y, width, height }
}

type RenderPolygonPoint = { x: number; y: number }
type RenderPolygon = { points: RenderPolygonPoint[] }

function isPageStructurePreviewAnchor(anchor: ReaderComponentSourceAnchor): boolean {
  return String(anchor.anchor_id || '').startsWith('page_structure_v3:')
}

function buildPolygonBounds(polygons: RenderPolygon[]): { x: number; y: number; width: number; height: number } | null {
  const points = polygons.flatMap((poly) => poly.points)
  if (points.length < 3) return null
  const xs = points.map((p) => p.x)
  const ys = points.map((p) => p.y)
  const x0 = Math.min(...xs)
  const x1 = Math.max(...xs)
  const y0 = Math.min(...ys)
  const y1 = Math.max(...ys)
  if (!Number.isFinite(x0) || !Number.isFinite(x1) || !Number.isFinite(y0) || !Number.isFinite(y1)) return null
  if (x1 <= x0 || y1 <= y0) return null
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }
}

function hasPlausibleSpatialBounds(
  x0: number,
  x1: number,
  top: number,
  bottom: number,
  sourceWidth: number,
  sourceHeight: number,
): boolean {
  if (![x0, x1, top, bottom].every((value) => Number.isFinite(value))) return false
  if (x1 <= x0 || bottom <= top) return false
  if (sourceWidth > 0) {
    const toleranceX = Math.max(24, sourceWidth * 0.08)
    if (x0 < -toleranceX || x1 > sourceWidth + toleranceX) return false
  }
  if (sourceHeight > 0) {
    const toleranceY = Math.max(24, sourceHeight * 0.08)
    if (top < -toleranceY || bottom > sourceHeight + toleranceY) return false
  }
  return true
}

function resolvePolygonsFromGeometry(
  anchor: ReaderComponentSourceAnchor,
  viewportWidth: number,
  viewportHeight: number,
  renderScale: number,
): { polygons: RenderPolygon[]; rect: { x: number; y: number; width: number; height: number }; confidence: number } | null {
  const geometry = anchor.geometry
  if (!geometry || !Array.isArray(geometry.polygons) || geometry.polygons.length === 0) return null
  const sourceWidth = Number(geometry.page_width || 0) > 0 ? Number(geometry.page_width) : viewportWidth / renderScale
  const sourceHeight = Number(geometry.page_height || 0) > 0 ? Number(geometry.page_height) : viewportHeight / renderScale
  const scaleX = sourceWidth > 0 ? viewportWidth / sourceWidth : 1
  const scaleY = sourceHeight > 0 ? viewportHeight / sourceHeight : 1
  const polygons: RenderPolygon[] = []

  for (const poly of geometry.polygons) {
    const rawPoints = Array.isArray(poly?.points) ? poly.points : []
    const rawXs = rawPoints.map((row) => Number((row as any)?.x || 0)).filter((value) => Number.isFinite(value))
    const rawYs = rawPoints.map((row) => Number((row as any)?.y || 0)).filter((value) => Number.isFinite(value))
    if (rawXs.length >= 3 && rawYs.length >= 3) {
      const polyX0 = Math.min(...rawXs)
      const polyX1 = Math.max(...rawXs)
      const polyY0 = Math.min(...rawYs)
      const polyY1 = Math.max(...rawYs)
      if (!hasPlausibleSpatialBounds(polyX0, polyX1, polyY0, polyY1, sourceWidth, sourceHeight)) {
        continue
      }
    }
    const points: RenderPolygonPoint[] = rawPoints
      .map((row) => ({ x: Number((row as any)?.x || 0) * scaleX, y: Number((row as any)?.y || 0) * scaleY }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
      .map((p) => ({
        x: Math.max(0, Math.min(viewportWidth - 1, p.x)),
        y: Math.max(0, Math.min(viewportHeight - 1, p.y)),
      }))
    if (points.length < 3) continue
    polygons.push({ points })
  }
  if (polygons.length === 0) return null
  const rect = buildPolygonBounds(polygons)
  if (!rect) return null
  const clampedRect = clampRect(rect, viewportWidth, viewportHeight)
  if (!clampedRect) return null
  return { polygons, rect: clampedRect, confidence: 0.94 }
}

function resolveRectFromBboxHint(
  anchor: ReaderComponentSourceAnchor,
  viewportWidth: number,
  viewportHeight: number,
  renderScale: number,
): { rect: { x: number; y: number; width: number; height: number }; confidence: number } | null {
  const hint = anchor.bbox_hint
  if (!hint) return null
  const x0 = Number(hint.x0)
  const x1 = Number(hint.x1)
  const top = Number(hint.top)
  const bottom = Number(hint.bottom)
  if (![x0, x1, top, bottom].every((n) => Number.isFinite(n))) return null
  if (x1 <= x0 || bottom <= top) return null
  const sourceWidth = Number(hint.page_width || 0) > 0 ? Number(hint.page_width) : viewportWidth / renderScale
  const sourceHeight = Number(hint.page_height || 0) > 0 ? Number(hint.page_height) : viewportHeight / renderScale
  if (!hasPlausibleSpatialBounds(x0, x1, top, bottom, sourceWidth, sourceHeight)) return null
  const scaleX = sourceWidth > 0 ? viewportWidth / sourceWidth : 1
  const scaleY = sourceHeight > 0 ? viewportHeight / sourceHeight : 1
  const rect = clampRect(
    {
      x: x0 * scaleX,
      y: top * scaleY,
      width: Math.max(2, (x1 - x0) * scaleX),
      height: Math.max(2, (bottom - top) * scaleY),
    },
    viewportWidth,
    viewportHeight,
  )
  if (!rect) return null
  return { rect, confidence: 0.92 }
}

type AnchorMetricRow = {
  text: string
  lower: string
  start: number
  end: number
  x: number
  y: number
  width: number
  height: number
}

function buildTextMetricsForAnchor(
  items: PdfTextItemLike[],
  viewport: { width: number; height: number; transform?: number[] },
  renderScale: number,
): { merged: string; rows: AnchorMetricRow[] } {
  const rows: AnchorMetricRow[] = []
  const mergedParts: string[] = []
  const util = (pdfjs as any)?.Util
  let cursor = 0
  for (const item of items) {
    const text = String(item?.str || '').replace(/\s+/g, ' ').trim()
    if (!text) continue
    const transform = Array.isArray(item?.transform) ? item.transform as number[] : []
    let tx = Number(transform[4] || 0) * renderScale
    let topY = 0
    const width = Math.max(4, Number(item?.width || Math.max(6, text.length * 5)) * renderScale)
    let height = 10
    if (util && Array.isArray(viewport.transform) && transform.length >= 6) {
      try {
        const transformed = util.transform(viewport.transform, transform)
        tx = Number(transformed[4] || 0)
        const ty = Number(transformed[5] || 0)
        const fontHeight =
          Math.hypot(Number(transformed[2] || 0), Number(transformed[3] || 0))
          || Math.hypot(Number(transformed[0] || 0), Number(transformed[1] || 0))
          || 10
        height = Math.max(9, fontHeight)
        topY = Math.max(0, ty - height)
      } catch {
        const ty = Number(transform[5] || 0) * renderScale
        const rawHeight =
          Math.abs(Number(transform[3] || 0))
          || Math.abs(Number(transform[0] || 0))
          || 10
        height = Math.max(9, rawHeight * renderScale)
        topY = Math.max(0, viewport.height - ty - height)
      }
    } else {
      const ty = Number(transform[5] || 0) * renderScale
      const rawHeight =
        Math.abs(Number(transform[3] || 0))
        || Math.abs(Number(transform[0] || 0))
        || 10
      height = Math.max(9, rawHeight * renderScale)
      topY = Math.max(0, viewport.height - ty - height)
    }
    const start = cursor
    const end = start + text.length
    cursor = end + 1
    mergedParts.push(text)
    rows.push({
      text,
      lower: text.toLowerCase(),
      start,
      end,
      x: tx,
      y: topY,
      width,
      height,
    })
  }
  return { merged: mergedParts.join(' '), rows }
}

function buildRectFromMetricRows(
  rows: AnchorMetricRow[],
  viewportWidth: number,
  viewportHeight: number,
): { x: number; y: number; width: number; height: number } | null {
  if (!rows.length) return null
  const x0 = Math.min(...rows.map((row) => row.x))
  const x1 = Math.max(...rows.map((row) => row.x + row.width))
  const y0 = Math.min(...rows.map((row) => row.y))
  const y1 = Math.max(...rows.map((row) => row.y + row.height))
  return clampRect(
    {
      x: x0,
      y: y0,
      width: Math.max(2, x1 - x0),
      height: Math.max(2, y1 - y0),
    },
    viewportWidth,
    viewportHeight,
  )
}

function rectArea(rect: { x: number; y: number; width: number; height: number }): number {
  return Math.max(0, rect.width) * Math.max(0, rect.height)
}

function rectIoU(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): number {
  const x1 = Math.max(a.x, b.x)
  const y1 = Math.max(a.y, b.y)
  const x2 = Math.min(a.x + a.width, b.x + b.width)
  const y2 = Math.min(a.y + a.height, b.y + b.height)
  const interW = Math.max(0, x2 - x1)
  const interH = Math.max(0, y2 - y1)
  const inter = interW * interH
  if (inter <= 0) return 0
  const union = rectArea(a) + rectArea(b) - inter
  if (union <= 0) return 0
  return inter / union
}

function resolveRectFromTextMetrics(
  anchor: ReaderComponentSourceAnchor,
  metrics: { merged: string; rows: AnchorMetricRow[] },
  viewportWidth: number,
  viewportHeight: number,
): { rect: { x: number; y: number; width: number; height: number }; method: AnchorMatchMethod; confidence: number } | null {
  if (!metrics.rows.length) return null
  const mergedLower = normalizeAnchorMatchText(metrics.merged)
  const quote = normalizeAnchorMatchText(String(anchor.quote_text || ''))
  const hasQuote = quote.length > 0

  if (hasQuote) {
    const probeList = [
      quote,
      quote.length > 220 ? quote.slice(0, 220).trim() : '',
      quote.split(' ').slice(0, 20).join(' ').trim(),
    ]
      .map((item) => normalizeAnchorMatchText(item))
      .filter((item, idx, arr) => item.length >= 18 && arr.indexOf(item) === idx)

    for (const probe of probeList) {
      const quoteHits = findAllOccurrences(mergedLower, probe, 48)
      if (quoteHits.length === 0) continue
      const targetChar = Math.max(0, Number(anchor.start_char || 0))
      const quoteIdx =
        targetChar > 0
          ? quoteHits.reduce((best, current) => (
            Math.abs(current - targetChar) < Math.abs(best - targetChar) ? current : best
          ), quoteHits[0])
          : quoteHits[0]
      const quoteEnd = quoteIdx + probe.length
      const hitRows = metrics.rows.filter((row) => row.end > quoteIdx && row.start < quoteEnd)
      const rect = buildRectFromMetricRows(hitRows, viewportWidth, viewportHeight)
      if (rect) {
        const confidence = probe === quote ? 0.85 : 0.8
        return { rect, method: 'quote_exact', confidence }
      }
    }

    const quoteTokens = quote.split(' ').filter((item) => item.length >= 4)
    if (quoteTokens.length >= 2) {
      const scoredRows = metrics.rows
        .map((row) => {
          const tokenHits = quoteTokens.reduce((acc, token) => (row.lower.includes(token) ? acc + 1 : acc), 0)
          return { row, tokenHits }
        })
        .filter((item) => item.tokenHits > 0)
        .sort((a, b) => b.tokenHits - a.tokenHits)
      const pivot = scoredRows[0]?.row
      if (pivot) {
        const sameBandRows = scoredRows
          .map((item) => item.row)
          .filter((row) => (
            Math.abs(row.y - pivot.y) <= Math.max(56, pivot.height * 2.4)
            && Math.abs(row.x - pivot.x) <= Math.max(260, pivot.width * 1.2)
          ))
          .slice(0, 8)
        const rect = buildRectFromMetricRows(sameBandRows.length > 0 ? sameBandRows : [pivot], viewportWidth, viewportHeight)
        if (rect) {
          const ratio = rectArea(rect) / Math.max(1, viewportWidth * viewportHeight)
          if (ratio <= 0.34) return { rect, method: 'quote_fuzzy', confidence: 0.66 }
        }
      }
    }
  }

  // 当锚点包含可用 quote 但无法在当前页文本中对齐时，不再盲目退回 char_range，
  // 以免出现“定位成功但位置错误”的假阳性证据框。
  if (hasQuote && quote.length >= 24) {
    return null
  }

  const start = Math.max(0, Number(anchor.start_char || 0))
  const end = Math.max(start + 1, Number(anchor.end_char || start + 1))
  const span = end - start
  if (end > start) {
    const hitRows = metrics.rows.filter((row) => row.end > start && row.start < end)
    const maxRows = span > 5200 ? 96 : span > 2800 ? 68 : span > 1400 ? 42 : 26
    const compactRows = hitRows.slice(0, maxRows)
    const rect = buildRectFromMetricRows(compactRows, viewportWidth, viewportHeight)
    if (rect) {
      const ratio = rectArea(rect) / Math.max(1, viewportWidth * viewportHeight)
      if (ratio <= 0.68) {
        const confidence = span > 3200 ? 0.48 : 0.56
        return { rect, method: 'char_range', confidence }
      }
    }
  }

  return null
}

function buildAnchorPreviewSnippet(rawText: string, anchor: ReaderComponentSourceAnchor): string {
  const text = String(rawText || '')
  if (!text) return ''
  const quote = String(anchor.quote_text || '').replace(/\s+/g, ' ').trim()
  if (quote) {
    const lowerText = text.toLowerCase()
    const lowerQuote = quote.toLowerCase()
    const quoteHits = findAllOccurrences(lowerText, lowerQuote, 36)
    if (quoteHits.length > 0) {
      const targetChar = Math.max(0, Number(anchor.start_char || 0))
      const quoteStart =
        targetChar > 0
          ? quoteHits.reduce((best, current) => (
            Math.abs(current - targetChar) < Math.abs(best - targetChar) ? current : best
          ), quoteHits[0])
          : quoteHits[0]
      const quoteEnd = quoteStart + quote.length
      const previewStart = Math.max(0, quoteStart - 120)
      const previewEnd = Math.min(text.length, quoteEnd + 180)
      return text.slice(previewStart, previewEnd).replace(/\s+/g, ' ').trim()
    }
    // quote 无法在当前抽取文本精确命中时，直接展示 quote，避免回退到错误 char_range。
    return quote.length > 420 ? `${quote.slice(0, 420)}...` : quote
  }
  const start = Math.max(0, Math.min(text.length, Number(anchor.start_char || 0)))
  const end = Math.max(start + 1, Math.min(text.length, Number(anchor.end_char || start + 1)))
  const previewStart = Math.max(0, start - 120)
  const previewEnd = Math.min(text.length, end + 180)
  return text.slice(previewStart, previewEnd).replace(/\s+/g, ' ').trim()
}

async function renderAnchorEvidenceImage(
  pageProxy: any,
  textItems: PdfTextItemLike[],
  anchor: ReaderComponentSourceAnchor,
): Promise<{ imageDataUrl: string | null; matchMethod: AnchorMatchMethod; confidence: number; fallbackUsed: boolean }> {
  const renderScale = 2.4
  const viewport = pageProxy.getViewport({ scale: renderScale })
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.floor(viewport.width))
  canvas.height = Math.max(1, Math.floor(viewport.height))
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    return { imageDataUrl: null, matchMethod: 'fallback', confidence: 0.3, fallbackUsed: true }
  }
  await pageProxy.render({ canvasContext: ctx, viewport }).promise

  const strictPageStructurePreview = isPageStructurePreviewAnchor(anchor)
  const fromPolygons = resolvePolygonsFromGeometry(anchor, viewport.width, viewport.height, renderScale)
  const fromBbox = resolveRectFromBboxHint(anchor, viewport.width, viewport.height, renderScale)
  const metrics = strictPageStructurePreview
    ? null
    : buildTextMetricsForAnchor(textItems, viewport, renderScale)
  const fromText = strictPageStructurePreview || !metrics
    ? null
    : resolveRectFromTextMetrics(anchor, metrics, viewport.width, viewport.height)
  const pageArea = Math.max(1, viewport.width * viewport.height)
  const rawBboxCoverRatio = fromBbox ? (rectArea(fromBbox.rect) / pageArea) : 0
  const effectiveBbox = strictPageStructurePreview
    ? fromBbox
    : rawBboxCoverRatio >= 0.56
      ? null
      : fromBbox
  let polygonCandidate: RenderPolygon[] | null = fromPolygons?.polygons || null
  let rectCandidate = effectiveBbox
  let matchMethod: AnchorMatchMethod = 'fallback'
  let confidence = 0.3

  if (fromPolygons) {
    rectCandidate = { rect: fromPolygons.rect, confidence: fromPolygons.confidence }
    matchMethod = 'polygon'
    confidence = fromPolygons.confidence
  } else if (effectiveBbox && fromText) {
    const iou = rectIoU(effectiveBbox.rect, fromText.rect)
    const bboxCoverRatio = rectArea(effectiveBbox.rect) / pageArea
    const textCoverRatio = rectArea(fromText.rect) / pageArea
    const preferText = (
      fromText.method === 'quote_exact'
      && textCoverRatio <= 0.46
      && (
        iou < 0.18
        || bboxCoverRatio >= 0.44
        || fromText.confidence >= 0.84
      )
    )
    if (preferText) {
      rectCandidate = { rect: fromText.rect, confidence: fromText.confidence }
      matchMethod = fromText.method
      confidence = fromText.confidence
    } else {
      rectCandidate = effectiveBbox
      matchMethod = 'bbox_hint'
      confidence = effectiveBbox.confidence
    }
  } else if (effectiveBbox) {
    rectCandidate = effectiveBbox
    matchMethod = 'bbox_hint'
    confidence = effectiveBbox.confidence
  } else if (fromText) {
    rectCandidate = { rect: fromText.rect, confidence: fromText.confidence }
    matchMethod = fromText.method
    confidence = fromText.confidence
  }

  const fullRect = { x: 0, y: 0, width: viewport.width, height: viewport.height }
  const rect = rectCandidate?.rect || fullRect
  const padX = strictPageStructurePreview
    ? Math.max(12, rect.width * 0.06)
    : Math.max(18, rect.width * 0.2)
  const padY = strictPageStructurePreview
    ? Math.max(14, rect.height * 0.18)
    : Math.max(26, rect.height * 0.52)
  const minCropWidth = strictPageStructurePreview
    ? Math.min(viewport.width * 0.98, Math.max(rect.width + padX * 2, viewport.width * 0.26))
    : Math.min(viewport.width * 0.92, Math.max(rect.width + padX * 2, viewport.width * 0.42))
  const minCropHeight = strictPageStructurePreview
    ? Math.min(viewport.height * 0.96, Math.max(rect.height + padY * 2, viewport.height * 0.14))
    : Math.min(viewport.height * 0.78, Math.max(rect.height + padY * 2, viewport.height * 0.28))
  const targetWidth = Math.min(canvas.width, Math.max(1, minCropWidth))
  const targetHeight = Math.min(canvas.height, Math.max(1, minCropHeight))
  const centerX = rect.x + rect.width / 2
  const centerY = strictPageStructurePreview
    ? rect.y + rect.height / 2
    : rect.y + rect.height / 2 + Math.min(40, rect.height * 0.18)
  const cropRect = clampRect(
    {
      x: Math.max(0, Math.min(canvas.width - targetWidth, centerX - targetWidth / 2)),
      y: Math.max(0, Math.min(canvas.height - targetHeight, centerY - targetHeight / 2)),
      width: targetWidth,
      height: targetHeight,
    },
    canvas.width,
    canvas.height,
  ) || fullRect

  const cropRatio = rectArea(cropRect) / pageArea
  let finalCropRect = cropRect
  let fallbackUsed = false
  if (!strictPageStructurePreview && rectCandidate && cropRatio > 0.72 && confidence <= 0.72 && matchMethod !== 'bbox_hint') {
    finalCropRect = fullRect
    matchMethod = 'fallback'
    confidence = Math.max(0.42, confidence - 0.2)
    fallbackUsed = true
    polygonCandidate = null
  }

  const maxOutputWidth = 1360
  const outputScale = finalCropRect.width > maxOutputWidth ? maxOutputWidth / finalCropRect.width : 1
  const outputCanvas = document.createElement('canvas')
  outputCanvas.width = Math.max(1, Math.floor(finalCropRect.width * outputScale))
  outputCanvas.height = Math.max(1, Math.floor(finalCropRect.height * outputScale))
  const outputCtx = outputCanvas.getContext('2d')
  if (!outputCtx) {
    return { imageDataUrl: null, matchMethod: 'fallback', confidence: Math.min(confidence, 0.45), fallbackUsed: true }
  }
  outputCtx.drawImage(
    canvas,
    finalCropRect.x,
    finalCropRect.y,
    finalCropRect.width,
    finalCropRect.height,
    0,
    0,
    outputCanvas.width,
    outputCanvas.height,
  )

  if (polygonCandidate && polygonCandidate.length > 0 && matchMethod === 'polygon') {
    outputCtx.fillStyle = 'rgba(245, 158, 11, 0.20)'
    outputCtx.strokeStyle = 'rgba(245, 158, 11, 0.95)'
    outputCtx.lineWidth = Math.max(2, Math.round(2 * outputScale))
    for (const poly of polygonCandidate) {
      const points = poly.points
        .map((p) => ({
          x: (p.x - finalCropRect.x) * outputScale,
          y: (p.y - finalCropRect.y) * outputScale,
        }))
        .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
      if (points.length < 3) continue
      outputCtx.beginPath()
      outputCtx.moveTo(points[0].x, points[0].y)
      for (let i = 1; i < points.length; i += 1) {
        outputCtx.lineTo(points[i].x, points[i].y)
      }
      outputCtx.closePath()
      outputCtx.fill()
      outputCtx.stroke()
    }
  } else if (rectCandidate?.rect) {
    const hx = (rect.x - finalCropRect.x) * outputScale
    const hy = (rect.y - finalCropRect.y) * outputScale
    const hw = rect.width * outputScale
    const hh = rect.height * outputScale
    outputCtx.fillStyle = 'rgba(245, 158, 11, 0.20)'
    outputCtx.strokeStyle = 'rgba(245, 158, 11, 0.95)'
    outputCtx.lineWidth = Math.max(2, Math.round(2 * outputScale))
    outputCtx.fillRect(hx, hy, hw, hh)
    outputCtx.strokeRect(hx, hy, hw, hh)
  }

  let imageDataUrl: string | null = null
  try {
    imageDataUrl = outputCanvas.toDataURL('image/webp', 0.82)
  } catch {
    imageDataUrl = outputCanvas.toDataURL('image/png')
  }
  return { imageDataUrl, matchMethod, confidence, fallbackUsed }
}

export default function PaperReaderPage() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { paperId } = useParams<{ paperId: string }>()
  const parsedPaperId = Number(paperId)
  const validPaperId = Number.isFinite(parsedPaperId) && parsedPaperId > 0
  const requestedPage = parsePositiveSearchParam(searchParams.get('page'))
  const requestedKbId = parsePositiveSearchParam(searchParams.get('kb'))
  const requestedComposePipelineVersion = normalizeComposePipelineVersion(searchParams.get('compose'))
  const effectiveComposePipelineVersion = requestedComposePipelineVersion || 'layout_uid_v1'

  const [loading, setLoading] = useState<boolean>(true)
  const [paper, setPaper] = useState<Paper | null>(null)
  const [readerSession, setReaderSession] = useState<ReaderSession | null>(null)
  const [annotations, setAnnotations] = useState<PaperAnnotation[]>([])
  const [comments, setComments] = useState<PaperComment[]>([])
  const [commentFilter, setCommentFilter] = useState<CommentFilter>('all')
  const [ratingSummary, setRatingSummary] = useState<PaperRatingSummary | null>(null)
  const [knowledgeLinks, setKnowledgeLinks] = useState<PaperKnowledgeLink[]>([])
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [collections, setCollections] = useState<PaperCollection[]>([])
  const [selectedKbId, setSelectedKbId] = useState<number | undefined>(undefined)

  const [annotationPage, setAnnotationPage] = useState<number>(1)
  const [annotationContent, setAnnotationContent] = useState<string>('')
  const [annotationType, setAnnotationType] = useState<AnnotationType>('note')
  const [editingAnnotationId, setEditingAnnotationId] = useState<number | null>(null)
  const [annotationSubmitting, setAnnotationSubmitting] = useState<boolean>(false)
  const [deletingAnnotationId, setDeletingAnnotationId] = useState<number | null>(null)

  const [commentText, setCommentText] = useState<string>('')

  const [askScope, setAskScope] = useState<LiteratureAskScope>('paper')
  const [askMode, setAskMode] = useState<'agentic' | 'classic'>('agentic')
  const [askCollectionId, setAskCollectionId] = useState<number | undefined>(undefined)
  const [askQuestion, setAskQuestion] = useState<string>('')
  const [askAnswer, setAskAnswer] = useState<string>('')
  const [askSources, setAskSources] = useState<LiteratureAskSource[]>([])
  const [askSessionId, setAskSessionId] = useState<number | undefined>(undefined)
  const [askSessions, setAskSessions] = useState<LiteratureAskSession[]>([])
  const [askMessages, setAskMessages] = useState<LiteratureAskMessage[]>([])
  const [collectionReadiness, setCollectionReadiness] = useState<CollectionKnowledgeReadiness | null>(null)
  const [collectionReadinessLoading, setCollectionReadinessLoading] = useState<boolean>(false)
  const [asking, setAsking] = useState<boolean>(false)

  const [readPage, setReadPage] = useState<number>(1)
  const [zoomPercent, setZoomPercent] = useState<number>(120)
  const [fitWidth, setFitWidth] = useState<boolean>(true)
  const [textMode, setTextMode] = useState<boolean>(false)
  const [workspaceTab, setWorkspaceTab] = useState<string>('annotation')
  const [readerAutoSaveStatus, setReaderAutoSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [readerAutoSaveAt, setReaderAutoSaveAt] = useState<string>('')
  const [readerAutoSaveError, setReaderAutoSaveError] = useState<string>('')
  const [pendingSectionJump, setPendingSectionJump] = useState<PendingSectionJump | null>(null)
  const [sectionJumpHighlightIndex, setSectionJumpHighlightIndex] = useState<number | null>(null)
  const [sectionLocating, setSectionLocating] = useState<boolean>(false)

  const [pdfSource, setPdfSource] = useState<string | undefined>(undefined)
  const [pdfLoading, setPdfLoading] = useState<boolean>(false)
  const [pdfNumPages, setPdfNumPages] = useState<number>(0)
  const [pdfDoc, setPdfDoc] = useState<any>(null)
  const [pageText, setPageText] = useState<string>('')
  const [rawPageText, setRawPageText] = useState<string>('')
  const [generativeLoading, setGenerativeLoading] = useState<boolean>(false)
  const [generativeError, setGenerativeError] = useState<string>('')
  const [generativeSections, setGenerativeSections] = useState<ReaderGenerativeSection[]>([])
  const [generativeBlocks, setGenerativeBlocks] = useState<ReaderGenerativeBlock[]>([])
  const [generativeAssets, setGenerativeAssets] = useState<ReaderGenerativeAsset[]>([])
  const [generativeSummary, setGenerativeSummary] = useState<string>('')
  const [generativePayload, setGenerativePayload] = useState<ReaderGenerativePagePayload | null>(null)
  const [generativeStyleKey, setGenerativeStyleKey] = useState<ReaderGenerativeStyleKey>('journal_classic')
  const [themeMode, setThemeMode] = useState<ReaderThemeMode>('light')
  const [detailLevel, setDetailLevel] = useState<ReaderDetailLevel>('standard')
  const [compareMode, setCompareMode] = useState<boolean>(false)
  const [citationTldr, setCitationTldr] = useState<boolean>(false)
  const [generativeStyleTuning, setGenerativeStyleTuning] = useState<ReaderGenerativeStyleTuning>(
    DEFAULT_READER_STYLE_TUNING,
  )
  const [generativeCacheLabel, setGenerativeCacheLabel] = useState<string>('')
  const [composedLoading, setComposedLoading] = useState<boolean>(false)
  const [composedError, setComposedError] = useState<string>('')
  const [composedPlan, setComposedPlan] = useState<ReaderUIPlan | null>(null)
  const [composedAssets, setComposedAssets] = useState<ReaderComposeAsset[]>([])
  const [composedPayload, setComposedPayload] = useState<ReaderComposePayload | null>(null)
  const [composedQuality, setComposedQuality] = useState<ReaderComposeQualityReport | null>(null)
  const [composedCacheLabel, setComposedCacheLabel] = useState<string>('')
  const [composeMaxIterations, setComposeMaxIterations] = useState<number>(DEFAULT_COMPOSE_MAX_ITERATIONS)
  const [composedRunSeed, setComposedRunSeed] = useState<number>(0)
  const [inlineQueryLoadingNodeId, setInlineQueryLoadingNodeId] = useState<string | null>(null)
  const [anchorPreview, setAnchorPreview] = useState<AnchorPreviewState>({
    visible: false,
    pinned: false,
    loading: false,
    preview_key: '',
    page: 0,
    text: '',
    title: '',
    anchors: [],
    anchor_index: 0,
    anchor_count: 0,
    image_data_url: null,
    match_method: 'fallback',
    match_confidence: 0,
    fallback_used: false,
  })

  const viewerRef = useRef<HTMLDivElement | null>(null)
  const textModeContainerRef = useRef<HTMLDivElement | null>(null)
  const lastTextModeRef = useRef<boolean>(false)
  const headingRefMap = useRef<Map<number, HTMLDivElement>>(new Map())
  const sectionPageCacheRef = useRef<Map<string, number>>(new Map())
  const generativeStreamControllerRef = useRef<AbortController | null>(null)
  const composedStreamControllerRef = useRef<AbortController | null>(null)
  const pendingComposedRunRef = useRef<PendingComposedRun>({
    regenerate: false,
    applyCurrentOptions: false,
  })
  const composedAppliedOptionsRef = useRef<ComposedBackendOptions>({
    detailLevel: 'standard',
    compareMode: false,
    citationTldr: false,
  })
  const inlineQueryStreamControllerRef = useRef<AbortController | null>(null)
  const annotationInputRef = useRef<any>(null)
  const anchorPreviewCacheRef = useRef<Map<string, {
    text: string
    imageDataUrl: string | null
    matchMethod: AnchorMatchMethod
    confidence: number
    fallbackUsed: boolean
  }>>(new Map())
  const prefetchedPagesRef = useRef<Set<number>>(new Set())
  const prefetchInFlightPagesRef = useRef<Set<number>>(new Set())
  const [viewerWidth, setViewerWidth] = useState<number>(860)
  const pdfObjectUrlRef = useRef<string | null>(null)
  const lastUrlSyncedReadPageRef = useRef<number | undefined>(undefined)
  const readerSessionHydratedRef = useRef<boolean>(false)
  const lastSavedReaderSignatureRef = useRef<string>('')
  const currentUserId = useMemo(() => getCurrentUserIdFromAuthStorage(), [])

  const readerCacheKey = useMemo(() => {
    if (!validPaperId) return undefined
    const userId = currentUserId ?? 0
    return `lit:reader:${userId}:${parsedPaperId}`
  }, [currentUserId, parsedPaperId, validPaperId])
  const annotationDraftKey = useMemo(() => {
    if (!validPaperId) return undefined
    const userId = currentUserId ?? 0
    return `lit:annotation:draft:${userId}:${parsedPaperId}`
  }, [currentUserId, parsedPaperId, validPaperId])
  const askDraftKey = useMemo(() => {
    if (!validPaperId) return undefined
    const userId = currentUserId ?? 0
    return `lit:ask:draft:${userId}:${parsedPaperId}`
  }, [currentUserId, parsedPaperId, validPaperId])

  const kbOptions = useMemo(
    () => knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id })),
    [knowledgeBases],
  )
  const collectionOptions = useMemo(
    () => collections.map((item) => ({ label: item.name, value: item.id })),
    [collections],
  )
  const academicTextBlocks = useMemo(() => buildAcademicTextBlocks(pageText), [pageText])
  const displayedTextBlocks = useMemo<AcademicTextBlock[]>(() => {
    if (generativeBlocks.length > 0) {
      return generativeBlocks.map((item) => ({
        kind: item.kind === 'heading' ? 'heading' : 'paragraph',
        text: String(item.text || ''),
      }))
    }
    return academicTextBlocks
  }, [academicTextBlocks, generativeBlocks])
  const pageWordCount = useMemo(() => {
    const text = displayedTextBlocks.map((item) => item.text).join(' ').trim()
    if (!text) return 0
    return text.split(/\s+/).filter(Boolean).length
  }, [displayedTextBlocks])
  const baseGenerativeStyle = useMemo(
    () => resolveGenerativeStyleTokens(generativeStyleKey, themeMode),
    [generativeStyleKey, themeMode],
  )
  const normalizedStyleTuning = useMemo(
    () => normalizeReaderStyleTuning(generativeStyleTuning, baseGenerativeStyle.bodyLineHeight),
    [baseGenerativeStyle.bodyLineHeight, generativeStyleTuning],
  )
  const activeGenerativeStyle = useMemo(() => {
    const base = baseGenerativeStyle
    const tunedBodyFontSize = Math.round(base.bodyFontSize * normalizedStyleTuning.body_scale * 10) / 10
    return {
      ...base,
      bodyFontSize: Math.max(14, Math.min(24, tunedBodyFontSize)),
      bodyLineHeight: normalizedStyleTuning.line_height,
    }
  }, [baseGenerativeStyle, normalizedStyleTuning])
  const composedStyleTokens = useMemo<Record<string, unknown>>(() => {
    if (composedPlan?.style_tokens && typeof composedPlan.style_tokens === 'object') {
      return composedPlan.style_tokens as Record<string, unknown>
    }
    if (composedPayload?.ui_plan?.style_tokens && typeof composedPayload.ui_plan.style_tokens === 'object') {
      return composedPayload.ui_plan.style_tokens as Record<string, unknown>
    }
    return {}
  }, [composedPlan, composedPayload])
  const activeComposedStyle = useMemo<GenerativeStyleTokens>(() => {
    const base = resolveGenerativeStyleTokens(generativeStyleKey, themeMode)

    const tokenBodySize = pickStyleTokenNumber(composedStyleTokens, ['body_font_size', 'bodyFontSize'])
    const tokenLineHeight = pickStyleTokenNumber(composedStyleTokens, ['body_line_height', 'bodyLineHeight'])

    const tunedBodyFontSize = Math.round(
      (tokenBodySize ?? base.bodyFontSize) * normalizedStyleTuning.body_scale * 10,
    ) / 10
    const nextStyle: GenerativeStyleTokens = {
      ...base,
      surfaceBackground: pickStyleTokenString(composedStyleTokens, ['surfaceBackground', 'surface_background']) || base.surfaceBackground,
      railBackground: pickStyleTokenString(composedStyleTokens, ['railBackground', 'rail_background']) || base.railBackground,
      overlayBackground: pickStyleTokenString(composedStyleTokens, ['overlayBackground', 'overlay_background']) || base.overlayBackground,
      mutedColor: pickStyleTokenString(composedStyleTokens, ['mutedColor', 'muted_color']) || base.mutedColor,
      bodyFontSize: Math.max(14, Math.min(24, tunedBodyFontSize)),
      bodyLineHeight: tokenLineHeight !== null
        ? Math.max(1.55, Math.min(2.2, tokenLineHeight))
        : normalizedStyleTuning.line_height,
    }
    return nextStyle
  }, [composedStyleTokens, generativeStyleKey, themeMode, normalizedStyleTuning])
  const generativeLayoutMode = useMemo<'split' | 'stack'>(() => (
    viewerWidth >= 1080 ? 'split' : 'stack'
  ), [viewerWidth])
  const currentPageAnnotations = useMemo(
    () =>
      annotations
        .filter((item) => item.page === readPage)
        .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()),
    [annotations, readPage],
  )
  useEffect(() => {
    const wasTextMode = lastTextModeRef.current
    if (textMode && !wasTextMode) {
      setWorkspaceTab('ai_context')
    } else if (!textMode && wasTextMode) {
      setWorkspaceTab((prev) => (prev === 'ai_context' ? 'annotation' : prev))
    }
    lastTextModeRef.current = textMode
  }, [textMode])
  const pageResourceLinks = useMemo(
    () => collectPageResourceLinks(paper, rawPageText || pageText),
    [paper, rawPageText, pageText],
  )
  const rawPageTextPreview = useMemo(() => {
    const normalized = normalizeAcademicArtifacts(rawPageText || pageText)
    if (!normalized) return ''
    return normalized.length > 2400 ? `${normalized.slice(0, 2400)}...` : normalized
  }, [rawPageText, pageText])
  const activeComposedPlan = useMemo(
    () => composedPlan || composedPayload?.ui_plan || null,
    [composedPlan, composedPayload],
  )
  const composedHasDedicatedHeaderCard = useMemo(
    () => Boolean(
      Array.isArray(activeComposedPlan?.components)
      && activeComposedPlan.components.some((node) => String(node?.type || '').trim() === 'PaperHeaderCard'),
    ),
    [activeComposedPlan],
  )
  const hasComposedPlan = Boolean(activeComposedPlan?.components?.length)
  const composedMainComponents = useMemo(
    () => (Array.isArray(activeComposedPlan?.components)
      ? activeComposedPlan.components.filter((node) => {
        const normalizedPaperTitle = normalizeAcademicArtifacts(String(paper?.title || '')).toLowerCase()
        const titleHints = collectReaderNodeTextHints(node)
          .map((item) => normalizeAcademicArtifacts(item).toLowerCase())
          .filter(Boolean)
        const duplicatesPageTitle = Boolean(normalizedPaperTitle) && titleHints.includes(normalizedPaperTitle)
        const demoteToContext = duplicatesPageTitle && composedHasDedicatedHeaderCard
        return !isContextOnlyReaderNode(node) && !demoteToContext
      })
      : []),
    [activeComposedPlan, composedHasDedicatedHeaderCard, paper],
  )
  const composedContextComponents = useMemo(
    () => (Array.isArray(activeComposedPlan?.components)
      ? activeComposedPlan.components.filter((node) => {
        const normalizedPaperTitle = normalizeAcademicArtifacts(String(paper?.title || '')).toLowerCase()
        const titleHints = collectReaderNodeTextHints(node)
          .map((item) => normalizeAcademicArtifacts(item).toLowerCase())
          .filter(Boolean)
        const duplicatesPageTitle = Boolean(normalizedPaperTitle) && titleHints.includes(normalizedPaperTitle)
        const demoteToContext = duplicatesPageTitle && composedHasDedicatedHeaderCard
        return isContextOnlyReaderNode(node) || demoteToContext
      })
      : []),
    [activeComposedPlan, composedHasDedicatedHeaderCard, paper],
  )
  const composedLayout = useMemo(
    () => ((activeComposedPlan?.layout || {}) as Record<string, unknown>),
    [activeComposedPlan],
  )
  const composedContentMaxWidth = useMemo(() => {
    const width = Number(composedLayout.content_max_width ?? composedLayout.contentMaxWidth ?? 920)
    if (!Number.isFinite(width)) return 920
    return clamp(width, 680, 1280)
  }, [composedLayout])
  const composedDecisionLog = useMemo(
    () => (Array.isArray(composedPayload?.decision_log)
      ? composedPayload.decision_log.map((item) => String(item || '').trim()).filter(Boolean)
      : []),
    [composedPayload],
  )
  const composedOmissions = useMemo(
    () => (Array.isArray(composedPayload?.omission_decisions)
      ? composedPayload.omission_decisions.filter((item) => item && typeof item === 'object')
      : []),
    [composedPayload],
  )
  const composedLinkAssets = useMemo(
    () => composedAssets.filter((item) => item.kind === 'link' || item.kind === 'external_image'),
    [composedAssets],
  )
  const composedPageImageUrl = useMemo(
    () => String(
      ((composedPayload as unknown as { docmind_structure?: { page_image_url?: unknown } })?.docmind_structure?.page_image_url) || '',
    ).trim(),
    [composedPayload],
  )
  const composedPageStructureIndex = useMemo(
    () => buildPageStructureSpatialIndex(
      (composedPayload?.page_structure_v3 || {}) as Record<string, unknown>,
      inferPageStructureSpatialDimensions((composedPayload || {}) as Record<string, unknown>),
      composedPayload?.page_grounding_v1 || null,
    ),
    [composedPayload],
  )
  const readerAutoSaveAtText = useMemo(() => {
    if (!readerAutoSaveAt) return '尚未同步'
    const ts = new Date(readerAutoSaveAt)
    if (!Number.isFinite(ts.getTime())) return '尚未同步'
    return ts.toLocaleTimeString('zh-CN', { hour12: false })
  }, [readerAutoSaveAt])
  const readerAutoSaveTag = useMemo(() => {
    if (readerAutoSaveStatus === 'saving') return { color: 'processing', label: '同步中' }
    if (readerAutoSaveStatus === 'saved') return { color: 'success', label: '已同步' }
    if (readerAutoSaveStatus === 'error') return { color: 'error', label: '同步失败' }
    return { color: 'default', label: '未同步' }
  }, [readerAutoSaveStatus])
  const askSourcesByIndex = useMemo(() => {
    const map = new Map<number, LiteratureAskSource>()
    for (const [position, source] of askSources.entries()) {
      const idx = Number(source?.idx || position + 1)
      if (Number.isFinite(idx) && idx > 0 && !map.has(idx)) {
        map.set(idx, source)
      }
    }
    return map
  }, [askSources])
  const notReadyCollectionPapers = useMemo(() => {
    if (!collectionReadiness) return []
    return collectionReadiness.papers.filter((item) => item.status !== 'completed')
  }, [collectionReadiness])
  const askScopeOptions = useMemo(
    () => [
      {
        value: 'paper',
        label: (
          <Space size={4}>
            <span>当前论文</span>
            <Tooltip title="仅围绕当前论文回答；可直接走 paper_read，未入库也可提问。">
              <QuestionCircleOutlined style={{ color: '#7fb2ff', fontSize: 14 }} />
            </Tooltip>
          </Space>
        ),
      },
      {
        value: 'collection',
        label: (
          <Space size={4}>
            <span>当前收藏夹</span>
            <Tooltip title="仅对收藏夹中 completed 论文做联合回答；未入库/处理中论文不会参与。">
              <QuestionCircleOutlined style={{ color: '#7fb2ff', fontSize: 14 }} />
            </Tooltip>
          </Space>
        ),
      },
    ],
    [],
  )
  const askModeOptions = useMemo(
    () => [
      {
        value: 'agentic',
        label: (
          <Space size={4}>
            <span>Agentic（高质量）</span>
            <Tooltip title="Agent 自主决定工具策略（paper_read / knowledge_search / web 工具），质量优先。">
              <QuestionCircleOutlined style={{ color: '#7fb2ff', fontSize: 14 }} />
            </Tooltip>
          </Space>
        ),
      },
      {
        value: 'classic',
        label: (
          <Space size={4}>
            <span>Classic（快速）</span>
            <Tooltip title="固定快速链路，响应更快但策略灵活度较低。">
              <QuestionCircleOutlined style={{ color: '#7fb2ff', fontSize: 14 }} />
            </Tooltip>
          </Space>
        ),
      },
    ],
    [],
  )

  const reloadComments = async (nextFilter: CommentFilter = commentFilter) => {
    if (!validPaperId) return
    const data = await literatureApi.getComments(parsedPaperId, nextFilter)
    setComments(data)
  }

  const reloadAskSessions = async (scopeValue: LiteratureAskScope, collectionId?: number) => {
    if (!validPaperId) return
    const params: {
      scope: LiteratureAskScope
      paper_id?: number
      collection_id?: number
      limit: number
    } = {
      scope: scopeValue,
      limit: 50,
    }
    if (scopeValue === 'paper') {
      params.paper_id = parsedPaperId
    } else if (collectionId) {
      params.collection_id = collectionId
    }
    const data = await literatureApi.getAskSessions(params)
    setAskSessions(data)
  }

  const reloadAskMessages = async (sessionId: number | undefined) => {
    if (!sessionId) {
      setAskMessages([])
      return
    }
    const data = await literatureApi.getAskMessages(sessionId, { limit: 200 })
    setAskMessages(data)
    const latestAssistant = [...data].reverse().find((item) => item.role === 'assistant')
    if (latestAssistant) {
      setAskAnswer(latestAssistant.content)
      setAskSources(Array.isArray(latestAssistant.sources) ? latestAssistant.sources : [])
    }
  }

  const loadPdfSource = async () => {
    if (!validPaperId) return

    setPdfLoading(true)
    setPdfDoc(null)
    setPdfNumPages(0)
    setPageText('')

    if (pdfObjectUrlRef.current) {
      URL.revokeObjectURL(pdfObjectUrlRef.current)
      pdfObjectUrlRef.current = null
    }

    const setBlobAsSource = async (blob: Blob) => {
      if (!blob || blob.size <= 0) {
        throw new Error('PDF 文件为空')
      }
      const mime = String(blob.type || '').toLowerCase()
      if (!mime.includes('pdf')) {
        const header = await blob.slice(0, 8).text().catch(() => '')
        if (!header.startsWith('%PDF-')) {
          throw new Error('服务返回内容不是 PDF')
        }
      }
      const objectUrl = URL.createObjectURL(blob)
      pdfObjectUrlRef.current = objectUrl
      setPdfSource(objectUrl)
    }

    try {
      let firstError: unknown = null
      try {
        const blob = await literatureApi.getPaperPdfBlob(parsedPaperId, 180000)
        await setBlobAsSource(blob)
        return
      } catch (err) {
        firstError = err
      }

      try {
        await literatureApi.downloadPdf(parsedPaperId, undefined, 180000)
        const retryBlob = await literatureApi.getPaperPdfBlob(parsedPaperId, 180000)
        await setBlobAsSource(retryBlob)
        message.success('PDF 已自动下载并加载')
        return
      } catch (retryErr) {
        setPdfSource(undefined)
        const fallbackMsg = isApiTimeoutError(firstError) || isApiTimeoutError(retryErr)
          ? 'PDF 加载超时，请稍后重试（可先点击“加入知识库/下载PDF”触发本地准备）'
          : retryErr instanceof Error
            ? retryErr.message
            : 'PDF 加载失败'
        message.error(fallbackMsg)
        console.error('[PaperReader] loadPdfSource failed', { firstError, retryErr })
      }
    } finally {
      setPdfLoading(false)
    }
  }

  const refreshKnowledgeLinks = async () => {
    if (!validPaperId) return []
    const links = await literatureApi.getKnowledgeLinks(parsedPaperId)
    setKnowledgeLinks(links)
    return links
  }

  const reloadCoreData = async () => {
    if (!validPaperId) return
    const [nextPaper, nextSession, nextAnnotations, nextComments, nextRating, nextLinks, kbList, collList] =
      await Promise.all([
        literatureApi.getPaper(parsedPaperId),
        literatureApi.getReaderSession(parsedPaperId),
        literatureApi.getAnnotations(parsedPaperId),
        literatureApi.getComments(parsedPaperId, commentFilter),
        literatureApi.getRatingSummary(parsedPaperId),
        literatureApi.getKnowledgeLinks(parsedPaperId),
        knowledgeApi.getKnowledgeBases().then((r) => r.items),
        literatureApi.getCollections(),
      ])

    const cachedReader = readJsonCache<Partial<ReaderSession>>(readerCacheKey)
    const cachedAnnotationDraft = readJsonCache<Partial<PaperAnnotation>>(annotationDraftKey)
    const cachedAskDraft = readJsonCache<{
      scope?: LiteratureAskScope
      mode?: 'agentic' | 'classic'
      collection_id?: number
      question?: string
      session_id?: number
    }>(askDraftKey)

    setPaper(nextPaper)
    setReaderSession(nextSession)
    const restoredPage = Math.max(
      1,
      requestedPage || Number(cachedReader?.page || 0) || Number(nextSession.page || 1),
    )
    const restoredZoom = parseZoomPercent(String(cachedReader?.zoom || nextSession.zoom || '120%'))
    const sessionAnchor = (
      (cachedReader?.last_anchor as Record<string, unknown> | undefined) ||
      (nextSession.last_anchor as Record<string, unknown> | undefined) ||
      {}
    )
    const restoredFitWidth = Boolean(
      sessionAnchor.fit_width ??
      true,
    )
    const restoredReaderMode = String(sessionAnchor.reader_mode || '').toLowerCase()
    const restoredStyleKey = normalizeGenerativeStyleKey(String(sessionAnchor.style_key || 'journal_classic'))
    const restoredThemeMode: ReaderThemeMode =
      String(sessionAnchor.theme_mode || 'light').toLowerCase() === 'dark' ? 'dark' : 'light'
    const rawDetailLevel = String(sessionAnchor.detail_level || 'standard').toLowerCase()
    const restoredDetailLevel: ReaderDetailLevel =
      rawDetailLevel === 'concise' || rawDetailLevel === 'deep' ? rawDetailLevel : 'standard'
    const restoredCompareMode = Boolean(sessionAnchor.compare_mode)
    const restoredCitationTldr = Boolean(sessionAnchor.citation_tldr)
    const restoredMaxIterations = Math.max(
      4,
      Math.min(24, Number(sessionAnchor.compose_max_iterations || DEFAULT_COMPOSE_MAX_ITERATIONS) || DEFAULT_COMPOSE_MAX_ITERATIONS),
    )
    composedAppliedOptionsRef.current = {
      detailLevel: restoredDetailLevel,
      compareMode: restoredCompareMode,
      citationTldr: restoredCitationTldr,
    }
    setReadPage(restoredPage)
    setZoomPercent(restoredZoom)
    setFitWidth(restoredFitWidth)
    setTextMode(false)
    setGenerativeStyleKey(restoredStyleKey)
    setThemeMode(restoredThemeMode)
    setDetailLevel(restoredDetailLevel)
    setCompareMode(restoredCompareMode)
    setCitationTldr(restoredCitationTldr)
    setComposeMaxIterations(restoredMaxIterations)
    setGenerativeStyleTuning(
      normalizeReaderStyleTuning({}, GENERATIVE_STYLE_TOKENS[restoredStyleKey].bodyLineHeight),
    )
    setAnnotations(nextAnnotations)
    setComments(nextComments)
    setRatingSummary(nextRating)
    setKnowledgeLinks(nextLinks)
    setKnowledgeBases(kbList)
    setCollections(collList)

    const fallbackKbCandidate = Number(
      requestedKbId ||
        cachedReader?.selected_kb_id ||
        nextSession.selected_kb_id ||
        nextPaper.knowledge_base_id ||
        kbList[0]?.id,
    )
    const fallbackKb =
      Number.isFinite(fallbackKbCandidate) && fallbackKbCandidate > 0 ? fallbackKbCandidate : undefined
    setSelectedKbId(fallbackKb)
    lastSavedReaderSignatureRef.current = JSON.stringify({
      page: restoredPage,
      zoom: `${restoredZoom}%`,
      scroll_y: 0,
      selected_kb_id: fallbackKb,
      last_anchor: {
        fit_width: restoredFitWidth,
        reader_mode: restoredReaderMode === 'generative' ? 'generative' : 'pdf',
        style_key: restoredStyleKey,
        theme_mode: restoredThemeMode,
        detail_level: restoredDetailLevel,
        compare_mode: restoredCompareMode,
        citation_tldr: restoredCitationTldr,
        compose_quality_target: 0.86,
        compose_max_iterations: restoredMaxIterations,
      },
    })
    setReaderAutoSaveStatus('saved')
    setReaderAutoSaveError('')
    setReaderAutoSaveAt(String(nextSession.updated_at || ''))
    readerSessionHydratedRef.current = true
    if (cachedAnnotationDraft?.content) {
      setAnnotationContent(String(cachedAnnotationDraft.content))
    }
    if (cachedAnnotationDraft?.page && Number(cachedAnnotationDraft.page) > 0) {
      setAnnotationPage(Number(cachedAnnotationDraft.page))
    }
    if (cachedAnnotationDraft?.annotation_type === 'highlight' || cachedAnnotationDraft?.annotation_type === 'note') {
      setAnnotationType(cachedAnnotationDraft.annotation_type)
    }
    if (cachedAskDraft?.scope === 'paper' || cachedAskDraft?.scope === 'collection') {
      setAskScope(cachedAskDraft.scope)
    }
    if (cachedAskDraft?.mode === 'agentic' || cachedAskDraft?.mode === 'classic') {
      setAskMode(cachedAskDraft.mode)
    }
    if (cachedAskDraft?.collection_id && Number(cachedAskDraft.collection_id) > 0) {
      setAskCollectionId(Number(cachedAskDraft.collection_id))
    }
    if (typeof cachedAskDraft?.question === 'string') {
      setAskQuestion(cachedAskDraft.question)
    }
    if (cachedAskDraft?.session_id && Number(cachedAskDraft.session_id) > 0) {
      setAskSessionId(Number(cachedAskDraft.session_id))
    }

    const historyScope = cachedAskDraft?.scope === 'collection' ? 'collection' : 'paper'
    const historyCollectionId =
      historyScope === 'collection'
        ? Number(cachedAskDraft?.collection_id || askCollectionId || 0) || undefined
        : undefined
    await reloadAskSessions(historyScope, historyCollectionId)
    if (cachedAskDraft?.session_id && Number(cachedAskDraft.session_id) > 0) {
      await reloadAskMessages(Number(cachedAskDraft.session_id))
    }

    await loadPdfSource()
  }

  const reloadCoreDataRef = useRef(reloadCoreData)
  reloadCoreDataRef.current = reloadCoreData

  const refreshKnowledgeLinksRef = useRef(refreshKnowledgeLinks)
  refreshKnowledgeLinksRef.current = refreshKnowledgeLinks

  const reloadAskSessionsRef = useRef(reloadAskSessions)
  reloadAskSessionsRef.current = reloadAskSessions

  useEffect(() => {
    if (!validPaperId) return
    let mounted = true
    readerSessionHydratedRef.current = false
    lastSavedReaderSignatureRef.current = ''
    setReaderAutoSaveStatus('idle')
    setReaderAutoSaveError('')
    setReaderAutoSaveAt('')
    setLoading(true)
    reloadCoreDataRef.current()
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : '加载论文阅读页失败'
        message.error(msg)
      })
      .finally(() => {
        if (mounted) setLoading(false)
      })
    return () => {
      mounted = false
    }
  }, [parsedPaperId, validPaperId])

  useEffect(() => {
    if (
      !validPaperId ||
      !requestedPage ||
      requestedPage === readPage ||
      requestedPage === lastUrlSyncedReadPageRef.current
    ) return
    setReadPage(requestedPage)
  }, [readPage, requestedPage, validPaperId])

  useEffect(() => {
    if (!validPaperId || !searchParams.has('kb') || requestedKbId === selectedKbId) return
    setSelectedKbId(requestedKbId)
  }, [requestedKbId, searchParams, selectedKbId, validPaperId])

  useEffect(() => {
    if (!validPaperId) return
    lastUrlSyncedReadPageRef.current = readPage
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('page', String(readPage))
      if (selectedKbId && selectedKbId > 0) next.set('kb', String(selectedKbId))
      else next.delete('kb')
      if (requestedComposePipelineVersion) next.set('compose', requestedComposePipelineVersion)
      else next.delete('compose')
      return next
    }, { replace: true })
  }, [readPage, requestedComposePipelineVersion, selectedKbId, setSearchParams, validPaperId])

  useEffect(() => {
    if (!validPaperId) return
    refreshKnowledgeLinksRef.current().catch(() => {
      // keep silent: status refresh should not block the page
    })
  }, [parsedPaperId, validPaperId])

  useEffect(() => {
    if (!validPaperId || workspaceTab !== 'rating') return
    refreshKnowledgeLinksRef.current().catch(() => {
      // keep silent: entering the rating panel should opportunistically resync status
    })
  }, [workspaceTab, parsedPaperId, validPaperId])

  useEffect(() => {
    return () => {
      generativeStreamControllerRef.current?.abort()
      generativeStreamControllerRef.current = null
      inlineQueryStreamControllerRef.current?.abort()
      inlineQueryStreamControllerRef.current = null
      if (pdfObjectUrlRef.current) {
        URL.revokeObjectURL(pdfObjectUrlRef.current)
        pdfObjectUrlRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    const el = viewerRef.current
    if (!el) return
    const updateWidth = () => {
      setViewerWidth(Math.max(420, Math.floor(el.clientWidth) - 24))
    }
    updateWidth()
    const observer = new ResizeObserver(updateWidth)
    observer.observe(el)
    return () => observer.disconnect()
  }, [paper?.id])

  useEffect(() => {
    if (pdfNumPages <= 0) return
    setReadPage((prev) => clamp(prev, 1, pdfNumPages))
    setAnnotationPage((prev) => clamp(prev, 1, pdfNumPages))
  }, [pdfNumPages])

  useEffect(() => {
    headingRefMap.current.clear()
    setSectionJumpHighlightIndex(null)
  }, [readPage, textMode])

  useEffect(() => {
    if (!pdfSource) {
      setPdfDoc(null)
      setPdfNumPages(0)
      setPageText('')
      setRawPageText('')
      return
    }
    let cancelled = false
    const loadingTask = pdfjs.getDocument(pdfSource)
    loadingTask.promise
      .then((doc) => {
        if (cancelled) return
        setPdfDoc(doc)
        setPdfNumPages(Number(doc?.numPages || 0))
      })
      .catch((error: unknown) => {
        if (cancelled) return
        console.error('[PaperReader] preload pdf doc failed', error)
      })
    return () => {
      cancelled = true
      loadingTask.destroy().catch(() => {
        // ignore cleanup errors
      })
    }
  }, [pdfSource])

  useEffect(() => {
    let cancelled = false
    const loadPageText = async () => {
      if (!pdfDoc || !readPage || (pdfNumPages > 0 && readPage > pdfNumPages)) {
        setPageText('')
        setRawPageText('')
        return
      }
      try {
        const page = await pdfDoc.getPage(readPage)
        const textContent = await page.getTextContent()
        const extracted = extractAcademicPageText(textContent)
        const fallback = Array.isArray(textContent?.items)
          ? textContent.items
            .map((item: PdfTextItemLike) => (typeof item?.str === 'string' ? item.str : ''))
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim()
          : ''
        if (!cancelled) {
          setRawPageText(fallback)
          setPageText(extracted || fallback)
        }
      } catch {
        if (!cancelled) {
          setPageText('')
          setRawPageText('')
        }
      }
    }
    loadPageText()
    return () => {
      cancelled = true
    }
  }, [pdfDoc, readPage, pdfNumPages])

  const requestGenerativeRefresh = () => {
    // 把“本次刷新参数”写入 ref，仅供下一次请求消费。
    pendingComposedRunRef.current = {
      regenerate: true,
      applyCurrentOptions: true,
    }
    setComposedRunSeed((prev) => prev + 1)
  }

  const resolveComposedCacheLabel = (
    input: {
      cacheHit?: boolean
      cacheLayer?: string
      buildMode?: string
      status?: string
      degradedReason?: string
    },
  ) => {
    const cacheHit = Boolean(input.cacheHit)
    const cacheLayer = String(input.cacheLayer || 'unknown').trim() || 'unknown'
    const buildMode = String(input.buildMode || 'compose_agent').trim() || 'compose_agent'
    const status = String(input.status || '').trim()
    const degradedReason = String(input.degradedReason || '').trim()

    if (cacheHit) {
      return `Cache hit (${cacheLayer})`
    }
    if (status === 'fallback' || degradedReason) {
      return `Fallback (${degradedReason || status || buildMode})`
    }
    return `Built (${buildMode})`
  }

  useEffect(() => {
    if (!validPaperId || !textMode) {
      generativeStreamControllerRef.current?.abort()
      composedStreamControllerRef.current?.abort()
      setGenerativeLoading(false)
      setComposedLoading(false)
      return
    }

    const runOptions = pendingComposedRunRef.current
    pendingComposedRunRef.current = { regenerate: false, applyCurrentOptions: false }
    if (runOptions.applyCurrentOptions) {
      composedAppliedOptionsRef.current = {
        detailLevel,
        compareMode,
        citationTldr,
      }
    }
    const appliedOptions = composedAppliedOptionsRef.current

    const controller = new AbortController()
    composedStreamControllerRef.current?.abort()
    composedStreamControllerRef.current = controller

    setComposedLoading(true)
    setComposedError('')
    setComposedCacheLabel('')
    setComposedPlan(null)
    setComposedAssets([])
    setComposedPayload(null)
    setComposedQuality(null)

    const applyRecoveredComposePayload = (
      recoveredPayload: ReaderComposePayload,
      cacheMeta?: Record<string, unknown>,
    ) => {
      setComposedPayload(recoveredPayload)
      setComposedPlan(recoveredPayload.ui_plan || null)
      setComposedAssets(Array.isArray(recoveredPayload.assets) ? recoveredPayload.assets : [])
      setComposedQuality(recoveredPayload.quality_report || null)
      setComposedError('')
      setComposedCacheLabel(resolveComposedCacheLabel({
        cacheHit: Boolean(cacheMeta?.cache_hit),
        cacheLayer: String(cacheMeta?.cache_layer || ''),
        buildMode: String(cacheMeta?.build_mode || recoveredPayload.build_mode || ''),
        status: recoveredPayload.status,
        degradedReason: recoveredPayload.degraded_reason,
      }))
      setComposedLoading(false)
    }

    const attemptRecoverFromCachedCompose = async (failureMessage: string) => {
      if (controller.signal.aborted) return false
      setComposedCacheLabel('Recovering cache...')
      try {
        const recovered = await literatureApi.getCachedReaderComposed(parsedPaperId, {
          page: readPage,
          selected_kb_id: selectedKbId,
          pipeline_version: effectiveComposePipelineVersion,
          force_refresh: false,
          regenerate: false,
          detail_level: appliedOptions.detailLevel,
          compare_mode: appliedOptions.compareMode,
          citation_tldr: appliedOptions.citationTldr,
          max_iterations: composeMaxIterations,
        })
        if (controller.signal.aborted) return false
        if (recovered?.payload) {
          applyRecoveredComposePayload(recovered.payload, recovered.cache_meta)
          return true
        }
      } catch {
        // ignore cache recovery miss and preserve original failure below
      }
      if (controller.signal.aborted) return false
      setComposedError(failureMessage)
      setComposedCacheLabel('Failed')
      setComposedLoading(false)
      return false
    }

    literatureApi
      .streamReaderComposed(
        parsedPaperId,
        {
          page: readPage,
          selected_kb_id: selectedKbId,
          pipeline_version: effectiveComposePipelineVersion,
          force_refresh: false,
          regenerate: runOptions.regenerate,
          detail_level: appliedOptions.detailLevel,
          compare_mode: appliedOptions.compareMode,
          citation_tldr: appliedOptions.citationTldr,
          max_iterations: composeMaxIterations,
        },
        (event, data) => {
          if (controller.signal.aborted) return

          if (event === 'start') {
            const startData = data as {
              cache_hit?: boolean
              cache_layer?: string
              build_mode?: string
            }
            const cacheLabel = resolveComposedCacheLabel({
              cacheHit: startData.cache_hit,
              cacheLayer: startData.cache_layer,
              buildMode: startData.build_mode,
            })
            setComposedCacheLabel(cacheLabel)
            return
          }

          if (event === 'plan_draft' || event === 'plan_patch') {
            const planData = data as {
              ui_plan?: ReaderUIPlan
            }
            if (planData.ui_plan) {
              setComposedPlan(planData.ui_plan)
            }
            return
          }

          if (event === 'component_patch') {
            const patchData = data as { ui_ops?: ReaderComponentPatchOp[] }
            const uiOps = Array.isArray(patchData.ui_ops) ? patchData.ui_ops : []
            if (uiOps.length > 0) {
              setComposedPlan((prev) => {
                if (!prev) return prev
                return {
                  ...prev,
                  components: applyComponentPatchOps(prev.components || [], uiOps),
                }
              })
              setComposedPayload((prev) => {
                if (!prev?.ui_plan) return prev
                return {
                  ...prev,
                  ui_plan: {
                    ...prev.ui_plan,
                    components: applyComponentPatchOps(prev.ui_plan.components || [], uiOps),
                  },
                }
              })
            }
            return
          }

          if (event === 'agent_trace') {
            const traceData = data as { trace?: Array<Record<string, unknown>>; tool_calls?: Array<Record<string, unknown>> }
            setComposedPlan((prev) => {
              if (!prev) return prev
              return {
                ...prev,
                trace_meta: {
                  ...(prev.trace_meta || {}),
                  agent_trace: Array.isArray(traceData.trace) ? traceData.trace : [],
                  agent_tool_calls: Array.isArray(traceData.tool_calls) ? traceData.tool_calls : [],
                },
              }
            })
            return
          }

          if (event === 'component_error') {
            const errorData = data as { message?: string; errors?: string[] }
            const details = Array.isArray(errorData.errors) && errorData.errors.length > 0
              ? ` (${errorData.errors.slice(0, 3).join('; ')})`
              : ''
            setComposedError(String(errorData.message || 'Component patch failed') + details)
            return
          }

          if (event === 'assets') {
            const assetData = data as { assets?: ReaderComposeAsset[] }
            setComposedAssets(Array.isArray(assetData.assets) ? assetData.assets : [])
            return
          }

          if (event === 'quality') {
            const qualityData = data as { quality_report?: ReaderComposeQualityReport }
            if (qualityData.quality_report) {
              setComposedQuality(qualityData.quality_report)
            }
            return
          }

          if (event === 'done') {
            const doneData = data as {
              status?: string
              degraded_reason?: string
              payload?: ReaderComposePayload
              cache_meta?: Record<string, unknown>
            }
            if (doneData.payload) {
              setComposedPayload(doneData.payload)
              setComposedPlan(doneData.payload.ui_plan || null)
              setComposedAssets(Array.isArray(doneData.payload.assets) ? doneData.payload.assets : [])
              setComposedQuality(doneData.payload.quality_report || null)
            }
            const cacheMeta = doneData.cache_meta || {}
            setComposedCacheLabel(resolveComposedCacheLabel({
              cacheHit: Boolean(cacheMeta.cache_hit),
              cacheLayer: String(cacheMeta.cache_layer || ''),
              buildMode: String(cacheMeta.build_mode || doneData.payload?.build_mode || ''),
              status: doneData.status || doneData.payload?.status,
              degradedReason: doneData.degraded_reason || doneData.payload?.degraded_reason,
            }))
            setComposedLoading(false)
            return
          }

          if (event === 'error') {
            const errorData = data as { message?: string }
            void attemptRecoverFromCachedCompose(String(errorData.message || 'AI 组件编排失败，已降级到本地提取'))
          }
        },
        controller,
      )
      .catch((error) => {
        if (controller.signal.aborted) return
        const msg = error instanceof Error ? error.message : 'AI 组件编排失败，已降级到本地提取'
        void attemptRecoverFromCachedCompose(msg)
      })

    return () => {
      controller.abort()
      if (composedStreamControllerRef.current === controller) {
        composedStreamControllerRef.current = null
      }
    }
  }, [
    validPaperId,
    parsedPaperId,
    textMode,
    readPage,
    selectedKbId,
    effectiveComposePipelineVersion,
    detailLevel,
    compareMode,
    citationTldr,
    composeMaxIterations,
    composedRunSeed,
  ])

  useEffect(() => {
    if (!validPaperId || !textMode) return
    if (composedLoading || !composedPayload || String(composedPayload.status || '') !== 'done') return
    const candidates = [readPage + 1].filter(
      (value) => value > 0 && (pdfNumPages <= 0 || value <= pdfNumPages),
    )
    const queuedCandidates = candidates.filter(
      (value) => !prefetchedPagesRef.current.has(value) && !prefetchInFlightPagesRef.current.has(value),
    )
    if (queuedCandidates.length === 0) return
    queuedCandidates.forEach((item) => prefetchInFlightPagesRef.current.add(item))
    const appliedOptions = composedAppliedOptionsRef.current
    literatureApi
      .prefetchReaderComposed(parsedPaperId, {
        pages: queuedCandidates,
        selected_kb_id: selectedKbId,
        pipeline_version: effectiveComposePipelineVersion,
        detail_level: appliedOptions.detailLevel,
        compare_mode: appliedOptions.compareMode,
        citation_tldr: appliedOptions.citationTldr,
        max_iterations: Math.max(4, composeMaxIterations - 2),
      })
      .then((result) => {
        if (Array.isArray(result.queued)) {
          result.queued.forEach((item) => {
            const page = Number(item)
            prefetchedPagesRef.current.add(page)
            prefetchInFlightPagesRef.current.delete(page)
          })
        }
      })
      .catch(() => {
        // keep silent for prefetch errors
        queuedCandidates.forEach((item) => prefetchInFlightPagesRef.current.delete(item))
      })
  }, [
    validPaperId,
    parsedPaperId,
    textMode,
    readPage,
    pdfNumPages,
    selectedKbId,
    effectiveComposePipelineVersion,
    composeMaxIterations,
    composedLoading,
    composedPayload,
  ])

  useEffect(() => {
    writeJsonCache(readerCacheKey, {
      page: readPage,
      zoom: `${zoomPercent}%`,
      scroll_y: 0,
      selected_kb_id: selectedKbId,
      last_anchor: {
        fit_width: fitWidth,
        reader_mode: textMode ? 'generative' : 'pdf',
        style_key: generativeStyleKey,
        theme_mode: themeMode,
        detail_level: detailLevel,
        compare_mode: compareMode,
        citation_tldr: citationTldr,
        compose_quality_target: 0.86,
        compose_max_iterations: composeMaxIterations,
      },
      updated_at: new Date().toISOString(),
    })
  }, [
    readerCacheKey,
    readPage,
    zoomPercent,
    selectedKbId,
    fitWidth,
    textMode,
    generativeStyleKey,
    themeMode,
    detailLevel,
    compareMode,
    citationTldr,
    composeMaxIterations,
  ])

  useEffect(() => {
    if (!validPaperId || !readerSessionHydratedRef.current) return
    const payload = {
      page: readPage,
      zoom: `${zoomPercent}%`,
      scroll_y: 0,
      selected_kb_id: selectedKbId,
      last_anchor: {
        fit_width: fitWidth,
        reader_mode: textMode ? 'generative' : 'pdf',
        style_key: generativeStyleKey,
        theme_mode: themeMode,
        detail_level: detailLevel,
        compare_mode: compareMode,
        citation_tldr: citationTldr,
        compose_quality_target: 0.86,
        compose_max_iterations: composeMaxIterations,
      },
    }
    const signature = JSON.stringify(payload)
    if (signature === lastSavedReaderSignatureRef.current) return

    const timer = window.setTimeout(() => {
      setReaderAutoSaveStatus('saving')
      setReaderAutoSaveError('')
      literatureApi
        .updateReaderSession(parsedPaperId, payload)
        .then((saved) => {
          setReaderSession(saved)
          lastSavedReaderSignatureRef.current = signature
          setReaderAutoSaveStatus('saved')
          setReaderAutoSaveAt(String(saved.updated_at || new Date().toISOString()))
        })
        .catch((error: unknown) => {
          const msg = error instanceof Error ? error.message : '自动保存失败'
          setReaderAutoSaveStatus('error')
          setReaderAutoSaveError(msg)
          console.warn('[PaperReader] 自动保存阅读位置失败', error)
        })
    }, 650)

    return () => window.clearTimeout(timer)
  }, [
    validPaperId,
    parsedPaperId,
    readPage,
    zoomPercent,
    selectedKbId,
    fitWidth,
    textMode,
    generativeStyleKey,
    themeMode,
    detailLevel,
    compareMode,
    citationTldr,
    composeMaxIterations,
  ])

  useEffect(() => {
    writeJsonCache(annotationDraftKey, {
      annotation_type: annotationType,
      page: annotationPage,
      content: annotationContent,
      updated_at: new Date().toISOString(),
    })
  }, [annotationDraftKey, annotationType, annotationPage, annotationContent])

  useEffect(() => {
    writeJsonCache(askDraftKey, {
      scope: askScope,
      mode: askMode,
      collection_id: askCollectionId,
      question: askQuestion,
      session_id: askSessionId,
      updated_at: new Date().toISOString(),
    })
  }, [askDraftKey, askScope, askMode, askCollectionId, askQuestion, askSessionId])

  useEffect(() => {
    if (!validPaperId) return
    reloadAskSessionsRef.current(askScope, askCollectionId).catch(() => {
      message.error('加载询问会话失败')
    })
  }, [validPaperId, askScope, askCollectionId, parsedPaperId])

  useEffect(() => {
    reloadAskMessages(askSessionId).catch(() => {
      message.error('加载会话消息失败')
    })
  }, [askSessionId])

  useEffect(() => {
    if (askScope !== 'collection' || !askCollectionId || !selectedKbId) {
      setCollectionReadiness(null)
      setCollectionReadinessLoading(false)
      return
    }
    let cancelled = false
    setCollectionReadinessLoading(true)
    literatureApi
      .getCollectionKnowledgeReadiness(askCollectionId, selectedKbId)
      .then((summary) => {
        if (!cancelled) setCollectionReadiness(summary)
      })
      .catch(() => {
        if (!cancelled) setCollectionReadiness(null)
      })
      .finally(() => {
        if (!cancelled) setCollectionReadinessLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [askScope, askCollectionId, selectedKbId])

  useEffect(() => {
    if (!validPaperId) return
    const streamController = new AbortController()

    literatureApi
      .streamStatusEvents(
        { paper_id: parsedPaperId },
        (event, payload) => {
          if (event === 'reader_page_ready') {
            const ready = payload as ReaderPageReadyEventData
            const readyPage = Number(ready?.page || 0)
            if (Number.isFinite(readyPage) && readyPage > 0) {
              prefetchedPagesRef.current.add(readyPage)
            }
            return
          }
          if (event !== 'paper_link_status') return
          const data = payload as PaperKnowledgeLinkStatusEventData
          const incomingLinkId = Number(data?.link_id || 0)
          if (!Number.isFinite(incomingLinkId) || incomingLinkId <= 0) return
          const normalizedIncomingStatus = normalizeKnowledgeLinkStatus(data.status)

          setKnowledgeLinks((prev) => {
            let matched = false
            const next = prev.map((link) => {
              if (link.id !== incomingLinkId) return link
              matched = true
              return {
                ...link,
                status: data.status,
                document_id: data.document_id || link.document_id,
                error_message: data.error_message,
                updated_at: data.updated_at || link.updated_at,
              }
            })
            return matched ? next : prev
          })

          if (
            normalizedIncomingStatus === 'completed'
            || normalizedIncomingStatus === 'failed'
            || normalizedIncomingStatus === 'timeout'
            || normalizedIncomingStatus === 'cancelled'
          ) {
            void refreshKnowledgeLinksRef.current().catch(() => {
              // keep silent: terminal status should reconcile against server truth
            })
          }
        },
        streamController,
      )
      .catch((error) => {
        if (streamController.signal.aborted) return
        console.warn('[PaperReader] 状态流订阅失败，降级低频轮询', error)
      })

    return () => {
      streamController.abort()
    }
  }, [parsedPaperId, validPaperId])

  useEffect(() => {
    if (!validPaperId) return
    const hasProcessing = knowledgeLinks.some((item) => {
      const normalized = normalizeKnowledgeLinkStatus(item.status)
      return normalized === 'pending' || normalized === 'running'
    })
    if (!hasProcessing) return

    const timer = setInterval(() => {
      refreshKnowledgeLinksRef.current()
        .catch(() => {
          // keep silent for fallback polling
        })
    }, 30000)

    return () => clearInterval(timer)
  }, [knowledgeLinks, parsedPaperId, validPaperId])

  useEffect(() => {
    if (!pendingSectionJump || !textMode) return
    if (pendingSectionJump.expectedPage && pendingSectionJump.expectedPage !== readPage) return

    const targetIndex = findBestSectionHeadingIndex(displayedTextBlocks, pendingSectionJump.sectionTitle)
    if (targetIndex == null) {
      if (pendingSectionJump.expectedPage && pendingSectionJump.expectedPage === readPage) {
        message.info(`未在第 ${readPage} 页命中章节“${pendingSectionJump.sectionTitle}”`)
        setPendingSectionJump(null)
      }
      return
    }

    const container = textModeContainerRef.current
    const node = headingRefMap.current.get(targetIndex)
    if (!container || !node) {
      setPendingSectionJump(null)
      return
    }

    container.scrollTo({
      top: Math.max(0, node.offsetTop - 18),
      behavior: 'smooth',
    })
    setSectionJumpHighlightIndex(targetIndex)
    window.setTimeout(() => {
      setSectionJumpHighlightIndex((current) => (current === targetIndex ? null : current))
    }, 2200)
    setPendingSectionJump(null)
  }, [pendingSectionJump, textMode, readPage, displayedTextBlocks])

  useEffect(() => {
    if (textMode) return
    setAnchorPreview((prev) => {
      if (!prev.visible) return prev
      return { ...prev, visible: false, pinned: false, loading: false }
    })
  }, [textMode])

  const handleBackToList = () => {
    if (window.history.length > 1) {
      navigate(-1)
      return
    }
    navigate('/literature')
  }

  const locateSectionPageByTitle = async (sectionTitle: string): Promise<number | null> => {
    const normalized = normalizeSectionKey(sectionTitle)
    if (!normalized || !pdfDoc || pdfNumPages <= 0) return null

    const cached = sectionPageCacheRef.current.get(normalized)
    if (cached && cached > 0) return cached

    const maxPages = Math.min(pdfNumPages, 80)
    let bestPage: number | null = null
    let bestScore = 0
    for (let pageNo = 1; pageNo <= maxPages; pageNo += 1) {
      const page = await pdfDoc.getPage(pageNo)
      const textContent = await page.getTextContent()
      const extracted = extractAcademicPageText(textContent)
      if (!extracted) continue
      const blocks = buildAcademicTextBlocks(extracted)
      for (const block of blocks) {
        if (block.kind !== 'heading') continue
        const score = computeSectionMatchScore(sectionTitle, block.text)
        if (score > bestScore) {
          bestScore = score
          bestPage = pageNo
        }
      }
      if (bestScore >= 140) break
    }

    if (bestPage && bestScore >= 70) {
      sectionPageCacheRef.current.set(normalized, bestPage)
      return bestPage
    }
    return null
  }

  const handleStartEditAnnotation = (item: PaperAnnotation) => {
    setEditingAnnotationId(item.id)
    setAnnotationType(item.annotation_type)
    setAnnotationPage(Number(item.page || readPage) || readPage)
    setAnnotationContent(String(item.content || item.quote_text || '').trim())
    setWorkspaceTab('annotation')
  }

  const handleCancelEditAnnotation = () => {
    setEditingAnnotationId(null)
    setAnnotationType('note')
    setAnnotationPage(readPage)
    setAnnotationContent('')
  }

  const handleSaveAnnotation = async () => {
    if (!validPaperId || !annotationContent.trim()) return
    setAnnotationSubmitting(true)
    try {
      if (editingAnnotationId) {
        const item = await literatureApi.updateAnnotation(parsedPaperId, editingAnnotationId, {
          annotation_type: annotationType,
          page: annotationPage,
          content: annotationContent.trim(),
          anchor: { page: annotationPage },
        })
        setAnnotations((prev) => prev.map((row) => (row.id === item.id ? item : row)))
        handleCancelEditAnnotation()
        message.success('批注已更新')
      } else {
        const item = await literatureApi.createAnnotation(parsedPaperId, {
          annotation_type: annotationType,
          page: annotationPage,
          content: annotationContent.trim(),
          anchor: { page: annotationPage },
        })
        setAnnotations((prev) => [...prev, item])
        setAnnotationContent('')
        setAnnotationPage(readPage)
        message.success('批注已添加')
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : (editingAnnotationId ? '更新批注失败' : '添加批注失败')
      message.error(msg)
    } finally {
      setAnnotationSubmitting(false)
    }
  }

  const handleDeleteAnnotation = async (annotationId: number) => {
    if (!validPaperId || annotationId <= 0) return
    setDeletingAnnotationId(annotationId)
    try {
      await literatureApi.deleteAnnotation(parsedPaperId, annotationId)
      setAnnotations((prev) => prev.filter((row) => row.id !== annotationId))
      if (editingAnnotationId === annotationId) {
        handleCancelEditAnnotation()
      }
      message.success('批注已删除')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除批注失败'
      message.error(msg)
    } finally {
      setDeletingAnnotationId(null)
    }
  }

  const handleAddComment = async () => {
    if (!validPaperId || !commentText.trim()) return
    try {
      await literatureApi.createComment(parsedPaperId, { content: commentText.trim() })
      setCommentText('')
      await reloadComments()
      message.success('评论已发布')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '发布评论失败'
      message.error(msg)
    }
  }

  const handleRate = async (value: number) => {
    if (!validPaperId || value <= 0) return
    try {
      const summary = await literatureApi.putRating(parsedPaperId, value)
      setRatingSummary(summary)
      message.success('评分已更新')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '评分失败'
      message.error(msg)
    }
  }

  const handleAddToKnowledge = async () => {
    if (!validPaperId || !selectedKbId) {
      message.warning('请先选择知识库')
      return
    }
    try {
      await literatureApi.addToKnowledge(parsedPaperId, selectedKbId)
      await refreshKnowledgeLinksRef.current()
      message.success('已加入知识库，正在处理')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '加入知识库失败'
      message.error(msg)
    }
  }

  const handleAsk = async () => {
    if (!validPaperId || !selectedKbId || !askQuestion.trim()) {
      message.warning('请补全提问参数')
      return
    }
    if (askScope === 'collection' && !askCollectionId) {
      message.warning('请选择收藏夹')
      return
    }
    if (
      askScope === 'collection' &&
      collectionReadiness &&
      !collectionReadiness.can_cross_paper_answer
    ) {
      message.warning('当前收藏夹在所选知识库暂无 completed 论文，请先入库后再询问')
      return
    }

    setAskAnswer('')
    setAskSources([])
    setAsking(true)
    try {
      await literatureApi.askStream(
        {
          scope: askScope,
          paper_id: askScope === 'paper' ? parsedPaperId : undefined,
          collection_id: askScope === 'collection' ? askCollectionId : undefined,
          knowledge_base_id: selectedKbId,
          question: askQuestion.trim(),
          mode: askMode,
          session_id: askSessionId,
        },
        (event, data) => {
          if (event === 'token') {
            const token = String(data?.text || '')
            setAskAnswer((prev) => prev + token)
          }
          if (event === 'sources') {
            setAskSources(Array.isArray(data) ? data : [])
          }
          if (event === 'done') {
            const nextSession = Number(data?.session_id || 0)
            if (nextSession > 0) setAskSessionId(nextSession)
            reloadAskSessions(askScope, askCollectionId).catch(() => {
              message.error('刷新会话列表失败')
            })
            reloadAskMessages(nextSession > 0 ? nextSession : askSessionId).catch(() => {
              message.error('刷新会话消息失败')
            })
            setAsking(false)
          }
          if (event === 'error') {
            const msg = String(data?.message || '询问失败')
            message.error(msg)
            setAsking(false)
          }
        },
      )
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '询问失败'
      message.error(msg)
      setAsking(false)
    }
  }

  const jumpToSource = async (source: LiteratureAskSource) => {
    if (source.page && source.page > 0) {
      setReadPage(source.page)
      setTextMode(false)
      return
    }
    if (source.section_title) {
      const title = String(source.section_title || '').trim()
      if (!title) {
        message.info('该引用缺少可跳转定位信息')
        return
      }

      const localHeadingIndex = findBestSectionHeadingIndex(displayedTextBlocks, title)
      if (localHeadingIndex != null) {
        setTextMode(true)
        setPendingSectionJump({ sectionTitle: title, expectedPage: readPage })
        return
      }

      if (!pdfDoc || pdfNumPages <= 0) {
        setTextMode(true)
        message.info(`该引用缺少页码，已切换生成式模式，请手动查找章节：${title}`)
        return
      }

      const msgKey = `section-jump-${Date.now()}`
      setSectionLocating(true)
      message.loading({ key: msgKey, content: `正在定位章节：${title}`, duration: 0 })
      try {
        const page = await locateSectionPageByTitle(title)
        if (page && page > 0) {
          setReadPage(page)
          setTextMode(true)
          setPendingSectionJump({ sectionTitle: title, expectedPage: page })
          message.success({ key: msgKey, content: `已定位章节到第 ${page} 页`, duration: 2 })
        } else {
          setTextMode(true)
          message.info({ key: msgKey, content: `未定位到章节“${title}”，已切换生成式模式`, duration: 2.4 })
        }
      } catch {
        message.error({ key: msgKey, content: '章节定位失败，请稍后重试', duration: 2 })
      } finally {
        setSectionLocating(false)
      }
      return
    }
    message.info('该引用缺少可跳转定位信息')
  }

  const applyNodeReplaceToComposeState = (nodeId: string, nodeAfter: ReaderComponentNode) => {
    setComposedPlan((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        components: replaceNodeInTree(prev.components || [], nodeId, nodeAfter),
      }
    })
    setComposedPayload((prev) => {
      if (!prev?.ui_plan) return prev
      return {
        ...prev,
        ui_plan: {
          ...prev.ui_plan,
          components: replaceNodeInTree(prev.ui_plan.components || [], nodeId, nodeAfter),
        },
        overlay_applied: true,
        overlay_count: Math.max(1, Number(prev.overlay_count || 0) + 1),
      }
    })
  }

  const applyNodeInsertToComposeState = (nodeId: string, nodeAfter: ReaderComponentNode) => {
    setComposedPlan((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        components: insertNodeAfterInTree(prev.components || [], nodeId, nodeAfter),
      }
    })
    setComposedPayload((prev) => {
      if (!prev?.ui_plan) return prev
      return {
        ...prev,
        ui_plan: {
          ...prev.ui_plan,
          components: insertNodeAfterInTree(prev.ui_plan.components || [], nodeId, nodeAfter),
        },
      }
    })
  }

  const handleComposedNodeAction = async (node: ReaderComponentNode, action: 'regenerate' | 'degrade') => {
    if (!validPaperId) return
    try {
      const requestPayload: ReaderNodeActionRequest = {
        page: readPage,
        node_id: String(node.id),
        action,
        reason: action === 'degrade' ? '用户手动触发降级' : '用户手动触发修复',
        selected_kb_id: selectedKbId,
        style_intent: generativeStyleKey,
        theme_mode: themeMode,
        detail_level: detailLevel,
        compare_mode: compareMode,
        citation_tldr: citationTldr,
      }
      const result = await literatureApi.actionReaderComposedNode(parsedPaperId, requestPayload)
      const nextNode = result.node_after
      if (nextNode?.id) {
        applyNodeReplaceToComposeState(String(node.id), nextNode)
      }
      message.success(result.message || '节点已更新')
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '节点操作失败'
      message.error(msg)
    }
  }

  const toAnchorList = (rows: ReaderInlineQuerySource[]): ReaderComponentSourceAnchor[] =>
    (Array.isArray(rows) ? rows : [])
      .map((row) => ({
        page: Number(row.page || readPage),
        start_char: Number(row.start_char || 0),
        end_char: Number(row.end_char || 0),
        quote_text: row.quote_text || undefined,
        canonical_block_id: row.canonical_block_id || undefined,
        coord_version: row.coord_version || undefined,
        anchor_confidence: Number.isFinite(Number(row.anchor_confidence)) ? Number(row.anchor_confidence) : undefined,
      }))
      .filter((row) => row.page > 0 && row.end_char > row.start_char)

  const handleInlineQuery = async (node: ReaderComponentNode, question: string) => {
    if (!validPaperId) return
    const compactQuestion = String(question || '').trim()
    if (!compactQuestion) return

    setInlineQueryLoadingNodeId(String(node.id))
    inlineQueryStreamControllerRef.current?.abort()
    const controller = new AbortController()
    inlineQueryStreamControllerRef.current = controller

    let aggregatedAnswer = ''
    let sourceRows: ReaderInlineQuerySource[] = []
    let inserted = false
    const targetRef = String(((node.props || {}) as Record<string, unknown>).target_node_ref || '').trim()
    const requestNodeId = (
      String(node.type || '') === 'InlineQuerySlot' && targetRef
        ? targetRef
        : String(node.id)
    )

    try {
      await literatureApi.streamReaderComposedInlineQuery(
        parsedPaperId,
        {
          page: readPage,
          node_id: requestNodeId,
          question: compactQuestion,
          scope: 'section',
          selected_kb_id: selectedKbId,
          style_intent: generativeStyleKey,
          theme_mode: themeMode,
          detail_level: detailLevel,
          compare_mode: compareMode,
          citation_tldr: citationTldr,
        },
        (event: ReaderInlineQueryEvent, data) => {
          if (event === 'token') {
            aggregatedAnswer += String((data as { text?: string })?.text || '')
            return
          }
          if (event === 'sources') {
            sourceRows = Array.isArray(data) ? (data as ReaderInlineQuerySource[]) : []
            return
          }
          if (event === 'done') {
            const doneData = data as { node?: ReaderComponentNode; sources?: ReaderInlineQuerySource[] }
            const sourceAnchors = toAnchorList(
              Array.isArray(doneData.sources) ? doneData.sources : sourceRows,
            )
            const fallbackNode: ReaderComponentNode = {
              id: `answer_${Date.now()}`,
              type: 'AnswerCard',
              props: {
                question: compactQuestion,
                answer: aggregatedAnswer || '暂无回答，请稍后重试。',
                foldable: true,
              },
              children: [],
              source_block_ids: Array.isArray(node.source_block_ids) ? node.source_block_ids : [],
              source_atom_ids: Array.isArray(node.source_atom_ids) ? node.source_atom_ids : [],
              source_anchor_refs: sourceAnchors,
            }
            const answerNode = doneData.node?.id ? doneData.node : fallbackNode
            applyNodeInsertToComposeState(String(node.id), answerNode)
            inserted = true
          }
          if (event === 'error') {
            const msg = String((data as { message?: string })?.message || '内联问答失败')
            message.error(msg)
          }
        },
        controller,
      )
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : '内联问答失败'
      message.error(msg)
    } finally {
      if (!inserted && aggregatedAnswer.trim()) {
        const fallbackNode: ReaderComponentNode = {
          id: `answer_${Date.now()}`,
          type: 'AnswerCard',
          props: {
            question: compactQuestion,
            answer: aggregatedAnswer,
            foldable: true,
          },
          children: [],
          source_block_ids: Array.isArray(node.source_block_ids) ? node.source_block_ids : [],
          source_atom_ids: Array.isArray(node.source_atom_ids) ? node.source_atom_ids : [],
          source_anchor_refs: toAnchorList(sourceRows),
        }
        applyNodeInsertToComposeState(String(node.id), fallbackNode)
      }
      if (inlineQueryStreamControllerRef.current === controller) {
        inlineQueryStreamControllerRef.current = null
      }
      setInlineQueryLoadingNodeId((current) => (current === node.id ? null : current))
    }
  }

  const buildPreviewTextFromAnchor = (anchor: ReaderComponentSourceAnchor): string => {
    const quote = String(anchor.quote_text || '').trim()
    if (quote) return quote
    if (Number(anchor.page || 0) === readPage) {
      return buildAnchorPreviewSnippet(rawPageText || pageText, anchor)
    }
    return ''
  }

  const showAnchorPreview = (
    anchors: ReaderComponentSourceAnchor[],
    options?: ReaderAnchorPreviewOptions,
  ) => {
    const target = buildAnchorFromPageStructureBlocks({
      anchors,
      sourceBlockIds: options?.sourceBlockIds,
      sourceAtomIds: options?.sourceAtomIds,
      preferredPage: readPage,
      pageStructureIndex: composedPageStructureIndex,
    }) || buildAnchorPreviewTarget(anchors, readPage)
    if (!target) return
    const anchor = target.previewAnchor
    const previewAnchors = target.previewAnchors
    const resolvedIndex = 0

    const previewKey = buildPreviewKey(anchor)
    const cached = anchorPreviewCacheRef.current.get(previewKey)
    const nextPinned = Boolean(options?.pinPreview)
    const previewText = cached?.text || buildPreviewTextFromAnchor(anchor)
    setAnchorPreview({
      visible: true,
      pinned: nextPinned,
      loading: !cached && !previewText,
      preview_key: previewKey,
      page: Number(anchor.page || readPage),
      text: previewText || 'Loading evidence snippet...',
      title: `Evidence · Page ${Number(anchor.page || readPage)}`,
      anchors: previewAnchors,
      anchor_index: resolvedIndex,
      anchor_count: previewAnchors.length,
      image_data_url: cached?.imageDataUrl ?? null,
      match_method: cached?.matchMethod || 'fallback',
      match_confidence: cached?.confidence || 0,
      fallback_used: cached?.fallbackUsed || false,
    })
    if (nextPinned && Number(anchor.page || 0) > 0 && Number(anchor.page || 0) !== readPage) {
      setReadPage(Number(anchor.page))
    }

    if (pdfDoc && Number(anchor.page || 0) > 0 && !cached) {
      const targetPage = Number(anchor.page)
      void (async () => {
        try {
          const pageProxy = await pdfDoc.getPage(targetPage)
          const textContent = await pageProxy.getTextContent()
          const textItems = Array.isArray(textContent?.items) ? (textContent.items as PdfTextItemLike[]) : []
          const extracted = extractAcademicPageText(textContent)
          const fallback = Array.isArray(textContent?.items)
            ? textContent.items
              .map((item: PdfTextItemLike) => (typeof item?.str === 'string' ? item.str : ''))
              .join(' ')
              .replace(/\s+/g, ' ')
              .trim()
            : ''
          const source = extracted || fallback
          const resolvedText = buildAnchorPreviewSnippet(source, anchor) || source.slice(0, 320)
          const rendered = await renderAnchorEvidenceImage(pageProxy, textItems, anchor)
          anchorPreviewCacheRef.current.set(previewKey, {
            text: resolvedText,
            imageDataUrl: rendered.imageDataUrl,
            matchMethod: rendered.matchMethod,
            confidence: rendered.confidence,
            fallbackUsed: rendered.fallbackUsed,
          })
          if (anchorPreviewCacheRef.current.size > 120) {
            const oldestKey = anchorPreviewCacheRef.current.keys().next().value
            if (oldestKey) anchorPreviewCacheRef.current.delete(oldestKey)
          }
          setAnchorPreview((prev) => {
            if (!prev.visible) return prev
            if (String(prev.preview_key || '') !== previewKey) return prev
            return {
              ...prev,
              loading: false,
              image_data_url: rendered.imageDataUrl,
              match_method: rendered.matchMethod,
              match_confidence: rendered.confidence,
              fallback_used: rendered.fallbackUsed,
              text: resolvedText || prev.text || '未检索到可展示的原文片段。',
            }
          })
        } catch {
          setAnchorPreview((prev) => {
            if (!prev.visible) return prev
            if (String(prev.preview_key || '') !== previewKey) return prev
            return {
              ...prev,
              loading: false,
              fallback_used: true,
              text: prev.text || '原文片段加载失败，请切换 PDF 模式核对。',
            }
          })
        }
      })()
    }
  }

  const resolveAnchorPreviewImage = async (
    anchors: ReaderComponentSourceAnchor[],
    options?: { preferredPage?: number; segmentIndex?: number; sourceBlockIds?: string[]; sourceAtomIds?: string[] },
  ): Promise<string | null> => {
    const target = buildAnchorFromPageStructureBlocks({
      anchors,
      sourceBlockIds: options?.sourceBlockIds,
      sourceAtomIds: options?.sourceAtomIds,
      preferredPage: options?.preferredPage || readPage,
      pageStructureIndex: composedPageStructureIndex,
    }) || buildAnchorPreviewTarget(anchors, options?.preferredPage || readPage)
    if (!target) return null
    const anchor = target.previewAnchor

    const previewKey = buildPreviewKey(anchor)
    const cached = anchorPreviewCacheRef.current.get(previewKey)
    if (cached?.imageDataUrl) return cached.imageDataUrl
    if (!pdfDoc || Number(anchor.page || 0) <= 0) return null

    try {
      const targetPage = Number(anchor.page)
      const pageProxy = await pdfDoc.getPage(targetPage)
      const textContent = await pageProxy.getTextContent()
      const textItems = Array.isArray(textContent?.items) ? (textContent.items as PdfTextItemLike[]) : []
      const extracted = extractAcademicPageText(textContent)
      const fallback = Array.isArray(textContent?.items)
        ? textContent.items
          .map((item: PdfTextItemLike) => (typeof item?.str === 'string' ? item.str : ''))
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim()
        : ''
      const source = extracted || fallback
      const resolvedText = buildAnchorPreviewSnippet(source, anchor) || source.slice(0, 320)
      const rendered = await renderAnchorEvidenceImage(pageProxy, textItems, anchor)
      anchorPreviewCacheRef.current.set(previewKey, {
        text: resolvedText,
        imageDataUrl: rendered.imageDataUrl,
        matchMethod: rendered.matchMethod,
        confidence: rendered.confidence,
        fallbackUsed: rendered.fallbackUsed,
      })
      if (anchorPreviewCacheRef.current.size > 120) {
        const oldestKey = anchorPreviewCacheRef.current.keys().next().value
        if (oldestKey) anchorPreviewCacheRef.current.delete(oldestKey)
      }
      return rendered.imageDataUrl
    } catch {
      return null
    }
  }

  const hideAnchorPreview = () => {
    setAnchorPreview((prev) => {
      if (prev.pinned) return prev
      return { ...prev, visible: false, loading: false }
    })
  }

  const handleOpenExperiencePage = () => {
    if (!validPaperId) return
    const params = new URLSearchParams()
    params.set('page', String(readPage))
    if (selectedKbId && selectedKbId > 0) params.set('kb', String(selectedKbId))
    navigate(`/literature/${parsedPaperId}/experience?${params.toString()}`)
  }
  const appendMarkdownToAnnotation = (markdown: string) => {
    const text = String(markdown || '').trim()
    if (!text) return
    setAnnotationContent((prev) => {
      const current = String(prev || '')
      const textarea = annotationInputRef.current?.resizableTextArea?.textArea as HTMLTextAreaElement | undefined
      if (!textarea) {
        return current ? `${current}\n\n${text}` : text
      }
      const start = Number(textarea.selectionStart || 0)
      const end = Number(textarea.selectionEnd || start)
      const before = current.slice(0, start)
      const after = current.slice(end)
      const glue = before && !before.endsWith('\n') ? '\n\n' : ''
      const next = `${before}${glue}${text}${after}`
      window.requestAnimationFrame(() => {
        textarea.focus()
        const cursor = before.length + glue.length + text.length
        textarea.setSelectionRange(cursor, cursor)
      })
      return next
    })
    setAnnotationPage(readPage)
  }

  const handleAnnotationDragOver = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }

  const handleAnnotationDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    const customPayload = event.dataTransfer.getData('application/x-reader-component+json')
    const markdownPayload = event.dataTransfer.getData('text/markdown') || event.dataTransfer.getData('text/plain')
    let markdown = markdownPayload
    if (!markdown && customPayload) {
      try {
        const parsed = JSON.parse(customPayload) as { markdown?: string }
        markdown = String(parsed.markdown || '')
      } catch {
        markdown = ''
      }
    }
    if (!markdown.trim()) return
    appendMarkdownToAnnotation(markdown)
    message.success('组件内容已放入批注草稿')
  }

  const renderAnchorEvidenceCard = () => {
    const methodLabelMap: Record<AnchorMatchMethod, string> = {
      polygon: 'Polygon',
      bbox_hint: 'Layout bbox',
      quote_exact: 'Quote exact',
      quote_fuzzy: 'Quote fuzzy',
      char_range: 'Char range',
      fallback: 'Fallback',
    }
    const method = anchorPreview.match_method || 'fallback'
    const confidence = Math.max(0, Math.min(1, Number(anchorPreview.match_confidence || 0)))
    return (
      <Card
        className="reader-composed-preview"
        size="small"
        title={anchorPreview.title || `Evidence · Page ${readPage}`}
        style={{
          marginTop: 8,
          borderRadius: 12,
          border: `1px solid ${activeComposedStyle.borderColor}`,
          background: activeComposedStyle.panelBackground,
          color: activeComposedStyle.bodyColor,
        }}
        extra={(
          <Space size={8}>
            {anchorPreview.pinned ? <Tag color="blue">Pinned</Tag> : null}
            <Button
              size="small"
              disabled={!anchorPreview.visible}
              onClick={() => {
                const targetPage = Number(anchorPreview.page || 0)
                if (targetPage > 0) setReadPage(targetPage)
              }}
            >
              跳转
            </Button>
            <Button
              size="small"
              onClick={() => {
                setAnchorPreview((prev) => ({ ...prev, visible: false, pinned: false, loading: false }))
              }}
            >
              关闭
            </Button>
          </Space>
        )}
      >
        <div style={{ maxHeight: 'min(72vh, 760px)', overflowY: 'auto', paddingRight: 4 }}>
          <Text type="secondary" style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>
            悬停或点击左侧带锚点组件后，这里会显示 PDF 局部证据和命中区域。
          </Text>
          {!anchorPreview.visible ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前暂无证据预览" />
          ) : anchorPreview.loading ? (
            <div className="h-[120px] flex items-center justify-center">
              <Spin size="small" />
            </div>
          ) : (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              <Space size={8} wrap>
                <Tag color="blue">{methodLabelMap[method] || methodLabelMap.fallback}</Tag>
                <Tag color={confidence >= 0.8 ? 'green' : confidence >= 0.6 ? 'gold' : 'red'}>
                  置信度: {Math.round(confidence * 100)}%
                </Tag>
                {anchorPreview.fallback_used ? <Tag color="orange">图像已回退整页裁剪</Tag> : null}
              </Space>
              {anchorPreview.image_data_url ? (
                <img
                  src={anchorPreview.image_data_url}
                  alt="anchor-evidence"
                  style={{
                    width: '100%',
                    maxHeight: '56vh',
                    display: 'block',
                    margin: '0 auto',
                    borderRadius: 10,
                    border: `1px solid ${activeComposedStyle.borderColor}`,
                    imageRendering: 'auto',
                    objectFit: 'contain',
                    background: '#fff',
                  }}
                />
              ) : null}
              <div
                style={{
                  whiteSpace: 'pre-wrap',
                  lineHeight: 1.75,
                  maxHeight: 280,
                  overflowY: 'auto',
                  color: activeComposedStyle.bodyColor,
                  fontSize: 14,
                  borderRadius: 10,
                  border: `1px solid ${activeComposedStyle.borderColor}`,
                  padding: '10px 12px',
                  background: activeComposedStyle.pageBackground,
                }}
              >
                {anchorPreview.text || '暂无可展示的锚点原文。'}
              </div>
            </Space>
          )}
        </div>
      </Card>
    )
  }

  const renderReaderSettingsContent = () => (
    <ConfigProvider
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorBgContainer: activeComposedStyle.panelBackground,
          colorBgElevated: activeComposedStyle.overlayBackground,
          colorBorder: activeComposedStyle.borderColor,
          colorSplit: activeComposedStyle.borderColor,
          colorText: activeComposedStyle.bodyColor,
          colorTextHeading: activeComposedStyle.headingColor,
          colorTextSecondary: activeComposedStyle.mutedColor,
          colorFillAlter: activeComposedStyle.surfaceBackground,
          colorPrimary: activeComposedStyle.headingColor,
        },
        components: {
          Typography: {
            colorText: activeComposedStyle.bodyColor,
            colorTextHeading: activeComposedStyle.headingColor,
            colorTextDescription: activeComposedStyle.mutedColor,
          },
          Select: {
            selectorBg: activeComposedStyle.panelBackground,
            colorText: activeComposedStyle.bodyColor,
            colorTextPlaceholder: activeComposedStyle.mutedColor,
            optionSelectedBg: 'rgba(67, 104, 191, 0.1)',
            optionActiveBg: 'rgba(67, 104, 191, 0.06)',
            optionSelectedColor: activeComposedStyle.headingColor,
          },
          Button: {
            defaultColor: activeComposedStyle.bodyColor,
            defaultBg: activeComposedStyle.panelBackground,
            defaultBorderColor: activeComposedStyle.borderColor,
            defaultHoverColor: activeComposedStyle.headingColor,
            defaultHoverBg: activeComposedStyle.overlayBackground,
            defaultHoverBorderColor: activeComposedStyle.headingColor,
            colorPrimary: '#ffffff',
            colorPrimaryHover: '#ffffff',
            colorPrimaryActive: '#ffffff',
          },
        },
      }}
    >
      <Space direction="vertical" size={12} style={{ width: 280 }}>
        <div>
          <Text strong>阅读设置</Text>
          <div>
            <Text type="secondary">这些是低频调整项，收起后把阅读画布还给正文。</Text>
          </div>
        </div>
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Text type="secondary">主题</Text>
          <Select
            popupClassName="reader-composed-popover-select"
            size="small"
            style={{ width: '100%' }}
            value={themeMode}
            onChange={(value) => setThemeMode(value as ReaderThemeMode)}
            options={[
              { label: '浅色', value: 'light' },
              { label: '深色', value: 'dark' },
            ]}
          />
        </Space>
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Text type="secondary">细节层级</Text>
          <Select
            popupClassName="reader-composed-popover-select"
            size="small"
            style={{ width: '100%' }}
            value={detailLevel}
            onChange={(value) => setDetailLevel(value as ReaderDetailLevel)}
            options={[
              { label: '简洁', value: 'concise' },
              { label: '标准', value: 'standard' },
              { label: '深入', value: 'deep' },
            ]}
          />
        </Space>
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Text type="secondary">阅读风格</Text>
          <Select
            popupClassName="reader-composed-popover-select"
            size="small"
            style={{ width: '100%' }}
            value={generativeStyleKey}
            onChange={(value) => {
              const nextStyle = value as ReaderGenerativeStyleKey
              setGenerativeStyleKey(nextStyle)
              setGenerativeStyleTuning(
                normalizeReaderStyleTuning({}, GENERATIVE_STYLE_TOKENS[nextStyle].bodyLineHeight),
              )
            }}
            options={Object.entries(GENERATIVE_STYLE_LABELS).map(([value, label]) => ({ value, label }))}
          />
        </Space>
        <Space size={8} wrap>
          <Button
            size="small"
            type={compareMode ? 'primary' : 'default'}
            className="reader-settings-toggle-btn"
            onClick={() => setCompareMode((prev) => !prev)}
          >
            对比模式
          </Button>
          <Button
            size="small"
            type={citationTldr ? 'primary' : 'default'}
            className="reader-settings-toggle-btn"
            onClick={() => setCitationTldr((prev) => !prev)}
          >
            引用 TL;DR
          </Button>
        </Space>
      </Space>
    </ConfigProvider>
  )

  const renderAnswerWithCitations = () => {
    if (!askAnswer) {
      return <Text style={{ color: activeWorkspaceStyle.mutedColor }}>暂无回答</Text>
    }
    const answerMarkdown = normalizeAnswerMarkdown(askAnswer)
    return (
      <div
        style={{
          color: activeWorkspaceStyle.bodyColor,
          fontSize: 15,
          lineHeight: 1.85,
          whiteSpace: 'normal',
        }}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => (
              <p style={{ margin: '0 0 14px', color: activeWorkspaceStyle.bodyColor, lineHeight: 1.85 }}>
                {children}
              </p>
            ),
            h1: ({ children }) => (
              <h1 style={{ margin: '20px 0 10px', color: activeWorkspaceStyle.headingColor, fontSize: 26, fontWeight: 700 }}>
                {children}
              </h1>
            ),
            h2: ({ children }) => (
              <h2 style={{ margin: '18px 0 10px', color: activeWorkspaceStyle.headingColor, fontSize: 22, fontWeight: 700 }}>
                {children}
              </h2>
            ),
            h3: ({ children }) => (
              <h3 style={{ margin: '16px 0 8px', color: activeWorkspaceStyle.headingColor, fontSize: 18, fontWeight: 700 }}>
                {children}
              </h3>
            ),
            h4: ({ children }) => (
              <h4 style={{ margin: '14px 0 8px', color: activeWorkspaceStyle.headingColor, fontSize: 16, fontWeight: 700 }}>
                {children}
              </h4>
            ),
            ul: ({ children }) => (
              <ul style={{ margin: '0 0 14px 20px', color: activeWorkspaceStyle.bodyColor, paddingLeft: 0 }}>
                {children}
              </ul>
            ),
            ol: ({ children }) => (
              <ol style={{ margin: '0 0 14px 20px', color: activeWorkspaceStyle.bodyColor, paddingLeft: 0 }}>
                {children}
              </ol>
            ),
            li: ({ children }) => (
              <li style={{ marginBottom: 6, color: activeWorkspaceStyle.bodyColor, lineHeight: 1.8 }}>
                {children}
              </li>
            ),
            table: ({ children }) => (
              <div style={{ margin: '0 0 14px', overflowX: 'auto' }}>
                <table
                  style={{
                    width: '100%',
                    borderCollapse: 'collapse',
                    border: `1px solid ${activeWorkspaceStyle.borderColor}`,
                    borderRadius: 10,
                    overflow: 'hidden',
                    background: activeWorkspaceStyle.panelBackground,
                  }}
                >
                  {children}
                </table>
              </div>
            ),
            thead: ({ children }) => (
              <thead style={{ background: activeWorkspaceStyle.surfaceBackground, color: activeWorkspaceStyle.headingColor }}>
                {children}
              </thead>
            ),
            th: ({ children }) => (
              <th
                style={{
                  padding: '10px 12px',
                  borderBottom: `1px solid ${activeWorkspaceStyle.borderColor}`,
                  textAlign: 'left',
                  fontWeight: 700,
                }}
              >
                {children}
              </th>
            ),
            td: ({ children }) => (
              <td
                style={{
                  padding: '10px 12px',
                  borderTop: `1px solid ${activeWorkspaceStyle.borderColor}`,
                  color: activeWorkspaceStyle.bodyColor,
                  verticalAlign: 'top',
                }}
              >
                {children}
              </td>
            ),
            strong: ({ children }) => (
              <strong style={{ color: activeWorkspaceStyle.headingColor, fontWeight: 700 }}>{children}</strong>
            ),
            em: ({ children }) => (
              <em style={{ color: activeWorkspaceStyle.bodyColor }}>{children}</em>
            ),
            del: ({ children }) => (
              <del style={{ color: activeWorkspaceStyle.mutedColor }}>{children}</del>
            ),
            blockquote: ({ children }) => (
              <blockquote
                style={{
                  margin: '0 0 14px',
                  padding: '10px 14px',
                  borderLeft: `4px solid ${activeWorkspaceStyle.headingColor}`,
                  background: activeWorkspaceStyle.surfaceBackground,
                  color: activeWorkspaceStyle.bodyColor,
                  borderRadius: 8,
                }}
              >
                {children}
              </blockquote>
            ),
            code: ({ className, children }) => {
              const codeText = Array.isArray(children) ? children.join('') : String(children ?? '')
              const isInlineCode = !className && !codeText.includes('\n')
              return isInlineCode ? (
                <code
                  style={{
                    padding: '1px 6px',
                    borderRadius: 6,
                    background: activeWorkspaceStyle.surfaceBackground,
                    color: activeWorkspaceStyle.headingColor,
                    fontSize: 13,
                  }}
                >
                  {children}
                </code>
              ) : (
                <code>{children}</code>
              )
            },
            hr: () => (
              <hr
                style={{
                  margin: '18px 0',
                  border: 0,
                  borderTop: `1px solid ${activeWorkspaceStyle.borderColor}`,
                }}
              />
            ),
            input: ({ checked, disabled, type }) => {
              if (type !== 'checkbox') return <input checked={checked} disabled={disabled} type={type} readOnly />
              return (
                <input
                  checked={checked}
                  disabled={disabled}
                  type="checkbox"
                  readOnly
                  style={{
                    marginRight: 8,
                    accentColor: activeWorkspaceStyle.headingColor,
                  }}
                />
              )
            },
            pre: ({ children }) => (
              <pre
                style={{
                  margin: '0 0 14px',
                  padding: '12px 14px',
                  borderRadius: 10,
                  overflowX: 'auto',
                  border: `1px solid ${activeWorkspaceStyle.borderColor}`,
                  background: activeWorkspaceStyle.surfaceBackground,
                  color: activeWorkspaceStyle.bodyColor,
                  lineHeight: 1.7,
                }}
              >
                {children}
              </pre>
            ),
            a: ({ href, children }) => {
              const hrefText = String(href || '').trim()
              const sourceMatch = hrefText.match(/^source:\/\/(\d{1,3})$/)
              if (sourceMatch) {
                const sourceIndex = Number(sourceMatch[1])
                const targetSource = askSourcesByIndex.get(sourceIndex)
                return (
                  <Button
                    size="small"
                    type="link"
                    style={{
                      paddingInline: 2,
                      height: 'auto',
                      color: activeWorkspaceStyle.headingColor,
                      fontWeight: 600,
                    }}
                    disabled={!targetSource}
                    onClick={() => {
                      if (targetSource) void jumpToSource(targetSource)
                    }}
                  >
                    {`[来源${sourceIndex}]`}
                  </Button>
                )
              }
              return (
                <a
                  href={hrefText || '#'}
                  target="_blank"
                  rel="noreferrer"
                  style={{ color: activeWorkspaceStyle.headingColor, textDecoration: 'underline' }}
                >
                  {children}
                </a>
              )
            },
          }}
        >
          {answerMarkdown}
        </ReactMarkdown>
      </div>
    )
  }

  const renderGenerativeTextPanel = () => {
    const useComposedView =
      hasComposedPlan ||
      composedLoading ||
      Boolean(composedError) ||
      Boolean(composedPayload)
    const toAbsoluteApiUrl = (rawUrl: string): string => {
      const token = String(rawUrl || '').trim()
      if (!token) return ''
      if (/^https?:\/\//i.test(token) || token.startsWith('data:') || token.startsWith('blob:')) return token
      if (!token.startsWith('/')) return token
      if (!READER_API_BASE_URL) return token
      return `${READER_API_BASE_URL}${token}`
    }
    const resolveFigureImageUrl = (rawUrl: string, node?: ReaderComponentNode): string => {
      const token = String(rawUrl || '').trim()
      if (!token) return ''
      const assetPage = (() => {
        const pages = Array.isArray(node?.source_anchor_refs)
          ? node.source_anchor_refs
            .map((item) => Number((item as ReaderComponentSourceAnchor | undefined)?.page || 0))
            .filter((item) => Number.isFinite(item) && item > 0)
          : []
        return pages[0] || readPage
      })()
      const sourceBlockIds = Array.isArray(node?.source_block_ids)
        ? node!.source_block_ids.map((item) => String(item || '').trim()).filter((item) => item.length > 0)
        : []
      const pickImageHintUrl = (): string => {
        for (const asset of composedAssets) {
          if (asset.kind !== 'image_hint') continue
          const meta = (asset.meta && typeof asset.meta === 'object')
            ? asset.meta as Record<string, unknown>
            : {}
          const candidateUrl = String(asset.href || meta.image_url || '').trim()
          if (!candidateUrl || candidateUrl.startsWith('data:image/')) continue
          const layoutUniqueId = String(meta.layout_unique_id || meta.unique_id || '').trim()
          if (!sourceBlockIds.length || (layoutUniqueId && sourceBlockIds.includes(layoutUniqueId))) {
            return toAbsoluteApiUrl(candidateUrl)
          }
        }
        for (const asset of composedAssets) {
          if (asset.kind !== 'image_hint') continue
          const meta = (asset.meta && typeof asset.meta === 'object')
            ? asset.meta as Record<string, unknown>
            : {}
          const candidateUrl = String(asset.href || meta.image_url || '').trim()
          if (!candidateUrl || candidateUrl.startsWith('data:image/')) continue
          return toAbsoluteApiUrl(candidateUrl)
        }
        return ''
      }
      if (token.startsWith('data:image/')) {
        return token
      }
      if (token.startsWith('asset:')) {
        const assetId = token.slice('asset:'.length).trim()
        for (const asset of composedAssets) {
          if (asset.kind !== 'image_hint') continue
          const meta = (asset.meta && typeof asset.meta === 'object')
            ? asset.meta as Record<string, unknown>
            : {}
          const candidateId = String(meta.asset_id || meta.layout_unique_id || meta.unique_id || '').trim()
          const candidateUrl = String(
            asset.href || meta.image_url || '',
          ).trim()
          if (candidateUrl.startsWith('data:image/')) continue
          if (assetId && candidateId && candidateId === assetId && candidateUrl) {
            return toAbsoluteApiUrl(candidateUrl)
          }
        }
        if (assetId) {
          return toAbsoluteApiUrl(`/api/v1/literature/reader/figure-assets/${paperId}/${assetPage}/${assetId}`)
        }
        if (composedPageImageUrl) return toAbsoluteApiUrl(composedPageImageUrl)
        return ''
      }
      if (/^https?:\/\/(?:dx\.)?doi\.org\//i.test(token) && composedPageImageUrl) {
        return toAbsoluteApiUrl(composedPageImageUrl)
      }
      if (
        !token.startsWith('/')
        && !/^https?:\/\//i.test(token)
        && !token.startsWith('blob:')
        && !token.startsWith('data:')
      ) {
        const hinted = pickImageHintUrl()
        if (hinted) return hinted
      }
      return toAbsoluteApiUrl(token)
    }

    if (useComposedView) {
      return (
        <ConfigProvider
          theme={{
            algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
            token: {
              colorText: activeComposedStyle.bodyColor,
              colorTextHeading: activeComposedStyle.headingColor,
              colorBorder: activeComposedStyle.borderColor,
            },
            components: {
              Typography: {
                colorText: activeComposedStyle.bodyColor,
                colorTextHeading: activeComposedStyle.headingColor,
              },
            },
          }}
        >
          <div
            className="reader-composed-surface reader-workbench reader-workbench--embedded"
            style={{
              '--reader-card-bg': activeComposedStyle.panelBackground,
              '--reader-card-border': activeComposedStyle.borderColor,
              '--reader-text': activeComposedStyle.bodyColor,
              '--reader-heading': activeComposedStyle.headingColor,
              '--reader-muted-text': activeComposedStyle.mutedColor,
              '--reader-workbench-page-bg': activeComposedStyle.pageBackground,
              '--reader-workbench-surface-bg': activeComposedStyle.surfaceBackground,
              '--reader-workbench-rail-bg': activeComposedStyle.railBackground,
              '--reader-workbench-overlay-bg': activeComposedStyle.overlayBackground,
              '--reader-workbench-measure': `${composedContentMaxWidth}px`,
              '--reader-workbench-body-font': activeComposedStyle.bodyFontFamily,
              '--reader-workbench-heading-font': activeComposedStyle.headingFontFamily,
              border: `1px solid ${activeComposedStyle.borderColor}`,
              background: activeComposedStyle.pageBackground,
              boxShadow: '0 18px 42px rgba(15, 23, 42, 0.06), inset 0 1px 0 rgba(255, 255, 255, 0.86)',
              color: activeComposedStyle.bodyColor,
            } as CSSProperties}
          >
            <div className="reader-workbench__topbar">
              <div className="reader-workbench__meta">
                <div className="reader-workbench__eyebrow">
                  <Tag color="blue">AI Reader</Tag>
                  <Tag color="geekblue">第 {readPage} 页</Tag>
                  {effectiveComposePipelineVersion ? <Tag color="purple">{effectiveComposePipelineVersion}</Tag> : null}
                  {composedCacheLabel ? <Tag color="cyan">{composedCacheLabel}</Tag> : null}
                </div>
                <Title level={3} className="reader-workbench__title">
                  AI Reading Workbench
                </Title>
                <Text className="reader-workbench__subtitle">
                  {pageWordCount} 词 · {detailLevel === 'concise' ? '简洁' : detailLevel === 'deep' ? '深入' : '标准'}阅读 · {GENERATIVE_STYLE_LABELS[generativeStyleKey]} · {themeMode === 'dark' ? '深色纸面' : '浅色纸面'}
                </Text>
              </div>

              <div className="reader-workbench__controls">
                <Space size={8} wrap>
                  <Tag>{compareMode ? '对比开' : '对比关'}</Tag>
                  <Tag>{citationTldr ? 'TL;DR 开' : 'TL;DR 关'}</Tag>
                </Space>
                <Popover
                  trigger="click"
                  placement="bottomRight"
                  content={renderReaderSettingsContent()}
                  overlayClassName="reader-composed-popover"
                  styles={{
                    root: {
                      '--reader-card-bg': activeComposedStyle.panelBackground,
                      '--reader-card-border': activeComposedStyle.borderColor,
                      '--reader-text': activeComposedStyle.bodyColor,
                      '--reader-muted-text': activeComposedStyle.mutedColor,
                      '--reader-workbench-overlay-bg': activeComposedStyle.overlayBackground,
                    } as CSSProperties,
                  }}
                >
                  <Button size="small" icon={<SettingOutlined />}>
                    阅读设置
                  </Button>
                </Popover>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={composedLoading}
                  onClick={() => requestGenerativeRefresh()}
                >
                  重新生成
                </Button>
              </div>
            </div>

            <div ref={textModeContainerRef} className="reader-workbench__body reader-workbench__body--solo">
              <div className="reader-workbench__canvas">
                <div className="reader-workbench__surface reader-workbench__surface--plain">
                  {composedError ? (
                    <Alert
                      showIcon
                      type="warning"
                      message="AI 编排视图生成失败"
                      description={`${composedError}；已降级为本地结构化文本。`}
                      style={{ marginBottom: 12 }}
                    />
                  ) : null}

                  {composedLoading && !hasComposedPlan ? (
                    <div className="h-[360px] flex items-center justify-center">
                      <Spin />
                    </div>
                  ) : null}

                  {hasComposedPlan && activeComposedPlan ? (
                    <div className="reader-workbench__content" style={{ maxWidth: composedContentMaxWidth, margin: '0 auto', width: '100%' }}>
                      {composedMainComponents.length > 0 ? (
                        renderReaderComponentTree(composedMainComponents, {
                          themeStyle: activeComposedStyle,
                          qualityReport: composedQuality || composedPayload?.quality_report || null,
                          inlineQueryLoadingNodeId,
                          resolveFigureImageUrl: (imageUrl, node) => resolveFigureImageUrl(imageUrl, node),
                          isActionableAnchor,
                          onInlineQuery: async (node, question) => {
                            await handleInlineQuery(node, question)
                          },
                          onPreviewAnchors: (anchors, options) => {
                            showAnchorPreview(anchors, options)
                          },
                          onJumpAnchor: (anchors, options) => {
                            showAnchorPreview(anchors, { pinPreview: Boolean(options?.pinPreview ?? true) })
                          },
                          onHidePreview: () => {
                            hideAnchorPreview()
                          },
                          resolveAnchorPreviewImage: async (anchors, options) => {
                            return resolveAnchorPreviewImage(anchors, options)
                          },
                          onDropMarkdown: (markdown) => {
                            appendMarkdownToAnnotation(markdown)
                          },
                          onManualInsertSlot: (nodeId) => {
                            const targetNode = activeComposedPlan
                              ? findNodeInTree(activeComposedPlan.components || [], String(nodeId))
                              : null
                            const inheritedBlockIds = Array.isArray(targetNode?.source_block_ids)
                              ? targetNode.source_block_ids
                              : []
                            const inheritedAnchors = Array.isArray(targetNode?.source_anchor_refs)
                              ? targetNode.source_anchor_refs
                              : []
                            const inheritedAtomIds = Array.isArray(targetNode?.source_atom_ids)
                              ? targetNode.source_atom_ids
                              : []
                            const slotNode: ReaderComponentNode = {
                              id: `manual-slot-${Date.now()}`,
                              type: 'InlineQuerySlot',
                              props: {
                                placeholder: '请输入你对这段内容的追问（仅基于当前段落及邻近上下文）...',
                                target_node_ref: String(nodeId),
                              },
                              children: [],
                              source_block_ids: inheritedBlockIds,
                              source_atom_ids: inheritedAtomIds,
                              source_anchor_refs: inheritedAnchors,
                            }
                            applyNodeInsertToComposeState(nodeId, slotNode)
                          },
                        })
                      ) : (
                        <Empty description="当前页无可内联展示的阅读流内容，AI 资产已收纳到右侧栏。" />
                      )}
                    </div>
                  ) : null}

                  {!composedLoading && !hasComposedPlan ? (
                    <Empty description="当前页暂未生成可用的 AI 组件视图" />
                  ) : null}
                </div>
              </div>
            </div>
          </div>
        </ConfigProvider>
      )
    }

    const linkAssets = generativeAssets.filter((item) => item.kind === 'link')
    const annotationAssets = generativeAssets.filter((item) => item.kind === 'annotation')
    const imageHintAssets = generativeAssets.filter((item) => item.kind === 'image_hint')
    const effectiveLinks = linkAssets.length > 0
      ? linkAssets
        .map((item, index) => ({
          label: item.label || `链接${index + 1}`,
          href: String(item.href || ''),
          source: item.source === 'metadata' ? 'metadata' : 'text',
        }))
        .filter((item) => Boolean(item.href))
      : pageResourceLinks
    const sectionCount = generativeSections.length > 0 ? generativeSections.length : 0
    const summaryText = String(generativeSummary || generativePayload?.summary || '').trim()
    const sideCardStyle: CSSProperties = {
      border: `1px solid ${activeGenerativeStyle.borderColor}`,
      background: activeGenerativeStyle.panelBackground,
      borderRadius: 12,
      padding: '12px',
    }
    return (
      <div
        style={{
          border: `1px solid ${activeGenerativeStyle.borderColor}`,
          borderRadius: 12,
          background: activeGenerativeStyle.pageBackground,
          boxShadow: 'inset 0 1px 0 rgba(255, 255, 255, 0.85)',
        }}
      >
        <div
          style={{
            borderBottom: '1px solid rgba(79, 148, 255, 0.24)',
            padding: '10px 14px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 10,
            flexWrap: 'wrap',
          }}
        >
          <Space size={8} wrap>
            <Tag color="blue">Generative Reader</Tag>
            <Text style={{ color: '#1e3a8a' }}>第 {readPage} 页 · 词数: {pageWordCount}</Text>
            {sectionCount > 0 ? <Tag color="geekblue">{sectionCount} 个章节</Tag> : null}
            {generativeCacheLabel ? <Tag color="cyan">{generativeCacheLabel}</Tag> : null}
            {prefetchedPagesRef.current.has(readPage) ? <Tag color="green">已预读</Tag> : null}
          </Space>
          <Space size={8} wrap>
            <Text style={{ color: '#3b567a' }}>
              {generativeLayoutMode === 'split' ? '双列生成流' : '单列生成流'} · 原文增强阅读
            </Text>
            <Select
              size="small"
              style={{ minWidth: 168 }}
              value={generativeStyleKey}
              onChange={(value) => {
                // 切换风格会触发 useEffect 重新拉取该风格对应内容。
                const nextStyle = value as ReaderGenerativeStyleKey
                setGenerativeStyleKey(nextStyle)
                setGenerativeStyleTuning(
                  normalizeReaderStyleTuning({}, GENERATIVE_STYLE_TOKENS[nextStyle].bodyLineHeight),
                )
              }}
              options={Object.entries(GENERATIVE_STYLE_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={generativeLoading}
              onClick={() => requestGenerativeRefresh()}
            >
              重新生成
            </Button>
          </Space>
        </div>
        <div ref={textModeContainerRef} style={{ maxHeight: 650, overflowY: 'auto', padding: '18px 20px 24px' }}>
          {generativeError ? (
            <Alert
              showIcon
              type="warning"
              message="生成式阅读服务暂不可用"
              description={`${generativeError}；已自动降级为本地文本提取。`}
              style={{ marginBottom: 12 }}
            />
          ) : null}
          {generativeLoading && generativeBlocks.length === 0 ? (
            <div className="h-[560px] flex items-center justify-center">
              <Spin />
            </div>
          ) : displayedTextBlocks.length > 0 || rawPageTextPreview ? (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: generativeLayoutMode === 'split' ? '320px minmax(0, 1fr)' : '1fr',
                gap: 14,
                alignItems: 'start',
              }}
            >
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div style={sideCardStyle}>
                  <Text strong style={{ display: 'block', color: '#12305c' }}>{paper?.title || '未命名论文'}</Text>
                  <Text type="secondary">
                    {paper?.venue || '未知期刊'} {paper?.year ? `· ${paper?.year}` : ''}
                  </Text>
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">
                      作者：{paper?.authors?.slice(0, 3).map((item) => item.name).join(', ') || '未知作者'}
                      {paper?.authors && paper?.authors.length > 3 ? ` 等 ${paper?.authors.length} 位` : ''}
                    </Text>
                  </div>
                </div>

                <div style={sideCardStyle}>
                  <Text strong style={{ color: '#12305c' }}>章节摘要</Text>
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">
                      {summaryText || '当前页暂未生成摘要'}
                    </Text>
                  </div>
                </div>

                <div style={sideCardStyle}>
                  <Text strong style={{ color: '#12305c' }}>图注/图表线索</Text>
                  <div style={{ marginTop: 10 }}>
                    {imageHintAssets.length > 0 ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {imageHintAssets.slice(0, 6).map((item, idx) => (
                          <Tag key={`img-hint-${idx}`} color="default" style={{ marginInlineEnd: 0, whiteSpace: 'normal' }}>
                            {item.label}
                          </Tag>
                        ))}
                      </div>
                    ) : (
                      <Text type="secondary">当前页无图注线索；可切换 PDF 模式查看原页。</Text>
                    )}
                  </div>
                </div>

                <div style={sideCardStyle}>
                  <Space align="center" size={6}>
                    <LinkOutlined style={{ color: '#2f67ca' }} />
                    <Text strong style={{ color: '#12305c' }}>资源链接</Text>
                  </Space>
                  {effectiveLinks.length > 0 ? (
                    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {effectiveLinks.map((link, index) => (
                        <a
                          key={`${link.href}-${index}`}
                          href={link.href}
                          target="_blank"
                          rel="noreferrer"
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            gap: 8,
                            border: '1px solid rgba(93, 134, 210, 0.22)',
                            background: 'rgba(242, 247, 255, 0.95)',
                            borderRadius: 8,
                            padding: '8px 10px',
                            color: '#1f4f9d',
                            textDecoration: 'none',
                            fontSize: 12,
                          }}
                        >
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {link.label}
                          </span>
                          <Tag color={link.source === 'metadata' ? 'blue' : 'default'} style={{ marginInlineEnd: 0 }}>
                            {link.source === 'metadata' ? '元数据' : '页内'}
                          </Tag>
                        </a>
                      ))}
                    </div>
                  ) : (
                    <div style={{ marginTop: 10 }}>
                      <Text type="secondary">当前页未识别到可用链接</Text>
                    </div>
                  )}
                </div>

                <div style={sideCardStyle}>
                  <Space align="center" size={6}>
                    <PushpinOutlined style={{ color: '#2f67ca' }} />
                    <Text strong style={{ color: '#12305c' }}>页内注释</Text>
                  </Space>
                  {annotationAssets.length > 0 ? (
                    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {annotationAssets.slice(0, 6).map((item, idx) => (
                        <div
                          key={`anno-asset-${idx}`}
                          style={{
                            border: '1px solid rgba(93, 134, 210, 0.2)',
                            background: 'rgba(245, 249, 255, 0.94)',
                            borderRadius: 8,
                            padding: '8px 10px',
                          }}
                        >
                          <Text>{item.label || '(空内容)'}</Text>
                        </div>
                      ))}
                    </div>
                  ) : currentPageAnnotations.length > 0 ? (
                    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {currentPageAnnotations.slice(0, 6).map((item) => (
                        <div
                          key={item.id}
                          style={{
                            border: '1px solid rgba(93, 134, 210, 0.2)',
                            background: 'rgba(245, 249, 255, 0.94)',
                            borderRadius: 8,
                            padding: '8px 10px',
                          }}
                        >
                          <Space wrap size={6}>
                            <Tag color={item.annotation_type === 'highlight' ? 'gold' : 'blue'} style={{ marginInlineEnd: 0 }}>
                              {item.annotation_type === 'highlight' ? '高亮' : '笔记'}
                            </Tag>
                            <Text type="secondary">{String(item.updated_at || '').replace('T', ' ').slice(0, 16)}</Text>
                          </Space>
                          <div style={{ marginTop: 4 }}>
                            <Text>{item.content || item.quote_text || '(空内容)'}</Text>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={{ marginTop: 10 }}>
                      <Text type="secondary">当前页暂无批注，可在右侧“批注”面板添加。</Text>
                    </div>
                  )}
                </div>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div
                  style={{
                    border: '1px solid rgba(101, 154, 244, 0.22)',
                    background: activeGenerativeStyle.panelBackground,
                    borderRadius: 12,
                    padding: '16px 18px 14px',
                  }}
                >
                  <Text strong style={{ color: '#12305c' }}>页面正文（提取后结构化）</Text>
                  <div
                    style={{
                      marginTop: 14,
                      fontFamily: activeGenerativeStyle.bodyFontFamily,
                      fontSize: activeGenerativeStyle.bodyFontSize,
                      lineHeight: activeGenerativeStyle.bodyLineHeight,
                      color: activeGenerativeStyle.bodyColor,
                      textAlign: 'justify',
                      letterSpacing: '0.01em',
                    }}
                  >
                    {displayedTextBlocks.map((block, index) => {
                      if (block.kind === 'heading') {
                        const headingMatch = block.text.match(/^(\d+(?:\.\d+)*)/)
                        const headingDepth = headingMatch ? headingMatch[1].split('.').length : 1
                        const baseHeadingSize =
                          headingDepth <= 1
                            ? 26
                            : headingDepth === 2
                              ? 22
                              : 19
                        const headingSize = Math.round(baseHeadingSize * normalizedStyleTuning.heading_scale)
                        return (
                          <div
                            key={`heading-${index}`}
                            ref={(node) => {
                              if (node) headingRefMap.current.set(index, node)
                              else headingRefMap.current.delete(index)
                            }}
                            style={{
                              fontFamily: activeGenerativeStyle.headingFontFamily,
                              fontWeight: 700,
                              fontSize: headingSize,
                              letterSpacing: headingDepth <= 1 ? '0.005em' : '0.002em',
                              lineHeight: 1.45,
                              color: activeGenerativeStyle.headingColor,
                              borderBottom: '1px solid rgba(15, 76, 129, 0.18)',
                              paddingBottom: 4,
                              margin: headingDepth <= 1 ? '0.78em 0 0.52em' : '0.62em 0 0.4em',
                              textAlign: 'left',
                              background: sectionJumpHighlightIndex === index ? 'rgba(112, 184, 255, 0.22)' : 'transparent',
                              borderRadius: 8,
                              transition: 'background 0.25s ease',
                            }}
                          >
                            {block.text}
                          </div>
                        )
                      }

                      const prevBlock = index > 0 ? displayedTextBlocks[index - 1] : null
                      const noIndent = !prevBlock || prevBlock.kind === 'heading'
                      return (
                        <p
                          key={`paragraph-${index}`}
                          style={{
                            margin: '0 0 1.05em',
                            textIndent: noIndent ? 0 : '2em',
                          }}
                        >
                          {block.text}
                        </p>
                      )
                    })}
                  </div>
                </div>

              </div>
            </div>
          ) : (
            <Empty description="当前页暂无可提取文本（可能是扫描图像页）" />
          )}
        </div>
      </div>
    )
  }

  if (!validPaperId) return <Alert type="error" showIcon message="无效论文ID" />

  if (loading || !paper) {
    return (
      <div className="h-full flex items-center justify-center">
        <Spin />
      </div>
    )
  }

  const renderWidth = fitWidth ? viewerWidth : undefined
  const renderScale = fitWidth ? undefined : zoomPercent / 100
  const activeWorkspaceStyle = textMode ? activeComposedStyle : activeGenerativeStyle
  const workspaceSurfaceVars: CSSProperties = {
    '--reader-card-bg': activeWorkspaceStyle.panelBackground,
    '--reader-card-border': activeWorkspaceStyle.borderColor,
    '--reader-text': activeWorkspaceStyle.bodyColor,
    '--reader-heading': activeWorkspaceStyle.headingColor,
    '--reader-muted-text': activeWorkspaceStyle.mutedColor,
    '--reader-workbench-surface-bg': activeWorkspaceStyle.surfaceBackground,
    '--reader-workbench-overlay-bg': activeWorkspaceStyle.overlayBackground,
    '--reader-workbench-accent': activeWorkspaceStyle.headingColor,
  } as CSSProperties
  const panelCardStyle: CSSProperties = {
    ...workspaceSurfaceVars,
    border: `1px solid ${activeWorkspaceStyle.borderColor}`,
    borderRadius: 16,
    overflow: 'hidden',
    color: activeWorkspaceStyle.bodyColor,
    background: activeWorkspaceStyle.panelBackground,
    boxShadow: themeMode === 'dark'
      ? '0 18px 34px rgba(3, 7, 18, 0.28)'
      : '0 16px 34px rgba(15, 23, 42, 0.07)',
  }
  const panelHeaderStyle: CSSProperties = {
    padding: '13px 16px 11px',
    borderBottom: `1px solid ${activeWorkspaceStyle.borderColor}`,
    background: activeWorkspaceStyle.overlayBackground,
  }
  const panelOpsStyle: CSSProperties = {
    padding: '14px 16px',
    borderBottom: `1px dashed ${activeWorkspaceStyle.borderColor}`,
    background: activeWorkspaceStyle.surfaceBackground,
  }
  const panelListStyle: CSSProperties = {
    padding: '14px 16px',
    maxHeight: 'min(64vh, 660px)',
    overflowY: 'auto',
    background: activeWorkspaceStyle.panelBackground,
  }
  const workspacePrimaryButtonStyle: CSSProperties = themeMode === 'dark'
    ? {
      alignSelf: 'flex-start',
      height: 40,
      paddingInline: 22,
      color: '#eef4ff',
      borderColor: 'rgba(121, 167, 244, 0.24)',
      background: 'linear-gradient(180deg, rgba(74, 121, 208, 0.22) 0%, rgba(51, 86, 152, 0.16) 100%)',
      boxShadow: 'none',
      fontWeight: 600,
    }
    : {
      alignSelf: 'flex-start',
      height: 40,
      paddingInline: 22,
      color: '#153a82',
      borderColor: 'rgba(39, 80, 162, 0.18)',
      background: 'linear-gradient(180deg, rgba(255, 255, 255, 0.94) 0%, rgba(228, 238, 255, 0.98) 100%)',
      boxShadow: 'none',
      fontWeight: 600,
    }
  const workspaceSecondaryButtonStyle: CSSProperties = themeMode === 'dark'
    ? {
      height: 40,
      paddingInline: 18,
      color: 'rgba(223, 234, 255, 0.92)',
      borderColor: 'rgba(121, 167, 244, 0.18)',
      background: 'rgba(255, 255, 255, 0.04)',
      boxShadow: 'none',
    }
    : {
      height: 40,
      paddingInline: 18,
      color: activeWorkspaceStyle.bodyColor,
      borderColor: activeWorkspaceStyle.borderColor,
      background: activeWorkspaceStyle.surfaceBackground,
      boxShadow: 'none',
    }
  const renderThreeTierPanel = (
    title: string,
    subtitle: string,
    operations: ReactNode,
    listBlock: ReactNode,
  ) => (
    <ConfigProvider
      theme={{
        algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorBgContainer: activeWorkspaceStyle.panelBackground,
          colorBgElevated: activeWorkspaceStyle.overlayBackground,
          colorBorder: activeWorkspaceStyle.borderColor,
          colorSplit: activeWorkspaceStyle.borderColor,
          colorText: activeWorkspaceStyle.bodyColor,
          colorTextHeading: activeWorkspaceStyle.headingColor,
          colorTextSecondary: activeWorkspaceStyle.mutedColor,
          colorFillAlter: activeWorkspaceStyle.surfaceBackground,
        },
        components: {
          Typography: {
            colorText: activeWorkspaceStyle.bodyColor,
            colorTextHeading: activeWorkspaceStyle.headingColor,
            colorTextDescription: activeWorkspaceStyle.mutedColor,
          },
        },
      }}
    >
      <div className="reader-composed-surface reader-side-panel" style={panelCardStyle}>
        <div style={panelHeaderStyle}>
          <Text strong>{title}</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>{subtitle}</Text>
          </div>
        </div>
        <div style={panelOpsStyle}>{operations}</div>
        <div style={panelListStyle}>{listBlock}</div>
      </div>
    </ConfigProvider>
  )

  const renderAiContextPanel = () => {
    const sectionItems = [
      ...(composedContextComponents.length > 0 ? [{
        key: 'page-context',
        label: `AI 资产 · ${composedContextComponents.length}`,
        children: (
          <div style={{ maxHeight: 360, overflowY: 'auto', paddingRight: 4 }}>
            {renderReaderComponentTree(composedContextComponents, {
              themeStyle: activeComposedStyle,
              qualityReport: composedQuality || composedPayload?.quality_report || null,
              readOnly: true,
              resolveFigureImageUrl: (imageUrl, node) => {
                const token = String(imageUrl || '').trim()
                if (!token) return ''
                if (/^https?:\/\//i.test(token) || token.startsWith('data:') || token.startsWith('blob:')) return token
                if (token.startsWith('/')) return READER_API_BASE_URL ? `${READER_API_BASE_URL}${token}` : token
                return token
              },
              isActionableAnchor,
              onPreviewAnchors: (anchors, options) => {
                showAnchorPreview(anchors, options)
              },
              onJumpAnchor: (anchors, options) => {
                showAnchorPreview(anchors, { pinPreview: Boolean(options?.pinPreview ?? true) })
              },
              onHidePreview: () => {
                hideAnchorPreview()
              },
              resolveAnchorPreviewImage: async (anchors, options) => {
                return resolveAnchorPreviewImage(anchors, options)
              },
            })}
          </div>
        ),
      }] : []),
      {
        key: 'decisions',
        label: `AI 决策${composedDecisionLog.length ? ` · ${composedDecisionLog.length}` : ''}`,
        children: (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {composedPayload?.scheme_choice?.scheme_id ? (
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space size={8} wrap>
                  <Tag color="geekblue">{String(composedPayload.scheme_choice.scheme_id || '').trim()}</Tag>
                </Space>
                {composedPayload.scheme_choice.rationale ? (
                  <Text className="reader-workbench__rail-note">{String(composedPayload.scheme_choice.rationale || '').trim()}</Text>
                ) : null}
              </Space>
            ) : null}
            {composedDecisionLog.length > 0 ? (
              <div className="reader-workbench__decision-list">
                {composedDecisionLog.map((item, idx) => (
                  <div key={`compose-decision-${idx}`} className="reader-workbench__decision-item">
                    <Text>{item}</Text>
                  </div>
                ))}
              </div>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No decision log." />
            )}
          </Space>
        ),
      },
      {
        key: 'omissions',
        label: `Intentional Omissions${composedOmissions.length ? ` · ${composedOmissions.length}` : ''}`,
        children: composedOmissions.length > 0 ? (
          <div className="reader-workbench__omission-list">
            {composedOmissions.map((item, idx) => {
              const decision = String(item.decision || '').trim()
              const reason = String(item.reason || '').trim()
              return (
                <div key={`compose-omit-${idx}`} className="reader-workbench__omission-item">
                  <Space size={8} wrap>
                    {decision ? <Tag color={decision === 'hide' ? 'red' : (decision === 'collapse' ? 'gold' : 'blue')}>{decision}</Tag> : null}
                    {item.recoverable ? <Tag color="green">recoverable</Tag> : null}
                  </Space>
                  <div style={{ marginTop: 6 }}>
                    {reason ? <Text>{reason}</Text> : <Text type="secondary">未提供原因</Text>}
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No intentional omissions." />
        ),
      },
      {
        key: 'quality',
        label: '质量与定位',
        children: (
          <div className="reader-workbench__quality-list">
            <Space size={8} wrap>
              {composedQuality ? <Tag color="purple">质量 {Math.round((composedQuality.overall || 0) * 100)}/100</Tag> : null}
              {prefetchedPagesRef.current.has(readPage) ? <Tag color="green">已预读</Tag> : null}
              {composedQuality?.anchor_gate_passed === true ? <Tag color="green">定位门禁通过</Tag> : null}
              {composedQuality?.anchor_gate_passed === false ? <Tag color="red">定位门禁关闭</Tag> : null}
              {composedQuality?.mm_assist_used ? (
                <Tag color="magenta">
                  MM: {composedQuality.mm_model || 'enabled'}
                  {composedQuality.mm_fallback_used ? ' (fallback)' : ''}
                </Tag>
              ) : null}
              {typeof composedQuality?.cross_column_merge_ratio === 'number' ? (
                <Tag color={composedQuality.cross_column_merge_ratio <= 0.08 ? 'green' : 'gold'}>
                  跨栏 {(composedQuality.cross_column_merge_ratio * 100).toFixed(1)}%
                </Tag>
              ) : null}
              {typeof composedQuality?.duplicate_ratio === 'number' ? (
                <Tag color={composedQuality.duplicate_ratio <= 0.1 ? 'green' : 'orange'}>
                  重复 {(composedQuality.duplicate_ratio * 100).toFixed(1)}%
                </Tag>
              ) : null}
              {typeof composedQuality?.anchor_quote_hit_rate === 'number' ? (
                <Tag color={(composedQuality.anchor_quote_hit_rate || 0) >= 0.8 ? 'green' : 'orange'}>
                  命中 {(Number(composedQuality.anchor_quote_hit_rate || 0) * 100).toFixed(1)}%
                </Tag>
              ) : null}
              {typeof composedQuality?.anchor_bbox_iou === 'number' ? (
                <Tag color={(composedQuality.anchor_bbox_iou || 0) >= 0.25 ? 'green' : 'gold'}>
                  IoU {(Number(composedQuality.anchor_bbox_iou || 0) * 100).toFixed(1)}%
                </Tag>
              ) : null}
              {typeof composedQuality?.anchor_misjump_rate === 'number' ? (
                <Tag color={(composedQuality.anchor_misjump_rate || 1) <= 0.2 ? 'green' : 'red'}>
                  误跳 {(Number(composedQuality.anchor_misjump_rate || 0) * 100).toFixed(1)}%
                </Tag>
              ) : null}
            </Space>
            <div style={{ marginTop: 10 }}>
              {composedQuality?.stop_reason ? (
                <Text className="reader-workbench__rail-note">停止原因：{composedQuality.stop_reason}</Text>
              ) : (
                <Text className="reader-workbench__rail-note">当前证据、决策和质量收束在右栏，正文画布不再重复显示同一块信息。</Text>
              )}
            </div>
          </div>
        ),
      },
    ]
    if (composedLinkAssets.length > 0) {
      sectionItems.push({
        key: 'links',
        label: `Supplementary Links · ${Math.min(composedLinkAssets.length, 6)}`,
        children: (
          <div className="reader-workbench__link-list">
            {composedLinkAssets.slice(0, 6).map((item, idx) => {
              const href = String(item.href || '').trim()
              if (!href) return null
              return (
                <a key={`compose-link-${idx}`} href={href} target="_blank" rel="noreferrer">
                  {item.label || href}
                </a>
              )
            })}
          </div>
        ),
      })
    }

    return renderThreeTierPanel(
      'AI 上下文',
      '把证据、决策和质量说明固定在右栏，阅读画布只保留正文与图。',
      <Space wrap size={8}>
        <Tag color="blue">第 {readPage} 页</Tag>
        <Tag>{pageWordCount} 词</Tag>
        {effectiveComposePipelineVersion ? <Tag color="purple">{effectiveComposePipelineVersion}</Tag> : null}
        {composedCacheLabel ? <Tag color="cyan">{composedCacheLabel}</Tag> : null}
      </Space>,
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {renderAnchorEvidenceCard()}
        <Collapse
          bordered={false}
          defaultActiveKey={['decisions', 'quality']}
          items={sectionItems}
        />
      </Space>,
    )
  }

  return (
    <div className="p-4 space-y-4">
      <div>
        <Title level={4} className="!mb-1">{paper.title}</Title>
        <Text type="secondary">
          论文阅读工作台（PDF.js 真阅读器，支持文本层选择、缩放与引用跳转）
        </Text>
      </div>

      <Row gutter={16}>
        <Col span={textMode ? 17 : 16}>
          <Card
            title={(
              <Space align="center" size={10}>
                <Button icon={<ArrowLeftOutlined />} onClick={handleBackToList}>
                  返回
                </Button>
                <Text strong>阅读区</Text>
                {sectionLocating ? <Tag color="processing">章节定位中</Tag> : null}
              </Space>
            )}
          >
            <div
              style={{
                marginBottom: 12,
                padding: '12px 14px',
                borderRadius: 12,
                border: '1px solid rgba(110, 154, 235, 0.24)',
                background: 'rgba(11, 28, 58, 0.32)',
              }}
            >
              <Space wrap size={[10, 10]} style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space wrap size={8}>
                  <Button
                    icon={<LeftOutlined />}
                    disabled={readPage <= 1}
                    onClick={() => setReadPage((prev) => Math.max(1, prev - 1))}
                  />
                  <Input
                    style={{ width: 104 }}
                    type="number"
                    min={1}
                    max={pdfNumPages || undefined}
                    value={readPage}
                    onChange={(e) => {
                      const raw = Number(e.target.value || 1)
                      if (!Number.isFinite(raw) || raw <= 0) return
                      const maxPage = pdfNumPages > 0 ? pdfNumPages : raw
                      setReadPage(clamp(Math.round(raw), 1, maxPage))
                    }}
                    addonBefore="页"
                  />
                  <Button
                    icon={<RightOutlined />}
                    disabled={pdfNumPages > 0 && readPage >= pdfNumPages}
                    onClick={() => setReadPage((prev) => (pdfNumPages > 0 ? Math.min(pdfNumPages, prev + 1) : prev + 1))}
                  />
                  <Text type="secondary">/ {pdfNumPages > 0 ? pdfNumPages : '-'}</Text>
                </Space>
                <Space wrap size={8}>
                  <Button type={fitWidth ? 'primary' : 'default'} onClick={() => setFitWidth((prev) => !prev)}>
                    {fitWidth ? '已适宽' : '适宽'}
                  </Button>
                  <Button onClick={() => setTextMode((prev) => !prev)}>
                    {textMode ? '切到PDF' : '切到AI阅读'}
                  </Button>
                  <Button icon={<LinkOutlined />} onClick={handleOpenExperiencePage}>
                    展开页面
                  </Button>
                  <Tag color={readerAutoSaveTag.color}>{readerAutoSaveTag.label}</Tag>
                </Space>
              </Space>
              <Space wrap size={8} style={{ marginTop: 10 }}>
                <Text type="secondary">缩放</Text>
                <Slider
                  min={60}
                  max={240}
                  step={10}
                  value={zoomPercent}
                  onChange={(v) => setZoomPercent(Array.isArray(v) ? v[0] : v)}
                  style={{ width: 220 }}
                  disabled={fitWidth}
                />
                <Text>{zoomPercent}%</Text>
                <Text type={readerAutoSaveStatus === 'error' ? 'danger' : 'secondary'}>
                  {readerAutoSaveStatus === 'error'
                    ? `自动保存失败：${readerAutoSaveError || '网络异常'}`
                    : `最近自动保存：${readerAutoSaveAtText}`}
                </Text>
              </Space>
            </div>
            <div ref={viewerRef} style={{ width: '100%', minHeight: 720 }}>
              {pdfLoading ? (
                <div className="h-[680px] flex items-center justify-center">
                  <Spin />
                </div>
              ) : !pdfSource ? (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Empty description="该论文暂无可预览 PDF，请先下载或入库后再试" />
                  <Button onClick={() => loadPdfSource()}>重试加载 PDF</Button>
                </Space>
              ) : (
                <>
                  <div
                    style={{
                      overflow: 'auto',
                      maxHeight: 720,
                      border: '1px solid #f0f0f0',
                      borderRadius: 8,
                      display: textMode ? 'none' : 'block',
                    }}
                  >
                    <PdfDocument
                      file={pdfSource}
                      loading={(
                        <div className="h-[680px] flex items-center justify-center">
                          <Spin />
                        </div>
                      )}
                      onLoadSuccess={(doc: any) => {
                        setPdfDoc(doc)
                        setPdfNumPages(Number(doc?.numPages || 0))
                      }}
                      onLoadError={(error: unknown) => {
                        const msg = error instanceof Error ? error.message : '未知错误'
                        message.error(`PDF 加载失败: ${msg}`)
                        console.error('[PaperReader] PDF render error', error)
                      }}
                    >
                      <div className="p-3 flex justify-center">
                        <PdfPage
                          pageNumber={readPage}
                          width={renderWidth}
                          scale={renderScale}
                          renderAnnotationLayer
                          renderTextLayer
                        />
                      </div>
                    </PdfDocument>
                  </div>

                  {textMode ? renderGenerativeTextPanel() : null}
                </>
              )}
            </div>
          </Card>
        </Col>

        <Col span={textMode ? 7 : 8}>
          <ConfigProvider
            theme={{
              algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
              token: {
                colorBgContainer: activeWorkspaceStyle.panelBackground,
                colorBgElevated: activeWorkspaceStyle.overlayBackground,
                colorBorder: activeWorkspaceStyle.borderColor,
                colorSplit: activeWorkspaceStyle.borderColor,
                colorText: activeWorkspaceStyle.bodyColor,
                colorTextHeading: activeWorkspaceStyle.headingColor,
                colorTextSecondary: activeWorkspaceStyle.mutedColor,
                colorFillAlter: activeWorkspaceStyle.surfaceBackground,
              },
              components: {
                Typography: {
                  colorText: activeWorkspaceStyle.bodyColor,
                  colorTextHeading: activeWorkspaceStyle.headingColor,
                  colorTextDescription: activeWorkspaceStyle.mutedColor,
                },
              },
            }}
          >
            <div
              className={`reader-workspace-sidebar reader-workspace-sidebar--${themeMode}`}
              style={workspaceSurfaceVars}
            >
              <Tabs
                className="reader-workspace-tabs"
                activeKey={workspaceTab}
                onChange={(key) => setWorkspaceTab(key)}
                size="large"
                tabBarGutter={28}
                popupClassName="reader-workspace-tabs-popup"
                tabBarStyle={{ marginBottom: 14, paddingInline: 2 }}
                items={[
              ...(textMode ? [{
                key: 'ai_context',
                label: 'AI 上下文',
                children: renderAiContextPanel(),
              }] : []),
              {
                key: 'annotation',
                label: '批注',
                children: (
                  renderThreeTierPanel(
                    '批注工作区',
                    '在阅读过程中记录页级批注，并用摘要列表快速扫描。',
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      {editingAnnotationId ? (
                        <Space size={8} wrap>
                          <Tag color="processing">编辑中</Tag>
                          <Text type="secondary">保存后会覆盖原批注内容。</Text>
                        </Space>
                      ) : null}
                      <Space size={8} wrap>
                        <Tag color="blue">全部 {annotations.length}</Tag>
                        <Tag color="geekblue">当前页 {currentPageAnnotations.length}</Tag>
                      </Space>
                      <Space wrap size={10} style={{ width: '100%' }}>
                        <Select
                          value={annotationType}
                          onChange={(v) => setAnnotationType(v)}
                          options={[
                            { label: '笔记', value: 'note' },
                            { label: '高亮', value: 'highlight' },
                          ]}
                          style={{ width: 122 }}
                        />
                        <Input
                          type="number"
                          min={1}
                          max={pdfNumPages || undefined}
                          value={annotationPage}
                          onChange={(e) => {
                            const value = Number(e.target.value || 1)
                            if (!Number.isFinite(value) || value <= 0) return
                            const maxPage = pdfNumPages > 0 ? pdfNumPages : value
                            setAnnotationPage(clamp(Math.round(value), 1, maxPage))
                          }}
                          style={{ width: 112 }}
                          addonBefore="页"
                        />
                        <Button onClick={() => setAnnotationPage(readPage)}>定位到当前页</Button>
                      </Space>
                      <div onDragOver={handleAnnotationDragOver} onDrop={handleAnnotationDrop}>
                        <TextArea
                          ref={annotationInputRef}
                          rows={4}
                          value={annotationContent}
                          onChange={(e) => setAnnotationContent(e.target.value)}
                          placeholder={editingAnnotationId ? '编辑批注内容' : '输入批注内容（支持从左侧拖拽组件 Markdown 到此处）'}
                          style={{ borderRadius: 10 }}
                        />
                      </div>
                      <Space size={10} wrap>
                        <Button
                          loading={annotationSubmitting}
                          onClick={handleSaveAnnotation}
                          style={workspacePrimaryButtonStyle}
                        >
                          {editingAnnotationId ? '保存修改' : '新增批注'}
                        </Button>
                        {editingAnnotationId ? (
                          <Button
                            onClick={handleCancelEditAnnotation}
                            disabled={annotationSubmitting}
                            style={workspaceSecondaryButtonStyle}
                          >
                            取消编辑
                          </Button>
                        ) : null}
                      </Space>
                    </Space>,
                    <Space direction="vertical" size={10} style={{ width: '100%' }}>
                      <List
                        size="small"
                        dataSource={[...annotations].sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())}
                        style={{ maxHeight: 340, overflowY: 'auto', paddingRight: 4 }}
                        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无批注" /> }}
                        renderItem={(item) => (
                          <List.Item
                            actions={[
                              <Button key="jump" size="small" onClick={() => setReadPage(item.page)}>
                                跳转
                              </Button>,
                              <Button key="edit" size="small" onClick={() => handleStartEditAnnotation(item)}>
                                编辑
                              </Button>,
                              <Popconfirm
                                key="delete"
                                title="删除这条批注？"
                                description="删除后不可恢复。"
                                okText="删除"
                                cancelText="取消"
                                okButtonProps={{ danger: true, loading: deletingAnnotationId === item.id }}
                                onConfirm={async () => handleDeleteAnnotation(item.id)}
                              >
                                <Button size="small" danger loading={deletingAnnotationId === item.id}>
                                  删除
                                </Button>
                              </Popconfirm>,
                            ]}
                          >
                            <Space direction="vertical" size={4} style={{ width: '100%' }}>
                              <Space size={8} wrap>
                                <Text strong>第 {item.page} 页</Text>
                                <Tag color={item.annotation_type === 'highlight' ? 'gold' : 'blue'} style={{ marginInlineEnd: 0 }}>
                                  {item.annotation_type === 'highlight' ? '高亮' : '笔记'}
                                </Tag>
                                {editingAnnotationId === item.id ? <Tag color="processing">正在编辑</Tag> : null}
                              </Space>
                              <Paragraph
                                style={{ marginBottom: 0 }}
                                type="secondary"
                                ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
                              >
                                {item.content || item.quote_text || '(空)'}
                              </Paragraph>
                            </Space>
                          </List.Item>
                        )}
                      />
                    </Space>,
                  )
                ),
              },
              {
                key: 'comment',
                label: '评论',
                children: (
                  renderThreeTierPanel(
                    '评论区',
                    '查看公开评论，支持全部/同组筛选。',
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      <Radio.Group
                        value={commentFilter}
                        onChange={async (e) => {
                          const next = e.target.value as CommentFilter
                          setCommentFilter(next)
                          try {
                            await reloadComments(next)
                          } catch {
                            message.error('加载评论失败')
                          }
                        }}
                        options={[
                          { label: '全部', value: 'all' },
                          { label: '同组', value: 'same_group' },
                        ]}
                      />
                      <TextArea
                        rows={4}
                        value={commentText}
                        onChange={(e) => setCommentText(e.target.value)}
                        placeholder="输入评论"
                        style={{ borderRadius: 10 }}
                      />
                      <Button onClick={handleAddComment} style={workspacePrimaryButtonStyle}>
                        发布评论
                      </Button>
                    </Space>,
                    <List
                      size="small"
                      dataSource={comments}
                      style={{ maxHeight: 300, overflowY: 'auto', paddingRight: 4 }}
                      renderItem={(item) => (
                        <List.Item>
                          <Space direction="vertical" size={2}>
                            <Text strong>{item.author?.full_name || item.author?.username || `用户${item.user_id}`}</Text>
                            <Text>{item.content}</Text>
                          </Space>
                        </List.Item>
                      )}
                    />,
                  )
                ),
              },
              {
                key: 'rating',
                label: '评分/入库',
                children: (
                  renderThreeTierPanel(
                    '评分与入库',
                    '维护个人评分，并跟踪知识库处理状态。',
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      <Space align="center">
                        <Text>我的评分</Text>
                        <Rate value={ratingSummary?.my_rating || 0} onChange={handleRate} />
                      </Space>
                      <Space wrap>
                        <Tag>全站均分: {ratingSummary?.global_avg ?? '-'}</Tag>
                        <Tag>全站人数: {ratingSummary?.global_count ?? 0}</Tag>
                        <Tag>同组均分: {ratingSummary?.same_group_avg ?? '-'}</Tag>
                        <Tag>同组人数: {ratingSummary?.same_group_count ?? 0}</Tag>
                      </Space>
                      <Select
                        style={{ width: '100%' }}
                        placeholder="选择知识库"
                        options={kbOptions}
                        value={selectedKbId}
                        onChange={(v) => setSelectedKbId(v)}
                      />
                      <Button onClick={handleAddToKnowledge}>加入知识库</Button>
                    </Space>,
                    <List
                      size="small"
                      dataSource={knowledgeLinks}
                      renderItem={(item) => {
                        const normalized = normalizeKnowledgeLinkStatus(item.status)
                        const color =
                          normalized === 'completed'
                            ? 'green'
                            : normalized === 'failed'
                              ? 'red'
                              : normalized === 'pending'
                                ? 'gold'
                                : normalized === 'running'
                                  ? 'blue'
                                  : 'default'
                        return (
                          <List.Item>
                            <Space direction="vertical" size={2}>
                              <Text>KB#{item.knowledge_base_id}</Text>
                              <Tag color={color}>{normalized}</Tag>
                              {item.error_message ? <Text type="danger">{item.error_message}</Text> : null}
                            </Space>
                          </List.Item>
                        )
                      }}
                    />,
                  )
                ),
              },
              {
                key: 'ask',
                label: '询问',
                children: (
                  renderThreeTierPanel(
                    '询问工作区',
                    '配置提问范围与模式，查看回答、历史和可跳转来源。',
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                      <Radio.Group
                        value={askScope}
                        onChange={(e) => setAskScope(e.target.value as LiteratureAskScope)}
                        options={askScopeOptions}
                      />
                      <Radio.Group
                        value={askMode}
                        onChange={(e) => setAskMode(e.target.value as 'agentic' | 'classic')}
                        options={askModeOptions}
                      />
                      {askScope === 'collection' ? (
                        <Select
                          placeholder="选择收藏夹"
                          options={collectionOptions}
                          value={askCollectionId}
                          onChange={(v) => setAskCollectionId(v)}
                        />
                      ) : null}
                      <Select
                        placeholder="选择知识库"
                        options={kbOptions}
                        value={selectedKbId}
                        onChange={(v) => setSelectedKbId(v)}
                      />
                      {askScope === 'collection' && askCollectionId && selectedKbId ? (
                        collectionReadinessLoading ? (
                          <Space>
                            <Spin size="small" />
                            <Text type="secondary">正在检查收藏夹入库就绪度...</Text>
                          </Space>
                        ) : collectionReadiness ? (
                          <Alert
                            showIcon
                            type={collectionReadiness.can_cross_paper_answer ? 'info' : 'warning'}
                            message={
                              collectionReadiness.can_cross_paper_answer
                                ? `可跨论文联合回答：${collectionReadiness.completed_papers}/${collectionReadiness.total_papers} 篇已就绪`
                                : '当前收藏夹暂无可联合回答论文'
                            }
                            description={(
                              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                <Text type="secondary">
                                  联合回答仅覆盖 `completed` 状态论文；未入库/处理中/失败论文不会参与本轮答案。
                                </Text>
                                <Space wrap size={6}>
                                  <Tag color="green">completed: {collectionReadiness.completed_papers}</Tag>
                                  <Tag color="blue">running: {collectionReadiness.running_papers}</Tag>
                                  <Tag color="gold">pending: {collectionReadiness.pending_papers}</Tag>
                                  <Tag color="red">failed: {collectionReadiness.failed_papers}</Tag>
                                  <Tag color="orange">timeout: {collectionReadiness.timeout_papers}</Tag>
                                  <Tag color="purple">cancelled: {collectionReadiness.cancelled_papers}</Tag>
                                  <Tag>missing: {collectionReadiness.missing_papers}</Tag>
                                </Space>
                                {notReadyCollectionPapers.length > 0 ? (
                                  <Text type="secondary">
                                    未就绪示例：{notReadyCollectionPapers.slice(0, 3).map((item) => item.title).join('；')}
                                  </Text>
                                ) : null}
                              </Space>
                            )}
                          />
                        ) : null
                      ) : null}
                      <Select
                        placeholder="会话历史（仅自己可见）"
                        value={askSessionId}
                        allowClear
                        onChange={(v) => {
                          const next = Number(v || 0)
                          setAskSessionId(next > 0 ? next : undefined)
                        }}
                        options={askSessions.map((item) => ({
                          label: `${item.title || '未命名问题'} · ${String(item.updated_at || '').replace('T', ' ').slice(0, 16)}`,
                          value: item.id,
                        }))}
                      />
                      <TextArea
                        rows={3}
                        value={askQuestion}
                        onChange={(e) => setAskQuestion(e.target.value)}
                        placeholder="输入你的问题"
                      />
                      <Button type="primary" loading={asking} onClick={handleAsk}>
                        开始询问
                      </Button>
                    </Space>,
                    <Space direction="vertical" size={10} style={{ width: '100%' }}>
                      <Card size="small" title="回答">
                        {renderAnswerWithCitations()}
                      </Card>
                      <Card size="small" title="会话记录">
                        <List
                          size="small"
                          dataSource={askMessages}
                          locale={{ emptyText: '暂无会话消息' }}
                          renderItem={(item) => (
                            <List.Item>
                              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                                <Text strong>{item.role === 'assistant' ? '助手' : '我'}</Text>
                                <Text>{item.content}</Text>
                              </Space>
                            </List.Item>
                          )}
                        />
                      </Card>
                      <List
                        size="small"
                        header="引用来源（支持页码/章节定位）"
                        dataSource={askSources}
                        renderItem={(item, itemIndex) => {
                          const pageTextValue = item.page
                            ? `${item.page}${item.page_source === 'estimated' ? '（估算）' : ''}`
                            : '未知'
                          return (
                            <List.Item
                              actions={[
                                <Button key="jump" size="small" onClick={() => void jumpToSource(item)}>
                                  跳转
                                </Button>,
                              ]}
                            >
                              <Space direction="vertical" size={2}>
                                <Text strong>{`[来源${Number(item.idx || itemIndex + 1)}] ${item.document_name}`}</Text>
                                <Space wrap size={4}>
                                  <Tag>页码: {pageTextValue}</Tag>
                                  {item.section_title ? <Tag color="blue">章节: {item.section_title}</Tag> : null}
                                  {item.score_source === 'fallback' || item.score == null ? (
                                    <Tag color="default">分数: 无（回退检索）</Tag>
                                  ) : (
                                    <Tag>分数: {item.score}</Tag>
                                  )}
                                </Space>
                                <Text>{item.snippet}</Text>
                              </Space>
                            </List.Item>
                          )
                        }}
                      />
                    </Space>,
                  )
                ),
              },
                ]}
              />
            </div>
          </ConfigProvider>
        </Col>
      </Row>

      <div
        style={{
          display: 'none',
          position: 'fixed',
          bottom: 32,
          right: 'calc(33.33vw + 24px)',
          width: 440,
          zIndex: 1000,
          opacity: anchorPreview.visible ? 1 : 0,
          transform: anchorPreview.visible ? 'translateY(0)' : 'translateY(20px)',
          pointerEvents: anchorPreview.visible ? 'auto' : 'none',
          transition: 'all 0.25s cubic-bezier(0.16, 1, 0.3, 1)',
          boxShadow: '0 12px 48px rgba(0, 0, 0, 0.15), 0 4px 16px rgba(0,0,0,0.08)',
          borderRadius: 12,
        }}
      >
        <ConfigProvider
          theme={{
            algorithm: themeMode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
            token: {
              colorText: activeGenerativeStyle.bodyColor,
              colorTextHeading: activeGenerativeStyle.headingColor,
              colorBorder: activeGenerativeStyle.borderColor,
            },
            components: {
              Typography: {
                colorText: activeGenerativeStyle.bodyColor,
                colorTextHeading: activeGenerativeStyle.headingColor,
              }
            }
          }}
        >
          <Card
            size="small"
            title={anchorPreview.title || `原文证据 · 第 ${anchorPreview.page} 页`}
            style={{
              margin: 0,
              borderRadius: 12,
              border: `1px solid ${activeGenerativeStyle.borderColor}`,
              background: activeGenerativeStyle.panelBackground,
              color: activeGenerativeStyle.bodyColor,
            }}
            extra={(
              <Space size={8}>
                {anchorPreview.pinned ? (
                  <Tag color="blue">已钉住</Tag>
                ) : (
                  <Button
                    size="small"
                    onClick={() => setAnchorPreview((prev) => ({ ...prev, pinned: true, visible: true }))}
                  >
                    钉住
                  </Button>
                )}
                <Button
                  size="small"
                  onClick={() => {
                    setAnchorPreview((prev) => ({ ...prev, visible: false, pinned: false, loading: false }))
                  }}
                >
                  关闭
                </Button>
              </Space>
            )}
          >
            <Text type="secondary" style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>
              悬停组件可预览局部证据，点击“定位到证据”可固定并联动原 PDF 跳转。
            </Text>
            {anchorPreview.loading ? <Spin size="small" /> : (
              <div
                style={{
                  whiteSpace: 'pre-wrap',
                  lineHeight: 1.75,
                  maxHeight: 280,
                  overflowY: 'auto',
                  color: activeGenerativeStyle.bodyColor,
                  fontSize: 14,
                }}
              >
                {anchorPreview.text || '暂无可展示的锚点原文。'}
              </div>
            )}
          </Card>
        </ConfigProvider>
      </div>
    </div>
  )
}
