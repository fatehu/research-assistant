import { useState, useEffect } from 'react'
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
  Collapse,
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
const { Panel } = Collapse
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
      bodyStyle={{ padding: 16 }}
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
  chunk: { content: string; metadata: { level: string; section_type?: string; has_citations: boolean } }
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
  const [customConfig, setCustomConfig] = useState<Partial<ChunkingConfig>>({
    strategy: ChunkingStrategy.HYBRID,
    // V3 Token 计量新增
    use_token_based: true,
    base_chunk_tokens: 128,        // 约 512 英文字符 / 192 中文字符
    overlap_tokens: 16,
    min_semantic_tokens: 32,
    max_semantic_tokens: 384,
    // 字符计量（旧）
    base_chunk_size: 500,
    chunk_overlap: 50,
    breakpoint_percentile: 90,
    semantic_threshold: 0.75, // 保留以兼容类型定义
    min_semantic_chunk: 100,
    max_semantic_chunk: 1500,
    enable_hierarchical: true,
    hierarchy_levels: [ChunkLevel.PARAGRAPH, ChunkLevel.SECTION],
    detect_academic_structure: true,
    preserve_citations: true,
  })

  // 加载预设列表
  useEffect(() => {
    loadPresets()
    if (kbId) {
      loadKnowledgeBase()
      loadCurrentConfig()
    }
  }, [kbId])

  const loadPresets = async () => {
    try {
      const data = await chunkingApi.getPresets()
      setPresets(data.presets)
    } catch (error) {
      console.error('Failed to load presets:', error)
      message.error('加载预设配置失败')
    }
  }

  const loadKnowledgeBase = async () => {
    if (!kbId) return
    try {
      const kb = await knowledgeApi.getKnowledgeBase(parseInt(kbId))
      setKnowledgeBase(kb)
    } catch (error) {
      console.error('Failed to load knowledge base:', error)
    }
  }

  const loadCurrentConfig = async () => {
    if (!kbId) return
    try {
      const config = await chunkingApi.getKnowledgeBaseConfig(parseInt(kbId))
      setCurrentConfig(config)
      if (config?.name) {
        setSelectedPreset(config.name)
      }
    } catch (error) {
      console.error('Failed to load config:', error)
    }
  }

  // 测试分块
  const handlePreviewChunking = async () => {
    if (!testText.trim()) {
      message.warning('请输入测试文本')
      return
    }

    setTesting(true)
    try {
      const result = await chunkingApi.previewChunking(
        testText,
        activeTab === 'custom' ? customConfig : undefined,
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
        await chunkingApi.updateKnowledgeBaseConfig(parseInt(kbId), customConfig as ChunkingConfig)
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
              <span className="text-blue-300">
                当前知识库使用的配置: <Tag color="blue">{currentConfig.name || currentConfig.strategy}</Tag>
              </span>
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
                                {key === 'fixed' && '— 按固定字符数切分，速度最快'}
                                {key === 'semantic' && '— 基于语义相似度自动切分'}
                                {key === 'hierarchical' && '— 多层级结构（段落/章节/文档）'}
                                {key === 'academic' && '— 识别论文 Abstract/Method/Results 等'}
                                {key === 'hybrid' && '— 自动选择最佳策略（推荐）'}
                              </Text>
                            </span>
                          ),
                        }))}
                      />
                    </Form.Item>

                    <Divider className="border-slate-700">基础分块参数</Divider>

                    {/* Token/字符计量模式切换 */}
                    <Form.Item label={<Text className="text-slate-300">计量模式</Text>}>
                      <Switch
                        checked={customConfig.use_token_based}
                        onChange={(v) => setCustomConfig({ ...customConfig, use_token_based: v })}
                        checkedChildren="Token"
                        unCheckedChildren="字符"
                      />
                      <Text className="text-slate-500 text-xs ml-2">
                        {customConfig.use_token_based
                          ? 'Token 模式: 自动适配中英文信息密度（推荐）'
                          : '字符模式: 按字符数切分（旧行为）'}
                      </Text>
                    </Form.Item>

                    {customConfig.use_token_based ? (
                      // Token 模式的滑块组
                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item
                            label={
                              <Tooltip title="每个分块的目标大小（Token数）。128 Tokens 约等于 500 英文字符或 200 中文字符。">
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
                              <Tooltip title="相邻分块之间的重叠大小（Token数）。">
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

                        <Col span={12}>
                          <Form.Item
                            label={
                              <Tooltip title="语义分块的最小大小（Token数）。">
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
                              <Tooltip title="语义分块的最大大小（Token数）。">
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
                    ) : (
                      // 字符模式的滑块组 (原有代码)
                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item
                            label={
                              <Tooltip title="每个分块的目标大小（字符数）。较小的值产生更精细的分块，适合精确检索；较大的值保留更多上下文。">
                                <Text className="text-slate-300">
                                  基础块大小 <QuestionCircleOutlined className="text-slate-500" />
                                </Text>
                              </Tooltip>
                            }
                          >
                            <Slider
                              min={100}
                              max={2000}
                              step={50}
                              value={customConfig.base_chunk_size}
                              onChange={(v) =>
                                setCustomConfig({ ...customConfig, base_chunk_size: v })
                              }
                              marks={{ 100: '100', 500: '500', 1000: '1K', 2000: '2K' }}
                            />
                            <Text className="text-slate-500 text-xs">当前: {customConfig.base_chunk_size} 字符</Text>
                          </Form.Item>
                        </Col>
                        <Col span={12}>
                          <Form.Item
                            label={
                              <Tooltip title="相邻分块之间的重叠字符数。重叠可以避免关键信息被截断，但会增加总分块数。">
                                <Text className="text-slate-300">
                                  块重叠大小 <QuestionCircleOutlined className="text-slate-500" />
                                </Text>
                              </Tooltip>
                            }
                          >
                            <Slider
                              min={0}
                              max={300}
                              step={10}
                              value={customConfig.chunk_overlap}
                              onChange={(v) =>
                                setCustomConfig({ ...customConfig, chunk_overlap: v })
                              }
                              marks={{ 0: '0', 50: '50', 150: '150', 300: '300' }}
                            />
                            <Text className="text-slate-500 text-xs">当前: {customConfig.chunk_overlap} 字符</Text>
                          </Form.Item>
                        </Col>
                      </Row>
                    )}

                    {/* 语义分块参数 */}
                    {(customConfig.strategy === ChunkingStrategy.SEMANTIC ||
                      customConfig.strategy === ChunkingStrategy.HYBRID ||
                      customConfig.strategy === ChunkingStrategy.ACADEMIC) && (
                        <>
                          <Divider className="border-slate-700">语义分块参数</Divider>
                          <Form.Item
                            label={
                              <Tooltip title="语义断点检测的敏感度（百分位）。值越小（如 50），检测到的断点越多，分块越细；值越大（如 95），检测到的断点越少，分块越粗（语义更聚合）。">
                                <Text className="text-slate-300">
                                  语义敏感度 (百分位) <QuestionCircleOutlined className="text-slate-500" />
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
                              marks={{ 20: '细碎', 50: '50', 75: '75', 90: '默认', 99: '聚合' }}
                            />
                          </Form.Item>
                          <Row gutter={16}>
                            <Col span={12}>
                              <Form.Item
                                label={
                                  <Tooltip title="语义分块的最小字符数。小于此值的块会被合并到相邻块中，避免产生过碎的分块。">
                                    <Text className="text-slate-300">
                                      最小语义块 (字符) <QuestionCircleOutlined className="text-slate-500" />
                                    </Text>
                                  </Tooltip>
                                }
                              >
                                <Slider
                                  min={50}
                                  max={500}
                                  step={25}
                                  value={customConfig.min_semantic_chunk}
                                  onChange={(v) =>
                                    setCustomConfig({ ...customConfig, min_semantic_chunk: v })
                                  }
                                  marks={{ 50: '50', 100: '100', 250: '250', 500: '500' }}
                                />
                                <Text className="text-slate-500 text-xs">当前: {customConfig.min_semantic_chunk} 字符</Text>
                              </Form.Item>
                            </Col>
                            <Col span={12}>
                              <Form.Item
                                label={
                                  <Tooltip title="语义分块的最大字符数。超过此值的块会被强制二次切分，防止单块过大影响检索精度。">
                                    <Text className="text-slate-300">
                                      最大语义块 (字符) <QuestionCircleOutlined className="text-slate-500" />
                                    </Text>
                                  </Tooltip>
                                }
                              >
                                <Slider
                                  min={500}
                                  max={3000}
                                  step={100}
                                  value={customConfig.max_semantic_chunk}
                                  onChange={(v) =>
                                    setCustomConfig({ ...customConfig, max_semantic_chunk: v })
                                  }
                                  marks={{ 500: '500', 1000: '1K', 1500: '默认', 2000: '2K', 3000: '3K' }}
                                />
                                <Text className="text-slate-500 text-xs">当前: {customConfig.max_semantic_chunk} 字符</Text>
                              </Form.Item>
                            </Col>
                          </Row>
                        </>
                      )}

                    {/* 层级与学术参数 */}
                    <Divider className="border-slate-700">高级选项</Divider>
                    <Row gutter={16}>
                      <Col span={8}>
                        <Form.Item
                          label={
                            <Tooltip title="生成段落→章节→文档的多级分块结构，检索时可回溯上级获取更多上下文。">
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
                      </Col>
                      <Col span={8}>
                        <Form.Item
                          label={
                            <Tooltip title="自动识别 Abstract、Introduction、Methods、Results、Conclusion 等学术论文章节。">
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
                      </Col>
                      <Col span={8}>
                        <Form.Item
                          label={
                            <Tooltip title="当分块边界落在引用 [1] 附近时，自动扩展上下文以保留完整的引用语境。">
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
                      </Col>
                    </Row>

                    {customConfig.enable_hierarchical && (
                      <Form.Item
                        label={
                          <Tooltip title="选择需要生成的层级。段落级用于精确检索，章节级用于上下文回溯，文档级用于全局摘要。">
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
