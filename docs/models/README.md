# Models

This directory documents the runtime model setup used by the local RAG system and the `docling-fast` PDF backend.

## Current Active Model Paths

### Main backend (`research_backend`)
- Embedding model: `BAAI/bge-m3`
- Reranker model: `BAAI/bge-reranker-v2-m3`
- Runtime cache root inside container: `/app/model_cache`
- Hugging Face cache inside container:
  - `HF_HOME=/app/model_cache/huggingface`
  - `HUGGINGFACE_HUB_CACHE=/app/model_cache/huggingface/hub`
  - `TRANSFORMERS_CACHE=/app/model_cache/huggingface/transformers`
- Effective SentenceTransformer repo cache:
  - `/app/model_cache/models--BAAI--bge-m3`
  - `/app/model_cache/models--BAAI--bge-reranker-v2-m3`

### PDF hybrid backend (`research_pdf_hybrid_backend`)
- Backend service: `app.services.local_structured_pdf.opendataloader_upstream_hybrid_server`
- Default benchmark/runtime path:
  - `force_ocr=false`
  - `enrich_formula=false`
  - `enrich_picture_description=false`
- Current active benchmark line does not use Qwen enrich stages.

## Host Cache Mount Strategy

To avoid first-request stalls when loading large Hugging Face models, the main backend now mounts the host cache read-only and hydrates missing repos into the writable Docker volume at startup.

### Compose mount
- Host cache source:
  - `${HOST_HUGGINGFACE_CACHE_DIR:-/mnt/c/Users/yui/.cache/huggingface}`
- Mounted into container as:
  - `/app/host_model_cache/huggingface` (read-only)

### Startup hydration
`backend/docker-entrypoint.sh` checks `MODEL_CACHE_PREWARM_REPOS` and copies any complete host-side repo that is missing `snapshots/` and `blobs/` in the writable container cache.

Current default:
- `MODEL_CACHE_PREWARM_REPOS=BAAI/bge-m3,BAAI/bge-reranker-v2-m3`

Hydration target:
- `/app/model_cache/models--<repo>`

This keeps runtime caches writable while still reusing the host machine's already-downloaded model files.

## Why This Exists

Before this change, the backend only mounted the named Docker volume:
- `model_cache:/app/model_cache`

The main backend uses `SentenceTransformer(..., cache_folder=/app/model_cache)`, so the repo path that actually matters is:
- `/app/model_cache/models--<repo>`

The `HF_HOME/hub` directory alone is not authoritative for this service. When diagnosing model readiness, check `/app/model_cache/models--<repo>` first.

## Current Known State

As of 2026-03-29:
- Windows host cache contains a complete `BAAI/bge-m3` repo
- Windows host cache does **not** currently contain a complete `BAAI/bge-reranker-v2-m3` repo
- The current Docker volume already contains usable runtime snapshots for both:
  - `/app/model_cache/models--BAAI--bge-m3`
  - `/app/model_cache/models--BAAI--bge-reranker-v2-m3`
- Therefore the host-cache mount is now primarily a cold-start/bootstrap safeguard, not the only source of truth for the currently running backend

## Operational Notes

### Verify cache inside backend container
```bash
docker exec research_backend sh -lc 'find /app/model_cache/models--BAAI--bge-m3 -maxdepth 2 -type d | sort'
```

### Verify host cache mount is visible
```bash
docker exec research_backend sh -lc 'find /app/host_model_cache/huggingface/hub/models--BAAI--bge-m3 -maxdepth 2 -type d | sort'
```

### Recreate backend after compose changes
```bash
docker compose up -d backend
```

## Follow-up Work

- Prewarm `BAAI/bge-reranker-v2-m3` so reranker startup is also deterministic
- Add a model-ready health probe for embedding/reranker availability
- Add an instance-level load lock in embedding/reranker services so concurrent uploads do not race model initialization
