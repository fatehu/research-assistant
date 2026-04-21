#!/usr/bin/env python3
"""Check the paper workflow runtime environment without importing heavy ML modules."""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import shutil
import sys
from typing import Any


DEFAULT_ML_PACKAGES = [
    ("numpy", "numpy"),
    ("pandas", "pandas"),
    ("scipy", "scipy"),
    ("scikit-learn", "sklearn"),
    ("h5py", "h5py"),
    ("matplotlib", "matplotlib"),
    ("seaborn", "seaborn"),
    ("torch", "torch"),
    ("schedulefree", "schedulefree"),
    ("papermill", "papermill"),
    ("jupyter-repo2docker", "repo2docker"),
]

DEFAULT_COMMANDS = ["python", "python3", "papermill", "repo2docker", "devcontainer", "docker"]


def _package_status(distribution_name: str, import_name: str) -> dict[str, Any]:
    installed = importlib.util.find_spec(import_name) is not None
    try:
        version = importlib_metadata.version(distribution_name) if installed else None
    except importlib_metadata.PackageNotFoundError:
        version = None
    return {
        "distribution": distribution_name,
        "import_name": import_name,
        "installed": installed,
        "version": version,
    }


def build_report() -> dict[str, Any]:
    packages = {
        distribution_name: _package_status(distribution_name, import_name)
        for distribution_name, import_name in DEFAULT_ML_PACKAGES
    }
    commands = {name: {"available": bool(shutil.which(name)), "path": shutil.which(name)} for name in DEFAULT_COMMANDS}
    return {
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "packages": packages,
        "commands": commands,
        "cache_env": {
            "PIP_CACHE_DIR": os.getenv("PIP_CACHE_DIR", ""),
            "HF_HOME": os.getenv("HF_HOME", ""),
            "HUGGINGFACE_HUB_CACHE": os.getenv("HUGGINGFACE_HUB_CACHE", ""),
            "XDG_CACHE_HOME": os.getenv("XDG_CACHE_HOME", ""),
        },
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"python: {report['python']['version']} ({report['python']['executable']})")
    print("packages:")
    for name, payload in report["packages"].items():
        status = "ok" if payload["installed"] else "missing"
        version = payload["version"] or "-"
        print(f"  - {name}: {status} {version}")
    print("commands:")
    for name, payload in report["commands"].items():
        status = "ok" if payload["available"] else "missing"
        print(f"  - {name}: {status} {payload['path'] or '-'}")
    print("cache:")
    for name, value in report["cache_env"].items():
        print(f"  - {name}: {value or '-'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--require-ml-defaults", action="store_true")
    parser.add_argument("--require", action="append", default=[], help="Distribution name to require.")
    args = parser.parse_args()

    report = build_report()
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)

    required = set(args.require or [])
    if args.require_ml_defaults:
        required.update(name for name, _ in DEFAULT_ML_PACKAGES)
    missing = [name for name in sorted(required) if not dict(report["packages"].get(name) or {}).get("installed")]
    if missing:
        print(f"missing required packages: {', '.join(missing)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
