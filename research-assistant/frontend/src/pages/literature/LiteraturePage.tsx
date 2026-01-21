import { useEffect, useState } from 'react'
import { 
  Input, Button, Select, Tag, Space, Spin, Empty, 
  message, Modal, Form, Dropdown, Tooltip, Badge, Rate, Drawer
} from 'antd'
import {
  SearchOutlined, BookOutlined, FolderOutlined, AppstoreOutlined,
  UnorderedListOutlined, PlusOutlined,
  DownloadOutlined, DeleteOutlined, EyeOutlined,
  LinkOutlined, CalendarOutlined, TeamOutlined,
  MoreOutlined, CheckOutlined, FireOutlined, ClockCircleOutlined,
  FileTextOutlined, DatabaseOutlined
} from '@ant-design/icons'
import { useLiteratureStore } from '@/stores/literatureStore'
import { PaperSearchResult, Paper } from '@/services/api'
import PaperDetailPanel from './PaperDetailPanel'

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
    savePaper, deletePaper, selectPaper,
    createCollection, deleteCollection, selectCollection,
    setViewMode, toggleDetailPanel, downloadPdf
  } = useLiteratureStore()

  const [activeTab, setActiveTab] = useState<'library' | 'search'>('library')
  const [searchValue, setSearchValue] = useState('')
  const [selectedSource, setSelectedSource] = useState('semantic_scholar')
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [form] = Form.useForm()

  // 数据源配置
  const sources = [
    { key: 'semantic_scholar', name: 'Semantic Scholar', icon: '🔬', color: 'blue' },
    { key: 'arxiv', name: 'arXiv', icon: '📄', color: 'orange' },
    { key: 'pubmed', name: 'PubMed', icon: '🏥', color: 'green' },
    { key: 'openalex', name: 'OpenAlex', icon: '📚', color: 'purple' },
    { key: 'crossref', name: 'CrossRef', icon: '🔗', color: 'cyan' }
  ]

  const getSourceInfo = (key: string) => sources.find(s => s.key === key) || sources[0]

  useEffect(() => {
    init()
  }, [init])

  // 搜索论文
  const handleSearch = async () => {
    if (!searchValue.trim()) return
    setActiveTab('search')
    await searchPapers(searchValue, selectedSource, {})
  }

  // 保存论文到库
  const handleSavePaper = async (paper: PaperSearchResult) => {
    try {
      await savePaper(paper)
      message.success('论文已保存到文献库')
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

  // Tab 按钮配置
  const tabConfig = [
    { key: 'library', label: '文献库', icon: <BookOutlined /> },
    { key: 'search', label: '搜索结果', icon: <SearchOutlined />, badge: searchTotal },
  ]

  // 渲染搜索结果卡片
  const renderSearchResultCard = (paper: PaperSearchResult, index: number) => {
    const source = getSourceInfo(paper.source)
    return (
      <div
        key={paper.external_id}
        className="glass-card p-4 mb-3 hover:border-emerald-500/30 transition-all duration-300"
        style={{ animationDelay: `${index * 50}ms` }}
      >
        <div className="flex justify-between gap-4">
          <div className="flex-1 min-w-0">
            {/* 标题 */}
            <h4 className="font-semibold text-base mb-2 text-slate-100 leading-snug">
              <a 
                href={paper.url} 
                target="_blank" 
                rel="noopener noreferrer" 
                className="hover:text-emerald-400 transition-colors line-clamp-2"
              >
                {paper.title}
              </a>
            </h4>
            
            {/* 作者 */}
            <div className="text-slate-400 text-sm mb-2 flex items-center gap-1">
              <TeamOutlined className="text-emerald-500/60" />
              <span className="truncate">
                {paper.authors?.slice(0, 3).map(a => a.name).join(', ')}
                {paper.authors?.length > 3 && ' 等'}
              </span>
            </div>

            {/* 元信息 */}
            <div className="flex items-center gap-4 text-sm text-slate-500 mb-3">
              {paper.year && (
                <span className="flex items-center gap-1">
                  <CalendarOutlined className="text-blue-400/60" />
                  {paper.year}
                </span>
              )}
              {paper.venue && (
                <span className="flex items-center gap-1 truncate max-w-[200px]">
                  <BookOutlined className="text-purple-400/60" />
                  {paper.venue}
                </span>
              )}
              {paper.citation_count > 0 && (
                <span className="flex items-center gap-1">
                  <FireOutlined className="text-orange-400/60" />
                  {paper.citation_count} 引用
                </span>
              )}
            </div>

            {/* 摘要 */}
            {paper.abstract && (
              <p className="text-slate-400 text-sm line-clamp-2 mb-3 leading-relaxed">
                {paper.abstract}
              </p>
            )}

            {/* 标签 */}
            <div className="flex flex-wrap gap-1.5">
              {paper.fields_of_study?.slice(0, 3).map((field, i) => (
                <Tag key={i} className="!bg-blue-500/10 !border-blue-500/20 !text-blue-300 text-xs">
                  {field}
                </Tag>
              ))}
              <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs">
                {source.icon} {source.name}
              </Tag>
              {paper.doi && (
                <Tag className="!bg-cyan-500/10 !border-cyan-500/20 !text-cyan-300 text-xs">
                  DOI
                </Tag>
              )}
            </div>
          </div>

          {/* 操作按钮 */}
          <div className="flex flex-col gap-2 flex-shrink-0">
            {paper.is_saved ? (
              <Button 
                className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400" 
                icon={<CheckOutlined />}
                disabled
              >
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
                className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50 hover:!text-emerald-400"
              >
                PDF
              </Button>
            )}
            {paper.url && (
              <Button 
                icon={<LinkOutlined />}
                href={paper.url}
                target="_blank"
                className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50 hover:!text-emerald-400"
              >
                链接
              </Button>
            )}
          </div>
        </div>
      </div>
    )
  }

  // 渲染搜索结果列表项
  const renderSearchResultListItem = (paper: PaperSearchResult, index: number) => {
    const source = getSourceInfo(paper.source)
    return (
      <div
        key={paper.external_id}
        className="flex items-center gap-4 px-4 py-3 border-b border-slate-700/50 hover:bg-slate-800/30 transition-colors group"
        style={{ animationDelay: `${index * 30}ms` }}
      >
        {/* 来源图标 */}
        <div className="w-10 h-10 rounded-lg bg-slate-700/50 flex items-center justify-center flex-shrink-0 text-lg">
          {source.icon}
        </div>

        {/* 主要信息 */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <a 
              href={paper.url} 
              target="_blank" 
              rel="noopener noreferrer"
              className="font-medium text-slate-200 truncate hover:text-emerald-400 transition-colors"
            >
              {paper.title}
            </a>
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-500">
            <span className="truncate max-w-[200px]">
              {paper.authors?.slice(0, 2).map(a => a.name).join(', ')}
              {paper.authors?.length > 2 && ' 等'}
            </span>
            {paper.year && <span>{paper.year}</span>}
            {paper.venue && <span className="truncate max-w-[150px]">{paper.venue}</span>}
            {paper.citation_count > 0 && (
              <span className="flex items-center gap-1">
                <FireOutlined className="text-orange-400/60" />
                {paper.citation_count}
              </span>
            )}
          </div>
        </div>

        {/* 标签 */}
        <div className="flex gap-1 flex-shrink-0">
          {paper.doi && (
            <Tag className="!bg-cyan-500/10 !border-cyan-500/20 !text-cyan-300 text-xs !m-0">
              DOI
            </Tag>
          )}
          {paper.pdf_url && (
            <Tag className="!bg-green-500/10 !border-green-500/20 !text-green-400 text-xs !m-0">
              PDF
            </Tag>
          )}
        </div>

        {/* 操作按钮 */}
        <div className="flex gap-1 flex-shrink-0">
          {paper.is_saved ? (
            <Tag className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400 !m-0">
              <CheckOutlined /> 已保存
            </Tag>
          ) : (
            <Button 
              type="primary" 
              size="small"
              icon={<PlusOutlined />}
              onClick={() => handleSavePaper(paper)}
            >
              保存
            </Button>
          )}
          {paper.pdf_url && (
            <Tooltip title="下载 PDF">
              <Button 
                size="small"
                icon={<DownloadOutlined />}
                href={paper.pdf_url}
                target="_blank"
                className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50"
              />
            </Tooltip>
          )}
          {paper.url && (
            <Tooltip title="打开链接">
              <Button 
                size="small"
                icon={<LinkOutlined />}
                href={paper.url}
                target="_blank"
                className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50"
              />
            </Tooltip>
          )}
        </div>
      </div>
    )
  }

  // 渲染论文卡片
  const renderPaperCard = (paper: Paper, index: number) => {
    const source = getSourceInfo(paper.source)
    return (
      <div
        key={paper.id}
        className="glass-card p-4 cursor-pointer hover:border-emerald-500/30 hover:shadow-lg hover:shadow-emerald-500/5 transition-all duration-300 group"
        style={{ animationDelay: `${index * 50}ms` }}
        onClick={() => {
          selectPaper(paper)
          toggleDetailPanel(true)
        }}
      >
        <div className="flex justify-between gap-4">
          <div className="flex-1 min-w-0">
            {/* 标题行 */}
            <div className="flex items-start gap-2 mb-2">
              <h4 className="font-semibold text-base text-slate-100 leading-snug flex-1 line-clamp-2 group-hover:text-emerald-400 transition-colors">
                {paper.title}
              </h4>
              <div className="flex items-center gap-1 flex-shrink-0">
                {paper.is_read && (
                  <Tag className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400 !m-0" icon={<CheckOutlined />}>
                    已读
                  </Tag>
                )}
                {paper.rating > 0 && (
                  <Rate disabled value={paper.rating} className="text-sm !text-yellow-400" />
                )}
              </div>
            </div>
            
            {/* 作者 */}
            <div className="text-slate-400 text-sm mb-2 flex items-center gap-1">
              <TeamOutlined className="text-emerald-500/60" />
              <span className="truncate">
                {paper.authors?.slice(0, 3).map(a => a.name).join(', ')}
                {paper.authors?.length > 3 && ' 等'}
              </span>
            </div>

            {/* 元信息 */}
            <div className="flex items-center gap-4 text-sm text-slate-500 mb-3">
              {paper.year && (
                <span className="flex items-center gap-1">
                  <CalendarOutlined className="text-blue-400/60" />
                  {paper.year}
                </span>
              )}
              {paper.venue && (
                <span className="flex items-center gap-1 truncate max-w-[200px]">
                  <BookOutlined className="text-purple-400/60" />
                  {paper.venue}
                </span>
              )}
              {paper.citation_count > 0 && (
                <span className="flex items-center gap-1">
                  <FireOutlined className="text-orange-400/60" />
                  {paper.citation_count} 引用
                </span>
              )}
            </div>

            {/* 标签 */}
            <div className="flex flex-wrap gap-1.5">
              {paper.tags?.map((tag, i) => (
                <Tag key={i} className="!bg-slate-500/10 !border-slate-500/20 !text-slate-300 text-xs">
                  {tag}
                </Tag>
              ))}
              <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs">
                {source.icon} {source.name}
              </Tag>
              {paper.pdf_downloaded && (
                <Tag className="!bg-green-500/10 !border-green-500/20 !text-green-400 text-xs" icon={<FileTextOutlined />}>
                  PDF
                </Tag>
              )}
            </div>
          </div>

          {/* 操作菜单 */}
          <div className="flex flex-col items-end" onClick={e => e.stopPropagation()}>
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
              <Button 
                type="text" 
                icon={<MoreOutlined />} 
                className="!text-slate-400 hover:!text-emerald-400 opacity-0 group-hover:opacity-100 transition-opacity"
              />
            </Dropdown>
          </div>
        </div>
      </div>
    )
  }

  // 渲染列表视图项
  const renderListItem = (paper: Paper, index: number) => {
    const source = getSourceInfo(paper.source)
    return (
      <div
        key={paper.id}
        className="flex items-center gap-4 px-4 py-3 border-b border-slate-700/50 hover:bg-slate-800/30 cursor-pointer transition-colors group"
        style={{ animationDelay: `${index * 30}ms` }}
        onClick={() => {
          selectPaper(paper)
          toggleDetailPanel(true)
        }}
      >
        {/* 状态图标 */}
        <div className="w-8 h-8 rounded-lg bg-slate-700/50 flex items-center justify-center flex-shrink-0">
          {paper.is_read ? (
            <CheckOutlined className="text-emerald-400" />
          ) : (
            <FileTextOutlined className="text-slate-500" />
          )}
        </div>

        {/* 主要信息 */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-200 truncate group-hover:text-emerald-400 transition-colors">
              {paper.title}
            </span>
            {paper.rating > 0 && (
              <Rate disabled value={paper.rating} className="text-xs !text-yellow-400" />
            )}
          </div>
          <div className="flex items-center gap-3 text-sm text-slate-500">
            <span>{paper.authors?.slice(0, 2).map(a => a.name).join(', ')}</span>
            {paper.year && <span>{paper.year}</span>}
            {paper.citation_count > 0 && <span>{paper.citation_count} 引用</span>}
          </div>
        </div>

        {/* 来源标签 */}
        <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs !m-0">
          {source.icon}
        </Tag>

        {/* 操作 */}
        <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity" onClick={e => e.stopPropagation()}>
          <Tooltip title="引用图谱">
            <Button 
              type="text" 
              size="small"
              icon={<NodeIndexOutlined />}
              onClick={() => handleShowGraph(paper)}
              disabled={!paper.semantic_scholar_id}
              className="!text-slate-400 hover:!text-emerald-400"
            />
          </Tooltip>
          <Tooltip title="删除">
            <Button 
              type="text" 
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={() => handleDeletePaper(paper.id)}
            />
          </Tooltip>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex overflow-hidden">
      {/* 左侧收藏夹面板 */}
      <div className="w-64 border-r border-slate-700/50 flex flex-col bg-slate-900/30 flex-shrink-0">
        {/* 标题 */}
        <div className="p-4 border-b border-slate-700/50 flex-shrink-0">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-lg text-slate-200 flex items-center gap-2">
              <FolderOutlined className="text-emerald-400" />
              收藏夹
            </h3>
            <Tooltip title="新建收藏夹">
              <Button 
                type="text" 
                icon={<PlusOutlined />}
                onClick={() => setCreateModalOpen(true)}
                className="!text-slate-400 hover:!text-emerald-400"
              />
            </Tooltip>
          </div>
        </div>

        {/* 收藏夹列表 - 可滚动 */}
        <div className="flex-1 overflow-y-auto p-2 scrollbar-thin">
          {collectionsLoading ? (
            <div className="flex justify-center py-8">
              <Spin />
            </div>
          ) : (
            <div className="space-y-1">
              {/* 全部论文 */}
              <div
                className={`px-3 py-2.5 rounded-lg cursor-pointer flex justify-between items-center transition-all ${
                  selectedCollectionId === null 
                    ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30' 
                    : 'hover:bg-slate-800/50 text-slate-400 border border-transparent'
                }`}
                onClick={() => selectCollection(null)}
              >
                <span className="flex items-center gap-2">
                  <DatabaseOutlined />
                  全部论文
                </span>
                <Badge 
                  count={papers.length} 
                  showZero 
                  className={selectedCollectionId === null ? '[&_.ant-badge-count]:!bg-emerald-500' : '[&_.ant-badge-count]:!bg-slate-600'}
                />
              </div>
              
              {/* 收藏夹列表 */}
              {collections.map(coll => (
                <div
                  key={coll.id}
                  className={`px-3 py-2.5 rounded-lg cursor-pointer flex justify-between items-center group transition-all ${
                    selectedCollectionId === coll.id 
                      ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30' 
                      : 'hover:bg-slate-800/50 text-slate-400 border border-transparent'
                  }`}
                  onClick={() => selectCollection(coll.id)}
                >
                  <span className="flex items-center gap-2 min-w-0">
                    <span 
                      className="w-3 h-3 rounded flex-shrink-0" 
                      style={{ backgroundColor: coll.color }}
                    />
                    <span className="truncate">{coll.name}</span>
                  </span>
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <Badge 
                      count={coll.paper_count} 
                      showZero 
                      className={selectedCollectionId === coll.id ? '[&_.ant-badge-count]:!bg-emerald-500' : '[&_.ant-badge-count]:!bg-slate-600'}
                    />
                    {!coll.is_default && (
                      <Button
                        type="text"
                        size="small"
                        icon={<DeleteOutlined />}
                        className="!text-slate-500 hover:!text-red-400 opacity-0 group-hover:opacity-100 transition-opacity !w-6 !h-6 !min-w-0"
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
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 主内容区 */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* 顶部搜索栏 */}
        <div className="p-4 border-b border-slate-700/50 bg-slate-900/50 backdrop-blur-sm flex-shrink-0">
          <div className="flex gap-4 items-center">
            {/* 数据源选择 */}
            <Select
              value={selectedSource}
              onChange={setSelectedSource}
              style={{ width: 180 }}
              className="[&_.ant-select-selector]:!bg-slate-800/50 [&_.ant-select-selector]:!border-slate-600"
            >
              {sources.map(s => (
                <Option key={s.key} value={s.key}>
                  <span className="flex items-center gap-2">
                    <span>{s.icon}</span>
                    <span>{s.name}</span>
                  </span>
                </Option>
              ))}
            </Select>
            
            {/* 搜索框 */}
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
                className={`px-4 py-2.5 rounded-t-lg font-medium text-sm flex items-center gap-2 transition-all border-b-2 -mb-[1px] ${
                  activeTab === tab.key
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

        {/* 内容区 - 关键：使用 flex-1 和 overflow-y-auto */}
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
                  {papers.map((paper, i) => renderPaperCard(paper, i))}
                </div>
              ) : (
                <div className="glass-card overflow-hidden">
                  {papers.map((paper, i) => renderListItem(paper, i))}
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
                      在 {getSourceInfo(selectedSource).name} 找到约 <span className="text-emerald-400 font-semibold">{searchTotal.toLocaleString()}</span> 篇关于 "<span className="text-slate-200">{searchQuery}</span>" 的论文
                    </div>
                    <div className="text-slate-500 text-sm">
                      已加载 {searchOffset} / {searchTotal.toLocaleString()}
                    </div>
                  </div>

                  {/* 根据视图模式渲染 */}
                  {viewMode === 'card' ? (
                    <div className="grid grid-cols-1 gap-3">
                      {searchResults.map((paper, i) => renderSearchResultCard(paper, i))}
                    </div>
                  ) : (
                    <div className="glass-card overflow-hidden">
                      {searchResults.map((paper, i) => renderSearchResultListItem(paper, i))}
                    </div>
                  )}

                  {/* 加载更多按钮 */}
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

                  {/* 已加载全部提示 */}
                  {!searchHasMore && searchResults.length > 0 && (
                    <div className="text-center text-slate-500 text-sm py-6 border-t border-slate-700/50 mt-4">
                      已加载全部 {searchOffset} 篇论文
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* 引用图谱 Tab */}
          {activeTab === 'graph' && (
            <div className="absolute inset-0">
              {graphLoading ? (
                <div className="h-full flex flex-col items-center justify-center">
                  <Spin size="large" />
                  <p className="text-slate-500 mt-4">加载引用图谱...</p>
                </div>
              ) : citationGraph ? (
                <CitationGraph 
                  data={citationGraph} 
                  onNodeClick={(nodeId) => {
                    console.log('Node clicked:', nodeId)
                  }}
                />
              ) : (
                <div className="h-full flex flex-col items-center justify-center">
                  <div className="w-20 h-20 rounded-full bg-slate-800/50 flex items-center justify-center mb-4">
                    <NodeIndexOutlined className="text-4xl text-slate-600" />
                  </div>
                  <p className="text-slate-400 text-lg mb-2">选择一篇论文查看引用图谱</p>
                  <p className="text-slate-500 text-sm">引用图谱展示论文间的引用关系网络</p>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 右侧详情面板 */}
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
        {selectedPaper && (
          <PaperDetailPanel 
            paper={selectedPaper}
            onShowGraph={() => handleShowGraph(selectedPaper)}
          />
        )}
      </Drawer>

      {/* 创建收藏夹对话框 */}
      <Modal
        title={
          <span className="flex items-center gap-2">
            <FolderOutlined className="text-emerald-400" />
            创建收藏夹
          </span>
        }
        open={createModalOpen}
        onCancel={() => setCreateModalOpen(false)}
        onOk={() => form.submit()}
        okText="创建"
        cancelText="取消"
      >
        <Form form={form} onFinish={handleCreateCollection} layout="vertical" className="mt-4">
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
    </div>
  )
}
