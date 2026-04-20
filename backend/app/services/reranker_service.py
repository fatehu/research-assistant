"""
Reranker service based on sentence-transformers CrossEncoder.
"""
import asyncio
import math
from pathlib import Path
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
        requested = str(settings.reranker_device or "auto").strip().lower()
        device = requested

        try:
            import torch

            if device == "auto":
                if torch.cuda.is_available():
                    return "cuda"
                if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    return "mps"
                return "cpu"

            if device == "cuda" and not torch.cuda.is_available():
                logger.warning("RERANKER_DEVICE=cuda 但当前环境无可用 CUDA，自动回退到 CPU")
                return "cpu"
            if device == "mps" and not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
                logger.warning("RERANKER_DEVICE=mps 但当前环境无可用 MPS，自动回退到 CPU")
                return "cpu"
            return device
        except Exception as exc:
            logger.warning(f"检测 reranker 设备可用性失败，将回退 CPU: {exc}")
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
                init_kwargs = {
                    "device": device,
                    "automodel_args": {"torch_dtype": "auto"},
                }
                if cache_dir:
                    init_kwargs["cache_folder"] = cache_dir
                if bool(self._resolve_cached_main_snapshot_dir(cache_dir, model_name)):
                    init_kwargs["local_files_only"] = True
                if int(settings.reranker_max_length or 0) > 0:
                    init_kwargs["max_length"] = int(settings.reranker_max_length)

                try:
                    self._model = CrossEncoder(
                        model_name,
                        trust_remote_code=True,
                        **init_kwargs,
                    )
                except TypeError:
                    # Compatibility fallback for older sentence-transformers versions.
                    init_kwargs.pop("max_length", None)
                    init_kwargs.pop("automodel_args", None)
                    self._model = CrossEncoder(model_name, **init_kwargs)
                    if int(settings.reranker_max_length or 0) > 0 and hasattr(self._model, "max_length"):
                        self._model.max_length = int(settings.reranker_max_length)
                else:
                    if int(settings.reranker_max_length or 0) > 0 and hasattr(self._model, "max_length"):
                        self._model.max_length = int(settings.reranker_max_length)
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

    @staticmethod
    def _resolve_cached_main_snapshot_dir(cache_dir: Optional[str], model_name: str) -> Optional[Path]:
        if not cache_dir:
            return None
        normalized_model_name = str(model_name or "").strip()
        if not normalized_model_name:
            return None

        model_cache_dir = Path(cache_dir) / f"models--{normalized_model_name.replace('/', '--')}"
        ref_file = model_cache_dir / "refs" / "main"
        try:
            snapshot_id = ref_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not snapshot_id:
            return None
        snapshot_dir = model_cache_dir / "snapshots" / snapshot_id
        if snapshot_dir.exists():
            return snapshot_dir
        return None

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

        def _predict_sync() -> List[Tuple[int, float]]:
            self._load_model()
            pairs = [[query, doc] for doc in documents]
            batch_size = max(1, int(settings.reranker_batch_size or 1))
            scores = self._model.predict(
                pairs,
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )

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

        return await asyncio.to_thread(_predict_sync)

    async def warmup(self) -> dict[str, object]:
        """Preload reranker weights and a tiny inference pass."""
        metadata: dict[str, object] = {
            "enabled": bool(settings.enable_reranker),
            "model": str(settings.reranker_model or "").strip(),
            "device": self._resolve_device(),
        }
        if not bool(settings.enable_reranker):
            return {
                "status": "skipped",
                "detail": "reranker disabled",
                "metadata": metadata,
            }

        await self.rerank(
            query="retrieval warmup query",
            documents=["retrieval warmup document"],
            top_k=1,
        )
        return {
            "status": "warmed",
            "detail": "reranker ready",
            "metadata": metadata,
        }

    def get_runtime_status(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": bool(settings.enable_reranker),
            "model": str(settings.reranker_model or "").strip(),
            "device": self._device or self._resolve_device(),
            "ready": bool(self._loaded),
        }
        return payload

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
