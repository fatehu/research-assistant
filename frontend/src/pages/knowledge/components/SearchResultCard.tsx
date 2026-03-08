import { useState } from 'react'
import { Card, Tag, Button, Typography } from 'antd'
import { ExpandAltOutlined, InfoCircleOutlined, NodeIndexOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import type { SearchResult } from '@/services/api'
import { CHUNK_LEVEL_CONFIG, SECTION_TYPE_COLORS } from '../utils'

const { Text, Paragraph } = Typography

interface SearchResultCardProps {
  result: SearchResult
  index: number
}

interface AdjacentContextItem {
  chunkId: number
  chunkIndex: number
  relativeOffset: number
  chunkLevel: string | undefined
  sectionTitle: string | undefined
  content: string
}

const asRecord = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {}
  }
  return value as Record<string, unknown>
}

const asBool = (value: unknown): boolean | null => {
  if (typeof value === 'boolean') {
    return value
  }
  return null
}

const asString = (value: unknown): string | null => {
  if (typeof value !== 'string') {
    return null
  }
  const clean = value.trim()
  return clean ? clean : null
}

const asNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  return null
}

const boolText = (value: boolean | null): string => {
  if (value === null) {
    return '-'
  }
  return value ? '是' : '否'
}

const formatOffset = (value: number): string => {
  if (value > 0) {
    return `+${value}`
  }
  return String(value)
}

