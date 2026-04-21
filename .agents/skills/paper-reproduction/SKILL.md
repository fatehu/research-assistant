---
name: paper-reproduction
description: Reproduce and tune saved machine-learning or deep-learning papers from archived Project artifacts. Use when the user asks to reproduce a paper, run its code, prepare implementation plans, launch baseline experiments, tune parameters/models, compare results, or continue a saved paper Project.
---

# Paper Reproduction

Use this skill as the complete business workflow for reproducing and tuning a saved paper.

The user should not need to know internal phases. Always begin from the saved Project state, then continue from the first missing, blocked, or requested step.

## Truth Files

Treat these two archived files as the editable workflow truth:

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

Call `paper_research_status` first when `paper_id` or `project_id` is available.

Then decide the next action from state:

1. If no workspace or structured intake exists, run intake with `paper_research_prepare`.
2. If intake exists but repo/data evidence is missing, materialize or inspect repo evidence.
3. If `specs/implementation_spec.json` is missing or stale, create or revise it from intake plus repo/data evidence and the current runtime/environment snapshot.
4. If `drafts/run_drafts.json` is missing or stale, create grounded run drafts from `implementation_spec`.
5. If the user asks to reproduce/run and baseline is not complete, prepare the missing prerequisite step or baseline execution, then continue toward the requested run.
6. If a prerequisite execution such as `env_setup` or `data_prep` is needed, start it, read its result in the same overall task, and continue when it completes.
7. If a true experiment execution (`baseline_repro`, `tuning`, `compare`) is running or pending, report `execution_id` and stop the turn.
8. If baseline completed and the user asks to optimize/tune/compare, first analyze current baseline plus repo evidence, then produce grounded tuning options for the user to choose from.
9. Only start a `first_tuning` execution after the user explicitly confirms which option to run.
10. If all requested work is complete, summarize evidence, metrics, blockers, and the smallest next action.

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
- Long-running ML/DL jobs are background tasks. Once a real training/comparison job starts, report the execution and stop instead of waiting.
- Short prerequisite jobs such as `env_setup` or `data_prep` should be treated as workflow continuation steps, not as the final answer.
- Do not let `execution_spec` become the only source of truth. If an execution reveals new facts, sync them back into `implementation_spec` or `run_drafts`.

## Tool Use

Allowed paper workflow tools:

- `paper_research_status`
- `paper_research_prepare`
- `paper_research_clone_repo`
- `paper_research_get_artifact_manifest`
- `paper_research_read_artifact`
- `paper_research_read_repo_file`
- `paper_research_search_repo`
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
- `paper_research_cancel_execution`
- `web_search`
- `web_scrape`

Use web tools only for focused diagnosis or official-source recovery after local repo/runtime evidence is insufficient.

## Stage Guidance

Read only the reference needed for the current state:

- PDF/intake facts and JSON constraints: `references/intake-contract.md`
- Implementation spec and run drafts: `references/implementation-planning.md`
- Runtime execution, source recovery, long tasks, and tuning/compare: `references/execution-and-tuning.md`
- Backend artifact fields and UI-facing explanations: `references/output-fields.md`
- PDF/markdown fallback behavior: `references/pipeline.md`
- Runtime environment and execution examples: `references/runtime-environment.md`
- Run-draft repo heuristics: `references/run-draft-heuristics.md`

Before writing structured artifacts, read the relevant schema:

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
  - During `implementation_prep`, call `paper_research_inspect_runtime` and treat `runtime_candidates` plus `runtime_worker.environment` as planning inputs, not only execution diagnostics.
  - The generated `implementation_spec.json` should reflect the current runtime snapshot, available commands, and any grounded missing packages that constrain later execution.
  - Do not postpone all environment reasoning until `execution`; the plan should already know whether the current machine supports `devcontainer`, `docker compose`, `repo2docker`, `papermill`, or only `plain-python`.
- Always inspect runtime with `paper_research_inspect_runtime` before writing an execution spec.
- Before writing a new execution spec, read the latest relevant truth file first:
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
