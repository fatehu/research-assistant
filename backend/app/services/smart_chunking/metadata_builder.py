from __future__ import annotations

import re
from typing import Any, Optional

from .academic_detector import AcademicStructureDetector
from .types import ChunkLevel, ChunkMetadata


_CAPTION_RE = re.compile(r"^\s*(?:Figure|Fig\.?|Table|Tab\.?|图|表)\s*\d", re.IGNORECASE | re.MULTILINE)
_TABLE_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_EQUATION_RE = re.compile(r"(^\$\$)|(^```math\b)|(^\\\[)", re.MULTILINE)
_LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d+[\.\)])\s+\S", re.MULTILINE)
_CODE_RE = re.compile(r"^\s*```", re.MULTILINE)


def infer_content_flags(content: str) -> dict[str, bool]:
    text = str(content or "")
    return {
        "has_table": bool(_TABLE_RE.search(text)),
        "has_equation": bool(_EQUATION_RE.search(text)),
        "has_list": bool(_LIST_RE.search(text)),
        "has_code": bool(_CODE_RE.search(text)),
        "has_caption": bool(_CAPTION_RE.search(text)),
    }


def build_extra_metadata(
    *,
    engine: str,
    engine_mode: str,
    splitter: str,
    source_format: str,
    start_char: int,
    end_char: int,
    header_path: Optional[list[str]] = None,
    prev_id: Optional[str] = None,
    next_id: Optional[str] = None,
    content: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "engine": str(engine or ""),
        "engine_mode": str(engine_mode or ""),
        "splitter": str(splitter or ""),
        "source_format": str(source_format or ""),
        "header_path": list(header_path or []),
        "content_flags": infer_content_flags(content),
        "start_index": int(start_char),
        "end_index": int(end_char),
        "prev_id": prev_id,
        "next_id": next_id,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def build_chunk_metadata(
    *,
    level: ChunkLevel,
    content: str,
    position_ratio: float,
    section_title: Optional[str] = None,
    section_type: Optional[str] = None,
    parent_id: Optional[str] = None,
    child_ids: Optional[list[str]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> ChunkMetadata:
    resolved_section_type = section_type or AcademicStructureDetector.detect_section_type(content)
    resolved_section_title = section_title.strip() if isinstance(section_title, str) and section_title.strip() else section_title
    return ChunkMetadata(
        level=level,
        section_type=resolved_section_type,
        section_title=resolved_section_title,
        parent_id=parent_id,
        child_ids=list(child_ids or []),
        has_citations=AcademicStructureDetector.has_citations(content),
        position_ratio=float(position_ratio or 0.0),
        extra=dict(extra or {}),
    )
