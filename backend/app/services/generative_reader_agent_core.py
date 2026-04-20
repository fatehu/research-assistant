"""
Generative reader agent core.

Specialized ReAct agent for planning resource augmentation and interactive JS modules
on top of an existing reader compose payload.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from app.services.react_agent import AgentCore


class GenerativeReaderAgentCore(AgentCore):
    """Constrained AgentCore for generative reader planning."""

    DEFAULT_ALLOWED_TOOLS = {
        "paper_read",
        "knowledge_search",
        "web_search",
        "web_scrape",
    }

    SYSTEM_PROMPT = (
        "You are a generative reader planning agent.\n"
        "You work on top of an already-extracted scientific reading flow.\n"
        "Before you decide modules, infer the page story and reading path.\n"
        "You may use tools only from the allowlist.\n"
        "Your job is to produce a page brief, then plan resource modules, interaction modules, and JS widgets around the reading flow.\n"
        "Tools are optional: choose the minimum useful set needed to improve the page.\n"
        "You may use paper_read, knowledge_search, web_search, or web_scrape when they add real value.\n"
        "Do not call tools mechanically. If the page is already clear, keep the plan lightweight.\n"
        "Reader-native tools are often helpful for grounding, but you must decide what is necessary.\n"
        "When the page mentions named benchmarks, exams, institutions, training pathways, or evaluation frameworks unfamiliar to a general reader, it is usually valuable to add 1-3 authoritative public resources.\n"
        "If you keep a public resource link, use web_scrape when it materially improves confidence in the summary.\n"
        "Stay compact: produce at most 2 resource modules, 2 interaction modules, and 1 JS widget unless the page strongly demands more.\n"
        "Avoid repeated tool loops. Once you have enough grounding for the top targets, finalize the plan.\n"
        "Do not rewrite the paper body.\n"
        "Do not invent provenance, geometry, or scientific facts.\n"
        "Your final answer must be JSON only and must follow the requested generative reader plan schema.\n"
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
            "intent": "generative_reader",
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
