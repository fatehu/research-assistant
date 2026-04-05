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
from app.services.chat_context_store import ConversationItemStreamStore, build_context_snapshot_payload
from app.services.agent_tools import ToolRegistry, ToolResult
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
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
    turn_id: Optional[str] = None
    chat_preferences_override: Dict[str, Any] = field(default_factory=dict)
    rag_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    messages: List[Dict[str, Any]]
    turn_id: Optional[str] = None
    steps: List[AgentStep] = field(default_factory=list)
    state: AgentState = AgentState.IDLE
    iteration: int = 0
    max_iterations: int = field(default_factory=lambda: settings.react_max_iterations)
    final_answer: str = ""
    error: Optional[str] = None
    allowed_source_labels: set[str] = field(default_factory=set)
    allowed_web_source_labels: set[str] = field(default_factory=set)
    next_knowledge_source_label: int = 1
    next_web_source_label: int = 1
    knowledge_search_calls: int = 0
    web_search_calls: int = 0
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
    message_tokens_before_trim: int = 0
    message_tokens_after_trim: int = 0
    context_summary: str = ""
    compacted_history: Dict[str, Any] = field(default_factory=dict)
    conversation_state: Dict[str, Any] = field(default_factory=dict)
    item_stream: Dict[str, Any] = field(default_factory=dict)
    history_messages: List[Dict[str, Any]] = field(default_factory=list)
    memory_contexts: List[MemoryContext] = field(default_factory=list)
    memory_enabled: bool = False
    user_chat_preferences: Dict[str, Any] = field(default_factory=dict)
    active_rag_overrides: Dict[str, Any] = field(default_factory=dict)
    tool_failure_streaks: Dict[str, int] = field(default_factory=dict)
    context_debug: Dict[str, Any] = field(default_factory=dict)
    reasoning_summary: str = ""
    mid_run_compactions: int = 0


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
    tool_call_id: str
    arguments: Dict[str, Any]
    success: bool
    error: Optional[str]
    permission_required: bool
    execution_time_ms: float
    output_tokens_estimate: int
    truncated: bool


@dataclass
class RoutingDecision:
    intent: str = "general_chat"
    intent_user_text: str = ""
    carry_over_previous_goal: bool = False
    needs_tools: Optional[bool] = None
    confidence: float = 0.0
    reason: str = ""
    source: str = "rule"
    latest_user_text: str = ""


@dataclass
class PreparedDirectResponse:
    context: AgentContext
    system_prompt: str
    llm_messages: List[Dict[str, Any]]
    routing_decision: Optional[RoutingDecision] = None


@dataclass
class PreparedContextPreview:
    context: AgentContext
    system_prompt: str
    llm_messages: List[Dict[str, Any]]
    routing_decision: Optional[RoutingDecision] = None
    preview_mode: str = "agent"


