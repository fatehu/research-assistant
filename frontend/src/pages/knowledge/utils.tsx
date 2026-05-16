import { Tag } from 'antd'
import {
  FilePdfOutlined,
  FileMarkdownOutlined,
  FileTextOutlined,
  FileOutlined,
  ClockCircleOutlined,
  LoadingOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'

/** 文件类型图标映射 */
export const getFileIcon = (fileType: string) => {
  switch (fileType.toLowerCase()) {
    case 'pdf':
      return <FilePdfOutlined className="text-red-400" />
    case 'md':
    case 'markdown':
      return <FileMarkdownOutlined className="text-blue-400" />
    case 'txt':
      return <FileTextOutlined className="text-slate-400" />
    default:
      return <FileOutlined className="text-slate-400" />
  }
}

/** 状态标签映射 */
export const getStatusTag = (status: string, stageLabel?: string) => {
  const runningLabel = String(stageLabel || '').trim() || '处理中'
  const pendingLabel = String(stageLabel || '').trim() || '等待处理'
  switch (status) {
    case 'pending':
      return <Tag icon={<ClockCircleOutlined />} color="default">{pendingLabel}</Tag>
    case 'processing':
    case 'running':
      return <Tag icon={<LoadingOutlined spin />} color="processing">{runningLabel}</Tag>
    case 'ready':
      return <Tag icon={<CheckCircleOutlined />} color="success">已就绪</Tag>
    case 'completed':
      return <Tag icon={<CheckCircleOutlined />} color="success">已完成</Tag>
    case 'timeout':
      return <Tag icon={<ExclamationCircleOutlined />} color="warning">超时</Tag>
    case 'cancelled':
      return <Tag icon={<ExclamationCircleOutlined />} color="default">已取消</Tag>
    case 'failed':
      return <Tag icon={<ExclamationCircleOutlined />} color="error">失败</Tag>
    default:
      return <Tag color="default">{status}</Tag>
  }
}

/** 格式化文件大小 */
export const formatFileSize = (bytes: number) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

/** 分块层级配置 */
export const CHUNK_LEVEL_CONFIG: Record<string, { color: string; label: string }> = {
  paragraph: { color: 'blue', label: '段落' },
  section: { color: 'orange', label: '章节' },
  document: { color: 'purple', label: '文档' },
}

/** 章节类型标签颜色映射 */
export const SECTION_TYPE_COLORS: Record<string, string> = {
  abstract: 'gold',
  introduction: 'cyan',
  methodology: 'geekblue',
  results: 'green',
  discussion: 'lime',
  conclusion: 'purple',
  references: 'red',
}

/** 共享知识库类型 */
export interface SharedKnowledgeBase {
  id: number
  name: string
  description?: string
  document_count: number
  total_chunks: number
  owner_id: number
  owner_name: string
}
