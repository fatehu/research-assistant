"""
文献服务 - Semantic Scholar 和 arXiv API 集成
"""
import asyncio
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from loguru import logger
import re
import os


@dataclass
class PaperResult:
    """论文搜索结果"""
    source: str
    external_id: str
    title: str
    abstract: Optional[str]
    authors: List[Dict[str, Any]]
    year: Optional[int]
    venue: Optional[str]
    citation_count: int
    reference_count: int
    url: Optional[str]
    pdf_url: Optional[str]
    arxiv_id: Optional[str]
    doi: Optional[str]
    fields_of_study: List[str]
    raw_data: Dict[str, Any]


class SemanticScholarService:
    """Semantic Scholar API 服务"""
    
    BASE_URL = "https://api.semanticscholar.org/graph/v1"
    SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
    
    # API 字段
    PAPER_FIELDS = [
        "paperId", "externalIds", "title", "abstract", "venue", "year",
        "referenceCount", "citationCount", "influentialCitationCount",
        "isOpenAccess", "openAccessPdf", "fieldsOfStudy", "publicationDate",
        "journal", "authors", "citations", "references", "url"
    ]
    
    def __init__(self):
        self.api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        self.headers = {}
        if self.api_key:
            self.headers["x-api-key"] = self.api_key
            logger.info("[S2] Semantic Scholar API Key 已配置")
        else:
            logger.warning("[S2] 未配置 SEMANTIC_SCHOLAR_API_KEY，使用公共 API（有速率限制）")
        
        # 简单的内存缓存
        self._cache = {}
        self._cache_ttl = 300  # 5分钟缓存
    
    def _get_cache_key(self, query: str, **kwargs) -> str:
        """生成缓存键"""
        import hashlib
        key_str = f"{query}_{kwargs}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def _get_cached(self, cache_key: str):
        """获取缓存"""
        import time
        if cache_key in self._cache:
            data, timestamp = self._cache[cache_key]
            if time.time() - timestamp < self._cache_ttl:
                logger.info(f"[S2] 使用缓存结果")
                return data
            else:
                del self._cache[cache_key]
        return None
    
    def _set_cache(self, cache_key: str, data):
        """设置缓存"""
        import time
        self._cache[cache_key] = (data, time.time())
        # 清理过期缓存（最多保留100条）
        if len(self._cache) > 100:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
    
    async def _request_with_retry(self, client, url: str, params: dict, max_retries: int = 3):
        """带重试的请求"""
        for attempt in range(max_retries):
            response = await client.get(url, params=params, headers=self.headers)
            
            if response.status_code == 200:
                return response
            
            if response.status_code == 429:
                # 速率限制 - 指数退避
                wait_time = (2 ** attempt) * 2  # 2, 4, 8 秒
                logger.warning(f"[S2] 速率限制，等待 {wait_time} 秒后重试 ({attempt + 1}/{max_retries})")
                await asyncio.sleep(wait_time)
                continue
            
            # 其他错误直接返回
            return response
        
        return response  # 返回最后一次响应
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False
    ) -> Dict[str, Any]:
        """
        搜索论文
        
        Args:
            query: 搜索关键词
            limit: 返回数量 (最大100)
            offset: 偏移量
            year_range: 年份范围 (start_year, end_year)
            fields_of_study: 研究领域过滤
            open_access_only: 仅开放获取
        """
        logger.info(f"[S2] 搜索论文: {query}, limit={limit}, offset={offset}")
        
        # 检查缓存
        cache_key = self._get_cache_key(query, limit=limit, offset=offset, 
                                         year_range=year_range, fields=fields_of_study,
                                         open_access=open_access_only)
        cached = self._get_cached(cache_key)
        if cached:
            return cached
        
        params = {
            "query": query,
            "limit": min(limit, 100),
            "offset": offset,
            "fields": ",".join(self.PAPER_FIELDS)
        }
        
        # 年份过滤
        if year_range:
            params["year"] = f"{year_range[0]}-{year_range[1]}"
        
        # 领域过滤
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)
        
        # 开放获取过滤
        if open_access_only:
            params["openAccessPdf"] = ""
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await self._request_with_retry(client, self.SEARCH_URL, params)
                
                if response.status_code != 200:
                    logger.error(f"[S2] API 错误: {response.status_code} - {response.text[:200]}")
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                data = response.json()
                papers = [self._parse_paper(p) for p in data.get("data", [])]
                
                result = {
                    "total": data.get("total", len(papers)),
                    "offset": data.get("offset", offset),
                    "papers": papers
                }
                
                # 缓存结果
                self._set_cache(cache_key, result)
                
                return result
                
        except Exception as e:
            logger.error(f"[S2] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}
    
    async def get_paper(self, paper_id: str) -> Optional[PaperResult]:
        """获取论文详情"""
        logger.info(f"[S2] 获取论文详情: {paper_id}")
        
        url = f"{self.BASE_URL}/paper/{paper_id}"
        params = {"fields": ",".join(self.PAPER_FIELDS)}
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=self.headers)
                
                if response.status_code != 200:
                    logger.error(f"[S2] 获取论文失败: {response.status_code}")
                    return None
                
                return self._parse_paper(response.json())
                
        except Exception as e:
            logger.error(f"[S2] 获取论文错误: {e}")
            return None
    
    async def get_citations(self, paper_id: str, limit: int = 100) -> List[PaperResult]:
        """获取引用该论文的论文"""
        logger.info(f"[S2] 获取论文引用: {paper_id}")
        
        url = f"{self.BASE_URL}/paper/{paper_id}/citations"
        params = {"fields": ",".join(self.PAPER_FIELDS), "limit": limit}
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=self.headers)
                
                if response.status_code != 200:
                    return []
                
                data = response.json()
                return [self._parse_paper(item["citingPaper"]) for item in data.get("data", [])]
                
        except Exception as e:
            logger.error(f"[S2] 获取引用错误: {e}")
            return []
    
    async def get_references(self, paper_id: str, limit: int = 100) -> List[PaperResult]:
        """获取论文的参考文献"""
        logger.info(f"[S2] 获取论文参考文献: {paper_id}")
        
        url = f"{self.BASE_URL}/paper/{paper_id}/references"
        params = {"fields": ",".join(self.PAPER_FIELDS), "limit": limit}
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=self.headers)
                
                if response.status_code != 200:
                    return []
                
                data = response.json()
                return [self._parse_paper(item["citedPaper"]) for item in data.get("data", []) if item.get("citedPaper")]
                
        except Exception as e:
            logger.error(f"[S2] 获取参考文献错误: {e}")
            return []
    
    async def get_author(self, author_id: str) -> Optional[Dict[str, Any]]:
        """获取作者信息"""
        url = f"{self.BASE_URL}/author/{author_id}"
        params = {"fields": "authorId,name,affiliations,paperCount,citationCount,hIndex,papers.title,papers.year"}
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=self.headers)
                
                if response.status_code != 200:
                    return None
                
                return response.json()
                
        except Exception as e:
            logger.error(f"[S2] 获取作者信息错误: {e}")
            return None
    
    def _parse_paper(self, data: Dict[str, Any]) -> PaperResult:
        """解析论文数据"""
        external_ids = data.get("externalIds", {}) or {}
        
        # 获取 PDF URL
        pdf_url = None
        open_access = data.get("openAccessPdf")
        if open_access and isinstance(open_access, dict):
            pdf_url = open_access.get("url")
        
        # 解析作者
        authors = []
        for a in data.get("authors", []) or []:
            authors.append({
                "name": a.get("name", ""),
                "authorId": a.get("authorId"),
                "affiliations": a.get("affiliations", [])
            })
        
        return PaperResult(
            source="semantic_scholar",
            external_id=data.get("paperId", ""),
            title=data.get("title", "Untitled"),
            abstract=data.get("abstract"),
            authors=authors,
            year=data.get("year"),
            venue=data.get("venue"),
            citation_count=data.get("citationCount", 0) or 0,
            reference_count=data.get("referenceCount", 0) or 0,
            url=data.get("url"),
            pdf_url=pdf_url,
            arxiv_id=external_ids.get("ArXiv"),
            doi=external_ids.get("DOI"),
            fields_of_study=data.get("fieldsOfStudy") or [],
            raw_data=data
        )


