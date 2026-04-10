"""
Embedding 服务 - 支持本地科研嵌入模型和云端 API

支持的 Provider:
  - local:  使用 sentence-transformers 加载本地模型 (推荐科研场景)
  - mock:   使用确定性哈希向量 (CI / smoke / 离线测试)
  - aliyun: 阿里云 text-embedding-v2 API
  - openai: OpenAI text-embedding-3-small API
  - ollama: Ollama 本地 API

推荐科研嵌入模型:
  - BAAI/bge-m3: 多语言SOTA, 1024维, 支持中英文, 科研论文表现优秀 (默认)
  - allenai/specter2: Allen AI 专为科研论文设计, 768维, 仅英文
  - BAAI/bge-large-zh-v1.5: 中文优化, 1024维
"""
import asyncio
import gc
import hashlib
from pathlib import Path
import re
import threading
import numpy as np
from typing import List, Optional, Dict, Tuple
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


# ========== 模型维度注册表 ==========
MODEL_DIMENSIONS: Dict[str, int] = {
    # 科研专用 / 推荐模型
    "BAAI/bge-m3": 1024,
    "allenai/specter2": 768,
    "allenai/specter2_base": 768,
    # BGE 系列
    "BAAI/bge-large-zh-v1.5": 1024,
    "BAAI/bge-large-en-v1.5": 1024,
    "BAAI/bge-base-zh-v1.5": 768,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-small-zh-v1.5": 512,
    "BAAI/bge-small-en-v1.5": 384,
    # E5 系列
    "intfloat/e5-large-v2": 1024,
    "intfloat/e5-base-v2": 768,
    "intfloat/multilingual-e5-large-instruct": 1024,
    # 其他常用
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "moka-ai/m3e-large": 1024,
    "moka-ai/m3e-base": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    # API 模型
    "text-embedding-v2": 1536,
    "text-embedding-3-small": 1536,
    "nomic-embed-text": 768,
    # CI / smoke
    "mock/deterministic": 256,
}

# 查询前缀 (部分模型需要指令前缀以获得最佳检索效果)
MODEL_QUERY_PREFIXES: Dict[str, str] = {
    "BAAI/bge-m3": "",
    "BAAI/bge-large-zh-v1.5": "为这个句子生成表示以用于检索相关文章：",
    "BAAI/bge-large-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "BAAI/bge-base-zh-v1.5": "为这个句子生成表示以用于检索相关文章：",
    "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
    "intfloat/multilingual-e5-large-instruct": "query: ",
    "intfloat/e5-large-v2": "query: ",
    "intfloat/e5-base-v2": "query: ",
}


class _BgeM3FlagEmbeddingAdapter:
    """Thin adapter around the official BGEM3FlagModel dense output path."""

    def __init__(
        self,
        *,
        model_name_or_path: str,
        cache_dir: Optional[str],
        device: str,
        normalize_embeddings: bool,
        use_fp16: bool,
        max_length: int,
    ):
        from FlagEmbedding import BGEM3FlagModel

        self._dimension = MODEL_DIMENSIONS["BAAI/bge-m3"]
        self._normalize_embeddings = bool(normalize_embeddings)
        self._max_length = max(1, int(max_length or 8192))
        self._model = BGEM3FlagModel(
            model_name_or_path,
            normalize_embeddings=bool(normalize_embeddings),
            use_fp16=bool(use_fp16),
            devices=device,
            trust_remote_code=True,
            cache_dir=cache_dir,
            query_max_length=self._max_length,
            passage_max_length=self._max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )

    def get_sentence_embedding_dimension(self) -> int:
        return self._dimension

    def encode(
        self,
        texts: List[str],
        *,
        batch_size: int,
        show_progress_bar: bool = False,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
    ) -> np.ndarray:
        outputs = self._model.encode(
            texts,
            batch_size=max(1, int(batch_size or 1)),
            max_length=self._max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        dense_vecs = outputs.get("dense_vecs") if isinstance(outputs, dict) else outputs
        embeddings = np.asarray(dense_vecs, dtype=np.float32)
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)

        if embeddings.size > 0 and not np.isfinite(embeddings).all():
            logger.warning(
                "官方 BGEM3 输出包含非有限值，已执行数值清洗: "
                f"nan_count={int(np.isnan(embeddings).sum())}, "
                f"posinf_count={int(np.isposinf(embeddings).sum())}, "
                f"neginf_count={int(np.isneginf(embeddings).sum())}"
            )
            embeddings = np.nan_to_num(
                embeddings,
                nan=0.0,
                posinf=0.0,
                neginf=0.0,
            )

        # BGEM3FlagModel can normalize at model init; keep a defensive branch here
        # so behavior stays aligned with the service-level normalize flag.
        if normalize_embeddings and embeddings.size > 0:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            embeddings = embeddings / norms

        if convert_to_numpy:
            return embeddings
        return embeddings


