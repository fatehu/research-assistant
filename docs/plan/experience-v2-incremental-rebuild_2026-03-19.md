# Experience V2 Incremental Rebuild

Date: 2026-03-19
Owner: master-agent
Status: active

Plan status relative to existing docs:

- this document narrows and supersedes the v2-facing implementation direction for `/experience` and `/workbench`
- [generative-ui-guided-reading-plan.md](./generative-ui-guided-reading-plan.md) remains a historical design/reference document
- [generative-ui-rollout-plan.md](./generative-ui-rollout-plan.md) remains a historical rollout log for v1 and should not be treated as the active execution plan for v2
- when this document conflicts with prior experience/workbench planning documents, this document wins for v2 execution

## Why This Exists

`/experience` v1 has been repeatedly patched but still optimizes for the wrong product.

Current v1 strengths:

- grounded
- inspectable
- fallback-safe
- schema-controlled

Target v2 strengths:

- page-native
- agentic
- tool-iterative
- freeform generative UI

These do not remove the operational constraints that made v1 shippable.
V2 must still be:

- contract-validated
- cacheable and replayable
- fallback-safe
- versioned and gray-rollout friendly
- rollbackable beside v1

This document exists to prevent another open-ended rewrite. Work proceeds only in bounded phases, and every phase must leave proof artifacts behind before the next phase begins.

## Product Boundary

- `/read` stays the payload producer and remains frozen unless explicitly reopened.
- `/experience` remains the final reader-facing product slot.
- `/workbench` remains valuable. In the near term it is the inspection surface for v2 sessions and artifacts; fuller session orchestration is a future direction, not a Phase 4 hard requirement.

## Intended End-To-End Product Flow

The intended v2 flow is:

- `/read` current-page full payload
- neighboring-page ordered structured JSON
- page-scoped structured intermediate artifacts cached per paper/page and reused when a neighboring page later becomes the focus page
- one unified context bundle for generation
- agent first produces a narrative-understanding layer for reading strategy and continuity resolution
- agent then runs a constrained artifact-drafting ReAct loop, using tools only for targeted resource/media gaps rather than open-ended exploration
- agent then produces a structured content-artifact draft
- renderer/template layer binds assets and renders the final page

## Freeze Rules

Effective immediately:

- Stop patching `/experience` v1 generation semantics.
- Treat `/experience` v1 as the control group.
- Do not keep expanding v1 section/block/schema logic in pursuit of the new goal.
- New work lands under `v2` contracts and can coexist beside v1 until accepted.

## V2 Non-Negotiable Constraints

Even though v2 targets freer generative UI, the following remain mandatory:

- V2 must use explicit structured contracts at every boundary.
- V2 runtime outputs must validate before they are persisted or rendered.
- V2 must be cacheable, replayable, and resumable with deterministic cache keys.
- V2 must define timeout, failure, and partial-result semantics before reader routes are opened.
- V2 must run under explicit version namespaces and feature-gated coexistence with v1.
- V2 must support route-level rollback without polluting v1 cache or artifact stores.

Freeform page generation in v2 therefore means `freeform inside a validated artifact contract`, not `unbounded HTML with no governance`.

Validator boundary (hard):

- Validation in v2 governs contract integrity, renderability, replayability, cacheability, persistence safety, and route/runtime safety.
- Validation in v2 does not serve as a truth-adjudication layer for AI-authored explanation.
- v2 validators must not act as factual critics for AI-authored explanation content.
- legacy truth-first critic/repair/factual-gate behavior from v1 must not be reintroduced into the v2 primary path under new names.

## What Is Reused

The rebuild is not a full restart. These subsystems are reused:

- `/read` compose payload generation
- current page images and figure assets
- adjacent page extraction infrastructure
- tool and MCP integration plumbing
- auth, caching, route shell, and page ownership

## What Is Replaced

These v1 assumptions are explicitly replaced:

- compressed dossier instead of full `/read` grounding
- adjacent pages as weak reader-visible continuity patches or co-equal narrative anchors
- one-shot planner/tool/page pipeline
- fixed section/block registry as the final UI contract
- truth-first repair as the main product objective

## Coexistence And Rollback

V1 and v2 must coexist explicitly.

Required rollout rules:

