from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import httpx
from loguru import logger

from app.config import settings
from app.services.smart_chunking.types import ChunkLevel, generate_chunk_id


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_PAGE_NO_RE = re.compile(r"^(?:page\s*)?\d+(?:\s*/\s*\d+)?$", re.IGNORECASE)
_SYMBOL_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


ACTION_SYSTEM = (
    "You classify one extracted PDF line. "
    "Reply with one label only: KEEP, REPAIR, or OCR. "
    "Do not explain."
)
ACTION_USER_TEMPLATE = """Classify this PDF line.

Return exactly one label:
- KEEP
- REPAIR
- OCR

Line:
{text}"""

CLEAN_SYSTEM = (
    "You clean one extracted PDF line. "
    "Reply with the cleaned text only. "
    "Do not explain. "
    "Do not add information that is not present in the line."
)
CLEAN_USER_TEMPLATE = """Clean this extracted PDF line.

Return only the cleaned text.

Original line:
{text}"""

CHUNK_SYSTEM = (
    "You decide whether the current line should join the previous line in the same retrieval chunk. "
    "Reply with one label only: JOIN_PREV or NEW_CHUNK. "
    "Do not explain."
)
CHUNK_USER_TEMPLATE = """Decide whether the current line should stay in the same chunk as the previous line.

Return exactly one label:
- JOIN_PREV
- NEW_CHUNK

Previous line:
{prev_line}

Current line:
{curr_line}"""


def _normalize_spaces(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "")).strip()


def _clean_visible_text(text: str) -> str:
    cleaned = _CONTROL_CHARS.sub("", str(text or ""))
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u00a0", " ")
    return cleaned.strip()


def _safe_similarity(a: str, b: str) -> float:
    try:
        return float(SequenceMatcher(None, a or "", b or "").ratio())
    except Exception:
        return 0.0


@dataclass
class PdfLineRecord:
    source_order: int
    page: int
    page_line_index: int
    line_id: str
    line_uid: str
    raw_text: str
    source_text: str
    bbox: dict[str, float]
    column_slot: str
    raw_doc_start: int = 0
    raw_doc_end: int = 0


@dataclass
class ProcessedPdfLine:
    source: PdfLineRecord
    final_action: str
    normalized_text: str
    repair_used: bool = False
    ocr_used: bool = False
    ocr_text: Optional[str] = None
    debug: dict[str, Any] = field(default_factory=dict)


