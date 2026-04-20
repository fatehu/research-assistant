"""Minimal placeholder for future Docling Qwen OCR integration.

This module intentionally does not implement a working Docling OCR plugin yet.
It only centralizes the pending wiring point so the upstream hybrid server can
switch to a real `DocumentConverter` now without falling back to compat code.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def note_qwen_ocr_placeholder(
    *,
    force_full_page_ocr: bool,
    ocr_lang: list[str] | None,
) -> dict[str, Any]:
    """Return structured placeholder metadata for the pending Qwen OCR slot.

    The upstream server uses this only for logging and TODO wiring comments.
    Actual Docling OCR registration will follow once the concrete plugin API
    surface is pinned in this repository's runtime environment.
    """
    return {
        "requested": bool(force_full_page_ocr or ocr_lang),
        "force_full_page_ocr": bool(force_full_page_ocr),
        "ocr_lang": list(ocr_lang or []),
        "status": "placeholder",
        "blocking_api": "Docling OCR plugin registration with a concrete OcrOptions/BaseOcrModel pair",
    }


def log_qwen_ocr_placeholder(
    *,
    force_full_page_ocr: bool,
    ocr_lang: list[str] | None,
) -> None:
    """Emit a single warning when callers request the pending Qwen OCR path."""
    state = note_qwen_ocr_placeholder(
        force_full_page_ocr=force_full_page_ocr,
        ocr_lang=ocr_lang,
    )
    if state["requested"]:
        logger.warning(
            "Qwen OCR slot is not implemented in Docling yet; requested force_full_page_ocr=%s ocr_lang=%s. "
            "Continuing with Docling DocumentConverter wiring without a custom OCR plugin.",
            state["force_full_page_ocr"],
            state["ocr_lang"] or None,
        )
