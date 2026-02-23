"""
Agent 工具定义和执行 - 支持共享知识库搜索
"""
import json
import time
import math
import re
import asyncio
import httpx
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Callable, Type, Protocol
from dataclasses import dataclass
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, or_, and_, tuple_
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.services.embedding_service import get_embedding_service_for_model_and_dimension
from app.services.hybrid_retrieval_service import fuse_rrf, merge_rows_by_score
from app.services.query_rewrite_service import (
    QueryVariant,
    QueryRewriteResult,
    get_query_rewrite_service,
)
from app.services.reranker_service import get_reranker_service, RerankerService
from app.services.vector_search_tuning import apply_hnsw_ef_search, resolve_ef_search
from app.services.chinese_segmentation_service import segment_text_for_fts
from app.services.contextual_retrieval_service import (
    build_adjacent_lookup_keys,
    merge_adjacent_context,
    normalize_adjacent_window,
)
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)
from app.services.smart_chunking.token_utils import estimate_tokens, tokens_to_chars

# 尝试导入共享模块（可选）
try:
    from app.models.role import SharedResource, GroupMember, ResearchGroup, UserRole
    from app.models.user import User
    SHARING_ENABLED = True
except ImportError:
    SHARING_ENABLED = False


@dataclass
class EnhancedToolResult:
    """工具执行结果（增强版）。"""

    success: bool
    output: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    output_tokens_estimate: int = 0
    truncated: bool = False


ToolResult = EnhancedToolResult


