"""
Reranker service based on sentence-transformers CrossEncoder.
"""
import asyncio
import math
import threading
from typing import List, Optional, Tuple

from loguru import logger

from app.config import settings


class RerankerService:
    """Cross-encoder reranker with lazy loading."""

    def __init__(self):
        self._model = None
        self._loaded = False
        self._device: Optional[str] = None
        self._lock = threading.Lock()

    def _resolve_device(self) -> str:
        """Resolve reranker device from settings."""
        device = settings.reranker_device
        if device != "auto":
            return device

        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass

        return "cpu"

    def _load_model(self):
        """Load cross-encoder lazily and only once."""
        if self._loaded:
            return

        with self._lock:
            if self._loaded:
                return

            try:
                from sentence_transformers import CrossEncoder

                model_name = settings.reranker_model
                cache_dir = settings.local_embedding_cache_dir or None
                device = self._resolve_device()

                logger.info(f"Loading reranker model: {model_name}, device={device}")
                init_kwargs = {"device": device}
                if cache_dir:
                    init_kwargs["cache_folder"] = cache_dir

                try:
                    self._model = CrossEncoder(
                        model_name,
                        trust_remote_code=True,
                        **init_kwargs,
                    )
                except TypeError:
                    # Compatibility fallback for older sentence-transformers versions.
                    self._model = CrossEncoder(model_name, **init_kwargs)
                self._device = device
                self._loaded = True
                logger.info(f"Reranker model loaded: {model_name}, device={device}")
            except ImportError as exc:
                raise RuntimeError(
                    "Reranker requires sentence-transformers: pip install sentence-transformers"
                ) from exc
            except Exception as exc:
                logger.error(f"Failed to load reranker model: {exc}")
                raise

    async def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: int = 5,
    ) -> List[Tuple[int, float]]:
        """
        Rerank documents for query.

        Returns:
            List[(document_index, reranker_raw_score)] sorted by score desc.
        """
        if not query.strip() or not documents or top_k <= 0:
            return []

        self._load_model()

        pairs = [[query, doc] for doc in documents]
        scores = await asyncio.to_thread(self._model.predict, pairs)

        if hasattr(scores, "tolist"):
            scores = scores.tolist()
        if isinstance(scores, (float, int)):
            scores = [scores]

        ranked = sorted(
            ((idx, float(score)) for idx, score in enumerate(scores)),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]

    @staticmethod
    def normalize_score(score: float) -> float:
        """Convert raw reranker score to a stable 0-1 range for display."""
        if score >= 0:
            z = math.exp(-score)
            return 1.0 / (1.0 + z)
        z = math.exp(score)
        return z / (1.0 + z)


_reranker_service = RerankerService()


def get_reranker_service() -> RerankerService:
    """Get global reranker service instance."""
    return _reranker_service
