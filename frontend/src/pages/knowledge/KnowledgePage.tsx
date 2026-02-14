import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Card,
  Button,
  Input,
  Table,
  Tag,
  Space,
  Modal,
  Form,
  Upload,
  message,
  Tooltip,
  Empty,
  Spin,
  Statistic,
  Row,
  Col,
  Select,
  Switch,
  Collapse,
  Badge,
} from 'antd'
import {
  PlusOutlined,
  UploadOutlined,
  SearchOutlined,
  DeleteOutlined,
  FileTextOutlined,
  ReloadOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  ArrowLeftOutlined,
  ShareAltOutlined,
  SettingOutlined,
  FilterOutlined,
  CloseOutlined,
} from '@ant-design/icons'
import { useKnowledgeStore } from '@/stores/knowledgeStore'
import type { SearchResult } from '@/services/api'
import { isApiCanceledError, isApiTimeoutError, knowledgeApi } from '@/services/api'
import dayjs from 'dayjs'
import { KnowledgeBaseCard, SharedKnowledgeBaseCard, SearchResultCard } from './components'
import {
  getFileIcon,
  getStatusTag,
  formatFileSize,
  type SharedKnowledgeBase,
} from './utils'

const { TextArea } = Input

const getSearchStageText = (elapsedMs: number): string => {
  if (elapsedMs < 10000) return '编码中'
  if (elapsedMs < 60000) return '检索候选中'
  if (elapsedMs < 180000) return '重排压缩中'
  return '深度处理中'
}

type SearchLogLevel = 'info' | 'success' | 'warning' | 'error'

interface SearchLogEntry {
  id: number
  time: string
  level: SearchLogLevel
  message: string
}

const getSearchLogClassName = (level: SearchLogLevel): string => {
  if (level === 'success') return 'text-emerald-300'
  if (level === 'warning') return 'text-amber-300'
  if (level === 'error') return 'text-rose-300'
  return 'text-slate-300'
}

/**
 * KnowledgePage - 知识库管理页面（重构版）
 *
 * 已提取的子模块:
 *   - components/KnowledgeBaseCard       知识库卡片
 *   - components/SharedKnowledgeBaseCard  共享知识库卡片
 *   - components/SearchResultCard         搜索结果卡片
 *   - utils.ts                            工具函数 + 类型
 */
