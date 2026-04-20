import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Drawer,
  Empty,
  List,
  Popconfirm,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  FileTextOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { codelabApi, NotebookWorkspace, NotebookWorkspaceFile } from '@/services/api'
import { handleApiError } from '@/utils/apiErrorHandler'

interface NotebookFilesDrawerProps {
  notebookId: string
  open: boolean
  onClose: () => void
}

const formatFileSize = (sizeBytes: number) => {
  if (sizeBytes < 1024) return `${sizeBytes} B`
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`
}

const getUsageHint = (file?: NotebookWorkspaceFile | null) => {
  if (!file) {
    return [
      "list_uploaded_files()",
      "read_uploaded_text('notes.txt')",
      "uploaded_file_path('data.csv')",
    ]
  }

  const name = file.name
  const extension = String(file.extension || '').toLowerCase()
  if (extension === '.csv') return [`pd.read_csv('${name}')`, `pd.read_csv(uploaded_file_path('${name}')).head()`]
  if (extension === '.xlsx' || extension === '.xls') return [`pd.read_excel('${name}')`, `pd.read_excel(uploaded_file_path('${name}')).head()`]
  if (extension === '.json') return [`read_uploaded_text('${name}')[:500]`, `pd.read_json(uploaded_file_path('${name}'))`]
  if (extension === '.txt' || extension === '.md') return [`read_uploaded_text('${name}')[:500]`]
  return [`uploaded_file_path('${name}')`, `list_uploaded_files()`]
}

const NotebookFilesDrawer = ({ notebookId, open, onClose }: NotebookFilesDrawerProps) => {
  const [workspace, setWorkspace] = useState<NotebookWorkspace | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [isUploading, setIsUploading] = useState(false)

  const loadWorkspace = useCallback(async () => {
    if (!notebookId) return
    setIsLoading(true)
    try {
      const data = await codelabApi.listFiles(notebookId)
      setWorkspace(data)
    } catch (error) {
      handleApiError(error, '加载文件列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [notebookId])

  useEffect(() => {
    if (open && notebookId) {
      void loadWorkspace()
    }
  }, [open, notebookId, loadWorkspace])

  const handleUpload = useCallback(async (file: File) => {
    setIsUploading(true)
    try {
      await codelabApi.uploadFile(notebookId, file)
      message.success(`已上传 ${file.name}`)
      await loadWorkspace()
    } catch (error) {
      handleApiError(error, '上传文件失败')
    } finally {
      setIsUploading(false)
    }
    return false
  }, [loadWorkspace, notebookId])

  const handleCopy = useCallback((value: string, label: string) => {
    navigator.clipboard.writeText(value)
    message.success(`${label}已复制`)
  }, [])

  const handleDelete = useCallback(async (fileName: string) => {
    try {
      await codelabApi.deleteFile(notebookId, fileName)
      message.success(`已删除 ${fileName}`)
      await loadWorkspace()
    } catch (error) {
      handleApiError(error, '删除文件失败')
    }
  }, [loadWorkspace, notebookId])

  const usageHints = useMemo(
    () => getUsageHint(workspace?.files?.[0] || null),
    [workspace],
  )

  return (
    <Drawer
      title={<span className="text-slate-100">Notebook 文件</span>}
      placement="right"
      width={420}
      open={open}
      onClose={onClose}
      styles={{
        header: { background: '#0f172a', borderBottom: '1px solid #1e293b' },
        body: { background: '#020617', padding: 20 },
        content: { background: '#020617' },
      }}
      extra={(
        <Tooltip title="刷新文件列表">
          <Button
            type="text"
            size="small"
            icon={<ReloadOutlined />}
            loading={isLoading}
            onClick={() => void loadWorkspace()}
            className="text-slate-300 hover:text-white"
          />
        </Tooltip>
      )}
    >
      <div className="space-y-4">
        <div className="rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs uppercase tracking-[0.24em] text-slate-500">Workspace</div>
              <div className="mt-2 break-all font-mono text-xs text-slate-200">
                {workspace?.display_path || 'uploads/codelab/notebooks/...'}
              </div>
            </div>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              onClick={() => handleCopy(workspace?.workspace_dir || '', '工作区路径')}
              disabled={!workspace?.workspace_dir}
              className="text-slate-400 hover:text-white"
            />
          </div>
          <div className="mt-3 flex items-center gap-2 text-xs text-slate-400">
            <Tag color="geekblue">1 个 Notebook = 1 个文件夹</Tag>
            <span>{workspace?.file_count || 0} 个文件</span>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
          <div className="mb-3 text-sm font-medium text-slate-100">上传文件</div>
          <Upload
            showUploadList={false}
            multiple
            beforeUpload={(file) => {
              void handleUpload(file)
              return false
            }}
          >
            <Button type="primary" icon={<UploadOutlined />} loading={isUploading} className="rounded-xl">
              上传到当前 Notebook 文件夹
            </Button>
          </Upload>
          <div className="mt-3 space-y-1 text-xs text-slate-400">
            <div>代码里可直接用相对路径读取，例如 `pd.read_csv('data.csv')`。</div>
            <div>也可以用 helper：`list_uploaded_files()`、`uploaded_file_path(name)`、`read_uploaded_text(name)`。</div>
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
          <div className="mb-3 text-sm font-medium text-slate-100">使用提示</div>
          <div className="space-y-2">
            {usageHints.map((hint) => (
              <div key={hint} className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-950/70 px-3 py-2">
                <code className="min-w-0 flex-1 break-all text-xs text-emerald-300">{hint}</code>
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={() => handleCopy(hint, '代码片段')}
                  className="text-slate-400 hover:text-white"
                />
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900/70 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="text-sm font-medium text-slate-100">文件名单</div>
            <div className="text-xs text-slate-500">供 Notebook / Agent 使用</div>
          </div>

          {!workspace?.files?.length ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<span className="text-slate-500">当前还没有上传文件</span>}
            />
          ) : (
            <List
              dataSource={workspace.files}
              loading={isLoading}
              renderItem={(item) => (
                <List.Item className="!border-slate-800 !px-0">
                  <div className="w-full rounded-xl border border-slate-800 bg-slate-950/70 px-3 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <FileTextOutlined className="text-slate-400" />
                          <div className="truncate text-sm font-medium text-slate-100">{item.name}</div>
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                          <Tag bordered={false} color="default">{formatFileSize(item.size_bytes)}</Tag>
                          <Tag bordered={false} color="processing">{item.extension || 'file'}</Tag>
                          <span>{new Date(item.updated_at).toLocaleString()}</span>
                        </div>
                        <div className="mt-2 break-all font-mono text-[11px] text-slate-400">
                          {item.runtime_path}
                        </div>
                      </div>

                      <div className="flex items-center gap-1">
                        <Tooltip title="复制运行时路径">
                          <Button
                            type="text"
                            size="small"
                            icon={<CopyOutlined />}
                            onClick={() => handleCopy(item.runtime_path, '文件路径')}
                            className="text-slate-400 hover:text-white"
                          />
                        </Tooltip>
                        <Popconfirm
                          title={`删除 ${item.name}？`}
                          okText="删除"
                          cancelText="取消"
                          onConfirm={() => void handleDelete(item.name)}
                        >
                          <Tooltip title="删除文件">
                            <Button
                              type="text"
                              size="small"
                              icon={<DeleteOutlined />}
                              className="text-slate-400 hover:text-rose-400"
                            />
                          </Tooltip>
                        </Popconfirm>
                      </div>
                    </div>
                  </div>
                </List.Item>
              )}
            />
          )}
        </div>
      </div>
    </Drawer>
  )
}

export default NotebookFilesDrawer
