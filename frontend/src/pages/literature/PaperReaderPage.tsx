import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
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
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import {
  AnnotationType,
  CommentFilter,
  knowledgeApi,
  LiteratureAskScope,
  literatureApi,
  Paper,
  PaperAnnotation,
  PaperComment,
  PaperCollection,
  PaperKnowledgeLink,
  PaperRatingSummary,
  ReaderSession,
  KnowledgeBase,
  LiteratureAskSource,
} from '@/services/api'

const { Title, Text } = Typography
const { TextArea } = Input

export default function PaperReaderPage() {
  const { paperId } = useParams<{ paperId: string }>()
  const parsedPaperId = Number(paperId)

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
  const [asking, setAsking] = useState<boolean>(false)

  const [readPage, setReadPage] = useState<number>(1)
  const [readZoom, setReadZoom] = useState<string>('100%')

  const validPaperId = Number.isFinite(parsedPaperId) && parsedPaperId > 0

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

  const reloadCoreData = async () => {
    if (!validPaperId) return
    const [
      nextPaper,
      nextSession,
      nextAnnotations,
      nextComments,
      nextRating,
      nextLinks,
      kbList,
      collList,
    ] = await Promise.all([
      literatureApi.getPaper(parsedPaperId),
      literatureApi.getReaderSession(parsedPaperId),
      literatureApi.getAnnotations(parsedPaperId),
      literatureApi.getComments(parsedPaperId, commentFilter),
      literatureApi.getRatingSummary(parsedPaperId),
      literatureApi.getKnowledgeLinks(parsedPaperId),
      knowledgeApi.getKnowledgeBases().then((r) => r.items),
      literatureApi.getCollections(),
    ])

    setPaper(nextPaper)
    setReaderSession(nextSession)
    setReadPage(nextSession.page || 1)
    setReadZoom(nextSession.zoom || '100%')
    setAnnotations(nextAnnotations)
    setComments(nextComments)
    setRatingSummary(nextRating)
    setKnowledgeLinks(nextLinks)
    setKnowledgeBases(kbList)
    setCollections(collList)

    const fallbackKb = nextSession.selected_kb_id || nextPaper.knowledge_base_id || kbList[0]?.id
    setSelectedKbId(fallbackKb)
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
        if (mounted) {
          setLoading(false)
        }
      })
    return () => {
      mounted = false
    }
  }, [parsedPaperId, validPaperId])

  const handleSaveReaderSession = async () => {
    if (!validPaperId) return
    try {
      const saved = await literatureApi.updateReaderSession(parsedPaperId, {
        page: readPage,
        zoom: readZoom,
        scroll_y: 0,
        selected_kb_id: selectedKbId,
        last_anchor: {},
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
        anchor: {},
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
            if (nextSession > 0) {
              setAskSessionId(nextSession)
            }
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

  if (!validPaperId) {
    return <Alert type="error" showIcon message="无效论文ID" />
  }

  if (loading || !paper) {
    return (
      <div className="h-full flex items-center justify-center">
        <Spin />
      </div>
    )
  }

  return (
    <div className="p-4 space-y-4">
      <div>
        <Title level={4} className="!mb-1">{paper.title}</Title>
        <Text type="secondary">论文阅读工作台</Text>
      </div>

      <Row gutter={16}>
        <Col span={16}>
          <Card
            title="阅读区"
            extra={
              <Space>
                <Input
                  style={{ width: 88 }}
                  type="number"
                  min={1}
                  value={readPage}
                  onChange={(e) => setReadPage(Number(e.target.value || 1))}
                  addonBefore="页"
                />
                <Select
                  style={{ width: 96 }}
                  value={readZoom}
                  onChange={(v) => setReadZoom(v)}
                  options={[
                    { label: '75%', value: '75%' },
                    { label: '100%', value: '100%' },
                    { label: '125%', value: '125%' },
                    { label: '150%', value: '150%' },
                  ]}
                />
                <Button onClick={handleSaveReaderSession}>保存位置</Button>
              </Space>
            }
          >
            {paper.pdf_url ? (
              <iframe
                title="paper-pdf"
                src={paper.pdf_url}
                style={{ width: '100%', height: 680, border: '1px solid #f0f0f0' }}
              />
            ) : (
              <Empty description="该论文暂无可预览 PDF，请先下载或入库后再试" />
            )}
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
                        value={annotationPage}
                        onChange={(e) => setAnnotationPage(Number(e.target.value || 1))}
                        style={{ width: 90 }}
                        addonBefore="页"
                      />
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
                        <List.Item>
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
                    <List
                      size="small"
                      header="引用来源"
                      dataSource={askSources}
                      renderItem={(item) => (
                        <List.Item>
                          <Space direction="vertical" size={2}>
                            <Text strong>{item.document_name}</Text>
                            <Text type="secondary">页码: {item.page ?? '未知'} | 分数: {item.score}</Text>
                            <Text>{item.snippet}</Text>
                          </Space>
                        </List.Item>
                      )}
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