- v2 uses separate route semantics until accepted, for example `/experience-v2` and `/workbench-v2`, or explicit route/version flags.
- v2 uses separate cache namespaces for dossier, session, and final artifact.
- v2 never reads from or writes to v1 final artifact keys.
- v2 feature flags must allow per-route or per-user rollout.
- rollback means disabling the v2 route/flag and leaving v1 behavior unchanged.
- rollback is a route-level or feature-flag-level control, not a per-request reader fallback behavior.
- fallback to v1 must not become the default behavior of a single `/experience-v2` request.

This coexistence mechanism is a hard requirement from Phase 1 onward, not a later rollout detail.

## Canonical Samples And Coverage Set

`paper=78`, `page=7`, `reader=curious_generalist` remains the control sample because it is the fastest way to compare v1 and v2 behavior on the same failing page.

It is not sufficient as the only proof page.

The rebuild must maintain a coverage set with at least:

- one figure-heavy page
- one body-heavy page
- one terminology-heavy or methods-heavy page

Phase proofs may focus on the control sample for speed, but phase completion cannot rely on that sample alone once implementation starts landing.

## Session, Artifact, And Persistence Semantics

V2 must distinguish three persisted objects:

1. `reading_dossier_v2`
   - deterministic input package
   - cache key based on paper/page/reader/version and source signature

2. `experience_session_v2`
   - iterative agent state
   - contains iteration packets, tool trace, stop state, and latest draft
   - resumable until complete, failed, or expired

3. `page_artifact_v2`
   - completed final reader-facing artifact only
   - the only object `/experience-v2` is allowed to render as ready content

Required rules:

- session keys and final artifact keys are separate
- final artifact materializes only from a completed session
- if a completed `page_artifact_v2` exists, the reader route renders it directly instead of starting a new generation pass
- warm revisit may restore only completed `page_artifact_v2`
- timeout/failure must enter a persisted failed session state
- failed or timed-out sessions may be inspected and resumed in `/workbench-v2`, but must not appear as completed reader content
- partial/seed/provisional session drafts are inspectable in `/workbench-v2`, not reader-visible in `/experience-v2`
- `/experience-v2` must not auto-generate seed/provisional/fallback reader content as a recovery strategy
- the reader route must not perform multi-round recovery or auto-regeneration loops after a visible timeout/failure
- rollback and fallback are not the same thing: rollback disables or routes away from v2; fallback must not silently occur inside a live v2 reader request

Reader-route cold-start contract:

- if no completed `page_artifact_v2` exists, `/experience-v2` does not render seed/provisional page content
- if a completed `page_artifact_v2` exists, `/experience-v2` renders the completed artifact immediately
- route behavior during cold start must be explicitly versioned and chosen before implementation:
  - wait shell only
  - queue/progress shell
- explicit fallback to v1 is not a normal cold-start reader behavior; it belongs to route-level rollback / feature-flag rollback only
- whichever behavior is chosen, it must be deterministic, token-efficient, and must not drift into showing partial reader content as if it were final
- on the reader path, the default rule is: completed artifact first; otherwise generation shell only; no seed/repair/fallback variants

## Phase Plan

### Phase 0: Freeze And Archive

Goal:

- freeze v1
- record why v1 is not the target

Required artifacts:

- this plan document
- one current-state summary
- one stable control sample page to use in all later proofs
- one declared coverage set for non-control validation

Proof gate:

- the control sample is agreed: `paper=78`, `page=7`, `reader=curious_generalist`
- coverage-set categories are declared for later validation

### Phase 1: `reading_dossier_v2`

Goal:

- build the correct input surface for v2 before any new page generation work starts

Must include:

- full `/read` grounding, not only compressed targets/assets
- current page render/image references
- current page structured reading nodes and evidence anchors
- adjacent pages as ordered structured-page JSON that preserves near-complete continuity, not weak summaries
- page-scoped cached structured-page intermediates so neighboring-page parses are reusable instead of being regenerated as throwaway continuity summaries

Input-boundary rules:

- `/read` remains the owner of current-page grounding and is not reopened in this phase
- `reading_dossier_v2` composes existing `/read` current-page outputs with a new adjacent-context adapter; it does not replace `/read`
- adjacent-page rich context must declare its owner and shape explicitly, instead of reusing the current compact bridge payload without contract changes
- the current `reference_only_bridge_context` lane is a legacy/baseline starting point only; it is not the accepted end-state for v2 Phase 1 input quality
- if adjacent pages cannot provide geometry-grade grounding, the dossier must say so explicitly rather than pretending parity with current-page grounding
- neighboring-page structured artifacts should be cached per page and reused when possible; dossier assembly should prefer reusing those page-scoped intermediates over regenerating neighboring-page context on every reader request

