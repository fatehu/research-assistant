# Vercel AI SDK Generative UI

## Sources

- Vercel blog: [Introducing AI SDK 3.0 with Generative UI support](https://vercel.com/blog/ai-sdk-3-generative-ui)
- Vercel docs: [AI SDK introduction](https://ai-sdk.dev/docs/introduction)
- Vercel Academy: [Multi-Step & Generative UI](https://vercel.com/academy/ai-sdk/multi-step-and-generative-ui)

## Why this matters

Vercel's generative UI model is one of the clearest production references for:

- component streaming
- tool-aware UI
- rich page responses instead of plain markdown chat

## Key takeaways

### 1. Generative UI is not just text formatting

The core move is:

- tool results and model outputs render as known UI primitives
- richer interfaces are streamed progressively

Implication for this repo:

- `/experience` should keep moving away from "page with a few AI cards"
- it should become a true page generator over known blocks and widgets

### 2. Multi-step matters

Generative UI becomes useful when the runtime can:

- reason
- use tools
- keep state
- update UI as new information arrives

Implication for this repo:

- current single agent-planning phase is not enough
- we should move toward `planner -> tool/enricher -> page generation`

### 3. Tool outputs should become UI, not just logs

The AI SDK framing is strong here: tool results are not only backend metadata, they can materially change the surface.

Implication for this repo:

- `tool_trace` must be visible in `/workbench`
- `/experience` should eventually consume tool outputs more directly, not only through collapsed summaries

### 4. The renderer still owns execution

Even in Vercel's system, the model is not trusted with arbitrary code execution in production.

Implication for this repo:

- block registry remains correct
- stronger generation should mean richer plans, not unsafe codegen

## What to copy into this repo

- multi-step runtime as a first-class design
- UI as the output of tools and planning, not only prose
- progressive rendering mindset
- clear separation between planning and execution

## What not to copy blindly

- React Server Components assumptions
- Next.js-specific ergonomics
- chat-first surface assumptions

## Repo guidance

This source supports:

- making `/experience` the real generative page
- increasing planner freedom
- stronger tool participation
- better staged runtime design

It does not support:

- turning `/read` into the main generative UI product
- hiding runtime decisions from `/workbench`

