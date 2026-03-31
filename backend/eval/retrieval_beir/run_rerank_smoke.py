from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.reranker_service import get_reranker_service


DEFAULT_OUTPUT_ROOT = BACKEND_ROOT / "eval" / "retrieval_beir" / "output"
DEFAULT_DATA_ROOT = BACKEND_ROOT / "eval" / "retrieval_beir" / "data"


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run_rerank_smoke.py does not support running inside an active event loop")


def _slugify_model_name(model_name: str) -> str:
    token = str(model_name or "default").strip().lower()
    return "".join(ch if ch.isalnum() else "-" for ch in token).strip("-") or "default"


@dataclass
class EvalRunPaths:
    run_dir: Path
    metrics_path: Path
    config_path: Path
    runfile_path: Path


def _prepare_run_paths(*, dataset: str, output_root: Path, model_name: str) -> EvalRunPaths:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    model_slug = _slugify_model_name(model_name)
    run_dir = (output_root / dataset / f"{timestamp}-{model_slug}-smoke").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    return EvalRunPaths(
        run_dir=run_dir,
        metrics_path=run_dir / "metrics.json",
        config_path=run_dir / "config.json",
        runfile_path=run_dir / "run.trec",
    )


def _load_queries(path: Path) -> dict[str, str]:
    queries: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            row = json.loads(raw_line)
            queries[str(row["_id"])] = str(row.get("text") or "")
    return queries


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with path.open("r", encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) == 4:
                query_id, _, doc_id, score = row
            elif len(row) == 3:
                query_id, doc_id, score = row
            else:
                continue
            try:
                relevance = int(score)
            except ValueError:
                continue
            if relevance <= 0:
                continue
            qrels.setdefault(str(query_id), {})[str(doc_id)] = relevance
    return qrels


