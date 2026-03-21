# Generative UI Rollout Plan

Last updated: 2026-03-15
Status: in progress
Owner: Codex + repository maintainers

## Goal

Build a production-grade generative UI system for literature reading.

The target architecture is:

`compose payload -> page dossier -> planning brief -> planner -> tool/enricher -> page generation -> experience runtime`

The system should keep `/read` grounded in deterministic reader extraction, while letting `/experience` and `/workbench` evolve into schema-bound, agent-driven rich webpage generation surfaces.

For `/experience`, the current-page reading flow is the mandatory backbone. Adjacent-page VL context, tools, MCP, and enrichment modules exist to improve completeness, comprehension, and richness around that body flow; they must not replace or compress it away.

Tools, MCP, and external-resource pipelines are implementation means. The product objective is always a richer, clearer, more teachable `/experience` page. A runtime that calls more tools but does not visibly improve comprehension should be treated as a regression, not progress.

## Current Status Review

What is already true:

- `/read` is now a payload producer, not the target generative UI surface.
- `/experience` and `/workbench` already share a staged runtime with:
  `page_dossier`, `planning_brief`, `planner`, `tool/enricher`, `page_generation`, and runtime-stage inspection.
- adjacent-page `VL-flash` context, tool budgets, and tool traces are already wired in.
- current-page body flow is preserved more faithfully than the earlier compact-artifact runtime.
- MCP-backed public-web routes already exist behind `web_search` / `web_scrape`.

What is still not good enough:

- `/experience` still behaves too much like `reading_flow + side cards`.
- explanation, continuity bridges, figure/table walkthroughs, and tool results are not yet woven into the main reading path strongly enough.
- current block families are too weak for a textbook-style guided reading page.
- the system still risks drifting toward "compact artifact" behavior when the actual product goal is completeness, comprehension, and richness first.
- `knowledge_search` is an internal tool, not MCP, and its runtime stability is still weaker than required for primary `/experience` enrichment.
- MCP/public-web enrichment is still uneven: the system can call it, but the resulting page does not yet consistently become richer and easier to understand.

Practical conclusion:

- The next milestone is not more cards or more summaries.
- The next milestone is guided reading:
  near-complete current-page body flow stays intact, and AI explanation/tool enrichment is interleaved into that body flow.

## Corrected Product Target

`/experience` is a guided reading webpage, not a compact summary artifact.

Priority order:

1. content completeness
2. comprehension and continuity
3. richness from tools, neighboring pages, and external resources
4. compactness and visual polish

The planner may decide how the page is taught, but it may not decide whether the current-page body flow survives.

Detailed execution plan:

- `docs/plan/generative-ui-guided-reading-plan.md`

## Product Boundary

- [x] Keep `/literature/:id/read` as the stable reader for PDF/AI reading, evidence checking, annotations, and comments.
- [x] Narrow `/literature/:id/read` further:
  its target is simplified AI-arranged flowing reading plus cleaned body content and evidence verification, not the long-term home of page-level generative UI product design.
- [x] Treat the embedded AI-arranged view inside `/literature/:id/read` as legacy transitional compose preview debt, not the long-term generative UI product surface.
- [x] Treat `/literature/:paperId/experience` as the target product page for agent-driven rich webpage generation.
- [x] Treat `/literature/:paperId/read/workbench` as debug/workbench only, not the end-user product surface.
- [x] Keep the core scope at the system level: compose grounding -> dossier/planning -> tool/resource reasoning -> page generation -> experience rendering.
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

- [x] Align with mature generative UI patterns from Google/A2A/A2UI, Vercel AI SDK, Anthropic Artifacts, and similar agentic artifact systems at the principle level.
- [x] Maintain a local curated reference shelf under `docs/reference/generative-ui` and use it to constrain `/experience` and `/workbench` architecture decisions before adding new runtime or page-generation behavior.
- [x] Use those references to reinforce three constraints in this repo:
  renderer executes structured plans, agent outputs stay schema-bound, and arbitrary frontend code generation is out of scope.
- [x] Use the reference shelf in a disciplined order:
  repo guidance first, then protocol/runtime comparison, then official framework/runtime references, then academic papers for architectural guardrails.
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
- [ ] `/experience` must preserve near-complete current-page body flow as the main reading spine.
- [ ] Adjacent-page VL context must shape continuity and explanation, not sit as a weak optional note.
- [ ] Tool use should serve explicit reading goals:
  term explanation, figure/table walkthroughs, method background, and why-it-matters bridges.
