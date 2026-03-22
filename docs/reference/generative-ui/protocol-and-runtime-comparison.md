# Protocol and Runtime Comparison

## Why this file exists

The easiest way to drift is to mix together:

- transport/protocol ideas
- runtime/orchestration ideas
- product-surface ideas

These references solve different parts of the stack. This file keeps them separated.

## The useful mental model

There are four layers:

1. `grounding`
2. `runtime`
3. `ui protocol / contract`
4. `product surface`

Our repo should not force one source to do all four jobs.

## Source map

### Google A2UI

Best for:

- updateable agent-generated UI payloads
- cross-platform rendering contracts
- "client owns rendering" discipline

Use for this repo:

- schema-bound generated UI
- host-controlled rendering
- avoiding arbitrary code execution

Do not use as:

- the whole runtime design

### AG-UI

Best for:

- event-based agent↔UI communication
- long-running stateful interaction
- tool/event observability

Use for this repo:

- `/workbench` visibility
- eventual evented runtime
- richer interactive `/experience` flows

Do not use as:

- a page generation strategy by itself

### OpenAI Apps SDK

Best for:

- app-grade UI + tool coupling
- MCP-based app integration
- product discipline for deployable AI surfaces

Use for this repo:

- treating UI as part of tool/app design
- governance and review mindset
- app-grade expectations for `/experience`

Do not use as:

- proof that chat-native UX is enough for this product

### Vercel AI SDK Generative UI

Best for:

- multi-step tool use
- progressive UI updates
- tool outputs turning into renderable UI

Use for this repo:

- `planner -> tool/enricher -> page generation`
- progressive page assembly
- exposing tool contributions in `/workbench`

Do not use as:

- proof that React/Next chat examples are a final architecture

### Anthropic Artifacts

Best for:

- durable artifact mindset
- generated output as a standalone workspace

Use for this repo:

- making `/experience` feel like a generated product page, not a transcript
- treating `/workbench` as artifact inspection

Do not use as:

- a runtime/protocol substitute

## What this means for our architecture

### `/read`

Should keep:

- grounding
- provenance
- deterministic fallback

Should not absorb:

- app-like generative page freedom

### `/experience`

Should emphasize:

- page dossier
- adjacent-page structure
- planner freedom
- tool-enriched narrative generation
- rich modules inside a validated renderer

### `/workbench`

Should expose:

- dossier
- adjacent-page continuity
- tool trace
- resource strategy
- final plan
- degradation reasons

## Practical anti-drift rule

When someone says "make `/experience` more like generative UI", ask which layer they mean:

- grounding
- runtime
- protocol
- product surface

If the answer is unclear, do not implement yet.
