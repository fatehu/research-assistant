import { Button, Tag, Tooltip } from 'antd'
import {
  PlusOutlined, DownloadOutlined, LinkOutlined,
  CheckOutlined, FireOutlined
} from '@ant-design/icons'
import type { PaperSearchResult } from '@/services/api'
import type { SourceInfo } from '../constants'

interface SearchResultListItemProps {
  paper: PaperSearchResult
  index: number
  sourceInfo: SourceInfo
  onSave: (paper: PaperSearchResult) => void
  savePending?: boolean
}

/** 搜索结果列表视图项 */
const SearchResultListItem = ({ paper, index, sourceInfo, onSave, savePending = false }: SearchResultListItemProps) => (
  <div
    className="group flex items-center gap-4 rounded-[22px] border border-transparent px-4 py-3 transition-all duration-200 hover:border-white/[0.06] hover:bg-white/[0.04]"
    style={{ animationDelay: `${index * 30}ms` }}
  >
    {/* 来源图标 */}
    <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl border border-white/5 bg-white/[0.04] text-lg">
      {sourceInfo.icon}
    </div>

    {/* 主要信息 */}
    <div className="flex-1 min-w-0">
      <div className="flex items-center gap-2 mb-1">
        <a
          href={paper.url}
          target="_blank"
          rel="noopener noreferrer"
          className="font-medium text-slate-200 truncate hover:text-emerald-400 transition-colors"
        >
          {paper.title}
        </a>
      </div>
      <div className="flex items-center gap-3 text-sm text-slate-500">
        <span className="truncate max-w-[200px]">
          {paper.authors?.slice(0, 2).map(a => a.name).join(', ')}
          {paper.authors?.length > 2 && ' 等'}
        </span>
        {paper.year && <span>{paper.year}</span>}
        {paper.venue && <span className="truncate max-w-[150px]">{paper.venue}</span>}
        {paper.citation_count > 0 && (
          <span className="flex items-center gap-1">
            <FireOutlined className="text-orange-400/60" />
            {paper.citation_count}
          </span>
        )}
      </div>
    </div>

    {/* 标签 */}
    <div className="flex gap-1 flex-shrink-0">
      {paper.doi && (
        <Tag className="!bg-cyan-500/10 !border-cyan-500/20 !text-cyan-300 text-xs !m-0">DOI</Tag>
      )}
      {paper.pdf_url && (
        <Tag className="!bg-emerald-500/10 !border-emerald-500/20 !text-emerald-300 text-xs !m-0">PDF</Tag>
      )}
    </div>

    {/* 操作按钮 */}
    <div className="flex gap-1 flex-shrink-0">
      {paper.is_saved ? (
        <Tag className="!bg-emerald-500/20 !border-emerald-500/30 !text-emerald-400 !m-0">
          <CheckOutlined /> 已保存
        </Tag>
      ) : (
        <Button
          size="small"
          icon={<PlusOutlined />}
          onClick={() => onSave(paper)}
          loading={savePending}
          disabled={savePending}
          className="!border-emerald-400/20 !bg-emerald-400/12 !text-emerald-200 hover:!border-emerald-300/30 hover:!bg-emerald-400/18"
        >
          {savePending ? '保存中...' : '保存'}
        </Button>
      )}
      {paper.pdf_url && (
        <Tooltip title="下载 PDF">
          <Button size="small" icon={<DownloadOutlined />} href={paper.pdf_url} target="_blank" className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50" />
        </Tooltip>
      )}
      {paper.url && (
        <Tooltip title="打开链接">
          <Button size="small" icon={<LinkOutlined />} href={paper.url} target="_blank" className="!border-slate-600 !text-slate-300 hover:!border-emerald-500/50" />
        </Tooltip>
      )}
    </div>
  </div>
)

export default SearchResultListItem
