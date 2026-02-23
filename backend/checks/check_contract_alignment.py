from __future__ import annotations

from pathlib import Path
import re
import sys


FRONTEND_API_PATH = Path("frontend/src/services/api.ts")
COMPOSE_PATH = Path("docker-compose.yml")
ENV_EXAMPLE_PATH = Path(".env.example")
KNOWLEDGE_MODEL_PATH = Path("backend/app/models/knowledge.py")
LITERATURE_MODEL_PATH = Path("backend/app/models/literature.py")
STATUS_MIGRATION_PATH = Path("backend/alembic/versions/017_status_normalization_and_conflict_cleanup.py")

EXPECTED_TASK_STATUSES = {
    "pending",
    "running",
    "completed",
    "failed",
    "timeout",
    "cancelled",
}

EXPECTED_NORMALIZE_INPUT_TOKENS = {
    "processing",
    "running",
    "ready",
    "success",
    "done",
    "timeout",
    "cancelled",
    "canceled",
    "failed",
    "error",
    "pending",
    "queued",
}

EXPECTED_API_BASE_URL = "http://localhost:8888"
EXPECTED_WS_BASE_URL = "ws://localhost:8888"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_literal_union_members(block: str) -> set[str]:
    return set(re.findall(r"'([^'\n]+)'", block))


def extract_task_status_members(api_text: str) -> set[str]:
    match = re.search(
        r"export type TaskStatus\s*=\s*(?P<body>[\s\S]*?)\n\n",
        api_text,
        re.MULTILINE,
    )
    if not match:
        return set()
    return _extract_literal_union_members(match.group("body"))


def extract_normalize_task_status_tokens(api_text: str) -> set[str]:
    match = re.search(
        r"export const normalizeTaskStatus[\s\S]*?=>\s*\{(?P<body>[\s\S]*?)\n\}",
        api_text,
        re.MULTILINE,
    )
    if not match:
        return set()
    return _extract_literal_union_members(match.group("body"))


def extract_normalize_task_status_returns(api_text: str) -> set[str]:
    match = re.search(
        r"export const normalizeTaskStatus[\s\S]*?=>\s*\{(?P<body>[\s\S]*?)\n\}",
        api_text,
        re.MULTILINE,
    )
    if not match:
        return set()
    return set(re.findall(r"return\s+'([^']+)'", match.group("body")))


def extract_api_base_default(api_text: str) -> str | None:
    match = re.search(
        r"const API_BASE_URL = [^\n]*\|\| '([^']+)'",
        api_text,
    )
    return match.group(1) if match else None


def extract_compose_vite_default(compose_text: str, key: str) -> str | None:
    match = re.search(
        rf"{re.escape(key)}=\$\{{{re.escape(key)}:-([^}}]+)\}}",
        compose_text,
    )
    return match.group(1) if match else None


def extract_env_example_value(env_text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.+)$", env_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def extract_enum_members_from_class(model_text: str, class_name: str) -> set[str]:
    match = re.search(
        rf"class {re.escape(class_name)}\s*\(\s*str\s*,\s*enum\.Enum\s*\)\s*:(?P<body>[\s\S]*?)(?:\nclass |\Z)",
        model_text,
        re.MULTILINE,
    )
    if not match:
        return set()
    body = match.group("body")
    return set(re.findall(r"^\s*[A-Z_]+\s*=\s*\"([a-z_]+)\"", body, re.MULTILINE))


