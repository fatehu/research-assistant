# Grounding Contract

Use this reference when writing or revising `specs/grounding_report.json`.

`grounding_report.json` is the stage-2 truth artifact. Its job is to close evidence for:

- `repo`
- `entrypoint`
- `dataset`
- `runtime`
- `external_dependencies`

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
  - `overall_status`
  - `next_actions`

## URL Evidence Rules

`grounded` means the required evidence is actually closed.

- If `dataset.status="grounded"` and the dataset depends on remote URLs, every required official dataset URL must either:
  - appear in `dataset.sources` and have a successful matching `external_dependencies.probe_results`, or
  - be explicitly covered by `dataset.local_presence.available=true`
- If `external_dependencies.status="grounded"`, every URL in `external_dependencies.urls` must have a successful probe result.
- Do not use one successful sample link to claim a whole sibling list is grounded.
- If one required official URL fails probe, keep the affected area `blocked` and write the blocker explicitly.
- When an official URL is `blocked`, do one focused recovery pass for alternative sources. Record any trustworthy fallback candidates in:
  - `dataset.alternative_source_candidates`
  - `external_dependencies.alternative_source_candidates`
- Alternative-source candidates do not erase the official blocker. Keep both:
  - official source status = `blocked`
  - alternative candidates = explored / found / not found

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

- prefer `paper_research_probe_repo` and `paper_research_probe_url`
- when official evidence is blocked, use `web_search` / `web_scrape` only for one focused alternative-source recovery pass
- use clone/read/search/inspect only to close a specific missing fact
- do not write `implementation_spec`
- do not write execution scripts
- do not write `execution_spec`
- do not start execution

If evidence stays incomplete, write the blockers and stop in `grounding`.
