import { useEffect, useRef, useState } from 'react'
import { Card, Spin, Empty, Button, Tooltip, Space, Select, Slider } from 'antd'
import { 
  ZoomInOutlined, ZoomOutOutlined, FullscreenOutlined, 
  ReloadOutlined, AimOutlined 
} from '@ant-design/icons'
import { CitationGraph as CitationGraphData, GraphNode, GraphEdge } from '@/services/api'

interface CitationGraphProps {
  data: CitationGraphData | null
  onNodeClick?: (nodeId: string) => void
  loading?: boolean
}

// 节点颜色配置
const NODE_COLORS = {
  center: { background: '#1890ff', border: '#096dd9', font: '#fff' },
  citing: { background: '#52c41a', border: '#389e0d', font: '#fff' },
  referenced: { background: '#faad14', border: '#d48806', font: '#fff' },
}

// 布局选项
const LAYOUT_OPTIONS = [
  { value: 'hierarchical', label: '层级布局' },
  { value: 'force', label: '力导向布局' },
  { value: 'radial', label: '径向布局' },
]

export default function CitationGraph({ data, onNodeClick, loading }: CitationGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const networkRef = useRef<any>(null)
  const [layout, setLayout] = useState('hierarchical')
  const [physics, setPhysics] = useState(true)

  useEffect(() => {
    if (!data || !containerRef.current) return

    // 动态加载 vis-network
    const loadVisNetwork = async () => {
      try {
        // @ts-ignore
        const vis = await import('vis-network/standalone')
        
        // 准备节点数据
        const nodes = new vis.DataSet(
          data.nodes.map((node: GraphNode) => ({
            id: node.id,
            label: truncateTitle(node.title, 30),
            title: createTooltip(node),
            level: node.level,
            color: NODE_COLORS[node.type as keyof typeof NODE_COLORS] || NODE_COLORS.referenced,
            font: { 
              color: NODE_COLORS[node.type as keyof typeof NODE_COLORS]?.font || '#333',
              size: node.type === 'center' ? 14 : 12,
            },
            size: node.type === 'center' ? 30 : Math.min(15 + Math.log(node.citations + 1) * 3, 25),
            shape: 'dot',
            borderWidth: node.type === 'center' ? 3 : 2,
          }))
        )

        // 准备边数据
        const edges = new vis.DataSet(
          data.edges.map((edge: GraphEdge, index: number) => ({
            id: `edge-${index}`,
            from: edge.from,
            to: edge.to,
            arrows: { to: { enabled: true, scaleFactor: 0.5 } },
            color: { color: '#999', highlight: '#1890ff', hover: '#1890ff' },
            width: 1,
            smooth: {
              type: 'curvedCW',
              roundness: 0.2,
            },
          }))
        )

        // 配置选项
        const options = {
          nodes: {
            shape: 'dot',
            font: {
              size: 12,
              face: 'Arial',
            },
            scaling: {
              min: 10,
              max: 30,
            },
          },
          edges: {
            smooth: {
              type: 'continuous',
            },
            arrows: {
              to: { enabled: true, scaleFactor: 0.5 },
            },
          },
          physics: {
            enabled: physics,
            solver: 'forceAtlas2Based',
            forceAtlas2Based: {
              gravitationalConstant: -50,
              centralGravity: 0.01,
              springLength: 100,
              springConstant: 0.08,
            },
            stabilization: {
              enabled: true,
              iterations: 100,
            },
          },
          layout: getLayoutOptions(layout),
          interaction: {
            hover: true,
            tooltipDelay: 100,
            zoomView: true,
            dragView: true,
            dragNodes: true,
          },
        }

        // 创建网络
        if (networkRef.current) {
          networkRef.current.destroy()
        }
        
        networkRef.current = new vis.Network(
          containerRef.current,
          { nodes, edges },
          options
        )

        // 事件监听
        networkRef.current.on('click', (params: any) => {
          if (params.nodes.length > 0) {
            const nodeId = params.nodes[0]
            onNodeClick?.(nodeId)
          }
        })

        // 聚焦到中心节点
        setTimeout(() => {
          if (networkRef.current && data.center_id) {
            networkRef.current.focus(data.center_id, {
              scale: 0.8,
              animation: {
                duration: 500,
                easingFunction: 'easeInOutQuad',
              },
            })
          }
        }, 500)

      } catch (error) {
        console.error('Failed to load vis-network:', error)
      }
    }

    loadVisNetwork()

    return () => {
      if (networkRef.current) {
        networkRef.current.destroy()
        networkRef.current = null
      }
    }
  }, [data, layout, physics])

  // 获取布局配置
  const getLayoutOptions = (layoutType: string) => {
    switch (layoutType) {
      case 'hierarchical':
        return {
          hierarchical: {
            enabled: true,
            direction: 'UD',
            sortMethod: 'directed',
            levelSeparation: 150,
            nodeSpacing: 100,
          },
        }
      case 'radial':
        return {
          hierarchical: {
            enabled: true,
            direction: 'UD',
            sortMethod: 'directed',
            levelSeparation: 200,
            nodeSpacing: 150,
          },
        }
      default:
        return {
          hierarchical: {
            enabled: false,
          },
        }
    }
  }

  // 工具函数
  const truncateTitle = (title: string, maxLen: number) => {
    if (title.length <= maxLen) return title
    return title.substring(0, maxLen) + '...'
  }

  const createTooltip = (node: GraphNode) => {
    return `
      <div style="max-width: 300px; padding: 8px;">
        <div style="font-weight: bold; margin-bottom: 4px;">${node.title}</div>
        <div style="color: #666; font-size: 12px;">
          ${node.authors?.slice(0, 3).join(', ') || 'Unknown'}
          ${node.year ? ` (${node.year})` : ''}
        </div>
        <div style="margin-top: 4px; font-size: 12px;">
          引用数: ${node.citations}
        </div>
      </div>
    `
  }

  // 控制方法
  const handleZoomIn = () => {
    if (networkRef.current) {
      const scale = networkRef.current.getScale()
      networkRef.current.moveTo({ scale: scale * 1.2 })
    }
  }

  const handleZoomOut = () => {
    if (networkRef.current) {
      const scale = networkRef.current.getScale()
      networkRef.current.moveTo({ scale: scale * 0.8 })
    }
  }

  const handleFit = () => {
    if (networkRef.current) {
      networkRef.current.fit({
        animation: {
          duration: 500,
          easingFunction: 'easeInOutQuad',
        },
      })
    }
  }

  const handleFocusCenter = () => {
    if (networkRef.current && data?.center_id) {
      networkRef.current.focus(data.center_id, {
        scale: 1,
        animation: {
          duration: 500,
          easingFunction: 'easeInOutQuad',
        },
      })
    }
  }

  const handleRefresh = () => {
    if (networkRef.current) {
      networkRef.current.stabilize()
    }
  }

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Spin size="large" tip="加载引用图谱..." />
      </div>
    )
  }

  if (!data) {
    return (
      <Empty 
        className="py-20"
        description="选择一篇论文以查看其引用关系图谱"
      />
    )
  }

  return (
    <div className="h-full flex flex-col">
      {/* 工具栏 */}
      <div className="p-3 border-b bg-white flex items-center justify-between">
        <Space>
          <span className="text-gray-500">布局:</span>
          <Select
            value={layout}
            onChange={setLayout}
            options={LAYOUT_OPTIONS}
            style={{ width: 120 }}
            size="small"
          />
          <span className="text-gray-500 ml-4">物理模拟:</span>
          <Button 
            size="small"
            type={physics ? 'primary' : 'default'}
            onClick={() => setPhysics(!physics)}
          >
            {physics ? '开启' : '关闭'}
          </Button>
        </Space>

        <Space>
          <Tooltip title="放大">
            <Button icon={<ZoomInOutlined />} size="small" onClick={handleZoomIn} />
          </Tooltip>
          <Tooltip title="缩小">
            <Button icon={<ZoomOutOutlined />} size="small" onClick={handleZoomOut} />
          </Tooltip>
          <Tooltip title="适应屏幕">
            <Button icon={<FullscreenOutlined />} size="small" onClick={handleFit} />
          </Tooltip>
          <Tooltip title="聚焦中心">
            <Button icon={<AimOutlined />} size="small" onClick={handleFocusCenter} />
          </Tooltip>
          <Tooltip title="重新布局">
            <Button icon={<ReloadOutlined />} size="small" onClick={handleRefresh} />
          </Tooltip>
        </Space>
      </div>

      {/* 图例 */}
      <div className="px-4 py-2 border-b bg-gray-50 flex items-center gap-6 text-sm">
        <div className="flex items-center gap-2">
          <span className="w-4 h-4 rounded-full" style={{ backgroundColor: NODE_COLORS.center.background }} />
          <span>当前论文</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-4 h-4 rounded-full" style={{ backgroundColor: NODE_COLORS.citing.background }} />
          <span>引用了此论文</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-4 h-4 rounded-full" style={{ backgroundColor: NODE_COLORS.referenced.background }} />
          <span>被此论文引用</span>
        </div>
        <div className="ml-auto text-gray-500">
          共 {data.nodes.length} 篇论文, {data.edges.length} 条引用关系
        </div>
      </div>

      {/* 图谱容器 */}
      <div 
        ref={containerRef} 
        className="flex-1 bg-white"
        style={{ minHeight: '400px' }}
      />
    </div>
  )
}
