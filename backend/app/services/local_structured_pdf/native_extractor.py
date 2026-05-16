from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Sequence

try:
    from loguru import logger
except ImportError:  # pragma: no cover - optional dependency fallback
    import logging

    logger = logging.getLogger(__name__)

from .contracts import (
    PdfAnnotAtom,
    PdfBBox,
    PdfCharAtom,
    PdfCurveAtom,
    PdfHyperlinkAtom,
    PdfImageAtom,
    PdfLineAtom,
    PdfPageAtoms,
    PdfPageMeta,
    PdfRectAtom,
    PdfTableAtom,
    PdfTextBlockAtom,
    PdfWordAtom,
)


class LocalPdfNativeExtractor:
    """Stage-0 extractor: blend pdfplumber, PyMuPDF, and pypdf raw page signals."""

    @classmethod
    def runtime_status(cls) -> dict[str, bool]:
        return {
            "pdfplumber": cls._module_available("pdfplumber"),
            "pymupdf": cls._module_available("fitz"),
            "pypdf": cls._module_available("pypdf"),
        }

    @classmethod
    def ensure_runtime_dependencies(cls) -> None:
        status = cls.runtime_status()
        if status["pdfplumber"] or status["pymupdf"]:
            return
        raise RuntimeError(
            "Local structured PDF runtime is unavailable: neither pdfplumber nor "
            "PyMuPDF is installed in the current Python environment. Run the parser "
            "inside the backend container or an environment with PDF extraction "
            "dependencies installed."
        )

    def extract_page_atoms(
        self,
        *,
        pdf_path: str,
        page: int,
        include_chars: bool = True,
    ) -> PdfPageAtoms:
        page_number = max(1, int(page))
        path = Path(str(pdf_path or "").strip()).expanduser()
        fallback = self._fallback_page_atoms(page_number=page_number)
        if not path.is_file():
            return fallback

        plumber_pdf = None
        fitz_doc = None
        pypdf_reader = None
        try:
            # 每个文档只打开一次可用引擎；后续每页可以独立降级，
            # 不必为每个阶段重复解析 PDF。
            plumber_pdf = self._open_pdfplumber(path)
            fitz_doc = self._open_fitz(path)
            pypdf_reader = self._open_pypdf(path)

            page_count = self._detect_page_count(
                plumber_pdf=plumber_pdf,
                fitz_doc=fitz_doc,
                pypdf_reader=pypdf_reader,
            )
            if page_number > page_count:
                return fallback
            document_flags = self._extract_document_flags(pypdf_reader=pypdf_reader)
            return self._extract_page_atoms_from_handles(
                path=path,
                page_number=page_number,
                plumber_pdf=plumber_pdf,
                fitz_doc=fitz_doc,
                document_flags=document_flags,
                include_chars=include_chars,
            )
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] native extraction failed page={page_number}: {exc}")
            return fallback
        finally:
            self._close_quietly(plumber_pdf)
            self._close_quietly(fitz_doc)

    def extract_document_atoms(
        self,
        *,
        pdf_path: str,
        page_limit: int | None = None,
        include_chars: bool = True,
    ) -> list[PdfPageAtoms]:
        path = Path(str(pdf_path or "").strip()).expanduser()
        if not path.is_file():
            return []

        plumber_pdf = None
        fitz_doc = None
        pypdf_reader = None
        try:
            plumber_pdf = self._open_pdfplumber(path)
            fitz_doc = self._open_fitz(path)
            pypdf_reader = self._open_pypdf(path)

            page_count = self._detect_page_count(
                plumber_pdf=plumber_pdf,
                fitz_doc=fitz_doc,
                pypdf_reader=pypdf_reader,
            )
            if page_count <= 0:
                return []
            total_pages = page_count if page_limit is None else min(page_count, max(0, int(page_limit)))
            document_flags = self._extract_document_flags(pypdf_reader=pypdf_reader)
            extracted_pages: list[PdfPageAtoms] = []
            for page_number in range(1, total_pages + 1):
                try:
                    extracted_pages.append(
                        self._extract_page_atoms_from_handles(
                            path=path,
                            page_number=page_number,
                            plumber_pdf=plumber_pdf,
                            fitz_doc=fitz_doc,
                            document_flags=document_flags,
                            include_chars=include_chars,
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        f"[LocalStructuredPdf] page extraction failed page={page_number}: {exc}"
                    )
                    # 即使某个引擎在坏页上失败，也保留页数一致性。
                    extracted_pages.append(self._fallback_page_atoms(page_number=page_number))
            return extracted_pages
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] document extraction failed: {exc}")
            return []
        finally:
            self._close_quietly(plumber_pdf)
            self._close_quietly(fitz_doc)

    def _extract_page_atoms_from_handles(
        self,
        *,
        path: Path,
        page_number: int,
        plumber_pdf: Any,
        fitz_doc: Any,
        document_flags: dict[str, bool],
        include_chars: bool = True,
    ) -> PdfPageAtoms:
        fallback = self._fallback_page_atoms(page_number=page_number)
        page_index = page_number - 1
        plumber_page = self._get_plumber_page(plumber_pdf=plumber_pdf, page_index=page_index)
        fitz_page = self._get_fitz_page(fitz_doc=fitz_doc, page_index=page_index)

        if plumber_page is None and fitz_page is None:
            return fallback

        page_width, page_height, rotation = self._detect_page_geometry(
            plumber_page=plumber_page,
            fitz_page=fitz_page,
        )
        fitz_text = self._extract_fitz_text(fitz_page=fitz_page)
        plumber_text = self._extract_plumber_text(page_obj=plumber_page) if not fitz_text else ""

        page_atoms = PdfPageAtoms(
            meta=PdfPageMeta(
                page=page_number,
                page_width=round(page_width, 2),
                page_height=round(page_height, 2),
                rotation=rotation,
            ),
            extract_text_raw=fitz_text or plumber_text,
            extract_text_fitz=fitz_text,
            source_engines=self._detect_source_engines(
                plumber_page=plumber_page,
                fitz_page=fitz_page,
                document_flags=document_flags,
            ),
            mark_info_present=bool(document_flags.get("mark_info_present")),
            has_struct_tree=bool(document_flags.get("has_struct_tree")),
        )

        if fitz_page is not None:
            page_atoms.text_blocks = self._safe_page_stage(
                page_number=page_number,
                stage_name="text_blocks",
                default=[],
                extractor=lambda: self._extract_text_blocks(fitz_page=fitz_page),
            )
            page_atoms.images = self._coarse_images_from_text_blocks(
                text_blocks=page_atoms.text_blocks,
                fitz_page=fitz_page,
            )
        coarse_line_count = 0
        coarse_rect_count = 0
        coarse_curve_count = 0
        if fitz_page is not None:
            coarse_line_count, coarse_rect_count, coarse_curve_count = self._coarse_vector_counts_from_drawings(
                fitz_page=fitz_page,
            )
            page_atoms.coarse_line_count = int(coarse_line_count)
            page_atoms.coarse_rect_count = int(coarse_rect_count)
            page_atoms.coarse_curve_count = int(coarse_curve_count)

        if plumber_page is not None:
            skip_expensive_textual_stages = self._should_skip_words_and_tables_fast_path(
                page_atoms=page_atoms,
                line_like_count_override=coarse_line_count + coarse_rect_count,
                curve_count_override=coarse_curve_count,
            )
            # 图像/矢量密集的扫描页会让 pdfplumber 的词和表格阶段代价过高，
            # 但通常只能增加很少文本信号。
            if not skip_expensive_textual_stages:
                page_atoms.rects = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="rects",
                    default=[],
                    extractor=lambda: self._extract_rects(page_obj=plumber_page),
                )
                page_atoms.curves = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="curves",
                    default=[],
                    extractor=lambda: self._extract_curves(page_obj=plumber_page),
                )
                page_atoms.lines = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="lines",
                    default=[],
                    extractor=lambda: self._extract_lines(page_obj=plumber_page),
                )
                page_atoms.images = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="images",
                    default=[],
                    extractor=lambda: self._extract_images(page_obj=plumber_page),
                )
                page_atoms.words = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="words",
                    default=[],
                    extractor=lambda: self._extract_words(page_obj=plumber_page),
                )
                if include_chars:
                    page_atoms.chars = self._safe_page_stage(
                        page_number=page_number,
                        stage_name="chars",
                        default=[],
                        extractor=lambda: self._extract_chars(page_obj=plumber_page),
                    )
                    if page_atoms.words and page_atoms.chars:
                        page_atoms.words = self._attach_char_ranges(
                            words=page_atoms.words,
                            chars=page_atoms.chars,
                        )
            page_atoms.annots = self._safe_page_stage(
                page_number=page_number,
                stage_name="annots",
                default=[],
                extractor=lambda: self._extract_annots(page_obj=plumber_page),
            )
            page_atoms.hyperlinks = self._safe_page_stage(
                page_number=page_number,
                stage_name="hyperlinks",
                default=[],
                extractor=lambda: self._extract_hyperlinks(page_obj=plumber_page),
            )

        if fitz_page is not None:
            if (
                not self._should_skip_words_and_tables_fast_path(page_atoms=page_atoms)
                and self._should_probe_tables(page_atoms=page_atoms)
            ):
                # 表格检测先经过廉价的矢量/文本信号门控，因为 PyMuPDF 的表格探测
                # 在图形密集页面上成本较高。
                page_atoms.tables = self._safe_page_stage(
                    page_number=page_number,
                    stage_name="tables",
                    default=[],
                    extractor=lambda: self._extract_tables(fitz_page=fitz_page),
                )

        return page_atoms

    @staticmethod
    def _fallback_page_atoms(*, page_number: int) -> PdfPageAtoms:
        return PdfPageAtoms(
            meta=PdfPageMeta(
                page=page_number,
                page_width=0.0,
                page_height=0.0,
                rotation=0,
            )
        )

    @staticmethod
    def _close_quietly(resource: Any) -> None:
        try:
            if resource is not None and hasattr(resource, "close"):
                resource.close()
        except Exception:
            return

    @staticmethod
    def _module_available(module_name: str) -> bool:
        try:
            return importlib.util.find_spec(module_name) is not None
        except Exception:
            return False

    @staticmethod
    def _safe_page_stage(
        *,
        page_number: int,
        stage_name: str,
        default: Any,
        extractor: Any,
    ) -> Any:
        try:
            return extractor()
        except Exception as exc:
            # 部分 PDF 库经常只在某一类解析对象上失败，而文本抽取仍然成功；
            # 保持结构化页面的其他部分可用。
            logger.debug(
                f"[LocalStructuredPdf] page stage failed page={page_number} stage={stage_name}: {exc}"
            )
            return default

    @staticmethod
    def _open_pdfplumber(path: Path) -> Any:
        try:
            import pdfplumber

            return pdfplumber.open(str(path))
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] pdfplumber unavailable: {exc}")
            return None

    @staticmethod
    def _open_fitz(path: Path) -> Any:
        try:
            import fitz

            return fitz.open(str(path))
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] PyMuPDF unavailable: {exc}")
            return None

    @staticmethod
    def _open_pypdf(path: Path) -> Any:
        try:
            from pypdf import PdfReader

            return PdfReader(str(path))
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] pypdf unavailable: {exc}")
            return None

    @staticmethod
    def _detect_page_count(*, plumber_pdf: Any, fitz_doc: Any, pypdf_reader: Any) -> int:
        try:
            if plumber_pdf is not None:
                return int(len(plumber_pdf.pages))
        except Exception:
            pass
        try:
            if fitz_doc is not None:
                return int(fitz_doc.page_count)
        except Exception:
            pass
        try:
            if pypdf_reader is not None:
                return int(len(pypdf_reader.pages))
        except Exception:
            pass
        return 0

    @staticmethod
    def _extract_document_flags(*, pypdf_reader: Any) -> dict[str, bool]:
        flags = {
            "pypdf_available": pypdf_reader is not None,
            "mark_info_present": False,
            "has_struct_tree": False,
        }
        if pypdf_reader is None:
            return flags
        try:
            root = pypdf_reader.trailer.get("/Root")
            if hasattr(root, "get_object"):
                root = root.get_object()
            if not root:
                return flags
            mark_info = root.get("/MarkInfo")
            if hasattr(mark_info, "get_object"):
                mark_info = mark_info.get_object()
            struct_tree_root = root.get("/StructTreeRoot")
            if hasattr(struct_tree_root, "get_object"):
                struct_tree_root = struct_tree_root.get_object()
            flags["mark_info_present"] = mark_info is not None
            flags["has_struct_tree"] = struct_tree_root is not None
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] pypdf metadata inspection failed: {exc}")
        return flags

    @staticmethod
    def _get_plumber_page(*, plumber_pdf: Any, page_index: int) -> Any:
        try:
            if plumber_pdf is None:
                return None
            if page_index < 0 or page_index >= len(plumber_pdf.pages):
                return None
            return plumber_pdf.pages[page_index]
        except Exception:
            return None

    @staticmethod
    def _get_fitz_page(*, fitz_doc: Any, page_index: int) -> Any:
        try:
            if fitz_doc is None:
                return None
            if page_index < 0 or page_index >= int(fitz_doc.page_count):
                return None
            return fitz_doc.load_page(page_index)
        except Exception:
            return None

    def _detect_page_geometry(self, *, plumber_page: Any, fitz_page: Any) -> tuple[float, float, int]:
        width = 0.0
        height = 0.0
        rotation = 0
        try:
            if plumber_page is not None:
                width = self._safe_float(getattr(plumber_page, "width", 0.0))
                height = self._safe_float(getattr(plumber_page, "height", 0.0))
                rotation = self._safe_int(getattr(plumber_page, "rotation", 0))
        except Exception:
            pass
        try:
            if fitz_page is not None and (width <= 0.0 or height <= 0.0):
                rect = fitz_page.rect
                width = self._safe_float(getattr(rect, "width", width))
                height = self._safe_float(getattr(rect, "height", height))
                rotation = self._safe_int(getattr(fitz_page, "rotation", rotation))
        except Exception:
            pass
        return width, height, rotation

    @staticmethod
    def _detect_source_engines(*, plumber_page: Any, fitz_page: Any, document_flags: dict[str, bool]) -> list[str]:
        engines: list[str] = []
        if plumber_page is not None:
            engines.append("pdfplumber")
        if fitz_page is not None:
            engines.append("pymupdf")
        if document_flags.get("pypdf_available"):
            engines.append("pypdf")
        return engines

    @classmethod
    def _extract_plumber_text(cls, *, page_obj: Any) -> str:
        if page_obj is None:
            return ""
        try:
            return str(page_obj.extract_text(x_tolerance=1.5, y_tolerance=3) or "")
        except Exception:
            return ""

    @staticmethod
    def _extract_fitz_text(*, fitz_page: Any) -> str:
        if fitz_page is None:
            return ""
        try:
            return str(fitz_page.get_text("text", sort=True) or "")
        except Exception:
            return ""

    @staticmethod
    def _normalize_spaces(text: str) -> str:
        return " ".join(str(text or "").split()).strip()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @classmethod
    def _bbox_from_row(cls, row: dict[str, Any]) -> PdfBBox:
        x0 = cls._safe_float(row.get("x0"))
        x1 = cls._safe_float(row.get("x1"), x0)
        top = cls._safe_float(row.get("top") or row.get("doctop"))
        bottom = cls._safe_float(row.get("bottom"), top)
        return PdfBBox(
            x0=round(x0, 2),
            top=round(top, 2),
            x1=round(x1, 2),
            bottom=round(bottom, 2),
        )

    @classmethod
    def _bbox_from_tuple(cls, bbox: Sequence[Any] | None) -> PdfBBox:
        values = list(bbox or [0.0, 0.0, 0.0, 0.0])
        while len(values) < 4:
            values.append(0.0)
        x0, top, x1, bottom = values[:4]
        return PdfBBox(
            x0=round(cls._safe_float(x0), 2),
            top=round(cls._safe_float(top), 2),
            x1=round(cls._safe_float(x1), 2),
            bottom=round(cls._safe_float(bottom), 2),
        )

    @classmethod
    def _extract_words(cls, *, page_obj: Any) -> list[PdfWordAtom]:
        raw_words = page_obj.extract_words(
            x_tolerance=1.5,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["fontname", "size"],
        ) or []
        atoms: list[PdfWordAtom] = []
        for index, row in enumerate(raw_words, start=1):
            if not isinstance(row, dict):
                continue
            text = cls._normalize_spaces(str(row.get("text") or ""))
            if not text:
                continue
            atoms.append(
                PdfWordAtom(
                    word_id=f"w{index:06d}",
                    text=text[:120],
                    bbox=cls._bbox_from_row(row),
                    doctop=round(cls._safe_float(row.get("doctop") or row.get("top")), 2),
                    font_name=str(row.get("fontname") or "")[:120],
                    font_size=round(cls._safe_float(row.get("size")), 2),
                )
            )
        return atoms

    @classmethod
    def _extract_chars(cls, *, page_obj: Any) -> list[PdfCharAtom]:
        atoms: list[PdfCharAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "chars", []) or []), start=1):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "")
            if not text:
                continue
            atoms.append(
                PdfCharAtom(
                    char_id=f"c{index:06d}",
                    text=text[:8],
                    bbox=cls._bbox_from_row(row),
                    doctop=round(cls._safe_float(row.get("doctop") or row.get("top")), 2),
                    font_name=str(row.get("fontname") or "")[:120],
                    font_size=round(cls._safe_float(row.get("size")), 2),
                )
            )
        return atoms

    @classmethod
    def _attach_char_ranges(
        cls,
        *,
        words: Sequence[PdfWordAtom],
        chars: Sequence[PdfCharAtom],
    ) -> list[PdfWordAtom]:
        updated: list[PdfWordAtom] = []
        for word in words:
            matched_ids: list[str] = []
            wx0 = float(word.bbox.x0)
            wx1 = float(word.bbox.x1)
            wtop = float(word.bbox.top)
            wbottom = float(word.bbox.bottom)
            for ch in chars:
                if ch.bbox.bottom < (wtop - 1.2) or ch.bbox.top > (wbottom + 1.2):
                    continue
                center_x = ch.bbox.x0 + (ch.bbox.width / 2.0)
                if center_x < (wx0 - 0.8) or center_x > (wx1 + 0.8):
                    continue
                matched_ids.append(ch.char_id)
            if matched_ids:
                updated.append(
                    PdfWordAtom(
                        word_id=word.word_id,
                        text=word.text,
                        bbox=word.bbox,
                        doctop=word.doctop,
                        font_name=word.font_name,
                        font_size=word.font_size,
                        start_char_id=matched_ids[0],
                        end_char_id=matched_ids[-1],
                    )
                )
                continue
            updated.append(word)
        return updated

    @classmethod
    def _extract_images(cls, *, page_obj: Any) -> list[PdfImageAtom]:
        atoms: list[PdfImageAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "images", []) or [])[:256], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfImageAtom(
                    image_id=f"img{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    name=str(row.get("name") or "")[:180],
                    srcsize=str(row.get("srcsize") or "")[:80],
                    bits=cls._safe_int(row.get("bits")),
                    colorspace=str(row.get("colorspace") or "")[:80],
                )
            )
        return atoms

    @classmethod
    def _coarse_images_from_text_blocks(
        cls,
        *,
        text_blocks: Sequence[PdfTextBlockAtom],
        fitz_page: Any | None = None,
    ) -> list[PdfImageAtom]:
        atoms: list[PdfImageAtom] = []
        for index, block in enumerate(list(text_blocks or [])[:256], start=1):
            if str(getattr(block, "block_kind", "")).lower() != "image":
                continue
            bbox = getattr(block, "bbox", None)
            if bbox is None:
                continue
            atoms.append(
                PdfImageAtom(
                    image_id=f"tbimg{index:04d}",
                    bbox=bbox,
                    name="pymupdf_block_image",
                )
            )
        if atoms or fitz_page is None:
            return atoms
        try:
            raw_dict = fitz_page.get_text("dict", sort=False) or {}
            raw_blocks = list(raw_dict.get("blocks") or [])
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] PyMuPDF dict block extraction failed: {exc}")
            return atoms
        for block in raw_blocks[:256]:
            if not isinstance(block, dict):
                continue
            if cls._safe_int(block.get("type"), default=0) != 1:
                continue
            bbox = cls._bbox_from_tuple(block.get("bbox"))
            atoms.append(
                PdfImageAtom(
                    image_id=f"tbimg{len(atoms) + 1:04d}",
                    bbox=bbox,
                    name="pymupdf_dict_image",
                )
            )
        return atoms

    @classmethod
    def _coarse_vector_counts_from_drawings(cls, *, fitz_page: Any | None) -> tuple[int, int, int]:
        if fitz_page is None:
            return 0, 0, 0
        try:
            drawings = list(fitz_page.get_drawings() or [])
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] PyMuPDF drawing extraction failed: {exc}")
            return 0, 0, 0
        line_count = 0
        rect_count = 0
        curve_count = 0
        for drawing in drawings:
            for item in list(drawing.get("items") or []):
                op = str(item[0] or "")
                if op == "l":
                    line_count += 1
                elif op == "re":
                    rect_count += 1
                elif op in {"c", "v", "y"}:
                    curve_count += 1
        return line_count, rect_count, curve_count

    @classmethod
    def _extract_lines(cls, *, page_obj: Any) -> list[PdfLineAtom]:
        atoms: list[PdfLineAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "lines", []) or [])[:1200], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfLineAtom(
                    line_id=f"ln{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    linewidth=round(cls._safe_float(row.get("linewidth")), 2),
                    stroking_color=str(row.get("stroking_color") or "")[:120],
                )
            )
        return atoms

    @classmethod
    def _extract_rects(cls, *, page_obj: Any) -> list[PdfRectAtom]:
        atoms: list[PdfRectAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "rects", []) or [])[:1200], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfRectAtom(
                    rect_id=f"rc{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    linewidth=round(cls._safe_float(row.get("linewidth")), 2),
                    stroking_color=str(row.get("stroking_color") or "")[:120],
                    non_stroking_color=str(row.get("non_stroking_color") or "")[:120],
                )
            )
        return atoms

    @classmethod
    def _extract_curves(cls, *, page_obj: Any) -> list[PdfCurveAtom]:
        atoms: list[PdfCurveAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "curves", []) or [])[:1200], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfCurveAtom(
                    curve_id=f"cv{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    linewidth=round(cls._safe_float(row.get("linewidth")), 2),
                    stroking_color=str(row.get("stroking_color") or "")[:120],
                )
            )
        return atoms

    @classmethod
    def _extract_annots(cls, *, page_obj: Any) -> list[PdfAnnotAtom]:
        atoms: list[PdfAnnotAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "annots", []) or [])[:400], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfAnnotAtom(
                    annot_id=f"an{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    uri=str(row.get("uri") or "")[:300],
                    title=str(row.get("title") or "")[:200],
                    contents=str(row.get("contents") or "")[:500],
                )
            )
        return atoms

    @classmethod
    def _extract_hyperlinks(cls, *, page_obj: Any) -> list[PdfHyperlinkAtom]:
        atoms: list[PdfHyperlinkAtom] = []
        for index, row in enumerate(list(getattr(page_obj, "hyperlinks", []) or [])[:400], start=1):
            if not isinstance(row, dict):
                continue
            atoms.append(
                PdfHyperlinkAtom(
                    hyperlink_id=f"lk{index:04d}",
                    bbox=cls._bbox_from_row(row),
                    uri=str(row.get("uri") or "")[:300],
                )
            )
        return atoms

    @classmethod
    def _extract_text_blocks(cls, *, fitz_page: Any) -> list[PdfTextBlockAtom]:
        if fitz_page is None:
            return []
        atoms: list[PdfTextBlockAtom] = []
        try:
            raw_blocks = list(fitz_page.get_text("blocks", sort=False) or [])
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] PyMuPDF block extraction failed: {exc}")
            return []
        for index, row in enumerate(raw_blocks[:512], start=1):
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            text = cls._normalize_spaces(str(row[4] or ""))
            block_kind = "image" if int(row[6]) == 1 else "text" if len(row) > 6 else "text"
            atoms.append(
                PdfTextBlockAtom(
                    block_id=f"tb{index:04d}",
                    bbox=cls._bbox_from_tuple(row[:4]),
                    text=text[:4000],
                    block_kind=block_kind,
                    block_index=cls._safe_int(row[5] if len(row) > 5 else index),
                    line_count=max(1, len([item for item in str(row[4] or "").splitlines() if item.strip()])) if text else 0,
                )
            )
        return atoms

    @classmethod
    def _extract_tables(cls, *, fitz_page: Any) -> list[PdfTableAtom]:
        if fitz_page is None or not hasattr(fitz_page, "find_tables"):
            return []
        try:
            table_finder = fitz_page.find_tables()
            raw_tables = list(getattr(table_finder, "tables", []) or [])
        except Exception as exc:
            logger.debug(f"[LocalStructuredPdf] PyMuPDF table extraction failed: {exc}")
            return []

        atoms: list[PdfTableAtom] = []
        for index, table in enumerate(raw_tables[:64], start=1):
            try:
                extracted = table.extract() or []
            except Exception:
                extracted = []
            cells: list[list[str]] = []
            for row in list(extracted or []):
                if not isinstance(row, (list, tuple)):
                    continue
                cells.append([cls._normalize_spaces(str(cell or ""))[:1000] for cell in row])
            atoms.append(
                PdfTableAtom(
                    table_id=f"ft{index:04d}",
                    bbox=cls._bbox_from_tuple(getattr(table, "bbox", None)),
                    row_count=cls._safe_int(getattr(table, "row_count", len(cells))),
                    col_count=cls._safe_int(
                        getattr(
                            table,
                            "col_count",
                            max((len(row) for row in cells), default=0),
                        )
                    ),
                    cells=cells,
                )
            )
        return atoms

    @staticmethod
    def _should_probe_tables(*, page_atoms: PdfPageAtoms) -> bool:
        line_like_count = len(list(page_atoms.lines or [])) + len(list(page_atoms.rects or []))
        curve_count = len(list(page_atoms.curves or []))
        word_count = len(list(page_atoms.words or []))
        if line_like_count >= 6:
            return True
        if line_like_count >= 4 and curve_count >= 2:
            return True
        if line_like_count >= 3 and word_count >= 24:
            return True
        return False

    @classmethod
    def _should_skip_words_and_tables_fast_path(
        cls,
        *,
        page_atoms: PdfPageAtoms,
        line_like_count_override: int | None = None,
        curve_count_override: int | None = None,
    ) -> bool:
        text = str(getattr(page_atoms, "extract_text_raw", "") or "").strip()
        text_char_count = len(text)
        text_token_count = len([token for token in text.split() if token])

        page_width = float(getattr(getattr(page_atoms, "meta", None), "page_width", 0.0) or 0.0)
        page_height = float(getattr(getattr(page_atoms, "meta", None), "page_height", 0.0) or 0.0)
        page_area = max(1.0, page_width * page_height)

        image_areas: list[float] = []
        for image in list(getattr(page_atoms, "images", []) or []):
            bbox = getattr(image, "bbox", None)
            if bbox is None:
                continue
            width = max(0.0, float(getattr(bbox, "x1", 0.0) or 0.0) - float(getattr(bbox, "x0", 0.0) or 0.0))
            height = max(0.0, float(getattr(bbox, "bottom", 0.0) or 0.0) - float(getattr(bbox, "top", 0.0) or 0.0))
            area = width * height
            if area > 0.0:
                image_areas.append(area)
        largest_image_ratio = (max(image_areas) / page_area) if image_areas else 0.0
        total_image_ratio = (sum(image_areas) / page_area) if image_areas else 0.0

        line_like_count = (
            int(line_like_count_override)
            if line_like_count_override is not None
            else len(list(page_atoms.lines or [])) + len(list(page_atoms.rects or []))
        )
        curve_count = (
            int(curve_count_override)
            if curve_count_override is not None
            else len(list(page_atoms.curves or []))
        )
        vector_line_count = line_like_count + curve_count

        very_low_text = text_char_count <= 24 and text_token_count <= 4
        # 快速路径只用于看起来像扫描件、幻灯片或密集矢量图的页面；
        # 普通原生数字文本仍会执行词和表格抽取。
        if very_low_text and largest_image_ratio >= 0.70 and total_image_ratio >= 0.78:
            return True
        if (
            very_low_text
            and vector_line_count >= 220
            and line_like_count >= 160
            and curve_count >= 60
        ):
            return True
        return False