class _QwenAdapterRuntime:
    def __init__(self) -> None:
        self._lock = Lock()
        self._loaded = False
        self._load_error: Optional[str] = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = "cpu"

    @staticmethod
    def _resolve_model_dir(path_value: str) -> Path:
        path = Path(str(path_value or "").strip())
        if path.is_absolute():
            return path
        backend_root = Path(__file__).resolve().parents[2]
        return (backend_root / path).resolve()

    def _model_paths(self) -> dict[str, Path]:
        return {
            "action": self._resolve_model_dir(settings.pdf_rag_action_model_dir),
            "clean": self._resolve_model_dir(settings.pdf_rag_clean_model_dir),
            "chunk": self._resolve_model_dir(settings.pdf_rag_chunk_model_dir),
        }

    def available(self) -> bool:
        self._ensure_loaded()
        return self._model is not None

    @property
    def load_error(self) -> Optional[str]:
        self._ensure_loaded()
        return self._load_error

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                import torch
                from peft import PeftModel
                from transformers import AutoModelForCausalLM, AutoTokenizer

                model_paths = self._model_paths()
                for task_name, model_dir in model_paths.items():
                    if not model_dir.exists():
                        raise FileNotFoundError(f"{task_name} model dir missing: {model_dir}")
                    if not (model_dir / "adapter_config.json").exists():
                        raise FileNotFoundError(f"{task_name} adapter_config.json missing: {model_dir}")

                adapter_config = json.loads(
                    (model_paths["action"] / "adapter_config.json").read_text(encoding="utf-8")
                )
                base_model_name = str(adapter_config.get("base_model_name_or_path") or "").strip()
                if not base_model_name:
                    raise RuntimeError("action adapter missing base_model_name_or_path")

                requested_device = str(getattr(settings, "pdf_rag_qwen_device", "auto") or "auto").lower()
                if requested_device == "cuda":
                    self._device = "cuda"
                elif requested_device == "cpu":
                    self._device = "cpu"
                else:
                    self._device = "cuda" if torch.cuda.is_available() else "cpu"

                dtype = torch.float32
                if self._device == "cuda":
                    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

                self._tokenizer = AutoTokenizer.from_pretrained(
                    str(model_paths["action"]),
                    trust_remote_code=True,
                )
                if self._tokenizer.pad_token is None:
                    self._tokenizer.pad_token = self._tokenizer.eos_token
                self._tokenizer.padding_side = "left"

                base_model = AutoModelForCausalLM.from_pretrained(
                    base_model_name,
                    torch_dtype=dtype,
                    trust_remote_code=True,
                )
                model = PeftModel.from_pretrained(
                    base_model,
                    str(model_paths["action"]),
                    adapter_name="action",
                    is_trainable=False,
                )
                model.load_adapter(str(model_paths["clean"]), adapter_name="clean", is_trainable=False)
                model.load_adapter(str(model_paths["chunk"]), adapter_name="chunk", is_trainable=False)
                model.eval()
                model.to(self._device)
                self._model = model
                self._load_error = None
                logger.info(
                    f"[PdfRag] Qwen adapters loaded on device={self._device}: "
                    f"action={model_paths['action'].name}, clean={model_paths['clean'].name}, chunk={model_paths['chunk'].name}"
                )
            except Exception as exc:
                self._model = None
                self._tokenizer = None
                self._load_error = str(exc)
                logger.warning(f"[PdfRag] Qwen adapter runtime unavailable: {exc}")

    def _generate(self, *, adapter_name: str, messages: list[dict[str, str]], max_new_tokens: int) -> str:
        self._ensure_loaded()
        if self._model is None or self._tokenizer is None:
            raise RuntimeError(self._load_error or "Qwen adapter runtime unavailable")
        self._model.set_adapter(adapter_name)

        import torch

        prompt_text = self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        batch = self._tokenizer(prompt_text, return_tensors="pt")
        batch = {key: value.to(self._device) for key, value in batch.items()}
        with torch.no_grad():
            output_ids = self._model.generate(
                **batch,
                max_new_tokens=int(max_new_tokens),
                do_sample=False,
                pad_token_id=self._tokenizer.pad_token_id,
                eos_token_id=self._tokenizer.eos_token_id,
            )
        generated_ids = output_ids[0][batch["input_ids"].shape[1]:]
        return str(self._tokenizer.decode(generated_ids, skip_special_tokens=True) or "").strip()

    def classify_action(self, text: str) -> str:
        raw = self._generate(
            adapter_name="action",
            messages=[
                {"role": "system", "content": ACTION_SYSTEM},
                {"role": "user", "content": ACTION_USER_TEMPLATE.format(text=text)},
            ],
            max_new_tokens=4,
        ).upper()
        for label in ("KEEP", "REPAIR", "OCR"):
            if raw.startswith(label):
                return label
        for token in raw.replace("\n", " ").split():
            if token in {"KEEP", "REPAIR", "OCR"}:
                return token
        return "KEEP"

    def clean_line(self, text: str) -> str:
        return self._generate(
            adapter_name="clean",
            messages=[
                {"role": "system", "content": CLEAN_SYSTEM},
                {"role": "user", "content": CLEAN_USER_TEMPLATE.format(text=text)},
            ],
            max_new_tokens=96,
        )

    def classify_chunk(self, prev_line: str, curr_line: str) -> str:
        raw = self._generate(
            adapter_name="chunk",
            messages=[
                {"role": "system", "content": CHUNK_SYSTEM},
                {
                    "role": "user",
                    "content": CHUNK_USER_TEMPLATE.format(prev_line=prev_line, curr_line=curr_line),
                },
            ],
            max_new_tokens=4,
        ).upper()
        for label in ("JOIN_PREV", "NEW_CHUNK"):
            if raw.startswith(label):
                return label
        for token in raw.replace("\n", " ").split():
            if token in {"JOIN_PREV", "NEW_CHUNK"}:
                return token
        return "JOIN_PREV"


_runtime = _QwenAdapterRuntime()


