# Implementation Planning

Use this reference when creating or revising `specs/implementation_spec.json` and `drafts/run_drafts.json`.

## Truth Maintenance

`implementation_spec.json` and `run_drafts.json` are editable truth files, not write-once snapshots.

Revise them whenever later grounded evidence changes the known state, for example:

- repo already contains the dataset files
- runtime inspection already found valid candidates
- the real entrypoint/path/argv is now confirmed from repo evidence
- execution results exposed a concrete missing package
- a previous blocker was resolved by `env_setup`, `data_prep`, or another successful prerequisite execution

Do not leave stale blockers in place after local repo or execution evidence disproved them.

## Implementation Spec

`implementation_spec` must stay evidence-grounded and should follow `templates/implementation_spec.schema.json`.

Required top-level fields:

- `schema_version`
- `paper_id`
- `project_id`
- `workspace_id`
- `mode`
- `baseline`
- `repo_plan`
- `data_plan`
- `tuning_plan`
- `readiness`
- `blockers`
- `next_actions`
- `evidence_log`

Valid `mode` values:

- `repo_driven`
- `notebook_driven`
- `hybrid`
- `paper_only`
- `blocked`

Rules:

- Use `files_read` and `evidence_log` with the same relative-path convention returned by the manifest.
- Prefer official repo URLs and official dataset/artifact URLs recorded in the paper or README.
- `paper_research_clone_repo` and explicit repo evidence are the primary checks for repository viability.
- If the repo is already materialized, verify expected dataset files against the current repo before emitting `dataset_missing`, `needs download`, or equivalent blockers.
- `web_search` / `web_scrape` are only for diagnosing official-source failures or confirming an updated official location.
- If `repo_reference.json` exposes `repo_history_candidates_file`, read that file before public web search for dead official URLs.
- `readiness.can_create_run_draft` may be true only when evidence is enough to draft without guessing repo/data details.
- `readiness.can_execute` may be true only when required repo/data evidence is grounded or the implementation is strictly local.
- If `paper_research_inspect_runtime` already returns runtime candidates, do not keep a generic `runtime_unknown` blocker. Replace it with a specific package/runtime blocker only when local evidence supports it.
- If evidence is insufficient, write explicit blockers and next actions instead of optimistic placeholders.
- When repo data files are already present locally, rewrite dataset status to reflect that local truth instead of leaving `requires_download` or `dataset_missing` as the active blocker.
- When execution logs expose a concrete missing package, record it as a grounded blocker or dependency update instead of keeping a vague environment blocker.

## Run Drafts

`run_drafts` must stay grounded in `implementation_spec` plus explicitly read repo evidence.

Required top-level fields:

- `schema_version`
- `paper_id`
- `project_id`
- `workspace_id`
- `drafts`

Each draft should include:

- `id`
- `kind`
- `title`
- `objective`
- `entrypoint`
- `data_requirements`
- `env_requirements`
- `params`
- `expected_outputs`
- `blockers`
- `evidence_files`
- `grounding_notes`

Valid `kind` values:

- `env_setup`
- `data_prep`
- `smoke_test`
- `baseline_repro`
- `evaluation`
- `first_tuning`
- `custom`

Rules:

- Keep drafts implementation-ready, not execution-complete.
- Do not generate training code, shell scripts, notebook cells, or execution logs in this stage.
- Keep exact file names, parameter values, and blocker descriptions when evidence contains them.
- `entrypoint` must be an object, never a string.
- For repo-backed drafts, `entrypoint` must use `path_or_hint`, not `path`.
- Use `repo_script`, `notebook`, or `config` only when the path exists in `repo/repo_file_index.json` or was read via `paper_research_read_repo_file`.
- Use `entrypoint.type="repo_script"` for verified Python repo files. Do not emit legacy `python_script`.
- Use `readme_command`, `dataset_step`, or `manual_step` for README-derived setup/data actions that are not real repo scripts.
- Never invent a file path to satisfy the schema.
- Baseline drafts should depend on setup/data drafts when dependencies or data remain blockers.
- Tuning drafts should depend on a baseline draft and must not pretend the baseline has already succeeded.
- `env_requirements` should include dependencies derived from the selected entrypoint and the minimal local helper modules it imports. Do not stop at top-level README packages if local imports reveal more required packages.
- `evidence_files` must contain archived canonical paths:
  - repo files as `repo/source/<repo-relative-path>`
  - generated specs as `specs/implementation_spec.json`
  - other workspace artifacts using their manifest-relative path
- Do not use legacy aliases such as:
  - `draft_id`
  - `label`
  - `description`
  - `goal`
  - `changes`
  - `entrypoint.path`
  - `entrypoint.type="python_script"`

Minimal valid `repo_script` example:

```json
{
  "id": "baseline_bart",
  "kind": "baseline_repro",
  "title": "Baseline BART reproduction",
  "objective": "Reproduce the paper's reported BART baseline.",
  "entrypoint": {
    "type": "repo_script",
    "path_or_hint": "seq2seq.py"
  },
  "depends_on": ["env_setup", "data_prep"],
  "data_requirements": ["Dataset/train.json", "Dataset/val.json", "Dataset/test.json"],
  "env_requirements": ["torch", "transformers"],
  "params": {
    "model_name": "facebook/bart-base",
    "batch_size": 4,
    "max_source_length": 256
  },
  "expected_outputs": ["training logs", "evaluation metrics"],
  "blockers": [],
  "evidence_files": ["repo/source/seq2seq.py", "specs/implementation_spec.json"],
  "grounding_notes": ["Command and params were grounded in repo/source/README.md and repo/source/seq2seq.py."]
}
```

When later execution reveals new facts, revise the same draft instead of creating a competing second truth. Typical updates:

- remove `dataset_missing` after confirming `Dataset/train.json` exists
- replace `runtime_unknown` after runtime inspection succeeds
- add `numpyencoder` or another concrete package to `env_requirements`
- update `grounding_notes` when argv or entrypoint was corrected from repo evidence
