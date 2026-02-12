import { Button, Tooltip, Spin, Badge, Modal } from 'antd'
import { FolderOutlined, PlusOutlined, DeleteOutlined, DatabaseOutlined } from '@ant-design/icons'

interface Collection {
  id: number
  name: string
  color: string
  paper_count: number
  is_default: boolean
}

interface CollectionSidebarProps {
  collections: Collection[]
  collectionsLoading: boolean
  selectedCollectionId: number | null
  totalPapers: number
  onSelectCollection: (id: number | null) => void
  onDeleteCollection: (id: number) => void
  onCreateClick: () => void
}

/** 收藏夹侧边栏 */
const CollectionSidebar = ({
  collections,
  collectionsLoading,
  selectedCollectionId,
  totalPapers,
  onSelectCollection,
  onDeleteCollection,
  onCreateClick,
}: CollectionSidebarProps) => (
  <div className="w-64 border-r border-slate-700/50 flex flex-col bg-slate-900/30 flex-shrink-0">
    {/* 标题 */}
    <div className="p-4 border-b border-slate-700/50 flex-shrink-0">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-lg text-slate-200 flex items-center gap-2">
          <FolderOutlined className="text-emerald-400" />
          收藏夹
        </h3>
        <Tooltip title="新建收藏夹">
          <Button
            type="text"
            icon={<PlusOutlined />}
            onClick={onCreateClick}
            className="!text-slate-400 hover:!text-emerald-400"
          />
        </Tooltip>
      </div>
    </div>

    {/* 收藏夹列表 - 可滚动 */}
    <div className="flex-1 overflow-y-auto p-2 scrollbar-thin">
      {collectionsLoading ? (
        <div className="flex justify-center py-8"><Spin /></div>
      ) : (
        <div className="space-y-1">
          {/* 全部论文 */}
          <div
            className={`px-3 py-2.5 rounded-lg cursor-pointer flex justify-between items-center transition-all ${
              selectedCollectionId === null
                ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                : 'hover:bg-slate-800/50 text-slate-400 border border-transparent'
            }`}
            onClick={() => onSelectCollection(null)}
          >
            <span className="flex items-center gap-2">
              <DatabaseOutlined />
              全部论文
            </span>
            <Badge
              count={totalPapers}
              showZero
              className={selectedCollectionId === null ? '[&_.ant-badge-count]:!bg-emerald-500' : '[&_.ant-badge-count]:!bg-slate-600'}
            />
          </div>

          {/* 收藏夹列表 */}
          {collections.map(coll => (
            <div
              key={coll.id}
              className={`px-3 py-2.5 rounded-lg cursor-pointer flex justify-between items-center group transition-all ${
                selectedCollectionId === coll.id
                  ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                  : 'hover:bg-slate-800/50 text-slate-400 border border-transparent'
              }`}
              onClick={() => onSelectCollection(coll.id)}
            >
              <span className="flex items-center gap-2 min-w-0">
                <span className="w-3 h-3 rounded flex-shrink-0" style={{ backgroundColor: coll.color }} />
                <span className="truncate">{coll.name}</span>
              </span>
              <div className="flex items-center gap-1 flex-shrink-0">
                <Badge
                  count={coll.paper_count}
                  showZero
                  className={selectedCollectionId === coll.id ? '[&_.ant-badge-count]:!bg-emerald-500' : '[&_.ant-badge-count]:!bg-slate-600'}
                />
                {!coll.is_default && (
                  <Button
                    type="text"
                    size="small"
                    icon={<DeleteOutlined />}
                    className="!text-slate-500 hover:!text-red-400 opacity-0 group-hover:opacity-100 transition-opacity !w-6 !h-6 !min-w-0"
                    onClick={e => {
                      e.stopPropagation()
                      Modal.confirm({
                        title: '删除收藏夹',
                        content: `确定删除收藏夹 "${coll.name}" 吗？`,
                        onOk: () => onDeleteCollection(coll.id),
                      })
                    }}
                  />
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  </div>
)

export default CollectionSidebar
