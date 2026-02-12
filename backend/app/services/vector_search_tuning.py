"""
pgvector / HNSW 检索调优辅助函数。
"""
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def apply_hnsw_ef_search(
    db: AsyncSession,
    ef_search: int,
    source: str = "vector_search",
) -> None:
    """在当前事务内设置 hnsw.ef_search，不阻断主流程。"""
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
