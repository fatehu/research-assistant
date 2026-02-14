from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence

from loguru import logger

from app.config import settings
from app.services.agent_runtime_service import (
    AgentRuntimeService,
    MemoryContext,
    get_agent_runtime_service,
)
from app.services.agent_tools import ToolRegistry, ToolResult
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.llm_service import LLMService
from app.services.smart_chunking.token_utils import estimate_tokens


class AgentState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    OBSERVING = "observing"
    ANSWERING = "answering"
    DONE = "done"
    ERROR = "error"


@dataclass
class AgentStep:
    step_type: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    tool_name: Optional[str] = None
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: Optional[str] = None
    success: Optional[bool] = None


@dataclass
class AgentRuntimeContext:
    user_id: Optional[int] = None
    channel: str = "chat"
    conversation_id: Optional[int] = None
    notebook_id: Optional[str] = None


@dataclass
class AgentContext:
    messages: List[Dict[str, Any]]
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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    run_id: Optional[str] = None
    persist_events: List[Dict[str, Any]] = field(default_factory=list)
    context_truncated: bool = False
    context_summary: str = ""
    memory_contexts: List[MemoryContext] = field(default_factory=list)


@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    arguments: Dict[str, Any]
    arguments_raw: str = ""


@dataclass
class ExecutedToolCall:
    action_event: Dict[str, Any]
    observation_event: Dict[str, Any]
    tool_message: Dict[str, Any]
    tool_name: str
    observation_output: str


