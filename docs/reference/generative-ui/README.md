# Generative UI Reference Library

Last refreshed: 2026-03-15

## Purpose

This folder is the local reference shelf for `/experience` and `/workbench`.

It is not a dumping ground for random AI UI links. It is a curated set of:

- official protocol and framework references
- product/runtime patterns from strong generative UI implementations
- a small number of papers that help define the field
- repo-specific guidance to prevent architecture drift

The intended use is:

1. Read current page payload, dossier, and neighboring-page context.
2. Check these reference notes before changing runtime shape.
3. Keep `/experience` aligned with agentic page generation, not reader-style deterministic rendering.

## What belongs here

- official docs or official blogs
- papers with concrete architectural or interaction implications
- short local notes that connect those sources back to this repository

## What does not belong here

- low-signal listicles
- generic prompt engineering blogs
- UI inspiration without runtime implications
- copied full articles or full PDFs

## Current index

- [Google A2UI and Flutter GenUI](./google-a2ui-and-flutter-genui.md)
- [OpenAI Apps SDK and ChatGPT Apps](./openai-apps-sdk-and-chatgpt-apps.md)
- [Vercel AI SDK Generative UI](./vercel-ai-sdk-generative-ui.md)
- [Anthropic Artifacts and AG-UI](./anthropic-artifacts-and-ag-ui.md)
- [Protocol and Runtime Comparison](./protocol-and-runtime-comparison.md)
- [Academic Papers](./academic-papers.md)
- [Repo Guidance](./repo-guidance.md)

## Core orientation for this repo

- `/read`
  - stable reader
  - evidence and provenance first
  - deterministic fallback required

- `/experience`
  - generative product surface
  - page dossier first
  - adjacent-page context should materially shape the page
  - agent/tool usage is expected, not accidental

- `/workbench`
  - debug and inspection surface for the same runtime
  - must expose dossier, tool trace, continuity context, and plan internals

## Reference quality bar

Prefer these source classes in this order:

1. Official docs / official blogs
2. Protocol specs / SDK docs
3. Peer-reviewed or preprint papers with clear implementation relevance
4. High-signal examples only if they reveal runtime structure

## Suggested reading order for this repo

If the task is about `/experience` or `/workbench`, read in this order:

1. [Repo Guidance](./repo-guidance.md)
2. [Protocol and Runtime Comparison](./protocol-and-runtime-comparison.md)
3. [Google A2UI and Flutter GenUI](./google-a2ui-and-flutter-genui.md)
4. [OpenAI Apps SDK and ChatGPT Apps](./openai-apps-sdk-and-chatgpt-apps.md)
5. [Vercel AI SDK Generative UI](./vercel-ai-sdk-generative-ui.md)
6. [Anthropic Artifacts and AG-UI](./anthropic-artifacts-and-ag-ui.md)
7. [Academic Papers](./academic-papers.md)

That order matches the architectural questions we actually face:

- what kind of runtime are we building
- what protocol and renderer boundaries are healthy
- what rich-page product patterns are worth copying
- what research helps avoid drift into naive "model writes page" thinking
