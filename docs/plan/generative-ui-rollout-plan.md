# Generative UI Rollout Plan

Last updated: 2026-03-12
Status: in progress
Owner: Codex + repository maintainers

## Goal

Build a production-grade generative UI system for literature reading.

The target architecture is:

`compose payload -> generative plan -> experience runtime`

The system should stay grounded in deterministic reader extraction, and use generation only for controlled augmentation.

## Product Boundary

- [x] Keep `/literature/:id/read` as the stable reader for PDF/AI reading, evidence checking, annotations, and comments.
- [x] Narrow `/literature/:id/read` further:
  its target is simplified AI-arranged flowing reading plus cleaned body content and evidence verification, not the long-term home of page-level generative UI product design.
- [x] Treat the embedded AI-arranged view inside `/literature/:id/read` as legacy transitional compose preview debt, not the long-term generative UI product surface.
- [x] Treat `/literature/:paperId/experience` as the target product page for expanded generative UI.
- [x] Treat `/literature/:paperId/read/workbench` as debug/workbench only, not the end-user product surface.
- [x] Keep the core scope at the system level: compose grounding -> story understanding -> tool/resource reasoning -> experience rendering.
- [x] Preserve `page` and `kb` as valid reading context inputs when they come from real navigation state.
- [x] Forbid unsafe demo defaults such as hardcoded `kb=84`; the issue was the hardcoded default, not the parameter itself.

## Execution Lanes

- [x] Primary lane:
  continue building generative UI through `/experience` as the product surface, with `/read/workbench` as the debug and inspection lane for the same runtime.
- [x] Legacy lane:
  treat `/read` compose preview issues as inherited reader debt unless they directly block flowing reading, evidence verification, or fallback safety.
- [x] Do not let `/read` legacy compose/highlight problems redefine the main generative-ui plan:
  they should be tracked and fixed as a separate stability stream, not confused with `/experience` product progress.
- [x] Future simplification target for `/read`:
  keep AI-assisted layout and cleaned-text rendering, but simplify it toward HTML-style flowing reading while letting `/experience` absorb page design, storytelling, and generative interaction.

## External Reference Points

- [x] Align with mature generative UI patterns from Google/A2A/A2UI, Vercel AI SDK, and Anthropic Artifacts at the principle level.
- [x] Use those references to reinforce three constraints in this repo:
  renderer executes structured plans, agent outputs stay schema-bound, and arbitrary frontend code generation is out of scope.
- [x] Keep moving toward agent-decided page structure:
  section presence, section order, hero placement, region assignment, and layout variant should come from plan outputs instead of frontend heuristics.
- [x] Keep tool choice autonomous:
  `paper_read`, `knowledge_search`, `web_search`, and `web_scrape` remain optional tools, not a forced sequence.
- [x] Use external references as direction only; continue from the current repository architecture instead of replacing it.

## Rules

- [ ] Do not let the model generate arbitrary frontend code for production paths.
- [ ] Keep a deterministic reader base as the fallback path.
- [ ] All generated outputs must conform to a validated contract.
- [ ] All generated outputs must be cacheable, replayable, and observable.
- [ ] Any failure must degrade to the composed reader without blocking reading.

## Phase 0: Freeze Scope

- [x] Confirm the primary goal is controlled page-level generative augmentation, not freeform page generation.
- [ ] Limit the first production scope to the literature reader only.
- [ ] Define the single primary chain as `compose payload -> generative plan -> experience runtime`.
- [x] Decide which experimental entry points are temporary and which one is the long-term product surface.
- [ ] Publish a short architecture note for shared terminology:
  `story_substrate`, `page_brief`, `block`, `widget`, `ui_action`, `event`.

## Phase 1: Stabilize Current Flow

- [x] Audit current branch changes and identify risks in the generative reader path.
- [x] Reconfirm that `/read` generative compose is a stability target, not the page to polish into the final generative UI product.
- [x] Remove unsafe hardcoded frontend defaults such as `kb=84`.
- [x] Make experience/workbench routes safe with editable URL-backed defaults.
- [x] Refactor the experience page loading state machine to avoid stale-state polling bugs.
- [x] Correct the rollout assumptions so `kb` remains a valid context parameter and `/experience` remains the target product page.
- [x] Make user-visible fallback and deterministic experience copy default to Chinese without changing internal prompts/schema language.
- [x] Normalize cached/fresh plan loading behavior across workbench and experience routes.
- [x] Add explicit empty states for:
  no KB, no PDF, no cached compose payload, no generative plan.
- [x] Add a minimal regression path:
  open page -> seed plan -> background full plan -> cache hit after reload.

## Phase 2: Define Generative UI Contract v2

