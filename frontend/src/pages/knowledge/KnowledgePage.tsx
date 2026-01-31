import { useState, useEffect, useRef } from 'react'
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
  Progress,
  Empty,
  Spin,
  Dropdown,
  Drawer,
  Descriptions,
  List,
  Typography,
  Statistic,
  Row,
  Col,
  Badge,
} from 'antd'
import {
  PlusOutlined,
  UploadOutlined,
  SearchOutlined,
  DeleteOutlined,
  FolderOutlined,
  FileTextOutlined,
  FilePdfOutlined,
  FileMarkdownOutlined,
  FileOutlined,
  ReloadOutlined,
  MoreOutlined,
  EyeOutlined,
  DatabaseOutlined,
  CloudUploadOutlined,
  ThunderboltOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
  LoadingOutlined,
  ExclamationCircleOutlined,
  ArrowLeftOutlined,
  ShareAltOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import { useKnowledgeStore } from '@/stores/knowledgeStore'
import type { KnowledgeBase, Document, SearchResult } from '@/services/api'
import { knowledgeApi } from '@/services/api'
import dayjs from 'dayjs'

const { TextArea } = Input
const { Text, Paragraph } = Typography

// 共享知识库类型
interface SharedKnowledgeBase {
  id: number
  name: string
  description?: string
  document_count: number
  total_chunks: number
  owner_id: number
  owner_name: string
}

// 文件类型图标映射
const getFileIcon = (fileType: string) => {
  switch (fileType.toLowerCase()) {
    case 'pdf':
      return <FilePdfOutlined className="text-red-400" />
    case 'md':
    case 'markdown':
      return <FileMarkdownOutlined className="text-blue-400" />
    case 'txt':
      return <FileTextOutlined className="text-slate-400" />
    default:
      return <FileOutlined className="text-slate-400" />
  }
}

// 状态标签映射
const getStatusTag = (status: string) => {
  switch (status) {
    case 'pending':
      return <Tag icon={<ClockCircleOutlined />} color="default">等待处理</Tag>
    case 'processing':
      return <Tag icon={<LoadingOutlined spin />} color="processing">处理中</Tag>
    case 'completed':
      return <Tag icon={<CheckCircleOutlined />} color="success">已完成</Tag>
    case 'failed':
      return <Tag icon={<ExclamationCircleOutlined />} color="error">失败</Tag>
    default:
      return <Tag color="default">{status}</Tag>
  }
}

// 格式化文件大小
const formatFileSize = (bytes: number) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

// 知识库卡片组件
const KnowledgeBaseCard = ({
  kb,
  onClick,
  onDelete,
}: {
  kb: KnowledgeBase
  onClick: () => void
  onDelete: () => void
}) => {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      whileHover={{ scale: 1.02 }}
      className="cursor-pointer"
      onClick={onClick}
    >
      <Card
        className="bg-slate-800/50 border-slate-700/50 hover:border-emerald-500/50 transition-all"
        bodyStyle={{ padding: '20px' }}
      >
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 flex items-center justify-center">
              <DatabaseOutlined className="text-2xl text-emerald-400" />
            </div>
            <div>
              <h3 className="text-white font-medium text-lg">{kb.name}</h3>
              <p className="text-slate-500 text-sm">
                {dayjs(kb.updated_at).format('YYYY-MM-DD HH:mm')}
              </p>
            </div>
          </div>
          <Dropdown
            menu={{
              items: [
                {
                  key: 'delete',
                  icon: <DeleteOutlined />,
                  label: '删除',
                  danger: true,
                  onClick: (e) => {
                    e.domEvent.stopPropagation()
                    onDelete()
                  },
                },
              ],
            }}
            trigger={['click']}
          >
            <Button
              type="text"
              icon={<MoreOutlined />}
              onClick={(e) => e.stopPropagation()}
              className="text-slate-400 hover:text-white"
            />
          </Dropdown>
        </div>
        
        {kb.description && (
          <p className="text-slate-400 text-sm mb-4 line-clamp-2">{kb.description}</p>
        )}
        
        <div className="grid grid-cols-3 gap-4">
          <div className="text-center">
            <div className="text-2xl font-bold text-white">{kb.document_count}</div>
            <div className="text-xs text-slate-500">文档</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-white">{kb.total_chunks}</div>
            <div className="text-xs text-slate-500">分片</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-white">
              {kb.total_tokens > 1000 ? `${(kb.total_tokens / 1000).toFixed(1)}k` : kb.total_tokens}
            </div>
            <div className="text-xs text-slate-500">Tokens</div>
          </div>
        </div>
      </Card>
    </motion.div>
  )
}

