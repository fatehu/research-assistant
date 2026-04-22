---
name: paper-reproduction
description: Turn archived machine-learning or deep-learning papers into a gated workflow that first extracts structured facts and a reusable paper summary, then grounds repo/data/runtime evidence, defines a concrete reproduction path, surfaces blockers, and executes only when qualification and preflight conditions pass. Use when the user asks to understand or explain a saved paper, gather supporting material, prepare or validate a reproduction plan, qualify run drafts, run or debug grounded experiments, tune parameters/models, compare results, design verification steps, or continue a saved paper Project.
---

# Paper Reproduction

Use this skill as the control-plane workflow for a saved paper Project.

The goal is not to push execution at any cost. First read the saved Project state, produce reusable paper understanding artifacts, and ground repo/data/runtime facts. Then make the current reproduction path explicit, qualify which drafts are actually runnable, and enter execution only when preflight says the run is ready to start. When the required conditions are not satisfied, stop clearly, report the blocker, and leave behind artifacts that still support explanation, source gathering, tuning, and validation design.

Think in seven workflow stages even if the user does not name them:

1. `intake_summary`
2. `grounding`
3. `implementation_plan`
4. `run_qualification`
5. `execution_preflight`
6. `execution_run`
7. `analysis_tuning_report`

Current backend/runtime stage ids are not fully renamed yet.

- Runtime stage `planning` currently maps to the new stage-1 semantics `intake_summary`.
- Until the runtime ids are unified, read `planning` as “stage 1 / intake_summary”, not as a generic later-phase planning bucket.

The user should not need to know internal phases. Always begin from the saved Project state, then continue from the first missing, blocked, or requested step.

During `intake_summary`, preserve three planning artifacts together:

- `planning/paper_intake_result.json`
- `planning/paper_summary.json`
- `planning/experiment_spec.json`

In `intake_summary`, the paper is a background and clue source, not the final execution truth.

- Focus on paper understanding, link classification, and tuning hints.
- Prefer narrative evidence from正文/method/experiment prose/figure captions.
- Treat table cells as reference evidence only.
- Do not let stage 1 decide the final repo main path or executable run scope.

## Truth Files

Treat these archived files as the workflow truth:

- `specs/grounding_report.json`
- `specs/implementation_spec.json`
- `drafts/run_drafts.json`

Execution artifacts are run attempts, not the main truth.

When repo inspection or execution results reveal new grounded facts, revise the truth files first, then continue from the revised truth:

- dataset files actually exist in repo
- runtime candidates are available
- entrypoint path or argv is wrong and now corrected
- a missing dependency set is now known, for example `numpyencoder`
- an old blocker is no longer true

Do not keep rediscovering the same facts by repeatedly rereading broad repo context once the truth files can be updated.

## Core Rule

If the user explicitly asks only for `intake_summary`, paper explanation, or planning-only work, keep the turn inside `intake_summary`.

- In that case, do not let old `execution` / `tuning` state change the scope of the answer.
- You may use `paper_research_status` only to resolve `project_id` / `workspace_id` or confirm whether planning artifacts already exist.
- If any planning artifact is missing or stale, refresh planning with `paper_research_prepare` before synthesizing:
  - `planning/paper_intake_result.json`
  - `planning/paper_summary.json`
  - `planning/experiment_spec.json`
- Do not recommend repo grounding, runtime inspection, execution, or tuning when the user explicitly forbids them.

Outside that exception, call `paper_research_status` first when `paper_id` or `project_id` is available.

Then decide the next action from state:

1. If no workspace or structured intake exists, run intake with `paper_research_prepare`.
2. If any planning artifact is missing or stale, refresh planning with `paper_research_prepare` before moving to later stages.
3. If `specs/grounding_report.json` is missing or stale, stay in `grounding`: probe repo/url first, then clone/read/search/inspect only as needed, and write a grounded evidence report before any implementation work.
4. If `grounding_report.json` says repo, entrypoint, dataset, runtime, or external dependencies are `blocked`, stop clearly and report those blockers instead of moving on to implementation or execution.
5. If `specs/implementation_spec.json` is missing or stale, create or revise it from planning plus `grounding_report`.
6. If `drafts/run_drafts.json` is missing or stale, create grounded run drafts from `implementation_spec`.
7. If the user asks to reproduce/run and baseline is not complete, prepare the missing prerequisite step or baseline execution, then continue toward the requested run.
8. If a prerequisite execution such as `env_setup` or `data_prep` is needed, start it, read its result in the same overall task, and continue when it completes.
9. If a true experiment execution (`baseline_repro`, `tuning`, `compare`) is running or pending, report `execution_id` and stop the turn.
10. If baseline completed and the user asks to optimize/tune/compare, first analyze current baseline plus repo evidence, then produce grounded tuning options for the user to choose from.
11. Only start a `first_tuning` execution after the user explicitly confirms which option to run.
12. If all requested work is complete, summarize evidence, metrics, blockers, and the smallest next action.

