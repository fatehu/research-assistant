from __future__ import annotations

import argparse
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

METRICS = [
    ("overall_mean", "Overall", "#14b8a6"),
    ("nid_mean", "Reading Order", "#60a5fa"),
    ("teds_mean", "Table", "#f59e0b"),
    ("mhs_mean", "Heading", "#a3e635"),
]


@dataclass(frozen=True)
class EngineReport:
    name: str
    document_count: int
    elapsed_per_doc: float | None
    failed_count: int
    missing_predictions: int
    metrics: dict[str, float | None]
    path: Path

    @property
    def overall(self) -> float:
        value = self.metrics.get("overall_mean")
        return float(value) if value is not None else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate SVG benchmark charts from opendataloader-bench evaluation.json files.",
    )
    parser.add_argument(
        "--prediction-root",
        default="tmp/opendataloader-bench/prediction",
        help="Directory containing <engine>/evaluation.json files.",
    )
    parser.add_argument(
        "--output-dir",
        default="docs/report/pdf_benchmark_charts",
        help="Directory for generated SVG charts and summary files.",
    )
    parser.add_argument(
        "--min-documents",
        type=int,
        default=200,
        help="Only include reports with at least this many documents.",
    )
    parser.add_argument(
        "--baseline",
        default="opendataloader",
        help="Baseline engine for delta chart.",
    )
    parser.add_argument(
        "--target",
        default="local-structured-pdf",
        help="Target engine for delta chart.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prediction_root = resolve_path(args.prediction_root)
    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reports = load_reports(prediction_root)
    comparable = [
        report
        for report in reports
        if report.document_count >= args.min_documents
        and report.missing_predictions == 0
        and report.failed_count == 0
    ]
    comparable.sort(key=lambda item: item.overall, reverse=True)
    if not comparable:
        raise ValueError(f"No comparable reports found under {prediction_root}")

    write_readme_overview_chart(
        comparable,
        target=args.target,
        output_path=output_dir / "readme_benchmark_overview.svg",
    )
    write_quality_chart(comparable, output_dir / "quality_comparison.svg")
    write_speed_chart(comparable, output_dir / "speed_comparison.svg")
    write_delta_chart(
        comparable,
        baseline=args.baseline,
        target=args.target,
        output_path=output_dir / f"{slug(args.target)}_vs_{slug(args.baseline)}_delta.svg",
    )
    write_summary(comparable, output_dir / "README.md")
    write_summary_json(comparable, output_dir / "benchmark_summary.json")

    print(f"Wrote benchmark charts to {output_dir}")


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def load_reports(prediction_root: Path) -> list[EngineReport]:
    reports: list[EngineReport] = []
    for path in sorted(prediction_root.glob("*/evaluation.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data.get("summary") or {}
        metrics = (data.get("metrics") or {}).get("score") or {}
        failed_documents = summary.get("failed_documents") or []
        reports.append(
            EngineReport(
                name=str(summary.get("engine_name") or path.parent.name),
                document_count=int(summary.get("document_count") or 0),
                elapsed_per_doc=to_float(summary.get("elapsed_per_doc")),
                failed_count=len(failed_documents),
                missing_predictions=int((data.get("metrics") or {}).get("missing_predictions") or 0),
                metrics={key: to_float(metrics.get(key)) for key, _, _ in METRICS},
                path=path,
            )
        )
    return reports


def to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_quality_chart(reports: list[EngineReport], output_path: Path) -> None:
    width = 1180
    left = 230
    right = 70
    top = 110
    row_height = 78
    bar_height = 10
    gap = 4
    chart_width = width - left - right
    height = top + len(reports) * row_height + 60

    parts = svg_header(width, height)
    parts.append(svg_title("PDF Benchmark Quality Comparison", 36, 42))
    parts.append(
        text(
            36,
            70,
            "Full 200-document opendataloader-bench corpus; higher is better.",
            size=14,
            fill="#94a3b8",
        )
    )
    parts.append(legend(width - 480, 35))

    for tick in [0, 0.25, 0.5, 0.75, 1.0]:
        x = left + tick * chart_width
        parts.append(line(x, top - 10, x, height - 50, stroke="#1f2937", width=1))
        parts.append(text(x - 10, top - 20, f"{tick:.2f}", size=11, fill="#64748b"))

    for index, report in enumerate(reports):
        y = top + index * row_height
        parts.append(text(36, y + 28, report.name, size=15, fill="#e5edf7", weight=700))
        parts.append(text(36, y + 47, f"{report.document_count} docs", size=11, fill="#64748b"))
        for metric_index, (key, label, color) in enumerate(METRICS):
            value = report.metrics.get(key)
            bar_y = y + metric_index * (bar_height + gap)
            parts.append(text(left - 104, bar_y + 9, label, size=10, fill="#94a3b8"))
            parts.append(rect(left, bar_y, chart_width, bar_height, radius=5, fill="#0f172a"))
            if value is not None:
                parts.append(rect(left, bar_y, max(2, value * chart_width), bar_height, radius=5, fill=color))
                parts.append(text(left + value * chart_width + 8, bar_y + 9, f"{value:.3f}", size=10, fill="#cbd5e1"))
        if index < len(reports) - 1:
            parts.append(line(36, y + row_height - 18, width - 36, y + row_height - 18, stroke="#111827", width=1))

    parts.append(svg_footer())
    output_path.write_text("\n".join(parts), encoding="utf-8")


def write_readme_overview_chart(
    reports: list[EngineReport],
    *,
    target: str,
    output_path: Path,
) -> None:
    target_report = next((report for report in reports if report.name == target), None)
    width = 1280
    margin = 42
    top = 42
    card_top = 126
    card_height = 96
    table_top = 278
    row_height = 54
    height = table_top + len(reports) * row_height + 116
    table_left = margin
    name_width = 250
    overall_left = table_left + name_width
    metric_width = 112
    speed_left = overall_left + 360 + metric_width * 3 + 24

    parts = svg_header(width, height)
    parts.append(
        rect(
            margin - 12,
            top - 26,
            width - margin * 2 + 24,
            height - top,
            radius=28,
            fill="#020817",
        )
    )
    parts.append(svg_title("PDF Parser Benchmark on OpenDataLoader-Bench Corpus", margin, 56))
    parts.append(
        text(
            margin,
            84,
            "Local recorded evaluation files, full 200-document corpus, zero failed documents. Quality metrics are higher-is-better.",
            size=15,
            fill="#9fb0c4",
        )
    )
    parts.append(
        text(
            margin,
            108,
            "This is an internal reproduction chart, not the live upstream leaderboard. Speed values are recorded-run metadata.",
            size=13,
            fill="#64748b",
        )
    )

    if target_report is not None:
        cards = [
            ("Overall", target_report.metrics.get("overall_mean"), "#14b8a6"),
            ("Reading Order", target_report.metrics.get("nid_mean"), "#60a5fa"),
            ("Table", target_report.metrics.get("teds_mean"), "#f59e0b"),
            ("Heading", target_report.metrics.get("mhs_mean"), "#a3e635"),
            ("Speed", target_report.elapsed_per_doc, "#c084fc"),
        ]
        card_width = (width - margin * 2 - 4 * 16) / 5
        for index, (label, value, color) in enumerate(cards):
            x = margin + index * (card_width + 16)
            parts.append(rect(x, card_top, card_width, card_height, radius=18, fill="#0b1220"))
            parts.append(rect(x, card_top, 4, card_height, radius=2, fill=color))
            parts.append(text(x + 18, card_top + 30, label, size=13, fill="#94a3b8", weight=700))
            value_text = f"{value:.3f}s/doc" if label == "Speed" and value is not None else format_metric(value)
            parts.append(text(x + 18, card_top + 68, value_text, size=27, fill="#f8fafc", weight=800))

    parts.append(text(table_left, table_top - 28, "Engine", size=12, fill="#64748b", weight=800))
    parts.append(text(overall_left, table_top - 28, "Overall", size=12, fill="#64748b", weight=800))
    parts.append(text(overall_left + 392, table_top - 28, "Reading", size=12, fill="#64748b", weight=800))
    parts.append(text(overall_left + 500, table_top - 28, "Table", size=12, fill="#64748b", weight=800))
    parts.append(text(overall_left + 608, table_top - 28, "Heading", size=12, fill="#64748b", weight=800))
    parts.append(text(speed_left, table_top - 28, "Recorded Speed", size=12, fill="#64748b", weight=800))

    for rank, report in enumerate(reports, start=1):
        y = table_top + (rank - 1) * row_height
        is_target = report.name == target
        row_fill = "#0f1f2c" if is_target else "#07111f"
        parts.append(rect(table_left, y - 22, width - margin * 2, row_height - 8, radius=14, fill=row_fill))
        if is_target:
            parts.append(rect(table_left, y - 22, 5, row_height - 8, radius=3, fill="#14b8a6"))
        medal = "1" if rank == 1 else str(rank)
        parts.append(text(table_left + 20, y + 7, medal, size=12, fill="#94a3b8", weight=800))
        parts.append(
            text(
                table_left + 52,
                y + 7,
                report.name,
                size=15,
                fill="#f8fafc" if is_target else "#dbeafe",
                weight=800 if is_target else 650,
            )
        )
        parts.append(text(table_left + 52, y + 25, f"{report.document_count} docs", size=10, fill="#64748b"))

        overall = report.metrics.get("overall_mean") or 0.0
        parts.append(rect(overall_left, y - 2, 320, 14, radius=7, fill="#111827"))
        parts.append(rect(overall_left, y - 2, max(2, overall * 320), 14, radius=7, fill="#14b8a6" if is_target else "#475569"))
        parts.append(text(overall_left + 332, y + 10, f"{overall:.3f}", size=13, fill="#e5edf7", weight=800))

        for index, (key, _, color) in enumerate(METRICS[1:]):
            value = report.metrics.get(key)
            label_x = overall_left + 392 + index * metric_width
            parts.append(text(label_x, y + 10, format_metric(value), size=13, fill=color if is_target else "#cbd5e1", weight=800 if is_target else 600))

        speed = report.elapsed_per_doc
        speed_text = "" if speed is None else f"{speed:.3f}s/doc"
        speed_color = "#cbd5e1" if report.name != "local-structured-pdf" else "#c084fc"
        parts.append(text(speed_left, y + 10, speed_text, size=13, fill=speed_color, weight=700))

    footnote_y = height - 46
    parts.append(
        text(
            margin,
            footnote_y,
            "Data source: tmp/opendataloader-bench/prediction/*/evaluation.json. Excludes partial 2-document online_mm_eval.",
            size=12,
            fill="#64748b",
        )
    )
    parts.append(svg_footer())
    output_path.write_text("\n".join(parts), encoding="utf-8")


def write_speed_chart(reports: list[EngineReport], output_path: Path) -> None:
    filtered = [report for report in reports if report.elapsed_per_doc is not None and report.elapsed_per_doc > 0]
    filtered.sort(key=lambda item: float(item.elapsed_per_doc or 0))

    width = 1050
    left = 220
    right = 80
    top = 105
    row_height = 46
    chart_width = width - left - right
    height = top + len(filtered) * row_height + 55
    values = [float(report.elapsed_per_doc or 0) for report in filtered]
    min_log = math.log10(max(min(values), 0.001))
    max_log = math.log10(max(values))
    if math.isclose(min_log, max_log):
        max_log = min_log + 1

    parts = svg_header(width, height)
    parts.append(svg_title("PDF Benchmark Speed Comparison", 36, 42))
    parts.append(text(36, 70, "Seconds per document on recorded run; log scale, lower is better.", size=14, fill="#94a3b8"))

    for index, report in enumerate(filtered):
        y = top + index * row_height
        elapsed = float(report.elapsed_per_doc or 0)
        x_ratio = (math.log10(max(elapsed, 0.001)) - min_log) / (max_log - min_log)
        bar_width = max(3, x_ratio * chart_width)
        color = "#14b8a6" if report.name == "local-structured-pdf" else "#64748b"
        parts.append(text(36, y + 17, report.name, size=14, fill="#e5edf7", weight=700 if report.name == "local-structured-pdf" else 500))
        parts.append(rect(left, y, chart_width, 18, radius=9, fill="#0f172a"))
        parts.append(rect(left, y, bar_width, 18, radius=9, fill=color))
        parts.append(text(left + bar_width + 10, y + 14, f"{elapsed:.3f}s/doc", size=12, fill="#cbd5e1"))

    parts.append(svg_footer())
    output_path.write_text("\n".join(parts), encoding="utf-8")


def write_delta_chart(
    reports: list[EngineReport],
    *,
    baseline: str,
    target: str,
    output_path: Path,
) -> None:
    by_name = {report.name: report for report in reports}
    if baseline not in by_name or target not in by_name:
        return

    baseline_report = by_name[baseline]
    target_report = by_name[target]
    deltas = []
    for key, label, color in METRICS:
        base_value = baseline_report.metrics.get(key)
        target_value = target_report.metrics.get(key)
        if base_value is None or target_value is None:
            continue
        deltas.append((label, target_value - base_value, color))

    width = 920
    height = 380
    left = 270
    center = 520
    scale = 1700
    top = 105
    row_height = 52

    parts = svg_header(width, height)
    parts.append(svg_title(f"{target} vs {baseline}", 36, 42))
    parts.append(text(36, 70, "Metric delta on the same 200-document corpus; positive means target is higher.", size=14, fill="#94a3b8"))
    parts.append(line(center, top - 18, center, top + len(deltas) * row_height + 10, stroke="#334155", width=1))
    parts.append(text(center - 18, top - 26, "0", size=11, fill="#64748b"))

    for index, (label, delta, color) in enumerate(deltas):
        y = top + index * row_height
        parts.append(text(36, y + 16, label, size=15, fill="#e5edf7", weight=700))
        width_px = abs(delta) * scale
        x = center if delta >= 0 else center - width_px
        fill = color if delta >= 0 else "#ef4444"
        parts.append(rect(x, y, max(2, width_px), 18, radius=9, fill=fill))
        value_x = center + width_px + 10 if delta >= 0 else x - 92
        parts.append(text(value_x, y + 14, f"{delta:+.3f}", size=13, fill="#cbd5e1"))

    parts.append(svg_footer())
    output_path.write_text("\n".join(parts), encoding="utf-8")


def write_summary(reports: list[EngineReport], output_path: Path) -> None:
    lines = [
        "# PDF Benchmark Charts",
        "",
        "Generated from local `opendataloader-bench` `evaluation.json` files.",
        "",
        "Included reports are full 200-document runs with zero failed documents and zero missing predictions.",
        "",
        "## Charts",
        "",
        "- [README-style overview](readme_benchmark_overview.svg)",
        "- [Quality comparison](quality_comparison.svg)",
        "- [Speed comparison](speed_comparison.svg)",
        "- [local-structured-pdf vs opendataloader delta](local-structured-pdf_vs_opendataloader_delta.svg)",
        "",
        "## Summary",
        "",
        "| Engine | Docs | Overall | Reading order | Table | Heading | Seconds/doc |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        lines.append(
            "| {engine} | {docs} | {overall} | {nid} | {teds} | {mhs} | {speed} |".format(
                engine=report.name,
                docs=report.document_count,
                overall=format_metric(report.metrics.get("overall_mean")),
                nid=format_metric(report.metrics.get("nid_mean")),
                teds=format_metric(report.metrics.get("teds_mean")),
                mhs=format_metric(report.metrics.get("mhs_mean")),
                speed=format_metric(report.elapsed_per_doc),
            )
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- These charts compare local recorded runs, not the live upstream leaderboard.",
            "- `online_mm_eval` is excluded by default because the local record only covers 2 documents.",
            "- Raw generated benchmark outputs under `backend/eval/results/**` are ignored by Git; this report is the portable summary.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_summary_json(reports: list[EngineReport], output_path: Path) -> None:
    payload = [
        {
            "engine": report.name,
            "document_count": report.document_count,
            "elapsed_per_doc": report.elapsed_per_doc,
            "failed_count": report.failed_count,
            "missing_predictions": report.missing_predictions,
            "metrics": report.metrics,
            "source": str(report.path.relative_to(PROJECT_ROOT)),
        }
        for report in reports
    ]
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def format_metric(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value.strip()).strip("-")


def svg_header(width: int, height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">',
        '<stop offset="0%" stop-color="#020617"/>',
        '<stop offset="55%" stop-color="#0f172a"/>',
        '<stop offset="100%" stop-color="#111827"/>',
        "</linearGradient>",
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="url(#bg)"/>',
    ]


def svg_footer() -> str:
    return "</svg>"


def svg_title(value: str, x: float, y: float) -> str:
    return text(x, y, value, size=24, fill="#f8fafc", weight=800)


def legend(x: float, y: float) -> str:
    parts = []
    cursor = x
    for _, label, color in METRICS:
        parts.append(rect(cursor, y - 10, 14, 14, radius=3, fill=color))
        parts.append(text(cursor + 20, y + 2, label, size=12, fill="#cbd5e1"))
        cursor += 118
    return "\n".join(parts)


def text(
    x: float,
    y: float,
    value: str,
    *,
    size: int = 12,
    fill: str = "#e5edf7",
    weight: int = 500,
) -> str:
    escaped = html.escape(value)
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" fill="{fill}" '
        f'font-family="Inter, Segoe UI, Arial, sans-serif" font-size="{size}" '
        f'font-weight="{weight}">{escaped}</text>'
    )


def rect(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    radius: float = 0,
    fill: str = "#0f172a",
) -> str:
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" '
        f'rx="{radius:.2f}" ry="{radius:.2f}" fill="{fill}"/>'
    )


def line(x1: float, y1: float, x2: float, y2: float, *, stroke: str, width: float) -> str:
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="{width:.2f}"/>'
    )


if __name__ == "__main__":
    main()
