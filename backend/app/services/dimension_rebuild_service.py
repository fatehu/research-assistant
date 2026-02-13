"""
Asynchronous knowledge-base embedding dimension rebuild service.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy import and_, select, update

from app.core.database import AsyncSessionLocal
from app.models.knowledge import DocumentChunk, KnowledgeBase
from app.services.contextual_retrieval_service import compose_embedding_input
from app.services.embedding_service import get_embedding_service_for_model_and_dimension


@dataclass
class RebuildStartResult:
    scheduled: bool
    reason: str


class DimensionRebuildService:
    """Schedule and execute KB-level embedding dimension rebuild in background."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def schedule_kb_rebuild(
        self,
        *,
        kb_id: int,
        target_dimension: int,
        trigger_reason: str,
    ) -> RebuildStartResult:
        kb_key = int(kb_id)
        async with self._lock:
            running = self._tasks.get(kb_key)
            if running and not running.done():
                return RebuildStartResult(
                    scheduled=False,
                    reason="already_running",
                )

            task = asyncio.create_task(
                self._run_kb_rebuild(
                    kb_id=kb_key,
                    target_dimension=int(target_dimension),
                    trigger_reason=trigger_reason,
                ),
                name=f"kb-dimension-rebuild-{kb_key}",
            )
            self._tasks[kb_key] = task
            task.add_done_callback(lambda _: self._tasks.pop(kb_key, None))
            return RebuildStartResult(
                scheduled=True,
                reason="scheduled",
            )

    async def _update_kb_rebuild_status(
        self,
        *,
        kb_id: int,
        status: str,
        target_dimension: int,
        trigger_reason: str,
        message: str,
        report_path: Optional[str] = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            kb = await db.get(KnowledgeBase, int(kb_id))
            if kb is None:
                return
            metadata = dict(kb.metadata_ or {})
            now = datetime.utcnow().isoformat()
            rebuild_meta = dict(metadata.get("dimension_rebuild") or {})
            rebuild_meta.update(
                {
                    "status": status,
                    "target_dimension": int(target_dimension),
                    "trigger_reason": trigger_reason,
                    "message": message,
                    "updated_at": now,
                }
            )
            if report_path:
                rebuild_meta["recall_report_path"] = report_path
            metadata["dimension_rebuild"] = rebuild_meta
            kb.metadata_ = metadata
            await db.commit()

    async def _run_recall_gate(self, target_dimension: int) -> tuple[bool, str]:
        repo_root = Path(__file__).resolve().parents[3]
        backend_root = repo_root / "backend"
        cases_file = backend_root / "tests" / "data" / "retrieval_eval_cases.v1.json"
        if not cases_file.exists():
            return False, "cases_file_missing"

        report_name = f"auto_dimension_rebuild_eval_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.md"
        report_path = repo_root / "docs" / "test" / report_name
        report_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "app.maintenance.eval_retrieval_recall",
            "--cases",
            str(cases_file),
            "--top-k",
            "10",
            "--dims",
            f"1024,{int(target_dimension)}",
            "--baseline-dim",
            "1024",
            "--target-dim",
            str(int(target_dimension)),
            "--gate-max-recall-drop-pct",
            "3",
            "--gate-min-latency-improve-pct",
            "15",
            "--output",
            str(report_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(backend_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or stdout or b"").decode("utf-8", errors="ignore")[:800]
            return False, detail or "recall_gate_failed"
        return True, str(report_path)

    async def _run_kb_rebuild(
        self,
        *,
        kb_id: int,
        target_dimension: int,
        trigger_reason: str,
    ) -> None:
        await self._update_kb_rebuild_status(
            kb_id=kb_id,
            status="running",
            target_dimension=target_dimension,
            trigger_reason=trigger_reason,
            message="background rebuild started",
        )

        updated = 0
        batch_size = 64
        try:
            async with AsyncSessionLocal() as db:
                kb = await db.get(KnowledgeBase, int(kb_id))
                if kb is None:
                    raise RuntimeError(f"kb_not_found:{kb_id}")

                embedding_model = (kb.embedding_model or "").strip() or "BAAI/bge-m3"
                emb = get_embedding_service_for_model_and_dimension(
                    embedding_model,
                    int(target_dimension),
                )

                while True:
                    rows = (
                        await db.execute(
                            select(
                                DocumentChunk.id,
                                DocumentChunk.content,
                                DocumentChunk.context_summary,
                                DocumentChunk.chunk_level,
                            )
                            .where(
                                and_(
                                    DocumentChunk.knowledge_base_id == int(kb_id),
                                    DocumentChunk.embedding.is_not(None),
                                    DocumentChunk.embedding_dimension != int(target_dimension),
                                    DocumentChunk.content.is_not(None),
                                    DocumentChunk.content != "",
                                )
                            )
                            .order_by(DocumentChunk.id.asc())
                            .limit(batch_size)
                        )
                    ).all()
                    if not rows:
                        break

                    payload = []
                    row_ids: list[int] = []
                    for row in rows:
                        text = compose_embedding_input(
                            content=row.content or "",
                            context_summary=row.context_summary,
                            chunk_level=row.chunk_level,
                        )
                        if not text.strip():
                            continue
                        payload.append(text)
                        row_ids.append(int(row.id))

                    if not payload:
                        break

                    vectors = await emb.embed_texts(payload, is_query=False)
                    if len(vectors) != len(row_ids):
                        raise RuntimeError(
                            f"embedding_count_mismatch:{len(vectors)}!={len(row_ids)}"
                        )

                    for idx, chunk_id in enumerate(row_ids):
                        await db.execute(
                            update(DocumentChunk)
                            .where(DocumentChunk.id == int(chunk_id))
                            .values(
                                embedding=vectors[idx],
                                embedding_model=embedding_model,
                                embedding_dimension=int(target_dimension),
                            )
                        )
                    await db.commit()
                    updated += len(row_ids)

                kb.embedding_dimension = int(target_dimension)
                await db.commit()

            gate_ok, gate_msg = await self._run_recall_gate(int(target_dimension))
            await self._update_kb_rebuild_status(
                kb_id=kb_id,
                status="completed" if gate_ok else "completed_with_gate_warning",
                target_dimension=target_dimension,
                trigger_reason=trigger_reason,
                message=f"updated_chunks={updated}; gate={gate_msg if gate_ok else 'warning'}",
                report_path=gate_msg if gate_ok and gate_msg.endswith(".md") else None,
            )
            if not gate_ok:
                logger.warning(
                    f"[dimension_rebuild] recall gate warning kb={kb_id}, dim={target_dimension}: {gate_msg}"
                )
            logger.info(
                f"[dimension_rebuild] completed kb={kb_id}, dim={target_dimension}, updated_chunks={updated}"
            )
        except Exception as exc:
            logger.exception(
                f"[dimension_rebuild] failed kb={kb_id}, dim={target_dimension}: {exc}"
            )
            await self._update_kb_rebuild_status(
                kb_id=kb_id,
                status="failed",
                target_dimension=target_dimension,
                trigger_reason=trigger_reason,
                message=str(exc),
            )


_dimension_rebuild_service = DimensionRebuildService()


def get_dimension_rebuild_service() -> DimensionRebuildService:
    return _dimension_rebuild_service
