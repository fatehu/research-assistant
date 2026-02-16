from __future__ import annotations

from pathlib import Path
import sys


BASELINE = {
    "backend/app/api/codelab.py": 11,
    "backend/app/api/knowledge.py": 9,
    "backend/app/services/agent_tools_impl/registry.py": 20,
    "backend/app/services/react_agent.py": 19,
    "backend/app/services/mcp/client.py": 5,
    "backend/app/services/codelab_executor.py": 2,
}


def count_broad_except(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if "except Exception" in line)


def main() -> int:
    violations = []
    for rel, baseline in BASELINE.items():
        p = Path(rel)
        if not p.exists():
            violations.append(f"[missing] {rel}")
            continue
        current = count_broad_except(p)
        if current > baseline:
            violations.append(f"[exceeded] {rel}: current={current}, baseline={baseline}")

    if violations:
        print("Broad exception guard failed:")
        for item in violations:
            print(f" - {item}")
        return 1

    print("Broad exception guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

