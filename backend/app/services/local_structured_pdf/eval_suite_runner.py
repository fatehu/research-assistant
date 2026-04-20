from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import platform
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from .markdown_renderer import LocalPdfMarkdownRenderer
from .docling_fast_hybrid_pipeline import LocalStructuredPdfDoclingFastHybridPipeline
from .pipeline import LocalStructuredPdfPipeline


@dataclass(frozen=True)
class LocalPdfEvalSuite:
    name: str
    input_dir: Path
    ground_truth_dir: Path | None = None
    description: str = ""
    doc_ids: tuple[str, ...] = ()


def load_eval_suites(*, manifest_path: Path, project_root: Path) -> list[LocalPdfEvalSuite]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    suites = data.get("suites") or []
    resolved: list[LocalPdfEvalSuite] = []
    for item in suites:
        if not bool(item.get("enabled", True)):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        input_dir = _resolve_path(str(item.get("input_dir") or ""), project_root=project_root)
        ground_truth_value = str(item.get("ground_truth_dir") or "").strip()
        ground_truth_dir = _resolve_path(ground_truth_value, project_root=project_root) if ground_truth_value else None
        doc_ids = tuple(str(value).strip() for value in list(item.get("doc_ids") or []) if str(value).strip())
        resolved.append(
            LocalPdfEvalSuite(
                name=name,
                input_dir=input_dir,
                ground_truth_dir=ground_truth_dir,
                description=str(item.get("description") or "").strip(),
                doc_ids=doc_ids,
            )
        )
    return resolved


def select_eval_suites(
    *,
    suites: list[LocalPdfEvalSuite],
    suite_names: tuple[str, ...] = (),
) -> list[LocalPdfEvalSuite]:
    requested = tuple(str(name).strip() for name in suite_names if str(name).strip())
    if not requested:
        return list(suites or [])

    available = {suite.name: suite for suite in list(suites or [])}
    missing = [name for name in requested if name not in available]
    if missing:
        raise ValueError(
            "Unknown suite name(s): " + ", ".join(missing) + ". "
            + "Available suites: " + ", ".join(sorted(available))
        )
    return [available[name] for name in requested]


