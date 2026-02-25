"""
Reader Compose Agent Core.

用于阶段二接管 composed 生成循环：
- 限制工具白名单
- 输出可追溯 tool_call_trace
- 为质量阈值停机提供统一入口
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.services.react_agent import AgentCore


class ReaderComposeAgentCore(AgentCore):
    """受控 Reader Agent Core（阶段二扩展点）"""

    ALLOWED_TOOLS = {
        "reader_layout_extract",
        "reader_structure_repair",
        "reader_sidebar_filter",
        "reader_asset_collect_pdf",
        "reader_asset_search_web",
        "reader_ui_plan_build",
        "reader_ui_plan_score",
        "reader_ui_plan_revise",
    }

    SYSTEM_PROMPT = (
        "你是论文阅读 UI 编排代理。"
        "只能调用白名单工具，不可输出任意代码。"
        "每一步都要保留 source_anchor 对齐。"
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
        self.allowed_tool_names = set(allowed_tool_names or self.ALLOWED_TOOLS)

    @classmethod
    def build_default_trace(cls) -> List[Dict[str, Any]]:
        """返回默认 trace 结构，便于前端统一展示。"""
        return []
