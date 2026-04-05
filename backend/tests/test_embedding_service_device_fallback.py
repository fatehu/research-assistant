import os
from pathlib import Path
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


def test_local_embedding_auto_prefers_cuda_when_available(monkeypatch):
    captured = {"device": None}

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True):
            captured["device"] = device

        def get_sentence_embedding_dimension(self):
            return 1024

    fake_st = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "auto")

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert captured["device"] == "cuda"
    assert model._device == "cuda"


def test_local_embedding_runtime_cuda_error_retries_on_cpu(monkeypatch):
    calls = []

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            calls.append(("init", device))
            self.device = device

        def get_sentence_embedding_dimension(self):
            return 1024

        def encode(self, texts, batch_size=None, show_progress_bar=False, normalize_embeddings=True, convert_to_numpy=True):
            import numpy as np

            calls.append(("encode", self.device))
            if self.device == "cuda":
                raise RuntimeError("CUDA out of memory")
            return np.ones((len(texts), 1024), dtype=np.float32)

    fake_st = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_allow_runtime_cpu_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    embeddings = model.encode_sync(["hello"], is_query=False)

    assert embeddings.shape == (1, 1024)
    assert calls == [
        ("init", "cuda"),
        ("encode", "cuda"),
        ("init", "cpu"),
        ("encode", "cpu"),
    ]
    assert model._device == "cpu"


def test_local_embedding_prefers_safetensors_then_falls_back_to_legacy(monkeypatch):
    calls = []

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            calls.append(
                {
                    "cache_folder": cache_folder,
                    "device": device,
                    "trust_remote_code": trust_remote_code,
                    **kwargs,
                }
            )
            if kwargs.get("model_kwargs") == {"use_safetensors": True}:
                raise RuntimeError("safetensors path failed")

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
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "")
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", False)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert len(calls) == 2
    assert calls[0]["model_kwargs"] == {"use_safetensors": True}
    assert "model_kwargs" not in calls[1]
    assert model._loaded is True


def test_local_embedding_skips_safetensors_when_cached_main_snapshot_is_legacy(monkeypatch, tmp_path):
    calls = []

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            calls.append(
                {
                    "cache_folder": cache_folder,
                    "device": device,
                    "trust_remote_code": trust_remote_code,
                    **kwargs,
                }
            )

        def get_sentence_embedding_dimension(self):
            return 1024

    fake_st = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    snapshot_dir = (
        Path(tmp_path)
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "legacy-snapshot"
    )
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "pytorch_model.bin").write_text("stub", encoding="utf-8")
    refs_dir = snapshot_dir.parent.parent / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("legacy-snapshot", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", False)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert len(calls) == 1
    assert "model_kwargs" not in calls[0]
    assert calls[0]["local_files_only"] is True
    assert model._loaded is True


def test_local_embedding_local_files_only_still_prefers_safetensors_without_cache_dir(monkeypatch):
    calls = []

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            calls.append(
                {
                    "cache_folder": cache_folder,
                    "device": device,
                    "trust_remote_code": trust_remote_code,
                    **kwargs,
                }
            )

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
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "")
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", True)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert calls[0]["model_kwargs"] == {"use_safetensors": True}
    assert calls[0]["local_files_only"] is True
    assert model._loaded is True


def test_local_embedding_strips_unsupported_sentence_transformer_kwargs(monkeypatch):
    captured = {"calls": 0, "device": None}

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True):
            captured["calls"] += 1
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
    monkeypatch.setattr(settings, "local_embedding_device", "cpu")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "/tmp/model-cache")
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", True)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", False)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert captured["calls"] == 1
    assert captured["device"] == "cpu"
    assert model._loaded is True
