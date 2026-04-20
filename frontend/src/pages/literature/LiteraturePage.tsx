import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Input,
  Button,
  Select,
  Spin,
  message,
  Modal,
  Form,
  Badge,
  Tooltip,
} from 'antd'
import {
  SearchOutlined,
  BookOutlined,
  AppstoreOutlined,
  UnorderedListOutlined,
  DownloadOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  LinkOutlined,
  ArrowRightOutlined,
  CloseOutlined,
} from '@ant-design/icons'
import { useLiteratureStore } from '@/stores/literatureStore'
import type { Paper, PaperSearchResult } from '@/services/api'
import PaperDetailPanel from './PaperDetailPanel'
import { getSourceInfo, SOURCES } from './constants'
import {
  PaperCard,
  PaperListItem,
  SearchResultCard,
  SearchResultListItem,
  CollectionSidebar,
} from './components'

const { Option } = Select

const sectionCardClass =
  'overflow-hidden rounded-[28px] border border-white/[0.06] bg-slate-900/50 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_28px_60px_rgba(2,6,23,0.34)] backdrop-blur-2xl'

const controlButtonClass =
  '!h-11 !rounded-2xl !border-white/10 !bg-white/[0.04] !px-4 !text-slate-200 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] hover:!border-white/15 hover:!bg-white/[0.08] hover:!text-white'

const iconToggleButtonClass =
  'relative flex h-10 w-10 appearance-none bg-transparent outline-none items-center justify-center rounded-2xl border border-transparent text-slate-400 transition-all duration-200 hover:text-white'

const tabButtonClass =
  'relative flex cursor-pointer appearance-none bg-transparent border-none outline-none min-w-[132px] items-center justify-center gap-2 rounded-2xl px-4 py-3 text-sm font-medium transition-colors'

const floatingListShellClass =
  'space-y-2'

const emptyGlowClass =
  'absolute inset-x-10 top-6 h-32 rounded-full bg-emerald-400/12 blur-3xl'

const getSearchResultIdentity = (paper: Pick<PaperSearchResult, 'source' | 'external_id' | 'doi' | 'arxiv_id' | 'title'>): string => {
  const source = String(paper.source || '').trim().toLowerCase()
  const externalId = String(paper.external_id || '').trim()
  if (source && externalId) return `${source}:${externalId}`
  const doi = String(paper.doi || '').trim().toLowerCase()
  if (doi) return `doi:${doi}`
  const arxivId = String(paper.arxiv_id || '').trim().toLowerCase()
  if (arxivId) return `arxiv:${arxivId}`
  return `title:${String(paper.title || '').trim().toLowerCase()}`
}

const EmptyState = ({
  icon,
  title,
  description,
  action,
}: {
  icon: React.ReactNode
  title: string
  description: string
  action?: React.ReactNode
}) => (
  <div className="relative flex min-h-[360px] flex-col items-center justify-center overflow-hidden rounded-[28px] border border-white/[0.05] bg-white/[0.02] px-6 py-12 text-center">
    <div className={emptyGlowClass} />
    <div className="relative z-[1] flex h-20 w-20 items-center justify-center rounded-full border border-white/10 bg-white/[0.05] shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-emerald-400/12 text-3xl text-emerald-300 animate-pulse">
        {icon}
      </div>
    </div>
    <p className="relative z-[1] mt-6 text-lg font-medium text-slate-100">{title}</p>
    <p className="relative z-[1] mt-2 max-w-md text-sm leading-6 text-slate-500">{description}</p>
    {action ? <div className="relative z-[1] mt-6">{action}</div> : null}
  </div>
)