class LocalEmbeddingModel:
    """
    本地嵌入模型封装 - 基于 sentence-transformers

    特点:
    - 懒加载: 首次使用时才加载模型，避免启动延迟
    - GPU 加速: 自动检测并使用 CUDA / MPS
    - 科研优化: 内置对科研模型的查询前缀处理
    - 异步安全: 通过线程池执行推理，不阻塞事件循环
    """

    def __init__(self, model_name: Optional[str] = None, target_dimension: int = 0):
        self._model = None
        self._model_name: str = model_name or settings.local_embedding_model
        self._target_dimension: int = target_dimension  # 0 = use model default
        self._dimension: Optional[int] = None
        self._device: Optional[str] = None
        self._loaded = False
        self._runtime_device_override: Optional[str] = None
        self._load_lock = threading.Lock()

    @staticmethod
    def _instantiate_sentence_transformer(sentence_transformer_cls, model_name: str, init_kwargs: Dict[str, object]):
        """Instantiate SentenceTransformer while tolerating older constructor signatures."""
        attempts = [dict(init_kwargs)]

        while attempts:
            current_kwargs = attempts.pop(0)
            try:
                return sentence_transformer_cls(model_name, **current_kwargs)
            except TypeError:
                compatibility_kwargs = dict(current_kwargs)
                removed = False
                for key in ("model_kwargs", "local_files_only", "backend"):
                    if key in compatibility_kwargs:
                        compatibility_kwargs.pop(key, None)
                        removed = True
                if removed:
                    attempts.insert(0, compatibility_kwargs)
                    continue
                raise

        raise RuntimeError("SentenceTransformer 初始化失败：没有可用的兼容构造参数")

    def _resolve_sentence_transformer_model_kwargs(
        self,
        *,
        device: str,
        torch_module,
        use_safetensors: bool = False,
    ) -> Dict[str, object]:
        model_kwargs: Dict[str, object] = {}
        if use_safetensors:
            model_kwargs["use_safetensors"] = True

        use_fp16_on_cuda = bool(getattr(settings, "local_embedding_use_fp16_on_cuda", True))
        if (
            use_fp16_on_cuda
            and str(device or "").lower() == "cuda"
            and hasattr(torch_module, "float16")
        ):
            model_kwargs["torch_dtype"] = torch_module.float16

        return model_kwargs

    def _should_use_official_bge_m3_backend(self, device: str) -> bool:
        return (
            str(self._model_name or "").strip() == "BAAI/bge-m3"
            and bool(getattr(settings, "local_embedding_use_official_bge_m3_backend", True))
            and str(device or "").lower() in {"cpu", "cuda"}
        )

    def _resolve_bge_m3_model_source(self, cache_dir: Optional[str]) -> str:
        snapshot_dir = self._resolve_cached_main_snapshot_dir(cache_dir)
        if snapshot_dir is not None:
            return str(snapshot_dir)
        return self._model_name

    def _instantiate_official_bge_m3_backend(
        self,
        *,
        cache_dir: Optional[str],
        device: str,
        torch_module,
    ) -> _BgeM3FlagEmbeddingAdapter:
        if not self._should_use_official_bge_m3_backend(device):
            raise RuntimeError("official BGEM3 backend requested for unsupported model/device")

        model_source = self._resolve_bge_m3_model_source(cache_dir)
        use_fp16 = bool(
            getattr(settings, "local_embedding_use_fp16_on_cuda", True)
            and str(device or "").lower() == "cuda"
            and hasattr(torch_module, "float16")
        )
        try:
            model = _BgeM3FlagEmbeddingAdapter(
                model_name_or_path=model_source,
                cache_dir=cache_dir,
                device=device,
                normalize_embeddings=bool(settings.local_embedding_normalize),
                use_fp16=use_fp16,
                max_length=int(settings.local_embedding_max_length or 8192),
            )
        except Exception as exc:
            raise RuntimeError(
                "官方 BGEM3FlagModel 加载失败，请确认 FlagEmbedding 及其依赖已完整安装 "
                f"(model={self._model_name}, device={device}, source={model_source}): {exc}"
            ) from exc

        logger.info(
            f"本地嵌入模型加载成功: profile=flagembedding_official, "
            f"model={self._model_name}, source={model_source}, device={device}, fp16={use_fp16}"
        )
        return model

    def _build_sentence_transformer_load_profiles(
        self,
        *,
        cache_dir: Optional[str],
        device: str,
        torch_module,
    ) -> List[Tuple[str, Dict[str, object]]]:
        base_kwargs: Dict[str, object] = {
            "device": device,
            "trust_remote_code": True,
        }
        if cache_dir:
            base_kwargs["cache_folder"] = cache_dir

        profiles: List[Tuple[str, Dict[str, object]]] = []
        prefer_safetensors = bool(getattr(settings, "local_embedding_prefer_safetensors", True))
        local_files_only = bool(getattr(settings, "local_embedding_local_files_only", False))
        allow_legacy_fallback = bool(getattr(settings, "local_embedding_allow_legacy_pickle_fallback", True))
        safetensors_viable = self._cached_main_snapshot_supports_safetensors(cache_dir)
        legacy_snapshot_viable = self._cached_main_snapshot_supports_legacy_weights(cache_dir)
        cached_snapshot_available = self._resolve_cached_main_snapshot_dir(cache_dir) is not None

        if prefer_safetensors and safetensors_viable is False:
            logger.info(
                f"检测到本地模型主快照不支持 safetensors，跳过优先 safetensors 加载: model={self._model_name}"
            )
            prefer_safetensors = False
        if cached_snapshot_available and legacy_snapshot_viable is False:
            logger.warning(
                f"检测到本地模型主快照不完整，将允许远端补齐后再加载: model={self._model_name}"
            )

        if prefer_safetensors:
            if local_files_only or cache_dir:
                profiles.append(
                    (
                        "safetensors_local_only",
                        {
                            **base_kwargs,
                            "model_kwargs": self._resolve_sentence_transformer_model_kwargs(
                                device=device,
                                torch_module=torch_module,
                                use_safetensors=True,
                            ),
                            "local_files_only": True,
                        },
                    )
                )
            if not local_files_only:
                profiles.append(
                    (
                        "safetensors_remote_allowed",
                        {
                            **base_kwargs,
                            "model_kwargs": self._resolve_sentence_transformer_model_kwargs(
                                device=device,
                                torch_module=torch_module,
                                use_safetensors=True,
                            ),
                        },
                    )
                )

        if allow_legacy_fallback:
            legacy_kwargs = dict(base_kwargs)
            legacy_model_kwargs = self._resolve_sentence_transformer_model_kwargs(
                device=device,
                torch_module=torch_module,
                use_safetensors=False,
            )
            if legacy_model_kwargs:
                legacy_kwargs["model_kwargs"] = legacy_model_kwargs
            if local_files_only or legacy_snapshot_viable is True:
                legacy_kwargs["local_files_only"] = True
            profiles.append(("legacy_default", legacy_kwargs))

        if not profiles:
            profiles.append(("default", dict(base_kwargs)))
        return profiles

    @staticmethod
    def _required_sentence_transformer_allow_patterns() -> List[str]:
        return [
            "*.json",
            "*.txt",
            "*.model",
            "*.bin",
            "*.safetensors",
            "*.index.json",
            "modules.json",
            "1_Pooling/*",
            "2_Normalize/*",
            "sentencepiece*",
            "spiece.model",
            "tokenizer.*",
            "vocab.*",
            "special_tokens_map.json",
        ]

    def _ensure_cached_sentence_transformer_snapshot(self, cache_dir: Optional[str]) -> None:
        if not cache_dir:
            return
        if bool(getattr(settings, "local_embedding_local_files_only", False)):
            return
        if self._cached_main_snapshot_supports_legacy_weights(cache_dir) is True:
            return

        try:
            from huggingface_hub import snapshot_download
        except Exception as exc:
            logger.warning(f"无法导入 huggingface_hub，跳过嵌入模型缓存补齐: {exc}")
            return

        try:
            logger.info(
                f"检测到嵌入模型缓存不完整，开始补齐必要文件: model={self._model_name}, cache_dir={cache_dir}"
            )
            snapshot_download(
                self._model_name,
                cache_dir=cache_dir,
                local_files_only=False,
                allow_patterns=self._required_sentence_transformer_allow_patterns(),
            )
        except Exception as exc:
            logger.warning(f"嵌入模型缓存补齐失败，将继续尝试常规加载: model={self._model_name}, error={exc}")

    def _cached_main_snapshot_supports_safetensors(self, cache_dir: Optional[str]) -> Optional[bool]:
        """检查当前 refs/main 指向的本地快照是否具备 safetensors 权重文件。

        返回:
        - True: 明确支持 safetensors
        - False: 明确只看到 legacy bin 布局，不应优先走 safetensors
        - None: 无法判断，保留现有加载顺序
        """
        snapshot_dir = self._resolve_cached_main_snapshot_dir(cache_dir)
        if snapshot_dir is None:
            return None

        has_safetensors = any(
            (snapshot_dir / name).exists()
            for name in ("model.safetensors", "model.safetensors.index.json")
        )
        has_legacy_bin = any(
            (snapshot_dir / name).exists()
            for name in ("pytorch_model.bin", "pytorch_model.bin.index.json")
        )

        if has_safetensors:
            return True
        if has_legacy_bin:
            return False
        return None

    def _cached_main_snapshot_supports_legacy_weights(self, cache_dir: Optional[str]) -> Optional[bool]:
        """检查当前 refs/main 主快照是否足以完成 legacy Transformers 加载。"""
        snapshot_dir = self._resolve_cached_main_snapshot_dir(cache_dir)
        if snapshot_dir is None:
            return None

        has_weight = any(
            (snapshot_dir / name).exists()
            for name in (
                "pytorch_model.bin",
                "pytorch_model.bin.index.json",
                "model.safetensors",
                "model.safetensors.index.json",
            )
        )
        has_config = (snapshot_dir / "config.json").exists()
        has_tokenizer = any(
            (snapshot_dir / name).exists()
            for name in (
                "tokenizer.json",
                "tokenizer_config.json",
                "sentencepiece.bpe.model",
                "spiece.model",
                "vocab.txt",
            )
        )
        if has_weight and has_config and has_tokenizer:
            return True
        return False

    def _resolve_cached_main_snapshot_dir(self, cache_dir: Optional[str]) -> Optional[Path]:
        if not cache_dir:
            return None

        normalized_model_name = str(self._model_name or "").strip()
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

    @staticmethod
    def _is_runtime_device_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return any(
            needle in message
            for needle in (
                "cuda",
                "cudnn",
                "cublas",
                "out of memory",
                "mps",
                "hip",
                "rocm",
                "nvidia",
                "device-side",
                "driver",
            )
        )

    def _reset_loaded_model(self) -> None:
        self._model = None
        self._device = None
        self._loaded = False

    def _release_runtime_device_resources(self) -> None:
        """在运行时设备回退前尽量释放显存，避免 GPU/CPU 双占用。"""
        try:
            gc.collect()
        except Exception:
            pass

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass

    def _encode_with_runtime_fallback(
        self,
        texts: List[str],
        *,
        is_query: bool,
        show_progress: bool,
    ) -> np.ndarray:
        try:
            return self._model.encode(
                texts,
                batch_size=settings.local_embedding_batch_size,
                show_progress_bar=show_progress,
                normalize_embeddings=settings.local_embedding_normalize,
                convert_to_numpy=True,
            )
        except Exception as exc:
            allow_runtime_cpu_fallback = bool(
                getattr(settings, "local_embedding_allow_runtime_cpu_fallback", True)
            )
            current_device = str(self._device or "").lower()
            if (
                allow_runtime_cpu_fallback
                and current_device not in {"", "cpu"}
                and self._is_runtime_device_error(exc)
            ):
                logger.warning(
                    f"本地嵌入推理失败，尝试降级到 CPU 重试: model={self._model_name}, "
                    f"device={current_device}, error={exc}"
                )
                self._runtime_device_override = "cpu"
                self._reset_loaded_model()
                self._release_runtime_device_resources()
                self._load_model()
                return self._model.encode(
                    texts,
                    batch_size=settings.local_embedding_batch_size,
                    show_progress_bar=show_progress,
                    normalize_embeddings=settings.local_embedding_normalize,
                    convert_to_numpy=True,
                )
            raise

    def _load_model(self):
        """懒加载模型"""
        if self._loaded:
            return

        with self._load_lock:
            if self._loaded:
                return

            try:
                import torch

                model_name = self._model_name
                cache_dir = settings.local_embedding_cache_dir or None

                # 设备选择（显式 cuda/mps 也要做可用性校验，避免容器内无驱动直接失败）
                requested_device = str(
                    self._runtime_device_override or settings.local_embedding_device or "auto"
                ).strip().lower()
                device = requested_device
                if device == "auto":
                    if torch.cuda.is_available():
                        device = "cuda"
                    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                        device = "mps"
                    else:
                        device = "cpu"
                elif device == "cuda":
                    cuda_ok = False
                    try:
                        cuda_ok = bool(torch.cuda.is_available())
                    except Exception as exc:
                        logger.warning(f"检测 CUDA 可用性失败，将回退 CPU: {exc}")
                    if not cuda_ok:
                        logger.warning("LOCAL_EMBEDDING_DEVICE=cuda 但当前环境无可用 CUDA，自动回退到 CPU")
                        device = "cpu"
                elif device == "mps":
                    mps_ok = False
                    try:
                        mps_ok = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
                    except Exception as exc:
                        logger.warning(f"检测 MPS 可用性失败，将回退 CPU: {exc}")
                    if not mps_ok:
                        logger.warning("LOCAL_EMBEDDING_DEVICE=mps 但当前环境无可用 MPS，自动回退到 CPU")
                        device = "cpu"

                if device != requested_device and requested_device != "auto":
                    logger.info(f"Embedding 设备已从 {requested_device} 回退到 {device}")

                logger.info(f"加载本地嵌入模型: {model_name}, device={device}")
                if self._should_use_official_bge_m3_backend(device):
                    self._model = self._instantiate_official_bge_m3_backend(
                        cache_dir=cache_dir,
                        device=device,
                        torch_module=torch,
                    )
                else:
                    from sentence_transformers import SentenceTransformer

                    self._ensure_cached_sentence_transformer_snapshot(cache_dir)

                    load_profiles = self._build_sentence_transformer_load_profiles(
                        cache_dir=cache_dir,
                        device=device,
                        torch_module=torch,
                    )
                    last_error: Optional[Exception] = None
                    for profile_name, init_kwargs in load_profiles:
                        try:
                            self._model = self._instantiate_sentence_transformer(
                                SentenceTransformer,
                                model_name,
                                init_kwargs,
                            )
                            logger.info(
                                f"本地嵌入模型加载成功: profile={profile_name}, model={model_name}, device={device}"
                            )
                            break
                        except Exception as exc:
                            last_error = exc
                            logger.warning(
                                f"本地嵌入模型加载失败，尝试下一配置: profile={profile_name}, model={model_name}, error={exc}"
                            )

                    if self._model is None:
                        raise last_error or RuntimeError(f"加载本地嵌入模型失败: {model_name}")
                self._device = device

                # LocalEmbeddingModel 只表示底座模型本身的原始维度。
                # Matryoshka 截断在 encode 阶段按调用方目标维度处理，
                # 避免同一模型因为 1024/default/256 等输出维度重复加载。
                self._dimension = self._model.get_sentence_embedding_dimension()

                logger.info(
                    f"本地嵌入模型加载完成: {model_name}, "
                    f"dimension={self._dimension}, device={device}"
                )
                self._loaded = True
                if device == "cpu":
                    self._runtime_device_override = "cpu"
                else:
                    self._runtime_device_override = None

            except ImportError:
                raise RuntimeError(
                    "使用本地嵌入模型需要安装依赖:\n"
                    "  pip install sentence-transformers torch\n"
                    "如不需要 GPU 可安装 CPU 版本的 torch。"
                )
            except Exception as e:
                logger.error(f"加载本地嵌入模型失败: {e}")
                raise

    @property
    def is_loaded(self) -> bool:
        return bool(self._loaded)

    @property
    def device(self) -> Optional[str]:
        return self._device

    @property
    def dimension(self) -> int:
        """获取底座模型原始维度 (尽量不加载模型即可获得)"""
        if self._dimension is not None:
            return self._dimension
        # 尝试从注册表获取
        dim = MODEL_DIMENSIONS.get(self._model_name)
        if dim:
            return dim
        # 必须加载模型才能确定
        self._load_model()
        return self._dimension

    def _resolve_output_dimension(self, target_dimension: int = 0) -> int:
        explicit_target = max(0, int(target_dimension or 0))
        if explicit_target > 0:
            return explicit_target
        if self._target_dimension > 0:
            return int(self._target_dimension)
        configured_target = max(0, int(settings.local_embedding_dimension or 0))
        return configured_target

    def _add_query_prefix(self, text: str) -> str:
        """为查询添加模型特定的指令前缀"""
        prefix = MODEL_QUERY_PREFIXES.get(self._model_name, "")
        if prefix and not text.startswith(prefix):
            return prefix + text
        return text

    def encode_sync(
        self,
        texts: List[str],
        is_query: bool = False,
        show_progress: bool = False,
        target_dimension: int = 0,
    ) -> np.ndarray:
        """
        同步编码文本为向量

        Args:
            texts: 待编码的文本列表
            is_query: 是否为查询文本
            show_progress: 是否显示进度条

        Returns:
            np.ndarray, shape=(len(texts), dimension)
        """
        self._load_model()

        if is_query:
            texts = [self._add_query_prefix(t) for t in texts]

        embeddings = self._encode_with_runtime_fallback(
            texts,
            is_query=is_query,
            show_progress=show_progress,
        )

        # Matryoshka 维度截断
        target_dim = self._resolve_output_dimension(target_dimension)
        if target_dim > 0 and embeddings.shape[1] > target_dim:
            embeddings = embeddings[:, :target_dim]
            if settings.local_embedding_normalize:
                norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
                norms = np.where(norms == 0, 1, norms)
                embeddings = embeddings / norms

        return embeddings


