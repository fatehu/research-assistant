"""
Evaluate retrieval quality for different embedding dimensions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.knowledge import DocumentChunk
from app.services.embedding_service import get_embedding_service


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate retrieval recall for dimensions such as 1024 vs 512.")
    parser.add_argument("--cases", required=True, help="Path to evaluation cases JSON file.")
    parser.add_argument("--top-k", type=int, default=10, help="Top-k for recall/mrr.")
    parser.add_argument("--dims", default="1024,512", help="Comma-separated dimensions to evaluate.")
    parser.add_argument("--limit-chunks", type=int, default=0, help="Limit corpus size for quick evaluation.")
    parser.add_argument("--score-repeat", type=int, default=64, help="Repeat score computation to stabilize latency.")
    parser.add_argument(
        "--latency-corpus-multiplier",
        type=int,
        default=1,
        help="Replicate corpus matrix for more stable latency benchmarking.",
    )
    parser.add_argument("--baseline-dim", type=int, default=1024, help="Baseline dimension for gate comparison.")
    parser.add_argument("--target-dim", type=int, default=512, help="Target dimension for gate comparison.")
    parser.add_argument(
        "--gate-max-recall-drop-pct",
        type=float,
        default=None,
        help="Gate: maximum allowed recall@k drop percentage (baseline-target).",
    )
    parser.add_argument(
        "--gate-min-latency-improve-pct",
        type=float,
        default=None,
        help="Gate: minimum required retrieval latency improvement percentage.",
    )
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="Exit non-zero when gate check fails.",
    )
    parser.add_argument("--output", default="", help="Optional markdown output path.")
    return parser


def _to_array(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        return value.astype(np.float32)
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return np.array(value, dtype=np.float32)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        if not text:
            return None
        try:
            return np.array([float(x.strip()) for x in text.split(",") if x.strip()], dtype=np.float32)
        except Exception:
            return None
    return None


def _normalize_rows(rows: list[tuple[int, np.ndarray]], dim: int) -> tuple[list[int], np.ndarray]:
    ids: list[int] = []
    vectors: list[np.ndarray] = []
    for chunk_id, vec in rows:
        if vec.shape[0] < dim:
            continue
        clipped = vec[:dim]
        norm = np.linalg.norm(clipped)
        if norm == 0:
            continue
        ids.append(chunk_id)
        vectors.append((clipped / norm).astype(np.float32))
    if not vectors:
        return [], np.zeros((0, dim), dtype=np.float32)
    return ids, np.vstack(vectors)


async def _load_corpus(kb_ids: list[int], limit_chunks: int) -> list[tuple[int, np.ndarray]]:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(DocumentChunk.id, DocumentChunk.embedding)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(DocumentChunk.id.asc())
        )
        if kb_ids:
            stmt = stmt.where(DocumentChunk.knowledge_base_id.in_(kb_ids))
        if limit_chunks > 0:
            stmt = stmt.limit(limit_chunks)
        rows = (await db.execute(stmt)).all()

    corpus: list[tuple[int, np.ndarray]] = []
    for row in rows:
        vec = _to_array(row.embedding)
        if vec is None or vec.size == 0:
            continue
        corpus.append((int(row.id), vec))
    return corpus


def _calc_recall_at_k(pred_ids: list[int], expected_ids: list[int], k: int) -> float:
    if not expected_ids:
        return 0.0
    hit = set(pred_ids[:k]).intersection(set(expected_ids))
    return len(hit) / float(len(expected_ids))


def _calc_mrr(pred_ids: list[int], expected_ids: list[int]) -> float:
    expected = set(expected_ids)
    for idx, chunk_id in enumerate(pred_ids, start=1):
        if chunk_id in expected:
            return 1.0 / float(idx)
    return 0.0


async def _embed_queries(queries: list[dict[str, Any]]) -> list[np.ndarray]:
    emb_svc = get_embedding_service()
    query_texts = [str(item["query"]) for item in queries]
    vectors: list[np.ndarray] = []
    try:
        batch = await emb_svc.embed_texts(query_texts, is_query=True)
        if len(batch) != len(query_texts):
            raise RuntimeError(f"embedding count mismatch: {len(batch)} vs {len(query_texts)}")
        vectors = [np.array(vec, dtype=np.float32) for vec in batch]
    except Exception as exc:
        logger.warning(f"[eval_retrieval_recall] batch embedding failed, fallback to single: {exc}")
        vectors = []
        for text in query_texts:
            vec = await emb_svc.embed_text(text, is_query=True)
            vectors.append(np.array(vec, dtype=np.float32))
    return vectors


async def _run(
    cases_path: Path,
    top_k: int,
    dims: list[int],
    limit_chunks: int,
    score_repeat: int,
    latency_corpus_multiplier: int,
    baseline_dim: int,
    target_dim: int,
    gate_max_recall_drop_pct: float | None,
    gate_min_latency_improve_pct: float | None,
) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8-sig"))
    query_items = payload.get("queries", [])
    kb_ids = payload.get("kb_ids", [])

    if not isinstance(query_items, list) or not query_items:
        raise ValueError("cases file must include non-empty `queries` list")

    valid_queries: list[dict[str, Any]] = []
    for raw in query_items:
        query = str(raw.get("query") or "").strip()
        expected_ids = [int(x) for x in raw.get("expected_chunk_ids", [])]
        if query and expected_ids:
            valid_queries.append(
                {
                    "id": str(raw.get("id") or f"q_{len(valid_queries)+1}"),
                    "query": query,
                    "expected_chunk_ids": expected_ids,
                }
            )
    if not valid_queries:
        raise ValueError("no valid queries with expected_chunk_ids found")

    corpus = await _load_corpus(kb_ids=kb_ids if isinstance(kb_ids, list) else [], limit_chunks=limit_chunks)
    if not corpus:
        raise ValueError("no embeddings loaded from database")

    query_embeddings = await _embed_queries(valid_queries)
    repeat_n = max(1, int(score_repeat))
    latency_mult = max(1, int(latency_corpus_multiplier))
    result: dict[str, Any] = {
        "cases": len(valid_queries),
        "dimensions": {},
        "score_repeat": repeat_n,
        "latency_corpus_multiplier": latency_mult,
        "gate": {},
    }

    for dim in dims:
        ids, matrix = _normalize_rows(corpus, dim)
        if matrix.shape[0] == 0:
            result["dimensions"][str(dim)] = {
                "queries": 0,
                "mean_recall_at_k": 0.0,
                "mean_mrr": 0.0,
                "mean_retrieval_latency_ms": 0.0,
            }
            continue

        eval_matrix = matrix
        eval_ids = ids
        if latency_mult > 1:
            eval_matrix = np.tile(matrix, (latency_mult, 1))
            eval_ids = ids * latency_mult

        recalls: list[float] = []
        mrrs: list[float] = []
        latencies: list[float] = []

        for idx, item in enumerate(valid_queries):
            q_arr = query_embeddings[idx]
            if q_arr.shape[0] < dim:
                continue
            q_arr = q_arr[:dim]
            q_norm = np.linalg.norm(q_arr)
            if q_norm == 0:
                continue
            q_arr = q_arr / q_norm

            # Warm-up once to reduce first-call bias.
            _ = eval_matrix @ q_arr
            started = time.perf_counter()
            scores = None
            for _ in range(repeat_n):
                scores = eval_matrix @ q_arr
            elapsed_ms = (time.perf_counter() - started) * 1000.0 / float(repeat_n)
            if scores is None:
                continue

            top_indices = np.argsort(-scores, kind="mergesort")[: max(1, top_k)]
            pred_ids = [eval_ids[int(i)] for i in top_indices.tolist()]

            expected_ids = item["expected_chunk_ids"]
            recalls.append(_calc_recall_at_k(pred_ids, expected_ids, top_k))
            mrrs.append(_calc_mrr(pred_ids, expected_ids))
            latencies.append(elapsed_ms)

        result["dimensions"][str(dim)] = {
            "queries": len(recalls),
            "mean_recall_at_k": round(float(np.mean(recalls)) if recalls else 0.0, 4),
            "mean_mrr": round(float(np.mean(mrrs)) if mrrs else 0.0, 4),
            "mean_retrieval_latency_ms": round(float(np.mean(latencies)) if latencies else 0.0, 4),
        }

    baseline = result["dimensions"].get(str(baseline_dim), {})
    target = result["dimensions"].get(str(target_dim), {})
    baseline_recall = float(baseline.get("mean_recall_at_k", 0.0) or 0.0)
    target_recall = float(target.get("mean_recall_at_k", 0.0) or 0.0)
    baseline_latency = float(baseline.get("mean_retrieval_latency_ms", 0.0) or 0.0)
    target_latency = float(target.get("mean_retrieval_latency_ms", 0.0) or 0.0)

    if baseline_recall > 0:
        recall_drop_pct = max(0.0, (baseline_recall - target_recall) / baseline_recall * 100.0)
    else:
        recall_drop_pct = 0.0 if target_recall >= baseline_recall else 100.0

    if baseline_latency > 0:
        latency_improve_pct = (baseline_latency - target_latency) / baseline_latency * 100.0
    else:
        latency_improve_pct = 0.0

    pass_recall = (
        True
        if gate_max_recall_drop_pct is None
        else recall_drop_pct <= float(gate_max_recall_drop_pct)
    )
    pass_latency = (
        True
        if gate_min_latency_improve_pct is None
        else latency_improve_pct >= float(gate_min_latency_improve_pct)
    )

    result["gate"] = {
        "baseline_dim": baseline_dim,
        "target_dim": target_dim,
        "recall_drop_pct": round(recall_drop_pct, 4),
        "latency_improve_pct": round(latency_improve_pct, 4),
        "max_recall_drop_pct": gate_max_recall_drop_pct,
        "min_latency_improve_pct": gate_min_latency_improve_pct,
        "pass_recall": pass_recall,
        "pass_latency": pass_latency,
        "pass": bool(pass_recall and pass_latency),
    }
    return result


def _render_markdown(summary: dict[str, Any], top_k: int, dims: list[int], cases_path: Path) -> str:
    lines = [
        "# 检索评测报告",
        "",
        f"- 用例文件: `{cases_path}`",
        f"- 样本数: {summary.get('cases', 0)}",
        f"- 指标: recall@{top_k}, MRR, mean_retrieval_latency_ms",
        f"- latency 统计口径: 仅向量评分阶段，score_repeat={summary.get('score_repeat', 1)}，"
        f"latency_corpus_multiplier={summary.get('latency_corpus_multiplier', 1)}",
        "",
        "| 维度 | 查询数 | mean_recall@k | mean_mrr | mean_retrieval_latency_ms |",
        "|---|---:|---:|---:|---:|",
    ]
    for dim in dims:
        item = summary["dimensions"].get(str(dim), {})
        lines.append(
            f"| {dim} | {item.get('queries', 0)} | "
            f"{item.get('mean_recall_at_k', 0.0)} | "
            f"{item.get('mean_mrr', 0.0)} | "
            f"{item.get('mean_retrieval_latency_ms', 0.0)} |"
        )

    gate = summary.get("gate", {})
    if gate:
        lines.extend(
            [
                "",
                "## 严格门槛判定",
                "",
                f"- baseline_dim: {gate.get('baseline_dim')}",
                f"- target_dim: {gate.get('target_dim')}",
                f"- recall_drop_pct: {gate.get('recall_drop_pct')}%",
                f"- latency_improve_pct: {gate.get('latency_improve_pct')}%",
                f"- max_recall_drop_pct: {gate.get('max_recall_drop_pct')}",
                f"- min_latency_improve_pct: {gate.get('min_latency_improve_pct')}",
                f"- pass_recall: {gate.get('pass_recall')}",
                f"- pass_latency: {gate.get('pass_latency')}",
                f"- pass: {gate.get('pass')}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    args = _build_parser().parse_args()
    cases_path = Path(args.cases)
    dims: list[int] = []
    for token in str(args.dims).split(","):
        token = token.strip()
        if not token:
            continue
        dim = int(token)
        if dim not in dims:
            dims.append(dim)
    if not dims:
        raise ValueError("at least one dimension is required")

    summary = asyncio.run(
        _run(
            cases_path=cases_path,
            top_k=max(1, int(args.top_k)),
            dims=dims,
            limit_chunks=max(0, int(args.limit_chunks)),
            score_repeat=max(1, int(args.score_repeat)),
            latency_corpus_multiplier=max(1, int(args.latency_corpus_multiplier)),
            baseline_dim=int(args.baseline_dim),
            target_dim=int(args.target_dim),
            gate_max_recall_drop_pct=args.gate_max_recall_drop_pct,
            gate_min_latency_improve_pct=args.gate_min_latency_improve_pct,
        )
    )
    markdown = _render_markdown(
        summary=summary,
        top_k=max(1, int(args.top_k)),
        dims=dims,
        cases_path=cases_path,
    )
    print(markdown)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        logger.info(f"[eval_retrieval_recall] report written to {output_path}")

    gate = summary.get("gate", {})
    if args.fail_on_gate and gate and not gate.get("pass", False):
        logger.error("[eval_retrieval_recall] gate check failed")
        sys.exit(2)


if __name__ == "__main__":
    main()
