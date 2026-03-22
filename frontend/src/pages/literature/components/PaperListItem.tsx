import { Button, Tag, Tooltip, Rate } from 'antd'
import { DeleteOutlined, CheckOutlined, FileTextOutlined } from '@ant-design/icons'
import type { Paper } from '@/services/api'
import type { SourceInfo } from '../constants'

interface PaperListItemProps {
  paper: Paper
  index: number
  selected?: boolean
  sourceInfo: SourceInfo
  onSelect: (paper: Paper) => void
  onDelete: (id: number) => void
}

/** 论文列表视图项 - 用于文献库 Tab 的 list 视图 */
const PaperListItem = ({
  paper,
  index,
  selected = false,
  sourceInfo,
  onSelect,
  onDelete,
}: PaperListItemProps) => (
  <div
    className={`group flex cursor-pointer items-center gap-4 rounded-[22px] border px-4 py-3 transition-all duration-200 ${
      selected
        ? 'border-emerald-400/20 bg-slate-800/70 shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_18px_32px_rgba(2,6,23,0.24)]'
        : 'border-transparent bg-transparent hover:border-white/[0.06] hover:bg-white/[0.04]'
    }`}
    style={{ animationDelay: `${index * 30}ms` }}
    onClick={() => onSelect(paper)}
  >
    {/* 状态图标 */}
    <div
      className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl border ${
        selected ? 'border-emerald-400/20 bg-emerald-400/12' : 'border-white/5 bg-white/[0.04]'
      }`}
    >
      {paper.is_read ? (
        <CheckOutlined className="text-emerald-400" />
      ) : (
        <FileTextOutlined className="text-slate-500" />
      )}
    </div>

    {/* 主要信息 */}
    <div className="flex-1 min-w-0">
      <div className="flex items-center gap-2">
        <span className={`truncate font-medium transition-colors ${selected ? 'text-emerald-200' : 'text-slate-200 group-hover:text-emerald-300'}`}>
          {paper.title}
        </span>
        {(paper.rating ?? 0) > 0 && <Rate disabled value={paper.rating ?? 0} className="text-xs !text-yellow-400" />}
      </div>
      <div className="flex items-center gap-3 text-sm text-slate-500">
        <span>{paper.authors?.slice(0, 2).map(a => a.name).join(', ')}</span>
        {paper.year && <span>{paper.year}</span>}
        {paper.citation_count > 0 && <span>{paper.citation_count} 引用</span>}
      </div>
    </div>

    {/* 来源标签 */}
    <Tag className="!m-0 !border-white/10 !bg-white/[0.06] text-xs !text-slate-300">
      {sourceInfo.icon}
    </Tag>

    {/* 操作 */}
    <div
      className={`flex gap-1 transition-opacity ${selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`}
      onClick={(event) => event.stopPropagation()}
    >
      <Tooltip title="删除">
        <Button
          type="text"
          size="small"
          danger
          icon={<DeleteOutlined />}
          className="hover:!bg-white/[0.06]"
          onClick={() => onDelete(paper.id)}
        />
      </Tooltip>
    </div>
  </div>
)

export default PaperListItem