class EmbeddingModelPool:
    """
    本地嵌入模型实例池 - 按 model_name 缓存 LocalEmbeddingModel 实例
    
    线程安全，懒加载。避免为同一模型重复创建实例。
    """
    
    def __init__(self):
        self._models: Dict[Tuple[str, Tuple[str, ...]], LocalEmbeddingModel] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _runtime_signature() -> Tuple[str, ...]:
        return (
            str(settings.local_embedding_device or "auto"),
            str(settings.local_embedding_cache_dir or ""),
            str(bool(settings.local_embedding_prefer_safetensors)),
            str(bool(settings.local_embedding_local_files_only)),
            str(bool(settings.local_embedding_allow_legacy_pickle_fallback)),
            str(bool(settings.local_embedding_allow_runtime_cpu_fallback)),
            str(bool(settings.local_embedding_use_official_bge_m3_backend)),
            str(bool(settings.local_embedding_use_fp16_on_cuda)),
        )

    def get(self, model_name: str) -> LocalEmbeddingModel:
        """获取或创建指定模型的底座 LocalEmbeddingModel 实例。"""
        key = (model_name, self._runtime_signature())
        if key not in self._models:
            with self._lock:
                if key not in self._models:  # double-check
                    logger.info(f"模型池: 创建新实例 {model_name} (base)")
                    self._models[key] = LocalEmbeddingModel(
                        model_name=model_name,
                        target_dimension=0,
                    )
        return self._models[key]

    def list_loaded(self) -> List[str]:
        """列出已加载的模型"""
        return [model_name for model_name, _runtime_signature in self._models.keys()]

    def clear(self) -> None:
        with self._lock:
            self._models.clear()


