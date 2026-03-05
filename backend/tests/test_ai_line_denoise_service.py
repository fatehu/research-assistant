import pytest

from app.config import settings
from app.services.ai_line_denoise_service import AILineDenoiseService, LineUnit


def test_build_batches_respects_max_lines_per_call():
    lines = [LineUnit(line_id=i + 1, text=f"line-{i+1}") for i in range(10)]
    batches = AILineDenoiseService._build_batches(lines, max_lines_per_call=3)
    assert len(batches) == 4
    assert all(len(batch) <= 3 for batch in batches)
    assert [line.line_id for line in batches[0]] == [1, 2, 3]
    assert [line.line_id for line in batches[-1]] == [10]


def test_is_hard_noise_line_detects_a111_pattern():
    assert AILineDenoiseService._is_hard_noise_line("a1111111111")
    assert not AILineDenoiseService._is_hard_noise_line("This is a normal sentence 2023.")


@pytest.mark.asyncio
async def test_review_batch_uses_majority_vote(monkeypatch):
    service = AILineDenoiseService()
    monkeypatch.setattr(settings, "ai_line_denoise_parallel_votes", 3, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_retry_rounds", 1, raising=False)

    votes = [
        {"drop_line_ids": [2], "keep_line_ids": [1], "reason": "noise"},
        {"drop_line_ids": [2], "keep_line_ids": [1], "reason": "noise"},
        {"drop_line_ids": [], "keep_line_ids": [1, 2], "reason": "keep"},
    ]

    async def fake_review_batch_once(*, document_name, batch_lines, vote_index):
        return votes[vote_index - 1]

    monkeypatch.setattr(service, "_review_batch_once", fake_review_batch_once)
    lines = [LineUnit(1, "normal content"), LineUnit(2, "A111111111")]
    dropped, malformed, valid_votes = await service._review_batch(
        document_name="paper.pdf",
        batch_lines=lines,
    )

    assert 2 in dropped
    assert 1 not in dropped
    assert malformed == 0
    assert valid_votes == 3


@pytest.mark.asyncio
async def test_denoise_fail_open_when_votes_invalid(monkeypatch):
    service = AILineDenoiseService()
    monkeypatch.setattr(settings, "ai_line_denoise_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_fail_open", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_parallel_votes", 3, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_retry_rounds", 1, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_join_lines_with_space", True, raising=False)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def fake_review_batch_once(*, document_name, batch_lines, vote_index):
        return {"unexpected": "shape"}

    monkeypatch.setattr(service, "_review_batch_once", fake_review_batch_once)

    text = "First line\nA111111111\nLast line"
    result = await service.denoise_text(text, document_name="paper.pdf", file_type="pdf")
    assert result["report"]["enabled"] is True
    assert result["report"]["dropped_lines"] == 1
    assert result["report"]["rule_dropped_lines"] == 1
    assert "A111111111" not in result["text"]
    assert "\n" not in result["text"]


@pytest.mark.asyncio
async def test_denoise_reports_dropped_line_spans(monkeypatch):
    service = AILineDenoiseService()
    monkeypatch.setattr(settings, "ai_line_denoise_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_fail_open", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_parallel_votes", 3, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_retry_rounds", 1, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_join_lines_with_space", True, raising=False)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    votes = [
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
    ]

    async def fake_review_batch_once(*, document_name, batch_lines, vote_index):
        return votes[vote_index - 1]

    monkeypatch.setattr(service, "_review_batch_once", fake_review_batch_once)

    text = "First line\nA111111111\nLast line"
    line_spans = [
        {"line_id": 1, "text": "First line", "page": 1, "x0": 80, "y0": 700, "x1": 180, "y1": 712},
        {"line_id": 2, "text": "A111111111", "page": 1, "x0": 82, "y0": 680, "x1": 162, "y1": 692},
        {"line_id": 3, "text": "Last line", "page": 1, "x0": 84, "y0": 660, "x1": 172, "y1": 672},
    ]
    result = await service.denoise_text(
        text,
        document_name="paper.pdf",
        file_type="pdf",
        line_spans=line_spans,
    )
    report = dict(result.get("report") or {})
    assert report.get("line_spans_available") is True
    dropped_spans = list(report.get("dropped_line_spans") or [])
    assert len(dropped_spans) == 1
    assert int(dropped_spans[0].get("line_id") or 0) == 2
    assert int(dropped_spans[0].get("page") or 0) == 1


@pytest.mark.asyncio
async def test_denoise_should_recover_drop_lines_with_ocr(monkeypatch):
    service = AILineDenoiseService()
    monkeypatch.setattr(settings, "ai_line_denoise_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_fail_open", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_parallel_votes", 3, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_retry_rounds", 1, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_join_lines_with_space", False, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_drop_ocr_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_line_denoise_drop_ocr_model", "qwen3.5:0.8b-stable", raising=False)
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    votes = [
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
        {"drop_line_ids": [2], "keep_line_ids": [1, 3], "reason": "noise"},
    ]

    async def fake_review_batch_once(*, document_name, batch_lines, vote_index):
        return votes[vote_index - 1]

    async def fake_recover_dropped_lines_with_ocr(*, pdf_path, dropped_spans):
        assert pdf_path == "/tmp/paper.pdf"
        assert len(list(dropped_spans or [])) == 1
        return {
            "recovered_map": {2: "Recovered content from OCR"},
            "attempted": 1,
            "recovered": 1,
            "errors": 0,
            "rows": [{"line_id": 2, "accepted": True, "confidence": 0.92, "ocr_text": "Recovered content from OCR"}],
        }

    monkeypatch.setattr(service, "_review_batch_once", fake_review_batch_once)
    monkeypatch.setattr(service, "_recover_dropped_lines_with_ocr", fake_recover_dropped_lines_with_ocr)

    text = "Line one\nA111111111\nLine three"
    line_spans = [
        {"line_id": 1, "text": "Line one", "page": 1, "x0": 50, "y0": 700, "x1": 120, "y1": 712},
        {"line_id": 2, "text": "A111111111", "page": 1, "x0": 52, "y0": 680, "x1": 134, "y1": 692},
        {"line_id": 3, "text": "Line three", "page": 1, "x0": 50, "y0": 660, "x1": 146, "y1": 672},
    ]
    result = await service.denoise_text(
        text,
        document_name="paper.pdf",
        file_type="pdf",
        line_spans=line_spans,
        pdf_path="/tmp/paper.pdf",
    )
    report = dict(result.get("report") or {})
    assert "Recovered content from OCR" in str(result.get("text") or "")
    assert "A111111111" not in str(result.get("text") or "")
    assert int(report.get("drop_ocr_attempted") or 0) == 1
    assert int(report.get("drop_ocr_recovered") or 0) == 1
    assert int(report.get("dropped_lines", -1)) == 0
