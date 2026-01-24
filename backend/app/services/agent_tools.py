"""
Agent 工具定义和执行
"""
import json
import time
import math
import re
import httpx
from datetime import datetime
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
                    output="用户没有创建任何知识库，无法搜索相关内容。建议用户先上传文档到知识库。",
                    data={"results": [], "total": 0}
                )
            
            # 使用 pgvector 进行向量相似度搜索
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
                    output="未找到与查询相关的内容。可能知识库中没有相关信息，或者需要调整搜索关键词。",
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
    
    def __init__(
        self, 
        db: AsyncSession = None, 
        user_id: int = None,
        # Notebook 上下文参数
        notebook_id: str = None,
        kernel_manager = None,
        notebooks_store: dict = None,
        user_authorized: bool = False  # 用户是否授权 Agent 操作 Notebook
    ):
        self.db = db
        self.user_id = user_id
        self.notebook_id = notebook_id
        self.kernel_manager = kernel_manager
        self.notebooks_store = notebooks_store
        self.user_authorized = user_authorized
        self._tools: Dict[str, Tool] = {}
        self._register_default_tools()
        
        # 如果提供了 Notebook 上下文，注册 Notebook 工具
        if notebook_id and kernel_manager:
            self._register_notebook_tools()
    
    def _register_default_tools(self):
        """注册默认工具"""
        # 知识库搜索（需要数据库和用户ID）
        if self.db and self.user_id:
            self.register(KnowledgeSearchTool(self.db, self.user_id))
        
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
        tool = self.get(tool_name)
        if not tool:
            return ToolResult(
                success=False,
                output=f"未找到工具: {tool_name}。可用工具: {', '.join(self._tools.keys())}",
                error="tool_not_found"
            )
        
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


def get_tool_registry(db: AsyncSession, user_id: int) -> ToolRegistry:
    """获取工具注册表"""
    return ToolRegistry(db, user_id)
