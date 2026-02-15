import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { LeftOutlined, RightOutlined } from '@ant-design/icons'
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
  Typography,
} from 'antd'
import { Document as PdfDocument, Page as PdfPage, pdfjs } from 'react-pdf'
import {
  AnnotationType,
  CommentFilter,
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

export default function PaperReaderPage() {
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
  const [askCollectionId, setAskCollectionId] = useState<number | undefined>(undefined)
  const [askQuestion, setAskQuestion] = useState<string>('')
  const [askAnswer, setAskAnswer] = useState<string>('')
  const [askSources, setAskSources] = useState<LiteratureAskSource[]>([])
  const [askSessionId, setAskSessionId] = useState<number | undefined>(undefined)
  const [askSessions, setAskSessions] = useState<LiteratureAskSession[]>([])
  const [askMessages, setAskMessages] = useState<LiteratureAskMessage[]>([])
  const [asking, setAsking] = useState<boolean>(false)

  const [readPage, setReadPage] = useState<number>(1)
  const [zoomPercent, setZoomPercent] = useState<number>(120)
  const [fitWidth, setFitWidth] = useState<boolean>(true)
  const [textMode, setTextMode] = useState<boolean>(false)

  const [pdfSource, setPdfSource] = useState<string | undefined>(undefined)
  const [pdfLoading, setPdfLoading] = useState<boolean>(false)
  const [pdfNumPages, setPdfNumPages] = useState<number>(0)
  const [pdfDoc, setPdfDoc] = useState<any>(null)
  const [pageText, setPageText] = useState<string>('')

  const viewerRef = useRef<HTMLDivElement | null>(null)
  const [viewerWidth, setViewerWidth] = useState<number>(860)
  const pdfObjectUrlRef = useRef<string | null>(null)
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
      collection_id?: number
      question?: string
      session_id?: number
    }>(askDraftKey)

    setPaper(nextPaper)
    setReaderSession(nextSession)
    setReadPage(
      Math.max(1, Number(cachedReader?.page || 0) || Number(nextSession.page || 1)),
    )
    setZoomPercent(parseZoomPercent(String(cachedReader?.zoom || nextSession.zoom || '120%')))
    setFitWidth(
      Boolean(
        (cachedReader?.last_anchor as Record<string, unknown> | undefined)?.fit_width ??
          (nextSession.last_anchor as Record<string, unknown> | undefined)?.fit_width ??
          true,
      ),
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
    let cancelled = false
    const loadPageText = async () => {
      if (!pdfDoc || !readPage || (pdfNumPages > 0 && readPage > pdfNumPages)) {
        setPageText('')
        return
      }
      try {
        const page = await pdfDoc.getPage(readPage)
        const textContent = await page.getTextContent()
        const merged = textContent.items
          .map((item: any) => (typeof item?.str === 'string' ? item.str : ''))
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim()
        if (!cancelled) setPageText(merged)
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
      collection_id: askCollectionId,
      question: askQuestion,
      session_id: askSessionId,
      updated_at: new Date().toISOString(),
    })
  }, [askDraftKey, askScope, askCollectionId, askQuestion, askSessionId])

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

  const handleSaveReaderSession = async () => {
    if (!validPaperId) return
    try {
      const saved = await literatureApi.updateReaderSession(parsedPaperId, {
        page: readPage,
        zoom: `${zoomPercent}%`,
        scroll_y: 0,
        selected_kb_id: selectedKbId,
        last_anchor: { fit_width: fitWidth },
      })
      setReaderSession(saved)
      message.success('阅读位置已保存')
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '保存阅读位置失败'
      message.error(msg)
    }
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

  const jumpToSource = (source: LiteratureAskSource) => {
    if (source.page && source.page > 0) {
      setReadPage(source.page)
      setTextMode(false)
      return
    }
    if (source.section_title) {
      message.info(`该引用缺少精确页码，可先查看章节：${source.section_title}`)
      return
    }
    message.info('该引用缺少可跳转定位信息')
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
            title="阅读区"
            extra={(
              <Space wrap>
                <Button
                  icon={<LeftOutlined />}
                  disabled={readPage <= 1}
                  onClick={() => setReadPage((prev) => Math.max(1, prev - 1))}
                />
                <Input
                  style={{ width: 96 }}
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
                <Text type="secondary">
                  / {pdfNumPages > 0 ? pdfNumPages : '-'}
                </Text>
                <Space size={4}>
                  <Text type="secondary">缩放</Text>
                  <Slider
                    min={60}
                    max={240}
                    step={10}
                    value={zoomPercent}
                    onChange={(v) => setZoomPercent(Array.isArray(v) ? v[0] : v)}
                    style={{ width: 140 }}
                    disabled={fitWidth}
                  />
                  <Text>{zoomPercent}%</Text>
                </Space>
                <Button type={fitWidth ? 'primary' : 'default'} onClick={() => setFitWidth((prev) => !prev)}>
                  {fitWidth ? '已适宽' : '适宽'}
                </Button>
                <Button onClick={() => setTextMode((prev) => !prev)}>
                  {textMode ? 'PDF模式' : '文本模式'}
                </Button>
                <Button onClick={handleSaveReaderSession}>保存位置</Button>
              </Space>
            )}
          >
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
              ) : textMode ? (
                <Card size="small" title={`第 ${readPage} 页文本`} bordered>
                  <div
                    style={{
                      maxHeight: 680,
                      overflowY: 'auto',
                      whiteSpace: 'pre-wrap',
                      lineHeight: 1.8,
                      fontSize: 15,
                    }}
                  >
                    {pageText || '当前页暂无可提取文本（可能是扫描图像页）。'}
                  </div>
                </Card>
              ) : (
                <div style={{ overflow: 'auto', maxHeight: 720, border: '1px solid #f0f0f0', borderRadius: 8 }}>
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
              )}
            </div>
          </Card>
        </Col>

        <Col span={8}>
          <Tabs
            defaultActiveKey="annotation"
            items={[
              {
                key: 'annotation',
                label: '批注',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Space>
                      <Select
                        value={annotationType}
                        onChange={(v) => setAnnotationType(v)}
                        options={[
                          { label: '笔记', value: 'note' },
                          { label: '高亮', value: 'highlight' },
                        ]}
                        style={{ width: 100 }}
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
                        style={{ width: 96 }}
                        addonBefore="页"
                      />
                      <Button onClick={() => setAnnotationPage(readPage)}>当前页</Button>
                    </Space>
                    <TextArea
                      rows={3}
                      value={annotationContent}
                      onChange={(e) => setAnnotationContent(e.target.value)}
                      placeholder="输入批注内容"
                    />
                    <Button type="primary" onClick={handleAddAnnotation}>
                      新增批注
                    </Button>
                    <List
                      size="small"
                      dataSource={annotations}
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
                    />
                  </Space>
                ),
              },
              {
                key: 'comment',
                label: '评论',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
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
                      rows={3}
                      value={commentText}
                      onChange={(e) => setCommentText(e.target.value)}
                      placeholder="输入评论"
                    />
                    <Button type="primary" onClick={handleAddComment}>
                      发布评论
                    </Button>
                    <List
                      size="small"
                      dataSource={comments}
                      renderItem={(item) => (
                        <List.Item>
                          <Space direction="vertical" size={2}>
                            <Text strong>{item.author?.full_name || item.author?.username || `用户${item.user_id}`}</Text>
                            <Text>{item.content}</Text>
                          </Space>
                        </List.Item>
                      )}
                    />
                  </Space>
                ),
              },
              {
                key: 'rating',
                label: '评分/入库',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
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
                    />
                  </Space>
                ),
              },
              {
                key: 'ask',
                label: '询问',
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Radio.Group
                      value={askScope}
                      onChange={(e) => setAskScope(e.target.value as LiteratureAskScope)}
                      options={[
                        { label: '当前论文', value: 'paper' },
                        { label: '当前收藏夹', value: 'collection' },
                      ]}
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
                    <Card size="small" title="回答">
                      <Text>{askAnswer || '暂无回答'}</Text>
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
                      renderItem={(item) => {
                        const pageTextValue = item.page
                          ? `${item.page}${item.page_source === 'estimated' ? '（估算）' : ''}`
                          : '未知'
                        return (
                          <List.Item
                            actions={[
                              <Button key="jump" size="small" onClick={() => jumpToSource(item)}>
                                跳转
                              </Button>,
                            ]}
                          >
                            <Space direction="vertical" size={2}>
                              <Text strong>{item.document_name}</Text>
                              <Space wrap size={4}>
                                <Tag>页码: {pageTextValue}</Tag>
                                {item.section_title ? <Tag color="blue">章节: {item.section_title}</Tag> : null}
                                <Tag>分数: {item.score}</Tag>
                              </Space>
                              <Text>{item.snippet}</Text>
                            </Space>
                          </List.Item>
                        )
                      }}
                    />
                  </Space>
                ),
              },
            ]}
          />
        </Col>
      </Row>
    </div>
  )
}
