"""
文献服务 - Semantic Scholar 和 arXiv API 集成
"""
import asyncio
from dataclasses import dataclass
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from loguru import logger

from app.config import settings


SUPPORTED_LITERATURE_SOURCES = (
    "semantic_scholar",
    "arxiv",
    "pubmed",
    "openalex",
    "crossref",
)


def _parse_source_order(raw: str, default: tuple[str, ...]) -> list[str]:
    items = [str(part or "").strip().lower() for part in str(raw or "").split(",")]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item not in SUPPORTED_LITERATURE_SOURCES or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized or list(default)


def _parse_retry_after_seconds(value: Optional[str]) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        return None


def _normalize_search_sort_key(value: Optional[str]) -> str:
    normalized = str(value or "relevance").strip().lower().replace("-", "_")
    aliases = {
        "": "relevance",
        "score": "relevance",
        "publication": "latest",
        "publication_date": "latest",
        "published": "latest",
        "pub_date": "latest",
        "date": "latest",
        "latest_publication": "latest",
        "most_cited": "citations",
        "citation": "citations",
        "citation_count": "citations",
        "cited_by_count": "citations",
        "is_referenced_by_count": "citations",
        "recently_updated": "updated",
        "last_updated": "updated",
        "submitted_date": "submitted",
        "recently_added": "recent",
        "most_recent": "recent",
    }
    return aliases.get(normalized, normalized)


def _normalize_search_sort_order(value: Optional[str]) -> str:
    normalized = str(value or "desc").strip().lower()
    if normalized in {"asc", "ascending", "1"}:
        return "asc"
    return "desc"


def _arxiv_sort_order(value: Optional[str]) -> str:
    return "ascending" if _normalize_search_sort_order(value) == "asc" else "descending"


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


class RateLimitedHttpProvider:
    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        provider_name: str,
        min_interval_seconds: float,
    ) -> None:
        self.provider_name = str(provider_name or "provider").strip() or "provider"
        self.timeout_seconds = max(
            5.0,
            float(getattr(settings, "literature_search_provider_timeout_seconds", 20) or 20),
        )
        self.retry_attempts = max(
            0,
            int(getattr(settings, "literature_search_provider_retry_attempts", 2) or 2),
        )
        self.retry_backoff_seconds = max(
            0.0,
            float(getattr(settings, "literature_search_retry_backoff_seconds", 1.0) or 1.0),
        )
        self.min_interval_seconds = max(0.0, float(min_interval_seconds or 0.0))
        self._rate_limit_lock = asyncio.Lock()
        self._next_request_after = 0.0

    async def _respect_rate_limit(self) -> None:
        async with self._rate_limit_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_after - now)
            if wait_seconds > 0:
                logger.debug(
                    f"[{self.provider_name}] rate-limit sleep {wait_seconds:.2f}s before next request"
                )
                await asyncio.sleep(wait_seconds)
                now = time.monotonic()
            self._next_request_after = now + self.min_interval_seconds

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        method: str = "GET",
        retryable_status_codes: Optional[set[int]] = None,
        **kwargs,
    ) -> httpx.Response:
        retryable_status_codes = retryable_status_codes or self.RETRYABLE_STATUS_CODES
        last_response: Optional[httpx.Response] = None

        for attempt in range(self.retry_attempts + 1):
            await self._respect_rate_limit()
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= self.retry_attempts:
                    raise
                wait_seconds = max(
                    self.retry_backoff_seconds,
                    self.retry_backoff_seconds * (2 ** attempt),
                )
                logger.warning(
                    f"[{self.provider_name}] request error: {exc}; retry in {wait_seconds:.2f}s "
                    f"({attempt + 1}/{self.retry_attempts + 1})"
                )
                await asyncio.sleep(wait_seconds)
                continue

            last_response = response
            if response.status_code not in retryable_status_codes:
                return response
            if attempt >= self.retry_attempts:
                return response

            retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
            wait_seconds = retry_after if retry_after is not None else max(
                self.retry_backoff_seconds,
                self.retry_backoff_seconds * (2 ** attempt),
            )
            logger.warning(
                f"[{self.provider_name}] upstream returned HTTP {response.status_code}; retry in "
                f"{wait_seconds:.2f}s ({attempt + 1}/{self.retry_attempts + 1})"
            )
            await asyncio.sleep(wait_seconds)

        if last_response is None:
            raise RuntimeError(f"{self.provider_name} request finished without response")
        return last_response


