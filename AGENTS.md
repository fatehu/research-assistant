# Repository Guidelines

## Project Structure & Module Organization
`backend/` contains the FastAPI app, with APIs under `backend/app/api`, business logic in `backend/app/services`, SQLAlchemy models in `backend/app/models`, and async tests in `backend/tests`. `frontend/` is a Vite + React + TypeScript app; pages live in `frontend/src/pages`, shared UI in `frontend/src/components`, and state stores in `frontend/src/stores`. Cross-cutting docs and rollout notes are in `docs/`. End-to-end and smoke scripts live in `e2e/` and the root `acceptance_tests.ps1`.

## Build, Test, and Development Commands
Use Docker for the full stack:
```bash
docker compose up -d --build backend frontend
docker compose ps
```
Frontend local workflow:
```bash
cd frontend
npm ci
npm run dev
npm run lint
npm run build
```
Backend local checks:
```bash
python -m pytest backend/tests -q
python backend/checks/check_no_new_broad_excepts.py
python backend/checks/check_contract_alignment.py
```
On Windows, the documented test venv is `.\.venv-ragtest\Scripts\python.exe`.

## Coding Style & Naming Conventions
Python uses 4-space indentation, `snake_case` for modules/functions, and `PascalCase` for classes and Pydantic models. Keep API routers thin and place non-trivial logic in `backend/app/services`. TypeScript/React follows the checked-in ESLint config in `frontend/.eslintrc.cjs`; use `PascalCase` for page/component files such as `PaperReaderPage.tsx`, `camelCase` for hooks and helpers, and colocate page-specific styles beside the page. Backend formatting tools listed in `backend/requirements.txt` are `black` and `isort`.

## Testing Guidelines
Backend tests use `pytest` with `pytest-asyncio`; name files `test_*.py` and keep feature-specific coverage close to the service or API under test. Run focused regressions first, for example `python -m pytest backend/tests/test_literature_reader_api.py -q`, then broader suites. Frontend changes must pass `npm run lint` and `npm run build`. Use the PowerShell smoke scripts in `e2e/` when changing auth, role routing, upload, or MCP flows.

## Commit & Pull Request Guidelines
Recent history follows Conventional Commit style such as `feat(reader): ...`, `fix(admin): ...`, `ci(smoke): ...`, and `docs(readme): ...`. Keep scopes specific to the subsystem you changed. PRs should describe user-visible behavior, list verification commands you ran, link the related issue or task, and include screenshots for UI changes in `frontend/src/pages/literature`, chat, dashboard, or settings surfaces.

## Security & Configuration Tips
Copy `.env.example` to `.env` and keep secrets out of git. Prefer `AUTO_CREATE_TABLES=false` outside local development and use Alembic for schema changes. Treat `.env`, MCP credentials, and provider API keys as local-only configuration.
