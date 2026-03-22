# Experience V2 Phase 1 Execution

Date: 2026-03-19
Phase: 1
Status: helper/input-contract landed for current-page + neighboring-page lanes; live v1 adjacent extraction remains separate

## Scope

This execution log covers only Phase 1:

- define `reading_dossier_v2`
- prove the correct input surface for v2
- do not start session loop, final artifact, or `/experience-v2` rendering yet

Authoritative plan:

- [experience-v2-incremental-rebuild_2026-03-19.md](./experience-v2-incremental-rebuild_2026-03-19.md)

## Control Sample

- `paper=78`
- `page=7`
- `reader=curious_generalist`

## Research Completed

### Current-Page Rich Grounding

Confirmed:

- current-page rich grounding already exists in `compose_payload.page_grounding_v1`
- producer is compose service, not `/experience` API
- main fields are `layout_atoms`, `reading_nodes`, `evidence_map`, and `page_image`

Primary source:

- [literature_reader_compose_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/literature_reader_compose_service.py#L17204)

Resulting decision:

- `reading_dossier_v2.current_page.rich_grounding` will be sourced from `page_grounding_v1`
- v2 will not attempt to recreate this grounding from compressed targets/assets

### Adjacent-Page Context

Confirmed:

- adjacent-page context is API-owned, image-first VLM extraction
- it is currently `reference_only`
- it is not geometry-backed grounding
- current live shape and cached/runtime shape are not the same

Primary source:

- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1461)
- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1758)

Resulting decision:

- `reading_dossier_v2` will separate:
  - `current_page`
  - `adjacent_pages`
  - `derived_adjacent_bridge_cues`
- the current compact/reference-only adjacent lane is only a baseline starting point
- product-level Phase 1 requires upgrading neighboring-page inputs to ordered, much fuller structured-page JSON rather than `summary + body_text`
- different fidelity from current-page grounding remains true, but it must not be implemented as short-summary-only payloads
- neighboring-page structured outputs should be treated as page-scoped reusable intermediates, not throwaway continuity summaries for a single focus-page request
- dossier assembly should prefer reusing a cached neighboring-page structured artifact for `paper/page` over regenerating the same neighboring-page parse on every request

### Cache / Version Coexistence

Confirmed:

- v1 plan cache currently shares namespace `v33`
- v1 compatibility behavior includes empty-signature fallback keys
- changing `source_signature` alone is not enough to isolate v2

Primary source:

- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1868)
- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1888)
- [test_literature_reader_api.py](/mnt/d/codefield/agent-platform/research-assistant/backend/tests/test_literature_reader_api.py#L2427)

Resulting decision:

- Phase 1 must introduce a dossier-specific plan namespace
- if compose payload forks later, compose must also get a distinct `pipeline_version`

## New Phase 1 Artifacts

- [reading-dossier-v2-schema-note_2026-03-19.md](./reading-dossier-v2-schema-note_2026-03-19.md)
- [reading_dossier_v2_control_sample_p78_p7.json](./fixtures/reading_dossier_v2_control_sample_p78_p7.json)

## Worker 2 Proof Update (Focused Tests + Control Sample)

Focused tests were added in `backend/tests/test_literature_reader_api.py` for `reading_dossier_v2` helper/schema behavior.

Proof points covered:

- current-page rich grounding lane preserves `compose_payload.page_grounding_v1` under `current_page.rich_grounding`
- adjacent pages are isolated in `adjacent_pages.pages` with explicit lane metadata:
  - `adjacent_pages.owner = api/adjacent_page_extraction`
  - `adjacent_pages.fidelity = ordered_structured_context`
  - `adjacent_pages.limits.reference_only = false`
  - `adjacent_pages.pages[*].content_stream[]` preserves ordered neighboring-page rows
- cache/version lane is present:
  - `cache_meta.dossier_namespace` includes a v2 dossier namespace token
  - `cache_meta.compose_pipeline_version` is copied through
  - `cache_meta.source_sig_hash` is retained
- fake ordered-structured neighboring rows are rejected loudly instead of being silently normalized from compact summary fields

Important honesty correction:

- the checked-in control fixture now carries an ordered structured neighboring-page lane
- current-page fields still come from the real local control sample payload path
- neighboring-page ordered rows in the fixture are refreshed from the live v2 structured extractor path (`_build_experience_adjacent_page_structured_context_v2(...)`) and no longer preserve `legacy_phase1_fixture` normalization markers

Control-sample snapshot artifact:

- [reading_dossier_v2_control_sample_p78_p7.json](./fixtures/reading_dossier_v2_control_sample_p78_p7.json)
- this artifact is derived from the actual local control sample (`paper=78`, `page=7`) instead of placeholder/test literals
- capture method:
  - run in backend container (`research_backend`) with `PYTHONPATH=/app`
  - open DB session via `async_session_factory`, query `Paper.id == 78`
  - load real cached compose payload via `get_literature_reader_compose_service().get_latest_cached_payload_only(... page=7)`
  - build adjacent-page context via `_build_experience_adjacent_page_structured_context_v2(...)` for `page=6/8`
  - construct dossier with `_build_reading_dossier_v2(...)`
  - resulting real fields in the fixture include:
    - `compose_source_signature` with `compose_v3|p:78|kb:0|...`
    - `current_page.rich_grounding.page_image.url = http://localhost:8888/api/v1/literature/reader/grounding-page-assets/78/7`
    - `layout_atoms` count = `10`
    - `evidence_map` count = `10`
    - `adjacent_pages.pages[*].meta.parser_model = qwen3-vl-flash`
    - `adjacent_pages.pages[*].meta.page_scope_cache_layer` reflects the live v2 structured extractor cache lane
- normalization applied:
  - `current_page.rich_grounding.page_image.origin_url` is replaced with `__normalized__docmind_signed_origin_url__` because the raw value is a time-limited signed URL with security token

## Implementation Status Update

Current verified status:

- focused helper tests for `reading_dossier_v2` are passing, including the degraded current-page grounding path when `page_grounding_v1` is missing and explicit rejection of fake ordered-structured neighboring rows
- control-sample dossier artifact is sourced from local `paper=78/page=7` runtime data and uses:
  - `dossier_contract = rd2.v1`
  - `current_page.owner/fidelity`
  - `adjacent_pages.owner/fidelity`
  - `derived_adjacent_bridge_cues`
  - `cache_meta.dossier_namespace`
  - helper-emitted `build_meta` and `meta.compose_*` fields from the captured compose payload
- current `reading_dossier_v2` helper now rejects legacy compact neighboring rows for the v2 path and only accepts ordered structured neighboring-page content
- page-scoped neighboring-page structured-artifact cache/reuse remains a plan-level requirement, but broad route wiring/reuse proof is still deferred beyond this helper/input-contract note

## Master-Agent Validation

Verified in the main thread:

- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "reading_dossier_v2 or experience_session_v2 or page_artifact_v2" -q`
  - result: `31 passed, 48 deselected`
- control-sample artifact is captured from the actual local control sample payload path (`DB paper lookup -> cached compose payload -> adjacent extraction -> dossier builder`) with one explicit signed-URL normalization

## Next Step

Phase 1 helper/input-contract work is now sufficient for Phase 2 and Phase 3 helper layers to consume ordered structured neighboring-page context without silently degrading back to compact summary rows. Route-level reuse and rendering remain later-phase work.
