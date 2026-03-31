from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from beir.datasets.data_loader import GenericDataLoader
    from beir.retrieval.evaluation import EvaluateRetrieval
    from beir.retrieval.search.dense import DenseRetrievalExactSearch as DRES
except ImportError as exc:  # pragma: no cover - runtime import guard
    raise SystemExit(
        "BEIR is not installed. Install backend/eval/retrieval_beir/requirements.txt in an isolated eval environment first."
    ) from exc

from app.config import settings
from app.services.reranker_service import get_reranker_service
from run_dense import (
    DEFAULT_DATA_ROOT,
    DEFAULT_K_VALUES,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_URL_TEMPLATE,
    ResearchAssistantDenseModel,
    _limit_dataset,
    _parse_k_values,
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


def _load_trec_runfile(path: Path, *, candidate_k: int) -> dict[str, dict[str, float]]:
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

    trimmed: dict[str, dict[str, float]] = {}
    for query_id, rows in results.items():
        trimmed[query_id] = dict(
            sorted(rows, key=lambda item: item[1], reverse=True)[:candidate_k]
        )
    return trimmed


def _run_dense_candidates(
    *,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    model: ResearchAssistantDenseModel,
    batch_size: int,
    score_function: str,
    k_values: list[int],
    candidate_k: int,
) -> dict[str, dict[str, float]]:
    retriever = EvaluateRetrieval(
        DRES(model, batch_size=int(batch_size)),
        score_function=str(score_function),
        k_values=k_values,
    )
    dense_results = retriever.retrieve(corpus, queries)
    trimmed: dict[str, dict[str, float]] = {}
    for query_id, scores in dense_results.items():
        trimmed[query_id] = dict(
            sorted(scores.items(), key=lambda item: item[1], reverse=True)[:candidate_k]
        )
    return trimmed


def _rerank_results(
    *,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    dense_results: dict[str, dict[str, float]],
    top_k: int,
) -> dict[str, dict[str, float]]:
    reranker = get_reranker_service()
    reranked: dict[str, dict[str, float]] = {}

    for query_id, query_text in queries.items():
        candidates = dense_results.get(query_id, {})
        if not candidates:
            reranked[query_id] = {}
            continue

        ranked_doc_ids = [
            doc_id
            for doc_id, _ in sorted(candidates.items(), key=lambda item: item[1], reverse=True)
        ]
        documents = [_compose_corpus_text(corpus[doc_id]) for doc_id in ranked_doc_ids if doc_id in corpus]
        if not documents:
            reranked[query_id] = {}
            continue

        ranked = _run_async(
            reranker.rerank(query_text, documents, top_k=min(top_k, len(documents)))
        )
        reranked[query_id] = {
            ranked_doc_ids[index]: float(score)
            for index, score in ranked
            if 0 <= index < len(ranked_doc_ids)
        }

    return reranked


def _warmup_reranker() -> float:
    reranker = get_reranker_service()
    started_at = time.perf_counter()
    _run_async(reranker.rerank("warmup query", ["warmup document"], top_k=1))
    return round((time.perf_counter() - started_at) * 1000, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dense + backend reranker BEIR evaluation.")
    parser.add_argument("--dataset", required=True, help="Official BEIR dataset name, e.g. scifact")
    parser.add_argument("--split", default="test", help="Dataset split, default: test")
    parser.add_argument("--download", action="store_true", help="Download the official BEIR dataset if missing")
    parser.add_argument("--dataset-dir", default=None, help="Use an existing BEIR dataset directory")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help="Where downloaded datasets live")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Where eval outputs are written")
    parser.add_argument("--url-template", default=DEFAULT_URL_TEMPLATE, help="Official dataset URL template")
    parser.add_argument("--model-name", default=None, help="Override embedding model name")
    parser.add_argument("--target-dimension", type=int, default=0, help="Override embedding dimension; 0 uses current service default")
    parser.add_argument("--batch-size", type=int, default=32, help="Dense encoding batch size")
    parser.add_argument("--score-function", default="cos_sim", help="BEIR score function for dense retrieval, default: cos_sim")
    parser.add_argument("--k-values", default="1,3,5,10,20,50,100", help="Comma-separated evaluation k values")
    parser.add_argument("--max-queries", type=int, default=0, help="Optional smoke subset limit for queries; 0 keeps all")
    parser.add_argument("--max-corpus", type=int, default=0, help="Optional smoke subset limit for corpus; 0 keeps all")
    parser.add_argument("--candidate-k", type=int, default=0, help="Dense candidate pool size for rerank; 0 uses max(k_values) and reranker_top_k")
    parser.add_argument("--rerank-top-k", type=int, default=0, help="Final reranked top-k to keep; 0 uses max(k_values)")
    parser.add_argument("--dense-runfile", default=None, help="Optional existing dense run.trec path; when provided, skip dense retrieval and rerank these candidates")
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

    using_dense_runfile = bool(args.dense_runfile)
    model = None
    model_name = str(args.model_name or settings.local_embedding_model)
    dimension = int(args.target_dimension or 0)
    provider = "local"
    if not using_dense_runfile:
        model = ResearchAssistantDenseModel(
            batch_size=int(args.batch_size),
            model_name=args.model_name,
            target_dimension=int(args.target_dimension or 0),
        )
        model_name = str(model.model_name)
        dimension = int(model.dimension)
        provider = str(model.embedding_svc.provider)

    paths = _prepare_run_paths(
        dataset=args.dataset,
        output_root=output_root,
        model_name=f"{model_name}-rerank",
        dimension=max(0, int(dimension or 0)),
    )

    corpus, queries, qrels = GenericDataLoader(data_folder=str(dataset_dir)).load(split=args.split)
    corpus, queries, qrels, sample_info = _limit_dataset(
        corpus=corpus,
        queries=queries,
        qrels=qrels,
        max_queries=int(args.max_queries or 0),
        max_corpus=int(args.max_corpus or 0),
    )

    k_values = _parse_k_values(args.k_values)
    max_eval_k = max(k_values)
    rerank_top_k = max(int(args.rerank_top_k or 0), max_eval_k)
    candidate_k = max(int(args.candidate_k or 0), rerank_top_k, int(settings.reranker_top_k))

    warmup_ms = _warmup_reranker()

    started_at = time.perf_counter()
    if args.dense_runfile:
        dense_results = _load_trec_runfile(Path(args.dense_runfile).expanduser().resolve(), candidate_k=candidate_k)
    else:
        dense_results = _run_dense_candidates(
            corpus=corpus,
            queries=queries,
            model=model,
            batch_size=int(args.batch_size),
            score_function=str(args.score_function),
            k_values=k_values,
            candidate_k=candidate_k,
        )
    reranked_results = _rerank_results(
        corpus=corpus,
        queries=queries,
        dense_results=dense_results,
        top_k=rerank_top_k,
    )

    ndcg, _map, recall, precision = EvaluateRetrieval.evaluate(
        qrels,
        reranked_results,
        k_values,
    )
    mrr = EvaluateRetrieval.evaluate_custom(
        qrels,
        reranked_results,
        k_values,
        metric="mrr",
    )
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

    payload = {
        "dataset": args.dataset,
        "split": args.split,
        "mode": "dense_rerank",
        "metrics": {
            "ndcg": ndcg,
            "map": _map,
            "recall": recall,
            "precision": precision,
            "mrr": mrr,
        },
        "runtime": {
            "warmup_ms": warmup_ms,
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
        "embedding": {
            "provider": provider,
            "model": model_name,
            "dimension": int(dimension),
            "batch_size": int(args.batch_size),
        },
        "rerank": {
            "candidate_k": candidate_k,
            "rerank_top_k": rerank_top_k,
            "model": str(settings.reranker_model),
            "device": str(settings.reranker_device),
        },
        "config": {
            "score_function": str(args.score_function),
            "k_values": k_values,
            "dataset_dir": str(dataset_dir),
            "dense_runfile": str(args.dense_runfile or ""),
        },
    }

    with paths.metrics_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    with paths.config_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "dataset": args.dataset,
                "split": args.split,
                "dataset_dir": str(dataset_dir),
                "model_name": model_name,
                "dimension": int(dimension),
                "provider": provider,
                "batch_size": int(args.batch_size),
                "score_function": str(args.score_function),
                "k_values": k_values,
                "candidate_k": candidate_k,
                "rerank_top_k": rerank_top_k,
                "dense_runfile": str(args.dense_runfile or ""),
                "reranker_model": str(settings.reranker_model),
                "reranker_device": str(settings.reranker_device),
                "settings_local_model": str(settings.local_embedding_model or ""),
                "settings_local_dimension": int(settings.local_embedding_dimension or 0),
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
