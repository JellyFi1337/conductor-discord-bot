"""Память и файл блокировок create/delete: переживает рестарт, не дергает Discord."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Hashable
from pathlib import Path
from time import time
from types import SimpleNamespace
from typing import Any

import discord
import structlog

from conductor.domain.create_lock import (
    BUCKET_EMOJI,
    BUCKETS,
    CLOUDFLARE_FALLBACK,
    PERSIST_CAP,
    CreateHold,
    HoldSource,
    classify,
    hold_from_retry,
    hold_from_wait,
    persisted_until,
)
from conductor.domain.traffic import DEFER_ROUTE, Defer, Wait, current_lane

log = structlog.get_logger("conductor.create_lock")

LOCK_FILE = ".create-lock.json"

__all__ = [
    "LOCK_FILE",
    "before_mutate",
    "blocked",
    "clear_guild",
    "continue_after_hold",
    "deploy_emojis",
    "find_active",
    "hold_for",
    "hydrate",
    "is_defer_hold",
    "list_holds",
    "lock_path",
    "log_refused",
    "note_success",
    "remember",
    "retry_after",
    "snapshot_holds",
    "trip",
]


def lock_path(bot: Any) -> Path | None:
    folder = getattr(getattr(bot, "settings", None), "data_dir", None)
    if folder is None:
        return None
    return Path(folder) / LOCK_FILE


def memory(bot: Any) -> dict[tuple[int, str], CreateHold]:
    holds = getattr(bot, "create_holds", None)
    if not isinstance(holds, dict):
        holds = {}
        bot.create_holds = holds
    return holds


def remember(
    bot: Any,
    guild_id: int,
    bucket: str,
    retry_after: float,
    now: float,
    *,
    source: HoldSource,
    route: str = "",
) -> CreateHold:
    hold = hold_from_retry(bucket=bucket, retry_after=retry_after, now=now, source=source, route=route)
    _set_hold(bot, int(guild_id), hold, now=now)
    return hold


def retry_after(exc: BaseException) -> float | None:
    retry = getattr(exc, "retry_after", None)
    status = getattr(exc, "status", None)
    if retry is None and status != 429:
        return None
    if retry is None:
        return 0.0
    try:
        return max(0.0, float(retry))
    except (TypeError, ValueError):
        return 0.0


def trip(bot: Any, guild_id: int, bucket: str, exc: BaseException, *, now: float | None = None) -> CreateHold | None:
    moment = time() if now is None else float(now)
    rate_limited = type(exc).__name__ == "RateLimited"
    status = _status_of(exc)
    observed = _observed(bot, int(guild_id), str(bucket))
    source = classify(rate_limited=rate_limited, status=status, observed=observed, now=moment)
    if source is None:
        return None
    delay = retry_after(exc)
    if delay is None or delay <= 0:
        observed_retry = None if observed is None else observed.retry_after
        if observed_retry:
            delay = float(observed_retry)
        elif status == 429:
            delay = CLOUDFLARE_FALLBACK
        else:
            return None
    hold = CreateHold(
        bucket=str(bucket),
        until=moment + float(delay),
        retry_after=float(delay),
        source=source,
    )
    lookup = getattr(getattr(bot, "rate_observer", None), "last", None)
    latest = lookup(int(guild_id), str(bucket)) if callable(lookup) else None
    if latest is not None:
        log.info(
            "create_limit_diagnosed",
            guild_id=int(guild_id),
            bucket=str(bucket),
            source=str(source),
            retry_after=float(delay),
            last_status=latest.status,
            route_remaining=latest.remaining,
            route_reset_after=latest.reset_after,
            observation_age=max(0.0, moment - latest.at),
        )
    _set_hold(bot, int(guild_id), hold, now=moment)
    if source is HoldSource.DISCORD:
        note = getattr(getattr(bot, "traffic", None), "note_hidden_429", None)
        if callable(note):
            note(int(guild_id), str(bucket), shared=False)
    return hold


async def before_mutate(bot: Any, guild_id: int, bucket: str, *, now: float | None = None) -> CreateHold | None:
    """Учитывает ограничения Discord. Квот на число объектов у бота нет."""
    moment = time() if now is None else float(now)
    stored = find_active(bot, int(guild_id), (str(bucket),), moment)
    if stored is not None:
        return stored
    gov = getattr(bot, "traffic", None)
    if gov is not None:
        cap = gov.guild_cap_hold(int(guild_id), str(bucket), moment)
        if cap is not None:
            _set_hold(bot, int(guild_id), cap, now=moment)
            return cap
        verdict = gov.admit_mutation(int(guild_id), str(bucket), current_lane.get(), moment)
        if isinstance(verdict, Defer):
            hold = hold_from_wait(
                bucket=str(bucket),
                wait=verdict.seconds,
                now=moment,
                route=DEFER_ROUTE,
            )
            _set_hold(bot, int(guild_id), hold, now=moment)
            return hold
        if isinstance(verdict, Wait) and verdict.seconds > 0:
            await asyncio.sleep(verdict.seconds)
            moment = time() if now is None else float(now) + verdict.seconds
    if gov is not None:
        gov.note_demand(int(guild_id), str(bucket))
    return None


async def hold_for(bot: Any, guild_id: int, bucket: str, *, now: float | None = None) -> CreateHold | None:
    return await before_mutate(bot, guild_id, bucket, now=now)


def is_defer_hold(hold: CreateHold) -> bool:
    return str(hold.route) == DEFER_ROUTE


def continue_after_hold(
    bot: Any,
    key: Hashable,
    hold: CreateHold,
    job: Callable[[], Awaitable[Any]],
) -> bool:
    """Поставить продолжение в очередь 1d. True, если это Defer, а не отказ."""
    if not is_defer_hold(hold):
        return False
    queue = getattr(bot, "queue", None)
    schedule = getattr(queue, "schedule_later", None)
    if not callable(schedule):
        return False
    schedule(key, max(0.05, float(hold.retry_after)), job)
    return True


async def blocked(bot: Any, guild_id: int, bucket: str, *, now: float | None = None) -> bool:
    return await before_mutate(bot, guild_id, bucket, now=now) is not None


def note_success(bot: Any, guild_id: int, bucket: str, *, now: float | None = None) -> None:
    moment = time() if now is None else float(now)
    _hydrate(bot, now=moment)
    key = (int(guild_id), str(bucket))
    memory(bot).pop(key, None)
    path = lock_path(bot)
    if path is None or not path.is_file():
        return
    payload = _prune(_read(path), moment)
    guild_key = str(int(guild_id))
    guild = payload.get(guild_key)
    if not isinstance(guild, dict) or str(bucket) not in guild:
        return
    guild.pop(str(bucket), None)
    if guild:
        payload[guild_key] = guild
    else:
        payload.pop(guild_key, None)
    _dump(path, payload)


def clear_guild(bot: Any, guild_id: int, *, now: float | None = None) -> None:
    moment = time() if now is None else float(now)
    _hydrate(bot, now=moment)
    gid = int(guild_id)
    stored = memory(bot)
    for key in [item for item in stored if item[0] == gid]:
        stored.pop(key, None)
    path = lock_path(bot)
    if path is None:
        return
    payload = _prune(_read(path), moment)
    payload.pop(str(gid), None)
    _dump(path, payload)


def list_holds(bot: Any, guild_id: int, now: float) -> tuple[CreateHold, ...]:
    _hydrate(bot, now=now)
    gid = int(guild_id)
    rows = [hold for (item_gid, _bucket), hold in memory(bot).items() if item_gid == gid and hold.active(now)]
    rows.sort(key=lambda item: item.bucket)
    return tuple(rows)


def snapshot_holds(bot: Any, guild_id: int, *, now: float | None = None) -> tuple[tuple[CreateHold, bool], ...]:
    moment = time() if now is None else float(now)
    holds = list_holds(bot, guild_id, moment)
    path = lock_path(bot)
    raw = _read(path) if path is not None and path.is_file() else {}
    guild = raw.get(str(int(guild_id)))
    file_buckets = guild if isinstance(guild, dict) else {}
    return tuple((hold, hold.bucket in file_buckets) for hold in holds)


def log_refused(guild_id: int, bucket: str, hold: CreateHold, *, action: str, now: float | None = None) -> None:
    moment = time() if now is None else float(now)
    log.info(
        "create_refused",
        guild_id=int(guild_id),
        bucket=str(bucket),
        source=str(hold.source),
        remaining=hold.remaining(moment),
        action=action,
    )


async def deploy_emojis(bot: Any) -> CreateHold | Any:
    now = time()
    hold = find_active(bot, 0, (BUCKET_EMOJI,), now)
    if hold is not None:
        log_refused(0, BUCKET_EMOJI, hold, action="emoji", now=now)
        return hold
    report = await bot.motion.deploy(bot)
    retry = getattr(report, "retry_after", None)
    if retry:
        if getattr(report, "rate_limited", False):
            exc: BaseException = discord.RateLimited(float(retry))
        else:
            exc = SimpleNamespace(status=429, retry_after=float(retry))  # type: ignore[assignment]
        tripped = trip(bot, 0, BUCKET_EMOJI, exc, now=now)
        if tripped is not None:
            log_refused(0, BUCKET_EMOJI, tripped, action="emoji", now=now)
    else:
        note_success(bot, 0, BUCKET_EMOJI, now=now)
    return report


def find_active(bot: Any, guild_id: int, buckets: tuple[str, ...], now: float) -> CreateHold | None:
    _hydrate(bot, now=now)
    gid = int(guild_id)
    stored = memory(bot)
    found: CreateHold | None = None
    for bucket in buckets:
        hold = stored.get((gid, str(bucket)))
        if hold is None or not hold.active(now):
            continue
        if found is None or hold.remaining(now) > found.remaining(now):
            found = hold
    return found


def _set_hold(bot: Any, guild_id: int, hold: CreateHold, *, now: float) -> None:
    _hydrate(bot, now=now)
    memory(bot)[(int(guild_id), str(hold.bucket))] = hold
    persist = persisted_until(hold, now)
    path = lock_path(bot)
    persisted = persist is not None and path is not None
    if path is not None and (persist is not None or path.is_file()):
        payload = _prune(_read(path), now)
        guild = payload.get(str(int(guild_id)))
        if not isinstance(guild, dict):
            guild = {}
        if persist is None:
            guild.pop(hold.bucket, None)
        else:
            guild[hold.bucket] = {
                "until": persist,
                "retry_after": hold.retry_after,
                "source": str(hold.source),
                "route": hold.route,
            }
        if guild:
            payload[str(int(guild_id))] = guild
        else:
            payload.pop(str(int(guild_id)), None)
        _dump(path, payload)
    log.info(
        "create_hold_set",
        guild_id=int(guild_id),
        bucket=hold.bucket,
        source=str(hold.source),
        retry_after=hold.retry_after,
        persisted=persisted,
    )


def hydrate(bot: Any, *, now: float | None = None) -> None:
    _hydrate(bot, now=now)


def _hydrate(bot: Any, *, now: float | None = None) -> None:
    if getattr(bot, "_create_holds_loaded", False):
        return
    bot._create_holds_loaded = True
    path = lock_path(bot)
    if path is None or not path.is_file():
        log.info("create_lock_hydrated", discarded=0, capped=0)
        return
    moment = time() if now is None else float(now)
    raw = _read(path)
    holds = memory(bot)
    discarded = 0
    capped = 0
    kept: dict[str, dict[str, Any]] = {}
    for guild_key, buckets in raw.items():
        try:
            guild_id = int(guild_key)
        except (TypeError, ValueError):
            discarded += 1
            continue
        if not isinstance(buckets, dict):
            discarded += 1
            continue
        stored: dict[str, Any] = {}
        for bucket, payload in buckets.items():
            if bucket not in BUCKETS or not isinstance(payload, dict):
                discarded += 1
                continue
            source_raw = payload.get("source")
            if source_raw is None:
                discarded += 1
                continue
            try:
                source = HoldSource(str(source_raw))
                until = float(payload["until"])
                retry = float(payload.get("retry_after", 0))
            except (KeyError, TypeError, ValueError):
                discarded += 1
                continue
            if source not in {HoldSource.DISCORD}:
                discarded += 1
                continue
            hold = CreateHold(
                bucket=str(bucket),
                until=until,
                retry_after=retry,
                source=source,
                route=str(payload.get("route", "")),
            )
            persist = persisted_until(hold, moment)
            if persist is None or not hold.active(moment):
                discarded += 1
                continue
            if persist < hold.until:
                capped += 1
                hold = CreateHold(
                    bucket=hold.bucket,
                    until=persist,
                    retry_after=min(hold.retry_after, persist - moment),
                    source=hold.source,
                    route=hold.route,
                )
            holds[(guild_id, str(bucket))] = hold
            stored[str(bucket)] = {
                "until": hold.until,
                "retry_after": hold.retry_after,
                "source": str(hold.source),
                "route": hold.route,
            }
        if stored:
            kept[str(guild_id)] = stored
    _dump(path, kept)
    log.info("create_lock_hydrated", discarded=discarded, capped=capped)


def _observed(bot: Any, guild_id: int, bucket: str) -> Any:
    observer = getattr(bot, "rate_observer", None)
    lookup = getattr(observer, "last_429", None)
    if not callable(lookup):
        return None
    return lookup(int(guild_id), str(bucket))


def _status_of(exc: BaseException) -> int | None:
    raw = getattr(exc, "status", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _read(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _prune(payload: dict[str, Any], now: float) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for guild_key, buckets in payload.items():
        if not isinstance(buckets, dict):
            continue
        stored: dict[str, Any] = {}
        for bucket, item in buckets.items():
            if not isinstance(item, dict):
                continue
            try:
                until = float(item["until"])
                source = HoldSource(str(item.get("source")))
            except (KeyError, TypeError, ValueError):
                continue
            if source not in {HoldSource.DISCORD}:
                continue
            if until <= float(now) + 1.0:
                continue
            until = min(until, float(now) + PERSIST_CAP)
            stored[str(bucket)] = {
                "until": until,
                "retry_after": float(item.get("retry_after", 0) or 0),
                "source": str(source),
                "route": str(item.get("route", "")),
            }
        if stored:
            kept[str(guild_key)] = stored
    return kept


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
