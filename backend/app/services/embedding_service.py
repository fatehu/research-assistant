"""
Embedding 服务 - 支持本地科研嵌入模型和云端 API

支持的 Provider:
  - local:  使用 sentence-transformers 加载本地模型 (推荐科研场景)
  - aliyun: 阿里云 text-embedding-v2 API
  - openai: OpenAI text-embedding-3-small API
  - ollama: Ollama 本地 API

推荐科研嵌入模型:
  - BAAI/bge-m3: 多语言SOTA, 1024维, 支持中英文, 科研论文表现优秀 (默认)
  - allenai/specter2: Allen AI 专为科研论文设计, 768维, 仅英文
  - BAAI/bge-large-zh-v1.5: 中文优化, 1024维
"""
import asyncio
import threading
import numpy as np
from typing import List, Optional, Dict
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

    def _load_model(self):
        """懒加载模型"""
        if self._loaded:
            return

        try:
            from sentence_transformers import SentenceTransformer
            import torch

            model_name = self._model_name
            cache_dir = settings.local_embedding_cache_dir or None

            # 设备选择
            device = settings.local_embedding_device
            if device == "auto":
                if torch.cuda.is_available():
                    device = "cuda"
                elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                    device = "mps"
                else:
                    device = "cpu"

            logger.info(f"加载本地嵌入模型: {model_name}, device={device}")

            self._model = SentenceTransformer(
                model_name,
                cache_folder=cache_dir,
                device=device,
                trust_remote_code=True,
            )
            self._device = device

            # 确定输出维度 (优先使用实例级 target_dimension，其次全局 settings)
            target_dim = self._target_dimension or settings.local_embedding_dimension
            if target_dim > 0:
                self._dimension = target_dim
            else:
                self._dimension = self._model.get_sentence_embedding_dimension()

            logger.info(
                f"本地嵌入模型加载完成: {model_name}, "
                f"dimension={self._dimension}, device={device}"
            )
            self._loaded = True

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
    def dimension(self) -> int:
        """获取向量维度 (尽量不加载模型即可获得)"""
        if self._dimension is not None:
            return self._dimension
        # 尝试从注册表获取
        dim = MODEL_DIMENSIONS.get(self._model_name)
        if dim:
            target = self._target_dimension or settings.local_embedding_dimension
            return target if target > 0 else dim
        # 必须加载模型才能确定
        self._load_model()
        return self._dimension

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

        embeddings = self._model.encode(
            texts,
            batch_size=settings.local_embedding_batch_size,
            show_progress_bar=show_progress,
            normalize_embeddings=settings.local_embedding_normalize,
            convert_to_numpy=True,
        )

        # Matryoshka 维度截断
        target_dim = self._target_dimension or settings.local_embedding_dimension
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
        self._models: Dict[str, LocalEmbeddingModel] = {}
        self._lock = threading.Lock()
    
    def get(self, model_name: str, target_dimension: int = 0) -> LocalEmbeddingModel:
        """获取或创建指定模型的 LocalEmbeddingModel 实例"""
        key = f"{model_name}:{target_dimension}"
        if key not in self._models:
            with self._lock:
                if key not in self._models:  # double-check
                    logger.info(f"模型池: 创建新实例 {model_name} (dim={target_dimension})")
                    self._models[key] = LocalEmbeddingModel(
                        model_name=model_name,
                        target_dimension=target_dimension,
                    )
        return self._models[key]
    
    def list_loaded(self) -> List[str]:
        """列出已加载的模型"""
        return list(self._models.keys())


# 全局模型池
_model_pool = EmbeddingModelPool()


