import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.embedding_service import EmbeddingService


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
