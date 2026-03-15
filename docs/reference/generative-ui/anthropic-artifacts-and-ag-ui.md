# Anthropic Artifacts and AG-UI

## Sources

- Anthropic: [Artifacts are now generally available](https://claude.com/blog/artifacts)
- AG-UI docs: [AG-UI Overview](https://docs.ag-ui.com/introduction)

## Why this matters

These two references are useful together:

- Artifacts shows a strong product pattern for AI-generated workspaces
- AG-UI shows a strong runtime/protocol pattern for user-facing agent systems

## Key takeaways from Artifacts

### 1. The output should feel like a durable workspace

Artifacts are valuable because they:

- stand on their own
- can be iterated on
- are not trapped inside chat text

Implication for this repo:

- `/experience` should feel like a durable generated page, not a chat transcript expanded into cards
- `/workbench` should inspect how that artifact was produced

### 2. Rich outputs should still be editable and inspectable

Implication for this repo:

- generated page structure should be inspectable
- module/resource choices should be visible
- continuity context and tool use should not be hidden

## Key takeaways from AG-UI

### 1. Agentic apps need an event protocol

AG-UI treats agent↔user interaction as:

- long-running
- multimodal
- event-based
- stateful

Implication for this repo:

- `/experience` should evolve beyond request/response
- `/workbench` should surface events, tool trace, and state transitions

### 2. Generative UI can be static or declarative

AG-UI explicitly distinguishes:

- stable typed components
- more declarative trees under app validation

Implication for this repo:

- block registry is still correct
- but we can allow richer declarative composition over time

### 3. Shared state and interrupts matter

Implication for this repo:

- future `/experience` interactions should be event-bus driven
- current action bus is a start, not the end state

## What to copy into this repo

- artifact mindset for `/experience`
- explicit observability
- evented runtime thinking
- generated UI as a product surface, not a debug trick

## What not to copy blindly

- chat-product assumptions from Claude itself
- protocol complexity before we actually need it

## Repo guidance

These references justify:

- durable generated pages
- strong `/workbench` visibility
- future event-based runtime upgrades

They do not justify:

- hiding runtime state
- collapsing everything into opaque agent behavior