def extract_case_targets(migration_text: str, table_name: str) -> set[str]:
    match = re.search(
        rf"UPDATE\s+{re.escape(table_name)}\s+SET\s+status\s*=\s*CASE(?P<body>[\s\S]*?)END",
        migration_text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return set()
    body = match.group("body")
    then_targets = set(re.findall(r"THEN\s+'([a-z_]+)'", body, re.IGNORECASE))
    else_targets = set(re.findall(r"ELSE\s+'([a-z_]+)'", body, re.IGNORECASE))
    return then_targets | else_targets


def main() -> int:
    violations: list[str] = []

    for required_path in (
        FRONTEND_API_PATH,
        COMPOSE_PATH,
        ENV_EXAMPLE_PATH,
        KNOWLEDGE_MODEL_PATH,
        LITERATURE_MODEL_PATH,
        STATUS_MIGRATION_PATH,
    ):
        if not required_path.exists():
            violations.append(f"[missing] {required_path.as_posix()}")

    if violations:
        print("Contract alignment guard failed:")
        for item in violations:
            print(f" - {item}")
        return 1

    frontend_api_text = _read_text(FRONTEND_API_PATH)
    compose_text = _read_text(COMPOSE_PATH)
    env_example_text = _read_text(ENV_EXAMPLE_PATH)
    knowledge_model_text = _read_text(KNOWLEDGE_MODEL_PATH)
    literature_model_text = _read_text(LITERATURE_MODEL_PATH)
    status_migration_text = _read_text(STATUS_MIGRATION_PATH)

    task_status_members = extract_task_status_members(frontend_api_text)
    missing_task_statuses = sorted(EXPECTED_TASK_STATUSES - task_status_members)
    if missing_task_statuses:
        violations.append(
            f"[task_status] missing statuses in frontend/src/services/api.ts: {missing_task_statuses}"
        )

    normalize_tokens = extract_normalize_task_status_tokens(frontend_api_text)
    missing_normalize_tokens = sorted(EXPECTED_NORMALIZE_INPUT_TOKENS - normalize_tokens)
    if missing_normalize_tokens:
        violations.append(
            f"[normalize_tokens] missing normalize aliases in frontend/src/services/api.ts: {missing_normalize_tokens}"
        )

    normalize_returns = extract_normalize_task_status_returns(frontend_api_text)
    missing_normalize_returns = sorted(EXPECTED_TASK_STATUSES - normalize_returns)
    if missing_normalize_returns:
        violations.append(
            f"[normalize_return] missing normalize return statuses in frontend/src/services/api.ts: {missing_normalize_returns}"
        )

    document_status_members = extract_enum_members_from_class(knowledge_model_text, "DocumentStatus")
    if not document_status_members:
        violations.append("[document_status] failed to extract DocumentStatus from backend/app/models/knowledge.py")
    else:
        unmapped_document_statuses = sorted(document_status_members - normalize_tokens)
        if unmapped_document_statuses:
            violations.append(
                f"[document_status] backend DocumentStatus values missing in normalizeTaskStatus aliases: {unmapped_document_statuses}"
            )

    link_status_members = extract_enum_members_from_class(literature_model_text, "KnowledgeLinkStatus")
    if not link_status_members:
        violations.append("[link_status] failed to extract KnowledgeLinkStatus from backend/app/models/literature.py")
    else:
        unmapped_link_statuses = sorted(link_status_members - normalize_tokens)
        if unmapped_link_statuses:
            violations.append(
                f"[link_status] backend KnowledgeLinkStatus values missing in normalizeTaskStatus aliases: {unmapped_link_statuses}"
            )

    migration_document_targets = extract_case_targets(status_migration_text, "documents")
    if not migration_document_targets:
        violations.append("[migration_documents] failed to parse status targets for documents")
    elif document_status_members and not migration_document_targets.issubset(document_status_members):
        invalid_targets = sorted(migration_document_targets - document_status_members)
        violations.append(
            f"[migration_documents] migration targets not in DocumentStatus enum: {invalid_targets}"
        )

    migration_link_targets = extract_case_targets(status_migration_text, "paper_knowledge_links")
    if not migration_link_targets:
        violations.append("[migration_links] failed to parse status targets for paper_knowledge_links")
    elif link_status_members and not migration_link_targets.issubset(link_status_members):
        invalid_targets = sorted(migration_link_targets - link_status_members)
        violations.append(
            f"[migration_links] migration targets not in KnowledgeLinkStatus enum: {invalid_targets}"
        )

    api_default = extract_api_base_default(frontend_api_text)
    if api_default != EXPECTED_API_BASE_URL:
        violations.append(
            f"[api_base_default] frontend/src/services/api.ts expected {EXPECTED_API_BASE_URL}, got {api_default}"
        )

    compose_api_default = extract_compose_vite_default(compose_text, "VITE_API_BASE_URL")
    if compose_api_default != EXPECTED_API_BASE_URL:
        violations.append(
            f"[compose_api_default] docker-compose.yml expected {EXPECTED_API_BASE_URL}, got {compose_api_default}"
        )

    env_api_default = extract_env_example_value(env_example_text, "VITE_API_BASE_URL")
    if env_api_default != EXPECTED_API_BASE_URL:
        violations.append(
            f"[env_api_default] .env.example expected {EXPECTED_API_BASE_URL}, got {env_api_default}"
        )

    compose_ws_default = extract_compose_vite_default(compose_text, "VITE_WS_BASE_URL")
    if compose_ws_default != EXPECTED_WS_BASE_URL:
        violations.append(
            f"[compose_ws_default] docker-compose.yml expected {EXPECTED_WS_BASE_URL}, got {compose_ws_default}"
        )

    env_ws_default = extract_env_example_value(env_example_text, "VITE_WS_BASE_URL")
    if env_ws_default != EXPECTED_WS_BASE_URL:
        violations.append(
            f"[env_ws_default] .env.example expected {EXPECTED_WS_BASE_URL}, got {env_ws_default}"
        )

    if violations:
        print("Contract alignment guard failed:")
        for item in violations:
            print(f" - {item}")
        return 1

    print("Contract alignment guard passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
