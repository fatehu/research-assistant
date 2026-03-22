# Experience / Workbench Session Recovery

Date: 2026-03-17

## What happened

The local Codex session store under `~/.codex/sessions` was cleaned to reclaim WSL disk space.
That removed historical `rollout-*.jsonl` sub-agent sessions and also left no resumable local session files for `/resume`.

This file reconstructs the current project state so work can continue from the repository directory.

## Product boundary

- `/read` is considered complete and frozen except as a payload producer.
- Active product surfaces are:
  - `/experience`
  - `/workbench`

## Current state

The main implementation goals for `/experience` and `/workbench` were treated as complete before session cleanup:

- guided-reading runtime is in place
- internal planning text is hidden from `/experience`
- guided beat numbering is reader-facing
- experience/generative plan caching is persisted through DB-backed cache flow
- beat enrichment prefers concrete findings over generic public-link rewrites
- noisy caption cases recover useful reader-facing lines such as `target learner`
- `/workbench` inspect surfaces were previously brought to a usable state

## Key files

- [backend/app/services/generative_reader_agent_runtime.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/services/generative_reader_agent_runtime.py)
- [backend/app/api/literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/api/literature.py)
- [backend/app/models/literature.py](/mnt/d/codefield/agent-platform/research-assistant/backend/app/models/literature.py)
- [backend/alembic/versions/022_reader_plan_cache_persistence.py](/mnt/d/codefield/agent-platform/research-assistant/backend/alembic/versions/022_reader_plan_cache_persistence.py)
- [frontend/src/pages/literature/GenerativeExperienceRenderer.tsx](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/pages/literature/GenerativeExperienceRenderer.tsx)
- [frontend/src/pages/literature/PaperReaderExperiencePage.tsx](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/pages/literature/PaperReaderExperiencePage.tsx)
- [frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/pages/literature/PaperReaderWorkbenchPage.tsx)
- [frontend/src/pages/literature/readerSurfaceLoader.ts](/mnt/d/codefield/agent-platform/research-assistant/frontend/src/pages/literature/readerSurfaceLoader.ts)

## Last verified checks

These checks were green immediately before the session cleanup:

```bash
./backend/.venv-incremental/bin/python -m pytest backend/tests/test_generative_reader_agent_runtime.py -q
./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "experience_plan" -q
cd frontend && npx tsc --noEmit
cd frontend && npx eslint --quiet src/pages/literature/GenerativeExperienceRenderer.tsx src/pages/literature/PaperReaderExperiencePage.tsx
```

Observed results:

- `backend/tests/test_generative_reader_agent_runtime.py`: `57 passed`
- `backend/tests/test_literature_reader_api.py -k "experience_plan"`: `9 passed`

## Important behavior decisions

- Do not resume `/read` UI work unless explicitly requested.
- Prefer richer reader-facing explanations in `/experience`.
- Prefer concrete tool findings, especially `web_scrape`, over generic `public_link` background rewrites.
- Keep sub-agent usage cleaned up after acceptance/rejection to avoid WSL growth.

## Cleanup status

- `~/.codex/sessions` was reduced from about `81G` to effectively empty historical state.
- `rollout-*.jsonl` sub-agent session files were removed.
- WSL internal space was trimmed with `fstrim`.
- Windows-side `ext4.vhdx` still needs host-side compaction after `wsl --shutdown` if physical file size must shrink.

## If work resumes from here

1. Start from this repository directory:
   - `/mnt/d/codefield/agent-platform/research-assistant`
2. Treat this file as the session handoff note.
3. Re-run the verification commands above before making new changes.
