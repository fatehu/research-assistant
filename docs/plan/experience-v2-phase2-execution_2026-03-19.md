# Experience V2 Phase 2 Execution

Date: 2026-03-19
Phase: 2
Status: helper-runtime trace proof captured with explicit model-generated narrative-brief layer on bootstrap; route/render proof deferred to later phases

## Scope

This note covers only Phase 2 (`experience_session_v2`) contract/proof intent:

- sessionized generation loop semantics
- session persistence and resume behavior
- runtime budget semantics for iterations/tools/context carry
- internal narrative-understanding / reading-strategy behavior before final artifact drafting
- constrained artifact-drafting ReAct behavior for targeted external-resource/media retrieval
- normalized working-state/resource-bundle semantics for drafting inputs
- promotion boundary from session state to final artifact reference

Out of scope for this note:

- final `page_artifact_v2` contract completion (Phase 3)
- `/experience-v2` rendering/output acceptance (Phase 4)
- claims that `/experience-v2` route output is already implemented

Authoritative plan:

- [experience-v2-incremental-rebuild_2026-03-19.md](./experience-v2-incremental-rebuild_2026-03-19.md)

## Control Sample Intent

Control semantics target:

- `paper=78`
- `page=7`
- `reader=curious_generalist`

Fixture for this phase:

- [experience_session_v2_control_trace_p78_p7.json](./fixtures/experience_session_v2_control_trace_p78_p7.json)

The fixture is runtime-captured from current Phase 2 helper functions using the checked-in Phase 1 dossier control fixture (`paper=78`, `page=7`) plus a deterministic mocked model response for the bootstrap `narrative_brief` call. It is not route-level runtime proof and not live UI runtime proof.

Important dependency note:

- the checked-in Phase 1 dossier fixture now carries ordered structured neighboring-page rows
- current-page grounding remains real local control-sample data; the authoritative Phase 1 dossier proof sample is now refreshed from the live v2 neighboring-page structured extractor path rather than legacy normalized rows
- the checked-in Phase 2 session trace fixture is regenerated from that corrected Phase 1 dossier fixture and from the model-generated bootstrap brief path; it no longer preserves `legacy_phase1_fixture` / `normalized_from` neighboring-page artifacts in narrative-brief continuity text
- this is sufficient for Phase 2 helper/session semantics and narrative-brief proof, while live route wiring remains separate

## Accepted Phase 2 Semantics

### 1) Dedicated Session Namespace And Plan Kind

Phase 2 introduces a dedicated session cache lane distinct from v1 plan caching and distinct from final artifact storage.

Required semantics:

- session key includes explicit v2 session namespace token
- session payload declares `plan_kind` intent for sessionized ReAct execution
- session cache entries are not reused as completed reader artifacts

### 2) Bootstrap vs Revise Context Carry

Accepted context policy:

- bootstrap turn may include full `reading_dossier_v2` plus required trace context
- product intent for bootstrap is full current-page grounding plus full neighboring-page structured context once the corrected Phase 1 lane lands
- session execution is intended to pass through an internal narrative brief / reading-strategy layer before final artifact drafting, rather than jumping directly from raw context to rendered page output
- when the artifact draft cannot be completed from source context alone, the intended Phase 2 path is a constrained artifact-drafting ReAct subflow for targeted retrieval rather than open-ended search
- targeted retrieval results should be normalized into a working-state/resource-bundle layer rather than left only as raw trace transcripts
- revise turns must use compact carry inputs (delta packet / planning view / state handle), not full dossier replay
- full dossier replay after bootstrap is reserved for cold resume/corruption recovery only
- ordinary revise turns must not repeatedly replay the full neighboring-page structured payload once bootstrap has already established session state
- helper/session output now carries an explicit model-generated `narrative_brief` object on bootstrap and a compact brief slice in revise `delta_packet`

### 3) Failed-State And Resume Semantics

Accepted failure lifecycle:

- timeout/error transitions session to explicit failed state (not silently dropped)
- failed sessions remain inspectable and resumable using persisted state
- resume is preferred over fresh restart when persisted state is valid
- restart path is reserved for irrecoverable/corrupt session state

### 4) Session vs Final Artifact Separation

Accepted boundary:

- session state contains iterative drafts/traces/tool usage and stop metadata
- final artifact store is a separate promoted object lane
- no reader-facing completion is implied until explicit promotion succeeds

This note does not claim that `/experience-v2` rendering is already wired.

### 5) Runtime Budget Semantics

Phase 2 runtime policy requires:

- explicit `max_iterations`
- explicit `max_tool_rounds`
- no repeated tool calls for identical dossier hash in one active session path
- no repeated full dossier replay in normal revise turns
- no second full-generation pass after a completed artifact already exists

Current implementation boundary:

- explicit budget fields and contract-level guardrails are present in the session payload and helper surface
- completed-artifact blocking is implemented in helper logic
- iteration/tool-round enforcement and duplicate-tool-call enforcement are executed at the helper layer
- current checked-in proof should be read as helper/session semantics plus narrative-layer direction; targeted retrieval / normalized resource-bundle behavior is part of the accepted Phase 2 path even when a given control fixture does not exercise every retrieval branch
- therefore Phase 2 is accepted at helper level (enforcement + helper-runtime trace proof), while route/render runtime proof remains out of scope