- [ ] MCP and public-web tools are successful only when they create visible reader-facing value inside the guided reading flow; emitting tool traces or extra links alone is not sufficient.
- [ ] Treat MCP routes, internal tools, and external-resource pipelines strictly as implementation means:
  success is judged by whether `/experience` becomes more complete, clearer, and more teachable for the reader.
- [ ] Treat `knowledge_search` as an internal retrieval tool with currently weaker stability than desired:
  it may support enrichment, but `/experience` must not depend on it as the only path to richness or clarity.
- [ ] Planner, tool, and MCP activity should always be judged by one product question first:
  did this make the page easier to understand, richer, or more teachable for the reader?
- [ ] `/workbench` must explain why each guided-reading segment exists and what it used.

## Phase 0: Freeze Scope

- [x] Confirm the primary goal is agent-decided rich page generation inside a schema-bound renderer, not freeform frontend code generation.
- [ ] Limit the first production scope to the literature reader only.
- [x] Define the single primary chain as `compose payload -> page dossier -> planning brief -> planner -> tool/enricher -> page generation -> experience runtime`.
- [x] Decide which experimental entry points are temporary and which one is the long-term product surface.
- [x] Publish a short architecture note for shared terminology:
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
- [ ] Replace the current `reading_flow + side panels` composition with a guided-reading renderer that can interleave explanation blocks inside the main body flow.
- [ ] Make agent/tool/MCP output materially improve `/experience` itself, not just `/workbench` inspect panels.

## Phase 4: Narrow Agent Responsibilities

- [ ] Add page-archetype gating for `/read` compose previews:
  title / cover / author-heavy pages should not be treated as ordinary evidence-reading pages just because the compose pipeline can legally produce a `done` plan.
- [x] Start a separate `/read` `layout_uid_v1` pipeline skeleton:
  keep the new chain behind an internal pipeline-version gate, use `page_grounding_v1` layout atoms plus current-page render as input, and validate exact-once `uniqueId` grouping before any future default rollout.
- [ ] Keep `story_substrate` deterministic as much as possible.
- [ ] Limit the planner to:
  reading goal selection, block selection, block data population.
- [ ] Derive `page_brief` from deterministic signals first, then let the model fill gaps.
- [x] Inject adjacent-page VL context for continuation-heavy pages:
  previous/next page render -> VL extraction -> structured dossier context -> current page planning.
- [ ] Split external resource generation into:
  retrieval, filtering, summarization, trust scoring.
- [ ] Treat MCP-backed public-web enrichment as a first-class option for guided reading beats, not just a fallback/backfill after reader-native tools.
- [ ] Restrict widget generation to approved templates.
- [x] Add tool budgets:
  iteration count, latency, domain allowlist, duplicate-query suppression.
- [x] Consider a staged runtime:
  planner -> tool/enricher -> page generation.
- [x] Promote the staged runtime skeleton into the real runtime:
  `page dossier -> planning brief -> planner -> tool/enricher -> page generation -> experience runtime`.
- [x] Re-anchor `/experience` on full current-page body flow:
  planner, page brief, and reading-flow materialization now preserve ordered current-page targets as the main reading backbone; budgets only prune enrichment layers.
- [ ] Replace section-first page assembly with guided-reading beats:
  planner and page-generation should describe how the user is led through the page, not just which sections/cards appear.
- [ ] Interleave explanation blocks into the main reading path instead of treating them as sidecar resource cards.
- [ ] Expand the `/experience` block vocabulary toward textbook-style guided reading blocks.
- [ ] Make `/workbench` inspect beat coverage, tool objectives, enrichment outputs, and dropped/merged rationale.
- [ ] Freeze the corrected product intent in a dedicated execution plan under `docs/plan`.
- [ ] Promote adjacent-page VL context from "helpful continuity input" to a first-class planner input that explicitly shapes beat-level transitions, figure/table explanations, and next-step guidance.
- [ ] Promote planner output from section strategy to guided-reading beats:
  each beat should declare target coverage, reader goal, continuity note, tool objectives, and block stack.

## Phase 5: Make Interaction Real

