# Legacy Stage Facilities

This document preserves the older multi-stage paper-reproduction design as an optional reference.

It is **not** the default control flow anymore.

Current default:

1. keep `stage1 / intake_summary`
2. move into the repo-first run loop
3. use the facilities below only when they clearly help

## When To Use Each Facility

### `specs/grounding_report.json`

Use this when the repo-backed path is still unclear, risky, or externally blocked.

Typical triggers:

- the main repo path is ambiguous
- official dataset or weight URLs are failing
- runtime support is unclear enough that a structured blocker report will help
- you need to preserve a readiness judgment for later continuation

Do not create it only because an execution has not started yet.

### `specs/implementation_spec.json`

Use this when the runnable path has become stable enough that you want to preserve:

- the chosen repo entrypoint
- the minimum dependency/runtime picture
- known blockers
- the expected baseline path

Treat it as a stabilization file, not a prerequisite.

### `drafts/run_drafts.json`

Use this only when there are multiple plausible runnable paths or variants worth preserving.

Typical triggers:

- more than one possible baseline path
- multiple low-cost variants for tuning
- a need to compare alternative entrypoints or data flows

If there is only one clear runnable path, skip this file and write `execution_spec` directly.

### Execution Artifacts

Execution artifacts remain useful and should still be archived:

- `executions/<execution_id>/execution_spec.json`
- execution results
- execution logs
- execution-scoped helper scripts

These are run attempts, not planning gates.

### Tuning Analysis

Use a tuning-analysis pass only after a baseline result exists.

The tuning step should normally:

1. read the baseline evidence
2. propose a few grounded options
3. wait for explicit user confirmation before starting the next run

## Mapping From Old Stage Names

The runtime/backend may still refer to these old labels:

- `planning`
- `grounding`
- `implementation_prep`
- `run_drafts`
- `execution`
- `tuning`

Interpret them as labels for available facilities or archived state, not as mandatory gates.

## Recommended Decision Rule

When in doubt, ask one question:

**Does creating this artifact make the next repo action clearer?**

If yes, create it.
If no, keep running the repo-first loop instead.
