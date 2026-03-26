from __future__ import annotations

import json
from pathlib import Path

from app.services.local_structured_pdf.external_holdout_builder import (
    build_eval_suite_entry,
    build_external_holdout,
    discover_paired_documents,
    upsert_suite_manifest_entry,
)


def test_discover_paired_documents_matches_relative_pdf_and_markdown_paths(tmp_path: Path):
    source_root = tmp_path / "readoc"
    (source_root / "arxiv").mkdir(parents=True)
    (source_root / "github").mkdir(parents=True)

    (source_root / "arxiv" / "paper_a.pdf").write_bytes(b"%PDF-1.4")
    (source_root / "arxiv" / "paper_a.md").write_text("# A", encoding="utf-8")
    (source_root / "github" / "doc_b.pdf").write_bytes(b"%PDF-1.4")
    (source_root / "github" / "doc_b.md").write_text("# B", encoding="utf-8")
    (source_root / "github" / "orphan.pdf").write_bytes(b"%PDF-1.4")

    pairs = discover_paired_documents(source_root=source_root)

    assert [pair.doc_id for pair in pairs] == ["arxiv__paper_a", "github__doc_b"]
    assert [pair.subset for pair in pairs] == ["arxiv", "github"]


def test_build_external_holdout_balances_subsets_and_writes_manifest(tmp_path: Path):
    source_root = tmp_path / "readoc"
    output_root = tmp_path / "holdout"
    for subset in ["arxiv", "github"]:
        (source_root / subset).mkdir(parents=True)
        for index in range(3):
            stem = f"doc_{index+1}"
            (source_root / subset / f"{stem}.pdf").write_bytes(b"%PDF-1.4")
            (source_root / subset / f"{stem}.md").write_text(f"# {subset} {index}", encoding="utf-8")

    manifest = build_external_holdout(
        source_root=source_root,
        output_root=output_root,
        limit=4,
        subset="all",
        seed=7,
        balance_by_subset=True,
    )

    assert int(manifest["document_count"]) == 4
    assert len(list((output_root / "pdfs").glob("*.pdf"))) == 4
    loaded_manifest = json.loads((output_root / "holdout_manifest.json").read_text(encoding="utf-8"))
    subsets = [item["subset"] for item in loaded_manifest["documents"]]
    assert subsets.count("arxiv") == 2
    assert subsets.count("github") == 2


def test_upsert_suite_manifest_entry_replaces_matching_suite(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "local_structured_pdf_external_suites_v1",
                "generated_at": "",
                "suites": [
                    {
                        "name": "readoc_holdout",
                        "input_dir": "tmp/external/old/pdfs",
                        "ground_truth_dir": "tmp/external/old/markdown",
                        "enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    updated = upsert_suite_manifest_entry(
        manifest_path=manifest_path,
        suite_entry={
            "name": "readoc_holdout",
            "input_dir": "tmp/external/readoc/pdfs",
            "ground_truth_dir": "tmp/external/readoc/markdown",
            "enabled": True,
        },
    )

    assert len(updated["suites"]) == 1
    assert updated["suites"][0]["enabled"] is True
    assert updated["suites"][0]["input_dir"] == "tmp/external/readoc/pdfs"


def test_build_eval_suite_entry_uses_backend_relative_paths(tmp_path: Path):
    project_root = tmp_path / "backend"
    output_root = project_root / "tmp" / "external" / "readoc"
    (output_root / "pdfs").mkdir(parents=True)
    (output_root / "markdown").mkdir(parents=True)

    entry = build_eval_suite_entry(
        project_root=project_root,
        output_root=output_root,
        suite_name="readoc_holdout",
        description="READoc holdout",
        enabled=True,
    )

    assert entry == {
        "name": "readoc_holdout",
        "description": "READoc holdout",
        "input_dir": "tmp/external/readoc/pdfs",
        "ground_truth_dir": "tmp/external/readoc/markdown",
        "enabled": True,
    }
