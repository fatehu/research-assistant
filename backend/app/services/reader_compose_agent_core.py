"""
Reader compose agent core.

Specialized AgentCore for component assembly:
- strict tool whitelist
- strict system prompt for JSON ui_ops output
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from app.services.react_agent import AgentCore


class ReaderComposeAgentCore(AgentCore):
    """Constrained AgentCore for reader compose assembly."""

    DEFAULT_ALLOWED_TOOLS = {
        "paper_read",
        "knowledge_search",
    }

    SYSTEM_PROMPT = (
        "You are a React component assembly agent for a literature reader.\n"
        "You can call tools only from the allowlist.\n"
        "Your final answer must be JSON only and should follow the requested ui_ops schema.\n"
        "Never fabricate evidence coordinates.\n"
        "Never rewrite scientific facts.\n"
    )

    def __init__(
        self,
        *,
        llm_service: Any,
        tool_registry: Any,
        allowed_tool_names: Optional[Sequence[str]] = None,
        max_iterations: Optional[int] = None,
        runtime_context: Optional[Any] = None,
    ) -> None:
        super().__init__(
            llm_service=llm_service,
            tool_registry=tool_registry,
            max_iterations=max_iterations,
            runtime_context=runtime_context,
        )
        resolved = [str(item).strip() for item in list(allowed_tool_names or []) if str(item).strip()]
        self.allowed_tool_names: Set[str] = set(resolved or list(self.DEFAULT_ALLOWED_TOOLS))

    def _build_system_prompt(self, messages: Optional[List[Dict[str, Any]]] = None) -> str:
        tools_desc = self.tools.get_tools_description(include_tool_names=self.allowed_tool_names)
        self._last_tool_selection = {
            "intent": "reader_compose",
            "selected_tools": sorted(list(self.allowed_tool_names)),
            "prompt_desc_tokens": 0,
            "schema_scope": "selected",
            "tool_selection_enabled": True,
        }
        return self._compose_profile_prompt_sections(
            f"{self.SYSTEM_PROMPT}\n\nTools:\n{tools_desc}",
            available_tools=sorted(list(self.allowed_tool_names)),
            include_generic_citation_policy=False,
        )

    def _collect_llm_tool_schemas(self, user_text: str) -> List[Dict[str, Any]]:
        return self.tools.list_tools(include_tool_names=self.allowed_tool_names, user_text=user_text)
