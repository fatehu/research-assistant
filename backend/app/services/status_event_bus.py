"""
状态事件总线（状态机事件推送）。

优先使用 Redis Pub/Sub，在 Redis 不可用时降级为进程内队列。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime
from inspect import isawaitable
from typing import Any, AsyncIterator, Dict

from loguru import logger

from app.config import settings

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover - runtime optional
    redis_async = None


_LOCAL_QUEUE_MAXSIZE = 256
_DEFAULT_HEARTBEAT_SECONDS = 15.0

_local_channels: Dict[str, set[asyncio.Queue[str]]] = defaultdict(set)
_local_channels_lock = asyncio.Lock()
_redis_publisher = None


def build_status_channel_for_user(user_id: int) -> str:
    return f"status-events:user:{int(user_id)}"


def _serialize_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _deserialize_payload(raw: str) -> Dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _redis_url() -> str:
    return (getattr(settings, "redis_url", "") or "").strip()


async def _maybe_close(resource: Any) -> None:
    if resource is None:
        return
    close_fn = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close_fn is None:
        return
    result = close_fn()
    if isawaitable(result):
        await result


async def _get_redis_publisher():
    global _redis_publisher
    if redis_async is None:
        return None
    if _redis_publisher is not None:
        return _redis_publisher
    url = _redis_url()
    if not url:
        return None
    try:
        _redis_publisher = redis_async.from_url(url, decode_responses=True)
        return _redis_publisher
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        logger.warning(f"[StatusEventBus] Redis 发布端初始化失败，降级本地队列: {exc}")
        _redis_publisher = None
        return None


async def _publish_local(channel: str, payload: str) -> None:
    async with _local_channels_lock:
        queues = list(_local_channels.get(channel, set()))

    for queue in queues:
        if queue.full():
            try:
                queue.get_nowait()
            except Exception:
                pass
        try:
            queue.put_nowait(payload)
        except Exception:
            # 单个订阅者异常不影响全局广播
            continue


async def publish_status_event(channel: str, payload: Dict[str, Any]) -> None:
    """
    发布状态事件。
    优先 Redis，失败后回退本地队列。
    """
    serialized = _serialize_payload(payload)

    redis_client = await _get_redis_publisher()
    if redis_client is not None:
        try:
            await redis_client.publish(channel, serialized)
            return
        except Exception as exc:  # pragma: no cover - runtime dependent
            logger.warning(f"[StatusEventBus] Redis 发布失败，降级本地队列: {exc}")

    await _publish_local(channel, serialized)


async def _register_local_subscriber(channel: str) -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_LOCAL_QUEUE_MAXSIZE)
    async with _local_channels_lock:
        _local_channels[channel].add(queue)
    return queue


async def _unregister_local_subscriber(channel: str, queue: asyncio.Queue[str]) -> None:
    async with _local_channels_lock:
        subscribers = _local_channels.get(channel)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            _local_channels.pop(channel, None)


async def iter_status_events(
    channel: str,
    *,
    heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
) -> AsyncIterator[Dict[str, Any]]:
    """
    订阅状态事件流。
    Redis 可用时使用 Redis 订阅；否则回退进程内队列。
    """
    heartbeat_interval = max(5.0, float(heartbeat_seconds))
    last_heartbeat = time.monotonic()

    redis_client = None
    redis_pubsub = None
    local_queue: asyncio.Queue[str] | None = None

    if redis_async is not None and _redis_url():
        try:
            redis_client = redis_async.from_url(_redis_url(), decode_responses=True)
            redis_pubsub = redis_client.pubsub()
            await redis_pubsub.subscribe(channel)
        except Exception as exc:  # pragma: no cover - runtime dependent
            logger.warning(f"[StatusEventBus] Redis 订阅失败，降级本地队列: {exc}")
            await _maybe_close(redis_pubsub)
            await _maybe_close(redis_client)
            redis_pubsub = None
            redis_client = None

    if redis_pubsub is None:
        local_queue = await _register_local_subscriber(channel)

    try:
        while True:
            emitted = False
            if redis_pubsub is not None:
                message = await redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message.get("type") == "message":
                    payload = _deserialize_payload(str(message.get("data", "")))
                    if payload is not None:
                        yield payload
                        emitted = True
            else:
                try:
                    raw = await asyncio.wait_for(local_queue.get(), timeout=1.0)  # type: ignore[arg-type]
                    payload = _deserialize_payload(raw)
                    if payload is not None:
                        yield payload
                        emitted = True
                except asyncio.TimeoutError:
                    pass

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                yield {
                    "event": "heartbeat",
                    "data": {"ts": datetime.utcnow().isoformat()},
                }
                last_heartbeat = now

            if not emitted:
                await asyncio.sleep(0.05)
    finally:
        if local_queue is not None:
            await _unregister_local_subscriber(channel, local_queue)
        await _maybe_close(redis_pubsub)
        await _maybe_close(redis_client)
