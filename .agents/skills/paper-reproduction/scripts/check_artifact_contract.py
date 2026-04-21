#!/usr/bin/env python3
"""Validate top-level paper skill artifact contracts with lightweight checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_FIELDS = {
    "implementation_spec": [
        "schema_version",
        "paper_id",
        "project_id",
        "workspace_id",
        "mode",
        "baseline",
        "repo_plan",
        "data_plan",
        "tuning_plan",
        "readiness",
        "blockers",
        "next_actions",
        "evidence_log",
    ],
    "run_drafts": [
        "schema_version",
        "paper_id",
        "project_id",
        "workspace_id",
        "drafts",
    ],
    "execution_spec": [
        "execution_id",
        "runtime_type",
        "evidence_files",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=sorted(REQUIRED_FIELDS))
    parser.add_argument("--file", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    missing = [field for field in REQUIRED_FIELDS[args.kind] if field not in payload]
    if missing:
        print(json.dumps({"ok": False, "missing": missing}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "kind": args.kind, "file": args.file}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