def _load_trec_runfile(path: Path, *, candidate_k: int) -> dict[str, list[tuple[str, float]]]:
    results: dict[str, list[tuple[str, float]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            query_id, _, doc_id, rank, score, _ = parts[:6]
            try:
                score_value = float(score)
            except ValueError:
                continue
            results.setdefault(str(query_id), []).append((str(doc_id), score_value))

    trimmed: dict[str, list[tuple[str, float]]] = {}
    for query_id, rows in results.items():
        sorted_rows = sorted(rows, key=lambda item: item[1], reverse=True)
        trimmed[query_id] = sorted_rows[:candidate_k]
    return trimmed


def _load_corpus_subset(path: Path, *, needed_doc_ids: set[str]) -> dict[str, dict[str, str]]:
    corpus: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            row = json.loads(raw_line)
            doc_id = str(row["_id"])
            if doc_id not in needed_doc_ids:
                continue
            corpus[doc_id] = {
                "title": str(row.get("title") or ""),
                "text": str(row.get("text") or ""),
            }
    return corpus


def _compose_corpus_text(doc: dict[str, str]) -> str:
    title = str(doc.get("title") or "").strip()
    body = str(doc.get("text") or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _dcg(relevances: list[int]) -> float:
    total = 0.0
    for index, relevance in enumerate(relevances, start=1):
        if relevance <= 0:
            continue
        total += (2**relevance - 1) / math.log2(index + 1)
    return total


def _evaluate_results(
    *,
    qrels: dict[str, dict[str, int]],
    results: dict[str, list[tuple[str, float]]],
    k_values: list[int],
) -> dict[str, dict[str, float]]:
    ndcg = {f"NDCG@{k}": 0.0 for k in k_values}
    recall = {f"Recall@{k}": 0.0 for k in k_values}
    mrr = {f"MRR@{k}": 0.0 for k in k_values}
    query_ids = [query_id for query_id in qrels.keys() if qrels.get(query_id)]
    if not query_ids:
        return {"ndcg": ndcg, "recall": recall, "mrr": mrr}

    for query_id in query_ids:
        rels = qrels[query_id]
        ranked_doc_ids = [doc_id for doc_id, _ in results.get(query_id, [])]
        ideal_rels = sorted(rels.values(), reverse=True)
        total_relevant = sum(1 for score in rels.values() if score > 0)

        for k in k_values:
            top_doc_ids = ranked_doc_ids[:k]
            top_rels = [int(rels.get(doc_id, 0)) for doc_id in top_doc_ids]
            dcg = _dcg(top_rels)
            idcg = _dcg(ideal_rels[:k])
            ndcg[f"NDCG@{k}"] += dcg / idcg if idcg > 0 else 0.0

            hits = sum(1 for rel in top_rels if rel > 0)
            recall[f"Recall@{k}"] += hits / total_relevant if total_relevant > 0 else 0.0

            reciprocal_rank = 0.0
            for rank, doc_id in enumerate(top_doc_ids, start=1):
                if rels.get(doc_id, 0) > 0:
                    reciprocal_rank = 1.0 / rank
                    break
            mrr[f"MRR@{k}"] += reciprocal_rank

    query_count = float(len(query_ids))
    return {
        "ndcg": {key: round(value / query_count, 5) for key, value in ndcg.items()},
        "recall": {key: round(value / query_count, 5) for key, value in recall.items()},
        "mrr": {key: round(value / query_count, 5) for key, value in mrr.items()},
    }


def _format_trec(results: dict[str, list[tuple[str, float]]]) -> str:
    lines: list[str] = []
    for query_id in sorted(results.keys()):
        ranked = sorted(results[query_id], key=lambda item: item[1], reverse=True)
        for rank, (doc_id, score) in enumerate(ranked, start=1):
            lines.append(f"{query_id} Q0 {doc_id} {rank} {score:.6f} research-assistant-rerank-smoke")
    return "\n".join(lines) + ("\n" if lines else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run backend reranker smoke evaluation on an existing dense runfile.")
    parser.add_argument("--dataset", required=True, help="Dataset name, e.g. scifact")
    parser.add_argument("--dense-runfile", required=True, help="Existing dense run.trec path")
    parser.add_argument("--split", default="test", help="Dataset split, default: test")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help="Root directory for BEIR datasets")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Where eval outputs are written")
    parser.add_argument("--candidate-k", type=int, default=10, help="Number of dense candidates to rerank per query")
    parser.add_argument("--rerank-top-k", type=int, default=10, help="Final reranked top-k to retain")
    parser.add_argument("--max-queries", type=int, default=3, help="Limit number of queries from the dense runfile")
    parser.add_argument("--k-values", default="1,3,5,10", help="Comma-separated evaluation k values")
    args = parser.parse_args()

    dataset_dir = (Path(args.data_root).expanduser().resolve() / args.dataset).resolve()
    if not dataset_dir.exists():
        raise SystemExit(f"Dataset directory does not exist: {dataset_dir}")

    dense_runfile = Path(args.dense_runfile).expanduser().resolve()
    if not dense_runfile.exists():
        raise SystemExit(f"Dense runfile does not exist: {dense_runfile}")

    k_values = sorted({max(1, int(token.strip())) for token in str(args.k_values).split(",") if token.strip()})
    candidate_k = max(1, int(args.candidate_k))
    rerank_top_k = max(int(args.rerank_top_k), max(k_values))

    queries = _load_queries(dataset_dir / "queries.jsonl")
    qrels = _load_qrels(dataset_dir / "qrels" / f"{args.split}.tsv")
    dense_results = _load_trec_runfile(dense_runfile, candidate_k=candidate_k)

    selected_query_ids = [query_id for query_id in dense_results.keys() if qrels.get(query_id)]
    if int(args.max_queries or 0) > 0:
        selected_query_ids = selected_query_ids[: int(args.max_queries)]
    if not selected_query_ids:
        raise SystemExit("No query with qrels found in the dense runfile subset.")

    selected_queries = {query_id: queries[query_id] for query_id in selected_query_ids}
    selected_qrels = {query_id: qrels[query_id] for query_id in selected_query_ids}
    selected_dense = {query_id: dense_results[query_id] for query_id in selected_query_ids}

    needed_doc_ids = {doc_id for rows in selected_dense.values() for doc_id, _ in rows}
    corpus = _load_corpus_subset(dataset_dir / "corpus.jsonl", needed_doc_ids=needed_doc_ids)

    reranker = get_reranker_service()
    warmup_started_at = time.perf_counter()
    _run_async(reranker.rerank("warmup query", ["warmup document"], top_k=1))
    warmup_ms = round((time.perf_counter() - warmup_started_at) * 1000, 2)

    baseline_started_at = time.perf_counter()
    baseline_metrics = _evaluate_results(
        qrels=selected_qrels,
        results=selected_dense,
        k_values=k_values,
    )
    baseline_elapsed_ms = round((time.perf_counter() - baseline_started_at) * 1000, 2)

    rerank_started_at = time.perf_counter()
    reranked_results: dict[str, list[tuple[str, float]]] = {}
    for query_id, query_text in selected_queries.items():
        ranked_rows = selected_dense.get(query_id, [])
        ranked_doc_ids = [doc_id for doc_id, _ in ranked_rows if doc_id in corpus]
        documents = [_compose_corpus_text(corpus[doc_id]) for doc_id in ranked_doc_ids]
        ranked = _run_async(
            reranker.rerank(
                query_text,
                documents,
                top_k=min(rerank_top_k, len(documents)),
            )
        )
        reranked_results[query_id] = [
            (ranked_doc_ids[index], float(score))
            for index, score in ranked
            if 0 <= index < len(ranked_doc_ids)
        ]
    rerank_elapsed_ms = round((time.perf_counter() - rerank_started_at) * 1000, 2)

    rerank_metrics = _evaluate_results(
        qrels=selected_qrels,
        results=reranked_results,
        k_values=k_values,
    )

    paths = _prepare_run_paths(
        dataset=args.dataset,
        output_root=Path(args.output_root).expanduser().resolve(),
        model_name="backend-rerank",
    )
    paths.runfile_path.write_text(_format_trec(reranked_results), encoding="utf-8")

    payload = {
        "dataset": args.dataset,
        "split": args.split,
        "mode": "dense_rerank_smoke",
        "baseline_metrics": baseline_metrics,
        "rerank_metrics": rerank_metrics,
        "runtime": {
            "warmup_ms": warmup_ms,
            "baseline_eval_ms": baseline_elapsed_ms,
            "rerank_eval_ms": rerank_elapsed_ms,
            "query_count": len(selected_queries),
            "candidate_k": candidate_k,
            "rerank_top_k": rerank_top_k,
        },
        "selection": {
            "query_ids": selected_query_ids,
            "corpus_size": len(corpus),
        },
        "dense_runfile": str(dense_runfile),
    }

    paths.metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths.config_path.write_text(
        json.dumps(
            {
                "dataset": args.dataset,
                "split": args.split,
                "candidate_k": candidate_k,
                "rerank_top_k": rerank_top_k,
                "max_queries": int(args.max_queries or 0),
                "k_values": k_values,
                "dense_runfile": str(dense_runfile),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