# 全局模型池
_model_pool = EmbeddingModelPool()


class EmbeddingService:
    """
    统一嵌入服务 - 支持本地模型和云端 API

    通过 EMBEDDING_PROVIDER 环境变量切换:
      local  → sentence-transformers 本地推理 (默认, 推荐科研)
      mock   → 确定性哈希向量 (CI / smoke / 离线验证)
      aliyun → 阿里云 DashScope API
      openai → OpenAI API
      ollama → Ollama 本地 API
    """

    def __init__(self, model_name: Optional[str] = None, target_dimension: int = 0):
        """
        初始化嵌入服务
        
        Args:
            model_name: 指定模型名称。为 None 时使用全局配置。
                        本地模型格式: "BAAI/bge-m3" 等
                        API 模型格式: "text-embedding-v2", "text-embedding-3-small", "nomic-embed-text"
        """
        self._model_name_override = model_name
        self._target_dimension_override = max(0, int(target_dimension or 0))
        self.provider = self._resolve_provider(model_name)
        self._client = None
        self._local_model: Optional[LocalEmbeddingModel] = None

        if self.provider == "local":
            actual_model = model_name or settings.local_embedding_model
            self._local_model = _model_pool.get(actual_model)

        logger.info(
            f"Embedding 服务初始化: provider={self.provider}, "
            f"model={self._get_model()}, target_dim={self._target_dimension_override or 'default'}"
        )
    
    @staticmethod
    def _resolve_provider(model_name: Optional[str]) -> str:
        """根据模型名称推断 provider"""
        forced_provider = str(settings.embedding_provider or "local").strip().lower()
        if forced_provider == "mock":
            return "mock"

        if model_name is None:
            return forced_provider
        
        # API 模型 -> 对应 provider
        API_MODEL_PROVIDERS = {
            "text-embedding-v2": "aliyun",
            "text-embedding-3-small": "openai",
            "text-embedding-3-large": "openai",
            "nomic-embed-text": "ollama",
        }
        if model_name in API_MODEL_PROVIDERS:
            return API_MODEL_PROVIDERS[model_name]
        
        # 其他 (含 BAAI/*, allenai/*, intfloat/* 等) -> local
        return "local"

    def _get_api_client(self):
        """获取 API 客户端 (aliyun/openai/ollama)"""
        if self._client is None:
            from openai import AsyncOpenAI

            if self.provider == "aliyun":
                api_key = settings.aliyun_embedding_api_key or settings.aliyun_api_key
                if not api_key:
                    logger.warning("阿里云 Embedding API Key 未配置")
                self._client = AsyncOpenAI(
                    api_key=api_key,
                    base_url=settings.aliyun_base_url,
                )
            elif self.provider == "openai":
                self._client = AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )
            elif self.provider == "ollama":
                self._client = AsyncOpenAI(
                    api_key="ollama",
                    base_url=f"{settings.ollama_base_url}/v1",
                )
            else:
                logger.info(f"未知 provider: {self.provider}，回退到阿里云")
                self._client = AsyncOpenAI(
                    api_key=settings.aliyun_embedding_api_key or settings.aliyun_api_key,
                    base_url=settings.aliyun_base_url,
                )
        return self._client

    def _get_model(self) -> str:
        """获取模型名称"""
        if self._model_name_override:
            return self._model_name_override
        if self.provider == "local":
            return settings.local_embedding_model
        elif self.provider == "mock":
            return settings.mock_embedding_model
        elif self.provider == "aliyun":
            return settings.aliyun_embedding_model
        elif self.provider == "openai":
            return "text-embedding-3-small"
        elif self.provider == "ollama":
            return "nomic-embed-text"
        return settings.aliyun_embedding_model

    def get_dimension(self) -> int:
        """获取当前 provider 的向量维度"""
        if self._target_dimension_override > 0:
            return self._target_dimension_override
        if self.provider == "local":
            configured_target = max(0, int(settings.local_embedding_dimension or 0))
            if configured_target > 0:
                return configured_target
            return self._local_model.dimension
        elif self.provider == "mock":
            return max(1, int(settings.mock_embedding_dimension or MODEL_DIMENSIONS["mock/deterministic"]))
        elif self.provider == "aliyun":
            return 1536
        elif self.provider == "openai":
            return 1536
        elif self.provider == "ollama":
            return 768
        return 1536

    def get_target_dimension(self) -> int:
        return self.get_dimension()

    def get_runtime_status(self) -> Dict[str, object]:
        metadata: Dict[str, object] = {
            "provider": str(self.provider or "").strip().lower(),
            "model": self._get_model(),
        }
        if self.provider == "local":
            metadata["ready"] = bool(self._local_model and self._local_model.is_loaded)
            metadata["dimension"] = int(self.get_dimension() or 0)
            if self._local_model and self._local_model.device:
                metadata["device"] = self._local_model.device
            return metadata
        if self.provider == "mock":
            metadata["ready"] = True
            metadata["dimension"] = int(self.get_dimension() or 0)
            metadata["device"] = "mock"
            return metadata
        metadata["ready"] = False
        return metadata

    def _get_dimension_hint(self) -> int:
        if self._target_dimension_override > 0:
            return self._target_dimension_override
        if self.provider == "local":
            configured_target = max(0, int(settings.local_embedding_dimension or 0))
            if configured_target > 0:
                return configured_target
            return int(MODEL_DIMENSIONS.get(self._get_model(), 0) or 0)
        if self.provider == "mock":
            return max(1, int(settings.mock_embedding_dimension or MODEL_DIMENSIONS["mock/deterministic"]))
        return int(MODEL_DIMENSIONS.get(self._get_model(), 0) or 0)

    async def warmup(self) -> Dict[str, object]:
        """Preload local/mock embedding runtime for the first retrieval request."""
        provider = str(self.provider or "").strip().lower()
        model_name = self._get_model()
        metadata: Dict[str, object] = {
            "provider": provider,
            "model": model_name,
            "dimension": int(self._get_dimension_hint() or 0),
        }

        if provider not in {"local", "mock"}:
            return {
                "status": "skipped",
                "detail": f"provider={provider} startup warmup not required",
                "metadata": metadata,
            }

        await self.embed_text("retrieval warmup query", is_query=True)
        return {
            "status": "warmed",
            "detail": f"provider={provider} model ready",
            "metadata": metadata,
        }

    # ===================================================================
    #  核心嵌入方法
    # ===================================================================

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def embed_text(self, text: str, is_query: bool = True) -> List[float]:
        """
        获取单个文本的嵌入向量

        Args:
            text: 输入文本
            is_query: 是否为查询 (本地模型会添加检索前缀)
        """
        if not text.strip():
            return []

        if self.provider == "local":
            return await self._local_embed_single(text, is_query=is_query)
        elif self.provider == "mock":
            return await self._mock_embed_single(text, is_query=is_query)
        else:
            return await self._api_embed_text(text)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def embed_texts(self, texts: List[str], is_query: bool = False) -> List[List[float]]:
        """
        批量获取文本的嵌入向量

        Args:
            texts: 输入文本列表
            is_query: 是否为查询
        """
        if not texts:
            return []

        valid_texts = [t for t in texts if t.strip()]
        if not valid_texts:
            return []

        if self.provider == "local":
            return await self._local_embed_batch(valid_texts, is_query=is_query)
        elif self.provider == "mock":
            return await self._mock_embed_batch(valid_texts, is_query=is_query)
        else:
            return await self._api_embed_texts(valid_texts)

    # ===================================================================
    #  本地模型推理
    # ===================================================================

    async def _local_embed_single(self, text: str, is_query: bool = False) -> List[float]:
        """本地模型单文本嵌入"""
        loop = asyncio.get_event_loop()

        def _encode():
            return self._local_model.encode_sync(
                [text],
                is_query=is_query,
                target_dimension=self._target_dimension_override,
            )

        embeddings = await loop.run_in_executor(None, _encode)
        return embeddings[0].tolist()

    async def _local_embed_batch(
        self, texts: List[str], is_query: bool = False
    ) -> List[List[float]]:
        """本地模型批量嵌入"""
        loop = asyncio.get_event_loop()

        logger.info(f"本地模型批量 Embedding: {len(texts)} 条文本")

        def _encode():
            return self._local_model.encode_sync(
                texts,
                is_query=is_query,
                show_progress=len(texts) > 10,
                target_dimension=self._target_dimension_override,
            )

        embeddings = await loop.run_in_executor(None, _encode)
        result = [emb.tolist() for emb in embeddings]
        logger.info(f"本地模型批量 Embedding 完成: {len(result)} 个向量")
        return result

    # ===================================================================
    #  Mock / CI 确定性向量
    # ===================================================================

    @staticmethod
    def _tokenize_for_mock(text: str) -> List[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text.lower())
        if tokens:
            return tokens
        chars = [ch for ch in text.strip() if not ch.isspace()]
        return chars[:256]

    def _mock_embed_sync(self, texts: List[str], is_query: bool = False) -> np.ndarray:
        dim = int(self.get_dimension())
        if dim <= 0:
            dim = MODEL_DIMENSIONS["mock/deterministic"]

        salt = self._get_model()
        vectors: list[np.ndarray] = []
        for raw_text in texts:
            text = str(raw_text or "")
            payload = text if not is_query else f"query::{text}"
            tokens = self._tokenize_for_mock(payload)
            if not tokens:
                tokens = [payload]

            vec = np.zeros(dim, dtype=np.float32)
            for token in tokens:
                digest = hashlib.sha256(f"{salt}::{token}".encode("utf-8")).digest()
                for offset in range(0, 16, 4):
                    idx = int.from_bytes(digest[offset:offset + 4], "big", signed=False) % dim
                    sign = 1.0 if digest[(offset + 16) % len(digest)] % 2 == 0 else -1.0
                    weight = 0.5 + (digest[(offset + 20) % len(digest)] / 255.0)
                    vec[idx] += sign * weight

            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            vectors.append(vec)

        return np.vstack(vectors) if vectors else np.zeros((0, dim), dtype=np.float32)

    async def _mock_embed_single(self, text: str, is_query: bool = False) -> List[float]:
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: self._mock_embed_sync([text], is_query=is_query),
        )
        return embeddings[0].tolist()

    async def _mock_embed_batch(
        self, texts: List[str], is_query: bool = False
    ) -> List[List[float]]:
        loop = asyncio.get_event_loop()
        logger.info(f"Mock Embedding: total={len(texts)}, dim={self.get_dimension()}")
        embeddings = await loop.run_in_executor(
            None,
            lambda: self._mock_embed_sync(texts, is_query=is_query),
        )
        return [emb.tolist() for emb in embeddings]

    # ===================================================================
    #  API 模型调用
    # ===================================================================

    def _apply_target_dimension(self, embedding: List[float]) -> List[float]:
        target_dim = self._target_dimension_override
        if target_dim <= 0:
            return embedding
        if len(embedding) <= target_dim:
            return embedding

        clipped = np.array(embedding[:target_dim], dtype=np.float32)
        if settings.local_embedding_normalize:
            norm = np.linalg.norm(clipped)
            if norm > 0:
                clipped = clipped / norm
        return clipped.tolist()

    async def _api_embed_text(self, text: str) -> List[float]:
        """API 单文本嵌入"""
        client = self._get_api_client()
        model = self._get_model()

        try:
            logger.debug(f"API Embedding: model={model}, len={len(text)}")
            response = await client.embeddings.create(input=text, model=model)
            embedding = self._apply_target_dimension(response.data[0].embedding)
            logger.debug(f"API Embedding OK: dim={len(embedding)}")
            return embedding
        except Exception as e:
            logger.error(f"API Embedding 失败: {e}")
            raise

    async def _api_embed_texts(self, texts: List[str]) -> List[List[float]]:
        """API 批量嵌入"""
        client = self._get_api_client()
        model = self._get_model()

        batch_size = 5 if self.provider == "aliyun" else 20
        all_embeddings = []

        logger.info(f"API 批量 Embedding: total={len(texts)}, batch={batch_size}")

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            logger.debug(f"批次 {i // batch_size + 1}: {len(batch)} 条")

            try:
                response = await client.embeddings.create(input=batch, model=model)
                batch_embeddings = [
                    self._apply_target_dimension(d.embedding)
                    for d in response.data
                ]
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                logger.error(f"批次 {i // batch_size + 1} 失败: {e}")
                raise

            await asyncio.sleep(0.1)

        logger.info(f"API 批量 Embedding 完成: {len(all_embeddings)} 个向量")
        return all_embeddings

    # ===================================================================
    #  相似度计算
    # ===================================================================

    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if not vec1 or not vec2:
            return 0.0

        a = np.array(vec1)
        b = np.array(vec2)

        dot_product = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return float(dot_product / (norm_a * norm_b))

    def compute_similarity_batch(
        self,
        query_embedding: List[float],
        embeddings: List[List[float]],
    ) -> List[float]:
        """批量计算余弦相似度"""
        if not query_embedding or not embeddings:
            return []

        query_vec = np.array(query_embedding)
        query_norm = np.linalg.norm(query_vec)

        if query_norm == 0:
            return [0.0] * len(embeddings)

        similarities = []
        for emb in embeddings:
            if not emb:
                similarities.append(0.0)
                continue

            emb_vec = np.array(emb)
            emb_norm = np.linalg.norm(emb_vec)

            if emb_norm == 0:
                similarities.append(0.0)
            else:
                sim = float(np.dot(query_vec, emb_vec) / (query_norm * emb_norm))
                similarities.append(sim)

        return similarities


