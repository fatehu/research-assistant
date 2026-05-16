import { FileOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { Empty, Tree, Typography } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { useMemo, useState } from 'react'


const { Text } = Typography

type ProjectFolderBrowserProps = {
  tree?: string
}

type ProjectFolderNode = DataNode & {
  key: string
  name: string
  path: string
  isDirectory: boolean
  children?: ProjectFolderNode[]
}

const parseProjectTree = (tree: string): ProjectFolderNode[] => {
  const lines = String(tree || '')
    .split('\n')
    .map((line) => line.replace(/\r/g, ''))
    .filter(Boolean)

  if (lines.length <= 1) return []

  const roots: ProjectFolderNode[] = []
  const stack: ProjectFolderNode[] = []

  for (const line of lines.slice(1)) {
    const match = line.match(/^((?:\| {3}| {4})*)(?:\|-- |`-- )(.*)$/)
    if (!match) continue

    const depth = Math.floor(match[1].length / 4)
    const rawLabel = String(match[2] || '').trim()
    if (!rawLabel) continue

    const isDirectory = rawLabel.endsWith('/')
    const name = isDirectory ? rawLabel.slice(0, -1) : rawLabel
    while (stack.length > depth) {
      stack.pop()
    }

    const parent = depth > 0 ? stack[depth - 1] : null
    const path = parent ? `${parent.path}/${name}` : name
    const node: ProjectFolderNode = {
      key: path,
      title: name,
      name,
      path,
      isDirectory,
      isLeaf: !isDirectory,
      children: isDirectory ? [] : undefined,
    }

    if (parent) {
      parent.children = [...(parent.children || []), node]
    } else {
      roots.push(node)
    }

    if (isDirectory) {
      stack[depth] = node
    }
  }

  return roots
}

const collectInitialExpandedKeys = (nodes: ProjectFolderNode[]): string[] =>
  nodes.filter((node) => node.isDirectory).map((node) => String(node.key))

export function ProjectFolderBrowser({ tree }: ProjectFolderBrowserProps) {
  const treeData = useMemo(() => parseProjectTree(tree || '.'), [tree])
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([])

  const selectedPath = selectedKeys.length > 0 ? String(selectedKeys[0]) : null
  const initialExpandedKeys = useMemo(() => collectInitialExpandedKeys(treeData), [treeData])

  if (treeData.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前目录还是空的" />
  }

  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-[22px] border border-white/[0.08] bg-[#020817]/95 shadow-[inset_0_1px_0_rgba(255,255,255,0.035),0_18px_44px_rgba(0,0,0,0.24)]">
        <div className="flex items-center justify-between border-b border-white/[0.07] bg-white/[0.035] px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-red-400/80" />
            <span className="h-2.5 w-2.5 rounded-full bg-amber-300/80" />
            <span className="h-2.5 w-2.5 rounded-full bg-emerald-300/80" />
          </div>
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-slate-500">project explorer</span>
        </div>
        <div className="p-2">
          <Tree<ProjectFolderNode>
            className="premium-folder-tree"
            blockNode
            defaultExpandedKeys={initialExpandedKeys}
            selectedKeys={selectedKeys}
            treeData={treeData}
            height={360}
            onSelect={(keys) => setSelectedKeys(keys)}
            titleRender={(node) => (
              <div className="flex items-center gap-2 py-0.5">
                {node.isDirectory ? (
                  <FolderOpenOutlined className="text-cyan-300" />
                ) : (
                  <FileOutlined className="text-slate-400" />
                )}
                <span className="font-mono text-xs text-slate-200">{node.name}</span>
              </div>
            )}
          />
        </div>
      </div>
      <div className="rounded-2xl border border-white/[0.08] bg-[#020817]/70 px-3 py-2">
        <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">Selected Path</div>
        <Text className="!mt-1 !block !font-mono !text-xs !text-slate-300">
          {selectedPath || '未选择文件或目录'}
        </Text>
      </div>
    </div>
  )
}
