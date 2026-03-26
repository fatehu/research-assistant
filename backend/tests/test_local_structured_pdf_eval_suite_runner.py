from __future__ import annotations

import json
from pathlib import Path

from app.services.local_structured_pdf.eval_suite_runner import (
    LocalPdfEvalSuite,
    _collect_pdf_paths,
    _prepare_ground_truth_subset,
    load_eval_suites,
    run_eval_suite,
    run_eval_suites,
    select_eval_suites,
)


def test_load_eval_suites_resolves_relative_paths_and_skips_disabled(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    (tmp_path / "suite_a").mkdir()
    (tmp_path / "gt_a").mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "name": "suite_a",
                        "input_dir": "suite_a",
                        "ground_truth_dir": "gt_a",
                        "enabled": True,
                    },
                    {
                        "name": "disabled_suite",
                        "input_dir": "suite_b",
                        "enabled": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    suites = load_eval_suites(manifest_path=manifest_path, project_root=tmp_path)

    assert [suite.name for suite in suites] == ["suite_a"]
    assert suites[0].input_dir == (tmp_path / "suite_a").resolve()
    assert suites[0].ground_truth_dir == (tmp_path / "gt_a").resolve()


def test_collect_pdf_paths_honors_doc_ids(tmp_path: Path):
    input_dir = tmp_path / "pdfs"
    input_dir.mkdir()
    for doc_id in ["b_doc", "a_doc", "c_doc"]:
        (input_dir / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4")

    suite = LocalPdfEvalSuite(
        name="subset",
        input_dir=input_dir,
        doc_ids=("c_doc", "a_doc", "missing_doc"),
    )

    paths = _collect_pdf_paths(suite=suite)

    assert [path.name for path in paths] == ["c_doc.pdf", "a_doc.pdf"]


def test_run_eval_suites_writes_matrix_summary(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    def _fake_run_eval_suite(**kwargs):
        suite = kwargs["suite"]
        calls.append(suite.name)
        return {
            "name": suite.name,
            "document_count": 2,
            "elapsed_per_doc": 1.5,
        }

    monkeypatch.setattr(
        "app.services.local_structured_pdf.eval_suite_runner.run_eval_suite",
        _fake_run_eval_suite,
    )

    output_root = tmp_path / "results"
    matrix = run_eval_suites(
        suites=[
            LocalPdfEvalSuite(name="suite_a", input_dir=tmp_path / "a"),
            LocalPdfEvalSuite(name="suite_b", input_dir=tmp_path / "b"),
        ],
        project_root=tmp_path,
        output_root=output_root,
        engine_name="local-structured-pdf",
        heuristic_profile="balanced",
        evaluator_python=None,
        evaluator_script=None,
    )

    summary_path = output_root / "balanced_matrix_summary.json"
    assert calls == ["suite_a", "suite_b"]
    assert matrix["suite_count"] == 2
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert [suite["name"] for suite in summary["suites"]] == ["suite_a", "suite_b"]


def test_prepare_ground_truth_subset_copies_only_selected_docs(tmp_path: Path):
    ground_truth_dir = tmp_path / "gt"
    suite_root = tmp_path / "suite"
    ground_truth_dir.mkdir()
    (ground_truth_dir / "a_doc.md").write_text("A", encoding="utf-8")
    (ground_truth_dir / "b_doc.md").write_text("B", encoding="utf-8")
    (ground_truth_dir / "c_doc.md").write_text("C", encoding="utf-8")

    input_dir = tmp_path / "pdfs"
    input_dir.mkdir()
    pdf_paths = []
    for doc_id in ["c_doc", "a_doc"]:
        pdf_path = input_dir / f"{doc_id}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        pdf_paths.append(pdf_path)

    suite = LocalPdfEvalSuite(name="subset", input_dir=input_dir, ground_truth_dir=ground_truth_dir)

    target_dir = _prepare_ground_truth_subset(
        suite=suite,
        suite_root=suite_root,
        pdf_paths=pdf_paths,
    )

    assert sorted(path.name for path in target_dir.glob("*.md")) == ["a_doc.md", "c_doc.md"]


def test_select_eval_suites_filters_and_preserves_requested_order(tmp_path: Path):
    suites = [
        LocalPdfEvalSuite(name="suite_a", input_dir=tmp_path / "a"),
        LocalPdfEvalSuite(name="suite_b", input_dir=tmp_path / "b"),
        LocalPdfEvalSuite(name="suite_c", input_dir=tmp_path / "c"),
    ]

    selected = select_eval_suites(
        suites=suites,
        suite_names=("suite_c", "suite_a"),
    )

    assert [suite.name for suite in selected] == ["suite_c", "suite_a"]


def test_select_eval_suites_raises_on_unknown_name(tmp_path: Path):
    suites = [LocalPdfEvalSuite(name="suite_a", input_dir=tmp_path / "a")]

    try:
        select_eval_suites(suites=suites, suite_names=("suite_missing",))
    except ValueError as exc:
        assert "suite_missing" in str(exc)
        assert "suite_a" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("Expected ValueError for unknown suite name")


def test_run_eval_suite_checks_pipeline_runtime_before_export(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "pdfs"
    input_dir.mkdir()
    (input_dir / "demo.pdf").write_bytes(b"%PDF-1.4")
    output_root = tmp_path / "results"

    class _Pipeline:
        def __init__(self, *, heuristic_profile: str):
            self.heuristic_profile = heuristic_profile

        def ensure_runtime_ready(self) -> None:
            raise RuntimeError("missing parser backend")

    monkeypatch.setattr(
        "app.services.local_structured_pdf.eval_suite_runner.LocalStructuredPdfPipeline",
        _Pipeline,
    )

    suite = LocalPdfEvalSuite(name="runtime", input_dir=input_dir)
    try:
        run_eval_suite(
            suite=suite,
            project_root=tmp_path,
            output_root=output_root,
            engine_name="local-structured-pdf",
            heuristic_profile="balanced",
            evaluator_python=None,
            evaluator_script=None,
        )
    except RuntimeError as exc:
        assert "missing parser backend" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("Expected runtime preflight failure")
