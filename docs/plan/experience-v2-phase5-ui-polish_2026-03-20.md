# Experience V2 Phase 5 UI Polish Plan

## Status

`/experience-v2` and `/workbench-v2` are already live at the route/runtime layer.
The remaining problem is no longer the v2 data chain. The remaining problem is the
reader-facing shell and visual system.

This document defines a small follow-up phase focused on UI polish and acceptance.
It is not a new architecture phase and it must not reopen the Phase 1-4 contracts.

## Why This Phase Exists

Current `/experience-v2` issues are primarily frontend-shell issues:

- page scroll semantics are wrong:
  - global `body` scrolling is locked
  - old `/experience` created its own page scroll container
  - new `/experience-v2` did not recreate that behavior
- the visual system is mismatched:
  - global dark admin shell and dark body background are still dominant
  - the reader surface is a handwritten light overlay on top of that shell
  - the result looks like an admin app with a reading page pasted inside it
- component reuse is too shallow:
  - only generic primitives are reused
  - the actual reading shell, support rail, excerpt/media rhythm, and page pacing
    are still largely custom

## Goal

Make `/experience-v2` feel like a deliberate reading product rather than a debug-capable
contract demo.

The target outcome is:

- stable internal page scrolling
- a coherent reader-first shell
- cleaner, calmer visual hierarchy
- broader reuse of mature open-source component/library patterns
- custom UI limited to paper-specific reading surfaces that a mature library layer
  cannot express cleanly

## Non-Goals

This phase must not:

- redesign Phase 1-4 runtime contracts
- reopen `/read` ownership or v2 artifact contracts
- introduce arbitrary frontend code generation
- replace all existing frontend foundations at once
- turn into a broad design-system migration across the whole product

## Product Principles

### 1. Reader Surface First

`/experience-v2` should look like a reading surface, not an admin dashboard panel.

The reader page should have:

- its own scroll container
- its own shell/background rhythm
- its own content spacing and pacing
- a clean separation between main reading line and secondary support content

### 2. Reuse Mature UI Building Blocks

Default to open-source, production-grade component primitives and layout patterns.
Do not hand-build generic shells if an approved library already provides them well.

Preferred implementation rule:

- reuse existing Ant Design primitives first because they are already in the repo
- reuse mature open-source layout/content patterns where needed
- only write bespoke components for paper-specific needs such as:
  - excerpt rendering
  - figure/table/equation walkthrough surfaces
  - reading-step emphasis
  - artifact-specific media binding views

### 3. One Visual System Per Surface

Do not mix:

- dark admin shell defaults
- light reader overlays
- unrelated token sets

The reader shell should have a clear, self-consistent visual system.

### 4. Renderer Contract Stays Intact

This phase improves realization, not the artifact contract.

`template_id`, `layout_recipe`, `presentation_mode`, `widget_family`,
`motion_preset`, and `interaction_policy` should drive renderer behavior more
strongly, but the artifact contract remains schema-bound and renderer-decoupled.

## Implementation Plan

## Step 1: Fix Scroll Semantics First

Make `/experience-v2` establish its own page-level scroll container, equivalent in
stability to the old `/experience` page.

Required changes:

- preserve the global app behavior if needed for the rest of the product
- but give `/experience-v2` an explicit internal scroll surface
- ensure the main reading page can scroll independently even when `body` remains locked
- verify long artifacts, media-heavy artifacts, and mixed-layout artifacts all scroll correctly

Acceptance:

- long pages can be scrolled from top to bottom
- sticky or side support regions do not break scroll
- no hidden-overflow trap on the reader surface

## Step 2: Establish a Real Reader Shell

Replace the current “dark admin background + light handwritten overlay” look with a
reader-specific shell.

Required changes:

- define a reader-surface token set for background, text, borders, panels, and support surfaces
- stop relying on the admin-global dark background as the dominant visual base for `/experience-v2`
- make the reader shell coherent in both full-width and mixed-layout modes
- align hero, masthead, reading column, and support rail under one visual language

Acceptance:

- `/experience-v2` looks intentional as a standalone reading product
- the background and card surfaces belong to the same visual system
- the page no longer feels like an admin panel with a reading overlay

## Step 3: Increase Open-Source Component Reuse

Broaden library-backed implementation for generic page structure instead of continuing
to handcraft the full shell.

Preferred approach:

- keep Ant Design for generic primitives already used across the repo
- prefer mature, already-proven open-source layout/content patterns for:
  - page shell
  - side rail
  - responsive column behavior
  - collapsible support regions
  - media framing
  - section-level grouping
- avoid introducing multiple competing design systems at once

Implementation rule:

- generic structure should be library-backed
- artifact-specific reading blocks may remain custom

Examples of what should move away from bespoke shell code first:

- outer page shell
- support rail layout
- section grouping surfaces
- callout/aside framing
- generic media card framing

Examples of what may stay custom:

- original excerpt presentation
- guided-reading step surfaces
- paper-specific figure/table/equation explanation blocks
- artifact-binding-aware media blocks

## Step 4: Make Presentation Contract Visibly Matter

The reader renderer should visibly differentiate templates and layout strategies.

Required changes:

- `template_id` should select meaningfully different page treatment, not just badges
- `layout_recipe` should materially change column/rail structure
- `presentation_mode` should affect pacing and reading-line composition
- `motion_preset` should apply restrained, useful motion only where it helps reading
- `widget_family` and `interaction_policy` should influence support-surface realization

Acceptance:

- `mixed_layout`, `editorial`, and guided modes should feel visually distinct
- the renderer should not collapse all content into one mostly identical surface

## Step 5: Tighten Entry and Acceptance

UI polish should land together with product-surface correctness.

Required changes:

- make sure the reader-facing entry points prefer `/experience-v2` rather than leaving
  the main paper UI pointed only at old `/experience`
- preserve explicit failure states instead of masking broken UI
- perform acceptance using the compose-managed stack

Acceptance:

- direct entry to `/experience-v2` is easy to reach during review
- the page is readable, scrollable, and visually coherent in live Docker runtime
- `/workbench-v2` remains inspection-first and does not need the same visual treatment

## Verification

Use Docker as the authoritative runtime for this phase.

Required verification:

- `docker compose build frontend`
- `docker compose build backend`
- compose-managed smoke check for `/literature/:paperId/experience-v2`
- at least one long-page manual scroll check
- at least one mixed-layout artifact check
- before/after screenshots for the reader shell

Recommended supporting checks:

- `cd frontend && npm run lint`
- `cd frontend && npx tsc --noEmit`

## Deliverables

- improved `/experience-v2` reader shell
- corrected internal scroll behavior
- stronger presentation-contract realization
- documented component-library/design-system reuse strategy
- screenshots showing final accepted reader UI

## Completion Standard

This phase is complete when:

- `/experience-v2` no longer feels visually like an admin shell overlay
- long artifacts scroll correctly
- generic page structure is mainly library-backed rather than fully handwritten
- custom code is reserved for paper-specific reading blocks
- live Docker runtime smoke acceptance passes

## Follow-Up After This Phase

Non-blocking future work may still include:

- deeper page-scoped artifact reuse optimization
- richer future presentation families
- more refined motion tuning
- further reduction of bespoke renderer CSS over time

Those are follow-ups, not blockers for completing this UI polish phase.
