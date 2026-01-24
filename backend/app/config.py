"""
应用配置管理

所有可配置参数通过环境变量管理，支持 .env 文件
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
    
    # ========== 应用基础配置 ==========
    app_name: str = "AI科研助手"
    app_version: str = "1.0.0"
    debug: bool = True
    
    # ========== 数据库配置 ==========
    database_url: str = "postgresql://research_user:research_password_123@localhost:5432/research_assistant"
    
    # ========== Redis 配置 ==========
    redis_url: str = "redis://localhost:6379/0"
    
    # ========== JWT 配置 ==========
    secret_key: str = "your-super-secret-key-change-this-in-production-min-32-chars"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24小时
    
    # ========== LLM 提供商配置 ==========
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
    
    # ========== Embedding 配置 ==========
    embedding_provider: str = "aliyun"
    aliyun_embedding_api_key: str = ""
    aliyun_embedding_model: str = "text-embedding-v2"
    
    # ========== LLM 推理参数 ==========
    llm_temperature: float = 0.7           # LLM 默认温度 (0-1, 越高越随机)
    llm_max_tokens: int = 4096             # LLM 最大输出 tokens
    
    # ========== ReAct Agent 配置 ==========
    react_max_iterations: int = 10          # Agent 最大推理迭代次数
    react_temperature: float = 0.7          # Agent 推理温度
    react_output_max_length: int = 500      # 工具输出显示的最大长度
    
    # ========== 代码执行配置 ==========
    code_execution_timeout: int = 30        # 单次代码执行超时（秒）
    kernel_idle_timeout: int = 7200         # 内核空闲超时（秒），默认 2 小时
    
    # ========== Notebook 上下文配置 ==========
    # 这些参数控制 Agent 获取的 Notebook 上下文范围
    notebook_context_cells: int = 5               # 包含的最近 Cell 数量
    notebook_context_cell_max_length: int = 200   # 单个 Cell 代码预览最大字符数
    notebook_context_variables: int = 15          # 包含的最大变量数量
    notebook_context_output_cells: int = 5        # recent_outputs 包含的 Cell 数量
    
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
