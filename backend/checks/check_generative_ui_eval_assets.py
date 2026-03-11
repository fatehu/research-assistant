import json
from pathlib import Path


FIXTURE_PATH = Path("backend/tests/fixtures/generative_ui/golden_pages.json")
REQUIRED_CATEGORIES = {"figure-heavy", "methods-heavy", "concept-heavy"}


def main() -> int:
    if not FIXTURE_PATH.exists():
        print(f"Generative UI eval asset guard failed: missing {FIXTURE_PATH}")
        return 1

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    goldens = list(payload.get("goldens") or [])
    if not goldens:
        print("Generative UI eval asset guard failed: golden set is empty")
        return 1

    categories = set()
    errors: list[str] = []
    for index, row in enumerate(goldens):
        if not isinstance(row, dict):
            errors.append(f"golden[{index}] is not an object")
            continue
        golden_id = str(row.get("golden_id") or "").strip()
        if not golden_id:
            errors.append(f"golden[{index}] missing golden_id")
        source_kind = str(row.get("source_kind") or "").strip()
        if source_kind not in {"paper_page", "contract_fixture"}:
            errors.append(f"golden[{golden_id or index}] has invalid source_kind={source_kind!r}")
        category = str(row.get("category") or "").strip()
        if not category:
            errors.append(f"golden[{golden_id or index}] missing category")
        else:
            categories.add(category)
        required_labels = row.get("required_labels") or {}
        if not isinstance(required_labels, dict) or not required_labels:
            errors.append(f"golden[{golden_id or index}] missing required_labels")
        if source_kind == "paper_page":
            if int(row.get("paper_id") or 0) <= 0:
                errors.append(f"golden[{golden_id or index}] paper_page missing paper_id")
            if int(row.get("page") or 0) <= 0:
                errors.append(f"golden[{golden_id or index}] paper_page missing page")
        if source_kind == "contract_fixture" and not str(row.get("scenario") or "").strip():
            errors.append(f"golden[{golden_id or index}] contract_fixture missing scenario")

    missing_categories = REQUIRED_CATEGORIES - categories
    if missing_categories:
        errors.append(f"missing required categories: {sorted(missing_categories)}")

    if errors:
        print("Generative UI eval asset guard failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Generative UI eval asset guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
