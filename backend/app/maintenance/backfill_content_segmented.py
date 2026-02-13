"""
Backfill `document_chunks.content_segmented` in batches.

Usage:
  python -m app.maintenance.backfill_content_segmented --batch-size 200 --limit 2000 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy import or_, select, update

from app.core.database import AsyncSessionLocal
from app.models.knowledge import DocumentChunk
from app.services.chinese_segmentation_service import segment_text_for_fts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill content_segmented for Chinese FTS.")
    parser.add_argument("--batch-size", type=int, default=200, help="Rows per batch.")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process. 0 means no limit.")
    parser.add_argument("--dry-run", action="store_true", help="Only count updates without writing.")
    return parser


async def _run(batch_size: int, limit: int, dry_run: bool) -> None:
    processed = 0
    updated = 0
    last_id = 0
    max_rows: Optional[int] = limit if limit > 0 else None

    logger.info(
        f"[backfill_content_segmented] start batch_size={batch_size}, "
        f"limit={limit}, dry_run={dry_run}"
    )

    async with AsyncSessionLocal() as db:
        while True:
            stmt = (
                select(DocumentChunk.id, DocumentChunk.content)
                .where(
                    DocumentChunk.id > last_id,
                    DocumentChunk.content.is_not(None),
                    DocumentChunk.content != "",
                    or_(
                        DocumentChunk.content_segmented.is_(None),
                        DocumentChunk.content_segmented == "",
                    ),
                )
                .order_by(DocumentChunk.id.asc())
                .limit(batch_size)
            )

            rows = (await db.execute(stmt)).all()
            if not rows:
                break

            for row in rows:
                if max_rows is not None and processed >= max_rows:
                    break

                processed += 1
                last_id = max(last_id, int(row.id))
                segmented = segment_text_for_fts(row.content or "")
                if not segmented:
                    continue

                if dry_run:
                    updated += 1
                    continue

                await db.execute(
                    update(DocumentChunk)
                    .where(DocumentChunk.id == int(row.id))
                    .values(content_segmented=segmented)
                )
                updated += 1

            if not dry_run:
                await db.commit()

            logger.info(
                f"[backfill_content_segmented] progress processed={processed}, "
                f"updated={updated}, last_id={last_id}"
            )

            if max_rows is not None and processed >= max_rows:
                break

    logger.info(
        f"[backfill_content_segmented] done processed={processed}, updated={updated}, dry_run={dry_run}"
    )


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(
        _run(
            batch_size=max(1, args.batch_size),
            limit=max(0, args.limit),
            dry_run=bool(args.dry_run),
        )
    )


if __name__ == "__main__":
    main()

