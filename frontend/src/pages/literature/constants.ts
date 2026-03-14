/** 文献数据源配置 */
export interface SourceInfo {
  key: string
  name: string
  icon: string
  color: string
}

export const SOURCES: SourceInfo[] = [
  { key: 'manual', name: '链接导入', icon: '🔗', color: 'cyan' },
  { key: 'multi', name: 'Multi-Source', icon: '🌐', color: 'gold' },
  { key: 'semantic_scholar', name: 'Semantic Scholar', icon: '🔬', color: 'blue' },
  { key: 'arxiv', name: 'arXiv', icon: '📄', color: 'orange' },
  { key: 'pubmed', name: 'PubMed', icon: '🏥', color: 'green' },
  { key: 'openalex', name: 'OpenAlex', icon: '📚', color: 'purple' },
  { key: 'crossref', name: 'CrossRef', icon: '🔗', color: 'cyan' },
]

export const getSourceInfo = (key: string): SourceInfo =>
  SOURCES.find(s => s.key === key) || SOURCES.find(s => s.key === 'multi') || SOURCES[0]
