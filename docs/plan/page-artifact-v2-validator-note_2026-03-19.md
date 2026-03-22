# Page Artifact V2 Validator Note

Date: 2026-03-19
Phase: 3
Status: validator implemented and verified against the promoted final artifact from the bounded model-generated Phase 3 draft path

## Validation Targets

`_validate_page_artifact_v2_contract(...)` rejects artifacts that violate these semantics:

- explicit contract identity missing/invalid (`version`, `artifact_contract_id`)
- current-page primary spine rule not satisfied
- required presentation contract fields missing/empty:
  - `template_id`
  - `layout_recipe`
  - `presentation_mode`
  - `widget_family`
  - `motion_preset`
  - `interaction_policy`
- reader blocks fail to support mixed content lanes:
  - `original_excerpt`
  - `figure_slot`
  - `term_annotation`
  - `external_resource`
- `original_excerpt` blocks not sourced from `current_page` or page mismatch vs `focus_page`
- `current_page_spine.main_segment_ids` missing or referencing non-`original_excerpt` segments
- `figure_slot` blocks missing deterministic current-page render bindings
- current-page spine coverage dropping below near-complete main-flow retention
- provenance allowing adjacent pages as co-equal anchors

Validation boundary reminder:

- validation still targets the final `page_artifact_v2` object
- Phase 3 may now use a model-generated internal `artifact_draft` and bounded retrieval rounds before promotion
- validator scope does not expand into truth adjudication or unbounded runtime critique

## Proof Snapshot Validation Status

For the current Phase 3 control fixture:

- promotion/final builder used: `_build_page_artifact_v2_from_dossier(...)`
- validator used: `_validate_page_artifact_v2_contract(...)`
- validation result: `valid=true`, `renderable=true`, `errors=[]`

## Remaining Boundary

Validator pass at helper level does not imply `/experience-v2` route/render/UI runtime proof.
