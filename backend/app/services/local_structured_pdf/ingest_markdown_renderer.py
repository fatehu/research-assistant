from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import PdfSemanticBlock, PdfStructuredDocument


_SPACE_RE = re.compile(r"\s+")
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*•·▪◦]|(?:\d+|[A-Za-z]|[ivxlcdm]+)[\.\)])\s+\S",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RenderedMarkdownSpan:
    start_char: int
    end_char: int
    block_id: str
    block_type: str
    page_start: int
    page_end: int
    section_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_char": int(self.start_char),
            "end_char": int(self.end_char),
            "block_id": str(self.block_id or ""),
            "block_type": str(self.block_type or ""),
            "page_start": int(self.page_start),
            "page_end": int(self.page_end),
            "section_path": str(self.section_path or ""),
        }


@dataclass(frozen=True)
class RenderedMarkdownDocument:
    markdown: str
    spans: list[RenderedMarkdownSpan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "markdown": str(self.markdown or ""),
            "spans": [span.to_dict() for span in list(self.spans or [])],
        }


class LocalPdfIngestMarkdownRenderer:
    """Render structured PDF blocks into natural Markdown for downstream ingestion."""

    def render_document(self, *, document: PdfStructuredDocument) -> RenderedMarkdownDocument:
        rows: list[str] = []
        spans: list[RenderedMarkdownSpan] = []
        offset = 0
        for block in list(document.blocks or []):
            rendered = self._render_block(block)
            if not rendered:
                continue
            if rows:
                offset += 2  # "\n\n"
            rows.append(rendered)
            start_char = offset
            end_char = start_char + len(rendered)
            spans.append(
                RenderedMarkdownSpan(
                    start_char=start_char,
                    end_char=end_char,
                    block_id=str(block.block_id or ""),
                    block_type=str(block.block_type or ""),
                    page_start=int(block.page_start or 0),
                    page_end=int(block.page_end or 0),
                    section_path=str(block.section_path or ""),
                )
            )
            offset = end_char
        return RenderedMarkdownDocument(
            markdown="\n\n".join(rows).strip(),
            spans=spans,
        )

    def _render_block(self, block: PdfSemanticBlock) -> str:
        block_type = str(block.block_type or "").strip().lower()

        if block_type == "table" and block.table_rows:
            return self._render_table(block.table_rows)

        if block_type == "heading":
            text = self._normalize_prose_text(str(block.text or ""))
            if not text:
                return ""
            level = min(6, max(1, int(block.heading_level or 1)))
            return f'{"#" * level} {text}'

        if block_type == "list_item":
            text = self._normalize_prose_text(str(block.text or ""))
            if not text:
                return ""
            return text if _LIST_ITEM_RE.match(text) else f"- {text}"

        if block_type == "equation":
            return self._render_equation(str(block.text or ""))

        if block_type in {"caption", "figure_meta", "footnote"}:
            return self._normalize_rich_text(str(block.text or ""))

        return self._normalize_prose_text(str(block.text or ""))

    @staticmethod
    def _normalize_prose_text(raw_text: str) -> str:
        raw_text = str(raw_text or "").strip()
        if not raw_text:
            return ""
        rows = [
            _SPACE_RE.sub(" ", line).strip()
            for line in raw_text.splitlines()
            if _SPACE_RE.sub(" ", line).strip()
        ]
        return " ".join(rows).strip()

    @staticmethod
    def _normalize_rich_text(raw_text: str) -> str:
        raw_text = str(raw_text or "").strip()
        if not raw_text:
            return ""

        paragraphs: list[str] = []
        current: list[str] = []
        for raw_line in raw_text.splitlines():
            line = _SPACE_RE.sub(" ", raw_line).strip()
            if not line:
                if current:
                    paragraphs.append(" ".join(current).strip())
                    current = []
                continue
            current.append(line)

        if current:
            paragraphs.append(" ".join(current).strip())

        return "\n\n".join([item for item in paragraphs if item]).strip()

    def _render_equation(self, raw_text: str) -> str:
        payload = str(raw_text or "").strip()
        if not payload:
            return ""
        if payload.startswith("$$") and payload.endswith("$$"):
            return payload
        if payload.startswith("```math") and payload.endswith("```"):
            return payload
        normalized = self._normalize_rich_text(payload)
        if not normalized:
            return ""
        return f"$$\n{normalized}\n$$"

    @staticmethod
    def _render_table(rows: list[list[str]]) -> str:
        if len(rows) < 2:
            flattened = [" ".join(cell.strip() for cell in row if cell.strip()) for row in rows]
            return "\n".join([row for row in flattened if row]).strip()

        target_cols = max(len(row) for row in rows)
        normalized_rows = []
        for row in rows:
            cleaned = [str(cell or "").replace("|", "\\|").strip() for cell in row[:target_cols]]
            if len(cleaned) < target_cols:
                cleaned.extend([""] * (target_cols - len(cleaned)))
            normalized_rows.append(cleaned)

        header = normalized_rows[0]
        body = normalized_rows[1:]
        separator = ["---"] * target_cols
        markdown_rows = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(separator) + " |",
        ]
        markdown_rows.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(markdown_rows).strip()
