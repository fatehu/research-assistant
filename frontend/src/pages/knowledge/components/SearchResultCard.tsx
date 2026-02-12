import { useState } from 'react'
import { Card, Tag, Button, Typography } from 'antd'
import { ExpandAltOutlined, NodeIndexOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import type { SearchResult } from '@/services/api'
import { CHUNK_LEVEL_CONFIG, SECTION_TYPE_COLORS } from '../utils'

const { Text, Paragraph } = Typography

interface SearchResultCardProps {
  result: SearchResult
  index: number
}

/** 搜索结果卡片（增强版：含层级标签 + 章节标题 + 父级上下文） */
const SearchResultCard = ({ result, index }: SearchResultCardProps) => {
  const [showParent, setShowParent] = useState(false)
  const levelConfig = result.chunk_level ? CHUNK_LEVEL_CONFIG[result.chunk_level] : null

  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.05 }}
    >
      <Card className="bg-slate-800/50 border-slate-700/50 mb-3" bodyStyle={{ padding: '16px' }}>
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
            {/* 父级上下文 */}
            {result.parent_context && (
              <div className="mt-2">
                <Button
                  type="link"
                  size="small"
                  icon={<ExpandAltOutlined />}
                  onClick={() => setShowParent(!showParent)}
                  className="text-slate-500 hover:text-blue-400 px-0 text-xs"
                >
                  {showParent ? '收起上下文' : '查看父级上下文'}
                </Button>
                {showParent && (
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
                )}
              </div>
            )}
          </div>
        </div>
      </Card>
    </motion.div>
  )
}

export default SearchResultCard