Neighboring-page structured-context requirement:

- this is a Phase 1 input-contract requirement, not a Phase 3 artifact concern
- `adjacent_pages` must evolve from compact summary/body-text payloads to ordered, much fuller structured-page JSON
- the contract must preserve local continuity near page boundaries, because small carry-over fragments are often the only continuity signal that matters
- page-level summary is optional derived metadata only; it must not be the primary neighboring-page payload

Target neighboring-page row shape:

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

Contract rules for `content_stream[]`:

- items are ordered by page reading order
- local continuity near the page boundary must be preserved explicitly
- the page must not be collapsed into only `summary + body_text`
- item types must include at least:
  - `paragraph`
  - `figure`
  - `table`
  - `equation`
  - `caption`
  - `header`
  - `footer`
- OCR/body text must be preserved as ordered items rather than one compressed string
- figure labels/captions, table rows/cells, and equation structure should be preserved when extractable
- when exact structure is unavailable, fallback must preserve ordered raw extracted text rather than discarding it

Ownership/fidelity rules:

- current page remains owned by `/read` through `compose/page_grounding_v1`
- neighboring pages remain a separate owner/fidelity lane from current-page grounding
- separate fidelity must not be implemented as short-summary-only payloads
- neighboring-page structured context may be weaker than geometry-grade grounding while still being ordered, much fuller, and sequence-preserving

Required output semantics:

- deterministic contract version
- validation entrypoint
- cache namespace
- source signature fields that explain when the dossier should invalidate
- page-scoped cache/version metadata for reusable neighboring-page structured artifacts

Required output contract:

- one deterministic `reading_dossier_v2` JSON artifact for the sample page

Required proof artifacts:

- dossier schema note
- sample dossier snapshot JSON
- source-to-field mapping note
- cache key note
- adjacent-context ownership note

Proof gate:

- the sample dossier clearly shows current-page rich grounding and adjacent-page rich context together
- the dossier contract states what is parity and what is not between current-page and adjacent-page evidence
- the neighboring-page lane is concrete enough that implementation cannot drift back to `summary + body_text` as the primary content payload
- no page generation logic is introduced yet

### Phase 2: `experience_session_v2`

Goal:

- replace the fixed pipeline with a session-based ReAct loop

Must include:

- session state
- iteration packet
- tool/MCP trace
- an internal narrative brief / reading strategy layer inside session execution before final artifact drafting
- a constrained artifact-drafting ReAct subflow for targeted external-resource/media retrieval when the draft cannot be completed from the source context alone
- a normalized working-state/resource-bundle layer so tool outputs do not remain only as raw trace transcripts
- stop reason
- session cache key
- resume rules
- timeout and failure semantics
- promotion rule from session to final artifact

Execution-order clarification:

- the agent should not jump directly from raw dossier context to the final rendered page
- Phase 2 session execution first resolves narrative understanding, reading strategy, and continuity handling
- Phase 2 may keep this as an internal session artifact rather than a new top-level persisted contract
- after that internal narrative layer exists, session execution may enter a constrained artifact-drafting ReAct loop
- targeted resource retrieval should be driven by explicit draft gaps, not by open-ended search behavior
- retrieval results should be normalized into working state / resource bundle inputs for drafting, rather than accumulated as unbounded raw tool transcripts
- Phase 3 artifact drafting then materializes the structured reader-facing content from that intermediate narrative layer

Cost guardrails:

- explicit `max_iterations`
- explicit `max_tool_rounds`
- no repeated tool calls for an identical dossier hash within the same active session path
- no second full-generation pass once a completed artifact already exists
- resume from persisted session state instead of restart when possible
- hard context-carry/runtime-cost rule:
  - bootstrap pass must carry full `reading_dossier_v2` grounding plus full neighboring-page structured context and required trace context
  - after bootstrap, normal drafting/revise cycles must use compact working-state/context-carry inputs (`narrative_brief`, current draft state, resource bundle, unresolved slots, planning view, delta packet, and/or referenced session-state handle) rather than reserializing the full dossier and full trace each round
  - full dossier replay, including the fuller neighboring-page structured payload, is reserved for bootstrap, cold resume when required, or corruption recovery; it is not allowed as default behavior for ordinary revise turns
  - raw tool outputs and retrieval transcripts must not be blindly appended every round; only normalized results needed for current drafting should carry forward
- explicit-failure rule:
  - planned retrieval or binding paths should fail loudly with specific errors when required inputs or supported node kinds are missing
  - Phase 2 should not silently degrade corrected v2 drafting paths back into legacy compact behavior

