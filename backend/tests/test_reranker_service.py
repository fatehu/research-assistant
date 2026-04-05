import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.reranker_service import RerankerService


def test_reranker_prefers_local_files_only_when_cached_snapshot_exists(monkeypatch, tmp_path):
    calls = []

    class _FakeCrossEncoder:
        def __init__(self, model_name, trust_remote_code=True, **kwargs):
            calls.append(
                {
                    "model_name": model_name,
                    "trust_remote_code": trust_remote_code,
                    **kwargs,
                }
            )

        def predict(self, *args, **kwargs):
            return [0.5]

    fake_st = types.SimpleNamespace(CrossEncoder=_FakeCrossEncoder)
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    snapshot_dir = (
        Path(tmp_path)
        / "models--Alibaba-NLP--gte-reranker-modernbert-base"
        / "snapshots"
        / "cached-snapshot"
    )
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
    refs_dir = snapshot_dir.parent.parent / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("cached-snapshot", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "reranker_model", "Alibaba-NLP/gte-reranker-modernbert-base")
    monkeypatch.setattr(settings, "reranker_device", "auto")
    monkeypatch.setattr(settings, "reranker_max_length", 384)
    monkeypatch.setattr(settings, "local_embedding_cache_dir", str(tmp_path))

    service = RerankerService()
    service._load_model()

    assert calls
    assert calls[0]["local_files_only"] is True
    assert service.get_runtime_status()["ready"] is True
