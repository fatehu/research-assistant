import os
import sys

import pytest
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.embedding_service import (
    EmbeddingService,
    clear_embedding_service_cache,
    embedding_service,
    get_embedding_service,
    get_embedding_service_for_model_and_dimension,
)


@pytest.mark.asyncio
async def test_mock_provider_forces_deterministic_embeddings(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "mock")
    monkeypatch.setattr(settings, "mock_embedding_model", "mock/deterministic")
    monkeypatch.setattr(settings, "mock_embedding_dimension", 16)

    service = EmbeddingService(model_name="BAAI/bge-m3")

    assert service.provider == "mock"
    assert service.get_dimension() == 16

    first = await service.embed_text("Graph neural networks for molecules", is_query=True)
    second = await service.embed_text("Graph neural networks for molecules", is_query=True)
    other = await service.embed_text("Reinforcement learning for robotics", is_query=True)

    assert len(first) == 16
    assert first == pytest.approx(second)
    assert first != pytest.approx(other)


@pytest.mark.asyncio
async def test_mock_provider_batch_embeddings_are_stable(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "mock")
    monkeypatch.setattr(settings, "mock_embedding_model", "mock/deterministic")
    monkeypatch.setattr(settings, "mock_embedding_dimension", 8)

    service = EmbeddingService()
    texts = ["alpha beta gamma", "alpha beta gamma", "delta epsilon"]

    embeddings = await service.embed_texts(texts, is_query=False)

    assert len(embeddings) == 3
    assert all(len(vec) == 8 for vec in embeddings)
    assert embeddings[0] == pytest.approx(embeddings[1])
    assert embeddings[0] != pytest.approx(embeddings[2])


def test_default_getter_reinitializes_when_provider_changes(monkeypatch):
    clear_embedding_service_cache()
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "local_embedding_model", "BAAI/bge-m3")

    local_service = get_embedding_service()
    assert local_service.provider == "local"

    monkeypatch.setattr(settings, "embedding_provider", "mock")
    monkeypatch.setattr(settings, "mock_embedding_model", "mock/deterministic")
    monkeypatch.setattr(settings, "mock_embedding_dimension", 12)

    mock_service = get_embedding_service()
    assert mock_service.provider == "mock"
    assert mock_service is not local_service
    assert embedding_service.provider == "mock"


def test_model_specific_getter_cache_key_includes_provider(monkeypatch):
    clear_embedding_service_cache()
    monkeypatch.setattr(settings, "embedding_provider", "local")
    local_service = get_embedding_service_for_model_and_dimension("BAAI/bge-m3", target_dimension=0)
    assert local_service.provider == "local"

    monkeypatch.setattr(settings, "embedding_provider", "mock")
    monkeypatch.setattr(settings, "mock_embedding_model", "mock/deterministic")
    monkeypatch.setattr(settings, "mock_embedding_dimension", 10)
    mock_service = get_embedding_service_for_model_and_dimension("BAAI/bge-m3", target_dimension=0)

    assert mock_service.provider == "mock"
    assert mock_service is not local_service


@pytest.mark.asyncio
async def test_local_embedding_reuses_same_base_model_across_target_dimensions(monkeypatch):
    captured = {"init_calls": 0}

    class _FakeSentenceTransformer:
        def __init__(self, model_name, cache_folder=None, device=None, trust_remote_code=True, **kwargs):
            _ = model_name, cache_folder, device, trust_remote_code, kwargs
            captured["init_calls"] += 1

        def get_sentence_embedding_dimension(self):
            return 1024

        def encode(self, texts, batch_size=None, show_progress_bar=False, normalize_embeddings=True, convert_to_numpy=True):
            _ = batch_size, show_progress_bar, normalize_embeddings, convert_to_numpy
            return np.ones((len(texts), 1024), dtype=np.float32)

    fake_st = type("FakeSTModule", (), {"SentenceTransformer": _FakeSentenceTransformer})
    fake_torch = type(
        "FakeTorchModule",
        (),
        {
            "cuda": type("Cuda", (), {"is_available": staticmethod(lambda: False)}),
            "backends": type(
                "Backends",
                (),
                {"mps": type("Mps", (), {"is_available": staticmethod(lambda: False)})},
            ),
        },
    )

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "local_embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "local_embedding_dimension", 0)
    monkeypatch.setattr(settings, "local_embedding_device", "cpu")
    clear_embedding_service_cache()

    default_service = get_embedding_service_for_model_and_dimension("BAAI/bge-m3", target_dimension=0)
    reduced_service = get_embedding_service_for_model_and_dimension("BAAI/bge-m3", target_dimension=256)

    default_vectors = await default_service.embed_texts(["alpha"], is_query=False)
    reduced_vectors = await reduced_service.embed_texts(["alpha"], is_query=False)

    assert captured["init_calls"] == 1
    assert default_service._local_model is reduced_service._local_model
    assert len(default_vectors[0]) == 1024
    assert len(reduced_vectors[0]) == 256


def test_local_default_dimension_respects_global_override(monkeypatch):
    clear_embedding_service_cache()
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "local_embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "local_embedding_dimension", 256)

    service = get_embedding_service_for_model_and_dimension("BAAI/bge-m3", target_dimension=0)

    assert service.get_dimension() == 256
