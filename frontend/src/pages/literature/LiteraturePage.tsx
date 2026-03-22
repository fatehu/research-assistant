import { useEffect, useState } from 'react'
import {
  Input, Button, Select, Spin,
  message, Modal, Form, Badge, Tooltip, Drawer
} from 'antd'
import {
  SearchOutlined, BookOutlined, AppstoreOutlined,
  UnorderedListOutlined, DownloadOutlined,
  ClockCircleOutlined, FileTextOutlined, LinkOutlined
} from '@ant-design/icons'
import { useLiteratureStore } from '@/stores/literatureStore'
import { PaperSearchResult } from '@/services/api'
import PaperDetailPanel from './PaperDetailPanel'
import { getSourceInfo, SOURCES } from './constants'
import {
  PaperCard,
  PaperListItem,
  SearchResultCard,
  SearchResultListItem,
  CollectionSidebar,
} from './components'

const { Search } = Input
const { Option } = Select

export default function LiteraturePage() {
  const {
    papers, papersLoading,
    searchResults, searchQuery, searchTotal, searchLoading,
    searchOffset, searchLoadingMore, searchHasMore, loadMoreSearchResults,
    collections, selectedCollectionId, collectionsLoading,
    selectedPaper, detailPanelOpen,
    viewMode,
    init, searchPapers,
    savePaper, importPaperFromLink, deletePaper, selectPaper,
    createCollection, deleteCollection, selectCollection,
    setViewMode, toggleDetailPanel, downloadPdf
  } = useLiteratureStore()

  const [activeTab, setActiveTab] = useState<'library' | 'search'>('library')
  const [searchValue, setSearchValue] = useState('')
  const [selectedSource, setSelectedSource] = useState('multi')
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [linkImportModalOpen, setLinkImportModalOpen] = useState(false)
  const [linkImportSubmitting, setLinkImportSubmitting] = useState(false)
  const [createCollectionForm] = Form.useForm()
  const [linkImportForm] = Form.useForm()
  const selectedCollection = collections.find(collection => collection.id === selectedCollectionId) || null

  useEffect(() => {
    init()
  }, [init])

  // ---- 事件处理 ----

  const handleSearch = async () => {
    if (!searchValue.trim()) return
    setActiveTab('search')
    await searchPapers(searchValue, selectedSource, {})
  }

  const handleSavePaper = async (paper: PaperSearchResult) => {
    try {
      await savePaper(paper)
      message.success('论文已保存到文献库')
    } catch (error: any) {
      // Error handled by store
    }
  }

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
          // Error handled by store
        }
      },
    })
  }

  const handleSelectPaper = (paper: any) => {
    selectPaper(paper)
    toggleDetailPanel(true)
  }

  const handleDownloadPdf = async (paperId: number) => {
    try {
      await downloadPdf(paperId)
      message.success('PDF 下载成功')
    } catch {
      // Error handled by store
    }
  }

  const handleCreateCollection = async (values: any) => {
    try {
      await createCollection(values)
      message.success('收藏夹已创建')
      setCreateModalOpen(false)
      createCollectionForm.resetFields()
    } catch {
      // Error handled by store
    }
  }

  const handleImportPaperFromLink = async (values: { link: string }) => {
    try {
      setLinkImportSubmitting(true)
      const result = await importPaperFromLink(
        values.link,
        selectedCollectionId ? [selectedCollectionId] : []
      )
      setActiveTab('library')
      setLinkImportModalOpen(false)
      linkImportForm.resetFields()
      selectPaper(result.paper)
      toggleDetailPanel(true)
      if (result.already_exists) {
        message.info(selectedCollection
          ? `论文已存在，已同步到“${selectedCollection.name}”`
          : '论文已存在，已定位到现有记录')
      } else {
        message.success('论文已通过链接入库')
      }
    } catch {
      // Error handled by store
    } finally {
      setLinkImportSubmitting(false)
    }
  }

  // ---- Tab 配置 ----

  const tabConfig = [
    { key: 'library', label: '文献库', icon: <BookOutlined /> },
    { key: 'search', label: '搜索结果', icon: <SearchOutlined />, badge: searchTotal },
  ]

  // ---- 渲染 ----

  return (
    <div className="h-full flex overflow-hidden">
      {/* 左侧收藏夹面板 */}
      <CollectionSidebar
        collections={collections}
        collectionsLoading={collectionsLoading}
        selectedCollectionId={selectedCollectionId}
        totalPapers={papers.length}
        onSelectCollection={selectCollection}
        onDeleteCollection={deleteCollection}
        onCreateClick={() => setCreateModalOpen(true)}
      />

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* 顶部搜索栏 */}
        <div className="p-4 border-b border-slate-700/50 bg-slate-900/50 backdrop-blur-sm flex-shrink-0">
          <div className="flex gap-4 items-center">
            <Select
              value={selectedSource}
              onChange={setSelectedSource}
              style={{ width: 180 }}
              className="[&_.ant-select-selector]:!bg-slate-800/50 [&_.ant-select-selector]:!border-slate-600"
            >
              {SOURCES.map(s => (
                <Option key={s.key} value={s.key}>
                  <span className="flex items-center gap-2">
                    <span>{s.icon}</span>
                    <span>{s.name}</span>
                  </span>
                </Option>
              ))}
            </Select>

            <Search
              placeholder="搜索论文标题、作者、关键词..."
              value={searchValue}
              onChange={e => setSearchValue(e.target.value)}
              onSearch={handleSearch}
              loading={searchLoading}
              enterButton={
                <span className="flex items-center gap-1">
                  <SearchOutlined />
                  搜索
                </span>
              }
              style={{ width: 400 }}
              className="[&_.ant-input]:!bg-slate-800/50 [&_.ant-input]:!border-slate-600"
            />

            <div className="flex-1" />

            <Button
              icon={<LinkOutlined />}
              onClick={() => setLinkImportModalOpen(true)}
              className="!bg-slate-800/50 !border-slate-600 !text-slate-200 hover:!border-emerald-500/50 hover:!text-emerald-400"
            >
              链接入库
            </Button>

            {/* 视图切换 */}
            <div className="flex bg-slate-800/50 rounded-lg p-1 border border-slate-700/50">
              <Tooltip title="卡片视图">
                <Button
                  type={viewMode === 'card' ? 'primary' : 'text'}
                  icon={<AppstoreOutlined />}
                  onClick={() => setViewMode('card')}
                  className={viewMode !== 'card' ? '!text-slate-400' : ''}
                />
              </Tooltip>
              <Tooltip title="列表视图">
                <Button
                  type={viewMode === 'list' ? 'primary' : 'text'}
                  icon={<UnorderedListOutlined />}
                  onClick={() => setViewMode('list')}
                  className={viewMode !== 'list' ? '!text-slate-400' : ''}
                />
              </Tooltip>
            </div>
          </div>
        </div>

        {/* Tab 切换 */}
        <div className="px-4 pt-3 pb-0 border-b border-slate-700/50 bg-slate-900/30 flex-shrink-0">
          <div className="flex gap-1">
            {tabConfig.map(tab => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key as any)}
                className={`px-4 py-2.5 rounded-t-lg font-medium text-sm flex items-center gap-2 transition-all border-b-2 -mb-[1px] ${activeTab === tab.key
                  ? 'bg-slate-800/50 text-emerald-400 border-emerald-400'
                  : 'text-slate-400 hover:text-slate-200 border-transparent hover:bg-slate-800/30'
                  }`}
              >
                {tab.icon}
                {tab.label}
                {tab.badge !== undefined && tab.badge > 0 && (
                  <Badge
                    count={tab.badge}
                    className={activeTab === tab.key ? '[&_.ant-badge-count]:!bg-emerald-500' : '[&_.ant-badge-count]:!bg-slate-600'}
                  />
                )}
              </button>
            ))}
          </div>
        </div>

        {/* 内容区 */}
        <div className="flex-1 overflow-hidden relative">
          {/* 文献库 Tab */}
          {activeTab === 'library' && (
            <div className="absolute inset-0 overflow-y-auto p-4 scrollbar-thin">
              {papersLoading ? (
                <div className="flex flex-col items-center justify-center py-20">
                  <Spin size="large" />
                  <p className="text-slate-500 mt-4">加载中...</p>
                </div>
              ) : papers.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20">
                  <div className="w-20 h-20 rounded-full bg-slate-800/50 flex items-center justify-center mb-4">
                    <BookOutlined className="text-4xl text-slate-600" />
                  </div>
                  <p className="text-slate-400 text-lg mb-2">文献库为空</p>
                  <p className="text-slate-500 text-sm">搜索并保存论文到这里开始管理你的文献</p>
                </div>
              ) : viewMode === 'card' ? (
                <div className="grid grid-cols-1 gap-3">
                  {papers.map((paper, i) => (
                    <PaperCard
                      key={paper.id}
                      paper={paper}
                      index={i}
                      sourceInfo={getSourceInfo(paper.source)}
                      onSelect={handleSelectPaper}
                      onDelete={handleDeletePaper}
                      onDownloadPdf={handleDownloadPdf}
                    />
                  ))}
                </div>
              ) : (
                <div className="glass-card overflow-hidden">
                  {papers.map((paper, i) => (
                    <PaperListItem
                      key={paper.id}
                      paper={paper}
                      index={i}
                      sourceInfo={getSourceInfo(paper.source)}
                      onSelect={handleSelectPaper}
                      onDelete={handleDeletePaper}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 搜索结果 Tab */}
          {activeTab === 'search' && (
            <div className="absolute inset-0 overflow-y-auto p-4 scrollbar-thin">
              {searchLoading ? (
                <div className="flex flex-col items-center justify-center py-20">
                  <Spin size="large" />
                  <p className="text-slate-500 mt-4">搜索中...</p>
                </div>
              ) : searchResults.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20">
                  <div className="w-20 h-20 rounded-full bg-slate-800/50 flex items-center justify-center mb-4">
                    <SearchOutlined className="text-4xl text-slate-600" />
                  </div>
                  <p className="text-slate-400 text-lg mb-2">
                    {searchQuery ? `未找到 "${searchQuery}" 相关论文` : '输入关键词搜索论文'}
                  </p>
                  <p className="text-slate-500 text-sm">尝试使用不同的关键词或数据源</p>
                </div>
              ) : (
                <div>
                  {/* 搜索结果统计 */}
                  <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-2 text-slate-400">
                      <ClockCircleOutlined />
                      在 {getSourceInfo(selectedSource).name} 找到约{' '}
                      <span className="text-emerald-400 font-semibold">{searchTotal.toLocaleString()}</span>{' '}
                      篇关于 "<span className="text-slate-200">{searchQuery}</span>" 的论文
                    </div>
                    <div className="text-slate-500 text-sm">
                      已加载 {searchOffset} / {searchTotal.toLocaleString()}
                    </div>
                  </div>

                  {viewMode === 'card' ? (
                    <div className="grid grid-cols-1 gap-3">
                      {searchResults.map((paper, i) => (
                        <SearchResultCard
                          key={paper.external_id}
                          paper={paper}
                          index={i}
                          sourceInfo={getSourceInfo(paper.source)}
                          onSave={handleSavePaper}
                        />
                      ))}
                    </div>
                  ) : (
                    <div className="glass-card overflow-hidden">
                      {searchResults.map((paper, i) => (
                        <SearchResultListItem
                          key={paper.external_id}
                          paper={paper}
                          index={i}
                          sourceInfo={getSourceInfo(paper.source)}
                          onSave={handleSavePaper}
                        />
                      ))}
                    </div>
                  )}

                  {/* 加载更多 */}
                  {searchHasMore && (
                    <div className="flex justify-center mt-6 mb-4">
                      <Button
                        size="large"
                        loading={searchLoadingMore}
                        onClick={loadMoreSearchResults}
                        className="!bg-slate-800/50 !border-slate-600 !text-slate-300 hover:!border-emerald-500/50 hover:!text-emerald-400 px-8"
                        icon={<DownloadOutlined />}
                      >
                        {searchLoadingMore ? '加载中...' : `加载更多 (还有 ${(searchTotal - searchOffset).toLocaleString()} 篇)`}
                      </Button>
                    </div>
                  )}

                  {!searchHasMore && searchResults.length > 0 && (
                    <div className="text-center text-slate-500 text-sm py-6 border-t border-slate-700/50 mt-4">
                      已加载全部 {searchOffset} 篇论文
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 右侧详情 Drawer */}
      <Drawer
        title={
          <span className="flex items-center gap-2 text-slate-200">
            <FileTextOutlined className="text-emerald-400" />
            论文详情
          </span>
        }
        placement="right"
        width={480}
        open={detailPanelOpen}
        onClose={() => toggleDetailPanel(false)}
        mask={false}
        className="[&_.ant-drawer-header]:!bg-slate-900/95 [&_.ant-drawer-header]:!border-slate-700/50 [&_.ant-drawer-body]:!bg-slate-900/95 [&_.ant-drawer-body]:!p-4"
      >
        {selectedPaper && <PaperDetailPanel paper={selectedPaper} />}
      </Drawer>

      {/* 创建收藏夹对话框 */}
      <Modal
        title={
          <span className="flex items-center gap-2">
            <BookOutlined className="text-emerald-400" />
            创建收藏夹
          </span>
        }
        open={createModalOpen}
        onCancel={() => setCreateModalOpen(false)}
        onOk={() => createCollectionForm.submit()}
        okText="创建"
        cancelText="取消"
      >
        <Form form={createCollectionForm} onFinish={handleCreateCollection} layout="vertical" className="mt-4">
          <Form.Item
            name="name"
            label={<span className="text-slate-300">名称</span>}
            rules={[{ required: true, message: '请输入收藏夹名称' }]}
          >
            <Input placeholder="输入收藏夹名称" />
          </Form.Item>
          <Form.Item name="description" label={<span className="text-slate-300">描述</span>}>
            <Input.TextArea placeholder="可选的描述" rows={2} />
          </Form.Item>
          <Form.Item name="color" label={<span className="text-slate-300">颜色</span>} initialValue="#10b981">
            <Input type="color" style={{ width: 60, height: 32 }} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={
          <span className="flex items-center gap-2">
            <LinkOutlined className="text-emerald-400" />
            链接入库
          </span>
        }
        open={linkImportModalOpen}
        onCancel={() => {
          setLinkImportModalOpen(false)
          linkImportForm.resetFields()
        }}
        onOk={() => linkImportForm.submit()}
        okText="导入"
        cancelText="取消"
        confirmLoading={linkImportSubmitting}
      >
        <Form
          form={linkImportForm}
          onFinish={handleImportPaperFromLink}
          layout="vertical"
          className="mt-4"
        >
          <Form.Item
            name="link"
            label={<span className="text-slate-300">论文链接或标识</span>}
            rules={[
              { required: true, message: '请输入论文链接、DOI、arXiv 或 PubMed 标识' },
            ]}
            extra={
              <span className="text-slate-500">
                支持 DOI、arXiv、PubMed、OpenAlex、Semantic Scholar 和期刊详情页。
                {selectedCollection
                  ? ` 导入成功后会自动加入当前收藏夹“${selectedCollection.name}”。`
                  : ' 未选择收藏夹时将按默认规则入库。'}
              </span>
            }
          >
            <Input.TextArea
              rows={4}
              placeholder={'例如：10.1038/s41586-023-07042-4\nhttps://arxiv.org/abs/2401.12345\nhttps://pubmed.ncbi.nlm.nih.gov/12345678/'}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
