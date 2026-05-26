from __future__ import annotations

import asyncio
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import structlog

from core.timeline_payloads import TimelineEventPayload


logger = structlog.get_logger(__name__)

_TIMELINE_RATE_LIMIT_MAX = int(os.getenv("TIMELINE_REALTIME_RATE_LIMIT", "30"))
_TIMELINE_RATE_LIMIT_WINDOW = int(os.getenv("TIMELINE_REALTIME_RATE_WINDOW", "60"))

_REDIS_URL = os.getenv("REDIS_URL", "")


def _new_redis_client() -> Optional[Any]:
    if not _REDIS_URL:
        return None
    try:
        import redis as _redis_mod
        return _redis_mod.from_url(_REDIS_URL, decode_responses=True)
    except Exception:
        logger.warning("timeline_realtime_redis_unavailable")
        return None


class _SlidingWindowRateLimiter:
    """Per-case publish rate limiter with Redis-backed shared state for multi-worker."""

    def __init__(self) -> None:
        self._redis: Optional[Any] = _new_redis_client()
        self._local: Dict[int, list] = {}

    def allow(self, case_id: int) -> bool:
        now = time.time()
        key = f"tl_rl:{case_id}"

        if self._redis is not None:
            window_start = now - _TIMELINE_RATE_LIMIT_WINDOW
            try:
                pipe = self._redis.pipeline()
                pipe.zremrangebyscore(key, 0, window_start)
                pipe.zcard(key)
                count = pipe.execute()[1]
                if count >= _TIMELINE_RATE_LIMIT_MAX:
                    return False
                pipe = self._redis.pipeline()
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, _TIMELINE_RATE_LIMIT_WINDOW * 2)
                pipe.execute()
                return True
            except Exception:
                pass

        entries = self._local.setdefault(case_id, [])
        cutoff = now - _TIMELINE_RATE_LIMIT_WINDOW
        self._local[case_id] = [t for t in entries if t > cutoff]
        if len(self._local[case_id]) >= _TIMELINE_RATE_LIMIT_MAX:
            return False
        self._local[case_id].append(now)
        return True


@dataclass
class _CaseChannel:
    connections: Set[tuple["asyncio.Queue[Dict[str, Any]]", asyncio.AbstractEventLoop]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dropped_messages: int = 0


class TimelineRealtimeBus:
    """
    Simple in-memory case-scoped pub/sub bus with distributed rate limiting.

    - Each websocket connection subscribes by providing an asyncio.Queue
    - Writers broadcast a JSON-serializable payload to all subscribers of
      the given case_id.
    - Publish rate is throttled per case_id using Redis (or in-memory fallback)
      to prevent subscriber flooding in multi-worker deployments.
    """

    def __init__(self, queue_maxsize: int = 100) -> None:
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._channels: Dict[int, _CaseChannel] = {}
        self._global_lock = asyncio.Lock()
        self._drop_lock = threading.Lock()
        self._dropped_messages_total = 0
        self._rate_limiter = _SlidingWindowRateLimiter()

    @property
    def queue_maxsize(self) -> int:
        return self._queue_maxsize

    @property
    def dropped_messages_total(self) -> int:
        with self._drop_lock:
            return self._dropped_messages_total

    def _record_drop(self, case_id: int, channel: _CaseChannel) -> None:
        with self._drop_lock:
            self._dropped_messages_total += 1
            total_dropped = self._dropped_messages_total
            channel.dropped_messages += 1
            case_dropped = channel.dropped_messages

        logger.warning(
            "timeline_realtime_queue_dropped",
            case_id=case_id,
            queue_maxsize=self._queue_maxsize,
            dropped_messages=1,
            total_dropped_messages=total_dropped,
            case_dropped_messages=case_dropped,
            policy="drop_oldest_keep_latest",
        )

    @staticmethod
    def _deliver_message(
        *,
        queue: asyncio.Queue[Dict[str, Any]],
        message: Dict[str, Any],
        case_id: int,
        channel: _CaseChannel,
        record_drop,
    ) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                record_drop(case_id, channel)

        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            # If another producer filled the queue between eviction and put,
            # drop the oldest item once more and keep the newest payload.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            record_drop(case_id, channel)
            queue.put_nowait(message)

    async def _get_or_create_channel(self, case_id: int) -> _CaseChannel:
        async with self._global_lock:
            if case_id not in self._channels:
                self._channels[case_id] = _CaseChannel()
            return self._channels[case_id]

    async def subscribe(self, case_id: int) -> asyncio.Queue[Dict[str, Any]]:
        channel = await self._get_or_create_channel(case_id)
        q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        loop = asyncio.get_running_loop()
        async with channel.lock:
            channel.connections.add((q, loop))
        return q

    async def unsubscribe(self, case_id: int, q: asyncio.Queue[Dict[str, Any]]) -> None:
        async with self._global_lock:
            channel = self._channels.get(case_id)
            if channel is None:
                return
        async with channel.lock:
            channel.connections = {subscriber for subscriber in channel.connections if subscriber[0] is not q}
            if not channel.connections:
                async with self._global_lock:
                    if self._channels.get(case_id) is channel:
                        del self._channels[case_id]

    async def close(self) -> None:
        async with self._global_lock:
            self._channels.clear()

    async def publish(self, case_id: int, payload: Dict[str, Any]) -> None:
        if not self._rate_limiter.allow(case_id):
            logger.warning(
                "timeline_realtime_publish_rate_limited",
                case_id=case_id,
                limit=_TIMELINE_RATE_LIMIT_MAX,
                window=_TIMELINE_RATE_LIMIT_WINDOW,
            )
            return

        channel = await self._get_or_create_channel(case_id)
        validated_payload = TimelineEventPayload.model_validate(payload)
        message = validated_payload.model_dump(mode="json")
        current_loop = asyncio.get_running_loop()
        async with channel.lock:
            targets = list(channel.connections)

        # fan-out outside lock
        for q, loop in targets:
            deliver = lambda q=q, loop=loop: self._deliver_message(
                queue=q,
                message=message,
                case_id=case_id,
                channel=channel,
                record_drop=self._record_drop,
            )

            if loop is current_loop:
                deliver()
            else:
                loop.call_soon_threadsafe(deliver)


timeline_queue_maxsize = int(os.getenv("TIMELINE_REALTIME_QUEUE_MAXSIZE", "100"))
timeline_realtime_bus = TimelineRealtimeBus(queue_maxsize=timeline_queue_maxsize)


def publish_timeline_event_best_effort(payload: Dict[str, Any]) -> None:
    """Publish a timeline event without depending on the caller's loop state."""
    case_id = payload["case_id"]
    publish_coro = timeline_realtime_bus.publish(case_id=case_id, payload=payload)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(publish_coro)
        return

    fallback_loop = asyncio.new_event_loop()
    try:
        fallback_loop.run_until_complete(publish_coro)
    finally:
        fallback_loop.close()
