from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from loguru import logger

from app.config import settings
from app.services.agent_runtime_service import (
    AgentRuntimeService,
    MemoryContext,
    get_agent_runtime_service,
)
from app.services.agent_profiles import (
    AgentProfile,
    build_agent_channel_tool_policy_prompt,
    resolve_agent_profile,
)
from app.services.agent_skill_service import get_agent_skill_service
from app.services.chat_context_store import ConversationItemStreamStore, build_context_snapshot_payload
from app.services.agent_tools import (
    ToolRegistry,
    ToolResult,
    reset_tool_live_event_emitter,
    set_tool_live_event_emitter,
)
from app.services.contextual_compression_service import (
    CompressionInput,
    get_contextual_compression_service,
)
from app.services.agent_tool_error_contract import (
    build_tool_error_contract,
    merge_error_contract,
)
from app.services.llm_service import LLMService
from app.services.model_context_windows import (
    builtin_model_context_windows,
    normalize_model_window_key,
    normalize_provider_name,
    parse_model_window_overrides,
    resolve_model_context_window,
)
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
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    turn_id: Optional[str] = None
    chat_preferences_override: Dict[str, Any] = field(default_factory=dict)
    rag_overrides: Dict[str, Any] = field(default_factory=dict)
    active_skill_names: List[str] = field(default_factory=list)
    live_event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None


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
    stable_prefix_cache_key: str = ""
    stable_prefix_cache_messages: List[Dict[str, Any]] = field(default_factory=list)
    stable_prefix_cache_hits: int = 0
    stable_prefix_cache_misses: int = 0
    source_items_by_label: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    prefetched_rag_messages: List[Dict[str, Any]] = field(default_factory=list)
    prefetched_rag_metadata: Dict[str, Any] = field(default_factory=dict)
    prefetched_rag_search_count: int = 0


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
    result_data: Dict[str, Any]
    tool_call_id: str
    arguments: Dict[str, Any]
    success: bool
    error: Optional[str]
    permission_required: bool
    execution_time_ms: float
    output_tokens_estimate: int
    truncated: bool
    metadata: Dict[str, Any] = field(default_factory=dict)


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

如果用户意图涉及论文复现、继续论文复现项目、检查论文实现仓库、或要求开始复现工作，先激活并阅读 `paper-reproduction` skill，再按该 skill 的约束继续工作。

当模型不支持 function calling 时：
1. 需要工具：<think>...</think><action>{{"tool":"工具名","input":{{...}}}}</action>
2. 直接回答：<think>...</think><answer>...</answer>
"""
    FUNCTION_CALLING_SYSTEM_PROMPT = """你是一个智能AI助手。