- [ ] Upgrade `QuestionStarterPanel` into an actionable follow-up interaction.
- [ ] Make `GlossaryPanel` expandable and context-aware.
- [ ] Make figure widgets drive evidence highlighting and reader focus changes.
- [ ] Show why each external resource was recommended.
- [ ] Add comparison blocks for figure-vs-body evidence checks.
- [ ] Add guided reading blocks instead of static explanation cards.
- [ ] Add explanation-first block families for `/experience`:
  `GuideIntroBlock`, `ConceptBridgeBlock`, `FigureWalkthroughBlock`, `TableTakeawayBlock`, `WhyItMattersBlock`, `CheckpointBlock`, `NextStepBlock`.
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

## Exit Gates

### Feature-complete gate

- [ ] `/experience` no longer looks like reordered prose with side cards.
- [ ] current-page body flow remains near-complete.
- [ ] `/experience` is judged acceptable first on content completeness, reader comprehension, and resource richness, not on visual compactness.
- [ ] explanations, bridges, and walkthroughs are interleaved into the main reading path.
- [ ] planner output is beat-based rather than only section-based.
- [ ] `/workbench` can inspect beat coverage, target ids, tool objectives, tool outputs, public links, and dropped-content rationale.
- [ ] agent/tool/MCP usage produces visible reader-facing improvements in understanding, not just more metadata or empty support cards.
- [ ] at least one representative page shows meaningful beat-level summaries, supporting points, and resource links woven into the reading flow rather than relegated to empty side modules.

### Launch-ready gate

- [ ] versioned runtime and renderer contracts are finalized.
- [ ] telemetry exists for stage latency, tool counts, suppression, timeout, and failure reasons.
- [ ] golden/eval set covers figure-heavy, methods-heavy, and concept-heavy pages with explicit review criteria.
- [ ] rollout guards exist:
  feature flags, cache/version alignment, rollback path, kill switch.
- [ ] representative `/experience` pages are manually accepted for completeness, comprehension, richness, continuity, and latency.
- [ ] representative `/experience` pages are manually accepted for whether the page content is complete enough, explanations materially help understanding, and resources enrich reading without distracting from it.
- [ ] representative `/experience` pages are manually accepted for whether tools/MCP made the page more teachable, not merely more decorated.
- [ ] `/workbench` is manually accepted for full planner/tool/beat inspectability rather than partial runtime visibility.
- [ ] local embedding/runtime configuration is verified as GPU-first in production paths while remaining CPU-compatible for CI/CD and fallback environments.

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
- [x] Upgrade adjacent-page context into a structured `page_dossier` input for `/experience`:
  previous/next page `VL-flash` output now carries body text plus figure/table/equation descriptions and continuation hints, and `/workbench` surfaces the dossier directly for inspection.
- [x] Surface `page_dossier`, adjacent continuity, and compact `tool_trace` as first-class `/experience` and `/workbench` observability:
  the product/debug surfaces can now show what the agent saw, what tools it used, and what resource strategy/neighbor-page context shaped the webpage.
- [x] Seed a staged runtime skeleton for `/experience`:
  runtime now carries a deterministic `planning_brief` plus `runtime_stage_trace`, and `/workbench` can inspect those stages instead of treating the agent as a single opaque prompt.
- [x] Promote the staged runtime from observability to real execution boundaries:
  runtime now executes explicit `planner`, `tool/enricher`, and `page generation` stages with stage-specific outputs, legacy fallback, and failure handling.
- [x] Surface staged-runtime internals in `/experience` and `/workbench`:
  both surfaces now expose `planner_output` and `tool_enrichment_packet`, so dossier -> planner -> enrichment -> generation can be inspected without reading backend logs.
- [x] Add deterministic tool-budget guardrails to the staged runtime:
  `planning_brief` now carries `tool_budget`, planner prompt explicitly obeys it, tool execution enforces native/public-web caps plus duplicate-query suppression, and `/experience` + `/workbench` expose budget summaries and suppression results.
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
- [x] Stop exposing temporary DocMind page-image URLs through `page_grounding_v1`:
  localize DocMind page images into reader-owned assets, mark the localized source explicitly, and keep future grounding/image consumers on repository-controlled local files and routes.
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
- [x] Revert the global `/read` raster preview experiment and restore the stable preview baseline:
  `layout_uid_v1` no longer takes a separate DocMind raster preview path in `PaperReaderPage.tsx`; hover / pinned evidence are back on the same PDF.js preview chain used before the table reconstruction work, so table fixes no longer change the global anchor preview implementation.
- [x] Restore `/read` table evidence to the pre-reconstruction whole-table path:
  `TablePanel` no longer overrides evidence with row/cell-local hover/click handlers, table bundles again keep their full `source_layout_ids`, and `/read` table preview/jump flows go back to the generic node-level `uniqueId -> blocks[].pos` chain.