// 共享知识库卡片组件（只读）
const SharedKnowledgeBaseCard = ({
  kb,
  onClick,
}: {
  kb: SharedKnowledgeBase
  onClick: () => void
}) => {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      whileHover={{ scale: 1.02 }}
      className="cursor-pointer"
      onClick={onClick}
    >
      <Card
        className="bg-slate-800/50 border-slate-700/50 hover:border-purple-500/50 transition-all"
        bodyStyle={{ padding: '20px' }}
      >
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-purple-500/20 to-pink-500/20 flex items-center justify-center">
              <ShareAltOutlined className="text-2xl text-purple-400" />
            </div>
            <div>
              <h3 className="text-white font-medium text-lg">{kb.name}</h3>
              <div className="flex items-center gap-1 text-slate-500 text-sm">
                <UserOutlined className="text-xs" />
                <span>来自 {kb.owner_name}</span>
              </div>
            </div>
          </div>
          <Tag color="purple" className="text-xs">共享</Tag>
        </div>
        
        {kb.description && (
          <p className="text-slate-400 text-sm mb-4 line-clamp-2">{kb.description}</p>
        )}
        
        <div className="grid grid-cols-2 gap-4">
          <div className="text-center">
            <div className="text-2xl font-bold text-white">{kb.document_count}</div>
            <div className="text-xs text-slate-500">文档</div>
          </div>
          <div className="text-center">
            <div className="text-2xl font-bold text-white">{kb.total_chunks}</div>
            <div className="text-xs text-slate-500">分片</div>
          </div>
        </div>
      </Card>
    </motion.div>
  )
}

// 搜索结果卡片
const SearchResultCard = ({ result, index }: { result: SearchResult; index: number }) => {
  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.05 }}
    >
      <Card
        className="bg-slate-800/50 border-slate-700/50 mb-3"
        bodyStyle={{ padding: '16px' }}
      >
        <div className="flex items-start gap-3">
          <div className="flex-shrink-0">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500/20 to-indigo-500/20 flex items-center justify-center">
              <span className="text-blue-400 font-bold">{(result.score * 100).toFixed(0)}%</span>
            </div>
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-2">
              <Tag color="blue" className="text-xs">{result.knowledge_base_name}</Tag>
              <span className="text-slate-500 text-xs">{result.document_name}</span>
              <span className="text-slate-600 text-xs">#{result.chunk_index + 1}</span>
            </div>
            <Paragraph
              className="text-slate-300 mb-0"
              ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
            >
              {result.content}
            </Paragraph>
          </div>
        </div>
      </Card>
    </motion.div>
  )
}

