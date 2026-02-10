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
      className={`cursor-pointer transition-all border-2 ${
        selected
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
      {chunk.content.length} 字符
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
    base_chunk_size: 500,
    chunk_overlap: 50,
    semantic_threshold: 0.75,
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
                      <Radio.Group
                        value={customConfig.strategy}
                        onChange={(e) =>
                          setCustomConfig({ ...customConfig, strategy: e.target.value })
                        }
                        className="w-full"
                      >
                        <Space direction="vertical" className="w-full">
                          {Object.entries(STRATEGY_NAMES).map(([key, name]) => (
                            <Radio key={key} value={key} className="text-slate-300">
                              <span className="text-slate-200">{name}</span>
                            </Radio>
                          ))}
                        </Space>
                      </Radio.Group>
                    </Form.Item>

                    <Divider className="border-slate-700" />

                    {/* 基础参数 */}
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item
                          label={
                            <Tooltip title="每个分块的基础大小（字符数）">
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
                            marks={{ 100: '100', 500: '500', 1000: '1000', 2000: '2000' }}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item
                          label={
                            <Tooltip title="相邻分块之间的重叠字符数">
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
                        </Form.Item>
                      </Col>
                    </Row>

                    {/* 语义分块参数 */}
                    {(customConfig.strategy === ChunkingStrategy.SEMANTIC ||
                      customConfig.strategy === ChunkingStrategy.HYBRID) && (
                      <>
                        <Divider className="border-slate-700">语义分块参数</Divider>
                        <Form.Item
                          label={
                            <Tooltip title="低于此阈值的相似度被认为是语义边界">
                              <Text className="text-slate-300">
                                语义阈值 <QuestionCircleOutlined className="text-slate-500" />
                              </Text>
                            </Tooltip>
                          }
                        >
                          <Slider
                            min={0.5}
                            max={0.95}
                            step={0.05}
                            value={customConfig.semantic_threshold}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, semantic_threshold: v })
                            }
                            marks={{ 0.5: '0.5', 0.75: '0.75', 0.95: '0.95' }}
                          />
                        </Form.Item>
                      </>
                    )}

                    {/* 层级分块参数 */}
                    <Divider className="border-slate-700">层级与学术参数</Divider>
                    <Row gutter={16}>
                      <Col span={12}>
                        <Form.Item label={<Text className="text-slate-300">启用层级分块</Text>}>
                          <Switch
                            checked={customConfig.enable_hierarchical}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, enable_hierarchical: v })
                            }
                          />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item label={<Text className="text-slate-300">检测学术结构</Text>}>
                          <Switch
                            checked={customConfig.detect_academic_structure}
                            onChange={(v) =>
                              setCustomConfig({ ...customConfig, detect_academic_structure: v })
                            }
                          />
                        </Form.Item>
                      </Col>
                    </Row>

                    <Form.Item label={<Text className="text-slate-300">保留引用上下文</Text>}>
                      <Switch
                        checked={customConfig.preserve_citations}
                        onChange={(v) =>
                          setCustomConfig({ ...customConfig, preserve_citations: v })
                        }
                      />
                    </Form.Item>
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
                    <div className="flex items-center gap-2">
                      <Text className="text-slate-400">文档类型:</Text>
                      <Tag color={analysisResult.is_academic ? 'orange' : 'blue'}>
                        {analysisResult.is_academic ? '学术文档' : '通用文档'}
                      </Tag>
                      {analysisResult.has_citations && <Tag color="purple">含引用</Tag>}
                    </div>
                    <div className="flex items-center gap-2">
                      <Text className="text-slate-400">推荐策略:</Text>
                      <Tag color="green">{STRATEGY_NAMES[analysisResult.recommended_strategy]}</Tag>
                    </div>
                    <Text className="text-slate-500 text-sm">{analysisResult.recommended_reason}</Text>
                    
                    <Divider className="border-slate-700 my-2" />
                    
                    <Row gutter={8}>
                      <Col span={8}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">字符数</Text>}
                          value={analysisResult.document_stats.total_chars}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">句子数</Text>}
                          value={analysisResult.document_stats.total_sentences}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                      <Col span={8}>
                        <Statistic
                          title={<Text className="text-slate-500 text-xs">章节数</Text>}
                          value={analysisResult.document_stats.section_count}
                          valueStyle={{ fontSize: 14, color: '#94a3b8' }}
                        />
                      </Col>
                    </Row>
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
