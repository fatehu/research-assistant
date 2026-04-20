from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
from typing import Any, Sequence

try:
    import httpx
except ImportError:  # pragma: no cover - optional runtime dependency
    httpx = None

try:
    from loguru import logger
except ImportError:  # pragma: no cover - optional dependency fallback
    import logging

    logger = logging.getLogger(__name__)

from app.config import settings
from app.services.reader_single_agent_controller import parse_json_dict_from_model_text

from .contracts import (
    PdfBBox,
    PdfHybridModelAttempt,
    PdfHybridParsedBlock,
    PdfHybridParsedPage,
    PdfHybridTriageResult,
    PdfResolvedLine,
    PdfResolvedPage,
)
_VL_TEMPERATURE = 0.0
_VL_TOP_P = 0.8
_VL_TOP_K = 20
_VL_PRESENCE_PENALTY = 1.5
_DEFAULT_MODEL_CHAIN = ("qwen3.5:0.8b", "qwen3.5:2b-q4_K_M", "qwen3.5:4b-q4_K_M")
_PROMPT_KIND_WHITELIST = sorted(
    ["heading", "paragraph", "list_item", "caption", "table", "equation", "figure_meta", "footnote", "unknown"]
)
_PROMPT_ZONE_WHITELIST = sorted(["main", "side", "figure", "table", "footer", "header", "unknown"])
from .hybrid_backend_transformer import LocalPdfHybridBackendTransformer