## Proof Artifact Coverage (This Commit)

The helper-runtime control trace fixture demonstrates:

- one bootstrap pass with full dossier context and an explicit model-generated reading-strategy object
- one explicit narrative-brief / reading-strategy layer before later drafting work
- one compact revise pass without full dossier replay
- a failed state (`timeout`) and explicit persisted resume
- budget fields and budget-consumption progression
- promotion metadata boundary (session lane vs final artifact reference lane)
- session semantics remain stable while revise turns continue to use compact carry instead of replaying the full neighboring-page payload

Additional intended Phase 2 interpretation:

- session execution should be understood as: `bootstrap context -> internal narrative brief / reading strategy -> constrained artifact-drafting ReAct when needed -> normalized working-state/resource bundle -> artifact drafting`
- this note does not claim every control fixture already exercises a non-trivial retrieval branch, but it does fix the execution-order semantics that later runtime proof should follow

Phase 2 proof artifacts covered in this note:

- session trace:
  - [experience_session_v2_control_trace_p78_p7.json](./fixtures/experience_session_v2_control_trace_p78_p7.json)
- captured narrative-brief trace slices:
  - `iterations[0].narrative_brief`
  - `iterations[1].context_carry.delta_packet.narrative_brief`
- prompt contract note:
  - section `Prompt Contract Note (Phase 2 Proof Artifact)` below
- persistence note (session vs final artifact):
  - section `Persistence Note (Phase 2 Proof Artifact)` below
- runtime-budget note (iteration/tool limits and resume-vs-restart):
  - section `Runtime-Budget Note (Phase 2 Proof Artifact)` below

## Prompt Contract Note (Phase 2 Proof Artifact)

Accepted prompt-input contract semantics for `experience_session_v2`:

- bootstrap prompt input includes full `reading_dossier_v2` grounding and required trace context
- bootstrap input now assumes ordered structured neighboring-page context rather than compact summary rows
- bootstrap brief generation now consumes full current-page grounding plus full neighboring-page structured context and validates the returned JSON against the `ExperienceSessionV2NarrativeBrief` contract
- bootstrap is the only heavy-context turn by default; later drafting/revise turns should consume compact working-state inputs such as `narrative_brief`, current draft state, unresolved slots, and normalized resource bundle state
- prompt sequencing should preserve the intended intermediate layer:
  - internal narrative understanding / reading strategy first
  - constrained artifact-drafting ReAct only when explicit draft gaps require targeted retrieval
  - structured artifact drafting after the needed working-state/resource inputs exist
- revise prompt input carries compact incremental context (session-state handle plus delta/planning view), not full dossier replay
- prompt lanes must preserve phase boundary:
  - session prompts operate on session drafts/trace state
  - final artifact promotion is a separate step and not implied by any single revise turn
- helper/function names may vary; semantic obligations above are the contract target

This is a contract note, not a claim that prompt transcripts are fully captured from production runtime.

## Persistence Note (Phase 2 Proof Artifact)

Accepted persistence contract for Phase 2:

- `experience_session_v2` session state persists iterative execution artifacts:
  - iteration packets
  - tool/MCP trace metadata
  - stop reason
  - resume token/checkpoint reference
- failed sessions persist explicit failed state and remain resumable/inspectable
- final promoted reader artifact persists in a separate `page_artifact_v2` lane
- session records are not treated as completed reader artifacts

This note documents persistence semantics only and does not claim Phase 3 artifact schema completion or Phase 4 route rendering.

## Runtime-Budget Note (Phase 2 Proof Artifact)

Phase 2 runtime budget policy for session execution:

- explicit caps:
  - `max_iterations`
  - `max_tool_rounds`
- execution policy:
  - avoid duplicate tool calls for identical dossier hash within one active session path
  - avoid repeated full dossier replay in ordinary revise turns
  - the same rule applies to the fuller neighboring-page structured payload once the Phase 1 input correction lands; ordinary revise turns must not repeatedly resend it
  - prefer resume from persisted state over restart when state is valid
  - allow restart/full replay only for cold-resume necessity or corruption recovery
  - artifact-drafting retrieval should be targeted and gap-driven; tool usage is not an invitation to open-ended exploration
  - normalized retrieval outputs/resources should carry forward; raw transcripts should not accumulate as the de facto working state
  - planned retrieval/binding paths should fail loudly with specific errors when required inputs or supported node kinds are missing, rather than silently degrading into legacy compact behavior
- completion policy:
  - no second full-generation pass when a completed promoted artifact already exists

The control fixture values are runtime-captured helper outputs and are used here as proof of helper-level Phase 2 semantics; they are not proof of `/experience-v2` route/runtime rendering.

## Current Phase Boundary Statement

Phase 2 proof layer is documented here, but this note does not certify:

- `/experience-v2` route-level runtime capture proof
- Phase 3 final artifact schema implementation
- Phase 4 `/experience-v2` render behavior
- end-to-end reader-visible completion

## Next Step

Phase 2 is accepted at the helper/session layer. Phase 3 (`page_artifact_v2`) may proceed at the artifact layer on top of the landed ordered neighboring-page input contract, while route/render proof remains later work.

Later route-level and render-level runtime proof remains desirable, but it is not a blocking tail for closing the Phase 2 helper/session contract.
