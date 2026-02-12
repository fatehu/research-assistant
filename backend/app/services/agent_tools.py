"""
Agent 工具定义和执行 - 支持共享知识库搜索
"""
import json
import time
import math
import re
import asyncio
import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Callable
from dataclasses import dataclass
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, or_, and_

from app.config import settings
from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.services.embedding_service import get_embedding_service
from app.services.hybrid_retrieval_service import fuse_rrf, merge_rows_by_score
from app.services.query_rewrite_service import QueryVariant, get_query_rewrite_service
from app.services.reranker_service import get_reranker_service, RerankerService
from app.services.vector_search_tuning import apply_hnsw_ef_search

# 尝试导入共享模块（可选）
try:
    from app.models.role import SharedResource, GroupMember, ResearchGroup, UserRole
    from app.models.user import User
    SHARING_ENABLED = True
except ImportError:
    SHARING_ENABLED = False


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    output: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class Tool:
    """工具基类"""
    name: str
    description: str
    parameters: Dict[str, Any]
    
    async def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError


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


class WebSearchTool(Tool):
    """Web 搜索工具 - 使用 Serper API 进行 Google 搜索"""
    name = "web_search"
    description = "搜索互联网获取最新信息。当用户问题涉及新闻、实时信息、天气、或需要网络查询时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            },
            "max_results": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            }
        },
        "required": ["query"]
    }
    
    def __init__(self):
        import os
        self.api_key = os.getenv("SERPER_API_KEY", "")
        if self.api_key:
            logger.info(f"[WebSearch] Serper API Key 已配置 (长度: {len(self.api_key)})")
        else:
            logger.warning("[WebSearch] 未配置 SERPER_API_KEY")
    
    async def execute(self, query: str, max_results: int = 5) -> ToolResult:
        """执行 Web 搜索 - 使用 Serper API"""
        logger.info(f"[WebSearch] 开始搜索: {query}")
        
        # 先尝试 Serper API
        if self.api_key:
            try:
                result = await self._serper_search(query, max_results)
                if result.success:
                    return result
                logger.warning(f"[WebSearch] Serper API 失败: {result.error}, 尝试备用方案")
            except Exception as e:
                logger.error(f"[WebSearch] Serper API 异常: {type(e).__name__}: {e}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            logger.warning("[WebSearch] 未配置 SERPER_API_KEY，直接使用备用搜索")
        
        # 备用方案
        return await self._fallback_search(query, max_results)
    
    async def _serper_search(self, query: str, max_results: int) -> ToolResult:
        """使用 Serper API 搜索"""
        logger.info(f"[WebSearch] 使用 Serper API 搜索: {query}")
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": self.api_key,
                    "Content-Type": "application/json"
                },
                json={
                    "q": query,
                    "num": max_results,
                    "gl": "cn",
                    "hl": "zh-cn"
                }
            )
            
            logger.info(f"[WebSearch] Serper API 响应状态: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text[:500] if response.text else "无响应内容"
                logger.error(f"[WebSearch] Serper API 错误响应: {error_text}")
                return ToolResult(
                    success=False,
                    output=f"Serper API 请求失败: HTTP {response.status_code}",
                    error=f"http_{response.status_code}"
                )
            
            data = response.json()
            logger.info(f"[WebSearch] Serper API 返回数据键: {list(data.keys())}")
            
            results = []
            
            # 解析响应
            if "knowledgeGraph" in data:
                kg = data["knowledgeGraph"]
                results.append({
                    "type": "knowledge_graph",
                    "title": kg.get("title", ""),
                    "description": kg.get("description", ""),
                    "attributes": kg.get("attributes", {})
                })
            
            if "answerBox" in data:
                ab = data["answerBox"]
                answer = ab.get("answer") or ab.get("snippet") or ab.get("title", "")
                if answer:
                    results.append({
                        "type": "answer_box",
                        "answer": answer,
                        "source": ab.get("link", "")
                    })
            
            for item in data.get("organic", [])[:max_results]:
                results.append({
                    "type": "organic",
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                    "date": item.get("date", "")
                })
            
            if "peopleAlsoAsk" in data and len(results) < max_results + 2:
                for paa in data["peopleAlsoAsk"][:2]:
                    results.append({
                        "type": "related_question",
                        "question": paa.get("question", ""),
                        "snippet": paa.get("snippet", "")
                    })
            
            logger.info(f"[WebSearch] Serper API 解析出 {len(results)} 条结果")
            
            if not results:
                return ToolResult(
                    success=True,
                    output=f"未找到关于 '{query}' 的搜索结果。",
                    data={"results": [], "query": query}
                )
            
            output = self._format_results(query, results)
            
            return ToolResult(
                success=True,
                output=output,
                data={"results": results, "query": query}
            )
    
    def _format_results(self, query: str, results: list) -> str:
        """格式化搜索结果"""
        output_parts = [f"搜索 '{query}' 的结果：\n"]
        
        idx = 0
        for r in results:
            result_type = r.get("type", "organic")
            
            if result_type == "knowledge_graph":
                output_parts.append(f"\n📚 【知识卡片】{r.get('title', '')}")
                if r.get("description"):
                    output_parts.append(f"\n{r['description']}")
                if r.get("attributes"):
                    for k, v in list(r["attributes"].items())[:3]:
                        output_parts.append(f"\n  • {k}: {v}")
            
            elif result_type == "answer_box":
                output_parts.append(f"\n💡 【直接答案】{r.get('answer', '')}")
                if r.get("source"):
                    output_parts.append(f"\n来源: {r['source']}")
            
            elif result_type == "organic":
                idx += 1
                output_parts.append(f"\n\n【搜索结果{idx}】{r.get('title', '')}")
                if r.get("date"):
                    output_parts.append(f" ({r['date']})")
                if r.get("url"):
                    output_parts.append(f"\n链接: {r['url']}")
                if r.get("snippet"):
                    output_parts.append(f"\n摘要: {r['snippet']}")
            
            elif result_type == "related_question":
                output_parts.append(f"\n\n❓ 相关问题: {r.get('question', '')}")
                if r.get("snippet"):
                    output_parts.append(f"\n答案: {r['snippet']}")
        
        return "".join(output_parts)
    
    async def _fallback_search(self, query: str, max_results: int) -> ToolResult:
        """备用搜索方案 - 使用 DuckDuckGo"""
        logger.info(f"[WebSearch] 使用 DuckDuckGo 备用搜索: {query}")
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    }
                )
                
                logger.info(f"[WebSearch] DuckDuckGo 响应状态: {response.status_code}")
                
                if response.status_code != 200:
                    return ToolResult(
                        success=False,
                        output=f"搜索请求失败: HTTP {response.status_code}",
                        error="search_failed"
                    )
                
                html = response.text
                results = []
                
                # 更宽松的正则表达式匹配
                # 匹配标题和链接
                title_pattern = r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>([^<]*(?:<[^>]+>[^<]*)*)</a>'
                # 匹配摘要
                snippet_pattern = r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>([^<]*(?:<[^>]+>[^<]*)*)</a>'
                
                links = re.findall(title_pattern, html, re.DOTALL)
                snippets = re.findall(snippet_pattern, html, re.DOTALL)
                
                logger.info(f"[WebSearch] 找到 {len(links)} 个链接, {len(snippets)} 个摘要")
                
                for i, (url, title) in enumerate(links[:max_results]):
                    # 清理 HTML 标签
                    title = re.sub(r'<[^>]+>', '', title)
                    title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').strip()
                    
                    snippet = ""
                    if i < len(snippets):
                        snippet = re.sub(r'<[^>]+>', '', snippets[i])
                        snippet = snippet.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').strip()
                    
                    if title:  # 只添加有标题的结果
                        results.append({
                            "type": "organic",
                            "title": title,
                            "url": url,
                            "snippet": snippet
                        })
                
                logger.info(f"[WebSearch] 解析出 {len(results)} 条有效结果")
                
                if not results:
                    # 如果没有找到结果，尝试备用方案：直接返回提示
                    return ToolResult(
                        success=True,
                        output=f"未找到关于 '{query}' 的搜索结果。建议：\n1. 尝试使用不同的关键词\n2. 检查网络连接\n3. 如果需要最新信息，请稍后重试",
                        data={"results": [], "query": query}
                    )
                
                # 简单格式化输出（不调用 _format_results 避免潜在问题）
                output_parts = [f"搜索 '{query}' 的结果：\n"]
                for i, r in enumerate(results, 1):
                    output_parts.append(f"\n【结果{i}】{r['title']}")
                    if r['url']:
                        output_parts.append(f"\n链接: {r['url']}")
                    if r['snippet']:
                        output_parts.append(f"\n摘要: {r['snippet']}")
                    output_parts.append("\n")
                
                return ToolResult(
                    success=True,
                    output="".join(output_parts),
                    data={"results": results, "query": query}
                )
                
        except httpx.TimeoutException:
            logger.warning("[WebSearch] DuckDuckGo 搜索超时")
            return ToolResult(
                success=False,
                output="搜索超时，请稍后重试。",
                error="timeout"
            )
        except Exception as e:
            logger.error(f"[WebSearch] DuckDuckGo 搜索异常: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ToolResult(
                success=False,
                output=f"搜索失败: {type(e).__name__} - {str(e)}",
                error=str(e)
            )