Phase 2 dependency note:

- Phase 2 session semantics depend on the corrected Phase 1 neighboring-page input surface
- session logic may remain valid on a weaker baseline, but continuity quality is input-bound and therefore cannot be treated as solved until the neighboring-page structured-context upgrade lands

Required proof artifacts:

- one session trace for the sample page
- one captured narrative brief / reading strategy artifact or trace slice from the sample session
- one captured artifact-drafting working-state/resource-bundle example or trace slice, if targeted retrieval is exercised in the proof path
- one prompt contract note
- one persistence note describing session vs final artifact
- one runtime-budget note describing iteration/tool limits and resume-vs-restart policy

Proof gate:

- the sample trace shows at least one revise cycle
- the sample proof explicitly shows that a narrative brief / reading strategy layer was produced before final artifact drafting, rather than being left implicit
- the sample trace can be resumed or finalized without ambiguity
- the sample trace demonstrates that iteration/tool budgets are enforced and that resume is preferred over restart when possible
- the sample trace demonstrates the hard context-carry/runtime-cost rule is enforced: revise turns consume compact delta/session-state context, while full dossier replay appears only in bootstrap, cold-resume, or corruption-recovery paths

### Phase 3: `page_artifact_v2`

Goal:

- define a new final-page artifact that supports freeform guided reading
- interpret the landed helper/contract work as the beginning of a renderer-decoupled content artifact, not as a commitment to fixed cards or a single lecture-style page

Must support:

- authored narrative paragraphs and explanations
- inline original excerpts
- figure/table/equation/media slots
- continuity-aware explanation informed by neighboring-page context
- term annotations
- external resources
- optional side/aside content when the chosen presentation strategy needs it

Main-spine rule:

- the current-page body/evidence flow must survive as the primary reading spine of the final artifact
- `current_page` is the only primary narrative anchor and evidence spine
- `adjacent_pages` are full structured continuity-context inputs, not co-equal narrative anchors
- the agent must synthesize current-page grounding and neighboring-page context into one coherent, seamless current-page narrative
- neighboring-page context should reduce discontinuity, fill semantic gaps, improve figure/table understanding, and smooth narrative flow
- neighboring-page context is a latent continuity substrate for generation rather than a default reader-visible artifact layer
- authored explanation, continuity-aware neighboring-page context, term notes, and external resources are layered onto the current-page spine rather than replacing it
- v2 is not allowed to regress into a summary page, excerpt shelf, or detached explainer page where the current-page body flow disappears from the main reading path
- preserving the spine does not require raw one-to-one reproduction of every fragment, but the near-complete argumentative flow of the current page must remain legible in the final reading path
- The primary-reading-spine rule constrains content anchoring, not visual form. V2 may use diverse presentation strategies, layout recipes, approved template families, and motion presets as long as the current-page reading path remains primary and reader-legible.
- Inline enrichment means explanation, annotation, and external resources are anchored into the main reading journey by default, but they may appear through varied design patterns rather than a single lecture-style composition.

Contract rules:

- freeform authoring is represented inside a structured AST-like artifact, not arbitrary raw page output
- artifact validation must run before cache persistence
- artifact must support replay and deterministic render
- v2 may use approved template/layout families and approved JS interaction/motion primitives, but remains schema-bound and approved-template-bound rather than arbitrary frontend code generation
- AI-authored content should not require arbitrary raw page code generation to place images, tables, or equations; structured slots, bindings, and media references are the preferred mechanism
- exact media realization may vary by renderer/template/layout strategy without changing the underlying artifact direction
- external-resource nodes should preferentially bind to normalized retrieval outputs / resource bundles instead of free-form invented resource text
- unsupported requested node kinds or unresolved required media/resource bindings should fail explicitly rather than being silently dropped or broadly downgraded

Presentation flexibility clarification:

- card-based, editorial, scrollytelling, and mixed-layout presentation are all renderer-layer choices
- card-like presentation remains a valid and desirable visual strategy when it serves the page
- content modeling should not collapse into card-only generation thinking
- preserving this visual flexibility does not require changing the current presentation-contract fields
- reuse of mature approved web component libraries/design systems is primarily a later renderer-implementation concern, not a Phase 1-3 content-contract blocker

Reserved presentation-layer contract fields (must exist now even if not fully implemented yet):

- `template_id`
- `layout_recipe`
- `presentation_mode`
- `widget_family`
- `motion_preset`
- `interaction_policy`

