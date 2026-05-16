from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import random
import shutil
from typing import Any, Iterable


@dataclass(frozen=True)
class ExternalPdfMarkdownPair:
    doc_id: str
    source_key: str
    subset: str
    pdf_path: Path
    markdown_path: Path
    pdf_relative_path: str
    markdown_relative_path: str


def discover_paired_documents(*, source_root: Path) -> list[ExternalPdfMarkdownPair]:
    source_root = Path(source_root).resolve()
    pdf_map = _collect_relative_map(root=source_root, suffix=".pdf")
    markdown_map = _collect_relative_map(root=source_root, suffix=".md")

    pairs: list[ExternalPdfMarkdownPair] = []
    for source_key in sorted(set(pdf_map) & set(markdown_map)):
        pdf_path = pdf_map[source_key]
        markdown_path = markdown_map[source_key]
        relative_path = Path(source_key)
        pairs.append(
            ExternalPdfMarkdownPair(
                doc_id=_sanitize_doc_id(relative_path),
                source_key=source_key,
                subset=_infer_subset(relative_path),
                pdf_path=pdf_path,
                markdown_path=markdown_path,
                pdf_relative_path=pdf_path.relative_to(source_root).as_posix(),
                markdown_relative_path=markdown_path.relative_to(source_root).as_posix(),
            )
        )
    return pairs


def build_external_holdout(
    *,
    source_root: Path,
    output_root: Path,
    limit: int | None = None,
    subset: str = "all",
    seed: int = 42,
    balance_by_subset: bool = True,
) -> dict[str, object]:
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    pdf_dir = output_root / "pdfs"
    markdown_dir = output_root / "markdown"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)

    discovered = discover_paired_documents(source_root=source_root)
    filtered = _filter_pairs(pairs=discovered, subset=subset)
    selected = _select_pairs(
        pairs=filtered,
        limit=int(limit) if limit else None,
        seed=int(seed),
        balance_by_subset=bool(balance_by_subset),
    )

    for pair in selected:
        shutil.copy2(pair.pdf_path, pdf_dir / f"{pair.doc_id}.pdf")
        shutil.copy2(pair.markdown_path, markdown_dir / f"{pair.doc_id}.md")

    manifest = {
        "source_root": str(source_root),
        "output_root": str(output_root),
        "document_count": len(selected),
        "subset_filter": subset,
        "seed": int(seed),
        "balance_by_subset": bool(balance_by_subset),
        "documents": [
            {
                **asdict(pair),
                "pdf_path": str(pair.pdf_path),
                "markdown_path": str(pair.markdown_path),
            }
            for pair in selected
        ],
    }
    (output_root / "holdout_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def build_eval_suite_entry(
    *,
    project_root: Path,
    output_root: Path,
    suite_name: str,
    description: str,
    enabled: bool = False,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    output_root = Path(output_root).resolve()
    return {
        "name": str(suite_name).strip(),
        "description": str(description).strip(),
        "input_dir": (output_root / "pdfs").relative_to(project_root).as_posix(),
        "ground_truth_dir": (output_root / "markdown").relative_to(project_root).as_posix(),
        "enabled": bool(enabled),
    }


def upsert_suite_manifest_entry(
    *,
    manifest_path: Path,
    suite_entry: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    if manifest_path.is_file():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        data = {
            "manifest_version": "local_structured_pdf_external_suites_v1",
            "generated_at": "",
            "suites": [],
        }

    suites = list(data.get("suites") or [])
    target_name = str(suite_entry.get("name") or "").strip()
    if not target_name:
        raise ValueError("suite_entry.name is required")

    replaced = False
    normalized: list[dict[str, Any]] = []
    for item in suites:
        if str(item.get("name") or "").strip() == target_name:
            normalized.append(dict(suite_entry))
            replaced = True
        else:
            normalized.append(item)
    if not replaced:
        normalized.append(dict(suite_entry))

    data["generated_at"] = _utc_timestamp()
    data["suites"] = normalized
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def _collect_relative_map(*, root: Path, suffix: str) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for path in sorted(root.rglob(f"*{suffix}")):
        if not path.is_file():
            continue
        key = path.relative_to(root).with_suffix("").as_posix()
        mapping.setdefault(key, path)
    return mapping


def _sanitize_doc_id(relative_path: Path) -> str:
    return "__".join(part.strip().replace(" ", "_") for part in relative_path.parts if part.strip())


def _infer_subset(relative_path: Path) -> str:
    lowered_parts = [part.lower() for part in relative_path.parts]
    if any("arxiv" in part for part in lowered_parts):
        return "arxiv"
    if any("github" in part for part in lowered_parts):
        return "github"
    if any("zenodo" in part for part in lowered_parts):
        return "zenodo"
    return "unknown"


def _filter_pairs(
    *,
    pairs: Iterable[ExternalPdfMarkdownPair],
    subset: str,
) -> list[ExternalPdfMarkdownPair]:
    normalized_subset = str(subset or "all").strip().lower()
    if normalized_subset in {"", "all"}:
        return list(pairs)
    return [pair for pair in pairs if pair.subset == normalized_subset]


def _select_pairs(
    *,
    pairs: list[ExternalPdfMarkdownPair],
    limit: int | None,
    seed: int,
    balance_by_subset: bool,
) -> list[ExternalPdfMarkdownPair]:
    if limit is None or limit <= 0 or len(pairs) <= limit:
        return list(pairs)

    if not balance_by_subset:
        rng = random.Random(seed)
        shuffled = list(pairs)
        rng.shuffle(shuffled)
        return sorted(shuffled[:limit], key=lambda item: item.doc_id)

    groups: dict[str, list[ExternalPdfMarkdownPair]] = {}
    for pair in pairs:
        groups.setdefault(pair.subset, []).append(pair)

    rng = random.Random(seed)
    ordered_groups = sorted(groups.items(), key=lambda item: item[0])
    for _, items in ordered_groups:
        rng.shuffle(items)

    selected: list[ExternalPdfMarkdownPair] = []
    while len(selected) < limit and any(items for _, items in ordered_groups):
        for _, items in ordered_groups:
            if not items or len(selected) >= limit:
                continue
            selected.append(items.pop())
    return sorted(selected, key=lambda item: item.doc_id)


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
