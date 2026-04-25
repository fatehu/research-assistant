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
  paper_search: '论文搜索',
  paper_research_status: '论文状态检查',
  paper_research_prepare: '论文准备',
  project_tree: '项目目录树',
  project_read_file: '读取项目文件',
  project_write_file: '写入项目文件',
  project_bash: '项目 Bash',
  project_claude: 'Claude 执行者',
  docx_generate_with_claude: 'DOCX Claude',
  paper_research_search_project_zoekt: '项目 Zoekt 搜索',
  paper_research_inspect_runtime: '探测运行环境',
  paper_research_write_execution_script: '写入执行脚本',
  paper_research_write_execution_spec: '写入执行计划',
  paper_research_launch_claude_code: '启动 Claude Code',
  paper_research_read_execution_spec: '读取执行计划',
  paper_research_start_execution: '启动执行',
  paper_research_read_execution: '读取执行结果',
  paper_research_cancel_execution: '取消执行',
}
