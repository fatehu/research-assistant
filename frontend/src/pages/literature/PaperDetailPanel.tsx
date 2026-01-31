import { useState, useEffect } from 'react'
import { 
  Button, Tag, Space, Rate, Input, message, Tooltip, 
  Select
} from 'antd'
import {
  LinkOutlined, DownloadOutlined, BookOutlined, CalendarOutlined,
  TeamOutlined, StarOutlined, EditOutlined, SaveOutlined,
  CopyOutlined, CheckOutlined, FileTextOutlined,
  FolderAddOutlined, EyeOutlined, TagsOutlined, FireOutlined,
  CloseOutlined
} from '@ant-design/icons'
import { Paper } from '@/services/api'
import { useLiteratureStore } from '@/stores/literatureStore'

const { TextArea } = Input

interface PaperDetailPanelProps {
  paper: Paper
}

export default function PaperDetailPanel({ paper }: PaperDetailPanelProps) {
  const { 
    collections, 
    updatePaper, 
    addToCollection, 
    removeFromCollection,
    downloadPdf 
  } = useLiteratureStore()

  const [editingNotes, setEditingNotes] = useState(false)
  const [notes, setNotes] = useState(paper.notes || '')
  const [editingTags, setEditingTags] = useState(false)
  const [tags, setTags] = useState<string[]>(paper.tags || [])
  const [newTag, setNewTag] = useState('')
  const [abstractExpanded, setAbstractExpanded] = useState(false)

  // 当论文切换时，重置所有内部状态
  useEffect(() => {
    setNotes(paper.notes || '')
    setTags(paper.tags || [])
    setEditingNotes(false)
    setEditingTags(false)
    setNewTag('')
    setAbstractExpanded(false)
  }, [paper.id, paper.notes, paper.tags])

  // 更新阅读状态
  const handleToggleRead = async () => {
    try {
      await updatePaper(paper.id, { is_read: !paper.is_read })
      message.success(paper.is_read ? '已标记为未读' : '已标记为已读')
    } catch {
      message.error('更新失败')
    }
  }

  // 更新评分
  const handleRatingChange = async (value: number) => {
    try {
      await updatePaper(paper.id, { rating: value })
      message.success('评分已更新')
    } catch {
      message.error('评分更新失败')
    }
  }

  // 保存笔记
  const handleSaveNotes = async () => {
    try {
      await updatePaper(paper.id, { notes })
      setEditingNotes(false)
      message.success('笔记已保存')
    } catch {
      message.error('保存失败')
    }
  }

  // 保存标签
  const handleSaveTags = async () => {
    try {
      await updatePaper(paper.id, { tags })
      setEditingTags(false)
      message.success('标签已保存')
    } catch {
      message.error('保存失败')
    }
  }

  // 添加标签
  const handleAddTag = () => {
    if (newTag && !tags.includes(newTag)) {
      setTags([...tags, newTag])
      setNewTag('')
    }
  }

  // 移除标签
  const handleRemoveTag = (tag: string) => {
    setTags(tags.filter(t => t !== tag))
  }

  // 添加到收藏夹
  const handleAddToCollection = async (collectionId: number) => {
    try {
      await addToCollection(paper.id, [collectionId])
      message.success('已添加到收藏夹')
    } catch {
      message.error('添加失败')
    }
  }

  // 从收藏夹移除
  const handleRemoveFromCollection = async (collectionId: number) => {
    try {
      await removeFromCollection(paper.id, collectionId)
      message.success('已从收藏夹移除')
    } catch {
      message.error('移除失败')
    }
  }

  // 下载 PDF
  const handleDownloadPdf = async () => {
    if (!paper.pdf_url) {
      message.warning('该论文没有可用的 PDF 链接')
      return
    }
    try {
      await downloadPdf(paper.id)
      message.success('PDF 下载成功')
    } catch {
      message.error('下载失败')
    }
  }

  // 复制引用
  const handleCopyCitation = () => {
    const authors = paper.authors?.map(a => a.name).join(', ') || 'Unknown'
    const citation = `${authors}. "${paper.title}". ${paper.venue || ''} ${paper.year ? `(${paper.year})` : ''}`
    navigator.clipboard.writeText(citation)
    message.success('引用已复制到剪贴板')
  }

  // 获取未添加的收藏夹
  const availableCollections = collections.filter(
    c => !paper.collection_ids?.includes(c.id) && !c.is_default
  )

  // 获取已添加的收藏夹
  const addedCollections = collections.filter(
    c => paper.collection_ids?.includes(c.id)
  )

  return (
    <div className="space-y-5 text-slate-300">
      {/* 标题 */}
      <div>
        <h3 className="text-lg font-semibold text-slate-100 leading-snug mb-3">
          {paper.title}
        </h3>
        
        {/* 操作按钮 */}
        <div className="flex flex-wrap gap-2 mb-4">
          <Button 
            type={paper.is_read ? 'default' : 'primary'}
            icon={paper.is_read ? <CheckOutlined /> : <EyeOutlined />}
            onClick={handleToggleRead}
            size="small"
            className={paper.is_read ? '!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400' : ''}
          >
            {paper.is_read ? '已读' : '标记已读'}
          </Button>
          
          <Button
            icon={<CopyOutlined />}
            onClick={handleCopyCitation}
            size="small"
            className="!border-slate-600 !text-slate-300"
          >
            复制引用
          </Button>
        </div>

        {/* 评分 */}
        <div className="flex items-center gap-3 p-3 rounded-lg bg-slate-800/30 border border-slate-700/50">
          <span className="text-slate-500 text-sm">评分</span>
          <Rate 
            value={paper.rating || 0} 
            onChange={handleRatingChange}
            allowClear
            className="!text-yellow-400"
          />
        </div>
      </div>

      {/* 分割线 */}
      <div className="h-px bg-slate-700/50" />

      {/* 作者信息 */}
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-slate-500 text-sm">
          <TeamOutlined className="text-emerald-500/60" />
          <span>作者</span>
        </div>
        <div className="text-sm text-slate-300 leading-relaxed">
          {paper.authors?.map((author, i) => (
            <span key={i}>
              {author.name}
              {i < paper.authors.length - 1 && ', '}
            </span>
          )) || <span className="text-slate-500">未知</span>}
        </div>
      </div>

      {/* 发表信息 */}
      <div className="grid grid-cols-2 gap-3">
        {paper.year && (
          <div className="p-3 rounded-lg bg-slate-800/30 border border-slate-700/50">
            <div className="flex items-center gap-1 text-slate-500 text-xs mb-1">
              <CalendarOutlined className="text-blue-400/60" />
              年份
            </div>
            <div className="text-slate-200 font-medium">{paper.year}</div>
          </div>
        )}
        {paper.venue && (
          <div className="p-3 rounded-lg bg-slate-800/30 border border-slate-700/50">
            <div className="flex items-center gap-1 text-slate-500 text-xs mb-1">
              <BookOutlined className="text-purple-400/60" />
              发表
            </div>
            <div className="text-slate-200 font-medium text-sm truncate" title={paper.venue}>
              {paper.venue}
            </div>
          </div>
        )}
        {paper.citation_count > 0 && (
          <div className="p-3 rounded-lg bg-slate-800/30 border border-slate-700/50">
            <div className="flex items-center gap-1 text-slate-500 text-xs mb-1">
              <FireOutlined className="text-orange-400/60" />
              引用数
            </div>
            <div className="text-slate-200 font-medium">
              {paper.citation_count}
              {paper.influential_citation_count > 0 && (
                <span className="text-slate-500 text-xs ml-1">
                  ({paper.influential_citation_count} 有影响力)
                </span>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 链接 */}
      <div className="space-y-2">
        <div className="text-slate-500 text-sm">链接</div>
        <div className="flex flex-wrap gap-2">
          {paper.url && (
            <Button 
              type="link" 
              icon={<LinkOutlined />} 
              href={paper.url} 
              target="_blank"
              className="!p-0 !text-emerald-400 hover:!text-emerald-300"
            >
              论文主页
            </Button>
          )}
          {paper.pdf_url && (
            <div className="flex items-center gap-2">
              <Button 
                type="link" 
                icon={<FileTextOutlined />} 
                href={paper.pdf_url} 
                target="_blank"
                className="!p-0 !text-emerald-400 hover:!text-emerald-300"
              >
                PDF
              </Button>
              {!paper.pdf_downloaded ? (
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  onClick={handleDownloadPdf}
                  className="!border-slate-600 !text-slate-300"
                >
                  下载
                </Button>
              ) : (
                <Tag className="!bg-green-500/10 !border-green-500/20 !text-green-400 !m-0">
                  已下载
                </Tag>
              )}
            </div>
          )}
          {paper.arxiv_id && (
            <Button 
              type="link" 
              icon={<LinkOutlined />} 
              href={`https://arxiv.org/abs/${paper.arxiv_id}`} 
              target="_blank"
              className="!p-0 !text-orange-400 hover:!text-orange-300"
            >
              arXiv
            </Button>
          )}
          {paper.doi && (
            <Button 
              type="link" 
              icon={<LinkOutlined />} 
              href={`https://doi.org/${paper.doi}`} 
              target="_blank"
              className="!p-0 !text-cyan-400 hover:!text-cyan-300"
            >
              DOI
            </Button>
          )}
        </div>
      </div>

      {/* 分割线 */}
      <div className="h-px bg-slate-700/50" />

      {/* 摘要 */}
      <div className="space-y-2">
        <div className="text-slate-500 text-sm">摘要</div>
        {paper.abstract ? (
          <div className="p-3 rounded-lg bg-slate-800/30 border border-slate-700/50">
            <p className={`text-sm text-slate-400 leading-relaxed ${!abstractExpanded ? 'line-clamp-4' : ''}`}>
              {paper.abstract}
            </p>
            {paper.abstract.length > 200 && (
              <button 
                onClick={() => setAbstractExpanded(!abstractExpanded)}
                className="text-emerald-400 text-sm mt-2 hover:text-emerald-300"
              >
                {abstractExpanded ? '收起' : '展开全部'}
              </button>
            )}
          </div>
        ) : (
          <p className="text-slate-500 text-sm">无摘要</p>
        )}
      </div>

      {/* 研究领域 */}
      {paper.fields_of_study && paper.fields_of_study.length > 0 && (
        <div className="space-y-2">
          <div className="text-slate-500 text-sm">研究领域</div>
          <div className="flex flex-wrap gap-1.5">
            {paper.fields_of_study.map((field, i) => (
              <Tag key={i} className="!bg-blue-500/10 !border-blue-500/20 !text-blue-300">
                {field}
              </Tag>
            ))}
          </div>
        </div>
      )}

      {/* 分割线 */}
      <div className="h-px bg-slate-700/50" />

      {/* 收藏夹 */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-2 text-slate-500 text-sm">
            <FolderAddOutlined />
            收藏夹
          </span>
          {availableCollections.length > 0 && (
            <Select
              placeholder="添加到..."
              size="small"
              style={{ width: 140 }}
              onChange={handleAddToCollection}
              value={undefined}
              className="[&_.ant-select-selector]:!bg-slate-800/50 [&_.ant-select-selector]:!border-slate-600"
            >
              {availableCollections.map(c => (
                <Select.Option key={c.id} value={c.id}>
                  <span className="flex items-center gap-2">
                    <span 
                      className="w-2 h-2 rounded" 
                      style={{ backgroundColor: c.color }}
                    />
                    {c.name}
                  </span>
                </Select.Option>
              ))}
            </Select>
          )}
        </div>
        
        <div className="flex flex-wrap gap-1.5">
          {addedCollections.length > 0 ? (
            addedCollections.map(c => (
              <Tag 
                key={c.id}
                closable={!c.is_default}
                onClose={() => handleRemoveFromCollection(c.id)}
                style={{ 
                  backgroundColor: `${c.color}20`,
                  borderColor: `${c.color}40`,
                  color: c.color
                }}
              >
                {c.name}
              </Tag>
            ))
          ) : (
            <span className="text-slate-500 text-sm">未添加到任何收藏夹</span>
          )}
        </div>
      </div>

      {/* 分割线 */}
      <div className="h-px bg-slate-700/50" />

      {/* 标签 */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-2 text-slate-500 text-sm">
            <TagsOutlined />
            标签
          </span>
          {!editingTags ? (
            <Button 
              type="text" 
              size="small" 
              icon={<EditOutlined />}
              onClick={() => {
                setTags(paper.tags || [])
                setEditingTags(true)
              }}
              className="!text-slate-400 hover:!text-emerald-400"
            />
          ) : (
            <Space>
              <Button 
                size="small" 
                onClick={() => setEditingTags(false)}
                className="!border-slate-600 !text-slate-300"
              >
                取消
              </Button>
              <Button type="primary" size="small" onClick={handleSaveTags}>
                保存
              </Button>
            </Space>
          )}
        </div>
        
        {editingTags ? (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-1.5">
              {tags.map(tag => (
                <Tag 
                  key={tag}
                  closable
                  onClose={() => handleRemoveTag(tag)}
                  className="!bg-slate-700/50 !border-slate-600 !text-slate-300"
                >
                  {tag}
                </Tag>
              ))}
            </div>
            <Input.Search
              placeholder="添加标签"
              value={newTag}
              onChange={e => setNewTag(e.target.value)}
              onSearch={handleAddTag}
              enterButton="添加"
              size="small"
              className="[&_.ant-input]:!bg-slate-800/50 [&_.ant-input]:!border-slate-600"
            />
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {paper.tags?.length > 0 ? (
              paper.tags.map(tag => (
                <Tag key={tag} className="!bg-slate-700/50 !border-slate-600 !text-slate-300">
                  {tag}
                </Tag>
              ))
            ) : (
              <span className="text-slate-500 text-sm">暂无标签</span>
            )}
          </div>
        )}
      </div>

      {/* 分割线 */}
      <div className="h-px bg-slate-700/50" />

      {/* 笔记 */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-2 text-slate-500 text-sm">
            <EditOutlined />
            笔记
          </span>
          {!editingNotes ? (
            <Button 
              type="text" 
              size="small" 
              icon={<EditOutlined />}
              onClick={() => {
                setNotes(paper.notes || '')
                setEditingNotes(true)
              }}
              className="!text-slate-400 hover:!text-emerald-400"
            />
          ) : (
            <Space>
              <Button 
                size="small" 
                onClick={() => setEditingNotes(false)}
                className="!border-slate-600 !text-slate-300"
              >
                取消
              </Button>
              <Button 
                type="primary" 
                size="small" 
                icon={<SaveOutlined />}
                onClick={handleSaveNotes}
              >
                保存
              </Button>
            </Space>
          )}
        </div>
        
        {editingNotes ? (
          <TextArea
            value={notes}
            onChange={e => setNotes(e.target.value)}
            rows={4}
            placeholder="添加你的笔记..."
            className="!bg-slate-800/50 !border-slate-600 !text-slate-300"
          />
        ) : (
          <div className="p-3 rounded-lg bg-slate-800/30 border border-slate-700/50 min-h-[60px]">
            {paper.notes ? (
              <p className="text-sm text-slate-400 whitespace-pre-wrap">{paper.notes}</p>
            ) : (
              <p className="text-slate-500 text-sm">暂无笔记</p>
            )}
          </div>
        )}
      </div>

      {/* 元信息 */}
      <div className="pt-2 border-t border-slate-700/50">
        <div className="text-xs text-slate-500 space-y-1">
          <div className="flex justify-between">
            <span>来源</span>
            <span className="text-slate-400">{paper.source === 'semantic_scholar' ? 'Semantic Scholar' : paper.source}</span>
          </div>
          <div className="flex justify-between">
            <span>添加时间</span>
            <span className="text-slate-400">{new Date(paper.created_at).toLocaleString('zh-CN')}</span>
          </div>
          {paper.read_at && (
            <div className="flex justify-between">
              <span>阅读时间</span>
              <span className="text-slate-400">{new Date(paper.read_at).toLocaleString('zh-CN')}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