- [x] Stop stale table-era compose caches from masking `/read` evidence fixes:
  old `reader_compose_v7/v6` payloads are now treated as stale, and compatible cache reuse is constrained to the same pipeline version so `layout_uid_v1` no longer keeps reviving pre-fix table evidence behavior.
- [x] Restore `layout_uid_v1` anchor scaling to real DocMind page dimensions:
  when `page_grounding_v1.page_image.width/height` are missing, `/read` now resolves the true DocMind page image size instead of inferring page size from the largest content `x/y`; `reader_compose_v9` therefore emits `geometry.page_width/page_height` and `bbox_hint.page_width/page_height` that match the real page image, which removes the shared right/down scaling drift.
- [x] Codify `/read` evidence-chain invariants in repo docs:
  `/read` evidence/highlight changes must now follow explicit rules in `docs/TESTING_GUIDE.md` and `docs/LITERATURE_TEST_GUIDE.md`, including DocMind-only geometry truth, no global preview-path changes for local table/formula fixes, and mandatory prose-heavy plus table-heavy regression pages before merging evidence-chain changes.
- [x] Move `/read` formulas to an image-first display contract:
  `EquationBlock` now carries `render_mode=image_first` plus `transcript`, the body view prefers the original formula crop instead of forcing low-quality OCR into KaTeX, and OCR text is demoted to an optional transcript fallback.
- [x] Add AI-assisted formula normalization on top of image-first `/read` formulas:
  `layout_uid_v1` now preserves formula style hints (`alignment`, `line_height`, `style_id`) inside `page_grounding_v1`, runs a dedicated equation normalization pass with the current page render as visual reference, and materializes optional `normalized_text / normalized_latex / normalization_reason / normalization_confidence` into `EquationBlock` without changing DocMind evidence geometry. `/read` decision logs also expose `layout_uid_v1:equations_normalized=<count>` for the AI context rail.
- [x] Add an AI-assisted logical-row reconstruction pass for `/read` tables:
  keep DocMind `cells[]` and `uniqueId -> blocks[].pos` as geometry truth, use current-page render plus raw physical rows only to group physical rows into logical rows, and require strict exact-once fallback-safe validation before any table plan can override the current deterministic materializer.
  2026-03-13: runtime gap identified. The AI pass was wired into `layout_uid_v1`, but `_panel_plan_to_ui_plan(...)`
  dropped `logical_rows / logical_header_row_count / reconstruction_mode / reconstruction_notes`, so live `TablePanel`
  props still fell back to deterministic rows. Fix paired with `reader_compose_v10`.
  2026-03-13: prompt/payload strengthened for benchmark-table pairing. The AI table pass now explicitly recognizes
  multi-line headers, `value + (±)` row pairs, and blank-first-column continuation rows, and receives row-level hints
  (`blank_first_column`, `numeric_like_count`, `uncertainty_like_count`, `contains_pm`, `looks_like_uncertainty_row`).
  2026-03-13: live image delivery stabilized. `_invoke_single_agent_model(...)` now localizes safe remote page-image URLs to
  local prompt-cache files before calling DashScope, so normal `/read` fresh rebuilds no longer fall back with
  `Failed to download multimodal content`. Fresh `paper 85 / page 7` rebuilds now return `reader_compose_v11` with
  `reconstruction_mode=ai_logical_rows` and `logical_rows=13`.
- [x] Unify `/read` prompt images and grounding page images to the same local render asset:
  `layout_uid_v1` now upgrades `page_grounding_v1.page_image` to the same `/api/v1/literature/reader/page-assets/...`
  render asset used for prompt-time multimodal calls, while still keeping `page_image.width/height` derived from
  DocMind geometry truth. Fresh `paper 85 / page 8` rebuilds now return `reader_compose_v14` with
  `page_grounding_v1.page_image.source=page_render_asset`, and the live DashScope multimodal call succeeds with
  `images=1` instead of falling back because of an expired DocMind URL.
- [x] Add a generic AI-assisted normalize layer for `/read` prose/layout text:
  `page_grounding_v1.layout_atoms` and `reading_nodes` now preserve `normalized_text / normalization_reason /
  normalization_mode / normalization_confidence`; `layout_uid_v1` runs an exact-once text-normalization pass over
  titles, headings, paragraphs, lists, and figure/table captions using the current page render as visual reference;
  grouping and panel materialization now prefer `normalized_text`; and `/read` AI context surfaces `Normalize 变更`
  so display-layer repairs remain reviewable without changing DocMind evidence truth.
