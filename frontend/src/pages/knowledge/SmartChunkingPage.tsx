import { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  Card,
  Button,
  Tabs,
  Form,
  Input,
  Select,
  Slider,
  Switch,
  Tag,
  Space,
  Spin,
  message,
  Tooltip,
  Progress,
  List,
  Typography,
  Row,
  Col,
  Statistic,
  Alert,
  Divider,
  Radio,
  Badge,
  Empty,
} from 'antd'
import {
  ArrowLeftOutlined,
  ThunderboltOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  CheckCircleOutlined,
  BookOutlined,
  BranchesOutlined,
  SettingOutlined,
  PlayCircleOutlined,
  InfoCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  BarChartOutlined,
} from '@ant-design/icons'
import { motion, AnimatePresence } from 'framer-motion'
import {
  chunkingApi,
  knowledgeApi,
  ChunkingPreset,
  ChunkingStrategy,
  ChunkLevel,
  PresetDescription,
  ChunkingResult,
  DocumentAnalysis,
  StrategyComparison,
  ChunkingConfig,
  ChunkingConfigResponse,
  KnowledgeBase,
} from '@/services/api'

const { TextArea } = Input
const { Text, Paragraph, Title } = Typography
const { TabPane } = Tabs

// 预设颜色映射
const PRESET_COLORS: Record<string, string> = {
  default: 'blue',
  fast: 'green',
  precise: 'purple',
  academic: 'orange',
  deep: 'cyan',
}

// 预设图标映射
const PRESET_ICONS: Record<string, React.ReactNode> = {
  default: <ThunderboltOutlined />,
  fast: <ThunderboltOutlined />,
  precise: <ExperimentOutlined />,
  academic: <BookOutlined />,
  deep: <BranchesOutlined />,
}

// 策略中文名映射
const STRATEGY_NAMES: Record<string, string> = {
  fixed: '固定分块',
  semantic: '语义分块',
  hierarchical: '层级分块',
  academic: '学术专用',
  hybrid: '混合策略',
}

// 层级中文名映射
const LEVEL_NAMES: Record<string, string> = {
  paragraph: '段落级',
  section: '章节级',
  document: '文档级',
}

const DEFAULT_CUSTOM_CONFIG: Partial<ChunkingConfig> = {
  strategy: ChunkingStrategy.HYBRID,
  use_token_based: true,
  base_chunk_tokens: 128,
  overlap_tokens: 16,
  min_semantic_tokens: 32,
  max_semantic_tokens: 384,
  base_chunk_size: 500,
  chunk_overlap: 50,
  breakpoint_percentile: 95,
  semantic_threshold: 0.75,
  min_semantic_chunk: 100,
  max_semantic_chunk: 1500,
  enable_hierarchical: true,
  hierarchy_levels: [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION],
  detect_academic_structure: true,
  preserve_citations: true,
}

function toEditableConfig(config?: Partial<ChunkingConfigResponse> | null): Partial<ChunkingConfig> {
  return {
    ...DEFAULT_CUSTOM_CONFIG,
    ...config,
    hierarchy_levels:
      config?.hierarchy_levels && config.hierarchy_levels.length > 0
        ? config.hierarchy_levels
        : DEFAULT_CUSTOM_CONFIG.hierarchy_levels,
  }
}

function deriveCompatibilityChars(config: Partial<ChunkingConfig>): Pick<
  ChunkingConfig,
  'base_chunk_size' | 'chunk_overlap' | 'min_semantic_chunk' | 'max_semantic_chunk'
> {
  const charsPerToken = 4
  return {
    base_chunk_size: Math.max(100, Math.min(3000, Math.round((config.base_chunk_tokens || 128) * charsPerToken))),
    chunk_overlap: Math.max(0, Math.min(500, Math.round((config.overlap_tokens || 16) * charsPerToken))),
    min_semantic_chunk: Math.max(50, Math.min(500, Math.round((config.min_semantic_tokens || 32) * charsPerToken))),
    max_semantic_chunk: Math.max(500, Math.min(5000, Math.round((config.max_semantic_tokens || 384) * charsPerToken))),
  }
}

