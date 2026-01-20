import { useState } from 'react'
import { 
  Button, Tag, Space, Divider, Rate, Input, message, Tooltip, 
  Popconfirm, Select, Descriptions, Typography, Collapse 
} from 'antd'
import {
  LinkOutlined, DownloadOutlined, BookOutlined, CalendarOutlined,
  TeamOutlined, StarOutlined, EditOutlined, SaveOutlined,
  NodeIndexOutlined, CopyOutlined, CheckOutlined, FileTextOutlined,
  FolderAddOutlined, DeleteOutlined, EyeOutlined, TagsOutlined
} from '@ant-design/icons'
import { Paper, PaperCollection } from '@/services/api'
import { useLiteratureStore } from '@/stores/literatureStore'

const { TextArea } = Input
const { Text, Paragraph, Title } = Typography
const { Panel } = Collapse

interface PaperDetailPanelProps {
  paper: Paper
  onShowGraph?: () => void
}

export default function PaperDetailPanel({ paper, onShowGraph }: PaperDetailPanelProps) {
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
    <div className="space-y-4">
      {/* 标题和基本操作 */}
      <div>
        <Title level={5} className="mb-2">{paper.title}</Title>
        
        <Space wrap className="mb-3">
          <Button 
            type={paper.is_read ? 'default' : 'primary'}
            icon={paper.is_read ? <CheckOutlined /> : <EyeOutlined />}
            onClick={handleToggleRead}
            size="small"
          >
            {paper.is_read ? '已读' : '标记已读'}
          </Button>
          
          {paper.semantic_scholar_id && (
            <Button
              icon={<NodeIndexOutlined />}
              onClick={onShowGraph}
              size="small"
            >
              引用图谱
            </Button>
          )}
          
          <Button
            icon={<CopyOutlined />}
            onClick={handleCopyCitation}
            size="small"
          >
            复制引用
          </Button>
        </Space>

        <div className="flex items-center gap-2">
          <span className="text-gray-500">评分:</span>
          <Rate 
            value={paper.rating || 0} 
            onChange={handleRatingChange}
            allowClear
          />
        </div>
      </div>

      <Divider className="my-3" />

      {/* 作者信息 */}
      <div>
        <div className="text-gray-500 text-sm mb-1">
          <TeamOutlined className="mr-1" />
          作者
        </div>
        <div className="text-sm">
          {paper.authors?.map((author, i) => (
            <span key={i}>
              {author.name}
              {i < paper.authors.length - 1 && ', '}
            </span>
          )) || '未知'}
        </div>
      </div>

      {/* 发表信息 */}
      <Descriptions column={1} size="small">
        {paper.year && (
          <Descriptions.Item label={<><CalendarOutlined /> 年份</>}>
            {paper.year}
          </Descriptions.Item>
        )}
        {paper.venue && (
          <Descriptions.Item label={<><BookOutlined /> 发表</>}>
            {paper.venue}
          </Descriptions.Item>
        )}
        {paper.citation_count > 0 && (
          <Descriptions.Item label={<><StarOutlined /> 引用数</>}>
            {paper.citation_count}
            {paper.influential_citation_count > 0 && (
              <span className="text-gray-400 ml-2">
                (有影响力: {paper.influential_citation_count})
              </span>
            )}
          </Descriptions.Item>
        )}
      </Descriptions>

      {/* 链接 */}
      <div className="space-y-2">
        {paper.url && (
          <Button 
            type="link" 
            icon={<LinkOutlined />} 
            href={paper.url} 
            target="_blank"
            className="p-0"
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
              className="p-0"
            >
              PDF 链接
            </Button>
            {!paper.pdf_downloaded && (
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={handleDownloadPdf}
              >
                下载
              </Button>
            )}
            {paper.pdf_downloaded && (
              <Tag color="success">已下载</Tag>
            )}
          </div>
        )}
        {paper.arxiv_id && (
          <Button 
            type="link" 
            icon={<LinkOutlined />} 
            href={`https://arxiv.org/abs/${paper.arxiv_id}`} 
            target="_blank"
            className="p-0"
          >
            arXiv: {paper.arxiv_id}
          </Button>
        )}
        {paper.doi && (
          <Button 
            type="link" 
            icon={<LinkOutlined />} 
            href={`https://doi.org/${paper.doi}`} 
            target="_blank"
            className="p-0"
          >
            DOI: {paper.doi}
          </Button>
        )}
      </div>

      <Divider className="my-3" />

      {/* 摘要 */}
      <Collapse defaultActiveKey={['abstract']} ghost>
        <Panel header="摘要" key="abstract">
          {paper.abstract ? (
            <Paragraph 
              ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}
              className="text-sm text-gray-600"
            >
              {paper.abstract}
            </Paragraph>
          ) : (
            <Text type="secondary">无摘要</Text>
          )}
        </Panel>
      </Collapse>

      {/* 研究领域 */}
      {paper.fields_of_study && paper.fields_of_study.length > 0 && (
        <div>
          <div className="text-gray-500 text-sm mb-2">研究领域</div>
          <Space wrap>
            {paper.fields_of_study.map((field, i) => (
              <Tag key={i} color="blue">{field}</Tag>
            ))}
          </Space>
        </div>
      )}

      <Divider className="my-3" />

      {/* 收藏夹 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-gray-500 text-sm">
            <FolderAddOutlined className="mr-1" />
            收藏夹
          </span>
          {availableCollections.length > 0 && (
            <Select
              placeholder="添加到..."
              size="small"
              style={{ width: 120 }}
              onChange={handleAddToCollection}
              value={undefined}
            >
              {availableCollections.map(c => (
                <Select.Option key={c.id} value={c.id}>
                  <span 
                    className="inline-block w-2 h-2 rounded mr-1" 
                    style={{ backgroundColor: c.color }}
                  />
                  {c.name}
                </Select.Option>
              ))}
            </Select>
          )}
        </div>
        
        <Space wrap>
          {addedCollections.map(c => (
            <Tag 
              key={c.id}
              closable={!c.is_default}
              onClose={() => handleRemoveFromCollection(c.id)}
              color={c.color}
            >
              {c.name}
            </Tag>
          ))}
        </Space>
      </div>

      <Divider className="my-3" />

      {/* 标签 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-gray-500 text-sm">
            <TagsOutlined className="mr-1" />
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
            />
          ) : (
            <Space>
              <Button size="small" onClick={() => setEditingTags(false)}>取消</Button>
              <Button type="primary" size="small" onClick={handleSaveTags}>保存</Button>
            </Space>
          )}
        </div>
        
        {editingTags ? (
          <div>
            <Space wrap className="mb-2">
              {tags.map(tag => (
                <Tag 
                  key={tag}
                  closable
                  onClose={() => handleRemoveTag(tag)}
                >
                  {tag}
                </Tag>
              ))}
            </Space>
            <Input.Search
              placeholder="添加标签"
              value={newTag}
              onChange={e => setNewTag(e.target.value)}
              onSearch={handleAddTag}
              enterButton="添加"
              size="small"
            />
          </div>
        ) : (
          <Space wrap>
            {paper.tags?.length > 0 ? (
              paper.tags.map(tag => <Tag key={tag}>{tag}</Tag>)
            ) : (
              <Text type="secondary" className="text-sm">暂无标签</Text>
            )}
          </Space>
        )}
      </div>

      <Divider className="my-3" />

      {/* 笔记 */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-gray-500 text-sm">
            <EditOutlined className="mr-1" />
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
            />
          ) : (
            <Space>
              <Button size="small" onClick={() => setEditingNotes(false)}>取消</Button>
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
          />
        ) : (
          <div className="text-sm text-gray-600 bg-gray-50 p-3 rounded">
            {paper.notes || <Text type="secondary">暂无笔记</Text>}
          </div>
        )}
      </div>

      {/* 元信息 */}
      <Divider className="my-3" />
      
      <div className="text-xs text-gray-400">
        <div>来源: {paper.source === 'semantic_scholar' ? 'Semantic Scholar' : paper.source}</div>
        <div>添加时间: {new Date(paper.created_at).toLocaleString()}</div>
        {paper.read_at && (
          <div>阅读时间: {new Date(paper.read_at).toLocaleString()}</div>
        )}
      </div>
    </div>
  )
}
