import { Button, Tag, Tooltip, Rate } from 'antd'
import { DeleteOutlined, CheckOutlined, FireOutlined, FileTextOutlined } from '@ant-design/icons'
import type { Paper } from '@/services/api'
import type { SourceInfo } from '../constants'

interface PaperListItemProps {
  paper: Paper
  index: number
  sourceInfo: SourceInfo
  onSelect: (paper: Paper) => void
  onDelete: (id: number) => void
}

/** 论文列表视图项 - 用于文献库 Tab 的 list 视图 */
const PaperListItem = ({ paper, index, sourceInfo, onSelect, onDelete }: PaperListItemProps) => (
  <div
    className="flex items-center gap-4 px-4 py-3 border-b border-slate-700/50 hover:bg-slate-800/30 cursor-pointer transition-colors group"
    style={{ animationDelay: `${index * 30}ms` }}
    onClick={() => onSelect(paper)}
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
        {paper.rating > 0 && <Rate disabled value={paper.rating} className="text-xs !text-yellow-400" />}
      </div>
      <div className="flex items-center gap-3 text-sm text-slate-500">
        <span>{paper.authors?.slice(0, 2).map(a => a.name).join(', ')}</span>
        {paper.year && <span>{paper.year}</span>}
        {paper.citation_count > 0 && <span>{paper.citation_count} 引用</span>}
      </div>
    </div>

    {/* 来源标签 */}
    <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs !m-0">
      {sourceInfo.icon}
    </Tag>

    {/* 操作 */}
    <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity" onClick={e => e.stopPropagation()}>
      <Tooltip title="删除">
        <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => onDelete(paper.id)} />
      </Tooltip>
    </div>
  </div>
)

export default PaperListItem