class Tool:
    """工具协议（兼容旧实现）。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    parallel_safe: bool = False

    async def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError


class ToolBase(Tool, ABC):
    """增强工具基类：超时、重试、Pydantic 校验、输出 token 估算与截断。"""

    timeout_seconds: Optional[float] = None
    retry_count: Optional[int] = None
    input_model: Optional[Type[BaseModel]] = None
    output_max_tokens: Optional[int] = None

    @abstractmethod
    async def _execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def _resolve_timeout_seconds(self) -> float:
        timeout = self.timeout_seconds
        if timeout is None:
            timeout = float(getattr(settings, "tool_default_timeout_seconds", 20))
        return max(float(timeout), 1.0)

    def _resolve_retry_count(self) -> int:
        retries = self.retry_count
        if retries is None:
            retries = int(getattr(settings, "tool_default_retry_count", 1))
        return max(int(retries), 0)

    def _resolve_output_max_tokens(self) -> int:
        max_tokens = self.output_max_tokens
        if max_tokens is None:
            max_tokens = int(getattr(settings, "tool_output_max_tokens", 1200))
        return max(int(max_tokens), 64)

    @staticmethod
    def _validation_error_result(exc: ValidationError) -> ToolResult:
        contract = build_tool_error_contract(
            code="validation_error",
            message="工具参数校验失败，请检查输入格式。",
            stage="validate_input",
            detail=str(exc),
            retryable=False,
        )
        return ToolResult(
            success=False,
            output=str(contract["message"]),
            error=str(contract["code"]),
            data=merge_error_contract({"validation_errors": exc.errors()}, contract),
        )

    @staticmethod
    def _clamp_ratio(raw_ratio: float) -> float:
        return min(max(raw_ratio, 0.2), 0.9)

    def _truncate_output_if_needed(self, output: str) -> tuple[str, bool, int]:
        safe_output = str(output or "")
        est_tokens = estimate_tokens(safe_output)
        max_tokens = self._resolve_output_max_tokens()
        if est_tokens <= max_tokens:
            return safe_output, False, est_tokens

        char_budget = max(tokens_to_chars(max_tokens, safe_output), 120)
        marker = "\n\n...[TRUNCATED]...\n\n"
        ratio = self._clamp_ratio(float(getattr(settings, "tool_output_truncate_head_ratio", 0.75)))

        head_chars = max(40, int(char_budget * ratio))
        tail_budget = max(0, char_budget - head_chars - len(marker))
        if tail_budget > 0 and len(safe_output) > head_chars:
            truncated_output = f"{safe_output[:head_chars]}{marker}{safe_output[-tail_budget:]}"
        else:
            truncated_output = f"{safe_output[: max(40, char_budget - len(marker))]}{marker}"

        return truncated_output, True, estimate_tokens(truncated_output)

    def _validate_input(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if self.input_model is None:
            return kwargs
        validated = self.input_model.model_validate(kwargs)
        return validated.model_dump(exclude_none=True)

    def _with_finalized_result(
        self,
        result: ToolResult,
        *,
        started_at: float,
        retry_attempt: int,
    ) -> ToolResult:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        output, truncated, output_tokens_estimate = self._truncate_output_if_needed(result.output)
        merged_data = dict(result.data or {})
        merged_data.setdefault("retry_attempt", retry_attempt)
        if truncated:
            merged_data.setdefault("output_truncated", True)

        return ToolResult(
            success=bool(result.success),
            output=output,
            data=merged_data or None,
            error=result.error,
            execution_time_ms=elapsed_ms,
            output_tokens_estimate=output_tokens_estimate,
            truncated=bool(result.truncated or truncated),
        )

    async def execute(self, **kwargs) -> ToolResult:
        started_at = time.perf_counter()
        try:
            validated_kwargs = self._validate_input(kwargs)
        except ValidationError as exc:
            result = self._validation_error_result(exc)
            return self._with_finalized_result(result, started_at=started_at, retry_attempt=0)

        retries = self._resolve_retry_count()
        max_attempts = retries + 1
        timeout_seconds = self._resolve_timeout_seconds()
        last_result: ToolResult = ToolResult(success=False, output="工具执行失败", error="unknown_error")

        for attempt in range(1, max_attempts + 1):
            try:
                maybe_awaitable = self._execute(**validated_kwargs)
                result = await asyncio.wait_for(maybe_awaitable, timeout=timeout_seconds)
                if not isinstance(result, ToolResult):
                    raise TypeError(f"Tool returned unsupported result type: {type(result)}")
                last_result = result
                if result.success:
                    return self._with_finalized_result(
                        result,
                        started_at=started_at,
                        retry_attempt=attempt,
                    )
            except asyncio.TimeoutError:
                contract = build_tool_error_contract(
                    code="timeout",
                    message=f"工具执行超时（>{timeout_seconds:.1f}s）",
                    tool_name=self.name,
                    stage="execute",
                    retryable=(attempt < max_attempts),
                    metadata={
                        "timeout_seconds": timeout_seconds,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                last_result = ToolResult(
                    success=False,
                    output=str(contract["message"]),
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )
            except Exception as exc:
                contract = build_tool_error_contract(
                    code="tool_execution_exception",
                    message="工具执行失败",
                    tool_name=self.name,
                    stage="execute",
                    detail=str(exc),
                    retryable=(attempt < max_attempts),
                    metadata={
                        "exception_type": type(exc).__name__,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                    },
                )
                last_result = ToolResult(
                    success=False,
                    output=f"{contract['message']}: {exc}",
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )

            if attempt < max_attempts:
                logger.warning(
                    f"[ToolBase] retry tool={self.name}, attempt={attempt}/{max_attempts}, error={last_result.error}"
                )

        return self._with_finalized_result(
            last_result,
            started_at=started_at,
            retry_attempt=max_attempts,
        )


@dataclass
class ToolDependencyContext:
    db: Optional[AsyncSession]
    db_session_factory: Optional[Callable[[], AsyncSession]]
    user_id: Optional[int]
    notebook_id: Optional[str] = None
    kernel_manager: Any = None
    notebooks_store: Optional[dict] = None
    user_authorized: bool = False


class ToolProvider(Protocol):
    def build_default_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        ...

    def build_notebook_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        ...


class MCPRemoteTool(Tool):
    """Adapter: expose MCP remote tools through local Tool protocol."""

    def __init__(self, schema: Any, mcp_client_manager: Any):
        self.schema = schema
        self.mcp_client_manager = mcp_client_manager
        self.name = str(schema.qualified_name)
        self.description = str(schema.description or f"MCP 远程工具（{schema.server_name}.{schema.tool_name}）")
        self.parameters = schema.input_schema or {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs) -> ToolResult:
        result = await self.mcp_client_manager.call_tool(self.name, kwargs)
        return ToolResult(
            success=bool(result.success),
            output=str(result.output),
            data=result.data if isinstance(result.data, dict) else {"raw": result.data},
            error=result.error,
        )


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


class WebSearchTool(ToolBase):
    """Web 搜索工具 - Serper -> Tavily -> DDGS。"""

    name = "web_search"
    parallel_safe = True
    description = "搜索互联网获取最新信息。当用户问题涉及新闻、实时信息、天气、或需要网络查询时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认5，最大10",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    input_model = WebSearchInput
    timeout_seconds = 15
    retry_count = 0

    def __init__(self):
        self.serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
        self.tavily_api_key = (
            str(getattr(settings, "tavily_api_key", "") or os.getenv("TAVILY_API_KEY", ""))
            .strip()
        )
        if self.serper_api_key:
            logger.info(f"[WebSearch] Serper API key 已配置 (长度: {len(self.serper_api_key)})")
        else:
            logger.warning("[WebSearch] 未配置 SERPER_API_KEY")
        if self.tavily_api_key:
            logger.info(f"[WebSearch] Tavily API key 已配置 (长度: {len(self.tavily_api_key)})")
        else:
            logger.warning("[WebSearch] 未配置 TAVILY_API_KEY")

    async def _execute(self, query: str, max_results: int = 5) -> ToolResult:
        errors: List[str] = []
        logger.info(f"[WebSearch] query={query}, max_results={max_results}")

        if self.serper_api_key:
            result = await self._safe_provider_call("serper", self._serper_search, query, max_results)
            if result.success:
                return result
            errors.append(f"serper:{result.error or 'failed'}")

        if self.tavily_api_key:
            result = await self._safe_provider_call("tavily", self._tavily_search, query, max_results)
            if result.success:
                return result
            errors.append(f"tavily:{result.error or 'failed'}")

        result = await self._ddgs_search(query, max_results)
        if result.success:
            return result
        errors.append(f"ddgs:{result.error or 'failed'}")

        return ToolResult(
            success=False,
            output=f"网络搜索失败，已尝试 Serper/Tavily/DDGS。错误: {'; '.join(errors)}",
            error="web_search_all_failed",
        )

    async def _safe_provider_call(
        self,
        provider_name: str,
        provider_fn: Callable[[str, int], Any],
        query: str,
        max_results: int,
    ) -> ToolResult:
        try:
            return await provider_fn(query, max_results)
        except httpx.RequestError as exc:
            logger.warning(f"[WebSearch] {provider_name} request error: {exc}")
            return ToolResult(
                success=False,
                output=f"{provider_name} 请求失败: {exc}",
                error=f"{provider_name}_request_error",
            )
        except Exception as exc:  # pragma: no cover - 防御性保护 fallback 链路
            logger.exception(f"[WebSearch] {provider_name} unexpected error: {exc}")
            return ToolResult(
                success=False,
                output=f"{provider_name} 执行异常: {exc}",
                error=f"{provider_name}_exception",
            )

    async def _serper_search(self, query: str, max_results: int) -> ToolResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": self.serper_api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "q": query,
                    "num": max_results,
                    "gl": "cn",
                    "hl": "zh-cn",
                },
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    output=f"Serper API 请求失败: HTTP {response.status_code}",
                    error=f"serper_http_{response.status_code}",
                )

            data = response.json()
            results: List[Dict[str, Any]] = []

            if "knowledgeGraph" in data:
                kg = data["knowledgeGraph"]
                results.append(
                    {
                        "type": "knowledge_graph",
                        "title": kg.get("title", ""),
                        "description": kg.get("description", ""),
                        "attributes": kg.get("attributes", {}),
                    }
                )

            if "answerBox" in data:
                ab = data["answerBox"]
                answer = ab.get("answer") or ab.get("snippet") or ab.get("title", "")
                if answer:
                    results.append(
                        {
                            "type": "answer_box",
                            "answer": answer,
                            "source": ab.get("link", ""),
                        }
                    )

            for item in data.get("organic", [])[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", ""),
                        "date": item.get("date", ""),
                    }
                )

            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data={"results": results, "query": query, "provider": "serper"},
            )

    async def _tavily_search(self, query: str, max_results: int) -> ToolResult:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.tavily_api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    output=f"Tavily API 请求失败: HTTP {response.status_code}",
                    error=f"tavily_http_{response.status_code}",
                )

            payload = response.json()
            results: List[Dict[str, Any]] = []
            for item in payload.get("results", [])[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("content", ""),
                    }
                )

            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data={"results": results, "query": query, "provider": "tavily"},
            )

    async def _ddgs_search(self, query: str, max_results: int) -> ToolResult:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return ToolResult(
                success=False,
                output="duckduckgo-search 未安装，无法执行 DDGS 兜底搜索。",
                error="ddgs_not_installed",
            )

        def _search_sync() -> List[Dict[str, Any]]:
            with DDGS() as ddgs:
                rows = ddgs.text(query, max_results=max_results)
                return list(rows)

        try:
            rows = await asyncio.to_thread(_search_sync)
            results: List[Dict[str, Any]] = []
            for item in rows[:max_results]:
                results.append(
                    {
                        "type": "organic",
                        "title": item.get("title", ""),
                        "url": item.get("href", ""),
                        "snippet": item.get("body", ""),
                    }
                )
            return ToolResult(
                success=True,
                output=self._format_results(query, results),
                data={"results": results, "query": query, "provider": "ddgs"},
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output=f"DDGS 搜索失败: {exc}",
                error="ddgs_failed",
            )

    def _format_results(self, query: str, results: List[Dict[str, Any]]) -> str:
        if not results:
            return f"未找到关于 '{query}' 的搜索结果。"

        parts = [f"搜索 '{query}' 的结果："]
        organic_index = 0
        for item in results:
            result_type = item.get("type", "organic")
            if result_type == "knowledge_graph":
                parts.append(f"\n[知识卡片] {item.get('title', '')}")
                if item.get("description"):
                    parts.append(f"\n{item['description']}")
                for key, value in list((item.get("attributes") or {}).items())[:3]:
                    parts.append(f"\n- {key}: {value}")
                continue

            if result_type == "answer_box":
                parts.append(f"\n[直接答案] {item.get('answer', '')}")
                if item.get("source"):
                    parts.append(f"\n来源: {item.get('source', '')}")
                continue

            organic_index += 1
            parts.append(f"\n\n[结果{organic_index}] {item.get('title', '')}")
            if item.get("url"):
                parts.append(f"\n链接: {item['url']}")
            if item.get("snippet"):
                parts.append(f"\n摘要: {item['snippet']}")

        return "".join(parts)


class KnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    include_adjacent_chunks: bool = False
    adjacent_window: int = Field(default=1, ge=1, le=3)


@dataclass
class KnowledgeRetrieveRuntime:
    use_reranker: bool
    use_hybrid: bool
    final_top_k: int
    distance_threshold: float
    reranker_candidate_k: int
    vector_top_k: int
    text_top_k: int
    fusion_limit: int


@dataclass
class KnowledgeRetrieveState:
    rewrite_result: QueryRewriteResult
    runtime: KnowledgeRetrieveRuntime
    fused_candidates: List[Any]
    vector_rows: List[Any]
    text_rows: List[Any]
    retrieval_dimensions: Set[int]
    resolved_ef_search: int
    total_chunks: int
    ef_search_debug: List[Dict[str, int]]


class KnowledgeSearchTool(ToolBase):
    """知识库搜索工具 - 使用 pgvector 进行向量检索"""
    name = "knowledge_search"
    parallel_safe = True
    description = "搜索用户的知识库，检索与查询相关的文档片段。当用户问题涉及他们上传的文档、论文、资料时使用此工具。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询内容，应该是与问题相关的关键词或短语"
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            },
            "include_adjacent_chunks": {
                "type": "boolean",
                "description": "是否返回命中 chunk 的相邻上下文",
                "default": False
            },
            "adjacent_window": {
                "type": "integer",
                "description": "相邻窗口大小（1-3）",
                "default": 1
            }
        },
        "required": ["query"]
    }
    input_model = KnowledgeSearchInput
    retry_count = 0
    
    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = user_id
        self.db_session_factory = db_session_factory
        self.query_rewrite_service = get_query_rewrite_service()
    
    def _resolve_timeout_seconds(self) -> float:
        primary_timeout = float(getattr(settings, "search_timeout_primary_ms", 300000)) / 1000.0
        return max(primary_timeout, super()._resolve_timeout_seconds())

    async def _execute(
        self,
        query: str,
        top_k: int = 5,
        include_adjacent_chunks: bool = False,
        adjacent_window: int = 1,
    ) -> ToolResult:
        """执行知识库搜索（自动选择会话策略）"""
        if self.db is not None:
            return await self._execute_with_db(
                self.db,
                query,
                top_k,
                include_adjacent_chunks=include_adjacent_chunks,
                adjacent_window=adjacent_window,
            )

        if self.db_session_factory is None:
            return ToolResult(
                success=False,
                output="知识库搜索不可用：数据库会话未初始化",
                error="db_session_unavailable",
            )

        try:
            async with self.db_session_factory() as db:
                return await self._execute_with_db(
                    db,
                    query,
                    top_k,
                    include_adjacent_chunks=include_adjacent_chunks,
                    adjacent_window=adjacent_window,
                )
        except Exception as e:
            logger.error(f"知识库搜索失败（短会话模式）: {e}")
            return ToolResult(
                success=False,
                output=f"搜索过程中发生错误: {str(e)}",
                error=str(e)
            )

    async def _execute_with_db(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
        include_adjacent_chunks: bool = False,
        adjacent_window: int = 1,
    ) -> ToolResult:
        """执行知识库搜索 - 使用 pgvector 原生向量搜索，支持共享知识库"""
        try:
            start_time = time.time()
            runtime = self._resolve_retrieve_runtime(top_k)

            # 1) Rewrite
            rewrite_result = await self._rewrite(query)

            # 2) Retrieve
            retrieve_payload = await self._retrieve(db, query, rewrite_result, runtime)
            if isinstance(retrieve_payload, ToolResult):
                return retrieve_payload

            # 3) Rerank
            selected_candidates = await self._rerank(query, retrieve_payload)

            # 4) Compress
            results = await self._compress(
                db=db,
                state=retrieve_payload,
                selected_candidates=selected_candidates,
                include_adjacent_chunks=include_adjacent_chunks,
                adjacent_window=adjacent_window,
            )

            search_time = (time.time() - start_time) * 1000
            output = self._format_retrieval_output(results, search_time)
            self._log_retrieval_metrics(
                query=query,
                search_time=search_time,
                state=retrieve_payload,
                result_count=len(results),
            )

            return ToolResult(
                success=True,
                output=output,
                data={"results": results, "total": len(results), "search_time_ms": search_time},
            )

        except Exception as e:
            logger.error(f"知识库搜索失败: {e}")
            return ToolResult(
                success=False,
                output=f"搜索过程中发生错误: {str(e)}",
                error=str(e),
            )

    def _resolve_retrieve_runtime(self, top_k: int) -> KnowledgeRetrieveRuntime:
        use_reranker = bool(settings.enable_reranker)
        use_hybrid = bool(settings.enable_hybrid_retrieval)
        final_top_k = max(int(top_k), 1)
        score_threshold = max(
            0.0,
            min(float(settings.agent_knowledge_score_threshold), 1.0),
        )
        distance_threshold = 1 - score_threshold
        reranker_candidate_k = (
            max(final_top_k, int(settings.reranker_top_k))
            if use_reranker
            else final_top_k
        )
        vector_top_k = max(
            reranker_candidate_k,
            int(settings.hybrid_vector_top_k) if use_hybrid else 0,
        )
        text_top_k = (
            max(reranker_candidate_k, int(settings.hybrid_text_top_k))
            if use_hybrid
            else 0
        )
        return KnowledgeRetrieveRuntime(
            use_reranker=use_reranker,
            use_hybrid=use_hybrid,
            final_top_k=final_top_k,
            distance_threshold=distance_threshold,
            reranker_candidate_k=reranker_candidate_k,
            vector_top_k=vector_top_k,
            text_top_k=text_top_k,
            fusion_limit=reranker_candidate_k,
        )

    async def _rewrite(self, query: str) -> QueryRewriteResult:
        return await self.query_rewrite_service.rewrite_query(
            query,
            rewrite_mode="auto",
            use_query_rewrite=True,
        )

    async def _resolve_kb_ids(self, db: AsyncSession) -> Set[int]:
        kb_query = select(KnowledgeBase.id).where(KnowledgeBase.user_id == self.user_id)
        kb_result = await db.execute(kb_query)
        kb_ids = set(row[0] for row in kb_result.fetchall())
        shared_kb_ids = await self._get_shared_kb_ids(db)
        return kb_ids | shared_kb_ids

    async def _retrieve(
        self,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        runtime: KnowledgeRetrieveRuntime,
    ) -> ToolResult | KnowledgeRetrieveState:
        kb_ids = await self._resolve_kb_ids(db)
        if not kb_ids:
            return ToolResult(
                success=True,
                output="用户没有创建任何知识库，也没有收到共享的知识库，无法搜索相关内容。建议用户先上传文档到知识库，或请导师共享知识库。",
                data={"results": [], "total": 0},
            )

        kb_id_list = list(kb_ids)
        vector_rows, retrieval_dimensions, total_chunks, resolved_ef_search, ef_search_debug = await self._retrieve_vector_rows(
            db=db,
            query=query,
            rewrite_result=rewrite_result,
            kb_ids=kb_id_list,
            runtime=runtime,
        )
        text_rows = await self._retrieve_text_rows(
            db=db,
            query=query,
            rewrite_result=rewrite_result,
            kb_ids=kb_id_list,
            runtime=runtime,
        )

        fused_candidates = fuse_rrf(
            vector_rows=vector_rows,
            text_rows=text_rows if runtime.use_hybrid else [],
            rrf_k=settings.hybrid_rrf_k,
            limit=runtime.fusion_limit,
        )
        if not fused_candidates:
            return ToolResult(
                success=True,
                output="未找到与查询相关的内容。可能知识库中没有相关信息，或者需要调整搜索关键词。",
                data={"results": [], "total": 0},
            )

        return KnowledgeRetrieveState(
            rewrite_result=rewrite_result,
            runtime=runtime,
            fused_candidates=fused_candidates,
            vector_rows=vector_rows,
            text_rows=text_rows,
            retrieval_dimensions=retrieval_dimensions,
            resolved_ef_search=resolved_ef_search,
            total_chunks=total_chunks,
            ef_search_debug=ef_search_debug,
        )

    async def _retrieve_vector_rows(
        self,
        *,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        kb_ids: List[int],
        runtime: KnowledgeRetrieveRuntime,
    ) -> tuple[list[Any], set[int], int, int, list[dict[str, int]]]:
        vector_groups_sql = text(
            """
            SELECT
                COALESCE(NULLIF(embedding_model, ''), :default_embedding_model) AS embedding_model,
                embedding_dimension,
                COUNT(*) AS chunk_count
            FROM document_chunks
            WHERE knowledge_base_id = ANY(:kb_ids)
                AND embedding IS NOT NULL
                AND embedding_dimension IS NOT NULL
            GROUP BY COALESCE(NULLIF(embedding_model, ''), :default_embedding_model), embedding_dimension
            ORDER BY chunk_count DESC
            """
        )
        vector_groups = (
            await db.execute(
                vector_groups_sql,
                {
                    "kb_ids": kb_ids,
                    "default_embedding_model": settings.local_embedding_model,
                },
            )
        ).fetchall()

        vector_group_rows: list[tuple[str, str, list[Any]]] = []
        vector_variants = rewrite_result.vector_variants or [QueryVariant(text=query, strategy="original")]
        total_chunks = 0
        resolved_ef_search = int(settings.pgvector_hnsw_ef_search)
        retrieval_dimensions: set[int] = set()
        ef_search_debug: list[dict[str, int]] = []

        for group in vector_groups:
            group_model = str(
                getattr(group, "embedding_model", "") or settings.local_embedding_model
            ).strip()
            group_dimension = int(getattr(group, "embedding_dimension", 0) or 0)
            group_chunks = int(getattr(group, "chunk_count", 0) or 0)
            if group_dimension <= 0:
                continue

            retrieval_dimensions.add(group_dimension)
            total_chunks += group_chunks

            group_embedding_service = get_embedding_service_for_model_and_dimension(
                group_model,
                group_dimension,
            )
            vector_texts = [variant.text for variant in vector_variants]
            vector_embeddings: list[list[float]] = []
            try:
                vector_embeddings = await group_embedding_service.embed_texts(
                    vector_texts,
                    is_query=True,
                )
                if len(vector_embeddings) != len(vector_texts):
                    raise ValueError(
                        f"embedding count mismatch: {len(vector_embeddings)} vs {len(vector_texts)}"
                    )
            except Exception as e:
                logger.warning(
                    f"[KnowledgeSearch] Batch embedding failed for model={group_model}, "
                    f"dim={group_dimension}: {e}"
                )
                vector_embeddings = []
                for variant in vector_variants:
                    try:
                        emb = await group_embedding_service.embed_text(
                            variant.text,
                            is_query=True,
                        )
                    except Exception as single_exc:
                        logger.warning(
                            f"[KnowledgeSearch] Single embedding failed for strategy={variant.strategy}, "
                            f"model={group_model}, dim={group_dimension}: {single_exc}"
                        )
                        emb = []
                    vector_embeddings.append(emb)

            distance_expr = (
                f"(dc.embedding::vector({group_dimension}) <=> "
                f"(:query_vector)::vector({group_dimension}))"
            )
            vector_sql = text(
                f"""
                SELECT
                    dc.id,
                    dc.document_id,
                    dc.knowledge_base_id,
                    dc.content,
                    dc.chunk_index,
                    dc.embedding_model,
                    dc.embedding_dimension,
                    1 - {distance_expr} AS similarity,
                    NULL::float AS text_score,
                    d.original_filename AS document_name,
                    kb.name AS knowledge_base_name
                FROM document_chunks dc
                JOIN documents d ON dc.document_id = d.id
                JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
                WHERE dc.knowledge_base_id = ANY(:kb_ids)
                    AND dc.embedding IS NOT NULL
                    AND dc.embedding_dimension = :vector_dimension
                    AND {distance_expr} <= :distance_threshold
                ORDER BY {distance_expr}
                LIMIT :vector_top_k
                """
            )

            resolved_group_ef = resolve_ef_search(
                total_chunks=group_chunks,
                dimension=group_dimension,
            )
            await apply_hnsw_ef_search(
                db,
                resolved_group_ef,
                source=f"knowledge_search_tool.dim{group_dimension}",
            )
            resolved_ef_search = max(resolved_ef_search, resolved_group_ef)
            ef_search_debug.append(
                {
                    "dimension": group_dimension,
                    "chunks": group_chunks,
                    "ef_search": resolved_group_ef,
                }
            )

            for idx, variant in enumerate(vector_variants):
                query_embedding = vector_embeddings[idx] if idx < len(vector_embeddings) else []
                if not query_embedding or len(query_embedding) != group_dimension:
                    continue

                vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
                result = await db.execute(
                    vector_sql,
                    {
                        "query_vector": vector_str,
                        "distance_threshold": runtime.distance_threshold,
                        "kb_ids": kb_ids,
                        "vector_dimension": group_dimension,
                        "vector_top_k": runtime.vector_top_k,
                    },
                )
                rows = result.fetchall()
                if rows:
                    vector_group_rows.append((variant.strategy, variant.text, rows))

        vector_rows = merge_rows_by_score(
            vector_group_rows,
            score_attr="similarity",
            query_attr="matched_vector_query",
            strategy_attr="matched_vector_strategy",
            limit=runtime.vector_top_k,
        )
        return vector_rows, retrieval_dimensions, total_chunks, resolved_ef_search, ef_search_debug

    async def _retrieve_text_rows(
        self,
        *,
        db: AsyncSession,
        query: str,
        rewrite_result: QueryRewriteResult,
        kb_ids: List[int],
        runtime: KnowledgeRetrieveRuntime,
    ) -> list[Any]:
        if not runtime.use_hybrid:
            return []

        text_variants = rewrite_result.text_variants or [QueryVariant(text=query, strategy="original")]
        text_sql = text(
            """
            SELECT
                dc.id,
                dc.document_id,
                dc.knowledge_base_id,
                dc.content,
                dc.chunk_index,
                NULL::float as similarity,
                ts_rank_cd(
                    to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)),
                    websearch_to_tsquery('simple', :fts_query)
                ) as text_score,
                d.original_filename as document_name,
                kb.name as knowledge_base_name
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
            WHERE dc.knowledge_base_id = ANY(:kb_ids)
                AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) IS NOT NULL
                AND COALESCE(NULLIF(dc.content_segmented, ''), dc.content) <> ''
                AND to_tsvector('simple', COALESCE(NULLIF(dc.content_segmented, ''), dc.content)) @@ websearch_to_tsquery('simple', :fts_query)
            ORDER BY text_score DESC
            LIMIT :text_top_k
            """
        )
        text_group_rows: list[tuple[str, str, list[Any]]] = []
        for variant in text_variants:
            if not variant.text.strip():
                continue
            fts_query = segment_text_for_fts(variant.text)
            if not fts_query.strip():
                continue
            try:
                text_result = await db.execute(
                    text_sql,
                    {
                        "fts_query": fts_query,
                        "kb_ids": kb_ids,
                        "text_top_k": runtime.text_top_k,
                    },
                )
                rows = text_result.fetchall()
                if rows:
                    text_group_rows.append((variant.strategy, variant.text, rows))
            except Exception as e:
                logger.warning(
                    f"[KnowledgeSearch] Full-text query failed for "
                    f"strategy={variant.strategy}: {e}"
                )

        return merge_rows_by_score(
            text_group_rows,
            score_attr="text_score",
            query_attr="matched_text_query",
            strategy_attr="matched_text_strategy",
            limit=runtime.text_top_k,
        )

    async def _rerank(
        self,
        query: str,
        state: KnowledgeRetrieveState,
    ) -> List[tuple[Any, Optional[float]]]:
        selected_candidates: list[tuple[Any, Optional[float]]] = []
        if state.runtime.use_reranker:
            try:
                reranker = get_reranker_service()
                reranked = await reranker.rerank(
                    query=query,
                    documents=[candidate.row.content for candidate in state.fused_candidates],
                    top_k=state.runtime.final_top_k,
                )
                selected_candidates = [
                    (state.fused_candidates[idx], score)
                    for idx, score in reranked
                    if 0 <= idx < len(state.fused_candidates)
                ]
            except Exception as e:
                logger.warning(f"[KnowledgeSearch] Reranker failed, fallback to retrieval ranking: {e}")

        if not selected_candidates:
            selected_candidates = [
                (candidate, None)
                for candidate in state.fused_candidates[: state.runtime.final_top_k]
            ]
        return selected_candidates

    async def _compress(
        self,
        *,
        db: AsyncSession,
        state: KnowledgeRetrieveState,
        selected_candidates: List[tuple[Any, Optional[float]]],
        include_adjacent_chunks: bool,
        adjacent_window: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        adjacent_targets: list[tuple[int, int, int]] = []
        max_rrf_score = max((c.rrf_score for c in state.fused_candidates), default=0.0)
        use_hybrid = state.runtime.use_hybrid
        rewrite_result = state.rewrite_result
        retrieval_dimensions = state.retrieval_dimensions

        for candidate, reranker_score in selected_candidates:
            row = candidate.row
            vector_score = (
                round(float(candidate.vector_score), 4)
                if candidate.vector_score is not None
                else None
            )
            text_score = (
                round(float(candidate.text_score), 4)
                if candidate.text_score is not None
                else None
            )

            if reranker_score is not None:
                score = round(RerankerService.normalize_score(float(reranker_score)), 4)
            elif use_hybrid and max_rrf_score > 0:
                score = round(candidate.rrf_score / max_rrf_score, 4)
            elif vector_score is not None:
                score = vector_score
            else:
                score = 0.0

            retrieval_dimension = int(getattr(row, "embedding_dimension", 0) or 0)
            if retrieval_dimension <= 0 and retrieval_dimensions:
                retrieval_dimension = int(sorted(retrieval_dimensions)[0])

            result_item = {
                "content": row.content,
                "score": score,
                "document": row.document_name or "未知",
                "knowledge_base": row.knowledge_base_name or "未知",
                "document_id": row.document_id,
                "chunk_id": row.id,
                "chunk_index": row.chunk_index,
                "retrieval_mode": "hybrid" if use_hybrid else "vector",
                "query_rewrite_enabled": rewrite_result.enabled,
                "query_rewrite_strategies": rewrite_result.strategies,
                "query_rewrite_fallback": rewrite_result.fallback_reason,
                "query_rewrite_cache_hit": rewrite_result.cache_hit,
                "query_rewrite_skip_reason": rewrite_result.skip_reason,
                "query_rewrite_llm_called": rewrite_result.llm_called,
                "matched_vector_query": getattr(row, "matched_vector_query", None),
                "matched_vector_strategy": getattr(row, "matched_vector_strategy", None),
                "matched_text_query": getattr(row, "matched_text_query", None),
                "matched_text_strategy": getattr(row, "matched_text_strategy", None),
                "vector_rank": candidate.vector_rank,
                "text_rank": candidate.text_rank,
                "rrf_score": round(float(candidate.rrf_score), 6),
                "vector_score": vector_score,
                "text_score": text_score,
                "reranker_score": round(float(reranker_score), 4) if reranker_score is not None else None,
                "retrieval_dimension": retrieval_dimension,
                "retrieval_embedding_model": getattr(row, "embedding_model", None),
            }
            results.append(result_item)

            chunk_index = int(getattr(row, "chunk_index", -1) or -1)
            if include_adjacent_chunks and chunk_index >= 0:
                adjacent_targets.append((len(results) - 1, int(row.document_id), chunk_index))

        if include_adjacent_chunks and adjacent_targets:
            window = normalize_adjacent_window(adjacent_window)
            neighbor_keys: set[tuple[int, int]] = set()
            for _, doc_id, chunk_index in adjacent_targets:
                neighbor_keys.update(build_adjacent_lookup_keys(doc_id, chunk_index, window))

            adjacent_map: dict[tuple[int, int], Any] = {}
            if neighbor_keys:
                adjacent_result = await db.execute(
                    select(
                        DocumentChunk.id,
                        DocumentChunk.document_id,
                        DocumentChunk.chunk_index,
                        DocumentChunk.chunk_level,
                        DocumentChunk.section_title,
                        DocumentChunk.content,
                    ).where(tuple_(DocumentChunk.document_id, DocumentChunk.chunk_index).in_(list(neighbor_keys)))
                )
                adjacent_map = {
                    (int(row.document_id), int(row.chunk_index)): row
                    for row in adjacent_result.fetchall()
                }

            for idx, doc_id, chunk_index in adjacent_targets:
                results[idx]["adjacent_context"] = merge_adjacent_context(
                    document_id=doc_id,
                    chunk_index=chunk_index,
                    window=window,
                    row_map=adjacent_map,
                )
        return results

    @staticmethod
    def _format_retrieval_output(results: list[dict[str, Any]], search_time: float) -> str:
        output_parts = [f"找到 {len(results)} 条相关结果：\n"]
        for i, r in enumerate(results, 1):
            output_parts.append(
                f"\n【结果{i}】(相关度: {r['score']*100:.1f}%)\n"
                f"来源: {r['knowledge_base']} / {r['document']}\n"
                f"内容: {r['content'][:500]}{'...' if len(r['content']) > 500 else ''}"
            )
        output_parts.append(f"\n\n(搜索耗时: {search_time:.2f}ms)")
        return "".join(output_parts)

    def _log_retrieval_metrics(
        self,
        *,
        query: str,
        search_time: float,
        state: KnowledgeRetrieveState,
        result_count: int,
    ) -> None:
        rewrite_result = state.rewrite_result
        logger.info(
            f"[KnowledgeSearch] query='{query[:50]}...', results={result_count}, "
            f"hybrid={state.runtime.use_hybrid}, reranker={state.runtime.use_reranker}, "
            f"query_rewrite={rewrite_result.enabled}, "
            f"rewrite_variants={len(rewrite_result.vector_variants)}, "
            f"vector_hits={len(state.vector_rows)}, text_hits={len(state.text_rows)}, "
            f"ef_search={state.resolved_ef_search}, corpus_size={state.total_chunks}, "
            f"dims={sorted(state.retrieval_dimensions)}, ef_detail={state.ef_search_debug}, "
            f"time={search_time:.2f}ms"
        )

    async def _get_shared_kb_ids(self, db: AsyncSession) -> Set[int]:
        """获取共享给当前用户的知识库ID"""
        if not SHARING_ENABLED:
            logger.debug("共享功能未启用 (agent_tools)")
            return set()
        
        try:
            logger.debug(f"获取用户 {self.user_id} 的共享知识库 (agent_tools)")
            
            # 获取当前用户信息
            user_result = await db.execute(
                select(User).where(User.id == self.user_id)
            )
            current_user = user_result.scalar_one_or_none()
            if not current_user:
                logger.warning(f"用户 {self.user_id} 不存在")
                return set()
            
            logger.debug(f"当前用户: {current_user.username}, 角色: {current_user.role}, 导师ID: {current_user.mentor_id}")
            
            # 获取用户加入的研究组
            group_ids_result = await db.execute(
                select(GroupMember.group_id).where(GroupMember.user_id == self.user_id)
            )
            group_ids = [row[0] for row in group_ids_result.fetchall()]
            logger.debug(f"用户加入的研究组: {group_ids}")
            
            # 如果是导师，获取管理的研究组
            if current_user.role == UserRole.MENTOR.value:
                mentor_groups_result = await db.execute(
                    select(ResearchGroup.id).where(ResearchGroup.mentor_id == self.user_id)
                )
                mentor_group_ids = [row[0] for row in mentor_groups_result.fetchall()]
                group_ids = list(set(group_ids + mentor_group_ids))
            
            # 构建共享条件
            conditions = [
                and_(
                    SharedResource.shared_with_type == 'user',
                    SharedResource.shared_with_id == self.user_id
                ),
            ]
            
            if group_ids:
                conditions.append(
                    and_(
                        SharedResource.shared_with_type == 'group',
                        SharedResource.shared_with_id.in_(group_ids)
                    )
                )
            
            if current_user.mentor_id:
                conditions.append(
                    and_(
                        SharedResource.shared_with_type == 'all_students',
                        SharedResource.owner_id == current_user.mentor_id
                    )
                )
            
            if current_user.role == UserRole.STUDENT.value and group_ids:
                mentor_ids_result = await db.execute(
                    select(ResearchGroup.mentor_id).where(ResearchGroup.id.in_(group_ids))
                )
                mentor_ids = [row[0] for row in mentor_ids_result.fetchall()]
                if mentor_ids:
                    conditions.append(
                        and_(
                            SharedResource.shared_with_type == 'all_students',
                            SharedResource.owner_id.in_(mentor_ids)
                        )
                    )
            
            # 查询共享的知识库ID
            shared_result = await db.execute(
                select(SharedResource.resource_id).where(
                    and_(
                        SharedResource.resource_type == 'knowledge_base',
                        or_(*conditions),
                        or_(
                            SharedResource.expires_at == None,
                            SharedResource.expires_at > datetime.utcnow()
                        )
                    )
                )
            )
            
            # resource_id 是字符串，需要转为整数（知识库ID是整数）
            result = set()
            for row in shared_result.fetchall():
                try:
                    result.add(int(row[0]))
                except (ValueError, TypeError):
                    logger.warning(f"无效的知识库ID: {row[0]}")
            logger.info(f"用户 {self.user_id} 可访问的共享知识库 (agent_tools): {result}")
            return result
        except Exception as e:
            logger.warning(f"获取共享知识库失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return set()


class CalculatorInput(BaseModel):
    expression: str = Field(min_length=1, max_length=512)


class CalculatorTool(ToolBase):
    """计算器工具 - 执行数学计算"""
    name = "calculator"
    parallel_safe = True
    description = "执行数学计算，支持基本运算、三角函数、对数、幂运算等。当需要进行数值计算时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "数学表达式，如 '2+3*4', 'sqrt(16)', 'sin(3.14/2)', 'log(100, 10)'"
            }
        },
        "required": ["expression"]
    }
    input_model = CalculatorInput
    
    def __init__(self):
        # 安全的数学函数映射
        self.safe_functions = {
            'abs': abs,
            'round': round,
            'min': min,
            'max': max,
            'sum': sum,
            'pow': pow,
            'sqrt': math.sqrt,
            'sin': math.sin,
            'cos': math.cos,
            'tan': math.tan,
            'asin': math.asin,
            'acos': math.acos,
            'atan': math.atan,
            'sinh': math.sinh,
            'cosh': math.cosh,
            'tanh': math.tanh,
            'log': math.log,
            'log10': math.log10,
            'log2': math.log2,
            'exp': math.exp,
            'floor': math.floor,
            'ceil': math.ceil,
            'factorial': math.factorial,
            'gcd': math.gcd,
            'pi': math.pi,
            'e': math.e,
            'radians': math.radians,
            'degrees': math.degrees,
        }
    
    async def _execute(self, expression: str) -> ToolResult:
        """执行数学计算（asteval 安全求值）。"""
        expr = expression.strip()
        if not expr:
            return ToolResult(success=False, output="表达式不能为空", error="empty_expression")

        # 拒绝高风险语法
        forbidden_tokens = ["__", "import", "lambda", ";", "{", "}", "[", "]"]
        if any(token in expr for token in forbidden_tokens):
            return ToolResult(success=False, output="表达式包含不安全语法", error="unsafe_expression")

        # 限制标识符仅来自白名单
        allowed_names = set(self.safe_functions.keys())
        identifiers = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expr)
        for name in identifiers:
            if name not in allowed_names:
                return ToolResult(
                    success=False,
                    output=f"不支持的函数或变量: {name}",
                    error="invalid_identifier",
                )

        try:
            from asteval import Interpreter
        except ImportError:
            return ToolResult(
                success=False,
                output="asteval 未安装，无法执行安全求值。",
                error="asteval_not_installed",
            )

        try:
            evaluator = Interpreter(usersyms=dict(self.safe_functions), minimal=True)
            result = evaluator(expr)
            if evaluator.error:
                detail = "; ".join(err.get_error()[1] for err in evaluator.error)
                return ToolResult(
                    success=False,
                    output=f"计算错误: {detail}",
                    error="eval_error",
                )

            if isinstance(result, float):
                result_str = str(int(result)) if result.is_integer() else f"{result:.10g}"
            else:
                result_str = str(result)

            return ToolResult(
                success=True,
                output=f"计算结果: {expression} = {result_str}",
                data={"expression": expression, "result": result},
            )
        except ZeroDivisionError:
            return ToolResult(success=False, output="错误: 除数不能为零", error="division_by_zero")
        except Exception as exc:
            return ToolResult(success=False, output=f"计算错误: {exc}", error="calculation_error")


class DateTimeTool(Tool):
    """日期时间工具"""
    name = "datetime"
    parallel_safe = True
    description = "获取当前日期时间，或进行日期计算。当用户询问时间、日期相关问题时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型: 'now'(当前时间), 'date'(当前日期), 'weekday'(星期几), 'timestamp'(时间戳)",
                "enum": ["now", "date", "weekday", "timestamp", "format"]
            },
            "format": {
                "type": "string",
                "description": "日期格式，如 '%Y-%m-%d %H:%M:%S'，仅在 action='format' 时使用",
                "default": "%Y-%m-%d %H:%M:%S"
            }
        },
        "required": ["action"]
    }
    
    async def execute(self, action: str, format: str = "%Y-%m-%d %H:%M:%S") -> ToolResult:
        """获取日期时间信息"""
        try:
            now = datetime.now()
            
            if action == "now":
                result = now.strftime("%Y-%m-%d %H:%M:%S")
                output = f"当前时间: {result}"
            elif action == "date":
                result = now.strftime("%Y年%m月%d日")
                output = f"当前日期: {result}"
            elif action == "weekday":
                weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                result = weekdays[now.weekday()]
                output = f"今天是: {result}"
            elif action == "timestamp":
                result = int(now.timestamp())
                output = f"当前时间戳: {result}"
            elif action == "format":
                result = now.strftime(format)
                output = f"格式化时间: {result}"
            else:
                return ToolResult(
                    success=False,
                    output=f"不支持的操作: {action}",
                    error="invalid_action"
                )
            
            return ToolResult(
                success=True,
                output=output,
                data={"action": action, "result": result, "timestamp": int(now.timestamp())}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"日期时间操作错误: {str(e)}",
                error=str(e)
            )


class TextAnalysisTool(Tool):
    """文本分析工具"""
    name = "text_analysis"
    parallel_safe = True
    description = "分析文本的基本统计信息，如字数、词数、句子数等。用于文本分析需求。"
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要分析的文本内容"
            },
            "analysis_type": {
                "type": "string",
                "description": "分析类型: 'stats'(统计), 'keywords'(关键词提取)",
                "enum": ["stats", "keywords"],
                "default": "stats"
            }
        },
        "required": ["text"]
    }
    
    async def execute(self, text: str, analysis_type: str = "stats") -> ToolResult:
        """分析文本"""
        try:
            if analysis_type == "stats":
                # 基本统计
                char_count = len(text)
                char_no_space = len(text.replace(" ", "").replace("\n", ""))
                
                # 中文字数
                chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
                
                # 英文单词数
                english_words = len(re.findall(r'[a-zA-Z]+', text))
                
                # 句子数（简单估计）
                sentences = len(re.findall(r'[。！？.!?]+', text)) or 1
                
                # 段落数
                paragraphs = len([p for p in text.split('\n') if p.strip()])
                
                output = f"""文本统计分析:
