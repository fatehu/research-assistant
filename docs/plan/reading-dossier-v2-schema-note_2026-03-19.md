# Reading Dossier V2 Schema Note

Date: 2026-03-19
Scope: Phase 1 only
Status: helper/input-contract landed; route wiring remains later work

## Goal

`reading_dossier_v2` is the input contract for `/experience-v2`.

It exists to correct the current mismatch where `/experience` only sees compressed `targets/assets/quality + adjacent summaries`, while `/read` already owns much richer current-page grounding.

This note defines:

- the minimal current-page rich grounding that must survive into v2
- the separate owner/fidelity class for adjacent-page context
- the minimum cache/version rules needed to keep v2 isolated from v1
- the corrected Phase 1 requirement that neighboring-page inputs be ordered, much fuller structured-page JSON rather than compact continuity summaries

## Non-Goals

This note does not define:

- the session loop
- the final page artifact
- the final `/experience-v2` UI

## Control Sample

Primary control sample:

- `paper=78`
- `page=7`
- `reader=curious_generalist`

This remains the Phase 1 proof sample only. It is not the eventual sole coverage page.

## Contract Shape

Draft contract:

```json
{
  "version": "reading_dossier_v2",
  "dossier_contract": "rd2.v1",
  "focus_page": 7,
  "reader_profile": "curious_generalist",
  "compose_source_signature": "...",
  "current_page": {
    "owner": "compose/page_grounding_v1",
    "fidelity": "grounded_evidence",
    "build_meta": {
      "pipeline_version": "...",
      "build_mode": "..."
    },
    "rich_grounding": {
      "page": 7,
      "page_image": {
        "url": "...",
        "width": 0,
        "height": 0
      },
      "layout_atoms": [],
      "evidence_map": []
    }
  },
  "adjacent_pages": {
    "owner": "api/adjacent_page_extraction",
    "fidelity": "ordered_structured_context",
    "limits": {},
    "pages": []
  },
  "derived_adjacent_bridge_cues": {
    "owner": "runtime",
    "fidelity": "derived_summary",
    "items": []
  },
  "cache_meta": {
    "dossier_namespace": "...",
    "compose_pipeline_version": "...",
    "source_sig_hash": "..."
  },
  "meta": {}
}
```

## Current-Page Lane

The current-page lane is owned by `/read` via `compose payload.page_grounding_v1`.

Source:

- [literature_reader_compose_service.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/literature_reader_compose_service.py#L17204)

### Minimal Required Fields

These fields are required in `current_page.rich_grounding`:

- `page`
- `page_image.url`
- `page_image.width`
- `page_image.height`
- `layout_atoms[].layout_id`
- `layout_atoms[].reading_order`
- `layout_atoms[].node_kind`
- `layout_atoms[].layout_type`
- `layout_atoms[].layout_sub_type`
- `layout_atoms[].clean_text`
- `layout_atoms[].normalized_text`
- `layout_atoms[].normalization_reason`
- `layout_atoms[].normalization_mode`
- `layout_atoms[].normalization_confidence`
- `layout_atoms[].include_in_main_flow`
- `evidence_map[].source_layout_id`
- `evidence_map[].source_block_ids`
- `evidence_map[].block_positions`
- `evidence_map[].layout_pos`
- `evidence_map[].table_cells` when applicable

### Optional Current-Page Fields

These may be present but are not required for the Phase 1 minimum contract:

- `reading_nodes`
- `layout_atoms[].raw_text`
- `layout_atoms[].blocks`
- `layout_atoms[].canonical_block_ids`
- `layout_atoms[].region_hint`
- `layout_atoms[].alignment`
- `layout_atoms[].line_height`
- `layout_atoms[].meta`
- `evidence_map[].geometry_source`
- `evidence_map[].highlight_strategy`
- `evidence_map[].meta`
- `page_image.path`
- `page_image.source`
- `page_image.origin_url`
- `page_image.local_cached`
- `meta`

### Why `reading_nodes` Is Optional

In the current builder, `reading_nodes` is largely a 1:1 projection of `layout_atoms`:

- `node_id = layout:{layout_id}`
- `source_layout_ids = [layout_id]`
- `source_block_ids = canonical_block_ids`

That makes it useful as a convenience view, but not part of the Phase 1 minimum information set.

## Adjacent-Page Lane

Adjacent-page context is not current-page grounding with lower quality.

It is a different fidelity class with a different owner.

Owner:

- API-side adjacent-page extraction

Primary producer:

- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1461)
- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1758)

### Adjacent-Page Fidelity Rules

Draft lane metadata:

