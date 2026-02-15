"""
Simple API rate limiting with Redis-first storage and in-memory fallback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request
from jose import JWTError, jwt
from loguru import logger

from app.config import settings

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover - optional import path in some envs
    redis_async = None


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after_seconds: int


class _InMemoryRateStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._store: Dict[str, Tuple[int, int]] = {}

    async def increment(
        self,
        *,
        key: str,
        window_seconds: int,
        limit: int,
    ) -> RateLimitResult:
        now = int(time.time())
        async with self._lock:
            reset_at, count = self._store.get(key, (now + window_seconds, 0))
            if now >= reset_at:
                reset_at = now + window_seconds
                count = 0

            count += 1
            self._store[key] = (reset_at, count)

        remaining = max(0, limit - count)
        reset_seconds = max(1, reset_at - now)
        allowed = count <= limit
        retry_after = reset_seconds if not allowed else 0
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_seconds=reset_seconds,
            retry_after_seconds=retry_after,
        )


class _RedisRateStore:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Optional["redis_async.Redis"] = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        if redis_async is None:
            raise RuntimeError("redis.asyncio is unavailable")
        async with self._lock:
            if self._client is None:
                self._client = redis_async.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def increment(
        self,
        *,
        key: str,
        window_seconds: int,
        limit: int,
    ) -> RateLimitResult:
        client = await self._ensure_client()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, window_seconds)

        ttl = int(await client.ttl(key))
        reset_seconds = max(1, ttl if ttl > 0 else window_seconds)
        remaining = max(0, limit - count)
        allowed = count <= limit
        retry_after = reset_seconds if not allowed else 0
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_seconds=reset_seconds,
            retry_after_seconds=retry_after,
        )


class APIRateLimiter:
    def __init__(self) -> None:
        self._memory_store = _InMemoryRateStore()
        self._redis_store: Optional[_RedisRateStore] = None
        self._use_redis = False

        storage_mode = str(getattr(settings, "api_rate_limit_storage", "auto")).strip().lower()
        redis_url = (
            str(getattr(settings, "api_rate_limit_redis_url", "") or "").strip()
            or str(getattr(settings, "redis_url", "") or "").strip()
        )
        wants_redis = storage_mode in {"auto", "redis"} and bool(redis_url)
        if wants_redis and redis_async is not None:
            self._redis_store = _RedisRateStore(redis_url)
            self._use_redis = True
        elif storage_mode == "redis":
            logger.warning("[RateLimit] redis mode requested but redis is unavailable; fallback to memory")

    @staticmethod
    def _decode_token(token: str) -> Optional[Dict[str, str]]:
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
            if isinstance(payload, dict):
                return payload
        except JWTError:
            return None
        return None

    @staticmethod
    def _extract_actor_key(request: Request, scope: str) -> str:
        ip = (request.client.host if request.client else "unknown").strip() or "unknown"
        if scope == "ip":
            return ip

        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            payload = APIRateLimiter._decode_token(token)
            if payload:
                user_id = str(payload.get("sub", "")).strip()
                if user_id:
                    return f"user:{user_id}"
        return f"ip:{ip}"

    async def check(self, *, request: Request, bucket: str, scope: str, limit: int) -> RateLimitResult:
        window_seconds = max(1, int(getattr(settings, "api_rate_limit_window_seconds", 60)))
        actor = self._extract_actor_key(request, scope=scope)
        window_index = int(time.time() // window_seconds)
        key = f"rl:{bucket}:{actor}:{window_index}"

        if self._use_redis and self._redis_store is not None:
            try:
                return await self._redis_store.increment(key=key, window_seconds=window_seconds, limit=limit)
            except Exception as exc:
                logger.warning(f"[RateLimit] redis store failed, fallback to memory: {exc}")
                self._use_redis = False

        return await self._memory_store.increment(key=key, window_seconds=window_seconds, limit=limit)


rate_limiter = APIRateLimiter()


def _rate_headers(result: RateLimitResult) -> Dict[str, str]:
    return {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_seconds),
    }


def build_rate_limit_dependency(*, bucket: str, limit: int, scope: str = "user_or_ip"):
    async def _dependency(request: Request):
        if not bool(getattr(settings, "api_rate_limit_enabled", True)):
            return

        result = await rate_limiter.check(request=request, bucket=bucket, scope=scope, limit=max(1, int(limit)))
        headers = _rate_headers(result)
        request.state.rate_limit_headers = headers

        if result.allowed:
            return

        headers["Retry-After"] = str(max(1, result.retry_after_seconds))
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "请求过于频繁，请稍后重试",
                "retry_after_seconds": max(1, result.retry_after_seconds),
            },
            headers=headers,
        )

    return _dependency