Do not stop after intake when the user asked for full reproduction. Continue until a long execution starts, a required user confirmation is needed, or a real blocker is reached.

## Boundaries

- Requires a saved paper. Prefer `paper_id`; use exact saved title only as fallback.
- This skill does not accept arbitrary chat-uploaded PDFs yet.
- Do not invent repo URLs, dataset links, metrics, parameters, scripts, or results.
- Do not claim an artifact exists unless you read it in the current turn with the dedicated read tool.
- Do not claim a write succeeded unless the corresponding write tool returned success in the current turn.
- Do not use `knowledge_search`, `literature_search`, or `mcp.*` tools for this workflow.
- Treat Project workspace artifacts as the source of truth; keep web evidence separate from paper/PDF evidence.
- Execution uses `runtime-worker`, not the chat backend process.
- During `grounding`, do not write `implementation_spec`, `run_drafts`, `execution_spec`, `execution` scripts, or start execution until `grounding_report` is complete.
- During `grounding`, missing evidence must stay `absent`/`blocked`; do not translate “not found” into an invented path, entrypoint, or workaround script.
- Long-running ML/DL jobs are background tasks. Once a real training/comparison job starts, report the execution and stop instead of waiting.
- Short prerequisite jobs such as `env_setup` or `data_prep` should be treated as workflow continuation steps, not as the final answer.
- Do not let `execution_spec` become the only source of truth. If an execution reveals new facts, sync them back into `implementation_spec` or `run_drafts`.

## Tool Use

Allowed paper workflow tools:

- `paper_research_status`
- `paper_research_prepare`
- `paper_research_clone_repo`
- `paper_research_probe_repo`
- `paper_research_probe_url`
- `paper_research_get_artifact_manifest`
- `paper_research_read_artifact`
- `paper_research_search_outputs`
- `paper_research_read_repo_file`
- `paper_research_search_repo`
- `paper_research_git_status`
- `paper_research_git_diff`
- `paper_research_git_log`
- `paper_research_git_show`
- `paper_research_assess_repo_mainpath`
- `paper_research_list_outputs`
- `paper_research_delete_output`
- `paper_research_cleanup_scope`
- `paper_research_write_grounding_report`
- `paper_research_read_grounding_report`
- `paper_research_write_implementation_spec`
- `paper_research_read_implementation_spec`
- `paper_research_write_run_drafts`
- `paper_research_read_run_drafts`
- `paper_research_inspect_runtime`
- `paper_research_write_execution_script`
- `paper_research_write_execution_spec`
- `paper_research_read_execution_spec`
- `paper_research_start_execution`
- `paper_research_read_execution`
- `paper_research_tail_execution_log`
- `paper_research_cancel_execution`
- `web_search`
- `web_scrape`

Use web tools only for focused diagnosis or official-source recovery after local repo/runtime evidence is insufficient.

## Stage Guidance

Read only the reference needed for the current state:

- PDF/intake facts and JSON constraints: `references/intake-contract.md`
- Grounding report contract and URL evidence shape: `references/grounding-contract.md`
- Implementation spec and run drafts: `references/implementation-planning.md`
- Runtime execution, source recovery, long tasks, and tuning/compare: `references/execution-and-tuning.md`
- Backend artifact fields and UI-facing explanations: `references/output-fields.md`
- PDF/markdown fallback behavior: `references/pipeline.md`
- Runtime environment and execution examples: `references/runtime-environment.md`
- Run-draft repo heuristics: `references/run-draft-heuristics.md`

`grounding` is a formal evidence-closure stage between `planning` and `implementation_prep`.

Its job is to write `specs/grounding_report.json`, not to produce execution specs.

Grounding must explicitly classify:

- repo
- entrypoint
- dataset
- runtime
- external dependencies

Grounding should also answer one engineering question first:

- what is the repo's most likely runnable main path right now
- can that main path run now
- if not, is it blocked or runnable_with_patch

Each area should end in one of:

- `grounded`
- `absent`
- `blocked`
- only use `unknown` when the evidence truly has not been collected yet

During `grounding`:

