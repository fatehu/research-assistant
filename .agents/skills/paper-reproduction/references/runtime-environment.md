# Runtime Environment

Use this reference only when the task reaches execution, reproduction, tuning, or runtime debugging.

## Runtime Model

- Backend owns Project state, archived artifacts, and tool permissions.
- `runtime-worker` owns environment-heavy execution for paper projects.
- Do not run training in the backend process.
- Do not use the codelab runner for paper-reproduction baselines unless the selected draft is explicitly notebook/cell-only.
- Prefer repository-declared environments when available: devcontainer, docker compose, Dockerfile, repo2docker, papermill.
- Use `plain-python` only for explicit smoke tests, lightweight repo scripts, or repositories whose dependency surface already matches the worker environment.

## Local Startup

Start the worker profile and restart backend with worker access:

```bash
PROJECT_RUNTIME_WORKER_ENABLED=true docker compose --profile runtime up -d runtime-worker backend
```

Check status from the backend tool path:

```bash
docker compose exec -T backend python - <<'PY'
import asyncio
from app.services.project_runtime_service import ProjectRuntimeWorkerClient

async def main():
    print(await ProjectRuntimeWorkerClient().tools())

asyncio.run(main())
PY
```

Check the worker environment directly:

```bash
docker compose --profile runtime exec -T runtime-worker \
  python /app/.agents/skills/paper-reproduction/scripts/check_runtime_environment.py --require-ml-defaults
```

## Base ML Environment

The worker image is expected to include the common CPU ML stack once, not reinstall it per project:

- `torch`
- `numpy`
- `pandas`
- `scipy`
- `scikit-learn`
- `h5py`
- `matplotlib`
- `seaborn`
- `schedulefree`
- `papermill`
- `jupyter-repo2docker`
- `devcontainer` CLI
- Docker CLI

Project-specific small dependencies may be installed by an explicit setup draft, but do not install large packages such as PyTorch per project unless the repository requires an incompatible version and the user accepts the cost.

Runtime caches are mounted under `/app/runtime_cache`:

- `PIP_CACHE_DIR=/app/runtime_cache/pip`
- `HF_HOME=/app/runtime_cache/huggingface`
- `HUGGINGFACE_HUB_CACHE=/app/runtime_cache/huggingface/hub`
- `XDG_CACHE_HOME=/app/runtime_cache/xdg`

## Execution-Spec Helper

Use the helper to render a valid skeleton, then pass the JSON to `paper_research_write_execution_spec`.

Preferred structured example for a Python repo entrypoint:

```bash
python .agents/skills/paper-reproduction/scripts/render_execution_spec.py \
  --execution-id baseline-repro \
  --draft-id baseline_repro \
  --runtime-type plain-python \
  --entrypoint-type repo_script \
  --entrypoint-path train.py \
  --args-json '[]' \
  --artifact-glob 'executions/baseline-repro/**/*' \
  --evidence-file drafts/run_drafts.json
```

Executable shell repo entrypoints should use direct argv, not `execution_intent.repo_script`:

```bash
python .agents/skills/paper-reproduction/scripts/render_execution_spec.py \
  --execution-id baseline-repro \
  --draft-id baseline_repro \
  --runtime-type plain-python \
  --cwd repo/source \
  --command-json '["./classification-results.sh"]' \
  --artifact-glob 'executions/baseline-repro/**/*' \
  --evidence-file drafts/run_drafts.json
```

If the execution depends on an official external download, you may also declare it explicitly:

```bash
python .agents/skills/paper-reproduction/scripts/render_execution_spec.py \
  --execution-id data-prep \
  --draft-id data_prep \
  --runtime-type plain-python \
  --cwd repo/source \
  --command-json '["curl","-L","-o","300k_150x5_2.h5","https://official.example/file"]' \
  --external-dependency-json '{"name":"prior-dump","kind":"url","target":"https://official.example/file","expected_kind":"hdf5","required":true,"source":"official"}' \
  --evidence-file drafts/run_drafts.json
```

Papermill example:

```bash
python .agents/skills/paper-reproduction/scripts/render_execution_spec.py \
  --execution-id notebook-baseline \
  --draft-id baseline_repro \
  --runtime-type papermill \
  --entrypoint-type notebook \
  --entrypoint-path repo/source/demo.ipynb \
  --parameters-json '{"epochs": 1}' \
  --artifact-glob 'executions/notebook-baseline/**/*' \
  --evidence-file drafts/run_drafts.json
```

## Execution Stage Rules

1. Read `drafts/run_drafts.json`.
2. Pick the next useful draft. After a successful smoke test, prefer `baseline_repro`; do not repeat smoke unless new evidence says it is stale.
3. If the selected draft maps to an existing `execution_id`, read the archived spec/result first.
4. If an existing result answers the stage, summarize it and stop unless the user explicitly requested a rerun.
5. Call `paper_research_inspect_runtime` and read `runtime_worker.environment`.
6. If all matching runtime candidates are blocked, report the blocker and stop.
7. Write one `execution_spec` with workspace-relative paths.
8. Prefer `execution_intent` for Python repo files and notebooks. Use argv-array commands when the real entrypoint is an executable repo file such as `classification-results.sh`.
9. `execution_intent.entrypoint_type="repo_script"` currently means a Python repo file. For executable shell entrypoints, use direct argv such as `["./classification-results.sh"]`.
10. If you include `preflight_checks`, encode them as a JSON array of check objects, for example `[{"name":"check_python","required":true,"status":"passed"}]`; never send a JSON object map like `{"check_python": true}`.
11. Preserve the README or official repo command whenever possible. Do not rewrite official download URLs into ad-hoc mirrors.
12. `start_execution` performs internal preflight for official external dependencies. It also heuristically checks URLs embedded in download commands such as `curl` / `wget`.
13. Only after that should execution start.
14. Read execution result/log with `paper_research_read_execution` before interpreting success or failure.
15. If still running, return `execution_id` and status instead of pretending completion.
16. If failed, use log evidence and propose the smallest next correction.

## Failure Interpretation

- `OSError: Unable to synchronously open file (file signature not found)` from `h5py.File(...)` means the file path was opened but the file content is not a valid HDF5 payload. Treat this as a corrupt or wrong downloaded artifact, not simply as a missing file.
- When a data-prep execution exits successfully but the later baseline reports an invalid HDF5 signature, the smallest correction is to rerun data prep with explicit size/signature validation before retrying baseline.
- Do not mark `data_prep` semantically successful only because the process return code was zero; the expected data artifact must be usable by the baseline loader.

Execution files are archived under:

- `executions/{execution_id}/execution_spec.json`
- `executions/{execution_id}/execution_result.json`
- `executions/{execution_id}/execution.log`

Do not call `paper_research_read_artifact` for `executions/*` paths; it only reads manifest-backed planning/repo/spec/draft artifacts.

## What Not To Do

- Do not silently downgrade `baseline_repro` to `smoke_test`.
- Do not use absolute host paths in specs.
- Do not invent missing scripts, datasets, or config files.
- Do not re-run PDF intake to fix execution problems.
- Do not install heavyweight dependencies repeatedly in each project workspace.