class EmbeddingService:
    """
    统一嵌入服务 - 支持本地模型和云端 API

    通过 EMBEDDING_PROVIDER 环境变量切换:
      local  → sentence-transformers 本地推理 (默认, 推荐科研)
      aliyun → 阿里云 DashScope API
      openai → OpenAI API
      ollama → Ollama 本地 API
    """

    def __init__(self, model_name: Optional[str] = None):
        """
        初始化嵌入服务
        
        Args:
            model_name: 指定模型名称。为 None 时使用全局配置。
                        本地模型格式: "BAAI/bge-m3" 等
                        API 模型格式: "text-embedding-v2", "text-embedding-3-small", "nomic-embed-text"
        """
        self._model_name_override = model_name
        self.provider = self._resolve_provider(model_name)
        self._client = None
        self._local_model: Optional[LocalEmbeddingModel] = None

        if self.provider == "local":
            actual_model = model_name or settings.local_embedding_model
            self._local_model = _model_pool.get(actual_model)

        logger.info(
            f"Embedding 服务初始化: provider={self.provider}, "
            f"model={self._get_model()}"
        )
    
    @staticmethod
    def _resolve_provider(model_name: Optional[str]) -> str:
        """根据模型名称推断 provider"""
        if model_name is None:
            return settings.embedding_provider
        
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
        elif self.provider == "aliyun":
            return settings.aliyun_embedding_model
        elif self.provider == "openai":
            return "text-embedding-3-small"
        elif self.provider == "ollama":
            return "nomic-embed-text"
        return settings.aliyun_embedding_model

    def get_dimension(self) -> int:
        """获取当前 provider 的向量维度"""
        if self.provider == "local":
            return self._local_model.dimension
        elif self.provider == "aliyun":
            return 1536
        elif self.provider == "openai":
            return 1536
        elif self.provider == "ollama":
            return 768
        return 1536

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
        else:
            return await self._api_embed_texts(valid_texts)

    # ===================================================================
    #  本地模型推理
    # ===================================================================

    async def _local_embed_single(self, text: str, is_query: bool = False) -> List[float]:
        """本地模型单文本嵌入"""
        loop = asyncio.get_event_loop()

        def _encode():
            return self._local_model.encode_sync([text], is_query=is_query)

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
                texts, is_query=is_query, show_progress=len(texts) > 10
            )

        embeddings = await loop.run_in_executor(None, _encode)
        result = [emb.tolist() for emb in embeddings]
        logger.info(f"本地模型批量 Embedding 完成: {len(result)} 个向量")
        return result

    # ===================================================================
    #  API 模型调用
    # ===================================================================

    async def _api_embed_text(self, text: str) -> List[float]:
        """API 单文本嵌入"""
        client = self._get_api_client()
        model = self._get_model()

        try:
            logger.debug(f"API Embedding: model={model}, len={len(text)}")
            response = await client.embeddings.create(input=text, model=model)
            embedding = response.data[0].embedding
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
                batch_embeddings = [d.embedding for d in response.data]
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


# 全局默认实例
embedding_service = EmbeddingService()


def get_embedding_service() -> EmbeddingService:
    """获取默认嵌入服务实例（使用全局配置）"""
    return embedding_service


# 按模型名缓存的 EmbeddingService 实例
_service_cache: Dict[str, EmbeddingService] = {}
_service_cache_lock = threading.Lock()


def get_embedding_service_for_model(model_name: Optional[str] = None) -> EmbeddingService:
    """
    获取针对指定模型的嵌入服务实例
    
    Args:
        model_name: 模型名称，如 "BAAI/bge-m3"。
                    为 None 或与全局配置相同时返回默认实例。
    
    Returns:
        配置了对应模型的 EmbeddingService 实例
    """
    # None 或与全局模型相同 -> 返回默认实例
    if not model_name:
        return embedding_service
    
    default_model = embedding_service._get_model()
    if model_name == default_model:
        return embedding_service
    
    # 按模型名缓存
    if model_name not in _service_cache:
        with _service_cache_lock:
            if model_name not in _service_cache:
                logger.info(f"创建模型专用 EmbeddingService: {model_name}")
                _service_cache[model_name] = EmbeddingService(model_name=model_name)
    
    return _service_cache[model_name]