class PdfRagIngestService:
    async def ingest_pdf(
        self,
        *,
        file_path: str,
        document_name: str = "",
    ) -> dict[str, Any]:
        lines, raw_document_text = self._extract_lines(file_path)
        if not lines or not raw_document_text.strip():
            return {
                "applied": False,
                "failure_reason": "no_pdf_lines",
                "document_text": "",
                "chunks": [],
                "extractor": "pdfplumber_lines",
                "report": {"line_count": 0},
            }

        if not _runtime.available():
            return {
                "applied": False,
                "failure_reason": f"qwen_runtime_unavailable:{_runtime.load_error or 'unknown'}",
                "document_text": raw_document_text,
                "chunks": [],
                "extractor": "pdfplumber_lines",
                "report": {"line_count": len(lines)},
            }

        processed_lines, dropped_lines, report = await self._process_lines(
            file_path=file_path,
            lines=lines,
        )
        if not processed_lines:
            if bool(settings.pdf_rag_fail_open):
                processed_lines = [
                    ProcessedPdfLine(
                        source=line,
                        final_action="KEEP",
                        normalized_text=line.source_text,
                        debug={"fallback": "fail_open_keep_all"},
                    )
                    for line in lines
                ]
                report["fail_open_applied"] = True
                dropped_lines = []
            else:
                return {
                    "applied": False,
                    "failure_reason": "no_accepted_lines",
                    "document_text": raw_document_text,
                    "chunks": [],
                    "extractor": "pdfplumber_lines",
                    "report": report,
                }

        chunks = await self._build_chunks(processed_lines)
        chunks, coverage_report = self._validate_and_fill_coverage(chunks, processed_lines)

        report.update(
            {
                "pipeline": "pdf_line_rag_v1",
                "document_name": document_name or "",
                "extractor": "pdfplumber_lines",
                "line_count": len(lines),
                "accepted_line_count": len(processed_lines),
                "dropped_line_count": len(dropped_lines),
                "chunk_count": len(chunks),
                "coverage": coverage_report,
                "dropped_line_ids": [line.source.line_id for line in dropped_lines[:200]],
            }
        )

        return {
            "applied": True,
            "failure_reason": None,
            "document_text": raw_document_text,
            "chunks": chunks,
            "extractor": "pdfplumber_lines",
            "report": report,
        }

    def _extract_lines(self, file_path: str) -> tuple[list[PdfLineRecord], str]:
        import pdfplumber

        extracted: list[PdfLineRecord] = []
        source_order = 0
        with pdfplumber.open(file_path) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                raw_lines = page.extract_text_lines(strip=True, return_chars=True) or []
                page_width = float(page.width or 0.0)
                for line_index, line in enumerate(raw_lines):
                    chars = list(line.get("chars") or [])
                    text = self._rebuild_text_from_chars(chars) or _clean_visible_text(line.get("text") or "")
                    text = _clean_visible_text(text)
                    if not text:
                        continue
                    source_text = _normalize_spaces(text)
                    if not source_text:
                        continue
                    x0 = float(line.get("x0") or 0.0)
                    top = float(line.get("top") or 0.0)
                    x1 = float(line.get("x1") or x0)
                    bottom = float(line.get("bottom") or top)
                    column_slot = self._infer_column_slot(x0=x0, x1=x1, page_width=page_width)
                    line_id = f"p{page_index}_l{line_index:03d}_{column_slot}"
                    line_uid = hashlib.sha256(
                        f"{page_index}|{x0:.2f}|{top:.2f}|{x1:.2f}|{bottom:.2f}|{source_text}".encode("utf-8")
                    ).hexdigest()[:24]
                    extracted.append(
                        PdfLineRecord(
                            source_order=source_order,
                            page=page_index,
                            page_line_index=line_index,
                            line_id=line_id,
                            line_uid=line_uid,
                            raw_text=text,
                            source_text=source_text,
                            bbox={"x0": x0, "top": top, "x1": x1, "bottom": bottom},
                            column_slot=column_slot,
                        )
                    )
                    source_order += 1

        raw_document_parts: list[str] = []
        offset = 0
        for line in extracted:
            line.raw_doc_start = offset
            raw_document_parts.append(line.raw_text)
            offset += len(line.raw_text)
            line.raw_doc_end = offset
            offset += 1
        return extracted, "\n".join(raw_document_parts)

    @staticmethod
    def _rebuild_text_from_chars(chars: list[dict[str, Any]]) -> str:
        if not chars:
            return ""
        ordered = sorted(
            (ch for ch in chars if str(ch.get("text") or "").strip() or ch.get("text") == " "),
            key=lambda item: (float(item.get("x0") or 0.0), float(item.get("top") or 0.0)),
        )
        out: list[str] = []
        prev: Optional[dict[str, Any]] = None
        for ch in ordered:
            current_text = str(ch.get("text") or "")
            if not current_text:
                continue
            if prev is not None:
                gap = float(ch.get("x0") or 0.0) - float(prev.get("x1") or 0.0)
                width = max(float(prev.get("width") or 0.0), float(ch.get("width") or 0.0), 1.0)
                size = max(float(prev.get("size") or 0.0), float(ch.get("size") or 0.0), 1.0)
                if gap > max(width * 0.45, size * 0.08) and out and out[-1] != " ":
                    out.append(" ")
            out.append(current_text)
            prev = ch
        return _clean_visible_text("".join(out))

    @staticmethod
    def _infer_column_slot(*, x0: float, x1: float, page_width: float) -> str:
        if page_width <= 0:
            return "main"
        if x0 <= page_width * 0.12 and x1 >= page_width * 0.88:
            return "main"
        center = (x0 + x1) / 2.0
        if center < page_width * 0.45:
            return "main_left"
        if center > page_width * 0.55:
            return "main_right"
        return "main"

    @staticmethod
    def _rule_action(text: str) -> Optional[str]:
        normalized = _normalize_spaces(text)
        if not normalized:
            return "DROP"
        if _PAGE_NO_RE.fullmatch(normalized):
            return "DROP"
        if len(normalized) <= 2 and _SYMBOL_ONLY_RE.fullmatch(normalized):
            return "DROP"
        if _SYMBOL_ONLY_RE.fullmatch(normalized) and len(normalized) >= 3:
            return "DROP"
        return None

    async def _process_lines(
        self,
        *,
        file_path: str,
        lines: list[PdfLineRecord],
    ) -> tuple[list[ProcessedPdfLine], list[ProcessedPdfLine], dict[str, Any]]:
        accepted: list[ProcessedPdfLine] = []
        dropped: list[ProcessedPdfLine] = []
        action_counts = {"KEEP": 0, "REPAIR": 0, "OCR": 0, "DROP": 0}
        repair_count = 0
        ocr_used_count = 0
        ocr_recovered_count = 0

        for line in lines:
            rule_action = self._rule_action(line.source_text)
            if rule_action == "DROP":
                dropped.append(
                    ProcessedPdfLine(
                        source=line,
                        final_action="DROP",
                        normalized_text="",
                        debug={"reason": "hard_rule_drop"},
                    )
                )
                action_counts["DROP"] += 1
                continue

            action = await asyncio.to_thread(_runtime.classify_action, line.source_text)
            if action not in {"KEEP", "REPAIR", "OCR"}:
                action = "KEEP"
            action_counts[action] += 1

            if action == "KEEP":
                accepted.append(
                    ProcessedPdfLine(
                        source=line,
                        final_action="KEEP",
                        normalized_text=line.source_text,
                    )
                )
                continue

            if action == "REPAIR":
                cleaned = await asyncio.to_thread(_runtime.clean_line, line.source_text)
                normalized = self._sanitize_clean_output(source_text=line.source_text, cleaned_text=cleaned)
                accepted.append(
                    ProcessedPdfLine(
                        source=line,
                        final_action="REPAIR",
                        normalized_text=normalized,
                        repair_used=True,
                        debug={"cleaned_text": cleaned},
                    )
                )
                repair_count += 1
                continue

            recovered = await self._recover_with_ocr(file_path=file_path, line=line)
            if not recovered:
                dropped.append(
                    ProcessedPdfLine(
                        source=line,
                        final_action="DROP",
                        normalized_text="",
                        debug={"reason": "ocr_empty"},
                    )
                )
                action_counts["DROP"] += 1
                continue

            ocr_used_count += 1
            recovered_norm = _normalize_spaces(recovered)
            recovered_action = await asyncio.to_thread(_runtime.classify_action, recovered_norm)
            if recovered_action == "REPAIR":
                cleaned = await asyncio.to_thread(_runtime.clean_line, recovered_norm)
                recovered_norm = self._sanitize_clean_output(source_text=recovered_norm, cleaned_text=cleaned)
                repair_used = True
                repair_count += 1
            else:
                repair_used = False

            if recovered_action == "OCR":
                dropped.append(
                    ProcessedPdfLine(
                        source=line,
                        final_action="DROP",
                        normalized_text="",
                        ocr_used=True,
                        ocr_text=recovered_norm,
                        debug={"reason": "ocr_recheck_failed"},
                    )
                )
                action_counts["DROP"] += 1
                continue

            accepted.append(
                ProcessedPdfLine(
                    source=line,
                    final_action="OCR",
                    normalized_text=recovered_norm,
                    repair_used=repair_used,
                    ocr_used=True,
                    ocr_text=recovered_norm,
                )
            )
            ocr_recovered_count += 1

        report = {
            "action_counts": action_counts,
            "repair_count": repair_count,
            "ocr_used_count": ocr_used_count,
            "ocr_recovered_count": ocr_recovered_count,
        }
        return accepted, dropped, report

    @staticmethod
    def _sanitize_clean_output(*, source_text: str, cleaned_text: str) -> str:
        source = _normalize_spaces(source_text)
        cleaned = _normalize_spaces(_clean_visible_text(cleaned_text))
        if not cleaned:
            return source
        if cleaned.upper() in {"KEEP", "REPAIR", "OCR", "JOIN_PREV", "NEW_CHUNK"}:
            return source
        if len(cleaned) > max(len(source) * 2, len(source) + 80):
            return source
        if _safe_similarity(source, cleaned) < 0.35:
            return source
        return cleaned

    async def _recover_with_ocr(self, *, file_path: str, line: PdfLineRecord) -> Optional[str]:
        if not bool(settings.pdf_rag_ocr_enabled):
            return None
        image_bytes = await asyncio.to_thread(self._render_line_crop, file_path, line)
        if not image_bytes:
            return None
        prompt = (
            "Extract the main text from this cropped PDF line image. "
            "Return plain text only. Do not explain. "
            "Use the hint only if it matches the image.\n\n"
            f"Hint extracted text: {line.source_text}"
        )
        payload = {
            "model": settings.pdf_rag_ocr_model,
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [base64.b64encode(image_bytes).decode("ascii")],
                }
            ],
            "options": {"temperature": 0},
        }
        url = f"{str(settings.ollama_base_url).rstrip('/')}/api/chat"
        timeout = max(5, int(settings.pdf_rag_ocr_timeout_seconds or 30))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning(f"[PdfRag] OCR request failed for {line.line_id}: {exc}")
            return None
        message = data.get("message") if isinstance(data, dict) else {}
        text = ""
        if isinstance(message, dict):
            text = str(message.get("content") or "").strip()
        if not text:
            return None
        return _clean_visible_text(text)

    @staticmethod
    def _render_line_crop(file_path: str, line: PdfLineRecord) -> bytes:
        import fitz
        from PIL import Image

        doc = fitz.open(file_path)
        try:
            page = doc[line.page - 1]
            padding = float(settings.pdf_rag_ocr_padding or 4.0)
            dpi = max(72, int(settings.pdf_rag_ocr_dpi or 180))
            scale = dpi / 72.0
            rect = fitz.Rect(
                max(0.0, float(line.bbox["x0"]) - padding),
                max(0.0, float(line.bbox["top"]) - padding),
                min(float(page.rect.width), float(line.bbox["x1"]) + padding),
                min(float(page.rect.height), float(line.bbox["bottom"]) + padding),
            )
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            doc.close()

    async def _build_chunks(self, accepted_lines: list[ProcessedPdfLine]) -> list[dict[str, Any]]:
        if not accepted_lines:
            return []
        groups: list[list[ProcessedPdfLine]] = []
        current_group: list[ProcessedPdfLine] = []
        prev_line: Optional[ProcessedPdfLine] = None

        for line in accepted_lines:
            if not current_group:
                current_group = [line]
                prev_line = line
                continue

            must_split = False
            if prev_line is None:
                must_split = True
            elif line.source.page != prev_line.source.page:
                must_split = True
            elif line.source.source_order != prev_line.source.source_order + 1:
                must_split = True

            if not must_split:
                decision = await asyncio.to_thread(
                    _runtime.classify_chunk,
                    prev_line.normalized_text,
                    line.normalized_text,
                )
                must_split = decision != "JOIN_PREV"

            if must_split:
                groups.append(current_group)
                current_group = [line]
            else:
                current_group.append(line)
            prev_line = line

        if current_group:
            groups.append(current_group)

        total_lines = max(1, len(accepted_lines))
        return [self._build_chunk_from_group(group, total_lines=total_lines) for group in groups if group]

    def _build_chunk_from_group(
        self,
        group: list[ProcessedPdfLine],
        *,
        total_lines: int,
    ) -> dict[str, Any]:
        normalized_text = "\n".join(item.normalized_text for item in group if item.normalized_text).strip()
        raw_text = "\n".join(item.source.raw_text for item in group if item.source.raw_text).strip()
        pages = sorted({item.source.page for item in group})
        page_boxes: list[dict[str, Any]] = []
        for page in pages:
            page_lines = [item for item in group if item.source.page == page]
            page_boxes.append(
                {
                    "page": page,
                    "bbox": {
                        "x0": min(item.source.bbox["x0"] for item in page_lines),
                        "top": min(item.source.bbox["top"] for item in page_lines),
                        "x1": max(item.source.bbox["x1"] for item in page_lines),
                        "bottom": max(item.source.bbox["bottom"] for item in page_lines),
                    },
                }
            )

        start_char = group[0].source.raw_doc_start
        end_char = group[-1].source.raw_doc_end
        chunk_id = generate_chunk_id(normalized_text or raw_text, start_char)
        meta = {
            "level": ChunkLevel.PARAGRAPH.value,
            "section_type": "pdf_line_chunk",
            "section_title": None,
            "has_citations": bool(re.search(r"\[[0-9,\-\s]+\]|\([12][0-9]{3}\)", normalized_text)),
            "position_ratio": float(group[-1].source.source_order + 1) / float(total_lines),
            "keywords": [],
            "extra": {
                "source_kind": "pdf_line_rag_v1",
                "raw_text": raw_text,
                "normalized_text": normalized_text,
                "line_ids": [item.source.line_id for item in group],
                "line_uids": [item.source.line_uid for item in group],
                "pages": pages,
                "page_bboxes": page_boxes,
                "line_count": len(group),
                "ocr_used": any(item.ocr_used for item in group),
                "repair_used": any(item.repair_used for item in group),
                "actions": [item.final_action for item in group],
            },
        }
        return {
            "id": chunk_id,
            "content": normalized_text or raw_text,
            "start_char": start_char,
            "end_char": end_char,
            "metadata": meta,
        }

    def _validate_and_fill_coverage(
        self,
        chunks: list[dict[str, Any]],
        accepted_lines: list[ProcessedPdfLine],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        seen: set[str] = set()
        duplicate_line_uids: list[str] = []
        for chunk in chunks:
            line_uids = list((((chunk.get("metadata") or {}).get("extra") or {}).get("line_uids") or []))
            for line_uid in line_uids:
                if line_uid in seen:
                    duplicate_line_uids.append(line_uid)
                seen.add(line_uid)

        missing_lines = [line for line in accepted_lines if line.source.line_uid not in seen]
        if missing_lines:
            for line in missing_lines:
                chunks.append(self._build_chunk_from_group([line], total_lines=max(1, len(accepted_lines))))
            chunks.sort(key=lambda item: int(item.get("start_char") or 0))

        report = {
            "assigned_line_count": len(seen),
            "missing_line_count": len(missing_lines),
            "duplicate_line_uid_count": len(duplicate_line_uids),
            "missing_line_ids": [line.source.line_id for line in missing_lines[:200]],
            "duplicate_line_uids": duplicate_line_uids[:200],
        }
        return chunks, report


_pdf_rag_ingest_service = PdfRagIngestService()


def get_pdf_rag_ingest_service() -> PdfRagIngestService:
    return _pdf_rag_ingest_service