class SemanticScholarService(RateLimitedHttpProvider):
    """Semantic Scholar API 服务"""

    BASE_URL = "https://api.semanticscholar.org/graph/v1"
    SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"

    # API 字段
    PAPER_FIELDS = [
        "paperId", "externalIds", "title", "abstract", "venue", "year",
        "referenceCount", "citationCount", "openAccessPdf", "fieldsOfStudy",
        "publicationDate", "authors", "url"
    ]
    
    def __init__(self):
        self.api_key = (
            str(getattr(settings, "semantic_scholar_api_key", "") or os.getenv("SEMANTIC_SCHOLAR_API_KEY", ""))
            .strip()
        )
        min_interval_seconds = (
            float(getattr(settings, "literature_search_semantic_scholar_min_interval_seconds", 1.0) or 1.0)
            if self.api_key
            else float(
                getattr(
                    settings,
                    "literature_search_semantic_scholar_public_min_interval_seconds",
                    3.0,
                )
                or 3.0
            )
        )
        super().__init__(
            provider_name="semantic_scholar",
            min_interval_seconds=min_interval_seconds,
        )
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

    @staticmethod
    def _format_year_range(year_range: Optional[tuple]) -> Optional[str]:
        if not year_range:
            return None
        start, end = year_range
        start_token = str(start).strip() if start is not None else ""
        end_token = str(end).strip() if end is not None else ""
        if start_token and end_token:
            return f"{start_token}-{end_token}"
        if start_token:
            return f"{start_token}-"
        if end_token:
            return f"-{end_token}"
        return None

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        page_token: Optional[str] = None,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
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
        logger.info(f"[S2] 搜索论文(BULK): {query}, offset={offset}, token={bool(page_token)}")

        # 检查缓存
        cache_key = self._get_cache_key(
            query,
            limit=limit,
            offset=offset,
            page_token=page_token,
            year_range=year_range,
            fields=fields_of_study,
            open_access=open_access_only,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        params = {
            "query": query,
            "fields": ",".join(self.PAPER_FIELDS)
        }
        if page_token:
            params["token"] = page_token

        # 年份过滤
        year_filter = self._format_year_range(year_range)
        if year_filter:
            params["year"] = year_filter

        # 领域过滤
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)

        # 开放获取过滤
        if open_access_only:
            params["openAccessPdf"] = ""

        sort_key = _normalize_search_sort_key(sort_by)
        s2_sort_field = {
            "latest": "publicationDate",
            "citations": "citationCount",
            "paper_id": "paperId",
        }.get(sort_key)
        if s2_sort_field:
            params["sort"] = f"{s2_sort_field}:{_normalize_search_sort_order(sort_order)}"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    self.SEARCH_URL,
                    params=params,
                    headers=self.headers,
                )
                
                if response.status_code != 200:
                    logger.error(f"[S2] API 错误: {response.status_code} - {response.text[:200]}")
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                data = response.json()
                papers = [self._parse_paper(p) for p in data.get("data", [])]
                next_token = data.get("token")

                result = {
                    "total": data.get("total", len(papers)),
                    "offset": offset,
                    "next_token": next_token,
                    "has_more": bool(next_token),
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
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    url,
                    params=params,
                    headers=self.headers,
                )
                
                if response.status_code != 200:
                    logger.error(f"[S2] 获取论文失败: {response.status_code}")
                    return None
                
                return self._parse_paper(response.json())
                
        except Exception as e:
            logger.error(f"[S2] 获取论文错误: {e}")
            return None
    
    async def get_author(self, author_id: str) -> Optional[Dict[str, Any]]:
        """获取作者信息"""
        url = f"{self.BASE_URL}/author/{author_id}"
        params = {"fields": "authorId,name,affiliations,paperCount,citationCount,hIndex,papers.title,papers.year"}
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    url,
                    params=params,
                    headers=self.headers,
                )
                
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


