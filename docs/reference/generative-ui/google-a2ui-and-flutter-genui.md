# Google A2UI and Flutter GenUI

## Sources

- Google Developers Blog: [Introducing A2UI: An open project for agent-driven interfaces](https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/)
- Flutter docs: [GenUI SDK for Flutter](https://docs.flutter.dev/ai/genui)

## Why this matters

Google's direction is not "let the model generate arbitrary frontend code".

The stronger pattern is:

- host app keeps control of rendering
- agent emits structured UI intent
- neighboring agents and external systems can still participate
- the user-facing application remains stateful, styled, and safe

## Key takeaways

### 1. A2UI is native-first

The host client renders a blueprint of native components, instead of loading an opaque web app blob by default.

Implication for this repo:

- `/experience` should stay renderer-driven
- agent output should remain schema-bound
- arbitrary frontend code generation is still the wrong production path

### 2. A2UI is about agent-driven interfaces, not just rich chat bubbles

The model should be able to influence:

- page structure
- what modules appear
- ordering
- transitions between reading, explanation, and interaction

Implication for this repo:

- current `page_dossier -> generative plan -> experience runtime` direction is correct
- but `/experience` still needs a more explicit staged runtime

### 3. Flutter GenUI treats generative UI as orchestration

The Flutter docs explicitly frame GenUI as an orchestration layer coordinating:

- user input
- widgets
- AI agent
- state changes back into the agent loop

Implication for this repo:

- `/experience` should not stop at a one-shot page plan
- it should evolve toward evented, stateful interaction
- `/workbench` must expose enough state to debug those loops

### 4. The host app should own styling and accessibility

This is one of the strongest reasons to keep a controlled renderer.

Implication for this repo:

- block registry is still the right constraint
- richer freedom should come from planning and composition, not arbitrary code execution

## What to copy into this repo

- native component blueprint mindset
- structured UI payloads over freeform code
- strong host app control
- evented / stateful runtime design

## What not to copy blindly

- mobile-specific component assumptions
- Flutter-specific runtime mechanics
- any assumption that the model should directly own the final render tree without validation

## Repo guidance

For `/experience`, this source supports:

- stronger `page_dossier`
- staged runtime
- richer interaction blocks
- tool trace visibility

It does not justify:

- collapsing back into `/read`-style evidence-first rendering
- letting the model emit arbitrary frontend code