- prefer `paper_research_probe_repo` / `paper_research_probe_url` before deeper clone/read/search
- when a probe returns HTML, treat it as a diagnosis step first, not as success or failure by status code alone; use page title, page kind, page signals, suggested next action, and rationale to decide the blocker
- for download pages such as Google Drive gates, first write the concrete blocker (`download_gate`, `login_required`, `quota_limited`, `access_denied`, `not_found`) before deciding whether to recover, patch, or stop
- once repo evidence exists, use `paper_research_assess_repo_mainpath` so stage 2 converges on a concrete main-path judgment instead of only broad grep evidence
- when debugging old repos or verifying minimal fixes, prefer `paper_research_git_status` / `paper_research_git_diff` / `paper_research_git_log` / `paper_research_git_show` before inventing a workaround script
- use `paper_research_clone_repo`, `paper_research_read_repo_file`, `paper_research_search_repo`, and `paper_research_inspect_runtime` only to close a specific missing fact
- write `specs/grounding_report.json` as the stage-2 truth artifact
- if you claim a list of datasets or external downloads is `grounded`, every required official link in that list must be individually probed or be explicitly covered by grounded local presence; one successful sample link cannot stand in for sibling links
- if an official repo/data/dependency source is `blocked`, first write the blocker clearly, then do one focused recovery pass for alternative sources using `web_search` / `web_scrape`; record trustworthy candidates in `alternative_source_candidates`, but do not erase the official-source blocker
- blocked sections should explain *why* they are blocked in `blockers` and, when possible, `blocker_details` with concrete URL / diagnosis / status-code context
- do not start execution
- do not generate execution scripts as a workaround
- do not skip straight to `implementation_spec`

Before writing structured artifacts, read the relevant schema:

- `specs/grounding_report.json` uses the stage-2 grounding contract enforced by `paper_research_write_grounding_report`
- `templates/implementation_spec.schema.json`
- `templates/run_drafts.schema.json`

When writing `run_drafts`, follow the current schema exactly. Each draft must use:

- `id`
- `kind`
- `title`
- `objective`
- `entrypoint.type`
- `entrypoint.path_or_hint`
- `depends_on`
- `data_requirements`
- `env_requirements`
- `params`
- `expected_outputs`
- `blockers`
- `evidence_files`
- `grounding_notes`

Do not switch back to legacy aliases such as `draft_id`, `label`, `description`, `goal`, `changes`, `path`, or `python_script`.
For real repo files, use `entrypoint.type="repo_script"` and a repo-relative `entrypoint.path_or_hint`, for example `seq2seq.py`.
For repo evidence, use canonical archived paths such as `repo/source/seq2seq.py` or `specs/implementation_spec.json`.

Use helper scripts when deterministic output is safer:

- `scripts/render_stage_prompt.py` for launch prompts.
- `scripts/render_execution_spec.py` for execution spec skeletons.
- `scripts/check_artifact_contract.py` for artifact contract validation.
- `scripts/check_runtime_environment.py` for runtime-worker package checks.

## Execution Rules

- Environment constraints must enter the workflow before execution.
  - During `grounding`, call `paper_research_inspect_runtime` and treat `runtime_candidates` plus `runtime_worker.environment` as grounding inputs, not only execution diagnostics.
  - `grounding_report.json` should already classify whether runtime is `grounded`, `absent`, or `blocked`, and should record explicit blocker text when the worker cannot support the paper's likely path.
  - The generated `implementation_spec.json` should reflect the current runtime snapshot, available commands, and any grounded missing packages that constrain later execution.
  - Do not postpone all environment reasoning until `execution`; the plan should already know whether the current machine supports `devcontainer`, `docker compose`, `repo2docker`, `papermill`, or only `plain-python`.
- Always inspect runtime during `grounding` before writing `implementation_spec` or any execution artifact.
- Always read `grounding_report.json` before writing `implementation_spec` or any execution artifact.
- If `grounding_report.json` is missing or not complete, stop and update/report grounding instead of continuing into implementation or execution.
- Before writing a new execution spec, read the latest relevant truth file first:
  - `grounding_report.json` for repo/data/runtime/external-dependency status
  - `implementation_spec.json` for baseline assumptions and blockers
  - `run_drafts.json` for the current execution-ready draft
- If the latest execution result contradicts the truth files, revise the truth files first and only then create the next execution.
- For repo-backed execution, infer dependencies from repo evidence first:
  - read `README.md` and any dependency files such as `requirements.txt`, `pyproject.toml`, `environment.yml`, `setup.py`
  - read the selected entrypoint script and only the local modules needed to understand its imports
- When locating a code block, do not keep increasing `max_chars` on the same file.
  - First use `paper_research_search_repo` to get the hit line number.
  - Then use `paper_research_read_repo_file` with `line_start` and `line_end` to read only the needed range.
  - If search already returns `context_lines`, use that local snippet first before widening the read.