class ArxivService(RateLimitedHttpProvider):
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

    def __init__(self):
        super().__init__(
            provider_name="arxiv",
            min_interval_seconds=float(
                getattr(settings, "literature_search_arxiv_min_interval_seconds", 3.0) or 3.0
            ),
        )
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        categories: Optional[List[str]] = None,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        sort_by: Optional[str] = "relevance",
        sort_order: Optional[str] = "descending",
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
        category_tokens = list(categories or [])
        if fields_of_study:
            for token in fields_of_study:
                normalized = str(token or "").strip()
                if normalized and normalized in self.CATEGORIES and normalized not in category_tokens:
                    category_tokens.append(normalized)
        if category_tokens:
            cat_query = " OR ".join([f"cat:{c}" for c in category_tokens])
            search_query = f"({search_query}) AND ({cat_query})"

        if year_range:
            start_year, end_year = year_range
            start_date = f"{int(start_year) if start_year is not None else 1900}01010000"
            end_date = f"{int(end_year) if end_year is not None else 3000}12312359"
            search_query = f"({search_query}) AND submittedDate:[{start_date} TO {end_date}]"

        sort_key = _normalize_search_sort_key(sort_by)
        arxiv_sort_by = {
            "updated": "lastUpdatedDate",
            "submitted": "submittedDate",
            "latest": "submittedDate",
            "relevance": "relevance",
        }.get(sort_key, "relevance")

        params = {
            "search_query": search_query,
            "start": offset,
            "max_results": limit,
            "sortBy": arxiv_sort_by,
            "sortOrder": _arxiv_sort_order(sort_order),
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(client, self.BASE_URL, params=params)
                
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
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(client, self.BASE_URL, params=params)
                
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
                except (TypeError, ValueError):
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


class PubMedService(RateLimitedHttpProvider):
    """PubMed API 服务 - 生物医学文献"""
    
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    
    def __init__(self):
        self.api_key = (
            str(getattr(settings, "pubmed_api_key", "") or os.getenv("PUBMED_API_KEY", ""))
            .strip()
        )
        min_interval_seconds = (
            float(getattr(settings, "literature_search_pubmed_api_key_min_interval_seconds", 0.12) or 0.12)
            if self.api_key
            else float(getattr(settings, "literature_search_pubmed_min_interval_seconds", 0.34) or 0.34)
        )
        super().__init__(
            provider_name="pubmed",
            min_interval_seconds=min_interval_seconds,
        )
    
    @staticmethod
    def _build_search_term(
        query: str,
        year_range: Optional[tuple] = None,
        open_access_only: bool = False,
    ) -> str:
        terms = [f"({query})"]
        if year_range:
            start_year, end_year = year_range
            if start_year is not None or end_year is not None:
                start_value = int(start_year) if start_year is not None else 1800
                end_value = int(end_year) if end_year is not None else 3000
                terms.append(f"{start_value}:{end_value}[dp]")
        if open_access_only:
            terms.append("free full text[sb]")
        return " AND ".join(terms)

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """搜索 PubMed 论文"""
        logger.info(f"[PubMed] 搜索: {query}, limit={limit}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                search_term = self._build_search_term(
                    query,
                    year_range=year_range,
                    open_access_only=open_access_only,
                )

                # 第一步：通过 usehistory 保存结果集，后续 efetch 用 WebEnv/query_key 续取。
                search_params = {
                    "db": "pubmed",
                    "term": search_term,
                    "retmax": limit,
                    "retstart": offset,
                    "retmode": "json",
                    "sort": {
                        "latest": "pub date",
                        "recent": "most recent",
                        "title": "title",
                        "author": "author",
                        "journal": "journal",
                    }.get(_normalize_search_sort_key(sort_by), "relevance"),
                    "usehistory": "y",
                }
                if self.api_key:
                    search_params["api_key"] = self.api_key
                
                search_resp = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/esearch.fcgi",
                    params=search_params,
                )
                if search_resp.status_code != 200:
                    return {"total": 0, "papers": [], "error": f"Search error: {search_resp.status_code}"}
                
                search_data = search_resp.json()
                esearch_result = search_data.get("esearchresult", {})
                id_list = esearch_result.get("idlist", [])
                total = int(esearch_result.get("count", 0))
                webenv = esearch_result.get("webenv")
                query_key = esearch_result.get("querykey")

                logger.info(f"[PubMed] 搜索ID: total={total}, 获取到{len(id_list)}个ID, offset={offset}")

                if total <= 0 or (not id_list and not (webenv and query_key)):
                    return {"total": total, "papers": [], "offset": offset, "has_more": False}

                # 第二步：获取详细信息
                fetch_params = {
                    "db": "pubmed",
                    "retmode": "xml",
                    "retstart": offset,
                    "retmax": limit,
                }
                if webenv and query_key:
                    fetch_params["WebEnv"] = webenv
                    fetch_params["query_key"] = query_key
                else:
                    fetch_params["id"] = ",".join(id_list)
                if self.api_key:
                    fetch_params["api_key"] = self.api_key
                
                fetch_resp = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/efetch.fcgi",
                    params=fetch_params,
                )
                if fetch_resp.status_code != 200:
                    return {"total": total, "papers": [], "error": "Fetch error"}
                
                papers = self._parse_pubmed_xml(fetch_resp.text)
                
                logger.info(f"[PubMed] 搜索完成: total={total}, offset={offset}, 返回={len(papers)}篇")
                
                return {
                    "total": total,
                    "offset": offset,
                    "papers": papers,
                    "has_more": offset + len(papers) < total,
                }
                
        except Exception as e:
            logger.error(f"[PubMed] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}

    async def get_paper(self, pmid: str) -> Optional[PaperResult]:
        """按 PMID 获取 PubMed 论文。"""
        normalized_pmid = re.sub(r"\D", "", str(pmid or ""))
        if not normalized_pmid:
            return None

        params = {
            "db": "pubmed",
            "id": normalized_pmid,
            "retmode": "xml",
        }
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/efetch.fcgi",
                    params=params,
                )
                if response.status_code != 200:
                    logger.error(f"[PubMed] 获取论文失败: {response.status_code}")
                    return None

                papers = self._parse_pubmed_xml(response.text)
                return papers[0] if papers else None
        except Exception as e:
            logger.error(f"[PubMed] 获取论文错误: {e}")
            return None
    
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


