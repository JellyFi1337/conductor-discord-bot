"""Наблюдение всего REST-трафика Discord через aiohttp TraceConfig. Без чтения тела."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import time
from urllib.parse import urlsplit

import aiohttp
import structlog

from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_EMOJI, BUCKET_ROLE, ObservedLimit
from conductor.domain.traffic import (
    BUSY_BUCKETS,
    INVALID_WINDOW,
    MAX_BUCKETS,
    TOP_STATUS,
    BucketState,
    TrafficSnapshot,
    is_invalid,
    route_key,
    wait_notice,
)

log = structlog.get_logger("conductor.rate_observer")

_STARTS_MAX = 8192
_EVENTS_MAX = 4096
_CREATE = {
    "POST /guilds/{id}/channels": (BUCKET_CHANNEL, "major"),
    "POST /guilds/{id}/roles": (BUCKET_ROLE, "major"),
    "POST /guilds/{id}/emojis": (BUCKET_EMOJI, "major"),
    "POST /applications/{id}/emojis": (BUCKET_EMOJI, "zero"),
}


class RateObserver:
    """Память процесса: ведра, rps и последний 429 на создание."""

    def __init__(self, *, clock: Callable[[], float] = time) -> None:
        self._clock = clock
        self._last: dict[tuple[int, str], ObservedLimit] = {}
        self._last_429: dict[tuple[int, str], ObservedLimit] = {}
        self._states: dict[tuple[str, str], BucketState] = {}
        self._starts: deque[float] = deque(maxlen=_STARTS_MAX)
        self._invalid: deque[float] = deque(maxlen=_EVENTS_MAX)
        self._global_429: deque[float] = deque(maxlen=_EVENTS_MAX)
        self._status_hits: deque[tuple[float, int, str]] = deque(maxlen=_EVENTS_MAX)

    def trace_config(self) -> aiohttp.TraceConfig:
        config = aiohttp.TraceConfig()
        config.on_request_start.append(self._on_request_start)
        config.on_request_end.append(self._on_request_end)
        return config

    def last(self, guild_id: int, bucket: str) -> ObservedLimit | None:
        return self._last.get((int(guild_id), str(bucket)))

    def last_429(self, guild_id: int, bucket: str) -> ObservedLimit | None:
        return self._last_429.get((int(guild_id), str(bucket)))

    def state(self, template: str, major: str) -> BucketState | None:
        return self._states.get((str(template), str(major)))

    def snapshot(self, guild_id: int | None = None) -> TrafficSnapshot:
        now = float(self._clock())
        self._trim(now)
        states = list(self._states.values())
        if guild_id is not None:
            major = str(int(guild_id))
            states = [
                item
                for item in states
                if item.major == major or (int(guild_id) == 0 and not item.major)
            ]
        busy = sorted(
            states,
            key=lambda item: (item.remaining / item.limit if item.limit else 1.0, -item.at),
        )
        return TrafficSnapshot(
            rps_1s=_count_since(self._starts, now, 1.0),
            rps_60s=_count_since(self._starts, now, 60.0),
            invalid_10m=len(self._invalid),
            global_429_10m=len(self._global_429),
            top_429=_top_templates(self._status_hits, 429, now),
            top_403=_top_templates(self._status_hits, 403, now),
            buckets=tuple(busy[:BUSY_BUCKETS]),
        )

    def stored_count(self) -> int:
        return len(self._states)

    async def _on_request_start(self, _session: object, _context: object, _params: object) -> None:
        try:
            self._starts.append(float(self._clock()))
        except Exception:
            log.exception("rate_observer_start_failed")

    async def _on_request_end(self, _session: object, _context: object, params: object) -> None:
        try:
            self._record(params)
        except Exception:
            log.exception("rate_observer_failed")

    def _record(self, params: object) -> None:
        method = str(getattr(params, "method", "") or "").upper()
        path = _path_of(getattr(params, "url", None))
        if not method or not path:
            return
        template, major = route_key(method, path)
        response = getattr(params, "response", None)
        status = _as_int(getattr(response, "status", None))
        if status is None:
            return
        headers = getattr(response, "headers", None)
        scope = _header(headers, "X-RateLimit-Scope")
        now = float(self._clock())
        limit = _as_int(_header(headers, "X-RateLimit-Limit")) or 0
        remaining_raw = _as_int(_header(headers, "X-RateLimit-Remaining"))
        remaining = 0 if remaining_raw is None else remaining_raw
        reset_raw = _as_float(_header(headers, "X-RateLimit-Reset-After"))
        reset_after = 0.0 if reset_raw is None else reset_raw
        bucket_hash = _header(headers, "X-RateLimit-Bucket")
        state = BucketState(
            template=template,
            major=major,
            bucket_hash=bucket_hash,
            limit=limit,
            remaining=remaining,
            reset_after=reset_after,
            scope=scope,
            status=status,
            at=now,
        )
        self._put_state(state)
        self._status_hits.append((now, status, template))
        if is_invalid(status, scope):
            self._invalid.append(now)
        if status == 429 and _truthy(_header(headers, "X-RateLimit-Global")):
            self._global_429.append(now)
        self._note_create(template, major, status, scope, remaining_raw, reset_raw, headers, now)
        if status == 429:
            retry = _as_float(_header(headers, "Retry-After"))
            delay = reset_after if retry is None else retry
            callback = wait_notice.get(None)
            if callable(callback):
                try:
                    callback(float(delay or 0.0))
                except Exception:
                    log.exception("wait_notice_failed")
        log.debug("rate_observed", template=template, major=major, status=status)
        self._trim(now)

    def _put_state(self, state: BucketState) -> None:
        key = (state.template, state.major)
        if key not in self._states and len(self._states) >= MAX_BUCKETS:
            oldest = min(self._states, key=lambda item: self._states[item].at)
            self._states.pop(oldest, None)
        self._states[key] = state

    def _note_create(
        self,
        template: str,
        major: str,
        status: int,
        scope: str,
        remaining: int | None,
        reset_after: float | None,
        headers: object,
        now: float,
    ) -> None:
        mapped = _CREATE.get(template)
        if mapped is None:
            return
        bucket, guild_from = mapped
        guild_id = 0 if guild_from == "zero" else _as_int(major) or 0
        retry_after = _as_float(_header(headers, "Retry-After")) if status == 429 else None
        observed = ObservedLimit(
            guild_id=guild_id,
            bucket=bucket,
            status=status,
            scope=scope,
            retry_after=retry_after,
            remaining=remaining,
            reset_after=reset_after,
            at=now,
        )
        key = (guild_id, bucket)
        self._last[key] = observed
        if status == 429:
            self._last_429[key] = observed

    def _trim(self, now: float) -> None:
        cutoff = now - INVALID_WINDOW
        _drop_before(self._invalid, cutoff)
        _drop_before(self._global_429, cutoff)
        while self._status_hits and self._status_hits[0][0] < cutoff:
            self._status_hits.popleft()
        start_cut = now - 60.0
        while self._starts and self._starts[0] < start_cut:
            self._starts.popleft()


def _path_of(url: object) -> str:
    if url is None:
        return ""
    path = getattr(url, "path", None)
    if isinstance(path, str) and path:
        return path
    raw = str(url)
    return urlsplit(raw).path or raw


def _header(headers: object, name: str) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    try:
        value = getter(name)
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes"}


def _count_since(stamps: deque[float], now: float, window: float) -> int:
    cutoff = now - window
    return sum(1 for stamp in stamps if stamp >= cutoff)


def _drop_before(stamps: deque[float], cutoff: float) -> None:
    while stamps and stamps[0] < cutoff:
        stamps.popleft()


def _top_templates(hits: deque[tuple[float, int, str]], status: int, now: float) -> tuple[str, ...]:
    cutoff = now - INVALID_WINDOW
    counts: dict[str, int] = {}
    for stamp, code, template in hits:
        if stamp < cutoff or code != status:
            continue
        counts[template] = counts.get(template, 0) + 1
    ranked = sorted(counts, key=lambda item: (-counts[item], item))
    return tuple(ranked[:TOP_STATUS])


__all__ = ["RateObserver"]
