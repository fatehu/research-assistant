from __future__ import annotations

import re

from .contracts import PdfSemanticBlock, PdfStructuredDocument


_SPACE_RE = re.compile(r"\s+")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•·▪◦]|(?:\d+|[A-Za-z]|[ivxlcdm]+)[\.\)])\s+\S", re.IGNORECASE)


class LocalPdfMarkdownRenderer:
    """Render structured semantic blocks into benchmark-friendly Markdown."""

    def render_document(self, *, document: PdfStructuredDocument) -> str:
        rows: list[str] = []
        for block in list(document.blocks or []):
            rendered = self._render_block(block)
            if not rendered:
                continue
            rows.append(rendered)
        return "\n\n".join(rows).strip()

    def _render_block(self, block: PdfSemanticBlock) -> str:
        block_type = str(block.block_type or "")
        if block_type == "table" and block.table_rows:
            return self._render_table(block.table_rows)

        text = self._normalize_block_text(block)
        if not text:
            return ""
        if block_type == "heading":
            level = min(6, max(1, int(block.heading_level or 1)))
            return f'{"#" * level} {text}'
        if block_type == "list_item":
            return text if _LIST_ITEM_RE.match(text) else f"- {text}"
        return text

    @staticmethod
    def _normalize_block_text(block: PdfSemanticBlock) -> str:
        raw_text = str(block.text or "").strip()
        if not raw_text:
            return ""
        if str(block.block_type or "") == "heading":
            return _SPACE_RE.sub(" ", raw_text).strip()
        rows = [
            _SPACE_RE.sub(" ", line).strip()
            for line in raw_text.splitlines()
            if _SPACE_RE.sub(" ", line).strip()
        ]
        return " ".join(rows).strip()

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
