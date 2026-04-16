from __future__ import annotations

from pathlib import Path
import re
import sys


BASELINE = {
    "backend/app/api/codelab.py": {"exception": 6, "bare": 5},
    "backend/app/api/knowledge.py": {"exception": 13, "bare": 0},
    "backend/app/services/agent_tools_impl/registry.py": {"exception": 21, "bare": 0},
    "backend/app/services/react_agent.py": {"exception": 37, "bare": 0},
    "backend/app/services/mcp/client.py": {"exception": 3, "bare": 0},
    "backend/app/services/codelab_executor.py": {"exception": 2, "bare": 0},
}

_EXCEPT_EXCEPTION_PATTERN = re.compile(r"except\s+Exception\b")
_BARE_EXCEPT_PATTERN = re.compile(r"^\s*except\s*:\s*(?:#.*)?$")


def count_broad_except(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    exception_count = 0
    bare_count = 0
    for line in text.splitlines():
        if _EXCEPT_EXCEPTION_PATTERN.search(line):
            exception_count += 1
        elif _BARE_EXCEPT_PATTERN.match(line):
            bare_count += 1
    return exception_count, bare_count


def main() -> int:
    violations = []
    for rel, baseline in BASELINE.items():
        p = Path(rel)
        if not p.exists():
            violations.append(f"[missing] {rel}")
            continue
        current_exception, current_bare = count_broad_except(p)
        baseline_exception = int(baseline["exception"])
        baseline_bare = int(baseline["bare"])
        if current_exception > baseline_exception:
            violations.append(
                f"[exceeded:except Exception] {rel}: current={current_exception}, baseline={baseline_exception}"
            )
        if current_bare > baseline_bare:
            violations.append(
                f"[exceeded:except:] {rel}: current={current_bare}, baseline={baseline_bare}"
            )

    if violations:
        print("Broad exception guard failed:")
        for item in violations:
            print(f" - {item}")
        return 1

    print("Broad exception guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
