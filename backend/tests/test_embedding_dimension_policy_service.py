import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.embedding_dimension_policy_service import EmbeddingDimensionPolicyService


@pytest.fixture
def policy_service() -> EmbeddingDimensionPolicyService:
    return EmbeddingDimensionPolicyService()


def test_adaptive_dimension_band_selection(monkeypatch, policy_service: EmbeddingDimensionPolicyService):
    monkeypatch.setattr(settings, "embedding_dimension_policy", "adaptive")
    monkeypatch.setattr(settings, "local_embedding_dimension", 0)
    monkeypatch.setattr(settings, "embedding_dim_small", 256)
    monkeypatch.setattr(settings, "embedding_dim_medium", 512)
    monkeypatch.setattr(settings, "embedding_dim_small_max_chunks", 2000)
    monkeypatch.setattr(settings, "embedding_dim_medium_max_chunks", 10000)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_enabled", False)

    small = policy_service.decide_dimension(
        corpus_chunks=1800,
        embedding_model="BAAI/bge-m3",
        previous_dimension=1024,
    )
    medium = policy_service.decide_dimension(
        corpus_chunks=6000,
        embedding_model="BAAI/bge-m3",
        previous_dimension=1024,
    )
    large = policy_service.decide_dimension(
        corpus_chunks=20000,
        embedding_model="BAAI/bge-m3",
        previous_dimension=1024,
    )

    assert small.target_dimension == 256
    assert medium.target_dimension == 512
    assert large.target_dimension == 1024


def test_hysteresis_prevents_small_medium_jitter(monkeypatch, policy_service: EmbeddingDimensionPolicyService):
    monkeypatch.setattr(settings, "embedding_dimension_policy", "adaptive")
    monkeypatch.setattr(settings, "local_embedding_dimension", 0)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_enabled", True)
    monkeypatch.setattr(settings, "embedding_dim_small", 256)
    monkeypatch.setattr(settings, "embedding_dim_medium", 512)
    monkeypatch.setattr(settings, "embedding_dim_small_max_chunks", 2000)
    monkeypatch.setattr(settings, "embedding_dim_medium_max_chunks", 10000)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_small_up", 2500)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_small_down", 1500)

    keep_small = policy_service.decide_dimension(
        corpus_chunks=2200,
        embedding_model="BAAI/bge-m3",
        previous_dimension=256,
    )
    keep_medium = policy_service.decide_dimension(
        corpus_chunks=1800,
        embedding_model="BAAI/bge-m3",
        previous_dimension=512,
    )

    assert keep_small.target_dimension == 256
    assert keep_medium.target_dimension == 512


def test_hysteresis_prevents_medium_large_jitter(monkeypatch, policy_service: EmbeddingDimensionPolicyService):
    monkeypatch.setattr(settings, "embedding_dimension_policy", "adaptive")
    monkeypatch.setattr(settings, "local_embedding_dimension", 0)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_enabled", True)
    monkeypatch.setattr(settings, "embedding_dim_medium", 512)
    monkeypatch.setattr(settings, "embedding_dim_medium_max_chunks", 10000)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_medium_up", 12000)
    monkeypatch.setattr(settings, "embedding_dim_hysteresis_medium_down", 8000)

    keep_medium = policy_service.decide_dimension(
        corpus_chunks=11000,
        embedding_model="BAAI/bge-m3",
        previous_dimension=512,
    )
    keep_large = policy_service.decide_dimension(
        corpus_chunks=9000,
        embedding_model="BAAI/bge-m3",
        previous_dimension=1024,
    )

    assert keep_medium.target_dimension == 512
    assert keep_large.target_dimension == 1024


def test_fixed_policy_uses_model_default(monkeypatch, policy_service: EmbeddingDimensionPolicyService):
    monkeypatch.setattr(settings, "embedding_dimension_policy", "fixed")
    monkeypatch.setattr(settings, "local_embedding_dimension", 0)

    decision = policy_service.decide_dimension(
        corpus_chunks=100,
        embedding_model="BAAI/bge-m3",
        previous_dimension=256,
    )

    assert decision.target_dimension == 1024
    assert decision.should_rebuild is True
