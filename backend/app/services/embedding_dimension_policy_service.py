"""
Embedding dimension auto-selection policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.knowledge import DocumentChunk
from app.services.embedding_service import MODEL_DIMENSIONS


@dataclass
class DimensionDecision:
    target_dimension: int
    corpus_chunks: int
    previous_dimension: int
    should_rebuild: bool
    reason: str


class EmbeddingDimensionPolicyService:
    """Resolve retrieval/ingest embedding dimension with adaptive thresholds."""

    @staticmethod
    def _resolve_model_default_dimension(embedding_model: str) -> int:
        dim = MODEL_DIMENSIONS.get((embedding_model or "").strip())
        if dim and dim > 0:
            return int(dim)
        return 1024

    @staticmethod
    def _adaptive_target(corpus_chunks: int, model_default_dimension: int) -> int:
        chunks = max(0, int(corpus_chunks))
        if chunks <= int(settings.embedding_dim_small_max_chunks):
            return int(settings.embedding_dim_small)
        if chunks <= int(settings.embedding_dim_medium_max_chunks):
            return int(settings.embedding_dim_medium)
        return int(model_default_dimension)

    @staticmethod
    def _apply_hysteresis(
        proposed: int,
        previous: int,
        corpus_chunks: int,
        model_default_dimension: int,
    ) -> int:
        if not settings.embedding_dim_hysteresis_enabled:
            return proposed

        chunks = max(0, int(corpus_chunks))
        small = int(settings.embedding_dim_small)
        medium = int(settings.embedding_dim_medium)
        large = int(model_default_dimension)
        prev = int(previous)

        if prev == small and proposed == medium and chunks < int(settings.embedding_dim_hysteresis_small_up):
            return small
        if prev == medium and proposed == small and chunks > int(settings.embedding_dim_hysteresis_small_down):
            return medium
        if prev == medium and proposed == large and chunks < int(settings.embedding_dim_hysteresis_medium_up):
            return medium
        if prev == large and proposed == medium and chunks > int(settings.embedding_dim_hysteresis_medium_down):
            return large
        return proposed

    async def estimate_kb_paragraph_chunks(self, db: AsyncSession, kb_id: int) -> int:
        value = await db.execute(
            select(func.count(DocumentChunk.id)).where(
                and_(
                    DocumentChunk.knowledge_base_id == int(kb_id),
                    DocumentChunk.chunk_level == "paragraph",
                )
            )
        )
        return int(value.scalar() or 0)

    def decide_dimension(
        self,
        *,
        corpus_chunks: int,
        embedding_model: str,
        previous_dimension: Optional[int] = None,
        forced_dimension: int = 0,
    ) -> DimensionDecision:
        model_default = self._resolve_model_default_dimension(embedding_model)
        prev = int(previous_dimension or 0)
        forced = int(forced_dimension or 0)

        if forced > 0:
            target = forced
            reason = "forced_by_parameter"
        elif int(settings.local_embedding_dimension or 0) > 0:
            target = int(settings.local_embedding_dimension)
            reason = "forced_by_local_embedding_dimension"
        elif settings.embedding_dimension_policy == "fixed":
            target = model_default
            reason = "fixed_policy"
        else:
            proposed = self._adaptive_target(corpus_chunks=corpus_chunks, model_default_dimension=model_default)
            target = self._apply_hysteresis(
                proposed=proposed,
                previous=prev if prev > 0 else proposed,
                corpus_chunks=corpus_chunks,
                model_default_dimension=model_default,
            )
            reason = "adaptive_policy"

        if target <= 0:
            target = model_default
            reason = f"{reason}_fallback_default"

        return DimensionDecision(
            target_dimension=int(target),
            corpus_chunks=max(0, int(corpus_chunks)),
            previous_dimension=prev,
            should_rebuild=bool(prev > 0 and prev != int(target)),
            reason=reason,
        )


_embedding_dimension_policy_service = EmbeddingDimensionPolicyService()


def get_embedding_dimension_policy_service() -> EmbeddingDimensionPolicyService:
    return _embedding_dimension_policy_service

