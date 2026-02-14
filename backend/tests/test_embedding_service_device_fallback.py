import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.embedding_service import LocalEmbeddingModel


def test_local_embedding_forced_cuda_falls_back_to_cpu(monkeypatch):
    captured = {"device": None}

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True):
            captured["device"] = device

        def get_sentence_embedding_dimension(self):
            return 1024

    fake_st = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "cuda")

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert captured["device"] == "cpu"
    assert model._device == "cpu"