/** 搜索结果卡片（增强版：含层级标签 + 章节标题 + 父级上下文） */
const SearchResultCard = ({ result, index }: SearchResultCardProps) => {
  const [showMeta, setShowMeta] = useState(false)
  const [showAdjacent, setShowAdjacent] = useState(false)
  const [showParent, setShowParent] = useState(false)
  const levelConfig = result.chunk_level ? CHUNK_LEVEL_CONFIG[result.chunk_level] : null
  const metadata = asRecord(result.metadata)

  const retrievalDimension = asNumber(metadata.retrieval_dimension)
  const queryRewriteCacheHit = asBool(metadata.query_rewrite_cache_hit)
  const queryRewriteSkipReason = asString(metadata.query_rewrite_skip_reason)
  const queryRewriteLlmCalled = asBool(metadata.query_rewrite_llm_called)
  const compressionEnabled = asBool(metadata.contextual_compression_enabled)
  const compressionFallback = asString(metadata.contextual_compression_fallback)
  const fallbackRetryUsed = asBool(metadata.fallback_retry_used)
  const fallbackRetryReason = asString(metadata.fallback_retry_reason)

  const adjacentContext: AdjacentContextItem[] = Array.isArray(metadata.adjacent_context)
    ? metadata.adjacent_context
      .map((item) => {
        const row = asRecord(item)
        const chunkId = asNumber(row.chunk_id)
        const chunkIndex = asNumber(row.chunk_index)
        const relativeOffset = asNumber(row.relative_offset)
        const content = asString(row.content) || ''
        if (chunkId === null || chunkIndex === null || relativeOffset === null || !content) {
          return null
        }
        return {
          chunkId,
          chunkIndex,
          relativeOffset,
          chunkLevel: asString(row.chunk_level) || undefined,
          sectionTitle: asString(row.section_title) || undefined,
          content,
        }
      })
      .filter((item): item is NonNullable<typeof item> => item !== null)
    : []

  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.05 }}
    >
      <Card className="bg-slate-800/50 border-slate-700/50 mb-3" styles={{ body: { padding: '16px' } }}>
        <div className="flex items-start gap-3">
          <div className="flex-shrink-0">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500/20 to-indigo-500/20 flex items-center justify-center">
              <span className="text-blue-400 font-bold">{(result.score * 100).toFixed(0)}%</span>
            </div>
          </div>
          <div className="flex-1 min-w-0">
            {/* 标签行 */}
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <Tag color="blue" className="text-xs">{result.knowledge_base_name}</Tag>
              <span className="text-slate-500 text-xs">{result.document_name}</span>
              <span className="text-slate-600 text-xs">#{result.chunk_index + 1}</span>
              {levelConfig && (
                <Tag color={levelConfig.color} className="text-xs" style={{ margin: 0 }}>
                  {levelConfig.label}
                </Tag>
              )}
              {result.section_type && (
                <Tag
                  color={SECTION_TYPE_COLORS[result.section_type] || 'default'}
                  className="text-xs"
                  style={{ margin: 0 }}
                >
                  {result.section_type}
                </Tag>
              )}
            </div>
            {/* 章节标题 */}
            {result.section_title && (
              <div className="mb-1.5">
                <Text className="text-slate-400 text-xs">
                  <NodeIndexOutlined className="mr-1" />
                  {result.section_title}
                </Text>
              </div>
            )}
            {/* 内容 */}
            <Paragraph
              className="text-slate-300 mb-0"
              ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
            >
              {result.content}
            </Paragraph>

            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button
                type="link"
                size="small"
                icon={<InfoCircleOutlined />}
                onClick={() => setShowMeta(!showMeta)}
                className="text-slate-500 hover:text-emerald-400 px-0 text-xs"
              >
                {showMeta ? '收起检索元信息' : '查看检索元信息'}
              </Button>

              {adjacentContext.length > 0 && (
                <Button
                  type="link"
                  size="small"
                  icon={<ExpandAltOutlined />}
                  onClick={() => setShowAdjacent(!showAdjacent)}
                  className="text-slate-500 hover:text-teal-400 px-0 text-xs"
                >
                  {showAdjacent ? '收起相邻上下文' : `查看相邻上下文（${adjacentContext.length}）`}
                </Button>
              )}

              {result.parent_context && (
                <Button
                  type="link"
                  size="small"
                  icon={<ExpandAltOutlined />}
                  onClick={() => setShowParent(!showParent)}
                  className="text-slate-500 hover:text-blue-400 px-0 text-xs"
                >
                  {showParent ? '收起父级上下文' : '查看父级上下文'}
                </Button>
              )}
            </div>

            {showMeta && (
              <div className="mt-2">
                <div className="mt-1.5 p-2.5 rounded bg-slate-900/60 border border-slate-700/50">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1">
                    <Text className="text-slate-400 text-xs">检索维度: {retrievalDimension ?? '-'}</Text>
                    <Text className="text-slate-400 text-xs">改写缓存命中: {boolText(queryRewriteCacheHit)}</Text>
                    <Text className="text-slate-400 text-xs">改写跳过原因: {queryRewriteSkipReason || '-'}</Text>
                    <Text className="text-slate-400 text-xs">改写调用LLM: {boolText(queryRewriteLlmCalled)}</Text>
                    <Text className="text-slate-400 text-xs">启用压缩: {boolText(compressionEnabled)}</Text>
                    <Text className="text-slate-400 text-xs">压缩回退原因: {compressionFallback || '-'}</Text>
                    <Text className="text-slate-400 text-xs">降级重试: {boolText(fallbackRetryUsed)}</Text>
                    <Text className="text-slate-400 text-xs">降级原因: {fallbackRetryReason || '-'}</Text>
                  </div>
                </div>
              </div>
            )}

            {showAdjacent && adjacentContext.length > 0 && (
              <div className="mt-2">
                <div className="mt-1.5 p-2.5 rounded bg-slate-900/60 border border-slate-700/50">
                  {adjacentContext.map((item) => {
                    const adjLevelConfig = item.chunkLevel
                      ? CHUNK_LEVEL_CONFIG[item.chunkLevel as keyof typeof CHUNK_LEVEL_CONFIG]
                      : null
                    return (
                      <div key={`${item.chunkId}-${item.relativeOffset}`} className="mb-3 last:mb-0">
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <Tag color={item.relativeOffset < 0 ? 'geekblue' : 'green'} className="text-xs m-0">
                            偏移 {formatOffset(item.relativeOffset)}
                          </Tag>
                          <Tag color="default" className="text-xs m-0">
                            #{item.chunkIndex + 1}
                          </Tag>
                          {adjLevelConfig && (
                            <Tag color={adjLevelConfig.color} className="text-xs m-0">
                              {adjLevelConfig.label}
                            </Tag>
                          )}
                        </div>
                        {item.sectionTitle && (
                          <Text className="text-slate-500 text-xs block mb-1">{item.sectionTitle}</Text>
                        )}
                        <Text className="text-slate-400 text-xs leading-relaxed">
                          {item.content}
                        </Text>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* 父级上下文 */}
            {result.parent_context && showParent && (
              <div className="mt-2">
                <div className="mt-1.5 p-2.5 rounded bg-slate-900/60 border border-slate-700/50">
                  {result.parent_context.startsWith('📌') ? (
                    <>
                      <Text className="text-blue-400 text-xs font-medium block mb-1">
                        {result.parent_context.split('\n')[0]}
                      </Text>
                      {result.parent_context.includes('\n') && (
                        <Text className="text-slate-500 text-xs leading-relaxed">
                          {result.parent_context.split('\n').slice(1).join('\n')}
                        </Text>
                      )}
                    </>
                  ) : (
                    <Text className="text-slate-400 text-xs leading-relaxed">
                      {result.parent_context}
                    </Text>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </Card>
    </motion.div>
  )
}

export default SearchResultCard
