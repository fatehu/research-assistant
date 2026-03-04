import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.chunk_quality_gate_service import (
    ChunkDecision,
    ChunkQualityGateService,
    ChunkRepair,
)


@pytest.fixture(autouse=True)
def _gate_defaults(monkeypatch):
    monkeypatch.setattr(settings, "chunk_quality_gate_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_model", "qwen3.5:0.8b-stable", raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_timeout_seconds", 20, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_bad_threshold", 0.50, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_suspect_threshold", 0.65, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_neighbor_window", 1, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_max_chunks", 300, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_doc_fail_ratio", 0.55, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_fail_open", True, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_fail_on_unrepaired_bad", False, raising=False)
    monkeypatch.setattr(settings, "chunk_repair_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chunk_repair_max_rounds", 1, raising=False)
    monkeypatch.setattr(settings, "chunk_repair_max_fragments", 120, raising=False)
    monkeypatch.setattr(settings, "chunk_repair_max_chars_per_chunk", 1800, raising=False)


def _chunks():
    return [
        {
            "id": "c0",
            "content": "Background section with coherent scientific context.",
            "start_char": 0,
            "end_char": 56,
            "metadata": {"level": "paragraph"},
        },
        {
            "id": "c1",
            "content": "%%% 12 / 12 garbled page footer header !!!",
            "start_char": 57,
            "end_char": 100,
            "metadata": {"level": "paragraph"},
        },
    ]


@pytest.mark.asyncio
async def test_gate_disabled_returns_original(monkeypatch):
    service = ChunkQualityGateService()
    monkeypatch.setattr(settings, "chunk_quality_gate_enabled", False, raising=False)

    result = await service.gate_chunks(_chunks(), document_name="paper.pdf")

    assert result["should_fail_document"] is False
    assert result["report"]["enabled"] is False
    assert len(result["chunks"]) == 2


def test_build_repaired_text_requires_exact_substring():
    source_map = {
        "self": "alpha beta gamma",
        "prev_1": "previous exact sentence",
    }
    payload = {
        "fragments": [
            {"source": "self", "text": "alpha beta"},
            {"source": "prev_1", "text": "non-existing fragment"},
            {"source": "prev_1", "text": "previous exact sentence"},
        ]
    }

    text, used = ChunkQualityGateService._build_repaired_text(
        payload=payload,
        source_map=source_map,
        max_fragments=10,
        max_chars=200,
    )

    assert text == "alpha beta\nprevious exact sentence"
    assert len(used) == 2


@pytest.mark.asyncio
async def test_bad_chunk_can_be_repaired_and_kept(monkeypatch):
    service = ChunkQualityGateService()
    monkeypatch.setattr(service, "_llm_available", lambda: True)

    async def fake_evaluate(*, chunk_text, chunk_index, document_name, chunk_id):
        if "garbled" in chunk_text:
            return ChunkDecision(
                score=0.21,
                label="bad",
                issues=["garbled"],
                composition=["noise"],
                reason="too noisy",
            )
        return ChunkDecision(
            score=0.86,
            label="good",
            issues=[],
            composition=["main_text"],
            reason="usable",
        )

    async def fake_repair(*, chunks, chunk_index, document_name, chunk_id):
        return ChunkRepair(
            content="Recovered exact sentence from neighbor context.",
            used_fragments=[{"source": "prev_1", "length": 44}],
            rounds_used=1,
            decision=ChunkDecision(
                score=0.82,
                label="good",
                issues=[],
                composition=["main_text"],
                reason="repaired",
            ),
            reason="used neighbor text",
        )

    monkeypatch.setattr(service, "_evaluate_chunk", fake_evaluate)
    monkeypatch.setattr(service, "_repair_bad_chunk", fake_repair)

    result = await service.gate_chunks(_chunks(), document_name="paper.pdf")
    assert result["should_fail_document"] is False
    assert len(result["chunks"]) == 2
    repaired_meta = result["chunks"][1]["metadata"]["quality_gate"]
    assert repaired_meta["status"] == "repaired"
    assert repaired_meta["repaired"] is True
    assert repaired_meta["dropped"] is False
    assert repaired_meta["score"] >= 0.5


@pytest.mark.asyncio
async def test_unrepaired_bad_chunk_can_fail_document(monkeypatch):
    service = ChunkQualityGateService()
    monkeypatch.setattr(service, "_llm_available", lambda: True)
    monkeypatch.setattr(settings, "chunk_quality_gate_fail_on_unrepaired_bad", True, raising=False)
    monkeypatch.setattr(settings, "chunk_quality_gate_doc_fail_ratio", 0.01, raising=False)

    async def fake_evaluate(*, chunk_text, chunk_index, document_name, chunk_id):
        if chunk_id == "c1":
            return ChunkDecision(
                score=0.10,
                label="bad",
                issues=["garbled"],
                composition=["noise"],
                reason="garbled",
            )
        return ChunkDecision(
            score=0.91,
            label="good",
            issues=[],
            composition=["main_text"],
            reason="good",
        )

    async def fake_repair(*, chunks, chunk_index, document_name, chunk_id):
        return None

    monkeypatch.setattr(service, "_evaluate_chunk", fake_evaluate)
    monkeypatch.setattr(service, "_repair_bad_chunk", fake_repair)

    result = await service.gate_chunks(_chunks(), document_name="paper.pdf")
    assert result["should_fail_document"] is True
    assert result["report"]["unrepaired_bad_count"] == 1
    assert result["report"]["total_output"] == 1
    assert result["report"]["dropped_bad_chunk_ids"] == ["c1"]
