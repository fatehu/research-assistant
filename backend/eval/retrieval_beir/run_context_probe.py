from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select

from app.api.knowledge import search_knowledge
from app.core.database import AsyncSessionLocal
from app.models.knowledge import KnowledgeBase
from app.models.user import User
from app.schemas.knowledge import SearchRequest


DEFAULT_OUTPUT_ROOT = BACKEND_ROOT / "eval" / "retrieval_beir" / "output"


def _slugify_model_name(model_name: str) -> str:
    token = str(model_name or "default").strip().lower()
    return "".join(ch if ch.isalnum() else "-" for ch in token).strip("-") or "default"


@dataclass
class EvalRunPaths:
    run_dir: Path
    metrics_path: Path
    config_path: Path


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
        run_dir=run_dir,
        metrics_path=run_dir / "metrics.json",
        config_path=run_dir / "config.json",
    )


class _ProbeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _collect_queries(args: argparse.Namespace) -> list[str]:
    queries: list[str] = []
    for item in args.query or []:
        value = str(item).strip()
        if value:
            queries.append(value)
    if args.queries_file:
        raw = Path(args.queries_file).expanduser().read_text(encoding="utf-8")
        for line in raw.splitlines():
            value = line.strip()
            if value:
                queries.append(value)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    if not deduped:
        raise SystemExit("At least one --query or --queries-file is required for context probe.")
    return deduped


def _adjacent_payload_size(payload: list[dict[str, Any]] | None) -> int:
    total = 0
    for item in payload or []:
        total += len(str(item.get("content") or ""))
    return total


async def _resolve_user_and_kb(knowledge_base_id: int, user_id: int | None) -> tuple[KnowledgeBase, User]:
    async with AsyncSessionLocal() as db:
        kb = await db.get(KnowledgeBase, knowledge_base_id)
        if kb is None:
            raise SystemExit(f"Knowledge base {knowledge_base_id} not found.")
        resolved_user_id = int(user_id or kb.user_id)
        user = await db.get(User, resolved_user_id)
        if user is None:
            raise SystemExit(f"User {resolved_user_id} not found.")
        return kb, user


async def _run_probe(
    *,
    knowledge_base_id: int,
    user_id: int | None,
    queries: list[str],
    top_k: int,
    mode: str,
    adjacent_window: int,
) -> tuple[KnowledgeBase, User, dict[str, Any]]:
    kb, user = await _resolve_user_and_kb(knowledge_base_id, user_id)
    payload = await _run_once(
        queries=queries,
        knowledge_base_id=knowledge_base_id,
        current_user=user,
        top_k=top_k,
        mode=mode,
        adjacent_window=adjacent_window,
    )
    return kb, user, payload


