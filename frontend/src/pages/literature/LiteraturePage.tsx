import { useEffect, useState } from 'react'
import { 
  Input, Button, Select, Tabs, Card, List, Tag, Space, Spin, Empty, 
  message, Modal, Form, Dropdown, Tooltip, Badge, Rate, Drawer
} from 'antd'
import {
  SearchOutlined, BookOutlined, FolderOutlined, AppstoreOutlined,
  UnorderedListOutlined, NodeIndexOutlined, PlusOutlined, StarOutlined,
  DownloadOutlined, DeleteOutlined, EditOutlined, EyeOutlined,
  LinkOutlined, CalendarOutlined, TeamOutlined, FileTextOutlined,
  MoreOutlined, HeartOutlined, HeartFilled, CheckOutlined
} from '@ant-design/icons'
import { useLiteratureStore } from '@/stores/literatureStore'
import { PaperSearchResult, Paper, PaperCollection } from '@/services/api'
import CitationGraph from './CitationGraph'
import PaperDetailPanel from './PaperDetailPanel'

const { Search } = Input
const { Option } = Select
const { TabPane } = Tabs

export default function LiteraturePage() {
  const {
    papers, papersLoading,
    searchResults, searchQuery, searchSource, searchTotal, searchLoading,
    collections, selectedCollectionId, collectionsLoading,
    selectedPaper, detailPanelOpen,
    viewMode, citationGraph, graphLoading,
    init, searchPapers, clearSearch, loadPapers,
    savePaper, deletePaper, selectPaper, loadPaperDetail,
    loadCollections, createCollection, deleteCollection, selectCollection,
    loadCitationGraph, clearGraph,
    setViewMode, toggleDetailPanel, downloadPdf
  } = useLiteratureStore()

  const [activeTab, setActiveTab] = useState('library')
  const [searchValue, setSearchValue] = useState('')
  const [selectedSource, setSelectedSource] = useState('semantic_scholar')
  const [yearRange, setYearRange] = useState<[number?, number?]>([undefined, undefined])
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [form] = Form.useForm()

  // 数据源名称映射
  const sourceNames: Record<string, string> = {
    'semantic_scholar': 'Semantic Scholar',
    'arxiv': 'arXiv',
    'pubmed': 'PubMed',
    'openalex': 'OpenAlex',
    'crossref': 'CrossRef'
  }

  // 数据源标签颜色
  const sourceColors: Record<string, string> = {
    'semantic_scholar': 'blue',
    'arxiv': 'orange',
    'pubmed': 'green',
    'openalex': 'purple',
    'crossref': 'cyan'
  }

  useEffect(() => {
    init()
  }, [init])

  // 搜索论文
  const handleSearch = async () => {
    if (!searchValue.trim()) return
    setActiveTab('search')
    await searchPapers(searchValue, selectedSource, {
      year_start: yearRange[0],
      year_end: yearRange[1],
    })
  }

  // 保存论文到库
  const handleSavePaper = async (paper: PaperSearchResult) => {
    try {
      await savePaper(paper)
      message.success('论文已保存')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '保存失败')
    }
  }

  // 删除论文
  const handleDeletePaper = async (paperId: number) => {
    Modal.confirm({
      title: '确认删除',
      content: '确定要删除这篇论文吗？此操作不可恢复。',
      okText: '删除',
      okType: 'danger',
      onOk: async () => {
        try {
          await deletePaper(paperId)
          message.success('已删除')
        } catch {
          message.error('删除失败')
        }
      },
    })
  }

  // 创建收藏夹
  const handleCreateCollection = async (values: any) => {
    try {
      await createCollection(values)
      message.success('收藏夹已创建')
      setCreateModalOpen(false)
      form.resetFields()
    } catch {
      message.error('创建失败')
    }
  }

  // 显示引用图谱
  const handleShowGraph = async (paper: Paper) => {
    if (!paper.semantic_scholar_id) {
      message.warning('该论文没有 Semantic Scholar ID，无法获取引用图谱')
      return
    }
    selectPaper(paper)
    setActiveTab('graph')
    await loadCitationGraph(paper.id)
  }

  // 渲染搜索结果卡片
  const renderSearchResultCard = (paper: PaperSearchResult) => (
    <Card
      key={paper.external_id}
      className="mb-3 hover:shadow-md transition-shadow"
      size="small"
    >
      <div className="flex justify-between">
        <div className="flex-1 pr-4">
          <h4 className="font-medium text-base mb-2 line-clamp-2">
            <a href={paper.url} target="_blank" rel="noopener noreferrer" className="hover:text-blue-500">
              {paper.title}
            </a>
          </h4>
          
          <div className="text-gray-500 text-sm mb-2">
            <Space size="small" wrap>
              {paper.authors?.slice(0, 3).map((a, i) => (
                <span key={i}>
                  <TeamOutlined className="mr-1" />
                  {a.name}
                </span>
              ))}
              {paper.authors?.length > 3 && <span>等</span>}
            </Space>
          </div>

          <div className="text-gray-500 text-sm mb-2">
            <Space size="middle">
              {paper.year && (
                <span><CalendarOutlined className="mr-1" />{paper.year}</span>
              )}
              {paper.venue && (
                <span><BookOutlined className="mr-1" />{paper.venue}</span>
              )}
              {paper.citation_count > 0 && (
                <Tooltip title="引用数">
                  <span><StarOutlined className="mr-1" />{paper.citation_count}</span>
                </Tooltip>
              )}
            </Space>
          </div>

          {paper.abstract && (
            <p className="text-gray-600 text-sm line-clamp-2 mb-2">
              {paper.abstract}
            </p>
          )}

          <Space size="small" wrap>
            {paper.fields_of_study?.slice(0, 3).map((field, i) => (
              <Tag key={i} color="blue">{field}</Tag>
            ))}
            <Tag color={sourceColors[paper.source] || 'default'}>{sourceNames[paper.source] || paper.source}</Tag>
            {paper.doi && <Tag color="cyan">DOI</Tag>}
          </Space>
        </div>

        <div className="flex flex-col gap-2">
          {paper.is_saved ? (
            <Button type="text" icon={<CheckOutlined />} disabled>
              已保存
            </Button>
          ) : (
            <Button 
              type="primary" 
              icon={<PlusOutlined />}
              onClick={() => handleSavePaper(paper)}
            >
              保存
            </Button>
          )}
          {paper.pdf_url && (
            <Button 
              icon={<DownloadOutlined />}
              href={paper.pdf_url}
              target="_blank"
            >
              PDF
            </Button>
          )}
          {paper.url && (
            <Button 
              icon={<LinkOutlined />}
              href={paper.url}
              target="_blank"
            >
              链接
            </Button>
          )}
        </div>
      </div>
    </Card>
  )

  // 渲染论文卡片
  const renderPaperCard = (paper: Paper) => (
    <Card
      key={paper.id}
      className="mb-3 hover:shadow-md transition-shadow cursor-pointer"
      size="small"
      onClick={() => {
        selectPaper(paper)
        toggleDetailPanel(true)
      }}
    >
      <div className="flex justify-between">
        <div className="flex-1 pr-4">
          <div className="flex items-center gap-2 mb-2">
            <h4 className="font-medium text-base line-clamp-2 flex-1">
              {paper.title}
            </h4>
            {paper.is_read && (
              <Tag color="success" icon={<CheckOutlined />}>已读</Tag>
            )}
            {paper.rating && (
              <Rate disabled value={paper.rating} className="text-sm" />
            )}
          </div>
          
          <div className="text-gray-500 text-sm mb-2">
            <Space size="small" wrap>
              {paper.authors?.slice(0, 3).map((a, i) => (
                <span key={i}>{a.name}</span>
              ))}
              {paper.authors?.length > 3 && <span>等</span>}
            </Space>
          </div>

          <div className="text-gray-500 text-sm mb-2">
            <Space size="middle">
              {paper.year && <span>{paper.year}</span>}
              {paper.venue && <span>{paper.venue}</span>}
              {paper.citation_count > 0 && (
                <span>引用: {paper.citation_count}</span>
              )}
            </Space>
          </div>

          <Space size="small" wrap>
            {paper.tags?.map((tag, i) => (
              <Tag key={i}>{tag}</Tag>
            ))}
            {paper.source && <Tag color={sourceColors[paper.source] || 'default'}>{sourceNames[paper.source] || paper.source}</Tag>}
            {paper.pdf_downloaded && <Tag color="green">PDF</Tag>}
          </Space>
        </div>

        <div className="flex flex-col gap-1" onClick={e => e.stopPropagation()}>
          <Dropdown
            menu={{
              items: [
                {
                  key: 'view',
                  icon: <EyeOutlined />,
                  label: '查看详情',
                  onClick: () => {
                    selectPaper(paper)
                    toggleDetailPanel(true)
                  },
                },
                {
                  key: 'graph',
                  icon: <NodeIndexOutlined />,
                  label: '引用图谱',
                  onClick: () => handleShowGraph(paper),
                  disabled: !paper.semantic_scholar_id,
                },
                {
                  key: 'download',
                  icon: <DownloadOutlined />,
                  label: '下载 PDF',
                  onClick: async () => {
                    try {
                      await downloadPdf(paper.id)
                      message.success('PDF 下载成功')
                    } catch {
                      message.error('下载失败')
                    }
                  },
                  disabled: !paper.pdf_url || paper.pdf_downloaded,
                },
                { type: 'divider' },
                {
                  key: 'delete',
                  icon: <DeleteOutlined />,
                  label: '删除',
                  danger: true,
                  onClick: () => handleDeletePaper(paper.id),
                },
              ],
            }}
            trigger={['click']}
          >
            <Button type="text" icon={<MoreOutlined />} />
          </Dropdown>
        </div>
      </div>
    </Card>
  )

  // 渲染列表视图
  const renderListView = () => (
    <List
      loading={papersLoading}
      dataSource={papers}
      locale={{ emptyText: <Empty description="暂无论文" /> }}
      renderItem={paper => (
        <List.Item
          className="cursor-pointer hover:bg-gray-50 px-4"
          onClick={() => {
            selectPaper(paper)
            toggleDetailPanel(true)
          }}
          actions={[
            <Tooltip title="引用图谱" key="graph">
              <Button 
                type="text" 
                icon={<NodeIndexOutlined />}
                onClick={e => {
                  e.stopPropagation()
                  handleShowGraph(paper)
                }}
                disabled={!paper.semantic_scholar_id}
              />
            </Tooltip>,
            <Tooltip title="删除" key="delete">
              <Button 
                type="text" 
                danger
                icon={<DeleteOutlined />}
                onClick={e => {
                  e.stopPropagation()
                  handleDeletePaper(paper.id)
                }}
              />
            </Tooltip>,
          ]}
        >
          <List.Item.Meta
            title={
              <Space>
                {paper.title}
                {paper.is_read && <Tag color="success">已读</Tag>}
              </Space>
            }
            description={
              <Space size="middle">
                <span>{paper.authors?.slice(0, 2).map(a => a.name).join(', ')}</span>
                {paper.year && <span>{paper.year}</span>}
                {paper.citation_count > 0 && <span>引用: {paper.citation_count}</span>}
              </Space>
            }
          />
        </List.Item>
      )}
    />
  )

  return (
    <div className="h-full flex">
      {/* 左侧收藏夹 */}
      <div className="w-64 border-r bg-gray-50 p-4 flex flex-col">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium text-lg">
            <FolderOutlined className="mr-2" />
            收藏夹
          </h3>
          <Button 
            type="text" 
            icon={<PlusOutlined />}
            onClick={() => setCreateModalOpen(true)}
          />
        </div>

        <div className="flex-1 overflow-auto">
          {collectionsLoading ? (
            <div className="text-center py-4"><Spin /></div>
          ) : (
            <div className="space-y-1">
              <div
                className={`px-3 py-2 rounded cursor-pointer flex justify-between items-center ${
                  selectedCollectionId === null ? 'bg-blue-100 text-blue-600' : 'hover:bg-gray-100'
                }`}
                onClick={() => selectCollection(null)}
              >
                <span><BookOutlined className="mr-2" />全部论文</span>
                <Badge count={papers.length} showZero color={selectedCollectionId === null ? '#1890ff' : '#999'} />
              </div>
              
              {collections.map(coll => (
                <div
                  key={coll.id}
                  className={`px-3 py-2 rounded cursor-pointer flex justify-between items-center group ${
                    selectedCollectionId === coll.id ? 'bg-blue-100 text-blue-600' : 'hover:bg-gray-100'
                  }`}
                  onClick={() => selectCollection(coll.id)}
                >
                  <span>
                    <span 
                      className="inline-block w-3 h-3 rounded mr-2" 
                      style={{ backgroundColor: coll.color }}
                    />
                    {coll.name}
                  </span>
                  <Space>
                    <Badge count={coll.paper_count} showZero color={selectedCollectionId === coll.id ? '#1890ff' : '#999'} />
                    {!coll.is_default && (
                      <Button
                        type="text"
                        size="small"
                        icon={<DeleteOutlined />}
                        className="opacity-0 group-hover:opacity-100"
                        onClick={e => {
                          e.stopPropagation()
                          Modal.confirm({
                            title: '删除收藏夹',
                            content: `确定删除收藏夹 "${coll.name}" 吗？`,
                            onOk: () => deleteCollection(coll.id),
                          })
                        }}
                      />
                    )}
                  </Space>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* 搜索栏 */}
        <div className="p-4 border-b bg-white">
          <div className="flex gap-4 items-center">
            <Select
              value={selectedSource}
              onChange={setSelectedSource}
              style={{ width: 180 }}
            >
              <Option value="semantic_scholar">🔬 Semantic Scholar</Option>
              <Option value="arxiv">📄 arXiv</Option>
              <Option value="pubmed">🏥 PubMed</Option>
              <Option value="openalex">📚 OpenAlex</Option>
              <Option value="crossref">🔗 CrossRef</Option>
            </Select>
            
            <Search
              placeholder="搜索论文标题、作者、关键词..."
              value={searchValue}
              onChange={e => setSearchValue(e.target.value)}
              onSearch={handleSearch}
              loading={searchLoading}
              enterButton={<><SearchOutlined /> 搜索</>}
              style={{ width: 400 }}
            />

            <div className="flex-1" />

            <Space>
              <Tooltip title="卡片视图">
                <Button
                  type={viewMode === 'card' ? 'primary' : 'default'}
                  icon={<AppstoreOutlined />}
                  onClick={() => setViewMode('card')}
                />
              </Tooltip>
              <Tooltip title="列表视图">
                <Button
                  type={viewMode === 'list' ? 'primary' : 'default'}
                  icon={<UnorderedListOutlined />}
                  onClick={() => setViewMode('list')}
                />
              </Tooltip>
              <Tooltip title="图谱视图">
                <Button
                  type={viewMode === 'graph' ? 'primary' : 'default'}
                  icon={<NodeIndexOutlined />}
                  onClick={() => setViewMode('graph')}
                />
              </Tooltip>
            </Space>
          </div>
        </div>

        {/* 内容区 */}
        <div className="flex-1 overflow-hidden">
          <Tabs 
            activeKey={activeTab} 
            onChange={setActiveTab}
            className="h-full"
            tabBarStyle={{ padding: '0 16px', marginBottom: 0 }}
          >
            {/* 文献库 */}
            <TabPane tab={<span><BookOutlined />我的文献库</span>} key="library">
              <div className="h-full overflow-auto p-4">
                {viewMode === 'card' ? (
                  papersLoading ? (
                    <div className="text-center py-8"><Spin size="large" /></div>
                  ) : papers.length === 0 ? (
                    <Empty description="暂无论文，搜索并保存论文到这里" />
                  ) : (
                    <div className="grid grid-cols-1 gap-4">
                      {papers.map(renderPaperCard)}
                    </div>
                  )
                ) : viewMode === 'list' ? (
                  renderListView()
                ) : (
                  <div className="text-center py-8 text-gray-500">
                    请选择一篇论文查看引用图谱
                  </div>
                )}
              </div>
            </TabPane>

            {/* 搜索结果 */}
            <TabPane 
              tab={
                <span>
                  <SearchOutlined />
                  搜索结果
                  {searchTotal > 0 && <Badge count={searchTotal} className="ml-2" />}
                </span>
              } 
              key="search"
            >
              <div className="h-full overflow-auto p-4">
                {searchLoading ? (
                  <div className="text-center py-8"><Spin size="large" /></div>
                ) : searchResults.length === 0 ? (
                  <Empty description={searchQuery ? `未找到 "${searchQuery}" 相关论文` : '输入关键词搜索论文'} />
                ) : (
                  <div>
                    <p className="text-gray-500 mb-4">
                      在 {sourceNames[selectedSource] || selectedSource} 找到约 {searchTotal} 篇关于 "{searchQuery}" 的论文
                    </p>
                    {searchResults.map(renderSearchResultCard)}
                  </div>
                )}
              </div>
            </TabPane>

            {/* 引用图谱 */}
            <TabPane tab={<span><NodeIndexOutlined />引用图谱</span>} key="graph">
              <div className="h-full">
                {graphLoading ? (
                  <div className="h-full flex items-center justify-center">
                    <Spin size="large" tip="加载引用图谱..." />
                  </div>
                ) : citationGraph ? (
                  <CitationGraph 
                    data={citationGraph} 
                    onNodeClick={(nodeId) => {
                      // 可以实现点击节点查看论文详情
                      console.log('Node clicked:', nodeId)
                    }}
                  />
                ) : (
                  <Empty 
                    className="py-20"
                    description="选择一篇论文以查看其引用关系图谱"
                  />
                )}
              </div>
            </TabPane>
          </Tabs>
        </div>
      </div>

      {/* 右侧详情面板 */}
      <Drawer
        title="论文详情"
        placement="right"
        width={480}
        open={detailPanelOpen}
        onClose={() => toggleDetailPanel(false)}
        mask={false}
      >
        {selectedPaper && (
          <PaperDetailPanel 
            paper={selectedPaper}
            onShowGraph={() => handleShowGraph(selectedPaper)}
          />
        )}
      </Drawer>

      {/* 创建收藏夹对话框 */}
      <Modal
        title="创建收藏夹"
        open={createModalOpen}
        onCancel={() => setCreateModalOpen(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} onFinish={handleCreateCollection} layout="vertical">
          <Form.Item 
            name="name" 
            label="名称" 
            rules={[{ required: true, message: '请输入收藏夹名称' }]}
          >
            <Input placeholder="输入收藏夹名称" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="可选的描述" rows={2} />
          </Form.Item>
          <Form.Item name="color" label="颜色" initialValue="#3b82f6">
            <Input type="color" style={{ width: 60, height: 32 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
