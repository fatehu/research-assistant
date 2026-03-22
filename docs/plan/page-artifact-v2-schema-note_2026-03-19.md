# Page Artifact V2 Schema Note

Date: 2026-03-19
Phase: 3
Status: final page_artifact_v2 contract remains stable; live Phase 3 now reaches it through a model-generated internal artifact_draft plus bounded promotion

## Purpose

Document the landed `page_artifact_v2` final contract used after Phase 3 drafting/promotion.

## Required Contract Lanes

- identity/meta:
  - `version = page_artifact_v2`
  - `artifact_contract_id = page_artifact_v2.contract.v1`
  - `focus_page`, `reader_profile`, `dossier_signature`, optional `session_id`
- spine policy:
  - `current_page_spine.primary = true`
  - `current_page_spine.main_segment_ids` must anchor to `original_excerpt` segment IDs
  - `current_page_spine` coverage metadata must prove near-complete current-page main-flow retention at helper level
  - adjacent continuity remains implicit and non-coequal in provenance
- presentation contract fields (required):
  - `template_id`
  - `layout_recipe`
  - `presentation_mode`
  - `widget_family`
  - `motion_preset`
  - `interaction_policy`
- upstream drafting note:
  - live Phase 3 may first generate an internal model-authored `artifact_draft`
  - that internal draft is not the final formal contract
  - promotion into `page_artifact_v2` remains required before cache persistence or rendering
- reader-facing content blocks:
  - `reading_blocks[].segment_kind` supports and requires:
    - `original_excerpt`
    - `authored_explanation`
    - `figure_slot`
    - `term_annotation`
    - `external_resource`
- provenance lane:
  - `continuity_mode = current_page_primary_ordered_adjacent_context`
  - `adjacent_context_pages` preserved
  - `include_adjacent_as_coequal_anchor = false`
  - `source_lanes` includes current-page lane + adjacent-pages metadata
- figure-slot renderability:
  - `figure_slot` blocks must carry deterministic current-page bindings
  - binding may resolve through a current-page figure layout anchor or a current-page page-image anchor
  - helper proof fixture now demonstrates figure-layout binding against the checked-in real dossier fixture

## Fixture Alignment Note

The Phase 3 control fixture is a final-contract runtime snapshot from the checked-in Phase 1 dossier fixture, with one explicit size normalization:

- `provenance.source_lanes.current_page.rich_grounding` is replaced by a `rich_grounding_ref` pointer to the Phase 1 dossier fixture.

All other `page_artifact_v2` contract lanes in the fixture are direct helper-runtime output.

## Non-Goal

This note does not claim `/experience-v2` route or UI rendering proof.
