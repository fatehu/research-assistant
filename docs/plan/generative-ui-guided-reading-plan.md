# Generative UI Guided Reading Plan

Last updated: 2026-03-16
Status: active
Scope: `/literature/:paperId/experience` and `/literature/:paperId/read/workbench`

## Goal

Turn `/experience` into a textbook-style guided reading webpage.

The page should:

- preserve near-complete current-page content as the main reading spine
- use adjacent-page `VL-flash` context to improve continuity and explanation
- use tools and MCP aggressively to add high-value background, terminology help, figure/table interpretation, and why-it-matters framing
- guide the reader through the page instead of merely reordering extracted prose

Tools and MCP are not the goal. They are justified only when they make the page more readable, more explanatory, or more complete for the end reader.
If a beat triggers more tools but still reads like reordered prose with empty support cards, that is a failure, not enrichment.

Any increase in tool or MCP activity that does not produce visible reader-facing explanation, clearer transitions, or stronger resource guidance should be treated as regressions in experience quality.

`/workbench` should expose the full reasoning chain behind that page:

- what the runtime saw
- what the planner decided
- what tools were used
- what enrichment was injected
- why each guided segment exists

## Product Principles

1. Completeness first
- current-page content is the required backbone
- the system may trim noise, but not silently collapse major body segments

2. Comprehension first
- explanation should be inserted where the reader needs it
- the page should actively teach, not just summarize

3. Richness second
- tools, MCP, adjacent-page context, and external resources should deepen understanding
- enrichment is a feature, not a side note
- more tools is not automatically better; reader-facing value is the success metric

4. Evidence stays inspectable
- `/experience` can optimize for readability
- `/workbench` must preserve inspectability and runtime transparency

## Current Audited State

### Already in place

- staged runtime exists:
  `page_dossier -> planning_brief -> planner -> tool/enricher -> page_generation`
- adjacent-page structured `VL-flash` context exists
- tool budgets and tool trace exist
- `/workbench` can already inspect dossier, planner output, tool packet, and runtime stages
- current-page body flow is preserved more faithfully than before
- MCP-backed public-web routes already exist for `web_search` / `web_scrape`

### Still wrong

- main reading experience is still too close to:
  `reading_flow + side panels`
- explanations/resources are still treated as secondary cards instead of inline teaching moments
- planner still thinks mostly in sections instead of guided reading beats
- current block families are not rich enough for a guided reading page
- `knowledge_search` is useful as an internal reader-native retrieval tool, but its runtime stability is still weaker than we want for primary `/experience` enrichment
- public-web / MCP enrichment is present, but still not reliably strong enough to make every guided beat feel richer and more teachable

## Design Correction

The corrected model is:

`page_dossier -> planning_brief -> guided-reading planner -> beat-scoped tool enrichment -> page generation -> guided-reading renderer`

This means:

- the planner should output reading beats, not only section strategy
- tools should enrich specific reading beats, not produce generic side resources
- renderer should interleave prose and explanation, not dump one long prose stream plus add-ons

## Runtime Contract Changes

### 1. Introduce guided beats as the primary page-generation unit

Each beat should include at least:

- `beat_id`
- `target_ids`
- `reader_goal`
- `continuity_note`
- `tool_objectives`
- `block_stack`
- `importance`
- `drop_notes`

`main_sections` can stay for compatibility, but `/experience` should increasingly render from beats.

### 2. Separate backbone content from enrichment

The contract should distinguish:

- `page_core_flow`
  - near-complete current-page prose/figure/table order
- `guided_beats`
  - the teaching/explanation orchestration over that flow
- `tool_enrichment_packet`
  - evidence, context, background, terminology, links, and public references mapped to beats

### 3. Make adjacent-page context mandatory planner input

The planner must explicitly consume:

- previous-page structured text
- previous-page figure/table/equation description
- next-page structured text
- next-page figure/table/equation description
- continuation hints

It must use these for:

- carry-over explanation
- figure/table continuity
- bridge paragraphs
- next-step guidance

## Planner Responsibilities

The planner should no longer merely decide:

- which sections appear
- which focus target is important

It should decide:

- how the page is taught
- where the user will likely get stuck
- which paragraph clusters need explanation
- where neighboring-page continuity matters
- where tools should be invoked
- what narrative order best supports understanding

Expected planner output:

- `page_objective`
- `narrative_strategy`
- `guided_beats`
- `tool_requests`
- `resource_objectives`
- `page_generation_notes`

## Tool / Enricher Responsibilities

Tool usage should be high-agency but beat-scoped.

Tool hierarchy must stay explicit:

- `paper_read`
  - internal reader-native grounding tool
- `knowledge_search`
  - internal knowledge-base tool
  - useful, but currently less stable than desired as a primary enrichment source
- `web_search` / `web_scrape`
  - public-web enrichment tools
  - may route through MCP-backed providers
  - should be treated as first-class enrichment means when they improve reader understanding

MCP is not a product outcome. It is one route by which public-web enrichment can improve `/experience`.
The success criterion is never "more MCP usage"; it is "better explanation, stronger continuity, and richer reader-facing guidance."

Expected tool objectives:

- `term_explain`
- `figure_context`
- `table_takeaway`
- `method_background`
- `why_it_matters`
- `continuation_bridge`
- `external_comparison`

Expected output structure per beat:

- `beat_id`
- `objective`
- `summary`
- `supporting_points`
- `public_links`
- `reader_facing_notes`
- `confidence`

