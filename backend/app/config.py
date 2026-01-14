"""
应用配置管理
"""
from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # 应用配置
    app_name: str = "AI科研助手"
    app_version: str = "1.0.0"
    debug: bool = True
    
    # 数据库
    database_url: str = "postgresql://research_user:research_password_123@localhost:5432/research_assistant"
    
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    
    # JWT 配置
    secret_key: str = "your-super-secret-key-change-this-in-production-min-32-chars"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24小时
    
    # LLM 配置
    default_llm_provider: Literal["deepseek", "openai", "aliyun", "ollama"] = "deepseek"
    
    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    
    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    
    # 阿里云通义
    aliyun_api_key: str = ""
    aliyun_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    aliyun_model: str = "qwen-plus"
    
    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    
    # Embedding - 阿里云
    embedding_provider: str = "aliyun"
    aliyun_embedding_api_key: str = ""
    aliyun_embedding_model: str = "text-embedding-v2"
    
    def get_llm_config(self, provider: str = None):
        """获取 LLM 配置"""
        provider = provider or self.default_llm_provider
        
        configs = {
            "deepseek": {
                "api_key": self.deepseek_api_key,
                "base_url": self.deepseek_base_url,
                "model": self.deepseek_model,
            },
            "openai": {
                "api_key": self.openai_api_key,
                "base_url": self.openai_base_url,
                "model": self.openai_model,
            },
            "aliyun": {
                "api_key": self.aliyun_api_key,
                "base_url": self.aliyun_base_url,
                "model": self.aliyun_model,
            },
            "ollama": {
                "api_key": "ollama",  # Ollama 不需要 API key
                "base_url": f"{self.ollama_base_url}/v1",
                "model": self.ollama_model,
            },
        }
        
        return configs.get(provider, configs["deepseek"])


@lru_cache
def get_settings() -> Settings:
    """获取缓存的配置实例"""
    return Settings()


settings = get_settings()
