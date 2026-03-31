from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path


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

from app.services.query_rewrite_service import QueryRewriteResult, QueryVariant, get_query_rewrite_service
from run_dense import (
    DEFAULT_DATA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_URL_TEMPLATE,
    ResearchAssistantDenseModel,
    _limit_dataset,
    _parse_k_values,
    _prepare_run_paths,
    _resolve_dataset_dir,
    _run_async,
)


def _expanded_query_id(query_id: str, index: int, strategy: str) -> str:
    return f"{query_id}__{index}__{strategy}"


def _expand_queries(
    queries: dict[str, str],
    *,
    use_query_rewrite: bool,
    requested_strategies: list[str] | None,
    rewrite_mode: str | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, QueryRewriteResult]]:
    return _run_async(
        _expand_queries_async(
            queries,
            use_query_rewrite=use_query_rewrite,
            requested_strategies=requested_strategies,
            rewrite_mode=rewrite_mode,
        )
    )


async def _expand_queries_async(
    queries: dict[str, str],
    *,
    use_query_rewrite: bool,
    requested_strategies: list[str] | None,
    rewrite_mode: str | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, QueryRewriteResult]]:
    rewrite_service = get_query_rewrite_service()
    expanded_queries: dict[str, str] = {}
    reverse_map: dict[str, str] = {}
    rewrite_payloads: dict[str, QueryRewriteResult] = {}

    for query_id, query_text in queries.items():
        rewrite_result = await rewrite_service.rewrite_query(
            query_text,
            use_query_rewrite=use_query_rewrite,
            requested_strategies=requested_strategies,
            rewrite_mode=rewrite_mode,
        )
        rewrite_payloads[query_id] = rewrite_result

        variants = rewrite_result.vector_variants or [QueryVariant(text=query_text, strategy="original")]
        for index, variant in enumerate(variants):
            expanded_id = _expanded_query_id(query_id, index, variant.strategy)
            expanded_queries[expanded_id] = variant.text
            reverse_map[expanded_id] = query_id

    return expanded_queries, reverse_map, rewrite_payloads


def _merge_dense_results(
    expanded_results: dict[str, dict[str, float]],
    reverse_map: dict[str, str],
    *,
    limit: int,
) -> dict[str, dict[str, float]]:
    merged: dict[str, dict[str, float]] = {}

    for expanded_id, doc_scores in expanded_results.items():
        original_query_id = reverse_map.get(expanded_id)
        if not original_query_id:
            continue
        bucket = merged.setdefault(original_query_id, {})
        for doc_id, score in doc_scores.items():
            score_value = float(score)
            if score_value > float(bucket.get(doc_id, float("-inf"))):
                bucket[doc_id] = score_value

    trimmed: dict[str, dict[str, float]] = {}
    for query_id, scores in merged.items():
        trimmed[query_id] = dict(
            sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        )
    return trimmed


def _summarize_rewrite_payloads(payloads: dict[str, QueryRewriteResult]) -> dict[str, object]:
    enabled_count = 0
    llm_called_count = 0
    cache_hit_count = 0
    fallback_counter: Counter[str] = Counter()
    skip_counter: Counter[str] = Counter()
    strategy_counter: Counter[str] = Counter()
    variant_count = 0

    for result in payloads.values():
        if result.enabled:
            enabled_count += 1
        if result.llm_called:
            llm_called_count += 1
        if result.cache_hit:
            cache_hit_count += 1
        if result.fallback_reason:
            fallback_counter[result.fallback_reason] += 1
        if result.skip_reason:
            skip_counter[result.skip_reason] += 1
        for strategy in result.strategies:
            strategy_counter[strategy] += 1
        variant_count += len(result.vector_variants or [])

    total = max(1, len(payloads))
    return {
        "query_count": len(payloads),
        "rewrite_enabled_queries": enabled_count,
        "rewrite_enabled_ratio": round(enabled_count / total, 4),
        "llm_called_queries": llm_called_count,
        "llm_called_ratio": round(llm_called_count / total, 4),
        "cache_hit_queries": cache_hit_count,
        "cache_hit_ratio": round(cache_hit_count / total, 4),
        "avg_vector_variants": round(variant_count / total, 4),
        "fallback_reasons": dict(fallback_counter),
        "skip_reasons": dict(skip_counter),
        "strategies_used": dict(strategy_counter),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dense + backend query rewrite BEIR evaluation.")
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
    parser.add_argument("--candidate-k", type=int, default=0, help="Merged candidate limit; 0 uses max(k_values)")
    parser.add_argument("--rewrite-mode", default="auto", help="Rewrite mode: auto | force | off")
    parser.add_argument("--rewrite-strategies", default=None, help="Comma-separated strategies: synonym,hyde,decompose")
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
        model_name=f"{model.model_name}-rewrite",
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

    requested_strategies = None
    if args.rewrite_strategies:
        requested_strategies = [item.strip() for item in str(args.rewrite_strategies).split(",") if item.strip()]

    k_values = _parse_k_values(args.k_values)
    candidate_k = max(int(args.candidate_k or 0), max(k_values))

    expanded_queries, reverse_map, rewrite_payloads = _expand_queries(
        queries,
        use_query_rewrite=True,
        requested_strategies=requested_strategies,
        rewrite_mode=str(args.rewrite_mode),
    )

    retriever = EvaluateRetrieval(
        DRES(model, batch_size=int(args.batch_size)),
        score_function=str(args.score_function),
        k_values=k_values,
    )

    started_at = time.perf_counter()
    expanded_results = retriever.retrieve(corpus, expanded_queries)
    merged_results = _merge_dense_results(
        expanded_results,
        reverse_map,
        limit=candidate_k,
    )
    ndcg, _map, recall, precision = EvaluateRetrieval.evaluate(qrels, merged_results, retriever.k_values)
    mrr = EvaluateRetrieval.evaluate_custom(qrels, merged_results, retriever.k_values, metric="mrr")
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

    payload = {
        "dataset": args.dataset,
        "split": args.split,
        "mode": "dense_query_rewrite",
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
            "expanded_query_count": len(expanded_queries),
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
        "rewrite": {
            "mode": str(args.rewrite_mode),
            "requested_strategies": requested_strategies or [],
            **_summarize_rewrite_payloads(rewrite_payloads),
        },
        "config": {
            "score_function": str(args.score_function),
            "k_values": k_values,
            "candidate_k": candidate_k,
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
                "candidate_k": candidate_k,
                "rewrite_mode": str(args.rewrite_mode),
                "rewrite_strategies": requested_strategies or [],
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
