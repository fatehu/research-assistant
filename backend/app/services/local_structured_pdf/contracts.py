from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class PdfBBox:
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def width(self) -> float:
        return max(0.0, float(self.x1) - float(self.x0))

    @property
    def height(self) -> float:
        return max(0.0, float(self.bottom) - float(self.top))


@dataclass(frozen=True)
class PdfPageMeta:
    page: int
    page_width: float
    page_height: float
    rotation: int = 0


@dataclass(frozen=True)
class PdfWordAtom:
    word_id: str
    text: str
    bbox: PdfBBox
    doctop: float
    font_name: str = ""
    font_size: float = 0.0
    start_char_id: str = ""
    end_char_id: str = ""


@dataclass(frozen=True)
class PdfCharAtom:
    char_id: str
    text: str
    bbox: PdfBBox
    doctop: float
    font_name: str = ""
    font_size: float = 0.0


@dataclass(frozen=True)
class PdfImageAtom:
    image_id: str
    bbox: PdfBBox
    name: str = ""
    srcsize: str = ""
    bits: int = 0
    colorspace: str = ""


@dataclass(frozen=True)
class PdfLineAtom:
    line_id: str
    bbox: PdfBBox
    linewidth: float = 0.0
    stroking_color: str = ""


@dataclass(frozen=True)
class PdfRectAtom:
    rect_id: str
    bbox: PdfBBox
    linewidth: float = 0.0
    stroking_color: str = ""
    non_stroking_color: str = ""


@dataclass(frozen=True)
class PdfCurveAtom:
    curve_id: str
    bbox: PdfBBox
    linewidth: float = 0.0
    stroking_color: str = ""


@dataclass(frozen=True)
class PdfAnnotAtom:
    annot_id: str
    bbox: PdfBBox
    uri: str = ""
    title: str = ""
    contents: str = ""


@dataclass(frozen=True)
class PdfHyperlinkAtom:
    hyperlink_id: str
    bbox: PdfBBox
    uri: str = ""


@dataclass(frozen=True)
class PdfTextBlockAtom:
    block_id: str
    bbox: PdfBBox
    text: str = ""
    block_kind: str = "text"
    block_index: int = 0
    line_count: int = 0


@dataclass(frozen=True)
class PdfTableAtom:
    table_id: str
    bbox: PdfBBox
    row_count: int = 0
    col_count: int = 0
    cells: list[list[str]] = field(default_factory=list)


@dataclass
class PdfPageAtoms:
    meta: PdfPageMeta
    extract_text_raw: str = ""
    extract_text_fitz: str = ""
    words: list[PdfWordAtom] = field(default_factory=list)
    chars: list[PdfCharAtom] = field(default_factory=list)
    images: list[PdfImageAtom] = field(default_factory=list)
    lines: list[PdfLineAtom] = field(default_factory=list)
    rects: list[PdfRectAtom] = field(default_factory=list)
    curves: list[PdfCurveAtom] = field(default_factory=list)
    annots: list[PdfAnnotAtom] = field(default_factory=list)
    hyperlinks: list[PdfHyperlinkAtom] = field(default_factory=list)
    text_blocks: list[PdfTextBlockAtom] = field(default_factory=list)
    tables: list[PdfTableAtom] = field(default_factory=list)
    source_engines: list[str] = field(default_factory=list)
    mark_info_present: bool = False
    has_struct_tree: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def page(self) -> int:
        return int(self.meta.page)

    def is_empty(self) -> bool:
        return not any(
            (
                self.extract_text_raw.strip(),
                self.extract_text_fitz.strip(),
                self.words,
                self.chars,
                self.images,
                self.lines,
                self.rects,
                self.curves,
                self.annots,
                self.hyperlinks,
                self.text_blocks,
                self.tables,
            )
        )

    def word_map(self) -> dict[str, PdfWordAtom]:
        return {
            str(item.word_id): item
            for item in self.words
            if str(item.word_id or "").strip()
        }

    def char_map(self) -> dict[str, PdfCharAtom]:
        return {
            str(item.char_id): item
            for item in self.chars
            if str(item.char_id or "").strip()
        }


@dataclass(frozen=True)
class PdfTextLine:
    line_id: str
    page: int
    text: str
    bbox: PdfBBox
    word_ids: list[str]
    avg_font_size: float = 0.0
    dominant_font_name: str = ""
    band: str = "body"


@dataclass
class PdfNormalizedPage:
    meta: PdfPageMeta
    kept_words: list[PdfWordAtom] = field(default_factory=list)
    dropped_words: list[dict[str, Any]] = field(default_factory=list)
    table_bboxes: list[PdfBBox] = field(default_factory=list)
    text_blocks: list[PdfTextBlockAtom] = field(default_factory=list)
    text_lines: list[PdfTextLine] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def page(self) -> int:
        return int(self.meta.page)


@dataclass(frozen=True)
class PdfResolvedLine:
    line_id: str
    page: int
    text: str
    bbox: PdfBBox
    word_ids: list[str]
    avg_font_size: float = 0.0
    dominant_font_name: str = ""
    band: str = "body"
    region: str = "main"
    column_id: str = "main"
    reading_order: int = 0
    header_footer_role: Optional[str] = None


@dataclass
class PdfResolvedPage:
    meta: PdfPageMeta
    lines: list[PdfResolvedLine] = field(default_factory=list)
    dropped_lines: list[dict[str, Any]] = field(default_factory=list)
    column_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def page(self) -> int:
        return int(self.meta.page)


@dataclass
class PdfResolvedDocument:
    pages: list[PdfResolvedPage] = field(default_factory=list)
    header_signatures: list[str] = field(default_factory=list)
    footer_signatures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def ordered_text(self) -> str:
        rows: list[str] = []
        for page in self.pages:
            for line in page.lines:
                text = str(line.text or "").strip()
                if text:
                    rows.append(text)
        return "\n".join(rows).strip()


@dataclass(frozen=True)
class PdfSemanticBlock:
    block_id: str
    block_type: str
    page_start: int
    page_end: int
    text: str
    bbox: PdfBBox
    line_ids: list[str]
    column_id: str = "main"
    region: str = "main"
    avg_font_size: float = 0.0
    reading_order_start: int = 0
    reading_order_end: int = 0
    heading_level: Optional[int] = None
    parent_heading_id: Optional[str] = None
    section_heading_ids: list[str] = field(default_factory=list)
    section_titles: list[str] = field(default_factory=list)
    section_path: str = ""
    table_rows: list[list[str]] = field(default_factory=list)


@dataclass
class PdfStructuredPage:
    meta: PdfPageMeta
    blocks: list[PdfSemanticBlock] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def page(self) -> int:
        return int(self.meta.page)


@dataclass
class PdfStructuredDocument:
    pages: list[PdfStructuredPage] = field(default_factory=list)
    blocks: list[PdfSemanticBlock] = field(default_factory=list)
    body_font_size: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def ordered_text(self) -> str:
        rows = [str(block.text or "").strip() for block in self.blocks]
        return "\n\n".join([row for row in rows if row]).strip()


PdfAtom = Optional[
    PdfWordAtom
    | PdfCharAtom
    | PdfImageAtom
    | PdfLineAtom
    | PdfRectAtom
    | PdfCurveAtom
    | PdfAnnotAtom
    | PdfHyperlinkAtom
    | PdfTextBlockAtom
    | PdfTableAtom
]
