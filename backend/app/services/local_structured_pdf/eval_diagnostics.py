from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any


METRIC_KEYS = ("overall", "nid", "teds", "mhs")
SUMMARY_KEYS = ("overall_mean", "nid_mean", "teds_mean", "mhs_mean", "mhs_s_mean")


def load_evaluation_report(*, path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summarize_evaluation_report(
    *,
    report: dict[str, Any],
    label: str,
    top_k: int = 10,
) -> dict[str, Any]:
    score = dict((report.get("metrics") or {}).get("score") or {})
    documents = list(report.get("documents") or [])

    weakest_counter: Counter[str] = Counter()
    normalized_documents: list[dict[str, Any]] = []
    for item in documents:
        doc_id = str(item.get("document_id") or "").strip()
        scores = dict(item.get("scores") or {})
        weakest_metric = _weakest_metric(scores=scores)
        if weakest_metric:
            weakest_counter[weakest_metric] += 1
        normalized_documents.append(
            {
                "document_id": doc_id,
                "scores": scores,
                "weakest_metric": weakest_metric,
            }
        )

    lowest_by_metric: dict[str, list[dict[str, Any]]] = {}
    for metric in METRIC_KEYS:
        rows = [row for row in normalized_documents if row["scores"].get(metric) is not None]
        rows.sort(key=lambda row: float(row["scores"][metric]))
        lowest_by_metric[metric] = rows[: max(1, int(top_k))]

    return {
        "label": label,
        "summary": {key: score.get(key) for key in SUMMARY_KEYS},
        "document_count": len(normalized_documents),
        "weakest_metric_counts": dict(weakest_counter),
        "lowest_by_metric": lowest_by_metric,
    }


def compare_evaluation_reports(
    *,
    reports: list[tuple[str, dict[str, Any]]],
    top_k: int = 10,
) -> dict[str, Any]:
    summaries = [
        summarize_evaluation_report(report=report, label=label, top_k=top_k)
        for label, report in reports
    ]
    deltas: list[dict[str, Any]] = []
    if summaries:
        baseline = summaries[0]
        baseline_summary = baseline.get("summary") or {}
        for candidate in summaries[1:]:
            candidate_summary = candidate.get("summary") or {}
            deltas.append(
                {
                    "baseline": baseline["label"],
                    "candidate": candidate["label"],
                    "summary_delta": {
                        key: _safe_delta(candidate_summary.get(key), baseline_summary.get(key))
                        for key in SUMMARY_KEYS
                    },
                }
            )
    return {
        "reports": summaries,
        "deltas": deltas,
    }


def _weakest_metric(*, scores: dict[str, Any]) -> str | None:
    values = {
        key: float(scores[key])
        for key in METRIC_KEYS
        if scores.get(key) is not None
    }
    if not values:
        return None
    return min(values, key=values.get)


def _safe_delta(current: Any, baseline: Any) -> float | None:
    if current is None or baseline is None:
        return None
    return float(current) - float(baseline)
