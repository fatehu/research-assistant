"""
ReAct Agent 服务
实现 Reasoning + Acting 的智能代理框架

【增强版】改进了系统提示词，特别是 Notebook 单元格操作的说明
"""
import json
import inspect
import re
from typing import AsyncGenerator, List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService
from app.services.agent_tools import ToolRegistry, ToolResult
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.smart_chunking.token_utils import estimate_tokens


class AgentState(Enum):
    """Agent 状态"""
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    ANSWERING = "answering"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentStep:
    """Agent 执行步骤"""
    step_type: str  # thought, action, observation, answer
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[str] = None
    success: Optional[bool] = None


@dataclass
class AgentContext:
    """Agent 执行上下文"""
    messages: List[Dict[str, str]]
    steps: List[AgentStep] = field(default_factory=list)
    state: AgentState = AgentState.IDLE
    iteration: int = 0
    max_iterations: int = field(default_factory=lambda: settings.react_max_iterations)
    final_answer: str = ""
    error: Optional[str] = None
    allowed_source_labels: set[str] = field(default_factory=set)
    knowledge_search_calls: int = 0
    compression_calls: int = 0
    compression_success_chunks: int = 0
    compression_fallback_chunks: int = 0
    citation_repair_attempts: int = 0
    citation_repair_successes: int = 0


