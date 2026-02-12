import { Button, Tag, Dropdown, Rate, message } from 'antd'
import {
  DeleteOutlined, EyeOutlined, DownloadOutlined,
  MoreOutlined, CheckOutlined, FireOutlined,
  CalendarOutlined, TeamOutlined, BookOutlined, FileTextOutlined
} from '@ant-design/icons'
import type { Paper } from '@/services/api'
import type { SourceInfo } from '../constants'

interface PaperCardProps {
  paper: Paper
  index: number
  sourceInfo: SourceInfo
  onSelect: (paper: Paper) => void
  onDelete: (id: number) => void
  onDownloadPdf: (id: number) => Promise<void>
}

/** 论文卡片视图 - 用于文献库 Tab 的 card 视图 */
const PaperCard = ({ paper, index, sourceInfo, onSelect, onDelete, onDownloadPdf }: PaperCardProps) => (
  <div
    className="glass-card p-4 cursor-pointer hover:border-emerald-500/30 hover:shadow-lg hover:shadow-emerald-500/5 transition-all duration-300 group"
    style={{ animationDelay: `${index * 50}ms` }}
    onClick={() => onSelect(paper)}
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
              <Tag className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400 !m-0" icon={<CheckOutlined />}>已读</Tag>
            )}
            {paper.rating > 0 && <Rate disabled value={paper.rating} className="text-sm !text-yellow-400" />}
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
            <span className="flex items-center gap-1"><CalendarOutlined className="text-blue-400/60" />{paper.year}</span>
          )}
          {paper.venue && (
            <span className="flex items-center gap-1 truncate max-w-[200px]"><BookOutlined className="text-purple-400/60" />{paper.venue}</span>
          )}
          {paper.citation_count > 0 && (
            <span className="flex items-center gap-1"><FireOutlined className="text-orange-400/60" />{paper.citation_count} 引用</span>
          )}
        </div>

        {/* 标签 */}
        <div className="flex flex-wrap gap-1.5">
          {paper.tags?.map((tag, i) => (
            <Tag key={i} className="!bg-slate-500/10 !border-slate-500/20 !text-slate-300 text-xs">{tag}</Tag>
          ))}
          <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs">{sourceInfo.icon} {sourceInfo.name}</Tag>
          {paper.pdf_downloaded && (
            <Tag className="!bg-green-500/10 !border-green-500/20 !text-green-400 text-xs" icon={<FileTextOutlined />}>PDF</Tag>
          )}
        </div>
      </div>

      {/* 操作菜单 */}
      <div className="flex flex-col items-end" onClick={e => e.stopPropagation()}>
        <Dropdown
          menu={{
            items: [
              { key: 'view', icon: <EyeOutlined />, label: '查看详情', onClick: () => onSelect(paper) },
              {
                key: 'download', icon: <DownloadOutlined />, label: '下载 PDF',
                onClick: async () => {
                  try { await onDownloadPdf(paper.id); message.success('PDF 下载成功') }
                  catch { message.error('下载失败') }
                },
                disabled: !paper.pdf_url || paper.pdf_downloaded,
              },
              { type: 'divider' as const },
              { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true, onClick: () => onDelete(paper.id) },
            ],
          }}
          trigger={['click']}
        >
          <Button type="text" icon={<MoreOutlined />} className="!text-slate-400 hover:!text-emerald-400 opacity-0 group-hover:opacity-100 transition-opacity" />
        </Dropdown>
      </div>
    </div>
  </div>
)

export default PaperCard
