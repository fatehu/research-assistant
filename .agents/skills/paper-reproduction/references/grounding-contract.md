# Grounding Contract

Use this reference when writing or revising `specs/grounding_report.json`.

`grounding_report.json` is the optional readiness truth artifact. Treat it as a reproduction-readiness checklist when the repo/data/runtime picture is still ambiguous, blocked, or worth preserving explicitly. It is not a mandatory gate in front of the first repo-backed execution.

Its job is to investigate whether the current repo/resources are sufficiently clear and alive to justify continuing, patching, or reporting blockers.

The stage-2 investigation still needs to cover these canonical buckets:

- `repo`
- `entrypoint`
- `dataset`
- `runtime`
- `external_dependencies`

It should also produce a repo-mainpath run decision:

- `summary.run_decision = ready`
- `summary.run_decision = runnable_with_patch`
- `summary.run_decision = blocked`

This run decision is about the currently selected repo main path, not about proving every paper artifact is executable today.

## Investigation Order

When you choose to do grounding, follow this order:

1. Read README.
2. Inspect repo structure.
3. Merge stage-1 links with README/repo-discovered links.
4. Probe the merged checklist lightly.
5. Decide what is reachable, usable, paper-aligned, blocked, or still unknown.
6. Only then judge whether it is worth entering implementation.

Do not deep-dive code or speculate entrypoints before the README / structure / link checklist has been investigated.

## Checklist Semantics

Every important code/data/resource item should be evaluated as a checklist entry.

When a section uses object entries, prefer to include these fields on each item:

- `source`
- `reachable`
- `usable`
- `paper_aligned`
- `reason`

For failed items, also record:

- `failure_type`
- `why_not_usable`
- `replacement_attempted`
- `replacement_result`

These fields explain investigation quality. They do not replace the canonical section-level `status`.

Each section should end in:

- `grounded`
- `absent`
- `blocked`
- only use `unknown` when evidence has not yet been collected

## Canonical Shape

At minimum, keep these top-level objects:

- `repo`
- `entrypoint`
- `dataset`
- `runtime`
- `external_dependencies`
- `summary`

Key section fields:

- `repo`
  - `status`
  - `url`
  - `resolved_ref`
  - `default_branch`
  - `commit_sha`
  - `blockers`
- `entrypoint`
  - `status`
  - `candidates`
  - `selected_candidate`
  - `evidence_files`
  - `blockers`
- `dataset`
  - `status`
  - `sources`
  - `access_mode`
  - `local_presence`
  - `blockers`
  - `blocker_details`
  - `alternative_source_candidates`
- `runtime`
  - `status`
  - `inspection_summary`
  - `candidate_runtimes`
  - `tool_availability`
  - `blockers`
  - `blocker_details`
- `external_dependencies`
  - `status`
  - `urls`
  - `probe_results`
  - `blockers`
  - `blocker_details`
  - `alternative_source_candidates`
- `summary`
  - `blockers`
  - `repo_grounded`
  - `entrypoint_grounded`
  - `dataset_grounded`
  - `runtime_grounded`
  - `external_dependencies_grounded`
  - `run_decision`
  - `overall_status`
  - `next_actions`

Keep the final stage-2 business conclusion explicit through:

- `summary.run_decision`
- `summary.overall_status`
- `summary.next_actions`

This conclusion should answer:

- what is currently blocked
- what is still usable / worth continuing
- whether the next stage is justified

## URL Evidence Rules

`grounded` means the required evidence is actually closed.

For HTML responses, do not classify by status code alone.

- read probe semantics such as `page_title`, `page_kind`, `page_signals`, `suggested_next_action`, and `page_semantics_rationale`
- treat HTML download gates, login walls, quota pages, and branded error pages as blocker evidence first
- only treat a remote file URL as truly grounded when the probe reaches file-like bytes or grounded local presence explicitly covers it
- page reachable does not mean resource usable
- resource usable does not mean paper aligned

- If `dataset.status="grounded"` and the dataset depends on remote URLs, every required official dataset URL must either:
  - appear in `dataset.sources` and have a successful matching `external_dependencies.probe_results`, or
  - be explicitly covered by `dataset.local_presence.available=true`
- If `external_dependencies.status="grounded"`, every URL in `external_dependencies.urls` must have a successful probe result.
- Do not use one successful sample link to claim a whole sibling list is grounded.
- If one required official URL fails probe, keep the affected area `blocked` and write the blocker explicitly.
- `gdrive_confirm_required`, `download_gate`, or a Google Drive virus-scan warning page are not the same as `not_found`.
  - If the gate is recoverable, or the repo's own script already handles confirm/cookies automatically, keep the official link alive/recoverable.
  - Do not mark the whole `dataset` / `external_dependencies` section `blocked` just because the download needs one extra confirm step.
  - Reserve `blocked` for terminal failures such as `not_found`, `access_denied`, `quota_limited`, `http_403`, `http_404`, or similar non-recoverable states.
- When an official URL is `blocked`, do one focused recovery pass for alternative sources. Record any trustworthy fallback candidates in:
  - `dataset.alternative_source_candidates`
  - `external_dependencies.alternative_source_candidates`
- Alternative-source candidates do not erase the official blocker. Keep both:
  - official source status = `blocked`
  - alternative candidates = explored / found / not found
- If no clearly trustworthy alternative is found after one light recovery pass, stop and keep the official failure as a high-risk signal.

## Canonical URL Fields

Keep the URL evidence shape flat:

- `dataset.sources`
  - list of URL strings or source objects carrying a URL such as `url` / `source_url`
- `external_dependencies.urls`
  - list of URL strings
- `external_dependencies.probe_results`
  - list of probe result objects, each with at least:
    - `url`
    - `ok`
  - optional fields such as `status_code`, `content_type`, `detected_kind`, `diagnosis`
  - if a result is a recoverable Google Drive gate, keep `ok=true` and explain recoverability in `diagnosis` / `page_kind` instead of dropping the item or flipping the whole section to `blocked`

Avoid nesting probe results inside individual `urls` items. The canonical report keeps:

- URL list in `external_dependencies.urls`
- probe evidence in `external_dependencies.probe_results`

## Blocker Clarity

Blocked sections should not stop at a vague sentence.

Prefer this split:

- `blockers`
  - short human-readable lines
- `blocker_details`
  - structured objects such as:
    - `code`
    - `target`
    - `target_url`
    - `reason`
    - `diagnosis`
    - `status_code`

Examples:

- `Official dataset source blocked: IMDB (HTTP 403, http_403)`
- `Official external dependency blocked: https://example.com/file.bin (HTTP 404, not_found)`

## Grounding Boundary

During `grounding`:

- read README first and inspect repo structure before broad code search
- prefer `paper_research_probe_repo` and `paper_research_probe_url`
- once repo evidence is present, use `paper_research_assess_repo_mainpath` to identify the most likely runnable main path
- keep a merged reproduction-readiness checklist covering:
  - paper-discovered links
  - README links
  - repo-discovered links
- when official evidence is blocked, use `web_search` / `web_scrape` only for one focused alternative-source recovery pass
- use clone/read/search/inspect only to close a specific missing fact
- do not write `implementation_spec` only when the runnable path is still too ambiguous and you are still in fact-finding mode
- do not write execution scripts as a workaround for missing facts
- if the runnable path is already clear from README/repo/runtime evidence, you may skip straight to `execution_spec` / execution instead of forcing more readiness paperwork
- do not start execution only when the current repo/data/runtime picture is still too ambiguous to identify a runnable path

If evidence stays incomplete, write the blockers and stop in `grounding`.