- [x] Keep normalize display formatting out of the `/read` evidence chain:
  normalized markers such as `^6` may render as real superscripts in the main reading surface, but evidence anchors,
  quote-text cache repair, and preview geometry remain plain-text and DocMind-grounded.
- [x] Surface hidden normalize output in `/read` AI context:
  `footer / header / doi / metadata` now participate in the controlled normalize pass for review-only use, and `Intentional Omissions` is grouped by omission reason with readable text previews (and `source -> normalized` diffs when available).
- [x] Re-separate AI prompt images from evidence grounding images on `/read`:
  `page_render_asset` remains valid as a prompt-time multimodal image, but `page_grounding_v1.page_image` for
  evidence/highlight must prefer the localized DocMind page image and its true dimensions so `uniqueId -> blocks[].pos`
  does not get re-scaled against the wrong page size.
- [x] Remove the remaining stale URL dependency from `/read` normalize/page-grounding live calls:
  when `page_grounding_v1` already resolves to a local `page_render_asset`, width/height resolution now uses that same
  local asset instead of re-fetching `docmind_page_image_url`, which avoids extra `403 Forbidden` failures on pages
  whose DocMind image URLs have expired.
- [x] Preserve and backfill normalize enrichments through contract repair:
  `_ensure_payload_contract(...)` no longer overwrites `page_grounding_v1` with a clean rebuild that drops
  `normalized_text`; it now merges existing grounding enrichments and can backfill from
  `layout_advice_v3.text_normalizations.normalization_plan`, so cached `reader_compose_v15` payloads such as
  `paper 85 / page 8` still expose the 11 text-normalization repairs on read.
- [x] Preserve cached `page_grounding_v1` on payload-only read paths:
  when cache reads no longer carry enough source graph to rebuild grounding, contract repair now keeps the existing
  `page_image / layout_atoms / evidence_map / reading_nodes` and refreshes `layout_uid_v1` anchors from that preserved
  grounding, so body text normalization and evidence-preview geometry stay in sync.
- [x] Persist cache-read contract repairs back into storage:
  when a stale cached `/read` payload is repaired on read, the repaired payload is now written back into the DB/Redis
  cache layers so subsequent page loads no longer depend on transient on-read anchor repair.
- [x] Backfill the DocMind page-image contract inside the reader structure cache:
  `docmind_structure` now preserves `page_image_width / page_image_height` from `doc_info.pages[*]` and fresh reader
  payload builds eagerly try to localize the per-page DocMind image into `grounding_pages/page_<n>.*`. Old cached pages
  therefore recover the real DocMind page dimensions before compose/evidence runs, instead of falling back to content
  boundary sizes like `1227x1844` or `1296x1844`.
- [ ] Finish `/read` table stabilization:
  current `layout_uid_v1` table materializer handles common row/column tables and markdown-like table text, but still does not reconstruct rowspan/colspan-heavy layouts or full semantic notes.
- [x] Prepare a lower-cost backend rebuild baseline for local `/read` and generative-ui iteration:
  `backend/.dockerignore` now excludes local virtualenvs, uploads, docs, and test artifacts from Docker build context; local `.env/.env.example` default `TORCH_INDEX_URL` to `cu121` for NVIDIA machines; and `.gitattributes` enforces LF for shell scripts so `backend/docker-entrypoint.sh` no longer breaks rebuilt containers with CRLF shebangs.
- [x] Converge backend-side Docker services onto one shared build owner:
  `backend` now owns the only backend image build, while `codelab-runner`, `mcp_web`, and `mcp_literature` reuse `research-assistant-backend:latest` instead of repeating the same Dockerfile build graph.
- [x] Split local-vs-CI torch wheel defaults cleanly:
  local `.env` keeps `cu121` for NVIDIA development, while `.github/docker-compose.ci.yml` now forces `TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu` so nightly and PR smoke workflows do not inherit the local CUDA default.
- [x] Harden backend rebuilds against transient torch download failures and complete the shared-image rollout:
  `backend/Dockerfile` now retries the torch install step, the shared backend image successfully rebuilt with `cu121`, and both `backend` and `codelab-runner` are now running on the same rebuilt image digest instead of stale pre-fix images.