class KnowledgeSearchTool(Tool):
    """知识库搜索工具 - 使用 pgvector 进行向量检索"""
    name = "knowledge_search"
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
            }
        },
        "required": ["query"]
    }
    
    def __init__(
        self,
        db: Optional[AsyncSession],
        user_id: int,
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
    ):
        self.db = db
        self.user_id = user_id
        self.db_session_factory = db_session_factory
        self.embedding_service = get_embedding_service()
        self.query_rewrite_service = get_query_rewrite_service()
    
    async def execute(self, query: str, top_k: int = 5) -> ToolResult:
        """执行知识库搜索（自动选择会话策略）"""
        if self.db is not None:
            return await self._execute_with_db(self.db, query, top_k)

        if self.db_session_factory is None:
            return ToolResult(
                success=False,
                output="知识库搜索不可用：数据库会话未初始化",
                error="db_session_unavailable",
            )

        try:
            async with self.db_session_factory() as db:
                return await self._execute_with_db(db, query, top_k)
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
    ) -> ToolResult:
        """执行知识库搜索 - 使用 pgvector 原生向量搜索，支持共享知识库"""
        try:
            start_time = time.time()
            rewrite_result = await self.query_rewrite_service.rewrite_query(
                query,
                use_query_rewrite=True,
            )
            
            # 获取用户的知识库ID列表
            kb_query = select(KnowledgeBase.id).where(KnowledgeBase.user_id == self.user_id)
            kb_result = await db.execute(kb_query)
            kb_ids = set(row[0] for row in kb_result.fetchall())
            
            # 获取共享给用户的知识库ID
            shared_kb_ids = await self._get_shared_kb_ids(db)
            kb_ids = kb_ids | shared_kb_ids
            
            if not kb_ids:
                return ToolResult(
                    success=True,
                    output="用户没有创建任何知识库，也没有收到共享的知识库，无法搜索相关内容。建议用户先上传文档到知识库，或请导师共享知识库。",
                    data={"results": [], "total": 0}
                )
            
            kb_ids = list(kb_ids)
            use_reranker = settings.enable_reranker
            use_hybrid = settings.enable_hybrid_retrieval
            final_top_k = top_k
            score_threshold = max(
                0.0,
                min(float(settings.agent_knowledge_score_threshold), 1.0),
            )
            distance_threshold = 1 - score_threshold

            reranker_candidate_k = (
                max(final_top_k, settings.reranker_top_k)
                if use_reranker
                else final_top_k
            )
            vector_top_k = max(
                reranker_candidate_k,
                settings.hybrid_vector_top_k if use_hybrid else 0,
            )
            text_top_k = (
                max(reranker_candidate_k, settings.hybrid_text_top_k)
                if use_hybrid
                else 0
            )
            fusion_limit = reranker_candidate_k
            
            # 使用 pgvector 进行向量相似度搜索
            vector_sql = text("""
                SELECT 
                    dc.id,
                    dc.document_id,
                    dc.knowledge_base_id,
                    dc.content,
                    dc.chunk_index,
                    1 - (dc.embedding <=> :query_vector) as similarity,
                    NULL::float as text_score,
                    d.original_filename as document_name,
                    kb.name as knowledge_base_name
                FROM document_chunks dc
                JOIN documents d ON dc.document_id = d.id
                JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
                WHERE dc.knowledge_base_id = ANY(:kb_ids)
                    AND dc.embedding IS NOT NULL
                    AND (dc.embedding <=> :query_vector) <= :distance_threshold
                ORDER BY dc.embedding <=> :query_vector
                LIMIT :vector_top_k
            """)

            vector_rows = []
            vector_group_rows = []
            vector_variants = rewrite_result.vector_variants or [
                QueryVariant(text=query, strategy="original")
            ]

            vector_embeddings = []
            vector_texts = [variant.text for variant in vector_variants]
            try:
                vector_embeddings = await self.embedding_service.embed_texts(
                    vector_texts,
                    is_query=True,
                )
                if len(vector_embeddings) != len(vector_texts):
                    raise ValueError(
                        f"embedding count mismatch: {len(vector_embeddings)} vs {len(vector_texts)}"
                    )
            except Exception as e:
                logger.warning(f"[KnowledgeSearch] Batch embedding failed, fallback to single: {e}")
                vector_embeddings = []
                for variant in vector_variants:
                    try:
                        emb = await self.embedding_service.embed_text(
                            variant.text,
                            is_query=True,
                        )
                    except Exception as single_exc:
                        logger.warning(
                            f"[KnowledgeSearch] Single embedding failed for "
                            f"strategy={variant.strategy}: {single_exc}"
                        )
                        emb = []
                    vector_embeddings.append(emb)

            await apply_hnsw_ef_search(
                db,
                settings.pgvector_hnsw_ef_search,
                source="knowledge_search_tool",
            )

            for idx, variant in enumerate(vector_variants):
                query_embedding = vector_embeddings[idx] if idx < len(vector_embeddings) else []
                if not query_embedding:
                    continue

                vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
                result = await db.execute(
                    vector_sql,
                    {
                        "query_vector": vector_str,
                        "distance_threshold": distance_threshold,
                        "kb_ids": kb_ids,
                        "vector_top_k": vector_top_k,
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
                limit=vector_top_k,
            )

            text_rows = []
            if use_hybrid:
                text_variants = rewrite_result.text_variants or [
                    QueryVariant(text=query, strategy="original")
                ]
                text_sql = text("""
                    SELECT 
                        dc.id,
                        dc.document_id,
                        dc.knowledge_base_id,
                        dc.content,
                        dc.chunk_index,
                        NULL::float as similarity,
                        ts_rank_cd(
                            to_tsvector('simple', dc.content),
                            websearch_to_tsquery('simple', :fts_query)
                        ) as text_score,
                        d.original_filename as document_name,
                        kb.name as knowledge_base_name
                    FROM document_chunks dc
                    JOIN documents d ON dc.document_id = d.id
                    JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
                    WHERE dc.knowledge_base_id = ANY(:kb_ids)
                        AND dc.content IS NOT NULL
                        AND dc.content <> ''
                        AND to_tsvector('simple', dc.content) @@ websearch_to_tsquery('simple', :fts_query)
                    ORDER BY text_score DESC
                    LIMIT :text_top_k
                """)
                text_group_rows = []
                for variant in text_variants:
                    if not variant.text.strip():
                        continue
                    try:
                        text_result = await db.execute(
                            text_sql,
                            {
                                "fts_query": variant.text,
                                "kb_ids": kb_ids,
                                "text_top_k": text_top_k,
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

                text_rows = merge_rows_by_score(
                    text_group_rows,
                    score_attr="text_score",
                    query_attr="matched_text_query",
                    strategy_attr="matched_text_strategy",
                    limit=text_top_k,
                )

            fused_candidates = fuse_rrf(
                vector_rows=vector_rows,
                text_rows=text_rows if use_hybrid else [],
                rrf_k=settings.hybrid_rrf_k,
                limit=fusion_limit,
            )

            if not fused_candidates:
                return ToolResult(
                    success=True,
                    output="未找到与查询相关的内容。可能知识库中没有相关信息，或者需要调整搜索关键词。",
                    data={"results": [], "total": 0}
                )

            selected_candidates = []
            if use_reranker:
                try:
                    reranker = get_reranker_service()
                    reranked = await reranker.rerank(
                        query=query,
                        documents=[candidate.row.content for candidate in fused_candidates],
                        top_k=final_top_k,
                    )
                    selected_candidates = [
                        (fused_candidates[idx], score)
                        for idx, score in reranked
                        if 0 <= idx < len(fused_candidates)
                    ]
                except Exception as e:
                    logger.warning(f"[KnowledgeSearch] Reranker failed, fallback to retrieval ranking: {e}")

            if not selected_candidates:
                selected_candidates = [
                    (candidate, None)
                    for candidate in fused_candidates[:final_top_k]
                ]
            
            # 构建结果
            results = []
            max_rrf_score = max((c.rrf_score for c in fused_candidates), default=0.0)
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

                results.append({
                    "content": row.content,
                    "score": score,
                    "document": row.document_name or "未知",
                    "knowledge_base": row.knowledge_base_name or "未知",
                    "chunk_index": row.chunk_index,
                    "retrieval_mode": "hybrid" if use_hybrid else "vector",
                    "query_rewrite_enabled": rewrite_result.enabled,
                    "query_rewrite_strategies": rewrite_result.strategies,
                    "query_rewrite_fallback": rewrite_result.fallback_reason,
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
                })
            
            # 格式化输出
            output_parts = [f"找到 {len(results)} 条相关结果：\n"]
            for i, r in enumerate(results, 1):
                output_parts.append(
                    f"\n【结果{i}】(相关度: {r['score']*100:.1f}%)\n"
                    f"来源: {r['knowledge_base']} / {r['document']}\n"
                    f"内容: {r['content'][:500]}{'...' if len(r['content']) > 500 else ''}"
                )
            
            search_time = (time.time() - start_time) * 1000
            logger.info(
                f"[KnowledgeSearch] query='{query[:50]}...', results={len(results)}, "
                f"hybrid={use_hybrid}, reranker={use_reranker}, "
                f"query_rewrite={rewrite_result.enabled}, "
                f"rewrite_variants={len(rewrite_result.vector_variants)}, "
                f"vector_hits={len(vector_rows)}, text_hits={len(text_rows)}, "
                f"time={search_time:.2f}ms"
            )
            output_parts.append(f"\n\n(搜索耗时: {search_time:.2f}ms)")
            
            return ToolResult(
                success=True,
                output="".join(output_parts),
                data={"results": results, "total": len(results), "search_time_ms": search_time}
            )
            
        except Exception as e:
            logger.error(f"知识库搜索失败: {e}")
            return ToolResult(
                success=False,
                output=f"搜索过程中发生错误: {str(e)}",
                error=str(e)
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


class CalculatorTool(Tool):
    """计算器工具 - 执行数学计算"""
    name = "calculator"
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
    
    async def execute(self, expression: str) -> ToolResult:
        """执行数学计算"""
        try:
            # 清理表达式
            expr = expression.strip()
            
            # 安全检查 - 只允许数字、运算符和白名单函数
            allowed_chars = set('0123456789+-*/%()., ')
            allowed_names = set(self.safe_functions.keys())
            
            # 提取所有标识符
            identifiers = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', expr)
            for name in identifiers:
                if name not in allowed_names:
                    return ToolResult(
                        success=False,
                        output=f"不支持的函数或变量: {name}",
                        error="invalid_identifier"
                    )
            
            # 执行计算
            result = eval(expr, {"__builtins__": {}}, self.safe_functions)
            
            # 格式化结果
            if isinstance(result, float):
                if result.is_integer():
                    result_str = str(int(result))
                else:
                    result_str = f"{result:.10g}"
            else:
                result_str = str(result)
            
            return ToolResult(
                success=True,
                output=f"计算结果: {expression} = {result_str}",
                data={"expression": expression, "result": result}
            )
            
        except ZeroDivisionError:
            return ToolResult(
                success=False,
                output="错误: 除数不能为零",
                error="division_by_zero"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output=f"计算错误: {str(e)}",
                error=str(e)
            )


class DateTimeTool(Tool):
    """日期时间工具"""
    name = "datetime"
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


class LiteratureSearchTool(Tool):
    """学术文献搜索工具 - 使用 Semantic Scholar 和 arXiv API"""
    name = "literature_search"
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
                "description": "数据源: semantic_scholar (默认，更全面) 或 arxiv (预印本，更新快)",
                "enum": ["semantic_scholar", "arxiv"],
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
    
    def __init__(self):
        from app.services.literature_service import get_literature_service
        self.service = get_literature_service()
    
    async def execute(
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
            
            result = await self.service.search(
                query=query,
                source=source,
                limit=max_results,
                **kwargs
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
        source_name = "Semantic Scholar" if source == "semantic_scholar" else "arXiv"
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


class ToolRegistry:
    """工具注册表 - 支持 Notebook 工具扩展"""
    _mcp_route_circuit_state: Dict[str, Dict[str, Any]] = {}
    
    def __init__(
        self, 
        db: AsyncSession = None, 
        db_session_factory: Optional[Callable[[], AsyncSession]] = None,
        user_id: int = None,
        # Notebook 上下文参数
        notebook_id: str = None,
        kernel_manager = None,
        notebooks_store: dict = None,
        user_authorized: bool = False  # 用户是否授权 Agent 操作 Notebook
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
        self._mcp_tool_routes: Dict[str, List[str]] = self._load_mcp_tool_routes()
        self._register_default_tools()
        
        # 如果提供了 Notebook 上下文，注册 Notebook 工具
        if notebook_id and kernel_manager:
            self._register_notebook_tools()

        self._init_mcp_client_manager()
    
    def _register_default_tools(self):
        """注册默认工具"""
        # 知识库搜索（需要数据库和用户ID）
        if (self.db or self.db_session_factory) and self.user_id:
            self.register(
                KnowledgeSearchTool(
                    self.db,
                    self.user_id,
                    db_session_factory=self.db_session_factory,
                )
            )
        
        # 通用工具（无需特殊依赖）
        self.register(WebSearchTool())
        self.register(CalculatorTool())
        self.register(DateTimeTool())
        self.register(TextAnalysisTool())
        self.register(UnitConverterTool())
        self.register(LiteratureSearchTool())
    
    def _register_notebook_tools(self):
        """注册 Notebook 专用工具"""
        try:
            from app.services.notebook_tools import (
                NotebookExecuteTool,
                NotebookVariablesTool,
                NotebookCellTool,
                PipInstallTool,
                WebScrapeTool,
                CodeAnalysisTool,
                EnhancedLiteratureSearchTool,
            )
            
            # 核心执行工具 - 需要内核和授权，执行后自动创建 Cell
            self.register(NotebookExecuteTool(
                kernel_manager=self.kernel_manager,
                notebook_id=self.notebook_id,
                notebooks_store=self.notebooks_store,
                user_authorized=self.user_authorized
            ))
            
            # 变量查看工具 - 只读，无需授权
            self.register(NotebookVariablesTool(
                kernel_manager=self.kernel_manager,
                notebook_id=self.notebook_id
            ))
            
            # 单元格操作工具 - 修改操作需要授权
            if self.notebooks_store is not None:
                self.register(NotebookCellTool(
                    notebooks_store=self.notebooks_store,
                    notebook_id=self.notebook_id,
                    user_authorized=self.user_authorized
                ))
            
            # pip 安装工具 - 需要授权
            self.register(PipInstallTool(user_authorized=self.user_authorized))
            
            # 网页爬取工具 - 无需授权
            self.register(WebScrapeTool())
            
            # 代码分析工具 - 无需授权
            self.register(CodeAnalysisTool())
            
            # 增强的文献搜索工具
            self.register(EnhancedLiteratureSearchTool())
            
            logger.info(f"已注册 Notebook 工具集，授权状态: {self.user_authorized}")
        except ImportError as e:
            logger.warning(f"无法导入 Notebook 工具: {e}")

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
    
    def register(self, tool: Tool):
        """注册工具"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self._tools.get(name) or self._mcp_tools.get(name)
    
    def list_tools(self) -> List[Dict[str, Any]]:
        """获取工具列表（用于发送给 LLM）"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            }
            for tool in self._iter_all_tools()
        ]
    
    def get_tools_description(self) -> str:
        """获取工具描述（用于 ReAct prompt）"""
        descriptions = []
        for tool in self._iter_all_tools():
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
                logger.info(f"工具执行完成: {tool_name}, 成功: {result.success}")
                return result
            except Exception as e:
                logger.error(f"工具执行失败 {tool_name}: {e}")
                return ToolResult(
                    success=False,
                    output=f"工具执行失败: {str(e)}",
                    error=str(e)
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

        return ToolResult(
            success=False,
            output=f"未找到工具: {tool_name}。可用工具: {', '.join([t.name for t in self._iter_all_tools()])}",
            error="tool_not_found"
        )


def get_tool_registry(
    db: Optional[AsyncSession],
    user_id: int,
    db_session_factory: Optional[Callable[[], AsyncSession]] = None,
) -> ToolRegistry:
    """获取工具注册表"""
    return ToolRegistry(db=db, user_id=user_id, db_session_factory=db_session_factory)