class AgentCore:
    SYSTEM_PROMPT = """你是一个智能AI助手，可以使用以下工具来帮助回答问题：

{tools_description}

当模型不支持 function calling 时：
1. 需要工具：<think>...</think><action>{{"tool":"工具名","input":{{...}}}}</action>
2. 直接回答：<think>...</think><answer>...</answer>
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
        max_iterations: Optional[int] = None,
        runtime_context: Optional[AgentRuntimeContext] = None,
        runtime_service: Optional[AgentRuntimeService] = None,
    ):
        self.llm = llm_service
        self.tools = tool_registry
        self.max_iterations = max_iterations if max_iterations is not None else settings.react_max_iterations
        self.contextual_compression_service = get_contextual_compression_service()
        self.runtime_context = runtime_context or AgentRuntimeContext()
        self.runtime_service = runtime_service or get_agent_runtime_service()
        self._last_tool_selection: Dict[str, Any] = {}

    @staticmethod
    def _latest_user_text(messages: Optional[Sequence[Dict[str, Any]]]) -> str:
        for item in reversed(messages or []):
            if str(item.get("role", "")).lower() == "user":
                return str(item.get("content", "") or "")
        return ""

    @staticmethod
    def _strip_think_content(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL).strip()

    @staticmethod
    def _sanitize_message_for_context(message: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(message)
        if str(out.get("role", "")).lower() == "assistant":
            out["content"] = AgentCore._strip_think_content(out.get("content", ""))
        return out

    @staticmethod
    def _estimate_messages_tokens(messages: Sequence[Dict[str, Any]]) -> int:
        return sum(4 + estimate_tokens(str(m.get("content", "") or "")) for m in messages)

    @staticmethod
    def _summarize_messages(messages: Sequence[Dict[str, Any]], max_lines: int = 8) -> str:
        lines: List[str] = []
        for msg in messages:
            content = str(msg.get("content", "") or "").replace("\n", " ").strip()
            if not content:
                continue
            lines.append(f"- {msg.get('role', 'unknown')}: {content[:120]}")
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)

    def _build_system_prompt(self, messages: Optional[List[Dict[str, Any]]] = None) -> str:
        user_text = self._latest_user_text(messages)
        intent = "general_chat"
        selected_tools: List[str] = []

        if bool(getattr(settings, "tool_selection_enabled", True)):
            classify = getattr(self.tools, "classify_intent", None)
            if callable(classify):
                try:
                    intent = str(classify(user_text))
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
        self._last_tool_selection = {
            "intent": intent,
            "selected_tools": selected_tools,
            "prompt_desc_tokens": desc_tokens,
        }
        logger.info(
            f"[AgentCore] intent={intent} selected_tools={selected_tools or 'ALL'} prompt_desc_tokens={desc_tokens}"
        )
        return f"{self.SYSTEM_PROMPT.format(tools_description=tools_desc)}\n\n{self.CITATION_POLICY_PROMPT}"

    @staticmethod
    def _build_observation_message(tool_name: str, observation_output: str) -> str:
        if tool_name == "knowledge_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中出现过的来源编号。"
                "如证据不足，请明确说明。请用<answer>标签给出最终回答。"
            )
        else:
            followup = "请根据工具返回的信息继续。如果已有足够信息，请用<answer>标签给出最终回答。"
        return f"<observation>\n{observation_output}\n</observation>\n\n{followup}"

    @classmethod
    def _build_observation_message_multi(cls, observations: Sequence[ExecutedToolCall]) -> str:
        if not observations:
            return cls._build_observation_message("", "")
        has_knowledge = any(item.tool_name == "knowledge_search" for item in observations)
        output = "\n\n".join(f"[{item.tool_name}]\n{item.observation_output}" for item in observations)
        if has_knowledge:
            followup = "请综合所有 observation，答案中的引用必须只使用 observation 已出现过的 [来源X]。"
        else:
            followup = "请综合所有 observation 后继续。"
        return f"<observation>\n{output}\n</observation>\n\n{followup}"

    @staticmethod
    def _extract_source_labels(text: str) -> set[str]:
        return set(re.findall(r"\[来源(\d+)\]", text or ""))

    @staticmethod
    def _extract_answer_citations(answer: str) -> set[str]:
        return set(re.findall(r"\[来源(\d+)\]", answer or ""))

    @classmethod
    def _citations_are_valid(cls, answer: str, allowed_source_labels: set[str]) -> bool:
        if not allowed_source_labels:
            return True
        cited = cls._extract_answer_citations(answer)
        return bool(cited) and cited.issubset(allowed_source_labels)

    @classmethod
    def _build_rag_metrics(cls, context: AgentContext) -> Dict[str, Any]:
        cited = cls._extract_answer_citations(context.final_answer or "")
        allowed = context.allowed_source_labels
        citation_required = bool(allowed)
        citation_valid = cls._citations_are_valid(context.final_answer or "", allowed) if citation_required else True
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

    async def _ensure_citation_compliance(self, answer: str, context: AgentContext) -> str:
        clean = (answer or "").strip()
        allowed = context.allowed_source_labels
        if not clean or not allowed or self._citations_are_valid(clean, allowed):
            return clean
        context.citation_repair_attempts += 1
        allowed_tokens = ", ".join(f"[来源{idx}]" for idx in sorted(allowed, key=int))
        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": f"只修正来源标注，只能使用：{allowed_tokens}\n\n{clean}"}],
                system_prompt="你是引用修正助手。",
                temperature=0.0,
                max_tokens=min(settings.llm_max_tokens, 1000),
            )
            fixed = re.sub(r"</?answer>", "", str(resp.get("content") or "")).strip()
            if fixed and self._citations_are_valid(fixed, allowed):
                context.citation_repair_successes += 1
                return fixed
        except Exception as exc:
            logger.warning(f"[AgentCore] citation repair failed: {exc}")
        return f"{clean}\n\n注：当前可用来源仅为 {allowed_tokens}。"

    def _parse_response(self, response: str) -> Dict[str, Any]:
        result = {"thought": None, "action": None, "answer": None, "raw": response}
        think_match = re.search(r"<think>(.*?)</think>", response, re.DOTALL)
        if think_match:
            result["thought"] = think_match.group(1).strip()
        action_match = re.search(r"<action>(.*?)</action>", response, re.DOTALL)
        if action_match:
            payload = action_match.group(1).strip()
            for candidate in (payload, payload.replace("'", '"')):
                try:
                    result["action"] = json.loads(candidate)
                    break
                except Exception:
                    pass
        answer_match = re.search(r"<answer>(.*?)</answer>", response, re.DOTALL)
        if answer_match:
            result["answer"] = answer_match.group(1).strip()
        return result

    @staticmethod
    def _parse_actions(response: str) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        for match in re.finditer(r"<action>(.*?)</action>", response, re.DOTALL):
            payload = match.group(1).strip()
            for candidate in (payload, payload.replace("'", '"')):
                try:
                    action = json.loads(candidate)
                    if isinstance(action, dict) and action.get("tool"):
                        actions.append(action)
                        break
                except Exception:
                    continue
        return actions

    @staticmethod
    def _extract_answer_text(content: str) -> str:
        m = re.search(r"<answer>(.*?)</answer>", content or "", re.DOTALL)
        if m:
            return m.group(1).strip()
        return re.sub(r"</?(?:think|action|observation|answer)>", "", content or "").strip()

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
                    reranker_score=float(row["reranker_score"]) if row.get("reranker_score") is not None else None,
                )
            )
        if not compression_inputs:
            return result.output

        if context is not None:
            context.compression_calls += 1

        compression_results = await self.contextual_compression_service.compress_chunks(query, compression_inputs)
        compression_map = {item.source_id: item for item in compression_results}
        parts: List[str] = []

        for source_id, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            source_label = f"来源{source_id}"
            compressed = compression_map.get(source_id)
            if compressed and compressed.relevant_content:
                content = compressed.relevant_content
                score = compressed.relevance_score
                if context is not None:
                    context.compression_success_chunks += 1
            else:
                raw = str(row.get("content") or "").strip()
                if not raw:
                    continue
                content = f"[{source_label}] {raw[:320]}" + ("..." if len(raw) > 320 else "")
                score = 0.0
                if context is not None:
                    context.compression_fallback_chunks += 1

            retrieval_score = self._safe_float(row.get("score"), 0.0) * 100
            kb_name = row.get("knowledge_base") or row.get("knowledge_base_name") or "unknown_kb"
            doc_name = row.get("document") or row.get("document_name") or "unknown_doc"
            chunk_idx = int(self._safe_float(row.get("chunk_index"), 0))
            parts.append(
                f"\n[{source_label}] (retrieval score {retrieval_score:.1f}%)\n"
                f"Source: {kb_name} / {doc_name} / chunk {chunk_idx}\n"
                f"Compression score: {score:.1f}/10\n"
                f"Content: {content}"
            )
        if not parts:
            return result.output
        return f"Compressed contexts: {len(parts)}\n" + "".join(parts)

    async def _prepare_runtime_context(self, context: AgentContext) -> None:
        refresh_mcp_tools = getattr(self.tools, "refresh_mcp_tools", None)
        if callable(refresh_mcp_tools):
            try:
                maybe_awaitable = refresh_mcp_tools()
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable
            except Exception as exc:
                logger.warning(f"[AgentCore] MCP tool refresh failed, continue with local tools: {exc}")

        if not self.runtime_context.user_id:
            return
        user_text = self._latest_user_text(context.messages)
        classify = getattr(self.tools, "classify_intent", None)
        intent = "general_chat"
        if callable(classify):
            try:
                intent = str(classify(user_text))
            except Exception as exc:
                logger.warning(f"[AgentCore] classify_intent failed, fallback to general_chat: {exc}")
        select_names = getattr(self.tools, "select_tool_names_for_intent", None)
        selected_tools: List[str] = []
        if callable(select_names):
            try:
                selected_tools = list(select_names(intent, user_text=user_text))
            except Exception as exc:
                logger.warning(f"[AgentCore] select_tool_names_for_intent failed, fallback to empty: {exc}")

        if bool(getattr(settings, "agent_persist_steps_enabled", True)):
            try:
                context.run_id = await self.runtime_service.create_run(
                    user_id=self.runtime_context.user_id,
                    channel=self.runtime_context.channel,
                    conversation_id=self.runtime_context.conversation_id,
                    notebook_id=self.runtime_context.notebook_id,
                    intent=intent,
                    selected_tools=selected_tools,
                    model_provider=getattr(self.llm, "provider", None),
                    model_name=(getattr(self.llm, "config", {}) or {}).get("model"),
                    metadata={"path": "agent_core"},
                )
            except Exception as exc:
                logger.warning(f"[AgentCore] create_run failed: {exc}")

        if bool(getattr(settings, "agent_longterm_memory_enabled", False)):
            try:
                if self.runtime_context.conversation_id is not None:
                    scope_type, scope_id = "conversation", str(self.runtime_context.conversation_id)
                elif self.runtime_context.notebook_id is not None:
                    scope_type, scope_id = "notebook", str(self.runtime_context.notebook_id)
                else:
                    scope_type, scope_id = "user", str(self.runtime_context.user_id)
                context.memory_contexts = await self.runtime_service.recall(
                    user_id=self.runtime_context.user_id,
                    channel=self.runtime_context.channel,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    query=user_text,
                    top_k=3,
                )
            except Exception as exc:
                logger.warning(f"[AgentCore] memory recall failed: {exc}")

    def _memory_prompt(self, memories: Sequence[MemoryContext]) -> str:
        lines = ["以下是可参考的跨会话记忆（仅在相关时使用）："]
        for i, item in enumerate(memories, start=1):
            lines.append(f"- 记忆{i} score={item.score}: {item.content[:180]}")
        return "\n".join(lines)

    async def _prepare_llm_messages(self, context: AgentContext, system_prompt: str) -> List[Dict[str, Any]]:
        sanitized = [self._sanitize_message_for_context(item) for item in context.messages]
        if not bool(getattr(settings, "agent_context_budget_enabled", True)):
            return sanitized

        user_indices = [idx for idx, msg in enumerate(sanitized) if str(msg.get("role", "")).lower() == "user"]
        window_turns = max(int(getattr(settings, "agent_context_window_turns", 8)), 1)
        if len(user_indices) > window_turns:
            start_idx = user_indices[-window_turns]
            older, recent = sanitized[:start_idx], sanitized[start_idx:]
        else:
            older, recent = [], sanitized

        prefixes: List[Dict[str, Any]] = []
        if context.context_summary:
            prefixes.append({"role": "system", "content": f"历史摘要：\n{context.context_summary}"})
        if context.memory_contexts:
            prefixes.append({"role": "system", "content": self._memory_prompt(context.memory_contexts)})
        if older:
            summary = self._summarize_messages(older, max_lines=10)
            prefixes.append({"role": "system", "content": f"更早历史摘要：\n{summary}"})
            context.context_summary = summary
            if self.runtime_context.conversation_id and self._estimate_messages_tokens(sanitized) >= int(
                getattr(settings, "agent_context_summary_trigger_tokens", 7000)
            ):
                try:
                    await self.runtime_service.upsert_conversation_summary(self.runtime_context.conversation_id, summary)
                except Exception as exc:
                    logger.warning(f"[AgentCore] upsert summary failed: {exc}")

        candidate = prefixes + recent
        max_input_tokens = max(int(getattr(settings, "agent_context_max_input_tokens", 10000)), 1024)
        budget = max(256, max_input_tokens - estimate_tokens(system_prompt))

        def is_observation(msg: Dict[str, Any]) -> bool:
            role = str(msg.get("role", "")).lower()
            return role == "tool" or "<observation>" in str(msg.get("content", ""))

        while self._estimate_messages_tokens(candidate) > budget and len(candidate) > 1:
            drop = None
            last_user = max(
                [idx for idx, item in enumerate(candidate) if str(item.get("role", "")).lower() == "user"] or [len(candidate) - 1]
            )
            for idx, item in enumerate(candidate):
                if idx < last_user and is_observation(item):
                    drop = idx
                    break
            if drop is None:
                for idx in range(last_user):
                    drop = idx
                    break
            if drop is None:
                break
            candidate.pop(drop)
            context.context_truncated = True

        return candidate

    def _collect_llm_tool_schemas(self, user_text: str) -> List[Dict[str, Any]]:
        intent = self._last_tool_selection.get("intent")
        selected = self._last_tool_selection.get("selected_tools") or []
        try:
            if selected:
                return self.tools.list_tools(include_tool_names=set(selected), user_text=user_text)
            if intent:
                return self.tools.list_tools(intent=intent, user_text=user_text)
            return self.tools.list_tools(user_text=user_text)
        except TypeError:
            return self.tools.list_tools()

    @staticmethod
    def _normalize_tool_calls(tool_calls: Sequence[Dict[str, Any]]) -> List[ParsedToolCall]:
        out: List[ParsedToolCall] = []
        for raw in tool_calls:
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            raw_args = str(raw.get("arguments") or "{}")
            try:
                parsed_args = json.loads(raw_args)
                arguments = parsed_args if isinstance(parsed_args, dict) else {}
            except Exception:
                arguments = {}
            out.append(
                ParsedToolCall(
                    call_id=str(raw.get("id") or uuid.uuid4().hex),
                    name=name,
                    arguments=arguments,
                    arguments_raw=raw_args,
                )
            )
        return out

    def _append_step_from_event(self, context: AgentContext, event: Dict[str, Any]) -> None:
        et = event.get("type")
        data = event.get("data")
        if et == "thought":
            context.steps.append(AgentStep(step_type="thought", content=str(data or "")))
        elif et == "action" and isinstance(data, dict):
            context.steps.append(
                AgentStep(
                    step_type="action",
                    content=json.dumps(data, ensure_ascii=False),
                    tool_name=str(data.get("tool") or ""),
                    tool_input=data.get("input") if isinstance(data.get("input"), dict) else {},
                )
            )
        elif et == "observation" and isinstance(data, dict):
            context.steps.append(
                AgentStep(
                    step_type="observation",
                    content=str(data.get("output") or ""),
                    tool_name=str(data.get("tool") or ""),
                    tool_output=str(data.get("output") or ""),
                    success=bool(data.get("success")),
                )
            )
        elif et == "answer":
            context.steps.append(AgentStep(step_type="answer", content=str(data or "")))

    async def _execute_single_tool_call(
        self,
        context: AgentContext,
        call: ParsedToolCall,
        *,
        parallel_group: str,
    ) -> ExecutedToolCall:
        action_event = {
            "type": "action",
            "data": {
                "tool": call.name,
                "input": call.arguments,
                "tool_call_id": call.call_id,
                "parallel_group": parallel_group,
                "iteration": context.iteration,
            },
        }
        try:
            result = await self.tools.execute(call.name, **call.arguments)
        except Exception as exc:
            result = ToolResult(success=False, output=f"工具执行失败: {exc}", error=type(exc).__name__)

        observation_output = result.output
        if call.name == "knowledge_search":
            context.knowledge_search_calls += 1
            observation_output = await self._compress_knowledge_observation(
                str(call.arguments.get("query", "")),
                result,
                context=context,
            )
            context.allowed_source_labels.update(self._extract_source_labels(observation_output))

        observation_event = {
            "type": "observation",
            "data": {
                "tool": call.name,
                "success": bool(result.success),
                "output": observation_output,
                "data": result.data,
                "error": result.error,
                "execution_time_ms": float(getattr(result, "execution_time_ms", 0.0) or 0.0),
                "output_tokens_estimate": int(getattr(result, "output_tokens_estimate", 0) or 0),
                "truncated": bool(getattr(result, "truncated", False)),
                "tool_call_id": call.call_id,
                "parallel_group": parallel_group,
                "iteration": context.iteration,
                "input": call.arguments,
            },
        }
        return ExecutedToolCall(
            action_event=action_event,
            observation_event=observation_event,
            tool_name=call.name,
            observation_output=observation_output,
            tool_message={
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": observation_output,
            },
        )

    async def _execute_tool_calls(self, context: AgentContext, calls: Sequence[ParsedToolCall]) -> List[ExecutedToolCall]:
        if not calls:
            return []
        parallel_enabled = bool(getattr(settings, "agent_parallel_tool_calls_enabled", True))
        max_concurrency = max(int(getattr(settings, "agent_parallel_tool_calls_max_concurrency", 4)), 1)
        group_id = f"iter-{context.iteration}-{uuid.uuid4().hex[:6]}"
        results: List[Optional[ExecutedToolCall]] = [None] * len(calls)

        async def _run_one(idx: int, sem: Optional[asyncio.Semaphore] = None) -> None:
            if sem is None:
                results[idx] = await self._execute_single_tool_call(context, calls[idx], parallel_group=group_id)
                return
            async with sem:
                results[idx] = await self._execute_single_tool_call(context, calls[idx], parallel_group=group_id)

        if not parallel_enabled or len(calls) == 1:
            for i in range(len(calls)):
                await _run_one(i)
        else:
            sem = asyncio.Semaphore(max_concurrency)
            pending: List[asyncio.Task[None]] = []
            for i, call in enumerate(calls):
                tool_obj = self.tools.get(call.name) if hasattr(self.tools, "get") else None
                is_safe = bool(getattr(tool_obj, "parallel_safe", False)) if tool_obj is not None else False
                if is_safe:
                    pending.append(asyncio.create_task(_run_one(i, sem)))
                else:
                    if pending:
                        await asyncio.gather(*pending)
                        pending = []
                    await _run_one(i)
            if pending:
                await asyncio.gather(*pending)
        return [item for item in results if item is not None]

    @staticmethod
    def _accumulate_usage(context: AgentContext, payload: Dict[str, Any]) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return
        context.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        context.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        context.total_tokens += int(usage.get("total_tokens", 0) or 0)

    async def _run_iteration_function_calling(
        self,
        context: AgentContext,
        llm_messages: List[Dict[str, Any]],
        system_prompt: str,
    ) -> tuple[List[Dict[str, Any]], bool]:
        user_text = self._latest_user_text(context.messages)
        response = await self.llm.chat_with_tools(
            messages=llm_messages,
            tools=self._collect_llm_tool_schemas(user_text),
            system_prompt=system_prompt,
            temperature=settings.react_temperature,
            max_tokens=settings.llm_max_tokens,
            tool_choice="auto",
        )
        self._accumulate_usage(context, response)
        content = str(response.get("content") or "")
        parsed_calls = self._normalize_tool_calls(response.get("tool_calls") or [])
        events: List[Dict[str, Any]] = []

        if content.strip() and parsed_calls:
            thought = self._strip_think_content(content)
            if thought:
                events.append({"type": "thought", "data": thought})

        if parsed_calls:
            context.messages.append(
                {
                    "role": "assistant",
                    "content": self._strip_think_content(content),
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_raw or json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in parsed_calls
                    ],
                }
            )
            executed = await self._execute_tool_calls(context, parsed_calls)
            for item in executed:
                events.append(item.action_event)
                events.append(item.observation_event)
                context.messages.append(item.tool_message)
            return events, False

        answer = self._extract_answer_text(content)
        if answer:
            answer = await self._ensure_citation_compliance(answer, context)
            context.final_answer = answer
            context.state = AgentState.DONE
            events.append({"type": "answer", "data": answer})
            return events, True
        return events, False

    async def _run_iteration_xml(
        self,
        context: AgentContext,
        llm_messages: List[Dict[str, Any]],
        system_prompt: str,
    ) -> tuple[List[Dict[str, Any]], bool]:
        response = await self.llm.chat(
            messages=llm_messages,
            system_prompt=system_prompt,
            temperature=settings.react_temperature,
            max_tokens=settings.llm_max_tokens,
        )
        self._accumulate_usage(context, response)
        content = str(response.get("content") or "")
        parsed = self._parse_response(content)
        events: List[Dict[str, Any]] = []

        if parsed.get("thought"):
            events.append({"type": "thought", "data": parsed["thought"]})

        actions = self._parse_actions(content)
        if not actions and isinstance(parsed.get("action"), dict):
            actions = [parsed["action"]]
        if actions:
            parsed_calls: List[ParsedToolCall] = []
            for idx, action in enumerate(actions, start=1):
                parsed_calls.append(
                    ParsedToolCall(
                        call_id=f"xml-{context.iteration}-{idx}",
                        name=str(action.get("tool") or ""),
                        arguments=action.get("input") if isinstance(action.get("input"), dict) else {},
                        arguments_raw=json.dumps(action, ensure_ascii=False),
                    )
                )
            context.messages.append({"role": "assistant", "content": self._strip_think_content(content)})
            executed = await self._execute_tool_calls(context, parsed_calls)
            for item in executed:
                events.append(item.action_event)
                events.append(item.observation_event)
            context.messages.append({"role": "user", "content": self._build_observation_message_multi(executed)})
            return events, False

        answer = parsed.get("answer") or self._extract_answer_text(content)
        if answer:
            answer = await self._ensure_citation_compliance(str(answer), context)
            context.final_answer = answer
            context.state = AgentState.DONE
            events.append({"type": "answer", "data": answer})
            return events, True
        return events, False

    async def _persist_run_completion(self, context: AgentContext, status: str) -> None:
        if not context.run_id:
            return
        try:
            await self.runtime_service.append_steps(context.run_id, context.persist_events)
            await self.runtime_service.complete_run(
                context.run_id,
                status=status,
                prompt_tokens=context.prompt_tokens,
                completion_tokens=context.completion_tokens,
                total_tokens=context.total_tokens,
                iteration_count=context.iteration,
                metadata={
                    "intent": self._last_tool_selection.get("intent"),
                    "selected_tools": self._last_tool_selection.get("selected_tools") or [],
                    "prompt_desc_tokens": self._last_tool_selection.get("prompt_desc_tokens", 0),
                    "context_truncated": context.context_truncated,
                },
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] persist failed: {exc}")

    async def _persist_memory(self, context: AgentContext) -> None:
        if not bool(getattr(settings, "agent_longterm_memory_enabled", False)):
            return
        if not self.runtime_context.user_id or not context.final_answer:
            return

        if self.runtime_context.conversation_id is not None:
            scope_type, scope_id = "conversation", str(self.runtime_context.conversation_id)
        elif self.runtime_context.notebook_id is not None:
            scope_type, scope_id = "notebook", str(self.runtime_context.notebook_id)
        else:
            scope_type, scope_id = "user", str(self.runtime_context.user_id)

        try:
            await self.runtime_service.remember(
                user_id=self.runtime_context.user_id,
                channel=self.runtime_context.channel,
                scope_type=scope_type,
                scope_id=scope_id,
                content=f"用户问题: {self._latest_user_text(context.messages)}\n回答: {context.final_answer[:1000]}",
                importance=0.65,
                metadata={"source": "agent_answer"},
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] remember failed: {exc}")

    async def run(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = True,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        context = AgentContext(
            messages=[self._sanitize_message_for_context(item) for item in messages],
            max_iterations=self.max_iterations,
        )
        await self._prepare_runtime_context(context)

        yield {
            "type": "start",
            "data": {
                "provider": getattr(self.llm, "provider", ""),
                "model": (getattr(self.llm, "config", {}) or {}).get("model"),
            },
        }

        answer_emitted = False
        try:
            for i in range(1, context.max_iterations + 1):
                context.iteration = i
                context.state = AgentState.THINKING
                yield {"type": "thinking_start", "data": ""}

                system_prompt = self._build_system_prompt(context.messages)
                llm_messages = await self._prepare_llm_messages(context, system_prompt)

                use_fc = False
                supports_fc = getattr(self.llm, "supports_function_calling", None)
                if callable(supports_fc):
                    try:
                        use_fc = bool(supports_fc())
                    except Exception:
                        use_fc = False

                if use_fc:
                    try:
                        events, done = await self._run_iteration_function_calling(context, llm_messages, system_prompt)
                    except Exception as exc:
                        logger.warning(f"[AgentCore] function-calling failed, fallback={exc}")
                        if bool(getattr(settings, "agent_function_calling_fallback_xml", True)):
                            events, done = await self._run_iteration_xml(context, llm_messages, system_prompt)
                        else:
                            raise
                else:
                    events, done = await self._run_iteration_xml(context, llm_messages, system_prompt)

                for event in events:
                    self._append_step_from_event(context, event)
                    if event.get("type") in {"thought", "action", "observation", "answer", "content", "error"}:
                        context.persist_events.append(event)
                    if event.get("type") == "answer":
                        answer_emitted = True
                    yield event
                if done:
                    break

            if not context.final_answer:
                context.final_answer = "未能在限制轮次内完成回答，请重试或缩小问题范围。"
            if not answer_emitted:
                context.final_answer = await self._ensure_citation_compliance(context.final_answer, context)
                answer_event = {"type": "answer", "data": context.final_answer}
                self._append_step_from_event(context, answer_event)
                context.persist_events.append(answer_event)
                yield answer_event

            context.state = AgentState.DONE
            await self._persist_memory(context)
            rag_metrics = self._build_rag_metrics(context)
            yield {
                "type": "done",
                "data": {
                    "iterations": context.iteration,
                    "steps": len(context.steps),
                    "thought": next((s.content for s in reversed(context.steps) if s.step_type == "thought"), ""),
                    "answer": context.final_answer,
                    "rag_metrics": rag_metrics,
                },
            }
            await self._persist_run_completion(context, status="success")
            logger.info(
                f"[AgentCore] done iterations={context.iteration} steps={len(context.steps)} "
                f"total_tokens={context.total_tokens} truncated={context.context_truncated}"
            )
        except Exception as exc:
            context.state = AgentState.ERROR
            context.error = str(exc)
            err = {"type": "error", "data": str(exc)}
            context.persist_events.append(err)
            yield err
            await self._persist_run_completion(context, status="error")
            logger.exception(f"[AgentCore] run failed: {exc}")


class ReActAgent(AgentCore):
    """Compatibility alias."""


def create_react_agent(
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    max_iterations: Optional[int] = None,
    runtime_context: Optional[AgentRuntimeContext] = None,
    runtime_service: Optional[AgentRuntimeService] = None,
) -> ReActAgent:
    if max_iterations is None:
        max_iterations = settings.react_max_iterations
    logger.info(f"[AgentCore] create agent max_iterations={max_iterations}")
    return ReActAgent(
        llm_service=llm_service,
        tool_registry=tool_registry,
        max_iterations=max_iterations,
        runtime_context=runtime_context,
        runtime_service=runtime_service,
    )