async def _run_once(
    *,
    queries: list[str],
    knowledge_base_id: int,
    current_user: User,
    top_k: int,
    mode: str,
    adjacent_window: int,
) -> dict[str, Any]:
    baseline_times: list[float] = []
    enabled_times: list[float] = []
    exact_order_matches = 0
    result_count = 0
    enriched_result_count = 0
    total_parent_chars = 0
    total_adjacent_items = 0
    total_adjacent_chars = 0
    section_backfill_count = 0
    per_query: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        for query in queries:
            base_request = SearchRequest(
                query=query,
                knowledge_base_ids=[knowledge_base_id],
                top_k=top_k,
                use_reranker=False,
                use_hybrid=False,
                use_query_rewrite=False,
                rewrite_mode="off",
                use_contextual_compression=False,
                include_parent_context=False,
                include_adjacent_chunks=False,
                adjacent_window=adjacent_window,
            )
            enabled_request = base_request.model_copy(
                update={
                    "include_parent_context": mode == "parent",
                    "include_adjacent_chunks": mode == "adjacent",
                }
            )

            baseline = await search_knowledge(
                base_request,
                http_request=_ProbeRequest(),
                include_shared=False,
                db=db,
                current_user=current_user,
            )
            enabled = await search_knowledge(
                enabled_request,
                http_request=_ProbeRequest(),
                include_shared=False,
                db=db,
                current_user=current_user,
            )

            baseline_ids = [item.chunk_id for item in baseline.results]
            enabled_ids = [item.chunk_id for item in enabled.results]
            if baseline_ids == enabled_ids:
                exact_order_matches += 1

            baseline_times.append(float(baseline.search_time_ms))
            enabled_times.append(float(enabled.search_time_ms))
            result_count += len(enabled.results)

            query_enriched = 0
            query_parent_chars = 0
            query_adjacent_items = 0
            query_adjacent_chars = 0
            query_section_backfill = 0

            for base_item, enabled_item in zip(baseline.results, enabled.results):
                if mode == "parent":
                    parent_context = str(enabled_item.parent_context or "")
                    if parent_context:
                        enriched_result_count += 1
                        query_enriched += 1
                        chars = len(parent_context)
                        total_parent_chars += chars
                        query_parent_chars += chars
                    if not (base_item.section_title or "") and (enabled_item.section_title or ""):
                        section_backfill_count += 1
                        query_section_backfill += 1
                else:
                    payload = enabled_item.metadata.get("adjacent_context") if isinstance(enabled_item.metadata, dict) else None
                    if isinstance(payload, list) and payload:
                        enriched_result_count += 1
                        query_enriched += 1
                        total_adjacent_items += len(payload)
                        query_adjacent_items += len(payload)
                        chars = _adjacent_payload_size(payload)
                        total_adjacent_chars += chars
                        query_adjacent_chars += chars

            per_query.append(
                {
                    "query": query,
                    "baseline_time_ms": float(baseline.search_time_ms),
                    "enabled_time_ms": float(enabled.search_time_ms),
                    "baseline_result_ids": baseline_ids,
                    "enabled_result_ids": enabled_ids,
                    "order_unchanged": baseline_ids == enabled_ids,
                    "result_count": len(enabled.results),
                    "enriched_result_count": query_enriched,
                    "parent_context_chars": query_parent_chars if mode == "parent" else 0,
                    "adjacent_items": query_adjacent_items if mode == "adjacent" else 0,
                    "adjacent_chars": query_adjacent_chars if mode == "adjacent" else 0,
                    "section_backfill_count": query_section_backfill if mode == "parent" else 0,
                }
            )

    query_count = max(1, len(queries))
    response_count = max(1, result_count)
    return {
        "mode": mode,
        "query_count": len(queries),
        "result_count": result_count,
        "order_unchanged_queries": exact_order_matches,
        "order_unchanged_ratio": round(exact_order_matches / query_count, 4),
        "baseline_avg_search_ms": round(sum(baseline_times) / query_count, 2),
        "enabled_avg_search_ms": round(sum(enabled_times) / query_count, 2),
        "avg_search_ms_delta": round((sum(enabled_times) - sum(baseline_times)) / query_count, 2),
        "enriched_result_count": enriched_result_count,
        "enriched_result_ratio": round(enriched_result_count / response_count, 4),
        "avg_parent_context_chars": round(total_parent_chars / max(1, enriched_result_count), 2),
        "section_backfill_count": section_backfill_count,
        "avg_adjacent_items": round(total_adjacent_items / max(1, enriched_result_count), 2),
        "avg_adjacent_chars": round(total_adjacent_chars / max(1, enriched_result_count), 2),
        "adjacent_window": adjacent_window if mode == "adjacent" else 0,
        "per_query": per_query,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe parent/adjacent context enrichment via backend search service.")
    parser.add_argument("--knowledge-base-id", type=int, required=True, help="Knowledge base id to probe")
    parser.add_argument("--user-id", type=int, default=0, help="Optional user id; defaults to KB owner")
    parser.add_argument("--query", action="append", default=None, help="Repeatable query text")
    parser.add_argument("--queries-file", default=None, help="Optional text file with one query per line")
    parser.add_argument("--top-k", type=int, default=5, help="Search top-k")
    parser.add_argument("--mode", choices=["parent", "adjacent"], required=True, help="Which context factor to probe")
    parser.add_argument("--adjacent-window", type=int, default=1, help="Adjacent window size when mode=adjacent")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Where probe outputs are written")
    args = parser.parse_args()

    queries = _collect_queries(args)
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    paths = _prepare_run_paths(
        dataset=f"kb{int(args.knowledge_base_id)}",
        output_root=output_root,
        model_name=f"{args.mode}-context-probe",
        dimension=0,
    )

    kb, user, payload = asyncio.run(
        _run_probe(
            knowledge_base_id=int(args.knowledge_base_id),
            user_id=int(args.user_id or 0),
            queries=queries,
            top_k=int(args.top_k),
            mode=str(args.mode),
            adjacent_window=int(args.adjacent_window),
        )
    )
    payload["knowledge_base"] = {
        "id": kb.id,
        "name": kb.name,
        "owner_user_id": kb.user_id,
        "embedding_dimension": kb.embedding_dimension,
    }
    payload["config"] = {
        "top_k": int(args.top_k),
        "queries": queries,
    }

    with paths.metrics_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    with paths.config_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "knowledge_base_id": int(args.knowledge_base_id),
                "user_id": user.id,
                "mode": str(args.mode),
                "adjacent_window": int(args.adjacent_window),
                "top_k": int(args.top_k),
                "queries": queries,
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