# 按初始化口径缓存的 EmbeddingService 实例
_service_cache: Dict[Tuple[str, str, int, Tuple[str, ...]], EmbeddingService] = {}
_service_cache_lock = threading.Lock()


def _default_model_name_for_provider(provider: str) -> str:
    normalized_provider = str(provider or "local").strip().lower()
    if normalized_provider == "local":
        return str(settings.local_embedding_model or "BAAI/bge-m3")
    if normalized_provider == "mock":
        return str(settings.mock_embedding_model or "mock/deterministic")
    if normalized_provider == "aliyun":
        return str(settings.aliyun_embedding_model or "text-embedding-v2")
    if normalized_provider == "openai":
        return "text-embedding-3-small"
    if normalized_provider == "ollama":
        return "nomic-embed-text"
    return str(settings.aliyun_embedding_model or "text-embedding-v2")


def _provider_runtime_signature(provider: str) -> Tuple[str, ...]:
    normalized_provider = str(provider or "local").strip().lower()
    if normalized_provider == "local":
        return (
            normalized_provider,
            str(settings.local_embedding_device or "auto"),
            str(settings.local_embedding_cache_dir or ""),
            str(int(settings.local_embedding_dimension or 0)),
            str(bool(settings.local_embedding_prefer_safetensors)),
            str(bool(settings.local_embedding_local_files_only)),
            str(bool(settings.local_embedding_allow_legacy_pickle_fallback)),
            str(bool(settings.local_embedding_allow_runtime_cpu_fallback)),
            str(bool(settings.local_embedding_use_official_bge_m3_backend)),
            str(bool(settings.local_embedding_use_fp16_on_cuda)),
            str(int(settings.local_embedding_batch_size or 0)),
            str(bool(settings.local_embedding_normalize)),
        )
    if normalized_provider == "mock":
        return (
            normalized_provider,
            str(settings.mock_embedding_model or "mock/deterministic"),
            str(int(settings.mock_embedding_dimension or MODEL_DIMENSIONS["mock/deterministic"])),
        )
    if normalized_provider == "aliyun":
        return (
            normalized_provider,
            str(settings.aliyun_base_url or ""),
            str(settings.aliyun_embedding_api_key or settings.aliyun_api_key or ""),
            str(settings.aliyun_embedding_model or "text-embedding-v2"),
        )
    if normalized_provider == "openai":
        return (
            normalized_provider,
            str(settings.openai_base_url or ""),
            str(settings.openai_api_key or ""),
        )
    if normalized_provider == "ollama":
        return (
            normalized_provider,
            str(settings.ollama_base_url or ""),
        )
    return (normalized_provider,)