const matchPaper = (paper: Paper, query: string) => {
  if (!query) return true

  const haystack = [
    paper.title,
    paper.abstract,
    paper.venue,
    paper.year ? String(paper.year) : '',
    paper.notes,
    paper.authors?.map((author) => author.name).join(' '),
    paper.tags?.join(' '),
    paper.fields_of_study?.join(' '),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()

  return haystack.includes(query)
}

export default function LiteraturePage() {
  const {
    papers,
    papersLoading,
    searchResults,
    searchQuery,
    searchTotal,
    searchLoading,
    searchOffset,
    searchLoadingMore,
    searchHasMore,
    loadMoreSearchResults,
    collections,
    selectedCollectionId,
    collectionsLoading,
    selectedPaper,
    detailPanelOpen,
    viewMode,
    init,
    searchPapers,
    savePaper,
    importPaperFromLink,
    deletePaper,
    selectPaper,
    createCollection,
    deleteCollection,
    selectCollection,
    setViewMode,
    toggleDetailPanel,
    downloadPdf,
  } = useLiteratureStore()

  const [activeTab, setActiveTab] = useState<'library' | 'search'>('library')
  const [searchValue, setSearchValue] = useState('')
  const [selectedSource, setSelectedSource] = useState('multi')
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [linkImportModalOpen, setLinkImportModalOpen] = useState(false)
  const [linkImportSubmitting, setLinkImportSubmitting] = useState(false)
  const [savingPaperKeys, setSavingPaperKeys] = useState<Record<string, true>>({})
  const [createCollectionForm] = Form.useForm()
  const [linkImportForm] = Form.useForm()

  const selectedCollection =
    collections.find((collection) => collection.id === selectedCollectionId) || null
  const deferredSearchValue = useDeferredValue(searchValue.trim().toLowerCase())
  const showDetailPane = detailPanelOpen && !!selectedPaper
  const currentSource = getSourceInfo(selectedSource)

  const filteredPapers = useMemo(
    () => papers.filter((paper) => matchPaper(paper, deferredSearchValue)),
    [deferredSearchValue, papers],
  )

  const libraryScopeLabel = selectedCollection
    ? `收藏夹 · ${selectedCollection.name}`
    : '全部文献'

  useEffect(() => {
    init()
  }, [init])

  const handleRemoteSearch = async () => {
    if (!searchValue.trim()) return
    setActiveTab('search')
    await searchPapers(searchValue.trim(), selectedSource, {})
  }

  const handleSearchValueChange = (value: string) => {
    setSearchValue(value)
    if (activeTab === 'search' && !value.trim()) {
      setActiveTab('library')
    }
  }

  const handleSavePaper = async (paper: PaperSearchResult) => {
    const paperKey = getSearchResultIdentity(paper)
    if (paper.is_saved || savingPaperKeys[paperKey]) return
    setSavingPaperKeys(state => ({ ...state, [paperKey]: true }))
    try {
      await savePaper(paper)
      message.success('论文已保存到文献库')
    } catch {
      // Error handled by store
    } finally {
      setSavingPaperKeys(state => {
        const next = { ...state }
        delete next[paperKey]
        return next
      })
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
          if (selectedPaper?.id === paperId) {
            selectPaper(null)
            toggleDetailPanel(false)
          }
        } catch {
          // Error handled by store
        }
      },
    })
  }

  const handleSelectPaper = (paper: Paper) => {
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

  const handleCreateCollection = async (values: {
    name: string
    description?: string
    color?: string
  }) => {
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
        selectedCollectionId ? [selectedCollectionId] : [],
      )
      setActiveTab('library')
      setLinkImportModalOpen(false)
      linkImportForm.resetFields()
      selectPaper(result.paper)
      toggleDetailPanel(true)
      if (result.already_exists) {
        message.info(
          selectedCollection
            ? `论文已存在，已同步到“${selectedCollection.name}”`
            : '论文已存在，已定位到现有记录',
        )
      } else {
        message.success('论文已通过链接入库')
      }
    } catch {
      // Error handled by store
    } finally {
      setLinkImportSubmitting(false)
    }
  }

  const tabConfig = [
    { key: 'library', label: '文献库', icon: <BookOutlined />, badge: filteredPapers.length },
    { key: 'search', label: '搜索结果', icon: <SearchOutlined />, badge: searchTotal },
  ] as const

  const renderLibraryContent = () => {
    if (papersLoading) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center">
          <Spin size="large" />
          <p className="mt-4 text-slate-500">正在同步文献库...</p>
        </div>
      )
    }

    if (papers.length === 0) {
      return (
        <EmptyState
          icon={<BookOutlined />}
          title="文献库还是空的"
          description="先通过上方的全网搜索或链接入库把论文带进来。这里之后会成为你连续审阅、打标签和归档的总控台。"
          action={
            <Button className={controlButtonClass} icon={<LinkOutlined />} onClick={() => setLinkImportModalOpen(true)}>
              先导入一篇论文
            </Button>
          }
        />
      )
    }

    if (filteredPapers.length === 0) {
      return (
        <EmptyState
          icon={<SearchOutlined />}
          title="当前收藏范围内没有匹配项"
          description={`你正在“${libraryScopeLabel}”中做本地实时过滤。换个关键词，或者点击右侧“全网搜索”去扩展样本。`}
        />
      )
    }

    if (viewMode === 'card') {
      return (
        <div className="grid grid-cols-1 gap-3">
          {filteredPapers.map((paper, index) => (
            <PaperCard
              key={paper.id}
              paper={paper}
              index={index}
              sourceInfo={getSourceInfo(paper.source)}
              onSelect={handleSelectPaper}
              onDelete={handleDeletePaper}
              onDownloadPdf={handleDownloadPdf}
            />
          ))}
        </div>
      )
    }

    return (
      <div className={floatingListShellClass}>
        {filteredPapers.map((paper, index) => (
          <PaperListItem
            key={paper.id}
            paper={paper}
            index={index}
            selected={selectedPaper?.id === paper.id}
            sourceInfo={getSourceInfo(paper.source)}
            onSelect={handleSelectPaper}
            onDelete={handleDeletePaper}
          />
        ))}
      </div>
    )
  }

  const renderSearchContent = () => {
    if (searchLoading) {
      return (
        <div className="flex min-h-[360px] flex-col items-center justify-center">
          <Spin size="large" />
          <p className="mt-4 text-slate-500">正在从 {currentSource.name} 编织搜索结果...</p>
        </div>
      )
    }

    if (!searchQuery) {
      return (
        <EmptyState
          icon={<SearchOutlined />}
          title="远端搜索还未开始"
          description="上面的输入框现在优先服务本地过滤。确认关键词后，点击“全网搜索”才会切到这里并调用远端数据源。"
        />
      )
    }

    if (searchResults.length === 0) {
      return (
        <EmptyState
          icon={<SearchOutlined />}
          title={`没有找到 “${searchQuery}”`}
          description="可以换一个关键词、切换数据源，或者先在左边文献库做本地筛选确认你已经保存过这篇论文。"
        />
      )
    }

    return (
      <div>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-[22px] border border-white/[0.06] bg-white/[0.03] px-4 py-3">
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <ClockCircleOutlined />
            在 {currentSource.name} 找到约
            <span className="font-semibold text-emerald-300">{searchTotal.toLocaleString()}</span>
            篇关于
            <span className="font-medium text-slate-100">“{searchQuery}”</span>
            的论文
          </div>
          <div className="text-sm text-slate-500">
            已加载 {searchOffset} / {searchTotal.toLocaleString()}
          </div>
        </div>

        {viewMode === 'card' ? (
          <div className="grid grid-cols-1 gap-3">
            {searchResults.map((paper, index) => (
              <SearchResultCard
                key={getSearchResultIdentity(paper)}
                paper={paper}
                index={index}
                sourceInfo={getSourceInfo(paper.source)}
                onSave={handleSavePaper}
                savePending={Boolean(savingPaperKeys[getSearchResultIdentity(paper)])}
              />
            ))}
          </div>
        ) : (
          <div className={floatingListShellClass}>
            {searchResults.map((paper, index) => (
              <SearchResultListItem
                key={getSearchResultIdentity(paper)}
                paper={paper}
                index={index}
                sourceInfo={getSourceInfo(paper.source)}
                onSave={handleSavePaper}
                savePending={Boolean(savingPaperKeys[getSearchResultIdentity(paper)])}
              />
            ))}
          </div>
        )}

        {searchHasMore ? (
          <div className="mt-6 flex justify-center">
            <Button
              size="large"
              loading={searchLoadingMore}
              onClick={loadMoreSearchResults}
              className={`${controlButtonClass} !px-8`}
              icon={<DownloadOutlined />}
            >
              {searchLoadingMore
                ? '加载中...'
                : `加载更多 (还有 ${(searchTotal - searchOffset).toLocaleString()} 篇)`}
            </Button>
          </div>
        ) : (
          <div className="mt-6 border-t border-white/[0.06] pt-6 text-center text-sm text-slate-500">
            已加载全部 {searchOffset} 篇论文
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full overflow-hidden bg-slate-950">
      <CollectionSidebar
        collections={collections}
        collectionsLoading={collectionsLoading}
        selectedCollectionId={selectedCollectionId}
        totalPapers={papers.length}
        onSelectCollection={(collectionId) => {
          setActiveTab('library')
          selectCollection(collectionId)
        }}
        onDeleteCollection={deleteCollection}
        onCreateClick={() => setCreateModalOpen(true)}
      />

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden p-4 sm:p-6">
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden rounded-[32px] bg-[radial-gradient(circle_at_top_left,rgba(16,185,129,0.1),transparent_24%),radial-gradient(circle_at_top_right,rgba(56,189,248,0.08),transparent_18%),linear-gradient(180deg,rgba(2,6,23,0.92)_0%,rgba(15,23,42,0.88)_100%)]">
          <div className="sticky top-0 z-20 flex-shrink-0">
            <div className="rounded-[24px] border border-white/[0.06] bg-slate-950/72 p-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.04),0_18px_40px_rgba(2,6,23,0.24)] backdrop-blur-2xl">
              <div className="flex flex-wrap items-center gap-2">
                <div className="inline-flex rounded-[20px] border border-white/[0.06] bg-white/[0.03] p-1 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]">
                  {tabConfig.map((tab) => {
                    const isActive = activeTab === tab.key
                    return (
                      <button
                        key={tab.key}
                        type="button"
                        onClick={() => setActiveTab(tab.key)}
                        className={`${tabButtonClass} ${isActive ? 'text-slate-50' : 'text-slate-400 hover:text-white'}`}
                      >
                        {isActive ? (
                          <motion.span
                            layoutId="literature-tab-pill"
                            className="absolute inset-0 rounded-[18px] border border-white/10 bg-white/[0.08] shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]"
                          />
                        ) : null}
                        <span className="relative z-[1] flex items-center gap-2">
                          {tab.icon}
                          <span>{tab.label}</span>
                          {tab.badge > 0 ? (
                            <Badge
                              count={tab.badge}
                              className="[&_.ant-badge-count]:!bg-white/[0.12] [&_.ant-badge-count]:!text-slate-200 [&_.ant-badge-count]:!shadow-none"
                            />
                          ) : null}
                        </span>
                      </button>
                    )
                  })}
                </div>

                <div className="min-w-[180px]">
                  <Select
                    value={selectedSource}
                    onChange={setSelectedSource}
                    style={{ width: 180 }}
                    className="[&_.ant-select-selector]:!h-11 [&_.ant-select-selector]:!rounded-2xl [&_.ant-select-selector]:!border-white/10 [&_.ant-select-selector]:!bg-white/[0.04] [&_.ant-select-selector]:!px-4 [&_.ant-select-selector]:!shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] [&_.ant-select-arrow]:!text-slate-400 [&_.ant-select-arrow]:!opacity-70"
                  >
                    {SOURCES.map((source) => (
                      <Option key={source.key} value={source.key}>
                        <span className="flex items-center gap-2">
                          <span>{source.icon}</span>
                          <span>{source.name}</span>
                        </span>
                      </Option>
                    ))}
                  </Select>
                </div>

                <div className="min-w-[260px] flex-[1_1_360px]">
                  <Input
                    value={searchValue}
                    onChange={(event) => handleSearchValueChange(event.target.value)}
                    onPressEnter={() => {
                      if (activeTab === 'search' && searchValue.trim()) {
                        void handleRemoteSearch()
                      }
                    }}
                    prefix={<SearchOutlined className="text-slate-500" />}
                    placeholder={
                      activeTab === 'library'
                        ? `在“${libraryScopeLabel}”中过滤标题、作者、标签、摘要...`
                        : `输入关键词，点击“全网搜索”刷新 ${currentSource.name} 结果...`
                    }
                    className="h-11 rounded-2xl border-white/10 bg-white/[0.04] px-3 text-slate-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] hover:border-white/15"
                  />
                </div>

                <Button
                  className={controlButtonClass}
                  icon={<ArrowRightOutlined />}
                  loading={searchLoading && activeTab === 'search'}
                  onClick={() => void handleRemoteSearch()}
                >
                  全网搜索
                </Button>

                <Button
                  className={controlButtonClass}
                  icon={<LinkOutlined />}
                  onClick={() => setLinkImportModalOpen(true)}
                >
                  链接入库
                </Button>

                <div className="sm:ml-auto flex items-center gap-2 rounded-[20px] border border-white/[0.06] bg-white/[0.03] p-1">
                  <Tooltip title="卡片视图">
                    <button
                      type="button"
                      className={`${iconToggleButtonClass} ${viewMode === 'card' ? 'border-white/10 bg-white/[0.08] text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]' : ''}`}
                      onClick={() => setViewMode('card')}
                    >
                      <AppstoreOutlined />
                    </button>
                  </Tooltip>
                  <Tooltip title="列表视图">
                    <button
                      type="button"
                      className={`${iconToggleButtonClass} ${viewMode === 'list' ? 'border-white/10 bg-white/[0.08] text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]' : ''}`}
                      onClick={() => setViewMode('list')}
                    >
                      <UnorderedListOutlined />
                    </button>
                  </Tooltip>
                </div>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 gap-3 overflow-hidden">
            <div
              className={`min-w-0 transition-all duration-300 ${showDetailPane ? 'w-[52%]' : 'w-full'}`}
            >
              <div className={`${sectionCardClass} flex h-full min-h-0 flex-col`}>
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <p className="truncate text-sm font-medium text-slate-100">
                      {activeTab === 'library' ? '文献库' : '搜索结果'}
                    </p>
                    <span className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-xs text-slate-400">
                      {activeTab === 'library'
                        ? `${filteredPapers.length} / ${papers.length} 篇可见`
                        : `${searchOffset} / ${searchTotal.toLocaleString()} 已加载`}
                    </span>
                  </div>
                  <p className="min-w-0 truncate text-xs text-slate-500">
                    {activeTab === 'library'
                      ? deferredSearchValue
                        ? `过滤词：${deferredSearchValue}`
                        : `当前范围：${libraryScopeLabel}`
                      : searchQuery
                        ? `远端查询：${searchQuery}`
                        : `数据源：${currentSource.name}`}
                  </p>
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto p-4">
                  <AnimatePresence mode="wait">
                    <motion.div
                      key={activeTab}
                      initial={{ opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -8 }}
                      transition={{ duration: 0.18 }}
                    >
                      {activeTab === 'library' ? renderLibraryContent() : renderSearchContent()}
                    </motion.div>
                  </AnimatePresence>
                </div>
              </div>
            </div>

            <AnimatePresence>
              {showDetailPane && selectedPaper ? (
                <motion.aside
                  initial={{ width: 0, opacity: 0, x: 24 }}
                  animate={{ width: '48%', opacity: 1, x: 0 }}
                  exit={{ width: 0, opacity: 0, x: 24 }}
                  transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
                  className="min-w-0 overflow-hidden"
                >
                  <div className={`${sectionCardClass} flex h-full min-h-0 flex-col`}>
                    <div className="flex items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-3">
                      <p className="truncate text-sm font-medium text-slate-200">当前论文速读</p>
                      <button
                        type="button"
                        className="appearance-none outline-none flex h-10 w-10 items-center justify-center rounded-2xl border border-white/10 bg-white/[0.04] text-slate-400 transition-all hover:border-white/15 hover:bg-white/[0.08] hover:text-white"
                        onClick={() => toggleDetailPanel(false)}
                      >
                        <CloseOutlined />
                      </button>
                    </div>

                    <div className="min-h-0 flex-1 overflow-y-auto p-4">
                      <PaperDetailPanel paper={selectedPaper} />
                    </div>
                  </div>
                </motion.aside>
              ) : null}
            </AnimatePresence>
          </div>
        </div>
      </div>

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
              placeholder={
                '例如：10.1038/s41586-023-07042-4\nhttps://arxiv.org/abs/2401.12345\nhttps://pubmed.ncbi.nlm.nih.gov/12345678/'
              }
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
