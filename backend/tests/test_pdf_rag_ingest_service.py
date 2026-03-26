from __future__ import annotations

import pytest

from app.services.pdf_rag_ingest_service import (
    PdfLineRecord,
    PdfRagIngestService,
    ProcessedPdfLine,
)
import app.services.pdf_rag_ingest_service as pdf_rag_module


def _line(
    *,
    order: int,
    page: int,
    text: str,
    start: int,
    end: int,
) -> PdfLineRecord:
    return PdfLineRecord(
        source_order=order,
        page=page,
        page_line_index=order,
        line_id=f"p{page}_l{order:03d}_main",
        line_uid=f"uid-{page}-{order}",
        raw_text=text,
        source_text=text,
        bbox={"x0": 10.0, "top": 20.0 + order, "x1": 200.0, "bottom": 30.0 + order},
        column_slot="main",
        raw_doc_start=start,
        raw_doc_end=end,
    )


class _FakeRuntime:
    load_error = None

    def available(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def classify_action(self, text: str) -> str:
        if "drop me" in text:
            return "DROP"
        if "split words" in text:
            return "REPAIR"
        return "KEEP"

    def clean_line(self, text: str) -> str:
        return text.replace("split words", "split-words fixed")

    def classify_chunk(self, prev_line: str, curr_line: str) -> str:
        if curr_line.startswith("Methods"):
            return "NEW_CHUNK"
        return "JOIN_PREV"


class _UnavailableRuntime:
    load_error = "missing peft"

    def available(self) -> bool:
        return False

    def release(self) -> None:
        return None


class _PhaseRecordingRuntime:
    load_error = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def release(self) -> None:
        return None

    def classify_action(self, text: str) -> str:
        self.calls.append(f"action:{text}")
        if "repair" in text.lower():
            return "REPAIR"
        return "KEEP"

    def clean_line(self, text: str) -> str:
        self.calls.append(f"clean:{text}")
        return text.replace("repair", "cleaned")

    def classify_chunk(self, prev_line: str, curr_line: str) -> str:
        self.calls.append(f"chunk:{curr_line}")
        if curr_line.startswith("Methods"):
            return "NEW_CHUNK"
        return "JOIN_PREV"


@pytest.mark.asyncio
async def test_ingest_pdf_builds_line_chunks_with_raw_and_normalized_metadata(monkeypatch):
    service = PdfRagIngestService()
    lines = [
        _line(order=0, page=1, text="Introduction", start=0, end=12),
        _line(order=1, page=1, text="This line has split words", start=13, end=38),
        _line(order=2, page=1, text="Methods", start=39, end=46),
        _line(order=3, page=1, text="We evaluated the system", start=47, end=70),
    ]
    monkeypatch.setattr(
        service,
        "_extract_lines",
        lambda _file_path: (lines, "\n".join(item.raw_text for item in lines)),
    )
    monkeypatch.setattr(pdf_rag_module, "_runtime", _FakeRuntime())

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    assert result["report"]["line_count"] == 4
    assert result["report"]["accepted_line_count"] == 4
    assert result["report"]["chunk_count"] == 2
    assert result["report"]["coverage"]["missing_line_count"] == 0

    first_chunk = result["chunks"][0]
    first_meta = first_chunk["metadata"]["extra"]
    assert "split-words fixed" in first_chunk["content"]
    assert "This line has split words" in first_meta["raw_text"]
    assert first_meta["line_ids"] == ["p1_l000_main", "p1_l001_main"]

    second_chunk = result["chunks"][1]
    second_meta = second_chunk["metadata"]["extra"]
    assert second_meta["line_ids"] == ["p1_l002_main", "p1_l003_main"]
    assert second_meta["pages"] == [1]


@pytest.mark.asyncio
async def test_ingest_pdf_preserves_source_order_in_chunk_metadata(monkeypatch):
    service = PdfRagIngestService()
    lines = [
        _line(order=0, page=1, text="Heading", start=0, end=7),
        _line(order=1, page=1, text="Alpha", start=8, end=13),
        _line(order=2, page=1, text="drop me footer", start=14, end=28),
        _line(order=3, page=1, text="Methods", start=29, end=36),
        _line(order=4, page=1, text="Beta", start=37, end=41),
    ]
    monkeypatch.setattr(
        service,
        "_extract_lines",
        lambda _file_path: (lines, "\n".join(item.raw_text for item in lines)),
    )
    monkeypatch.setattr(pdf_rag_module, "_runtime", _FakeRuntime())

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    all_line_ids = []
    for chunk in result["chunks"]:
        all_line_ids.extend(chunk["metadata"]["extra"]["line_ids"])
    assert all_line_ids == ["p1_l000_main", "p1_l001_main", "p1_l003_main", "p1_l004_main"]


@pytest.mark.asyncio
async def test_ingest_pdf_drop_lines_are_discarded_without_ocr(monkeypatch):
    service = PdfRagIngestService()
    lines = [
        _line(order=0, page=1, text="Intro", start=0, end=5),
        _line(order=1, page=1, text="drop me footer", start=6, end=20),
        _line(order=2, page=1, text="Body", start=21, end=25),
    ]
    monkeypatch.setattr(
        service,
        "_extract_lines",
        lambda _file_path: (lines, "\n".join(item.raw_text for item in lines)),
    )
    monkeypatch.setattr(pdf_rag_module, "_runtime", _FakeRuntime())

    async def _unexpected_ocr(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("OCR should not be called for DROP lines")

    monkeypatch.setattr(service, "_recover_with_ocr", _unexpected_ocr)

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    assert result["report"]["accepted_line_count"] == 2
    assert result["report"]["dropped_line_count"] == 1
    assert result["report"]["action_counts"]["DROP"] == 1
    assert result["report"]["ocr_used_count"] == 0
    assert result["report"]["dropped_line_ids"] == ["p1_l001_main"]


def test_validate_and_fill_coverage_adds_missing_lines_as_single_chunks():
    service = PdfRagIngestService()
    accepted = [
        ProcessedPdfLine(source=_line(order=0, page=1, text="Intro", start=0, end=5), final_action="KEEP", normalized_text="Intro"),
        ProcessedPdfLine(source=_line(order=1, page=1, text="Body", start=6, end=10), final_action="KEEP", normalized_text="Body"),
        ProcessedPdfLine(source=_line(order=2, page=1, text="Tail", start=11, end=15), final_action="KEEP", normalized_text="Tail"),
    ]
    chunks = [service._build_chunk_from_group(accepted[:2], total_lines=len(accepted))]

    final_chunks, report = service._validate_and_fill_coverage(chunks, accepted)

    assert report["missing_line_count"] == 1
    assert report["missing_line_ids"] == ["p1_l002_main"]
    assert len(final_chunks) == 2
    assert final_chunks[1]["metadata"]["extra"]["line_ids"] == ["p1_l002_main"]


@pytest.mark.asyncio
async def test_ingest_pdf_returns_unapplied_when_runtime_unavailable(monkeypatch):
    service = PdfRagIngestService()
    lines = [_line(order=0, page=1, text="Only line", start=0, end=9)]
    monkeypatch.setattr(
        service,
        "_extract_lines",
        lambda _file_path: (lines, "Only line"),
    )
    monkeypatch.setattr(pdf_rag_module, "_runtime", _UnavailableRuntime())

    result = await service.ingest_pdf(file_path="dummy.pdf")

    assert result["applied"] is False
    assert str(result["failure_reason"]).startswith("qwen_runtime_unavailable:")
    assert result["report"]["line_count"] == 1


@pytest.mark.asyncio
async def test_ingest_pdf_runs_action_phase_before_clean_phase(monkeypatch):
    service = PdfRagIngestService()
    runtime = _PhaseRecordingRuntime()
    lines = [
        _line(order=0, page=1, text="Introduction", start=0, end=12),
        _line(order=1, page=1, text="Needs repair alpha", start=13, end=31),
        _line(order=2, page=1, text="Needs repair beta", start=32, end=49),
        _line(order=3, page=1, text="Methods", start=50, end=57),
    ]
    monkeypatch.setattr(
        service,
        "_extract_lines",
        lambda _file_path: (lines, "\n".join(item.raw_text for item in lines)),
    )
    monkeypatch.setattr(pdf_rag_module, "_runtime", runtime)

    result = await service.ingest_pdf(file_path="dummy.pdf", document_name="paper.pdf")

    assert result["applied"] is True
    action_positions = [idx for idx, call in enumerate(runtime.calls) if call.startswith("action:")]
    clean_positions = [idx for idx, call in enumerate(runtime.calls) if call.startswith("clean:")]
    assert len(action_positions) == 4
    assert len(clean_positions) == 2
    assert max(action_positions) < min(clean_positions)
    assert "Needs cleaned alpha" in result["chunks"][0]["content"]
    assert "Needs cleaned beta" in result["chunks"][0]["content"]