class ReActAgent:
    """
    ReAct Agent 实现
    
    ReAct (Reasoning + Acting) 框架流程:
    1. Thought: 分析问题，决定下一步行动
    2. Action: 选择并执行工具
    3. Observation: 观察工具执行结果
    4. 重复 1-3 直到得到最终答案
    5. Answer: 输出最终回答
    """
    
    # ReAct 系统提示词 - 【增强版】
    SYSTEM_PROMPT = """你是一个智能AI助手，可以使用以下工具来帮助回答问题：

{tools_description}

## 重要：输出格式要求

你必须严格按照以下XML格式输出，不要输出任何格式之外的内容：

**情况1 - 需要使用工具：**
```
<think>简要分析为什么需要使用工具</think>
<action>{{"tool": "工具名", "input": {{"参数": "值"}}}}</action>
```

**情况2 - 直接回答（不需要工具）：**
```
<think>简要分析</think>
<answer>你的回答内容</answer>
```

**情况3 - 收到工具结果后：**
```
<think>根据工具结果分析</think>
<answer>基于工具结果的完整回答</answer>
```

## 规则

1. **必须**使用 `<think>`, `<action>`, `<answer>` 标签
2. **禁止**在标签外输出任何内容
3. action 内容必须是合法的 JSON 格式
4. 每次只能调用一个工具
5. 使用中文回复

## 核心工具说明

### Notebook 单元格操作 (notebook_cell)
操作 Notebook 的单元格，支持以下操作:
- `get`: 获取所有单元格列表，显示每个单元格的【索引】和【ID】
- `add`: 添加新单元格
- `delete`: 删除单元格
- `update`: 更新单元格内容
- `move`: 移动单元格位置
- `get_one`: 获取单个单元格详情

**【重要】定位单元格的两种方式：**
1. `cell_index`: 使用从1开始的索引（推荐，如：第1个单元格索引为1）
2. `cell_id`: 使用单元格的UUID（如：abc123-def456-...）

**示例：**
```json
// 获取所有单元格
{{"tool": "notebook_cell", "input": {{"action": "get"}}}}

// 通过索引删除第2个单元格
{{"tool": "notebook_cell", "input": {{"action": "delete", "cell_index": 2}}}}

// 通过ID删除单元格
{{"tool": "notebook_cell", "input": {{"action": "delete", "cell_id": "abc123-..."}}}}

// 更新第3个单元格的内容
{{"tool": "notebook_cell", "input": {{"action": "update", "cell_index": 3, "content": "新内容"}}}}

// 在位置2插入新单元格
{{"tool": "notebook_cell", "input": {{"action": "add", "content": "代码", "index": 2}}}}
```

### 智能清理工具 (notebook_cleanup)
批量清理 Notebook 单元格，支持多种策略:
- `preview`: 预览分析，显示可清理的单元格（不实际删除）
- `duplicates`: 删除重复内容的单元格
- `empty`: 删除空白单元格
- `unexecuted`: 删除未执行的代码单元格
- `ai_created`: 删除AI创建的单元格
- `by_indices`: 批量删除指定索引的单元格

**示例：**
```json
// 预览分析
{{"tool": "notebook_cleanup", "input": {{"strategy": "preview"}}}}

// 清理重复单元格
{{"tool": "notebook_cleanup", "input": {{"strategy": "duplicates"}}}}

// 批量删除索引为2,3,5的单元格
{{"tool": "notebook_cleanup", "input": {{"strategy": "by_indices", "indices": [2, 3, 5]}}}}

// 预览模式（不实际删除）
{{"tool": "notebook_cleanup", "input": {{"strategy": "empty", "dry_run": true}}}}
```

### 其他工具
- `notebook_execute`: 执行代码，参数 code（代码）, description（描述）
- `notebook_variables`: 获取变量，参数 filter_type（类型过滤）
- `web_search`: 搜索互联网，参数 query（搜索词）
- `knowledge_search`: 搜索知识库，参数 query（搜索词）
- `calculator`: 数学计算，参数 expression（表达式如 sqrt(16)）
- `datetime`: 获取时间，参数 action（now/date/weekday）
- `pip_install`: 安装包，参数 packages（包列表）
- `code_analysis`: 代码分析，参数 code（代码）
- `error_diagnosis`: 错误诊断，参数 error_message（错误信息）

## 典型工作流程示例

**用户说"帮我清理无用的cell"：**
1. 先调用 `notebook_cleanup` 的 `preview` 策略分析
2. 根据分析结果，选择合适的清理策略
3. 执行清理（如 `duplicates` 或 `by_indices`）

**用户说"删除第2个单元格"：**
直接调用 `notebook_cell`：
```json
{{"tool": "notebook_cell", "input": {{"action": "delete", "cell_index": 2}}}}
```

**用户说"查看所有单元格"：**
```json
{{"tool": "notebook_cell", "input": {{"action": "get"}}}}
```
"""

    CITATION_POLICY_PROMPT = """
## 知识检索引用规范（必须遵守）
1. 当你基于 `knowledge_search` 返回内容作答时，关键结论后必须带 `[来源X]` 引用。
2. 引用编号必须来自 observation 中已出现的 `[来源X]`，禁止编造不存在的来源编号。
3. 若现有来源不足以支持结论，请明确说明“根据现有来源无法确认”。
4. 不要把 `<observation>` 原文整段照搬到 `<answer>`，只保留结论与必要引用。
""".strip()

    def __init__(
        self,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        max_iterations: int = None,
    ):
        self.llm = llm_service
        self.tools = tool_registry
        self.max_iterations = max_iterations if max_iterations is not None else settings.react_max_iterations
        self.contextual_compression_service = get_contextual_compression_service()
        self._last_tool_selection: Dict[str, Any] = {}
    
    @staticmethod
    def _latest_user_text(messages: Optional[List[Dict[str, str]]]) -> str:
        if not messages:
            return ""
        for item in reversed(messages):
            if str(item.get("role", "")).lower() == "user":
                return str(item.get("content", "") or "")
        return ""

    def _build_system_prompt(self, messages: Optional[List[Dict[str, str]]] = None) -> str:
        """构建系统提示词（支持按意图动态筛选工具描述）。"""
        user_text = self._latest_user_text(messages)
        intent = "general_chat"
        selected_tools: List[str] = []

        if bool(getattr(settings, "tool_selection_enabled", True)):
            classify_intent = getattr(self.tools, "classify_intent", None)
            if callable(classify_intent):
                try:
                    intent = str(classify_intent(user_text))
                except Exception:
                    intent = "general_chat"

            try:
                tools_desc = self.tools.get_tools_description(intent=intent, user_text=user_text)
            except TypeError:
                tools_desc = self.tools.get_tools_description()

            select_names = getattr(self.tools, "select_tool_names_for_intent", None)
            if callable(select_names):
                try:
                    selected_tools = list(select_names(intent, user_text=user_text))
                except Exception:
                    selected_tools = []
        else:
            tools_desc = self.tools.get_tools_description()

        desc_tokens = estimate_tokens(tools_desc)
        logger.info(
            f"[ReAct] tool-selection intent={intent}, selected_tools={selected_tools or 'ALL'}, "
            f"prompt_desc_tokens={desc_tokens}"
        )
        self._last_tool_selection = {
            "intent": intent,
            "selected_tools": selected_tools,
            "prompt_desc_tokens": desc_tokens,
        }
        base_prompt = self.SYSTEM_PROMPT.format(tools_description=tools_desc)
        return f"{base_prompt}\n\n{self.CITATION_POLICY_PROMPT}"

    @staticmethod
    def _build_observation_message(tool_name: str, observation_output: str) -> str:
        """Build follow-up prompt after one tool observation."""
        if tool_name == "knowledge_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中出现过的来源编号。"
                "如证据不足，请明确说明。请用<answer>标签给出最终回答。"
            )
        else:
            followup = "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。"

        return f"<observation>\n{observation_output}\n</observation>\n\n{followup}"

    @staticmethod
    def _extract_source_labels(text: str) -> set[str]:
        """Extract source numbers from [来源X] tokens."""
        if not text:
            return set()
        return set(re.findall(r"\[来源(\d+)\]", text))

    @staticmethod
    def _extract_answer_citations(answer: str) -> set[str]:
        """Extract citation numbers from final answer."""
        if not answer:
            return set()
        return set(re.findall(r"\[来源(\d+)\]", answer))

    @classmethod
    def _citations_are_valid(cls, answer: str, allowed_source_labels: set[str]) -> bool:
        """Check whether answer has citations and all are within allowed sources."""
        if not allowed_source_labels:
            return True
        cited = cls._extract_answer_citations(answer)
        return bool(cited) and cited.issubset(allowed_source_labels)

    @classmethod
    def _build_rag_metrics(cls, context: AgentContext) -> Dict[str, Any]:
        """Build RAG quality metrics for observability and regression baselines."""
        final_answer = (context.final_answer or "").strip()
        cited = cls._extract_answer_citations(final_answer)
        allowed = context.allowed_source_labels
        citation_required = bool(allowed)
        citation_valid = cls._citations_are_valid(final_answer, allowed) if citation_required else True

        return {
            "knowledge_search_calls": context.knowledge_search_calls,
            "source_labels_count": len(allowed),
            "source_labels": [f"来源{idx}" for idx in sorted(allowed, key=int)],
            "answer_citation_count": len(cited),
            "citation_required": citation_required,
            "citation_valid": citation_valid,
            "citation_repair_attempts": context.citation_repair_attempts,
            "citation_repair_successes": context.citation_repair_successes,
            "compression_calls": context.compression_calls,
            "compression_success_chunks": context.compression_success_chunks,
            "compression_fallback_chunks": context.compression_fallback_chunks,
        }

    async def _ensure_citation_compliance(
        self,
        answer: str,
        context: AgentContext,
    ) -> str:
        """
        Ensure final answer cites allowed [来源X] when knowledge_search was used.
        """
        clean_answer = (answer or "").strip()
        allowed = context.allowed_source_labels
        if not clean_answer or not allowed:
            return clean_answer
        if self._citations_are_valid(clean_answer, allowed):
            return clean_answer

        context.citation_repair_attempts += 1

        allowed_tokens = ", ".join(f"[来源{idx}]" for idx in sorted(allowed, key=int))
        repair_prompt = f"""
请修正下面这段回答的来源标注。

要求：
1. 关键结论必须带来源标注。
2. 只能使用这些来源标签：{allowed_tokens}
3. 不要新增事实，不要输出解释，只输出修正后的回答正文。

原回答：
{clean_answer}
""".strip()

        try:
            repaired_resp = await self.llm.chat(
                messages=[{"role": "user", "content": repair_prompt}],
                system_prompt=(
                    "你是一个引用修正助手。只修正来源标注，不改变事实与结构。"
                ),
                temperature=0.0,
                max_tokens=min(settings.llm_max_tokens, 1000),
            )
            repaired = str(repaired_resp.get("content") or "").strip()
            repaired = re.sub(r"</?answer>", "", repaired).strip()
            if repaired and self._citations_are_valid(repaired, allowed):
                context.citation_repair_successes += 1
                return repaired
        except Exception as exc:
            logger.warning(f"[ReAct] citation repair failed, fallback to annotated answer: {exc}")

        return (
            f"{clean_answer}\n\n"
            f"注：当前可用来源仅为 {allowed_tokens}，请按来源补充标注。"
        )
    
    def _parse_response(self, response: str) -> Dict[str, Any]:
        """
        解析 LLM 响应，提取思考、行动和回答
        """
        result = {
            "thought": None,
            "action": None,
            "answer": None,
            "raw": response,
        }
        
        # 提取思考内容
        think_match = re.search(r'<think>(.*?)</think>', response, re.DOTALL)
        if think_match:
            result["thought"] = think_match.group(1).strip()
        
        # 提取行动内容
        action_match = re.search(r'<action>(.*?)</action>', response, re.DOTALL)
        if action_match:
            try:
                action_str = action_match.group(1).strip()
                result["action"] = json.loads(action_str)
            except json.JSONDecodeError as e:
                logger.warning(f"无法解析 action JSON: {e}")
                # 尝试修复常见的 JSON 问题
                try:
                    # 替换单引号为双引号
                    fixed = action_str.replace("'", '"')
                    result["action"] = json.loads(fixed)
                except:
                    pass
        
        # 提取回答内容
        answer_match = re.search(r'<answer>(.*?)</answer>', response, re.DOTALL)
        if answer_match:
            result["answer"] = answer_match.group(1).strip()
        
        return result

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _compress_knowledge_observation(
        self,
        query: str,
        result: ToolResult,
        context: Optional[AgentContext] = None,
    ) -> str:
        data = result.data if isinstance(result.data, dict) else {}
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            return result.output

        compression_inputs: list[CompressionInput] = []
        for source_id, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            compression_inputs.append(
                CompressionInput(
                    source_id=source_id,
                    doc_name=str(row.get("document") or row.get("document_name") or "unknown_doc"),
                    chunk_idx=int(self._safe_float(row.get("chunk_index"), 0)),
                    chunk_content=str(row.get("content") or ""),
                    reranker_score=(
                        float(row.get("reranker_score"))
                        if row.get("reranker_score") is not None
                        else None
                    ),
                )
            )

        if not compression_inputs:
            return result.output

        if context is not None:
            context.compression_calls += 1

        compression_results = await self.contextual_compression_service.compress_chunks(
            query,
            compression_inputs,
        )

        compression_map = {item.source_id: item for item in compression_results}
        compressed_parts: list[str] = []
        for source_id, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue

            compressed = compression_map.get(source_id)
            retrieval_score = self._safe_float(row.get("score"), 0.0) * 100
            kb_name = row.get("knowledge_base") or row.get("knowledge_base_name") or "unknown_kb"
            doc_name = row.get("document") or row.get("document_name") or "unknown_doc"
            chunk_idx = int(self._safe_float(row.get("chunk_index"), 0))
            source_label = f"来源{source_id}"

            if compressed and compressed.relevant_content:
                content = compressed.relevant_content
                compression_score = compressed.relevance_score
                if context is not None:
                    context.compression_success_chunks += 1
            else:
                raw_content = str(row.get("content") or "").strip()
                if not raw_content:
                    continue
                content = f"[{source_label}] {raw_content[:320]}"
                if len(raw_content) > 320:
                    content += "..."
                compression_score = 0.0
                if context is not None:
                    context.compression_fallback_chunks += 1

            compressed_parts.append(
                f"\n[{source_label}] (retrieval score {retrieval_score:.1f}%)\n"
                f"Source: {kb_name} / {doc_name} / chunk {chunk_idx}\n"
                f"Compression score: {compression_score:.1f}/10\n"
                f"Content: {content}"
            )

        if not compressed_parts:
            return result.output
        return f"Compressed contexts: {len(compressed_parts)}\n" + "".join(compressed_parts)

    async def run(
        self,
        messages: List[Dict[str, str]],
        stream: bool = True,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        运行 ReAct Agent
        
        Args:
            messages: 对话历史
            stream: 是否流式输出
            
        Yields:
            事件字典，包含 type 和 data
        """
        context = AgentContext(
            messages=messages.copy(),
            max_iterations=self.max_iterations,
        )

        refresh_mcp_tools = getattr(self.tools, "refresh_mcp_tools", None)
        if callable(refresh_mcp_tools):
            try:
                maybe_awaitable = refresh_mcp_tools()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as exc:
                logger.warning(f"[ReAct] MCP tool refresh failed, continue with local tools: {exc}")

        system_prompt = self._build_system_prompt(messages=context.messages)
        
        # 发送开始事件
        yield {
            "type": "start",
            "data": {
                "provider": self.llm.provider,
                "model": self.llm.config["model"],
            }
        }
        
        while context.iteration < context.max_iterations:
            context.iteration += 1
            context.state = AgentState.THINKING
            
            logger.info(f"ReAct 迭代 {context.iteration}/{context.max_iterations}")
            
            # 调用 LLM
            if stream:
                async for event in self._stream_iteration(context, system_prompt):
                    yield event
                    
                    # 检查是否完成
                    if event["type"] == "answer":
                        context.state = AgentState.DONE
                        context.final_answer = event["data"]
                        break
                    elif event["type"] == "error":
                        context.state = AgentState.ERROR
                        context.error = event["data"]
                        break
            else:
                result = await self._run_iteration(context, system_prompt)
                for event in result:
                    yield event
            
            # 检查是否完成
            if context.state in [AgentState.DONE, AgentState.ERROR]:
                break
        
        # 如果达到最大迭代次数但没有答案，强制生成答案
        if context.state != AgentState.DONE and not context.final_answer:
            yield {
                "type": "thought",
                "data": "已达到最大迭代次数，根据已有信息生成回答。"
            }
            
            # 构建总结提示
            summary_messages = context.messages.copy()
            summary_messages.append({
                "role": "user",
                "content": "请根据以上信息直接给出最终回答，使用 <answer></answer> 标签包裹。"
            })
            
            full_response = ""
            async for chunk in self.llm.chat_stream(summary_messages, system_prompt):
                full_response += chunk
            
            parsed = self._parse_response(full_response)
            if parsed["answer"]:
                final_answer = await self._ensure_citation_compliance(parsed["answer"], context)
                context.final_answer = final_answer
                yield {"type": "answer", "data": final_answer}
            else:
                # 清理响应作为答案
                clean_answer = re.sub(r'</?(?:think|action|answer|observation)>', '', full_response).strip()
                clean_answer = await self._ensure_citation_compliance(clean_answer, context)
                context.final_answer = clean_answer
                yield {"type": "answer", "data": clean_answer}
        
        # 发送完成事件
        # 获取最后一次思考内容
        last_thought = ""
        for step in reversed(context.steps):
            if step.step_type == "thought":
                last_thought = step.content
                break
        
        logger.info(f"[ReAct] 完成: iterations={context.iteration}, steps={len(context.steps)}, answer_len={len(context.final_answer)}")

        rag_metrics = self._build_rag_metrics(context)
        
        yield {
            "type": "done",
            "data": {
                "iterations": context.iteration,
                "steps": len(context.steps),
                "thought": last_thought,
                "answer": context.final_answer,
                "rag_metrics": rag_metrics,
            }
        }
    
    async def _stream_iteration(
        self,
        context: AgentContext,
        system_prompt: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        执行一次流式迭代
        """
        full_response = ""
        buffer = ""
        current_mode = None
        think_content = ""
        action_content = ""
        answer_content = ""
        
        logger.info(f"[ReAct] 开始迭代 {context.iteration}")
        
        yield {"type": "thinking_start", "data": ""}
        
        async for chunk in self.llm.chat_stream(context.messages, system_prompt):
            full_response += chunk
            buffer += chunk
            
            # 状态机解析
            while True:
                processed = False
                
                if current_mode is None:
                    # 检查是否进入某个标签
                    if "<think>" in buffer:
                        idx = buffer.find("<think>")
                        # 丢弃 <think> 之前的内容
                        buffer = buffer[idx + 7:]
                        current_mode = "think"
                        logger.debug(f"[ReAct] 进入 think 模式")
                        processed = True
                    elif "<action>" in buffer:
                        idx = buffer.find("<action>")
                        buffer = buffer[idx + 8:]
                        current_mode = "action"
                        logger.debug(f"[ReAct] 进入 action 模式")
                        processed = True
                    elif "<answer>" in buffer:
                        idx = buffer.find("<answer>")
                        buffer = buffer[idx + 8:]
                        current_mode = "answer"
                        logger.debug(f"[ReAct] 进入 answer 模式")
                        processed = True
                        
                elif current_mode == "think":
                    if "</think>" in buffer:
                        idx = buffer.find("</think>")
                        think_content += buffer[:idx]
                        buffer = buffer[idx + 8:]
                        current_mode = None
                        
                        final_thought = think_content.strip()
                        logger.info(f"[ReAct] 思考完成: {final_thought[:100]}...")
                        
                        # 记录思考步骤
                        step = AgentStep(
                            step_type="thought",
                            content=final_thought
                        )
                        context.steps.append(step)
                        
                        yield {"type": "thought", "data": final_thought}
                        think_content = ""  # 重置
                        processed = True
                    else:
                        # 缓冲直到看到结束标签
                        break
                        
                elif current_mode == "action":
                    if "</action>" in buffer:
                        idx = buffer.find("</action>")
                        action_content += buffer[:idx]
                        buffer = buffer[idx + 9:]
                        current_mode = None
                        
                        action_str = action_content.strip()
                        logger.info(f"[ReAct] 行动: {action_str[:100]}...")
                        
                        try:
                            action_data = json.loads(action_str)
                            tool_name = action_data.get("tool")
                            tool_input = action_data.get("input", {})
                            
                            # 记录行动步骤
                            step = AgentStep(
                                step_type="action",
                                content=action_str,
                                tool_name=tool_name,
                                tool_input=tool_input
                            )
                            context.steps.append(step)
                            
                            yield {
                                "type": "action",
                                "data": {"tool": tool_name, "input": tool_input}
                            }
                            
                            # 执行工具
                            logger.info(f"[ReAct] 执行工具: {tool_name}")
                            result = await self.tools.execute(tool_name, **tool_input)
                            observation_output = result.output
                            if tool_name == "knowledge_search":
                                context.knowledge_search_calls += 1
                                observation_output = await self._compress_knowledge_observation(
                                    str(tool_input.get("query", "")),
                                    result,
                                    context=context,
                                )
                                context.allowed_source_labels.update(
                                    self._extract_source_labels(observation_output)
                                )
                            step.tool_output = observation_output
                            step.success = result.success
                            
                            yield {
                                "type": "observation",
                                "data": {
                                    "tool": tool_name,
                                    "success": result.success,
                                    "output": observation_output,
                                    "data": result.data
                                }
                            }
                            
                            # 更新对话历史
                            context.messages.append({
                                "role": "assistant",
                                "content": full_response
                            })
                            context.messages.append({
                                "role": "user",
                                "content": self._build_observation_message(tool_name, observation_output)
                            })
                            
                        except json.JSONDecodeError as e:
                            logger.error(f"[ReAct] 解析 action 失败: {e}")
                            yield {
                                "type": "error",
                                "data": f"Action 解析失败: {e}"
                            }
                        
                        action_content = ""  # 重置
                        processed = True
                        return  # 结束当前迭代，进入下一轮
                    else:
                        # 缓冲直到看到结束标签
                        break
                        
                elif current_mode == "answer":
                    if "</answer>" in buffer:
                        idx = buffer.find("</answer>")
                        answer_content += buffer[:idx]
                        buffer = buffer[idx + 9:]
                        current_mode = None
                        
                        final_answer = answer_content.strip()
                        final_answer = await self._ensure_citation_compliance(final_answer, context)
                        logger.info(f"[ReAct] 回答完成: {final_answer[:100]}...")
                        
                        # 记录回答步骤
                        step = AgentStep(
                            step_type="answer",
                            content=final_answer
                        )
                        context.steps.append(step)
                        
                        yield {"type": "answer", "data": final_answer}
                        return
                    else:
                        # 流式输出回答内容
                        if len(buffer) > 15:
                            send_chunk = buffer[:-15]
                            answer_content += send_chunk
                            yield {"type": "content", "data": send_chunk}
                            buffer = buffer[-15:]
                        break
                
                if not processed:
                    break
        
        # 处理剩余缓冲区
        logger.info(f"[ReAct] 处理剩余缓冲区, current_mode={current_mode}, buffer长度={len(buffer)}")
        logger.debug(f"[ReAct] 完整响应: {full_response}")
        
        # 如果还在某个模式中，处理剩余内容
        if current_mode == "think" and buffer.strip():
            think_content += buffer
            final_thought = re.sub(r'</think>.*', '', think_content).strip()
            if final_thought:
                yield {"type": "thought", "data": final_thought}
        elif current_mode == "answer" and buffer.strip():
            answer_content += buffer
            final_answer = re.sub(r'</answer>.*', '', answer_content).strip()
            if final_answer:
                final_answer = await self._ensure_citation_compliance(final_answer, context)
                yield {"type": "answer", "data": final_answer}
        elif current_mode == "action" and buffer.strip():
            # action 模式但没有结束标签
            action_content += buffer
            action_str = action_content.strip()
            logger.warning(f"[ReAct] action 模式未正常结束，尝试解析: {action_str}")
            
            # 尝试提取 JSON
            json_match = re.search(r'\{[^{}]*"tool"[^{}]*\}', action_str)
            if json_match:
                try:
                    action_data = json.loads(json_match.group())
                    tool_name = action_data.get("tool")
                    tool_input = action_data.get("input", {})
                    
                    logger.info(f"[ReAct] 从未结束的 action 中提取到工具调用: {tool_name}")
                    
                    yield {
                        "type": "action",
                        "data": {"tool": tool_name, "input": tool_input}
                    }
                    
                    result = await self.tools.execute(tool_name, **tool_input)
                    
                    observation_output = result.output
                    
                    if tool_name == "knowledge_search":
                        context.knowledge_search_calls += 1
                        observation_output = await self._compress_knowledge_observation(
                            str(tool_input.get("query", "")),
                            result,
                            context=context,
                        )
                        context.allowed_source_labels.update(
                            self._extract_source_labels(observation_output)
                        )
                    
                    yield {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": result.success,
                            "output": observation_output,
                            "data": result.data
                        }
                    }
                    
                    # 更新上下文继续迭代
                    context.messages.append({
                        "role": "assistant",
                        "content": full_response
                    })
                    context.messages.append({
                        "role": "user", 
                        "content": self._build_observation_message(tool_name, observation_output)
                    })
                    return
                except Exception as e:
                    logger.error(f"[ReAct] 解析未结束的 action 失败: {e}")
        
        # 检查完整响应中是否有未被解析的 action（裸 JSON）
        if not answer_content:
            # 尝试检测裸 JSON 格式的 action
            json_pattern = r'\{[^{}]*"tool"\s*:\s*"[^"]+"\s*,\s*"input"\s*:\s*\{[^{}]*\}[^{}]*\}'
            json_matches = re.findall(json_pattern, full_response)
            
            if json_matches:
                logger.warning(f"[ReAct] 检测到裸 JSON action: {json_matches[0][:100]}...")
                try:
                    action_data = json.loads(json_matches[0])
                    tool_name = action_data.get("tool")
                    tool_input = action_data.get("input", {})
                    
                    yield {
                        "type": "action",
                        "data": {"tool": tool_name, "input": tool_input}
                    }
                    
                    result = await self.tools.execute(tool_name, **tool_input)
                    
                    observation_output = result.output
                    
                    if tool_name == "knowledge_search":
                        context.knowledge_search_calls += 1
                        observation_output = await self._compress_knowledge_observation(
                            str(tool_input.get("query", "")),
                            result,
                            context=context,
                        )
                        context.allowed_source_labels.update(
                            self._extract_source_labels(observation_output)
                        )
                    
                    yield {
                        "type": "observation",
                        "data": {
                            "tool": tool_name,
                            "success": result.success,
                            "output": observation_output,
                            "data": result.data
                        }
                    }
                    
                    # 更新上下文继续迭代
                    context.messages.append({
                        "role": "assistant",
                        "content": full_response
                    })
                    context.messages.append({
                        "role": "user",
                        "content": self._build_observation_message(tool_name, observation_output)
                    })
                    return
                except Exception as e:
                    logger.error(f"[ReAct] 解析裸 JSON action 失败: {e}")
            
            # 如果还是没有答案，清理响应作为答案
            clean_response = re.sub(r'</?(?:think|action|answer|observation)>', '', full_response)
            # 移除 JSON 对象
            clean_response = re.sub(r'\{[^{}]*"tool"[^{}]*\}', '', clean_response).strip()
            if clean_response:
                clean_response = await self._ensure_citation_compliance(clean_response, context)
                logger.warning(f"[ReAct] 未找到标准格式，使用清理后的响应作为答案")
                yield {"type": "answer", "data": clean_response}
    
    async def _run_iteration(
        self,
        context: AgentContext,
        system_prompt: str,
    ) -> List[Dict[str, Any]]:
        """
        执行一次非流式迭代
        """
        events = []
        
        # 调用 LLM
        response = await self.llm.chat(context.messages, system_prompt)
        content = response["content"]
        
        # 解析响应
        parsed = self._parse_response(content)
        
        # 处理思考
        if parsed["thought"]:
            events.append({"type": "thought", "data": parsed["thought"]})
            context.steps.append(AgentStep(
                step_type="thought",
                content=parsed["thought"]
            ))
        
        # 处理行动
        if parsed["action"]:
            tool_name = parsed["action"].get("tool")
            tool_input = parsed["action"].get("input", {})
            
            events.append({
                "type": "action",
                "data": {"tool": tool_name, "input": tool_input}
            })
            
            # 执行工具
            result = await self.tools.execute(tool_name, **tool_input)
            observation_output = result.output
            if tool_name == "knowledge_search":
                context.knowledge_search_calls += 1
                observation_output = await self._compress_knowledge_observation(
                    str(tool_input.get("query", "")),
                    result,
                    context=context,
                )
                context.allowed_source_labels.update(
                    self._extract_source_labels(observation_output)
                )
            
            events.append({
                "type": "observation",
                "data": {
                    "tool": tool_name,
                    "success": result.success,
                    "output": observation_output,
                    "data": result.data
                }
            })
            
            context.steps.append(AgentStep(
                step_type="action",
                content=json.dumps(parsed["action"]),
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=observation_output,
                success=result.success
            ))
            
            # 更新对话历史
            context.messages.append({
                "role": "assistant",
                "content": content
            })
            context.messages.append({
                "role": "user",
                "content": self._build_observation_message(tool_name, observation_output)
            })
        
        # 处理回答
        if parsed["answer"]:
            final_answer = await self._ensure_citation_compliance(parsed["answer"], context)
            events.append({"type": "answer", "data": final_answer})
            context.final_answer = final_answer
            context.state = AgentState.DONE
            context.steps.append(AgentStep(
                step_type="answer",
                content=final_answer
            ))
        
        return events


def create_react_agent(
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    max_iterations: int = None,
) -> ReActAgent:
    """创建 ReAct Agent 实例"""
    if max_iterations is None:
        max_iterations = settings.react_max_iterations
    logger.info(f"[ReAct] 创建 Agent, 最大迭代次数: {max_iterations}")
    return ReActAgent(llm_service, tool_registry, max_iterations)