Every beat-scoped tool packet should be consumable by the renderer as reader-facing teaching material:
- a short summary
- a few supporting points
- bridge notes that explain why the resource matters here
- optional authoritative links for deeper reading

Tool budget remains enforced, but the policy should optimize for useful explanation, not minimal card count.

When trade-offs appear, the runtime should prefer:

- a stronger inline explanation over an extra decorative module
- a scraped or summarized authoritative source over a bare link list
- fewer but more useful tool outputs over noisy tool activity
- beat-level enrichment that materially improves comprehension over raw tool traces or metadata dumps

## Renderer Strategy

The renderer should shift from section-first output to beat-first output.

Instead of:

- hero
- focus stage
- large reading flow
- side explainers
- side resources

It should support:

1. short reading introduction
2. a prose segment
3. an inline concept bridge
4. a figure/table walkthrough
5. another prose segment
6. a why-it-matters note
7. a checkpoint
8. optional next-step or deeper resource link

This keeps the reading flow intact while reducing cognitive load.

## New Block Families

The next block expansion should prioritize guided reading blocks:

- `GuideIntroBlock`
- `ConceptBridgeBlock`
- `FigureWalkthroughBlock`
- `TableTakeawayBlock`
- `WhyItMattersBlock`
- `CheckpointBlock`
- `NextStepBlock`

These should become first-class blocks in `experienceBlockRegistry.tsx`.

Legacy resource/explainer/question cards can stay, but they should become secondary support blocks.

## Workbench Requirements

`/workbench` must expose beat-level inspectability:

- beat order
- beat coverage (`target_ids`)
- reader goal
- continuity note
- tool objectives
- tool outputs
- dropped/merged rationale
- resource origin

The user should be able to answer:

- why this paragraph cluster was preserved
- why this explanation was inserted here
- why a tool was called
- why some content was merged or omitted

## Execution Plan

### Phase A: Contract and planner upgrade

Status: in progress

Completed:
- Added `guided_beats` to the experience plan contract and renderer path.
- Added beat metadata fields (`reader_goal`, `continuity_note`, `tool_objectives`, `drop_notes`) to planner/storyboard normalization.
- `/experience` and `/workbench` now inspect guided beats directly.
- planner output now preserves `guided_beats` and attaches `beat_id` to tool requests.
- generative plan cache key has been bumped to isolate the guided-reading runtime from older compact plans.

Remaining:
1. Make planner emit beat-native output instead of only section/storyboard refinements.
2. Expand beat-level tool objectives and tool packet attachment.
3. Keep old section strategy as compatibility metadata.

### Phase B: Beat-scoped enrichment

Status: in progress

Completed:
- tool execution now emits `tool_enrichment_packet.beat_packets`.
- tool findings, requested tools, and public links are grouped by `beat_id`.
- `/workbench` now exposes beat-level enrichment packets and `beat_id`-scoped tool activity.
- `/experience` now renders beat-level enrichment inline inside guided beats instead of keeping tool output only in inspection views.
- tool budget has been shifted toward a higher-agency guided-reading mode.

1. Convert tool objectives from page-global to beat-scoped.
2. Emit structured enrichment packet entries keyed by `beat_id`.
3. Preserve tool budgets and suppression logging.

### Phase C: Renderer refactor

Status: in progress

Completed:
- `GenerativeExperienceRenderer` renders `guided_beats` before legacy section-based sections.
- beat-scoped enrichment is now interleaved into `figure_walkthrough`, `body_segment`, and generic enhancement beats.
- current-page body flow remains the primary reading spine, with enrichment layered above it.

1. Teach `GenerativeExperienceRenderer` to render beats in order.
2. Interleave prose segments and explanation blocks.
3. Keep the current page body flow as the primary spine.

### Phase D: Block expansion

1. Add guided reading block families.
2. Map planner block stacks to registry-backed components.
3. Reduce dependence on side panels for comprehension.

### Phase E: Workbench inspection

1. Add guided-beat panels.
2. Show target coverage and drop rationale.
3. Show tool objective -> tool output -> beat usage chain.

### Phase F: Launch gates

1. Version contracts and cache keys.
2. Add telemetry:
   stage latency, tool counts, suppressions, timeouts, failures
3. Expand golden set and review checklist.
4. Add rollout/kill-switch controls.

## Acceptance Criteria

This plan is complete when:

- `/experience` feels like an AI-guided teaching page, not reordered prose
- major current-page content is preserved with no silent collapse of important body segments
- inline explanations noticeably improve comprehension rather than increasing reading difficulty
- figures/tables are introduced and explained in context
- public resources are present when useful and materially enrich understanding instead of decorating the page
- tools contribute visible reader-facing value inside beats, not just hidden traces or side metadata
- tool/MCP activity yields beat-level summaries, supporting points, or bridges that materially improve understanding
- `/workbench` explains the generation process beat by beat
- `/workbench` can inspect beat coverage, target ids, tool objectives, tool outputs, public links, and drop rationale end to end
- MCP-backed enrichment is used opportunistically, but the page still reads well even when public-web tools are unavailable
- `knowledge_search` remains a supporting internal tool until its stability is strong enough to justify heavier reliance

## Non-goals

- changing `/read` UI or interaction behavior
- turning `/experience` into arbitrary code generation
- optimizing first for compactness
- hiding runtime reasoning completely from `/workbench`
