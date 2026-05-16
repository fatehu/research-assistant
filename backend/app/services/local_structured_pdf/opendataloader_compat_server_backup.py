#!/usr/bin/env python3
from __future__ import annotations

"""Fast docling-compatible server using a singleton converter adapter.

This file intentionally mirrors the upstream `opendataloader-pdf` Python
`hybrid_server.py` structure as closely as possible. The primary difference is
only the converter implementation: upstream constructs Docling's
`DocumentConverter`, while this repository constructs a local adapter whose
model slots are backed by Qwen.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import tempfile
import threading
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

from .contracts import PdfBBox, PdfSemanticBlock, PdfStructuredDocument
from .formula_enrichment_service import LocalPdfFormulaEnrichmentService
from .ocr_enrichment_service import LocalPdfOcrEnrichmentService
from .pipeline import LocalStructuredPdfPipeline
from .picture_enrichment_service import LocalPdfPictureEnrichmentService, PdfPictureDescription

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5002
MAX_FILE_SIZE = 100 * 1024 * 1024
DEFAULT_PICTURE_DESCRIPTION_PROMPT = (
    "Describe what you see in this image. Include any text, numbers, labels, and data values visible."
)

# Global converter instance (initialized on startup with CLI options)
converter = None

# Keep upstream singleton + sequential convert semantics.
_convert_lock = threading.Lock()

_INVALID_UNICODE_RE = re.compile(r"[\ud800-\udfff\x00]")


def build_conversion_response(
    status_value: str,
    json_content: dict,
    processing_time: float,
    errors: list[str],
    requested_pages: tuple[int, int] | None,
    total_pages: int | None = None,
) -> dict:
    failed_pages: list[int] = []

    if status_value == "partial_success":
        pages_dict = json_content.get("pages", {})
        present_pages = set()
        for key in pages_dict.keys():
            try:
                present_pages.add(int(key))
            except (TypeError, ValueError):
                logger.warning("Unexpected non-integer page key in compat output: %r", key)

        if requested_pages:
            expected_pages = set(range(requested_pages[0], requested_pages[1] + 1))
        elif total_pages is not None:
            expected_pages = set(range(1, total_pages + 1))
        elif present_pages:
            expected_pages = set(range(min(present_pages), max(present_pages) + 1))
        else:
            expected_pages = set()

        failed_pages = sorted(expected_pages - present_pages)

    return {
        "status": status_value,
        "document": {
            "json_content": json_content,
        },
        "processing_time": processing_time,
        "errors": errors,
        "failed_pages": failed_pages,
    }


def sanitize_unicode(data: Any) -> Any:
    if isinstance(data, str):
        return _INVALID_UNICODE_RE.sub("\ufffd", data)
    if isinstance(data, dict):
        return {key: sanitize_unicode(value) for key, value in data.items()}
    if isinstance(data, list):
        return [sanitize_unicode(item) for item in data]
    return data


def _get_loop_setting() -> str:
    if sys.platform == "win32":
        return "asyncio"
    return "auto"


def _check_dependencies() -> None:
    missing: list[str] = []
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")
    try:
        import fitz  # noqa: F401
    except ImportError:
        missing.append("pymupdf")

    if missing:
        raise ImportError(
            f"Missing dependencies: {', '.join(missing)}. "
            "Install the backend/runtime dependencies required by this repository."
        )


class _CompatConversionStatus(Enum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"


@dataclass(frozen=True)
class _CompatError:
    error_message: str


@dataclass(frozen=True)
class _CompatInput:
    page_count: int | None = None


@dataclass(frozen=True)
class _CompatDocument:
    json_content: dict[str, Any]

    def export_to_dict(self) -> dict[str, Any]:
        return self.json_content


@dataclass(frozen=True)
class _CompatConversionResult:
    status: _CompatConversionStatus
    document: _CompatDocument
    errors: list[_CompatError] = field(default_factory=list)
    input: _CompatInput | None = None


class _CompatDocumentConverter:
    def __init__(
        self,
        *,
        force_full_page_ocr: bool = False,
        ocr_lang: list[str] | None = None,
        enrich_formula: bool = False,
        enrich_picture_description: bool = False,
        picture_description_prompt: str | None = None,
        pipeline: LocalStructuredPdfPipeline | None = None,
        formula_enrichment_service: LocalPdfFormulaEnrichmentService | None = None,
        picture_enrichment_service: LocalPdfPictureEnrichmentService | None = None,
        ocr_enrichment_service: LocalPdfOcrEnrichmentService | None = None,
    ) -> None:
        self._force_full_page_ocr = bool(force_full_page_ocr)
        self._ocr_lang = list(ocr_lang or [])
        self._enrich_formula = bool(enrich_formula)
        self._enrich_picture_description = bool(enrich_picture_description)
        self._picture_description_prompt = picture_description_prompt or DEFAULT_PICTURE_DESCRIPTION_PROMPT
        self._pipeline = pipeline or LocalStructuredPdfPipeline()
        self._formula_enrichment_service = formula_enrichment_service or LocalPdfFormulaEnrichmentService()
        self._picture_enrichment_service = picture_enrichment_service or LocalPdfPictureEnrichmentService()
        self._ocr_enrichment_service = ocr_enrichment_service or LocalPdfOcrEnrichmentService()

    def convert(self, pdf_path: str, page_range: tuple[int, int] | None = None) -> _CompatConversionResult:
        active_pdf_path = pdf_path
        temp_subset_path: str | None = None
        try:
            if page_range:
                temp_subset_path = self._build_subset_pdf(pdf_path, page_range)
                active_pdf_path = temp_subset_path

            document = asyncio.run(
                self._parse_document(
                    pdf_path=active_pdf_path,
                    force_ocr=self._force_full_page_ocr,
                    ocr_lang=self._ocr_lang,
                    enrich_formula=self._enrich_formula,
                    enrich_picture_description=self._enrich_picture_description,
                    picture_description_prompt=self._picture_description_prompt,
                )
            )
            ocr_pages = self._auto_ocr_page_numbers(document=document)
            if self._force_full_page_ocr:
                ocr_pages = {int(page.page) for page in list(document.pages or [])}
            if ocr_pages:
                document = asyncio.run(
                    self._ocr_enrichment_service.enrich_document(
                        pdf_path=active_pdf_path,
                        document=document,
                        ocr_lang=self._ocr_lang,
                        page_numbers=ocr_pages,
                    )
                )
            if self._enrich_formula:
                document = asyncio.run(
                    self._formula_enrichment_service.enrich_document(
                        pdf_path=active_pdf_path,
                        document=document,
                    )
                )
            picture_descriptions: list[PdfPictureDescription] = []
            if self._enrich_picture_description:
                picture_descriptions = asyncio.run(
                    self._picture_enrichment_service.enrich_document(
                        pdf_path=active_pdf_path,
                        document=document,
                        picture_description_prompt=self._picture_description_prompt,
                    )
                )
            json_content = structured_document_to_docling_json(
                document,
                picture_descriptions=picture_descriptions,
            )
            json_content = sanitize_unicode(json_content)
            total_pages = len(list(document.pages or [])) or None
            return _CompatConversionResult(
                status=_CompatConversionStatus.SUCCESS,
                document=_CompatDocument(json_content=json_content),
                errors=[],
                input=_CompatInput(page_count=total_pages),
            )
        except Exception as exc:
            logger.error(f"Compat converter failed: {exc}\n{traceback.format_exc()}")
            return _CompatConversionResult(
                status=_CompatConversionStatus.FAILURE,
                document=_CompatDocument(json_content={"pages": {}, "texts": [], "tables": [], "pictures": []}),
                errors=[_CompatError(error_message=str(exc))],
                input=None,
            )
        finally:
            if temp_subset_path and os.path.exists(temp_subset_path):
                os.unlink(temp_subset_path)

    @staticmethod
    def _auto_ocr_page_numbers(document: PdfStructuredDocument) -> set[int]:
        page_numbers: set[int] = set()
        for page in list(document.pages or []):
            blocks = list(page.blocks or [])
            text_blocks = [
                block
                for block in blocks
                if str(getattr(block, "block_type", "") or "").strip().lower()
                not in {"table", "equation", "figure_meta"}
            ]
            visible_text = " ".join(str(getattr(block, "text", "") or "").strip() for block in text_blocks).strip()
            if not visible_text or len(visible_text) < 40:
                page_numbers.add(int(page.page))
        return page_numbers


class LocalStructuredPdfDoclingCompatBackend(_CompatDocumentConverter):
    """Public adapter matching this repository's existing naming."""

    async def _parse_document(
        self,
        *,
        pdf_path: str,
        force_ocr: bool,
        ocr_lang: list[str] | None,
        enrich_formula: bool,
        enrich_picture_description: bool,
        picture_description_prompt: str | None,
    ) -> PdfStructuredDocument:
        del force_ocr
        del ocr_lang
        del enrich_formula
        del enrich_picture_description
        del picture_description_prompt
        return await asyncio.to_thread(
            self._pipeline.parse_document,
            pdf_path=pdf_path,
        )

    @staticmethod
    def _build_subset_pdf(pdf_path: str, page_range: tuple[int, int]) -> str:
        import fitz

        start_page, end_page = page_range
        source = fitz.open(pdf_path)
        try:
            target = fitz.open()
            try:
                target.insert_pdf(
                    source,
                    from_page=max(0, start_page - 1),
                    to_page=max(0, end_page - 1),
                )
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.close()
                target.save(tmp.name)
                return tmp.name
            finally:
                target.close()
        finally:
            source.close()