function toRuntimeConfig(config: Partial<ChunkingConfig>): ChunkingConfig {
  const strategy = (config.strategy || ChunkingStrategy.HYBRID) as ChunkingStrategy
  const baseChunkTokens = config.base_chunk_tokens ?? (DEFAULT_CUSTOM_CONFIG.base_chunk_tokens as number)
  const overlapTokens = config.overlap_tokens ?? (DEFAULT_CUSTOM_CONFIG.overlap_tokens as number)
  const minSemanticTokens = config.min_semantic_tokens ?? (DEFAULT_CUSTOM_CONFIG.min_semantic_tokens as number)
  const maxSemanticTokens = config.max_semantic_tokens ?? (DEFAULT_CUSTOM_CONFIG.max_semantic_tokens as number)
  const hierarchyEnabled =
    strategy === ChunkingStrategy.HIERARCHICAL
      ? true
      : Boolean(config.enable_hierarchical ?? DEFAULT_CUSTOM_CONFIG.enable_hierarchical)
  const defaultHierarchyLevels =
    strategy === ChunkingStrategy.HIERARCHICAL
      ? [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION, ChunkLevel.DOCUMENT]
      : [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION]

  return {
    strategy,
    use_token_based: true,
    base_chunk_tokens: baseChunkTokens,
    overlap_tokens: overlapTokens,
    min_semantic_tokens: minSemanticTokens,
    max_semantic_tokens: maxSemanticTokens,
    ...deriveCompatibilityChars({
      ...config,
      base_chunk_tokens: baseChunkTokens,
      overlap_tokens: overlapTokens,
      min_semantic_tokens: minSemanticTokens,
      max_semantic_tokens: maxSemanticTokens,
    }),
    breakpoint_percentile: config.breakpoint_percentile ?? (DEFAULT_CUSTOM_CONFIG.breakpoint_percentile as number),
    semantic_threshold: DEFAULT_CUSTOM_CONFIG.semantic_threshold as number,
    enable_hierarchical: hierarchyEnabled,
    hierarchy_levels:
      hierarchyEnabled
        ? ((config.hierarchy_levels?.length ? config.hierarchy_levels : defaultHierarchyLevels) as ChunkLevel[])
        : ([ChunkLevel.PARAGRAPH, ChunkLevel.SECTION] as ChunkLevel[]),
    detect_academic_structure:
      strategy === ChunkingStrategy.ACADEMIC
        ? true
        : Boolean(config.detect_academic_structure ?? DEFAULT_CUSTOM_CONFIG.detect_academic_structure),
    preserve_citations: Boolean(config.preserve_citations ?? DEFAULT_CUSTOM_CONFIG.preserve_citations),
  }
}

// 预设卡片组件
const PresetCard = ({
  preset,
  selected,
  onClick,
}: {
  preset: PresetDescription
  selected: boolean
  onClick: () => void
}) => (
  <motion.div
    whileHover={{ scale: 1.02 }}
    whileTap={{ scale: 0.98 }}
  >
    <Card
      className={`cursor-pointer transition-all border-2 ${selected
        ? 'border-emerald-500 bg-emerald-500/10'
        : 'border-slate-700 bg-slate-800/50 hover:border-slate-600'
        }`}
      onClick={onClick}
      styles={{ body: { padding: 16 } }}
    >
      <div className="flex items-start gap-3">
        <div className={`text-2xl ${selected ? 'text-emerald-400' : 'text-slate-400'}`}>
          {PRESET_ICONS[preset.name]}
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <Text strong className={selected ? 'text-white' : 'text-slate-200'}>
              {preset.name.toUpperCase()}
            </Text>
            <Tag color={PRESET_COLORS[preset.name]}>{STRATEGY_NAMES[preset.strategy]}</Tag>
          </div>
          <Text className="text-slate-400 text-sm block mt-1">{preset.description}</Text>
          <div className="mt-2 flex flex-wrap gap-1">
            {preset.recommended_for.map((use, idx) => (
              <Tag key={idx} className="text-xs bg-slate-700/50 border-slate-600">
                {use}
              </Tag>
            ))}
          </div>
        </div>
        {selected && <CheckCircleOutlined className="text-emerald-400 text-xl" />}
      </div>
    </Card>
  </motion.div>
)

