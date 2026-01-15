"""
Agent 工具定义和执行
"""
import json
import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.models.knowledge import KnowledgeBase, Document, DocumentChunk
from app.services.embedding_service import get_embedding_service


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


class KnowledgeSearchTool(Tool):
    """知识库搜索工具 - 使用 pgvector 进行向量检索"""
    name = "knowledge_search"
    description = "搜索用户的知识库，检索与查询相关的文档片段。用于回答需要参考用户上传文档的问题。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询内容"
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量，默认5",
                "default": 5
            }
        },
        "required": ["query"]
    }
    
    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id
        self.embedding_service = get_embedding_service()
    
    async def execute(self, query: str, top_k: int = 5) -> ToolResult:
        """执行知识库搜索 - 使用 pgvector 原生向量搜索"""
        try:
            start_time = time.time()
            
            # 生成查询向量
            query_embedding = await self.embedding_service.embed_text(query)
            if not query_embedding:
                return ToolResult(
                    success=False,
                    output="无法生成查询向量",
                    error="embedding_failed"
                )
            
            # 获取用户的知识库ID列表
            kb_query = select(KnowledgeBase.id).where(KnowledgeBase.user_id == self.user_id)
            kb_result = await self.db.execute(kb_query)
            kb_ids = [row[0] for row in kb_result.fetchall()]
            
            if not kb_ids:
                return ToolResult(
                    success=True,
                    output="用户没有创建任何知识库，无法搜索相关内容。",
                    data={"results": [], "total": 0}
                )
            
            # 使用 pgvector 进行向量相似度搜索
            # <=> 是余弦距离运算符，返回 1 - cosine_similarity
            vector_str = f"[{','.join(str(x) for x in query_embedding)}]"
            
            sql = text("""
                SELECT 
                    dc.id,
                    dc.document_id,
                    dc.knowledge_base_id,
                    dc.content,
                    dc.chunk_index,
                    1 - (dc.embedding <=> :query_vector) as similarity,
                    d.original_filename as document_name,
                    kb.name as knowledge_base_name
                FROM document_chunks dc
                JOIN documents d ON dc.document_id = d.id
                JOIN knowledge_bases kb ON dc.knowledge_base_id = kb.id
                WHERE dc.knowledge_base_id = ANY(:kb_ids)
                    AND dc.embedding IS NOT NULL
                    AND (dc.embedding <=> :query_vector) <= 0.5
                ORDER BY dc.embedding <=> :query_vector
                LIMIT :top_k
            """)
            
            result = await self.db.execute(sql, {
                "query_vector": vector_str,
                "kb_ids": kb_ids,
                "top_k": top_k
            })
            rows = result.fetchall()
            
            if not rows:
                return ToolResult(
                    success=True,
                    output="未找到与查询相关的内容。",
                    data={"results": [], "total": 0}
                )
            
            # 构建结果
            results = []
            for row in rows:
                results.append({
                    "content": row.content,
                    "score": round(float(row.similarity), 4),
                    "document": row.document_name or "未知",
                    "knowledge_base": row.knowledge_base_name or "未知",
                    "chunk_index": row.chunk_index,
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


class ToolRegistry:
    """工具注册表"""
    
    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id
        self._tools: Dict[str, Tool] = {}
        self._register_default_tools()
    
    def _register_default_tools(self):
        """注册默认工具"""
        self.register(KnowledgeSearchTool(self.db, self.user_id))
    
    def register(self, tool: Tool):
        """注册工具"""
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self._tools.get(name)
    
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
            for tool in self._tools.values()
        ]
    
    def get_tools_description(self) -> str:
        """获取工具描述（用于 ReAct prompt）"""
        descriptions = []
        for tool in self._tools.values():
            params_desc = ", ".join(
                f"{k}: {v.get('description', v.get('type', 'any'))}"
                for k, v in tool.parameters.get('properties', {}).items()
            )
            descriptions.append(f"- {tool.name}({params_desc}): {tool.description}")
        return "\n".join(descriptions)
    
    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行工具"""
        tool = self.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output=f"未找到工具: {tool_name}",
                error="tool_not_found"
            )
        
        try:
            return await tool.execute(**kwargs)
        except Exception as e:
            logger.error(f"工具执行失败 {tool_name}: {e}")
            return ToolResult(
                success=False,
                output=f"工具执行失败: {str(e)}",
                error=str(e)
            )


def get_tool_registry(db: AsyncSession, user_id: int) -> ToolRegistry:
    """获取工具注册表"""
    return ToolRegistry(db, user_id)