- [x] Introduce an initial display-copy contract on top of the existing plan:
  keep raw evidence fields intact, and add `display_*` fields for user-visible hero/section/module/widget/claim copy.
- [x] Introduce a first page-level storyboard and content-budget layer inside `page_brief`:
  let the planner decide section beats and cap how many claims/hooks/resources/explainers/widgets the renderer should surface.
- [x] Replace loose module grouping with a unified `blocks` contract.
- [x] Require every block to include:
  `block_id`, `block_type`, `version`, `target_ids`, `priority`, `state`.
- [x] Add:
  `data_requirements`, `fallback_policy`, `user_actions`, `agent_actions`.
- [x] Introduce a minimal `ui_action` protocol for incremental updates.
- [x] Introduce a minimal `event` protocol for user-to-agent interaction.
- [ ] Ship JSON Schema definitions for plan and runtime payloads.
- [x] Validate all generated plans before they reach the renderer.

## Phase 3: Converge Frontend Runtime

- [x] Build a single `GenerativeExperienceRenderer`.
- [x] Eliminate duplicated rendering logic across workbench and experience pages.
- [x] Create a block registry so the model can only select known block types.
- [x] Move Figure, Glossary, Question, Resource, and the current widget families into the registry.
- [x] Support block-level loading, partial, empty, and error states.
- [ ] Reduce dependence on the fixed `section_type` taxonomy:
  treat the current section enum as a transitional compatibility layer, and keep moving toward layout/block-oriented execution instead of column names with AI-filled content.
- [ ] Remove remaining renderer-side section-specific branching where a block/layout contract can express the same intent:
  the goal is not a prettier template shell, but a thinner execution layer over plan outputs.
- [ ] Support incremental block patching instead of full-page refresh.
- [ ] Add runtime telemetry for render failures, durations, and engagement.
- [x] Turn workbench into a debug/inspection surface instead of a second product path.

## Phase 4: Narrow Agent Responsibilities

- [ ] Add page-archetype gating for `/read` compose previews:
  title / cover / author-heavy pages should not be treated as ordinary evidence-reading pages just because the compose pipeline can legally produce a `done` plan.
- [x] Start a separate `/read` `layout_uid_v1` pipeline skeleton:
  keep the new chain behind an internal pipeline-version gate, use `page_grounding_v1` layout atoms plus current-page render as input, and validate exact-once `uniqueId` grouping before any future default rollout.
- [ ] Keep `story_substrate` deterministic as much as possible.
- [ ] Limit the planner to:
  reading goal selection, block selection, block data population.
- [ ] Derive `page_brief` from deterministic signals first, then let the model fill gaps.
- [x] Inject adjacent-page OCR context as reference-only metadata for continuation-heavy pages:
  previous/next page render -> VL extraction -> labeled context -> current page planning.
- [ ] Split external resource generation into:
  retrieval, filtering, summarization, trust scoring.
- [ ] Restrict widget generation to approved templates.
- [ ] Add tool budgets:
  iteration count, latency, domain allowlist, duplicate-query suppression.
- [ ] Consider a staged runtime:
  planner -> enricher -> formatter.

## Phase 5: Make Interaction Real

- [ ] Upgrade `QuestionStarterPanel` into an actionable follow-up interaction.
- [ ] Make `GlossaryPanel` expandable and context-aware.
- [ ] Make figure widgets drive evidence highlighting and reader focus changes.
- [ ] Show why each external resource was recommended.
- [ ] Add comparison blocks for figure-vs-body evidence checks.
- [ ] Add guided reading blocks instead of static explanation cards.
- [ ] Route all interactions through a shared event bus.

## Phase 6: Build Evaluation and Review

- [ ] Create a fixed golden set of literature pages.
- [x] Cover at least:
  figure-heavy, methods-heavy, concept-heavy pages.
- [ ] Label each golden page with:
  main claim, key evidence, required terms, required background, trusted external links.
- [ ] Evaluate:
  factuality, anchor correctness, link quality, interaction usefulness, latency, fallback rate.
- [x] Add snapshot tests for generative plans and experience plans.
- [ ] Track:
  cache hit rate, tool success rate, timeout rate, block engagement.
- [ ] Create a review workflow that feeds bad outputs back into regression cases.

## Phase 7: Productize and Govern

- [ ] Add feature flags for user/paper/page rollout.
- [ ] Version plan and runtime contracts for safe rollback.
- [ ] Ship dashboards for request volume, success rate, timeout rate, and block usage.
- [ ] Cache at multiple layers:
  compose, plan, experience, external summaries.
- [ ] Preserve deterministic reader fallback as the hard safety net.
- [ ] Write maintainer docs for adding blocks, updating contracts, and extending evals.

## Recommended Execution Order