// 分块结果卡片
const ChunkCard = ({
  chunk,
  index,
}: {
  chunk: { content: string; metadata: { level: string; section_type?: string; has_citations: boolean; token_count?: number } }
  index: number
}) => (
  <Card
    className="bg-slate-800/50 border-slate-700 mb-3"
    size="small"
    title={
      <div className="flex items-center gap-2">
        <Text className="text-slate-400">#{index + 1}</Text>
        <Tag color="blue">{LEVEL_NAMES[chunk.metadata.level] || chunk.metadata.level}</Tag>
        {chunk.metadata.section_type && (
          <Tag color="orange">{chunk.metadata.section_type}</Tag>
        )}
        {chunk.metadata.has_citations && (
          <Tag color="purple">含引用</Tag>
        )}
      </div>
    }
  >
    <Paragraph
      className="text-slate-300 text-sm mb-0"
      ellipsis={{ rows: 3, expandable: true }}
    >
      {chunk.content}
    </Paragraph>
    <div className="mt-2 text-slate-500 text-xs">
      {chunk.content.length} 字符 | {chunk.metadata.token_count || '?'} Tokens
    </div>
  </Card>
)

// 主页面组件
export default function SmartChunkingPage() {
  const { kbId } = useParams<{ kbId?: string }>()
  const navigate = useNavigate()

  // 基础状态
  const [activeTab, setActiveTab] = useState('presets')
  const [loading, setLoading] = useState(false)
  const [presets, setPresets] = useState<PresetDescription[]>([])
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBase | null>(null)
  const [currentConfig, setCurrentConfig] = useState<ChunkingConfigResponse | null>(null)
  const [selectedPreset, setSelectedPreset] = useState<string>('default')

  // 测试状态
  const [testText, setTestText] = useState('')
  const [testResult, setTestResult] = useState<ChunkingResult | null>(null)
  const [analysisResult, setAnalysisResult] = useState<DocumentAnalysis | null>(null)
  const [comparisonResult, setComparisonResult] = useState<StrategyComparison | null>(null)
  const [testing, setTesting] = useState(false)

  // 自定义配置
  const [customConfig, setCustomConfig] = useState<Partial<ChunkingConfig>>(DEFAULT_CUSTOM_CONFIG)

  // 加载预设列表
  const loadPresets = useCallback(async () => {
    try {
      const data = await chunkingApi.getPresets()
      setPresets(data.presets)
    } catch (error) {
      console.error('Failed to load presets:', error)
      message.error('加载预设配置失败')
    }
  }, [])

  const loadKnowledgeBase = useCallback(async () => {
    if (!kbId) return
    try {
      const kb = await knowledgeApi.getKnowledgeBase(parseInt(kbId))
      setKnowledgeBase(kb)
    } catch (error) {
      console.error('Failed to load knowledge base:', error)
    }
  }, [kbId])

  const loadCurrentConfig = useCallback(async () => {
    if (!kbId) return
    try {
      const config = await chunkingApi.getKnowledgeBaseConfig(parseInt(kbId))
      setCurrentConfig(config)
      setCustomConfig(toEditableConfig(config))
      if (config?.name) {
        setSelectedPreset(config.name)
      }
    } catch (error) {
      console.error('Failed to load config:', error)
    }
  }, [kbId])

  // Load preset list
  useEffect(() => {
    loadPresets()
    if (kbId) {
      loadKnowledgeBase()
      loadCurrentConfig()
    }
  }, [kbId, loadCurrentConfig, loadKnowledgeBase, loadPresets])

  // 测试分块
  const handlePreviewChunking = async () => {
    if (!testText.trim()) {
      message.warning('请输入测试文本')
      return
    }

    setTesting(true)
    try {
      const runtimeConfig = toRuntimeConfig(customConfig)
      const result = await chunkingApi.previewChunking(
        testText,
        activeTab === 'custom' ? runtimeConfig : undefined,
        activeTab === 'presets' ? (selectedPreset as ChunkingPreset) : undefined
      )
      setTestResult(result)
      message.success('分块预览完成')
    } catch (error) {
      console.error('Preview failed:', error)
      message.error('分块预览失败')
    } finally {
      setTesting(false)
    }
  }

  // 分析文档
  const handleAnalyzeDocument = async () => {
    if (!testText.trim()) {
      message.warning('请输入测试文本')
      return
    }

    setTesting(true)
    try {
      const result = await chunkingApi.analyzeDocument(testText)
      setAnalysisResult(result)
      message.success('文档分析完成')
    } catch (error) {
      console.error('Analysis failed:', error)
      message.error('文档分析失败')
    } finally {
      setTesting(false)
    }
  }

  // 比较策略
  const handleCompareStrategies = async () => {
    if (!testText.trim()) {
      message.warning('请输入测试文本')
      return
    }

    setTesting(true)
    try {
      const result = await chunkingApi.compareStrategies(
        testText,
        [ChunkingPreset.FAST, ChunkingPreset.PRECISE, ChunkingPreset.ACADEMIC, ChunkingPreset.DEEP]
      )
      setComparisonResult(result)
      message.success('策略比较完成')
    } catch (error) {
      console.error('Comparison failed:', error)
      message.error('策略比较失败')
    } finally {
      setTesting(false)
    }
  }

  // 保存配置到知识库
  const handleSaveConfig = async () => {
    if (!kbId) {
      message.warning('请先选择知识库')
      return
    }

    setLoading(true)
    try {
      if (activeTab === 'presets') {
        // 使用专用的 apply-preset 端点
        await chunkingApi.applyPresetToKnowledgeBase(parseInt(kbId), selectedPreset as ChunkingPreset)
      } else {
        await chunkingApi.updateKnowledgeBaseConfig(parseInt(kbId), toRuntimeConfig(customConfig))
      }
      message.success('配置已保存')
      loadCurrentConfig()
    } catch (error) {
      console.error('Save failed:', error)
      message.error('保存配置失败')
    } finally {
      setLoading(false)
    }
  }

  const usesSemanticTuning =
    customConfig.strategy === ChunkingStrategy.SEMANTIC ||
    customConfig.strategy === ChunkingStrategy.HYBRID ||
    customConfig.strategy === ChunkingStrategy.ACADEMIC
  const usesHierarchyOutput =
    customConfig.strategy === ChunkingStrategy.HIERARCHICAL ||
    (customConfig.strategy === ChunkingStrategy.HYBRID && customConfig.enable_hierarchical)
  const usesAcademicRouting = customConfig.strategy === ChunkingStrategy.HYBRID
  const usesCitationProtection =
    customConfig.strategy === ChunkingStrategy.SEMANTIC ||
    customConfig.strategy === ChunkingStrategy.HYBRID ||
    customConfig.strategy === ChunkingStrategy.ACADEMIC

  return (
    <div className="min-h-screen bg-slate-900 p-6">
      <div className="max-w-7xl mx-auto">
        {/* 头部 */}
        <div className="mb-6">
          <Button
            type="text"
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(kbId ? `/knowledge/${kbId}` : '/knowledge')}
            className="text-slate-400 hover:text-white mb-4"
          >
            返回{knowledgeBase ? `「${knowledgeBase.name}」` : '知识库'}
          </Button>

          <div className="flex items-center justify-between">
            <div>
              <Title level={2} className="text-white mb-2">
                <SettingOutlined className="mr-3" />
                智能分块配置
              </Title>
              <Text className="text-slate-400">
                配置文档分块策略，优化检索质量
              </Text>
            </div>
            {kbId && (
              <Button
                type="primary"
                icon={<SaveOutlined />}
                onClick={handleSaveConfig}
                loading={loading}
                className="bg-emerald-600 hover:bg-emerald-500"
              >
                保存到知识库
              </Button>
            )}
          </div>
        </div>

        {/* 当前配置提示 */}
        {currentConfig && (
          <Alert
            type="info"
            showIcon
            className="mb-6 bg-blue-500/10 border-blue-500/30"
            message={
              <div className="text-blue-300 space-y-2">
                <div>
                  当前知识库使用的配置: <Tag color="blue">{currentConfig.name || currentConfig.strategy}</Tag>
                </div>
                <div className="text-blue-200/80 text-sm">
                  当前入库主链已切到 <Text code>structured extract -&gt; ingest-md -&gt; SmartChunkingService</Text>。
                  这里保存的配置只影响后续新上传文档，不会回溯重切已入库文档。
                </div>
              </div>
            }
          />
        )}

        <Row gutter={24}>
          {/* 左侧：配置区 */}
          <Col span={14}>
            <Card className="bg-slate-800/50 border-slate-700">
              <Tabs activeKey={activeTab} onChange={setActiveTab}>
                {/* 预设配置标签页 */}
                <TabPane
                  tab={
                    <span>
                      <ThunderboltOutlined />
                      预设配置
                    </span>
                  }
                  key="presets"
                >
                  <div className="space-y-4">
                    {presets.map((preset) => (
                      <PresetCard
                        key={preset.name}
                        preset={preset}
                        selected={selectedPreset === preset.name}
                        onClick={() => setSelectedPreset(preset.name)}
                      />
                    ))}
                  </div>
                </TabPane>

                {/* 自定义配置标签页 */}
                <TabPane
                  tab={
                    <span>
                      <SettingOutlined />
                      自定义配置
                    </span>
                  }
                  key="custom"
                >
                  <Form layout="vertical" className="text-slate-300">
                    <Alert
                      type="info"
                      showIcon
                      className="mb-4 bg-cyan-500/10 border-cyan-500/30"
                      message="自定义配置已切到新链路参数系统。页面只展示当前模式真实会用到的主参数；保存时会统一按 Token 模式写入运行配置。"
                    />

                    {/* 策略选择 */}
                    <Form.Item label={<Text className="text-slate-300">分块策略</Text>}>
                      <Select
                        value={customConfig.strategy}
                        onChange={(v) =>
                          setCustomConfig({ ...customConfig, strategy: v })
                        }
                        className="w-full"
                        options={Object.entries(STRATEGY_NAMES).map(([key, name]) => ({
                          value: key,
                          label: (
                            <span>
                              {name}
                              <Text className="text-slate-500 ml-2 text-xs">
                                {key === 'fixed' && '— 稳定快速，适合作为轻量基线'}
                                {key === 'semantic' && '— 语义优先，适合通用文本与 Markdown'}
                                {key === 'hierarchical' && '— 强化章节/文档上下文输出'}
                                {key === 'academic' && '— 面向论文结构与章节内语义切分'}
                                {key === 'hybrid' && '— 自动选择最佳策略（推荐）'}
                              </Text>
                            </span>
                          ),
                        }))}
                      />
                    </Form.Item>

                    <Alert
                      type="success"
                      showIcon
                      className="mb-4 bg-emerald-500/10 border-emerald-500/30"
                      message={
                        customConfig.strategy === ChunkingStrategy.FIXED
                          ? '固定分块模式只保留稳定的长度控制参数，适合追求速度和可预测性。'
                          : customConfig.strategy === ChunkingStrategy.SEMANTIC
                            ? '语义分块模式优先使用语义边界与 Token 粒度，适合作为通用 Markdown 主策略。'
                            : customConfig.strategy === ChunkingStrategy.HIERARCHICAL
                              ? '层级分块模式强调 paragraph/section/document 三层输出，适合需要强上下文回溯的场景。'
                              : customConfig.strategy === ChunkingStrategy.ACADEMIC
                                ? '学术模式会按章节先分段，再在章节内做语义细分，适合论文和技术报告。'
                                : '混合模式会在 academic 和 semantic 之间自动路由，并按需要补层级上下文。'
                      }
                    />

                    <Divider className="border-slate-700">基础粒度</Divider>
                    <Alert
                      type="info"
                      showIcon
                      className="mb-4 bg-slate-900/60 border-slate-700"
                      message="新链路统一使用 Token 计量。旧字符模式不再作为主配置项暴露。"
                    />

                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item
                          label={
                            <Tooltip title="每个分块的基础目标大小。值越大，上下文越完整；值越小，召回更细。">
                              <Text className="text-slate-300">
                                基础块大小 (Token) <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Slider
                            min={32}
                            max={512}
                            step={16}
                            value={customConfig.base_chunk_tokens}
                            onChange={(v) => setCustomConfig({ ...customConfig, base_chunk_tokens: v })}
                            marks={{ 32: '32', 128: '128', 256: '256', 512: '512' }}
                          />
                          <Text className="text-slate-500 text-xs">当前: {customConfig.base_chunk_tokens} Tokens</Text>
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          label={
                            <Tooltip title="相邻分块之间保留的重叠大小。固定模式和固定切分回退路径更依赖这个参数。">
                              <Text className="text-slate-300">
                                块重叠大小 (Token) <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Slider
                            min={0}
                            max={64}
                            step={4}
                            value={customConfig.overlap_tokens}
                            onChange={(v) => setCustomConfig({ ...customConfig, overlap_tokens: v })}
                            marks={{ 0: '0', 16: '16', 32: '32', 64: '64' }}
                          />
                          <Text className="text-slate-500 text-xs">当前: {customConfig.overlap_tokens} Tokens</Text>
                        </Form.Item>
                      </Col>
                    </Row>

                    {usesSemanticTuning && (
                      <>
                        <Divider className="border-slate-700">语义边界</Divider>
                        <Row gutter={16}>
                          <Col span={12}>
                            <Form.Item
                              label={
                                <Tooltip title="语义分块允许的最小粒度。值太小容易碎，值太大会吞掉细节。">
                                  <Text className="text-slate-300">
                                    最小语义块 (Token) <QuestionCircleOutlined className="text-slate-500" />
                                  </Text>
                                </Tooltip>
                              }
                            >
                              <Slider
                                min={16}
                                max={128}
                                step={8}
                                value={customConfig.min_semantic_tokens}
                                onChange={(v) => setCustomConfig({ ...customConfig, min_semantic_tokens: v })}
                                marks={{ 16: '16', 32: '32', 64: '64', 128: '128' }}
                              />
                              <Text className="text-slate-500 text-xs">当前: {customConfig.min_semantic_tokens} Tokens</Text>
                            </Form.Item>
                          </Col>
                          <Col span={12}>
                            <Form.Item
                              label={
                                <Tooltip title="语义分块允许的最大粒度。值越大，语义块越容易长上下文聚合。">
                                  <Text className="text-slate-300">
                                    最大语义块 (Token) <QuestionCircleOutlined className="text-slate-500" />
                                  </Text>
                                </Tooltip>
                              }
                            >
                              <Slider
                                min={128}
                                max={1024}
                                step={32}
                                value={customConfig.max_semantic_tokens}
                                onChange={(v) => setCustomConfig({ ...customConfig, max_semantic_tokens: v })}
                                marks={{ 128: '128', 384: '384', 512: '512', 1024: '1K' }}
                              />
                              <Text className="text-slate-500 text-xs">当前: {customConfig.max_semantic_tokens} Tokens</Text>
                            </Form.Item>
                          </Col>
                        </Row>

                        <Form.Item
                          label={
                            <Tooltip title="当前语义 splitter 的边界敏感度。越低切得越细，越高切得越聚合。">
                              <Text className="text-slate-300">
                                语义边界敏感度 <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Slider
                            min={20}
                            max={99}
                            step={5}
                            value={customConfig.breakpoint_percentile}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, breakpoint_percentile: v })
                            }
                            marks={{ 20: '更细', 50: '50', 75: '75', 90: '默认', 99: '更聚合' }}
                          />
                        </Form.Item>
                      </>
                    )}

                    {usesHierarchyOutput && (
                      <>
                        <Divider className="border-slate-700">层级输出</Divider>
                        {customConfig.strategy === ChunkingStrategy.HYBRID && (
                          <Form.Item
                            label={
                              <Tooltip title="混合模式下是否额外补 section/document 层级上下文。">
                                <Text className="text-slate-300">
                                  层级分块 <QuestionCircleOutlined className="text-slate-500" />
                                </Text>
                              </Tooltip>
                            }
                          >
                            <Switch
                              checked={customConfig.enable_hierarchical}
                              onChange={(v) =>
                                setCustomConfig({ ...customConfig, enable_hierarchical: v })
                              }
                            />
                          </Form.Item>
                        )}

                        <Form.Item
                          label={
                            <Tooltip title="控制要产出的层级粒度。混合模式主要影响补层级；层级模式下用于约束输出层次。">
                              <Text className="text-slate-300">
                                层级选择 <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Select
                            mode="multiple"
                            value={customConfig.hierarchy_levels}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, hierarchy_levels: v })
                            }
                            options={Object.entries(LEVEL_NAMES).map(([key, name]) => ({
                              value: key,
                              label: name,
                            }))}
                            placeholder="选择需要生成的层级"
                          />
                        </Form.Item>
                      </>
                    )}

                    {usesAcademicRouting && (
                      <>
                        <Divider className="border-slate-700">混合路由</Divider>
                        <Form.Item
                          label={
                            <Tooltip title="混合模式下，是否允许自动检测学术结构并切到 academic 路线。">
                              <Text className="text-slate-300">
                                学术结构 <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Switch
                            checked={customConfig.detect_academic_structure}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, detect_academic_structure: v })
                            }
                          />
                        </Form.Item>
                      </>
                    )}

                    {usesCitationProtection && (
                      <>
                        <Divider className="border-slate-700">文本保护</Divider>
                        <Form.Item
                          label={
                            <Tooltip title="对引用句和引文边界做保守保护，避免在学术文本里把引用上下文切断。">
                              <Text className="text-slate-300">
                                引用保护 <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Switch
                            checked={customConfig.preserve_citations}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, preserve_citations: v })
                            }
                          />
                        </Form.Item>
                      </>
                    )}
                  </Form>
                </TabPane>
              </Tabs>
            </Card>
          </Col>

          {/* 右侧：测试区 */}
          <Col span={10}>
            <Card className="bg-slate-800/50 border-slate-700" title="分块测试">
              {/* 测试文本输入 */}
              <Form.Item label={<Text className="text-slate-300">测试文本</Text>}>
                <TextArea
                  rows={6}
                  value={testText}
                  onChange={(e) => setTestText(e.target.value)}
                  placeholder="粘贴文档内容进行测试，或上传文件..."
                  className="bg-slate-900 border-slate-600 text-slate-300"
                />
              </Form.Item>

              {/* 操作按钮 */}
              <Space wrap className="mb-4">
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  onClick={handlePreviewChunking}
                  loading={testing}
                  className="bg-emerald-600 hover:bg-emerald-500"
                >
                  预览分块
                </Button>
                <Button
                  icon={<ExperimentOutlined />}
                  onClick={handleAnalyzeDocument}
                  loading={testing}
                >
                  分析文档
                </Button>
                <Button
                  icon={<BarChartOutlined />}
                  onClick={handleCompareStrategies}
                  loading={testing}
                >
                  比较策略
                </Button>
              </Space>

              {/* 分析结果 */}
              {analysisResult && (
                <Card
                  className="bg-slate-900/50 border-slate-600 mb-4"
                  size="small"
                  title={<Text className="text-slate-300">文档分析</Text>}
                >
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Text className="text-slate-400">文档类型:</Text>
                      <Tag color={analysisResult.is_academic ? 'orange' : 'blue'}>
                        {analysisResult.is_academic ? '学术文档' : '通用文档'}
                      </Tag>
                      {analysisResult.has_citations && <Tag color="purple">含引用</Tag>}
                      {analysisResult.language && (
                        <Tag color="cyan">{analysisResult.language === 'zh' ? '中文' : '英文'}</Tag>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <Text className="text-slate-400">推荐策略:</Text>
                      <Tag color="green">{STRATEGY_NAMES[analysisResult.recommended_strategy]}</Tag>
                      {analysisResult.estimated_chunks != null && (
                        <Tag color="geekblue">预估 {analysisResult.estimated_chunks} 块</Tag>
                      )}
                      {analysisResult.document_stats.total_tokens && (
                        <Tag color="blue">{analysisResult.document_stats.total_tokens} Tokens</Tag>
                      )}
                    </div>
                    <Text className="text-slate-500 text-sm">{analysisResult.recommended_reason}</Text>

                    <Divider className="border-slate-700 my-2" />

                    <Row gutter={8}>
                      <Col span={6}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">字符数</Text>}
                          value={analysisResult.document_stats.total_chars}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                      <Col span={6}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">句子数</Text>}
                          value={analysisResult.document_stats.total_sentences}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                      <Col span={6}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">段落数</Text>}
                          value={analysisResult.document_stats.total_paragraphs}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                      <Col span={6}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">章节数</Text>}
                          value={analysisResult.document_stats.section_count}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                    </Row>

                    {/* 检测到的章节结构可视化 */}
                    {analysisResult.detected_sections && analysisResult.detected_sections.length > 0 && (
                      <>
                        <Divider className="border-slate-700 my-2" />
                        <Text className="text-slate-400 text-xs block mb-2">检测到的章节结构:</Text>
                        <div className="space-y-1 max-h-48 overflow-y-auto pr-1">
                          {analysisResult.detected_sections.map((section, idx) => (
                            <div
                              key={idx}
                              className="flex items-center gap-2 px-2 py-1.5 rounded bg-slate-800/80 border border-slate-700/50"
                            >
                              <Badge
                                color={
                                  section.type === 'abstract' ? '#f59e0b' :
                                    section.type === 'methodology' ? '#3b82f6' :
                                      section.type === 'results' ? '#10b981' :
                                        section.type === 'conclusion' ? '#8b5cf6' :
                                          section.type === 'references' ? '#ef4444' :
                                            '#64748b'
                                }
                              />
                              <Text className="text-slate-300 text-xs flex-1 truncate" title={section.title}>
                                {section.title}
                              </Text>
                              <Tag
                                className="text-xs border-slate-600 bg-slate-700/50"
                                style={{ margin: 0, fontSize: 10 }}
                              >
                                {section.type}
                              </Tag>
                              <Text className="text-slate-500 text-xs whitespace-nowrap">
                                {section.length} 字符
                              </Text>
                            </div>
                          ))}
                        </div>
                      </>
                    )}
                  </div>
                </Card>
              )}

              {/* 比较结果 */}
              {comparisonResult && (
                <Card
                  className="bg-slate-900/50 border-slate-600 mb-4"
                  size="small"
                  title={<Text className="text-slate-300">策略比较</Text>}
                >
                  <Alert
                    type="success"
                    className="mb-3 bg-emerald-500/10 border-emerald-500/30"
                    message={
                      <span className="text-emerald-300">
                        推荐: <Tag color="green">{comparisonResult.recommendation.best_strategy}</Tag>
                        {comparisonResult.recommendation.reason}
                      </span>
                    }
                  />
                  <List
                    size="small"
                    dataSource={Object.entries(comparisonResult.comparisons)}
                    renderItem={([key, value]) => (
                      <List.Item className="border-slate-700">
                        <div className="w-full">
                          <div className="flex justify-between items-center">
                            <Tag color={PRESET_COLORS[key]}>{key.toUpperCase()}</Tag>
                            {value.error ? (
                              <Tag color="red">错误</Tag>
                            ) : (
                              <Text className="text-slate-400">{value.total_chunks} 块</Text>
                            )}
                          </div>
                          {value.stats && (
                            <div className="text-xs text-slate-500 mt-1">
                              平均 {value.stats.avg_chunk_size} 字符/块
                            </div>
                          )}
                        </div>
                      </List.Item>
                    )}
                  />
                </Card>
              )}

              {/* 分块结果 */}
              {testResult && (
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <Text className="text-slate-300">
                      分块结果 ({testResult.stats.total_chunks} 块)
                    </Text>
                    <Space>
                      <Tag>{STRATEGY_NAMES[testResult.strategy]}</Tag>
                      <Tag color="blue">平均 {testResult.stats.avg_chunk_size} 字符</Tag>
                    </Space>
                  </div>

                  <div className="max-h-96 overflow-y-auto pr-2">
                    {testResult.chunks.slice(0, 10).map((chunk, idx) => (
                      <ChunkCard key={chunk.id} chunk={chunk} index={idx} />
                    ))}
                    {testResult.chunks.length > 10 && (
                      <div className="text-center text-slate-500 py-2">
                        还有 {testResult.chunks.length - 10} 个分块...
                      </div>
                    )}
                  </div>
                </div>
              )}

              {!testResult && !analysisResult && !comparisonResult && (
                <Empty
                  description={<Text className="text-slate-500">输入文本后点击测试按钮</Text>}
                  className="py-8"
                />
              )}
            </Card>
          </Col>
        </Row>
      </div>
    </div>
  )
}
