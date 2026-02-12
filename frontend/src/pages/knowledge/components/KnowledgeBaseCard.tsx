import { Card, Button, Dropdown, Tag } from 'antd'
import { DeleteOutlined, MoreOutlined, DatabaseOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import type { KnowledgeBase } from '@/services/api'
import dayjs from 'dayjs'

interface KnowledgeBaseCardProps {
  kb: KnowledgeBase
  onClick: () => void
  onDelete: () => void
}

/** 知识库卡片组件 */
const KnowledgeBaseCard = ({ kb, onClick, onDelete }: KnowledgeBaseCardProps) => {
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

        {/* 嵌入模型标签 */}
        <div className="mt-3 pt-3 border-t border-slate-700/50 flex items-center gap-2">
          <Tag color="geekblue" className="text-xs" style={{ margin: 0 }}>
            {kb.embedding_model?.split('/').pop() || 'bge-m3'}
          </Tag>
          <span className="text-slate-600 text-xs">{kb.embedding_dimension || 1024}维</span>
        </div>
      </Card>
    </motion.div>
  )
}

export default KnowledgeBaseCard
