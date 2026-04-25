/** 文献数据源配置 */
export interface SourceInfo {
  key: string
  name: string
  icon: string
  color: string
}

export interface SearchSortOption {
  key: string
  label: string
  description: string
}

export const SOURCES: SourceInfo[] = [
  { key: 'manual', name: '链接导入', icon: '🔗', color: 'cyan' },
  { key: 'semantic_scholar', name: 'Semantic Scholar', icon: '🔬', color: 'blue' },
  { key: 'arxiv', name: 'arXiv', icon: '📄', color: 'orange' },
  { key: 'pubmed', name: 'PubMed', icon: '🏥', color: 'green' },
  { key: 'openalex', name: 'OpenAlex', icon: '📚', color: 'purple' },
  { key: 'crossref', name: 'CrossRef', icon: '🔗', color: 'cyan' },
]

export const SEARCH_SOURCES: SourceInfo[] = SOURCES.filter((source) => source.key !== 'manual')

const COMMON_RANKED_SORTS: SearchSortOption[] = [
  { key: 'relevance', label: '相关性', description: '使用当前数据源的默认相关性排序。' },
  { key: 'latest', label: '最新发表', description: '按论文发表日期倒序返回。' },
  { key: 'citations', label: '引用最高', description: '按引用数倒序返回。' },
]

export const SEARCH_SORT_OPTIONS: Record<string, SearchSortOption[]> = {
  openalex: COMMON_RANKED_SORTS,
  semantic_scholar: COMMON_RANKED_SORTS,
  crossref: COMMON_RANKED_SORTS,
  arxiv: [
    { key: 'relevance', label: '相关性', description: '使用 arXiv 的相关性排序。' },
    { key: 'submitted', label: '最新提交', description: '按 arXiv 初次提交日期倒序返回。' },
    { key: 'updated', label: '最近更新', description: '按 arXiv 最近版本更新日期倒序返回。' },
  ],
  pubmed: [
    { key: 'relevance', label: '相关性', description: '使用 PubMed 的 Best Match 相关性排序。' },
    { key: 'latest', label: '发表日期', description: '按 PubMed 发表日期倒序返回。' },
    { key: 'recent', label: '最近收录', description: '按 PubMed 最近收录时间倒序返回。' },
  ],
}

export const getSourceInfo = (key: string): SourceInfo =>
  SOURCES.find(s => s.key === key) || SOURCES.find(s => s.key === 'openalex') || SOURCES[0]

export const getSearchSortOptions = (source: string): SearchSortOption[] =>
  SEARCH_SORT_OPTIONS[source] || SEARCH_SORT_OPTIONS.openalex
