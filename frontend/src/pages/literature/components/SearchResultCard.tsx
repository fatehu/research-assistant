import { Button, Tag } from 'antd'
import {
  PlusOutlined, DownloadOutlined, LinkOutlined, CheckOutlined,
  FireOutlined, CalendarOutlined, TeamOutlined, BookOutlined
} from '@ant-design/icons'
import type { PaperSearchResult } from '@/services/api'
import type { SourceInfo } from '../constants'

interface SearchResultCardProps {
  paper: PaperSearchResult
  index: number
  sourceInfo: SourceInfo
  onSave: (paper: PaperSearchResult) => void
  savePending?: boolean
}

/** 搜索结果卡片视图 */
const SearchResultCard = ({ paper, index, sourceInfo, onSave, savePending = false }: SearchResultCardProps) => (
  <div
    className="glass-card p-4 mb-3 hover:border-emerald-500/30 transition-all duration-300"
    style={{ animationDelay: `${index * 50}ms` }}
  >
    <div className="flex justify-between gap-4">
      <div className="flex-1 min-w-0">
        {/* 标题 */}
        <h4 className="font-semibold text-base mb-2 text-slate-100 leading-snug">
          <a
            href={paper.url}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-emerald-400 transition-colors line-clamp-2"
          >
            {paper.title}
          </a>
        </h4>

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

        {/* 摘要 */}
        {paper.abstract && (
          <p className="text-slate-400 text-sm line-clamp-2 mb-3 leading-relaxed">{paper.abstract}</p>
        )}

        {/* 标签 */}
        <div className="flex flex-wrap gap-1.5">
          {paper.fields_of_study?.slice(0, 3).map((field, i) => (
            <Tag key={i} className="!bg-blue-500/10 !border-blue-500/20 !text-blue-300 text-xs">{field}</Tag>
          ))}
          <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs">
            {sourceInfo.icon} {sourceInfo.name}
          </Tag>
          {paper.doi && (
            <Tag className="!bg-cyan-500/10 !border-cyan-500/20 !text-cyan-300 text-xs">DOI</Tag>
          )}
        </div>
      </div>

      {/* 操作按钮 */}
      <div className="flex flex-col gap-2 flex-shrink-0">
        {paper.is_saved ? (
          <Button className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400" icon={<CheckOutlined />} disabled>
            已保存
          </Button>
        ) : (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => onSave(paper)} loading={savePending} disabled={savePending}>
            {savePending ? '保存中...' : '保存'}
          </Button>
        )}
        {paper.pdf_url && (
          <Button
            icon={<DownloadOutlined />}
            href={paper.pdf_url}
            target="_blank"
            className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50 hover:!text-emerald-400"
          >
            PDF
          </Button>
        )}
        {paper.url && (
          <Button
            icon={<LinkOutlined />}
            href={paper.url}
            target="_blank"
            className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50 hover:!text-emerald-400"
          >
            链接
          </Button>
        )}
      </div>
    </div>
  </div>
)

export default SearchResultCard
