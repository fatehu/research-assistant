import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeftOutlined, LeftOutlined, QuestionCircleOutlined, RightOutlined } from '@ant-design/icons'
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
  Paper,
  PaperAnnotation,
  PaperCollection,
  PaperComment,
  PaperKnowledgeLink,
  PaperKnowledgeLinkStatusEventData,
  PaperRatingSummary,
  ReaderSession,
} from '@/services/api'
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
    .map((line) => stripLikelyPageNumberSuffix(line))
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
    if (isLikelyStandalonePageNumber(line.text)) {
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
    output.push(line.text)
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

  const viewerRef = useRef<HTMLDivElement | null>(null)
  const textModeContainerRef = useRef<HTMLDivElement | null>(null)
  const headingRefMap = useRef<Map<number, HTMLDivElement>>(new Map())
  const sectionPageCacheRef = useRef<Map<string, number>>(new Map())
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
  const pageWordCount = useMemo(() => {
    const text = pageText.trim()
    if (!text) return 0
    return text.split(/\s+/).filter(Boolean).length
  }, [pageText])
  const textColumnCount = useMemo(() => (viewerWidth >= 1080 ? 2 : 1), [viewerWidth])
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
    return collectionReadiness.papers.filter((item) => item.status !== 'ready')
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
            <Tooltip title="仅对收藏夹中 ready 论文做联合回答；未入库/处理中论文不会参与。">
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
    const restoredFitWidth = Boolean(
      (cachedReader?.last_anchor as Record<string, unknown> | undefined)?.fit_width ??
        (nextSession.last_anchor as Record<string, unknown> | undefined)?.fit_width ??
        true,
    )
    setReadPage(restoredPage)
    setZoomPercent(restoredZoom)
    setFitWidth(restoredFitWidth)
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
      last_anchor: { fit_width: restoredFitWidth },
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

  useEffect(() => {
    if (!validPaperId) return
    let mounted = true
    readerSessionHydratedRef.current = false
    lastSavedReaderSignatureRef.current = ''
    setReaderAutoSaveStatus('idle')
    setReaderAutoSaveError('')
    setReaderAutoSaveAt('')
    setLoading(true)
    reloadCoreData()
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
        if (!cancelled) setPageText(extracted || fallback)
      } catch {
        if (!cancelled) setPageText('')
      }
    }
    loadPageText()
    return () => {
      cancelled = true
    }
  }, [pdfDoc, readPage, pdfNumPages])

  useEffect(() => {
    writeJsonCache(readerCacheKey, {
      page: readPage,
      zoom: `${zoomPercent}%`,
      scroll_y: 0,
      selected_kb_id: selectedKbId,
      last_anchor: { fit_width: fitWidth },
      updated_at: new Date().toISOString(),
    })
  }, [readerCacheKey, readPage, zoomPercent, selectedKbId, fitWidth])

  useEffect(() => {
    if (!validPaperId || !readerSessionHydratedRef.current) return
    const payload = {
      page: readPage,
      zoom: `${zoomPercent}%`,
      scroll_y: 0,
      selected_kb_id: selectedKbId,
      last_anchor: { fit_width: fitWidth },
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
  }, [validPaperId, parsedPaperId, readPage, zoomPercent, selectedKbId, fitWidth])

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
    reloadAskSessions(askScope, askCollectionId).catch(() => {
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
    const hasProcessing = knowledgeLinks.some((item) => item.status === 'pending' || item.status === 'processing')
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

    const targetIndex = findBestSectionHeadingIndex(academicTextBlocks, pendingSectionJump.sectionTitle)
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
  }, [pendingSectionJump, textMode, readPage, academicTextBlocks])

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
      message.warning('当前收藏夹在所选知识库暂无 ready 论文，请先入库后再询问')
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

      const localHeadingIndex = findBestSectionHeadingIndex(academicTextBlocks, title)
      if (localHeadingIndex != null) {
        setTextMode(true)
        setPendingSectionJump({ sectionTitle: title, expectedPage: readPage })
        return
      }

      if (!pdfDoc || pdfNumPages <= 0) {
        setTextMode(true)
        message.info(`该引用缺少页码，已切换文本模式，请手动查找章节：${title}`)
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
          message.info({ key: msgKey, content: `未定位到章节“${title}”，已切换文本模式`, duration: 2.4 })
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
                    {textMode ? 'PDF模式' : '文本模式'}
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

                  {textMode ? (
                    <div
                      style={{
                        border: '1px solid rgba(79, 148, 255, 0.35)',
                        borderRadius: 12,
                        background:
                          'linear-gradient(180deg, rgba(249, 251, 255, 0.98) 0%, rgba(241, 246, 255, 0.98) 100%)',
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
                          <Tag color="blue">Academic Text</Tag>
                          <Text style={{ color: '#1e3a8a' }}>第 {readPage} 页</Text>
                          <Text style={{ color: '#1e3a8a' }}>词数: {pageWordCount}</Text>
                        </Space>
                        <Text style={{ color: '#3b567a' }}>
                          {textColumnCount === 2 ? '双栏阅读' : '单栏阅读'} · 学术排版
                        </Text>
                      </div>
                      <div ref={textModeContainerRef} style={{ maxHeight: 650, overflowY: 'auto', padding: '24px 28px' }}>
                        {!pdfDoc ? (
                          <div className="h-[560px] flex items-center justify-center">
                            <Spin />
                          </div>
                        ) : academicTextBlocks.length > 0 ? (
                          <div
                            style={{
                              margin: '0 auto',
                              maxWidth: textColumnCount === 2 ? 980 : 760,
                              fontFamily:
                                '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
                              fontSize: textColumnCount === 2 ? 17 : 18,
                              lineHeight: 2,
                              color: '#14223b',
                              textAlign: 'justify',
                              letterSpacing: '0.01em',
                              columnCount: textColumnCount,
                              columnGap: textColumnCount === 2 ? '3rem' : 'normal',
                            }}
                          >
                            {academicTextBlocks.map((block, index) => {
                              if (block.kind === 'heading') {
                                const headingMatch = block.text.match(/^(\d+(?:\.\d+)*)/)
                                const headingDepth = headingMatch ? headingMatch[1].split('.').length : 1
                                const headingSize =
                                  headingDepth <= 1
                                    ? textColumnCount === 2
                                      ? 24
                                      : 26
                                    : headingDepth === 2
                                      ? textColumnCount === 2
                                        ? 20
                                        : 22
                                      : textColumnCount === 2
                                        ? 18
                                        : 19
                                return (
                                  <div
                                    key={`heading-${index}`}
                                    ref={(node) => {
                                      if (node) headingRefMap.current.set(index, node)
                                      else headingRefMap.current.delete(index)
                                    }}
                                    style={{
                                      fontFamily:
                                        '"Source Han Serif SC","Noto Serif SC","Source Serif 4","Times New Roman",serif',
                                      fontWeight: 700,
                                      fontSize: headingSize,
                                      letterSpacing: headingDepth <= 1 ? '0.005em' : '0.002em',
                                      lineHeight: 1.5,
                                      color: '#102a50',
                                      borderBottom: '1px solid rgba(15, 76, 129, 0.18)',
                                      paddingBottom: 4,
                                      margin: headingDepth <= 1 ? '0.78em 0 0.52em' : '0.62em 0 0.4em',
                                      breakInside: 'avoid-column',
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

                              const prevBlock = index > 0 ? academicTextBlocks[index - 1] : null
                              const noIndent = !prevBlock || prevBlock.kind === 'heading'
                              return (
                                <p
                                  key={`paragraph-${index}`}
                                  style={{
                                    margin: '0 0 1.05em',
                                    textIndent: noIndent ? 0 : '2em',
                                    breakInside: 'avoid',
                                  }}
                                >
                                  {block.text}
                                </p>
                              )
                            })}
                          </div>
                        ) : (
                          <Empty description="当前页暂无可提取文本（可能是扫描图像页）" />
                        )}
                      </div>
                    </div>
                  ) : null}
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
                      <TextArea
                        rows={4}
                        value={annotationContent}
                        onChange={(e) => setAnnotationContent(e.target.value)}
                        placeholder="输入批注内容"
                        style={{ borderRadius: 10 }}
                      />
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
                      renderItem={(item) => (
                        <List.Item>
                          <Space direction="vertical" size={2}>
                            <Text>KB#{item.knowledge_base_id}</Text>
                            <Tag color={item.status === 'ready' ? 'green' : item.status === 'failed' ? 'red' : 'blue'}>
                              {item.status}
                            </Tag>
                            {item.error_message ? <Text type="danger">{item.error_message}</Text> : null}
                          </Space>
                        </List.Item>
                      )}
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
                                ? `可跨论文联合回答：${collectionReadiness.ready_papers}/${collectionReadiness.total_papers} 篇已就绪`
                                : '当前收藏夹暂无可联合回答论文'
                            }
                            description={(
                              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                                <Text type="secondary">
                                  联合回答仅覆盖 `ready` 状态论文；未入库/处理中/失败论文不会参与本轮答案。
                                </Text>
                                <Space wrap size={6}>
                                  <Tag color="green">ready: {collectionReadiness.ready_papers}</Tag>
                                  <Tag color="blue">processing: {collectionReadiness.processing_papers}</Tag>
                                  <Tag color="gold">pending: {collectionReadiness.pending_papers}</Tag>
                                  <Tag color="red">failed: {collectionReadiness.failed_papers}</Tag>
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
    </div>
  )
}
