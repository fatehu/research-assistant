from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
except ImportError as exc:  # pragma: no cover - import guard for runtime env
    raise SystemExit(
        "BEIR is not installed. Install backend/eval/retrieval_beir/requirements.txt in an isolated eval environment first."
    ) from exc

from app.config import settings
from app.services.embedding_service import get_embedding_service_for_model_and_dimension


DEFAULT_DATA_ROOT = BACKEND_ROOT / "eval" / "retrieval_beir" / "data"
DEFAULT_OUTPUT_ROOT = BACKEND_ROOT / "eval" / "retrieval_beir" / "output"
DEFAULT_URL_TEMPLATE = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{dataset}.zip"
DEFAULT_K_VALUES = [1, 3, 5, 10, 20, 50, 100]


def _parse_k_values(raw: str) -> list[int]:
    values: list[int] = []
    for item in str(raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        values.append(int(token))
    if not values:
        return list(DEFAULT_K_VALUES)
    return sorted({max(1, value) for value in values})


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run_dense.py does not support running inside an active event loop")


def _batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    normalized_batch_size = max(1, int(batch_size or 1))
    for index in range(0, len(items), normalized_batch_size):
        yield items[index:index + normalized_batch_size]


def _slugify_model_name(model_name: str) -> str:
    token = str(model_name or "default").strip().lower()
    return "".join(ch if ch.isalnum() else "-" for ch in token).strip("-") or "default"


@dataclass
class EvalRunPaths:
    dataset_dir: Path
    run_dir: Path
    metrics_path: Path
    config_path: Path
    runfile_path: Path


class ResearchAssistantDenseModel:
    def __init__(self, *, batch_size: int, model_name: str | None = None, target_dimension: int = 0) -> None:
        self.batch_size = max(1, int(batch_size or 1))
        self.embedding_svc = get_embedding_service_for_model_and_dimension(
            model_name=model_name,
            target_dimension=target_dimension,
        )
        self.model_name = self.embedding_svc._get_model()
        self.dimension = self.embedding_svc.get_dimension()

    def encode_queries(self, queries: list[str], batch_size: int = 32, **kwargs) -> np.ndarray:
        _ = kwargs
        embeddings: list[list[float]] = []
        for batch in _batched(list(queries or []), batch_size or self.batch_size):
            embeddings.extend(_run_async(self.embedding_svc.embed_texts(batch, is_query=True)))
        return np.asarray(embeddings, dtype=np.float32)

    def encode_corpus(self, corpus: list[dict[str, str]], batch_size: int = 32, **kwargs) -> np.ndarray:
        _ = kwargs
        texts: list[str] = []
        for doc in list(corpus or []):
            title = str(doc.get("title") or "").strip()
            body = str(doc.get("text") or "").strip()
            if title and body:
                texts.append(f"{title}\n\n{body}")
            elif title:
                texts.append(title)
            else:
                texts.append(body)

        embeddings: list[list[float]] = []
        for batch in _batched(texts, batch_size or self.batch_size):
            embeddings.extend(_run_async(self.embedding_svc.embed_texts(batch, is_query=False)))
        return np.asarray(embeddings, dtype=np.float32)


def _resolve_dataset_dir(
    *,
    dataset: str,
    data_root: Path,
    dataset_dir: str | None,
    download: bool,
    url_template: str,
) -> Path:
    if dataset_dir:
        resolved = Path(dataset_dir).expanduser().resolve()
        if not resolved.exists():
            raise SystemExit(f"Dataset directory does not exist: {resolved}")
        return resolved

    target_dir = (data_root / dataset).resolve()
    if (target_dir / "corpus.jsonl").exists():
        return target_dir

    if not download:
        raise SystemExit(
            f"Dataset not found at {target_dir}. Re-run with --download or pass --dataset-dir."
        )

    data_root.mkdir(parents=True, exist_ok=True)
    url = url_template.format(dataset=dataset)
    util.download_and_unzip(url, str(data_root))
    if not (target_dir / "corpus.jsonl").exists():
        raise SystemExit(f"Downloaded dataset but corpus.jsonl not found under: {target_dir}")
    return target_dir


def _prepare_run_paths(
    *,
    dataset: str,
    output_root: Path,
    model_name: str,
    dimension: int,
) -> EvalRunPaths:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    model_slug = _slugify_model_name(model_name)
    run_dir = (output_root / dataset / f"{timestamp}-{model_slug}-dim{dimension}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    return EvalRunPaths(
        dataset_dir=output_root / dataset,
        run_dir=run_dir,
        metrics_path=run_dir / "metrics.json",
        config_path=run_dir / "config.json",
        runfile_path=run_dir / "run.trec",
    )


def _limit_dataset(
    *,
    corpus: dict[str, dict[str, str]],
    queries: dict[str, str],
    qrels: dict[str, dict[str, int]],
    max_queries: int,
    max_corpus: int,
) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, dict[str, int]], dict[str, int]]:
    selected_queries = dict(queries)
    selected_qrels = {
        query_id: dict(rels)
        for query_id, rels in qrels.items()
        if query_id in selected_queries
    }
    selected_corpus = dict(corpus)

    if max_queries > 0:
        ranked_query_ids = [
            query_id
            for query_id in selected_queries.keys()
            if selected_qrels.get(query_id)
        ]
        seen_query_ids = set(ranked_query_ids)
        if len(ranked_query_ids) < max_queries:
            ranked_query_ids.extend(
                query_id
                for query_id in selected_queries.keys()
                if query_id not in seen_query_ids
            )
        query_ids = ranked_query_ids[:max_queries]
        selected_queries = {query_id: selected_queries[query_id] for query_id in query_ids}
        selected_qrels = {
            query_id: selected_qrels.get(query_id, {})
            for query_id in query_ids
            if selected_qrels.get(query_id)
        }

    if max_corpus > 0:
        required_doc_ids: set[str] = set()
        for rels in selected_qrels.values():
            for doc_id, relevance in rels.items():
                if relevance and doc_id in selected_corpus:
                    required_doc_ids.add(doc_id)

        if len(required_doc_ids) > max_corpus:
            raise SystemExit(
                f"max_corpus={max_corpus} is too small for the selected queries; "
                f"required relevant docs={len(required_doc_ids)}"
            )

        kept_doc_ids = set(required_doc_ids)
        for doc_id in selected_corpus.keys():
            if len(kept_doc_ids) >= max_corpus:
                break
            kept_doc_ids.add(doc_id)

        selected_corpus = {
            doc_id: selected_corpus[doc_id]
            for doc_id in selected_corpus.keys()
            if doc_id in kept_doc_ids
        }
        selected_qrels = {
            query_id: {
                doc_id: relevance
                for doc_id, relevance in rels.items()
                if doc_id in selected_corpus
            }
            for query_id, rels in selected_qrels.items()
        }
        selected_qrels = {
            query_id: rels
            for query_id, rels in selected_qrels.items()
            if rels
        }
        selected_queries = {
            query_id: text
            for query_id, text in selected_queries.items()
            if query_id in selected_qrels
        }
    else:
        selected_qrels = {
            query_id: rels
            for query_id, rels in selected_qrels.items()
            if rels
        }
        selected_queries = {
            query_id: text
            for query_id, text in selected_queries.items()
            if query_id in selected_qrels
        }

    sample_info = {
        "corpus_size": len(selected_corpus),
        "query_count": len(selected_queries),
        "qrels_size": len(selected_qrels),
    }
    return selected_corpus, selected_queries, selected_qrels, sample_info


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dense-only BEIR retrieval evaluation.")
    parser.add_argument("--dataset", required=True, help="Official BEIR dataset name, e.g. scifact")
    parser.add_argument("--split", default="test", help="Dataset split, default: test")
    parser.add_argument("--download", action="store_true", help="Download the official BEIR dataset if missing")
    parser.add_argument("--dataset-dir", default=None, help="Use an existing BEIR dataset directory")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT), help="Where downloaded datasets live")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Where eval outputs are written")
    parser.add_argument("--url-template", default=DEFAULT_URL_TEMPLATE, help="Official dataset URL template")
    parser.add_argument("--model-name", default=None, help="Override embedding model name")
    parser.add_argument("--target-dimension", type=int, default=0, help="Override embedding dimension; 0 uses current service default")
    parser.add_argument("--batch-size", type=int, default=32, help="Encoding batch size")
    parser.add_argument("--score-function", default="cos_sim", help="BEIR score function, default: cos_sim")
    parser.add_argument("--k-values", default="1,3,5,10,20,50,100", help="Comma-separated evaluation k values")
    parser.add_argument("--max-queries", type=int, default=0, help="Optional smoke subset limit for queries; 0 keeps all")
    parser.add_argument("--max-corpus", type=int, default=0, help="Optional smoke subset limit for corpus; 0 keeps all")
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
        model_name=model.model_name,
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
    retriever = EvaluateRetrieval(
        DRES(model, batch_size=int(args.batch_size)),
        score_function=str(args.score_function),
        k_values=k_values,
    )

    started_at = time.perf_counter()
    results = retriever.retrieve(corpus, queries)
    ndcg, _map, recall, precision = retriever.evaluate(qrels, results, retriever.k_values)
    mrr = retriever.evaluate_custom(qrels, results, retriever.k_values, metric="mrr")
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

    payload = {
        "dataset": args.dataset,
        "split": args.split,
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
                "settings_local_model": str(settings.local_embedding_model or ""),
                "settings_local_dimension": int(settings.local_embedding_dimension or 0),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )

    util.save_runfile(str(paths.runfile_path), results)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved metrics: {paths.metrics_path}")
    print(f"Saved runfile: {paths.runfile_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
