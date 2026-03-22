# Experience V2 Phase 3 Execution

Date: 2026-03-19
Phase: 3
Status: bounded model-generated artifact_draft path landed in live v2 build; promotion to page_artifact_v2 remains the final contract; validator and explicit failure paths remain enforced

## Scope

This note covers the landed Phase 3 drafting/promotion path and the final `page_artifact_v2` validation boundary.

In scope:

- model-generated internal `artifact_draft` generation on top of the Phase 2 `narrative_brief`
- bounded targeted-retrieval rounds during drafting
- promotion from `artifact_draft` into the existing `page_artifact_v2` contract
- helper/runtime contract validation and renderability checks
- final-contract control fixture capture from the checked-in Phase 1 dossier fixture
- docs updates for schema/validator/execution proof
- incremental alignment of artifact intent toward a renderer-decoupled guided-reading content model, not a card-only output shape

Out of scope:

- `/experience-v2` route rendering proof
- UI runtime proof
- any claim that `/experience-v2` rendering already exists

Authoritative plan:

- [experience-v2-incremental-rebuild_2026-03-19.md](./experience-v2-incremental-rebuild_2026-03-19.md)

## Landed Phase 3 Contract Semantics

- current-page primary spine is mandatory:
  - `current_page_spine.primary = true`
  - main spine segment IDs are anchored to `original_excerpt` blocks sourced from `current_page`
  - helper output now preserves all eligible current-page main-flow excerpt anchors instead of truncating to a weak three-excerpt sample
- neighboring-page continuity remains implicit:
  - provenance continuity mode is `current_page_primary_ordered_adjacent_context`
  - adjacent pages are not co-equal anchors (`include_adjacent_as_coequal_anchor = false`)
  - artifact provenance no longer hardcodes the older adjacent reference-only contract as the artifact-level continuity model
- presentation contract fields are explicit in helper output:
  - `template_id`
  - `layout_recipe`
  - `presentation_mode`
  - `widget_family`
  - `motion_preset`
  - `interaction_policy`
- required reading block kinds are present in valid artifacts:
  - `original_excerpt`
  - `authored_explanation`
  - `figure_slot`
  - `term_annotation`
  - `external_resource`
- incremental broader node support is landed without reopening the artifact contract:
  - `table_slot`
  - `equation_slot`
  - `media_slot`
  - `aside_content`
  - unsupported requested node kinds now fail explicitly
  - unresolved slot/media bindings now fail explicitly instead of silently dropping the requested node
- Phase 3 should be interpreted as structured content-artifact work for a near-full guided-reading narrative:
  - the artifact remains schema-bound and renderable
  - renderer/template strategy may realize it as card-based, editorial, scrollytelling, or mixed-layout output
  - the choice to reuse mature web component libraries/design systems for those renderer realizations belongs primarily to later Phase 4 implementation work rather than to Phase 3 helper/contract proof
  - this is an incremental alignment patch, not a Phase 3 reset
- asset/media placement is intentionally renderer-decoupled:
  - images, tables, equations, and related media should preferentially land through structured slots / bindings / media refs rather than arbitrary raw page code generation
  - `external_resource` content should preferentially bind to normalized retrieval outputs / resource-bundle items rather than free-form invented resource text
  - unsupported requested node kinds or unresolved required media/resource bindings should fail explicitly instead of being silently dropped or broadly downgraded
- figure-slot renderability is now deterministic at helper level:
  - `figure_slot` blocks must resolve to a current-page figure-layout anchor or a page-image anchor
  - the refreshed control fixture demonstrates figure-layout anchor binding against the checked-in real dossier fixture
- live Phase 3 no longer relies primarily on helper-written authored paragraphs:
  - the bootstrap `narrative_brief` now feeds a model-generated internal `artifact_draft`
  - drafting may run more than once if targeted retrieval is explicitly requested
  - helper code now mainly validates, normalizes, promotes, and binds the draft into `page_artifact_v2`

## Runtime / Proof Note

Latest combined focused verification:

- `./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "page_artifact_v2 or experience_session_v2 or reading_dossier_v2 or experience_v2 or workbench_v2" -q`
- result: `58 passed, 54 deselected`

Fixture capture method:

- source dossier fixture: `docs/plan/fixtures/reading_dossier_v2_control_sample_p78_p7.json`
- live drafting path:
  - `app.api.literature._run_reader_experience_v2_artifact_drafting_loop(...)`
  - `app.api.literature._generate_experience_session_v2_artifact_draft(...)`
  - `app.api.literature._promote_experience_v2_artifact_draft_to_authored_plan(...)`
- final-contract builder: `app.api.literature._build_page_artifact_v2_from_dossier(...)`
- validator: `app.api.literature._validate_page_artifact_v2_contract(...)`
- proof level:
  - focused backend tests now cover the bounded model-draft/runtime path
  - the checked-in control fixture still represents final-contract helper/runtime output, not a live provider capture transcript
- note:
  - the refreshed control fixture continues to exercise ordered adjacent-context provenance and `aside_content`
  - focused tests now cover model-draft generation, requested retrieval rounds, resource-bundle normalization, promotion, and the explicit failure cases for unsupported node kinds / unresolved bindings

Updated artifacts:

- [page-artifact-v2-schema-note_2026-03-19.md](./page-artifact-v2-schema-note_2026-03-19.md)
- [page-artifact-v2-validator-note_2026-03-19.md](./page-artifact-v2-validator-note_2026-03-19.md)
- [page_artifact_v2_control_sample_p78_p7.json](./fixtures/page_artifact_v2_control_sample_p78_p7.json)

## Remaining Honesty Caveat

This phase now proves the bounded model-generated drafting path at backend runtime level. `/experience-v2` render behavior remains out of scope for this note.

Related Phase 2/3 execution-order note:

- the intended drafting path is now: internal narrative brief / reading strategy first, then bounded model-generated `artifact_draft`, then constrained targeted retrieval only when the draft explicitly asks for it, then promotion into the formal `page_artifact_v2`
- card-based presentation remains a valid renderer strategy, but the artifact contract should not be interpreted as a card-only content model
