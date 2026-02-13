"""
Utilities for pgvector / HNSW search tuning.
"""
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def resolve_ef_search(total_chunks: int, dimension: int) -> int:
    """
    Resolve ef_search by corpus scale and embedding dimension.
    """
    fixed_value = max(1, min(int(settings.pgvector_hnsw_ef_search), 1000))
    if settings.pgvector_hnsw_ef_search_mode == "fixed":
        return fixed_value

    ef_min = max(1, min(int(settings.pgvector_hnsw_ef_search_min), 1000))
    ef_max = max(ef_min, min(int(settings.pgvector_hnsw_ef_search_max), 1000))

    chunks = max(0, int(total_chunks or 0))
    dim = max(0, int(dimension or 0))

    if chunks <= 5_000:
        size_factor = 0.0
    elif chunks <= 20_000:
        size_factor = 0.25
    elif chunks <= 100_000:
        size_factor = 0.5
    elif chunks <= 500_000:
        size_factor = 0.75
    else:
        size_factor = 1.0

    if dim <= 384:
        dim_factor = -0.10
    elif dim <= 512:
        dim_factor = -0.05
    elif dim >= 1536:
        dim_factor = 0.10
    elif dim >= 1024:
        dim_factor = 0.05
    else:
        dim_factor = 0.0

    ratio = max(0.0, min(1.0, size_factor + dim_factor))
    resolved = int(round(ef_min + (ef_max - ef_min) * ratio))
    return max(ef_min, min(resolved, ef_max))


async def apply_hnsw_ef_search(
    db: AsyncSession,
    ef_search: int,
    source: str = "vector_search",
) -> None:
    """Set hnsw.ef_search in current transaction without breaking main flow."""
    bounded_value = max(1, min(int(ef_search), 1000))
    try:
        await db.execute(
            text("SELECT set_config('hnsw.ef_search', :ef_search, true)"),
            {"ef_search": str(bounded_value)},
        )
    except Exception as exc:
        logger.warning(
            f"[{source}] set hnsw.ef_search failed, fallback to default planner setting: {exc}"
        )