- 总字符数: {char_count}
- 字符数(不含空格): {char_no_space}
- 中文字数: {chinese_chars}
- 英文单词数: {english_words}
- 句子数: {sentences}
- 段落数: {paragraphs}
- 平均句长: {char_no_space / sentences:.1f} 字符"""
                
                return ToolResult(
                    success=True,
                    output=output,
                    data={
                        "char_count": char_count,
                        "char_no_space": char_no_space,
                        "chinese_chars": chinese_chars,
                        "english_words": english_words,
                        "sentences": sentences,
                        "paragraphs": paragraphs
                    }
                )
            
            elif analysis_type == "keywords":
                # 简单的关键词提取（基于词频）
                # 中文分词简单处理
                words = re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+', text.lower())
                
                # 过滤停用词（简单列表）
                stopwords = {'的', '是', '在', '和', '了', '有', '不', '这', '为', '上', 
                            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                            'to', 'of', 'and', 'in', 'that', 'it', 'for', 'on', 'with'}
                words = [w for w in words if w not in stopwords and len(w) > 1]
                
                # 统计词频
                word_freq = {}
                for w in words:
                    word_freq[w] = word_freq.get(w, 0) + 1
                
                # 取前10个高频词
                top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:10]
                
                output = "关键词提取（按频率排序）:\n"
                for word, freq in top_words:
                    output += f"- {word}: {freq}次\n"
                
                return ToolResult(
                    success=True,
                    output=output,
                    data={"keywords": dict(top_words)}
                )
            
            else:
                return ToolResult(
                    success=False,
                    output=f"不支持的分析类型: {analysis_type}",
                    error="invalid_analysis_type"
                )
                
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"文本分析错误: {str(e)}",
                error=str(e)
            )


class UnitConverterTool(Tool):
    """单位转换工具"""
    name = "unit_converter"
    parallel_safe = True
    description = "进行常见单位转换，如长度、重量、温度、数据存储等。"
    parameters = {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "要转换的数值"
            },
            "from_unit": {
                "type": "string",
                "description": "源单位，如 'km', 'mile', 'kg', 'lb', 'celsius', 'fahrenheit', 'GB', 'MB'"
            },
            "to_unit": {
                "type": "string",
                "description": "目标单位"
            }
        },
        "required": ["value", "from_unit", "to_unit"]
    }
    
    def __init__(self):
        # 单位转换因子（都转换为基本单位）
        self.conversions = {
            # 长度 (基本单位: 米)
            'm': 1, 'km': 1000, 'cm': 0.01, 'mm': 0.001,
            'mile': 1609.344, 'yard': 0.9144, 'foot': 0.3048, 'inch': 0.0254,
            '米': 1, '千米': 1000, '厘米': 0.01, '毫米': 0.001,
            
            # 重量 (基本单位: 克)
            'g': 1, 'kg': 1000, 'mg': 0.001, 'ton': 1000000,
            'lb': 453.592, 'oz': 28.3495,
            '克': 1, '千克': 1000, '毫克': 0.001, '吨': 1000000,
            
            # 数据存储 (基本单位: 字节)
            'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4,
            'byte': 1, 'bit': 0.125,
        }
        
        # 单位类别
        self.categories = {
            'length': ['m', 'km', 'cm', 'mm', 'mile', 'yard', 'foot', 'inch', '米', '千米', '厘米', '毫米'],
            'weight': ['g', 'kg', 'mg', 'ton', 'lb', 'oz', '克', '千克', '毫克', '吨'],
            'data': ['B', 'KB', 'MB', 'GB', 'TB', 'byte', 'bit'],
        }
    
    def _get_category(self, unit: str) -> Optional[str]:
        for category, units in self.categories.items():
            if unit in units:
                return category
        return None
    
    async def execute(self, value: float, from_unit: str, to_unit: str) -> ToolResult:
        """执行单位转换"""
        try:
            # 温度特殊处理
            if from_unit.lower() in ['celsius', 'c', '摄氏度'] and to_unit.lower() in ['fahrenheit', 'f', '华氏度']:
                result = value * 9/5 + 32
                return ToolResult(
                    success=True,
                    output=f"{value}°C = {result:.2f}°F",
                    data={"value": value, "from": from_unit, "to": to_unit, "result": result}
                )
            elif from_unit.lower() in ['fahrenheit', 'f', '华氏度'] and to_unit.lower() in ['celsius', 'c', '摄氏度']:
                result = (value - 32) * 5/9
                return ToolResult(
                    success=True,
                    output=f"{value}°F = {result:.2f}°C",
                    data={"value": value, "from": from_unit, "to": to_unit, "result": result}
                )
            
            # 检查单位是否支持
            if from_unit not in self.conversions:
                return ToolResult(
                    success=False,
                    output=f"不支持的源单位: {from_unit}",
                    error="unsupported_unit"
                )
            if to_unit not in self.conversions:
                return ToolResult(
                    success=False,
                    output=f"不支持的目标单位: {to_unit}",
                    error="unsupported_unit"
                )
            
            # 检查单位是否属于同一类别
            from_category = self._get_category(from_unit)
            to_category = self._get_category(to_unit)
            
            if from_category != to_category:
                return ToolResult(
                    success=False,
                    output=f"无法在不同类别的单位之间转换: {from_unit}({from_category}) -> {to_unit}({to_category})",
                    error="category_mismatch"
                )
            
            # 执行转换
            base_value = value * self.conversions[from_unit]
            result = base_value / self.conversions[to_unit]
            
            return ToolResult(
                success=True,
                output=f"{value} {from_unit} = {result:.6g} {to_unit}",
                data={"value": value, "from": from_unit, "to": to_unit, "result": result}
            )
            
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"单位转换错误: {str(e)}",
                error=str(e)
            )


class LiteratureSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    source: str = Field(default="semantic_scholar")
    max_results: int = Field(default=5, ge=1, le=20)
    year_start: Optional[int] = None
    year_end: Optional[int] = None


class LiteratureSearchTool(ToolBase):
    """学术文献搜索工具 - 使用 Semantic Scholar 和 arXiv API"""
    name = "literature_search"
    parallel_safe = True
    description = "搜索学术论文和文献。可以搜索 Semantic Scholar 或 arXiv 数据库，获取论文标题、摘要、作者、引用数等信息。适用于学术研究、文献综述、找相关论文等场景。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，可以是论文标题、作者名、研究主题等"
            },
            "source": {
                "type": "string",
                "description": "数据源: semantic_scholar (默认，更全面)、arxiv (预印本，更新快) 或 multi（三源并行融合）",
                "enum": ["semantic_scholar", "arxiv", "multi"],
                "default": "semantic_scholar"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            },
            "year_start": {
                "type": "integer",
                "description": "起始年份过滤（可选）"
            },
            "year_end": {
                "type": "integer",
                "description": "结束年份过滤（可选）"
            }
        },
        "required": ["query"]
    }
    input_model = LiteratureSearchInput
    
    def __init__(self):
        from app.services.literature_service import get_literature_service
        self.service = get_literature_service()
    
    async def _execute(
        self,
        query: str,
        source: str = "semantic_scholar",
        max_results: int = 5,
        year_start: int = None,
        year_end: int = None
    ) -> ToolResult:
        """执行学术文献搜索"""
        logger.info(f"[LiteratureSearch] 搜索: {query}, source={source}")
        
        try:
            kwargs = {}
            if year_start and year_end:
                kwargs["year_range"] = (year_start, year_end)
            
            if source == "multi":
                per_source = max(1, math.ceil(max_results / 3))
                result = await self.service.search_multi(
                    query=query,
                    limit_per_source=per_source,
                    **kwargs,
                )
                papers = result.get("papers", [])[:max_results]
                result["papers"] = papers
            else:
                result = await self.service.search(
                    query=query,
                    source=source,
                    limit=max_results,
                    **kwargs,
                )

            if "error" in result:
                return ToolResult(
                    success=False,
                    output=f"搜索失败: {result['error']}",
                    error=result["error"]
                )
            
            papers = result.get("papers", [])
            
            if not papers:
                return ToolResult(
                    success=True,
                    output=f"未找到关于 '{query}' 的学术论文。",
                    data={"papers": [], "query": query, "source": source}
                )
            
            # 格式化输出
            output = self._format_results(query, source, papers)
            
            return ToolResult(
                success=True,
                output=output,
                data={
                    "papers": [self._paper_to_dict(p) for p in papers],
                    "query": query,
                    "source": source,
                    "total": result.get("total", len(papers))
                }
            )
            
        except Exception as e:
            logger.error(f"[LiteratureSearch] 搜索错误: {e}")
            return ToolResult(
                success=False,
                output=f"文献搜索错误: {str(e)}",
                error=str(e)
            )
    
    def _format_results(self, query: str, source: str, papers: list) -> str:
        """格式化搜索结果"""
        source_name = {
            "semantic_scholar": "Semantic Scholar",
            "arxiv": "arXiv",
            "multi": "Semantic Scholar + arXiv + PubMed",
        }.get(source, source)
        output_parts = [f"在 {source_name} 搜索 '{query}' 的结果：\n"]
        
        for i, paper in enumerate(papers, 1):
            # 作者列表
            authors = paper.authors[:3] if paper.authors else []
            author_str = ", ".join([a.get("name", "") for a in authors])
            if len(paper.authors) > 3:
                author_str += " 等"
            
            output_parts.append(f"\n【{i}】{paper.title}")
            if paper.year:
                output_parts.append(f" ({paper.year})")
            output_parts.append(f"\n作者: {author_str or '未知'}")
            
            if paper.venue:
                output_parts.append(f"\n发表: {paper.venue}")
            
            if paper.citation_count > 0:
                output_parts.append(f"\n引用数: {paper.citation_count}")
            
            if paper.abstract:
                # 截断摘要
                abstract = paper.abstract[:200] + "..." if len(paper.abstract) > 200 else paper.abstract
                output_parts.append(f"\n摘要: {abstract}")
            
            if paper.url:
                output_parts.append(f"\n链接: {paper.url}")
            
            output_parts.append("\n")
        
        return "".join(output_parts)
    
    def _paper_to_dict(self, paper) -> dict:
        """将论文对象转换为字典"""
        return {
            "source": paper.source,
            "external_id": paper.external_id,
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": paper.authors,
            "year": paper.year,
            "venue": paper.venue,
            "citation_count": paper.citation_count,
            "reference_count": paper.reference_count,
            "url": paper.url,
            "pdf_url": paper.pdf_url,
            "arxiv_id": paper.arxiv_id,
            "doi": paper.doi,
            "fields_of_study": paper.fields_of_study
        }


class DefaultToolProvider:
    """默认工具提供器：负责实例化工具，不负责注册。"""

    def build_default_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        tools: List[Tool] = []
        if (ctx.db or ctx.db_session_factory) and ctx.user_id:
            tools.append(
                KnowledgeSearchTool(
                    ctx.db,
                    int(ctx.user_id),
                    db_session_factory=ctx.db_session_factory,
                )
            )

        tools.extend(
            [
                WebSearchTool(),
                CalculatorTool(),
                DateTimeTool(),
                TextAnalysisTool(),
                UnitConverterTool(),
                LiteratureSearchTool(),
            ]
        )
        return tools

    def build_notebook_tools(self, ctx: ToolDependencyContext) -> List[Tool]:
        if not ctx.notebook_id or not ctx.kernel_manager:
            return []
        try:
            from app.services.notebook_tools import create_notebook_tools

            return create_notebook_tools(
                kernel_manager=ctx.kernel_manager,
                notebooks_store=ctx.notebooks_store,
                notebook_id=ctx.notebook_id,
                user_authorized=ctx.user_authorized,
            )
        except Exception as exc:
            logger.warning(f"无法构建 Notebook 工具集: {exc}")
            return []


class ToolRegistry:
    """工具注册表 - 支持 Notebook 工具扩展"""
    _mcp_route_circuit_state: Dict[str, Dict[str, Any]] = {}
    _INTENT_TOOL_MAP: Dict[str, Set[str]] = {
        "knowledge_query": {"knowledge_search"},
        "web_query": {"web_search", "web_scrape"},
        "code_task": {
            "notebook_execute",
            "notebook_variables",
            "notebook_cell",
            "pip_install",
            "code_analysis",
            "calculator",
            "unit_converter",
        },
        "literature_task": {"literature_search"},
        "general_chat": {"datetime", "calculator", "text_analysis"},
    }
    
    def __init__(
        self, 
        db: AsyncSession = None, 
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
        user_id: int = None,
        # Notebook 上下文参数
        notebook_id: str = None,
        kernel_manager = None,
        notebooks_store: dict = None,
        user_authorized: bool = False,  # 用户是否授权 Agent 操作 Notebook
        tool_provider: Optional[ToolProvider] = None,
    ):
        self.db = db
        self.db_session_factory = db_session_factory
        self.user_id = user_id
        self.notebook_id = notebook_id
        self.kernel_manager = kernel_manager
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
        self._tools: Dict[str, Tool] = {}
        self._mcp_tools: Dict[str, MCPRemoteTool] = {}
        self._mcp_client_manager: Any = None
        self._tool_provider: ToolProvider = tool_provider or DefaultToolProvider()
        self._tool_context = ToolDependencyContext(
            db=self.db,
            db_session_factory=self.db_session_factory,
            user_id=self.user_id,
            notebook_id=self.notebook_id,
            kernel_manager=self.kernel_manager,
            notebooks_store=self.notebooks_store,
            user_authorized=self.user_authorized,
        )
        self._mcp_tool_routes: Dict[str, List[str]] = self._load_mcp_tool_routes()
        self._register_default_tools()
        
        # 如果提供了 Notebook 上下文，注册 Notebook 工具
        if notebook_id and kernel_manager:
            self._register_notebook_tools()

        self._init_mcp_client_manager()
    
    def _register_default_tools(self):
        """注册默认工具"""
        for tool in self._tool_provider.build_default_tools(self._tool_context):
            self.register(tool)
    
    def _register_notebook_tools(self):
        """注册 Notebook 专用工具"""
        notebook_tools = self._tool_provider.build_notebook_tools(self._tool_context)
        for tool in notebook_tools:
            self.register(tool)
        if notebook_tools:
            logger.info(f"已注册 Notebook 工具集，授权状态: {self.user_authorized}")

    def _init_mcp_client_manager(self) -> None:
        """Initialize MCP client manager when MCP is enabled."""
        if not settings.mcp_enabled:
            return
        try:
            self._mcp_client_manager = self._create_mcp_client_manager()
            logger.info("[MCP] MCP client manager initialized")
        except Exception as exc:
            logger.warning(f"[MCP] init failed, fallback to local tools only: {exc}")
            self._mcp_client_manager = None

    def _create_mcp_client_manager(self):
        from app.services.mcp import MCPClientManager, MCPServerManager, load_mcp_server_configs

        configs = load_mcp_server_configs(
            settings.mcp_servers,
            settings.mcp_call_timeout_seconds,
            config_path=getattr(settings, "mcp_config_path", ""),
        )
        if not configs:
            logger.warning("[MCP] MCP_ENABLED=true but MCP_SERVERS is empty")

        server_manager = MCPServerManager(configs)
        return MCPClientManager(server_manager, tool_prefix=settings.mcp_tool_prefix)

    def _load_mcp_tool_routes(self) -> Dict[str, List[str]]:
        """Load local-tool to remote-tool route mappings from MCP_TOOL_ROUTES."""
        raw = (getattr(settings, "mcp_tool_routes", "") or "").strip()
        if not raw:
            return {}

        try:
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning(f"[MCP] invalid MCP_TOOL_ROUTES JSON: {exc}")
            return {}

        if not isinstance(payload, dict):
            logger.warning("[MCP] MCP_TOOL_ROUTES must be a JSON object")
            return {}

        routes: Dict[str, List[str]] = {}
        for local_tool, remote_tools in payload.items():
            local_name = str(local_tool or "").strip()
            if not local_name:
                continue

            if isinstance(remote_tools, str):
                candidates = [remote_tools.strip()] if remote_tools.strip() else []
            elif isinstance(remote_tools, list):
                candidates = [str(item).strip() for item in remote_tools if str(item).strip()]
            else:
                logger.warning(f"[MCP] skip invalid route for tool={local_name}, expected string/list")
                continue

            if candidates:
                routes[local_name] = candidates
        return routes

    @classmethod
    def _is_circuit_open(cls, route_key: str) -> bool:
        state = cls._mcp_route_circuit_state.get(route_key)
        if not state:
            return False

        opened_until = float(state.get("opened_until", 0.0) or 0.0)
        if opened_until <= 0:
            return False

        now = time.time()
        if now < opened_until:
            return True

        state["opened_until"] = 0.0
        state["failures"] = 0
        return False

    @classmethod
    def _record_circuit_success(cls, route_key: str) -> None:
        state = cls._mcp_route_circuit_state.setdefault(
            route_key,
            {"failures": 0, "opened_until": 0.0},
        )
        state["failures"] = 0
        state["opened_until"] = 0.0

    @classmethod
    def _record_circuit_failure(cls, route_key: str, error: str) -> None:
        state = cls._mcp_route_circuit_state.setdefault(
            route_key,
            {"failures": 0, "opened_until": 0.0},
        )
        state["failures"] = int(state.get("failures", 0)) + 1

        threshold = max(int(getattr(settings, "mcp_route_circuit_breaker_failures", 3)), 1)
        if state["failures"] < threshold:
            return

        open_seconds = max(int(getattr(settings, "mcp_route_circuit_breaker_open_seconds", 120)), 1)
        state["opened_until"] = time.time() + open_seconds
        state["failures"] = 0
        logger.warning(
            f"[MCP] circuit opened route={route_key}, open_seconds={open_seconds}, last_error={error}"
        )

    async def _call_mcp_tool_with_retry(self, route_key: str, arguments: Dict[str, Any]):
        if not self._mcp_client_manager:
            return None

        if self._is_circuit_open(route_key):
            logger.warning(f"[MCP] circuit open, skip route={route_key}")
            return type(
                "MCPRouteResult",
                (),
                {
                    "success": False,
                    "output": f"MCP route circuit open: {route_key}",
                    "data": None,
                    "error": "circuit_open",
                },
            )()

        timeout_seconds = max(int(getattr(settings, "mcp_route_timeout_seconds", 15)), 1)
        retry_attempts = max(int(getattr(settings, "mcp_route_retry_attempts", 2)), 1)
        backoff_seconds = float(getattr(settings, "mcp_route_retry_backoff_seconds", 0.5))
        last_result = None

        for attempt in range(1, retry_attempts + 1):
            try:
                maybe_awaitable = self._mcp_client_manager.call_tool(route_key, arguments)
                result = await asyncio.wait_for(maybe_awaitable, timeout=timeout_seconds)
                last_result = result
                if result.success:
                    self._record_circuit_success(route_key)
                    return result
            except asyncio.TimeoutError:
                last_result = type(
                    "MCPRouteResult",
                    (),
                    {
                        "success": False,
                        "output": f"MCP route timeout after {timeout_seconds}s: {route_key}",
                        "data": None,
                        "error": "timeout",
                    },
                )()
            except Exception as exc:
                last_result = type(
                    "MCPRouteResult",
                    (),
                    {
                        "success": False,
                        "output": f"MCP route call failed: {exc}",
                        "data": None,
                        "error": "mcp_route_exception",
                    },
                )()

            if attempt < retry_attempts and backoff_seconds > 0:
                await asyncio.sleep(backoff_seconds * attempt)

        if last_result and not last_result.success:
            self._record_circuit_failure(route_key, str(last_result.error or "unknown_error"))
        return last_result

    async def _execute_routed_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[ToolResult]:
        """Try remote MCP routes first and fallback to local tool when all fail."""
        if not self._mcp_client_manager:
            return None

        candidates = self._mcp_tool_routes.get(tool_name) or []
        if not candidates:
            return None

        for route_key in candidates:
            result = await self._call_mcp_tool_with_retry(route_key, arguments)
            if not result:
                continue
            if result.success:
                logger.info(f"[MCP] routed success local={tool_name} remote={route_key}")
                return ToolResult(
                    success=True,
                    output=str(result.output),
                    data=result.data if isinstance(result.data, dict) else {"raw": result.data},
                    error=result.error,
                )
            logger.warning(
                f"[MCP] routed call failed local={tool_name} remote={route_key} error={result.error}"
            )

        return None

    async def refresh_mcp_tools(self, force_refresh: bool = False) -> None:
        """Refresh remote MCP tool cache."""
        if not self._mcp_client_manager:
            return

        schemas = await self._mcp_client_manager.discover_tools(force_refresh=force_refresh)
        self._mcp_tools = {
            schema.qualified_name: MCPRemoteTool(schema=schema, mcp_client_manager=self._mcp_client_manager)
            for schema in schemas
        }

    def _iter_all_tools(self) -> List[Tool]:
        return list(self._tools.values()) + list(self._mcp_tools.values())

    @staticmethod
    def classify_intent(user_text: str) -> str:
        text = (user_text or "").lower()
        if not text.strip():
            return "general_chat"

        if any(token in text for token in ["论文", "文献", "paper", "arxiv", "pubmed", "citation"]):
            return "literature_task"
        if any(
            token in text
            for token in [
                "代码",
                "notebook",
                "python",
                "cell",
                "运行",
                "debug",
                "报错",
                "画图",
                "绘图",
                "可视化",
                "plot",
                "matplotlib",
                "seaborn",
                "pandas",
                "numpy",
                "余弦",
                "正弦",
                "散点图",
                "折线图",
                "柱状图",
            ]
        ):
            return "code_task"

        knowledge_tokens = [
            "知识库",
            "文档",
            "资料",
            "文件",
            "附件",
            "上传资料",
            "上传文件",
            "我上传",
            "kb",
            "rag",
            "chunk",
            "向量检索",
            "knowledge base",
            "vector store",
            "my file",
            "my files",
            "my document",
            "my documents",
            "my docs",
            "uploaded",
            "upload",
        ]
        knowledge_patterns = [
            r"\b(my|this|that)\s+(uploaded\s+)?(pdf|docx?|pptx?|xlsx?|csv|file|document)\b",
            r"\b(summarize|summary|analyze|extract)\s+(my|this|that)\s+(pdf|docx?|file|document)\b",
            r"(根据|基于).*(我(上传|的)?|知识库|文档|资料|文件|附件|pdf)",
            r"(总结|概括|提炼|归纳).*(我(上传|的)?|文档|资料|文件|附件|pdf)",
        ]
        knowledge_hit = any(token in text for token in knowledge_tokens) or any(
            re.search(pattern, text) for pattern in knowledge_patterns
        )
        if knowledge_hit:
            return "knowledge_query"

        if any(
            token in text
            for token in [
                "网页",
                "网站",
                "新闻",
                "实时",
                "today",
                "latest",
                "搜索互联网",
                "web",
                "internet",
                "online",
            ]
        ):
            return "web_query"
        return "general_chat"

    @staticmethod
    def _parse_csv_names(value: str) -> Set[str]:
        return {item.strip() for item in (value or "").split(",") if item.strip()}

    def _fallback_tools(self) -> Set[str]:
        return self._parse_csv_names(str(getattr(settings, "tool_selection_fallback_tools", "")))

    def _mcp_tool_matches_intent(self, tool: Tool, intent: str) -> bool:
        text = f"{tool.name} {tool.description}".lower()
        if intent == "web_query":
            return any(k in text for k in ["web", "search", "scrape", "crawl", "fetch", "browser"])
        if intent == "knowledge_query":
            return any(k in text for k in ["knowledge", "kb", "rag", "vector", "document"])
        if intent == "literature_task":
            return any(k in text for k in ["literature", "paper", "arxiv", "pubmed", "semantic", "crossref", "openalex"])
        if intent == "code_task":
            return any(k in text for k in ["code", "notebook", "python", "execute", "analysis", "pip"])
        return False

    def select_tool_names_for_intent(self, intent: str, user_text: str = "") -> List[str]:
        if not bool(getattr(settings, "tool_selection_enabled", True)):
            return [tool.name for tool in self._iter_all_tools()]

        resolved_intent = intent if intent in self._INTENT_TOOL_MAP else self.classify_intent(user_text)
        selected = set(self._INTENT_TOOL_MAP.get(resolved_intent, set()))
        selected.update(self._fallback_tools())

        # Notebook 场景下默认保留代码工具，避免普通表述被误判为 general_chat 后无法操作 notebook。
        if self.notebook_id and self.kernel_manager:
            selected.update(
                {
                    "notebook_execute",
                    "notebook_variables",
                    "notebook_cell",
                    "notebook_cleanup",
                    "pip_install",
                    "code_analysis",
                }
            )

        for tool in self._iter_all_tools():
            if tool.name.startswith("mcp.") and self._mcp_tool_matches_intent(tool, resolved_intent):
                selected.add(tool.name)

        return [tool.name for tool in self._iter_all_tools() if tool.name in selected]

    def _filter_tools(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> List[Tool]:
        tools = self._iter_all_tools()
        if include_tool_names:
            allow = set(include_tool_names)
            return [tool for tool in tools if tool.name in allow]
        if intent:
            names = set(self.select_tool_names_for_intent(intent, user_text=user_text))
            return [tool for tool in tools if tool.name in names]
        return tools
    
    def register(self, tool: Tool):
        """注册工具"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self._tools.get(name) or self._mcp_tools.get(name)
    
    def list_tools(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> List[Dict[str, Any]]:
        """获取工具列表（用于发送给 LLM）"""
        filtered_tools = self._filter_tools(
            intent=intent,
            include_tool_names=include_tool_names,
            user_text=user_text,
        )
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "x_parallel_safe": bool(getattr(tool, "parallel_safe", False)),
                }
            }
            for tool in filtered_tools
        ]
    
    def get_tools_description(
        self,
        *,
        intent: Optional[str] = None,
        include_tool_names: Optional[Set[str]] = None,
        user_text: str = "",
    ) -> str:
        """获取工具描述（用于 ReAct prompt）"""
        descriptions = []
        for tool in self._filter_tools(
            intent=intent,
            include_tool_names=include_tool_names,
            user_text=user_text,
        ):
            params = tool.parameters.get('properties', {})
            required = tool.parameters.get('required', [])
            
            params_desc = []
            for k, v in params.items():
                param_str = f"{k}: {v.get('type', 'any')}"
                if k in required:
                    param_str += " (必填)"
                if 'description' in v:
                    param_str += f" - {v['description']}"
                params_desc.append(param_str)
            
            descriptions.append(
                f"**{tool.name}**: {tool.description}\n"
                f"  参数: {', '.join(params_desc) if params_desc else '无'}"
            )
        return "\n\n".join(descriptions)
    
    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行工具"""
        local_tool = self._tools.get(tool_name)
        if local_tool:
            routed_result = await self._execute_routed_mcp_tool(tool_name, kwargs)
            if routed_result:
                return routed_result

        tool = self.get(tool_name)
        if tool:
            try:
                logger.info(f"执行工具: {tool_name}, 参数: {kwargs}")
                result = await tool.execute(**kwargs)
                retry_attempt = None
                if isinstance(result.data, dict):
                    retry_attempt = result.data.get("retry_attempt")
                logger.info(
                    f"工具执行完成: {tool_name}, 成功: {result.success}, "
                    f"retry_attempt={retry_attempt}, "
                    f"execution_time_ms={result.execution_time_ms:.2f}, "
                    f"tokens_est={result.output_tokens_estimate}, truncated={result.truncated}"
                )
                return result
            except Exception as e:
                logger.error(f"工具执行失败 {tool_name}: {e}")
                contract = build_tool_error_contract(
                    code="tool_execute_failed",
                    message="工具执行失败",
                    tool_name=tool_name,
                    stage="dispatch",
                    detail=str(e),
                    retryable=False,
                    metadata={"exception_type": type(e).__name__},
                )
                return ToolResult(
                    success=False,
                    output=f"{contract['message']}: {e}",
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )

        if self._mcp_client_manager:
            mcp_result = await self._mcp_client_manager.call_tool(tool_name, kwargs)
            if mcp_result.error != "tool_not_found":
                return ToolResult(
                    success=mcp_result.success,
                    output=mcp_result.output,
                    data=mcp_result.data,
                    error=mcp_result.error,
                )

        contract = build_tool_error_contract(
            code="tool_not_found",
            message=f"未找到工具: {tool_name}",
            tool_name=tool_name,
            stage="dispatch",
            retryable=False,
            metadata={"available_tools": [t.name for t in self._iter_all_tools()]},
        )
        return ToolResult(
            success=False,
            output=f"{contract['message']}。可用工具: {', '.join(contract['metadata']['available_tools'])}",
            error=str(contract["code"]),
            data=merge_error_contract(None, contract),
        )


def get_tool_registry(
    db: Optional[AsyncSession],
    user_id: int,
    db_session_factory: Optional[Callable[[], AsyncSession]] = None,
) -> ToolRegistry:
    """获取工具注册表"""
    return ToolRegistry(db=db, user_id=user_id, db_session_factory=db_session_factory)
