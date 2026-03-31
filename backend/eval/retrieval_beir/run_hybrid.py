from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from beir import util
    from beir.datasets.data_loader import GenericDataLoader
    from beir.retrieval.evaluation import EvaluateRetrieval
    from beir.retrieval.search.dense import DenseRetrievalExactSearch as DRES
    from rank_bm25 import BM25Okapi
except ImportError as exc:  # pragma: no cover - runtime import guard
    raise SystemExit(
        "Hybrid eval dependencies are missing. Install backend/eval/retrieval_beir/requirements.txt in an isolated eval environment first."
    ) from exc

from app.config import settings
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
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _compose_corpus_text(doc: dict[str, str]) -> str:
    title = _normalize_text(doc.get("title") or "")
    body = _normalize_text(doc.get("text") or "")
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_text(text).lower()
    return re.findall(r"[A-Za-z0-9_]+", normalized)


def _build_bm25_corpus(corpus: dict[str, dict[str, str]]) -> tuple[list[str], list[list[str]]]:
    doc_ids = list(corpus.keys())
    tokenized_docs = [_tokenize(_compose_corpus_text(corpus[doc_id])) for doc_id in doc_ids]
    return doc_ids, tokenized_docs


def _retrieve_bm25(
    *,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    top_k: int,
) -> dict[str, dict[str, float]]:
    doc_ids, tokenized_docs = _build_bm25_corpus(corpus)
    bm25 = BM25Okapi(tokenized_docs)
    results: dict[str, dict[str, float]] = {}

    for query_id, query_text in queries.items():
        tokenized_query = _tokenize(query_text)
        if not tokenized_query:
            results[query_id] = {}
            continue

        scores = bm25.get_scores(tokenized_query)
        ranked_indexes = np.argsort(scores)[::-1]
        limited: dict[str, float] = {}
        for idx in ranked_indexes[:top_k]:
            score = float(scores[idx])
            if score <= 0:
                continue
            limited[doc_ids[int(idx)]] = score
        results[query_id] = limited

    return results


def _fuse_rrf(
    *,
    dense_results: dict[str, dict[str, float]],
    text_results: dict[str, dict[str, float]],
    rrf_k: int,
    limit: int,
) -> dict[str, dict[str, float]]:
    fused: dict[str, dict[str, float]] = {}
    all_query_ids = set(dense_results.keys()) | set(text_results.keys())
    safe_rrf_k = max(1, int(rrf_k or 60))
    safe_limit = max(1, int(limit or 1))

    for query_id in all_query_ids:
        accum: dict[str, float] = {}

        dense_ranked = sorted(
            dense_results.get(query_id, {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )
        for rank, (doc_id, _) in enumerate(dense_ranked, start=1):
            accum[doc_id] = accum.get(doc_id, 0.0) + (1.0 / (safe_rrf_k + rank))

        text_ranked = sorted(
            text_results.get(query_id, {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )
        for rank, (doc_id, _) in enumerate(text_ranked, start=1):
            accum[doc_id] = accum.get(doc_id, 0.0) + (1.0 / (safe_rrf_k + rank))

        fused[query_id] = dict(
            sorted(accum.items(), key=lambda item: item[1], reverse=True)[:safe_limit]
        )

    return fused


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dense+BM25 hybrid BEIR retrieval evaluation with RRF.")
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
    parser.add_argument("--dense-top-k", type=int, default=0, help="Dense candidate pool size; 0 uses max(k_values)")
    parser.add_argument("--text-top-k", type=int, default=0, help="BM25 candidate pool size; 0 uses max(k_values)")
    parser.add_argument("--fusion-limit", type=int, default=0, help="RRF fused candidate limit; 0 uses max(k_values)")
    parser.add_argument("--rrf-k", type=int, default=int(settings.hybrid_rrf_k), help="RRF k parameter")
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

    model = ResearchAssistantDenseModel(
        batch_size=int(args.batch_size),
        model_name=args.model_name,
        target_dimension=int(args.target_dimension or 0),
    )
    paths = _prepare_run_paths(
        dataset=args.dataset,
        output_root=output_root,
        model_name=f"{model.model_name}-hybrid",
        dimension=model.dimension,
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
    dense_top_k = max(int(args.dense_top_k or 0), max_eval_k)
    text_top_k = max(int(args.text_top_k or 0), max_eval_k)
    fusion_limit = max(int(args.fusion_limit or 0), max_eval_k)

    dense_retriever = EvaluateRetrieval(
        DRES(model, batch_size=int(args.batch_size)),
        score_function=str(args.score_function),
        k_values=k_values,
    )

    started_at = time.perf_counter()
    dense_results = dense_retriever.retrieve(corpus, queries)
    dense_results = {
        query_id: dict(
            sorted(scores.items(), key=lambda item: item[1], reverse=True)[:dense_top_k]
        )
        for query_id, scores in dense_results.items()
    }
    text_results = _retrieve_bm25(
        corpus=corpus,
        queries=queries,
        top_k=text_top_k,
    )
    fused_results = _fuse_rrf(
        dense_results=dense_results,
        text_results=text_results,
        rrf_k=int(args.rrf_k),
        limit=fusion_limit,
    )

    ndcg, _map, recall, precision = EvaluateRetrieval.evaluate(
        qrels,
        fused_results,
        k_values,
    )
    mrr = EvaluateRetrieval.evaluate_custom(
        qrels,
        fused_results,
        k_values,
        metric="mrr",
    )
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

    payload = {
        "dataset": args.dataset,
        "split": args.split,
        "mode": "hybrid_rrf",
        "metrics": {
            "ndcg": ndcg,
            "map": _map,
            "recall": recall,
            "precision": precision,
            "mrr": mrr,
        },
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
        "embedding": {
            "provider": str(model.embedding_svc.provider),
            "model": str(model.model_name),
            "dimension": int(model.dimension),
            "batch_size": int(args.batch_size),
        },
        "hybrid": {
            "dense_top_k": dense_top_k,
            "text_top_k": text_top_k,
            "fusion_limit": fusion_limit,
            "rrf_k": int(args.rrf_k),
            "text_retriever": "bm25_okapi",
        },
        "config": {
            "score_function": str(args.score_function),
            "k_values": k_values,
            "dataset_dir": str(dataset_dir),
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
                "model_name": model.model_name,
                "dimension": model.dimension,
                "provider": model.embedding_svc.provider,
                "batch_size": int(args.batch_size),
                "score_function": str(args.score_function),
                "k_values": k_values,
                "dense_top_k": dense_top_k,
                "text_top_k": text_top_k,
                "fusion_limit": fusion_limit,
                "rrf_k": int(args.rrf_k),
                "settings_local_model": str(settings.local_embedding_model or ""),
                "settings_local_dimension": int(settings.local_embedding_dimension or 0),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )

    util.save_runfile(str(paths.runfile_path), fused_results)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved metrics: {paths.metrics_path}")
    print(f"Saved runfile: {paths.runfile_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
