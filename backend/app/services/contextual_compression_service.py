"""
LLM-based contextual compression for retrieved chunks.
"""
import asyncio
import json
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
你是一个信息提取助手。给定一个用户问题和一段文档内容，
请只提取与问题直接相关的句子，并标注来源。

用户问题：{query}
文档内容 [来源: {doc_name}, 第{chunk_idx}段, 引用标签: [{source_label}]]：
{chunk_content}

请输出：
1. 相关内容（只保留相关句子，用 [{source_label}] 标注）
2. 相关度评分（0-10）

请额外输出一个 JSON 对象（必须可被 json.loads 解析）：
{{
  "relevant_content": "相关内容",
  "relevance_score": 0
}}
""".strip()

    SYSTEM_PROMPT = (
        "你是检索上下文压缩器。只保留与问题直接相关的信息，不要编造，不要扩展。"
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
    def _extract_json(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if not text:
            return {}

        fenced = re.search(
            r"```(?:json)?\s*(\{[\s\S]*\})\s*```",
            text,
            re.IGNORECASE,
        )
        if fenced:
            text = fenced.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return {}
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
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
            r"(?:relevant_content|content|相关内容)\s*[:：]\s*([\s\S]*?)"
            r"(?:\n\s*(?:2[\.、\)]\s*)?(?:relevance_score|score|相关度评分|评分)|$)",
            r"1[\.、\)]\s*(?:relevant_content|content|相关内容)\s*[:：]?\s*([\s\S]*?)"
            r"(?:\n\s*(?:2[\.、\)]\s*)?(?:relevance_score|score|相关度评分|评分)|$)",
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
            if re.search(r"(?:相关度评分|评分|relevance_score|score)", stripped, re.IGNORECASE):
                continue
            lines.append(stripped)
        return "\n".join(lines).strip()

    @staticmethod
    def _extract_score_fallback(text: str) -> float:
        if not text:
            return 0.0

        match = re.search(
            r"(?:相关度评分|评分|relevance_score|score)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
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

    def _parse_compression_response(
        self,
        raw_text: str,
        source_label: str,
    ) -> tuple[str, float]:
        payload = self._extract_json(raw_text)

        if payload:
            relevant = str(
                payload.get("relevant_content")
                or payload.get("content")
                or ""
            ).strip()
            score = self._parse_score(
                payload.get("relevance_score", payload.get("score", 0))
            )
        else:
            relevant = ""
            score = 0.0

        if not relevant:
            relevant = self._extract_relevant_text_fallback(raw_text)
        if score <= 0.0:
            score = self._extract_score_fallback(raw_text)

        return self._normalize_relevant_content(relevant, source_label), score

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

        if not self._llm_available():
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=0.0,
                used_compression=False,
                fallback_reason="llm_unavailable",
            )

        max_chars = max(256, settings.contextual_compression_max_chars_per_chunk)
        chunk_text = raw_chunk[:max_chars]
        prompt = self.COMPRESS_PROMPT.format(
            query=query,
            doc_name=chunk.doc_name or "未知文档",
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
                ),
                timeout=max(1, settings.contextual_compression_timeout_seconds),
            )
        except Exception as exc:
            logger.warning(f"[ContextualCompression] compression failed: {exc}")
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=0.0,
                used_compression=False,
                fallback_reason="compression_error",
            )

        raw_response = str(response.get("content") or "").strip()
        relevant_content, relevance_score = self._parse_compression_response(
            raw_response,
            source_label,
        )

        if relevance_score < settings.contextual_compression_min_relevance:
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=relevance_score,
                used_compression=True,
                fallback_reason="low_relevance",
                raw_response=raw_response,
            )

        if not relevant_content:
            return CompressionResult(
                source_id=chunk.source_id,
                source_label=source_label,
                doc_name=chunk.doc_name,
                chunk_idx=chunk.chunk_idx,
                relevant_content="",
                relevance_score=relevance_score,
                used_compression=True,
                fallback_reason="no_relevant_sentence",
                raw_response=raw_response,
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

    async def compress_chunks(
        self,
        query: str,
        chunks: Sequence[CompressionInput],
        *,
        use_contextual_compression: bool = True,
    ) -> list[CompressionResult]:
        if not chunks:
            return []

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
