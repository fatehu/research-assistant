"""
LLM-based contextual compression for retrieved chunks.
"""
import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from loguru import logger

from app.config import settings
from app.services.llm_service import LLMService


@dataclass
class CompressionInput:
    source_id: int
    doc_name: str
    chunk_idx: int
    chunk_content: str
    reranker_score: Optional[float] = None


@dataclass
class CompressionResult:
    source_id: int
    source_label: str
    doc_name: str
    chunk_idx: int
    relevant_content: str
    relevance_score: float
    used_compression: bool
    fallback_reason: Optional[str] = None
    raw_response: Optional[str] = None


class ContextualCompressionService:
    """
    Compress long retrieval chunks into query-relevant sentences only.
    """

    COMPRESS_PROMPT = """
你是一个信息提取助手。给定用户问题和文档片段，只保留与问题直接相关的句子。
用户问题：{query}
文档内容 [来源: {doc_name}, chunk: {chunk_idx}, 标签: [{source_label}]]:
{chunk_content}

请输出 JSON：
{{
  "relevant_content": "保留后的相关内容（必须带 [{source_label}] 标签）",
  "relevance_score": 0
}}
""".strip()

    BATCH_COMPRESS_PROMPT = """
你是检索结果压缩器。请根据用户问题，同时压缩多个文档片段，只保留直接相关句子。
用户问题：{query}

输入片段（JSON 数组）：
{chunks_json}

请严格输出 JSON（不要输出其他文字）：
{{
  "items": [
    {{
      "source_id": 1,
      "relevant_content": "相关内容，必须带 [来源1] 标签",
      "relevance_score": 0
    }}
  ]
}}
""".strip()

    SYSTEM_PROMPT = (
        "你是检索上下文压缩器。只保留直接相关信息，不编造，不扩展，必须输出可解析 JSON。"
    )

    def __init__(self):
        self._llm_service: Optional[LLMService] = None

    @staticmethod
    def _source_label(source_id: int) -> str:
        return f"来源{source_id}"

    def _ensure_llm_service(self) -> LLMService:
        if self._llm_service is None:
            self._llm_service = LLMService()
        return self._llm_service

    def _llm_available(self) -> bool:
        llm = self._ensure_llm_service()
        if llm.provider == "ollama":
            return True

        api_key = (llm.config.get("api_key") or "").strip()
        if not api_key:
            return False

        lower_key = api_key.lower()
        if lower_key in {"your-api-key", "changeme", "replace-me"}:
            return False
        if lower_key.startswith("your-") or lower_key.startswith("your_"):
            return False
        return True

    @staticmethod
    def _normalize_reranker_score(score: float) -> float:
        """Map raw reranker score to a stable 0-1 scale."""
        if score >= 0:
            z = math.exp(-score)
            return 1.0 / (1.0 + z)
        z = math.exp(score)
        return z / (1.0 + z)

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        value = ContextualCompressionService._extract_json_value(content)
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _extract_json_value(content: str) -> Any:
        text = (content or "").strip()
        if not text:
            return {}

        fenced = re.search(
            r"```(?:json)?\s*([\s\S]*?)\s*```",
            text,
            re.IGNORECASE,
        )
        if fenced:
            text = fenced.group(1).strip()

        for candidate in (text,):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        return {}

    @staticmethod
    def _parse_score(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            score = 0.0
        return max(0.0, min(10.0, score))

    @staticmethod
    def _extract_relevant_text_fallback(text: str) -> str:
        if not text:
            return ""

        patterns = [
            r"(?:relevant_content|content|相关内容)\s*[:：]?\s*([\s\S]*?)"
            r"(?:\n\s*(?:2[\.。、)]\s*)?(?:relevance_score|score|相关度|评分)|$)",
            r"1[\.。、)]\s*(?:relevant_content|content|相关内容)\s*[:：]?\s*([\s\S]*?)"
            r"(?:\n\s*(?:2[\.。、)]\s*)?(?:relevance_score|score|相关度|评分)|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.search(r"(?:相关度|评分|relevance_score|score)", stripped, re.IGNORECASE):
                continue
            lines.append(stripped)
        return "\n".join(lines).strip()

    @staticmethod
    def _extract_score_fallback(text: str) -> float:
        if not text:
            return 0.0

        match = re.search(
            r"(?:相关度|评分|relevance_score|score)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
            text,
            re.IGNORECASE,
        )
        if not match:
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*/\s*10", text)
        if not match:
            return 0.0
        return ContextualCompressionService._parse_score(match.group(1))

    @staticmethod
    def _normalize_relevant_content(text: str, source_label: str) -> str:
        content = (text or "").strip()
        if not content:
            return ""

        content = re.sub(r"^[-*\s]+", "", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        source_token = f"[{source_label}]"
        if source_token not in content:
            content = f"{source_token} {content}"
        return content.strip()

    @staticmethod
    def _split_batches(chunks: Sequence[CompressionInput], batch_size: int) -> list[list[CompressionInput]]:
        if batch_size <= 0:
            batch_size = 1
        return [
            list(chunks[idx : idx + batch_size])
            for idx in range(0, len(chunks), batch_size)
        ]

    @staticmethod
    def _tokenize_for_overlap(text: str) -> list[str]:
        if not text:
            return []
        normalized = text.lower()
        return re.findall(r"[\u4e00-\u9fff]|[a-z0-9_]+", normalized)

    def _extractive_fallback_text(
        self,
        query: str,
        chunk_content: str,
        source_label: str,
    ) -> str:
        raw = (chunk_content or "").strip()
        if not raw:
            return ""

        max_chars = max(256, settings.contextual_compression_max_chars_per_chunk)
        raw = raw[:max_chars]
        sentences = [
            seg.strip()
            for seg in re.split(r"(?<=[。！？!?\.])\s+|\n+", raw)
            if seg and seg.strip()
        ]
        if not sentences:
            return self._normalize_relevant_content(raw[:280], source_label)

        query_tokens = set(self._tokenize_for_overlap(query))

        scored: list[tuple[int, int, str]] = []
        for idx, sentence in enumerate(sentences):
            sent_tokens = set(self._tokenize_for_overlap(sentence))
            overlap = len(query_tokens & sent_tokens)
            bonus = 0
            if query and query in sentence:
                bonus += 2
            if any(len(token) > 3 and token in sentence.lower() for token in query_tokens):
                bonus += 1
            score = overlap + bonus
            scored.append((score, idx, sentence))

        scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = [item for item in scored if item[0] > 0][:2]
        if not selected:
            selected = scored[:2]

        selected = sorted(selected, key=lambda item: item[1])
        merged = " ".join(item[2] for item in selected).strip()
        if not merged:
            merged = raw[:280]
        return self._normalize_relevant_content(merged, source_label)

    def _result_with_fallback(
        self,
        query: str,
        chunk: CompressionInput,
        source_label: str,
        fallback_reason: str,
        *,
        relevance_score: float = 0.0,
        raw_response: Optional[str] = None,
        used_compression: bool = False,
    ) -> CompressionResult:
        extracted = self._extractive_fallback_text(query, chunk.chunk_content, source_label)
        return CompressionResult(
            source_id=chunk.source_id,
            source_label=source_label,
            doc_name=chunk.doc_name,
            chunk_idx=chunk.chunk_idx,
            relevant_content=extracted,
            relevance_score=relevance_score,
            used_compression=used_compression,
            fallback_reason=fallback_reason,
            raw_response=raw_response,
        )

    def _result_for_high_reranker(
        self,
        chunk: CompressionInput,
        source_label: str,
    ) -> CompressionResult:
        max_chars = max(180, min(settings.contextual_compression_max_chars_per_chunk, 420))
        direct_text = self._normalize_relevant_content(
            (chunk.chunk_content or "")[:max_chars],
            source_label,
        )
        return CompressionResult(
            source_id=chunk.source_id,
            source_label=source_label,
            doc_name=chunk.doc_name,
            chunk_idx=chunk.chunk_idx,
            relevant_content=direct_text,
            relevance_score=10.0,
            used_compression=False,
            fallback_reason="skip_high_reranker",
            raw_response=None,
        )

    def _parse_compression_response(
        self,
        raw_text: str,
        source_label: str,
    ) -> tuple[str, float]:
        payload = self._extract_json(raw_text)

        if payload:
            relevant = str(payload.get("relevant_content") or payload.get("content") or "").strip()
            score = self._parse_score(payload.get("relevance_score", payload.get("score", 0)))
        else:
            relevant = ""
            score = 0.0

        if not relevant:
            relevant = self._extract_relevant_text_fallback(raw_text)
        if score <= 0.0:
            score = self._extract_score_fallback(raw_text)

        return self._normalize_relevant_content(relevant, source_label), score

    def _parse_batch_compression_response(self, raw_text: str) -> dict[int, tuple[str, float]]:
        parsed = self._extract_json_value(raw_text)
        items: list[dict[str, Any]] = []

        if isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
        elif isinstance(parsed, dict):
            possible_items = parsed.get("items") or parsed.get("results") or parsed.get("chunks")
            if isinstance(possible_items, list):
                items = [item for item in possible_items if isinstance(item, dict)]
            elif "source_id" in parsed:
                items = [parsed]

        results: dict[int, tuple[str, float]] = {}
        for item in items:
            try:
                source_id = int(item.get("source_id"))
            except (TypeError, ValueError):
                continue
            relevant = str(item.get("relevant_content") or item.get("content") or "").strip()
            score = self._parse_score(item.get("relevance_score", item.get("score", 0)))
            results[source_id] = (relevant, score)
        return results

    async def compress_chunk(
        self,
        query: str,
        chunk: CompressionInput,
        *,
        use_contextual_compression: bool = True,
    ) -> CompressionResult:
        source_label = self._source_label(chunk.source_id)
        raw_chunk = (chunk.chunk_content or "").strip()

        if not raw_chunk:
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=0.0,
                used_compression=False,
                fallback_reason="empty_chunk",
            )

        if not settings.enable_contextual_compression or not use_contextual_compression:
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=0.0,
                used_compression=False,
                fallback_reason="disabled",
            )

        threshold = max(0.0, min(settings.contextual_compression_skip_rerank_threshold, 1.0))
        if chunk.reranker_score is not None and self._normalize_reranker_score(float(chunk.reranker_score)) >= threshold:
            return self._result_for_high_reranker(chunk, source_label)

        if not self._llm_available():
            return self._result_with_fallback(
                query,
                chunk,
                source_label,
                "llm_unavailable_extractive",
            )

        max_chars = max(256, settings.contextual_compression_max_chars_per_chunk)
        chunk_text = raw_chunk[:max_chars]
        prompt = self.COMPRESS_PROMPT.format(
            query=query,
            doc_name=chunk.doc_name or "unknown_doc",
            chunk_idx=chunk.chunk_idx,
            source_label=source_label,
            chunk_content=chunk_text,
        )

        llm = self._ensure_llm_service()
        try:
            response = await asyncio.wait_for(
                llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=self.SYSTEM_PROMPT,
                    temperature=settings.contextual_compression_temperature,
                    max_tokens=min(
                        settings.llm_max_tokens,
                        settings.contextual_compression_max_output_tokens,
                    ),
                    source="retrieval.contextual_compression.single",
                ),
                timeout=max(1, settings.contextual_compression_timeout_seconds),
            )
        except Exception as exc:
            logger.warning(f"[ContextualCompression] compression failed: {exc}")
            return self._result_with_fallback(
                query,
                chunk,
                source_label,
                "compression_error_extractive",
            )

        raw_response = str(response.get("content") or "").strip()
        relevant_content, relevance_score = self._parse_compression_response(raw_response, source_label)

        if relevance_score < settings.contextual_compression_min_relevance:
            return self._result_with_fallback(
                query,
                chunk,
                source_label,
                "low_relevance_extractive",
                relevance_score=relevance_score,
                raw_response=raw_response,
                used_compression=True,
            )

        if not relevant_content:
            return self._result_with_fallback(
                query,
                chunk,
                source_label,
                "no_relevant_sentence_extractive",
                relevance_score=relevance_score,
                raw_response=raw_response,
                used_compression=True,
            )

        return CompressionResult(
            source_id=chunk.source_id,
            source_label=source_label,
            doc_name=chunk.doc_name,
            chunk_idx=chunk.chunk_idx,
            relevant_content=relevant_content,
            relevance_score=relevance_score,
            used_compression=True,
            fallback_reason=None,
            raw_response=raw_response,
        )

    async def _compress_batch_once(
        self,
        query: str,
        chunks: Sequence[CompressionInput],
    ) -> str:
        llm = self._ensure_llm_service()
        max_chars = max(256, settings.contextual_compression_max_chars_per_chunk)
        payload = [
            {
                "source_id": chunk.source_id,
                "doc_name": chunk.doc_name or "unknown_doc",
                "chunk_idx": chunk.chunk_idx,
                "chunk_content": (chunk.chunk_content or "")[:max_chars],
            }
            for chunk in chunks
        ]
        prompt = self.BATCH_COMPRESS_PROMPT.format(
            query=query,
            chunks_json=json.dumps(payload, ensure_ascii=False),
        )
        response = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=self.SYSTEM_PROMPT,
                temperature=settings.contextual_compression_temperature,
                max_tokens=min(
                    settings.llm_max_tokens,
                    settings.contextual_compression_max_output_tokens * max(1, len(chunks)),
                ),
                source="retrieval.contextual_compression.batch",
            ),
            timeout=max(1, settings.contextual_compression_timeout_seconds),
        )
        return str(response.get("content") or "").strip()

    async def compress_chunks_batched(
        self,
        query: str,
        chunks: Sequence[CompressionInput],
        *,
        use_contextual_compression: bool = True,
    ) -> list[CompressionResult]:
        if not chunks:
            return []

        results: dict[int, CompressionResult] = {}
        pending: list[CompressionInput] = []

        threshold = max(0.0, min(settings.contextual_compression_skip_rerank_threshold, 1.0))
        for chunk in chunks:
            source_label = self._source_label(chunk.source_id)
            raw_chunk = (chunk.chunk_content or "").strip()
            if not raw_chunk:
                results[chunk.source_id] = CompressionResult(
                    source_id=chunk.source_id,
                    source_label=source_label,
                    doc_name=chunk.doc_name,
                    chunk_idx=chunk.chunk_idx,
                    relevant_content="",
                    relevance_score=0.0,
                    used_compression=False,
                    fallback_reason="empty_chunk",
                )
                continue

            if not settings.enable_contextual_compression or not use_contextual_compression:
                results[chunk.source_id] = CompressionResult(
                    source_id=chunk.source_id,
                    source_label=source_label,
                    doc_name=chunk.doc_name,
                    chunk_idx=chunk.chunk_idx,
                    relevant_content="",
                    relevance_score=0.0,
                    used_compression=False,
                    fallback_reason="disabled",
                )
                continue

            if chunk.reranker_score is not None and self._normalize_reranker_score(float(chunk.reranker_score)) >= threshold:
                results[chunk.source_id] = self._result_for_high_reranker(chunk, source_label)
                continue

            pending.append(chunk)

        if not pending:
            return [results[item.source_id] for item in chunks if item.source_id in results]

        if not self._llm_available():
            for chunk in pending:
                source_label = self._source_label(chunk.source_id)
                results[chunk.source_id] = self._result_with_fallback(
                    query,
                    chunk,
                    source_label,
                    "llm_unavailable_extractive",
                )
            return [results[item.source_id] for item in chunks if item.source_id in results]

        batch_size = max(1, settings.contextual_compression_batch_max_chunks)
        retry_attempts = max(1, settings.contextual_compression_batch_retry_attempts)
        batches = self._split_batches(pending, batch_size)

        for batch in batches:
            success = False
            for attempt in range(1, retry_attempts + 1):
                try:
                    raw_response = await self._compress_batch_once(query, batch)
                    parsed_items = self._parse_batch_compression_response(raw_response)
                    if not parsed_items:
                        raise ValueError("empty batch compression payload")

                    for chunk in batch:
                        source_label = self._source_label(chunk.source_id)
                        parsed = parsed_items.get(chunk.source_id)
                        if not parsed:
                            results[chunk.source_id] = self._result_with_fallback(
                                query,
                                chunk,
                                source_label,
                                "batch_missing_item_extractive",
                                raw_response=raw_response,
                                used_compression=True,
                            )
                            continue

                        relevant_text, relevance_score = parsed
                        normalized = self._normalize_relevant_content(relevant_text, source_label)

                        if relevance_score < settings.contextual_compression_min_relevance:
                            results[chunk.source_id] = self._result_with_fallback(
                                query,
                                chunk,
                                source_label,
                                "batch_low_relevance_extractive",
                                relevance_score=relevance_score,
                                raw_response=raw_response,
                                used_compression=True,
                            )
                            continue

                        if not normalized:
                            results[chunk.source_id] = self._result_with_fallback(
                                query,
                                chunk,
                                source_label,
                                "batch_no_relevant_sentence_extractive",
                                relevance_score=relevance_score,
                                raw_response=raw_response,
                                used_compression=True,
                            )
                            continue

                        results[chunk.source_id] = CompressionResult(
                            source_id=chunk.source_id,
                            source_label=source_label,
                            doc_name=chunk.doc_name,
                            chunk_idx=chunk.chunk_idx,
                            relevant_content=normalized,
                            relevance_score=relevance_score,
                            used_compression=True,
                            fallback_reason=None,
                            raw_response=raw_response,
                        )
                    success = True
                    break
                except Exception as exc:
                    logger.warning(
                        f"[ContextualCompression] batch compression failed (attempt {attempt}/{retry_attempts}): {exc}"
                    )
                    if attempt < retry_attempts:
                        await asyncio.sleep(0)

            if success:
                continue

            for chunk in batch:
                source_label = self._source_label(chunk.source_id)
                results[chunk.source_id] = self._result_with_fallback(
                    query,
                    chunk,
                    source_label,
                    "batch_compression_error_extractive",
                )

        return [results[item.source_id] for item in chunks if item.source_id in results]

    async def compress_chunks(
        self,
        query: str,
        chunks: Sequence[CompressionInput],
        *,
        use_contextual_compression: bool = True,
    ) -> list[CompressionResult]:
        if not chunks:
            return []

        if settings.contextual_compression_mode == "batch":
            return await self.compress_chunks_batched(
                query,
                chunks,
                use_contextual_compression=use_contextual_compression,
            )

        concurrency = max(1, settings.contextual_compression_max_concurrency)
        semaphore = asyncio.Semaphore(concurrency)

        async def _run(chunk: CompressionInput) -> CompressionResult:
            async with semaphore:
                return await self.compress_chunk(
                    query,
                    chunk,
                    use_contextual_compression=use_contextual_compression,
                )

        return list(await asyncio.gather(*(_run(chunk) for chunk in chunks)))


_contextual_compression_service = ContextualCompressionService()


def get_contextual_compression_service() -> ContextualCompressionService:
    """Get global contextual compression service instance."""
    return _contextual_compression_service