class OpenAlexService(RateLimitedHttpProvider):
    """OpenAlex API 服务 - 开放学术图谱"""
    
    BASE_URL = "https://api.openalex.org"
    
    def __init__(self):
        super().__init__(
            provider_name="openalex",
            min_interval_seconds=float(
                getattr(settings, "literature_search_openalex_min_interval_seconds", 0.12) or 0.12
            ),
        )
        self.email = (
            str(getattr(settings, "openalex_email", "") or os.getenv("OPENALEX_EMAIL", ""))
            .strip()
        )
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """搜索 OpenAlex 论文"""
        logger.info(f"[OpenAlex] 搜索: {query}, limit={limit}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                sort_key = _normalize_search_sort_key(sort_by)
                openalex_sort_field = {
                    "latest": "publication_date",
                    "citations": "cited_by_count",
                    "title": "display_name",
                    "relevance": "relevance_score",
                }.get(sort_key, "relevance_score")
                openalex_order = _normalize_search_sort_order(sort_order)
                params = {
                    "search": query,
                    "per_page": limit,
                    "page": (offset // limit) + 1,
                    "sort": f"{openalex_sort_field}:{openalex_order}",
                }

                if self.email:
                    params["mailto"] = self.email

                filter_parts: List[str] = []
                if year_range:
                    start_year, end_year = year_range
                    if start_year is not None:
                        filter_parts.append(f"from_publication_date:{int(start_year)}-01-01")
                    if end_year is not None:
                        filter_parts.append(f"to_publication_date:{int(end_year)}-12-31")
                    elif sort_key == "latest":
                        filter_parts.append(f"to_publication_date:{datetime.utcnow().date().isoformat()}")
                elif sort_key == "latest":
                    filter_parts.append(f"to_publication_date:{datetime.utcnow().date().isoformat()}")
                if open_access_only:
                    filter_parts.append("open_access.is_oa:true")
                if filter_parts:
                    params["filter"] = ",".join(filter_parts)

                response = await self._request_with_retry(client, f"{self.BASE_URL}/works", params=params)
                
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

    async def get_paper(self, work_id: str) -> Optional[PaperResult]:
        """按 OpenAlex Work ID 获取论文。"""
        normalized = str(work_id or "").strip().upper()
        if normalized.startswith("HTTPS://OPENALEX.ORG/"):
            normalized = normalized.rsplit("/", 1)[-1]
        if normalized.startswith("HTTPS://API.OPENALEX.ORG/WORKS/"):
            normalized = normalized.rsplit("/", 1)[-1]
        if not re.fullmatch(r"W\d+", normalized):
            return None

        params: Dict[str, Any] = {}
        if self.email:
            params["mailto"] = self.email

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/works/{normalized}",
                    params=params,
                )
                if response.status_code != 200:
                    logger.error(f"[OpenAlex] 获取论文失败: {response.status_code}")
                    return None
                return self._parse_work(response.json())
        except Exception as e:
            logger.error(f"[OpenAlex] 获取论文错误: {e}")
            return None

    async def get_paper_by_doi(self, doi: str) -> Optional[PaperResult]:
        """按 DOI 获取 OpenAlex 论文。"""
        normalized = str(doi or "").strip()
        normalized = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.IGNORECASE)
        if not normalized:
            return None

        params: Dict[str, Any] = {}
        if self.email:
            params["mailto"] = self.email

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                encoded = quote(f"https://doi.org/{normalized}", safe="")
                response = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/works/{encoded}",
                    params=params,
                )
                if response.status_code != 200:
                    logger.error(f"[OpenAlex] DOI 获取论文失败: {response.status_code}")
                    return None
                return self._parse_work(response.json())
        except Exception as e:
            logger.error(f"[OpenAlex] DOI 获取论文错误: {e}")
            return None
    
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
        ids = work.get("ids") or {}
        doi = ids.get("doi") or work.get("doi", "")
        if doi:
            doi = doi.replace("https://doi.org/", "")
        
        # PDF URL
        pdf_url = None
        oa = work.get("open_access") or {}
        if oa.get("is_oa") and oa.get("oa_url"):
            pdf_url = oa["oa_url"]
        
        # 领域
        fields = []
        for concept in work.get("concepts", [])[:5]:
            if concept.get("display_name"):
                fields.append(concept["display_name"])
        
        primary_location = work.get("primary_location") or {}
        primary_source = primary_location.get("source") or {}

        return PaperResult(
            source="openalex",
            external_id=openalex_id,
            title=work.get("title", ""),
            abstract=work.get("abstract", None),  # OpenAlex 通常不返回摘要
            authors=authors,
            year=work.get("publication_year"),
            venue=primary_source.get("display_name"),
            citation_count=work.get("cited_by_count", 0),
            reference_count=len(work.get("referenced_works", [])),
            url=primary_location.get("landing_page_url") or work.get("id"),
            pdf_url=pdf_url,
            arxiv_id=None,
            doi=doi if doi else None,
            fields_of_study=fields,
            raw_data=work
        )