def create_converter(
    force_full_page_ocr: bool = False,
    ocr_lang: list[str] | None = None,
    enrich_formula: bool = False,
    enrich_picture_description: bool = False,
    picture_description_prompt: str | None = None,
):
    """Create a converter with upstream-compatible options.

    This intentionally matches the upstream factory signature so that the rest
    of the server flow can stay almost identical to `hybrid_server.py`.
    """
    return LocalStructuredPdfDoclingCompatBackend(
        force_full_page_ocr=force_full_page_ocr,
        ocr_lang=ocr_lang,
        enrich_formula=enrich_formula,
        enrich_picture_description=enrich_picture_description,
        picture_description_prompt=picture_description_prompt,
    )


def structured_document_to_docling_json(
    document: PdfStructuredDocument,
    *,
    picture_descriptions: list[PdfPictureDescription] | None = None,
) -> dict[str, Any]:
    page_entries: dict[str, dict[str, Any]] = {}
    texts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    pictures: list[dict[str, Any]] = []

    for page in list(document.pages or []):
        page_no = int(page.page)
        page_entries[str(page_no)] = {
            "page_no": page_no,
            "size": {
                "width": float(page.meta.page_width),
                "height": float(page.meta.page_height),
            },
        }

        for block in list(page.blocks or []):
            label = _block_label(block)
            payload = _block_to_docling_payload(block, label=label)
            if payload is None:
                continue
            if label == "table":
                tables.append(payload)
            elif label == "picture":
                pictures.append(payload)
            else:
                texts.append(payload)

    for description in list(picture_descriptions or []):
        payload = {
            "prov": [
                {
                    "page_no": int(description.page),
                    "bbox": _bbox_to_docling(description.bbox),
                }
            ],
            "annotations": [{"kind": "description", "text": str(description.description or "").strip()}],
        }
        merged = False
        for existing in pictures:
            if _docling_picture_matches(existing=existing, candidate=payload):
                existing["annotations"] = list(payload.get("annotations") or [])
                merged = True
                break
        if not merged:
            pictures.append(payload)

    return {
        "pages": page_entries,
        "texts": texts,
        "tables": tables,
        "pictures": pictures,
    }


