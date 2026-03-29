import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.knowledge import _resolve_pdf_rag_structured_mode


def test_pdf_rag_structured_mode_defaults_to_fast():
    assert _resolve_pdf_rag_structured_mode("local_fast") == "fast"


def test_pdf_rag_structured_mode_maps_local_hybrid():
    assert _resolve_pdf_rag_structured_mode("local_hybrid") == "hybrid"