Why reserve now:

- lock a stable artifact contract before renderer diversification so cache/replay and compatibility remain deterministic
- make presentation decisions inspectable in `/workbench-v2` without coupling Phase 3 to full UI feature completion
- prevent fallback to a single lecture-style default by requiring explicit, versioned presentation intent in artifacts

Required proof artifacts:

- artifact schema note
- one final artifact snapshot
- one validator note

Proof gate:

- the artifact clearly interleaves original text and authored explanation
- the final artifact demonstrates that the current-page body/evidence spine remains the dominant reading path
- artifact validation and renderability are both demonstrated

### Phase 4: `/experience-v2` And `/workbench-v2`

Goal:

- render the new final artifact for readers
- expose the session and provenance in workbench

Renderer implementation preference:

- Phase 4 renderer work should prefer mature approved web component libraries/design systems for general UI primitives and interaction patterns rather than rebuilding broad UI foundations from scratch
- bespoke components should be reserved for paper-specific reading surfaces, artifact-specific layouts, or interactions that cannot be cleanly expressed through the approved component-library layer
- this preference does not weaken the existing artifact/presentation contracts; it guides how `/experience-v2` and `/workbench-v2` are implemented once renderer work begins

Required proof artifacts:

- one `/experience-v2` sample route output
- one `/workbench-v2` sample session view
- acceptance screenshots
- rollout/flag note
- rollback note
- one renderer implementation note describing the approved component-library/design-system reuse strategy, plus any justified bespoke reader/workbench components
- one Docker-based verification note covering the authoritative Phase 4 backend/frontend build and runtime-check path

Proof gate:

- `/experience-v2` only shows completed final artifact
- `/experience-v2` should read as one continuous narrative line for the current page rather than a visibly patched sequence of page-boundary repairs
- neighboring-page continuity should usually appear as naturally integrated explanation, not as explicit `previous page` / `next page` callouts or a separate continuity layer
- `/workbench-v2` can explain dossier, iterations, and final page assembly
- `/workbench-v2` may expose neighboring-page provenance, continuity inputs, and how they affected generation; `/experience-v2` should not surface that provenance by default unless explicitly needed
- `/workbench-v2` should expose inspection visibility for presentation choice rationale (for example template/layout/presentation mode/motion strategy selection) when available; full orchestration and richer decision tooling remain future direction, not a Phase 4 hard commitment
- v1 and v2 can coexist without shared cache pollution
- delivery must prove presentation-contract use, not silent lecture-default collapse: `/experience-v2` output must either show a concrete non-default presentation choice or, at minimum, show populated and exercised presentation-layer fields (`template_id`, `layout_recipe`, `presentation_mode`, `widget_family`, `motion_preset`, `interaction_policy`) rather than empty placeholders
- Phase 4 acceptance must use the containerized project environment as the authoritative verification path for both backend and frontend build/runtime checks
- frontend production-build acceptance for Phase 4 must be validated with `docker compose build frontend`, not only host-side `npm run build`
- route/runtime smoke validation for `/experience-v2` and `/workbench-v2` should run against the compose-managed stack once the containers are up

## Phase 1 Scope

Phase 1 is intentionally narrow.

In scope:

- define `reading_dossier_v2`
- map v1 source fields into the new dossier
- produce one fixed control-sample artifact
- define adjacent-context ownership and parity rules

Out of scope:

- new page renderer
- new agent loop
- new resource strategy
- new reader UI
- changing `/read` extraction ownership

## Likely Files For Phase 1

- `backend/app/api/literature.py`
- `backend/app/services/generative_reader_agent_runtime.py`
- `backend/app/schemas/literature.py`
- `backend/tests/test_literature_reader_api.py`
- `backend/tests/test_generative_reader_agent_runtime.py`
- `docs/plan/`

## Phase 1 Acceptance Bundle

Before Phase 1 is marked complete, the repo must contain:

- a `reading_dossier_v2` schema description
- a stable control-sample dossier snapshot for page 78/7
- tests proving the dossier contains:
  - current page full grounding
  - current page image/figure references
  - adjacent pages as structured context
- tests proving cache/version metadata exists
- one note explaining adjacent-page context ownership and fidelity limits
- an execution note describing what changed

Phase 1 should also name, but does not yet need to fully implement, the coverage-set pages used in later phase gates.

## Working Rule

No phase advances on taste alone.

Each phase must leave:

- code
- tests
- one sample artifact
- one short execution log

Only then does the next phase begin.