def run_eval_suites(
    *,
    suites: list[LocalPdfEvalSuite],
    project_root: Path,
    output_root: Path,
    engine_name: str,
    heuristic_profile: str,
    pipeline_mode: str = "deterministic",
    hybrid_mode: str = "auto",
    write_trace: bool = False,
    page_limit: int | None = None,
    evaluator_python: Path | None = None,
    evaluator_script: Path | None = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    suite_reports: list[dict[str, Any]] = []

    for suite in suites:
        suite_report = run_eval_suite(
            suite=suite,
            project_root=project_root,
            output_root=output_root,
            engine_name=engine_name,
            heuristic_profile=heuristic_profile,
            pipeline_mode=pipeline_mode,
            hybrid_mode=hybrid_mode,
            write_trace=write_trace,
            page_limit=page_limit,
            evaluator_python=evaluator_python,
            evaluator_script=evaluator_script,
        )
        suite_reports.append(suite_report)

    matrix_summary = {
        "manifest_version": "local_structured_pdf_eval_suites_v1",
        "engine_name": engine_name,
        "heuristic_profile": heuristic_profile,
        "pipeline_mode": pipeline_mode,
        "hybrid_mode": hybrid_mode if pipeline_mode == "hybrid" else "",
        "write_trace": bool(write_trace),
        "suite_count": len(suite_reports),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "processor": _processor_name(),
        "suites": suite_reports,
    }
    (output_root / f"{heuristic_profile}_matrix_summary.json").write_text(
        json.dumps(matrix_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return matrix_summary


def run_eval_suite(
    *,
    suite: LocalPdfEvalSuite,
    project_root: Path,
    output_root: Path,
    engine_name: str,
    heuristic_profile: str,
    pipeline_mode: str = "deterministic",
    hybrid_mode: str = "auto",
    write_trace: bool = False,
    page_limit: int | None = None,
    evaluator_python: Path | None = None,
    evaluator_script: Path | None = None,
) -> dict[str, Any]:
    suite_root = output_root / heuristic_profile / suite.name
    prediction_root = suite_root / "prediction"
    engine_dir = prediction_root / engine_name
    markdown_dir = engine_dir / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)

    normalized_pipeline_mode = _normalize_pipeline_mode(pipeline_mode)
    normalized_hybrid_mode = _normalize_hybrid_mode(hybrid_mode)
    pipeline = (
        LocalStructuredPdfDoclingFastHybridPipeline(heuristic_profile=heuristic_profile)
        if normalized_pipeline_mode == "hybrid"
        else LocalStructuredPdfPipeline(heuristic_profile=heuristic_profile)
    )
    pipeline.ensure_runtime_ready()
    renderer = LocalPdfMarkdownRenderer()
    pdf_paths = _collect_pdf_paths(suite=suite)
    failures: list[str] = []
    per_doc_rows: list[dict[str, Any]] = []
    trace_dir = suite_root / "trace" / engine_name if bool(write_trace) else None
    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    for pdf_path in pdf_paths:
        doc_started_at = time.time()
        logging.info("Suite=%s processing %s", suite.name, pdf_path.name)
        try:
            document, trace = _run_pipeline_parse(
                pipeline=pipeline,
                pipeline_mode=normalized_pipeline_mode,
                hybrid_mode=normalized_hybrid_mode,
                pdf_path=pdf_path,
                page_limit=page_limit,
            )
            markdown = renderer.render_document(document=document)
            (markdown_dir / f"{pdf_path.stem}.md").write_text(markdown, encoding="utf-8")
            if trace_dir is not None and trace is not None:
                (trace_dir / f"{pdf_path.stem}.json").write_text(
                    json.dumps(trace.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as exc:  # pragma: no cover - defensive CLI guard
            logging.exception("Suite=%s failed to process %s: %s", suite.name, pdf_path.name, exc)
            failures.append(pdf_path.stem)
            (markdown_dir / f"{pdf_path.stem}.md").write_text("", encoding="utf-8")
        per_doc_rows.append(
            {
                "document_id": pdf_path.stem,
                "elapsed_seconds": time.time() - doc_started_at,
            }
        )

    total_elapsed = time.time() - started_at
    doc_count = len(pdf_paths)
    export_summary = {
        "engine_name": engine_name,
        "engine_version": "0.1.0-dev",
        "heuristic_profile": heuristic_profile,
        "pipeline_mode": normalized_pipeline_mode,
        "hybrid_mode": normalized_hybrid_mode if normalized_pipeline_mode == "hybrid" else "",
        "suite": {
            "name": suite.name,
            "input_dir": str(suite.input_dir),
            "ground_truth_dir": str(suite.ground_truth_dir) if suite.ground_truth_dir else "",
            "description": suite.description,
            "doc_ids": list(suite.doc_ids),
        },
        "processor": _processor_name(),
        "document_count": doc_count,
        "total_elapsed": total_elapsed,
        "elapsed_per_doc": (total_elapsed / doc_count) if doc_count else 0.0,
        "failed_documents": failures,
        "date": time.strftime("%Y-%m-%d"),
        "documents": per_doc_rows,
        "trace_dir": str(trace_dir) if trace_dir is not None else "",
    }
    summary_path = engine_dir / "summary.json"
    summary_path.write_text(json.dumps(export_summary, indent=2, ensure_ascii=False), encoding="utf-8")

    suite_report: dict[str, Any] = {
        "name": suite.name,
        "description": suite.description,
        "heuristic_profile": heuristic_profile,
        "pipeline_mode": normalized_pipeline_mode,
        "hybrid_mode": normalized_hybrid_mode if normalized_pipeline_mode == "hybrid" else "",
        "input_dir": str(suite.input_dir),
        "ground_truth_dir": str(suite.ground_truth_dir) if suite.ground_truth_dir else "",
        "document_count": doc_count,
        "failed_documents": failures,
        "summary_path": str(summary_path),
        "markdown_dir": str(markdown_dir),
        "trace_dir": str(trace_dir) if trace_dir is not None else "",
        "elapsed_per_doc": export_summary["elapsed_per_doc"],
    }

    if suite.ground_truth_dir and evaluator_python and evaluator_script:
        evaluation_ground_truth_dir = _prepare_ground_truth_subset(
            suite=suite,
            suite_root=suite_root,
            pdf_paths=pdf_paths,
        )
        evaluation_path = _run_evaluator(
            evaluator_python=evaluator_python,
            evaluator_script=evaluator_script,
            ground_truth_dir=evaluation_ground_truth_dir,
            prediction_root=prediction_root,
            engine_name=engine_name,
            workdir=project_root,
        )
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        suite_report["evaluation_path"] = str(evaluation_path)
        suite_report["evaluation_ground_truth_dir"] = str(evaluation_ground_truth_dir)
        suite_report["metrics"] = evaluation.get("metrics", {})
        suite_report["evaluation_summary"] = evaluation.get("summary", {})

    suite_report_path = suite_root / "suite_summary.json"
    suite_report_path.write_text(json.dumps(suite_report, indent=2, ensure_ascii=False), encoding="utf-8")
    return suite_report


def _collect_pdf_paths(*, suite: LocalPdfEvalSuite) -> list[Path]:
    if suite.doc_ids:
        return [path for path in [suite.input_dir / f"{doc_id}.pdf" for doc_id in suite.doc_ids] if path.is_file()]
    return sorted(path for path in suite.input_dir.glob("*.pdf") if path.is_file())


def _normalize_pipeline_mode(pipeline_mode: str) -> str:
    token = str(pipeline_mode or "deterministic").strip().lower()
    if token not in {"deterministic", "hybrid"}:
        return "deterministic"
    return token


def _normalize_hybrid_mode(hybrid_mode: str) -> str:
    token = str(hybrid_mode or "auto").strip().lower()
    if token not in {"auto", "full"}:
        return "auto"
    return token


def _run_pipeline_parse(
    *,
    pipeline: Any,
    pipeline_mode: str,
    hybrid_mode: str,
    pdf_path: Path,
    page_limit: int | None,
) -> tuple[Any, Any | None]:
    if pipeline_mode == "hybrid":
        trace = asyncio.run(
            pipeline.parse_document_with_trace(
                pdf_path=str(pdf_path),
                page_limit=page_limit,
                mode=hybrid_mode,
            )
        )
        return trace.document, trace
    return pipeline.parse_document(pdf_path=str(pdf_path), page_limit=page_limit), None


def _prepare_ground_truth_subset(
    *,
    suite: LocalPdfEvalSuite,
    suite_root: Path,
    pdf_paths: list[Path],
) -> Path:
    if suite.ground_truth_dir is None:
        raise ValueError("ground_truth_dir is required to prepare subset")
    target_dir = suite_root / "ground_truth" / "markdown"
    target_dir.mkdir(parents=True, exist_ok=True)

    selected_ids = [path.stem for path in pdf_paths]
    for doc_id in selected_ids:
        source_path = suite.ground_truth_dir / f"{doc_id}.md"
        if not source_path.is_file():
            raise FileNotFoundError(f"Missing ground truth markdown for {doc_id}: {source_path}")
        (target_dir / f"{doc_id}.md").write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    return target_dir


def _run_evaluator(
    *,
    evaluator_python: Path,
    evaluator_script: Path,
    ground_truth_dir: Path,
    prediction_root: Path,
    engine_name: str,
    workdir: Path,
) -> Path:
    command = [
        str(evaluator_python),
        str(evaluator_script),
        "--ground-truth-dir",
        str(ground_truth_dir),
        "--prediction-root",
        str(prediction_root),
        "--engine",
        engine_name,
    ]
    completed = subprocess.run(
        command,
        cwd=str(workdir),
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stdout:
        logging.info(completed.stdout.strip())
    if completed.stderr:
        logging.info(completed.stderr.strip())
    return prediction_root / engine_name / "evaluation.json"


def _processor_name() -> str:
    cpu = platform.processor().strip()
    if cpu:
        return cpu
    return platform.uname().processor or platform.machine() or "unknown"


def _resolve_path(value: str, *, project_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (project_root / path).resolve()
