"""
Hybrid retrieval helpers: vector + full-text fusion with RRF.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence


@dataclass
class RetrievalCandidate:
    chunk_id: int
    row: Any
    vector_score: Optional[float] = None
    text_score: Optional[float] = None
    vector_rank: Optional[int] = None
    text_rank: Optional[int] = None
    rrf_score: float = 0.0


def _get_row_id(row: Any) -> int:
    """Read row id from SQLAlchemy row-like object."""
    return int(getattr(row, "id"))


def fuse_rrf(
    vector_rows: Sequence[Any],
    text_rows: Sequence[Any],
    *,
    rrf_k: int = 60,
    limit: Optional[int] = None,
) -> list[RetrievalCandidate]:
    """
    Fuse vector ranking and text ranking with Reciprocal Rank Fusion.
    """
    if rrf_k <= 0:
        rrf_k = 60

    candidates: Dict[int, RetrievalCandidate] = {}

    for rank, row in enumerate(vector_rows, start=1):
        chunk_id = _get_row_id(row)
        candidate = candidates.get(chunk_id)
        if candidate is None:
            candidate = RetrievalCandidate(chunk_id=chunk_id, row=row)
            candidates[chunk_id] = candidate

        candidate.row = row
        candidate.vector_rank = rank
        candidate.vector_score = float(getattr(row, "similarity", 0.0))
        candidate.rrf_score += 1.0 / (rrf_k + rank)

    for rank, row in enumerate(text_rows, start=1):
        chunk_id = _get_row_id(row)
        candidate = candidates.get(chunk_id)
        if candidate is None:
            candidate = RetrievalCandidate(chunk_id=chunk_id, row=row)
            candidates[chunk_id] = candidate
        elif candidate.row is None:
            candidate.row = row

        candidate.text_rank = rank
        candidate.text_score = float(getattr(row, "text_score", 0.0))
        candidate.rrf_score += 1.0 / (rrf_k + rank)

    fused = sorted(
        candidates.values(),
        key=lambda item: (
            item.rrf_score,
            item.vector_score if item.vector_score is not None else -1.0,
            item.text_score if item.text_score is not None else -1.0,
        ),
        reverse=True,
    )

    if limit is not None and limit > 0:
        return fused[:limit]
    return fused
