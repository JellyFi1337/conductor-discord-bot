"""Регулятор REST: глобальный темп, полосы, AIMD создания, предохранитель."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic, time
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import structlog
from discord import RateLimited
from sqlalchemy import select

from conductor.db.models import RateProfileState
from conductor.db.session import session_scope
from conductor.domain.create_lock import CreateHold, HoldSource, hold_from_wait
from conductor.domain.traffic import (
    AIMD_FREEZE,
    AIMD_WINDOW,
    CREATE_TEMPLATES,
    GLOBAL_CUT_HOLD,
    PROFILES,
    SUPPRESS_FOR,
    SUPPRESS_WINDOW,
    Admit,
    AimdState,
    AimdView,
    Defer,
    FuseLevel,
    GovernorSnapshot,
    Lane,
    LaneWait,
    ProfileLimits,
    RateProfile,
    Wait,
    admit,
    aimd_advance,
    aimd_cap,
    apply_overrides,
    current_lane,
    damped_rps,
    fuse_level,
    is_interaction_route,
    percentile,
    request_deadline,
    resource_capped,
    route_key,
    seed_allowance,
    split_concurrency,
    suppress_until,
    wait_notice,
)
from conductor.presentation import copy
from conductor.services.create_lock import remember

log = structlog.get_logger("conductor.traffic")

# Окно чуть длиннее секунды: между выдачей жетона и уходом запроса есть разброс,
# и без запаса внешний замер «за секунду» может показать больше цели.
GRANT_WINDOW = 1.05
GRANT_BURST = 3.0

_lane_depth: ContextVar[int] = ContextVar("conductor_lane_depth", default=0)

__all__ = ["TrafficGovernor", "lane", "wait_progress"]


@contextmanager
def lane(value: Lane) -> Iterator[None]:
    token = current_lane.set(value)
    try:
        yield
    finally:
        current_lane.reset(token)


@contextmanager
def wait_progress(callback: Callable[[float], object] | None) -> Iterator[None]:
    token = wait_notice.set(callback)
    try:
        yield
    finally:
        wait_notice.reset(token)


class TrafficGovernor:
    """Держит REST у границы Discord: темп, полосы, выученные создания, предохранитель."""

    def __init__(
        self,
        bot: Any,
        *,
        observer: Any | None = None,
        limits: ProfileLimits | None = None,
        profile: RateProfile | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] | None = None,
        aimd_window: float = AIMD_WINDOW,
        freeze_for: float = AIMD_FREEZE,
        suppress_for: float = SUPPRESS_FOR,
        global_cut_for: float = GLOBAL_CUT_HOLD,
    ) -> None:
        self._bot = bot
        settings = getattr(bot, "settings", None)
        resolved = getattr(settings, "traffic_limits", None) if settings is not None else None
        self.limits = limits or resolved or apply_overrides(PROFILES[RateProfile.EDGE])
        raw_profile = profile or getattr(settings, "rate_profile", RateProfile.EDGE)
        try:
            self.profile = raw_profile if isinstance(raw_profile, RateProfile) else RateProfile(str(raw_profile))
        except ValueError:
            self.profile = RateProfile.EDGE
        self._observer = observer if observer is not None else getattr(bot, "rate_observer", None)
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self.aimd_window = float(aimd_window)
        self.freeze_for = float(freeze_for)
        self.suppress_for = float(suppress_for)
        self.global_cut_for = float(global_cut_for)
        # Метки выданных жетонов за последнюю секунду: окно, а не интервал между
        # выдачами. Иначе запоздание таймера (на Windows шаг около 15 мс) режет темп.
        self._tat = 0.0
        self._grants: deque[float] = deque()
        self._cut_until = 0.0
        self._global_pause_until = 0.0
        self._waiters: list[deque[asyncio.Future[bool]]] = [deque() for _ in Lane]
        self._queues = [0, 0, 0, 0]
        self._wait_samples: list[list[float]] = [[] for _ in Lane]
        self._handle: asyncio.TimerHandle | None = None
        split = split_concurrency(self.limits.outbound_concurrency)
        self._sems = {lane: asyncio.Semaphore(size) for lane, size in split.items()}
        self._aimd: dict[tuple[int, str], AimdState] = {}
        self._dirty: set[tuple[int, str]] = set()
        self._forbidden: dict[tuple[str, str], list[float]] = {}
        self._suppressed: dict[tuple[str, str], float] = {}
        self._fuse_warned_at = 0.0
        self._persist_scheduled = False

    def trace_config(self) -> aiohttp.TraceConfig:
        config = aiohttp.TraceConfig()
        config.on_request_start.append(self._on_request_start)
        observer = self._observer
        if observer is not None:
            config.on_request_start.append(observer._on_request_start)
            config.on_request_end.append(observer._on_request_end)
        config.on_request_end.append(self._on_request_end)
        config.on_request_exception.append(self._on_request_exception)
        return config

    def fuse(self) -> FuseLevel:
        observer = self._observer
        invalid = 0 if observer is None else int(observer.snapshot().invalid_10m)
        return fuse_level(invalid, self.limits)

    def view(self, guild_id: int = 0) -> GovernorSnapshot:
        now = float(self._clock())
        lanes: list[LaneWait] = []
        for item in (Lane.USER, Lane.STAFF, Lane.BACKGROUND):
            samples = tuple(self._wait_samples[int(item)])
            queued = self._queues[int(item)]
            lanes.append(
                LaneWait(
                    lane=item.name.lower(),
                    queue=queued,
                    p50=percentile(samples, 50),
                    p95=percentile(samples, 95),
                )
            )
        wanted = int(guild_id)
        allowances: list[AimdView] = []
        for (gid, bucket), state in sorted(self._aimd.items()):
            if wanted and gid != wanted:
                continue
            allowances.append(AimdView(bucket=bucket, allowance=state.allowance, frozen=now < state.frozen_until))
        return GovernorSnapshot(
            profile=str(self.profile.value),
            rps_target=damped_rps(self.limits.global_rps_target, now, self._cut_until),
            fuse=str(self.fuse().value),
            lanes=tuple(lanes),
            allowances=tuple(allowances),
        )

    def mutation_pace(self, guild_id: int, bucket: str) -> tuple[int, float]:
        """AIMD для диагностики; локальная квота и дополнительная пауза отключены."""
        state = self._touch(int(guild_id), str(bucket))
        return state.allowance, 0.0

    def note_demand(self, guild_id: int, bucket: str) -> None:
        self._touch(int(guild_id), str(bucket), demand=1)

    def note_hidden_429(self, guild_id: int, bucket: str, *, shared: bool = False) -> None:
        if shared:
            return
        self._touch(int(guild_id), str(bucket), hit_429=True)

    def admit_mutation(self, guild_id: int, bucket: str, lane: Lane, now: float) -> Admit | Wait | Defer:
        template = {
            "channel": "POST /guilds/{id}/channels",
            "role": "POST /guilds/{id}/roles",
            "emoji": "POST /guilds/{id}/emojis",
        }.get(str(bucket))
        if template is None:
            return Admit()
        observer = self._observer
        getter = getattr(observer, "state", None)
        state = None if not callable(getter) else getter(template, str(int(guild_id)))
        return admit(lane, state, now, self.limits)

    def guild_cap_hold(self, guild_id: int, bucket: str, now: float) -> CreateHold | None:
        getter = getattr(self._bot, "get_guild", None)
        guild = getter(int(guild_id)) if callable(getter) else None
        if guild is None:
            return None
        channels = len(getattr(guild, "channels", ()) or ())
        roles = len(getattr(guild, "roles", ()) or ())
        if not resource_capped(str(bucket), channel_count=channels, role_count=roles):
            return None
        route = "guild_channel_cap" if str(bucket) == "channel" else "guild_role_cap"
        return hold_from_wait(bucket=str(bucket), wait=3600.0, now=now, route=route)

    def suppressed(self, template: str, major: str, now: float | None = None) -> bool:
        moment = self._clock() if now is None else float(now)
        return moment < self._suppressed.get((str(template), str(major)), 0.0)

    async def acquire_lane(self) -> None:
        item = current_lane.get()
        if item is Lane.INTERACTION:
            return
        depth = _lane_depth.get()
        if depth > 0:
            _lane_depth.set(depth + 1)
            return
        await self._sems[item].acquire()
        _lane_depth.set(1)

    def release_lane(self) -> None:
        item = current_lane.get()
        if item is Lane.INTERACTION:
            return
        depth = _lane_depth.get()
        if depth > 1:
            _lane_depth.set(depth - 1)
            return
        if depth == 1:
            _lane_depth.set(0)
            self._sems[item].release()

    async def hydrate(self) -> None:
        sessions = getattr(self._bot, "sessions", None)
        if sessions is None:
            return
        async with session_scope(sessions) as session:
            rows = await session.scalars(select(RateProfileState))
            now = float(self._clock())
            for row in rows:
                frozen = 0.0 if row.frozen_until is None else row.frozen_until.timestamp()
                last = None if row.last_429_at is None else row.last_429_at.timestamp()
                self._aimd[(int(row.guild_id), str(row.bucket))] = AimdState(
                    allowance=int(row.allowance),
                    frozen_until=frozen,
                    last_429_at=last,
                    window_start=now,
                    demand=0,
                    had_429=False,
                )

    async def flush(self) -> None:
        await self._persist_dirty()

    async def _on_request_start(self, _session: object, _context: object, params: object) -> None:
        try:
            await self._before_send(params)
        except Exception:
            log.exception("traffic_start_failed")

    async def _on_request_end(self, _session: object, _context: object, params: object) -> None:
        try:
            self.release_lane()
            self._after_send(params)
        except Exception:
            log.exception("traffic_end_failed")
        response = getattr(params, "response", None)
        headers = getattr(response, "headers", None)
        if (
            request_deadline.get() is not None
            and _as_int(getattr(response, "status", None)) == 429
            and _truthy(_header(headers, "X-RateLimit-Global"))
        ):
            # Отмена сна discord.py оставляет внутренний global Event закрытым.
            # Для сборки общую паузу держит регулятор, не HTTPClient.
            raise RateLimited(max(0.0, self._global_pause_until - self._clock()))

    async def _on_request_exception(self, _session: object, _context: object, _params: object) -> None:
        try:
            self.release_lane()
        except Exception:
            log.exception("traffic_exception_failed")

    async def _before_send(self, params: object) -> None:
        method = str(getattr(params, "method", "") or "").upper()
        path = _path_of(getattr(params, "url", None))
        template, major = route_key(method, path) if method and path else ("", "")
        item = current_lane.get()
        if is_interaction_route(template) or item is Lane.INTERACTION:
            return
        now = float(self._clock())
        until = self._suppressed.get((template, major), 0.0)
        if now < self._global_pause_until:
            await self._sleep(self._global_pause_until - now)
            now = float(self._clock())
        if now < until:
            await self._sleep(until - now)
        await self._acquire_token(item)
        await self.acquire_lane()
        mapped = CREATE_TEMPLATES.get(template)
        if mapped is None:
            return
        guild_id = int(major) if major.isdigit() else 0
        self._touch(guild_id, mapped, demand=1)

    def _after_send(self, params: object) -> None:
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
        if status == 403:
            self._note_forbidden(template, major, now)
        if status == 429 and _truthy(_header(headers, "X-RateLimit-Global")):
            self._cut_until = now + self.global_cut_for
            retry_text = _header(headers, "Retry-After") or _header(headers, "X-RateLimit-Reset-After")
            try:
                delay = float(retry_text)
            except (TypeError, ValueError):
                delay = 0.0
            self._global_pause_until = max(self._global_pause_until, now + delay)
        mapped = CREATE_TEMPLATES.get(template)
        if mapped is not None and status == 429:
            # Сохраняем Retry-After сразу: команда может закончиться по бюджету,
            # пока discord.py ещё спит внутри HTTPClient.request.
            retry_text = _header(headers, "Retry-After") or _header(headers, "X-RateLimit-Reset-After")
            try:
                delay = float(retry_text)
            except (TypeError, ValueError):
                delay = 0.0
            if delay > 0:
                remember(
                    self._bot,
                    int(major) if major.isdigit() else 0,
                    mapped,
                    delay,
                    time(),
                    source=HoldSource.DISCORD_SHARED if scope == "shared" else HoldSource.DISCORD,
                )
        if mapped is not None and status == 429 and scope != "shared":
            guild_id = int(major) if major.isdigit() else 0
            self._touch(guild_id, mapped, hit_429=True)
        self._maybe_fuse_warn(now)
        self._arm()

    def _note_forbidden(self, template: str, major: str, now: float) -> None:
        key = (str(template), str(major))
        hits = [stamp for stamp in self._forbidden.get(key, []) if now - stamp <= SUPPRESS_WINDOW]
        hits.append(now)
        self._forbidden[key] = hits
        until = suppress_until(tuple(hits), now, hold=self.suppress_for)
        if until is None:
            return
        self._suppressed[key] = until
        log.warning("traffic_suppressed", template=template, major=major, until=until)

    def _touch(
        self,
        guild_id: int,
        bucket: str,
        *,
        demand: int = 0,
        hit_429: bool = False,
    ) -> AimdState:
        key = (int(guild_id), str(bucket))
        now = float(self._clock())
        current = self._aimd.get(key)
        if current is None:
            current = AimdState(seed_allowance(bucket), 0.0, None, now, 0, False)
        nxt = aimd_advance(
            current,
            now,
            window=self.aimd_window,
            cap=aimd_cap(self.profile, bucket),
            demand=demand,
            hit_429=hit_429,
            freeze_for=self.freeze_for,
        )
        self._aimd[key] = nxt
        if nxt.allowance != current.allowance or nxt.frozen_until != current.frozen_until:
            self._dirty.add(key)
            self._schedule_persist()
        return nxt

    async def _acquire_token(self, item: Lane) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._waiters[int(item)].append(fut)
        self._queues[int(item)] += 1
        started = float(self._clock())
        self._arm()
        try:
            await fut
        finally:
            queue = self._waiters[int(item)]
            if fut in queue:
                queue.remove(fut)
            self._queues[int(item)] = max(0, self._queues[int(item)] - 1)
            waited = max(0.0, float(self._clock()) - started)
            if waited > 0:
                samples = self._wait_samples[int(item)]
                samples.append(waited)
                overflow = len(samples) - 200
                if overflow > 0:
                    del samples[:overflow]

    def _arm(self, delay: float = 0.0) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._handle = loop.call_later(max(0.0, delay), self._grant)

    def _grant(self) -> None:
        self._handle = None
        now = float(self._clock())
        rps = damped_rps(self.limits.global_rps_target, now, self._cut_until)
        level = self.fuse()
        while True:
            while self._grants and self._grants[0] <= now - GRANT_WINDOW:
                self._grants.popleft()
            item = self._pick(level)
            if item is None:
                break
            cost = 2 if level is FuseLevel.THROTTLE and item is Lane.STAFF else 1
            # Жёсткий потолок: в любом окне в секунду выдач не больше цели.
            if len(self._grants) + cost > rps:
                oldest = self._grants[0] if self._grants else now
                self._arm(max(0.0, oldest + GRANT_WINDOW - now))
                return
            # Сглаживание: без него потолок выдаётся залпом, и соседние секунды
            # складываются в скользящем окне. Догон ограничен GRANT_BURST интервалами.
            interval = GRANT_WINDOW / max(1.0, rps)
            if self._tat - now > 0:
                self._arm(self._tat - now)
                return
            self._tat = max(self._tat, now - interval * GRANT_BURST) + interval * cost
            self._grants.extend([now] * cost)
            waiter = self._waiters[int(item)].popleft()
            if not waiter.done():
                waiter.set_result(True)
            now = float(self._clock())
        pending = any(self._waiters[int(item)] for item in (Lane.USER, Lane.STAFF, Lane.BACKGROUND))
        if pending and level in {FuseLevel.STOP, FuseLevel.THROTTLE}:
            self._arm(0.05)

    def _pick(self, level: FuseLevel) -> Lane | None:
        if level is FuseLevel.STOP:
            return None
        for item in (Lane.USER, Lane.STAFF, Lane.BACKGROUND):
            if level is FuseLevel.THROTTLE and item is Lane.BACKGROUND:
                continue
            if self._waiters[int(item)]:
                return item
        return None

    def _maybe_fuse_warn(self, now: float) -> None:
        if self.fuse() is FuseLevel.OK:
            return
        if now - self._fuse_warned_at < 600.0:
            return
        self._fuse_warned_at = now
        invalid = 0 if self._observer is None else int(self._observer.snapshot().invalid_10m)
        log.warning("traffic_fuse", level=str(self.fuse().value), invalid=invalid)
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        text = copy.TRAFFIC_FUSE_SOFT
        configs = getattr(self._bot, "config_by_guild", {}) or {}
        for config in configs.values():
            submit(getattr(config, "structure_channel_id", None), content=text)
            submit(getattr(config, "admin_log_channel_id", None), content=text)

    def _schedule_persist(self) -> None:
        if self._persist_scheduled:
            return
        spawn = getattr(self._bot, "spawn", None)
        if not callable(spawn):
            return
        self._persist_scheduled = True
        spawn(self._persist_dirty(), name="traffic-aimd")

    async def _persist_dirty(self) -> None:
        self._persist_scheduled = False
        sessions = getattr(self._bot, "sessions", None)
        if sessions is None or not self._dirty:
            return
        keys = list(self._dirty)
        self._dirty.clear()
        now = datetime.now(UTC)
        async with session_scope(sessions) as session:
            for guild_id, bucket in keys:
                state = self._aimd.get((guild_id, bucket))
                if state is None:
                    continue
                row = await session.get(RateProfileState, (guild_id, bucket))
                frozen = _as_dt(state.frozen_until)
                last = _as_dt(state.last_429_at)
                if row is None:
                    session.add(
                        RateProfileState(
                            guild_id=guild_id,
                            bucket=bucket,
                            allowance=state.allowance,
                            frozen_until=frozen,
                            last_429_at=last,
                            updated_at=now,
                        )
                    )
                    continue
                row.allowance = state.allowance
                row.frozen_until = frozen
                row.last_429_at = last
                row.updated_at = now


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


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes"}


def _as_dt(epoch: float | None) -> datetime | None:
    if epoch is None or float(epoch) <= 0:
        return None
    return datetime.fromtimestamp(float(epoch), tz=UTC)