def _block_label(block: PdfSemanticBlock) -> str:
    token = str(block.block_type or "").strip().lower()
    if token == "heading":
        return "section_header"
    if token == "caption":
        return "caption"
    if token == "footnote":
        return "footnote"
    if token == "list_item":
        return "list_item"
    if token == "equation":
        return "formula"
    if token == "table":
        return "table"
    if token == "figure_meta":
        return "picture"
    return "text"


def _block_to_docling_payload(block: PdfSemanticBlock, *, label: str) -> dict[str, Any] | None:
    prov = [
        {
            "page_no": int(block.page_start),
            "bbox": _bbox_to_docling(block.bbox),
        }
    ]

    if label == "table":
        rows = [list(row) for row in list(block.table_rows or [])]
        if not rows:
            return None
        max_cols = max((len(row) for row in rows), default=0)
        return {
            "label": "table",
            "prov": prov,
            "data": {
                "grid": [[{} for _ in range(max_cols)] for _ in rows],
                "table_cells": [
                    {
                        "start_row_offset_idx": row_index,
                        "start_col_offset_idx": col_index,
                        "row_span": 1,
                        "col_span": 1,
                        "text": row[col_index] if col_index < len(row) else "",
                    }
                    for row_index, row in enumerate(rows)
                    for col_index in range(max_cols)
                ],
            },
        }

    if label == "picture":
        payload: dict[str, Any] = {
            "prov": prov,
        }
        description = str(block.text or "").strip()
        if description:
            payload["annotations"] = [{"kind": "description", "text": description}]
        return payload

    text = str(block.text or "").strip()
    if not text:
        return None
    payload: dict[str, Any] = {
        "label": label,
        "text": text,
        "orig": text,
        "prov": prov,
    }
    if label == "section_header" and block.heading_level:
        payload["meta"] = {"level": int(block.heading_level)}
    return payload


