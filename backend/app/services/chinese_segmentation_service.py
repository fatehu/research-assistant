"""
Chinese segmentation utilities for PostgreSQL simple FTS.
"""
from __future__ import annotations

import re
from functools import lru_cache

from loguru import logger


_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")


@lru_cache(maxsize=1)
def _load_jieba():
    try:
        import jieba  # type: ignore

        return jieba
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[ChineseSegmentation] jieba unavailable, fallback to raw text: {exc}")
        return None


def contains_cjk(text: str) -> bool:
    return bool(_CJK_PATTERN.search(text or ""))


def segment_text_for_fts(text: str) -> str:
    """
    Segment text for PostgreSQL `simple` FTS.
    - CJK text: jieba cut and join by spaces.
    - Non-CJK text: keep alnum tokens with single spaces.
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    if not contains_cjk(raw):
        tokens = _TOKEN_PATTERN.findall(raw)
        return " ".join(tokens) if tokens else raw

    jieba = _load_jieba()
    if jieba is None:
        return raw

    try:
        tokens = [token.strip() for token in jieba.cut(raw) if token and token.strip()]
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[ChineseSegmentation] jieba.cut failed, fallback to raw text: {exc}")
        return raw

    return " ".join(tokens) if tokens else raw