class LocalOllamaQwenVlPageParser:
    """Page-level structural parser that routes selected pages to local Ollama VL."""

    def __init__(
        self,
        *,
        timeout_seconds: int | None = None,
        render_dpi: int | None = None,
        max_image_side: int | None = None,
        ocr_max_image_side: int | None = None,
        max_lines_per_page: int | None = None,
    ) -> None:
        self._last_invoke_error = ""
        self._last_invoke_protocol = ""
        self._last_raw_response_preview = ""
        self._transformer = LocalPdfHybridBackendTransformer()
        self._timeout_seconds = max(
            10,
            int(timeout_seconds or getattr(settings, "local_structured_pdf_hybrid_timeout_seconds", 90) or 90),
        )
        self._render_dpi = max(
            96,
            int(render_dpi or getattr(settings, "local_structured_pdf_hybrid_render_dpi", 144) or 144),
        )
        self._max_image_side = max(
            768,
            int(max_image_side or getattr(settings, "local_structured_pdf_hybrid_max_image_side", 1600) or 1600),
        )
        self._ocr_max_image_side = max(
            768,
            int(
                ocr_max_image_side
                or getattr(settings, "local_structured_pdf_hybrid_ocr_max_image_side", 1024)
                or 1024
            ),
        )
        self._max_lines_per_page = max(
            12,
            int(max_lines_per_page or getattr(settings, "local_structured_pdf_hybrid_max_lines_per_page", 80) or 80),
        )

    def is_configured(self) -> bool:
        return bool(self._resolved_model().strip()) and bool(str(getattr(settings, "ollama_base_url", "") or "").strip())

    async def describe_picture_region(
        self,
        *,
        pdf_path: str,
        page: int,
        bbox: PdfBBox,
        prompt: str | None = None,
    ) -> tuple[str, str]:
        models = self._resolved_models()
        if not models:
            return "", ""
        image_b64 = await asyncio.to_thread(
            self._render_region_image_base64,
            pdf_path,
            int(page),
            bbox,
            min(self._max_image_side, 1024),
        )
        if not image_b64:
            return "", ""
        user_prompt = self._build_picture_description_prompt(prompt=prompt)
        for model in models:
            text = await self._invoke_ollama_text(
                model=model,
                user_prompt=user_prompt,
                image_b64=image_b64,
                max_output_tokens=220,
            )
            normalized = self._normalize_picture_description(text)
            if normalized:
                return normalized, model
        return "", ""

    async def describe_formula_region(
        self,
        *,
        pdf_path: str,
        page: int,
        bbox: PdfBBox,
    ) -> tuple[str, str]:
        models = self._resolved_models()
        if not models:
            return "", ""
        image_b64 = await asyncio.to_thread(
            self._render_region_image_base64,
            pdf_path,
            int(page),
            bbox,
            min(self._max_image_side, 768),
        )
        if not image_b64:
            return "", ""
        user_prompt = (
            "You are a PDF formula-enrichment backend. "
            "Read the formula in the image and return only the formula text, preferably valid LaTeX when clear. "
            "Do not explain. Do not use markdown fences."
        )
        for model in models:
            text = await self._invoke_ollama_text(
                model=model,
                user_prompt=user_prompt,
                image_b64=image_b64,
                max_output_tokens=180,
            )
            normalized = self._normalize_formula_text(text)
            if normalized:
                return normalized, model
        return "", ""

    async def transcribe_page_text(
        self,
        *,
        pdf_path: str,
        page: int,
        page_type: str | None = None,
        prompt: str | None = None,
    ) -> tuple[str, str]:
        models = self._resolved_models()
        if not models:
            return "", ""
        normalized_page_type = str(page_type or "").strip().lower()
        max_image_side = min(self._max_image_side, self._ocr_max_image_side)
        if normalized_page_type == "visual_or_scanned":
            max_image_side = min(max_image_side, 768)
        image_b64 = await asyncio.to_thread(
            self._render_page_image_base64,
            pdf_path,
            int(page),
            max_image_side,
        )
        if not image_b64:
            return "", ""
        user_prompt = str(prompt or "").strip() or (
            "You are an OCR backend for PDF pages. "
            "Read all visible text in natural reading order and return plain text only. "
            "Preserve paragraph breaks when clear. "
            "Do not return JSON. Do not explain. Do not use markdown fences."
        )
        model = models[0]
        text = await self._invoke_ollama_text(
            model=model,
            user_prompt=user_prompt,
            image_b64=image_b64,
            max_output_tokens=1600,
        )
        normalized = self._normalize_ocr_text(text)
        if normalized:
            return normalized, model
        return "", ""

    async def parse_page(
        self,
        *,
        pdf_path: str,
        resolved_page: PdfResolvedPage,
        triage_result: PdfHybridTriageResult | None = None,
        force_ocr: bool = False,
        task_hints: dict[str, Any] | None = None,
    ) -> PdfHybridParsedPage:
        models = self._resolved_models()
        page_number = int(getattr(resolved_page, "page", 0) or 0)
        if not models:
            return PdfHybridParsedPage(
                page=page_number,
                model="",
                attempted_models=[],
                protocol="",
                raw_response_preview="",
                error="ollama_model_missing",
            )

        line_rows = self._select_line_rows(resolved_page=resolved_page)
        if not line_rows:
            return PdfHybridParsedPage(
                page=page_number,
                model=models[0],
                attempted_models=list(models),
                protocol="",
                raw_response_preview="",
                error="resolved_page_empty",
            )

        image_b64 = await asyncio.to_thread(
            self._render_page_image_base64,
            pdf_path,
            page_number,
            self._max_image_side_for_page(triage_result=triage_result),
        )
        if not image_b64:
            return PdfHybridParsedPage(
                page=page_number,
                model=models[0],
                attempted_models=list(models),
                protocol="",
                raw_response_preview="",
                error="page_image_unavailable",
            )

        prompt_payload = self._build_prompt_payload(
            resolved_page=resolved_page,
            line_rows=line_rows,
            triage_result=triage_result,
            force_ocr=force_ocr,
            task_hints=task_hints,
        )
        attempted_models: list[str] = []
        attempts: list[PdfHybridModelAttempt] = []
        best_failure: PdfHybridParsedPage | None = None
        for model in models:
            attempted_models.append(model)
            validated, retry_used, retry_count, error = await self._call_with_retry(
                prompt_payload=prompt_payload,
                image_b64=image_b64,
                model=model,
            )
            if not isinstance(validated, dict):
                attempts.append(
                    PdfHybridModelAttempt(
                        model=model,
                        accepted=False,
                        used=False,
                        protocol=self._last_invoke_protocol,
                        retry_used=retry_used,
                        retry_count=retry_count,
                        reason="backend_parse_failed",
                        error=error or "ollama_parse_failed",
                        raw_response_preview=self._last_raw_response_preview,
                    )
                )
                best_failure = PdfHybridParsedPage(
                    page=page_number,
                    model=model,
                    attempted_models=list(attempted_models),
                    attempts=list(attempts),
                    protocol=self._last_invoke_protocol,
                    raw_response_preview=self._last_raw_response_preview,
                    retry_used=retry_used,
                    retry_count=retry_count,
                    error=error or "ollama_parse_failed",
                )
                continue

            blocks = self._materialize_blocks(
                payload=validated,
                resolved_page=resolved_page,
                line_rows=line_rows,
            )
            candidate = PdfHybridParsedPage(
                page=page_number,
                model=model,
                page_role=str(validated.get("page_role") or "unknown").strip() or "unknown",
                blocks=blocks,
                notes=[
                    str(item).strip()
                    for item in list(validated.get("notes") or [])[:24]
                    if str(item).strip()
                ],
                attempted_models=list(attempted_models),
                attempts=[],
                protocol=self._last_invoke_protocol,
                raw_response_preview=self._last_raw_response_preview,
                used=True,
                retry_used=retry_used,
                retry_count=retry_count,
                error="",
            )
            accepted, accept_reason = self._should_accept_result(
                candidate=candidate,
                resolved_page=resolved_page,
                triage_result=triage_result,
            )
            attempts.append(
                PdfHybridModelAttempt(
                    model=model,
                    accepted=accepted,
                    used=accepted,
                    page_role=candidate.page_role,
                    block_count=len(candidate.blocks),
                    anchored_block_count=sum(1 for block in candidate.blocks if list(block.source_line_ids or [])),
                    unanchored_block_count=sum(1 for block in candidate.blocks if not list(block.source_line_ids or [])),
                    protocol=candidate.protocol,
                    retry_used=candidate.retry_used,
                    retry_count=candidate.retry_count,
                    reason=accept_reason,
                    raw_response_preview=candidate.raw_response_preview,
                )
            )
            if accepted:
                candidate.attempts = list(attempts)
                return candidate
            best_failure = PdfHybridParsedPage(
                page=page_number,
                model=model,
                page_role=candidate.page_role,
                blocks=candidate.blocks,
                notes=list(candidate.notes),
                attempted_models=list(attempted_models),
                attempts=list(attempts),
                protocol=candidate.protocol,
                raw_response_preview=candidate.raw_response_preview,
                used=False,
                retry_used=candidate.retry_used,
                retry_count=candidate.retry_count,
                error=f"backend_result_insufficient:{accept_reason}",
            )

        if best_failure is not None:
            return best_failure
        return PdfHybridParsedPage(
            page=page_number,
            model=models[0],
            attempted_models=list(attempted_models or models),
            attempts=list(attempts),
            protocol="",
            raw_response_preview="",
            error="ollama_parse_failed",
        )

    async def parse_pages(
        self,
        *,
        pdf_path: str,
        resolved_pages: Sequence[PdfResolvedPage],
        triage_results: Sequence[PdfHybridTriageResult | None] | None = None,
        force_ocr: bool = False,
        task_hints: dict[str, Any] | None = None,
    ) -> list[PdfHybridParsedPage]:
        resolved_page_list = [
            page
            for page in list(resolved_pages or [])
            if page is not None
        ]
        if not resolved_page_list:
            return []

        triage_by_page = {
            int(getattr(result, "page", 0) or 0): result
            for result in list(triage_results or [])
            if result is not None
        }
        ordered_pages = sorted(
            resolved_page_list,
            key=lambda item: int(getattr(item, "page", 0) or 0),
        )
        models = self._resolved_models()
        if not models:
            return [
                PdfHybridParsedPage(
                    page=int(getattr(page, "page", 0) or 0),
                    model="",
                    attempted_models=[],
                    protocol="",
                    raw_response_preview="",
                    error="ollama_model_missing",
                )
                for page in ordered_pages
            ]

        page_records: list[dict[str, Any]] = []
        render_tasks = []
        for resolved_page in ordered_pages:
            page_number = int(getattr(resolved_page, "page", 0) or 0)
            triage_result = triage_by_page.get(page_number)
            prompt_payload = self._build_prompt_payload(
                resolved_page=resolved_page,
                line_rows=self._select_line_rows(resolved_page=resolved_page),
                triage_result=triage_result,
                force_ocr=force_ocr,
                task_hints=task_hints,
            )
            page_records.append(
                {
                    "page": page_number,
                    "resolved_page": resolved_page,
                    "triage_result": triage_result,
                    "prompt_payload": prompt_payload,
                }
            )
            render_tasks.append(
                asyncio.to_thread(
                    self._render_page_image_base64,
                    pdf_path,
                    page_number,
                    self._max_image_side_for_page(triage_result=triage_result),
                )
            )

        rendered_images = await asyncio.gather(*render_tasks, return_exceptions=True)
        request_records: list[dict[str, Any]] = []
        parsed_by_page: dict[int, PdfHybridParsedPage] = {}
        for record, rendered in zip(page_records, rendered_images):
            page_number = int(record["page"])
            if isinstance(rendered, Exception) or not str(rendered or "").strip():
                parsed_by_page[page_number] = PdfHybridParsedPage(
                    page=page_number,
                    model=models[0],
                    attempted_models=[],
                    protocol="",
                    raw_response_preview="",
                    error="page_image_unavailable",
                )
                continue
            record["image_b64"] = str(rendered)
            request_records.append(record)

        if request_records:
            for batch_records in self._group_batch_request_records(request_records=request_records):
                (
                    validated,
                    error,
                    preview,
                    batch_model,
                    attempted_models,
                    protocol,
                ) = await self._invoke_batch_with_model_fallback(
                    models=models,
                    request_records=batch_records,
                )

                if isinstance(validated, dict):
                    validated_by_page = self._normalize_batch_page_payloads(
                        validated=validated,
                        request_records=batch_records,
                    )
                    for record in batch_records:
                        page_number = int(record["page"])
                        triage_result = record["triage_result"]
                        page_payload = validated_by_page.get(page_number)
                        if not isinstance(page_payload, dict):
                            parsed_by_page[page_number] = self._build_failed_page_result(
                                page=page_number,
                                model=batch_model,
                                prompt_payload=record["prompt_payload"],
                                triage_result=triage_result,
                                reason="batch_page_missing",
                                protocol=protocol,
                                raw_response_preview=preview,
                                attempted_models=attempted_models,
                            )
                            continue

                        normalized = self._transformer.transform_payload(
                            payload=page_payload,
                            prompt_payload=record["prompt_payload"],
                        )
                        if normalized is None:
                            parsed_by_page[page_number] = self._build_failed_page_result(
                                page=page_number,
                                model=batch_model,
                                prompt_payload=record["prompt_payload"],
                                triage_result=triage_result,
                                reason="schema_validation_failed",
                                protocol=protocol,
                                raw_response_preview=preview,
                                attempted_models=attempted_models,
                            )
                            continue

                        parsed_by_page[page_number] = self._build_page_result_from_validated_payload(
                            payload=normalized,
                            resolved_page=record["resolved_page"],
                            triage_result=triage_result,
                            model=batch_model,
                            protocol=protocol,
                            raw_response_preview=preview,
                            attempted_models=attempted_models,
                        )
                    continue

                for record in batch_records:
                    page_number = int(record["page"])
                    parsed_by_page[page_number] = self._build_failed_page_result(
                        page=page_number,
                        model=batch_model,
                        prompt_payload=record["prompt_payload"],
                        triage_result=record["triage_result"],
                        reason=error or "ollama_batch_parse_failed",
                        protocol=protocol,
                        raw_response_preview=preview,
                        attempted_models=attempted_models,
                    )

        return [parsed_by_page[int(getattr(page, "page", 0) or 0)] for page in ordered_pages]

    def _group_batch_request_records(self, *, request_records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        grouped_records: dict[bool, list[dict[str, Any]]] = {}
        group_order: list[bool] = []
        for record in request_records:
            use_response_format = self._should_use_response_format(prompt_payload=dict(record["prompt_payload"]))
            if use_response_format not in grouped_records:
                grouped_records[use_response_format] = []
                group_order.append(use_response_format)
            grouped_records[use_response_format].append(record)
        return [grouped_records[key] for key in group_order if grouped_records.get(key)]

    async def _invoke_batch_with_model_fallback(
        self,
        *,
        models: Sequence[str],
        request_records: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str, str, str, list[str], str]:
        batch_prompt_payload = self._build_batch_prompt_payload(page_records=request_records)
        prompt_payloads = [dict(record["prompt_payload"]) for record in request_records]
        user_prompt = self._build_batch_prompt_text(
            batch_prompt_payload=batch_prompt_payload,
            retry_hint="",
        )
        use_response_format = self._should_use_response_format_for_batch(page_payloads=prompt_payloads)
        max_output_tokens = self._max_output_tokens_for_batch(page_payloads=prompt_payloads)
        image_b64s = [str(record["image_b64"]) for record in request_records]

        attempted_models: list[str] = []
        final_error = "ollama_batch_parse_failed"
        final_preview = ""
        final_protocol = ""

        for model in list(models or []):
            attempted_models.append(model)
            validated, error, preview = await self._invoke_ollama_batch_json(
                model=model,
                user_prompt=user_prompt,
                image_b64s=image_b64s,
                use_response_format=use_response_format,
                max_output_tokens=max_output_tokens,
            )
            final_protocol = self._last_invoke_protocol
            final_preview = preview or self._last_raw_response_preview
            if isinstance(validated, dict):
                return validated, "", final_preview, model, list(attempted_models), final_protocol
            if str(error or "").strip():
                final_error = str(error)

        final_model = attempted_models[-1] if attempted_models else ""
        return None, final_error, final_preview, final_model, attempted_models, final_protocol

    @staticmethod
    def _normalize_batch_page_payloads(
        *,
        validated: dict[str, Any],
        request_records: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        pages_payload = validated.get("pages")
        normalized: dict[int, dict[str, Any]] = {}
        if isinstance(pages_payload, list):
            for item in pages_payload:
                if not isinstance(item, dict):
                    continue
                page_number = int(item.get("page") or 0)
                if page_number <= 0 and len(request_records) == 1:
                    page_number = int(request_records[0].get("page") or 0)
                if page_number > 0:
                    normalized[page_number] = item
            if normalized:
                return normalized

        if len(request_records) == 1 and LocalOllamaQwenVlPageParser._looks_like_single_page_payload(validated):
            page_number = int(request_records[0].get("page") or 0)
            if page_number > 0:
                payload = dict(validated)
                payload["page"] = int(payload.get("page") or page_number)
                normalized[page_number] = payload
        return normalized

    @staticmethod
    def _looks_like_single_page_payload(payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        if any(key in payload for key in ("texts", "tables", "pictures", "elements", "blocks")):
            return True
        return bool(payload.get("page_role") or payload.get("document_role") or payload.get("notes"))

    def _build_batch_prompt_payload(self, *, page_records: list[dict[str, Any]]) -> dict[str, Any]:
        return {"pages": [self._sanitize_batch_prompt_payload(record["prompt_payload"]) for record in page_records]}

    @staticmethod
    def _sanitize_batch_prompt_payload(prompt_payload: dict[str, Any]) -> dict[str, Any]:
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()
        task_hints = prompt_payload.get("task_hints") if isinstance(prompt_payload, dict) else {}
        source_rows = [
            {
                "text": str(item.get("text") or ""),
                "band": str(item.get("band") or ""),
                "column_id": str(item.get("column_id") or ""),
                "region": str(item.get("region") or ""),
                "bbox": dict(item.get("bbox") or {}),
            }
            for item in list(prompt_payload.get("source_rows") or [])
            if isinstance(item, dict)
        ]
        if page_type == "visual_or_scanned":
            source_rows = []
        return {
            "page": int(prompt_payload.get("page") or 0),
            "page_width": float(prompt_payload.get("page_width") or 0.0),
            "page_height": float(prompt_payload.get("page_height") or 0.0),
            "column_count": int(prompt_payload.get("column_count") or 1),
            "triage": dict(triage) if isinstance(triage, dict) else {},
            "task_hints": dict(task_hints) if isinstance(task_hints, dict) else {},
            "source_rows": source_rows,
            "kind_whitelist": list(_PROMPT_KIND_WHITELIST),
            "zone_whitelist": list(_PROMPT_ZONE_WHITELIST),
        }

    @staticmethod
    def _build_batch_prompt_text(*, batch_prompt_payload: dict[str, Any], retry_hint: str) -> str:
        page_payloads = [
            item
            for item in list(batch_prompt_payload.get("pages") or [])
            if isinstance(item, dict)
        ]
        page_count = len(page_payloads)
        visual_page_count = sum(
            1
            for item in page_payloads
            if str(((item.get("triage") or {}).get("page_type") or "")).strip().lower() == "visual_or_scanned"
        )
        page_briefs = [
            LocalOllamaQwenVlPageParser._format_batch_page_brief(item)
            for item in page_payloads
        ]
        ocr_requested = any(bool((item.get("task_hints") or {}).get("force_ocr")) for item in page_payloads)
        formula_requested = any(bool((item.get("task_hints") or {}).get("enrich_formula")) for item in page_payloads)
        picture_requested = any(bool((item.get("task_hints") or {}).get("enrich_picture_description")) for item in page_payloads)
        schema_example = (
            '{'
            '"pages":['
            '{"page":1,"page_role":"body","texts":[{"label":"section_header","text":"Introduction","bbox":{"x0":80,"top":96,"x1":220,"bottom":116},"meta":{"level":1}}],"notes":[]},'
            '{"page":2,"page_role":"poster","texts":[{"label":"text","text":"Recovered OCR paragraph","bbox":{"x0":96,"top":190,"x1":530,"bottom":280}}],"pictures":[{"bbox":{"x0":72,"top":330,"x1":540,"bottom":620},"annotations":[{"kind":"description","text":"chart"}]}],"notes":[]}'
            ']}'
        )
        prompt = "".join(
            [
                "You are a PDF hybrid backend. Process multiple pages in one response. Return JSON only.\n",
                "Return a top-level object containing an array of page results. Preserve the input page numbers and page order.\n",
                "Use the page briefs below as compact hints only; do not echo their fields back in the output.\n",
                "Model each page in a docling-like loose structure. Preferred top-level fields are texts, tables, pictures, notes.\n",
                "For text regions, use label/text/bbox.\n",
                "If OCR is requested, prioritize recovering all visible text from the image and preserve it as coarse text regions.\n"
                if ocr_requested
                else "",
                "If formula enrichment is requested, emit formulas using label=formula and provide formula text conservatively when visible.\n"
                if formula_requested
                else "",
                "If picture description is requested, include pictures entries with description annotations for notable non-text visuals.\n"
                if picture_requested
                else "",
                "Do not invent content.\n",
                "Do not wrap the JSON in markdown fences.\n",
                "Allowed labels: section_header, text, caption, footnote, list_item, formula, page_header, page_footer, table, picture, unknown.\n",
                f"page_count: {page_count}\n",
                f"visual_page_count: {visual_page_count}\n",
                "page briefs:\n",
                "\n".join(f"- {brief}" for brief in page_briefs),
                "\n" if page_briefs else "",
                f"output_schema_example: {schema_example}\n",
                "Return JSON now.",
            ]
        )
        if str(retry_hint or "").strip():
            prompt += f"\nRetry hint: {json.dumps(str(retry_hint), ensure_ascii=False)}"
        return prompt

    @staticmethod
    def _format_batch_page_brief(page_payload: dict[str, Any]) -> str:
        triage = page_payload.get("triage") if isinstance(page_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower() or "unknown"
        task_hints = page_payload.get("task_hints") if isinstance(page_payload, dict) else {}
        requested = ",".join(
            token
            for token, enabled in (
                ("ocr", bool((task_hints or {}).get("force_ocr"))),
                ("formula", bool((task_hints or {}).get("enrich_formula"))),
                ("picture", bool((task_hints or {}).get("enrich_picture_description"))),
            )
            if enabled
        ) or "none"
        page = int(page_payload.get("page") or 0)
        source_rows = [
            item
            for item in list(page_payload.get("source_rows") or [])
            if isinstance(item, dict)
        ]
        visual_or_scanned = page_type == "visual_or_scanned"
        if visual_or_scanned:
            return f"page {page} | role_hint={page_type} | requested={requested} | anchors=0 | image_first=true"
        line_preview = [
            str(item.get("text") or "").strip()
            for item in source_rows[:4]
            if str(item.get("text") or "").strip()
        ]
        preview_text = "; ".join(line_preview) if line_preview else "no_text_anchors"
        return f"page {page} | role_hint={page_type} | requested={requested} | anchors={len(source_rows)} | first_lines={preview_text}"

    def _build_page_result_from_validated_payload(
        self,
        *,
        payload: dict[str, Any],
        resolved_page: PdfResolvedPage,
        triage_result: PdfHybridTriageResult | None,
        model: str,
        protocol: str,
        raw_response_preview: str,
        attempted_models: list[str],
    ) -> PdfHybridParsedPage:
        page_number = int(getattr(resolved_page, "page", 0) or 0)
        line_rows = self._select_line_rows(resolved_page=resolved_page)
        blocks = self._materialize_blocks(
            payload=payload,
            resolved_page=resolved_page,
            line_rows=line_rows,
        )
        candidate = PdfHybridParsedPage(
            page=page_number,
            model=model,
            page_role=str(payload.get("page_role") or "unknown").strip() or "unknown",
            blocks=blocks,
            notes=[
                str(item).strip()
                for item in list(payload.get("notes") or [])[:24]
                if str(item).strip()
            ],
            attempted_models=list(attempted_models),
            attempts=[],
            protocol=protocol,
            raw_response_preview=raw_response_preview,
            used=True,
            retry_used=False,
            retry_count=0,
            error="",
        )
        accepted, accept_reason = self._should_accept_result(
            candidate=candidate,
            resolved_page=resolved_page,
            triage_result=triage_result,
        )
        attempt = PdfHybridModelAttempt(
            model=model,
            accepted=accepted,
            used=accepted,
            page_role=candidate.page_role,
            block_count=len(candidate.blocks),
            anchored_block_count=sum(1 for block in candidate.blocks if list(block.source_line_ids or [])),
            unanchored_block_count=sum(1 for block in candidate.blocks if not list(block.source_line_ids or [])),
            protocol=candidate.protocol,
            retry_used=False,
            retry_count=0,
            reason=accept_reason,
            raw_response_preview=candidate.raw_response_preview,
        )
        if accepted:
            candidate.attempts = [attempt]
            return candidate
        return PdfHybridParsedPage(
            page=page_number,
            model=model,
            page_role=candidate.page_role,
            blocks=candidate.blocks,
            notes=list(candidate.notes),
            attempted_models=list(attempted_models),
            attempts=[attempt],
            protocol=protocol,
            raw_response_preview=raw_response_preview,
            used=False,
            retry_used=False,
            retry_count=0,
            error=f"backend_result_insufficient:{accept_reason}",
        )

    def _build_failed_page_result(
        self,
        *,
        page: int,
        model: str,
        prompt_payload: dict[str, Any],
        triage_result: PdfHybridTriageResult | None,
        reason: str,
        protocol: str,
        raw_response_preview: str,
        attempted_models: list[str],
    ) -> PdfHybridParsedPage:
        page_role = str((prompt_payload.get("triage") or {}).get("page_type") or "unknown").strip() or "unknown"
        if triage_result is not None:
            page_role = str(getattr(triage_result, "page_type", "") or page_role).strip() or page_role
        return PdfHybridParsedPage(
            page=page,
            model=model,
            page_role=page_role,
            attempted_models=list(attempted_models),
            attempts=[
                PdfHybridModelAttempt(
                    model=model,
                    accepted=False,
                    used=False,
                    page_role=page_role,
                    block_count=0,
                    anchored_block_count=0,
                    unanchored_block_count=0,
                    protocol=protocol,
                    retry_used=False,
                    retry_count=0,
                    reason=reason,
                    raw_response_preview=raw_response_preview,
                )
            ],
            protocol=protocol,
            raw_response_preview=raw_response_preview,
            used=False,
            retry_used=False,
            retry_count=0,
            error=reason,
        )

    @staticmethod
    def _max_output_tokens_for_batch(*, page_payloads: list[dict[str, Any]]) -> int:
        total = 512
        for item in page_payloads:
            triage = item.get("triage") if isinstance(item, dict) else {}
            page_type = str((triage or {}).get("page_type") or "").strip().lower()
            if page_type == "visual_or_scanned":
                total += 3200
            elif page_type in {"mixed_layout", "formula_or_display_heavy", "front_matter_heavy"}:
                total += 2400
            else:
                total += 1800
        return min(8192, max(256, total))

    @staticmethod
    def _should_use_response_format_for_batch(*, page_payloads: list[dict[str, Any]]) -> bool:
        for item in page_payloads:
            triage = item.get("triage") if isinstance(item, dict) else {}
            page_type = str((triage or {}).get("page_type") or "").strip().lower()
            if page_type == "visual_or_scanned":
                return False
        return True

    async def _invoke_ollama_batch_json(
        self,
        *,
        model: str,
        user_prompt: str,
        image_b64s: Sequence[str],
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> tuple[dict[str, Any] | None, str, str]:
        self._last_invoke_error = ""
        self._last_invoke_protocol = ""
        self._last_raw_response_preview = ""
        base_url = str(getattr(settings, "ollama_base_url", "") or "").rstrip("/")
        if not base_url:
            self._last_invoke_error = "ollama_base_url_missing"
            return None, self._last_invoke_error, ""
        if httpx is None:
            self._last_invoke_error = "httpx_unavailable"
            return None, self._last_invoke_error, ""
        errors: list[str] = []
        protocol_attempts: list[tuple[str, Any]] = [("openai_compat", self._invoke_openai_compat_batch_json)]
        if bool(getattr(settings, "local_structured_pdf_hybrid_enable_native_fallback", False)):
            protocol_attempts.append(("native", self._invoke_native_ollama_batch_json))
        for protocol, attempt in protocol_attempts:
            parsed, error, preview = await attempt(
                base_url=base_url,
                model=model,
                user_prompt=user_prompt,
                image_b64s=list(image_b64s),
                use_response_format=use_response_format,
                max_output_tokens=max(256, int(max_output_tokens or 1800)),
            )
            if preview:
                self._last_invoke_protocol = protocol
                self._last_raw_response_preview = preview
            if isinstance(parsed, dict):
                self._last_invoke_error = ""
                return parsed, "", preview
            if error:
                errors.append(error)
        self._last_invoke_error = errors[-1] if errors else "ollama_request_failed"
        return None, self._last_invoke_error, ""

    async def _invoke_openai_compat_batch_json(
        self,
        *,
        base_url: str,
        model: str,
        user_prompt: str,
        image_b64s: Sequence[str],
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> tuple[dict[str, Any] | None, str, str]:
        disable_thinking = bool(getattr(settings, "local_structured_pdf_hybrid_disable_thinking", True))
        content = [{"type": "text", "text": user_prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
            for image_b64 in list(image_b64s or [])
            if str(image_b64 or "").strip()
        )
        payload = {
            "model": model,
            "stream": False,
            "temperature": _VL_TEMPERATURE,
            "top_p": _VL_TOP_P,
            "presence_penalty": _VL_PRESENCE_PENALTY,
            "max_tokens": max(256, int(max_output_tokens or 1800)),
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
        }
        if disable_thinking:
            payload["reasoning_effort"] = "none"
            payload["reasoning"] = {"effort": "none"}
        if use_response_format:
            payload["response_format"] = self._openai_batch_response_format_schema()
        url = f"{base_url}/v1/chat/completions"
        response = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
        except Exception as exc:  # pragma: no cover - runtime/network failure
            logger.warning(f"[LocalStructuredPdfHybrid] OpenAI-compatible Ollama batch parse failed: {exc}")
            return None, self._format_ollama_error(prefix="ollama_openai_batch", exc=exc), ""

        try:
            data = response.json()
        except Exception as exc:
            raw_text = ""
            try:
                raw_text = str(getattr(response, "text", "") or "").strip()
            except Exception as raw_exc:  # pragma: no cover - defensive fallback
                logger.warning(
                    f"[LocalStructuredPdfHybrid] OpenAI-compatible Ollama batch raw text unavailable after json parse failure: {raw_exc}"
                )
            fallback_preview = self._preview_text(raw_text or str(exc))
            if raw_text:
                try:
                    recovered = await parse_json_dict_from_model_text(raw_text)
                except Exception as raw_exc:  # pragma: no cover - defensive fallback
                    logger.warning(
                        f"[LocalStructuredPdfHybrid] OpenAI-compatible Ollama batch raw text recovery failed: {raw_exc}"
                    )
                else:
                    if isinstance(recovered, dict):
                        data = recovered
                    else:
                        logger.warning(
                            "[LocalStructuredPdfHybrid] OpenAI-compatible Ollama batch response.json() failed and raw text was not recoverable"
                        )
                        return (
                            None,
                            "ollama_openai_batch_response_json_failed",
                            fallback_preview,
                        )
            if not isinstance(data, dict):
                return None, "ollama_openai_batch_response_json_failed", fallback_preview

        try:
            message = data["choices"][0]["message"]
            content_text = str(message.get("content") or "").strip()
            reasoning = str(message.get("reasoning") or message.get("thinking") or "").strip()
        except Exception:
            return None, "ollama_openai_batch_bad_response", self._preview_text(json.dumps(data, ensure_ascii=False))
        preview_source = content_text or reasoning
        if not preview_source:
            return None, "ollama_openai_batch_empty_content", ""
        parsed = await parse_json_dict_from_model_text(preview_source)
        preview = self._preview_text(preview_source)
        return (parsed, "", preview) if isinstance(parsed, dict) else (None, "ollama_openai_batch_non_json_content", preview)

    @staticmethod
    def _openai_batch_response_format_schema() -> dict[str, Any]:
        page_schema = LocalOllamaQwenVlPageParser._openai_response_format_schema()["json_schema"]["schema"]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "pdf_backend_pages",
                "schema": {
                    "type": "object",
                    "properties": {
                        "pages": {
                            "type": "array",
                            "items": page_schema,
                        }
                    },
                    "required": ["pages"],
                    "additionalProperties": True,
                },
            },
        }

    async def _invoke_native_ollama_batch_json(
        self,
        *,
        base_url: str,
        model: str,
        user_prompt: str,
        image_b64s: Sequence[str],
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> tuple[dict[str, Any] | None, str, str]:
        disable_thinking = bool(getattr(settings, "local_structured_pdf_hybrid_disable_thinking", True))
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [str(item) for item in image_b64s if str(item or "").strip()],
                }
            ],
            "options": {
                "temperature": _VL_TEMPERATURE,
                "top_p": _VL_TOP_P,
                "top_k": _VL_TOP_K,
                "num_predict": max(256, int(max_output_tokens or 1800)),
            },
        }
        if use_response_format:
            payload["format"] = self._openai_batch_response_format_schema()["json_schema"]["schema"]
        if disable_thinking:
            payload["think"] = False
        url = f"{base_url}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # pragma: no cover - runtime/network failure
            logger.warning(f"[LocalStructuredPdfHybrid] Native Ollama batch parse failed: {exc}")
            return None, self._format_ollama_error(prefix="ollama_native_batch", exc=exc), ""

        message = data.get("message") if isinstance(data, dict) else {}
        content = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        if not content:
            return None, "ollama_native_batch_empty_content", self._preview_text(json.dumps(data, ensure_ascii=False))
        parsed = await parse_json_dict_from_model_text(content)
        preview = self._preview_text(content)
        return (parsed, "", preview) if isinstance(parsed, dict) else (None, "ollama_native_batch_non_json_content", preview)

    def _resolved_model(self) -> str:
        models = self._resolved_models()
        return models[0] if models else ""

    def _resolved_models(self) -> list[str]:
        chain = str(getattr(settings, "local_structured_pdf_hybrid_model_chain", "") or "").strip()
        if chain:
            items = [token.strip() for token in chain.split(",") if token.strip()]
            if items:
                return items
        single = str(getattr(settings, "local_structured_pdf_hybrid_model", "") or "").strip()
        if single:
            return [single]
        return [str(item).strip() for item in _DEFAULT_MODEL_CHAIN if str(item).strip()]

    def _select_line_rows(self, *, resolved_page: PdfResolvedPage) -> list[dict[str, Any]]:
        lines = list(getattr(resolved_page, "lines", []) or [])
        selected = sorted(
            lines,
            key=lambda item: (
                int(getattr(item, "reading_order", 0) or 0),
                round(float(item.bbox.top), 2),
                round(float(item.bbox.x0), 2),
                str(item.line_id),
            ),
        )[: self._max_lines_per_page]
        return [
            {
                "line_id": str(line.line_id),
                "text": str(line.text or ""),
                "band": str(line.band or "body"),
                "column_id": str(line.column_id or "main"),
                "region": str(line.region or "main"),
                "reading_order": int(line.reading_order or 0),
                "bbox": {
                    "x0": round(float(line.bbox.x0), 2),
                    "top": round(float(line.bbox.top), 2),
                    "x1": round(float(line.bbox.x1), 2),
                    "bottom": round(float(line.bbox.bottom), 2),
                },
            }
            for line in selected
        ]

    def _build_prompt_payload(
        self,
        *,
        resolved_page: PdfResolvedPage,
        line_rows: list[dict[str, Any]],
        triage_result: PdfHybridTriageResult | None,
        force_ocr: bool | None = None,
        task_hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        triage_dict = triage_result.to_dict() if triage_result is not None and hasattr(triage_result, "to_dict") else {
            "page_type": str(getattr(triage_result, "page_type", "") or ""),
            "decision": str(getattr(triage_result, "decision", "") or ""),
            "confidence": float(getattr(triage_result, "confidence", 0.0) or 0.0),
            "reasons": list(getattr(triage_result, "reasons", []) or []),
        }
        page_type = str(triage_dict.get("page_type") or "").strip().lower()
        prompt_rows = list(line_rows)
        if page_type == "visual_or_scanned":
            prompt_rows = prompt_rows[:24]
        elif page_type in {"mixed_layout", "formula_or_display_heavy", "front_matter_heavy"}:
            prompt_rows = prompt_rows[:48]

        source_text_full = "\n".join(str(item.get("text") or "") for item in prompt_rows).strip()
        if page_type == "visual_or_scanned" and len(source_text_full) > 400:
            source_text_full = source_text_full[:400].rstrip()
        elif len(source_text_full) > 1200:
            source_text_full = source_text_full[:1200].rstrip()
        source_checksum = hashlib.sha256(source_text_full.encode("utf-8")).hexdigest() if source_text_full else ""
        normalized_task_hints = self._normalize_task_hints(task_hints=task_hints)
        if force_ocr is not None:
            normalized_task_hints["force_ocr"] = bool(force_ocr)
        return {
            "page": int(getattr(resolved_page, "page", 0) or 0),
            "page_width": float(getattr(resolved_page.meta, "page_width", 0.0) or 0.0),
            "page_height": float(getattr(resolved_page.meta, "page_height", 0.0) or 0.0),
            "column_count": int(getattr(resolved_page, "column_count", 1) or 1),
            "source_checksum": source_checksum,
            "source_text_full": source_text_full,
            "triage": triage_dict,
            "task_hints": normalized_task_hints,
            "line_rows": prompt_rows,
            "source_rows": [
                {
                    "text": str(item.get("text") or ""),
                    "band": str(item.get("band") or ""),
                    "column_id": str(item.get("column_id") or ""),
                    "region": str(item.get("region") or ""),
                    "bbox": dict(item.get("bbox") or {}),
                }
                for item in prompt_rows
                if isinstance(item, dict)
            ],
            "kind_whitelist": _PROMPT_KIND_WHITELIST,
            "zone_whitelist": _PROMPT_ZONE_WHITELIST,
        }

    def _max_image_side_for_page(self, *, triage_result: PdfHybridTriageResult | None) -> int:
        page_type = str(getattr(triage_result, "page_type", "") or "").strip().lower()
        if page_type == "visual_or_scanned":
            return min(self._max_image_side, 1024)
        if page_type in {"mixed_layout", "formula_or_display_heavy"}:
            return min(self._max_image_side, 1280)
        return self._max_image_side

    @staticmethod
    def _max_output_tokens_for_page(*, prompt_payload: dict[str, Any]) -> int:
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()
        task_hints = prompt_payload.get("task_hints") if isinstance(prompt_payload, dict) else {}
        extra = 0
        if bool((task_hints or {}).get("force_ocr")):
            extra += 400
        if bool((task_hints or {}).get("enrich_formula")):
            extra += 300
        if bool((task_hints or {}).get("enrich_picture_description")):
            extra += 300
        if page_type == "visual_or_scanned":
            return 3200 + extra
        if page_type in {"mixed_layout", "formula_or_display_heavy", "front_matter_heavy"}:
            return 2400 + extra
        return 1800 + extra

    async def _call_with_retry(
        self,
        *,
        prompt_payload: dict[str, Any],
        image_b64: str,
        model: str,
    ) -> tuple[dict[str, Any] | None, bool, int, str]:
        retry_used = False
        retry_count = 0
        error = ""
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()

        validated, errors = await self._attempt_parse(
            prompt_payload=prompt_payload,
            image_b64=image_b64,
            model=model,
            retry_hint="",
        )
        if isinstance(validated, dict):
            return validated, retry_used, retry_count, ""

        retry_used = True
        retry_count = 1
        error = ",".join(errors) if errors else "validation_failed"
        if page_type == "visual_or_scanned":
            retry_hint = (
                "Previous output failed validation. "
                "Return strict JSON only. "
                "Treat the page as visual/scanned. "
                "If source_rows are incomplete, prefer OCR-style text+bbox elements. "
                "Do not invent content. "
                "If uncertain, return fewer larger elements."
            )
        else:
            retry_hint = (
                "Previous output failed validation. "
                "Return strict JSON only. "
                "Prefer docling-like loose elements with text+bbox. "
                "Do not invent content. "
                "If uncertain, split conservatively and use kind=unknown."
            )
        validated_retry, retry_errors = await self._attempt_parse(
            prompt_payload=prompt_payload,
            image_b64=image_b64,
            model=model,
            retry_hint=retry_hint,
        )
        if isinstance(validated_retry, dict):
            return validated_retry, retry_used, retry_count, ""
        error = ",".join(retry_errors) if retry_errors else error
        return None, retry_used, retry_count, error

    async def _attempt_parse(
        self,
        *,
        prompt_payload: dict[str, Any],
        image_b64: str,
        model: str,
        retry_hint: str,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        max_output_tokens = self._max_output_tokens_for_page(prompt_payload=prompt_payload)
        raw = await self._invoke_ollama_json(
            model=model,
            user_prompt=self._build_prompt_text(prompt_payload=prompt_payload, retry_hint=retry_hint),
            image_b64=image_b64,
            use_response_format=self._should_use_response_format(prompt_payload=prompt_payload),
            max_output_tokens=max_output_tokens,
        )
        if not isinstance(raw, dict):
            return None, [self._last_invoke_error or "backend_request_failed"]
        validated = self._transformer.transform_payload(payload=raw, prompt_payload=prompt_payload)
        if validated is None:
            return None, ["schema_validation_failed"]
        return validated, []

    async def _invoke_ollama_json(
        self,
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> dict[str, Any] | None:
        self._last_invoke_error = ""
        self._last_invoke_protocol = ""
        self._last_raw_response_preview = ""
        base_url = str(getattr(settings, "ollama_base_url", "") or "").rstrip("/")
        if not base_url:
            self._last_invoke_error = "ollama_base_url_missing"
            return None
        if httpx is None:
            self._last_invoke_error = "httpx_unavailable"
            return None
        errors: list[str] = []
        protocol_attempts: list[tuple[str, Any]] = [("openai_compat", self._invoke_openai_compat_json)]
        if bool(getattr(settings, "local_structured_pdf_hybrid_enable_native_fallback", False)):
            protocol_attempts.append(("native", self._invoke_native_ollama_json))
        for protocol, attempt in protocol_attempts:
            parsed, error, preview = await attempt(
                base_url=base_url,
                model=model,
                user_prompt=user_prompt,
                image_b64=image_b64,
                use_response_format=use_response_format,
                max_output_tokens=max(256, int(max_output_tokens or 1800)),
            )
            if preview:
                self._last_invoke_protocol = protocol
                self._last_raw_response_preview = preview
            if isinstance(parsed, dict):
                self._last_invoke_error = ""
                return parsed
            if error:
                errors.append(error)
        self._last_invoke_error = errors[-1] if errors else "ollama_request_failed"
        return None

    async def _invoke_ollama_text(
        self,
        *,
        model: str,
        user_prompt: str,
        image_b64: str,
        max_output_tokens: int = 220,
    ) -> str:
        self._last_invoke_error = ""
        base_url = str(getattr(settings, "ollama_base_url", "") or "").rstrip("/")
        if not base_url or httpx is None:
            return ""
        payload = {
            "model": model,
            "stream": False,
            "temperature": _VL_TEMPERATURE,
            "top_p": _VL_TOP_P,
            "presence_penalty": _VL_PRESENCE_PENALTY,
            "max_tokens": max(96, int(max_output_tokens or 220)),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        }
        if bool(getattr(settings, "local_structured_pdf_hybrid_disable_thinking", True)):
            payload["reasoning_effort"] = "none"
            payload["reasoning"] = {"effort": "none"}
        url = f"{base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # pragma: no cover - runtime/network failure
            logger.warning(f"[LocalStructuredPdfHybrid] Picture description request failed: {exc}")
            self._last_invoke_error = self._format_ollama_error(prefix="ollama_openai", exc=exc)
            return ""
        try:
            message = data["choices"][0]["message"]
        except Exception:
            self._last_invoke_error = "ollama_openai_bad_response"
            return ""
        return str(message.get("content") or "").strip()

    async def _invoke_openai_compat_json(
        self,
        *,
        base_url: str,
        model: str,
        user_prompt: str,
        image_b64: str,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> tuple[dict[str, Any] | None, str, str]:
        disable_thinking = bool(getattr(settings, "local_structured_pdf_hybrid_disable_thinking", True))
        payload = {
            "model": model,
            "stream": False,
            "temperature": _VL_TEMPERATURE,
            "top_p": _VL_TOP_P,
            "presence_penalty": _VL_PRESENCE_PENALTY,
            "max_tokens": max(256, int(max_output_tokens or 1800)),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        }
        if disable_thinking:
            payload["reasoning_effort"] = "none"
            payload["reasoning"] = {"effort": "none"}
        if use_response_format:
            payload["response_format"] = self._openai_response_format_schema()
        url = f"{base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # pragma: no cover - runtime/network failure
            logger.warning(f"[LocalStructuredPdfHybrid] OpenAI-compatible Ollama parse failed: {exc}")
            return None, self._format_ollama_error(prefix="ollama_openai", exc=exc), ""

        try:
            message = data["choices"][0]["message"]
            content = str(message.get("content") or "").strip()
            reasoning = str(message.get("reasoning") or message.get("thinking") or "").strip()
        except Exception:
            return None, "ollama_openai_bad_response", self._preview_text(json.dumps(data, ensure_ascii=False))
        preview_source = content or reasoning
        if not preview_source:
            return None, "ollama_openai_empty_content", ""
        parsed = await parse_json_dict_from_model_text(preview_source)
        preview = self._preview_text(preview_source)
        return (parsed, "", preview) if isinstance(parsed, dict) else (None, "ollama_openai_non_json_content", preview)

    @staticmethod
    def _openai_response_format_schema() -> dict[str, Any]:
        bbox_schema = {
            "type": "object",
            "properties": {
                "x0": {"type": "number"},
                "top": {"type": "number"},
                "x1": {"type": "number"},
                "bottom": {"type": "number"},
            },
            "required": ["x0", "top", "x1", "bottom"],
            "additionalProperties": False,
        }
        prov_schema = {
            "type": "object",
            "properties": {
                "page_no": {"type": "integer"},
                "bbox": {
                    "type": "object",
                    "properties": {
                        "l": {"type": "number"},
                        "t": {"type": "number"},
                        "r": {"type": "number"},
                        "b": {"type": "number"},
                        "coord_origin": {"type": "string"},
                    },
                    "required": ["l", "t", "r", "b"],
                    "additionalProperties": True,
                },
            },
            "required": ["page_no", "bbox"],
            "additionalProperties": True,
        }
        text_item_schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "text": {"type": "string"},
                "orig": {"type": "string"},
                "bbox": bbox_schema,
                "prov": {"type": "array", "items": prov_schema},
            },
            "required": ["label"],
            "additionalProperties": True,
        }
        table_cell_schema = {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start_row_offset_idx": {"type": "integer"},
                "start_col_offset_idx": {"type": "integer"},
                "row_span": {"type": "integer"},
                "col_span": {"type": "integer"},
            },
            "required": ["start_row_offset_idx", "start_col_offset_idx"],
            "additionalProperties": True,
        }
        table_item_schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "bbox": bbox_schema,
                "prov": {"type": "array", "items": prov_schema},
                "data": {
                    "type": "object",
                    "properties": {
                        "grid": {
                            "type": "array",
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "table_cells": {"type": "array", "items": table_cell_schema},
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["label"],
            "additionalProperties": True,
        }
        picture_item_schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "bbox": bbox_schema,
                "prov": {"type": "array", "items": prov_schema},
                "annotations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["label"],
            "additionalProperties": True,
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "pdf_backend_page",
                "schema": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer"},
                        "page_role": {"type": "string"},
                        "texts": {"type": "array", "items": text_item_schema},
                        "tables": {"type": "array", "items": table_item_schema},
                        "pictures": {"type": "array", "items": picture_item_schema},
                        "notes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["page", "page_role"],
                    "additionalProperties": True,
                },
            },
        }

    async def _invoke_native_ollama_json(
        self,
        *,
        base_url: str,
        model: str,
        user_prompt: str,
        image_b64: str,
        use_response_format: bool = True,
        max_output_tokens: int = 1800,
    ) -> tuple[dict[str, Any] | None, str, str]:
        disable_thinking = bool(getattr(settings, "local_structured_pdf_hybrid_disable_thinking", True))
        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                    "images": [image_b64],
                }
            ],
            "options": {
                "temperature": _VL_TEMPERATURE,
                "top_p": _VL_TOP_P,
                "top_k": _VL_TOP_K,
                "num_predict": max(256, int(max_output_tokens or 1800)),
            },
        }
        if use_response_format:
            payload["format"] = self._openai_response_format_schema()["json_schema"]["schema"]
        if disable_thinking:
            payload["think"] = False
        url = f"{base_url}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # pragma: no cover - runtime/network failure
            logger.warning(f"[LocalStructuredPdfHybrid] Native Ollama parse failed: {exc}")
            return None, self._format_ollama_error(prefix="ollama_native", exc=exc), ""

        message = data.get("message") if isinstance(data, dict) else {}
        content = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        if not content:
            return None, "ollama_native_empty_content", self._preview_text(json.dumps(data, ensure_ascii=False))
        parsed = await parse_json_dict_from_model_text(content)
        preview = self._preview_text(content)
        return (parsed, "", preview) if isinstance(parsed, dict) else (None, "ollama_native_non_json_content", preview)

    @staticmethod
    def _format_ollama_error(*, prefix: str, exc: Exception) -> str:
        if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
            return f"{prefix}_http_{int(exc.response.status_code)}"
        return f"{prefix}_request_failed"

    def _build_prompt_text(self, *, prompt_payload: dict[str, Any], retry_hint: str) -> str:
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        task_hints = prompt_payload.get("task_hints") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()
        visual_or_scanned = page_type == "visual_or_scanned"
        force_ocr = bool((task_hints or {}).get("force_ocr"))
        enrich_formula = bool((task_hints or {}).get("enrich_formula"))
        enrich_picture_description = bool((task_hints or {}).get("enrich_picture_description"))
        picture_description_prompt = str((task_hints or {}).get("picture_description_prompt") or "").strip()
        source_rows = [
            {
                "text": str(item.get("text") or ""),
                "band": str(item.get("band") or ""),
                "column_id": str(item.get("column_id") or ""),
                "region": str(item.get("region") or ""),
                "bbox": dict(item.get("bbox") or {}),
            }
            for item in list(prompt_payload.get("source_rows") or [])
            if isinstance(item, dict)
        ]
        page_number = int(prompt_payload.get("page") or 0)
        source_text_full = str(prompt_payload.get("source_text_full") or "").strip()
        if visual_or_scanned:
            schema_example = (
                '{'
                '"page":1,'
                '"page_role":"poster",'
                '"texts":['
                '{"label":"section_header","text":"REAL TITLE FROM IMAGE","bbox":{"x0":110,"top":80,"x1":520,"bottom":150},"meta":{"level":1}},'
                '{"label":"text","text":"Recovered OCR paragraph","bbox":{"x0":96,"top":190,"x1":530,"bottom":280}}'
                '],'
                '"pictures":[{"bbox":{"x0":72,"top":330,"x1":540,"bottom":620},"annotations":[{"kind":"description","text":"chart"}]}],'
                '"notes":[]'
                '}'
            )
            prompt = "".join(
                [
                    "You are a PDF hybrid backend for visually complex or scanned pages. Return JSON only.\n",
                    "Model the page in a docling-like loose structure. Preferred top-level fields are texts, tables, pictures, notes.\n",
                    "For text regions, use objects with label/text/bbox.\n",
                    "If the page is image-heavy or scanned, emit OCR-style text+bbox regions.\n",
                    "OCR is explicitly requested for this page. Recover all readable text conservatively.\n"
                    if force_ocr
                    else "",
                    "Formula enrichment is requested. Emit visible formulas with label=formula when confident.\n"
                    if enrich_formula
                    else "",
                    "Picture description is requested. Include pictures entries with description annotations for major visuals.\n"
                    if enrich_picture_description
                    else "",
                    f"Picture description prompt: {json.dumps(picture_description_prompt, ensure_ascii=False)}\n"
                    if enrich_picture_description and picture_description_prompt
                    else "",
                    "Do not invent content. Keep regions coarse and conservative.\n",
                    "Return a small number of large regions. Merge nearby sentences into broader text regions.\n",
                    "Prefer 4-12 text regions for a poster-like page unless there are clearly separate sections.\n",
                    "Use label=section_header only for clear headings or section banners.\n",
                    "Large sentence-like statements, slogans, or explanatory claims should use label=text.\n",
                    "Avoid returning many section_header elements on a single poster-like page unless there are clearly separated sections.\n",
                    "Do not return one element per short line. Do not wrap the JSON in markdown fences.\n",
                    "Allowed labels: section_header, text, caption, footnote, list_item, formula, page_header, page_footer, table, picture, unknown.\n",
                    f"page: {page_number}\n",
                    f"output_schema_example: {schema_example}\n",
                    "Return JSON now.",
                ]
            )
            if str(retry_hint or "").strip():
                prompt += f"\nRetry hint: {json.dumps(str(retry_hint), ensure_ascii=False)}"
            return prompt
        schema_example = (
            '{'
            '"page":1,'
            '"page_role":"body",'
            '"texts":['
            '{"label":"section_header","text":"Introduction","bbox":{"x0":80,"top":96,"x1":220,"bottom":116},"meta":{"level":1}},'
            '{"label":"text","text":"First sentence. Second sentence.","bbox":{"x0":80,"top":120,"x1":520,"bottom":170}}'
            '],'
            '"notes":[]'
            '}'
        )
        prompt = "".join(
            [
                "You are a PDF hybrid backend. Return JSON only.\n",
                "Model the page in a docling-like loose structure. Preferred top-level fields are texts, tables, pictures, notes.\n",
                "For text regions, use label/text/bbox.\n",
                "OCR is explicitly requested for this page. Recover all readable text conservatively.\n"
                if force_ocr
                else "",
                "Formula enrichment is requested. Emit visible formulas with label=formula when confident.\n"
                if enrich_formula
                else "",
                "Picture description is requested. Include pictures entries with description annotations for major visuals.\n"
                if enrich_picture_description
                else "",
                f"Picture description prompt: {json.dumps(picture_description_prompt, ensure_ascii=False)}\n"
                if enrich_picture_description and picture_description_prompt
                else "",
                "Do not invent content.\n",
                "Do not wrap the JSON in markdown fences.\n",
                "Allowed labels: section_header, text, caption, footnote, list_item, formula, page_header, page_footer, table, picture, unknown.\n",
                f"page: {page_number}\n",
                f"source_rows: {json.dumps(source_rows, ensure_ascii=False)}\n",
                f"output_schema_example: {schema_example}\n",
                "Return JSON now.",
            ]
        )
        if str(retry_hint or "").strip():
            prompt += f"\nRetry hint: {json.dumps(str(retry_hint), ensure_ascii=False)}"
        return prompt

    @staticmethod
    def _should_use_response_format(*, prompt_payload: dict[str, Any]) -> bool:
        triage = prompt_payload.get("triage") if isinstance(prompt_payload, dict) else {}
        page_type = str((triage or {}).get("page_type") or "").strip().lower()
        return page_type not in {"visual_or_scanned"}

    @staticmethod
    def _normalize_task_hints(*, task_hints: dict[str, Any] | None) -> dict[str, Any]:
        hints = dict(task_hints or {})
        prompt = str(hints.get("picture_description_prompt") or "").strip()
        if len(prompt) > 400:
            prompt = prompt[:400].rstrip()
        return {
            "force_ocr": bool(hints.get("force_ocr")),
            "enrich_formula": bool(hints.get("enrich_formula")),
            "enrich_picture_description": bool(hints.get("enrich_picture_description")),
            "picture_description_prompt": prompt,
        }

    def _materialize_blocks(
        self,
        *,
        payload: dict[str, Any],
        resolved_page: PdfResolvedPage,
        line_rows: list[dict[str, Any]],
    ) -> list[PdfHybridParsedBlock]:
        blocks: list[PdfHybridParsedBlock] = []
        for index, row in enumerate(list(payload.get("blocks") or []), start=1):
            if not isinstance(row, dict):
                continue
            merge_strategy = str(row.get("merge_strategy") or "space")
            text = str(row.get("text") or "").strip()
            bbox = self._coerce_bbox(
                row.get("bbox"),
                fallback=PdfBBox(
                    x0=0.0,
                    top=0.0,
                    x1=float(getattr(resolved_page.meta, "page_width", 0.0) or 0.0),
                    bottom=float(getattr(resolved_page.meta, "page_height", 0.0) or 0.0),
                ),
            )
            reading_order = max(1, int(row.get("reading_order") or index))
            if not text:
                continue
            source_line_ids = [str(item).strip() for item in list(row.get("source_line_ids") or []) if str(item).strip()]
            blocks.append(
                PdfHybridParsedBlock(
                    block_id=str(row.get("block_id") or ""),
                    kind=str(row.get("kind") or "unknown"),
                    page=int(payload.get("page") or resolved_page.page),
                    reading_order=max(1, reading_order),
                    text=text,
                    bbox=bbox,
                    source_line_ids=source_line_ids,
                    table_rows=[list(item) for item in list(row.get("table_rows") or [])],
                    zone=str(row.get("zone") or "main"),
                    merge_strategy=merge_strategy,
                    confidence=max(0.0, min(1.0, float(row.get("confidence") or 0.0))),
                    heading_level=(
                        self._coerce_positive_int(row.get("heading_level"))
                        if str(row.get("kind") or "unknown").strip().lower() == "heading"
                        else None
                    ),
                )
            )
        return sorted(blocks, key=lambda item: (int(item.reading_order), item.block_id))

    def _should_accept_result(
        self,
        *,
        candidate: PdfHybridParsedPage,
        resolved_page: PdfResolvedPage,
        triage_result: PdfHybridTriageResult | None,
    ) -> tuple[bool, str]:
        blocks = list(candidate.blocks or [])
        if not blocks:
            return False, "no_blocks"
        page_type = str(getattr(triage_result, "page_type", "") or "").strip().lower()
        if page_type == "visual_or_scanned":
            if self._has_redundant_visual_heading_paragraph_pair(
                candidate=candidate,
                resolved_page=resolved_page,
            ):
                return False, "visual_page_redundant_heading_paragraph_pair"
            if any(not list(block.source_line_ids or []) for block in blocks):
                return True, "has_unanchored_ocr_blocks"
            resolved_text = " ".join(str(line.text or "").strip() for line in list(getattr(resolved_page, "lines", []) or [])).strip()
            parsed_text = " ".join(str(block.text or "").strip() for block in blocks).strip()
            if len(parsed_text) > max(24, len(resolved_text) + 12):
                return True, "parsed_text_richer_than_resolved_text"
            return False, "visual_page_still_only_residual_text"
        if page_type == "dense_table":
            has_table_block = any(str(block.kind or "").strip().lower() == "table" for block in blocks)
            has_table_rows = any(list(block.table_rows or []) for block in blocks)
            if has_table_block or has_table_rows:
                return True, "has_table_structure"
            return False, "dense_table_missing_table_structure"
        if page_type in {"mixed_layout", "front_matter_heavy", "formula_or_display_heavy"}:
            if len(blocks) >= 2:
                return True, "multiple_blocks"
            if any(str(block.kind or "") == "heading" for block in blocks):
                return True, "has_heading_signal"
            return False, "insufficient_structure_for_complex_page"
        return True, "default_accept"

    @staticmethod
    def _normalized_block_text(text: str) -> str:
        return " ".join(str(text or "").split()).strip().lower()

    @staticmethod
    def _bbox_overlap_ratio(left: PdfBBox, right: PdfBBox) -> float:
        inter_width = max(0.0, min(float(left.x1), float(right.x1)) - max(float(left.x0), float(right.x0)))
        inter_height = max(0.0, min(float(left.bottom), float(right.bottom)) - max(float(left.top), float(right.top)))
        inter_area = inter_width * inter_height
        if inter_area <= 0.0:
            return 0.0
        left_area = max(0.0, float(left.width) * float(left.height))
        right_area = max(0.0, float(right.width) * float(right.height))
        if left_area <= 0.0 or right_area <= 0.0:
            return 0.0
        return inter_area / max(1.0, min(left_area, right_area))

    def _has_redundant_visual_heading_paragraph_pair(
        self,
        *,
        candidate: PdfHybridParsedPage,
        resolved_page: PdfResolvedPage,
    ) -> bool:
        blocks = list(candidate.blocks or [])
        if len(blocks) != 2:
            return False
        if len(list(getattr(resolved_page, "lines", []) or [])) <= 0:
            return False
        kinds = {str(block.kind or "").strip().lower() for block in blocks}
        if "heading" not in kinds:
            return False
        if not ({"paragraph", "text"} & kinds):
            return False
        left_text = self._normalized_block_text(blocks[0].text)
        right_text = self._normalized_block_text(blocks[1].text)
        if not left_text or left_text != right_text:
            return False
        overlap_ratio = self._bbox_overlap_ratio(blocks[0].bbox, blocks[1].bbox)
        if overlap_ratio < 0.75:
            return False
        return True

    @staticmethod
    def _preview_text(text: str) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= 320:
            return cleaned
        return f"{cleaned[:317]}..."

    @staticmethod
    def _coerce_bbox(value: Any, *, fallback: PdfBBox) -> PdfBBox:
        if not isinstance(value, dict):
            return fallback
        try:
            x0 = float(value.get("x0"))
            top = float(value.get("top"))
            x1 = float(value.get("x1"))
            bottom = float(value.get("bottom"))
        except (TypeError, ValueError):
            return fallback
        if x1 <= x0 or bottom <= top:
            return fallback
        return PdfBBox(x0=x0, top=top, x1=x1, bottom=bottom)

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _render_page_image_base64(self, pdf_path: str, page: int, max_image_side: int | None = None) -> str:
        import fitz
        from PIL import Image

        doc = fitz.open(str(pdf_path))
        try:
            page_index = max(0, int(page) - 1)
            if page_index >= len(doc):
                return ""
            page_obj = doc[page_index]
            scale = float(self._render_dpi) / 72.0
            pix = page_obj.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            max_side = max(512, int(max_image_side or self._max_image_side))
            image.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=85, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        finally:
            doc.close()

    def _render_region_image_base64(
        self,
        pdf_path: str,
        page: int,
        bbox: PdfBBox,
        max_image_side: int | None = None,
    ) -> str:
        import fitz
        from PIL import Image

        doc = fitz.open(str(pdf_path))
        try:
            page_index = max(0, int(page) - 1)
            if page_index >= len(doc):
                return ""
            page_obj = doc[page_index]
            margin_x = max(8.0, float(bbox.width) * 0.06)
            margin_y = max(8.0, float(bbox.height) * 0.06)
            clip = fitz.Rect(
                max(0.0, float(bbox.x0) - margin_x),
                max(0.0, float(bbox.top) - margin_y),
                min(float(page_obj.rect.width), float(bbox.x1) + margin_x),
                min(float(page_obj.rect.height), float(bbox.bottom) + margin_y),
            )
            scale = float(self._render_dpi) / 72.0
            pix = page_obj.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            max_side = max(384, int(max_image_side or self._max_image_side))
            image.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=85, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        finally:
            doc.close()

    @staticmethod
    def _build_picture_description_prompt(*, prompt: str | None) -> str:
        custom = str(prompt or "").strip()
        if custom:
            return (
                "You are a PDF picture-description backend. "
                "Describe the main visual accurately in 1-3 sentences. "
                "Mention visible labels, axes, legends, numbers, and embedded text. "
                "Do not use markdown.\n"
                f"Instruction: {custom}"
            )
        return (
            "You are a PDF picture-description backend. "
            "Describe the main visual accurately in 1-3 sentences. "
            "Mention visible labels, axes, legends, numbers, and embedded text. "
            "Do not use markdown."
        )

    @staticmethod
    def _normalize_picture_description(text: str) -> str:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return ""
        if cleaned.startswith("{") or cleaned.startswith("["):
            return ""
        cleaned = cleaned.strip("`").strip()
        if len(cleaned) > 600:
            cleaned = cleaned[:600].rstrip()
        return cleaned

    @staticmethod
    def _normalize_formula_text(text: str) -> str:
        cleaned = " ".join(str(text or "").replace("```", " ").split()).strip()
        if not cleaned:
            return ""
        lowered = cleaned.lower()
        if lowered.startswith("here is") or lowered.startswith("the formula"):
            return ""
        if len(cleaned) > 300:
            cleaned = cleaned[:300].rstrip()
        return cleaned

    @staticmethod
    def _normalize_ocr_text(text: str) -> str:
        cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not cleaned:
            return ""
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^\s*(transcription|ocr)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        if len(cleaned) > 8000:
            cleaned = cleaned[:8000].rstrip()
        return cleaned.strip()