def _bbox_to_docling(bbox: PdfBBox) -> dict[str, Any]:
    return {
        "l": float(bbox.x0),
        "t": float(bbox.top),
        "r": float(bbox.x1),
        "b": float(bbox.bottom),
        "coord_origin": "BOTTOMLEFT",
    }


def _docling_picture_matches(*, existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    existing_bbox = _first_docling_bbox(existing)
    candidate_bbox = _first_docling_bbox(candidate)
    if existing_bbox is None or candidate_bbox is None:
        return False
    overlap_width = max(0.0, min(float(existing_bbox.x1), float(candidate_bbox.x1)) - max(float(existing_bbox.x0), float(candidate_bbox.x0)))
    overlap_height = max(0.0, min(float(existing_bbox.bottom), float(candidate_bbox.bottom)) - max(float(existing_bbox.top), float(candidate_bbox.top)))
    inter_area = overlap_width * overlap_height
    if inter_area <= 0.0:
        return False
    existing_area = max(1.0, float(existing_bbox.width) * float(existing_bbox.height))
    candidate_area = max(1.0, float(candidate_bbox.width) * float(candidate_bbox.height))
    return inter_area / min(existing_area, candidate_area) >= 0.5


def _first_docling_bbox(payload: dict[str, Any]) -> PdfBBox | None:
    prov = payload.get("prov")
    if not isinstance(prov, list) or not prov:
        return None
    first = prov[0]
    if not isinstance(first, dict):
        return None
    bbox = first.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        return PdfBBox(
            x0=float(bbox.get("l")),
            top=float(bbox.get("t")),
            x1=float(bbox.get("r")),
            bottom=float(bbox.get("b")),
        )
    except (TypeError, ValueError):
        return None


def create_app(
    force_ocr: bool = False,
    ocr_lang: list[str] | None = None,
    enrich_formula: bool = False,
    enrich_picture_description: bool = False,
    picture_description_prompt: str | None = None,
):
    """Create and configure the FastAPI application.

    Args:
        force_ocr: If True, force full-page OCR on all pages.
        ocr_lang: OCR language codes.
        enrich_formula: If True, enable formula enrichment.
        enrich_picture_description: If True, enable picture description.
        picture_description_prompt: Custom prompt for picture description.
    """
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        global converter
        lang_str = ",".join(ocr_lang) if ocr_lang else "default"
        enrichments = []
        if enrich_formula:
            enrichments.append("formula")
        if enrich_picture_description:
            enrichments.append("picture-description")
        enrichment_str = ",".join(enrichments) if enrichments else "none"
        logger.info(
            f"Initializing DocumentConverter "
            f"(force_ocr={force_ocr}, lang={lang_str}, enrichments={enrichment_str})..."
        )
        start = time.perf_counter()

        converter = create_converter(
            force_full_page_ocr=force_ocr,
            ocr_lang=ocr_lang,
            enrich_formula=enrich_formula,
            enrich_picture_description=enrich_picture_description,
            picture_description_prompt=picture_description_prompt,
        )

        elapsed = time.perf_counter() - start
        logger.info(f"DocumentConverter initialized in {elapsed:.2f}s")
        yield

    app = FastAPI(
        title="Docling Fast Server",
        description="Fast PDF conversion using a singleton converter adapter",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/convert/file")
    async def convert_file(
        files: UploadFile = File(...),
        page_ranges: Optional[str] = Form(default=None),
    ):
        global converter

        if converter is None:
            return JSONResponse(
                {"status": "failure", "errors": ["Server not initialized"]},
                status_code=503,
            )

        page_range_tuple = None
        if page_ranges:
            try:
                parts = page_ranges.split("-")
                if len(parts) == 2:
                    page_range_tuple = (int(parts[0]), int(parts[1]))
            except ValueError:
                pass

        content = await files.read()
        if len(content) > MAX_FILE_SIZE:
            return JSONResponse(
                {
                    "status": "failure",
                    "errors": [f"File size exceeds maximum allowed ({MAX_FILE_SIZE // (1024 * 1024)}MB)"],
                },
                status_code=413,
            )

        tmp_path = None
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            def _do_convert():
                with _convert_lock:
                    t0 = time.perf_counter()
                    if page_range_tuple:
                        result = converter.convert(tmp_path, page_range=page_range_tuple)
                    else:
                        result = converter.convert(tmp_path)
                    return result, time.perf_counter() - t0

            result, processing_time = await asyncio.to_thread(_do_convert)

            json_content = result.document.export_to_dict()
            json_content = sanitize_unicode(json_content)

            status_value = result.status.value if hasattr(result.status, "value") else str(result.status)
            errors = [getattr(item, "error_message", str(item)) for item in result.errors] if result.errors else []
            input_page_count = getattr(result.input, "page_count", None) if result.input else None

            response = build_conversion_response(
                status_value=status_value,
                json_content=json_content,
                processing_time=processing_time,
                errors=errors,
                requested_pages=page_range_tuple,
                total_pages=input_page_count,
            )
            return JSONResponse(response)
        except Exception as exc:
            logger.error(f"PDF conversion failed: {exc}\n{traceback.format_exc()}")
            return JSONResponse(
                {
                    "status": "failure",
                    "errors": ["PDF conversion failed. Check server logs for details."],
                },
                status_code=500,
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return app


def main():
    _check_dependencies()
    import uvicorn

    parser = argparse.ArgumentParser(description="Docling Fast Server for opendataloader-pdf")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind to (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind to (default: {DEFAULT_PORT})")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Force full-page OCR on all pages (default: auto-detect)",
    )
    parser.add_argument(
        "--ocr-lang",
        type=str,
        default=None,
        help="OCR languages (comma-separated codes). Default: repository/runtime default",
    )
    parser.add_argument(
        "--enrich-formula",
        action="store_true",
        default=False,
        help="Enable formula enrichment model (LaTeX extraction)",
    )
    parser.add_argument("--no-enrich-formula", action="store_false", dest="enrich_formula")
    parser.add_argument(
        "--enrich-picture-description",
        action="store_true",
        default=False,
        help="Enable picture description model (alt text generation using Qwen)",
    )
    parser.add_argument("--no-enrich-picture-description", action="store_false", dest="enrich_picture_description")
    parser.add_argument(
        "--picture-description-prompt",
        type=str,
        default=None,
        help="Custom prompt for picture description. If not set, uses default prompt optimized for charts and images.",
    )
    args = parser.parse_args()

    ocr_lang = None
    if args.ocr_lang:
        ocr_lang = [lang.strip() for lang in args.ocr_lang.split(",") if lang.strip()]

    enrichments = []
    if args.enrich_formula:
        enrichments.append("formula")
    if args.enrich_picture_description:
        enrichments.append("picture-description")

    logger.info(f"Starting Docling Fast Server on http://{args.host}:{args.port}")
    logger.info(f"OCR settings: force_ocr={args.force_ocr}, lang={ocr_lang or 'default'}")
    if enrichments:
        logger.info(f"Enrichments enabled: {', '.join(enrichments)}")

    app = create_app(
        force_ocr=args.force_ocr,
        ocr_lang=ocr_lang,
        enrich_formula=args.enrich_formula,
        enrich_picture_description=args.enrich_picture_description,
        picture_description_prompt=args.picture_description_prompt,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        loop=_get_loop_setting(),
    )


if __name__ == "__main__":
    main()
