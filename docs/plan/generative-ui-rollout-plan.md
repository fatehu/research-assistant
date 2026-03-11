# Generative UI Rollout Plan

Last updated: 2026-03-11
Status: in progress
Owner: Codex + repository maintainers

## Goal

Build a production-grade generative UI system for literature reading.

The target architecture is:

`compose payload -> generative plan -> experience runtime`

The system should stay grounded in deterministic reader extraction, and use generation only for controlled augmentation.

## Product Boundary

- [x] Keep `/literature/:id/read` as the stable reader for PDF/AI reading, evidence checking, annotations, and comments.
- [x] Treat `/literature/:paperId/experience` as the target product page for expanded generative UI.
- [x] Treat `/literature/:paperId/read/workbench` as debug/workbench only, not the end-user product surface.
- [x] Keep the core scope at the system level: compose grounding -> story understanding -> tool/resource reasoning -> experience rendering.
- [x] Preserve `page` and `kb` as valid reading context inputs when they come from real navigation state.
- [x] Forbid unsafe demo defaults such as hardcoded `kb=84`; the issue was the hardcoded default, not the parameter itself.

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
- [ ] Eliminate duplicated rendering logic across workbench and experience pages.
- [ ] Create a block registry so the model can only select known block types.
- [ ] Move Figure, Glossary, Question, Resource, and future blocks into the registry.
- [ ] Support block-level loading, partial, empty, and error states.
- [ ] Support incremental block patching instead of full-page refresh.
- [ ] Add runtime telemetry for render failures, durations, and engagement.
- [x] Turn workbench into a debug/inspection surface instead of a second product path.

## Phase 4: Narrow Agent Responsibilities

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
- [ ] Cover at least:
  figure-heavy, methods-heavy, concept-heavy pages.
- [ ] Label each golden page with:
  main claim, key evidence, required terms, required background, trusted external links.
- [ ] Evaluate:
  factuality, anchor correctness, link quality, interaction usefulness, latency, fallback rate.
- [ ] Add snapshot tests for generative plans and experience plans.
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
