"""
Application settings loaded from environment variables.
"""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base
    app_name: str = "AI科研助手"
    app_version: str = "1.0.0"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = True
    sqlalchemy_echo: bool = False

    # Database / cache
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440

    # LLM providers
    default_llm_provider: Literal["deepseek", "openai", "aliyun", "ollama"] = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    aliyun_api_key: str = ""
    aliyun_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    aliyun_model: str = "qwen-plus"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Embedding
    embedding_provider: str = "local"  # local / aliyun / openai / ollama
    aliyun_embedding_api_key: str = ""
    aliyun_embedding_model: str = "text-embedding-v2"
    local_embedding_model: str = "BAAI/bge-m3"
    local_embedding_device: str = "auto"
    local_embedding_batch_size: int = 32
    local_embedding_max_length: int = 8192
    local_embedding_cache_dir: str = ""
    local_embedding_normalize: bool = True
    local_embedding_dimension: int = 0
    embedding_dimension_policy: Literal["fixed", "adaptive"] = "adaptive"
    embedding_dim_small: int = 256
    embedding_dim_medium: int = 512
    embedding_dim_small_max_chunks: int = 2000
    embedding_dim_medium_max_chunks: int = 10000
    embedding_dim_hysteresis_enabled: bool = True
    embedding_dim_hysteresis_small_up: int = 2500
    embedding_dim_hysteresis_small_down: int = 1500
    embedding_dim_hysteresis_medium_up: int = 12000
    embedding_dim_hysteresis_medium_down: int = 8000
    embedding_dim_rebuild_async: bool = True

    # Reranker / retrieval
    enable_reranker: bool = True
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "auto"
    reranker_top_k: int = 20

    enable_hybrid_retrieval: bool = True
    hybrid_vector_top_k: int = 20
    hybrid_text_top_k: int = 20
    hybrid_rrf_k: int = 60
    pgvector_hnsw_ef_search: int = 40
    pgvector_hnsw_ef_search_mode: Literal["fixed", "adaptive"] = "adaptive"
    pgvector_hnsw_ef_search_min: int = 32
    pgvector_hnsw_ef_search_max: int = 96
    agent_knowledge_score_threshold: float = 0.5
    search_timeout_primary_ms: int = 300000
    search_timeout_fallback_ms: int = 90000
    search_timeout_auto_fallback: bool = True

    # Query rewrite
    enable_query_rewrite: bool = True
    query_rewrite_strategies: str = "synonym,hyde,decompose"
    query_rewrite_max_synonyms: int = 3
    query_rewrite_max_subqueries: int = 3
    query_rewrite_max_variants: int = 8
    query_rewrite_hyde_max_chars: int = 240
    query_rewrite_timeout_seconds: int = 12
    query_rewrite_temperature: float = 0.2
    query_rewrite_cache_size: int = 2000
    query_rewrite_cache_ttl_seconds: int = 1800
    query_rewrite_skip_short_chars: int = 10

    # Contextual compression
    enable_contextual_compression: bool = True
    contextual_compression_mode: Literal["batch", "single"] = "batch"
    contextual_compression_timeout_seconds: int = 12
    contextual_compression_temperature: float = 0.0
    contextual_compression_min_relevance: float = 4.0
    contextual_compression_max_chars_per_chunk: int = 2200
    contextual_compression_max_output_tokens: int = 400
    contextual_compression_max_concurrency: int = 3
    contextual_compression_batch_max_chunks: int = 8
    contextual_compression_skip_rerank_threshold: float = 0.82
    contextual_compression_batch_retry_attempts: int = 2

    # Document processing safety guard
    document_processing_stale_timeout_seconds: int = 7200

    # LLM runtime
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096

    # Generic tool runtime
    tool_default_timeout_seconds: int = 20
    tool_default_retry_count: int = 1
    tool_output_max_tokens: int = 1200
    tool_output_truncate_head_ratio: float = 0.75
    tool_selection_enabled: bool = True
    tool_selection_fallback_tools: str = "datetime,calculator"

    # Search / scrape external providers
    tavily_api_key: str = ""
    web_scrape_enforce_robots: bool = True
    web_scrape_domain_min_interval_seconds: float = 1.5

    # ReAct
    react_max_iterations: int = 10
    literature_agent_max_iterations: int = 8
    react_temperature: float = 0.7
    react_output_max_length: int = 500
    agent_function_calling_enabled: bool = True
    agent_function_calling_fallback_xml: bool = True
    agent_parallel_tool_calls_enabled: bool = True
    agent_parallel_tool_calls_max_concurrency: int = 4
    agent_context_budget_enabled: bool = True
    agent_context_max_input_tokens: int = 10000
    agent_context_window_turns: int = 8
    agent_context_summary_trigger_tokens: int = 7000
    agent_persist_steps_enabled: bool = True
    agent_longterm_memory_enabled: bool = False
    agent_memory_top_k: int = 3
    agent_memory_retention_days: int = 180
    agent_memory_max_items_per_user_channel: int = 2000
    agent_memory_scan_limit: int = 200
    agent_memory_scope_match_boost: float = 0.18
    agent_memory_default_channels: str = "chat,codelab_agent,notebook_agent,literature_agent"

    # MCP (Phase 1)
    mcp_enabled: bool = False
    mcp_tool_prefix: str = "mcp"
    mcp_call_timeout_seconds: int = 20
    mcp_servers: str = "[]"
    mcp_config_path: str = "mcp_servers.json"
    mcp_tool_routes: str = "{}"
    mcp_route_timeout_seconds: int = 15
    mcp_route_retry_attempts: int = 2
    mcp_route_retry_backoff_seconds: float = 0.5
    mcp_route_circuit_breaker_failures: int = 3
    mcp_route_circuit_breaker_open_seconds: int = 120

    # Code execution
    code_execution_timeout: int = 30
    kernel_idle_timeout: int = 7200
    codelab_sandbox_enabled: bool = True
    codelab_exec_timeout_hard_seconds: int = 20
    codelab_max_concurrency_per_user: int = 2
    codelab_direct_execute_enabled: bool = False
    cors_allow_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    auto_create_tables: bool = False

    # API rate limiting
    api_rate_limit_enabled: bool = True
    api_rate_limit_storage: Literal["auto", "redis", "memory"] = "auto"
    api_rate_limit_redis_url: str = ""
    api_rate_limit_window_seconds: int = 60
    api_rate_limit_auth_per_minute: int = 20
    api_rate_limit_chat_per_minute: int = 60
    api_rate_limit_knowledge_per_minute: int = 120
    api_rate_limit_codelab_per_minute: int = 30

    # CodeLab sandbox runner
    codelab_runner_enabled: bool = True
    codelab_runner_url: str = "http://codelab-runner:8099"
    codelab_runner_token: str = ""
    codelab_runner_timeout_seconds: int = 25
    codelab_runner_connect_timeout_seconds: int = 3

    # Notebook context
    notebook_context_cells: int = 5
    notebook_context_cell_max_length: int = 200
    notebook_context_variables: int = 15
    notebook_context_output_cells: int = 5

    # PDF layout parser
    pdf_layout_parser: Literal["auto", "markitdown", "docling", "none"] = "auto"
    pdf_layout_min_chars: int = 200

    def get_llm_config(self, provider: str = None):
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
                "api_key": "ollama",
                "base_url": f"{self.ollama_base_url}/v1",
                "model": self.ollama_model,
            },
        }
        return configs.get(provider, configs["deepseek"])

    def get_cors_allow_origins(self) -> list[str]:
        origins = [item.strip() for item in self.cors_allow_origins.split(",")]
        return [item for item in origins if item]

    @model_validator(mode="after")
    def validate_security_constraints(self):
        if self.app_env not in {"staging", "production"}:
            return self

        weak_secret_markers = {
            "",
            "change-me",
            "your-super-secret-key-change-this-in-production-min-32-chars",
            "secret",
            "password",
        }
        normalized_secret = (self.secret_key or "").strip()
        if (
            len(normalized_secret) < 32
            or normalized_secret in weak_secret_markers
            or normalized_secret.startswith("your-super-secret-key")
        ):
            raise ValueError("SECRET_KEY is missing or weak for staging/production")

        normalized_db_url = (self.database_url or "").strip().lower()
        if not normalized_db_url:
            raise ValueError("DATABASE_URL is required for staging/production")
        if any(token in normalized_db_url for token in ["research_password_123", ":123456@", ":password@"]):
            raise ValueError("DATABASE_URL contains weak default credentials")

        if self.codelab_runner_enabled and not (self.codelab_runner_token or "").strip():
            raise ValueError("CODELAB_RUNNER_TOKEN is required when CODELAB_RUNNER_ENABLED=true")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
