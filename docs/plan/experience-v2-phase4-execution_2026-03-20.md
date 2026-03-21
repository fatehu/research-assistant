# Experience V2 Phase 4 Execution

## Status

- Phase 4 live route/runtime wiring is landed for the v2 path.
- Scope stayed incremental on top of the landed Phase 1-3 helper/contract work.
- Reader/workbench v2 routes are distinct from v1 and do not silently fall back to legacy output.

## Landed Scope

### Backend

- Added live v2 route wiring for:
  - `POST /api/v1/literature/papers/{paper_id}/experience-v2/cached`
  - `POST /api/v1/literature/papers/{paper_id}/experience-v2`
  - `POST /api/v1/literature/papers/{paper_id}/workbench-v2`
- Reader-facing v2 path now:
  - loads/builds `reading_dossier_v2`
  - loads/builds `experience_session_v2`
  - requires the internal `narrative_brief`
  - loads/builds `page_artifact_v2`
  - returns only completed artifact on the reader route
- Explicit failure semantics are enforced for missing ordered adjacent context, missing current-page grounding, missing narrative brief, unresolved artifact generation, and invalid cached artifact.

### Frontend

- Added live routes for:
  - `/literature/:paperId/experience-v2`
  - `/literature/:paperId/workbench-v2`
- Added a reader-first `experience-v2` page:
  - cached request first
  - ready artifact render when available
  - generation shell while no completed artifact exists
  - explicit failure path with pointer to `/workbench-v2`
- Added an inspect-first `workbench-v2` page exposing:
  - dossier
  - session
  - narrative brief / reading strategy
  - artifact
  - artifact validation
  - failure state
  - presentation rationale when present
- Added `PageArtifactV2Renderer` for schema-bound artifact blocks instead of v1 guided-card semantics.

## Verification

### Backend

```bash
./backend/.venv-incremental/bin/python -m pytest backend/tests/test_literature_reader_api.py -k "reading_dossier_v2 or experience_session_v2 or page_artifact_v2 or experience_v2 or workbench_v2" -q
```

Result:

- `39 passed, 48 deselected`

### Frontend

```bash
cd frontend
npm run lint
npx tsc --noEmit
npm run build
```

Results:

- `npm run lint` passed
- `npx tsc --noEmit` passed
- `npm run build` is still blocked by an environment-level Vite/DrvFs module-resolution failure under `node_modules`, not by a v2 route/type error

## Honest Remaining Gap

- Phase 4 route/runtime integration is landed and verified at the backend plus frontend type/lint layer.
- Production build is not yet clean because Vite still hits an environment-specific `node_modules` resolution failure on this Windows-mounted workspace.
- No route-level fallback to v1 was added to mask this issue.
