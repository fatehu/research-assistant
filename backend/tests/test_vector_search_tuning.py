from unittest.mock import AsyncMock

import pytest

from app.services.vector_search_tuning import apply_hnsw_ef_search


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
