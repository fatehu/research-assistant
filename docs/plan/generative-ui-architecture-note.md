# Generative UI Architecture Note

Last updated: 2026-03-15

## Product split

- `/read`
  - Stable reading surface.
  - Priorities: deterministic extraction, evidence verification, fallback safety.
  - Generation is limited to controlled cleanup and arrangement.
- `/experience`
  - Product surface for rich generative webpages.
  - Priorities: current-page completeness, narrative quality, continuity, resource enrichment, interaction richness.
  - Uses grounding as an anchor, not as the only product constraint.
- `/read/workbench`
  - Debug and inspection surface for the same `/experience` runtime.
  - Must expose inputs, runtime stages, tool traces, and final page plan.

## Primary runtime chain

`compose payload -> page dossier -> planning brief -> planner -> tool/enricher -> page generation -> experience runtime`

## Shared terms

- `compose payload`
  - Stable deterministic page extraction and enrichment inputs from the reader pipeline.
- `page dossier`
  - Rich page input bundle for `/experience`, including current-page compose, adjacent-page VL context, assets, and quality metadata.
- `planning brief`
  - Deterministic planning seed derived from dossier signals. It should frame the narrative, continuity, target emphasis, and likely tool use before the main agent stage.
- `tool_budget`
  - Deterministic runtime guardrail carried by `planning_brief` and planner output. It constrains how many tools may run, how many may be reader-native vs public-web, whether scraping is allowed, timeout ceilings, and duplicate-query suppression.
- `planner`
  - Stage that decides the page strategy:
    hero angle, narrative structure, section presence, interaction opportunities, and resource gaps.
- `tool/enricher`
  - Stage that fills concrete understanding gaps using `paper_read`, `knowledge_search`, `web_search`, `web_scrape`, or `mcp.*`.
- `planner_output`
  - Compact structured output from the planner stage: page objective, section strategy, tool requests, resource objectives, widget focus, and generation notes.
- `tool_enrichment_packet`
  - Compact structured output from the tool/enricher stage: executed tools, summarized findings, public links, and generation notes that the page-generation stage is allowed to use.
- `page generation`
  - Stage that turns the strategy plus enriched resources into a schema-bound webpage plan.
- `experience runtime`
  - Deterministic layer that validates, caches, renders, and observes the final generated plan.
- `story_substrate`
  - Deterministic summary of the page's evidence, claims, terms, and narrative turns used by planning/runtime.
- `page_brief`
  - Mid-level planning object that guides section beats, content budget, focus targets, body-flow target preservation, and reading path.
- `block`
  - Smallest renderer-executable content unit in `/experience`.
- `widget`
  - Specialized interactive block family with tighter template constraints.
- `ui_action`
  - Renderer-executable action emitted by the runtime plan.
- `event`
  - User-to-agent or user-to-runtime interaction payload routed through the shared event bus.

## Constraints

- Renderer executes structured plans only.
- Arbitrary frontend code generation is out of scope.
- Agent freedom should increase page richness, not bypass validation.
- Agent freedom should enrich the current-page reading flow, not compress or replace it.
- MCP routes, internal tools, and public-web retrieval are supporting infrastructure only; they are successful only when they make `/experience` easier to read and understand.
- `/workbench` must always be able to explain what the runtime saw and why it produced a page.
- `/experience` and `/workbench` should expose `page_dossier`, `planning_brief`, `tool_budget`, `planner_output`, `tool_enrichment_packet`, `runtime_stage_trace`, and `tool_trace` without requiring backend log inspection.
