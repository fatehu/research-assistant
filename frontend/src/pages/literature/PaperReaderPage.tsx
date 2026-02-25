import { type CSSProperties, type DragEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeftOutlined,
  LeftOutlined,
  LinkOutlined,
  PushpinOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RightOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  List,
  message,
  Radio,
  Rate,
  Row,
  Select,
  Slider,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { Document as PdfDocument, Page as PdfPage, pdfjs } from 'react-pdf'
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
  ReaderPageReadyEventData,
  ReaderSession,
} from '@/services/api'
import {
  GENERATIVE_STYLE_LABELS,
  GENERATIVE_STYLE_TOKENS,
  normalizeGenerativeStyleKey,
  type ReaderThemeMode,
  resolveGenerativeStyleTokens,
} from './generativeStyles'
import { renderReaderComponentTree } from './readerComponents'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'

const { Title, Text } = Typography
const { TextArea } = Input

pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()

function parseZoomPercent(zoom: string | undefined): number {
  if (!zoom) return 120
  const value = Number(String(zoom).replace('%', '').trim())
  if (!Number.isFinite(value) || value <= 0) return 120
  return Math.max(60, Math.min(240, Math.round(value)))
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
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

type AnswerSegment =
  | { type: 'text'; value: string }
  | { type: 'citation'; value: string; index: number }

type PendingSectionJump = {
  sectionTitle: string
  expectedPage?: number
}

type ReaderDetailLevel = 'concise' | 'standard' | 'deep'

type AnchorPreviewState = {
  visible: boolean
  pinned: boolean
  loading: boolean
  page: number
  text: string
  title: string
  anchors: ReaderComponentSourceAnchor[]
}

function splitAnswerByCitation(answer: string): AnswerSegment[] {
  const value = String(answer || '')
  if (!value) return [{ type: 'text', value: '' }]
  const parts: AnswerSegment[] = []
  const citationRegex = /\[(?:来源)?(\d{1,3})\]/g
  let last = 0
  let match: RegExpExecArray | null = citationRegex.exec(value)
  while (match) {
    const full = match[0]
    const numText = match[1]
    const start = match.index
    if (start > last) {
      parts.push({ type: 'text', value: value.slice(last, start) })
    }
    const idx = Number(numText)
    if (Number.isFinite(idx) && idx > 0) {
      parts.push({ type: 'citation', value: full, index: idx })
    } else {
      parts.push({ type: 'text', value: full })
    }
    last = start + full.length
    match = citationRegex.exec(value)
  }
  if (last < value.length) {
    parts.push({ type: 'text', value: value.slice(last) })
  }
  return parts
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

function pickPrimaryAnchor(anchors: ReaderComponentSourceAnchor[]): ReaderComponentSourceAnchor | null {
  if (!Array.isArray(anchors) || anchors.length === 0) return null
  const anchor = anchors[0]
  if (!anchor || !Number.isFinite(anchor.page) || anchor.page <= 0) return null
  return anchor
}

function buildAnchorPreviewSnippet(rawText: string, anchor: ReaderComponentSourceAnchor): string {
  const text = String(rawText || '')
  if (!text) return ''
  const start = Math.max(0, Math.min(text.length, Number(anchor.start_char || 0)))
  const end = Math.max(start + 1, Math.min(text.length, Number(anchor.end_char || start + 1)))
  const previewStart = Math.max(0, start - 120)
  const previewEnd = Math.min(text.length, end + 180)
  return text.slice(previewStart, previewEnd).replace(/\s+/g, ' ').trim()
}

export default function PaperReaderPage() {
  const navigate = useNavigate()
  const { paperId } = useParams<{ paperId: string }>()
  const parsedPaperId = Number(paperId)
  const validPaperId = Number.isFinite(parsedPaperId) && parsedPaperId > 0

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
  const [composedRunSeed, setComposedRunSeed] = useState<number>(0)
  const [inlineQueryLoadingNodeId, setInlineQueryLoadingNodeId] = useState<string | null>(null)
  const [anchorPreview, setAnchorPreview] = useState<AnchorPreviewState>({
    visible: false,
    pinned: false,
    loading: false,
    page: 0,
    text: '',
    title: '',
    anchors: [],
  })

  const viewerRef = useRef<HTMLDivElement | null>(null)
  const textModeContainerRef = useRef<HTMLDivElement | null>(null)
  const headingRefMap = useRef<Map<number, HTMLDivElement>>(new Map())
  const sectionPageCacheRef = useRef<Map<string, number>>(new Map())
  const generativeStreamControllerRef = useRef<AbortController | null>(null)
  const composedStreamControllerRef = useRef<AbortController | null>(null)
  const pendingComposedRunRef = useRef<{ forceRefresh: boolean; regenerate: boolean }>({
    forceRefresh: false,
    regenerate: false,
  })
  const inlineQueryStreamControllerRef = useRef<AbortController | null>(null)
  const annotationInputRef = useRef<any>(null)
  const prefetchedPagesRef = useRef<Set<number>>(new Set())
  const [viewerWidth, setViewerWidth] = useState<number>(860)
  const pdfObjectUrlRef = useRef<string | null>(null)
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
  const pageResourceLinks = useMemo(
    () => collectPageResourceLinks(paper, rawPageText || pageText),
    [paper, rawPageText, pageText],
  )
  const rawPageTextPreview = useMemo(() => {
    const normalized = normalizeAcademicArtifacts(rawPageText || pageText)
    if (!normalized) return ''
    return normalized.length > 2400 ? `${normalized.slice(0, 2400)}...` : normalized
  }, [rawPageText, pageText])
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
    const restoredPage = Math.max(1, Number(cachedReader?.page || 0) || Number(nextSession.page || 1))
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
    setReadPage(restoredPage)
    setZoomPercent(restoredZoom)
    setFitWidth(restoredFitWidth)
    setTextMode(restoredReaderMode === 'generative')
    setGenerativeStyleKey(restoredStyleKey)
    setThemeMode(restoredThemeMode)
    setDetailLevel(restoredDetailLevel)
    setCompareMode(restoredCompareMode)
    setCitationTldr(restoredCitationTldr)
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
      cachedReader?.selected_kb_id || nextSession.selected_kb_id || nextPaper.knowledge_base_id || kbList[0]?.id,
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

  const requestGenerativeRefresh = (options?: { forceRefresh?: boolean; preferAgent?: boolean }) => {
    // 中文注释：把“本次刷新参数”写入 ref，仅供下一次请求消费。
    pendingComposedRunRef.current = {
      forceRefresh: Boolean(options?.forceRefresh),
      regenerate: Boolean(options?.preferAgent),
    }
    setComposedRunSeed((prev) => prev + 1)
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
    pendingComposedRunRef.current = { forceRefresh: false, regenerate: false }

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

    literatureApi
      .streamReaderComposed(
        parsedPaperId,
        {
          page: readPage,
          selected_kb_id: selectedKbId,
          force_refresh: runOptions.forceRefresh,
          regenerate: runOptions.regenerate,
          style_intent: generativeStyleKey,
          theme_mode: themeMode,
          detail_level: detailLevel,
          compare_mode: compareMode,
          citation_tldr: citationTldr,
        },
        (event, data) => {
          if (controller.signal.aborted) return

          if (event === 'start') {
            const startData = data as {
              cache_hit?: boolean
              cache_layer?: string
              build_mode?: string
            }
            const cacheLabel = startData.cache_hit
              ? `Cache hit (${startData.cache_layer || 'unknown'})`
              : `Built (${startData.build_mode || 'compose_agent'})`
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
            const doneData = data as { payload?: ReaderComposePayload }
            if (doneData.payload) {
              setComposedPayload(doneData.payload)
              setComposedPlan(doneData.payload.ui_plan || null)
              setComposedAssets(Array.isArray(doneData.payload.assets) ? doneData.payload.assets : [])
              setComposedQuality(doneData.payload.quality_report || null)
            }
            setComposedLoading(false)
            return
          }

          if (event === 'error') {
            const errorData = data as { message?: string }
            setComposedError(String(errorData.message || 'AI 组件编排失败，已降级到本地提取'))
            setComposedLoading(false)
          }
        },
        controller,
      )
      .catch((error) => {
        if (controller.signal.aborted) return
        const msg = error instanceof Error ? error.message : 'AI 组件编排失败，已降级到本地提取'
        setComposedError(msg)
        setComposedLoading(false)
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
    generativeStyleKey,
    themeMode,
    detailLevel,
    compareMode,
    citationTldr,
    composedRunSeed,
  ])

  useEffect(() => {
    if (!validPaperId) return
    const candidates = [readPage - 1, readPage + 1, readPage + 2].filter(
      (value) => value > 0 && (pdfNumPages <= 0 || value <= pdfNumPages),
    )
    if (candidates.length === 0) return
    literatureApi
      .prefetchReaderComposed(parsedPaperId, {
        pages: candidates,
        selected_kb_id: selectedKbId,
        style_intent: generativeStyleKey,
        theme_mode: themeMode,
        detail_level: detailLevel,
        compare_mode: compareMode,
        citation_tldr: citationTldr,
      })
      .then((result) => {
        if (Array.isArray(result.queued)) {
          result.queued.forEach((item) => prefetchedPagesRef.current.add(Number(item)))
        }
      })
      .catch(() => {
        // keep silent for prefetch errors
      })
  }, [
    validPaperId,
    parsedPaperId,
    readPage,
    pdfNumPages,
    selectedKbId,
    generativeStyleKey,
    themeMode,
    detailLevel,
    compareMode,
    citationTldr,
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
      literatureApi.getKnowledgeLinks(parsedPaperId)
        .then((links) => setKnowledgeLinks(links))
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

  const handleAddAnnotation = async () => {
    if (!validPaperId || !annotationContent.trim()) return
    try {
      const item = await literatureApi.createAnnotation(parsedPaperId, {
        annotation_type: annotationType,
        page: annotationPage,
        content: annotationContent.trim(),
        anchor: { page: annotationPage },
      })
      setAnnotations((prev) => [...prev, item])
      setAnnotationContent('')
      message.success('批注已添加')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '添加批注失败'
      message.error(msg)
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
      const links = await literatureApi.getKnowledgeLinks(parsedPaperId)
      setKnowledgeLinks(links)
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

    try {
      await literatureApi.streamReaderComposedInlineQuery(
        parsedPaperId,
        {
          page: readPage,
          node_id: String(node.id),
          question: compactQuestion,
          scope: 'section',
          selected_kb_id: selectedKbId,
          style_intent: generativeStyleKey,
          detail_level: detailLevel,
          compare_mode: compareMode,
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
    options?: { pinPreview?: boolean },
  ) => {
    const anchor = pickPrimaryAnchor(anchors)
    if (!anchor) return
    const nextPinned = Boolean(options?.pinPreview)
    const previewText = buildPreviewTextFromAnchor(anchor)
    setAnchorPreview({
      visible: true,
      pinned: nextPinned,
      loading: !previewText,
      page: Number(anchor.page || readPage),
      text: previewText || '正在加载该锚点原文片段...',
      title: `原文证据 · 第 ${Number(anchor.page || readPage)} 页`,
      anchors,
    })
    if (nextPinned && Number(anchor.page || 0) > 0 && Number(anchor.page || 0) !== readPage) {
      setReadPage(Number(anchor.page))
    }

    if (!previewText && pdfDoc && Number(anchor.page || 0) > 0) {
      const targetPage = Number(anchor.page)
      void (async () => {
        try {
          const page = await pdfDoc.getPage(targetPage)
          const textContent = await page.getTextContent()
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
          setAnchorPreview((prev) => {
            if (!prev.visible) return prev
            return {
              ...prev,
              loading: false,
              text: resolvedText || prev.text || '未检索到可展示的原文片段。',
            }
          })
        } catch {
          setAnchorPreview((prev) => {
            if (!prev.visible) return prev
            return { ...prev, loading: false, text: prev.text || '原文片段加载失败，请切换 PDF 模式核对。' }
          })
        }
      })()
    }
  }

  const hideAnchorPreview = () => {
    setAnchorPreview((prev) => {
      if (prev.pinned) return prev
      return { ...prev, visible: false, loading: false }
    })
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

  const renderAnswerWithCitations = () => {
    if (!askAnswer) {
      return <Text>暂无回答</Text>
    }
    const segments = splitAnswerByCitation(askAnswer)
    return (
      <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.85 }}>
        {segments.map((segment, idx) => {
          if (segment.type === 'text') {
            return <span key={`txt-${idx}`}>{segment.value}</span>
          }
          const targetSource = askSourcesByIndex.get(segment.index)
          return (
            <Button
              key={`cite-${idx}`}
              size="small"
              type="link"
              style={{ paddingInline: 2, height: 'auto' }}
              disabled={!targetSource}
              onClick={() => {
                if (targetSource) void jumpToSource(targetSource)
              }}
            >
              {segment.value}
            </Button>
          )
        })}
      </div>
    )
  }

  const renderGenerativeTextPanel = () => {
    const activeComposedPlan = composedPlan || composedPayload?.ui_plan || null
    const hasComposedPlan = Boolean(activeComposedPlan?.components?.length)
    const useComposedView =
      hasComposedPlan ||
      composedLoading ||
      Boolean(composedError) ||
      Boolean(composedPayload)
    const composedLinkAssets = composedAssets.filter((item) => item.kind === 'link' || item.kind === 'external_image')

    if (useComposedView) {
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
              <Tag color="blue">AI Composed Reader</Tag>
              <Text style={{ color: activeGenerativeStyle.headingColor }}>第 {readPage} 页 · 词数: {pageWordCount}</Text>
              {composedCacheLabel ? <Tag color="cyan">{composedCacheLabel}</Tag> : null}
              {prefetchedPagesRef.current.has(readPage) ? <Tag color="green">已预读</Tag> : null}
              {composedQuality ? <Tag color="purple">质量 {Math.round((composedQuality.overall || 0) * 100)}/100</Tag> : null}
            </Space>
            <Space size={8} wrap>
              <Select
                size="small"
                style={{ minWidth: 98 }}
                value={themeMode}
                onChange={(value) => setThemeMode(value as ReaderThemeMode)}
                options={[
                  { label: '浅色', value: 'light' },
                  { label: '深色', value: 'dark' },
                ]}
              />
              <Select
                size="small"
                style={{ minWidth: 118 }}
                value={detailLevel}
                onChange={(value) => setDetailLevel(value as ReaderDetailLevel)}
                options={[
                  { label: '简洁', value: 'concise' },
                  { label: '标准', value: 'standard' },
                  { label: '深入', value: 'deep' },
                ]}
              />
              <Button
                size="small"
                type={compareMode ? 'primary' : 'default'}
                onClick={() => setCompareMode((prev) => !prev)}
              >
                对比模式
              </Button>
              <Button
                size="small"
                type={citationTldr ? 'primary' : 'default'}
                onClick={() => setCitationTldr((prev) => !prev)}
              >
                引用TL;DR
              </Button>
              <Select
                size="small"
                style={{ minWidth: 168 }}
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
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={composedLoading}
                onClick={() => requestGenerativeRefresh({ forceRefresh: true, preferAgent: true })}
              >
                重新生成
              </Button>
            </Space>
          </div>

          <div ref={textModeContainerRef} style={{ maxHeight: 650, overflowY: 'auto', padding: '18px 20px 24px' }}>
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
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                {renderReaderComponentTree(activeComposedPlan.components, {
                  qualityReport: composedQuality || composedPayload?.quality_report || null,
                  inlineQueryLoadingNodeId,
                  onNodeAction: (node, action) => {
                    void handleComposedNodeAction(node, action)
                  },
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
                  onDropMarkdown: (markdown) => {
                    appendMarkdownToAnnotation(markdown)
                  },
                  onManualInsertSlot: (nodeId) => {
                    const slotNode: ReaderComponentNode = {
                      id: `manual-slot-${Date.now()}`,
                      type: 'InlineQuerySlot',
                      props: { placeholder: '请输入您关于上述段落的疑问...' },
                      children: [],
                      source_anchor_refs: [],
                    }
                    applyNodeInsertToComposeState(nodeId, slotNode)
                  },
                })}
              </div>
            ) : null}

            {!composedLoading && !hasComposedPlan ? (
              <Empty description="当前页暂未生成可用的 AI 组件视图" />
            ) : null}

            {composedLinkAssets.length > 0 ? (
              <Card size="small" title="补充资源" style={{ marginTop: 12, borderRadius: 12 }}>
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  {composedLinkAssets.slice(0, 6).map((item, idx) => {
                    const href = String(item.href || '').trim()
                    if (!href) return null
                    return (
                      <a key={`compose-link-${idx}`} href={href} target="_blank" rel="noreferrer">
                        {item.label || href}
                      </a>
                    )
                  })}
                </Space>
              </Card>
            ) : null}
          </div>
        </div>
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
                // 中文注释：切换风格会触发 useEffect 重新拉取该风格对应内容。
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
              onClick={() => requestGenerativeRefresh({ forceRefresh: true, preferAgent: true })}
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
  const panelCardStyle: CSSProperties = {
    border: '1px solid rgba(86, 151, 255, 0.24)',
    borderRadius: 14,
    overflow: 'hidden',
    background: 'linear-gradient(180deg, rgba(7,19,43,0.45) 0%, rgba(6,15,34,0.34) 100%)',
  }
  const panelHeaderStyle: CSSProperties = {
    padding: '12px 14px 10px',
    borderBottom: '1px solid rgba(86, 151, 255, 0.18)',
    background: 'rgba(8, 24, 52, 0.5)',
  }
  const panelOpsStyle: CSSProperties = {
    padding: '12px 14px',
    borderBottom: '1px dashed rgba(86, 151, 255, 0.16)',
  }
  const panelListStyle: CSSProperties = {
    padding: '12px 14px',
  }
  const renderThreeTierPanel = (
    title: string,
    subtitle: string,
    operations: ReactNode,
    listBlock: ReactNode,
  ) => (
    <div style={panelCardStyle}>
      <div style={panelHeaderStyle}>
        <Text strong>{title}</Text>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>{subtitle}</Text>
        </div>
      </div>
      <div style={panelOpsStyle}>{operations}</div>
      <div style={panelListStyle}>{listBlock}</div>
    </div>
  )

  return (
    <div className="p-4 space-y-4">
      <div>
        <Title level={4} className="!mb-1">{paper.title}</Title>
        <Text type="secondary">
          论文阅读工作台（PDF.js 真阅读器，支持文本层选择、缩放与引用跳转）
        </Text>
      </div>

      <Row gutter={16}>
        <Col span={16}>
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
                    {textMode ? 'PDF模式' : '生成式模式'}
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

        <Col span={8}>
          <Tabs
            defaultActiveKey="annotation"
            size="large"
            tabBarGutter={28}
            tabBarStyle={{ marginBottom: 14, paddingInline: 2 }}
            items={[
              {
                key: 'annotation',
                label: '批注',
                children: (
                  renderThreeTierPanel(
                    '批注工作区',
                    '在阅读过程中记录页级批注，并可一键回跳。',
                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
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
                          placeholder="输入批注内容（支持从左侧拖拽组件 Markdown 到此处）"
                          style={{ borderRadius: 10 }}
                        />
                      </div>
                      <Button type="primary" onClick={handleAddAnnotation} style={{ alignSelf: 'flex-start', height: 40, paddingInline: 22 }}>
                        新增批注
                      </Button>
                    </Space>,
                    <List
                      size="small"
                      dataSource={annotations}
                      style={{ maxHeight: 300, overflowY: 'auto', paddingRight: 4 }}
                      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无批注" /> }}
                      renderItem={(item) => (
                        <List.Item
                          actions={[
                            <Button key="jump" size="small" onClick={() => setReadPage(item.page)}>
                              跳转
                            </Button>,
                          ]}
                        >
                          <Space direction="vertical" size={2}>
                            <Text strong>第 {item.page} 页</Text>
                            <Text type="secondary">{item.content || item.quote_text || '(空)'}</Text>
                          </Space>
                        </List.Item>
                      )}
                    />,
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
                      <Button type="primary" onClick={handleAddComment} style={{ alignSelf: 'flex-start', height: 40, paddingInline: 22 }}>
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
        </Col>
      </Row>

      <div
        style={{
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
        <Card
          size="small"
          title={anchorPreview.title || `原文证据 · 第 ${anchorPreview.page} 页`}
          style={{
            margin: 0,
            borderRadius: 12,
            border: `1px solid ${activeGenerativeStyle.borderColor}`,
            background: activeGenerativeStyle.panelBackground,
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
      </div>
    </div>
  )
}


