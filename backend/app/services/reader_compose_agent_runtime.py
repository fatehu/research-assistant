"""
Reader compose agent runtime.

Runs the configured reader multimodal agent for component assembly and returns validated ui_ops.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from app.config import settings
from app.services.dashscope_multimodal_service import DashScopeMultimodalService
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
    async def _build_reader_llm() -> Any:
        provider = str(
            getattr(settings, "reader_agent_provider", "")
            or getattr(settings, "default_llm_provider", "deepseek")
            or "deepseek"
        ).strip()
        llm = await get_llm_service(provider=provider)
        preferred_model = str(getattr(settings, "reader_agent_model", "") or "").strip()
        if preferred_model:
            llm.config = dict(getattr(llm, "config", {}) or {})
            llm.config["model"] = preferred_model
        return llm

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

    @staticmethod
    def _compact_omission_decisions(
        omission_decisions: Sequence[Mapping[str, Any]],
        limit: int = 24,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in list(omission_decisions or [])[:limit]:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                {
                    "decision": str(row.get("decision") or "").strip(),
                    "reason": str(row.get("reason") or "").strip()[:180],
                    "recoverable": bool(row.get("recoverable")),
                    "target_block_ids": [
                        str(item).strip()
                        for item in list(row.get("target_block_ids") or [])[:8]
                        if str(item).strip()
                    ],
                    "target_atom_ids": [
                        str(item).strip()
                        for item in list(row.get("target_atom_ids") or [])[:8]
                        if str(item).strip()
                    ],
                    "target_layout_ids": [
                        str(item).strip()
                        for item in list(row.get("target_layout_ids") or [])[:8]
                        if str(item).strip()
                    ],
                }
            )
        return rows

    @staticmethod
    def _compact_review_diagnostics(
        diagnostics: Sequence[Mapping[str, Any]],
        limit: int = 16,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for row in list(diagnostics or [])[:limit]:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                {
                    "code": str(row.get("code") or "").strip(),
                    "severity": str(row.get("severity") or "").strip(),
                    "message": str(row.get("message") or "").strip()[:180],
                    "component_ids": [
                        str(item).strip()
                        for item in list(row.get("component_ids") or [])[:8]
                        if str(item).strip()
                    ],
                    "meta": dict(row.get("meta") or {}),
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
        scheme_choice: Optional[Mapping[str, Any]] = None,
        decision_log: Optional[Sequence[str]] = None,
        omission_decisions: Optional[Sequence[Mapping[str, Any]]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
        review_context: Optional[Mapping[str, Any]] = None,
    ) -> str:
        compact_components = self._compact_components(ui_plan)
        compact_blocks = self._compact_blocks(page_structure_v3)
        compact_omissions = self._compact_omission_decisions(
            [row for row in list(omission_decisions or []) if isinstance(row, Mapping)]
        )
        compact_review_context = dict(review_context or {})
        compact_review = {
            "render_route": str(compact_review_context.get("render_route") or "").strip(),
            "has_render_image": bool(
                str(compact_review_context.get("render_image_url") or "").strip()
                or str(compact_review_context.get("render_image_path") or "").strip()
            ),
            "diagnostics": self._compact_review_diagnostics(
                [row for row in list(compact_review_context.get("diagnostics") or []) if isinstance(row, Mapping)]
            ),
        }
        compact_phase1 = {
            "input_mode": str((phase1_compact_input or {}).get("input_mode") or "").strip(),
            "scheme_catalog": [
                {
                    "scheme_id": str(item.get("scheme_id") or "").strip(),
                    "label": str(item.get("label") or "").strip(),
                }
                for item in list((phase1_compact_input or {}).get("scheme_catalog") or [])[:3]
                if isinstance(item, Mapping)
            ],
            "token_strategy": dict((phase1_compact_input or {}).get("token_strategy") or {}),
            "pdf_reference": {
                "has_page_image": bool(
                    str((((phase1_compact_input or {}).get("pdf_reference") or {}).get("page_image_url")) or "").strip()
                    or str((((phase1_compact_input or {}).get("pdf_reference") or {}).get("page_image_path")) or "").strip()
                ),
                "region_image_count": len(
                    [
                        item
                        for item in list((((phase1_compact_input or {}).get("pdf_reference") or {}).get("region_images")) or [])[:4]
                        if str(item).strip()
                    ]
                ),
            },
        }
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
            "9) Treat scheme_choice as the current layout contract; prefer local adjustments over scheme replacement.\n"
            "10) Respect omission decisions unless there is a strong, explicit reason to restore content.\n"
            "11) Use review diagnostics to focus on local fixes, not full rewrites.\n"
            "12) Do not remove a leading paragraph fragment solely because it looks like page carry-over or starts mid-sentence; preserve ambiguous continuation context unless the user explicitly asks to hide it.\n"
            f"page: {int(page)}\n"
            f"user_intent: {json.dumps(str(user_intent or 'standard'), ensure_ascii=False)}\n"
            f"scheme_choice: {json.dumps(dict(scheme_choice or {}), ensure_ascii=False)}\n"
            f"decision_log: {json.dumps([str(item).strip() for item in list(decision_log or []) if str(item).strip()][:16], ensure_ascii=False)}\n"
            f"omission_decisions: {json.dumps(compact_omissions, ensure_ascii=False)}\n"
            f"phase1_context: {json.dumps(compact_phase1, ensure_ascii=False)}\n"
            f"review_context: {json.dumps(compact_review, ensure_ascii=False)}\n"
            f"component_contract: {json.dumps(contract, ensure_ascii=False)}\n"
            f"existing_components: {json.dumps(compact_components, ensure_ascii=False)}\n"
            f"page_structure_v3_blocks: {json.dumps(compact_blocks, ensure_ascii=False)}\n"
            f"layout_advice_v3: {json.dumps(layout_advice, ensure_ascii=False)}\n"
            f"output_schema_example: {json.dumps(output_schema, ensure_ascii=False)}\n"
            "Return JSON now."
        )

    @staticmethod
    def _review_patch_schema() -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ui_ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "agent_summary": {"type": "string"},
            },
            "required": ["ui_ops"],
            "additionalProperties": True,
        }

    @staticmethod
    def _build_direct_review_content(
        *,
        prompt: str,
        review_context: Optional[Mapping[str, Any]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
        include_images: bool = True,
    ) -> List[Dict[str, Any]]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        if not include_images:
            return content

        def _candidate_image_urls() -> List[str]:
            seen: set[str] = set()
            values: List[str] = []
            candidate_urls = [
                str((review_context or {}).get("render_image_url") or "").strip(),
                str((((phase1_compact_input or {}).get("pdf_reference") or {}).get("page_image_url")) or "").strip(),
            ]
            for raw in candidate_urls:
                token = str(raw or "").strip()
                if not token or token in seen:
                    continue
                if token.startswith("http://") or token.startswith("https://"):
                    values.append(token)
                    seen.add(token)
                if len(values) >= 2:
                    break
            return values

        for token in _candidate_image_urls():
            content.append({"type": "image_url", "image_url": {"url": token}})
        return content

    @staticmethod
    def _collect_local_review_image_paths(
        *,
        review_context: Optional[Mapping[str, Any]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
    ) -> List[str]:
        return DashScopeMultimodalService.collect_local_file_uris(
            str((review_context or {}).get("render_image_path") or "").strip(),
            str((((phase1_compact_input or {}).get("pdf_reference") or {}).get("page_image_path")) or "").strip(),
            limit=2,
        )

    async def _run_dashscope_local_review_patch(
        self,
        *,
        prompt: str,
        review_context: Optional[Mapping[str, Any]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
        existing_component_ids: Sequence[str],
        valid_block_ids: set[str],
    ) -> Dict[str, Any]:
        image_paths = self._collect_local_review_image_paths(
            review_context=review_context,
            phase1_compact_input=phase1_compact_input,
        )
        if not image_paths:
            return {
                "used": False,
                "fallback_reason": "dashscope_local_image_missing",
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
            }

        result = await DashScopeMultimodalService.chat_json(
            api_key=str(getattr(settings, "aliyun_api_key", "") or "").strip(),
            base_url=str(getattr(settings, "aliyun_dashscope_api_base", "") or getattr(settings, "aliyun_base_url", "") or "").strip(),
            model=str(getattr(settings, "reader_agent_model", "qwen-3.5-plus") or "qwen-3.5-plus").strip(),
            system_prompt=(
                "You are a multimodal reader UI review agent.\n"
                "Return JSON only.\n"
                "Prefer local UI ops only. Do not rewrite the page wholesale.\n"
            ),
            user_prompt=(
                f"{prompt}\n"
                "Return exactly one JSON object with this shape:\n"
                f"{json.dumps(self._review_patch_schema(), ensure_ascii=False, separators=(',', ':'))}"
            ),
            image_paths=image_paths,
            max_tokens=max(900, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000) // 3),
            temperature=0.2,
        )
        parsed = dict(result.get("parsed") or {})
        if not parsed:
            return {
                "used": False,
                "fallback_reason": "dashscope_direct_review_not_json",
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
            }

        ui_ops, ui_ops_errors = self._contract.validate_and_sanitize_ui_ops(
            list(parsed.get("ui_ops") or []),
            existing_component_ids=list(existing_component_ids),
            valid_block_ids=valid_block_ids,
        )
        if ui_ops_errors:
            return {
                "used": False,
                "fallback_reason": "ui_ops_validation_failed",
                "validation_errors": ui_ops_errors,
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
                "agent_summary": str(parsed.get("agent_summary") or ""),
            }

        return {
            "used": True,
            "model": str(result.get("model") or getattr(settings, "reader_agent_model", "") or ""),
            "ui_ops": ui_ops,
            "agent_trace": [
                {
                    "type": "direct_review",
                    "data": {
                        "image_mode": "dashscope_local_file",
                        "tool_choice": "emit_review_patch_json_only",
                    },
                }
            ],
            "agent_tool_calls": [],
            "agent_summary": str(parsed.get("agent_summary") or ""),
            "fallback_reason": "",
        }

    async def _run_direct_review_patch(
        self,
        *,
        llm: Any,
        prompt: str,
        existing_component_ids: Sequence[str],
        valid_block_ids: set[str],
        review_context: Optional[Mapping[str, Any]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        local_image_paths = self._collect_local_review_image_paths(
            review_context=review_context,
            phase1_compact_input=phase1_compact_input,
        )
        if (
            str(getattr(settings, "reader_agent_provider", "") or "").strip() == "aliyun"
            and bool(local_image_paths)
            and DashScopeMultimodalService.is_available()
        ):
            try:
                local_result = await self._run_dashscope_local_review_patch(
                    prompt=prompt,
                    review_context=review_context,
                    phase1_compact_input=phase1_compact_input,
                    existing_component_ids=existing_component_ids,
                    valid_block_ids=valid_block_ids,
                )
                if bool(local_result.get("used")):
                    return local_result
            except Exception as exc:
                logger.warning(f"[ReaderComposeAgentRuntime] dashscope local review failed, falling back to compatible mode: {exc}")

        if not getattr(llm, "supports_function_calling", lambda: False)():
            return {
                "used": False,
                "fallback_reason": "direct_review_function_calling_unavailable",
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
            }

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "emit_review_patch",
                    "parameters": self._review_patch_schema(),
                },
            }
        ]
        system_prompt = (
            "You are a multimodal reader UI review agent.\n"
            "Return exactly one function call to emit_review_patch.\n"
            "Prefer local UI ops only. Do not rewrite the page wholesale.\n"
        )

        async def _request(include_images: bool) -> Any:
            return await llm.client.chat.completions.create(
                model=llm.config["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": self._build_direct_review_content(
                            prompt=prompt,
                            review_context=review_context,
                            phase1_compact_input=phase1_compact_input,
                            include_images=include_images,
                        ),
                    },
                ],
                tools=tools,
                tool_choice={"type": "function", "function": {"name": "emit_review_patch"}},
                temperature=0.2,
                max_tokens=max(900, int(getattr(settings, "reader_agent_max_tokens", 7000) or 7000) // 3),
            )

        response = None
        had_images = any(
            item.get("type") == "image_url"
            for item in self._build_direct_review_content(
                prompt="",
                review_context=review_context,
                phase1_compact_input=phase1_compact_input,
                include_images=True,
            )
        )
        try:
            response = await _request(include_images=True)
        except Exception as exc:
            if had_images:
                logger.warning(f"[ReaderComposeAgentRuntime] direct review with images failed, retrying text-only: {exc}")
                try:
                    response = await _request(include_images=False)
                except Exception as retry_exc:
                    logger.warning(f"[ReaderComposeAgentRuntime] direct review text-only retry failed: {retry_exc}")
                    return {
                        "used": False,
                        "fallback_reason": "direct_review_call_failed",
                        "ui_ops": [],
                        "agent_trace": [],
                        "agent_tool_calls": [],
                    }
            else:
                logger.warning(f"[ReaderComposeAgentRuntime] direct review failed: {exc}")
                return {
                    "used": False,
                    "fallback_reason": "direct_review_call_failed",
                    "ui_ops": [],
                    "agent_trace": [],
                    "agent_tool_calls": [],
                }

        msg = response.choices[0].message
        parsed: Optional[Dict[str, Any]] = None
        for tool_call in list(getattr(msg, "tool_calls", None) or []):
            fn = getattr(tool_call, "function", None)
            if fn and str(getattr(fn, "name", "") or "") == "emit_review_patch":
                parsed = self._extract_json_dict(str(getattr(fn, "arguments", "") or ""))
                if parsed:
                    break
        if not isinstance(parsed, dict):
            parsed = self._extract_json_dict(str(getattr(msg, "content", "") or ""))
        if not isinstance(parsed, dict):
            return {
                "used": False,
                "fallback_reason": "direct_review_not_json",
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
            }

        ui_ops, ui_ops_errors = self._contract.validate_and_sanitize_ui_ops(
            list(parsed.get("ui_ops") or []),
            existing_component_ids=list(existing_component_ids),
            valid_block_ids=valid_block_ids,
        )
        if ui_ops_errors:
            return {
                "used": False,
                "fallback_reason": "ui_ops_validation_failed",
                "validation_errors": ui_ops_errors,
                "ui_ops": [],
                "agent_trace": [],
                "agent_tool_calls": [],
                "agent_summary": str(parsed.get("agent_summary") or ""),
            }

        return {
            "used": True,
            "model": str(getattr(response, "model", "") or llm.config.get("model") or ""),
            "ui_ops": ui_ops,
            "agent_trace": [
                {
                    "type": "direct_review",
                    "data": {
                        "image_mode": "multimodal" if had_images else "text_only",
                        "tool_choice": "emit_review_patch",
                    },
                }
            ],
            "agent_tool_calls": [],
            "agent_summary": str(parsed.get("agent_summary") or ""),
            "fallback_reason": "",
        }

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
        scheme_choice: Optional[Mapping[str, Any]] = None,
        decision_log: Optional[Sequence[str]] = None,
        omission_decisions: Optional[Sequence[Mapping[str, Any]]] = None,
        phase1_compact_input: Optional[Mapping[str, Any]] = None,
        review_context: Optional[Mapping[str, Any]] = None,
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
        llm = await self._build_reader_llm()
        direct_prompt = self._build_agent_prompt(
            page=int(page),
            user_intent=user_intent,
            ui_plan=ui_plan,
            page_structure_v3=page_structure_v3,
            layout_advice_v3=layout_advice_v3,
            scheme_choice=scheme_choice,
            decision_log=decision_log,
            omission_decisions=omission_decisions,
            phase1_compact_input=phase1_compact_input,
            review_context=review_context,
        )
        direct_existing_component_ids = [
            str((row or {}).get("id") or "").strip()
            for row in list(ui_plan.get("components") or [])
            if isinstance(row, dict)
        ]
        direct_valid_block_ids = {
            str((row or {}).get("block_id") or "").strip()
            for row in list(page_structure_v3.get("block_groups") or [])
            if isinstance(row, dict) and str((row or {}).get("block_id") or "").strip()
        }
        direct_result = await self._run_direct_review_patch(
            llm=llm,
            prompt=direct_prompt,
            existing_component_ids=direct_existing_component_ids,
            valid_block_ids=direct_valid_block_ids,
            review_context=review_context,
            phase1_compact_input=phase1_compact_input,
        )
        if bool(direct_result.get("used")):
            return direct_result

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
        prompt = direct_prompt

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
            "model": str(((getattr(llm, "config", {}) or {}).get("model")) or getattr(settings, "reader_agent_model", "") or getattr(settings, "deepseek_model", "deepseek-chat")),
            "ui_ops": ui_ops,
            "agent_trace": agent_trace,
            "agent_tool_calls": agent_tool_calls,
            "agent_summary": str(parsed.get("agent_summary") or ""),
            "fallback_reason": "",
        }


def get_reader_compose_agent_runtime() -> ReaderComposeAgentRuntime:
    return ReaderComposeAgentRuntime()
