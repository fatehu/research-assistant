# Repo Guidance: How These References Apply Here

## Current repo direction

The repository already has the beginnings of the right stack:

- deterministic reader grounding
- generative plan
- experience runtime
- shared renderer
- workbench inspection surface

But it is still between two phases:

- phase 1: safe schema-bound augmentation
- phase 2: true agentic page generation

## What `/experience` should become

`/experience` should be:

- dossier-driven
- agent-planned
- tool-enriched
- rich in narrative and modules
- still rendered by a controlled host runtime

Concretely, the target chain should be:

1. page dossier assembly
2. planner
3. tool/enricher
4. page generation
5. renderer
6. workbench inspection

## What `/workbench` should become

`/workbench` should not be a second product page.

It should expose:

- page dossier
- adjacent-page continuity context
- tool trace
- resource strategy
- final generative plan
- final experience plan
- fallback reason if anything degraded

## What `/read` should not do

`/read` should not absorb this responsibility.

`/read` should remain:

- evidence-oriented
- provenance-preserving
- stable
- deterministic-first

## Anti-drift checklist

Before changing `/experience`, check:

- Are we improving `page dossier` quality, or just adding more prompt text?
- Are neighboring pages materially shaping the page, or only shown as debug metadata?
- Are tools and MCP being used because the page needs them, or just because they are available?
- Is `/workbench` getting more observable, or more opaque?
- Are we increasing agent freedom inside a validated renderer, or drifting toward arbitrary code generation?
- Are we keeping `/read` and `/experience` product boundaries separate?

## Immediate engineering priorities

### P0

- staged runtime: `planner -> tool/enricher -> page generation`
- `page_dossier` as the actual planning center, not prompt garnish
- structured previous/next-page VL context as first-class input
- stronger `/workbench` inspection of runtime internals
- richer `/experience` page modules and layouts

### P1

- better eval coverage
- better runtime telemetry
- safer rollout and contract versioning

### P2

- more advanced interaction loops
- finer-grained incremental patching
- stronger event protocol across runtime and UI