class CrossRefService(RateLimitedHttpProvider):
    """CrossRef API 服务 - DOI 元数据"""
    
    BASE_URL = "https://api.crossref.org/works"
    
    def __init__(self):
        super().__init__(
            provider_name="crossref",
            min_interval_seconds=float(
                getattr(settings, "literature_search_crossref_min_interval_seconds", 0.25) or 0.25
            ),
        )
        self.email = (
            str(getattr(settings, "crossref_email", "") or os.getenv("CROSSREF_EMAIL", ""))
            .strip()
        )
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        page_token: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """搜索 CrossRef 论文"""
        logger.info(f"[CrossRef] 搜索: {query}, limit={limit}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                sort_key = _normalize_search_sort_key(sort_by)
                crossref_sort = {
                    "latest": "published",
                    "citations": "is-referenced-by-count",
                    "updated": "updated",
                    "references": "references-count",
                    "relevance": "relevance",
                }.get(sort_key, "relevance")
                params = {
                    "query": query,
                    "rows": limit,
                    "sort": crossref_sort,
                    "order": _normalize_search_sort_order(sort_order),
                }
                if page_token or offset == 0:
                    params["cursor"] = page_token or "*"
                else:
                    params["offset"] = offset

                filter_parts: List[str] = []
                if year_range:
                    start_year, end_year = year_range
                    if start_year is not None:
                        filter_parts.append(f"from-pub-date:{int(start_year)}-01-01")
                    if end_year is not None:
                        filter_parts.append(f"until-pub-date:{int(end_year)}-12-31")
                if open_access_only:
                    filter_parts.append("has-license:true")
                if filter_parts:
                    params["filter"] = ",".join(filter_parts)

                headers = {}
                if self.email:
                    headers["User-Agent"] = f"ResearchAssistant/1.0 (mailto:{self.email})"
                
                response = await self._request_with_retry(
                    client,
                    self.BASE_URL,
                    params=params,
                    headers=headers,
                )
                
                if response.status_code != 200:
                    return {"total": 0, "papers": [], "error": f"API error: {response.status_code}"}
                
                data = response.json()
                message = data.get("message", {})
                papers = [self._parse_item(item) for item in message.get("items", [])]
                next_cursor = message.get("next-cursor") if "cursor" in params else None

                return {
                    "total": message.get("total-results", 0),
                    "offset": offset,
                    "papers": papers,
                    "next_token": next_cursor,
                    "has_more": bool(next_cursor and next_cursor != page_token and papers),
                }
                
        except Exception as e:
            logger.error(f"[CrossRef] 搜索错误: {e}")
            return {"total": 0, "papers": [], "error": str(e)}

    async def get_paper_by_doi(self, doi: str) -> Optional[PaperResult]:
        """按 DOI 获取 CrossRef 元数据。"""
        normalized = str(doi or "").strip()
        normalized = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.IGNORECASE)
        if not normalized:
            return None

        headers = {}
        if self.email:
            headers["User-Agent"] = f"ResearchAssistant/1.0 (mailto:{self.email})"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await self._request_with_retry(
                    client,
                    f"{self.BASE_URL}/{quote(normalized, safe='')}",
                    headers=headers,
                )
                if response.status_code != 200:
                    logger.error(f"[CrossRef] DOI 获取论文失败: {response.status_code}")
                    return None
                message = response.json().get("message", {})
                if not isinstance(message, dict) or not message:
                    return None
                return self._parse_item(message)
        except Exception as e:
            logger.error(f"[CrossRef] DOI 获取论文错误: {e}")
            return None
    
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
        
        abstract = item.get("abstract")
        if isinstance(abstract, str) and "<" in abstract:
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        return PaperResult(
            source="crossref",
            external_id=doi,
            title=item.get("title", [""])[0] if item.get("title") else "",
            abstract=abstract,
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

    MULTI_SOURCE_PRIORITY = {
        "openalex": 4,
        "semantic_scholar": 3,
        "pubmed": 2,
        "arxiv": 1,
        "crossref": 0,
    }
    AUTO_SOURCE_ORDER = ("openalex", "semantic_scholar", "arxiv", "pubmed", "crossref")
    MULTI_SOURCE_ORDER = ("openalex", "semantic_scholar", "arxiv", "pubmed")
    
    def __init__(self):
        self.s2 = SemanticScholarService()
        self.arxiv = ArxivService()
        self.pubmed = PubMedService()
        self.openalex = OpenAlexService()
        self.crossref = CrossRefService()
        self.default_source = str(
            getattr(settings, "literature_search_default_source", "auto") or "auto"
        ).strip().lower() or "auto"
        self.auto_source_order = _parse_source_order(
            getattr(settings, "literature_search_auto_source_order", ""),
            self.AUTO_SOURCE_ORDER,
        )
        self.multi_source_order = _parse_source_order(
            getattr(settings, "literature_search_multi_source_order", ""),
            self.MULTI_SOURCE_ORDER,
        )

    def multi_source_count(self) -> int:
        return max(1, len(self.multi_source_order))
    
    async def search(
        self,
        query: str,
        source: str = "semantic_scholar",
        limit: int = 10,
        offset: int = 0,
        page_token: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """统一搜索接口"""
        normalized_source = str(source or "").strip().lower() or self.default_source
        year_range = kwargs.get("year_range")
        fields_of_study = kwargs.get("fields_of_study")
        open_access_only = bool(kwargs.get("open_access_only", False))
        sort_by = kwargs.get("sort_by")
        sort_order = kwargs.get("sort_order")
        if normalized_source == "auto":
            return await self.search_auto(query, limit=limit, offset=offset, **kwargs)
        if normalized_source == "multi":
            return await self.search_multi(
                query=query,
                limit_per_source=limit,
                offset=offset,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        if normalized_source == "arxiv":
            return await self.arxiv.search(
                query,
                limit,
                offset,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        if normalized_source == "pubmed":
            return await self.pubmed.search(
                query,
                limit,
                offset,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        if normalized_source == "openalex":
            return await self.openalex.search(
                query,
                limit,
                offset,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        if normalized_source == "crossref":
            return await self.crossref.search(
                query,
                limit,
                offset,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                page_token=page_token,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        if normalized_source == "semantic_scholar":
            return await self.s2.search(
                query,
                limit,
                offset,
                page_token=page_token,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        return {
            "total": 0,
            "offset": offset,
            "papers": [],
            "error": f"unsupported_source:{normalized_source}",
        }

    async def search_auto(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        **kwargs,
    ) -> Dict[str, Any]:
        attempted_sources: List[str] = []
        errors: Dict[str, str] = {}

        for source_name in self.auto_source_order:
            attempted_sources.append(source_name)
            result = await self.search(
                query=query,
                source=source_name,
                limit=limit,
                offset=offset,
                **kwargs,
            )
            papers = [p for p in result.get("papers", []) if isinstance(p, PaperResult)]
            if papers:
                payload = dict(result)
                payload["papers"] = papers
                payload["requested_source"] = "auto"
                payload["resolved_source"] = source_name
                payload["attempted_sources"] = list(attempted_sources)
                if errors:
                    payload["partial_errors"] = dict(errors)
                return payload
            if result.get("error"):
                errors[source_name] = str(result["error"])

        payload: Dict[str, Any] = {
            "total": 0,
            "offset": offset,
            "papers": [],
            "requested_source": "auto",
            "resolved_source": None,
            "attempted_sources": attempted_sources,
        }
        if errors:
            if len(errors) == len(attempted_sources):
                payload["error"] = "literature_search_all_failed"
            payload["partial_errors"] = errors
        return payload

    @staticmethod
    def _normalize_title(title: str) -> str:
        normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", "", normalized)
        return normalized

    @classmethod
    def _paper_dedupe_key(cls, paper: PaperResult) -> str:
        doi = (paper.doi or "").strip().lower()
        if doi:
            return f"doi:{doi}"

        arxiv_id = (paper.arxiv_id or "").strip().lower()
        if arxiv_id:
            return f"arxiv:{arxiv_id}"

        title_key = cls._normalize_title(paper.title)
        year_key = str(paper.year or 0)
        return f"title:{title_key}|year:{year_key}"

    @staticmethod
    def _pick_better_paper(current: PaperResult, candidate: PaperResult) -> PaperResult:
        current_rank = (
            1 if (current.abstract or "").strip() else 0,
            1 if (current.pdf_url or "").strip() else 0,
            1 if (current.doi or "").strip() else 0,
            int(current.citation_count or 0),
            int(current.reference_count or 0),
            int(current.year or 0),
            len(current.authors or []),
            len(current.fields_of_study or []),
            len(current.abstract or ""),
        )
        candidate_rank = (
            1 if (candidate.abstract or "").strip() else 0,
            1 if (candidate.pdf_url or "").strip() else 0,
            1 if (candidate.doi or "").strip() else 0,
            int(candidate.citation_count or 0),
            int(candidate.reference_count or 0),
            int(candidate.year or 0),
            len(candidate.authors or []),
            len(candidate.fields_of_study or []),
            len(candidate.abstract or ""),
        )
        return candidate if candidate_rank > current_rank else current

    @classmethod
    def _multi_sort_key(cls, paper: PaperResult) -> tuple:
        raw_data = paper.raw_data or {}
        source_rank = int(raw_data.get("_multi_source_rank", 10_000))
        return (
            max(0, 10_000 - source_rank),
            cls.MULTI_SOURCE_PRIORITY.get(paper.source, 0),
            int(paper.citation_count or 0),
            int(paper.year or 0),
            1 if (paper.abstract or "").strip() else 0,
            1 if (paper.pdf_url or "").strip() else 0,
        )

    async def search_multi(
        self,
        query: str,
        limit_per_source: int = 5,
        offset: int = 0,
        year_range: Optional[tuple] = None,
        fields_of_study: Optional[List[str]] = None,
        open_access_only: bool = False,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """多源并行搜索并融合去重。"""
        limit_per_source = max(1, int(limit_per_source))
        offset = max(0, int(offset))
        fetch_limit = min(max(limit_per_source + offset, limit_per_source), 100)

        tasks = {
            source_name: self.search(
                query=query,
                source=source_name,
                limit=fetch_limit,
                offset=0,
                year_range=year_range,
                fields_of_study=fields_of_study,
                open_access_only=open_access_only,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            for source_name in self.multi_source_order
        }
        settled = await asyncio.gather(*tasks.values(), return_exceptions=True)

        merged: Dict[str, PaperResult] = {}
        source_totals: Dict[str, int] = {}
        source_seen_counts: Dict[str, int] = {}
        errors: Dict[str, str] = {}

        for source_name, result in zip(tasks.keys(), settled):
            if isinstance(result, Exception):
                errors[source_name] = str(result)
                source_totals[source_name] = 0
                continue

            if not isinstance(result, dict):
                errors[source_name] = "invalid_payload"
                source_totals[source_name] = 0
                continue

            papers: List[PaperResult] = [p for p in result.get("papers", []) if isinstance(p, PaperResult)]
            source_totals[source_name] = int(result.get("total", len(papers)) or 0)
            source_seen_counts[source_name] = len(papers)
            if result.get("error"):
                errors[source_name] = str(result["error"])

            for source_rank, paper in enumerate(papers):
                raw_data = dict(paper.raw_data or {})
                raw_data["_multi_source_rank"] = int(source_rank)
                raw_data["_multi_fetch_limit"] = int(fetch_limit)
                paper.raw_data = raw_data
                key = self._paper_dedupe_key(paper)
                existing = merged.get(key)
                merged[key] = paper if existing is None else self._pick_better_paper(existing, paper)

        deduped = list(merged.values())
        deduped.sort(key=self._multi_sort_key, reverse=True)
        page = deduped[offset: offset + limit_per_source]
        has_unfetched_candidates = any(
            int(source_totals.get(source_name, 0) or 0) > int(source_seen_counts.get(source_name, 0) or 0)
            for source_name in source_totals.keys()
        )
        estimated_total = (
            max(len(deduped), sum(source_totals.values()))
            if has_unfetched_candidates
            else len(deduped)
        )
        has_more = (offset + limit_per_source) < len(deduped) or any(
            int(source_totals.get(source_name, 0) or 0) > int(source_seen_counts.get(source_name, 0) or 0)
            for source_name in source_totals.keys()
        )

        payload: Dict[str, Any] = {
            "total": estimated_total,
            "offset": offset,
            "has_more": has_more,
            "papers": page,
            "sources": source_totals,
            "requested_source": "multi",
            "resolved_source": "multi",
            "attempted_sources": list(tasks.keys()),
        }
        if errors:
            payload["partial_errors"] = errors
        return payload
    
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
    
    async def download_pdf(
        self,
        pdf_url: str,
        save_path: str
    ) -> tuple[bool, str]:
        """下载 PDF"""
        logger.info(f"[Literature] 下载 PDF: {pdf_url}")
        
        try:
            async with httpx.AsyncClient(
                timeout=60.0,
                follow_redirects=True,
                headers={"User-Agent": "ResearchAssistant/1.0"},
            ) as client:
                response = await client.get(pdf_url)

                if response.status_code != 200:
                    detail = f"PDF 下载失败，上游返回 {response.status_code}"
                    logger.error(f"[Literature] {detail}: {pdf_url}")
                    return False, detail

                content = response.content or b""
                content_type = str(response.headers.get("content-type") or "").strip().lower()
                if not content.startswith(b"%PDF-"):
                    detail = "下载链接未返回有效 PDF 文件"
                    if content_type and "pdf" not in content_type:
                        detail = f"{detail}（content-type: {content_type}）"
                    logger.error(f"[Literature] {detail}: {pdf_url}")
                    return False, detail

                with open(save_path, 'wb') as f:
                    f.write(content)
                logger.info(f"[Literature] PDF 下载成功: {save_path}")
                return True, ""
        except httpx.TimeoutException:
            detail = "PDF 下载超时，请稍后重试"
            logger.error(f"[Literature] {detail}: {pdf_url}")
            return False, detail
        except Exception as e:
            logger.error(f"[Literature] PDF 下载错误: {e}")
            return False, "PDF 下载请求失败，请稍后重试"


# 单例
_literature_service: Optional[LiteratureService] = None


def get_literature_service() -> LiteratureService:
    """获取文献服务单例"""
    global _literature_service
    if _literature_service is None:
        _literature_service = LiteratureService()
    return _literature_service
