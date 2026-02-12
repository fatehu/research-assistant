import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.hybrid_retrieval_service import fuse_rrf, merge_rows_by_score


def _row(row_id: int, similarity: float | None = None, text_score: float | None = None):
    return SimpleNamespace(
        id=row_id,
        similarity=similarity,
        text_score=text_score,
        content=f"chunk-{row_id}",
    )


def test_fuse_rrf_vector_only_order_kept():
    vector_rows = [
        _row(1, similarity=0.9),
        _row(2, similarity=0.8),
        _row(3, similarity=0.7),
    ]

    fused = fuse_rrf(vector_rows=vector_rows, text_rows=[], rrf_k=60)
    fused_ids = [item.chunk_id for item in fused]

    assert fused_ids == [1, 2, 3]
    assert fused[0].rrf_score > fused[1].rrf_score > fused[2].rrf_score
    assert fused[0].vector_rank == 1
    assert fused[0].text_rank is None


def test_fuse_rrf_overlap_boosts_shared_candidates():
    vector_rows = [
        _row(1, similarity=0.9),
        _row(2, similarity=0.8),
        _row(3, similarity=0.7),
    ]
    text_rows = [
        _row(3, text_score=2.0),
        _row(2, text_score=1.8),
        _row(4, text_score=1.6),
    ]

    fused = fuse_rrf(vector_rows=vector_rows, text_rows=text_rows, rrf_k=1)
    fused_ids = [item.chunk_id for item in fused]

    assert fused_ids == [3, 2, 1, 4]

    top = fused[0]
    assert top.chunk_id == 3
    assert top.vector_rank == 3
    assert top.text_rank == 1


def test_fuse_rrf_limit():
    vector_rows = [_row(1, similarity=0.9), _row(2, similarity=0.8)]
    text_rows = [_row(3, text_score=1.0), _row(4, text_score=0.9)]

    fused = fuse_rrf(vector_rows=vector_rows, text_rows=text_rows, rrf_k=60, limit=2)
    assert len(fused) == 2


def test_merge_rows_by_score_keeps_best_hit_with_query_trace():
    group_1 = [
        _row(1, similarity=0.70),
        _row(2, similarity=0.80),
    ]
    group_2 = [
        _row(1, similarity=0.92),
        _row(3, similarity=0.75),
    ]

    merged = merge_rows_by_score(
        [
            ("original", "attention mechanism", group_1),
            ("synonym", "self-attention mechanism", group_2),
        ],
        score_attr="similarity",
        query_attr="matched_vector_query",
        strategy_attr="matched_vector_strategy",
        limit=3,
    )

    merged_ids = [row.id for row in merged]
    assert merged_ids == [1, 2, 3]
    assert merged[0].matched_vector_query == "self-attention mechanism"
    assert merged[0].matched_vector_strategy == "synonym"