可用工具会通过独立的 tool/function schema 提供，不会在这里重复列出工具目录。
当工具能显著提升答案质量时再调用；优先选择最少且最合适的工具，避免为同一目标重复搜索、重复抓取或重复读取。
当已经获得足够证据时，直接给出答案。
如果用户意图涉及论文复现、继续论文复现项目、检查论文实现仓库、或要求开始复现工作，先激活并阅读 `paper-reproduction` skill，再按该 skill 的约束继续调用工具。
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
    _PAPER_SKILL_NAME = "paper-reproduction"
    _PAPER_RESEARCH_TOOL_PREFIX = "paper_research_"
    _LITERATURE_REVIEW_SKILL_NAME = "literature-review"
    _LITERATURE_REVIEW_TOOL_NAMES: set[str] = {
        "literature_review_start",
        "literature_review_download_pdf",
        "literature_review_read",
        "literature_review_search_zoekt",
        "literature_review_pdf_to_markdown",
        "review_writer",
    }
    _PAPER_SKILL_SELF_WORK_TOOL_NAMES: set[str] = {
        "project_bash",
        "project_write_file",
        "paper_research_write_execution_script",
        "paper_research_write_execution_spec",
        "paper_research_start_execution",
    }
    _PROJECT_TOOL_NAMES: set[str] = {
        "project_tree",
        "project_read_file",
        "project_write_file",
        "project_bash",
        "project_claude",
    }
    _PAPER_SKILL_HIDDEN_TOOL_NAMES: set[str] = set(_PAPER_SKILL_SELF_WORK_TOOL_NAMES)
    _PAPER_PREPARE_MARKERS = (
        "paper_research_prepare",
        "paper_research_status",
        "pdf2markdown",
        "pdf to markdown",
        "intake",
        "reference",
        "manifest",
        "产物",
        "状态",
    )
    _DECISION_STATE_STATUSES = {"active", "ready", "blocked", "waiting"}
    _DECISION_EVIDENCE_STATUSES = {"insufficient", "sufficient"}
    _PAPER_ARTIFACT_ACTION_MARKERS = (
        "生成",
        "创建",
        "写入",
        "保存",
        "归档",
        "刷新",
        "修订",
        "重写",
        "改写",
        "跑通",
        "验收",
        "继续",
        "generate",
        "create",
        "write",
        "save",
        "archive",
        "refresh",
        "revise",
        "rewrite",
        "rerun",
        "continue",
    )
    _PAPER_ARTIFACT_MUTATION_MARKERS = (
        "生成",
        "创建",
        "写入",
        "保存",
        "归档",
        "刷新",
        "修订",
        "重写",
        "改写",
        "generate",
        "create",
        "write",
        "save",
        "archive",
        "refresh",
        "revise",
        "rewrite",
    )
    _PAPER_EXECUTION_MARKERS = (
        "execution",
        "execution_spec",
        "execution result",
        "baseline_repro",
        "data_prep",
        "smoke_test",
        "执行",
        "运行",
        "训练",
        "复现",
        "结果",
        "日志",
        "read_execution",
        "start_execution",
        "paper_research_read_execution",
        "paper_research_start_execution",
    )
    _PAPER_READBACK_MARKERS = (
        "读回",
        "读取",
        "read back",
        "readback",
        "确认",
        "verify",
        "校验",
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
        self.skill_service = get_agent_skill_service()
        self._last_tool_selection: Dict[str, Any] = {}
        self._last_skill_resolution: Dict[str, Any] = {}
        self._routing_decision: Optional[RoutingDecision] = None
        self._active_chat_preferences: Dict[str, Any] = {}
        self._active_rag_overrides: Dict[str, Any] = {}
        self._active_channel_system_context: str = ""

    def set_channel_system_context(self, context: str) -> None:
        """Attach route-specific runtime context to the top-level model system prompt."""
        self._active_channel_system_context = str(context or "").strip()

    def _agent_profile(self) -> AgentProfile:
        return resolve_agent_profile(str(getattr(self.runtime_context, "channel", "") or "").strip())

    def _conversation_artifact_conversation_id(self) -> Optional[int]:
        profile = self._agent_profile()
        if not profile.load_conversation_artifacts:
            return None
        try:
            return int(self.runtime_context.conversation_id) if self.runtime_context.conversation_id is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _run_binding_conversation_id(self) -> Optional[int]:
        profile = self._agent_profile()
        if not profile.bind_runs_to_conversation:
            return None
        try:
            return int(self.runtime_context.conversation_id) if self.runtime_context.conversation_id is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    def _memory_scope(self) -> tuple[str, str]:
        explicit_scope_type = str(getattr(self.runtime_context, "scope_type", "") or "").strip()
        explicit_scope_id = str(getattr(self.runtime_context, "scope_id", "") or "").strip()
        if explicit_scope_type and explicit_scope_id:
            return explicit_scope_type, explicit_scope_id

        profile = self._agent_profile()
        preferred_scope = str(getattr(profile, "default_memory_scope_type", "user") or "user").strip().lower()
        if preferred_scope == "conversation":
            conversation_id = self._conversation_artifact_conversation_id()
            if conversation_id is not None:
                return "conversation", str(conversation_id)
        if preferred_scope == "notebook" and self.runtime_context.notebook_id is not None:
            return "notebook", str(self.runtime_context.notebook_id)
        return "user", str(self.runtime_context.user_id)

    @staticmethod
    def _latest_user_text(messages: Optional[Sequence[Dict[str, Any]]]) -> str:
        for item in reversed(messages or []):
            if str(item.get("role", "")).lower() == "user":
                return str(item.get("content", "") or "")
        return ""

    def _resolve_skills_for_messages(self, messages: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
        latest_user_text = self._intent_user_text(messages) or self._latest_user_text(messages)
        channel = str(getattr(self.runtime_context, "channel", "") or "chat").strip().lower() or "chat"
        active_skill_names = [
            str(item or "").strip()
            for item in list(getattr(self.runtime_context, "active_skill_names", []) or [])
            if str(item or "").strip()
        ]
        try:
            resolution = self.skill_service.resolve(
                latest_user_text,
                channel=channel,
                active_skill_names=active_skill_names,
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] skill resolution failed, continuing without skills: {exc}")
            payload: Dict[str, Any] = {
                "channel": channel,
                "latest_user_text": latest_user_text,
                "available_skills": [],
                "active_skills": [],
                "active_prompt": "",
                "active_prompt_tokens": 0,
            }
        else:
            payload = {
                "channel": resolution.channel,
                "latest_user_text": resolution.latest_user_text,
                "available_skills": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "path": item.path,
                        "config_path": item.config_path,
                        "interface_path": item.interface_path,
                        "session_system_prompt": item.session_system_prompt,
                        "score": int(item.score),
                        "activation_reason": item.activation_reason,
                        "display_name": item.display_name,
                        "short_description": item.short_description,
                        "default_prompt": item.default_prompt,
                        "when_to_use": item.when_to_use,
                        "user_invocable": bool(item.user_invocable),
                        "execution_context": item.execution_context,
                        "agent": item.agent,
                        "effort": item.effort,
                        "allow_implicit_invocation": bool(item.allow_implicit_invocation),
                        "enforced_tool_names": [str(name) for name in list(item.enforced_tool_names or []) if str(name or "").strip()],
                        "blocked_tool_names": [str(name) for name in list(item.blocked_tool_names or []) if str(name or "").strip()],
                        "scripts": [str(name) for name in list(item.scripts or []) if str(name or "").strip()],
                        "stage_names": [str(name) for name in list(item.stage_names or []) if str(name or "").strip()],
                        "stage_policies": [str(name) for name in list(item.stage_policies or []) if str(name or "").strip()],
                        "artifact_paths": [str(name) for name in list(item.artifact_paths or []) if str(name or "").strip()],
                        "continue_policies": [str(name) for name in list(item.continue_policies or []) if str(name or "").strip()],
                        "default_continue_policy": str(item.default_continue_policy or ""),
                    }
                    for item in resolution.available_skills
                ],
                "active_skills": [
                    {
                        "name": item.name,
                        "description": item.description,
                        "path": item.path,
                        "config_path": item.config_path,
                        "interface_path": item.interface_path,
                        "session_system_prompt": item.session_system_prompt,
                        "score": int(item.score),
                        "activation_reason": item.activation_reason,
                        "display_name": item.display_name,
                        "short_description": item.short_description,
                        "default_prompt": item.default_prompt,
                        "when_to_use": item.when_to_use,
                        "user_invocable": bool(item.user_invocable),
                        "execution_context": item.execution_context,
                        "agent": item.agent,
                        "effort": item.effort,
                        "allow_implicit_invocation": bool(item.allow_implicit_invocation),
                        "enforced_tool_names": [str(name) for name in list(item.enforced_tool_names or []) if str(name or "").strip()],
                        "blocked_tool_names": [str(name) for name in list(item.blocked_tool_names or []) if str(name or "").strip()],
                        "scripts": [str(name) for name in list(item.scripts or []) if str(name or "").strip()],
                        "stage_names": [str(name) for name in list(item.stage_names or []) if str(name or "").strip()],
                        "stage_policies": [str(name) for name in list(item.stage_policies or []) if str(name or "").strip()],
                        "artifact_paths": [str(name) for name in list(item.artifact_paths or []) if str(name or "").strip()],
                        "continue_policies": [str(name) for name in list(item.continue_policies or []) if str(name or "").strip()],
                        "default_continue_policy": str(item.default_continue_policy or ""),
                    }
                    for item in resolution.active_skills
                ],
                "active_prompt": resolution.active_prompt,
                "active_prompt_tokens": int(resolution.active_prompt_tokens),
                "active_system_prompt": resolution.active_system_prompt,
                "active_system_prompt_tokens": int(resolution.active_system_prompt_tokens),
                "enforced_tool_names": [str(name) for name in list(resolution.enforced_tool_names or []) if str(name or "").strip()],
                "blocked_tool_names": [str(name) for name in list(resolution.blocked_tool_names or []) if str(name or "").strip()],
                "persisted_active_skill_names": active_skill_names,
            }
        self._last_skill_resolution = payload
        return payload

    @staticmethod
    def _render_skill_catalog(skill_resolution: Dict[str, Any]) -> str:
        available_skills = [
            item for item in list(skill_resolution.get("available_skills") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if not available_skills:
            return ""
        active_skill_names = [
            str(item.get("name") or "").strip()
            for item in list(skill_resolution.get("active_skills") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        lines = [
            "## Available Skills",
            "你可以直接回答，也可以先调用 `activate_skill` 激活一个技能，再按照该技能的约束继续调用工具。",
            "如果用户当前是在做论文复现、继续复现项目、检查论文仓库、或明确说开始复现工作，必须先激活 `paper-reproduction` skill。",
            f"Current active skills: {', '.join(active_skill_names) if active_skill_names else 'none'}",
        ]
        for item in available_skills:
            name = str(item.get("name") or "").strip()
            description = str(item.get("short_description") or item.get("description") or item.get("when_to_use") or "").strip()
            if len(description) > 160:
                description = f"{description[:157].rstrip()}..."
            lines.append(f"- {name}: {description}")
        return "\n".join(lines).strip()

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

    def _active_skill_name_set(self) -> set[str]:
        names = {
            str(item or "").strip()
            for item in list((self._last_tool_selection or {}).get("active_skill_names") or [])
            if str(item or "").strip()
        }
        for item in list((self._last_skill_resolution or {}).get("active_skills") or []):
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.add(name)
        return names

    def _paper_skill_is_active_for_context(self, context: AgentContext) -> bool:
        if self._PAPER_SKILL_NAME in self._active_skill_name_set():
            return True
        runtime_skill_names = {
            str(item or "").strip()
            for item in list(getattr(self.runtime_context, "active_skill_names", None) or [])
            if str(item or "").strip()
        }
        if self._PAPER_SKILL_NAME in runtime_skill_names:
            return True
        conversation_state = context.conversation_state if isinstance(context.conversation_state, dict) else {}
        state_skill_names = {
            str(item or "").strip()
            for item in list(conversation_state.get("active_skill_names") or [])
            if str(item or "").strip()
        }
        if self._PAPER_SKILL_NAME in state_skill_names:
            return True
        workflow_binding = conversation_state.get("workflow_binding") if isinstance(conversation_state, dict) else {}
        if isinstance(workflow_binding, dict):
            return str(workflow_binding.get("skill") or "").strip() == self._PAPER_SKILL_NAME
        return False

    def _literature_review_skill_is_active_for_context(self, context: AgentContext) -> bool:
        if self._LITERATURE_REVIEW_SKILL_NAME in self._active_skill_name_set():
            return True
        runtime_skill_names = {
            str(item or "").strip()
            for item in list(getattr(self.runtime_context, "active_skill_names", None) or [])
            if str(item or "").strip()
        }
        if self._LITERATURE_REVIEW_SKILL_NAME in runtime_skill_names:
            return True
        conversation_state = context.conversation_state if isinstance(context.conversation_state, dict) else {}
        state_skill_names = {
            str(item or "").strip()
            for item in list(conversation_state.get("active_skill_names") or [])
            if str(item or "").strip()
        }
        if self._LITERATURE_REVIEW_SKILL_NAME in state_skill_names:
            return True
        workflow_binding = conversation_state.get("workflow_binding") if isinstance(conversation_state, dict) else {}
        if isinstance(workflow_binding, dict):
            return str(workflow_binding.get("skill") or "").strip() == self._LITERATURE_REVIEW_SKILL_NAME
        return False

    def _build_paper_skill_self_work_block_result(self, tool_name: str) -> ToolResult:
        normalized_tool_name = str(tool_name or "").strip()
        contract = build_tool_error_contract(
            code="paper_reproduction_requires_claude_worker",
            message="paper-reproduction 当前不允许主 agent 自行执行项目工作",
            tool_name=normalized_tool_name,
            stage="agent_execute",
            detail=(
                "复现执行必须交给 Claude Code。"
                "如果 project_claude 联系失败，主 agent 应直接说明 Claude Code 不可用并建议稍后重试，"
                "不能改用 project_bash、project_write_file 或 execution 脚本工具继续工作。"
            ),
            retryable=True,
            metadata={
                "required_worker": "project_claude",
                "blocked_tools": sorted(self._PAPER_SKILL_SELF_WORK_TOOL_NAMES),
            },
        )
        return ToolResult(
            success=False,
            output=(
                f"{contract['message']}: `{normalized_tool_name}` 已被阻止。"
                "请调用 `project_claude`；如果 Claude Code 当前超时或不可达，"
                "请如实告知用户并停止本轮执行，不要切换到 bash 或自行改文件。"
            ),
            error=str(contract["code"]),
            data=merge_error_contract(None, contract),
        )

    def _tool_failure_scope_reminder(self, context: AgentContext, tool_name: str, result: ToolResult) -> str:
        if bool(result.success):
            return ""
        normalized_tool_name = str(tool_name or "").strip()
        if not normalized_tool_name:
            return ""

        if normalized_tool_name == "project_claude" and self._paper_skill_is_active_for_context(context):
            return (
                "工具适用范围提示：`project_claude` 是 paper-reproduction 的唯一项目执行 worker。"
                "它失败通常表示 Claude Code/runtime-worker 当前不可用或超时。"
                "主 agent 不能改用 `project_bash`、`project_write_file`、"
                "`paper_research_start_execution` 等工具自行下载数据、运行训练或修改代码；"
                "应向用户报告 Claude Code 不可用，并等待用户重试或修复 runtime-worker。"
            )

        if self._paper_skill_is_active_for_context(context) and normalized_tool_name in self._PAPER_SKILL_SELF_WORK_TOOL_NAMES:
            return (
                "工具适用范围提示：paper-reproduction 已激活时，项目执行和代码修改必须交给 Claude Code。"
                f"`{normalized_tool_name}` 不适合由主 agent 在该工作流中作为替代执行路径使用。"
                "如果 Claude Code 不可达，请停止本轮并说明阻塞原因。"
            )

        if normalized_tool_name == "literature_review_download_pdf" and self._literature_review_skill_is_active_for_context(context):
            return (
                "工具适用范围提示：`literature_review_download_pdf` 只负责尝试保存单篇候选论文 PDF。"
                "403、404 或缺少 PDF URL 通常表示该候选论文暂时不可下载，"
                "应跳过这篇或重新搜索可下载的开放获取论文；不要反复调用同一个失败链接。"
            )

        if normalized_tool_name in {"docx_generate_with_claude", "docx_refine_with_claude"}:
            return (
                f"工具适用范围提示：`{normalized_tool_name}` 只负责 DOCX 工作区生成/修改。"
                "如果它失败或没有产出文件，应向用户报告 Claude/DOCX 生成失败或重新调用该工具重试；"
                "不要改用 `project_tree`、`project_read_file`、`project_bash`、`project_claude` 检查或补救。"
                "Project 工具只用于论文复现、代码优化、代码编写 Project 工作区。"
            )

        if normalized_tool_name in self._PROJECT_TOOL_NAMES:
            return (
                "工具适用范围提示：Project 工具只用于论文复现、代码优化、代码编写 Project 工作区，"
                "目录语义固定为 `/app/uploads/projects/{project_id}`。"
                "不要用于 DOCX 生成、文献综述工作区、模板管理、普通文件查看/下载，"
                "也不要作为其他 Claude/docx 工具失败后的 fallback。"
            )

        scope_reminders = {
            "project_bash": (
                "`project_bash` 只适用于明确允许主 agent 在 Project 根目录执行一次受控命令的场景；"
                "它不是 Claude Code 失败后的 fallback worker。"
            ),
            "project_write_file": (
                "`project_write_file` 只适用于写入一个已明确指定路径和完整内容的文件；"
                "参数不完整或需要调试/迭代时，应先报告缺失信息，不要伪造工具调用。"
            ),
            "paper_research_start_execution": (
                "`paper_research_start_execution` 只适用于已有有效 execution spec 的托管执行；"
                "不要用它替代 Claude Code 做开放式复现工作。"
            ),
            "paper_research_write_execution_spec": (
                "`paper_research_write_execution_spec` 只适用于把已确定的入口、命令和产物写成 execution spec；"
                "不是自由格式 shell wrapper。"
            ),
        }
        return scope_reminders.get(normalized_tool_name, "")

    @classmethod
    def _hidden_tool_names_for_active_skills(cls, skill_resolution: Dict[str, Any]) -> set[str]:
        active_skill_names = {
            str(item.get("name") or "").strip()
            for item in list(skill_resolution.get("active_skills") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        hidden_tool_names: set[str] = set()
        if cls._PAPER_SKILL_NAME in active_skill_names:
            hidden_tool_names.update(cls._PAPER_SKILL_HIDDEN_TOOL_NAMES)
        return hidden_tool_names

    @staticmethod
    def _contains_any_marker(text: str, markers: Sequence[str]) -> bool:
        lowered = str(text or "").lower()
        return any(str(marker or "").lower() in lowered for marker in markers if str(marker or "").strip())

    @staticmethod
    def _decision_action_for_tool(tool_name: str) -> Optional[str]:
        normalized = str(tool_name or "").strip()
        mapping = {
            "paper_research_write_execution_spec": "write_execution_spec",
            "paper_research_launch_claude_code": "start_execution",
            "paper_research_write_execution_script": "write_execution_script",
            "paper_research_start_execution": "start_execution",
            "paper_research_read_execution": "observe_execution",
            "paper_research_search_project_zoekt": "search_repo",
            "paper_research_probe_repo": "probe_repo",
            "paper_research_probe_url": "probe_url",
            "paper_research_assess_repo_mainpath": "assess_repo_mainpath",
            "web_search": "web_search",
            "web_scrape": "web_scrape",
        }
        return mapping.get(normalized)

    @classmethod
    def _should_enforce_decision_state_gate(cls, tool_name: str) -> bool:
        normalized = str(tool_name or "").strip()
        if normalized.startswith(cls._PAPER_RESEARCH_TOOL_PREFIX):
            return False
        return True

    @classmethod
    def _should_bypass_decision_state_gate_for_tool(
        cls,
        context: AgentContext,
        tool_name: str,
    ) -> bool:
        normalized = str(tool_name or "").strip()
        if normalized not in {"web_search", "web_scrape"}:
            return False
        conversation_state = (
            dict(context.conversation_state or {})
            if isinstance(context.conversation_state, dict)
            else {}
        )
        workflow_binding = cls._normalize_workflow_binding(
            conversation_state.get("workflow_binding") or {}
        )
        return str(workflow_binding.get("skill") or "").strip().lower() == cls._PAPER_SKILL_NAME

    @staticmethod
    def _tool_action_names(context: AgentContext) -> set[str]:
        return {
            str(step.tool_name or "").strip()
            for step in list(context.steps or [])
            if step.step_type == "action" and str(step.tool_name or "").strip()
        }

    @staticmethod
    def _successful_tool_names(context: AgentContext) -> set[str]:
        return {
            str(step.tool_name or "").strip()
            for step in list(context.steps or [])
            if step.step_type == "observation" and bool(step.success) and str(step.tool_name or "").strip()
        }

    def _missing_required_paper_skill_tool_calls(self, context: AgentContext) -> List[str]:
        if self._PAPER_SKILL_NAME not in self._active_skill_name_set():
            return []

        user_text = self._current_user_text(context)
        if not user_text:
            return []

        workflow_binding = dict((context.conversation_state or {}).get("workflow_binding") or {})
        has_paper_binding = bool(workflow_binding.get("paper_id") or workflow_binding.get("project_id"))
        is_prepare_or_status_request = self._contains_any_marker(user_text, self._PAPER_PREPARE_MARKERS)
        is_artifact_action = self._contains_any_marker(user_text, self._PAPER_ARTIFACT_ACTION_MARKERS)
        if not (has_paper_binding and (is_prepare_or_status_request or is_artifact_action)):
            return []

        attempted_tools = self._tool_action_names(context)
        paper_attempted = {
            name for name in attempted_tools
            if name.startswith(self._PAPER_RESEARCH_TOOL_PREFIX)
        }
        missing: List[str] = []
        if not paper_attempted:
            missing.append("any paper_research_* tool call")

        deduped: List[str] = []
        for item in missing:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _build_paper_skill_tool_guard_message(self, context: AgentContext, missing_tools: Sequence[str]) -> str:
        missing = ", ".join(str(item) for item in missing_tools if str(item or "").strip())
        if not missing:
            missing = "required paper_research tool calls"
        return (
            "当前激活的是 paper-reproduction，并且用户请求涉及 project/reference 准备或归档产物。"
            "不能用自然语言声明已经完成；必须先调用真实工具。\n"
            f"缺失工具调用: {missing}\n"
            "下一步请调用对应 paper_research_* 工具完成读取、写入或读回。"
            "如果写入工具返回失败，请基于真实失败结果解释原因；不要假装产物已经保存。"
        )

    def _maybe_guard_paper_skill_direct_answer(
        self,
        context: AgentContext,
        *,
        events: List[Dict[str, Any]],
    ) -> Optional[tuple[List[Dict[str, Any]], bool]]:
        missing_paper_tools = self._missing_required_paper_skill_tool_calls(context)
        if not missing_paper_tools:
            return None
        retries = int((context.context_debug or {}).get("paper_skill_tool_guard_retries") or 0)
        context.context_debug = {
            **dict(context.context_debug or {}),
            "paper_skill_tool_guard_retries": retries + 1,
            "paper_skill_tool_guard_missing": list(missing_paper_tools),
        }
        if retries >= 2:
            safe_answer = (
                "本轮没有完成必要的 paper_research 工具调用，"
                "因此不能确认对应产物已经生成、写入或读回。"
                "请重试该步骤，或先查看当前 Project/reference 状态。"
            )
            context.final_answer = safe_answer
            context.state = AgentState.DONE
            events.append({"type": "answer", "data": safe_answer})
            return events, True
        guard_message = self._build_paper_skill_tool_guard_message(context, missing_paper_tools)
        events.append(
            {
                "type": "thought",
                "data": "检测到当前阶段需要真实 paper_research 工具调用，已阻止直接回答并要求先执行工具。",
            }
        )
        context.messages.append({"role": "user", "content": guard_message})
        return events, False

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
        active_rag_overrides = dict(self._active_rag_overrides or {})
        if bool(active_rag_overrides.get("enabled", False)):
            decision = RoutingDecision(
                intent="general_chat",
                intent_user_text=self._intent_user_text(messages),
                carry_over_previous_goal=False,
                needs_tools=True,
                confidence=1.0,
                reason="current_turn_rag_enabled",
                source="rag_override",
                latest_user_text=latest_user_text,
            )
            self._routing_decision = decision
            return decision
        self._routing_decision = None
        return None

    async def resolve_routing_decision(
        self,
        messages: List[Dict[str, Any]],
    ) -> Optional[RoutingDecision]:
        sanitized = [self._sanitize_message_for_context(item) for item in messages]
        self._routing_decision = None
        await self._prepare_routing_decision(sanitized)
        return self._routing_decision_for_messages(sanitized)

    def _build_direct_response_system_prompt(
        self,
        messages: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> str:
        prompt = self.DIRECT_RESPONSE_SYSTEM_PROMPT
        if messages is not None or not self._last_skill_resolution:
            self._resolve_skills_for_messages(messages)
        profile = self._agent_profile()
        if profile.include_channel_system_context:
            channel_system_context = str(getattr(self, "_active_channel_system_context", "") or "").strip()
            if channel_system_context:
                prompt = f"{prompt}\n\n## CodeLab / Notebook Runtime Context\n{channel_system_context}"
        active_skill_prompt = str((self._last_skill_resolution or {}).get("active_prompt") or "").strip()
        active_system_prompt = str((self._last_skill_resolution or {}).get("active_system_prompt") or "").strip()
        skill_catalog_prompt = self._render_skill_catalog(self._last_skill_resolution or {})
        if active_system_prompt:
            prompt = f"{prompt}\n\n## Skill Session System Prompt\n{active_system_prompt}"
        if skill_catalog_prompt:
            prompt = f"{prompt}\n\n{skill_catalog_prompt}"
        if active_skill_prompt:
            prompt = f"{prompt}\n\n## 已激活 Skills\n{active_skill_prompt}"
        if profile.include_rag_overrides:
            rag_prompt = self._render_rag_overrides_prompt(self._active_rag_overrides)
            if rag_prompt:
                prompt = f"{prompt}\n\n## 本轮临时 RAG 注入\n{rag_prompt}"
        if profile.include_user_chat_preferences:
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
        system_prompt = self._build_direct_response_system_prompt(context.messages)
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
            system_prompt = self._build_direct_response_system_prompt(context.messages)
        else:
            use_fc = self._supports_function_calling()
            system_prompt = self._build_system_prompt(context.messages, function_calling=use_fc)
        llm_messages = await self._prepare_llm_messages(context, system_prompt)
        if self._should_force_initial_rag_retrieval(context):
            self._mark_forced_rag_search_debug(context, planned=True, executed=False)
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
        return build_agent_channel_tool_policy_prompt(self._agent_profile(), available_tools)

    @classmethod
    def _project_tool_policy_prompt(cls, available_tools: Sequence[str]) -> str:
        selected = {str(name or "").strip() for name in available_tools if str(name or "").strip()}
        project_tools = sorted(selected.intersection(cls._PROJECT_TOOL_NAMES))
        if not project_tools:
            return ""
        return "\n".join(
            [
                "## Project 工具适用范围（必须遵守）",
                "Project 工具是一组专用于论文复现、代码优化、代码编写 Project 工作区的工具。",
                "它们只操作 `/app/uploads/projects/{project_id}`，只在用户任务明确属于论文复现、代码实现/调试/优化时使用。",
                "DOCX 生成、文献综述、模板管理、普通文件查看/下载、artifact 更新，不要使用 project_* 工具。",
                "如果 DOCX/综述/其他 Claude 工具失败，不要把 project_* 当作 fallback；应报告对应工具失败或重试原工具。",
                "当前可用 Project 工具: " + ", ".join(project_tools),
            ]
        )

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
    def _stable_json_hash(payload: Dict[str, Any]) -> str:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            encoded = str(payload)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _context_window_overrides(cls) -> Dict[str, int]:
        raw = str(getattr(settings, "agent_context_model_window_overrides", "{}") or "{}").strip()
        return parse_model_window_overrides(raw)

    @classmethod
    def _normalize_provider_name(cls, provider: str) -> str:
        return normalize_provider_name(provider)

    @classmethod
    def _normalize_model_window_key(cls, key: str) -> str:
        return normalize_model_window_key(key)

    @classmethod
    def _builtin_model_context_windows(cls) -> Dict[str, int]:
        deepseek_test_alias = str(getattr(settings, "deepseek_test_model_alias", "deepseek-chat-test") or "deepseek-chat-test").strip().lower()
        deepseek_test_window = max(int(getattr(settings, "deepseek_test_model_window", 4096) or 4096), 1024)
        return builtin_model_context_windows(
            deepseek_test_alias=deepseek_test_alias,
            deepseek_test_window=deepseek_test_window,
        )

    @classmethod
    def _resolve_model_context_window(cls, provider: str, model_name: str) -> Optional[int]:
        return resolve_model_context_window(
            provider=provider,
            model_name=model_name,
            overrides_json=str(getattr(settings, "agent_context_model_window_overrides", "{}") or "{}"),
            deepseek_test_alias=str(getattr(settings, "deepseek_test_model_alias", "deepseek-chat-test") or "deepseek-chat-test"),
            deepseek_test_window=max(int(getattr(settings, "deepseek_test_model_window", 4096) or 4096), 1024),
        )

    def _current_model_context_window(self) -> Optional[int]:
        provider = self._normalize_provider_name(
            str(getattr(self.llm, "provider", "") or settings.default_llm_provider).strip()
        )
        llm_config = getattr(self.llm, "config", {}) or {}
        model_name = str(
            llm_config.get("context_window_model")
            or llm_config.get("display_model")
            or llm_config.get("model")
            or ""
        ).strip()
        return self._resolve_model_context_window(provider, model_name)

    @staticmethod
    def _configured_system_budget_cap() -> int:
        raw = int(getattr(settings, "agent_context_max_input_tokens", 0) or 0)
        return raw if raw > 0 else 0

    def _resolve_system_budget_cap(self, *, model_context_window: Optional[int]) -> int:
        configured_cap = self._configured_system_budget_cap()
        if configured_cap > 0:
            return max(configured_cap, 1024)
        if model_context_window is not None:
            return max(int(model_context_window), 1024)
        return 10000

    def _estimate_tool_schema_tokens(self, user_text: str) -> int:
        if not self._supports_function_calling():
            return 0
        try:
            schemas = self._collect_llm_tool_schemas(user_text)
        except Exception:
            return 0
        if not schemas:
            return 0
        try:
            raw = json.dumps(schemas, ensure_ascii=False, sort_keys=True)
        except Exception:
            raw = str(schemas)
        return max(estimate_tokens(raw), 0)

    def _build_budget_state(self, *, user_text: str, system_prompt: str) -> Dict[str, Any]:
        model_context_window = self._current_model_context_window()
        system_cap = self._resolve_system_budget_cap(model_context_window=model_context_window)
        min_budget = max(int(getattr(settings, "agent_context_budget_min_tokens", 1024) or 1024), 256)
        configured_reserve_tokens = max(int(getattr(settings, "agent_context_budget_reserve_tokens", 3072) or 3072), 0)
        completion_reserve_tokens = max(int(getattr(settings, "llm_max_tokens", 0) or 0), 0)
        reserve_tokens = max(configured_reserve_tokens, completion_reserve_tokens)
        system_prompt_tokens = max(estimate_tokens(system_prompt), 0)
        tool_schema_tokens = self._estimate_tool_schema_tokens(user_text)

        if model_context_window is None:
            effective_budget = max(min_budget, system_cap - system_prompt_tokens - tool_schema_tokens)
            return {
                "budget_mode": "system_cap",
                "model_context_window": None,
                "system_budget_cap": system_cap,
                "budget_reserve_tokens": reserve_tokens,
                "configured_budget_reserve_tokens": configured_reserve_tokens,
                "completion_reserve_tokens": completion_reserve_tokens,
                "system_prompt_tokens": system_prompt_tokens,
                "tool_schema_tokens_estimate": tool_schema_tokens,
                "effective_budget": effective_budget,
                "model_budget_before_cap": None,
            }

        model_budget = max(
            int(model_context_window) - int(system_prompt_tokens) - int(tool_schema_tokens) - int(reserve_tokens),
            256,
        )
        effective_budget = max(min_budget, min(system_cap, model_budget))
        return {
            "budget_mode": "model_aware",
            "model_context_window": int(model_context_window),
            "system_budget_cap": system_cap,
            "budget_reserve_tokens": reserve_tokens,
            "configured_budget_reserve_tokens": configured_reserve_tokens,
            "completion_reserve_tokens": completion_reserve_tokens,
            "system_prompt_tokens": system_prompt_tokens,
            "tool_schema_tokens_estimate": tool_schema_tokens,
            "effective_budget": effective_budget,
            "model_budget_before_cap": int(model_budget),
        }

    @staticmethod
    def _extract_citation_tokens_in_order(text: str) -> List[str]:
        seen: set[str] = set()
        labels: List[str] = []
        for match in re.finditer(r"\[(来源\d+|网页\d+)\]", text or ""):
            label = str(match.group(1) or "").strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
        return labels

    @classmethod
    def _build_tool_result_source_items(
        cls,
        *,
        tool_name: str,
        observation_output: str,
        result_data: Optional[Dict[str, Any]],
        result_metadata: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        metadata = dict(result_metadata or {}) if isinstance(result_metadata, dict) else {}
        data = dict(result_data or {}) if isinstance(result_data, dict) else {}
        labels = [
            str(item).strip()
            for item in list(metadata.get("source_labels") or cls._extract_citation_tokens_in_order(observation_output))
            if str(item).strip()
        ]
        if not labels:
            return []

        rows = cls._citation_result_rows(tool_name=tool_name, result_data=data)
        source_kind = str(metadata.get("source_kind") or tool_name).strip() or tool_name
        retrieval_scope = dict(metadata.get("retrieval_scope") or {}) if isinstance(metadata.get("retrieval_scope"), dict) else None
        provenance = dict(metadata.get("provenance") or data.get("provenance") or {}) if isinstance(metadata.get("provenance") or data.get("provenance"), dict) else {}
        items: List[Dict[str, Any]] = []

        for index, label in enumerate(labels):
            row = rows[index] if index < len(rows) else {}
            item: Dict[str, Any] = {
                "label": label,
                "source_kind": source_kind,
                "tool_name": tool_name,
            }

            if retrieval_scope:
                item["retrieval_scope"] = dict(retrieval_scope)
            for key in ("provider", "provider_route"):
                value = provenance.get(key) or row.get(key)
                if value is not None:
                    text = str(value).strip()
                    if text:
                        item[key] = text

            for key in (
                "title",
                "domain",
                "url",
                "knowledge_base",
                "document",
                "source_label",
                "citation_label",
            ):
                value = row.get(key)
                if value is not None:
                    text = str(value).strip()
                    if text:
                        item[key] = text

            excerpt = (
                row.get("reader_excerpt")
                or row.get("snippet")
                or row.get("summary")
                or row.get("content")
                or row.get("description")
            )
            if excerpt is not None:
                if source_kind in {"public_web_search", "public_web_page"}:
                    compacted = str(excerpt).strip()
                else:
                    compacted = cls._compact_debug_text(excerpt, 220)
                if compacted:
                    item["content_preview"] = compacted

            for key in ("rank", "chunk_index"):
                try:
                    value = row.get(key)
                    if value is not None:
                        item[key] = int(value)
                except Exception:
                    pass

            try:
                score = row.get("score")
                if score is not None:
                    item["retrieval_score"] = round(float(score) * 100.0, 1)
            except Exception:
                pass

            items.append(item)

        return items

    @classmethod
    def _citation_tool_name(
        cls,
        tool_name: str,
        result_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        normalized_name = str(tool_name or "").strip()
        lowered_name = normalized_name.lower()
        data = dict(result_data or {}) if isinstance(result_data, dict) else {}
        if normalized_name in {"knowledge_search", "web_search"}:
            return normalized_name

        source_kind = str(data.get("source_kind") or "").strip().lower()
        if source_kind == "knowledge_base_search":
            return "knowledge_search"
        if source_kind in {"public_web_search", "public_web_page"}:
            return "web_search"

        provenance = dict(data.get("provenance") or {}) if isinstance(data.get("provenance"), dict) else {}
        tool_kind = str(
            data.get("tool_kind")
            or provenance.get("tool_kind")
            or data.get("local_tool_name")
            or ""
        ).strip().lower()
        if tool_kind == "knowledge_search":
            return "knowledge_search"
        if tool_kind in {"web_search", "web_scrape"}:
            return "web_search"

        if lowered_name.startswith("mcp.") and any(
            token in lowered_name for token in ("tavily", "firecrawl")
        ) and any(
            token in lowered_name
            for token in ("search", "scrape", "extract", "crawl")
        ):
            return "web_search"
        return ""

    @classmethod
    def _citation_result_rows(
        cls,
        *,
        tool_name: str,
        result_data: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        data = dict(result_data or {}) if isinstance(result_data, dict) else {}
        rows = [dict(item) for item in list(data.get("results") or []) if isinstance(item, dict)]
        if rows:
            return rows

        citation_tool = cls._citation_tool_name(tool_name, data)
        if citation_tool != "web_search":
            return []

        public_links = [dict(item) for item in list(data.get("public_links") or []) if isinstance(item, dict)]
        if public_links:
            normalized_rows: List[Dict[str, Any]] = []
            for index, item in enumerate(public_links, start=1):
                href = str(item.get("href") or item.get("url") or "").strip()
                if not href:
                    continue
                normalized_rows.append(
                    {
                        "rank": index,
                        "title": str(item.get("label") or href).strip(),
                        "url": href,
                        "domain": cls._extract_hostname(href),
                        "snippet": str(
                            item.get("snippet")
                            or data.get("markdown")
                            or data.get("text")
                            or data.get("content")
                            or data.get("reader_summary")
                            or ""
                        ).strip(),
                        "reader_excerpt": str(
                            item.get("snippet")
                            or data.get("markdown")
                            or data.get("text")
                            or data.get("content")
                            or data.get("reader_summary")
                            or ""
                        ).strip(),
                    }
                )
            if normalized_rows:
                return normalized_rows

        metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), dict) else {}
        url = str(data.get("url") or metadata.get("url") or "").strip()
        title = str(
            metadata.get("title")
            or data.get("title")
            or data.get("source_domain")
            or url
            or "Public web result"
        ).strip()
        snippet = str(
            data.get("markdown")
            or data.get("text")
            or data.get("content")
            or data.get("reader_summary")
            or data.get("summary")
            or data.get("description")
            or ""
        ).strip()
        if not (url or title or snippet):
            return []
        return [
            {
                "rank": 1,
                "title": title,
                "url": url,
                "domain": str(data.get("source_domain") or cls._extract_hostname(url)).strip(),
                "snippet": snippet,
                "reader_excerpt": snippet or title,
            }
        ]

    @staticmethod
    def _remember_source_items(context: AgentContext, items: Sequence[Dict[str, Any]]) -> None:
        for item in list(items or []):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            context.source_items_by_label[label] = dict(item)

    async def _set_active_skill_names(
        self,
        context: AgentContext,
        active_skill_names: Sequence[str],
        *,
        update_timestamp: bool = True,
    ) -> None:
        normalized_active_skill_names: List[str] = []
        for item in list(active_skill_names or []):
            name = str(item or "").strip()
            if not name or name in normalized_active_skill_names:
                continue
            normalized_active_skill_names.append(name)
        if not normalized_active_skill_names:
            return

        self.runtime_context.active_skill_names = list(normalized_active_skill_names)
        if isinstance(self._last_tool_selection, dict):
            self._last_tool_selection["active_skill_names"] = list(normalized_active_skill_names)
        if isinstance(context.conversation_state, dict):
            next_state = dict(context.conversation_state or {})
        else:
            next_state = {}
        next_state["active_skill_names"] = list(normalized_active_skill_names)
        if update_timestamp:
            next_state["active_skill_updated_at"] = datetime.utcnow().isoformat()
        context.conversation_state = next_state

        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return
        try:
            await self.runtime_service.upsert_conversation_context_state(
                int(conversation_id),
                dict(next_state),
            )
        except Exception as exc:
            logger.warning(f"[AgentCore] failed to persist active skills for conversation {conversation_id}: {exc}")

    @classmethod
    def _normalize_workflow_binding(cls, binding: Any) -> Dict[str, Any]:
        if not isinstance(binding, dict):
            return {}

        def _coerce_int(value: Any) -> Optional[int]:
            if value in (None, "", False):
                return None
            try:
                normalized = int(value)
            except Exception:
                return None
            return normalized if normalized > 0 else None

        def _coerce_text(value: Any) -> Optional[str]:
            text = str(value or "").strip()
            return text or None

        normalized: Dict[str, Any] = {}
        for field in ("skill", "notebook_id", "current_stage", "current_draft_id", "baseline_execution_id", "tuning_execution_id", "updated_at"):
            value = _coerce_text(binding.get(field))
            if value:
                normalized[field] = value
        for field in ("paper_id", "project_id", "workspace_id"):
            value = _coerce_int(binding.get(field))
            if value is not None:
                normalized[field] = value
        return normalized

    @classmethod
    def _merge_workflow_binding(cls, existing: Any, incoming: Any) -> Dict[str, Any]:
        existing_normalized = cls._normalize_workflow_binding(existing)
        incoming_normalized = cls._normalize_workflow_binding(incoming)
        if not existing_normalized and not incoming_normalized:
            return {}

        merged = dict(existing_normalized)
        merged.update(incoming_normalized)
        merged["updated_at"] = (
            str(
                incoming_normalized.get("updated_at")
                or existing_normalized.get("updated_at")
                or datetime.utcnow().isoformat()
            ).strip()
            or datetime.utcnow().isoformat()
        )
        return merged

    @classmethod
    def _normalize_decision_state(
        cls,
        payload: Any,
        *,
        workflow_binding: Any = None,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}

        def _coerce_text(value: Any, limit: int = 160) -> Optional[str]:
            compacted = cls._compact_debug_text(value or "", limit)
            return compacted or None

        normalized: Dict[str, Any] = {}
        status = str(payload.get("status") or "").strip().lower()
        if status in cls._DECISION_STATE_STATUSES:
            normalized["status"] = status
        evidence_status = str(payload.get("evidence_status") or "").strip().lower()
        if evidence_status in cls._DECISION_EVIDENCE_STATUSES:
            normalized["evidence_status"] = evidence_status
        next_action = _coerce_text(payload.get("next_action"), 96)
        if next_action:
            normalized["next_action"] = next_action
        blocked_reason = _coerce_text(payload.get("blocked_reason"), 120)
        if blocked_reason:
            normalized["blocked_reason"] = blocked_reason
        allowed_actions = [
            item
            for item in (
                _coerce_text(raw, 72)
                for raw in list(payload.get("allowed_actions") or [])
            )
            if item
        ]
        if allowed_actions:
            normalized["allowed_actions"] = allowed_actions[:6]
        if payload.get("repo_edit_allowed") is not None:
            normalized["repo_edit_allowed"] = bool(payload.get("repo_edit_allowed"))

        binding = cls._normalize_workflow_binding(workflow_binding or {})
        if binding.get("skill") == cls._PAPER_SKILL_NAME and "repo_edit_allowed" not in normalized:
            normalized["repo_edit_allowed"] = False

        if normalized.get("blocked_reason") and "status" not in normalized:
            normalized["status"] = "blocked"
        if normalized.get("next_action") and "allowed_actions" not in normalized:
            normalized["allowed_actions"] = [str(normalized["next_action"])]
        if normalized.get("status") == "blocked" and "evidence_status" not in normalized:
            normalized["evidence_status"] = "sufficient"
        return normalized

    @classmethod
    def _merge_decision_state(
        cls,
        existing: Any,
        incoming: Any,
        *,
        workflow_binding: Any = None,
    ) -> Dict[str, Any]:
        existing_normalized = cls._normalize_decision_state(
            existing,
            workflow_binding=workflow_binding,
        )
        incoming_normalized = cls._normalize_decision_state(
            incoming,
            workflow_binding=workflow_binding,
        )
        if not existing_normalized and not incoming_normalized:
            return {}

        merged = dict(existing_normalized)
        for key, value in incoming_normalized.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        return cls._normalize_decision_state(merged, workflow_binding=workflow_binding)

    @classmethod
    def _merge_conversation_state_with_workflow_binding(
        cls,
        existing_state: Any,
        next_state: Any,
    ) -> Dict[str, Any]:
        merged_state = dict(next_state or {}) if isinstance(next_state, dict) else {}
        existing_binding = dict(existing_state.get("workflow_binding") or {}) if isinstance(existing_state, dict) else {}
        next_binding = dict(merged_state.get("workflow_binding") or {}) if isinstance(merged_state, dict) else {}
        merged_binding = cls._merge_workflow_binding(existing_binding, next_binding)
        if merged_binding:
            merged_state["workflow_binding"] = merged_binding
        else:
            merged_state.pop("workflow_binding", None)
        existing_decision_state = (
            dict(existing_state.get("decision_state") or {})
            if isinstance(existing_state, dict)
            else {}
        )
        next_decision_state = (
            dict(merged_state.get("decision_state") or {})
            if isinstance(merged_state, dict)
            else {}
        )
        merged_decision_state = cls._merge_decision_state(
            existing_decision_state,
            next_decision_state,
            workflow_binding=merged_binding,
        )
        if merged_decision_state:
            merged_state["decision_state"] = merged_decision_state
        else:
            merged_state.pop("decision_state", None)
        return merged_state

    @classmethod
    def _extract_workflow_binding_from_tool_result(
        cls,
        *,
        tool_name: str,
        result_data: Any,
    ) -> Dict[str, Any]:
        if not str(tool_name or "").startswith(cls._PAPER_RESEARCH_TOOL_PREFIX):
            return {}
        if not isinstance(result_data, dict):
            return {}

        paper_payload = dict(result_data.get("paper") or {})
        project_payload = dict(result_data.get("project") or {})
        workspace_payload = dict(result_data.get("workspace") or {})
        status_summary = dict(result_data.get("status_summary") or {})
        background_execution = (
            dict(result_data.get("background_execution") or {})
            if isinstance(result_data.get("background_execution"), dict)
            else {}
        )

        binding: Dict[str, Any] = {
            "skill": cls._PAPER_SKILL_NAME,
            "paper_id": paper_payload.get("id"),
            "project_id": project_payload.get("id") or result_data.get("project_id") or background_execution.get("project_id"),
            "workspace_id": workspace_payload.get("id") or result_data.get("workspace_id"),
            "notebook_id": workspace_payload.get("notebook_id") or result_data.get("notebook_id"),
            "current_stage": (
                status_summary.get("current_stage")
                or background_execution.get("stage")
                or result_data.get("current_stage")
            ),
            "current_draft_id": (
                result_data.get("draft_id")
                or result_data.get("run_id")
                or result_data.get("label")
            ),
            "baseline_execution_id": (
                status_summary.get("baseline_execution_id")
                or (
                    background_execution.get("execution_id")
                    if str(background_execution.get("stage") or "").strip().lower() == "baseline_repro"
                    else None
                )
            ),
            "tuning_execution_id": (
                status_summary.get("tuning_execution_id")
                or (
                    background_execution.get("execution_id")
                    if str(background_execution.get("stage") or "").strip().lower() == "tuning"
                    else None
                )
            ),
            "updated_at": datetime.utcnow().isoformat(),
        }
        return cls._normalize_workflow_binding(binding)

    async def _maybe_update_workflow_binding_from_tool_result(
        self,
        context: AgentContext,
        call: ParsedToolCall,
        result: ToolResult,
    ) -> None:
        if not bool(result.success):
            return

        next_binding = self._extract_workflow_binding_from_tool_result(
            tool_name=call.name,
            result_data=result.data,
        )
        if not next_binding:
            return

        existing_state = dict(context.conversation_state or {}) if isinstance(context.conversation_state, dict) else {}
        merged_state = self._merge_conversation_state_with_workflow_binding(
            existing_state,
            {
                **existing_state,
                "workflow_binding": next_binding,
            },
        )
        if merged_state == existing_state:
            return
        context.conversation_state = merged_state

        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return
        try:
            await self.runtime_service.upsert_conversation_context_state(
                int(conversation_id),
                dict(merged_state),
            )
        except Exception as exc:
            logger.warning(
                f"[AgentCore] failed to persist workflow binding for conversation {conversation_id}: {exc}"
            )

    async def _maybe_pin_skill_from_tool_result(
        self,
        context: AgentContext,
        call: ParsedToolCall,
        result: ToolResult,
    ) -> None:
        if not bool(result.success):
            return

        if call.name == "activate_skill" and isinstance(result.data, dict):
            active_skill_names = [
                str(item or "").strip()
                for item in list(result.data.get("active_skill_names") or [])
                if str(item or "").strip()
            ]
            if active_skill_names:
                await self._set_active_skill_names(context, active_skill_names)
            return

        if call.name in self._LITERATURE_REVIEW_TOOL_NAMES:
            current_active_skill_names = [
                str(item or "").strip()
                for item in list(getattr(self.runtime_context, "active_skill_names", []) or [])
                if str(item or "").strip()
            ]
            if self._LITERATURE_REVIEW_SKILL_NAME not in current_active_skill_names:
                await self._set_active_skill_names(
                    context,
                    [*current_active_skill_names, self._LITERATURE_REVIEW_SKILL_NAME],
                )
            return

        if not str(call.name or "").startswith(self._PAPER_RESEARCH_TOOL_PREFIX):
            return

        current_active_skill_names = [
            str(item or "").strip()
            for item in list(getattr(self.runtime_context, "active_skill_names", []) or [])
            if str(item or "").strip()
        ]
        if self._PAPER_SKILL_NAME in current_active_skill_names:
            return

        await self._set_active_skill_names(
            context,
            [*current_active_skill_names, self._PAPER_SKILL_NAME],
        )

    @classmethod
    def _build_citation_index(cls, answer: str, context: AgentContext) -> Dict[str, Dict[str, Any]]:
        citation_index: Dict[str, Dict[str, Any]] = {}
        for label in cls._extract_citation_tokens_in_order(answer):
            item = dict((context.source_items_by_label or {}).get(label) or {})
            if not item:
                item = {
                    "label": label,
                    "source_kind": "knowledge_base_search" if label.startswith("来源") else "public_web_search",
                }
            else:
                item["label"] = label
            citation_index[label] = item
        return citation_index

    @classmethod
    def _normalize_tool_result_metadata(
        cls,
        *,
        tool_name: str,
        observation_output: str,
        result_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        data = dict(result_data or {}) if isinstance(result_data, dict) else {}
        metadata: Dict[str, Any] = {}
        source_kind = str(data.get("source_kind") or "").strip()
        if source_kind:
            metadata["source_kind"] = source_kind

        source_labels = sorted(
            {
                f"来源{label}" for label in ReActAgent._extract_source_labels(observation_output or "")
            }
            | {
                f"网页{label}" for label in ReActAgent._extract_web_source_labels(observation_output or "")
            }
        )
        if source_labels:
            metadata["source_labels"] = source_labels[:12]

        total = data.get("total")
        try:
            if total is None and isinstance(data.get("results"), list):
                total = len(data.get("results") or [])
            total_int = int(total) if total is not None else None
        except Exception:
            total_int = None
        if total_int is not None:
            metadata["result_count"] = max(total_int, 0)

        if isinstance(data.get("retrieval_scope"), dict):
            metadata["retrieval_scope"] = dict(data.get("retrieval_scope") or {})
        if isinstance(data.get("retrieval_runtime"), dict):
            metadata["retrieval_runtime"] = dict(data.get("retrieval_runtime") or {})
        if isinstance(data.get("provenance"), dict):
            metadata["provenance"] = {
                key: value
                for key, value in dict(data.get("provenance") or {}).items()
                if value is not None and str(value).strip()
            }

        previews: List[Dict[str, Any]] = []
        for row in list(data.get("results") or [])[:4]:
            if not isinstance(row, dict):
                continue
            preview: Dict[str, Any] = {}
            for key in (
                "title",
                "url",
                "domain",
                "knowledge_base",
                "document",
                "source_label",
                "citation_label",
                "provider",
            ):
                value = row.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    preview[key] = text
            rank = row.get("rank")
            try:
                if rank is not None:
                    preview["rank"] = int(rank)
            except Exception:
                pass
            if preview:
                previews.append(preview)
        if previews:
            metadata["evidence_preview"] = previews
        source_items = cls._build_tool_result_source_items(
            tool_name=tool_name,
            observation_output=observation_output,
            result_data=data,
            result_metadata=metadata,
        )
        if source_items:
            metadata.setdefault("source_kind", tool_name)
            metadata["source_items"] = source_items

        if not metadata and tool_name:
            metadata["source_kind"] = tool_name
        return metadata

    def _build_stable_prefix_messages(
        self,
        *,
        context: AgentContext,
        conversation_state_prompt: str,
        persisted_anchor_summary: str,
        persisted_summary: str,
        replacement_history_entries: Sequence[Dict[str, Any]],
        memory_prompt: str,
    ) -> List[Dict[str, Any]]:
        payload = {
            "conversation_state_prompt": conversation_state_prompt,
            "persisted_anchor_summary": persisted_anchor_summary,
            "persisted_summary": persisted_summary if not replacement_history_entries else "",
            "memory_prompt": memory_prompt,
            "replacement_history_present": bool(replacement_history_entries),
        }
        cache_key = self._stable_json_hash(payload)
        if cache_key and cache_key == str(context.stable_prefix_cache_key or "") and context.stable_prefix_cache_messages:
            context.stable_prefix_cache_hits += 1
            return [dict(item) for item in list(context.stable_prefix_cache_messages or [])]

        prefixes: List[Dict[str, Any]] = []
        if conversation_state_prompt:
            prefixes.append({"role": "system", "content": f"会话上下文状态：\n{conversation_state_prompt}"})
        if persisted_anchor_summary:
            prefixes.append({"role": "system", "content": f"持久历史锚点：\n{persisted_anchor_summary}"})
        if persisted_summary and not replacement_history_entries:
            prefixes.append({"role": "system", "content": f"历史摘要：\n{persisted_summary}"})
        if memory_prompt:
            prefixes.append({"role": "system", "content": memory_prompt})

        context.stable_prefix_cache_key = cache_key
        context.stable_prefix_cache_messages = [dict(item) for item in prefixes]
        context.stable_prefix_cache_misses += 1
        return prefixes

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
        workflow_binding = cls._normalize_workflow_binding(state.get("workflow_binding") or {})
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
            provenance_hints = [
                cls._compact_debug_text(name, 72)
                for name in list(item.get("provenance_hints") or [])
                if cls._compact_debug_text(name, 72)
            ]
            source_kind = cls._compact_debug_text(item.get("source_kind") or "", 48)
            suffix_parts: List[str] = []
            if source_labels:
                suffix_parts.append("来源: " + "、".join(source_labels[:3]))
            if tool_names:
                suffix_parts.append("工具: " + "、".join(tool_names[:2]))
            if source_kind:
                suffix_parts.append("类型: " + source_kind)
            if provenance_hints:
                suffix_parts.append("线索: " + "、".join(provenance_hints[:2]))
            suffix = f"（{'；'.join(suffix_parts)}）" if suffix_parts else ""
            evidence_ledger.append(f"{summary}{suffix}")
        last_reasoning_summary = cls._compact_debug_text(state.get("last_reasoning_summary") or "", 180)
        decision_state = cls._normalize_decision_state(
            state.get("decision_state") or {},
            workflow_binding=workflow_binding,
        )

        if workflow_binding:
            skill_name = cls._compact_debug_text(workflow_binding.get("skill") or "", 64)
            paper_id = workflow_binding.get("paper_id")
            project_id = workflow_binding.get("project_id")
            workspace_id = workflow_binding.get("workspace_id")
            notebook_id = cls._compact_debug_text(workflow_binding.get("notebook_id") or "", 96)
            current_stage = cls._compact_debug_text(workflow_binding.get("current_stage") or "", 96)
            current_draft_id = cls._compact_debug_text(workflow_binding.get("current_draft_id") or "", 96)
            baseline_execution_id = cls._compact_debug_text(workflow_binding.get("baseline_execution_id") or "", 96)
            tuning_execution_id = cls._compact_debug_text(workflow_binding.get("tuning_execution_id") or "", 96)
            if skill_name:
                lines.append(f"- 当前 workflow: {skill_name}")
            binding_parts: List[str] = []
            if paper_id is not None:
                binding_parts.append(f"paper_id={paper_id}")
            if project_id is not None:
                binding_parts.append(f"project_id={project_id}")
            if workspace_id is not None:
                binding_parts.append(f"workspace_id={workspace_id}")
            if binding_parts:
                lines.append(f"- Workflow 绑定: {', '.join(binding_parts)}")
            if notebook_id:
                lines.append(f"- Notebook 绑定: {notebook_id}")
            if current_stage:
                lines.append(f"- 当前阶段绑定: {current_stage}")
            if current_draft_id:
                lines.append(f"- 当前 draft 绑定: {current_draft_id}")
            if baseline_execution_id:
                lines.append(f"- baseline_execution_id: {baseline_execution_id}")
            if tuning_execution_id:
                lines.append(f"- tuning_execution_id: {tuning_execution_id}")

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
        if decision_state:
            lines.append("- 当前决策提示（供参考，不是硬门禁）:")
            if decision_state.get("status"):
                lines.append(f"  - 状态: {decision_state.get('status')}")
            if decision_state.get("evidence_status"):
                lines.append(f"  - 证据充分度: {decision_state.get('evidence_status')}")
            if decision_state.get("next_action"):
                lines.append(f"  - 下一动作: {decision_state.get('next_action')}")
            if decision_state.get("blocked_reason"):
                lines.append(f"  - 阻塞原因: {decision_state.get('blocked_reason')}")
            if decision_state.get("allowed_actions"):
                lines.append(
                    "  - 允许动作: " + "、".join(
                        str(item)
                        for item in list(decision_state.get("allowed_actions") or [])[:4]
                    )
                )
            if decision_state.get("repo_edit_allowed") is not None:
                lines.append(
                    f"  - 允许直接修改 repo/source: {'是' if bool(decision_state.get('repo_edit_allowed')) else '否'}"
                )
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
            "use_contextual_compression": "contextual compression",
        }
        feature_lines = [
            f"- {label}: {'开启' if bool(overrides.get(key)) else '关闭'}"
            for key, label in feature_labels.items()
            if key in overrides and overrides.get(key) is not None
        ]
        rewrite_profile = str(overrides.get("query_rewrite_profile") or "").strip().lower()
        if rewrite_profile in {"off", "light", "deep"}:
            rewrite_labels = {
                "off": "关闭",
                "light": "轻量（仅同义扩展）",
                "deep": "深度（同义扩展 + HyDE + 子问题分解）",
            }
            feature_lines.append(f"- query rewrite: {rewrite_labels[rewrite_profile]}")
        elif "use_query_rewrite" in overrides and overrides.get("use_query_rewrite") is not None:
            feature_lines.append(
                f"- query rewrite: {'开启' if bool(overrides.get('use_query_rewrite')) else '关闭'}"
            )
        lines.extend(feature_lines)
        lines.append("- 本轮已显式开启 RAG：系统会先按以上范围和策略预取一轮 `knowledge_search` 证据并注入当前上下文。")
        lines.append("- 若预取证据仍不足，可继续调用 `knowledge_search`；后续检索也会继续继承以上临时约束。")
        return "\n".join(lines).strip()

    def _apply_tool_call_overrides(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        workflow_binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        effective_arguments = dict(arguments or {})
        normalized_binding = self._normalize_workflow_binding(workflow_binding or {})
        if normalized_binding and str(normalized_binding.get("skill") or "").strip() == self._PAPER_SKILL_NAME:
            project_id = normalized_binding.get("project_id")
            paper_id = normalized_binding.get("paper_id")
            if project_id is not None and "project_id" not in effective_arguments and (
                str(tool_name or "").startswith(self._PAPER_RESEARCH_TOOL_PREFIX)
                or str(tool_name or "").startswith("project_")
            ):
                effective_arguments["project_id"] = int(project_id)
            if paper_id is not None and "paper_id" not in effective_arguments and tool_name in {
                "paper_research_prepare",
                "paper_research_status",
            }:
                effective_arguments["paper_id"] = int(paper_id)
        if tool_name != "knowledge_search" or not self._agent_profile().include_rag_overrides:
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
            "use_contextual_compression",
        ):
            if key in overrides and overrides.get(key) is not None:
                effective_arguments[key] = bool(overrides.get(key))
        rewrite_profile = str(overrides.get("query_rewrite_profile") or "").strip().lower()
        if rewrite_profile in {"off", "light", "deep"}:
            effective_arguments["query_rewrite_profile"] = rewrite_profile
            effective_arguments["use_query_rewrite"] = rewrite_profile != "off"

        return effective_arguments

    def _should_force_initial_rag_retrieval(self, context: AgentContext) -> bool:
        if not self._agent_profile().include_rag_overrides:
            return False
        overrides = dict(context.active_rag_overrides or self._active_rag_overrides or {})
        if not overrides or not bool(overrides.get("enabled", False)):
            return False
        if list(context.prefetched_rag_messages or []) or int(context.prefetched_rag_search_count or 0) > 0:
            return False
        if int(context.knowledge_search_calls or 0) > 0:
            return False
        query = self._current_user_text(context)
        if not str(query or "").strip():
            return False
        get_tool = getattr(self.tools, "get", None)
        if callable(get_tool):
            try:
                return get_tool("knowledge_search") is not None
            except Exception:
                pass
        list_tools = getattr(self.tools, "list_tools", None)
        if callable(list_tools):
            try:
                raw_tools = list_tools()
            except TypeError:
                raw_tools = []
            except Exception:
                raw_tools = []
            for tool in list(raw_tools or []):
                if not isinstance(tool, dict):
                    continue
                function_payload = tool.get("function")
                if isinstance(function_payload, dict):
                    name = str(function_payload.get("name") or "").strip()
                else:
                    name = str(tool.get("name") or "").strip()
                if name == "knowledge_search":
                    return True
        return False

    def _build_forced_rag_tool_call(self, context: AgentContext) -> Optional[ParsedToolCall]:
        query = str(self._current_user_text(context) or "").strip()
        if not query:
            return None
        return ParsedToolCall(
            call_id=f"rag-bootstrap-{context.iteration or 1}",
            name="knowledge_search",
            arguments={"query": query},
            arguments_raw=json.dumps({"query": query}, ensure_ascii=False),
        )

    def _mark_forced_rag_search_debug(
        self,
        context: AgentContext,
        *,
        planned: bool,
        executed: bool,
    ) -> None:
        payload = dict(context.context_debug or {})
        payload["rag_force_initial_knowledge_search"] = bool(planned)
        payload["rag_force_initial_knowledge_search_executed"] = bool(executed)
        payload["rag_force_initial_query"] = self._compact_debug_text(self._current_user_text(context), 220)
        context.context_debug = payload

    @staticmethod
    def _build_prefetched_rag_message(observation_output: str) -> Dict[str, Any]:
        return {
            "role": "system",
            "content": (
                "本轮 RAG 预取证据：\n"
                f"{str(observation_output or '').strip()}\n\n"
                "请优先基于以上证据回答；若证据不足，可继续调用工具补充。"
            ).strip(),
        }

    def _mark_prefetched_rag_debug(
        self,
        context: AgentContext,
        *,
        query: str,
        succeeded: bool,
        result_count: int,
        source_labels: Sequence[str],
        reused_from_plan: bool = False,
        failed_reason: Optional[str] = None,
    ) -> None:
        payload = dict(context.context_debug or {})
        payload["rag_prefetch_enabled"] = True
        payload["rag_prefetch_succeeded"] = bool(succeeded)
        payload["rag_prefetch_reused_from_plan"] = bool(reused_from_plan)
        payload["rag_prefetch_query"] = self._compact_debug_text(query, 220)
        payload["rag_prefetch_result_count"] = int(max(0, result_count))
        payload["rag_prefetch_source_labels"] = [str(item) for item in list(source_labels or [])[:12] if str(item or "").strip()]
        if failed_reason:
            payload["rag_prefetch_failed_reason"] = self._compact_debug_text(failed_reason, 220)
        context.context_debug = payload

    def _hydrate_prefetched_rag_context(self, context: AgentContext) -> None:
        metadata = dict(context.prefetched_rag_metadata or {}) if isinstance(context.prefetched_rag_metadata, dict) else {}
        labels = [
            str(item)
            for item in list(metadata.get("source_labels") or [])
            if re.fullmatch(r"来源\d+", str(item or "").strip())
        ]
        if not labels:
            for message in list(context.prefetched_rag_messages or []):
                if not isinstance(message, dict):
                    continue
                labels.extend(f"来源{idx}" for idx in self._extract_source_labels(str(message.get("content", "") or "")))
        numeric_labels: set[str] = set()
        for label in labels:
            match = re.fullmatch(r"来源(\d+)", label)
            if match:
                numeric_labels.add(match.group(1))
        if numeric_labels:
            context.allowed_source_labels.update(numeric_labels)
            try:
                context.next_knowledge_source_label = max(int(item) for item in numeric_labels) + 1
            except Exception:
                pass
        source_items = list(metadata.get("source_items") or []) if isinstance(metadata.get("source_items"), list) else []
        if source_items:
            self._remember_source_items(context, source_items)
        if list(context.prefetched_rag_messages or []):
            context.prefetched_rag_search_count = max(int(context.prefetched_rag_search_count or 0), 1)
            self._mark_prefetched_rag_debug(
                context,
                query=str(metadata.get("query") or self._current_user_text(context) or ""),
                succeeded=True,
                result_count=int(metadata.get("result_count") or len(source_items) or 0),
                source_labels=labels,
                reused_from_plan=True,
            )

    async def _maybe_prefetch_rag_context(self, context: AgentContext) -> None:
        overrides = dict(context.active_rag_overrides or self._active_rag_overrides or {})
        if not overrides or not bool(overrides.get("enabled", False)):
            return
        if list(context.prefetched_rag_messages or []):
            self._hydrate_prefetched_rag_context(context)
            return
        query = str(self._current_user_text(context) or "").strip()
        if not query:
            return
        if not self._should_force_initial_rag_retrieval(context):
            return

        effective_arguments = self._apply_tool_call_overrides("knowledge_search", {"query": query})
        try:
            result = await self.tools.execute("knowledge_search", **effective_arguments)
        except Exception as exc:
            logger.warning(f"[AgentCore] rag prefetch knowledge_search failed: {exc}")
            self._mark_prefetched_rag_debug(
                context,
                query=query,
                succeeded=False,
                result_count=0,
                source_labels=[],
                failed_reason=str(exc),
            )
            return

        if not bool(getattr(result, "success", False)):
            self._mark_prefetched_rag_debug(
                context,
                query=query,
                succeeded=False,
                result_count=0,
                source_labels=[],
                failed_reason=str(getattr(result, "error", "") or "tool_failed"),
            )
            return

        observation_output = await self._compress_knowledge_observation(query, result, context=context)
        if not str(observation_output or "").strip():
            self._mark_prefetched_rag_debug(
                context,
                query=query,
                succeeded=False,
                result_count=0,
                source_labels=[],
                failed_reason="empty_observation",
            )
            return

        result_data = result.data if isinstance(result.data, dict) else {}
        result_metadata = self._normalize_tool_result_metadata(
            tool_name="knowledge_search",
            observation_output=observation_output,
            result_data=result_data,
        )
        source_items = list(result_metadata.get("source_items") or []) if isinstance(result_metadata.get("source_items"), list) else []
        self._remember_source_items(context, source_items)
        labels = [f"来源{idx}" for idx in sorted(self._extract_source_labels(observation_output), key=int)]
        context.allowed_source_labels.update(self._extract_source_labels(observation_output))
        context.prefetched_rag_messages = [self._build_prefetched_rag_message(observation_output)]
        context.prefetched_rag_metadata = {
            "query": query,
            "result_count": int(result_metadata.get("result_count") or len(source_items) or 0),
            "source_labels": labels,
            "retrieval_scope": dict(result_metadata.get("retrieval_scope") or {}) if isinstance(result_metadata.get("retrieval_scope"), dict) else {},
            "retrieval_runtime": dict(result_metadata.get("retrieval_runtime") or {}) if isinstance(result_metadata.get("retrieval_runtime"), dict) else {},
            "source_items": source_items,
        }
        context.prefetched_rag_search_count += 1
        self._mark_prefetched_rag_debug(
            context,
            query=query,
            succeeded=True,
            result_count=int(context.prefetched_rag_metadata.get("result_count") or 0),
            source_labels=labels,
        )

    @staticmethod
    def _normalize_compacted_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    async def _compress_text_with_qwen_turbo(
        text: str,
        *,
        target_token_budget: int,
        source: str,
        compression_kind: str,
    ) -> str:
        normalized = AgentCore._normalize_compacted_text(text)
        if not normalized:
            return ""

        provider = str(getattr(settings, "agent_budget_compression_provider", "aliyun") or "aliyun").strip()
        model_name = str(getattr(settings, "agent_budget_compression_model", "qwen-turbo") or "qwen-turbo").strip()
        output_budget = max(min(int(getattr(settings, "agent_budget_compression_max_tokens", 420) or 420), 1200), 96)
        output_budget = min(output_budget, max(int(target_token_budget or 0), 96))
        timeout_seconds = max(
            float(getattr(settings, "agent_budget_compression_timeout_seconds", 8.0) or 8.0),
            0.1,
        )

        current = normalized
        for attempt in range(2):
            try:
                llm = LLMService(provider)
                llm.config = dict(llm.config)
                llm.config["model"] = model_name
                response = await asyncio.wait_for(
                    llm.chat(
                        messages=[{"role": "user", "content": current}],
                        system_prompt=(
                            "你是上下文压缩器。"
                            f"当前任务是压缩{compression_kind}。"
                            "请保留任务目标、关键事实、路径、文件名、参数名、错误名、execution_id、tool 名称等可继续执行所必需的信息。"
                            "删除寒暄、重复描述、冗余细节。"
                            "不要使用 markdown，不要解释，不要加前言。"
                            f"请尽量把结果压到约 {max(int(target_token_budget or 0), 96)} token 以内。"
                        ),
                        temperature=0.1,
                        max_tokens=output_budget,
                        source=source,
                    ),
                    timeout=timeout_seconds,
                )
                compressed = AgentCore._normalize_compacted_text(response.get("content", ""))
            except asyncio.TimeoutError:
                logger.warning(
                    f"[AgentCore] qwen-turbo compression timed out source={source} timeout={timeout_seconds}"
                )
                compressed = ""
            except Exception as exc:
                logger.warning(f"[AgentCore] qwen-turbo compression failed source={source}: {exc}")
                compressed = ""

            if not compressed:
                break
            current = compressed
            if estimate_tokens(current) <= max(int(target_token_budget or 0), 0):
                return current

        if current and estimate_tokens(current) <= estimate_tokens(normalized):
            return current
        return f"[上下文压缩失败，请在需要时重新读取原始内容。kind={compression_kind}]"

    @classmethod
    def _summarize_messages(cls, messages: Sequence[Dict[str, Any]], max_lines: int = 8) -> str:
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
        return "\n".join(lines).strip()

    @classmethod
    async def _build_system_compression_message(
        cls,
        messages: Sequence[Dict[str, Any]],
        *,
        title: str,
        max_lines: int = 8,
    ) -> Optional[Dict[str, str]]:
        summary = cls._summarize_messages(messages, max_lines=max_lines).strip()
        if not summary:
            return None
        if estimate_tokens(summary) > max(160, max_lines * 40):
            summary = await cls._compress_text_with_qwen_turbo(
                summary,
                target_token_budget=max(160, max_lines * 40),
                source="chat.budget.message_summary",
                compression_kind="消息摘要",
            )
        return {"role": "system", "content": f"{title}：\n{summary}"}

    @staticmethod
    def _split_messages_preserving_recent_turns(
        messages: Sequence[Dict[str, Any]],
        *,
        preserve_recent_turns: int,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        if not rows:
            return [], []

        preserve_recent_turns = max(int(preserve_recent_turns or 0), 1)
        user_indices = [idx for idx, msg in enumerate(rows) if str(msg.get("role", "")).strip().lower() == "user"]
        if len(user_indices) <= preserve_recent_turns:
            return [], rows

        keep_start = user_indices[-preserve_recent_turns]
        return rows[:keep_start], rows[keep_start:]

    @staticmethod
    async def _truncate_message_content_to_token_budget(
        content: str,
        token_budget: int,
        *,
        role: str,
        kind: str,
    ) -> str:
        text = str(content or "")
        if not text:
            return text
        current_tokens = estimate_tokens(text)
        if current_tokens <= max(int(token_budget or 0), 0):
            return text

        marker = f"[system-compression-truncated role={role or 'unknown'} kind={kind or 'unknown'}]"
        marker_tokens = estimate_tokens(marker)
        target_budget = max(int(token_budget or 0), 96)
        compressed_budget = max(target_budget - marker_tokens - 8, 48)
        compressed = await AgentCore._compress_text_with_qwen_turbo(
            text,
            target_token_budget=compressed_budget,
            source="chat.budget.message_truncation",
            compression_kind=f"消息裁剪 role={role or 'unknown'} kind={kind or 'unknown'}",
        )
        if compressed:
            return f"{marker}\n{compressed}"
        return marker

    @classmethod
    async def _apply_content_truncation_until_budget(
        cls,
        messages: Sequence[Dict[str, Any]],
        *,
        budget: int,
    ) -> tuple[List[Dict[str, Any]], bool]:
        candidate = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        if not candidate:
            return candidate, False

        changed = False

        def _is_observation(msg: Dict[str, Any]) -> bool:
            role = str(msg.get("role", "")).strip().lower()
            return role == "tool" or "<observation>" in str(msg.get("content", "") or "")

        while cls._estimate_messages_tokens(candidate) > budget:
            last_user = max(
                [idx for idx, item in enumerate(candidate) if str(item.get("role", "")).strip().lower() == "user"] or [len(candidate) - 1]
            )
            truncatable: List[tuple[int, int]] = []
            for idx, item in enumerate(candidate):
                if idx == last_user:
                    continue
                role = str(item.get("role", "")).strip().lower()
                kind = cls._context_prefix_kind(item)
                if role not in {"assistant", "system", "tool"} and not _is_observation(item):
                    continue
                if role == "system" and kind not in {
                    "older_summary",
                    "persisted_summary",
                    "memory",
                    "rag_prefetch",
                    "system_compression",
                }:
                    continue
                content_tokens = estimate_tokens(str(item.get("content", "") or ""))
                if content_tokens <= 24:
                    continue
                truncatable.append((content_tokens, idx))

            if not truncatable:
                break

            truncatable.sort(reverse=True)
            _tokens, idx = truncatable[0]
            current_content = str(candidate[idx].get("content", "") or "")
            current_tokens = estimate_tokens(current_content)
            overshoot = max(cls._estimate_messages_tokens(candidate) - budget, 1)
            shrink_by = max(overshoot + 16, current_tokens // 3)
            target_budget = max(24, current_tokens - shrink_by)
            target_role = str(candidate[idx].get("role", "")).strip().lower()
            target_kind = cls._context_prefix_kind(candidate[idx])
            truncated = await cls._truncate_message_content_to_token_budget(
                current_content,
                target_budget,
                role=target_role,
                kind=target_kind,
            )
            if truncated == current_content:
                break
            candidate[idx]["content"] = truncated
            changed = True

        return candidate, changed

    def _build_system_prompt(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        *,
        function_calling: bool = False,
    ) -> str:
        profile = self._agent_profile()
        routing_decision = self._routing_decision_for_messages(messages)
        latest_user_text = self._latest_user_text(messages)
        skill_resolution = self._resolve_skills_for_messages(messages)
        tool_choice = "auto"
        tool_selection_enabled = False
        resolved_intent = "general_chat"
        selected_tool_names: List[str] = []

        select_tool_names_for_user_text = getattr(self.tools, "select_tool_names_for_user_text", None)
        resolve_intent = getattr(self.tools, "resolve_intent", None)
        select_tool_names_for_intent = getattr(self.tools, "select_tool_names_for_intent", None)
        channel = str(getattr(self.runtime_context, "channel", "") or "").strip().lower()
        disable_intent_filtering = bool(function_calling and channel == "chat")
        if bool(getattr(settings, "tool_selection_enabled", True)) and not disable_intent_filtering:
            if callable(select_tool_names_for_user_text):
                try:
                    raw_selected = select_tool_names_for_user_text(latest_user_text)
                except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                    logger.warning(f"[AgentCore] select_tool_names_for_user_text failed, fallback to available tools: {exc}")
                else:
                    selected_tool_names = [
                        str(item).strip()
                        for item in list(raw_selected or [])
                        if str(item or "").strip()
                    ]
                    tool_selection_enabled = True
                    if callable(resolve_intent):
                        try:
                            maybe_intent = str(resolve_intent(latest_user_text) or "").strip()
                        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                            logger.warning(f"[AgentCore] resolve_intent failed after user-text selection: {exc}")
                        else:
                            if maybe_intent:
                                resolved_intent = maybe_intent
            elif callable(resolve_intent) and callable(select_tool_names_for_intent):
                try:
                    maybe_intent = str(resolve_intent(latest_user_text) or "").strip()
                except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                    logger.warning(f"[AgentCore] resolve_intent failed, fallback to available tools: {exc}")
                else:
                    if maybe_intent:
                        resolved_intent = maybe_intent
                    try:
                        raw_selected = select_tool_names_for_intent(resolved_intent, user_text=latest_user_text)
                    except TypeError:
                        raw_selected = select_tool_names_for_intent(resolved_intent)
                    except (RuntimeError, ValueError, AttributeError) as exc:
                        logger.warning(f"[AgentCore] select_tool_names_for_intent failed, fallback to available tools: {exc}")
                    else:
                        selected_tool_names = [
                            str(item).strip()
                            for item in list(raw_selected or [])
                            if str(item or "").strip()
                        ]
                        tool_selection_enabled = True

        skill_enforced_tool_names = [
            str(item).strip()
            for item in list(skill_resolution.get("enforced_tool_names") or [])
            if str(item or "").strip()
        ]
        skill_blocked_tool_names = {
            str(item).strip()
            for item in list(skill_resolution.get("blocked_tool_names") or [])
            if str(item or "").strip()
        }
        skill_blocked_tool_names.update(self._hidden_tool_names_for_active_skills(skill_resolution))
        if skill_enforced_tool_names:
            selected_tool_names = [
                name for name in skill_enforced_tool_names
                if name not in skill_blocked_tool_names
            ]
            tool_selection_enabled = True
        elif skill_blocked_tool_names:
            if selected_tool_names:
                selected_tool_names = [
                    name for name in selected_tool_names
                    if name not in skill_blocked_tool_names
                ]
            else:
                list_tools = getattr(self.tools, "list_tools", None)
                if callable(list_tools):
                    try:
                        raw_tools = list_tools()
                    except TypeError:
                        raw_tools = []
                    else:
                        selected_tool_names = []
                        for tool in list(raw_tools or []):
                            if not isinstance(tool, dict):
                                continue
                            function_payload = tool.get("function")
                            if isinstance(function_payload, dict):
                                name = str(function_payload.get("name") or "").strip()
                            else:
                                name = str(tool.get("name") or "").strip()
                            if name and name not in skill_blocked_tool_names:
                                selected_tool_names.append(name)
            tool_selection_enabled = True
        if channel == "chat" and selected_tool_names and "activate_skill" not in selected_tool_names:
            selected_tool_names.append("activate_skill")

        tools_desc = ""
        if not function_calling:
            get_tools_description = getattr(self.tools, "get_tools_description", None)
            if callable(get_tools_description):
                try:
                    if selected_tool_names:
                        tools_desc = get_tools_description(include_tool_names=set(selected_tool_names))
                    else:
                        tools_desc = get_tools_description()
                except TypeError:
                    tools_desc = get_tools_description()
        available_tools: List[str] = []
        list_tools = getattr(self.tools, "list_tools", None)
        if callable(list_tools):
            try:
                if selected_tool_names:
                    raw_tools = list_tools(include_tool_names=set(selected_tool_names))
                else:
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
        channel_policy_prompt = (
            self._channel_tool_policy_prompt(available_tools)
            if profile.include_channel_tool_policy
            else ""
        )
        desc_tokens = estimate_tokens(tools_desc)
        self._last_tool_selection = {
            "intent": resolved_intent,
            "intent_user_text": latest_user_text,
            "selected_tools": available_tools if available_tools else selected_tool_names,
            "prompt_desc_tokens": desc_tokens,
            "schema_scope": "selected" if selected_tool_names else "available",
            "tool_selection_enabled": tool_selection_enabled,
            "tool_choice": tool_choice,
            "routing_source": routing_decision.source if routing_decision else "default_agent",
            "routing_reason": routing_decision.reason if routing_decision else "",
            "routing_confidence": routing_decision.confidence if routing_decision else 0.0,
            "carry_over_previous_goal": routing_decision.carry_over_previous_goal if routing_decision else False,
            "router_needs_tools": routing_decision.needs_tools if routing_decision else None,
            "skill_enforced_tools": selected_tool_names if skill_enforced_tool_names else [],
            "skill_blocked_tools": sorted(skill_blocked_tool_names),
            "active_skill_names": [
                str(item.get("name") or "").strip()
                for item in list(skill_resolution.get("active_skills") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ],
        }
        logger.info(
            f"[AgentCore] selected_tools={self._last_tool_selection.get('selected_tools') or 'ALL'} "
            f"schema_scope={self._last_tool_selection.get('schema_scope')} "
            f"tool_choice={tool_choice} prompt_desc_tokens={desc_tokens} "
            f"routing_source={self._last_tool_selection.get('routing_source')}"
        )
        if function_calling:
            prompt = self.FUNCTION_CALLING_SYSTEM_PROMPT.strip()
        else:
            prompt = self.SYSTEM_PROMPT.format(tools_description=tools_desc)
        return self._compose_profile_prompt_sections(
            prompt,
            available_tools=available_tools,
            include_generic_citation_policy=profile.include_generic_citation_policy,
            channel_policy_prompt=channel_policy_prompt,
            active_skill_system_prompt=str(skill_resolution.get("active_system_prompt") or ""),
            active_skill_prompt=str(skill_resolution.get("active_prompt") or ""),
            skill_catalog_prompt=self._render_skill_catalog(skill_resolution),
        )

    def _compose_profile_prompt_sections(
        self,
        prompt: str,
        *,
        available_tools: Optional[Sequence[str]] = None,
        include_generic_citation_policy: bool = True,
        channel_policy_prompt: Optional[str] = None,
        active_skill_system_prompt: Optional[str] = None,
        active_skill_prompt: Optional[str] = None,
        skill_catalog_prompt: Optional[str] = None,
    ) -> str:
        profile = self._agent_profile()
        composed = str(prompt or "").strip()
        if include_generic_citation_policy:
            composed = f"{composed}\n\n{self.CITATION_POLICY_PROMPT}"
        project_tool_policy_prompt = self._project_tool_policy_prompt(list(available_tools or []))
        if project_tool_policy_prompt:
            composed = f"{composed}\n\n{project_tool_policy_prompt}"
        if profile.include_channel_system_context:
            channel_system_context = str(getattr(self, "_active_channel_system_context", "") or "").strip()
            if channel_system_context:
                composed = f"{composed}\n\n## CodeLab / Notebook Runtime Context\n{channel_system_context}"
        if active_skill_system_prompt:
            composed = f"{composed}\n\n## Skill Session System Prompt\n{str(active_skill_system_prompt).strip()}"
        if skill_catalog_prompt:
            composed = f"{composed}\n\n{str(skill_catalog_prompt).strip()}"
        if active_skill_prompt:
            composed = f"{composed}\n\n## 已激活 Skills\n{str(active_skill_prompt).strip()}"
        if profile.include_user_chat_preferences:
            user_pref_prompt = self._render_user_chat_preferences(self._active_chat_preferences)
            if user_pref_prompt:
                composed = f"{composed}\n\n## 用户已确认的聊天偏好\n{user_pref_prompt}"
        if profile.include_rag_overrides:
            rag_prompt = self._render_rag_overrides_prompt(self._active_rag_overrides)
            if rag_prompt:
                composed = f"{composed}\n\n## 本轮临时 RAG 注入\n{rag_prompt}"
        policy_prompt = channel_policy_prompt
        if policy_prompt is None and profile.include_channel_tool_policy:
            policy_prompt = self._channel_tool_policy_prompt(list(available_tools or []))
        if policy_prompt:
            composed = f"{composed}\n\n{policy_prompt}"
        return composed

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
        has_knowledge = any(
            cls._citation_tool_name(item.tool_name, item.metadata) == "knowledge_search"
            or bool(cls._extract_source_labels(item.observation_output))
            for item in observations
        )
        has_web = any(
            cls._citation_tool_name(item.tool_name, item.metadata) == "web_search"
            or bool(cls._extract_web_source_labels(item.observation_output))
            for item in observations
        )
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
        used_labels = cls._extract_citation_tokens_in_order(context.final_answer or "")
        available_labels = (
            [f"来源{idx}" for idx in sorted(context.allowed_source_labels, key=int)]
            + [f"网页{idx}" for idx in sorted(context.allowed_web_source_labels, key=int)]
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
            "prefetched_knowledge_search_count": int(max(0, context.prefetched_rag_search_count)),
            "web_search_calls": context.web_search_calls,
            "source_labels_count": len(used_labels),
            "source_labels": used_labels,
            "available_source_labels_count": len(available_labels),
            "available_source_labels": available_labels,
            "answer_citation_count": len(cited),
            "citation_required": citation_required,
            "citation_valid": citation_valid,
            "citation_repair_attempts": context.citation_repair_attempts,
            "citation_repair_successes": context.citation_repair_successes,
            "compression_calls": context.compression_calls,
            "compression_success_chunks": context.compression_success_chunks,
            "compression_fallback_chunks": context.compression_fallback_chunks,
        }

    @classmethod
    def _strip_unsupported_citation_tokens(
        cls,
        answer: str,
        *,
        allowed_source_labels: Optional[set[str]] = None,
        allowed_web_source_labels: Optional[set[str]] = None,
    ) -> str:
        clean = str(answer or "").strip()
        if not clean:
            return ""
        allowed = cls._allowed_citation_tokens(allowed_source_labels or set(), allowed_web_source_labels or set())

        def _replace(match: re.Match[str]) -> str:
            token = str(match.group(1) or "").strip()
            return match.group(0) if token in allowed else ""

        stripped = re.sub(r"\[(来源\d+|网页\d+)\]", _replace, clean)
        stripped = re.sub(r"[ \t]{2,}", " ", stripped)
        stripped = re.sub(r"\s+([，。；：！？,.;!?])", r"\1", stripped)
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
        return stripped.strip()

    @classmethod
    def _seed_allowed_citations_from_messages(cls, context: AgentContext) -> None:
        for item in list(context.messages or []):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            if role not in {"assistant", "tool", "system"}:
                continue
            content = str(item.get("content", "") or "")
            context.allowed_source_labels.update(cls._extract_source_labels(content))
            context.allowed_web_source_labels.update(cls._extract_web_source_labels(content))

    async def _ensure_citation_compliance(self, answer: str, context: AgentContext) -> str:
        clean = (answer or "").strip()
        allowed = self._allowed_citation_tokens(
            context.allowed_source_labels,
            context.allowed_web_source_labels,
        )
        if not clean:
            return clean
        if not allowed:
            return self._strip_unsupported_citation_tokens(
                clean,
                allowed_source_labels=context.allowed_source_labels,
                allowed_web_source_labels=context.allowed_web_source_labels,
            )
        if self._citations_are_valid(
            clean,
            context.allowed_source_labels,
            context.allowed_web_source_labels,
        ):
            return clean
        context.citation_repair_attempts += 1
        allowed_tokens = ", ".join(f"[{token}]" for token in sorted(allowed))
        try:
            timeout_seconds = max(
                float(getattr(settings, "agent_citation_repair_timeout_seconds", 8.0) or 8.0),
                0.1,
            )
            resp = await asyncio.wait_for(
                self.llm.chat(
                    messages=[{"role": "user", "content": f"只修正来源标注，只能使用：{allowed_tokens}\n\n{clean}"}],
                    system_prompt="你是引用修正助手。",
                    temperature=0.0,
                    max_tokens=min(settings.llm_max_tokens, 1000),
                ),
                timeout=timeout_seconds,
            )
            fixed = re.sub(r"</?answer>", "", str(resp.get("content") or "")).strip()
            if fixed and self._citations_are_valid(
                fixed,
                context.allowed_source_labels,
                context.allowed_web_source_labels,
            ):
                context.citation_repair_successes += 1
                return fixed
        except asyncio.TimeoutError:
            logger.warning("[AgentCore] citation repair timed out")
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

    @staticmethod
    def _extract_hostname(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        parsed = urlparse(raw)
        host = str(parsed.netloc or parsed.path or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host

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
        valid_rows = self._citation_result_rows(tool_name="web_search", result_data=data)
        if not valid_rows:
            return result.output

        source_kind = str(data.get("source_kind") or "").strip().lower()
        page_url = str(data.get("url") or "").strip()
        page_content = str(
            data.get("markdown")
            or data.get("text")
            or data.get("content")
            or data.get("reader_summary")
            or ""
        ).strip()
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
            embedded_urls = [
                str(item or "").strip()
                for item in list(row.get("embedded_urls") or [])
                if str(item or "").strip()
            ]
            candidate_download_urls = [
                str(item or "").strip()
                for item in list(row.get("candidate_download_urls") or [])
                if str(item or "").strip()
            ]
            is_primary_page = bool(
                source_kind == "public_web_page"
                and page_url
                and url
                and url == page_url
            )
            if source_kind == "public_web_page" and is_primary_page:
                body = snippet or page_content or "No page content available."
                section = (
                    f"\n[{source_label}] {title or 'Public web page'}\n"
                    f"Domain: {domain or 'unknown'}\n"
                    f"URL: {url or 'N/A'}\n"
                    f"Content:\n{body}"
                )
            else:
                section = (
                    f"\n[{source_label}] {title or 'Public web result'}\n"
                    f"Domain: {domain or 'unknown'}\n"
                    f"URL: {url or 'N/A'}\n"
                    f"Snippet: {snippet or 'No content available.'}"
                )
            if candidate_download_urls:
                section += f"\nDirect candidate URLs: {', '.join(candidate_download_urls[:3])}"
            elif embedded_urls:
                section += f"\nEmbedded URLs: {', '.join(embedded_urls[:3])}"
            parts.append(section)
        if context is not None:
            context.next_web_source_label = base_source_id + len(valid_rows)
        return f"Public web contexts: {len(parts)}\n" + "".join(parts)

    async def _prepare_runtime_context(self, context: AgentContext) -> None:
        profile = self._agent_profile()
        skill_resolution = self._resolve_skills_for_messages(context.messages)
        skill_enforced_tool_names = [
            str(item).strip()
            for item in list(skill_resolution.get("enforced_tool_names") or [])
            if str(item or "").strip()
        ]
        should_refresh_mcp_tools = True
        if skill_enforced_tool_names:
            should_refresh_mcp_tools = any(name.startswith("mcp.") for name in skill_enforced_tool_names)
        refresh_mcp_tools = getattr(self.tools, "refresh_mcp_tools", None)
        if should_refresh_mcp_tools and callable(refresh_mcp_tools):
            try:
                maybe_awaitable = refresh_mcp_tools()
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable
            except Exception as exc:
                logger.warning(f"[AgentCore] MCP tool refresh failed, continue with local tools: {exc}")

        user_text = self._latest_user_text(context.messages)

        if self.runtime_context.user_id:
            try:
                memory_control = await self.runtime_service.get_user_memory_control(
                    user_id=self.runtime_context.user_id,
                    channel=self.runtime_context.channel,
                )
                context.memory_enabled = bool(memory_control.get("effective_enabled", False))
            except Exception as exc:
                context.memory_enabled = False
                logger.warning(f"[AgentCore] load memory control failed: {exc}")

            conversation_artifact_id = self._conversation_artifact_conversation_id()
            if conversation_artifact_id is not None:
                try:
                    latest_state = await self.runtime_service.get_conversation_context_state(
                        conversation_artifact_id
                    )
                    if isinstance(latest_state, dict):
                        context.conversation_state = latest_state
                    latest_compacted_history = await self.runtime_service.get_conversation_compacted_history(
                        conversation_artifact_id
                    )
                    if isinstance(latest_compacted_history, dict):
                        context.compacted_history = latest_compacted_history
                        persisted_summary = str(latest_compacted_history.get("history_summary") or "").strip()
                        if persisted_summary:
                            context.context_summary = persisted_summary
                    item_stream = await self.runtime_service.get_conversation_item_stream(
                        conversation_artifact_id
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
                        context.history_messages = self._active_history_messages_from_item_stream(
                            list(item_stream.get("entries") or []),
                            fallback_boundary_message_id=normalized_boundary_message_id,
                        )
                except Exception as exc:
                    logger.warning(f"[AgentCore] load conversation context artifacts failed: {exc}")

        context.messages = self._merge_history_messages(context.history_messages, context.messages)
        self._seed_allowed_citations_from_messages(context)

        if profile.include_user_chat_preferences and self.runtime_context.user_id:
            try:
                context.user_chat_preferences = await self.runtime_service.get_user_chat_preferences(
                    user_id=self.runtime_context.user_id
                )
            except Exception as exc:
                logger.warning(f"[AgentCore] load user chat preferences failed: {exc}")
                context.user_chat_preferences = {}
        else:
            context.user_chat_preferences = {}
        overrides = dict(self.runtime_context.chat_preferences_override or {})
        if profile.include_user_chat_preferences and overrides:
            context.user_chat_preferences = self.runtime_service.merge_chat_preferences(
                context.user_chat_preferences,
                overrides,
            )
        self._active_chat_preferences = dict(context.user_chat_preferences or {})
        normalize_rag_overrides = getattr(self.runtime_service, "normalize_chat_rag_overrides", None)
        if profile.include_rag_overrides and callable(normalize_rag_overrides):
            context.active_rag_overrides = normalize_rag_overrides(self.runtime_context.rag_overrides)
        elif profile.include_rag_overrides:
            context.active_rag_overrides = dict(self.runtime_context.rag_overrides or {})
        else:
            context.active_rag_overrides = {}
        self._active_rag_overrides = dict(context.active_rag_overrides or {})
        if profile.include_rag_overrides and list(context.prefetched_rag_messages or []):
            self._hydrate_prefetched_rag_context(context)
        elif profile.include_rag_overrides:
            await self._maybe_prefetch_rag_context(context)
        else:
            context.prefetched_rag_messages = []
            context.prefetched_rag_metadata = {}
            context.prefetched_rag_search_count = 0

        if context.memory_enabled and self.runtime_context.user_id:
            try:
                scope_type, scope_id = self._memory_scope()
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
        scope_type, scope_id = self._memory_scope()
        try:
            context.run_id = await self.runtime_service.create_run(
                user_id=self.runtime_context.user_id,
                channel=self.runtime_context.channel,
                conversation_id=self._run_binding_conversation_id(),
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
                    "scope_type": scope_type,
                    "scope_id": scope_id,
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

        summary_text = await self.generate_reasoning_summary_from_trace(trace)
        if not summary_text:
            return ""
        context.context_debug["reasoning_summary"] = summary_text
        context.context_debug["reasoning_summary_model"] = str(
            getattr(settings, "agent_reasoning_summary_model", "qwen3.5-flash") or "qwen3.5-flash"
        ).strip()
        context.context_debug["reasoning_summary_provider"] = str(
            getattr(settings, "agent_reasoning_summary_provider", "aliyun") or "aliyun"
        ).strip()
        context.reasoning_summary = summary_text
        return summary_text

    @staticmethod
    async def generate_reasoning_summary_from_trace(trace: str) -> str:
        trace_text = str(trace or "").strip()
        if not trace_text:
            return ""

        provider = str(getattr(settings, "agent_reasoning_summary_provider", "aliyun") or "aliyun").strip()
        model_name = str(getattr(settings, "agent_reasoning_summary_model", "qwen3.5-flash") or "qwen3.5-flash").strip()
        max_tokens = max(int(getattr(settings, "agent_reasoning_summary_max_tokens", 220) or 220), 64)

        try:
            summary_llm = LLMService(provider)
            summary_llm.config = dict(summary_llm.config)
            summary_llm.config["model"] = model_name
            response = await summary_llm.chat(
                messages=[{"role": "user", "content": trace_text}],
                system_prompt=(
                    "你是一个推理过程压缩器。请把给定的多轮推理、工具使用与最终回答压缩成 1 到 3 句中文总结。"
                    "只保留回答策略、关键证据或工具、是否仍有未解问题。"
                    "不要复述整段最终答案，不要输出项目符号，不要解释任务本身，不要超过 120 个汉字。"
                    "直接输出摘要正文。"
                ),
                temperature=0.2,
                max_tokens=max_tokens,
                source="chat.reasoning_summary",
            )
            summary_text = AgentCore._compact_debug_text(response.get("content", ""), 160).strip()
            return summary_text
        except Exception as exc:
            logger.warning(f"[AgentCore] reasoning summary failed: {exc}")
            return ""

    @staticmethod
    def _compact_debug_text(value: Any, limit: int = 180) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

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
        budget_state: Optional[Dict[str, Any]] = None,
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
        effective_budget_state = dict(budget_state or {})
        skill_resolution = dict(self._last_skill_resolution or {})
        available_skills = [
            dict(item)
            for item in list(skill_resolution.get("available_skills") or [])
            if isinstance(item, dict)
        ]
        active_skills = [
            dict(item)
            for item in list(skill_resolution.get("active_skills") or [])
            if isinstance(item, dict)
        ]

        payload = {
            "version": "chat_context_debug.v1",
            "iteration": int(max(0, context.iteration)),
            "context_truncated": bool(context.context_truncated),
            "estimated_tokens": int(max(0, estimated_tokens)),
            "budget": int(max(0, budget)),
            "effective_budget": int(max(0, effective_budget_state.get("effective_budget") or budget)),
            "budget_mode": str(effective_budget_state.get("budget_mode") or "system_cap"),
            "model_context_window": effective_budget_state.get("model_context_window"),
            "system_budget_cap": int(max(0, effective_budget_state.get("system_budget_cap") or budget)),
            "model_budget_before_cap": effective_budget_state.get("model_budget_before_cap"),
            "budget_reserve_tokens": int(max(0, effective_budget_state.get("budget_reserve_tokens") or 0)),
            "configured_budget_reserve_tokens": int(max(0, effective_budget_state.get("configured_budget_reserve_tokens") or 0)),
            "completion_reserve_tokens": int(max(0, effective_budget_state.get("completion_reserve_tokens") or 0)),
            "system_prompt_tokens": int(max(0, effective_budget_state.get("system_prompt_tokens") or 0)),
            "tool_schema_tokens_estimate": int(max(0, effective_budget_state.get("tool_schema_tokens_estimate") or 0)),
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
            "skill_enforced_tools": [str(item) for item in (selection.get("skill_enforced_tools") or []) if str(item or "").strip()],
            "skill_blocked_tools": [str(item) for item in (selection.get("skill_blocked_tools") or []) if str(item or "").strip()],
            "active_skill_names": [str(item) for item in (selection.get("active_skill_names") or []) if str(item or "").strip()],
            "tool_choice": str(selection.get("tool_choice") or "auto"),
            "available_skills": available_skills,
            "active_skills": active_skills,
            "skill_prompt_tokens_estimate": int(max(0, skill_resolution.get("active_prompt_tokens") or 0)),
            "skill_system_prompt_tokens_estimate": int(max(0, skill_resolution.get("active_system_prompt_tokens") or 0)),
            "conversation_state": conversation_state,
            "conversation_state_summary": self._compact_debug_text(conversation_state_summary, 600),
            "anchor_summary": self._compact_debug_text(anchor_summary, 600),
            "persisted_anchor_summary": self._compact_debug_text(persisted_anchor_summary, 600),
            "persisted_summary": self._compact_debug_text(persisted_summary, 600),
            "older_history_summary": self._compact_debug_text(older_summary, 600),
            "compact_boundary_message_id": compacted_history.get("compact_boundary_message_id"),
            "replacement_history_count": int(len(replacement_history)),
            "system_compression_message_count": int(
                sum(1 for item in llm_messages if self._context_prefix_kind(item) == "system_compression")
            ),
            "mid_run_compactions": int(max(0, context.mid_run_compactions)),
            "stable_prefix_cache_hits": int(max(0, context.stable_prefix_cache_hits)),
            "stable_prefix_cache_misses": int(max(0, context.stable_prefix_cache_misses)),
            "stable_prefix_cache_active": bool(context.stable_prefix_cache_messages),
            "memory_enabled": bool(context.memory_enabled),
            "memory_count": int(len(context.memory_contexts or [])),
            "memory_lines": memory_lines,
            "user_chat_preferences": user_chat_preferences,
            "rag_overrides": active_rag_overrides,
            "prefetched_rag_search_count": int(max(0, context.prefetched_rag_search_count)),
            "prefetched_rag_message_count": int(len(context.prefetched_rag_messages or [])),
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
        if content.startswith("本轮 RAG 预取证据："):
            return "rag_prefetch"
        if content.startswith("关键历史锚点："):
            return "anchor"
        if content.startswith("持久历史锚点："):
            return "persisted_anchor"
        if content.startswith("更早历史摘要："):
            return "older_summary"
        if content.startswith("历史摘要："):
            return "persisted_summary"
        if content.startswith("更早历史系统压缩：") or content.startswith("滑出窗口历史系统压缩：") or content.startswith("近期历史系统压缩：") or content.startswith("临近上下文历史系统压缩："):
            return "system_compression"
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
        if history_messages:
            return history_messages
        for entry in list(entries or []):
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or "").strip().lower()
            if kind not in {"reasoning_summary", "tool_use_summary"}:
                continue
            summary = str(entry.get("summary") or "").strip()
            if not summary:
                continue
            history_messages.append(
                cls._sanitize_message_for_context(
                    {
                        "role": str(entry.get("role") or "assistant").strip().lower() or "assistant",
                        "content": "",
                        "thought": summary,
                        "metadata": dict(entry.get("metadata") or {}) if isinstance(entry.get("metadata"), dict) else {},
                    }
                )
            )
        return history_messages

    @classmethod
    def _active_history_messages_from_item_stream(
        cls,
        entries: Sequence[Dict[str, Any]],
        *,
        fallback_boundary_message_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        store = ConversationItemStreamStore.from_payload(
            {"version": "conversation_item_stream.v1", "entries": list(entries or [])}
        )
        replay_rows = store.canonical_active_message_rows(
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
        budget_state = self._build_budget_state(
            user_text=self._current_user_text(context),
            system_prompt=system_prompt,
        )
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
                budget=int(
                    budget_state.get("effective_budget")
                    or self._resolve_system_budget_cap(model_context_window=self._current_model_context_window())
                ),
                budget_state=budget_state,
                window_turns=max(int(getattr(settings, "agent_context_window_turns", 8)), 1),
                total_messages=len(sanitized),
                older_messages_count=0,
                recent_messages_count=len(sanitized),
            )
            return sanitized

        window_turns = max(int(getattr(settings, "agent_context_window_turns", 8)), 1)
        raw_recently_slid_turns = getattr(settings, "agent_context_recently_slid_turns", 2)
        recently_slid_turns = max(
            int(raw_recently_slid_turns if raw_recently_slid_turns is not None else 2),
            0,
        )
        older, recently_slid, recent = self._split_context_windows(
            history_source,
            recent_turns=window_turns,
            recently_slid_turns=recently_slid_turns,
        )

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
        memory_prompt = self._memory_prompt(context.memory_contexts) if context.memory_contexts else ""
        prefixes = self._build_stable_prefix_messages(
            context=context,
            conversation_state_prompt=conversation_state_prompt,
            persisted_anchor_summary=persisted_anchor_summary,
            persisted_summary=persisted_summary,
            replacement_history_entries=replacement_history_entries,
            memory_prompt=memory_prompt,
        )
        summary_trigger_tokens = max(int(getattr(settings, "agent_context_summary_trigger_tokens", 7000) or 7000), 0)
        preserve_recent_turns = max(int(getattr(settings, "agent_context_preserve_recent_turns", 2) or 2), 1)
        overflow_compression_messages: List[Dict[str, Any]] = []
        older_summary_parts: List[str] = []
        raw_recently_slid = [dict(item) for item in list(recently_slid or []) if isinstance(item, dict)]
        raw_recent = [dict(item) for item in list(recent or []) if isinstance(item, dict)]
        prefetched_rag_messages = [
            dict(item) for item in list(context.prefetched_rag_messages or []) if isinstance(item, dict)
        ]

        if older:
            compressed_older = await self._build_system_compression_message(
                older,
                title="更早历史系统压缩",
                max_lines=10,
            )
            if compressed_older:
                overflow_compression_messages.append(compressed_older)
                older_summary = self._summarize_messages(older, max_lines=10)
                if older_summary:
                    older_summary_parts.append(older_summary)
                    context.context_summary = older_summary
                context.context_truncated = True

        def build_candidate(*, opportunistic_summary: str = "") -> List[Dict[str, Any]]:
            dynamic_prefixes = [dict(item) for item in prefixes]
            if opportunistic_summary:
                dynamic_prefixes.append({"role": "system", "content": f"更早历史摘要：\n{opportunistic_summary}"})
            return (
                dynamic_prefixes
                + replacement_history_entries
                + overflow_compression_messages
                + prefetched_rag_messages
                + raw_recently_slid
                + raw_recent
                + ephemeral_messages
            )

        candidate = build_candidate()
        context.message_tokens_before_trim = self._estimate_messages_tokens(candidate)
        budget = max(int(budget_state.get("effective_budget") or 0), 256)

        if self._estimate_messages_tokens(candidate) > budget and raw_recently_slid:
            compressed_slid = await self._build_system_compression_message(
                raw_recently_slid,
                title="滑出窗口历史系统压缩",
                max_lines=8,
            )
            if compressed_slid:
                overflow_compression_messages.append(compressed_slid)
                slid_summary = self._summarize_messages(raw_recently_slid, max_lines=8)
                if slid_summary:
                    older_summary_parts.append(slid_summary)
                raw_recently_slid = []
                context.context_truncated = True
                candidate = build_candidate()

        if self._estimate_messages_tokens(candidate) > budget and raw_recent:
            compactable_recent, preserved_recent = self._split_messages_preserving_recent_turns(
                raw_recent,
                preserve_recent_turns=preserve_recent_turns,
            )
            if compactable_recent:
                compressed_recent = await self._build_system_compression_message(
                    compactable_recent,
                    title="近期历史系统压缩",
                    max_lines=8,
                )
                if compressed_recent:
                    overflow_compression_messages.append(compressed_recent)
                    recent_summary = self._summarize_messages(compactable_recent, max_lines=8)
                    if recent_summary:
                        older_summary_parts.append(recent_summary)
                    raw_recent = preserved_recent
                    context.context_truncated = True
                    candidate = build_candidate()

        if self._estimate_messages_tokens(candidate) > budget and raw_recent:
            compactable_recent, preserved_recent = self._split_messages_preserving_recent_turns(
                raw_recent,
                preserve_recent_turns=1,
            )
            if compactable_recent:
                compressed_recent = await self._build_system_compression_message(
                    compactable_recent,
                    title="临近上下文历史系统压缩",
                    max_lines=6,
                )
                if compressed_recent:
                    overflow_compression_messages.append(compressed_recent)
                    recent_summary = self._summarize_messages(compactable_recent, max_lines=6)
                    if recent_summary:
                        older_summary_parts.append(recent_summary)
                    raw_recent = preserved_recent
                    context.context_truncated = True
                    candidate = build_candidate()

        opportunistic_summary = ""
        if (
            not older
            and raw_recently_slid
            and not replacement_history_entries
            and self._estimate_messages_tokens(history_source + ephemeral_messages) >= summary_trigger_tokens
        ):
            opportunistic_summary = self._summarize_messages(raw_recently_slid, max_lines=10)
            if opportunistic_summary:
                older_summary_parts.append(opportunistic_summary)

        candidate = build_candidate(opportunistic_summary=opportunistic_summary)
        candidate, content_truncated = await self._apply_content_truncation_until_budget(candidate, budget=budget)
        if content_truncated:
            context.context_truncated = True

        older_summary = "\n".join(part for part in older_summary_parts if str(part or "").strip())

        estimated_tokens = self._estimate_messages_tokens(candidate)
        if estimated_tokens > budget:
            context.context_truncated = True
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
            budget_state=budget_state,
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
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    def _current_compaction_boundary_message_id(self, context: AgentContext) -> Optional[int]:
        if not isinstance(context.compacted_history, dict):
            return None
        raw_boundary = context.compacted_history.get("compact_boundary_message_id")
        try:
            return int(raw_boundary) if raw_boundary is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    async def _gather_runtime_compaction_inputs(self, context: AgentContext) -> Dict[str, Any]:
        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            raise RuntimeError("conversation_id is required for runtime compaction")

        from app.services.conversation_context_compaction_service import (
            ConversationContextCompactionService,
            ConversationItemStreamUnavailableError,
        )

        item_stream_payload = ConversationContextCompactionService._require_item_stream_payload(
            int(conversation_id),
            await self.runtime_service.get_conversation_item_stream(int(conversation_id)),
        )
        store = ConversationItemStreamStore.from_payload(item_stream_payload)
        fallback_boundary_message_id = self._current_compaction_boundary_message_id(context)
        canonical = store.canonical_history(
            fallback_boundary_message_id=fallback_boundary_message_id,
        )
        active_entries = [entry.__dict__ for entry in canonical.active_entries]
        payload_rows = [
            self._sanitize_message_for_context(item)
            for item in list(context.messages or [])
            if isinstance(item, dict)
        ]
        canonical_rows = store.canonical_replay_rows(
            fallback_boundary_message_id=fallback_boundary_message_id,
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
        return {
            "conversation_id": int(conversation_id),
            "item_stream_payload": item_stream_payload,
            "store": store,
            "canonical": canonical,
            "active_entries": active_entries,
            "payload_rows": payload_rows,
            "canonical_rows": canonical_rows,
            "tool_rows": tool_rows,
            "latest_message_id": latest_message_id,
            "conversation_item_stream_unavailable_error": ConversationItemStreamUnavailableError,
        }

    async def _resolve_runtime_compaction_artifacts(
        self,
        *,
        payload_rows: Sequence[Dict[str, Any]],
        canonical_rows: Sequence[Dict[str, Any]],
        tool_rows: Sequence[Dict[str, Any]],
        latest_message_id: Optional[int],
    ) -> tuple[Dict[str, Any], Any]:
        from app.services.conversation_context_compaction_service import ConversationContextCompactionService

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
                return {}, artifacts
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
        return compacted_history, artifacts

    async def _persist_runtime_compaction(
        self,
        *,
        context: AgentContext,
        conversation_id: int,
        compacted_history: Dict[str, Any],
        artifacts: Any,
        latest_message_id: Optional[int],
        mode: str,
        history_event_title: str,
    ) -> None:
        from app.services.conversation_context_compaction_service import ConversationItemStreamUnavailableError

        compacted_history = dict(compacted_history or {})
        compacted_history["mid_run"] = mode == "mid_run"
        compacted_history["mode"] = mode
        merged_context_state = self._merge_conversation_state_with_workflow_binding(
            context.conversation_state,
            artifacts.context_state,
        )
        artifacts.context_state = dict(merged_context_state)
        context.conversation_state = dict(merged_context_state)
        await self.runtime_service.upsert_conversation_context_state(
            int(conversation_id),
            dict(merged_context_state),
        )
        await self.runtime_service.upsert_conversation_compacted_history(
            int(conversation_id),
            compacted_history,
        )
        await self.runtime_service.append_conversation_history_event(
            int(conversation_id),
            title=history_event_title,
            detail=(
                f"mode={mode}, "
                f"iteration={int(context.iteration)}, "
                f"compacted_messages={artifacts.compacted_message_count}, "
                f"summary_chars={len(artifacts.summary_text or '')}, "
                f"up_to_message_id={latest_message_id or 0}"
            ),
        )
        await self.runtime_service.append_conversation_context_snapshot(
            int(conversation_id),
            build_context_snapshot_payload(
                mode=mode,
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
                    "status": mode,
                    "message_id": latest_message_id,
                    "metadata": {
                        "compact_boundary_message_id": compacted_history.get("compact_boundary_message_id"),
                        "replacement_history": list(compacted_history.get("replacement_history") or []),
                        "compacted_message_count": artifacts.compacted_message_count,
                        "keep_turn_id": context.turn_id,
                        "mode": mode,
                    },
                    "created_at": datetime.utcnow().isoformat(),
                }
            ],
        )

        refreshed_item_stream = await self.runtime_service.get_conversation_item_stream(int(conversation_id))
        if not isinstance(refreshed_item_stream, dict):
            raise ConversationItemStreamUnavailableError(int(conversation_id))

        context.context_summary = artifacts.summary_text or context.context_summary
        context.conversation_state = dict(artifacts.context_state or {})
        context.compacted_history = compacted_history
        context.item_stream = refreshed_item_stream
        context.history_messages = self._active_history_messages_from_item_stream(
            list(refreshed_item_stream.get("entries") or []),
            fallback_boundary_message_id=latest_message_id,
        )
        context.messages = [
            self._sanitize_message_for_context(item)
            for item in list(context.history_messages or [])
            if isinstance(item, dict)
        ]
        context.context_truncated = True
        context.context_debug = {
            **dict(context.context_debug or {}),
            "formal_compaction_mode": mode,
            "formal_compaction_applied": True,
        }

    async def _maybe_pre_turn_compact(self, context: AgentContext) -> bool:
        if not bool(getattr(settings, "agent_context_budget_enabled", True)):
            return False
        if not bool(getattr(settings, "agent_pre_turn_compaction_enabled", True)):
            return False

        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return False
        if not callable(getattr(self.runtime_service, "get_conversation_item_stream", None)):
            context.context_debug = {
                **dict(context.context_debug or {}),
                "pre_turn_compaction_skipped": "runtime_item_stream_unavailable",
            }
            return False

        inputs = await self._gather_runtime_compaction_inputs(context)
        candidate_rows = [
            dict(item)
            for item in list(inputs.get("canonical_rows") or inputs.get("payload_rows") or [])
            if isinstance(item, dict)
        ]
        if not candidate_rows:
            context.context_debug = {
                **dict(context.context_debug or {}),
                "pre_turn_compaction_skipped": "no_history_rows",
            }
            return False

        window_turns = max(int(getattr(settings, "agent_context_window_turns", 8) or 8), 1)
        raw_pre_turn_recently_slid_turns = getattr(settings, "agent_context_recently_slid_turns", 2)
        recently_slid_turns = max(
            int(raw_pre_turn_recently_slid_turns if raw_pre_turn_recently_slid_turns is not None else 2),
            0,
        )
        older_rows, recently_slid_rows, _recent_rows = self._split_context_windows(
            candidate_rows,
            recent_turns=window_turns,
            recently_slid_turns=recently_slid_turns,
        )
        compactable_rows = list(older_rows or []) + list(recently_slid_rows or [])
        if not compactable_rows:
            context.context_debug = {
                **dict(context.context_debug or {}),
                "pre_turn_compaction_skipped": "no_compactable_history",
                "pre_turn_compaction_candidate_messages": len(candidate_rows),
            }
            return False

        effective_budget = max(
            int(
                (context.context_debug or {}).get("effective_budget")
                or self._resolve_system_budget_cap(model_context_window=self._current_model_context_window())
                or 0
            ),
            1024,
        )
        configured_trigger = int(getattr(settings, "agent_mid_run_compaction_message_tokens_trigger", 0) or 0)
        summary_trigger = int(getattr(settings, "agent_context_summary_trigger_tokens", 7000) or 7000)
        default_trigger = min(max(int(effective_budget * 0.6), 2048), max(summary_trigger, 2048))
        trigger_tokens = max(configured_trigger or default_trigger, 256)
        candidate_tokens = self._estimate_messages_tokens(candidate_rows)
        old_history_exists = bool(older_rows)
        pressure_triggered = candidate_tokens >= trigger_tokens
        if not old_history_exists and not pressure_triggered:
            context.context_debug = {
                **dict(context.context_debug or {}),
                "pre_turn_compaction_skipped": "below_pressure",
                "pre_turn_compaction_candidate_tokens": candidate_tokens,
                "pre_turn_compaction_trigger_tokens": trigger_tokens,
                "pre_turn_compaction_compactable_messages": len(compactable_rows),
            }
            return False

        pre_turn_payload_rows = inputs["canonical_rows"] or inputs["payload_rows"]
        compacted_history, artifacts = await self._resolve_runtime_compaction_artifacts(
            payload_rows=pre_turn_payload_rows,
            canonical_rows=inputs["canonical_rows"],
            tool_rows=inputs["tool_rows"],
            latest_message_id=inputs["latest_message_id"],
        )
        if not compacted_history:
            return False

        await self._persist_runtime_compaction(
            context=context,
            conversation_id=int(conversation_id),
            compacted_history=compacted_history,
            artifacts=artifacts,
            latest_message_id=inputs["latest_message_id"],
            mode="pre_turn",
            history_event_title="pre_turn_compact",
        )
        return True

    async def _maybe_mid_run_compact(self, context: AgentContext, system_prompt: str) -> bool:
        if not bool(getattr(settings, "agent_mid_run_compaction_enabled", True)):
            return False
        active_skill_names = [
            str(item.get("name") or "").strip()
            for item in list((self._last_skill_resolution or {}).get("active_skills") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if active_skill_names:
            context.context_debug = {
                **dict(context.context_debug or {}),
                "mid_run_compaction_active_skills": active_skill_names,
            }
            if self._PAPER_SKILL_NAME in active_skill_names:
                context.context_debug["mid_run_compaction_skill_mark"] = self._PAPER_SKILL_NAME
        min_iteration = max(int(getattr(settings, "agent_mid_run_compaction_min_iteration", 2) or 2), 1)
        if int(context.iteration or 0) < min_iteration:
            return False
        if int(context.mid_run_compactions or 0) >= max(
            int(getattr(settings, "agent_mid_run_compaction_max_per_run", 2) or 2),
            1,
        ):
            return False
        effective_budget = max(
            int(
                (context.context_debug or {}).get("effective_budget")
                or self._resolve_system_budget_cap(model_context_window=self._current_model_context_window())
                or 0
            ),
            1024,
        )
        configured_trigger = int(getattr(settings, "agent_mid_run_compaction_message_tokens_trigger", 0) or 0)
        default_trigger = max(int(effective_budget * 0.6), 2048)
        trigger_tokens = max(configured_trigger or default_trigger, 256)
        message_pressure_triggered = int(context.message_tokens_before_trim or 0) >= trigger_tokens
        if not bool(context.context_truncated) and not message_pressure_triggered:
            return False

        conversation_id = getattr(self.runtime_context, "conversation_id", None)
        if conversation_id is None:
            return False

        inputs = await self._gather_runtime_compaction_inputs(context)
        compacted_history, artifacts = await self._resolve_runtime_compaction_artifacts(
            payload_rows=inputs["payload_rows"],
            canonical_rows=inputs["canonical_rows"],
            tool_rows=inputs["tool_rows"],
            latest_message_id=inputs["latest_message_id"],
        )
        if not compacted_history:
            return False

        await self._persist_runtime_compaction(
            context=context,
            conversation_id=int(conversation_id),
            compacted_history=compacted_history,
            artifacts=artifacts,
            latest_message_id=inputs["latest_message_id"],
            mode="mid_run",
            history_event_title="mid_run_compact",
        )
        context.mid_run_compactions += 1
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
        return re.sub(r"\s+", " ", str(value or "")).strip()

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

    @staticmethod
    def _execution_spec_failure_requires_script(detail: str) -> bool:
        normalized = str(detail or "").strip().lower()
        if not normalized:
            return False
        script_tokens = (
            "generated_python",
            "generated_files",
            "generated file",
            "entrypoint_path",
            "generated_program_name",
            "relative_path",
            "shell wrapper",
            "raw command",
            "raw cwd",
            "execution_intent",
            "outside workspace",
        )
        return any(token in normalized for token in script_tokens)

    @staticmethod
    def _repo_read_failure_requires_search(detail: str) -> bool:
        normalized = str(detail or "").strip().lower()
        if not normalized:
            return False
        return any(
            marker in normalized
            for marker in (
                "repo_file_not_found",
                "仓库文件不存在",
                "missing repo file",
            )
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
            if self._probe_failure_counts_as_grounding_evidence(context, item):
                context.tool_failure_streaks[tool_name] = 0
                continue
            if tool_name == "literature_review_download_pdf" and self._literature_review_skill_is_active_for_context(context):
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
        if stop_tool == "paper_research_write_execution_spec" and self._execution_spec_failure_requires_script(stop_output):
            context.final_answer = (
                "`paper_research_write_execution_spec` 已连续失败 "
                f"{stop_count} 次，已停止自动重试。最近失败信息：{recent_detail}。"
                "先检查当前入口到底是哪一类：如果是 Python repo 文件，用 "
                "`execution_intent.entrypoint_type=\"repo_script\"`；"
                "如果是可执行 shell 脚本（如 `classification-results.sh`），直接写 argv "
                "如 `command=[\"./classification-results.sh\"]`；"
                "只有在确实需要新 wrapper/辅助程序时，才调用 "
                "`paper_research_write_execution_script` 写到 `executions/{execution_id}/...`，"
                "再让 `paper_research_write_execution_spec` 引用它。"
            )
            context.state = AgentState.DONE
            return (
                "检测到 execution_spec 连续失败且问题集中在脚本/命令表达，"
                "已收束到“先分清 Python 入口 / 可执行 shell 入口 / 新建 wrapper”这三种合法路径。"
            )
        context.final_answer = (
            f"`{stop_tool}` 已连续失败 {stop_count} 次，已停止自动重试。"
            f"最近失败信息：{recent_detail}。建议先检查前置条件或调整指令后再继续。"
        )
        context.state = AgentState.DONE
        return (
            f"检测到 `{stop_tool}` 连续失败 {stop_count} 次，继续自动尝试大概率只会重复犯错，"
            "本轮提前停止。"
        )

    def _probe_failure_counts_as_grounding_evidence(
        self,
        context: AgentContext,
        observation: Dict[str, Any],
    ) -> bool:
        tool_name = str(observation.get("tool") or "").strip()
        if tool_name not in {"paper_research_probe_url", "paper_research_probe_repo"}:
            return False
        workflow_binding = (
            dict((context.conversation_state or {}).get("workflow_binding") or {})
            if isinstance(context.conversation_state, dict)
            else {}
        )
        if str(workflow_binding.get("current_stage") or "").strip().lower() != "planning":
            return False

        error_code = str(observation.get("error") or "").strip().lower()
        result_data = dict(observation.get("data") or {}) if isinstance(observation.get("data"), dict) else {}
        diagnosis = str(result_data.get("diagnosis") or "").strip().lower()
        raw_status = result_data.get("status_code", result_data.get("status"))
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = None

        if tool_name == "paper_research_probe_url":
            if error_code != "url_probe_failed":
                return False
            if status_code is not None and status_code >= 400:
                return True
            return diagnosis in {
                "gdrive_confirm_required",
                "download_gate",
                "login_required",
                "quota_limited",
                "access_denied",
                "auth_required",
                "forbidden",
                "not_found",
                "accepted_but_empty",
                "redirect_broken",
                "checksum_mismatch",
                "license_gate",
                "manual_download_required",
                "http_401",
                "http_403",
                "http_404",
                "http_410",
            }

        if error_code != "repo_probe_failed":
            return False
        if status_code is not None and status_code >= 400:
            return True
        return diagnosis in {"repo_unreachable", "repo_page_reachable_but_not_cloneable"}

    def _maybe_stop_after_authorization_required(
        self,
        context: AgentContext,
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        channel = str(getattr(self.runtime_context, "channel", "") or "").strip().lower()
        if channel not in {"chat", "codelab_agent", "notebook_agent"}:
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

    def _maybe_stop_after_background_execution_started(
        self,
        context: AgentContext,
        events: List[Dict[str, Any]],
    ) -> Optional[str]:
        channel = str(getattr(self.runtime_context, "channel", "") or "").strip().lower()
        if channel not in {"chat", "codelab_agent", "notebook_agent"}:
            return None

        observations = [
            event.get("data")
            for event in events
            if event.get("type") == "observation" and isinstance(event.get("data"), dict)
        ]
        if not observations:
            return None

        latest_background_observation: Optional[Dict[str, Any]] = None
        for item in observations:
            tool_data = item.get("data") if isinstance(item.get("data"), dict) else {}
            result_data = tool_data.get("data") if isinstance(tool_data.get("data"), dict) else {}
            execution = dict(result_data.get("background_execution") or tool_data.get("background_execution") or {})
            status = str(execution.get("status") or "").strip().lower()
            if (
                bool(result_data.get("background_execution_started") or tool_data.get("background_execution_started"))
                and not bool(result_data.get("background_execution_completed") or tool_data.get("background_execution_completed"))
                and status in {"", "pending", "running"}
            ):
                latest_background_observation = item

        if latest_background_observation is None:
            return None

        tool_data = latest_background_observation.get("data") if isinstance(latest_background_observation.get("data"), dict) else {}
        result_data = tool_data.get("data") if isinstance(tool_data.get("data"), dict) else {}
        execution = dict(result_data.get("background_execution") or tool_data.get("background_execution") or {})
        stage = str(execution.get("stage") or "").strip().lower()
        execution_id = str(execution.get("execution_id") or "").strip()
        cell_id = str(result_data.get("cell_id") or tool_data.get("cell_id") or execution.get("cell_id") or "").strip()
        if stage in {"env_setup", "data_prep"}:
            return None
        detail = str(
            result_data.get("background_execution_user_summary")
            or tool_data.get("background_execution_user_summary")
            or latest_background_observation.get("output")
            or "已启动后台 execution。"
        ).strip()
        detail = self._truncate_failure_text(detail, limit=420)
        suffix_parts = []
        if cell_id:
            suffix_parts.append(f"cell_id={cell_id}")
        if execution_id and f"Execution ID: {execution_id}" not in detail and f"execution_id={execution_id}" not in detail:
            suffix_parts.append(f"execution_id={execution_id}")
        if suffix_parts:
            detail = f"{detail}\n- Context: {'，'.join(suffix_parts)}"
        context.final_answer = detail
        context.state = AgentState.DONE
        return "检测到长任务已切换到后台执行，本轮停止继续调用工具，改为向用户回报任务状态。"

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
        read_only_tools = {
            "notebook_cell",
            "notebook_variables",
            "paper_research_status",
            "paper_research_search_project_zoekt",
            "paper_research_read_execution",
            "paper_research_read_execution_spec",
        }
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
        threshold = 2 if tool_name.startswith("paper_research_") else threshold
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
        target_label = ""
        latest_input = latest_matching.get("input") if isinstance(latest_matching.get("input"), dict) else {}
        if tool_name == "paper_research_search_project_zoekt":
            target_label = str(latest_input.get("query") or "").strip()
        interventions = int(context.context_debug.get("repeat_read_interventions") or 0)
        repo_read_family = tool_name.startswith("paper_research_")
        if interventions < 1:
            context.context_debug["repeat_read_interventions"] = interventions + 1
            guard_body = (
                "不要继续重复读取；请直接基于现有 notebook 信息回答用户问题。"
                "如果信息仍不足，请明确指出缺失的前置步骤，而不是再次调用同一读取工具。"
            )
            guard_summary = "检测到重复读取同一 Notebook 信息，已强制要求基于现有 observation 收束回答。"
            if repo_read_family:
                guard_body = (
                    "不要继续重复读取/检索同一目标；请基于现有 repo observation 直接收束。"
                    "如果当前 skill 不允许改 repo/source，就明确报告 blocker；"
                    "如果还缺一步，请说明缺的是什么，而不是继续搜索同一脚本。"
                )
                guard_summary = "检测到重复读取同一 repo 目标，已强制要求基于现有 observation 收束。"
            context.messages.append(
                {
                    "role": "user",
                    "content": (
                        "<observation>\n"
                        f"系统提示：你已经连续 {repeat_count} 次调用 `{tool_name}` 读取同一目标，且 observation 已成功返回。"
                        + (f"目标={target_label}。" if target_label else "")
                        + guard_body
                        + "\n"
                        f"最近 observation 摘要：{observation_summary}\n"
                        "</observation>\n\n"
                        "请现在直接给出最终回答。"
                    ),
                }
            )
            return guard_summary

        context.final_answer = (
            f"`{tool_name}` 已连续重复读取同一目标 {repeat_count} 次，已停止自动重试。"
            f"最近 observation 摘要：{observation_summary}。"
            + (
                "这通常说明当前更需要基于已有 observation 直接报告 blocker 或给出下一步。"
                if repo_read_family
                else "这通常说明当前更需要基于已有 observation 直接作答，而不是继续读取同一单元格或变量。"
            )
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
        effective_arguments = self._apply_tool_call_overrides(
            call.name,
            call.arguments,
            workflow_binding=(
                dict((context.conversation_state or {}).get("workflow_binding") or {})
                if isinstance(context.conversation_state, dict)
                else {}
            ),
        )
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
        selected_tools = {
            str(item).strip()
            for item in list(self._last_tool_selection.get("selected_tools") or [])
            if str(item or "").strip()
        }
        live_event_token = None
        if getattr(self.runtime_context, "live_event_callback", None) and call.name in {
            "project_claude",
            "docx_generate_with_claude",
            "docx_refine_with_claude",
        }:
            live_event_token = set_tool_live_event_emitter(self.runtime_context.live_event_callback)
        try:
            if self._paper_skill_is_active_for_context(context) and call.name in self._PAPER_SKILL_SELF_WORK_TOOL_NAMES:
                result = self._build_paper_skill_self_work_block_result(call.name)
            elif selected_tools and call.name not in selected_tools:
                contract = build_tool_error_contract(
                    code="tool_not_allowed",
                    message="当前回合不允许调用该工具",
                    tool_name=call.name,
                    stage="agent_execute",
                    detail=f"selected_tools={sorted(selected_tools)}",
                    retryable=False,
                    metadata={"selected_tools": sorted(selected_tools)},
                )
                result = ToolResult(
                    success=False,
                    output=(
                        f"{contract['message']}: `{call.name}` 不在当前允许集合内。"
                        f"允许工具: {', '.join(sorted(selected_tools))}"
                    ),
                    error=str(contract["code"]),
                    data=merge_error_contract(None, contract),
                )
            else:
                decision_state = self._normalize_decision_state(
                    dict((context.conversation_state or {}).get("decision_state") or {})
                    if isinstance(context.conversation_state, dict)
                    else {},
                    workflow_binding=(context.conversation_state or {}).get("workflow_binding") or {},
                )
                allowed_action_list = [
                    str(item).strip()
                    for item in list(decision_state.get("allowed_actions") or [])
                    if str(item or "").strip()
                ]
                allowed_actions = set(allowed_action_list)
                requested_action = self._decision_action_for_tool(call.name)
                enforce_decision_state_gate = self._should_enforce_decision_state_gate(call.name)
                if enforce_decision_state_gate and self._should_bypass_decision_state_gate_for_tool(context, call.name):
                    enforce_decision_state_gate = False
                if (
                    enforce_decision_state_gate
                    and (
                    allowed_actions
                    and requested_action
                    and requested_action not in allowed_actions
                    )
                ):
                    contract = build_tool_error_contract(
                        code="tool_not_allowed_by_decision_state",
                        message="当前决策状态不允许调用该工具",
                        tool_name=call.name,
                        stage="agent_execute",
                        detail=(
                            f"requested_action={requested_action}; "
                            f"allowed_actions={allowed_action_list}; "
                            f"decision_status={decision_state.get('status') or 'unknown'}"
                        ),
                        retryable=False,
                        metadata={
                            "requested_action": requested_action,
                            "allowed_actions": allowed_action_list,
                            "decision_status": decision_state.get("status"),
                            "next_action": decision_state.get("next_action"),
                        },
                    )
                    result = ToolResult(
                        success=False,
                        output=(
                            f"{contract['message']}: `{call.name}` 对应动作 `{requested_action}`，"
                            f"但当前只允许 {', '.join(allowed_action_list)}。"
                        ),
                        error=str(contract["code"]),
                        data=merge_error_contract(None, contract),
                    )
                else:
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
        finally:
            if live_event_token is not None:
                reset_tool_live_event_emitter(live_event_token)

        observation_output = result.output
        scope_reminder = self._tool_failure_scope_reminder(context, call.name, result)
        if scope_reminder:
            observation_output = f"{str(observation_output or '').rstrip()}\n\n{scope_reminder}".strip()
        permission_required = self._tool_result_requires_permission(result)
        citation_tool_name = self._citation_tool_name(
            call.name,
            result.data if isinstance(result.data, dict) else {},
        )
        if result.success and citation_tool_name == "knowledge_search":
            context.knowledge_search_calls += 1
            observation_output = await self._compress_knowledge_observation(
                str(effective_arguments.get("query", "")),
                result,
                context=context,
            )
            context.allowed_source_labels.update(self._extract_source_labels(observation_output))
        elif result.success and citation_tool_name == "web_search":
            context.web_search_calls += 1
            observation_output = await self._compress_web_search_observation(
                str(call.arguments.get("query", "")),
                result,
                context=context,
            )
            context.allowed_web_source_labels.update(self._extract_web_source_labels(observation_output))
        result_metadata = self._normalize_tool_result_metadata(
            tool_name=call.name,
            observation_output=observation_output,
            result_data=result.data if isinstance(result.data, dict) else {},
        )
        await self._maybe_update_workflow_binding_from_tool_result(context, call, result)
        await self._maybe_pin_skill_from_tool_result(context, call, result)
        self._remember_source_items(
            context,
            list(result_metadata.get("source_items") or []) if isinstance(result_metadata.get("source_items"), list) else [],
        )

        observation_event = {
            "type": "observation",
            "data": {
                "tool": call.name,
                "success": bool(result.success),
                "output": observation_output,
                "data": result.data,
                "metadata": result_metadata,
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
            result_data=dict(result.data or {}) if isinstance(result.data, dict) else {},
            tool_call_id=call.call_id,
            arguments=dict(effective_arguments or {}),
            success=bool(result.success),
            error=str(result.error or "").strip() or None,
            permission_required=permission_required,
            execution_time_ms=float(getattr(result, "execution_time_ms", 0.0) or 0.0),
            output_tokens_estimate=int(getattr(result, "output_tokens_estimate", 0) or 0),
            truncated=bool(getattr(result, "truncated", False)),
            metadata=result_metadata,
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

    def _paper_skill_active_for_context(
        self,
        context: AgentContext,
        executed_calls: Sequence[ExecutedToolCall],
    ) -> bool:
        workflow_binding = (
            dict((context.conversation_state or {}).get("workflow_binding") or {})
            if isinstance(context.conversation_state, dict)
            else {}
        )
        if str(workflow_binding.get("skill") or "").strip() == self._PAPER_SKILL_NAME:
            return True
        active_skill_names = [
            str(item or "").strip()
            for item in list(getattr(self.runtime_context, "active_skill_names", []) or [])
            if str(item or "").strip()
        ]
        if self._PAPER_SKILL_NAME in active_skill_names:
            return True
        return any(
            str(item.tool_name or "").strip().startswith(self._PAPER_RESEARCH_TOOL_PREFIX)
            for item in list(executed_calls or [])
            if isinstance(item, ExecutedToolCall)
        )

    @classmethod
    def _tool_workflow_refs(cls, item: ExecutedToolCall) -> List[str]:
        result_data = dict(item.result_data or {}) if isinstance(item.result_data, dict) else {}
        refs: List[str] = []
        relative_path = str(
            result_data.get("relative_path")
            or result_data.get("repo_relative_path")
            or result_data.get("path")
            or ""
        ).strip()
        if relative_path:
            if result_data.get("line_start") is not None and result_data.get("line_end") is not None:
                refs.append(f"{relative_path}:{result_data.get('line_start')}-{result_data.get('line_end')}")
            elif result_data.get("line_number") is not None:
                refs.append(f"{relative_path}:{result_data.get('line_number')}")
            else:
                refs.append(relative_path)
        execution_id = str(result_data.get("execution_id") or "").strip()
        if execution_id:
            refs.append(f"execution:{execution_id}")
        background_execution = (
            dict(result_data.get("background_execution") or {})
            if isinstance(result_data.get("background_execution"), dict)
            else {}
        )
        background_execution_id = str(background_execution.get("execution_id") or "").strip()
        if background_execution_id:
            refs.append(f"background:{background_execution_id}")
        return list(dict.fromkeys(refs))

    @classmethod
    def _tool_workflow_highlight(cls, item: ExecutedToolCall) -> Optional[str]:
        result_data = dict(item.result_data or {}) if isinstance(item.result_data, dict) else {}
        tool_name = str(item.tool_name or "").strip()
        status = "需授权" if item.permission_required else ("成功" if item.success else "失败")
        if tool_name == "paper_research_search_project_zoekt":
            matched_files = int(result_data.get("matched_file_count") or 0)
            returned_matches = int(result_data.get("returned_matches") or 0)
            query = cls._compact_debug_text(result_data.get("query") or item.arguments.get("query") or "", 72)
            if query:
                return f"{status} · Project Zoekt 搜索 `{query}` · 命中 {returned_matches} 处 / {matched_files} 个文件"
        if tool_name == "paper_research_read_execution":
            execution_id = str(result_data.get("execution_id") or "").strip()
            execution_status = str(result_data.get("status") or "").strip()
            if execution_id or execution_status:
                return f"{status} · execution={execution_id or '-'} · status={execution_status or '-'}"
        if tool_name == "paper_research_start_execution":
            background_execution = (
                dict(result_data.get("background_execution") or {})
                if isinstance(result_data.get("background_execution"), dict)
                else {}
            )
            execution_id = str(
                background_execution.get("execution_id")
                or result_data.get("execution_id")
                or ""
            ).strip()
            stage = str(background_execution.get("stage") or "").strip()
            if execution_id:
                suffix = f" · stage={stage}" if stage else ""
                return f"{status} · 已启动 execution `{execution_id}`{suffix}"
        if tool_name in {
            "paper_research_write_execution_spec",
            "paper_research_write_execution_script",
        }:
            ref = next(iter(cls._tool_workflow_refs(item)), "")
            if ref:
                return f"{status} · 已更新 `{ref}`"
        detail = cls._truncate_failure_text(item.error or item.observation_output or "", limit=140)
        if detail:
            return f"{status} · {detail}"
        return None

    @classmethod
    def _tool_workflow_next_action(
        cls,
        rows: Sequence[ExecutedToolCall],
        *,
        paper_skill_active: bool,
        repo_edit_allowed: Optional[bool],
        workflow_binding: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, Dict[str, Any]]:
        failed_rows = [item for item in rows if not item.success]
        permission_rows = [item for item in rows if item.permission_required]
        tool_names = [str(item.tool_name or "").strip() for item in rows if str(item.tool_name or "").strip()]
        last_tool = tool_names[-1] if tool_names else ""
        current_stage = str(dict(workflow_binding or {}).get("current_stage") or "").strip().lower()
        planning_tool_names = {
            "paper_research_probe_repo",
            "paper_research_probe_url",
            "paper_research_inspect_runtime",
        }
        planning_context = current_stage == "planning" or any(name in planning_tool_names for name in tool_names)

        if permission_rows:
            return (
                "等待用户授权后继续。",
                {
                    "status": "waiting",
                    "evidence_status": "sufficient",
                    "next_action": "wait_user",
                    "blocked_reason": "authorization_required",
                },
            )
        if failed_rows:
            next_action = "report_blocker" if paper_skill_active else "inspect_failure"
            blocked_reason = "tool_failed"
            if last_tool == "paper_research_write_execution_spec":
                last_failure = failed_rows[-1]
                failure_detail = str(
                    last_failure.observation_output or last_failure.error or ""
                )
                if cls._execution_spec_failure_requires_script(failure_detail):
                    return (
                        "execution_spec 连续卡在脚本/命令表达；先区分当前入口是 Python repo 文件、可执行 shell 脚本，还是确实需要新 wrapper，再按对应合法形态重写 execution_spec。",
                        {
                            "status": "blocked",
                            "evidence_status": "sufficient",
                            "next_action": "inspect_execution_spec",
                            "blocked_reason": "execution_contract_invalid",
                            "allowed_actions": ["inspect_execution_spec", "write_execution_script"],
                        },
                    )
                next_action = "inspect_execution_spec"
                blocked_reason = "execution_contract_invalid"
            elif last_tool == "paper_research_start_execution":
                last_failure = failed_rows[-1]
                failure_detail = str(
                    last_failure.observation_output or last_failure.error or ""
                )
                next_action = "inspect_execution_spec"
                blocked_reason = "execution_contract_invalid"
            return (
                "基于失败 observation 收束原因，不要继续重复同类读搜。",
                {
                    "status": "blocked",
                    "evidence_status": "sufficient",
                    "next_action": next_action,
                    "blocked_reason": blocked_reason,
                },
            )
        if last_tool in {"paper_research_start_execution", "paper_research_launch_claude_code"}:
            return (
                "等待 execution 运行，并调用 `paper_research_read_execution` 观察状态。",
                {
                    "status": "waiting",
                    "evidence_status": "sufficient",
                    "next_action": "observe_execution",
                    "allowed_actions": ["observe_execution"],
                },
            )
        if last_tool == "paper_research_read_execution":
            last_row = rows[-1] if rows else None
            result_data = dict(last_row.result_data or {}) if isinstance(last_row, ExecutedToolCall) else {}
            execution_status = str(result_data.get("status") or "").strip().lower()
            if execution_status in {"running", "pending"}:
                return (
                    "继续观察 execution 状态，等待下一次稳定结果。",
                    {
                        "status": "waiting",
                        "evidence_status": "sufficient",
                        "next_action": "observe_execution",
                        "allowed_actions": ["observe_execution"],
                    },
                )
            if execution_status in {"failed", "error"}:
                return (
                    "收束失败原因；若当前 skill 不允许改 repo/source，则直接报告 blocker。",
                    {
                        "status": "blocked",
                        "evidence_status": "sufficient",
                        "next_action": "report_blocker" if paper_skill_active else "inspect_failure",
                        "blocked_reason": "execution_failed",
                    },
                )
        if last_tool == "paper_research_write_execution_spec":
            return (
                "execution_spec 已准备，可启动 execution；如果你决定把 repo 执行交给 Claude Code，也可以直接切到 `paper_research_launch_claude_code`。",
                {
                    "status": "ready",
                    "evidence_status": "sufficient",
                    "next_action": "start_execution",
                    "allowed_actions": ["start_execution", "launch_claude_code"],
                },
            )
        if planning_context and last_tool in {
            "paper_research_probe_repo",
            "paper_research_probe_url",
            "paper_research_search_project_zoekt",
            "paper_research_inspect_runtime",
        }:
            return (
                "reference / repo / runtime 证据已更新。继续用 `project_tree` 确认目录结构、用 `project_read_file` 读回关键文件、用 `paper_research_search_project_zoekt` 做更具体的文本检索；证据足够后直接综合当前发现。",
                {
                    "status": "active",
                    "evidence_status": "sufficient",
                    "next_action": "inspect_project",
                    "allowed_actions": [
                        "inspect_project",
                        "search_repo",
                        "synthesize",
                        "report_blocker",
                    ],
                },
            )
        if last_tool == "paper_research_search_project_zoekt":
            message = (
                "已拿到 project/repo 证据；下一步应读取关键文件、继续用更具体的 Zoekt 查询缩小范围，或直接综合当前发现。"
            )
            decision_state = {
                "status": "active",
                "evidence_status": "sufficient",
                "next_action": "synthesize",
                "allowed_actions": [
                    "inspect_project",
                    "search_repo",
                    "synthesize",
                    "report_blocker",
                ],
            }
            if paper_skill_active and repo_edit_allowed is False:
                decision_state["blocked_reason"] = "repo_patch_required"
            return message, decision_state

        return (
            "基于当前 observation 收束下一步，不要重复调用同一成功工具。",
            {
                "status": "active",
                "evidence_status": "sufficient",
                "next_action": "synthesize",
                "allowed_actions": ["synthesize"],
            },
        )

    def _build_tool_workflow_summary(
        self,
        context: AgentContext,
        executed_calls: Sequence[ExecutedToolCall],
    ) -> Dict[str, Any]:
        rows = [item for item in list(executed_calls or []) if isinstance(item, ExecutedToolCall)]
        if not rows:
            return {}

        tool_names = [str(item.tool_name or "").strip() for item in rows if str(item.tool_name or "").strip()]
        unique_tool_names = list(dict.fromkeys(tool_names))
        success_rows = [item for item in rows if item.success]
        failed_rows = [item for item in rows if not item.success]
        permission_rows = [item for item in rows if item.permission_required]
        paper_skill_active = self._paper_skill_active_for_context(context, rows)
        existing_state = dict(context.conversation_state or {}) if isinstance(context.conversation_state, dict) else {}
        workflow_binding = dict(existing_state.get("workflow_binding") or {})
        existing_decision_state = self._normalize_decision_state(
            existing_state.get("decision_state") or {},
            workflow_binding=workflow_binding,
        )
        repo_edit_allowed = existing_decision_state.get("repo_edit_allowed")
        if repo_edit_allowed is None and workflow_binding.get("skill") == self._PAPER_SKILL_NAME:
            repo_edit_allowed = False

        if len(unique_tool_names) == 1:
            headline = f"{unique_tool_names[0]} 已执行"
        elif unique_tool_names:
            headline = "、".join(unique_tool_names[:3]) + " 已执行"
        else:
            headline = "工具批次已执行"

        summary_status = "observed"
        if permission_rows:
            summary_status = "waiting"
        elif failed_rows:
            summary_status = "blocked"
        elif any(name in {"paper_research_start_execution", "paper_research_launch_claude_code"} for name in unique_tool_names):
            summary_status = "progressed"
        elif any(name.startswith("paper_research_write_") for name in unique_tool_names):
            summary_status = "ready"

        highlights = [
            item
            for item in (
                self._tool_workflow_highlight(call)
                for call in rows[:6]
            )
            if item
        ]
        next_action_text, decision_state = self._tool_workflow_next_action(
            rows,
            paper_skill_active=paper_skill_active,
            repo_edit_allowed=bool(repo_edit_allowed) if repo_edit_allowed is not None else None,
            workflow_binding=workflow_binding,
        )
        decision_state = self._merge_decision_state(
            existing_decision_state,
            {
                **decision_state,
                "repo_edit_allowed": repo_edit_allowed,
            },
            workflow_binding=workflow_binding,
        )
        evidence_refs: List[str] = []
        for item in rows:
            evidence_refs.extend(self._tool_workflow_refs(item))
        return {
            "version": "tool_workflow_summary.v1",
            "headline": headline,
            "status": summary_status,
            "highlights": highlights[:4],
            "next_action": self._compact_debug_text(next_action_text, 160),
            "evidence_refs": list(dict.fromkeys(evidence_refs))[:6],
            "decision_state": decision_state,
            "tool_names": unique_tool_names[:6],
            "success_count": len(success_rows),
            "failure_count": len(failed_rows),
            "permission_count": len(permission_rows),
        }

    @classmethod
    async def _tool_result_ledger_summary_text(cls, item: ExecutedToolCall) -> str:
        result_data = dict(item.result_data or {}) if isinstance(item.result_data, dict) else {}
        metadata = dict(item.metadata or {}) if isinstance(item.metadata, dict) else {}
        status = (
            "需授权"
            if item.permission_required
            else "成功"
            if item.success
            else "失败"
        )

        parts: List[str] = [f"tool={item.tool_name}", f"status={status}"]
        for key in ("relative_path", "repo_relative_path", "execution_id", "content_type", "mode"):
            value = result_data.get(key)
            if value is not None and str(value).strip():
                parts.append(f"{key}={str(value).strip()}")
        for key in ("line_start", "line_end", "page", "total_pages", "chunk_index", "total_chunks"):
            value = result_data.get(key)
            if value is not None:
                parts.append(f"{key}={value}")

        background_execution = (
            dict(result_data.get("background_execution") or {})
            if isinstance(result_data.get("background_execution"), dict)
            else {}
        )
        if background_execution:
            for key in ("execution_id", "stage", "status"):
                value = background_execution.get(key)
                if value is not None and str(value).strip():
                    parts.append(f"background_{key}={str(value).strip()}")

        source_labels = [
            str(label).strip()
            for label in list(metadata.get("source_labels") or [])
            if str(label or "").strip()
        ]
        if source_labels:
            parts.append(f"source_labels={','.join(source_labels[:6])}")
        if metadata.get("result_count") is not None:
            parts.append(f"result_count={metadata.get('result_count')}")

        detail = cls._tool_result_detail_for_ledger(item)
        if detail:
            if len(detail) > 1600:
                detail = detail[:1600].rstrip()
            raw_summary = f"{' | '.join(parts)}\nDetail:\n{detail}"
        else:
            raw_summary = " | ".join(parts)

        if cls._tool_result_has_structured_debug_detail(item):
            return cls._normalize_compacted_text(raw_summary)

        compressed = await cls._compress_text_with_qwen_turbo(
            raw_summary,
            target_token_budget=120,
            source="chat.tool_result_ledger_summary",
            compression_kind="单次工具结果摘要",
        )
        compressed = cls._normalize_compacted_text(compressed)
        if compressed:
            return compressed
        return cls._normalize_compacted_text(" | ".join(parts))

    @classmethod
    def _tool_result_has_structured_debug_detail(cls, item: ExecutedToolCall) -> bool:
        result_data = dict(item.result_data or {}) if isinstance(item.result_data, dict) else {}
        return any(
            isinstance(result_data.get(key), list) and list(result_data.get(key) or [])
            for key in (
                "structured_validation_errors",
                "schema_errors",
                "grounding_conflicts",
                "draft_errors",
                "global_errors",
            )
        ) or bool(result_data.get("allowed_paths"))

    @classmethod
    def _tool_result_detail_for_ledger(cls, item: ExecutedToolCall) -> str:
        result_data = dict(item.result_data or {}) if isinstance(item.result_data, dict) else {}
        lines: List[str] = []

        structured_validation_errors = [
            dict(entry)
            for entry in list(result_data.get("structured_validation_errors") or [])
            if isinstance(entry, dict)
        ]
        for entry in structured_validation_errors[:3]:
            path = cls._compact_debug_text(entry.get("path") or "", 96)
            code = cls._compact_debug_text(entry.get("code") or "", 72)
            message = cls._compact_debug_text(entry.get("message") or "", 220)
            evidence_needed = cls._compact_debug_text(entry.get("evidence_needed") or "", 140)
            suggested_calls = [
                cls._render_tool_call_suggestion(item)
                for item in list(entry.get("suggested_tool_calls") or [])
                if isinstance(item, dict)
            ]
            prefix_parts = [part for part in [path, code] if part]
            prefix = f"[{' / '.join(prefix_parts)}] " if prefix_parts else ""
            line = f"{prefix}{message}" if message else prefix.rstrip()
            if evidence_needed:
                line += f"；需要证据: {evidence_needed}"
            if suggested_calls:
                line += f"；建议: {'; '.join(suggested_calls[:2])}"
            if line:
                lines.append(line)

        schema_errors = [
            dict(entry)
            for entry in list(result_data.get("schema_errors") or [])
            if isinstance(entry, dict)
        ]
        for entry in schema_errors[:3]:
            path = cls._compact_debug_text(entry.get("path") or "", 96)
            code = cls._compact_debug_text(entry.get("code") or "", 72)
            message = cls._compact_debug_text(entry.get("message") or "", 220)
            prefix_parts = [part for part in [path, code] if part]
            prefix = f"[{' / '.join(prefix_parts)}] " if prefix_parts else ""
            line = f"{prefix}{message}" if message else prefix.rstrip()
            if line:
                lines.append(line)

        grounding_conflicts = [
            dict(entry)
            for entry in list(result_data.get("grounding_conflicts") or [])
            if isinstance(entry, dict)
        ]
        for entry in grounding_conflicts[:3]:
            path = cls._compact_debug_text(entry.get("path") or "", 96)
            code = cls._compact_debug_text(entry.get("code") or "", 72)
            message = cls._compact_debug_text(entry.get("message") or "", 220)
            prefix_parts = [part for part in [path, code] if part]
            prefix = f"[{' / '.join(prefix_parts)}] " if prefix_parts else ""
            line = f"{prefix}{message}" if message else prefix.rstrip()
            if line:
                lines.append(line)

        draft_errors = [
            dict(entry)
            for entry in list(result_data.get("draft_errors") or [])
            if isinstance(entry, dict)
        ]
        for group in draft_errors[:2]:
            draft_id = cls._compact_debug_text(group.get("draft_id") or "", 72) or "unknown_draft"
            errors = [
                dict(entry)
                for entry in list(group.get("errors") or [])
                if isinstance(entry, dict)
            ]
            for entry in errors[:2]:
                path = cls._compact_debug_text(entry.get("path") or "", 96)
                message = cls._compact_debug_text(entry.get("message") or "", 220)
                prefix = f"[draft={draft_id}"
                if path:
                    prefix += f" / {path}"
                prefix += "] "
                line = f"{prefix}{message}" if message else prefix.rstrip()
                if line:
                    lines.append(line)

        global_errors = [
            dict(entry)
            for entry in list(result_data.get("global_errors") or [])
            if isinstance(entry, dict)
        ]
        for entry in global_errors[:3]:
            path = cls._compact_debug_text(entry.get("path") or "", 96)
            message = cls._compact_debug_text(entry.get("message") or "", 220)
            prefix = f"[{path}] " if path else ""
            line = f"{prefix}{message}" if message else prefix.rstrip()
            if line:
                lines.append(line)

        allowed_paths = [
            cls._compact_debug_text(item, 96)
            for item in list(result_data.get("allowed_paths") or [])
            if cls._compact_debug_text(item, 96)
        ]
        if allowed_paths:
            lines.append(
                "允许的 reference 路径: " + ", ".join(allowed_paths[:6]) + "；先用 project_tree / project_read_file 按 Project 根目录继续检查。"
            )

        if lines:
            return "\n".join(f"- {line}" for line in lines)
        return cls._normalize_compacted_text(item.error or item.observation_output or "")

    @classmethod
    def _render_tool_call_suggestion(cls, payload: Dict[str, Any]) -> str:
        tool = cls._compact_debug_text(payload.get("tool") or "", 64)
        args = dict(payload.get("args") or {}) if isinstance(payload.get("args"), dict) else {}
        if not tool:
            return ""
        if not args:
            return tool
        preview_parts: List[str] = []
        for key in sorted(args.keys())[:3]:
            value = args.get(key)
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            else:
                rendered = str(value)
            preview_parts.append(f"{key}={cls._compact_debug_text(rendered, 80)}")
        suffix = ", ".join(item for item in preview_parts if item)
        return f"{tool}({suffix})" if suffix else tool

    @classmethod
    async def _tool_use_summary_text(
        cls,
        executed_calls: Sequence[ExecutedToolCall],
        *,
        workflow_summary: Optional[Dict[str, Any]] = None,
    ) -> str:
        rows = [item for item in list(executed_calls or []) if isinstance(item, ExecutedToolCall)]
        if not rows:
            return ""
        summary = dict(workflow_summary or {}) if isinstance(workflow_summary, dict) else {}
        headline = cls._compact_debug_text(summary.get("headline") or "", 120) or "工具批次已执行"
        status = cls._compact_debug_text(summary.get("status") or "", 32)
        highlights = [
            cls._compact_debug_text(item, 160)
            for item in list(summary.get("highlights") or [])
            if cls._compact_debug_text(item, 160)
        ]
        next_action = cls._compact_debug_text(summary.get("next_action") or "", 180)
        refs = [
            cls._compact_debug_text(item, 96)
            for item in list(summary.get("evidence_refs") or [])
            if cls._compact_debug_text(item, 96)
        ]

        lines: List[str] = [f"headline={headline}"]
        if status:
            lines.append(f"status={status}")
        for item in highlights[:3]:
            lines.append(f"- {item}")
        if next_action:
            lines.append(f"next_action={next_action}")
        if refs:
            lines.append(f"evidence_refs={', '.join(refs[:4])}")
        return "\n".join(lines).strip()

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
        workflow_summary = self._build_tool_workflow_summary(context, executed_calls)
        summary_text = await self._tool_use_summary_text(
            executed_calls,
            workflow_summary=workflow_summary,
        )
        permission_rows = [
            item for item in executed_calls
            if isinstance(item, ExecutedToolCall) and item.permission_required
        ]
        if not summary_text and not permission_rows:
            return
        try:
            items_to_append: List[Dict[str, Any]] = []
            if summary_text:
                items_to_append.append(
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
                            "workflow_summary": workflow_summary or None,
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )
            if permission_rows:
                permission_text = "；".join(
                    self._truncate_failure_text(
                        item.observation_output or item.error or f"`{item.tool_name}` 需要授权后才能继续。",
                        limit=120,
                    )
                    for item in permission_rows[:2]
                )
                items_to_append.append(
                    {
                        "kind": "permission_denial",
                        "turn_id": context.turn_id,
                        "role": "assistant",
                        "run_id": context.run_id,
                        "iteration": context.iteration,
                        "summary": permission_text or "本轮有工具因权限限制未执行。",
                        "content": permission_text or "本轮有工具因权限限制未执行。",
                        "status": "authorization_required",
                        "parallel_group": parallel_group,
                        "metadata": {
                            "tool_names": [
                                item.tool_name
                                for item in permission_rows
                                if str(item.tool_name or "").strip()
                            ],
                            "tool_call_ids": [
                                item.tool_call_id
                                for item in permission_rows
                                if str(item.tool_call_id or "").strip()
                            ],
                        },
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )
            await self.runtime_service.append_conversation_item_entries(
                int(conversation_id),
                items_to_append,
            )
            decision_state = self._normalize_decision_state(
                (workflow_summary or {}).get("decision_state") or {},
                workflow_binding=(context.conversation_state or {}).get("workflow_binding") or {},
            )
            if decision_state:
                current_state = dict(context.conversation_state or {}) if isinstance(context.conversation_state, dict) else {}
                merged_state = self._merge_conversation_state_with_workflow_binding(
                    current_state,
                    {
                        **current_state,
                        "decision_state": decision_state,
                    },
                )
                context.conversation_state = merged_state
                await self.runtime_service.upsert_conversation_context_state(
                    int(conversation_id),
                    dict(merged_state),
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

    async def _build_tool_result_ledger_entries(
        self,
        context: AgentContext,
        executed_calls: Sequence[ExecutedToolCall],
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for item in executed_calls:
            status = "authorization_required" if item.permission_required else ("succeeded" if item.success else "failed")
            fallback = item.error or ("tool call succeeded" if item.success else "tool call failed")
            summary = await self._tool_result_ledger_summary_text(item)
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
                    "summary": summary or self._tool_ledger_summary(item.observation_output, fallback=fallback),
                    "success": item.success,
                    "error": item.error,
                    "permission_required": item.permission_required,
                    "execution_time_ms": item.execution_time_ms,
                    "output_tokens_estimate": item.output_tokens_estimate,
                    "truncated": item.truncated,
                    "metadata": dict(item.metadata or {}) if isinstance(item.metadata, dict) else None,
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
            await self._build_tool_result_ledger_entries(context, executed_calls),
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

    async def _finalize_function_calling_iteration(
        self,
        context: AgentContext,
        *,
        content: str,
        reasoning: str,
        parsed_calls: Sequence[ParsedToolCall],
    ) -> tuple[List[Dict[str, Any]], bool]:
        events: List[Dict[str, Any]] = []
        answer_hint = self._extract_answer_text(content)
        raw_thought_text = ""

        if reasoning:
            raw_thought_text = reasoning

        if not raw_thought_text and content.strip():
            raw_thought_text = self._extract_think_text(content)

        if not raw_thought_text and content.strip() and parsed_calls:
            raw_thought_text = self._strip_think_content(content)
        thought_text = str(raw_thought_text or "").strip()
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
            guarded = self._maybe_guard_paper_skill_direct_answer(context, events=events)
            if guarded is not None:
                return guarded
            if not thought_text:
                events.append({"type": "thought", "data": "已完成问题分析，准备给出答案。"})
            answer = await self._ensure_citation_compliance(answer, context)
            context.final_answer = answer
            context.state = AgentState.DONE
            events.append({"type": "answer", "data": answer})
            return events, True
        return events, False

    async def _run_iteration_function_calling_once(
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
        return await self._finalize_function_calling_iteration(
            context,
            content=str(response.get("content") or ""),
            reasoning=str(response.get("reasoning") or "").strip(),
            parsed_calls=self._normalize_tool_calls(response.get("tool_calls") or []),
        )

    async def _run_iteration_function_calling(
        self,
        context: AgentContext,
        llm_messages: List[Dict[str, Any]],
        system_prompt: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        user_text = self._current_user_text(context)
        llm_messages = self._normalize_messages_for_function_calling(llm_messages)
        stream_method = getattr(self.llm, "chat_with_tools_stream", None)
        if not callable(stream_method):
            events, done = await self._run_iteration_function_calling_once(context, llm_messages, system_prompt)
            for event in events:
                yield event
            yield {"type": "_iteration_done", "data": {"done": done}}
            return

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        final_payload: Dict[str, Any] = {}
        streamed_content = False

        async for stream_event in stream_method(
            messages=llm_messages,
            tools=self._collect_llm_tool_schemas(user_text),
            system_prompt=system_prompt,
            temperature=settings.react_temperature,
            max_tokens=settings.llm_max_tokens,
            tool_choice=str(self._last_tool_selection.get("tool_choice") or "auto"),
        ):
            event_type = str(stream_event.get("type") or "")
            event_data = stream_event.get("data")
            if event_type == "content":
                chunk = str(event_data or "")
                if not chunk:
                    continue
                content_parts.append(chunk)
                streamed_content = True
                yield {"type": "content", "data": chunk}
                continue
            if event_type == "reasoning":
                reasoning_parts.append(str(event_data or ""))
                continue
            if event_type in {"tool_call", "tool_call_delta"}:
                continue
            if event_type == "done" and isinstance(event_data, dict):
                final_payload = dict(event_data)

        if final_payload:
            self._accumulate_usage(context, final_payload)
        content = str(final_payload.get("content") or "".join(content_parts))
        reasoning = str(final_payload.get("reasoning") or "".join(reasoning_parts)).strip()
        parsed_calls = self._normalize_tool_calls(final_payload.get("tool_calls") or [])

        events, done = await self._finalize_function_calling_iteration(
            context,
            content=content,
            reasoning=reasoning,
            parsed_calls=parsed_calls,
        )
        streamed_answer = "".join(content_parts)
        for event in events:
            if (
                str(event.get("type") or "") == "answer"
                and streamed_content
                and not parsed_calls
                and str(event.get("data") or "") == streamed_answer
            ):
                yield {
                    "type": "_answer_streamed",
                    "data": {"answer": str(event.get("data") or "")},
                }
                continue
            yield event
        yield {"type": "_iteration_done", "data": {"done": done}}

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
            display_thought = str(parsed["thought"] or "").strip()
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
            guarded = self._maybe_guard_paper_skill_direct_answer(context, events=events)
            if guarded is not None:
                return guarded
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

        scope_type, scope_id = self._memory_scope()

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
        prepared_prefetched_rag_messages: List[Dict[str, Any]] = []
        prepared_prefetched_rag_metadata: Dict[str, Any] = {}
        if isinstance(prepared_plan, dict):
            prepared_prefetched_rag_messages = [
                dict(item)
                for item in list(prepared_plan.get("prefetched_rag_messages") or [])
                if isinstance(item, dict)
            ]
            prepared_prefetched_rag_metadata = (
                dict(prepared_plan.get("prefetched_rag_metadata") or {})
                if isinstance(prepared_plan.get("prefetched_rag_metadata"), dict)
                else {}
            )
        context = AgentContext(
            messages=[self._sanitize_message_for_context(item) for item in messages],
            turn_id=self.runtime_context.turn_id,
            max_iterations=self.max_iterations,
            prefetched_rag_messages=prepared_prefetched_rag_messages,
            prefetched_rag_metadata=prepared_prefetched_rag_metadata,
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
                except (TypeError, ValueError):
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

        pre_turn_compacted = False
        try:
            pre_turn_compacted = await self._maybe_pre_turn_compact(context)
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.warning(f"[AgentCore] pre-turn compact skipped: {exc}")
            pre_turn_compacted = False
        if pre_turn_compacted:
            prepared_system_prompt = ""
            prepared_llm_messages = []

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
                if i == 1 and pre_turn_compacted:
                    thought_event = {
                        "type": "thought",
                        "data": "发送前已压缩较早上下文，并基于替代历史继续当前任务。",
                    }
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                context.state = AgentState.THINKING
                yield {"type": "thinking_start", "data": ""}
                yield {"type": "thinking", "data": "正在分析问题并规划下一步..."}
                forced_initial_rag_retrieval = False
                if i == 1 and self._should_force_initial_rag_retrieval(context):
                    forced_initial_rag_retrieval = True
                    await self._ensure_run_created(context)
                    self._mark_forced_rag_search_debug(context, planned=True, executed=True)
                    thought_event = {
                        "type": "thought",
                        "data": "本轮已启用 RAG 注入，先执行一次 knowledge_search 以载入知识库证据。",
                    }
                    self._append_step_from_event(context, thought_event)
                    context.persist_events.append(thought_event)
                    yield thought_event
                    forced_call = self._build_forced_rag_tool_call(context)
                    if forced_call is not None:
                        context.messages.append(
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": forced_call.call_id,
                                        "type": "function",
                                        "function": {
                                            "name": forced_call.name,
                                            "arguments": forced_call.arguments_raw,
                                        },
                                    }
                                ],
                            }
                        )
                        forced_executed = await self._execute_tool_calls(context, [forced_call])
                        for item in forced_executed:
                            for event in (item.action_event, item.observation_event):
                                self._append_step_from_event(context, event)
                                context.persist_events.append(event)
                                yield event
                            context.messages.append(item.tool_message)

                use_fc = self._supports_function_calling()

                if i == 1 and prepared_system_prompt and prepared_llm_messages and not forced_initial_rag_retrieval:
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

                emit_events_after_call = not use_fc
                if use_fc:
                    try:
                        events: List[Dict[str, Any]] = []
                        done = False
                        async for event in self._run_iteration_function_calling(context, llm_messages, system_prompt):
                            event_type = str(event.get("type") or "")
                            if event_type == "_iteration_done":
                                event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
                                done = bool(event_data.get("done"))
                                continue
                            if event_type == "_answer_streamed":
                                event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
                                answer = str(event_data.get("answer") or context.final_answer or "").strip()
                                if answer:
                                    answer_emitted = True
                                continue
                            events.append(event)
                            self._append_step_from_event(context, event)
                            if event_type in {"thought", "action", "observation", "answer", "content", "error"}:
                                context.persist_events.append(event)
                            if event_type == "answer":
                                answer_emitted = True
                            yield event
                    except Exception as exc:
                        logger.warning(f"[AgentCore] function-calling failed, fallback={exc}")
                        if bool(getattr(settings, "agent_function_calling_fallback_xml", True)):
                            events, done = await self._run_iteration_xml(context, llm_messages, system_prompt)
                            emit_events_after_call = True
                        else:
                            raise
                else:
                    events, done = await self._run_iteration_xml(context, llm_messages, system_prompt)

                if emit_events_after_call:
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
                background_stop_thought = self._maybe_stop_after_background_execution_started(context, events)
                if background_stop_thought:
                    thought_event = {"type": "thought", "data": background_stop_thought}
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
            reasoning_trace = (
                self._build_reasoning_trace_for_summary(context)
                if self._should_generate_reasoning_summary(context)
                else ""
            )
            final_thought = next(
                (s.content for s in reversed(context.steps) if s.step_type == "thought"),
                "",
            )
            await self._persist_memory(context)
            rag_metrics = self._build_rag_metrics(context)
            citation_index = self._build_citation_index(context.final_answer, context)
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
                    "reasoning_summary": None,
                    "reasoning_summary_pending": bool(reasoning_trace),
                    "_reasoning_trace": reasoning_trace or None,
                    "citation_index": citation_index or None,
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
