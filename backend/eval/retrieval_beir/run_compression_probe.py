from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from beir.datasets.data_loader import GenericDataLoader
except ImportError as exc:  # pragma: no cover - runtime import guard
    raise SystemExit(
        "BEIR is not installed. Install backend/eval/retrieval_beir/requirements.txt in an isolated eval environment first."
    ) from exc

from app.services.contextual_compression_service import CompressionInput, get_contextual_compression_service
from app.config import settings
from run_dense import (
    DEFAULT_DATA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_URL_TEMPLATE,
    _limit_dataset,
    _prepare_run_paths,
    _resolve_dataset_dir,
    _run_async,
)


def _compose_corpus_text(doc: dict[str, str]) -> str:
    title = str(doc.get("title") or "").strip()
    body = str(doc.get("text") or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _load_trec_runfile(path: Path, *, top_k: int) -> dict[str, list[str]]:
    results: dict[str, list[tuple[str, float]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            query_id, _, doc_id, _, score, _ = parts[:6]
            try:
                score_value = float(score)
            except ValueError:
                continue
            results.setdefault(query_id, []).append((doc_id, score_value))

    trimmed: dict[str, list[str]] = {}
    for query_id, rows in results.items():
        trimmed[query_id] = [
            doc_id
            for doc_id, _ in sorted(rows, key=lambda item: item[1], reverse=True)[:top_k]
        ]
    return trimmed


async def _run_probe_async(
    *,
    queries: dict[str, str],
    corpus: dict[str, dict[str, str]],
    candidate_map: dict[str, list[str]],
) -> tuple[int, int, int, float, Counter[str], list[dict[str, object]]]:
    compression_service = get_contextual_compression_service()
    fallback_counter: Counter[str] = Counter()
    total_original_chars = 0
    total_compressed_chars = 0
    used_compression = 0
    total_relevance_score = 0.0
    result_count = 0
    per_query_summary: list[dict[str, object]] = []

    for query_id, query_text in queries.items():
        doc_ids = candidate_map.get(query_id, [])
        if not doc_ids:
            continue

        inputs: list[CompressionInput] = []
        for idx, doc_id in enumerate(doc_ids, start=1):
            doc = corpus.get(doc_id)
            if not doc:
                continue
            content = _compose_corpus_text(doc)
            inputs.append(
                CompressionInput(
                    source_id=idx,
                    doc_name=doc_id,
                    chunk_idx=idx - 1,
                    chunk_content=content,
                    reranker_score=None,
                )
            )

        results = await compression_service.compress_chunks(
            query_text,
            inputs,
            use_contextual_compression=True,
        )

        query_used = 0
        query_original_chars = 0
        query_compressed_chars = 0
        for item, result in zip(inputs, results):
            original_chars = len(item.chunk_content or "")
            compressed_chars = len(result.relevant_content or "")
            total_original_chars += original_chars
            total_compressed_chars += compressed_chars
            total_relevance_score += float(result.relevance_score or 0.0)
            result_count += 1
            query_original_chars += original_chars
            query_compressed_chars += compressed_chars
            if result.used_compression:
                used_compression += 1
                query_used += 1
            if result.fallback_reason:
                fallback_counter[result.fallback_reason] += 1

        per_query_summary.append(
            {
                "query_id": query_id,
                "candidate_count": len(inputs),
                "used_compression_count": query_used,
                "original_chars": query_original_chars,
                "compressed_chars": query_compressed_chars,
            }
        )

    return (
        total_original_chars,
        total_compressed_chars,
        used_compression,
        total_relevance_score,
        fallback_counter,
        per_query_summary,
    )


@contextmanager
def _temporary_compression_settings(*, mode: str | None, min_relevance: float | None):
    original_mode = settings.contextual_compression_mode
    original_min_relevance = settings.contextual_compression_min_relevance
    try:
        if mode:
            settings.contextual_compression_mode = mode
        if min_relevance is not None:
            settings.contextual_compression_min_relevance = float(min_relevance)
        yield
    finally:
        settings.contextual_compression_mode = original_mode
        settings.contextual_compression_min_relevance = original_min_relevance


def main() -> int:
    parser = argparse.ArgumentParser(description="Run contextual compression probe on retrieved BEIR candidates.")
    parser.add_argument("--dataset", required=True, help="Official BEIR dataset name, e.g. scifact")
    parser.add_argument("--split", default="test", help="Dataset split, default: test")
    parser.add_argument("--download", action="store_true", help="Download the official BEIR dataset if missing")
    parser.add_argument("--dataset-dir", default=None, help="Use an existing BEIR dataset directory")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help="Where downloaded datasets live")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Where eval outputs are written")
    parser.add_argument("--url-template", default=DEFAULT_URL_TEMPLATE, help="Official dataset URL template")
    parser.add_argument("--dense-runfile", required=True, help="Existing dense/hybrid run.trec path used to source top-k candidates")
    parser.add_argument("--max-queries", type=int, default=0, help="Optional smoke subset limit for queries; 0 keeps all")
    parser.add_argument("--max-corpus", type=int, default=0, help="Optional smoke subset limit for corpus; 0 keeps all")
    parser.add_argument("--top-k", type=int, default=5, help="How many retrieved candidates per query to compress")
    parser.add_argument("--mode", choices=["batch", "single"], default=None, help="Override contextual compression mode for probe")
    parser.add_argument("--min-relevance", type=float, default=None, help="Override contextual compression min relevance threshold for probe")
    args = parser.parse_args()

    data_root = Path(args.data_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    dataset_dir = _resolve_dataset_dir(
        dataset=args.dataset,
        data_root=data_root,
        dataset_dir=args.dataset_dir,
        download=bool(args.download),
        url_template=str(args.url_template),
    )
    paths = _prepare_run_paths(
        dataset=args.dataset,
        output_root=output_root,
        model_name="compression-probe",
        dimension=0,
    )

    corpus, queries, qrels = GenericDataLoader(data_folder=str(dataset_dir)).load(split=args.split)
    corpus, queries, qrels, sample_info = _limit_dataset(
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        max_queries=int(args.max_queries or 0),
        max_corpus=int(args.max_corpus or 0),
    )
    candidate_map = _load_trec_runfile(
        Path(args.dense_runfile).expanduser().resolve(),
        top_k=max(1, int(args.top_k or 1)),
    )

    with _temporary_compression_settings(
        mode=args.mode,
        min_relevance=args.min_relevance,
    ):
        started_at = time.perf_counter()
        (
            total_original_chars,
            total_compressed_chars,
            used_compression,
            total_relevance_score,
            fallback_counter,
            per_query_summary,
        ) = _run_async(
            _run_probe_async(
                queries=queries,
                corpus=corpus,
                candidate_map=candidate_map,
            )
        )
        result_count = sum(item["candidate_count"] for item in per_query_summary)

        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        avg_relevance = round(total_relevance_score / result_count, 4) if result_count else 0.0
        compression_ratio = round(
            (total_compressed_chars / total_original_chars),
            4,
        ) if total_original_chars else 0.0

    payload = {
        "dataset": args.dataset,
        "split": args.split,
        "mode": "contextual_compression_probe",
        "runtime": {
            "elapsed_ms": elapsed_ms,
            "query_count": len(queries),
            "corpus_size": len(corpus),
            "qrels_size": len(qrels),
        },
        "sample": {
            "enabled": bool(args.max_queries or args.max_corpus),
            "max_queries": int(args.max_queries or 0),
            "max_corpus": int(args.max_corpus or 0),
            **sample_info,
        },
        "compression": {
            "top_k": int(args.top_k),
            "result_count": result_count,
            "used_compression_count": used_compression,
            "used_compression_ratio": round((used_compression / result_count), 4) if result_count else 0.0,
            "avg_relevance_score": avg_relevance,
            "original_chars": total_original_chars,
            "compressed_chars": total_compressed_chars,
            "compression_ratio": compression_ratio,
            "fallback_reasons": dict(fallback_counter),
        },
        "config": {
            "dataset_dir": str(dataset_dir),
            "dense_runfile": str(args.dense_runfile),
            "contextual_compression_mode": settings.contextual_compression_mode if args.mode is None else args.mode,
            "contextual_compression_min_relevance": (
                settings.contextual_compression_min_relevance
                if args.min_relevance is None
                else float(args.min_relevance)
            ),
        },
        "per_query": per_query_summary,
    }

    with paths.metrics_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    with paths.config_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "dataset": args.dataset,
                "split": args.split,
                "dataset_dir": str(dataset_dir),
                "dense_runfile": str(args.dense_runfile),
                "top_k": int(args.top_k),
                "mode": settings.contextual_compression_mode if args.mode is None else args.mode,
                "min_relevance": (
                    settings.contextual_compression_min_relevance
                    if args.min_relevance is None
                    else float(args.min_relevance)
                ),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved metrics: {paths.metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
