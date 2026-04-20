# Execution And Tuning

Use this reference when the user asks to run, reproduce, test, verify, tune, compare, inspect background jobs, or debug execution failures.

## Execution State

- Execution recovery is part of the execution stage, not a separate diagnostic branch.
- Execution is controlled by small tools and archived specs, not hidden backend workflow.
- If the selected `execution_id` already has an archived spec or result, read it first.
- Do not create or start a new execution when an existing result is sufficient, unless the user explicitly asks to rerun.
- If execution is still running, report `execution_id` and current status instead of producing final results.
- If an execution result reveals a new grounded fact, update the truth files first:
  - `specs/implementation_spec.json`
  - `drafts/run_drafts.json`
- Do not keep broad repo rediscovery loops once the contradiction is understood. Revise truth, then continue from truth.

## Runtime Selection

Call `paper_research_inspect_runtime` before writing an execution spec.

Prefer repository-declared environments over fallback execution:

- `.devcontainer/devcontainer.json`
- `docker-compose.yml` / `compose.yml`
- `Dockerfile`
- notebook entrypoints for `papermill`
- standard dependency files for `repo2docker`
- `plain-python` only when it matches the repo/draft evidence

Dependency understanding must be repo-driven:

- First read repo evidence:
  - `README.md`
  - dependency files such as `requirements.txt`, `pyproject.toml`, `environment.yml`, `setup.py` when present
  - the selected entrypoint script
  - only the local helper modules needed to understand that entrypoint's imports
- Let the model infer the actual dependency set for the selected draft from those files.
- Treat generic environment checks as verification only, not as the source of truth when repo evidence already exists.

Do not claim Docker/devcontainer/repo2docker execution succeeded when runtime worker or CLI is unavailable.

If repo evidence implies missing packages, stop at an explicit environment blocker or create a concrete `env_setup` execution only for those small project-specific dependencies.
When you need a package/environment probe, use `scripts/check_runtime_environment.py` with explicit `--require` packages derived from the repo instead of inventing a long inline `python -c` one-liner or relying on a fixed default package list.
If a local helper import reveals an extra package such as `numpyencoder`, add it to the draft truth before starting the next baseline attempt.

## Execution Spec

An `execution_spec` is one concrete run attempt, not a research plan. It should include:

- `execution_id` or `draft_id`
- `runtime_type`
- `cwd`
- `command` for command-driven runtimes
- `input_notebook` and `parameters` for `papermill`
- `expected_outputs`
- `artifact_globs`
- `evidence_files`
- `blockers`
- `external_dependencies` when official downloads are required
- `generated_files` only for execution-scoped variant files

Rules:

- Save the spec with `paper_research_write_execution_spec` before starting it.
- `command` must be an argv array, for example `["python", "train.py"]`.
- `preflight_checks` must be a list of objects, for example `[{"name":"check_python","required":true,"status":"passed"}]`. Do not send a dict such as `{"check_python": true}`.
- Use workspace-relative paths only.
- Keep `cwd` aligned with the actual repo root. Do not drift into a data subdirectory unless the execution really targets that subdirectory.
- Preserve official repo/data URLs in the spec. Runtime preflight will verify them.
- Read results with `paper_research_read_execution`.
- Never use `paper_research_read_artifact` for `executions/*`.
- A baseline or tuning `execution_spec` must come from the latest truth files. Do not treat a newly improvised command as the new truth unless you also sync it back to `implementation_spec` or `run_drafts`.

## Official Source Recovery

If `paper_research_start_execution` returns `external_dependency_preflight_failed` for a required official external dependency, run one focused recovery attempt before concluding blocked.

Recovery order:

1. Read `repo_reference.json`.
2. If it contains `repo_history_candidates_file`, read that file first and inspect commit-diff candidates.
3. Prefer a repo-history candidate when filename matches exactly and host still belongs to the same official org/lab/project.
4. If history candidates are empty, unusable, or rejected by preflight, use public web search with exact filename plus repo/project/org/lab terms.
5. If search results look promising but ambiguous, use `web_scrape` on the current official repo page, project page, or same-org result.
6. Accept a candidate only when it still looks official and points to the same artifact.
7. Write one new execution spec with the recovered candidate URL and run preflight again through `paper_research_start_execution`.

Do not loop on broad searches. Stop after one focused recovery attempt if no official candidate validates.

## Fresh Baseline After Data Prep

When `preferred_draft_id=baseline_repro`, treat an older baseline failure as stale if:

- a newer `data_prep` execution completed successfully, and
- the older baseline failure was caused by invalid/corrupt/schema-mismatched data.

Then read the successful `data_prep` result, confirm the prepared artifact exists, and write a fresh `baseline_repro` execution spec.

## Baseline Dependency Gate

Before starting a repo-backed `baseline_repro`:

- confirm the selected draft's entrypoint and dataset paths from repo evidence
- infer the concrete dependency set for that draft from README/dependency files/imports
- verify those inferred packages with `scripts/check_runtime_environment.py --require ...`

Do not start baseline when a required repo-derived dependency is still missing. Surface the missing package set or create one concrete `env_setup` step for it.
After a prerequisite step succeeds, remove the now-resolved blocker from the truth files before retrying baseline.

## Long Tasks

When `paper_research_start_execution` returns `running` or `pending`:

- if the started execution is a prerequisite step such as `env_setup` or `data_prep`, do not treat it as the final answer
- read the execution result/log in the same overall task and continue back to baseline/tuning when it finishes
- if that result updates what is known about dataset/runtime/dependencies/argv, sync the truth files before the next execution
- if the started execution is a true experiment run such as `baseline_repro`, `tuning`, or `compare`, report `execution_id`
- report the selected draft and current status
- stop the turn for those true long-running experiment jobs
- do not keep waiting for ML/DL training
- do not continue broad search or unrelated tool calls after the real training/comparison job starts

## First Tuning

After baseline succeeds, the natural next step is not immediate execution. First produce a grounded tuning analysis and a small set of candidate options. Execute only after the user explicitly confirms which option to run.

Use `implementation_spec.tuning_plan` or `experiment_spec.optimization_candidates` as the primary source. Do not invent generic ML advice when no paper-grounded tuning plan exists.

Default output before execution:

- concise baseline state summary
- 2-4 grounded tuning options
- for each option: exact change, supporting evidence, expected benefit, risk, and estimated cost
- a recommendation for the smallest useful first tuning

Prefer the smallest useful first tuning:

- one parameter or one small algorithmic switch
- no dataset change
- no heavyweight new dependency
- likely to finish on current machine

For repo-driven projects:

- Use `paper_research_search_repo` to locate CLI/config/notebook parameter entrypoints before reading individual files.
- Prefer existing CLI/config/notebook parameters.
- If params are hard-coded, create an execution-scoped sibling variant script through `execution_spec.generated_files`.
- Do not overwrite original repo files.
- `generated_files.relative_path` must stay under the execution workspace, for example `executions/{execution_id}/train_variant.py`.
- If `cwd` remains `repo/source`, invoke generated files with a path relative to the repo root, for example `../executions/{execution_id}/train_variant.py`.

Hyperparameter sweeps, model replacement, architecture changes, or multi-run comparisons require explicit user confirmation before execution.

Even for a low-risk first tuning, do not start execution unless the user explicitly asks to run/start/execute the proposed option.

After tuning completes, read baseline and tuning executions, compare shared metrics, and summarize improved/regressed/inconclusive.
