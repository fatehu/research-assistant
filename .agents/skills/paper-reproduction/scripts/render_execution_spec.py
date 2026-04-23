#!/usr/bin/env python3
"""Render one concrete execution_spec JSON from explicit inputs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


RUNTIME_TYPES = {
    "devcontainer",
    "docker_compose",
    "dockerfile",
    "repo2docker",
    "papermill",
    "plain-python",
}

ENTRYPOINT_TYPES = {
    "generated_python",
    "notebook",
    "repo_script",
}


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
    return (text or "execution")[:80]


def _relative_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError(f"path must be workspace-relative: {value}")
    parts = [part for part in raw.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"path must not escape workspace: {value}")
    return "/".join(parts)


def _json_list(value: str, *, name: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) and item for item in parsed):
        raise ValueError(f"{name} must be a non-empty JSON string array")
    return parsed


def _json_object(value: str, *, name: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _json_optional_list(value: str | None, *, name: str) -> list[str]:
    if value in (None, ""):
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item for item in parsed):
        raise ValueError(f"{name} must be a JSON string array")
    return parsed


def build_spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.runtime_type not in RUNTIME_TYPES:
        raise ValueError(f"runtime_type must be one of {sorted(RUNTIME_TYPES)}")
    execution_id = _safe_slug(args.execution_id or args.draft_id or args.label or args.runtime_type)
    spec: dict[str, Any] = {
        "schema_version": "project_execution_spec_v1",
        "execution_id": execution_id,
        "draft_id": args.draft_id or execution_id,
        "runtime_type": args.runtime_type,
        "repo_root_relative_path": _relative_path(args.repo_root),
        "expected_outputs": list(args.expected_output or []),
        "artifact_globs": [_relative_path(item) for item in list(args.artifact_glob or [])],
        "evidence_files": [_relative_path(item) for item in list(args.evidence_file or [])],
        "blockers": list(args.blocker or []),
    }

    if args.entrypoint_type and args.command_json:
        raise ValueError("--entrypoint-type cannot be combined with --command-json")

    if args.entrypoint_type:
        if args.entrypoint_type not in ENTRYPOINT_TYPES:
            raise ValueError(f"entrypoint_type must be one of {sorted(ENTRYPOINT_TYPES)}")
        entrypoint_path = args.entrypoint_path or args.input_notebook or ""
        if args.entrypoint_type == "notebook":
            if not entrypoint_path:
                raise ValueError("notebook execution_intent requires --entrypoint-path or --input-notebook")
        else:
            if not entrypoint_path and args.entrypoint_type != "generated_python":
                raise ValueError("execution_intent requires --entrypoint-path for repo_script")
            if args.entrypoint_type == "repo_script" and str(entrypoint_path).strip().endswith(".sh"):
                raise ValueError(
                    "repo_script execution_intent is for Python repo files; "
                    "for executable shell entrypoints use --command-json '[\"./classification-results.sh\"]'"
                )

        intent: dict[str, Any] = {
            "runtime_type": args.runtime_type,
            "entrypoint_type": args.entrypoint_type,
            "cwd_mode": args.cwd_mode,
            "args": _json_optional_list(args.args_json, name="args_json"),
        }
        if entrypoint_path:
            intent["entrypoint_path"] = _relative_path(entrypoint_path)
        if args.generated_program_name:
            intent["generated_program_name"] = args.generated_program_name.strip()
        spec["execution_intent"] = {key: value for key, value in intent.items() if value not in ("", [], None)}
        if args.runtime_type == "papermill":
            spec["parameters"] = _json_object(args.parameters_json or "{}", name="parameters_json")
    elif args.runtime_type == "papermill":
        if not args.input_notebook:
            raise ValueError("papermill execution requires --input-notebook or --entrypoint-type notebook")
        spec["cwd"] = _relative_path(args.cwd)
        spec["input_notebook"] = _relative_path(args.input_notebook)
        spec["parameters"] = _json_object(args.parameters_json or "{}", name="parameters_json")
    else:
        if not args.command_json:
            raise ValueError(f"{args.runtime_type} execution requires --command-json")
        spec["cwd"] = _relative_path(args.cwd)
        spec["command"] = _json_list(args.command_json, name="command_json")
    if args.service:
        spec["service"] = args.service
    if args.dockerfile:
        spec["dockerfile"] = _relative_path(args.dockerfile)
    if args.compose_file:
        spec["compose_file"] = _relative_path(args.compose_file)
    if args.external_dependency_json:
        spec["external_dependencies"] = [
            _json_object(item, name="external_dependency_json")
            for item in args.external_dependency_json
        ]
    if args.preflight_check_json:
        spec["preflight_checks"] = [_json_object(item, name="preflight_check_json") for item in args.preflight_check_json]
    return {key: value for key, value in spec.items() if value not in ("", [], {}, None)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id")
    parser.add_argument("--draft-id")
    parser.add_argument("--label")
    parser.add_argument("--runtime-type", required=True, choices=sorted(RUNTIME_TYPES))
    parser.add_argument("--cwd", default="repo/source")
    parser.add_argument("--repo-root", default="repo/source")
    parser.add_argument("--command-json", help='Legacy direct argv example: ["python","train.py","--epochs","1"] or ["./classification-results.sh"]')
    parser.add_argument("--entrypoint-type", choices=sorted(ENTRYPOINT_TYPES), help="Preferred structured execution_intent entrypoint type.")
    parser.add_argument("--entrypoint-path", help="Repo-relative path for execution_intent, for example train.py or demo.ipynb.")
    parser.add_argument("--generated-program-name", help="Generated program name for execution_intent.entrypoint_type=generated_python.")
    parser.add_argument("--cwd-mode", default="repo_root", help="execution_intent cwd_mode, for example repo_root or execution_root.")
    parser.add_argument("--args-json", help='JSON string array for execution_intent args, for example ["--epochs","1"]')
    parser.add_argument("--input-notebook")
    parser.add_argument("--parameters-json")
    parser.add_argument("--service")
    parser.add_argument("--dockerfile")
    parser.add_argument("--compose-file")
    parser.add_argument("--expected-output", action="append")
    parser.add_argument("--artifact-glob", action="append")
    parser.add_argument("--evidence-file", action="append")
    parser.add_argument("--blocker", action="append")
    parser.add_argument("--external-dependency-json", action="append")
    parser.add_argument(
        "--preflight-check-json",
        action="append",
        help='Repeat with JSON objects, for example \'{"name":"check_python","required":true,"status":"passed"}\'',
    )
    args = parser.parse_args()

    try:
        spec = build_spec(args)
    except Exception as exc:
        print(f"render_execution_spec failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(spec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