- use the model to form the concrete dependency set for the selected draft from those files
- Include locally imported helper-module dependencies in `env_requirements` when they are needed by the selected entrypoint.
- Write an archived execution spec before starting execution.
- Prefer `execution_spec.execution_intent` over free-form `command`/`cwd`.
  - Use typed fields such as `runtime_type`, `entrypoint_type`, `entrypoint_path`, `cwd_mode`, and `args`.
  - Let the backend render the final argv/cwd deterministically.
- Do not mix `execution_intent` with raw `command`, `cwd`, or `input_notebook`.
- `execution_spec.command` must be a JSON string array, never a shell string.
- Do not use shell wrappers such as `bash -lc`, `sh -c`, or PowerShell wrappers.
- `execution_spec.preflight_checks` must be a JSON object array, never a key/value map. Use forms like `[{"name":"check_python","required":true,"status":"passed"}]`, not `{"check_python": true}`.
- Preserve official repo/data URLs in `command` or `external_dependencies`; runtime preflight will verify them.
- Use workspace-relative paths only. Prefer `repo/source/...` for repository files.
- Read execution results with `paper_research_read_execution`; never use `paper_research_read_artifact` for `executions/*`.
- When only the live progress or latest failure matters, prefer `paper_research_tail_execution_log` instead of re-reading the full execution payload.
- If `paper_research_start_execution` starts a prerequisite job such as `env_setup` or `data_prep`, continue the workflow by reading its result and resuming the original task.
- If `paper_research_start_execution` returns `running` or `pending` for a true experiment execution, return the execution id/status and stop the turn.
- `scripts/check_runtime_environment.py` is only a verifier. Do not let a fixed generic ML package list override direct repo dependency evidence.
- When execution reveals a concrete blocker, convert it into truth-file updates:
  - missing package -> add/update `env_requirements` and blocker text in `run_drafts`
  - dataset already present -> remove stale `dataset_missing` blocker
  - runtime already verified -> remove stale `runtime_unknown` blocker
  - corrected argv or entrypoint -> update the draft and grounding notes
- Do not repeatedly reread large repo sections after a blocker is already grounded in the truth files. Read only the minimal files needed to resolve a specific contradiction.

## Tuning Rules

- Tuning analysis begins only after a completed baseline.
- Do not automatically execute tuning just because baseline completed.
- Default tuning behavior is: analyze current state, read baseline + implementation plan + repo evidence, then present 2-4 grounded tuning options.
- Each option should explain: what changes, why it is supported by paper/repo evidence, expected benefit, risk, and whether it is low-cost on the current machine.
- Only execute after the user explicitly says to run/start/execute one option or clearly confirms a specific `first_tuning`.
- Use `implementation_spec.tuning_plan` / `experiment_spec.optimization_candidates` as the primary source.
- First tuning should be one minimal, low-risk change: one parameter, one small algorithmic switch, or one lightweight model/config variant.
- Do not change dataset or add heavyweight dependencies for the first tuning unless the user explicitly asks.
- If params are hard-coded, create an execution-scoped generated script via `execution_spec.generated_files`; do not overwrite repo files.
- Prefer `paper_research_write_execution_script` when you need to author a larger execution-scoped Python variant first.
  - It writes only under `executions/{execution_id}/...`.
  - Then reference that script from `paper_research_write_execution_spec` using `execution_intent.entrypoint_type="generated_python"`.
- Each `generated_files` item should contain:
  - `relative_path`
  - `content`
  Use execution-scoped paths such as `executions/{execution_id}/train_variant.py`.
- When `cwd` is `repo/source`, run generated files via a relative path such as `../executions/{execution_id}/train_variant.py`.
- Compare baseline and tuning with shared metrics and state whether the result improved, regressed, or is inconclusive.

## Response

Answer in Chinese by default.

For planning/prep responses, include:

- Current source state: PDF markdown vs metadata fallback.
- Existing artifacts read this turn.
- What was written or why it was not written.
- Current blockers and next action.

For `intake_summary`-only responses, also enforce:

- Focus on paper facts, reusable summary, reproduction risks, and next verification questions.
- Do not report old baseline/tuning/runtime state unless the user explicitly asks for Project status.
- If `paper_summary.json` is missing, say that planning artifacts need refresh instead of pretending later stages are the current task.

For tuning-analysis responses before execution, include:

- baseline current state and key metrics/evidence
- 2-4 grounded tuning options
- one recommended first option
- a clear note that execution has not started yet and requires user confirmation

For execution responses, include:

- Selected draft or execution id.
- Runtime type and command summary.
- Status: `pending`, `running`, `completed`, `failed`, or `blocked`.
- Log/result evidence when available.
- If running, stop after reporting status.
