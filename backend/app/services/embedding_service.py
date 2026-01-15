"""
Embedding 服务 - 使用阿里云 text-embedding-v2 模型
"""
import hashlib
import numpy as np
from typing import List, Optional
from openai import AsyncOpenAI
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings


class EmbeddingService:
    """
    嵌入服务 - 默认使用阿里云 text-embedding-v2
    
    阿里云 text-embedding-v2 参数：
    - 向量维度: 1536
    - 最大输入长度: 2048 tokens
    - 支持中英文
    - API 文档: https://help.aliyun.com/zh/dashscope/developer-reference/text-embedding-api-details
    """
    
    # 阿里云 text-embedding-v2 向量维度
    ALIYUN_EMBEDDING_DIMENSION = 1536
    
    def __init__(self):
        self.provider = settings.embedding_provider
        self._client: Optional[AsyncOpenAI] = None
        logger.info(f"Embedding 服务初始化: provider={self.provider}, model={self._get_model()}")
        
    def _get_client(self) -> AsyncOpenAI:
        """获取客户端"""
        if self._client is None:
            if self.provider == "aliyun":
                # 阿里云 DashScope OpenAI 兼容接口
                api_key = settings.aliyun_embedding_api_key or settings.aliyun_api_key
                if not api_key:
                    logger.warning("阿里云 Embedding API Key 未配置，请设置 ALIYUN_EMBEDDING_API_KEY")
                self._client = AsyncOpenAI(
                    api_key=api_key,
                    base_url=settings.aliyun_base_url,
                )
                logger.debug(f"使用阿里云 DashScope API: {settings.aliyun_base_url}")
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
                # 默认使用阿里云
                logger.info(f"未知的 embedding provider: {self.provider}，使用默认阿里云")
                self._client = AsyncOpenAI(
                    api_key=settings.aliyun_embedding_api_key or settings.aliyun_api_key,
                    base_url=settings.aliyun_base_url,
                )
        return self._client
    
    def _get_model(self) -> str:
        """获取模型名称"""
        if self.provider == "aliyun":
            return settings.aliyun_embedding_model  # text-embedding-v2
        elif self.provider == "openai":
            return "text-embedding-3-small"
        elif self.provider == "ollama":
            return "nomic-embed-text"
        return settings.aliyun_embedding_model
    
    def get_dimension(self) -> int:
        """获取向量维度"""
        if self.provider == "aliyun":
            return self.ALIYUN_EMBEDDING_DIMENSION  # 1536
        elif self.provider == "openai":
            return 1536  # text-embedding-3-small 默认维度
        elif self.provider == "ollama":
            return 768  # nomic-embed-text 维度
        return self.ALIYUN_EMBEDDING_DIMENSION
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def embed_text(self, text: str) -> List[float]:
        """获取单个文本的嵌入向量"""
        if not text.strip():
            return []
            
        client = self._get_client()
        model = self._get_model()
        
        try:
            logger.debug(f"Embedding 请求: model={model}, text_length={len(text)}")
            response = await client.embeddings.create(
                input=text,
                model=model,
            )
            embedding = response.data[0].embedding
            logger.debug(f"Embedding 成功: dimension={len(embedding)}")
            return embedding
        except Exception as e:
            logger.error(f"Embedding 失败: {e}")
            raise
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量获取文本的嵌入向量"""
        if not texts:
            return []
        
        # 过滤空文本
        valid_texts = [t for t in texts if t.strip()]
        if not valid_texts:
            return []
        
        client = self._get_client()
        model = self._get_model()
        
        try:
            # 阿里云 text-embedding-v2 批量限制为 25 个
            batch_size = 20 if self.provider == "aliyun" else 20
            all_embeddings = []
            
            logger.info(f"批量 Embedding: total={len(valid_texts)}, batch_size={batch_size}")
            
            for i in range(0, len(valid_texts), batch_size):
                batch = valid_texts[i:i + batch_size]
                logger.debug(f"处理批次 {i//batch_size + 1}: {len(batch)} 条文本")
                
                response = await client.embeddings.create(
                    input=batch,
                    model=model,
                )
                batch_embeddings = [d.embedding for d in response.data]
                all_embeddings.extend(batch_embeddings)
            
            logger.info(f"批量 Embedding 完成: {len(all_embeddings)} 个向量")
            return all_embeddings
        except Exception as e:
            logger.error(f"批量 Embedding 失败: {e}")
            raise
    
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
        embeddings: List[List[float]]
    ) -> List[float]:
        """批量计算相似度"""
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


# 全局实例
embedding_service = EmbeddingService()


def get_embedding_service() -> EmbeddingService:
    """获取嵌入服务实例"""
    return embedding_service