def _resolve_service_cache_key(
    model_name: Optional[str] = None,
    target_dimension: int = 0,
) -> Tuple[str, str, int, Tuple[str, ...]]:
    requested_model = str(model_name or "").strip() or None
    provider = EmbeddingService._resolve_provider(requested_model)
    effective_model = requested_model or _default_model_name_for_provider(provider)
    dim_key = max(0, int(target_dimension or 0))
    runtime_signature = _provider_runtime_signature(provider)
    return provider, effective_model, dim_key, runtime_signature


def clear_embedding_service_cache() -> None:
    """清空 EmbeddingService 实例缓存。"""
    with _service_cache_lock:
        _service_cache.clear()
    _model_pool.clear()


def get_embedding_service() -> EmbeddingService:
    """获取默认嵌入服务实例（使用当前配置，懒加载）。"""
    return get_embedding_service_for_model_and_dimension(model_name=None, target_dimension=0)


def get_embedding_service_for_model(model_name: Optional[str] = None) -> EmbeddingService:
    return get_embedding_service_for_model_and_dimension(model_name, target_dimension=0)


def get_embedding_service_for_model_and_dimension(
    model_name: Optional[str] = None,
    target_dimension: int = 0,
) -> EmbeddingService:
    provider_key, model_key, dim_key, runtime_signature = _resolve_service_cache_key(
        model_name=model_name,
        target_dimension=target_dimension,
    )
    cache_key = (provider_key, model_key, dim_key, runtime_signature)
    if cache_key not in _service_cache:
        with _service_cache_lock:
            if cache_key not in _service_cache:
                logger.info(
                    f"创建 EmbeddingService: provider={provider_key}, "
                    f"model={model_key}, dim={dim_key or 'default'}"
                )
                _service_cache[cache_key] = EmbeddingService(
                    model_name=model_key,
                    target_dimension=dim_key,
                )

    return _service_cache[cache_key]


class _EmbeddingServiceProxy:
    """默认 embedding service 的惰性代理，兼容历史 import 口径。"""

    def __getattr__(self, name):
        return getattr(get_embedding_service(), name)


# 向后兼容：保留同名导出，但不再在导入时初始化真实模型服务
embedding_service = _EmbeddingServiceProxy()