- [ ] Finish Phase 1 first.
- [ ] Start Phase 2 immediately after Phase 1 is stable.
- [ ] Run Phase 3 and Phase 4 in parallel where ownership is clear.
- [ ] Start Phase 5 only after the runtime contract is stable.
- [ ] Treat Phase 6 as continuous work, not a tail task.
- [ ] Use Phase 7 for controlled rollout after technical stability is proven.

## Current Sprint

- [x] Persist the rollout plan under `docs/plan`.
- [x] Remove unsafe KB defaults from the new reader pages.
- [x] Refactor experience page recovery and polling behavior.
- [x] Re-check route-level UX after the first fixes land.
- [x] Re-align the plan with the actual product boundary: `/experience` is the goal page, `/read` stays stable, `/workbench` is debug only.
- [x] Make the `/experience` renderer trust section-level plan outputs more strictly than frontend heuristics.
- [x] Add a reusable `/experience` acceptance checklist under `docs/plan`.
- [x] Bump generative/experience cache versions to flush stale English-heavy plans.
- [x] Add runtime-side generic English reader-copy rewrites so model outputs are still normalized for Chinese-facing UI.
- [x] Add reference-only adjacent-page OCR context into fresh generative/experience planning and cover it with backend regressions.
- [x] Add route-level empty states for `/experience` and `/workbench` when KB/PDF/compose payload/plan are unavailable.
- [x] Add a regression test for the cached seed -> fresh full plan -> cached hit flow on `/experience`.
- [x] Add `display copy vs raw evidence` contract fields to the existing plan/runtime path and make `/experience` prefer `display_*` values.
- [x] Add `page_brief.storyboard` and `page_brief.content_budget`, and make `/experience` follow that beat order instead of surfacing every module by default.
- [x] Make module ownership section-exclusive:
  the same resource / explainer / widget should not be mounted into multiple sections at once.
- [x] Validate generative plans and experience plans at runtime boundaries:
  normalize malformed `story_substrate` / `page_brief`, enforce contract shape, and fallback safely when validation fails.
- [x] Consolidate `/experience` and `/workbench` onto a shared surface loader/state machine:
  unify cached/fresh/seed/background-refresh semantics instead of keeping per-page loading branches.
- [x] Add the first section-level `blocks` contract:
  keep `resource_module_ids / interaction_module_ids / widget_ids` for compatibility, but emit unified `blocks` refs and let `/experience` prefer them.
- [x] Materialize block action semantics:
  runtime now derives `user_actions / agent_actions` per block type so follow-up event/action contracts can build on real plan objects instead of template assumptions.
- [x] Add structured `ui_actions` and `event_bindings` to section blocks:
  block-level interaction semantics are now emitted by runtime contract instead of being inferred from frontend templates.
- [x] Wire the first renderer-side action dispatch path:
  `/experience` now consumes block `ui_actions / event_bindings`, updates focus state, and surfaces the last triggered protocol event instead of treating blocks as static cards.
- [x] Extract the first page-local experience action bus:
  `/experience` no longer keeps action dispatch inline only; focus switching and last-event feedback now live behind a dedicated hook that can be promoted into a shared renderer/event-bus layer.
- [x] Extract `/experience` section/layout/block execution into `GenerativeExperienceRenderer`:
  `PaperReaderExperiencePage` now focuses on route state, loader state, alerts, params, and details while the renderer executes section `blocks`, layout regions, focus rendering, and action feedback.
- [x] Add the first renderer-side block registry:
  `experienceBlockRegistry.tsx` now holds registered resource / interaction / widget definitions so the renderer stops hardcoding every block family inline.
- [x] Converge `/workbench` onto the shared renderer/runtime path:
  `PaperReaderWorkbenchPage.tsx` now reuses the same surface loader, action bus, renderer, and block registry, with debug panels layered on top instead of keeping a second page implementation.
- [x] Add block-level runtime states to the contract and renderer execution path:
  section `blocks` can now surface `ready / loading / partial / empty / error`, and the renderer degrades each block without collapsing the whole section.
- [x] Seed Phase 6 evaluation assets with a hybrid golden set and snapshot guards:
  add one real paper-page golden plus contract fixtures for methods-heavy and concept-heavy coverage, alongside generative/experience snapshot tests and an eval asset guard.
- [x] Separate internal planning copy from user-visible section summaries:
  storyboard `purpose` now stays in section metadata as planner notes, while `/experience` shows user-facing summaries instead of prompt-like planning text.
- [x] Repair malformed cached compose fallback payloads at the API boundary:
  `/read` no longer fails the fallback path just because legacy cached payloads miss `engine_version` or `ui_plan.plan_id`.
