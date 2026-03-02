"""
Reader compose agent runtime.

Runs DeepSeek agent for component assembly and returns validated ui_ops.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from loguru import logger

from app.config import settings
from app.services.llm_service import get_llm_service
from app.services.reader_component_contract_service import ReaderComponentContractService
from app.services.reader_compose_agent_core import ReaderComposeAgentCore
from app.services.reader_compose_agent_tools import (
    build_reader_compose_tool_registry,
    resolve_reader_agent_tool_whitelist,
)
from app.services.react_agent import AgentRuntimeContext


class ReaderComposeAgentRuntime:
    def __init__(self) -> None:
        self._contract = ReaderComponentContractService()

    @staticmethod
    def _extract_json_dict(raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    @staticmethod
    def _compact_components(ui_plan: Dict[str, Any], limit: int = 160) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for idx, node in enumerate(list(ui_plan.get("components") or [])[:limit]):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            if not node_id:
                continue
            props = dict(node.get("props") or {})
            text = str(
                props.get("text")
                or props.get("title")
                or props.get("caption")
                or props.get("question")
                or ""
            ).strip()
            rows.append(
                {
                    "id": node_id,
                    "type": str(node.get("type") or ""),
                    "index": idx,
                    "zone_type": str(node.get("zone_type") or ""),
                    "text_preview": text[:240],
                    "source_anchor_count": len(list(node.get("source_anchor_refs") or [])),
                }
            )
        return rows

    @staticmethod
    def _compact_blocks(page_structure_v3: Dict[str, Any], limit: int = 260) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        block_groups = [
            row for row in list(page_structure_v3.get("block_groups") or []) if isinstance(row, dict)
        ]
        for row in block_groups[:limit]:
            block_id = str(row.get("block_id") or "").strip()
            if not block_id:
                continue
            rows.append(
                {
                    "block_id": block_id,
                    "kind": str(row.get("kind") or "unknown"),
                    "zone_type": str(row.get("zone_type") or "unknown"),
                    "reading_order": int(row.get("reading_order") or 0),
                    "parent_block_id": str(row.get("parent_block_id") or ""),
                    "text": str(row.get("text") or "")[:420],
                }
            )
        return rows

    def _build_agent_prompt(
        self,
        *,
        page: int,
        user_intent: str,
        ui_plan: Dict[str, Any],
        page_structure_v3: Dict[str, Any],
        layout_advice_v3: Dict[str, Any],
    ) -> str:
        compact_components = self._compact_components(ui_plan)
        compact_blocks = self._compact_blocks(page_structure_v3)
        layout_advice = {
            "ordered_block_ids": [
                str(item).strip()
                for item in list(layout_advice_v3.get("ordered_block_ids") or [])[:220]
                if str(item).strip()
            ],
            "suggested_components": [
                item
                for item in list(layout_advice_v3.get("suggested_components") or [])[:220]
                if isinstance(item, dict)
            ],
            "grouping_hints": [
                item
                for item in list(layout_advice_v3.get("grouping_hints") or [])[:220]
                if isinstance(item, dict)
            ],
            "visual_hints": [
                item
                for item in list(layout_advice_v3.get("visual_hints") or [])[:220]
                if isinstance(item, dict)
            ],
        }
        contract = self._contract.component_schema_manifest()
        output_schema = {
            "ui_ops": [
                {
                    "op": "reorder_components|insert_component|update_component_props|remove_component",
                    "reason": "why",
                    "ordered_component_ids": ["existing_component_id"],
                    "component_id": "existing_component_id",
                    "props_patch": {"key": "value"},
                    "after_component_id": "existing_component_id_or_null",
                    "component": {
                        "id": "new_component_id",
                        "type": "must_be_registered_component",
                        "props": {"schema_validated": True},
                        "source_block_ids": ["existing_block_id"],
                    },
                }
            ],
            "agent_summary": "one paragraph",
        }
        return (
            "You are the final React-UI assembly agent for a literature reader.\n"
            "Goal: create UI operations that produce a readable, visually organized generative UI.\n"
            "Hard constraints:\n"
            "1) Output JSON only.\n"
            "2) Do not change scientific facts or body text content.\n"
            "3) Do not output evidence coordinates.\n"
            "4) All component types must be from component contract.\n"
            "5) source_block_ids must reference existing blocks.\n"
            "6) Prefer one paragraph block -> one ParagraphProse component.\n"
            "7) Keep heading hierarchy and caption/figure relationships.\n"
            "8) If no change needed, return ui_ops:[] with summary.\n"
            f"page: {int(page)}\n"
            f"user_intent: {json.dumps(str(user_intent or 'standard'), ensure_ascii=False)}\n"
            f"component_contract: {json.dumps(contract, ensure_ascii=False)}\n"
            f"existing_components: {json.dumps(compact_components, ensure_ascii=False)}\n"
            f"page_structure_v3_blocks: {json.dumps(compact_blocks, ensure_ascii=False)}\n"
            f"layout_advice_v3: {json.dumps(layout_advice, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(output_schema, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    async def run_component_assembly(
        self,
        *,
        user_id: int,
        page: int,
        user_intent: str,
        ui_plan: Dict[str, Any],
        page_structure_v3: Dict[str, Any],
        layout_advice_v3: Dict[str, Any],
        latency_budget_ms: int,
    ) -> Dict[str, Any]:
        if not bool(getattr(settings, "reader_agent_component_stream_enabled", True)):
            return {
                "used": False,
                "fallback_reason": "agent_component_stream_disabled",
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
            }

        allowed_tools = resolve_reader_agent_tool_whitelist()
        if not bool(getattr(settings, "reader_agent_tools_enabled", True)):
            allowed_tools = set()
        llm = await get_llm_service()
        registry = build_reader_compose_tool_registry(
            user_id=int(user_id),
            allowed_tool_names=sorted(list(allowed_tools)),
        )
        agent = ReaderComposeAgentCore(
            llm_service=llm,
            tool_registry=registry,
            max_iterations=max(2, min(int(getattr(settings, "reader_agent_max_iterations", 4) or 4), 8)),
            runtime_context=AgentRuntimeContext(
                user_id=int(user_id),
                channel="reader_compose",
                conversation_id=None,
                notebook_id=None,
            ),
            allowed_tool_names=sorted(list(allowed_tools)),
        )
        prompt = self._build_agent_prompt(
            page=int(page),
            user_intent=user_intent,
            ui_plan=ui_plan,
            page_structure_v3=page_structure_v3,
            layout_advice_v3=layout_advice_v3,
        )

        configured_timeout = float(getattr(settings, "reader_agent_assembly_timeout_seconds", 180) or 180)
        answer_text = ""
        agent_trace: List[Dict[str, Any]] = []
        agent_tool_calls: List[Dict[str, Any]] = []
        try:
            async def _run() -> None:
                nonlocal answer_text
                async for event in agent.run(
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                ):
                    et = str((event or {}).get("type") or "")
                    data = (event or {}).get("data")
                    if et in {"thought", "answer", "error"}:
                        agent_trace.append(
                            {
                                "type": et,
                                "data": data,
                            }
                        )
                    if et == "action" and isinstance(data, dict):
                        agent_tool_calls.append(
                            {
                                "tool": str(data.get("tool") or ""),
                                "input": data.get("input") or {},
                                "iteration": int(data.get("iteration") or 0),
                            }
                        )
                    if et == "answer":
                        answer_text = str(data or "")

            if configured_timeout <= 0:
                await _run()
            else:
                timeout_seconds = max(8.0, configured_timeout)
                await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return {
                "used": False,
                "fallback_reason": "agent_timeout",
                "ui_ops": [],
                "agent_trace": agent_trace,
                "agent_tool_calls": agent_tool_calls,
            }
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"[ReaderComposeAgentRuntime] run_component_assembly failed: {exc}")
            return {
                "used": False,
                "fallback_reason": "agent_exception",
                "ui_ops": [],
                "agent_trace": agent_trace,
                "agent_tool_calls": agent_tool_calls,
            }

        parsed = self._extract_json_dict(answer_text)
        if not isinstance(parsed, dict):
            return {
                "used": False,
                "fallback_reason": "agent_answer_not_json",
                "ui_ops": [],
                "agent_trace": agent_trace,
                "agent_tool_calls": agent_tool_calls,
            }

        existing_component_ids = [
            str((row or {}).get("id") or "").strip()
            for row in list(ui_plan.get("components") or [])
            if isinstance(row, dict)
        ]
        valid_block_ids = {
            str((row or {}).get("block_id") or "").strip()
            for row in list(page_structure_v3.get("block_groups") or [])
            if isinstance(row, dict) and str((row or {}).get("block_id") or "").strip()
        }
        ui_ops, ui_ops_errors = self._contract.validate_and_sanitize_ui_ops(
            list(parsed.get("ui_ops") or []),
            existing_component_ids=existing_component_ids,
            valid_block_ids=valid_block_ids,
        )
        if ui_ops_errors:
            return {
                "used": False,
                "fallback_reason": "ui_ops_validation_failed",
                "validation_errors": ui_ops_errors,
                "ui_ops": [],
                "agent_trace": agent_trace,
                "agent_tool_calls": agent_tool_calls,
                "agent_summary": str(parsed.get("agent_summary") or ""),
            }

        return {
            "used": True,
            "model": str(getattr(llm, "default_model", "") or getattr(settings, "deepseek_model", "deepseek-chat")),
            "ui_ops": ui_ops,
            "agent_trace": agent_trace,
            "agent_tool_calls": agent_tool_calls,
            "agent_summary": str(parsed.get("agent_summary") or ""),
            "fallback_reason": "",
        }


def get_reader_compose_agent_runtime() -> ReaderComposeAgentRuntime:
    return ReaderComposeAgentRuntime()
