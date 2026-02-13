"""Backfill/rebuild contextual retrieval fields and embeddings.

Usage examples:
  python -m app.maintenance.backfill_context_summary_and_embeddings --mode rebuild --truncate-first --dry-run
  python -m app.maintenance.backfill_context_summary_and_embeddings --mode rebuild --truncate-first --batch-size 20
  python -m app.maintenance.backfill_context_summary_and_embeddings --mode backfill --batch-size 200
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from loguru import logger
from sqlalchemy import and_, delete, func, or_, select, text, update

from app.api.knowledge import process_document_task
from app.core.database import AsyncSessionLocal
from app.models.knowledge import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.services.contextual_retrieval_service import build_context_summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill context_summary and rebuild embeddings.")
    parser.add_argument("--mode", choices=["backfill", "rebuild"], default="backfill", help="Execution mode.")
    parser.add_argument("--truncate-first", action="store_true", help="Delete all chunks before rebuild.")
    parser.add_argument("--batch-size", type=int, default=50, help="Rows/docs per batch.")
    parser.add_argument("--limit", type=int, default=0, help="Max rows/docs to process. 0 means no limit.")
    parser.add_argument("--start-doc-id", type=int, default=0, help="Only process documents with id >= start-doc-id.")
    parser.add_argument("--dry-run", action="store_true", help="Only print plan without writing data.")
    return parser


async def _assert_required_columns(required_columns: set[str]) -> None:
    """Fail fast if required columns are missing before destructive operations."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'document_chunks'
                """
            )
        )
        existing = {str(row[0]) for row in rows.fetchall()}

    missing = sorted(col for col in required_columns if col not in existing)
    if missing:
        raise RuntimeError(
            "document_chunks 缺少必要列: "
            + ", ".join(missing)
            + "。请先执行 alembic upgrade head 后再运行本脚本。"
        )


async def _recount_kb_stats(dry_run: bool) -> None:
    async with AsyncSessionLocal() as db:
        kb_ids = [int(row[0]) for row in (await db.execute(select(KnowledgeBase.id))).all()]
        for kb_id in kb_ids:
            chunk_count = int(
                (await db.execute(select(func.count(DocumentChunk.id)).where(DocumentChunk.knowledge_base_id == kb_id))).scalar()
                or 0
            )
            completed_doc_count = int(
                (
                    await db.execute(
                        select(func.count(Document.id)).where(
                            and_(
                                Document.knowledge_base_id == kb_id,
                                Document.status == DocumentStatus.COMPLETED.value,
                            )
                        )
                    )
                ).scalar()
                or 0
            )
            token_sum = int(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(Document.token_count), 0)).where(
                            and_(
                                Document.knowledge_base_id == kb_id,
                                Document.status == DocumentStatus.COMPLETED.value,
                            )
                        )
                    )
                ).scalar()
                or 0
            )

            logger.info(
                f"[context_rebuild] recount kb={kb_id}, docs={completed_doc_count}, chunks={chunk_count}, tokens={token_sum}"
            )
            if dry_run:
                continue

            await db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(
                    document_count=completed_doc_count,
                    total_chunks=chunk_count,
                    total_tokens=token_sum,
                )
            )
        if not dry_run:
            await db.commit()


async def _run_backfill(batch_size: int, limit: int, dry_run: bool) -> None:
    await _assert_required_columns({"context_summary"})

    processed = 0
    updated = 0
    last_id = 0
    max_rows: Optional[int] = limit if limit > 0 else None

    logger.info(
        f"[context_backfill] start batch_size={batch_size}, limit={limit}, dry_run={dry_run}"
    )

    async with AsyncSessionLocal() as db:
        while True:
            stmt = (
                select(
                    DocumentChunk.id,
                    DocumentChunk.content,
                    DocumentChunk.chunk_level,
                    DocumentChunk.section_title,
                    DocumentChunk.section_type,
                    DocumentChunk.metadata_,
                    Document.original_filename,
                )
                .join(Document, Document.id == DocumentChunk.document_id)
                .where(
                    DocumentChunk.id > last_id,
                    DocumentChunk.content.is_not(None),
                    DocumentChunk.content != "",
                    or_(DocumentChunk.context_summary.is_(None), DocumentChunk.context_summary == ""),
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
                chunk_id = int(row.id)
                last_id = max(last_id, chunk_id)
                summary = build_context_summary(
                    document_name=row.original_filename,
                    chunk_level=row.chunk_level,
                    section_title=row.section_title,
                    section_type=row.section_type,
                    metadata=row.metadata_ if isinstance(row.metadata_, dict) else {},
                )
                if not summary:
                    continue

                logger.debug(f"[context_backfill] chunk={chunk_id} summary={summary[:80]}")
                if dry_run:
                    updated += 1
                    continue

                await db.execute(
                    update(DocumentChunk)
                    .where(DocumentChunk.id == chunk_id)
                    .values(context_summary=summary)
                )
                updated += 1

            if not dry_run:
                await db.commit()

            logger.info(
                f"[context_backfill] progress processed={processed}, updated={updated}, last_id={last_id}"
            )

            if max_rows is not None and processed >= max_rows:
                break

    logger.info(
        f"[context_backfill] done processed={processed}, updated={updated}, dry_run={dry_run}"
    )


async def _truncate_chunks_for_rebuild(dry_run: bool) -> None:
    async with AsyncSessionLocal() as db:
        total_chunks = int((await db.execute(select(func.count(DocumentChunk.id)))).scalar() or 0)
        logger.info(f"[context_rebuild] truncate-first enabled, current chunks={total_chunks}")
        if dry_run:
            return

        await db.execute(delete(DocumentChunk))
        await db.execute(
            update(Document).values(
                status=DocumentStatus.PENDING.value,
                chunk_count=0,
                error_message=None,
                processed_at=None,
            )
        )
        await db.execute(update(KnowledgeBase).values(document_count=0, total_chunks=0, total_tokens=0))
        await db.commit()


async def _run_rebuild(
    truncate_first: bool,
    batch_size: int,
    limit: int,
    start_doc_id: int,
    dry_run: bool,
) -> None:
    await _assert_required_columns({"content_segmented", "context_summary"})

    if truncate_first:
        await _truncate_chunks_for_rebuild(dry_run=dry_run)

    async with AsyncSessionLocal() as db:
        docs_stmt = (
            select(Document.id, Document.knowledge_base_id)
            .where(Document.id >= max(0, int(start_doc_id)))
            .order_by(Document.id.asc())
        )
        rows = (await db.execute(docs_stmt)).all()

    doc_pairs = [(int(row.id), int(row.knowledge_base_id)) for row in rows]
    if limit > 0:
        doc_pairs = doc_pairs[:limit]

    logger.info(
        f"[context_rebuild] start docs={len(doc_pairs)}, batch_size={batch_size}, "
        f"truncate_first={truncate_first}, dry_run={dry_run}"
    )

    processed = 0
    failed = 0
    for doc_id, kb_id in doc_pairs:
        processed += 1
        async with AsyncSessionLocal() as db:
            kb = await db.get(KnowledgeBase, kb_id)
            if not kb:
                logger.warning(f"[context_rebuild] skip doc={doc_id}, kb={kb_id} not found")
                failed += 1
                continue
            chunk_size = int(kb.chunk_size)
            chunk_overlap = int(kb.chunk_overlap)

        if dry_run:
            logger.info(
                f"[context_rebuild] dry-run doc={doc_id}, chunk_size={chunk_size}, chunk_overlap={chunk_overlap}"
            )
        else:
            try:
                await process_document_task(doc_id, chunk_size, chunk_overlap)
                async with AsyncSessionLocal() as verify_db:
                    doc_status = (
                        await verify_db.execute(
                            select(Document.status, Document.error_message).where(Document.id == doc_id)
                        )
                    ).first()
                if not doc_status:
                    failed += 1
                    logger.error(f"[context_rebuild] rebuild failed doc={doc_id}, document not found after processing")
                elif doc_status.status != DocumentStatus.COMPLETED.value:
                    failed += 1
                    logger.error(
                        f"[context_rebuild] rebuild failed doc={doc_id}, status={doc_status.status}, "
                        f"error={doc_status.error_message}"
                    )
                else:
                    logger.info(f"[context_rebuild] rebuilt doc={doc_id}")
            except Exception as exc:
                failed += 1
                logger.exception(f"[context_rebuild] rebuild failed doc={doc_id}: {exc}")

        if processed % max(1, batch_size) == 0:
            logger.info(f"[context_rebuild] progress processed={processed}, failed={failed}")

    await _recount_kb_stats(dry_run=dry_run)
    logger.info(f"[context_rebuild] done processed={processed}, failed={failed}, dry_run={dry_run}")


async def _run(
    mode: str,
    truncate_first: bool,
    batch_size: int,
    limit: int,
    start_doc_id: int,
    dry_run: bool,
) -> None:
    if mode == "backfill":
        await _run_backfill(batch_size=batch_size, limit=limit, dry_run=dry_run)
        return

    await _run_rebuild(
        truncate_first=truncate_first,
        batch_size=batch_size,
        limit=limit,
        start_doc_id=start_doc_id,
        dry_run=dry_run,
    )


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(
        _run(
            mode=args.mode,
            truncate_first=bool(args.truncate_first),
            batch_size=max(1, int(args.batch_size)),
            limit=max(0, int(args.limit)),
            start_doc_id=max(0, int(args.start_doc_id)),
            dry_run=bool(args.dry_run),
        )
    )


if __name__ == "__main__":
    main()
