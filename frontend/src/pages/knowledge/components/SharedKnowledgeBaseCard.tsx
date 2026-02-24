import { Card, Tag } from 'antd'
import { ShareAltOutlined, UserOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import type { SharedKnowledgeBase } from '../utils'

interface SharedKnowledgeBaseCardProps {
  kb: SharedKnowledgeBase
  onClick: () => void
}

/** 共享知识库卡片组件（只读） */
const SharedKnowledgeBaseCard = ({ kb, onClick }: SharedKnowledgeBaseCardProps) => {
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
        styles={{ body: { padding: '20px' } }}
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

export default SharedKnowledgeBaseCard