const KnowledgePage = () => {
  const navigate = useNavigate()
  const { kbId } = useParams()

  const {
    knowledgeBases,
    currentKnowledgeBase,
    documents,
    totalDocuments,
    searchResults,
    searchQuery,
    searchTime,
    isLoading,
    isUploading,
    isSearching,
    fetchKnowledgeBases,
    createKnowledgeBase,
    selectKnowledgeBase,
    deleteKnowledgeBase,
    fetchDocuments,
    uploadDocument,
    deleteDocument,
    refreshDocumentStatus,
    search,
    clearSearch,
    clearCurrentKnowledgeBase,
  } = useKnowledgeStore()

  const [createModalVisible, setCreateModalVisible] = useState(false)
  const [searchModalVisible, setSearchModalVisible] = useState(false)
  const [deleteModalVisible, setDeleteModalVisible] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<{ type: 'kb' | 'doc'; id: number } | null>(null)
  const [searchInput, setSearchInput] = useState('')
  const [form] = Form.useForm()

  // 高级搜索过滤器状态
  const [searchChunkLevel, setSearchChunkLevel] = useState<string>('paragraph')
  const [searchSectionType, setSearchSectionType] = useState<string | undefined>(undefined)
  const [searchIncludeParent, setSearchIncludeParent] = useState(false)
  const [searchUseQueryRewrite, setSearchUseQueryRewrite] = useState(true)
  const [searchUseHybrid, setSearchUseHybrid] = useState(true)
  const [searchUseReranker, setSearchUseReranker] = useState(true)
  const [searchUseContextualCompression, setSearchUseContextualCompression] = useState(true)
  const [searchIncludeAdjacent, setSearchIncludeAdjacent] = useState(false)
  const [searchAdjacentWindow, setSearchAdjacentWindow] = useState<number>(1)
  const [searchTimeoutMs, setSearchTimeoutMs] = useState<number>(300000)
  const [searchAbortController, setSearchAbortController] = useState<AbortController | null>(null)
  const [searchStageText, setSearchStageText] = useState('')
  const [searchElapsedSeconds, setSearchElapsedSeconds] = useState(0)
  const [searchFallbackUsed, setSearchFallbackUsed] = useState(false)
  const [searchFallbackReason, setSearchFallbackReason] = useState('')
  const [searchLogs, setSearchLogs] = useState<SearchLogEntry[]>([])

  const buildSearchLogEntry = useCallback((level: SearchLogLevel, message: string): SearchLogEntry => {
    return {
      id: Date.now() + Math.floor(Math.random() * 1000),
      time: dayjs().format('HH:mm:ss'),
      level,
      message,
    }
  }, [])

  const appendSearchLog = useCallback((level: SearchLogLevel, message: string) => {
    setSearchLogs((prev) => {
      const next = buildSearchLogEntry(level, message)
      return [...prev.slice(-79), next]
    })
  }, [buildSearchLogEntry])

  const resetSearchLogsWithInitial = useCallback((level: SearchLogLevel, message: string) => {
    setSearchLogs(() => [buildSearchLogEntry(level, message)])
  }, [buildSearchLogEntry])

  // 共享知识库状态
  const [sharedKnowledgeBases, setSharedKnowledgeBases] = useState<SharedKnowledgeBase[]>([])
  const [sharingEnabled, setSharingEnabled] = useState(false)

  // ─── 初始化 ────────────────────────────────────
  useEffect(() => {
    fetchKnowledgeBases()
    knowledgeApi
      .getAvailableKnowledgeBases()
      .then((data) => {
        setSharedKnowledgeBases(data.shared || [])
        setSharingEnabled(data.sharing_enabled || false)
      })
      .catch(() => setSharedKnowledgeBases([]))
  }, [])

  useEffect(() => {
    if (kbId) {
      selectKnowledgeBase(parseInt(kbId))
    } else {
      clearCurrentKnowledgeBase()
    }
  }, [kbId])

  // 轮询处理中的文档状态
  useEffect(() => {
    if (!currentKnowledgeBase) return
    const processingDocs = documents.filter(
      (d) => d.status === 'processing' || d.status === 'pending'
    )
    if (processingDocs.length === 0) return

    const interval = setInterval(() => {
      processingDocs.forEach((doc) => {
        refreshDocumentStatus(currentKnowledgeBase.id, doc.id)
      })
    }, 3000)

    return () => clearInterval(interval)
  }, [documents, currentKnowledgeBase])

  useEffect(() => {
    if (!isSearching) {
      setSearchStageText('')
      setSearchElapsedSeconds(0)
      return
    }

    const startedAt = Date.now()
    let lastStage = getSearchStageText(0)
    let heartbeatBucket = 0
    setSearchStageText(lastStage)
    setSearchElapsedSeconds(0)
    appendSearchLog('info', `阶段：${lastStage}`)
    const timer = setInterval(() => {
      const elapsedMs = Date.now() - startedAt
      const stage = getSearchStageText(elapsedMs)
      const elapsedSeconds = Math.floor(elapsedMs / 1000)
      setSearchStageText(stage)
      setSearchElapsedSeconds(elapsedSeconds)

      if (stage !== lastStage) {
        lastStage = stage
        appendSearchLog('info', `阶段切换：${stage}`)
      }

      const nextBucket = Math.floor(elapsedMs / 15000)
      if (nextBucket > heartbeatBucket) {
        heartbeatBucket = nextBucket
        appendSearchLog('info', `处理中，已等待 ${elapsedSeconds}s`)
      }
    }, 1000)

    return () => clearInterval(timer)
  }, [appendSearchLog, isSearching])

  // ─── 操作回调 ──────────────────────────────────
  const handleCreate = async (values: { name: string; description?: string }) => {
    try {
      const kb = await createKnowledgeBase(values.name, values.description)
      message.success('创建成功')
      setCreateModalVisible(false)
      form.resetFields()
      navigate(`/knowledge/${kb.id}`)
    } catch {
      // Error handled by store
    }
  }

  const handleUpload = async (file: File) => {
    if (!currentKnowledgeBase) return
    try {
      await uploadDocument(currentKnowledgeBase.id, file)
      message.success('上传成功，正在处理...')
    } catch (error: any) {
      // Error handled by store
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    try {
      if (deleteTarget.type === 'kb') {
        await deleteKnowledgeBase(deleteTarget.id)
        message.success('删除成功')
        navigate('/knowledge')
      } else if (currentKnowledgeBase) {
        await deleteDocument(currentKnowledgeBase.id, deleteTarget.id)
        message.success('删除成功')
      }
      setDeleteModalVisible(false)
      setDeleteTarget(null)
    } catch {
      // Error handled by store
    }
  }

  const handleSearch = async () => {
    if (!searchInput.trim()) return
    const kbIds = currentKnowledgeBase ? [currentKnowledgeBase.id] : undefined
    const primaryController = new AbortController()
    setSearchAbortController(primaryController)
    setSearchFallbackUsed(false)
    setSearchFallbackReason('')
    resetSearchLogsWithInitial(
      'info',
      `开始搜索：层级=${searchChunkLevel}，混合=${searchUseHybrid ? '开' : '关'}，改写=${searchUseQueryRewrite ? '开' : '关'}，精排=${searchUseReranker ? '开' : '关'}，压缩=${searchUseContextualCompression ? '开' : '关'}，超时=${Math.floor(searchTimeoutMs / 1000)}s`,
    )

    try {
      const primaryResponse = await search(
        searchInput,
        kbIds,
        undefined,
        searchChunkLevel,
        searchSectionType,
        searchIncludeParent,
        {
          useQueryRewrite: searchUseQueryRewrite,
          useHybrid: searchUseHybrid,
          useReranker: searchUseReranker,
          useContextualCompression: searchUseContextualCompression,
          includeAdjacentChunks: searchIncludeAdjacent,
          adjacentWindow: searchAdjacentWindow,
          timeoutMs: searchTimeoutMs,
          signal: primaryController.signal,
        },
      )
      const dimensions = Array.from(
        new Set(
          primaryResponse.results
            .map((item) => item.metadata?.retrieval_dimension)
            .filter((dim): dim is number => typeof dim === 'number'),
        ),
      )
      const rewriteCacheHit = primaryResponse.results.filter(
        (item) => item.metadata?.query_rewrite_cache_hit === true,
      ).length
      const compressionEnabled = primaryResponse.results.filter(
        (item) => item.metadata?.contextual_compression_enabled === true,
      ).length
      const compressionSkippedHighReranker = primaryResponse.results.filter(
        (item) => item.metadata?.contextual_compression_fallback === 'skip_high_reranker',
      ).length
      const compressionFallbackCount = primaryResponse.results.filter(
        (item) => Boolean(item.metadata?.contextual_compression_fallback),
      ).length
      appendSearchLog(
        'success',
        `主请求成功：${primaryResponse.results.length} 条，耗时 ${Math.round(primaryResponse.search_time_ms)}ms`,
      )
      appendSearchLog(
        'info',
        `结果摘要：维度=${dimensions.length > 0 ? dimensions.join('/') : '未知'}，改写缓存命中=${rewriteCacheHit}，实际压缩=${compressionEnabled}，高分跳过=${compressionSkippedHighReranker}，压缩回退=${compressionFallbackCount}`,
      )
      setSearchAbortController(null)
      return
    } catch (error) {
      if (isApiCanceledError(error)) {
        appendSearchLog('warning', '用户取消了主请求')
        setSearchAbortController(null)
        return
      }
      if (!isApiTimeoutError(error)) {
        appendSearchLog('error', '主请求失败（非超时）')
        setSearchAbortController(null)
        message.error('搜索失败，请稍后重试')
        return
      }
    }

    appendSearchLog('warning', '主请求超时，触发自动降级重试')
    message.warning('主请求超时，正在自动降级重试')
    const fallbackController = new AbortController()
    setSearchAbortController(fallbackController)
    try {
      const fallbackResponse = await search(
        searchInput,
        kbIds,
        undefined,
        searchChunkLevel,
        searchSectionType,
        searchIncludeParent,
        {
          useQueryRewrite: false,
          useHybrid: searchUseHybrid,
          useReranker: false,
          useContextualCompression: false,
          includeAdjacentChunks: searchIncludeAdjacent,
          adjacentWindow: searchAdjacentWindow,
          timeoutMs: 90000,
          signal: fallbackController.signal,
        },
      )
      setSearchFallbackUsed(true)
      setSearchFallbackReason('主请求超时后自动降级重试')
      appendSearchLog(
        'success',
        `降级重试成功：${fallbackResponse.results.length} 条，耗时 ${Math.round(fallbackResponse.search_time_ms)}ms`,
      )
      const fallbackFlagged = fallbackResponse.results.filter(
        (item) => item.metadata?.fallback_retry_used === true,
      ).length
      appendSearchLog('info', `降级标记结果数：${fallbackFlagged}`)
      useKnowledgeStore.setState((state) => ({
        searchResults: state.searchResults.map((item) => ({
          ...item,
          metadata: {
            ...(item.metadata || {}),
            fallback_retry_used: true,
            fallback_retry_reason: 'primary_timeout',
          },
        })),
      }))
    } catch (fallbackError) {
      if (isApiCanceledError(fallbackError)) {
        appendSearchLog('warning', '用户取消了降级重试')
        return
      }
      if (isApiTimeoutError(fallbackError)) {
        appendSearchLog('error', '主请求与降级重试均超时')
        message.error('检索超时：主请求与降级重试均超时，请缩短查询或关闭增强选项后重试')
        return
      }
      appendSearchLog('error', '降级重试失败（非超时）')
      message.error('降级重试失败，请稍后重试')
    } finally {
      setSearchAbortController(null)
    }
  }

  const handleCancelSearch = () => {
    if (!searchAbortController) return
    searchAbortController.abort()
    setSearchAbortController(null)
    setSearchStageText('')
    setSearchElapsedSeconds(0)
    appendSearchLog('warning', '用户手动取消搜索')
    message.info('已取消搜索')
  }

  // ─── 渲染：知识库列表 ─────────────────────────
  const renderKnowledgeBaseList = () => (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white mb-2">知识库</h1>
          <p className="text-slate-400">管理你的文档和知识，支持向量检索</p>
        </div>
        <Space>
          <Button
            icon={<SearchOutlined />}
            onClick={() => setSearchModalVisible(true)}
            className="bg-slate-700/50 border-slate-600 text-slate-300 hover:text-white hover:border-slate-500"
          >
            全局搜索
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateModalVisible(true)}
            className="bg-gradient-to-r from-emerald-500 to-teal-600 border-none"
          >
            新建知识库
          </Button>
        </Space>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-20"><Spin size="large" /></div>
      ) : knowledgeBases.length === 0 && sharedKnowledgeBases.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<span className="text-slate-500">暂无知识库</span>}
          className="py-20"
        >
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateModalVisible(true)}>
            创建第一个知识库
          </Button>
        </Empty>
      ) : (
        <>
          {knowledgeBases.length > 0 && (
            <>
              <h3 className="text-lg font-medium text-white mb-4">我的知识库</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
                {knowledgeBases.map((kb) => (
                  <KnowledgeBaseCard
                    key={kb.id}
                    kb={kb}
                    onClick={() => navigate(`/knowledge/${kb.id}`)}
                    onDelete={() => {
                      setDeleteTarget({ type: 'kb', id: kb.id })
                      setDeleteModalVisible(true)
                    }}
                  />
                ))}
              </div>
            </>
          )}
          {sharedKnowledgeBases.length > 0 && (
            <>
              <h3 className="text-lg font-medium text-white mb-4 flex items-center gap-2">
                <ShareAltOutlined className="text-purple-400" />
                共享给我的知识库
                <Tag color="purple" className="ml-2">{sharedKnowledgeBases.length}</Tag>
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {sharedKnowledgeBases.map((kb) => (
                  <SharedKnowledgeBaseCard
                    key={`shared-${kb.id}`}
                    kb={kb}
                    onClick={() => message.info('共享的知识库可以在 AI 对话中直接选择使用')}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )

  // ─── 渲染：知识库详情 ─────────────────────────
  const renderKnowledgeBaseDetail = () => {
    if (!currentKnowledgeBase) return null

    return (
      <div>
        {/* 头部 */}
        <div className="flex items-center gap-4 mb-6">
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate('/knowledge')} className="text-slate-400 hover:text-white" />
          <div className="flex-1">
            <h1 className="text-2xl font-bold text-white mb-1">{currentKnowledgeBase.name}</h1>
            {currentKnowledgeBase.description && <p className="text-slate-400">{currentKnowledgeBase.description}</p>}
          </div>
          <Space>
            <Button icon={<SearchOutlined />} onClick={() => setSearchModalVisible(true)} className="bg-slate-700/50 border-slate-600 text-slate-300 hover:text-white hover:border-slate-500">搜索</Button>
            <Button icon={<SettingOutlined />} onClick={() => navigate(`/knowledge/${currentKnowledgeBase.id}/chunking`)} className="bg-slate-700/50 border-slate-600 text-slate-300 hover:text-white hover:border-slate-500">分块配置</Button>
            <Upload
              accept=".txt,.md,.pdf,.html"
              showUploadList={false}
              beforeUpload={(file) => { handleUpload(file); return false }}
            >
              <Button type="primary" icon={<UploadOutlined />} loading={isUploading} className="bg-gradient-to-r from-emerald-500 to-teal-600 border-none">上传文档</Button>
            </Upload>
          </Space>
        </div>

        {/* 统计卡片 */}
        <Row gutter={16} className="mb-6">
          <Col span={6}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic title={<span className="text-slate-400">文档数</span>} value={currentKnowledgeBase.document_count} valueStyle={{ color: '#fff' }} prefix={<FileTextOutlined className="text-blue-400" />} />
            </Card>
          </Col>
          <Col span={6}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic title={<span className="text-slate-400">分片数</span>} value={currentKnowledgeBase.total_chunks} valueStyle={{ color: '#fff' }} prefix={<ThunderboltOutlined className="text-amber-400" />} />
            </Card>
          </Col>
          <Col span={6}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic
                title={<span className="text-slate-400">Token 数</span>}
                value={currentKnowledgeBase.total_tokens}
                valueStyle={{ color: '#fff' }}
                prefix={<DatabaseOutlined className="text-emerald-400" />}
                formatter={(value) => Number(value) > 1000 ? `${(Number(value) / 1000).toFixed(1)}k` : String(value)}
              />
            </Card>
          </Col>
          <Col span={6}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <div>
                <div className="text-slate-400 text-sm mb-2">嵌入模型</div>
                <div className="text-white font-medium text-base">{currentKnowledgeBase.embedding_model?.split('/').pop() || 'bge-m3'}</div>
                <div className="text-slate-500 text-xs mt-1">{currentKnowledgeBase.embedding_dimension || 1024} 维向量</div>
              </div>
            </Card>
          </Col>
        </Row>

        {/* 文档列表 */}
        <Card
          title={<span className="text-white">文档列表</span>}
          className="bg-slate-800/50 border-slate-700/50"
          extra={
            <Button type="text" icon={<ReloadOutlined />} onClick={() => fetchDocuments(currentKnowledgeBase.id)} className="text-slate-400 hover:text-white">刷新</Button>
          }
        >
          {isLoading ? (
            <div className="flex justify-center py-10"><Spin /></div>
          ) : documents.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span className="text-slate-500">暂无文档</span>}>
              <Upload accept=".txt,.md,.pdf,.html" showUploadList={false} beforeUpload={(file) => { handleUpload(file); return false }}>
                <Button type="primary" icon={<UploadOutlined />}>上传第一个文档</Button>
              </Upload>
            </Empty>
          ) : (
            <Table
              dataSource={documents}
              rowKey="id"
              pagination={false}
              className="custom-table"
              columns={[
                {
                  title: '文件名',
                  dataIndex: 'original_filename',
                  key: 'filename',
                  render: (text: string, record: any) => (
                    <div className="flex items-center gap-2">
                      {getFileIcon(record.file_type)}
                      <span className="text-slate-300">{text}</span>
                    </div>
                  ),
                },
                { title: '大小', dataIndex: 'file_size', key: 'size', width: 100, render: (size: number) => <span className="text-slate-400">{formatFileSize(size)}</span> },
                { title: '状态', dataIndex: 'status', key: 'status', width: 120, render: (status: string) => getStatusTag(status) },
                { title: '分片', dataIndex: 'chunk_count', key: 'chunks', width: 80, render: (count: number) => <span className="text-slate-400">{count}</span> },
                { title: '上传时间', dataIndex: 'created_at', key: 'created_at', width: 160, render: (date: string) => <span className="text-slate-400">{dayjs(date).format('YYYY-MM-DD HH:mm')}</span> },
                {
                  title: '操作',
                  key: 'action',
                  width: 80,
                  render: (_: any, record: any) => (
                    <Space>
                      <Tooltip title="删除">
                        <Button
                          type="text"
                          icon={<DeleteOutlined />}
                          danger
                          onClick={() => {
                            setDeleteTarget({ type: 'doc', id: record.id })
                            setDeleteModalVisible(true)
                          }}
                        />
                      </Tooltip>
                    </Space>
                  ),
                },
              ]}
            />
          )}
        </Card>
      </div>
    )
  }

  // ─── 主渲染 ────────────────────────────────────
  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-900 to-slate-950 p-6">
      <div className="max-w-6xl mx-auto">
        {kbId ? renderKnowledgeBaseDetail() : renderKnowledgeBaseList()}
      </div>

      {/* 创建知识库弹窗 */}
      <Modal title="新建知识库" open={createModalVisible} onCancel={() => setCreateModalVisible(false)} footer={null}>
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入知识库名称' }]}>
            <Input placeholder="输入知识库名称" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <TextArea placeholder="输入描述（可选）" rows={3} />
          </Form.Item>
          <Form.Item className="mb-0 text-right">
            <Space>
              <Button onClick={() => setCreateModalVisible(false)}>取消</Button>
              <Button type="primary" htmlType="submit">创建</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>

      {/* 搜索弹窗 */}
      <Modal
        title="向量搜索"
        open={searchModalVisible}
        onCancel={() => {
          if (searchAbortController) {
            searchAbortController.abort()
            setSearchAbortController(null)
          }
          setSearchModalVisible(false)
          clearSearch()
          setSearchInput('')
          setSearchFallbackUsed(false)
          setSearchFallbackReason('')
          setSearchStageText('')
          setSearchElapsedSeconds(0)
          setSearchLogs([])
        }}
        footer={null}
        width={720}
      >
        <div className="mb-4">
          <Input.Search
            placeholder="输入搜索内容..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onSearch={handleSearch}
            loading={isSearching}
            enterButton="搜索"
            size="large"
          />
        </div>
        {isSearching && (
          <div className="mb-3 flex items-center justify-between rounded border border-slate-700/60 bg-slate-900/50 px-3 py-2">
            <span className="text-slate-300 text-xs">
              {searchStageText || '处理中'}（已等待 {searchElapsedSeconds}s）
            </span>
            <Button
              size="small"
              icon={<CloseOutlined />}
              onClick={handleCancelSearch}
              className="border-slate-600 bg-slate-800/80 text-slate-200 hover:border-cyan-400 hover:text-cyan-200 hover:bg-slate-700/80"
              style={{ boxShadow: 'none' }}
            >
              取消
            </Button>
          </div>
        )}
        {searchFallbackUsed && (
          <div className="mb-3 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-amber-200 text-xs">
            降级结果：{searchFallbackReason || '已关闭改写/重排/压缩后重试返回'}
          </div>
        )}
        {(isSearching || searchLogs.length > 0) && (
          <div className="mb-3 rounded border border-slate-700/60 bg-slate-900/60">
            <div className="border-b border-slate-700/60 px-3 py-2 text-xs text-slate-400">
              检索过程日志（最新在下）
            </div>
            <div className="max-h-36 overflow-y-auto px-3 py-2 font-mono text-[12px] leading-5">
              {searchLogs.length === 0 ? (
                <div className="text-slate-500">等待日志输出...</div>
              ) : (
                searchLogs.map((log) => (
                  <div key={log.id} className={getSearchLogClassName(log.level)}>
                    [{log.time}] {log.message}
                  </div>
                ))
              )}
            </div>
          </div>
        )}
        <Collapse
          ghost
          className="mb-4"
          items={[{
            key: 'advanced',
            label: (
              <span className="text-slate-400 text-sm">
                <FilterOutlined className="mr-1" />
                高级过滤
                {(
                  searchChunkLevel !== 'paragraph'
                  || searchSectionType
                  || searchIncludeParent
                  || !searchUseQueryRewrite
                  || !searchUseHybrid
                  || !searchUseReranker
                  || !searchUseContextualCompression
                  || searchIncludeAdjacent
                  || searchAdjacentWindow !== 1
                  || searchTimeoutMs !== 300000
                ) && <Badge dot className="ml-2" />}
              </span>
            ),
            children: (
              <Row gutter={[16, 12]}>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">分块层级</div>
                  <Select value={searchChunkLevel} onChange={setSearchChunkLevel} size="small" className="w-full" options={[
                    { value: 'paragraph', label: '段落级' },
                    { value: 'section', label: '章节级' },
                    { value: 'document', label: '文档级' },
                    { value: 'all', label: '全部层级' },
                  ]} />
                </Col>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">章节类型</div>
                  <Select value={searchSectionType} onChange={setSearchSectionType} size="small" className="w-full" allowClear placeholder="不限" options={[
                    { value: 'abstract', label: '摘要' }, { value: 'introduction', label: '引言' },
                    { value: 'methodology', label: '方法' }, { value: 'results', label: '结果' },
                    { value: 'discussion', label: '讨论' }, { value: 'conclusion', label: '结论' },
                    { value: 'references', label: '参考文献' },
                  ]} />
                </Col>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">父级上下文</div>
                  <Switch checked={searchIncludeParent} onChange={setSearchIncludeParent} checkedChildren="开启" unCheckedChildren="关闭" size="small" />
                  <span className="text-slate-500 text-xs ml-2">回溯上级</span>
                </Col>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">相邻上下文</div>
                  <Switch
                    checked={searchIncludeAdjacent}
                    onChange={setSearchIncludeAdjacent}
                    checkedChildren="开启"
                    unCheckedChildren="关闭"
                    size="small"
                  />
                  <span className="text-slate-500 text-xs ml-2">返回前后窗口</span>
                </Col>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">相邻窗口大小</div>
                  <Select
                    value={searchAdjacentWindow}
                    onChange={setSearchAdjacentWindow}
                    size="small"
                    className="w-full"
                    disabled={!searchIncludeAdjacent}
                    options={[
                      { value: 1, label: '1（前后各1段）' },
                      { value: 2, label: '2（前后各2段）' },
                      { value: 3, label: '3（前后各3段）' },
                    ]}
                  />
                </Col>
                <Col span={8}>
                  <div className="text-slate-400 text-xs mb-1">请求超时</div>
                  <Select
                    value={searchTimeoutMs}
                    onChange={setSearchTimeoutMs}
                    size="small"
                    className="w-full"
                    options={[
                      { value: 90000, label: '90 秒' },
                      { value: 120000, label: '120 秒' },
                      { value: 300000, label: '300 秒（推荐）' },
                    ]}
                  />
                </Col>
                <Col span={12}>
                  <div className="text-slate-400 text-xs mb-1">Query Rewrite 改写</div>
                  <Switch
                    checked={searchUseQueryRewrite}
                    onChange={setSearchUseQueryRewrite}
                    checkedChildren="开启"
                    unCheckedChildren="关闭"
                    size="small"
                  />
                  <span className="text-slate-500 text-xs ml-2">扩展查询语义，召回更全</span>
                </Col>
                <Col span={12}>
                  <div className="text-slate-400 text-xs mb-1">Hybrid 混合检索</div>
                  <Switch
                    checked={searchUseHybrid}
                    onChange={setSearchUseHybrid}
                    checkedChildren="开启"
                    unCheckedChildren="关闭"
                    size="small"
                  />
                  <span className="text-slate-500 text-xs ml-2">向量 + 全文融合，兼顾精确与召回</span>
                </Col>
                <Col span={12}>
                  <div className="text-slate-400 text-xs mb-1">Reranker 精排</div>
                  <Switch
                    checked={searchUseReranker}
                    onChange={setSearchUseReranker}
                    checkedChildren="开启"
                    unCheckedChildren="关闭"
                    size="small"
                  />
                  <span className="text-slate-500 text-xs ml-2">提高排序质量，增加耗时</span>
                </Col>
                <Col span={12}>
                  <div className="text-slate-400 text-xs mb-1">Contextual Compression</div>
                  <Switch
                    checked={searchUseContextualCompression}
                    onChange={setSearchUseContextualCompression}
                    checkedChildren="开启"
                    unCheckedChildren="关闭"
                    size="small"
                  />
                  <span className="text-slate-500 text-xs ml-2">压缩上下文降低噪声，增加 LLM 开销</span>
                </Col>
              </Row>
            ),
          }]}
        />
        {searchResults.length > 0 && (
          <div className="mb-2 text-slate-400 text-sm">
            找到 {searchResults.length} 条结果，耗时 {searchTime.toFixed(2)}ms
          </div>
        )}
        <div className="max-h-96 overflow-y-auto">
          {searchResults.map((result, index) => (
            <SearchResultCard key={result.chunk_id} result={result} index={index} />
          ))}
          {searchResults.length === 0 && searchQuery && !isSearching && (
            <Empty description="暂无搜索结果" />
          )}
        </div>
      </Modal>

      {/* 删除确认弹窗 */}
      <Modal
        title="确认删除"
        open={deleteModalVisible}
        onOk={handleDelete}
        onCancel={() => { setDeleteModalVisible(false); setDeleteTarget(null) }}
        okText="删除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        <p>
          {deleteTarget?.type === 'kb'
            ? '确定要删除这个知识库吗？所有关联的文档和分片都将被删除。'
            : '确定要删除这个文档吗？此操作不可撤销。'}
        </p>
      </Modal>
    </div>
  )
}

export default KnowledgePage