class ArxivService:
    """arXiv API 服务"""
    
    BASE_URL = "http://export.arxiv.org/api/query"
    
    # arXiv 分类映射
    CATEGORIES = {
        "cs.AI": "Artificial Intelligence",
        "cs.CL": "Computation and Language",
        "cs.CV": "Computer Vision",
        "cs.LG": "Machine Learning",
        "cs.NE": "Neural and Evolutionary Computing",
        "cs.IR": "Information Retrieval",
        "stat.ML": "Machine Learning (Statistics)",
        "math.OC": "Optimization and Control",
        "physics": "Physics",
        "q-bio": "Quantitative Biology",
        "q-fin": "Quantitative Finance",
    }
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        categories: Optional[List[str]] = None,
        sort_by: str = "relevance",  # relevance, lastUpdatedDate, submittedDate
        sort_order: str = "descending"
    ) -> Dict[str, Any]:
        """
        搜索 arXiv 论文
        
        Args:
            query: 搜索关键词
            limit: 返回数量
            offset: 偏移量
            categories: arXiv 分类过滤 (如 cs.AI, cs.LG)
            sort_by: 排序方式
            sort_order: 排序顺序
        """
        logger.info(f"[arXiv] 搜索论文: {query}, limit={limit}, offset={offset}")
        
        # 构建查询
        search_query = f"all:{query}"
        if categories:
            cat_query = " OR ".join([f"cat:{c}" for c in categories])
            search_query = f"({search_query}) AND ({cat_query})"
        
        params = {
            "search_query": search_query,
            "start": offset,
            "max_results": limit,
            "sortBy": sort_by,
            "sortOrder": sort_order
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(self.BASE_URL, params=params)
                
                if response.status_code != 200:
                    logger.error(f"[arXiv] API 错误: {response.status_code}")
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                papers, total = self._parse_atom_feed_with_total(response.text)
                
                logger.info(f"[arXiv] 搜索完成: total={total}, offset={offset}, 返回={len(papers)}篇")
                
                return {
                    "total": total,
                    "offset": offset,
                    "papers": papers
                }
                
        except Exception as e:
            logger.error(f"[arXiv] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}
    
    async def get_paper(self, arxiv_id: str) -> Optional[PaperResult]:
        """获取论文详情"""
        logger.info(f"[arXiv] 获取论文: {arxiv_id}")
        
        # 清理 ID
        arxiv_id = self._clean_arxiv_id(arxiv_id)
        
        params = {
            "id_list": arxiv_id,
            "max_results": 1
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(self.BASE_URL, params=params)
                
                if response.status_code != 200:
                    return None
                
                papers = self._parse_atom_feed(response.text)
                return papers[0] if papers else None
                
        except Exception as e:
            logger.error(f"[arXiv] 获取论文错误: {e}")
            return None
    
    def _clean_arxiv_id(self, arxiv_id: str) -> str:
        """清理 arXiv ID"""
        # 移除 arXiv: 前缀
        arxiv_id = re.sub(r'^arxiv:', '', arxiv_id, flags=re.IGNORECASE)
        # 移除版本号
        arxiv_id = re.sub(r'v\d+$', '', arxiv_id)
        return arxiv_id.strip()
    
    def _parse_atom_feed(self, xml_text: str) -> List[PaperResult]:
        """解析 arXiv Atom feed"""
        papers, _ = self._parse_atom_feed_with_total(xml_text)
        return papers
    
    def _parse_atom_feed_with_total(self, xml_text: str) -> tuple[List[PaperResult], int]:
        """解析 arXiv Atom feed，同时返回论文列表和总数"""
        papers = []
        total = 0
        
        # 定义命名空间
        namespaces = {
            'atom': 'http://www.w3.org/2005/Atom',
            'arxiv': 'http://arxiv.org/schemas/atom',
            'opensearch': 'http://a9.com/-/spec/opensearch/1.1/'
        }
        
        try:
            root = ET.fromstring(xml_text)
            
            # 获取总数
            total_elem = root.find('opensearch:totalResults', namespaces)
            if total_elem is not None and total_elem.text:
                total = int(total_elem.text)
            
            for entry in root.findall('atom:entry', namespaces):
                paper = self._parse_entry(entry, namespaces)
                if paper:
                    papers.append(paper)
            
            # 如果没有找到 opensearch:totalResults，使用返回的数量作为 fallback
            if total == 0:
                total = len(papers)
            
        except Exception as e:
            logger.error(f"[arXiv] 解析 XML 错误: {e}")
        
        return papers, total
    
    def _parse_entry(self, entry, namespaces) -> Optional[PaperResult]:
        """解析单个论文条目"""
        try:
            # 获取 arXiv ID
            id_elem = entry.find('atom:id', namespaces)
            if id_elem is None:
                return None
            
            arxiv_url = id_elem.text
            arxiv_id = arxiv_url.split('/abs/')[-1] if '/abs/' in arxiv_url else arxiv_url
            
            # 标题
            title_elem = entry.find('atom:title', namespaces)
            title = title_elem.text.strip().replace('\n', ' ') if title_elem is not None else "Untitled"
            
            # 摘要
            summary_elem = entry.find('atom:summary', namespaces)
            abstract = summary_elem.text.strip().replace('\n', ' ') if summary_elem is not None else None
            
            # 作者
            authors = []
            for author in entry.findall('atom:author', namespaces):
                name_elem = author.find('atom:name', namespaces)
                if name_elem is not None:
                    authors.append({
                        "name": name_elem.text,
                        "authorId": None,
                        "affiliations": []
                    })
            
            # 发布日期
            published_elem = entry.find('atom:published', namespaces)
            year = None
            if published_elem is not None:
                try:
                    year = int(published_elem.text[:4])
                except:
                    pass
            
            # 分类（作为 venue）
            categories = []
            for cat in entry.findall('atom:category', namespaces):
                term = cat.get('term')
                if term:
                    categories.append(term)
            
            primary_category = entry.find('arxiv:primary_category', namespaces)
            venue = primary_category.get('term') if primary_category is not None else (categories[0] if categories else None)
            
            # PDF 链接
            pdf_url = None
            for link in entry.findall('atom:link', namespaces):
                if link.get('title') == 'pdf':
                    pdf_url = link.get('href')
                    break
            
            # DOI
            doi_elem = entry.find('arxiv:doi', namespaces)
            doi = doi_elem.text if doi_elem is not None else None
            
            return PaperResult(
                source="arxiv",
                external_id=arxiv_id,
                title=title,
                abstract=abstract,
                authors=authors,
                year=year,
                venue=venue,
                citation_count=0,  # arXiv 不提供引用数
                reference_count=0,
                url=arxiv_url,
                pdf_url=pdf_url or f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                arxiv_id=arxiv_id,
                doi=doi,
                fields_of_study=categories,
                raw_data={
                    "arxiv_id": arxiv_id,
                    "categories": categories,
                    "published": published_elem.text if published_elem is not None else None
                }
            )
            
        except Exception as e:
            logger.error(f"[arXiv] 解析条目错误: {e}")
            return None


class PubMedService:
    """PubMed API 服务 - 生物医学文献"""
    
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    
    def __init__(self):
        self.api_key = os.getenv("PUBMED_API_KEY", "")  # 可选，提高速率限制
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0
    ) -> Dict[str, Any]:
        """搜索 PubMed 论文"""
        logger.info(f"[PubMed] 搜索: {query}, limit={limit}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                # 第一步：搜索获取 ID 列表
                search_params = {
                    "db": "pubmed",
                    "term": query,
                    "retmax": limit,
                    "retstart": offset,
                    "retmode": "json",
                    "sort": "relevance"
                }
                if self.api_key:
                    search_params["api_key"] = self.api_key
                
                search_resp = await client.get(f"{self.BASE_URL}/esearch.fcgi", params=search_params)
                if search_resp.status_code != 200:
                    return {"total": 0, "papers": [], "error": f"Search error: {search_resp.status_code}"}
                
                search_data = search_resp.json()
                id_list = search_data.get("esearchresult", {}).get("idlist", [])
                total = int(search_data.get("esearchresult", {}).get("count", 0))
                
                logger.info(f"[PubMed] 搜索ID: total={total}, 获取到{len(id_list)}个ID, offset={offset}")
                
                if not id_list:
                    return {"total": 0, "papers": [], "offset": offset}
                
                # 第二步：获取详细信息
                fetch_params = {
                    "db": "pubmed",
                    "id": ",".join(id_list),
                    "retmode": "xml"
                }
                if self.api_key:
                    fetch_params["api_key"] = self.api_key
                
                fetch_resp = await client.get(f"{self.BASE_URL}/efetch.fcgi", params=fetch_params)
                if fetch_resp.status_code != 200:
                    return {"total": total, "papers": [], "error": "Fetch error"}
                
                papers = self._parse_pubmed_xml(fetch_resp.text)
                
                logger.info(f"[PubMed] 搜索完成: total={total}, offset={offset}, 返回={len(papers)}篇")
                
                return {
                    "total": total,
                    "offset": offset,
                    "papers": papers
                }
                
        except Exception as e:
            logger.error(f"[PubMed] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}
    
    def _parse_pubmed_xml(self, xml_text: str) -> List[PaperResult]:
        """解析 PubMed XML"""
        papers = []
        try:
            root = ET.fromstring(xml_text)
            articles = root.findall(".//PubmedArticle")
            logger.info(f"[PubMed] 找到 {len(articles)} 篇文章待解析")
            
            for article in articles:
                try:
                    medline = article.find(".//MedlineCitation")
                    if medline is None:
                        continue
                    
                    pmid = medline.findtext(".//PMID", "")
                    article_elem = medline.find(".//Article")
                    if article_elem is None:
                        continue
                    
                    title = article_elem.findtext(".//ArticleTitle", "")
                    abstract_elem = article_elem.find(".//Abstract/AbstractText")
                    abstract = abstract_elem.text if abstract_elem is not None else None
                    
                    # 作者
                    authors = []
                    for author in article_elem.findall(".//Author"):
                        last = author.findtext("LastName", "")
                        first = author.findtext("ForeName", "")
                        if last:
                            authors.append({"name": f"{first} {last}".strip()})
                    
                    # 年份
                    year = None
                    pub_date = article_elem.find(".//PubDate")
                    if pub_date is not None:
                        year_text = pub_date.findtext("Year")
                        if year_text:
                            year = int(year_text)
                    
                    # 期刊
                    journal = article_elem.find(".//Journal")
                    venue = journal.findtext(".//Title", "") if journal is not None else None
                    
                    # DOI
                    doi = None
                    for eid in article.findall(".//ArticleId"):
                        if eid.get("IdType") == "doi":
                            doi = eid.text
                            break
                    
                    papers.append(PaperResult(
                        source="pubmed",
                        external_id=pmid,
                        title=title,
                        abstract=abstract,
                        authors=authors,
                        year=year,
                        venue=venue,
                        citation_count=0,  # PubMed 不直接提供引用数
                        reference_count=0,
                        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                        pdf_url=None,
                        arxiv_id=None,
                        doi=doi,
                        fields_of_study=["Medicine", "Biology"],
                        raw_data={}
                    ))
                except Exception as e:
                    logger.warning(f"[PubMed] 解析文章错误: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"[PubMed] XML 解析错误: {e}")
        
        logger.info(f"[PubMed] 成功解析 {len(papers)} 篇论文")
        return papers


class OpenAlexService:
    """OpenAlex API 服务 - 开放学术图谱"""
    
    BASE_URL = "https://api.openalex.org"
    
    def __init__(self):
        self.email = os.getenv("OPENALEX_EMAIL", "")  # 可选，提高速率限制
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        year_range: Optional[tuple] = None
    ) -> Dict[str, Any]:
        """搜索 OpenAlex 论文"""
        logger.info(f"[OpenAlex] 搜索: {query}, limit={limit}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                params = {
                    "search": query,
                    "per_page": limit,
                    "page": (offset // limit) + 1,
                    "sort": "relevance_score:desc"
                }
                
                if self.email:
                    params["mailto"] = self.email
                
                if year_range:
                    params["filter"] = f"publication_year:{year_range[0]}-{year_range[1]}"
                
                response = await client.get(f"{self.BASE_URL}/works", params=params)
                
                if response.status_code != 200:
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                data = response.json()
                papers = [self._parse_work(w) for w in data.get("results", [])]
                
                return {
                    "total": data.get("meta", {}).get("count", 0),
                    "offset": offset,
                    "papers": papers
                }
                
        except Exception as e:
            logger.error(f"[OpenAlex] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}
    
    def _parse_work(self, work: dict) -> PaperResult:
        """解析 OpenAlex 论文"""
        # 提取 OpenAlex ID
        openalex_id = work.get("id", "").replace("https://openalex.org/", "")
        
        # 作者
        authors = []
        for authorship in work.get("authorships", [])[:10]:
            author = authorship.get("author", {})
            if author.get("display_name"):
                authors.append({"name": author["display_name"]})
        
        # DOI
        doi = work.get("doi", "")
        if doi:
            doi = doi.replace("https://doi.org/", "")
        
        # PDF URL
        pdf_url = None
        oa = work.get("open_access", {})
        if oa.get("is_oa") and oa.get("oa_url"):
            pdf_url = oa["oa_url"]
        
        # 领域
        fields = []
        for concept in work.get("concepts", [])[:5]:
            if concept.get("display_name"):
                fields.append(concept["display_name"])
        
        return PaperResult(
            source="openalex",
            external_id=openalex_id,
            title=work.get("title", ""),
            abstract=work.get("abstract", None),  # OpenAlex 通常不返回摘要
            authors=authors,
            year=work.get("publication_year"),
            venue=work.get("primary_location", {}).get("source", {}).get("display_name"),
            citation_count=work.get("cited_by_count", 0),
            reference_count=len(work.get("referenced_works", [])),
            url=work.get("id"),
            pdf_url=pdf_url,
            arxiv_id=None,
            doi=doi if doi else None,
            fields_of_study=fields,
            raw_data=work
        )


class CrossRefService:
    """CrossRef API 服务 - DOI 元数据"""
    
    BASE_URL = "https://api.crossref.org/works"
    
    def __init__(self):
        self.email = os.getenv("CROSSREF_EMAIL", "")
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0
    ) -> Dict[str, Any]:
        """搜索 CrossRef 论文"""
        logger.info(f"[CrossRef] 搜索: {query}, limit={limit}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                params = {
                    "query": query,
                    "rows": limit,
                    "offset": offset,
                    "sort": "relevance"
                }
                
                headers = {}
                if self.email:
                    headers["User-Agent"] = f"ResearchAssistant/1.0 (mailto:{self.email})"
                
                response = await client.get(self.BASE_URL, params=params, headers=headers)
                
                if response.status_code != 200:
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                data = response.json()
                message = data.get("message", {})
                papers = [self._parse_item(item) for item in message.get("items", [])]
                
                return {
                    "total": message.get("total-results", 0),
                    "offset": offset,
                    "papers": papers
                }
                
        except Exception as e:
            logger.error(f"[CrossRef] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}
    
    def _parse_item(self, item: dict) -> PaperResult:
        """解析 CrossRef 条目"""
        # 作者
        authors = []
        for author in item.get("author", [])[:10]:
            name_parts = []
            if author.get("given"):
                name_parts.append(author["given"])
            if author.get("family"):
                name_parts.append(author["family"])
            if name_parts:
                authors.append({"name": " ".join(name_parts)})
        
        # 年份
        year = None
        published = item.get("published-print") or item.get("published-online") or item.get("created")
        if published and published.get("date-parts"):
            parts = published["date-parts"][0]
            if parts:
                year = parts[0]
        
        # 期刊
        venue = None
        container = item.get("container-title", [])
        if container:
            venue = container[0]
        
        # DOI
        doi = item.get("DOI", "")
        
        return PaperResult(
            source="crossref",
            external_id=doi,
            title=item.get("title", [""])[0] if item.get("title") else "",
            abstract=item.get("abstract"),
            authors=authors,
            year=year,
            venue=venue,
            citation_count=item.get("is-referenced-by-count", 0),
            reference_count=item.get("references-count", 0),
            url=item.get("URL"),
            pdf_url=None,
            arxiv_id=None,
            doi=doi,
            fields_of_study=[],
            raw_data=item
        )


class LiteratureService:
    """统一文献服务"""
    
    def __init__(self):
        self.s2 = SemanticScholarService()
        self.arxiv = ArxivService()
        self.pubmed = PubMedService()
        self.openalex = OpenAlexService()
        self.crossref = CrossRefService()
    
    async def search(
        self,
        query: str,
        source: str = "semantic_scholar",
        limit: int = 10,
        offset: int = 0,
        **kwargs
    ) -> Dict[str, Any]:
        """统一搜索接口"""
        if source == "arxiv":
            return await self.arxiv.search(query, limit, offset, **kwargs)
        elif source == "pubmed":
            return await self.pubmed.search(query, limit, offset)
        elif source == "openalex":
            return await self.openalex.search(query, limit, offset, **kwargs)
        elif source == "crossref":
            return await self.crossref.search(query, limit, offset)
        else:
            return await self.s2.search(query, limit, offset, **kwargs)
    
    async def search_all(
        self,
        query: str,
        limit_per_source: int = 5
    ) -> Dict[str, Any]:
        """同时搜索多个来源"""
        results = await asyncio.gather(
            self.s2.search(query, limit=limit_per_source),
            self.arxiv.search(query, limit=limit_per_source),
            self.pubmed.search(query, limit=limit_per_source),
            self.openalex.search(query, limit=limit_per_source),
            self.crossref.search(query, limit=limit_per_source),
            return_exceptions=True
        )
        
        all_papers = []
        for result in results:
            if isinstance(result, dict) and "papers" in result:
                all_papers.extend(result["papers"])
        
        return {
            "total": len(all_papers),
            "papers": all_papers
        }
    
    async def get_paper_network(
        self,
        paper_id: str,
        source: str = "semantic_scholar",
        depth: int = 1,
        max_per_level: int = 10
    ) -> Dict[str, Any]:
        """
        获取论文引用网络
        
        Returns:
            {
                "nodes": [{"id", "title", "year", "citations", ...}],
                "edges": [{"from", "to", "type"}]  # type: "cites" or "cited_by"
            }
        """
        if source != "semantic_scholar":
            return {"nodes": [], "edges": [], "error": "Only Semantic Scholar supports citation network"}
        
        nodes = {}
        edges = []
        
        # 获取中心论文
        center_paper = await self.s2.get_paper(paper_id)
        if not center_paper:
            return {"nodes": [], "edges": [], "error": "Paper not found"}
        
        nodes[paper_id] = {
            "id": paper_id,
            "title": center_paper.title,
            "year": center_paper.year,
            "citations": center_paper.citation_count,
            "authors": center_paper.author_names[:3] if hasattr(center_paper, 'author_names') else [a.get('name', '') for a in center_paper.authors[:3]],
            "level": 0,
            "type": "center"
        }
        
        # 获取引用和参考文献
        citations, references = await asyncio.gather(
            self.s2.get_citations(paper_id, limit=max_per_level),
            self.s2.get_references(paper_id, limit=max_per_level)
        )
        
        # 添加引用论文（引用了中心论文的）
        for paper in citations[:max_per_level]:
            if paper.external_id and paper.external_id not in nodes:
                nodes[paper.external_id] = {
                    "id": paper.external_id,
                    "title": paper.title,
                    "year": paper.year,
                    "citations": paper.citation_count,
                    "authors": [a.get('name', '') for a in paper.authors[:3]],
                    "level": 1,
                    "type": "citing"  # 引用了中心论文
                }
                edges.append({
                    "from": paper.external_id,
                    "to": paper_id,
                    "type": "cites"
                })
        
        # 添加参考文献（被中心论文引用的）
        for paper in references[:max_per_level]:
            if paper.external_id and paper.external_id not in nodes:
                nodes[paper.external_id] = {
                    "id": paper.external_id,
                    "title": paper.title,
                    "year": paper.year,
                    "citations": paper.citation_count,
                    "authors": [a.get('name', '') for a in paper.authors[:3]],
                    "level": 1,
                    "type": "referenced"  # 被中心论文引用
                }
            if paper.external_id:
                edges.append({
                    "from": paper_id,
                    "to": paper.external_id,
                    "type": "cites"
                })
        
        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "center_id": paper_id
        }
    
    async def download_pdf(
        self,
        pdf_url: str,
        save_path: str
    ) -> bool:
        """下载 PDF"""
        logger.info(f"[Literature] 下载 PDF: {pdf_url}")
        
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                response = await client.get(pdf_url)
                
                if response.status_code == 200:
                    with open(save_path, 'wb') as f:
                        f.write(response.content)
                    logger.info(f"[Literature] PDF 下载成功: {save_path}")
                    return True
                else:
                    logger.error(f"[Literature] PDF 下载失败: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"[Literature] PDF 下载错误: {e}")
            return False


# 单例
_literature_service: Optional[LiteratureService] = None


def get_literature_service() -> LiteratureService:
    """获取文献服务单例"""
    global _literature_service
    if _literature_service is None:
        _literature_service = LiteratureService()
    return _literature_service