- [x] Reclassify `/read` AI compose as a transitional embedded preview:
  attractive sample pages on `/read` do not define the product target; `/experience` remains the page that should absorb future generative UI product investment.
- [x] Split `/read` highlight issues into two tracks:
  `78/page=4` vs `78/page=7` is not a missing-backend-anchor problem, while `85/page=1` is a real `bbox_rebuilt` geometry regression on metadata-heavy pages.
- [x] Start the lightweight `/read` grounding split:
  add `page_grounding_v1` to composed payloads so `uniqueId`-level layout atoms, page-local reading nodes, evidence geometry, and page-image references are preserved as a stable input layer for later `/experience` and for a slimmer `/read`.
- [x] Add the first `layout_uid_v1` `/read` pipeline skeleton:
  when explicitly selected, `/read` can now route away from `simplified_v2`, build a `uniqueId`-only grouping prompt for `qwen3.5-plus`, enforce exact-once `layout_id` assignment, and fall back to deterministic grouping without switching default traffic yet.
- [x] Expose the new `/read` pipeline for direct acceptance through URL state:
  `/read?...&compose=layout_uid_v1` now forwards `pipeline_version` through cached/stream/prefetch paths and surfaces a visible pipeline tag in the page UI.
- [x] Make `layout_uid_v1` the default `/read` compose path:
  `READER_PIPELINE_VERSION`, service defaults, and Docker defaults now all point to `layout_uid_v1`, so `/read` no longer needs an explicit `compose=` query to stay on the `uniqueId`-grouping chain.
- [x] Switch `/read` highlight geometry to prefer `uniqueId -> blocks[].pos`:
  `layout_uid_v1` components now emit layout-based anchors from `page_grounding_v1.evidence_map`, pass `source_atom_ids=source_layout_ids`, and let the frontend preview/highlight flow resolve evidence from `page_grounding_v1` before falling back to block-level page-structure geometry.
- [x] Start deterministic `table / equation` materialization on `/read`:
  `layout_uid_v1` no longer treats tables as empty `rows=[]` shells or formulas as stray prose; table groups now build matrix/header/caption props from `uniqueId` block geometry and markdown-like rows, while equation groups materialize into dedicated `EquationBlock` nodes.
- [x] Preserve DocMind `table.cells[]` in `page_grounding_v1` and restore row-level table evidence:
  `/read` table materialization now prefers cell truth over flattened markdown rows, keeps table captions coalesced with table bundles, and emits `row_evidence` anchors so hovering/clicking a table row can preview and jump to evidence instead of only highlighting the whole table.
- [x] Fix `/read` table evidence and equation rendering regressions:
  row-level table anchors no longer use row-local pseudo page dimensions, `TablePanel` keeps `table_cells` through contract normalization and exposes direct `证据 / 预览` controls again, and `EquationBlock` now renders KaTeX plus the original evidence image while splitting trailing `where ...` prose into description text.
- [x] Seal `/read` table cell truth through API/runtime:
  `ReaderComposePayload` now preserves `page_grounding_v1.layout_atoms[].table_cells` and `page_grounding_v1.evidence_map[].table_cells` through schema serialization, and `reader_compose_v7` invalidates stale table caches so runtime responses carry `cell_evidence` and body-only `source_atom_ids`.
- [x] Restore `layout_uid_v1` evidence on auto-injected no-drop fallback nodes:
  recovered prose nodes now rebuild `layout_uid_v1` anchors from `page_grounding_v1 + layout_unique_id`, keep `source_atom_ids/source_layout_ids`, and no longer lose the `证据` action just because they were injected by the no-drop safety layer.
- [x] Remove the global `/read` evidence gate regression on new pages:
  `PaperReaderPage.tsx` no longer rejects all `layout_uid_v1` anchors at the page level, so hover preview, jump-to-evidence, and pinned evidence work again on newly generated `/read` pages instead of only on legacy `anchor_v2` payloads.
- [x] Align `layout_uid_v1` evidence previews to the correct coordinate system:
  `layout_uid_v1` polygons now render on top of a DocMind page-image proxy route instead of PDF.js viewport pixels, which removes the shared right/down offset caused by mismatched `1483x1920` DocMind image geometry vs `1468.8x1900.8` PDF.js page rendering.
- [x] Restore `/read` table evidence to the pre-reconstruction whole-table path:
  `TablePanel` no longer overrides evidence with row/cell-local hover/click handlers, table bundles again keep their full `source_layout_ids`, and `/read` table preview/jump flows go back to the generic node-level `uniqueId -> blocks[].pos` chain.
- [ ] Finish `/read` table stabilization:
  current `layout_uid_v1` table materializer handles common row/column tables and markdown-like table text, but still does not reconstruct rowspan/colspan-heavy layouts or full semantic notes.
