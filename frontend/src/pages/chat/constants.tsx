import {
  SearchOutlined,
  GlobalOutlined,
  CalculatorOutlined,
  ClockCircleOutlined,
  FileTextOutlined,
  SwapOutlined,
} from '@ant-design/icons'

/** 工具图标映射 */
export const toolIcons: Record<string, React.ReactNode> = {
  knowledge_search: <SearchOutlined />,
  web_search: <GlobalOutlined />,
  calculator: <CalculatorOutlined />,
  datetime: <ClockCircleOutlined />,
  text_analysis: <FileTextOutlined />,
  unit_converter: <SwapOutlined />,
}

/** 工具名称映射 */
export const toolNames: Record<string, string> = {
  knowledge_search: '知识库搜索',
  web_search: '网络搜索',
  calculator: '计算器',
  datetime: '日期时间',
  text_analysis: '文本分析',
  unit_converter: '单位转换',
}
