import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.knowledge import (
    _resolve_pdf_rag_structured_mode,
    _should_run_chunk_quality_gate,
)


def test_chunk_quality_gate_runs_for_non_pdf_rag_chunks():
    assert _should_run_chunk_quality_gate(
        gate_enabled=True,
        used_pdf_rag_ingest=False,
    ) is True


def test_chunk_quality_gate_is_forced_off_for_pdf_rag_chunks():
    assert _should_run_chunk_quality_gate(
        gate_enabled=True,
        used_pdf_rag_ingest=True,
    ) is False


def test_chunk_quality_gate_stays_off_when_disabled():
    assert _should_run_chunk_quality_gate(
        gate_enabled=False,
        used_pdf_rag_ingest=False,
    ) is False


def test_pdf_rag_structured_mode_defaults_to_fast():
    assert _resolve_pdf_rag_structured_mode("local_fast") == "fast"


def test_pdf_rag_structured_mode_maps_local_hybrid():
    assert _resolve_pdf_rag_structured_mode("local_hybrid") == "hybrid"
