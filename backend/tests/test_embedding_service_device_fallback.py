import os
from pathlib import Path
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.embedding_service import LocalEmbeddingModel


@pytest.fixture(autouse=True)
def _disable_official_bge_m3_backend(monkeypatch):
    monkeypatch.setattr(settings, "local_embedding_use_official_bge_m3_backend", False)


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


def test_local_embedding_cuda_uses_fp16_model_kwargs(monkeypatch):
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
        float16="fp16",
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "cuda")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "")
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", False)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)
    monkeypatch.setattr(settings, "local_embedding_use_fp16_on_cuda", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert calls[0]["device"] == "cuda"
    assert calls[0]["model_kwargs"] == {"use_safetensors": True, "torch_dtype": "fp16"}
    assert model._loaded is True


def test_local_embedding_bge_m3_uses_official_flagembedding_backend(monkeypatch, tmp_path):
    calls = []

    class _FakeBGEM3FlagModel:
        def __init__(self, model_name_or_path, **kwargs):
            calls.append({"model_name_or_path": model_name_or_path, **kwargs})

        def encode(self, texts, **kwargs):
            import numpy as np

            return {"dense_vecs": np.ones((len(texts), 1024), dtype=np.float32)}

    class _UnexpectedSentenceTransformer:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SentenceTransformer should not be used for official bge-m3 backend")

    fake_flag = types.SimpleNamespace(BGEM3FlagModel=_FakeBGEM3FlagModel)
    fake_st = types.SimpleNamespace(SentenceTransformer=_UnexpectedSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        float16="fp16",
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    snapshot_dir = (
        Path(tmp_path)
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "official-snapshot"
    )
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "pytorch_model.bin").write_text("stub", encoding="utf-8")
    (snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
    (snapshot_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    refs_dir = snapshot_dir.parent.parent / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("official-snapshot", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_use_official_bge_m3_backend", True)
    monkeypatch.setattr(settings, "local_embedding_device", "cuda")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_embedding_use_fp16_on_cuda", True)
    monkeypatch.setattr(settings, "local_embedding_max_length", 4096)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    embeddings = model.encode_sync(["hello world"], is_query=False)

    assert embeddings.shape == (1, 1024)
    assert calls
    assert calls[0]["model_name_or_path"] == str(snapshot_dir)
    assert calls[0]["use_fp16"] is True
    assert calls[0]["devices"] == "cuda"
    assert calls[0]["query_max_length"] == 4096
    assert calls[0]["passage_max_length"] == 4096


def test_local_embedding_bge_m3_official_backend_failure_does_not_fallback(monkeypatch):
    class _BrokenBGEM3FlagModel:
        def __init__(self, *args, **kwargs):
            raise ModuleNotFoundError("No module named 'datasets'")

    class _UnexpectedSentenceTransformer:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SentenceTransformer should not be used when official bge-m3 backend fails")

    fake_flag = types.SimpleNamespace(BGEM3FlagModel=_BrokenBGEM3FlagModel)
    fake_st = types.SimpleNamespace(SentenceTransformer=_UnexpectedSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        float16="fp16",
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_use_official_bge_m3_backend", True)
    monkeypatch.setattr(settings, "local_embedding_device", "cuda")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "")
    monkeypatch.setattr(settings, "local_embedding_use_fp16_on_cuda", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")

    with pytest.raises(RuntimeError, match="官方 BGEM3FlagModel 加载失败"):
        model._load_model()


def test_local_embedding_bge_m3_official_backend_sanitizes_non_finite_values(monkeypatch):
    class _FakeBGEM3FlagModel:
        def __init__(self, *args, **kwargs):
            pass

        def encode(self, texts, **kwargs):
            import numpy as np

            return {
                "dense_vecs": np.array(
                    [
                        [float("nan"), float("inf"), float("-inf"), 3.0],
                    ] * len(texts),
                    dtype=np.float32,
                )
            }

    class _UnexpectedSentenceTransformer:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SentenceTransformer should not be used for official bge-m3 backend")

    fake_flag = types.SimpleNamespace(BGEM3FlagModel=_FakeBGEM3FlagModel)
    fake_st = types.SimpleNamespace(SentenceTransformer=_UnexpectedSentenceTransformer)
    fake_torch = types.SimpleNamespace(
        float16="fp16",
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False),
        ),
    )

    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake_flag)
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_use_official_bge_m3_backend", True)
    monkeypatch.setattr(settings, "local_embedding_device", "cuda")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", "")
    monkeypatch.setattr(settings, "local_embedding_use_fp16_on_cuda", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    embeddings = model.encode_sync(["hello world"], is_query=False, target_dimension=4)

    assert embeddings.shape == (1, 4)
    assert np.isfinite(embeddings).all()
    assert pytest.approx(float(np.linalg.norm(embeddings[0])), rel=1e-6) == 1.0


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
    (snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
    (snapshot_dir / "tokenizer_config.json").write_text("{}", encoding="utf-8")
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


def test_local_embedding_incomplete_cached_snapshot_allows_remote_legacy_fallback(monkeypatch, tmp_path):
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

    snapshot_dir = (
        Path(tmp_path)
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "incomplete-snapshot"
    )
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
    refs_dir = snapshot_dir.parent.parent / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("incomplete-snapshot", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", False)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert len(calls) == 3
    assert calls[0]["local_files_only"] is True
    assert calls[1].get("local_files_only") is None
    assert "local_files_only" not in calls[2]
    assert model._loaded is True


def test_local_embedding_incomplete_cached_snapshot_prefetches_required_files(monkeypatch, tmp_path):
    download_calls = []
    load_calls = []

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            load_calls.append(
                {
                    "model_name": model_name,
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

    class _FakeHub:
        @staticmethod
        def snapshot_download(repo_id, **kwargs):
            download_calls.append({"repo_id": repo_id, **kwargs})
            snapshot_dir = (
                Path(kwargs["cache_dir"])
                / "models--BAAI--bge-m3"
                / "snapshots"
                / "main-snapshot"
            )
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            (snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
            (snapshot_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
            (snapshot_dir / "pytorch_model.bin").write_text("stub", encoding="utf-8")
            refs_dir = snapshot_dir.parent.parent / "refs"
            refs_dir.mkdir(parents=True, exist_ok=True)
            (refs_dir / "main").write_text("main-snapshot", encoding="utf-8")
            return str(snapshot_dir)

    incomplete_snapshot_dir = (
        Path(tmp_path)
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "broken-snapshot"
    )
    incomplete_snapshot_dir.mkdir(parents=True)
    (incomplete_snapshot_dir / "config.json").write_text("{}", encoding="utf-8")
    refs_dir = incomplete_snapshot_dir.parent.parent / "refs"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("broken-snapshot", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "huggingface_hub", _FakeHub)
    monkeypatch.setattr(settings, "local_embedding_device", "auto")
    monkeypatch.setattr(settings, "local_embedding_cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "local_embedding_prefer_safetensors", True)
    monkeypatch.setattr(settings, "local_embedding_local_files_only", False)
    monkeypatch.setattr(settings, "local_embedding_allow_legacy_pickle_fallback", True)

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")
    model._load_model()

    assert len(download_calls) == 1
    assert download_calls[0]["repo_id"] == "BAAI/bge-m3"
    assert download_calls[0]["local_files_only"] is False
    assert any(pattern == "tokenizer.*" for pattern in download_calls[0]["allow_patterns"])
    assert len(load_calls) >= 1
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


def test_local_embedding_load_is_serialized(monkeypatch):
    calls = {"count": 0}
    start_barrier = threading.Barrier(2)

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            calls["count"] += 1
            time.sleep(0.05)

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

    model = LocalEmbeddingModel(model_name="BAAI/bge-m3")

    def _load() -> None:
        start_barrier.wait(timeout=1.0)
        model._load_model()

    threads = [threading.Thread(target=_load) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1.0)

    assert calls["count"] == 1
    assert model._loaded is True
