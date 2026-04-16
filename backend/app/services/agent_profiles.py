from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AgentProfile:
    key: str
    include_generic_citation_policy: bool = True
    include_user_chat_preferences: bool = False
    include_rag_overrides: bool = False
    include_channel_system_context: bool = False
    include_channel_tool_policy: bool = False
    load_conversation_artifacts: bool = False
    bind_runs_to_conversation: bool = False
    default_memory_scope_type: str = "user"


CHAT_AGENT_PROFILE = AgentProfile(
    key="chat",
    include_generic_citation_policy=True,
    include_user_chat_preferences=True,
    include_rag_overrides=True,
    include_channel_system_context=False,
    include_channel_tool_policy=False,
    load_conversation_artifacts=True,
    bind_runs_to_conversation=True,
    default_memory_scope_type="conversation",
)


CODELAB_AGENT_PROFILE = AgentProfile(
    key="codelab",
    include_generic_citation_policy=False,
    include_user_chat_preferences=False,
    include_rag_overrides=False,
    include_channel_system_context=True,
    include_channel_tool_policy=True,
    load_conversation_artifacts=False,
    bind_runs_to_conversation=False,
    default_memory_scope_type="notebook",
)


LITERATURE_AGENT_PROFILE = AgentProfile(
    key="literature",
    include_generic_citation_policy=False,
    include_user_chat_preferences=False,
    include_rag_overrides=False,
    include_channel_system_context=False,
    include_channel_tool_policy=False,
    load_conversation_artifacts=False,
    bind_runs_to_conversation=False,
    default_memory_scope_type="user",
)


DEFAULT_AGENT_PROFILE = AgentProfile(
    key="default",
    include_generic_citation_policy=True,
    include_user_chat_preferences=False,
    include_rag_overrides=False,
    include_channel_system_context=False,
    include_channel_tool_policy=False,
    load_conversation_artifacts=False,
    bind_runs_to_conversation=False,
    default_memory_scope_type="user",
)


def resolve_agent_profile(channel: str) -> AgentProfile:
    normalized = str(channel or "").strip().lower()
    if normalized == "chat":
        return CHAT_AGENT_PROFILE
    if normalized in {"codelab_agent", "notebook_agent"}:
        return CODELAB_AGENT_PROFILE
    if normalized in {"literature", "literature_agent"}:
        return LITERATURE_AGENT_PROFILE
    return DEFAULT_AGENT_PROFILE


def build_agent_channel_tool_policy_prompt(
    profile: AgentProfile,
    available_tools: Sequence[str],
) -> str:
    if profile.key != "codelab":
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
