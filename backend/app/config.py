"""
Application settings loaded from environment variables.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
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
    aliyun_dashscope_api_base: str = "https://dashscope.aliyuncs.com/api/v1"
    aliyun_model: str = "qwen-plus"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Embedding
    embedding_provider: str = "local"  # local / mock / aliyun / openai / ollama
    aliyun_embedding_api_key: str = ""
    aliyun_embedding_model: str = "text-embedding-v2"
    local_embedding_model: str = "BAAI/bge-m3"
    local_embedding_device: str = "auto"
    local_embedding_batch_size: int = 32
    local_embedding_max_length: int = 8192
    local_embedding_cache_dir: str = ""
    local_embedding_normalize: bool = True
    local_embedding_dimension: int = 0
    local_embedding_prefer_safetensors: bool = True
    local_embedding_local_files_only: bool = False
    local_embedding_allow_legacy_pickle_fallback: bool = True
    local_embedding_allow_runtime_cpu_fallback: bool = True
    mock_embedding_model: str = "mock/deterministic"
    mock_embedding_dimension: int = 256
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
    knowledge_search_timeout_ms: int = 45000
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
    knowledge_resume_running_documents_on_startup: bool = True
    knowledge_resume_running_documents_limit: int = 20

    # Chunk quality gate (RAG ingestion)
    chunk_quality_gate_enabled: bool = False
    chunk_quality_gate_model: str = "qwen3.5:0.8b-stable"
    chunk_quality_gate_timeout_seconds: int = 40
    chunk_quality_gate_bad_threshold: float = 0.50
    chunk_quality_gate_suspect_threshold: float = 0.65
    chunk_quality_gate_neighbor_window: int = 1
    chunk_quality_gate_max_chunks: int = 300
    chunk_quality_gate_doc_fail_ratio: float = 0.55
    chunk_quality_gate_fail_open: bool = True
    chunk_quality_gate_fail_on_unrepaired_bad: bool = False
    chunk_repair_enabled: bool = True
    chunk_repair_max_rounds: int = 1
    chunk_repair_max_fragments: int = 120
    chunk_repair_max_chars_per_chunk: int = 1800

    # PDF line-level RAG ingestion
    pdf_rag_line_pipeline_enabled: bool = True
    pdf_rag_fail_open: bool = True
    pdf_rag_qwen_device: Literal["auto", "cpu", "cuda"] = "auto"
    pdf_rag_action_model_dir: str = str(
        Path("models") / "runtime" / "qwen_action_lora_base_v4_drop"
    )
    pdf_rag_clean_model_dir: str = str(
        Path("models") / "runtime" / "qwen_clean_lora_merged_v1"
    )
    pdf_rag_chunk_model_dir: str = str(
        Path("models") / "runtime" / "qwen_chunk_lora_v5_context_paragraph"
    )
    pdf_rag_ocr_enabled: bool = False
    pdf_rag_ocr_model: str = "qwen3.5:0.8b-stable"
    pdf_rag_ocr_timeout_seconds: int = 30
    pdf_rag_ocr_dpi: int = 180
    pdf_rag_ocr_padding: float = 4.0

    # Knowledge base online multimodal ingestion
    kb_online_mm_ingest_enabled: bool = False
    kb_online_mm_default_mode: Literal["local_fast", "online_mm", "auto"] = "local_fast"
    kb_online_mm_primary_model: str = "qwen3-vl-flash"
    kb_online_mm_fallback_model: str = "qwen-vl-ocr-latest"
    kb_online_mm_chunk_planner_model: str = "qwen3.5-plus"
    kb_online_mm_timeout_ms: int = 300000
    kb_online_mm_render_dpi: int = 200
    kb_online_mm_trim_whitespace: bool = True
    kb_online_mm_trim_padding_px: int = 24
    kb_online_mm_image_max_side: int = 1920
    kb_online_mm_image_max_pixels: int = 2600000
    kb_online_mm_pages_per_call: int = 1
    kb_online_mm_window_overlap: int = 0
    kb_online_mm_extract_max_concurrency: int = 6
    kb_online_mm_extract_max_tokens: int = 20000
    kb_online_mm_max_pages_soft_limit: int = 80
    kb_online_mm_max_estimated_cost_rmb: float = 1.0

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
    literature_agent_max_iterations: int = 14
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
    pdf_layout_parser: Literal["auto", "markitdown", "docling", "document_mind", "none"] = "document_mind"
    pdf_layout_min_chars: int = 200
    # Alibaba Cloud Document Mind parser (optional, parser-chain fallback)
    reader_document_mind_enabled: bool = True
    reader_document_mind_allowlist: str = ""
    document_mind_access_key_id: str = ""
    document_mind_access_key_secret: str = ""
    document_mind_region_id: str = "cn-hangzhou"
    document_mind_endpoint: str = "docmind-api.aliyuncs.com"
    document_mind_option: str = "docStructure"
    document_mind_poll_interval_seconds: float = 1.5
    document_mind_timeout_seconds: int = 90
    document_mind_raw_cache_dir: str = "./uploads/docmind_raw_cache"
    # Reader compose multimodal assist (layout only, no rewriting)
    reader_mm_assist_enabled: bool = True
    # Cheap multimodal advisor for parser/line-level hints
    reader_mm_parser_model: str = "qwen3-vl-flash"
    # Fallback for parser schema repair (can be a stronger text model)
    reader_mm_parser_fallback_model: str = "qwen-vl-ocr-latest"
    # Main planner for layout segments/components
    reader_mm_layout_model: str = "qwen3.5-plus"
    reader_mm_primary_model: str = "qwen3-vl-flash"
    reader_mm_fallback_model: str = "qwen3-vl-plus"
    reader_mm_timeout_ms: int = 90000
    reader_mm_parser_timeout_ms: int = 120000
    reader_mm_max_tokens: int = 7000
    reader_mm_parser_max_tokens: int = 7000
    reader_mm_max_calls_per_page: int = 1
    reader_mm_trigger_confidence: float = 0.62
    reader_mm_max_doc_trigger_ratio: float = 0.08
    reader_mm_image_resolution: int = 96
    reader_mm_image_max_side: int = 1024
    reader_mm_line_candidate_limit: int = 100
    reader_mm_block_candidate_limit: int = 72
    reader_mm_prompt_version: str = "mm_layout_v1"
    reader_mm_layout_schema_version: str = "mm_layout_schema_v1"
    # Reader compose layout plan v2 (Qwen planner + DeepSeek assembler)
    reader_layout_plan_v2_enabled: bool = True
    # Deprecated: layout plan v2 now applies globally when enabled.
    reader_layout_plan_v2_allowlist: str = ""
    # Unified reader pipeline mode switch.
    reader_pipeline_mode: Literal["legacy", "single_agent_v2"] = "single_agent_v2"
    # Optional allowlist for gradual rollout. Empty means all papers/pages for current mode.
    reader_pipeline_allowlist_papers: str = ""
    reader_pipeline_allowlist_pages: str = ""
    # Reader simplified 4-step pipeline switch (DocMind truth -> Stage1 semantic -> Stage2 design -> DeepSeek assembly).
    # Deprecated. Kept for one release as compatibility fallback to reader_pipeline_mode.
    reader_simplified_pipeline_enabled: bool = False
    # Optional allowlist for gradual rollout. Empty means all papers/pages when enabled.
    # Deprecated. Use reader_pipeline_allowlist_*.
    reader_simplified_allowlist_papers: str = ""
    reader_simplified_allowlist_pages: str = ""
    # Cache/payload version isolation token for simplified pipeline.
    reader_pipeline_version: str = "layout_uid_v1"
    # Single-agent V2 runtime contract
    reader_agent_provider: Literal["deepseek", "openai", "aliyun", "ollama"] = "aliyun"
    reader_agent_model: str = "qwen-3.5-plus"
    reader_agent_timeout_ms: int = 90000
    reader_agent_max_tokens: int = 12000
    # Optional dedicated Phase 3 artifact-drafting model settings.
    # Empty / zero values cleanly fall back to the Phase 2 reader-agent config.
    reader_artifact_agent_provider: str = ""
    reader_artifact_agent_model: str = ""
    reader_artifact_agent_timeout_ms: int = 0
    reader_artifact_agent_max_tokens: int = 24000
    reader_agent_max_steps: int = 12
    reader_agent_max_repair_rounds: int = 2
    # Optional startup cleanup for legacy compose cache keys.
    reader_cache_cleanup_on_startup: bool = False
    reader_cache_cleanup_timeout_seconds: int = 120
    reader_cache_cleanup_scan_count: int = 200
    # Enable parser-v2 contract (doc_nav_tree + block_groups + word/char anchoring).
    reader_page_structure_v2_enabled: bool = True
    # Enable polygon-first evidence geometry; fallback to bbox when unavailable.
    reader_polygon_highlight_enabled: bool = True
    reader_compose_layout_llm_enabled: bool = True
    reader_compose_layout_llm_prompt_version: str = "compose_layout_llm_v1"
    reader_compose_layout_llm_max_blocks: int = 80
    # Compose end-to-end latency budget in milliseconds.
    reader_compose_latency_budget_ms: int = 600000
    # Hard ceiling for compose latency budget to avoid unbounded runs.
    reader_compose_latency_budget_max_ms: int = 600000
    # Reader compose agent runtime (component stream / tool-calling)
    reader_agent_component_stream_enabled: bool = True
    reader_agent_tools_enabled: bool = True
    reader_agent_tool_whitelist: str = "paper_read,knowledge_search"
    # Agent assembly timeout in seconds. <=0 means no timeout.
    reader_agent_assembly_timeout_seconds: int = 180
    # Generative reader agent (resource enrichment + interactive module planning)
    generative_reader_agent_provider: Literal["deepseek", "openai", "aliyun", "ollama"] = "aliyun"
    generative_reader_agent_model: str = ""
    generative_reader_agent_tool_whitelist: str = "paper_read,knowledge_search,web_search,web_scrape"
    generative_reader_agent_max_iterations: int = 6
    generative_reader_agent_timeout_seconds: int = 180
    generative_reader_planner_timeout_seconds: int = 75
    generative_reader_page_generation_timeout_seconds: int = 90
    # Legacy DeepSeek assembly timeout in seconds. <=0 means no timeout.
    reader_compose_layout_llm_timeout_seconds: int = 120
    # Reader anchor quality gate / jump control
    reader_anchor_min_confidence: float = 0.78
    reader_anchor_eval_gate_enabled: bool = True
    reader_anchor_eval_min_hit_rate: float = 0.8
    reader_anchor_eval_min_iou: float = 0.25
    reader_anchor_eval_max_misjump: float = 0.2
    # Legacy compatibility flag
    reader_multimodal_enabled: bool = False
    # Reader compose external image fallback (disabled by default)
    reader_external_image_enabled: bool = False

    @field_validator("debug", "sqlalchemy_echo", mode="before")
    @classmethod
    def _parse_bool_like_flags(cls, value):  # type: ignore[no-untyped-def]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "on", "debug", "dev", "development"}:
            return True
        if text in {"0", "false", "no", "n", "off", "release", "prod", "production", "staging"}:
            return False
        return value

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