// 主页面组件
const KnowledgePage = () => {
  const navigate = useNavigate()
  const { kbId } = useParams()
  const fileInputRef = useRef<HTMLInputElement>(null)
  
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
  
  // 共享知识库状态
  const [sharedKnowledgeBases, setSharedKnowledgeBases] = useState<SharedKnowledgeBase[]>([])
  const [sharingEnabled, setSharingEnabled] = useState(false)
  
  // 初始化
  useEffect(() => {
    fetchKnowledgeBases()
    // 获取共享的知识库
    knowledgeApi.getAvailableKnowledgeBases().then((data) => {
      setSharedKnowledgeBases(data.shared || [])
      setSharingEnabled(data.sharing_enabled || false)
    }).catch(() => {
      // 如果API不存在或失败，忽略错误
      setSharedKnowledgeBases([])
    })
  }, [])
  
  // 选中知识库
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
    
    const processingDocs = documents.filter((d) => d.status === 'processing' || d.status === 'pending')
    if (processingDocs.length === 0) return
    
    const interval = setInterval(() => {
      processingDocs.forEach((doc) => {
        refreshDocumentStatus(currentKnowledgeBase.id, doc.id)
      })
    }, 3000)
    
    return () => clearInterval(interval)
  }, [documents, currentKnowledgeBase])
  
  // 创建知识库
  const handleCreate = async (values: { name: string; description?: string }) => {
    try {
      const kb = await createKnowledgeBase(values.name, values.description)
      message.success('创建成功')
      setCreateModalVisible(false)
      form.resetFields()
      navigate(`/knowledge/${kb.id}`)
    } catch (error) {
      message.error('创建失败')
    }
  }
  
  // 上传文件
  const handleUpload = async (file: File) => {
    if (!currentKnowledgeBase) return
    
    try {
      await uploadDocument(currentKnowledgeBase.id, file)
      message.success('上传成功，正在处理...')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '上传失败')
    }
  }
  
  // 删除确认
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
    } catch (error) {
      message.error('删除失败')
    }
  }
  
  // 执行搜索
  const handleSearch = async () => {
    if (!searchInput.trim()) return
    
    try {
      await search(searchInput)
    } catch (error) {
      message.error('搜索失败')
    }
  }
  
  // 渲染知识库列表
  const renderKnowledgeBaseList = () => (
    <div>
      {/* 头部 */}
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
      
      {/* 知识库网格 */}
      {isLoading ? (
        <div className="flex justify-center py-20">
          <Spin size="large" />
        </div>
      ) : knowledgeBases.length === 0 && sharedKnowledgeBases.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<span className="text-slate-500">暂无知识库</span>}
          className="py-20"
        >
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateModalVisible(true)}
          >
            创建第一个知识库
          </Button>
        </Empty>
      ) : (
        <>
          {/* 我的知识库 */}
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
          
          {/* 共享的知识库 */}
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
                    onClick={() => {
                      // 点击共享知识库可以跳转到对话页面使用
                      message.info('共享的知识库可以在 AI 对话中直接选择使用')
                    }}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
  
  // 渲染知识库详情
  const renderKnowledgeBaseDetail = () => {
    if (!currentKnowledgeBase) return null
    
    return (
      <div>
        {/* 头部 */}
        <div className="flex items-center gap-4 mb-6">
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate('/knowledge')}
            className="text-slate-400 hover:text-white"
          />
          <div className="flex-1">
            <h1 className="text-2xl font-bold text-white mb-1">{currentKnowledgeBase.name}</h1>
            {currentKnowledgeBase.description && (
              <p className="text-slate-400">{currentKnowledgeBase.description}</p>
            )}
          </div>
          <Space>
            <Button
              icon={<SearchOutlined />}
              onClick={() => setSearchModalVisible(true)}
              className="bg-slate-700/50 border-slate-600 text-slate-300 hover:text-white hover:border-slate-500"
            >
              搜索
            </Button>
            <Upload
              accept=".txt,.md,.pdf,.html"
              showUploadList={false}
              beforeUpload={(file) => {
                handleUpload(file)
                return false
              }}
            >
              <Button
                type="primary"
                icon={<UploadOutlined />}
                loading={isUploading}
                className="bg-gradient-to-r from-emerald-500 to-teal-600 border-none"
              >
                上传文档
              </Button>
            </Upload>
          </Space>
        </div>
        
        {/* 统计卡片 */}
        <Row gutter={16} className="mb-6">
          <Col span={8}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic
                title={<span className="text-slate-400">文档数</span>}
                value={currentKnowledgeBase.document_count}
                valueStyle={{ color: '#fff' }}
                prefix={<FileTextOutlined className="text-blue-400" />}
              />
            </Card>
          </Col>
          <Col span={8}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic
                title={<span className="text-slate-400">分片数</span>}
                value={currentKnowledgeBase.total_chunks}
                valueStyle={{ color: '#fff' }}
                prefix={<ThunderboltOutlined className="text-amber-400" />}
              />
            </Card>
          </Col>
          <Col span={8}>
            <Card className="bg-slate-800/50 border-slate-700/50">
              <Statistic
                title={<span className="text-slate-400">Token 数</span>}
                value={currentKnowledgeBase.total_tokens}
                valueStyle={{ color: '#fff' }}
                prefix={<DatabaseOutlined className="text-emerald-400" />}
                formatter={(value) =>
                  Number(value) > 1000
                    ? `${(Number(value) / 1000).toFixed(1)}k`
                    : String(value)
                }
              />
            </Card>
          </Col>
        </Row>
        
        {/* 文档列表 */}
        <Card
          title={<span className="text-white">文档列表</span>}
          className="bg-slate-800/50 border-slate-700/50"
          extra={
            <Button
              type="text"
              icon={<ReloadOutlined />}
              onClick={() => fetchDocuments(currentKnowledgeBase.id)}
              className="text-slate-400 hover:text-white"
            >
              刷新
            </Button>
          }
        >
          {isLoading ? (
            <div className="flex justify-center py-10">
              <Spin />
            </div>
          ) : documents.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span className="text-slate-500">暂无文档</span>}
            >
              <Upload
                accept=".txt,.md,.pdf,.html"
                showUploadList={false}
                beforeUpload={(file) => {
                  handleUpload(file)
                  return false
                }}
              >
                <Button type="primary" icon={<UploadOutlined />}>
                  上传第一个文档
                </Button>
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
                  render: (text, record) => (
                    <div className="flex items-center gap-2">
                      {getFileIcon(record.file_type)}
                      <span className="text-slate-300">{text}</span>
                    </div>
                  ),
                },
                {
                  title: '大小',
                  dataIndex: 'file_size',
                  key: 'size',
                  width: 100,
                  render: (size) => <span className="text-slate-400">{formatFileSize(size)}</span>,
                },
                {
                  title: '状态',
                  dataIndex: 'status',
                  key: 'status',
                  width: 120,
                  render: (status) => getStatusTag(status),
                },
                {
                  title: '分片',
                  dataIndex: 'chunk_count',
                  key: 'chunks',
                  width: 80,
                  render: (count) => <span className="text-slate-400">{count}</span>,
                },
                {
                  title: '上传时间',
                  dataIndex: 'created_at',
                  key: 'created_at',
                  width: 160,
                  render: (date) => (
                    <span className="text-slate-400">
                      {dayjs(date).format('YYYY-MM-DD HH:mm')}
                    </span>
                  ),
                },
                {
                  title: '操作',
                  key: 'action',
                  width: 80,
                  render: (_, record) => (
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
  
  return (
    <div className="h-full overflow-y-auto bg-gradient-to-b from-slate-900 to-slate-950 p-6">
      <div className="max-w-6xl mx-auto">
        {kbId ? renderKnowledgeBaseDetail() : renderKnowledgeBaseList()}
      </div>
      
      {/* 创建知识库弹窗 */}
      <Modal
        title="新建知识库"
        open={createModalVisible}
        onCancel={() => setCreateModalVisible(false)}
        footer={null}
      >
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入知识库名称' }]}
          >
            <Input placeholder="输入知识库名称" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <TextArea placeholder="输入描述（可选）" rows={3} />
          </Form.Item>
          <Form.Item className="mb-0 text-right">
            <Space>
              <Button onClick={() => setCreateModalVisible(false)}>取消</Button>
              <Button type="primary" htmlType="submit">
                创建
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
      
      {/* 搜索弹窗 */}
      <Modal
        title="向量搜索"
        open={searchModalVisible}
        onCancel={() => {
          setSearchModalVisible(false)
          clearSearch()
          setSearchInput('')
        }}
        footer={null}
        width={700}
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
        onCancel={() => {
          setDeleteModalVisible(false)
          setDeleteTarget(null)
        }}
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