```json
{
  "owner": "api/adjacent_page_extraction",
  "fidelity": "ordered_structured_context",
  "source_surface": "rendered_pdf_page_image",
  "extraction_backend": "dashscope_multimodal",
  "extraction_model": "qwen3-vl-flash",
  "reference_only": false,
  "parity_with_current_page_grounding": "none",
  "supports_target_ids": false,
  "supports_layout_ids": false,
  "supports_source_block_ids": false,
  "supports_geometry": false,
  "quote_safe": false,
  "completeness": "ordered_best_effort_immediate_neighbors_only",
  "allowed_uses": [
    "continuity",
    "carry_over",
    "local boundary reconstruction",
    "figure/table/equation understanding"
  ],
  "disallowed_uses": [
    "replace current-page evidence",
    "source-of-truth claims",
    "highlight anchors",
    "exact quotation provenance"
  ]
}
```

Different fidelity does not mean short-summary-only payloads.

For v2, the old compact `reference_only_bridge_context` row shape is a legacy/baseline starting point only. It is not the accepted neighboring-page contract end-state because small local fragments near the page boundary are often the decisive continuity signal.

### Adjacent-Page Data Shape

Phase 1 canonical adjacent-page row must preserve ordered, much fuller structured-page content:

- `page`
- `relation`
- `source`
- `fidelity = ordered_structured_context`
- `reference_only = false`
- `page_image`
- `page_summary` as optional derived metadata only
- `content_stream[]`
- `continuation_hints[]`
- `meta`

Target planning-level row shape:

```json
{
  "page": 8,
  "relation": "previous_page|next_page",
  "source": "neighbor_page_vlm_parse",
  "fidelity": "ordered_structured_context",
  "reference_only": false,
  "page_image": {
    "url": "...",
    "width": 0,
    "height": 0
  },
  "page_summary": "...",
  "content_stream": [
    {
      "seq": 1,
      "type": "paragraph",
      "text": "...",
      "ocr_text": "...",
      "role": "body"
    },
    {
      "seq": 2,
      "type": "figure",
      "label": "Figure 3",
      "caption": "...",
      "description": "...",
      "ocr_text": "..."
    },
    {
      "seq": 3,
      "type": "table",
      "label": "Table 2",
      "caption": "...",
      "description": "...",
      "columns": ["..."],
      "rows": [["...", "..."]]
    },
    {
      "seq": 4,
      "type": "equation",
      "label": "(1)",
      "normalized_text": "...",
      "description": "..."
    }
  ],
  "continuation_hints": ["..."],
  "meta": {}
}
```

Rules for `content_stream[]`:

- items are ordered by page reading order
- local continuity, especially near page boundaries, must be preserved
- the page must not be collapsed into only `summary + body_text`
- item types should include at least:
  - `paragraph`
  - `figure`
  - `table`
  - `equation`
  - `caption`
  - `header`
  - `footer`
- OCR/body text must be preserved as ordered items rather than a single compressed string
- figure labels/captions, table rows/cells, and equation structure should be preserved when extractable
- when exact structure is unavailable, fallback must preserve ordered raw extracted text rather than discard it

The compact runtime form used in cached generative-plan metadata should be treated as a derived/legacy view, not the source-of-truth adjacent-page contract.

## Derived Bridge-Cue Lane

`derived_adjacent_bridge_cues` exists to keep runtime-generated continuity summaries separate from adjacent-page extraction itself.

This lane is:

- runtime-owned
- derived
- not a replacement for `adjacent_pages.pages`

Phase 1 does not need to populate this lane, but the contract must reserve it so future runtime output does not overwrite the adjacent source lane.

This lane must not be used to justify keeping the adjacent source lane compressed. Continuity summaries are derivative metadata, not the primary neighboring-page payload.

## Cache And Version Rules

Phase 1 already has one hard conclusion:

- changing `compose_source_signature` alone is not enough to isolate v2 from v1

Minimum coexistence rules:

- `reading_dossier_v2` must use a dossier-specific plan namespace distinct from v1 plan cache namespace
- if v2 will also emit a forked compose payload, compose must use a distinct `pipeline_version`
- v2 must not reuse v1 fallback cache keys by accident

Source references:

- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1868)
- [literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py#L1888)
- [test_literature_reader_api.py](/mnt/d/codefield/agent-platform/research-assistant/backend/tests/test_literature_reader_api.py#L2427)

### Minimum Cache Inputs

At minimum, the dossier lane needs:

- `paper_id`
- `focus_page`
- `selected_kb_id`
- `reader_profile`
- `compose_source_signature`
- `reading_dossier_version` or `dossier_contract`
- model/provider hash when the consuming runtime depends on model-specific behavior

## Phase 1 Deliverables From This Note

Implementation may proceed when these concrete artifacts are added:

- schema definition for `reading_dossier_v2`
- one control-sample dossier snapshot
- one source-to-field mapping note
- one adjacent-page owner/fidelity note
- one cache/version coexistence note

## Open Questions Reserved For Implementation

- whether `current_page.rich_grounding` should preserve optional `reading_nodes` in Phase 1 or only in a debug lane
- whether adjacent-page extraction remains `focus_page±1` only or becomes configurable in Phase 2
- exact namespace token naming for dossier cache keys
