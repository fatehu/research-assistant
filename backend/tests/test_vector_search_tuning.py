import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.services.vector_search_tuning import apply_hnsw_ef_search, resolve_ef_search


@pytest.mark.asyncio
async def test_apply_hnsw_ef_search_sets_local_guc():
    db = AsyncMock()

    await apply_hnsw_ef_search(db, 40, source="test")

    db.execute.assert_awaited_once()
    stmt, params = db.execute.await_args.args
    assert "set_config('hnsw.ef_search'" in str(stmt)
    assert params["ef_search"] == "40"


@pytest.mark.asyncio
async def test_apply_hnsw_ef_search_bounds_value():
    db = AsyncMock()

    await apply_hnsw_ef_search(db, -3, source="test")
    _, params = db.execute.await_args.args
    assert params["ef_search"] == "1"

    db.reset_mock()
    await apply_hnsw_ef_search(db, 5000, source="test")
    _, params = db.execute.await_args.args
    assert params["ef_search"] == "1000"


@pytest.mark.asyncio
async def test_apply_hnsw_ef_search_swallows_db_error():
    db = AsyncMock()
    db.execute.side_effect = RuntimeError("unknown setting")

    await apply_hnsw_ef_search(db, 40, source="test")

    db.execute.assert_awaited_once()


def test_resolve_ef_search_fixed_mode(monkeypatch):
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_mode", "fixed")
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search", 55)

    assert resolve_ef_search(total_chunks=100_000, dimension=1024) == 55


def test_resolve_ef_search_adaptive_respects_min_max(monkeypatch):
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_mode", "adaptive")
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_min", 32)
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_max", 96)

    small = resolve_ef_search(total_chunks=2_000, dimension=512)
    large = resolve_ef_search(total_chunks=1_000_000, dimension=1536)

    assert 32 <= small <= 96
    assert 32 <= large <= 96
    assert large >= small


def test_resolve_ef_search_dimension_bias(monkeypatch):
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_mode", "adaptive")
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_min", 32)
    monkeypatch.setattr(settings, "pgvector_hnsw_ef_search_max", 96)

    same_scale_low_dim = resolve_ef_search(total_chunks=200_000, dimension=512)
    same_scale_high_dim = resolve_ef_search(total_chunks=200_000, dimension=1536)

    assert same_scale_high_dim >= same_scale_low_dim