class AgentCore:
    SYSTEM_PROMPT = """你是一个智能AI助手，可以使用以下工具来帮助回答问题：

{tools_description}

当模型不支持 function calling 时：
1. 需要工具：<think>...</think><action>{{"tool":"工具名","input":{{...}}}}</action>
2. 直接回答：<think>...</think><answer>...</answer>
"""
    FUNCTION_CALLING_SYSTEM_PROMPT = """你是一个智能AI助手。

可用工具会通过独立的 tool/function schema 提供，不会在这里重复列出工具目录。
当工具能显著提升答案质量时再调用；优先选择最少且最合适的工具，避免为同一目标重复搜索、重复抓取或重复读取。
当已经获得足够证据时，直接给出答案。
"""
    _FOLLOWUP_ONLY_PATTERNS = (
        r"^\s*(继续|继续说|继续讲|接着说|展开|展开讲讲|详细说说|详细讲讲|细讲|再说说|再展开一点|还有呢|然后呢)\s*$",
        r"^\s*(为什么|怎么回事|什么意思|具体呢|那呢|这个呢|那个呢)\s*[？?]?\s*$",
        r"^\s*(继续|展开|详细|具体|那|这个|那个).{0,8}\s*$",
    )

    CITATION_POLICY_PROMPT = """
## 知识检索引用规范（必须遵守）
1. 当你基于 `knowledge_search` 返回内容作答时，关键结论后必须带 `[来源X]` 引用。
2. 当你基于 `web_search` 返回内容作答时，关键结论后必须带 `[网页X]` 引用。
3. `[来源X]` 只对应知识库检索，`[网页X]` 只对应公网检索，禁止混用。
4. 引用编号必须来自 observation 中已出现的标签，禁止编造不存在的来源编号。
5. 若现有来源不足以支持结论，请明确说明“根据现有来源无法确认”。
6. 不要把 `<observation>` 原文整段照搬到 `<answer>`，只保留结论与必要引用。
""".strip()
    DIRECT_RESPONSE_SYSTEM_PROMPT = (
        "你是一个专业的AI科研助手。请直接输出最终中文回答。"
        "不要输出<think>、<thinking>、<answer>、<action>等标签，也不要描述工具调用过程。"
        "如果上下文中已有 [来源X] 或 [网页X]，只能复用已有编号，不能编造新的来源编号。"
    )
    _DIRECT_EXPLICIT_TOOL_PATTERNS = (
        r"(联网|上网|网页|网站|搜索|搜一下|查一下|查最新|最新新闻|最新信息|浏览器)",
        r"\b(web|search|google|browse|bing|news|latest)\b",
        r"(知识库|kb|rag|文档库|向量检索|检索资料|上传文档)",
        r"(论文|paper|arxiv|引文|参考文献)",
        r"(写代码|实现|notebook|调试|运行代码|执行代码|单元格|变量|脚本)",
    )
    _DIRECT_OBVIOUS_CHAT_PATTERNS = (
        r"^(?:(?:请|请你|麻烦|帮我|直接)\s*)*(?:用)?(?:一句话)?\s*(解释|介绍|概述|总结|说明|聊聊|讲讲)",
        r"(是什么|什么意思|怎么理解|为什么|区别是什么|有哪些特点)",
        r"^(你好|hi|hello|早上好|下午好|晚上好)[!！。,. ]*$",
    )
    _DIRECT_FOLLOWUP_CHAT_PATTERNS = (
        r"^(?:(?:再|继续|接着|顺便)\s*)*(?:用)?(?:一|两)?句话\s*(补充|解释|说明|总结|概括)",
        r"^(?:(?:再|继续|接着|顺便)\s*)*(补充|解释|说明|展开|详细说说|详细讲讲|概括|总结)",
        r"(它|这个|那|该机制|这种机制).*(解决了什么问题|有什么作用|有什么限制|为什么重要)",
    )

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
        self._routing_decision: Optional[RoutingDecision] = None
        self._active_chat_preferences: Dict[str, Any] = {}
        self._active_rag_overrides: Dict[str, Any] = {}

    @staticmethod
    def _latest_user_text(messages: Optional[Sequence[Dict[str, Any]]]) -> str:
        for item in reversed(messages or []):
            if str(item.get("role", "")).lower() == "user":
                return str(item.get("content", "") or "")
        return ""

    @classmethod
    def _looks_like_followup_only(cls, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        return any(re.match(pattern, clean, re.IGNORECASE) for pattern in cls._FOLLOWUP_ONLY_PATTERNS)

    @classmethod
    def _intent_user_text(cls, messages: Optional[Sequence[Dict[str, Any]]]) -> str:
        user_texts = [
            str(item.get("content", "") or "").strip()
            for item in (messages or [])
            if str(item.get("role", "")).lower() == "user" and str(item.get("content", "") or "").strip()
        ]
        if not user_texts:
            return ""
        latest = user_texts[-1]
        if len(user_texts) == 1:
            return latest
        if cls._looks_like_followup_only(latest):
            return "\n".join(user_texts[-2:])
        return latest

    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _routing_decision_for_messages(
        self,
        messages: Optional[Sequence[Dict[str, Any]]],
    ) -> Optional[RoutingDecision]:
        decision = self._routing_decision
        if decision is None:
            return None
        latest_user_text = self._latest_user_text(messages)
        if latest_user_text and decision.latest_user_text == latest_user_text:
            return decision
        return None

    def _current_user_text(self, context: AgentContext) -> str:
        latest = self._latest_user_text(context.messages)
        if latest:
            return latest
        decision = self._routing_decision
        if decision and str(decision.latest_user_text or "").strip():
            return str(decision.latest_user_text or "").strip()
        selection = dict(self._last_tool_selection or {})
        latest = str(selection.get("intent_user_text") or "").strip()
        return latest

    @staticmethod
    def _user_texts(messages: Optional[Sequence[Dict[str, Any]]]) -> List[str]:
        return [
            str(item.get("content", "") or "").strip()
            for item in (messages or [])
            if str(item.get("role", "")).lower() == "user" and str(item.get("content", "") or "").strip()
        ]

    @classmethod
    def _maybe_short_circuit_direct_routing(
        cls,
        messages: Optional[Sequence[Dict[str, Any]]],
    ) -> Optional[RoutingDecision]:
        latest_user_text = str(cls._latest_user_text(messages) or "").strip()
        if not latest_user_text:
            return None

        user_texts = cls._user_texts(messages)
        if len(user_texts) != 1:
            return None

        recent_roles = {
            str(item.get("role", "")).strip().lower()
            for item in list(messages or [])
            if isinstance(item, dict)
        }
        if any(role in {"assistant", "tool"} for role in recent_roles):
            return None

        compact = " ".join(latest_user_text.split())
        if len(compact) > 240:
            return None

        lowered = compact.lower()
        if any(re.search(pattern, compact, re.IGNORECASE) for pattern in cls._DIRECT_EXPLICIT_TOOL_PATTERNS):
            return None

        if not any(re.search(pattern, compact, re.IGNORECASE) for pattern in cls._DIRECT_OBVIOUS_CHAT_PATTERNS):
            if "?" not in compact and "？" not in compact:
                return None
            if len(lowered) > 120:
                return None

        return RoutingDecision(
            intent="general_chat",
            intent_user_text=latest_user_text,
            carry_over_previous_goal=False,
            needs_tools=False,
            confidence=0.98,
            reason="obvious_single_turn_direct_chat",
            source="heuristic_direct",
            latest_user_text=latest_user_text,
        )

    @classmethod
    def _maybe_short_circuit_followup_direct_routing(
        cls,
        messages: Optional[Sequence[Dict[str, Any]]],
    ) -> Optional[RoutingDecision]:
        latest_user_text = str(cls._latest_user_text(messages) or "").strip()
        if not latest_user_text:
            return None

        user_texts = cls._user_texts(messages)
        if len(user_texts) < 2:
            return None

        recent_roles = [
            str(item.get("role", "")).strip().lower()
            for item in list(messages or [])
            if isinstance(item, dict)
        ]
        if "tool" in recent_roles:
            return None
        if "assistant" not in recent_roles:
            return None

        compact = " ".join(latest_user_text.split())
        if not compact or len(compact) > 120:
            return None
        if any(re.search(pattern, compact, re.IGNORECASE) for pattern in cls._DIRECT_EXPLICIT_TOOL_PATTERNS):
            return None

        looks_like_followup = cls._looks_like_followup_only(compact) or any(
            re.search(pattern, compact, re.IGNORECASE) for pattern in cls._DIRECT_FOLLOWUP_CHAT_PATTERNS
        )
        if not looks_like_followup:
            followup_hint_tokens = (
                "它",
                "这个",
                "那",
                "如果",
                "为什么",
                "怎么",
                "会出现什么",
                "还能",
                "能不能",
            )
            looks_like_followup = (
                len(compact) <= 80
                and any(token in compact for token in followup_hint_tokens)
            )
        if not looks_like_followup:
            return None

        return RoutingDecision(
            intent="general_chat",
            intent_user_text=cls._intent_user_text(messages),
            carry_over_previous_goal=True,
            needs_tools=False,
            confidence=0.94,
            reason="obvious_followup_direct_chat",
            source="heuristic_direct_followup",
            latest_user_text=latest_user_text,
        )

    async def _prepare_routing_decision(
        self,
        messages: Optional[Sequence[Dict[str, Any]]],
    ) -> Optional[RoutingDecision]:
        latest_user_text = self._latest_user_text(messages)
        if not latest_user_text:
            self._routing_decision = None
            return None
        decision = self._maybe_short_circuit_direct_routing(messages)
        if decision is None:
            decision = self._maybe_short_circuit_followup_direct_routing(messages)
        self._routing_decision = decision
        return decision

    async def resolve_routing_decision(
        self,
        messages: List[Dict[str, Any]],
    ) -> Optional[RoutingDecision]:
        sanitized = [self._sanitize_message_for_context(item) for item in messages]
        self._routing_decision = None
        await self._prepare_routing_decision(sanitized)
        return self._routing_decision_for_messages(sanitized)

    def _build_direct_response_system_prompt(self) -> str:
        prompt = f"{self.DIRECT_RESPONSE_SYSTEM_PROMPT}\n\n{self.CITATION_POLICY_PROMPT}"
        user_pref_prompt = self._render_user_chat_preferences(self._active_chat_preferences)
        if user_pref_prompt:
            prompt = f"{prompt}\n\n## 用户已确认的聊天偏好\n{user_pref_prompt}"
        return prompt

    def _supports_function_calling(self) -> bool:
        supports_fc = getattr(self.llm, "supports_function_calling", None)
        if not callable(supports_fc):
            return False
        try:
            return bool(supports_fc())
        except Exception:
            return False

    async def prepare_direct_response(
        self,
        messages: List[Dict[str, Any]],
        *,
        force_no_tools: bool = False,
    ) -> Optional[PreparedDirectResponse]:
        context = AgentContext(
            messages=[self._sanitize_message_for_context(item) for item in messages],
            turn_id=self.runtime_context.turn_id,
            max_iterations=self.max_iterations,
        )
        self._routing_decision = None
        await self._prepare_runtime_context(context)
        decision = await self._prepare_routing_decision(context.messages)
        if not force_no_tools and (not decision or decision.needs_tools is not False):
            return None
        self._build_system_prompt(context.messages, function_calling=self._supports_function_calling())
        system_prompt = self._build_direct_response_system_prompt()
        llm_messages = await self._prepare_llm_messages(context, system_prompt)
        self._augment_context_debug_with_model_request(
            context=context,
            system_prompt=system_prompt,
            llm_messages=llm_messages,
            request_mode="direct",
        )
        return PreparedDirectResponse(
            context=context,
            system_prompt=system_prompt,
            llm_messages=llm_messages,
            routing_decision=decision,
        )

    async def prepare_context_preview(
        self,
        messages: List[Dict[str, Any]],
    ) -> PreparedContextPreview:
        context = AgentContext(
            messages=[self._sanitize_message_for_context(item) for item in messages],
            turn_id=self.runtime_context.turn_id,
            max_iterations=self.max_iterations,
        )
        self._routing_decision = None
        await self._prepare_runtime_context(context)
        decision = await self._prepare_routing_decision(context.messages)
        preview_mode = "agent"
        use_fc = False
        system_prompt = ""
        if decision and decision.needs_tools is False:
            preview_mode = "direct"
            system_prompt = self._build_direct_response_system_prompt()
        else:
            use_fc = self._supports_function_calling()
            system_prompt = self._build_system_prompt(context.messages, function_calling=use_fc)
        llm_messages = await self._prepare_llm_messages(context, system_prompt)
        self._augment_context_debug_with_model_request(
            context=context,
            system_prompt=system_prompt,
            llm_messages=llm_messages,
            request_mode="direct" if preview_mode == "direct" else ("function_calling" if use_fc else "xml"),
        )
        return PreparedContextPreview(
            context=context,
            system_prompt=system_prompt,
            llm_messages=llm_messages,
            routing_decision=decision,
            preview_mode=preview_mode,
        )

    def _channel_tool_policy_prompt(
        self,
        available_tools: Sequence[str],
    ) -> str:
        channel = str(getattr(self.runtime_context, "channel", "") or "").strip().lower()
        if channel not in {"codelab_agent", "notebook_agent"}:
            return ""

        selected = {str(name or "").strip() for name in available_tools if str(name or "").strip()}
        if not selected.intersection({"notebook_execute", "notebook_variables", "notebook_cell", "code_analysis"}):
            return ""

        lines = [
            "## CodeLab 场景规则（必须遵守）",
            "1. 你当前在 CodeLab Notebook 中工作，默认先使用 `notebook_cell`、`notebook_variables`、`notebook_execute` 和当前工作区文件解决问题。",
            "2. 只要问题涉及当前 notebook、当前 cell、变量、上传文件、csv/xlsx/数据集、建模、画图或调试，就先按本地 Notebook 任务处理。",
            "3. 除非用户明确要求“查知识库”“联网”“搜索网页”，否则不要调用 `knowledge_search`、`web_search`、`web_scrape` 或任何 `mcp.*` 工具。",
            "4. 修复已有单元格时优先围绕当前/最近相关 cell 操作，不要脱离当前 notebook 另起一套无关方案。",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _normalize_think_tag_aliases(text: str) -> str:
        normalized = str(text or "")
        normalized = normalized.replace("<thinking>", "<think>")
        normalized = normalized.replace("</thinking>", "</think>")
        return normalized

    @classmethod
    def _extract_think_text(cls, text: str) -> str:
        normalized = cls._normalize_think_tag_aliases(text)
        match = re.search(r"<think>(.*?)</think>", normalized, re.DOTALL)
        return match.group(1).strip() if match else ""

    @classmethod
    def _strip_think_content(cls, text: str) -> str:
        normalized = cls._normalize_think_tag_aliases(text)
        return re.sub(r"<think>.*?</think>", "", normalized, flags=re.DOTALL).strip()

    @staticmethod
    def _normalize_display_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip()

    @classmethod
    def _looks_like_answer_draft(cls, text: str, answer_hint: str = "") -> bool:
        raw = str(text or "")
        clean = cls._normalize_display_text(raw)
        if not clean:
            return False

        signals = 0
        if len(clean) >= 220:
            signals += 1
        if re.search(r"(^|\n)\s*(?:[-*•]|\d+\.)\s+", raw):
            signals += 1
        if any(marker in raw for marker in ("##", "**", "[来源")):
            signals += 1
        if any(
            token in clean
            for token in (
                "关键里程碑",
                "可以概括",
                "具体来说",
                "总结",
                "核心",
                "如下",
                "首先",
                "其次",
                "因此",
            )
        ):
            signals += 1

        hint = cls._normalize_display_text(answer_hint)
        if hint and len(hint) >= 18:
            prefix = hint[:18]
            if prefix and prefix in clean:
                signals += 2
        return signals >= 2

    @staticmethod
    def _format_tool_names_for_display(tool_names: Sequence[str]) -> str:
        unique: List[str] = []
        for item in tool_names:
            name = str(item or "").strip()
            if not name or name in unique:
                continue
            unique.append(name)
        if not unique:
            return "相关工具"
        if len(unique) == 1:
            return f"`{unique[0]}`"
        if len(unique) == 2:
            return f"`{unique[0]}` 和 `{unique[1]}`"
        return "、".join(f"`{name}`" for name in unique[:-1]) + f" 和 `{unique[-1]}`"

    @classmethod
    def _coerce_thought_for_display(
        cls,
        raw_text: str,
        *,
        tool_names: Optional[Sequence[str]] = None,
        answer_hint: str = "",
    ) -> str:
        clean = cls._normalize_display_text(raw_text)
        if not clean:
            return ""

        has_tool_plan = bool([name for name in (tool_names or []) if str(name or "").strip()])
        if has_tool_plan and (cls._looks_like_answer_draft(raw_text, answer_hint=answer_hint) or len(clean) > 160):
            tools_desc = cls._format_tool_names_for_display(tool_names or [])
            return f"正在整理已有分析，并准备调用 {tools_desc} 补齐关键信息。"

        if cls._looks_like_answer_draft(raw_text, answer_hint=answer_hint) or len(clean) > 240:
            return "正在收束已有分析并组织最终回答。"

        if len(clean) > 180:
            return clean[:180].rstrip("，、；：,. ") + "…"
        return clean

    @staticmethod
    def _sanitize_message_for_context(message: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(message)
        if str(out.get("role", "")).lower() == "assistant":
            out["content"] = AgentCore._strip_think_content(out.get("content", ""))
        return out

    @classmethod
    def _messages_equivalent_for_context(cls, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        left_msg = cls._sanitize_message_for_context(left)
        right_msg = cls._sanitize_message_for_context(right)
        return (
            str(left_msg.get("role") or "").strip().lower() == str(right_msg.get("role") or "").strip().lower()
            and str(left_msg.get("content") or "") == str(right_msg.get("content") or "")
            and str(left_msg.get("thought") or "").strip() == str(right_msg.get("thought") or "").strip()
        )

    @classmethod
    def _merge_history_messages(
        cls,
        history_messages: Sequence[Dict[str, Any]],
        current_messages: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        history_rows = [
            cls._sanitize_message_for_context(item)
            for item in list(history_messages or [])
            if isinstance(item, dict)
        ]
        current_rows = [
            cls._sanitize_message_for_context(item)
            for item in list(current_messages or [])
            if isinstance(item, dict)
        ]
        if not history_rows:
            return current_rows
        if not current_rows:
            return list(history_rows)
        if len(current_rows) >= len(history_rows):
            prefix = current_rows[: len(history_rows)]
            if all(cls._messages_equivalent_for_context(a, b) for a, b in zip(history_rows, prefix)):
                return current_rows
        return history_rows + current_rows

    @staticmethod
    def _estimate_messages_tokens(messages: Sequence[Dict[str, Any]]) -> int:
        return sum(4 + estimate_tokens(str(m.get("content", "") or "")) for m in messages)

    @staticmethod
    def _extract_reasoning_summary_from_message(message: Dict[str, Any]) -> str:
        thought = str(message.get("thought") or "").strip()
        if thought:
            return thought
        return ""

    @classmethod
    def _render_conversation_context_state(cls, state: Dict[str, Any]) -> str:
        if not isinstance(state, dict):
            return ""

        lines: List[str] = []
        active_topic = cls._compact_debug_text(state.get("active_topic") or "", 220)
        user_goal = cls._compact_debug_text(state.get("user_goal") or "", 220)
        constraints = [
            cls._compact_debug_text(item, 160)
            for item in list(state.get("constraints") or [])
            if cls._compact_debug_text(item, 160)
        ]
        open_questions = [
            cls._compact_debug_text(item, 160)
            for item in list(state.get("open_questions") or [])
            if cls._compact_debug_text(item, 160)
        ]
        resolved_facts = [
            cls._compact_debug_text(item, 160)
            for item in list(state.get("resolved_facts") or [])
            if cls._compact_debug_text(item, 160)
        ]
        evidence_ledger: List[str] = []
        for item in list(state.get("evidence_ledger") or []):
            if isinstance(item, str):
                compacted = cls._compact_debug_text(item, 160)
                if compacted:
                    evidence_ledger.append(compacted)
                continue
            if not isinstance(item, dict):
                continue
            summary = cls._compact_debug_text(item.get("summary") or "", 160)
            if not summary:
                continue
            source_labels = [
                cls._compact_debug_text(label, 32)
                for label in list(item.get("source_labels") or [])
                if cls._compact_debug_text(label, 32)
            ]
            tool_names = [
                cls._compact_debug_text(name, 48)
                for name in list(item.get("tool_names") or [])
                if cls._compact_debug_text(name, 48)
            ]
            suffix_parts: List[str] = []
            if source_labels:
                suffix_parts.append("来源: " + "、".join(source_labels[:3]))
            if tool_names:
                suffix_parts.append("工具: " + "、".join(tool_names[:2]))
            suffix = f"（{'；'.join(suffix_parts)}）" if suffix_parts else ""
            evidence_ledger.append(f"{summary}{suffix}")
        last_reasoning_summary = cls._compact_debug_text(state.get("last_reasoning_summary") or "", 180)

        if active_topic:
            lines.append(f"- 当前主题: {active_topic}")
        if user_goal and user_goal != active_topic:
            lines.append(f"- 当前目标: {user_goal}")
        if constraints:
            lines.append("- 仍然有效的约束:")
            lines.extend(f"  - {item}" for item in constraints[:4])
        if open_questions:
            lines.append("- 未解决问题:")
            lines.extend(f"  - {item}" for item in open_questions[:4])
        if resolved_facts:
            lines.append("- 已确认事实:")
            lines.extend(f"  - {item}" for item in resolved_facts[:4])
        if evidence_ledger:
            lines.append("- 证据账本:")
            lines.extend(f"  - {item}" for item in evidence_ledger[:4])
        if last_reasoning_summary:
            lines.append(f"- 最近推理摘要: {last_reasoning_summary}")
        return "\n".join(lines).strip()

    @classmethod
    def _render_user_chat_preferences(cls, preferences: Dict[str, Any]) -> str:
        if not isinstance(preferences, dict):
            return ""
        language = str(preferences.get("response_language") or "auto").strip()
        verbosity = str(preferences.get("response_verbosity") or "balanced").strip()
        web_search = str(preferences.get("web_search") or "ask").strip()
        lines: List[str] = []
        if language == "zh-CN":
            lines.append("- 默认输出语言: 中文")
        elif language == "en-US":
            lines.append("- 默认输出语言: English")
        if verbosity == "concise":
            lines.append("- 默认回答风格: 简洁、快速")
        elif verbosity == "detailed":
            lines.append("- 默认回答风格: 详细、展开")
        if web_search == "avoid":
            lines.append("- 默认联网策略: 除非用户明确要求，否则避免联网/网页工具")
        elif web_search == "allow_when_needed":
            lines.append("- 默认联网策略: 当问题明显需要外部信息时，可主动考虑网页工具")
        return "\n".join(lines).strip()

    @staticmethod
    def _render_rag_overrides_prompt(overrides: Dict[str, Any]) -> str:
        if not isinstance(overrides, dict) or not bool(overrides.get("enabled", False)):
            return ""

        scope_mode = str(overrides.get("scope_mode") or "all").strip()
        knowledge_base_ids = [
            int(item)
            for item in list(overrides.get("knowledge_base_ids") or [])
            if str(item).isdigit()
        ]
        document_ids = [
            int(item)
            for item in list(overrides.get("document_ids") or [])
            if str(item).isdigit()
        ]

        lines: List[str] = []
        if scope_mode == "knowledge_base" and knowledge_base_ids:
            lines.append(f"- 检索范围: 仅限指定知识库 {knowledge_base_ids}")
        elif scope_mode == "document" and document_ids:
            lines.append(f"- 检索范围: 仅限指定文档 {document_ids}")
        else:
            lines.append("- 检索范围: 全部可访问知识库")

        feature_labels = {
            "use_reranker": "reranker",
            "use_hybrid": "hybrid retrieval",
            "use_query_rewrite": "query rewrite",
            "use_contextual_compression": "contextual compression",
        }
        feature_lines = [
            f"- {label}: {'开启' if bool(overrides.get(key)) else '关闭'}"
            for key, label in feature_labels.items()
            if key in overrides and overrides.get(key) is not None
        ]
        lines.extend(feature_lines)
        lines.append("- 如果需要调用 `knowledge_search`，系统会自动把以上临时约束注入到本轮检索执行。")
        return "\n".join(lines).strip()

    def _apply_tool_call_overrides(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        effective_arguments = dict(arguments or {})
        if tool_name != "knowledge_search":
            return effective_arguments

        overrides = dict(self._active_rag_overrides or {})
        if not overrides or not bool(overrides.get("enabled", False)):
            return effective_arguments

        scope_mode = str(overrides.get("scope_mode") or "all").strip()
        if scope_mode == "knowledge_base":
            knowledge_base_ids = [
                int(item)
                for item in list(overrides.get("knowledge_base_ids") or [])
                if str(item).isdigit()
            ]
            if knowledge_base_ids:
                effective_arguments["knowledge_base_ids"] = knowledge_base_ids
                effective_arguments.pop("document_ids", None)
        elif scope_mode == "document":
            document_ids = [
                int(item)
                for item in list(overrides.get("document_ids") or [])
                if str(item).isdigit()
            ]
            knowledge_base_ids = [
                int(item)
                for item in list(overrides.get("knowledge_base_ids") or [])
                if str(item).isdigit()
            ]
            if knowledge_base_ids:
                effective_arguments["knowledge_base_ids"] = knowledge_base_ids
            if document_ids:
                effective_arguments["document_ids"] = document_ids

        for key in (
            "use_reranker",
            "use_hybrid",
            "use_query_rewrite",
        ):
            if key in overrides and overrides.get(key) is not None:
                effective_arguments[key] = bool(overrides.get(key))

        return effective_arguments

    @staticmethod
    def _summarize_messages(messages: Sequence[Dict[str, Any]], max_lines: int = 8) -> str:
        lines: List[str] = []
        for msg in messages:
            role = str(msg.get("role", "unknown") or "unknown")
            content = str(msg.get("content", "") or "").replace("\n", " ").strip()
            reasoning_summary = AgentCore._extract_reasoning_summary_from_message(msg).replace("\n", " ").strip()
            if role.lower() == "assistant" and reasoning_summary:
                if content:
                    content = f"回答要点: {content[:90]} | 推理摘要: {reasoning_summary[:80]}"
                else:
                    content = f"推理摘要: {reasoning_summary[:120]}"
            if not content:
                continue
            lines.append(f"- {role}: {content[:160]}")
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)

    def _build_system_prompt(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        *,
        function_calling: bool = False,
    ) -> str:
        routing_decision = self._routing_decision_for_messages(messages)
        latest_user_text = self._latest_user_text(messages)
        tool_choice = "auto"
        tools_desc = ""
        if not function_calling:
            get_tools_description = getattr(self.tools, "get_tools_description", None)
            tools_desc = get_tools_description() if callable(get_tools_description) else ""
        available_tools: List[str] = []
        list_tools = getattr(self.tools, "list_tools", None)
        if callable(list_tools):
            try:
                raw_tools = list_tools()
            except TypeError:
                raw_tools = []
            else:
                for tool in list(raw_tools or []):
                    if not isinstance(tool, dict):
                        continue
                    function_payload = tool.get("function")
                    if isinstance(function_payload, dict):
                        name = str(function_payload.get("name") or "").strip()
                    else:
                        name = str(tool.get("name") or "").strip()
                    if name:
                        available_tools.append(name)
        channel_policy_prompt = self._channel_tool_policy_prompt(available_tools)
        desc_tokens = estimate_tokens(tools_desc)
        self._last_tool_selection = {
            "intent": "general_chat",
            "intent_user_text": latest_user_text,
            "selected_tools": available_tools,
            "prompt_desc_tokens": desc_tokens,
            "schema_scope": "available",
            "tool_selection_enabled": False,
            "tool_choice": tool_choice,
            "routing_source": routing_decision.source if routing_decision else "default_agent",
            "routing_reason": routing_decision.reason if routing_decision else "",
            "routing_confidence": routing_decision.confidence if routing_decision else 0.0,
            "carry_over_previous_goal": routing_decision.carry_over_previous_goal if routing_decision else False,
            "router_needs_tools": routing_decision.needs_tools if routing_decision else None,
        }
        logger.info(
            f"[AgentCore] selected_tools={available_tools or 'ALL'} "
            f"schema_scope=available tool_choice={tool_choice} prompt_desc_tokens={desc_tokens} "
            f"routing_source={self._last_tool_selection.get('routing_source')}"
        )
        if function_calling:
            prompt = f"{self.FUNCTION_CALLING_SYSTEM_PROMPT.strip()}\n\n{self.CITATION_POLICY_PROMPT}"
        else:
            prompt = f"{self.SYSTEM_PROMPT.format(tools_description=tools_desc)}\n\n{self.CITATION_POLICY_PROMPT}"
        user_pref_prompt = self._render_user_chat_preferences(self._active_chat_preferences)
        if user_pref_prompt:
            prompt = f"{prompt}\n\n## 用户已确认的聊天偏好\n{user_pref_prompt}"
        rag_prompt = self._render_rag_overrides_prompt(self._active_rag_overrides)
        if rag_prompt:
            prompt = f"{prompt}\n\n## 本轮临时 RAG 注入\n{rag_prompt}"
        if channel_policy_prompt:
            prompt = f"{prompt}\n\n{channel_policy_prompt}"
        return prompt

    @staticmethod
    def _build_observation_message(tool_name: str, observation_output: str) -> str:
        if tool_name == "knowledge_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "必须在关键结论后保留对应的 [来源X] 标注，且只能使用 observation 中出现过的来源编号。"
                "如证据不足，请明确说明。请用<answer>标签给出最终回答。"
            )
        elif tool_name == "web_search":
            followup = (
                "请根据工具返回的信息继续。若要给出最终回答，"
                "基于公网搜索结果的关键结论必须保留对应的 [网页X] 标注，且只能使用 observation 中出现过的网页编号。"
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
        has_web = any(item.tool_name == "web_search" for item in observations)
        output = "\n\n".join(f"[{item.tool_name}]\n{item.observation_output}" for item in observations)
        if has_knowledge and has_web:
            followup = (
                "请综合所有 observation。知识库结论只能使用已出现过的 [来源X]，"
                "公网检索结论只能使用已出现过的 [网页X]。"
            )
        elif has_knowledge:
            followup = "请综合所有 observation，答案中的知识库引用必须只使用 observation 已出现过的 [来源X]。"
        elif has_web:
            followup = "请综合所有 observation，答案中的公网引用必须只使用 observation 已出现过的 [网页X]。"
        else:
            followup = "请综合所有 observation 后继续。"
        return f"<observation>\n{output}\n</observation>\n\n{followup}"

    @staticmethod
    def _extract_source_labels(text: str) -> set[str]:
        return set(re.findall(r"\[来源(\d+)\]", text or ""))

    @staticmethod
    def _extract_web_source_labels(text: str) -> set[str]:
        return set(re.findall(r"\[网页(\d+)\]", text or ""))

    @classmethod
    def _allowed_citation_tokens(
        cls,
        allowed_source_labels: set[str],
        allowed_web_source_labels: Optional[set[str]] = None,
    ) -> set[str]:
        tokens = {f"来源{idx}" for idx in allowed_source_labels}
        tokens.update(f"网页{idx}" for idx in (allowed_web_source_labels or set()))
        return tokens

    @classmethod
    def _extract_answer_citation_tokens(cls, answer: str) -> set[str]:
        cited = {f"来源{idx}" for idx in cls._extract_source_labels(answer)}
        cited.update(f"网页{idx}" for idx in cls._extract_web_source_labels(answer))
        return cited

    @staticmethod
    def _extract_answer_citations(answer: str) -> set[str]:
        return set(re.findall(r"\[来源(\d+)\]", answer or ""))

    @classmethod
    def _citations_are_valid(
        cls,
        answer: str,
        allowed_source_labels: set[str],
        allowed_web_source_labels: Optional[set[str]] = None,
    ) -> bool:
        allowed = cls._allowed_citation_tokens(allowed_source_labels, allowed_web_source_labels)
        if not allowed:
            return True
        cited = cls._extract_answer_citation_tokens(answer)
        return bool(cited) and cited.issubset(allowed)

    @classmethod
    def _build_rag_metrics(cls, context: AgentContext) -> Dict[str, Any]:
        cited = cls._extract_answer_citation_tokens(context.final_answer or "")
        allowed = cls._allowed_citation_tokens(
            context.allowed_source_labels,
            context.allowed_web_source_labels,
        )
        citation_required = bool(allowed)
        citation_valid = (
            cls._citations_are_valid(
                context.final_answer or "",
                context.allowed_source_labels,
                context.allowed_web_source_labels,
            )
            if citation_required else True
        )
        return {
            "knowledge_search_calls": context.knowledge_search_calls,
            "web_search_calls": context.web_search_calls,
            "source_labels_count": len(allowed),
            "source_labels": (
                [f"来源{idx}" for idx in sorted(context.allowed_source_labels, key=int)]
                + [f"网页{idx}" for idx in sorted(context.allowed_web_source_labels, key=int)]
            ),
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
        allowed = self._allowed_citation_tokens(
            context.allowed_source_labels,
            context.allowed_web_source_labels,
        )
        if not clean or not allowed or self._citations_are_valid(
            clean,
            context.allowed_source_labels,
            context.allowed_web_source_labels,
        ):
            return clean
        context.citation_repair_attempts += 1
        allowed_tokens = ", ".join(f"[{token}]" for token in sorted(allowed))
        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": f"只修正来源标注，只能使用：{allowed_tokens}\n\n{clean}"}],
                system_prompt="你是引用修正助手。",
                temperature=0.0,
                max_tokens=min(settings.llm_max_tokens, 1000),
            )
            fixed = re.sub(r"</?answer>", "", str(resp.get("content") or "")).strip()
            if fixed and self._citations_are_valid(
                fixed,
                context.allowed_source_labels,
                context.allowed_web_source_labels,
            ):
                context.citation_repair_successes += 1
                return fixed
        except Exception as exc:
            logger.warning(f"[AgentCore] citation repair failed: {exc}")
        return f"{clean}\n\n注：当前可用来源仅为 {allowed_tokens}。"

    def _parse_response(self, response: str) -> Dict[str, Any]:
        normalized = self._normalize_think_tag_aliases(response)
        result = {"thought": None, "action": None, "answer": None, "raw": response}
        think_match = re.search(r"<think>(.*?)</think>", normalized, re.DOTALL)
        if think_match:
            result["thought"] = think_match.group(1).strip()
        action_match = re.search(r"<action>(.*?)</action>", normalized, re.DOTALL)
        if action_match:
            payload = action_match.group(1).strip()
            for candidate in (payload, payload.replace("'", '"')):
                try:
                    result["action"] = json.loads(candidate)
                    break
                except Exception:
                    pass
        answer_match = re.search(r"<answer>(.*?)</answer>", normalized, re.DOTALL)
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
        normalized = AgentCore._normalize_think_tag_aliases(content)
        m = re.search(r"<answer>(.*?)</answer>", normalized or "", re.DOTALL)
        if m:
            return m.group(1).strip()
        stripped = AgentCore._strip_think_content(normalized or "")
        return re.sub(r"</?(?:action|observation|answer)>", "", stripped).strip()

    @classmethod
    def _normalize_messages_for_plain_chat(cls, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for raw in list(messages or []):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role", "") or "").strip().lower() or "user"
            content = str(raw.get("content", "") or "")
            if role == "tool":
                text = content.strip()
                if text:
                    normalized.append({"role": "user", "content": f"<observation>\n{text}\n</observation>"})
                continue
            if role == "assistant":
                clean = cls._strip_think_content(content)
                if raw.get("tool_calls"):
                    if clean:
                        normalized.append({"role": "assistant", "content": clean})
                    continue
                normalized.append({"role": "assistant", "content": clean})
                continue
            normalized.append({"role": role, "content": content})
        return normalized

    @classmethod
    def _normalize_messages_for_function_calling(cls, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        rows = [item for item in list(messages or []) if isinstance(item, dict)]
        idx = 0
        total = len(rows)

        def _tool_as_observation(msg: Dict[str, Any]) -> None:
            text = str(msg.get("content", "") or "").strip()
            if text:
                normalized.append({"role": "user", "content": f"<observation>\n{text}\n</observation>"})

        while idx < total:
            raw = rows[idx]
            role = str(raw.get("role", "") or "").strip().lower() or "user"
            content = str(raw.get("content", "") or "")

            if role == "tool":
                _tool_as_observation(raw)
                idx += 1
                continue

            if role == "assistant" and raw.get("tool_calls"):
                clean = cls._strip_think_content(content)
                tool_calls = [call for call in list(raw.get("tool_calls") or []) if isinstance(call, dict)]
                expected_ids = [str(call.get("id") or "").strip() for call in tool_calls if str(call.get("id") or "").strip()]

                following_tools: List[Dict[str, Any]] = []
                cursor = idx + 1
                while cursor < total and str(rows[cursor].get("role", "") or "").strip().lower() == "tool":
                    following_tools.append(rows[cursor])
                    cursor += 1

                found_ids = [str(item.get("tool_call_id") or "").strip() for item in following_tools if str(item.get("tool_call_id") or "").strip()]
                if expected_ids and all(call_id in found_ids for call_id in expected_ids):
                    normalized.append(
                        {
                            "role": "assistant",
                            "content": clean,
                            "tool_calls": tool_calls,
                        }
                    )
                    for tool_msg in following_tools:
                        entry = {
                            "role": "tool",
                            "tool_call_id": str(tool_msg.get("tool_call_id") or ""),
                            "content": str(tool_msg.get("content", "") or ""),
                        }
                        tool_name = str(tool_msg.get("name") or "").strip()
                        if tool_name:
                            entry["name"] = tool_name
                        normalized.append(entry)
                else:
                    if clean:
                        normalized.append({"role": "assistant", "content": clean})
                    for tool_msg in following_tools:
                        _tool_as_observation(tool_msg)
                idx = cursor
                continue

            if role == "assistant":
                normalized.append({"role": "assistant", "content": cls._strip_think_content(content)})
                idx += 1
                continue

            normalized.append({"role": role, "content": content})
            idx += 1

        return normalized

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
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return result.output

        use_contextual_compression = True
        if context is not None and isinstance(context.active_rag_overrides, dict):
            override = context.active_rag_overrides.get("use_contextual_compression")
            if override is not None:
                use_contextual_compression = bool(override)

        if not use_contextual_compression:
            fallback_output = self._format_knowledge_observation_without_compression(valid_rows, context=context)
            return fallback_output or result.output

        compression_inputs: list[CompressionInput] = []
        base_source_id = max(int(getattr(context, "next_knowledge_source_label", 1) or 1), 1) if context is not None else 1
        for offset, row in enumerate(valid_rows):
            source_id = base_source_id + offset
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

        for offset, row in enumerate(valid_rows):
            source_id = base_source_id + offset
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
        if context is not None:
            context.next_knowledge_source_label = base_source_id + len(valid_rows)
        return f"Compressed contexts: {len(parts)}\n" + "".join(parts)

    def _format_knowledge_observation_without_compression(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        context: Optional[AgentContext] = None,
    ) -> str:
        base_source_id = max(int(getattr(context, "next_knowledge_source_label", 1) or 1), 1) if context is not None else 1
        parts: List[str] = []
        for offset, row in enumerate(list(rows or [])):
            if not isinstance(row, dict):
                continue
            source_id = base_source_id + offset
            source_label = f"来源{source_id}"
            raw = str(row.get("content") or "").strip()
            if not raw:
                continue
            retrieval_score = self._safe_float(row.get("score"), 0.0) * 100
            kb_name = row.get("knowledge_base") or row.get("knowledge_base_name") or "unknown_kb"
            doc_name = row.get("document") or row.get("document_name") or "unknown_doc"
            chunk_idx = int(self._safe_float(row.get("chunk_index"), 0))
            content = f"[{source_label}] {raw[:320]}" + ("..." if len(raw) > 320 else "")
            parts.append(
                f"\n[{source_label}] (retrieval score {retrieval_score:.1f}%)\n"
                f"Source: {kb_name} / {doc_name} / chunk {chunk_idx}\n"
                f"Content: {content}"
            )
        if not parts:
            return ""
        if context is not None:
            context.next_knowledge_source_label = base_source_id + len(parts)
        return f"Knowledge contexts: {len(parts)}\n" + "".join(parts)

    async def _compress_web_search_observation(
        self,
        query: str,
        result: ToolResult,
        context: Optional[AgentContext] = None,
    ) -> str:
        data = result.data if isinstance(result.data, dict) else {}
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            return result.output
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return result.output

        base_source_id = max(int(getattr(context, "next_web_source_label", 1) or 1), 1) if context is not None else 1
        parts: List[str] = []
        for offset, row in enumerate(valid_rows):
            source_id = base_source_id + offset
            source_label = f"网页{source_id}"
            title = str(row.get("title") or row.get("answer") or "Public web result").strip()
            url = str(
                row.get("url")
                or row.get("link")
                or row.get("href")
                or row.get("source")
                or ""
            ).strip()
            domain = str(row.get("domain") or row.get("display_url") or "").strip()
            snippet = str(
                row.get("snippet")
                or row.get("summary")
                or row.get("description")
                or row.get("reader_excerpt")
                or ""
            ).strip()

            parts.append(
                f"\n[{source_label}] {title or 'Public web result'}\n"
                f"Domain: {domain or 'unknown'}\n"
                f"URL: {url or 'N/A'}\n"
                f"Summary: {snippet or 'No summary available.'}"
            )
        if context is not None:
            context.next_web_source_label = base_source_id + len(valid_rows)
        return f"Public web contexts: {len(parts)}\n" + "".join(parts)

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

        try:
            memory_control = await self.runtime_service.get_user_memory_control(
                user_id=self.runtime_context.user_id,
                channel=self.runtime_context.channel,
            )
            context.memory_enabled = bool(memory_control.get("effective_enabled", False))
        except Exception as exc:
            context.memory_enabled = False
            logger.warning(f"[AgentCore] load memory control failed: {exc}")

        if self.runtime_context.conversation_id is not None:
            try:
                latest_state = await self.runtime_service.get_conversation_context_state(
                    int(self.runtime_context.conversation_id)
                )
                if isinstance(latest_state, dict):
                    context.conversation_state = latest_state
                latest_compacted_history = await self.runtime_service.get_conversation_compacted_history(
                    int(self.runtime_context.conversation_id)
                )
                if isinstance(latest_compacted_history, dict):
                    context.compacted_history = latest_compacted_history
                    persisted_summary = str(latest_compacted_history.get("history_summary") or "").strip()
                    if persisted_summary:
                        context.context_summary = persisted_summary
                item_stream = await self.runtime_service.get_conversation_item_stream(
                    int(self.runtime_context.conversation_id)
                )
                if isinstance(item_stream, dict):
                    context.item_stream = item_stream
                    boundary_message_id = (
                        latest_compacted_history.get("compact_boundary_message_id")
                        if isinstance(latest_compacted_history, dict)
                        else None
                    )
                    try:
                        normalized_boundary_message_id = int(boundary_message_id) if boundary_message_id is not None else None
                    except Exception:
                        normalized_boundary_message_id = None
                    context.history_messages = self._history_messages_from_item_stream(
                        list(item_stream.get("entries") or []),
                        fallback_boundary_message_id=normalized_boundary_message_id,
                    )
            except Exception as exc:
                logger.warning(f"[AgentCore] load conversation context artifacts failed: {exc}")

        context.messages = self._merge_history_messages(context.history_messages, context.messages)

        if self.runtime_context.user_id:
            try:
                context.user_chat_preferences = await self.runtime_service.get_user_chat_preferences(
                    user_id=self.runtime_context.user_id
                )
            except Exception as exc:
                logger.warning(f"[AgentCore] load user chat preferences failed: {exc}")
                context.user_chat_preferences = {}
        overrides = dict(self.runtime_context.chat_preferences_override or {})
        if overrides:
            context.user_chat_preferences = self.runtime_service.merge_chat_preferences(
                context.user_chat_preferences,
                overrides,
            )
        self._active_chat_preferences = dict(context.user_chat_preferences or {})
        context.active_rag_overrides = self.runtime_service.normalize_chat_rag_overrides(
            self.runtime_context.rag_overrides
        )
        self._active_rag_overrides = dict(context.active_rag_overrides or {})

        if context.memory_enabled:
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
                    top_k=max(int(getattr(settings, "agent_memory_top_k", 3)), 1),
                )
            except Exception as exc:
                logger.warning(f"[AgentCore] memory recall failed: {exc}")

    async def _ensure_run_created(self, context: AgentContext) -> None:
        if context.run_id or not bool(getattr(settings, "agent_persist_steps_enabled", True)):
            return
        if not self.runtime_context.user_id:
            return

        selection = dict(self._last_tool_selection or {})
        try:
            context.run_id = await self.runtime_service.create_run(
                user_id=self.runtime_context.user_id,
                channel=self.runtime_context.channel,
                conversation_id=self.runtime_context.conversation_id,
                notebook_id=self.runtime_context.notebook_id,
                intent=str(selection.get("intent") or "general_chat"),
                selected_tools=[
                    str(item)
                    for item in list(selection.get("selected_tools") or [])
                    if str(item or "").strip()
                ],
                model_provider=getattr(self.llm, "provider", None),
                model_name=(getattr(self.llm, "config", {}) or {}).get("model"),
                metadata={
                    "path": "agent_core",
                    "routing_source": str(selection.get("routing_source") or "default"),
                    "turn_id": context.turn_id,
                },
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] create_run failed: {exc}")

    def _memory_prompt(self, memories: Sequence[MemoryContext]) -> str:
        lines = ["以下是可参考的跨会话记忆（仅在相关时使用）："]
        for i, item in enumerate(memories, start=1):
            lines.append(f"- 记忆{i} score={item.score}: {item.content[:180]}")
        return "\n".join(lines)

    def _should_generate_reasoning_summary(self, context: AgentContext) -> bool:
        if not bool(getattr(settings, "agent_reasoning_summary_enabled", True)):
            return False
        if not str(context.final_answer or "").strip():
            return False
        if str(context.reasoning_summary or "").strip():
            return False
        has_tool_activity = any(step.step_type in {"action", "observation"} for step in context.steps)
        min_iterations = max(int(getattr(settings, "agent_reasoning_summary_min_iterations", 2) or 2), 1)
        return has_tool_activity or int(context.iteration or 0) >= min_iterations

    def _build_reasoning_trace_for_summary(self, context: AgentContext) -> str:
        lines: List[str] = []
        latest_user = self._latest_user_text(context.messages)
        if latest_user:
            lines.append(f"用户问题: {self._compact_debug_text(latest_user, 260)}")
        if context.final_answer:
            lines.append(f"最终回答: {self._compact_debug_text(context.final_answer, 360)}")
        lines.append(f"迭代轮数: {int(max(0, context.iteration))}")

        selected_tools = [
            str(item)
            for item in (self._last_tool_selection.get("selected_tools") or [])
            if str(item or "").strip()
        ]
        if selected_tools:
            lines.append(f"调度工具: {', '.join(selected_tools[:6])}")

        step_lines: List[str] = []
        for step in list(context.steps)[-8:]:
            if step.step_type == "thought":
                compact = self._compact_debug_text(step.content, 180)
                if compact:
                    step_lines.append(f"- 思考: {compact}")
            elif step.step_type == "action":
                tool_name = str(step.tool_name or "").strip() or "unknown_tool"
                tool_input = self._compact_debug_text(json.dumps(step.tool_input or {}, ensure_ascii=False), 160)
                step_lines.append(f"- 工具: {tool_name} {tool_input}")
            elif step.step_type == "observation":
                tool_name = str(step.tool_name or "").strip() or "unknown_tool"
                output = self._compact_debug_text(step.tool_output or step.content or "", 200)
                suffix = "成功" if step.success else "失败" if step.success is False else "返回"
                if output:
                    step_lines.append(f"- 观察({tool_name},{suffix}): {output}")
        if step_lines:
            lines.append("推理轨迹:\n" + "\n".join(step_lines))
        return "\n".join(lines).strip()

    async def _generate_reasoning_summary(self, context: AgentContext) -> str:
        if not self._should_generate_reasoning_summary(context):
            return ""

        trace = self._build_reasoning_trace_for_summary(context)
        if not trace:
            return ""

        provider = str(getattr(settings, "agent_reasoning_summary_provider", "aliyun") or "aliyun").strip()
        model_name = str(getattr(settings, "agent_reasoning_summary_model", "qwen3.5-flash") or "qwen3.5-flash").strip()
        max_tokens = max(int(getattr(settings, "agent_reasoning_summary_max_tokens", 220) or 220), 64)

        try:
            summary_llm = LLMService(provider)
            summary_llm.config = dict(summary_llm.config)
            summary_llm.config["model"] = model_name
            response = await summary_llm.chat(
                messages=[{"role": "user", "content": trace}],
                system_prompt=(
                    "你是一个推理过程压缩器。请把给定的多轮推理、工具使用与最终回答压缩成 1 到 3 句中文总结。"
                    "只保留回答策略、关键证据或工具、是否仍有未解问题。"
                    "不要复述整段最终答案，不要输出项目符号，不要解释任务本身，不要超过 120 个汉字。"
                    "直接输出摘要正文。"
                ),
                temperature=0.2,
                max_tokens=max_tokens,
            )
            summary_text = self._compact_debug_text(response.get("content", ""), 160).strip()
            if not summary_text:
                return ""
            context.context_debug["reasoning_summary"] = summary_text
            context.context_debug["reasoning_summary_model"] = model_name
            context.context_debug["reasoning_summary_provider"] = provider
            context.reasoning_summary = summary_text
            return summary_text
        except Exception as exc:
            logger.warning(f"[AgentCore] reasoning summary failed: {exc}")
            return ""

    @staticmethod
    def _compact_debug_text(value: Any, limit: int = 180) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "…"

    @classmethod
    def _debug_message_preview(cls, item: Dict[str, Any]) -> str:
        content = cls._compact_debug_text(item.get("content", ""), 320)
        if content:
            return content

        role = str(item.get("role", "")).strip().lower()
        if role == "assistant":
            tool_calls = [call for call in list(item.get("tool_calls") or []) if isinstance(call, dict)]
            tool_names: List[str] = []
            for call in tool_calls:
                function_payload = call.get("function")
                if isinstance(function_payload, dict):
                    name = str(function_payload.get("name") or "").strip()
                else:
                    name = str(call.get("name") or "").strip()
                if name:
                    tool_names.append(name)
            if tool_names:
                return f"调用工具: {', '.join(tool_names[:3])}"
        return "（空内容）"

    def _build_context_debug_payload(
        self,
        *,
        context: AgentContext,
        anchor_summary: str,
        persisted_anchor_summary: str,
        persisted_summary: str,
        older_summary: str,
        llm_messages: Sequence[Dict[str, Any]],
        recently_slid_messages: Sequence[Dict[str, Any]],
        estimated_tokens: int,
        budget: int,
        window_turns: int,
        total_messages: int,
        older_messages_count: int,
        recent_messages_count: int,
    ) -> Dict[str, Any]:
        def _preview_messages(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
            preview_rows: List[Dict[str, str]] = []
            for item in list(rows):
                role = str(item.get("role", "")).strip().lower()
                content = self._debug_message_preview(item)
                if role:
                    preview_rows.append({"role": role, "content": content})
            return preview_rows

        recent_messages: List[Dict[str, str]] = []
        for item in list(llm_messages):
            role = str(item.get("role", "")).strip().lower()
            content = self._debug_message_preview(item)
            if role:
                recent_messages.append({"role": role, "content": content})
        recently_slid_previews = _preview_messages(recently_slid_messages)

        selection = dict(self._last_tool_selection or {})
        successful_queries = self._successful_knowledge_search_queries(context)
        source_labels = [
            f"来源{idx}"
            for idx in sorted(
                context.allowed_source_labels,
                key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value)),
            )
        ]
        memory_lines = [
            self._compact_debug_text(item.content, 180)
            for item in list(context.memory_contexts or [])[:3]
            if self._compact_debug_text(item.content, 180)
        ]
        conversation_state = dict(context.conversation_state or {}) if isinstance(context.conversation_state, dict) else {}
        conversation_state_summary = self._render_conversation_context_state(conversation_state)
        compacted_history = dict(context.compacted_history or {}) if isinstance(context.compacted_history, dict) else {}
        replacement_history = [
            dict(item)
            for item in list(compacted_history.get("replacement_history") or [])
            if isinstance(item, dict)
        ]
        user_chat_preferences = dict(context.user_chat_preferences or {}) if isinstance(context.user_chat_preferences, dict) else {}
        active_rag_overrides = dict(context.active_rag_overrides or {}) if isinstance(context.active_rag_overrides, dict) else {}

        payload = {
            "version": "chat_context_debug.v1",
            "iteration": int(max(0, context.iteration)),
            "context_truncated": bool(context.context_truncated),
            "estimated_tokens": int(max(0, estimated_tokens)),
            "budget": int(max(0, budget)),
            "window_turns": int(max(1, window_turns)),
            "message_count_before_trim": int(max(0, total_messages)),
            "message_count_sent": int(max(0, len(llm_messages))),
            "older_messages_count": int(max(0, older_messages_count)),
            "recently_slid_messages_count": int(max(0, len(recently_slid_messages))),
            "recent_messages_count": int(max(0, recent_messages_count)),
            "intent": str(selection.get("intent") or "general_chat"),
            "intent_user_text": self._compact_debug_text(selection.get("intent_user_text") or "", 220),
            "routing_source": str(selection.get("routing_source") or "rule"),
            "routing_reason": self._compact_debug_text(selection.get("routing_reason") or "", 220),
            "routing_confidence": float(selection.get("routing_confidence") or 0.0),
            "carry_over_previous_goal": bool(selection.get("carry_over_previous_goal")),
            "selected_tools": [str(item) for item in (selection.get("selected_tools") or []) if str(item or "").strip()],
            "tool_choice": str(selection.get("tool_choice") or "auto"),
            "conversation_state": conversation_state,
            "conversation_state_summary": self._compact_debug_text(conversation_state_summary, 600),
            "anchor_summary": self._compact_debug_text(anchor_summary, 600),
            "persisted_anchor_summary": self._compact_debug_text(persisted_anchor_summary, 600),
            "persisted_summary": self._compact_debug_text(persisted_summary, 600),
            "older_history_summary": self._compact_debug_text(older_summary, 600),
            "compact_boundary_message_id": compacted_history.get("compact_boundary_message_id"),
            "replacement_history_count": int(len(replacement_history)),
            "mid_run_compactions": int(max(0, context.mid_run_compactions)),
            "memory_enabled": bool(context.memory_enabled),
            "memory_count": int(len(context.memory_contexts or [])),
            "memory_lines": memory_lines,
            "user_chat_preferences": user_chat_preferences,
            "rag_overrides": active_rag_overrides,
            "recently_slid_messages": recently_slid_previews,
            "recent_messages": recent_messages,
            "successful_knowledge_queries": successful_queries[:6],
            "source_labels": source_labels[:10],
        }
        for key, value in (context.context_debug or {}).items():
            if key not in payload:
                payload[key] = value
        return payload

    def _augment_context_debug_with_model_request(
        self,
        *,
        context: AgentContext,
        system_prompt: str,
        llm_messages: Sequence[Dict[str, Any]],
        request_mode: str,
    ) -> None:
        payload = dict(context.context_debug or {})
        payload["model_request_mode"] = str(request_mode or "").strip() or "direct"
        payload["model_system_prompt"] = str(system_prompt or "")
        assembled_messages = [
            dict(item)
            for item in list(llm_messages or [])
            if isinstance(item, dict)
        ]
        payload["model_messages_assembled_raw"] = assembled_messages
        normalized_messages: List[Dict[str, Any]]
        if request_mode == "function_calling":
            normalized_messages = self._normalize_messages_for_function_calling(llm_messages)
        elif request_mode == "xml":
            normalized_messages = self._normalize_messages_for_plain_chat(llm_messages)
        else:
            normalized_messages = assembled_messages
        payload["model_messages_raw"] = LLMService.sanitize_provider_messages(normalized_messages)
        if request_mode == "function_calling":
            payload["model_tool_schemas_raw"] = [
                dict(item)
                for item in list(self._collect_llm_tool_schemas(self._current_user_text(context)) or [])
                if isinstance(item, dict)
            ]
        else:
            payload["model_tool_schemas_raw"] = []
        context.context_debug = payload

    @staticmethod
    def _context_prefix_kind(message: Dict[str, Any]) -> str:
        if str(message.get("role", "")).lower() != "system":
            return ""
        content = str(message.get("content", "") or "")
        if content.startswith("关键历史锚点："):
            return "anchor"
        if content.startswith("持久历史锚点："):
            return "persisted_anchor"
        if content.startswith("更早历史摘要："):
            return "older_summary"
        if content.startswith("历史摘要："):
            return "persisted_summary"
        if content.startswith("以下是可参考的跨会话记忆"):
            return "memory"
        return ""

    @classmethod
    def _split_item_stream_entries(
        cls,
        entries: Sequence[Dict[str, Any]],
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, str]], Optional[int]]:
        store = ConversationItemStreamStore.from_payload(
            {"version": "conversation_item_stream.v1", "entries": list(entries or [])}
        )
        canonical = store.canonical_history(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        active_item_ids = {item.item_id for item in canonical.active_entries}
        return (
            [entry for entry in store.replay() if str(entry.get("item_id") or "").strip() in active_item_ids],
            list(canonical.replacement_history),
            canonical.boundary_message_id,
        )

    @classmethod
    def _history_messages_from_item_stream(
        cls,
        entries: Sequence[Dict[str, Any]],
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        store = ConversationItemStreamStore.from_payload(
            {"version": "conversation_item_stream.v1", "entries": list(entries or [])}
        )
        replay_rows = store.canonical_replay_rows(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        history_messages: List[Dict[str, Any]] = []
        for row in replay_rows:
            history_messages.append(
                cls._sanitize_message_for_context(
                    {
                        "role": str(row.get("role") or "assistant").strip().lower() or "assistant",
                        "content": str(row.get("content") or ""),
                        "thought": str(row.get("thought") or "").strip() or None,
                        "metadata": dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {},
                    }
                )
            )
        return history_messages

    @staticmethod
    def _split_context_windows(
        messages: Sequence[Dict[str, Any]],
        *,
        recent_turns: int,
        recently_slid_turns: int,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        if not rows:
            return [], [], []

        user_indices = [idx for idx, msg in enumerate(rows) if str(msg.get("role", "")).lower() == "user"]
        if len(user_indices) <= recent_turns:
            return [], [], rows

        recent_start = user_indices[-recent_turns]
        if recently_slid_turns <= 0:
            return rows[:recent_start], [], rows[recent_start:]

        slid_start_pos = max(len(user_indices) - recent_turns - recently_slid_turns, 0)
        slid_start = user_indices[slid_start_pos]
        older = rows[:slid_start]
        recently_slid = rows[slid_start:recent_start]
        recent = rows[recent_start:]
        return older, recently_slid, recent

    async def _prepare_llm_messages(self, context: AgentContext, system_prompt: str) -> List[Dict[str, Any]]:
        context.context_truncated = False
        context.message_tokens_before_trim = 0
        context.message_tokens_after_trim = 0
        sanitized = [self._sanitize_message_for_context(item) for item in context.messages]
        history_source = [
            self._sanitize_message_for_context(item)
            for item in list(context.history_messages or [])
            if isinstance(item, dict)
        ]
        if history_source and len(sanitized) >= len(history_source):
            ephemeral_messages = sanitized[len(history_source):]
        else:
            history_source = list(sanitized)
            ephemeral_messages: List[Dict[str, Any]] = []
        anchor_summary = ""
        recently_slid: List[Dict[str, Any]] = []
        if not bool(getattr(settings, "agent_context_budget_enabled", True)):
            context.context_debug = self._build_context_debug_payload(
                context=context,
                anchor_summary=anchor_summary,
                persisted_anchor_summary="",
                persisted_summary=context.context_summary,
                older_summary="",
                llm_messages=sanitized,
                recently_slid_messages=recently_slid,
                estimated_tokens=self._estimate_messages_tokens(sanitized),
                budget=max(int(getattr(settings, "agent_context_max_input_tokens", 10000)), 1024),
                window_turns=max(int(getattr(settings, "agent_context_window_turns", 8)), 1),
                total_messages=len(sanitized),
                older_messages_count=0,
                recent_messages_count=len(sanitized),
            )
            return sanitized

        window_turns = max(int(getattr(settings, "agent_context_window_turns", 8)), 1)
        recently_slid_turns = max(int(getattr(settings, "agent_context_recently_slid_turns", 2) or 2), 0)
        older, recently_slid, recent = self._split_context_windows(
            history_source,
            recent_turns=window_turns,
            recently_slid_turns=recently_slid_turns,
        )

        prefixes: List[Dict[str, Any]] = []
        persisted_history = dict(context.compacted_history or {}) if isinstance(context.compacted_history, dict) else {}
        persisted_anchor_summary = self._compact_debug_text(persisted_history.get("history_anchors", ""), 600)
        persisted_summary = self._compact_debug_text(
            persisted_history.get("history_summary", "") or context.context_summary,
            600,
        )
        replacement_history_entries = [
            {
                "role": str(item.get("role") or "system").strip().lower() or "system",
                "content": str(item.get("content") or ""),
            }
            for item in list(persisted_history.get("replacement_history") or [])
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ]
        older_summary = ""
        conversation_state_prompt = self._render_conversation_context_state(context.conversation_state)
        if conversation_state_prompt:
            prefixes.append({"role": "system", "content": f"会话上下文状态：\n{conversation_state_prompt}"})
        if persisted_anchor_summary:
            prefixes.append({"role": "system", "content": f"持久历史锚点：\n{persisted_anchor_summary}"})
        if persisted_summary and not replacement_history_entries:
            prefixes.append({"role": "system", "content": f"历史摘要：\n{persisted_summary}"})
        if context.memory_contexts:
            prefixes.append({"role": "system", "content": self._memory_prompt(context.memory_contexts)})
        summary_trigger_tokens = max(int(getattr(settings, "agent_context_summary_trigger_tokens", 7000) or 7000), 0)
        history_summary_source = older if older else recently_slid
        if (
            history_summary_source
            and not replacement_history_entries
            and self._estimate_messages_tokens(history_source + ephemeral_messages) >= summary_trigger_tokens
        ):
            older_summary = self._summarize_messages(history_summary_source, max_lines=10)
            prefixes.append({"role": "system", "content": f"更早历史摘要：\n{older_summary}"})
            context.context_summary = older_summary

        candidate = prefixes + replacement_history_entries + recently_slid + recent + ephemeral_messages
        context.message_tokens_before_trim = self._estimate_messages_tokens(candidate)
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
                for idx, item in enumerate(candidate):
                    if idx != last_user and is_observation(item):
                        drop = idx
                        break
            if drop is None:
                for kind in ("older_summary", "memory", "persisted_summary"):
                    for idx, item in enumerate(candidate):
                        if idx == last_user:
                            continue
                        if self._context_prefix_kind(item) == kind:
                            drop = idx
                            break
                    if drop is not None:
                        break
            if drop is None:
                for idx in range(last_user):
                    if self._context_prefix_kind(candidate[idx]) in {"anchor", "persisted_anchor"}:
                        continue
                    drop = idx
                    break
            if drop is None:
                for idx in range(len(candidate)):
                    if idx != last_user and self._context_prefix_kind(candidate[idx]) not in {"anchor", "persisted_anchor"}:
                        drop = idx
                        break
            if drop is None:
                for idx in range(len(candidate)):
                    if idx != last_user:
                        drop = idx
                        break
            if drop is None:
                break
            candidate.pop(drop)
            context.context_truncated = True

        estimated_tokens = self._estimate_messages_tokens(candidate)
        context.message_tokens_after_trim = estimated_tokens
        context.context_debug = self._build_context_debug_payload(
            context=context,
            anchor_summary=anchor_summary,
            persisted_anchor_summary=persisted_anchor_summary,
            persisted_summary=persisted_summary,
            older_summary=older_summary,
            llm_messages=candidate,
            recently_slid_messages=recently_slid,
            estimated_tokens=estimated_tokens,
            budget=budget,
            window_turns=window_turns,
            total_messages=len(history_source) + len(ephemeral_messages),
            older_messages_count=len(older),
            recent_messages_count=len(recent) + len(ephemeral_messages),
        )
        return candidate

    @staticmethod
    def _latest_item_stream_message_id(entries: Sequence[Dict[str, Any]]) -> Optional[int]:
        for item in reversed(list(entries or [])):
            if not isinstance(item, dict):
                continue
            raw_message_id = item.get("message_id")
            try:
                if raw_message_id is not None:
                    return int(raw_message_id)
            except Exception:
                continue
        return None

    async def _maybe_mid_run_compact(self, context: AgentContext, system_prompt: str) -> bool:
        if not bool(getattr(settings, "agent_mid_run_compaction_enabled", True)):
            return False
        if int(context.iteration or 0) < max(int(getattr(settings, "agent_mid_run_compaction_min_iteration", 2) or 2), 1):
            return False
        if int(context.mid_run_compactions or 0) >= max(
            int(getattr(settings, "agent_mid_run_compaction_max_per_run", 2) or 2),
            1,
        ):
            return False
        trigger_tokens = max(
            int(
                getattr(
                    settings,
                    "agent_mid_run_compaction_message_tokens_trigger",
                    max(int(getattr(settings, "agent_context_max_input_tokens", 10000) * 0.6), 2048),
                )
                or 0
            ),
            256,
        )
        message_pressure_triggered = int(context.message_tokens_before_trim or 0) >= trigger_tokens
        if not bool(context.context_truncated) and not message_pressure_triggered:
            return False

        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return False

        from app.services.conversation_context_compaction_service import (
            ConversationContextCompactionService,
            ConversationItemStreamUnavailableError,
        )

        item_stream_payload = ConversationContextCompactionService._require_item_stream_payload(
            int(conversation_id),
            await self.runtime_service.get_conversation_item_stream(int(conversation_id)),
        )
        store = ConversationItemStreamStore.from_payload(item_stream_payload)
        canonical = store.canonical_history(
            fallback_boundary_message_id=(
                context.compacted_history.get("compact_boundary_message_id")
                if isinstance(context.compacted_history, dict)
                else None
            ),
        )
        active_entries = [entry.__dict__ for entry in canonical.active_entries]
        payload_rows = [
            self._sanitize_message_for_context(item)
            for item in list(context.messages or [])
            if isinstance(item, dict)
        ]
        canonical_rows = store.canonical_replay_rows(
            fallback_boundary_message_id=(
                context.compacted_history.get("compact_boundary_message_id")
                if isinstance(context.compacted_history, dict)
                else None
            ),
        )
        if not payload_rows:
            payload_rows = canonical_rows
        tool_rows: List[Dict[str, Any]] = []
        tool_ledger_payload = await self.runtime_service.get_conversation_tool_ledger(int(conversation_id))
        if isinstance(tool_ledger_payload, dict):
            active_turn_ids = {
                str(entry.turn_id or "").strip()
                for entry in canonical.active_entries
                if str(entry.turn_id or "").strip()
            }
            for row in list(tool_ledger_payload.get("entries") or []):
                if not isinstance(row, dict):
                    continue
                row_turn_id = str(row.get("turn_id") or "").strip()
                if active_turn_ids and row_turn_id and row_turn_id not in active_turn_ids:
                    continue
                tool_rows.append(dict(row))
        if not tool_rows:
            tool_rows = ConversationContextCompactionService._item_stream_to_tool_rows(active_entries)
        latest_message_id = self._latest_item_stream_message_id(active_entries)
        artifacts = await ConversationContextCompactionService.build_artifacts(
            payload_rows,
            tool_ledger_entries=tool_rows,
            up_to_message_id=latest_message_id,
        )
        if not dict(artifacts.compacted_history or {}) and payload_rows is not canonical_rows:
            artifacts = await ConversationContextCompactionService.build_artifacts(
                canonical_rows,
                tool_ledger_entries=tool_rows,
                up_to_message_id=latest_message_id,
            )
        compacted_history = dict(artifacts.compacted_history or {})
        if not compacted_history:
            sanitized_rows = [
                self._sanitize_message_for_context(item)
                for item in list(payload_rows or canonical_rows)
                if isinstance(item, dict)
            ]
            older_rows, recently_slid_rows, _recent_rows = self._split_context_windows(
                sanitized_rows,
                recent_turns=max(int(getattr(settings, "agent_context_window_turns", 8) or 8), 1),
                recently_slid_turns=max(int(getattr(settings, "agent_context_recently_slid_turns", 2) or 2), 0),
            )
            compact_source = older_rows + recently_slid_rows
            if not compact_source:
                return False
            fallback_summary = self._summarize_messages(compact_source, max_lines=10)
            fallback_anchors = self._summarize_messages(compact_source, max_lines=6)
            compacted_history = ConversationContextCompactionService._normalize_compacted_history_payload(
                {
                    "history_anchors": fallback_anchors,
                    "history_summary": fallback_summary,
                    "replacement_history": [
                        {
                            "role": "system",
                            "content": fallback_summary or fallback_anchors,
                        }
                    ],
                },
                compacted_message_count=len(compact_source),
                up_to_message_id=latest_message_id,
            )
            artifacts.summary_text = fallback_summary
            artifacts.compacted_message_count = len(compact_source)
        if not compacted_history:
            return False

        compacted_history["mid_run"] = True
        await self.runtime_service.upsert_conversation_context_state(
            int(conversation_id),
            dict(artifacts.context_state or {}),
        )
        await self.runtime_service.upsert_conversation_compacted_history(
            int(conversation_id),
            compacted_history,
        )
        await self.runtime_service.append_conversation_history_event(
            int(conversation_id),
            title="mid_run_compact",
            detail=(
                f"iteration={int(context.iteration)}, "
                f"compacted_messages={artifacts.compacted_message_count}, "
                f"summary_chars={len(artifacts.summary_text or '')}, "
                f"up_to_message_id={latest_message_id or 0}"
            ),
        )
        await self.runtime_service.append_conversation_context_snapshot(
            int(conversation_id),
            build_context_snapshot_payload(
                mode="mid_run",
                context_state=artifacts.context_state,
                compacted_history=compacted_history,
                summary_text=artifacts.summary_text,
                compacted_message_count=artifacts.compacted_message_count,
                up_to_message_id=latest_message_id,
            ),
        )
        await self.runtime_service.append_conversation_item_entries(
            int(conversation_id),
            [
                {
                    "kind": "compact_boundary",
                    "turn_id": context.turn_id,
                    "role": "system",
                    "run_id": context.run_id,
                    "iteration": context.iteration,
                    "content": artifacts.summary_text,
                    "summary": str(compacted_history.get("history_anchors") or "").strip() or None,
                    "status": "mid_run",
                    "message_id": latest_message_id,
                    "metadata": {
                        "compact_boundary_message_id": compacted_history.get("compact_boundary_message_id"),
                        "replacement_history": list(compacted_history.get("replacement_history") or []),
                        "compacted_message_count": artifacts.compacted_message_count,
                        "keep_turn_id": context.turn_id,
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }
            ],
        )

        refreshed_item_stream = await self.runtime_service.get_conversation_item_stream(int(conversation_id))
        if not isinstance(refreshed_item_stream, dict):
            raise ConversationItemStreamUnavailableError(int(conversation_id))

        context.mid_run_compactions += 1
        context.context_summary = artifacts.summary_text or context.context_summary
        context.conversation_state = dict(artifacts.context_state or {})
        context.compacted_history = compacted_history
        context.item_stream = refreshed_item_stream
        context.history_messages = self._history_messages_from_item_stream(
            list(refreshed_item_stream.get("entries") or []),
            fallback_boundary_message_id=latest_message_id,
        )
        context.messages = [
            self._sanitize_message_for_context(item)
            for item in list(context.history_messages or [])
            if isinstance(item, dict)
        ]
        return True

    def _collect_llm_tool_schemas(self, user_text: str) -> List[Dict[str, Any]]:
        list_tools = getattr(self.tools, "list_tools", None)
        if not callable(list_tools):
            return []
        selected = self._last_tool_selection.get("selected_tools") or []
        if selected:
            try:
                return list_tools(include_tool_names=set(selected))
            except TypeError:
                return list_tools()
        try:
            return list_tools()
        except TypeError:
            return list_tools()

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

    @staticmethod
    def _truncate_failure_text(value: str, limit: int = 180) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

    @staticmethod
    def _normalize_search_query_tokens(query: str) -> set[str]:
        text = str(query or "").strip().lower()
        if not text:
            return set()
        return {
            token
            for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", text)
            if len(token) >= 2
        }

    @classmethod
    def _knowledge_query_similarity(cls, left: str, right: str) -> float:
        left_clean = str(left or "").strip().lower()
        right_clean = str(right or "").strip().lower()
        if not left_clean or not right_clean:
            return 0.0
        if left_clean == right_clean:
            return 1.0
        left_tokens = cls._normalize_search_query_tokens(left_clean)
        right_tokens = cls._normalize_search_query_tokens(right_clean)
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = len(left_tokens & right_tokens)
        return overlap / max(1, min(len(left_tokens), len(right_tokens)))

    @staticmethod
    def _successful_knowledge_search_queries(context: AgentContext) -> List[str]:
        queries: List[str] = []
        pending_query: Optional[str] = None
        for step in context.steps:
            if step.step_type == "action" and step.tool_name == "knowledge_search":
                pending_query = str((step.tool_input or {}).get("query") or "").strip()
                continue
            if step.step_type == "observation" and step.tool_name == "knowledge_search":
                if step.success and pending_query:
                    queries.append(pending_query)
                pending_query = None
        return queries

    @classmethod
    def _find_redundant_knowledge_search_queries(
        cls,
        context: AgentContext,
        calls: Sequence[ParsedToolCall],
    ) -> List[str]:
        previous_queries = cls._successful_knowledge_search_queries(context)
        if not previous_queries:
            return []
        knowledge_calls = [call for call in calls if call.name == "knowledge_search"]
        if not knowledge_calls or len(knowledge_calls) != len(calls):
            return []

        redundant_queries: List[str] = []
        for call in knowledge_calls:
            query = str(call.arguments.get("query") or "").strip()
            if not query:
                continue
            best_similarity = max(
                (cls._knowledge_query_similarity(query, previous) for previous in previous_queries),
                default=0.0,
            )
            if best_similarity >= 0.6:
                redundant_queries.append(query)
        return redundant_queries

    @staticmethod
    def _redundant_knowledge_search_observation(queries: Sequence[str]) -> str:
        joined = "；".join(str(item or "").strip() for item in queries if str(item or "").strip()) or "当前重复检索"
        return (
            "系统提示：已检测到与现有知识库结果高度相似的重复检索请求。"
            f"重复 query：{joined}。请直接基于现有 observation 给出最终回答，"
            "只使用已经出现过的 [来源X] 引用，不要继续用同义改写重复搜索。"
        )

    def _maybe_stop_after_repeated_tool_failures(
        self,
        context: AgentContext,
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        threshold = max(int(getattr(settings, "agent_tool_failure_streak_limit", 3) or 3), 1)
        observations = [
            event.get("data")
            for event in events
            if event.get("type") == "observation" and isinstance(event.get("data"), dict)
        ]
        if not observations:
            return None

        stop_tool = ""
        stop_count = 0
        stop_output = ""
        for item in observations:
            tool_name = str(item.get("tool") or "").strip()
            if not tool_name:
                continue
            if bool(item.get("success")):
                context.tool_failure_streaks[tool_name] = 0
                continue

            next_count = int(context.tool_failure_streaks.get(tool_name, 0)) + 1
            context.tool_failure_streaks[tool_name] = next_count
            if next_count >= threshold:
                stop_tool = tool_name
                stop_count = next_count
                stop_output = str(item.get("output") or item.get("error") or "")
                break

        if not stop_tool:
            return None

        recent_detail = self._truncate_failure_text(stop_output or "工具 observation 连续失败。")
        context.final_answer = (
            f"`{stop_tool}` 已连续失败 {stop_count} 次，已停止自动重试。"
            f"最近失败信息：{recent_detail}。建议先检查前置条件或调整指令后再继续。"
        )
        context.state = AgentState.DONE
        return (
            f"检测到 `{stop_tool}` 连续失败 {stop_count} 次，继续自动尝试大概率只会重复犯错，"
            "本轮提前停止。"
        )

    def _maybe_stop_after_authorization_required(
        self,
        context: AgentContext,
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        channel = str(getattr(self.runtime_context, "channel", "") or "").strip().lower()
        if channel not in {"codelab_agent", "notebook_agent"}:
            return None

        observations = [
            event.get("data")
            for event in events
            if event.get("type") == "observation" and isinstance(event.get("data"), dict)
        ]
        if not observations:
            return None

        latest_auth_failure: Optional[Dict[str, Any]] = None
        for item in observations:
            if str(item.get("error") or "").strip().lower() == "authorization_required":
                latest_auth_failure = item

        if latest_auth_failure is None:
            return None

        tool_name = str(latest_auth_failure.get("tool") or "").strip() or "当前操作"
        detail = self._truncate_failure_text(
            str(latest_auth_failure.get("output") or "当前操作需要用户授权后才能继续。"),
            limit=220,
        )
        context.final_answer = (
            f"{detail} 当前我会先停在建议模式，不再继续自动修改 Notebook。"
            "如果你希望我直接创建、更新单元格、执行代码或安装依赖，请先开启「允许 AI 操作 Notebook」。"
        )
        context.state = AgentState.DONE
        return f"检测到 `{tool_name}` 需要用户授权，已停止后续自动写操作并改为直接说明原因。"

    @staticmethod
    def _tool_repeat_signature(tool_name: str, tool_input: Optional[Dict[str, Any]]) -> str:
        try:
            encoded = json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            encoded = str(tool_input or {})
        return f"{tool_name}:{encoded}"

    def _maybe_interrupt_redundant_successful_reads(
        self,
        context: AgentContext,
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        read_only_tools = {"notebook_cell", "notebook_variables"}
        threshold = 3
        observations = [
            event.get("data")
            for event in events
            if event.get("type") == "observation" and isinstance(event.get("data"), dict)
        ]
        if not observations:
            return None

        latest_matching: Optional[Dict[str, Any]] = None
        for item in observations:
            tool_name = str(item.get("tool") or "").strip()
            if tool_name in read_only_tools and bool(item.get("success")):
                latest_matching = item

        if latest_matching is None:
            if any(event.get("type") in {"action", "observation", "answer"} for event in events):
                context.context_debug.pop("repeat_read_signature", None)
                context.context_debug.pop("repeat_read_count", None)
                context.context_debug.pop("repeat_read_interventions", None)
            return None

        tool_name = str(latest_matching.get("tool") or "").strip()
        signature = self._tool_repeat_signature(
            tool_name,
            latest_matching.get("input") if isinstance(latest_matching.get("input"), dict) else {},
        )
        previous_signature = str(context.context_debug.get("repeat_read_signature") or "")
        previous_count = int(context.context_debug.get("repeat_read_count") or 0)
        repeat_count = previous_count + 1 if signature == previous_signature else 1
        context.context_debug["repeat_read_signature"] = signature
        context.context_debug["repeat_read_count"] = repeat_count

        if repeat_count < threshold:
            return None

        observation_summary = self._truncate_failure_text(str(latest_matching.get("output") or ""), limit=240)
        interventions = int(context.context_debug.get("repeat_read_interventions") or 0)
        if interventions < 1:
            context.context_debug["repeat_read_interventions"] = interventions + 1
            context.messages.append(
                {
                    "role": "user",
                    "content": (
                        "<observation>\n"
                        f"系统提示：你已经连续 {repeat_count} 次调用 `{tool_name}` 读取同一目标，且 observation 已成功返回。"
                        "不要继续重复读取；请直接基于现有 notebook 信息回答用户问题。"
                        "如果信息仍不足，请明确指出缺失的前置步骤，而不是再次调用同一读取工具。\n"
                        f"最近 observation 摘要：{observation_summary}\n"
                        "</observation>\n\n"
                        "请现在直接给出最终回答。"
                    ),
                }
            )
            return "检测到重复读取同一 Notebook 信息，已强制要求基于现有 observation 收束回答。"

        context.final_answer = (
            f"`{tool_name}` 已连续重复读取同一目标 {repeat_count} 次，已停止自动重试。"
            f"最近 observation 摘要：{observation_summary}。"
            "这通常说明当前更需要基于已有 observation 直接作答，而不是继续读取同一单元格或变量。"
        )
        context.state = AgentState.DONE
        return f"检测到 `{tool_name}` 连续重复读取同一目标 {repeat_count} 次，本轮提前停止。"

    async def _execute_single_tool_call(
        self,
        context: AgentContext,
        call: ParsedToolCall,
        *,
        parallel_group: str,
    ) -> ExecutedToolCall:
        effective_arguments = self._apply_tool_call_overrides(call.name, call.arguments)
        action_event = {
            "type": "action",
            "data": {
                "tool": call.name,
                "input": effective_arguments,
                "tool_call_id": call.call_id,
                "parallel_group": parallel_group,
                "iteration": context.iteration,
            },
        }
        try:
            result = await self.tools.execute(call.name, **effective_arguments)
        except Exception as exc:
            contract = build_tool_error_contract(
                code="tool_dispatch_failed",
                message="工具调度失败",
                tool_name=call.name,
                stage="agent_execute",
                detail=str(exc),
                retryable=False,
                metadata={"exception_type": type(exc).__name__},
            )
            result = ToolResult(
                success=False,
                output=f"{contract['message']}: {exc}",
                error=str(contract["code"]),
                data=merge_error_contract(None, contract),
            )

        observation_output = result.output
        permission_required = self._tool_result_requires_permission(result)
        if call.name == "knowledge_search":
            context.knowledge_search_calls += 1
            observation_output = await self._compress_knowledge_observation(
                str(effective_arguments.get("query", "")),
                result,
                context=context,
            )
            context.allowed_source_labels.update(self._extract_source_labels(observation_output))
        elif call.name == "web_search":
            context.web_search_calls += 1
            observation_output = await self._compress_web_search_observation(
                str(call.arguments.get("query", "")),
                result,
                context=context,
            )
            context.allowed_web_source_labels.update(self._extract_web_source_labels(observation_output))

        observation_event = {
            "type": "observation",
            "data": {
                "tool": call.name,
                "success": bool(result.success),
                "output": observation_output,
                "data": result.data,
                "error": result.error,
                "error_contract": (result.data or {}).get("error_contract") if isinstance(result.data, dict) else None,
                "execution_time_ms": float(getattr(result, "execution_time_ms", 0.0) or 0.0),
                "output_tokens_estimate": int(getattr(result, "output_tokens_estimate", 0) or 0),
                "truncated": bool(getattr(result, "truncated", False)),
                "permission_required": permission_required,
                "tool_call_id": call.call_id,
                "parallel_group": parallel_group,
                "iteration": context.iteration,
                "input": effective_arguments,
            },
        }
        return ExecutedToolCall(
            action_event=action_event,
            observation_event=observation_event,
            tool_name=call.name,
            observation_output=observation_output,
            tool_call_id=call.call_id,
            arguments=dict(effective_arguments or {}),
            success=bool(result.success),
            error=str(result.error or "").strip() or None,
            permission_required=permission_required,
            execution_time_ms=float(getattr(result, "execution_time_ms", 0.0) or 0.0),
            output_tokens_estimate=int(getattr(result, "output_tokens_estimate", 0) or 0),
            truncated=bool(getattr(result, "truncated", False)),
            tool_message={
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": observation_output,
            },
        )

    @staticmethod
    def _tool_result_requires_permission(result: ToolResult) -> bool:
        error_text = str(getattr(result, "error", "") or "").strip().lower()
        if error_text == "authorization_required":
            return True
        data = result.data if isinstance(result.data, dict) else {}
        if bool(data.get("permission_required")) or bool(data.get("requires_authorization")):
            return True
        contract = data.get("error_contract") if isinstance(data, dict) else None
        if isinstance(contract, dict) and str(contract.get("code") or "").strip().lower() == "authorization_required":
            return True
        return False

    @classmethod
    def _tool_ledger_summary(cls, value: str, *, fallback: str = "") -> str:
        text = cls._truncate_failure_text(value, limit=320)
        if text:
            return text
        return cls._truncate_failure_text(fallback, limit=320)

    @classmethod
    def _tool_use_summary_text(cls, executed_calls: Sequence[ExecutedToolCall]) -> str:
        rows = [item for item in list(executed_calls or []) if isinstance(item, ExecutedToolCall)]
        if not rows:
            return ""

        success_rows = [item for item in rows if item.success]
        failed_rows = [item for item in rows if not item.success]
        permission_rows = [item for item in rows if item.permission_required]
        tool_names = [item.tool_name for item in rows if str(item.tool_name or "").strip()]
        unique_tool_names = list(dict.fromkeys(tool_names))

        if len(unique_tool_names) == 1:
            headline = f"`{unique_tool_names[0]}` 已执行"
        elif unique_tool_names:
            headline = "、".join(f"`{name}`" for name in unique_tool_names[:3]) + " 已执行"
        else:
            headline = "工具批次已执行"

        detail_parts: List[str] = []
        if success_rows:
            detail_parts.append(f"成功 {len(success_rows)}")
        if failed_rows:
            detail_parts.append(f"失败 {len(failed_rows)}")
        if permission_rows:
            detail_parts.append(f"需授权 {len(permission_rows)}")

        summaries = [
            cls._truncate_failure_text(item.observation_output or item.error or "", limit=90)
            for item in rows
            if cls._truncate_failure_text(item.observation_output or item.error or "", limit=90)
        ]
        suffix = f"（{'，'.join(detail_parts)}）" if detail_parts else ""
        preview = "；".join(summaries[:2])
        if preview:
            return f"{headline}{suffix}：{preview}"
        return f"{headline}{suffix}"

    async def _append_tool_use_summary_item(
        self,
        context: AgentContext,
        executed_calls: Sequence[ExecutedToolCall],
        *,
        parallel_group: str,
    ) -> None:
        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return
        summary_text = self._tool_use_summary_text(executed_calls)
        if not summary_text:
            return
        try:
            await self.runtime_service.append_conversation_item_entries(
                int(conversation_id),
                [
                    {
                        "kind": "tool_use_summary",
                        "turn_id": context.turn_id,
                        "role": "assistant",
                        "run_id": context.run_id,
                        "iteration": context.iteration,
                        "summary": summary_text,
                        "content": summary_text,
                        "status": "completed",
                        "parallel_group": parallel_group,
                        "metadata": {
                            "tool_names": [
                                item.tool_name
                                for item in executed_calls
                                if str(item.tool_name or "").strip()
                            ],
                            "tool_call_ids": [
                                item.tool_call_id
                                for item in executed_calls
                                if str(item.tool_call_id or "").strip()
                            ],
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                ],
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] append tool use summary item failed: {exc}")

    async def _append_tool_ledger_entries(
        self,
        context: AgentContext,
        entries: Sequence[Dict[str, Any]],
    ) -> None:
        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return
        normalized_entries = [dict(item) for item in list(entries or []) if isinstance(item, dict)]
        if not normalized_entries:
            return
        try:
            await self.runtime_service.append_conversation_tool_ledger_entries(
                int(conversation_id),
                normalized_entries,
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] append tool ledger failed: {exc}")

    def _build_tool_call_ledger_entries(
        self,
        context: AgentContext,
        calls: Sequence[ParsedToolCall],
        *,
        parallel_group: str,
    ) -> List[Dict[str, Any]]:
        created_at = datetime.utcnow().isoformat()
        return [
            {
                "entry_id": uuid.uuid4().hex,
                "kind": "tool_call",
                "tool_name": call.name,
                "turn_id": context.turn_id,
                "tool_call_id": call.call_id,
                "run_id": context.run_id,
                "iteration": context.iteration,
                "status": "started",
                "arguments": dict(call.arguments or {}),
                "summary": self._tool_ledger_summary(
                    json.dumps(call.arguments or {}, ensure_ascii=False, sort_keys=True),
                    fallback="tool call started",
                ),
                "parallel_group": parallel_group,
                "created_at": created_at,
            }
            for call in calls
        ]

    def _build_tool_result_ledger_entries(
        self,
        context: AgentContext,
        executed_calls: Sequence[ExecutedToolCall],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for item in executed_calls:
            status = "authorization_required" if item.permission_required else ("succeeded" if item.success else "failed")
            fallback = item.error or ("tool call succeeded" if item.success else "tool call failed")
            entries.append(
                {
                    "entry_id": uuid.uuid4().hex,
                    "kind": "tool_result",
                    "tool_name": item.tool_name,
                    "turn_id": context.turn_id,
                    "tool_call_id": item.tool_call_id,
                    "run_id": context.run_id,
                    "iteration": context.iteration,
                    "status": status,
                    "arguments": dict(item.arguments or {}),
                    "summary": self._tool_ledger_summary(item.observation_output, fallback=fallback),
                    "success": item.success,
                    "error": item.error,
                    "permission_required": item.permission_required,
                    "execution_time_ms": item.execution_time_ms,
                    "output_tokens_estimate": item.output_tokens_estimate,
                    "truncated": item.truncated,
                    "created_at": datetime.utcnow().isoformat(),
                }
            )
        return entries

    async def _execute_tool_calls(self, context: AgentContext, calls: Sequence[ParsedToolCall]) -> List[ExecutedToolCall]:
        if not calls:
            return []
        parallel_enabled = bool(getattr(settings, "agent_parallel_tool_calls_enabled", True))
        max_concurrency = max(int(getattr(settings, "agent_parallel_tool_calls_max_concurrency", 4)), 1)
        group_id = f"iter-{context.iteration}-{uuid.uuid4().hex[:6]}"
        await self._append_tool_ledger_entries(
            context,
            self._build_tool_call_ledger_entries(context, calls, parallel_group=group_id),
        )
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
        executed_calls = [item for item in results if item is not None]
        await self._append_tool_ledger_entries(
            context,
            self._build_tool_result_ledger_entries(context, executed_calls),
        )
        await self._append_tool_use_summary_item(
            context,
            executed_calls,
            parallel_group=group_id,
        )
        return executed_calls

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
        user_text = self._current_user_text(context)
        llm_messages = self._normalize_messages_for_function_calling(llm_messages)
        response = await self.llm.chat_with_tools(
            messages=llm_messages,
            tools=self._collect_llm_tool_schemas(user_text),
            system_prompt=system_prompt,
            temperature=settings.react_temperature,
            max_tokens=settings.llm_max_tokens,
            tool_choice=str(self._last_tool_selection.get("tool_choice") or "auto"),
        )
        self._accumulate_usage(context, response)
        content = str(response.get("content") or "")
        reasoning = str(response.get("reasoning") or "").strip()
        parsed_calls = self._normalize_tool_calls(response.get("tool_calls") or [])
        events: List[Dict[str, Any]] = []
        answer_hint = self._extract_answer_text(content)
        raw_thought_text = ""

        if reasoning:
            raw_thought_text = reasoning

        if not raw_thought_text and content.strip():
            raw_thought_text = self._extract_think_text(content)

        if not raw_thought_text and content.strip() and parsed_calls:
            raw_thought_text = self._strip_think_content(content)
        thought_text = self._coerce_thought_for_display(
            raw_thought_text,
            tool_names=[call.name for call in parsed_calls],
            answer_hint=answer_hint,
        )
        if thought_text:
            events.append({"type": "thought", "data": thought_text})

        if parsed_calls:
            redundant_queries = self._find_redundant_knowledge_search_queries(context, parsed_calls)
            if redundant_queries:
                notice = self._redundant_knowledge_search_observation(redundant_queries)
                events.append(
                    {
                        "type": "thought",
                        "data": "检测到重复知识库搜索，改为基于现有检索结果直接收束回答。",
                    }
                )
                context.messages.append(
                    {
                        "role": "user",
                        "content": f"<observation>\n{notice}\n</observation>\n\n请直接给出最终回答。",
                    }
                )
                return events, False
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

        answer = answer_hint
        if answer:
            if not thought_text:
                events.append({"type": "thought", "data": "已完成问题分析，准备给出答案。"})
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
        llm_messages = self._normalize_messages_for_plain_chat(llm_messages)
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
        answer_hint = str(parsed.get("answer") or self._extract_answer_text(content) or "")

        if parsed.get("thought"):
            actions = self._parse_actions(content)
            if not actions and isinstance(parsed.get("action"), dict):
                actions = [parsed["action"]]
            parsed_calls = [
                ParsedToolCall(
                    call_id=f"xml-{context.iteration}-{idx}",
                    name=str(action.get("tool") or ""),
                    arguments=action.get("input") if isinstance(action.get("input"), dict) else {},
                    arguments_raw=json.dumps(action, ensure_ascii=False),
                )
                for idx, action in enumerate(actions, start=1)
            ]
            display_thought = self._coerce_thought_for_display(
                str(parsed["thought"] or ""),
                tool_names=[call.name for call in parsed_calls],
                answer_hint=answer_hint,
            )
            if display_thought:
                events.append({"type": "thought", "data": display_thought})

        actions = self._parse_actions(content)
        if not actions and isinstance(parsed.get("action"), dict):
            actions = [parsed["action"]]
        if actions:
            parsed_calls = []
            for idx, action in enumerate(actions, start=1):
                parsed_calls.append(
                    ParsedToolCall(
                        call_id=f"xml-{context.iteration}-{idx}",
                        name=str(action.get("tool") or ""),
                        arguments=action.get("input") if isinstance(action.get("input"), dict) else {},
                        arguments_raw=json.dumps(action, ensure_ascii=False),
                    )
                )
            redundant_queries = self._find_redundant_knowledge_search_queries(context, parsed_calls)
            if redundant_queries:
                notice = self._redundant_knowledge_search_observation(redundant_queries)
                events.append(
                    {
                        "type": "thought",
                        "data": "检测到重复知识库搜索，改为基于现有检索结果直接收束回答。",
                    }
                )
                context.messages.append(
                    {
                        "role": "user",
                        "content": f"<observation>\n{notice}\n</observation>\n\n请直接给出最终回答。",
                    }
                )
                return events, False
            context.messages.append({"role": "assistant", "content": self._strip_think_content(content)})
            executed = await self._execute_tool_calls(context, parsed_calls)
            for item in executed:
                events.append(item.action_event)
                events.append(item.observation_event)
            context.messages.append({"role": "user", "content": self._build_observation_message_multi(executed)})
            return events, False

        answer = answer_hint
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
                    "memory_enabled": context.memory_enabled,
                },
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] persist failed: {exc}")

    async def _persist_memory(self, context: AgentContext) -> None:
        if not context.memory_enabled:
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
                content=f"用户问题: {self._current_user_text(context)}\n回答: {context.final_answer[:1000]}",
                importance=0.65,
                metadata={"source": "agent_answer"},
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] remember failed: {exc}")

    async def run(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = True,
        prepared_plan: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        context = AgentContext(
            messages=[self._sanitize_message_for_context(item) for item in messages],
            turn_id=self.runtime_context.turn_id,
            max_iterations=self.max_iterations,
        )
        self._routing_decision = None
        await self._prepare_runtime_context(context)
        prepared_system_prompt = ""
        prepared_llm_messages: List[Dict[str, Any]] = []
        if isinstance(prepared_plan, dict):
            prepared_system_prompt = str(prepared_plan.get("system_prompt") or "")
            prepared_llm_messages = [
                dict(item)
                for item in list(prepared_plan.get("llm_messages") or [])
                if isinstance(item, dict)
            ]
            routing_payload = prepared_plan.get("routing_decision")
            if isinstance(routing_payload, dict):
                try:
                    self._routing_decision = RoutingDecision(
                        intent=str(routing_payload.get("intent") or "general_chat"),
                        intent_user_text=str(routing_payload.get("intent_user_text") or ""),
                        carry_over_previous_goal=bool(routing_payload.get("carry_over_previous_goal")),
                        needs_tools=routing_payload.get("needs_tools"),
                        confidence=float(routing_payload.get("confidence") or 0.0),
                        reason=str(routing_payload.get("reason") or ""),
                        source=str(routing_payload.get("source") or "llm"),
                        latest_user_text=str(routing_payload.get("latest_user_text") or ""),
                    )
                except Exception:
                    self._routing_decision = None
            tool_selection = prepared_plan.get("tool_selection")
            if isinstance(tool_selection, dict):
                self._last_tool_selection = dict(tool_selection)
            conversation_state = prepared_plan.get("conversation_state")
            if isinstance(conversation_state, dict):
                context.conversation_state = dict(conversation_state)
            compacted_history = prepared_plan.get("compacted_history")
            if isinstance(compacted_history, dict):
                context.compacted_history = dict(compacted_history)
        if not self._routing_decision:
            await self._prepare_routing_decision(context.messages)

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
                yield {"type": "thinking", "data": "正在分析问题并规划下一步..."}
                use_fc = self._supports_function_calling()

                if i == 1 and prepared_system_prompt and prepared_llm_messages:
                    system_prompt = prepared_system_prompt
                    llm_messages = [dict(item) for item in prepared_llm_messages]
                else:
                    system_prompt = self._build_system_prompt(context.messages, function_calling=use_fc)
                    llm_messages = await self._prepare_llm_messages(context, system_prompt)
                await self._ensure_run_created(context)
                if await self._maybe_mid_run_compact(context, system_prompt):
                    thought_event = {
                        "type": "thought",
                        "data": "运行中已压缩较早上下文，并基于替代历史继续当前任务。",
                    }
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                    system_prompt = self._build_system_prompt(context.messages, function_calling=use_fc)
                    llm_messages = await self._prepare_llm_messages(context, system_prompt)

                if context.context_debug:
                    self._augment_context_debug_with_model_request(
                        context=context,
                        system_prompt=system_prompt,
                        llm_messages=llm_messages,
                        request_mode="function_calling" if use_fc else "xml",
                    )
                    yield {"type": "context_debug", "data": dict(context.context_debug)}

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
                authorization_stop_thought = self._maybe_stop_after_authorization_required(context, events)
                if authorization_stop_thought:
                    thought_event = {"type": "thought", "data": authorization_stop_thought}
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                    break
                repeated_read_thought = self._maybe_interrupt_redundant_successful_reads(context, events)
                if repeated_read_thought:
                    thought_event = {"type": "thought", "data": repeated_read_thought}
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                    if context.state == AgentState.DONE:
                        break
                    continue
                failure_stop_thought = self._maybe_stop_after_repeated_tool_failures(context, events)
                if failure_stop_thought:
                    thought_event = {"type": "thought", "data": failure_stop_thought}
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                    break
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
            reasoning_summary = await self._generate_reasoning_summary(context)
            final_thought = reasoning_summary or next(
                (s.content for s in reversed(context.steps) if s.step_type == "thought"),
                "",
            )
            await self._persist_memory(context)
            rag_metrics = self._build_rag_metrics(context)
            yield {
                "type": "done",
                "data": {
                    "iterations": context.iteration,
                    "steps": len(context.steps),
                    "turn_id": context.turn_id,
                    "run_id": context.run_id,
                    "thought": final_thought,
                    "answer": context.final_answer,
                    "rag_metrics": rag_metrics,
                    "reasoning_summary": reasoning_summary or None,
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


class ChatPreviewPlanner(AgentCore):
    """Lightweight planner for preview and direct-response preparation."""


def create_chat_preview_planner(
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    runtime_context: Optional[AgentRuntimeContext] = None,
    runtime_service: Optional[AgentRuntimeService] = None,
) -> ChatPreviewPlanner:
    logger.info("[AgentCore] create chat preview planner")
    return ChatPreviewPlanner(
        llm_service=llm_service,
        tool_registry=tool_registry,
        max_iterations=1,
        runtime_context=runtime_context,
        runtime_service=runtime_service,
    )


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
