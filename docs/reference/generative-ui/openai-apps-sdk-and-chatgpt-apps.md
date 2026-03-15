# OpenAI Apps SDK and ChatGPT Apps

## Sources

- OpenAI: [Introducing apps in ChatGPT and the new Apps SDK](https://openai.com/index/introducing-apps-in-chatgpt/)
- OpenAI Developers: [OpenAI for developers](https://developers.openai.com/)

## Why this matters

OpenAI's Apps work is one of the strongest public references for:

- chat-adjacent but app-shaped AI experiences
- tool + interface co-design
- MCP-based app integration
- product expectations for app-grade AI surfaces

This is directly relevant to `/experience` and `/workbench`, even though our product is page-first rather than chat-first.

## Key takeaways

### 1. Apps combine tool logic and interface design

The important move is not just "ChatGPT can call tools". The Apps SDK explicitly extends MCP so developers can define both:

- app logic
- app interface

Implication for this repo:

- `/experience` should not stop at "tool trace exists"
- tool outputs should materially influence the generated page
- the runtime should treat interface generation as a first-class planning concern

### 2. Conversation and interactive UI are meant to coexist

OpenAI's framing is that apps fit naturally into conversation while also exposing interactive surfaces.

Implication for this repo:

- `/experience` should feel like a durable generated page
- `/workbench` should expose how agent decisions, tools, and UI outputs relate
- we should not collapse the experience into a plain chat transcript with cards

### 3. Distribution-grade systems need standards and review

The Apps work is notable because it treats generative UI as something that must survive:

- review
- policy constraints
- partner integrations
- broad user distribution

Implication for this repo:

- richer generation does not justify unsafe freeform codegen
- renderer validation and runtime observability remain necessary
- rollout, evals, and governance are product requirements, not polish

### 4. MCP is necessary but not sufficient

Apps SDK is built on MCP, but the product value comes from the layer above it:

- interface patterns
- user trust
- app discovery
- coherent interaction design

Implication for this repo:

- "we have MCP" is not a product strategy
- `/experience` still needs dossier-driven planning, tool selection, and rich page composition

## What to copy into this repo

- app-grade thinking for generated interfaces
- treating UI as part of the tool contract
- strong review / rollout posture
- a clean separation between host renderer and agent freedom

## What not to copy blindly

- chat-container assumptions
- consumer app store assumptions
- OpenAI-specific distribution or submission workflows

## Repo guidance

This source strengthens the case that `/experience` should become:

- dossier-driven
- tool-enriched
- renderer-validated
- visibly inspectable in `/workbench`

It does not justify:

- turning `/read` into an app-like generative product
- letting the model emit arbitrary frontend code
