"""Сценарии нагрузочного стенда против FakeDiscord. Не ходить на discord.com."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import discord
from discord.http import Route
from tests.fake_discord import FakeDiscord

from conductor.domain.traffic import PROFILES, FuseLevel, Lane, ProfileLimits, RateProfile
from conductor.presentation.layout import collected_text
from conductor.services.limits import InteractionLimiter
from conductor.services.log_pump import LogPump
from conductor.services.rate_observer import RateObserver
from conductor.services.traffic import TrafficGovernor, lane

LIVE_HOSTS = ("discord.com", "discordapp.com")


@dataclass(frozen=True, slots=True)
class BenchRow:
    scenario: str
    threshold: str
    actual: str
    ok: bool


class _HttpChannel(discord.abc.Messageable):
    def __init__(self, http: Any, channel_id: int) -> None:
        self.id = channel_id
        self._http = http
        self.texts: list[str] = []

    async def _get_channel(self):  # pragma: no cover
        return self

    async def send(self, **kwargs: Any) -> None:
        view = kwargs.get("view")
        content = kwargs.get("content")
        text = collected_text(view) if view is not None else str(content or "")
        self.texts.append(text)
        await self._http.request(
            Route("POST", "/channels/{channel_id}/messages", channel_id=self.id),
            json={"content": (text[:32] or "x")},
        )


def refuse_if_live(base: str | None = None) -> None:
    text = str(base if base is not None else Route.BASE).lower()
    if any(host in text for host in LIVE_HOSTS):
        raise SystemExit("traffic_bench: отказ запускаться, Route.BASE указывает на Discord")


def block_live_base() -> None:
    if any(host in str(Route.BASE).lower() for host in LIVE_HOSTS):
        Route.BASE = "http://127.0.0.1:9/blocked"


def _bot(*, observer: RateObserver | None = None, limits: ProfileLimits | None = None) -> SimpleNamespace:
    watch = observer or RateObserver()
    profile = RateProfile.EDGE
    resolved = limits or PROFILES[profile]
    settings = SimpleNamespace(rate_profile=profile, traffic_limits=resolved)
    bot = SimpleNamespace(settings=settings, rate_observer=watch, spawn=None, get_channel=lambda _cid: None)
    bot.traffic = TrafficGovernor(bot, observer=watch, limits=resolved, profile=profile)
    return bot


def _peak_1s(stamps: list[float]) -> int:
    ordered = sorted(stamps)
    best = 0
    right = 0
    for left, start in enumerate(ordered):
        while right < len(ordered) and ordered[right] < start + 1.0:
            right += 1
        best = max(best, right - left)
    return best


def format_table(rows: list[BenchRow]) -> str:
    lines = ["сценарий | порог | факт | OK/FAIL", "---|---|---|---"]
    for row in rows:
        mark = "OK" if row.ok else "FAIL"
        lines.append(f"{row.scenario} | {row.threshold} | {row.actual} | {mark}")
    return "\n".join(lines)


async def scenario_a(*, compressed: bool) -> BenchRow:
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    count = 80 if compressed else 400
    async with FakeDiscord(global_limit=10_000, route_limit=10_000) as fake:
        refuse_if_live(fake.base)
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=limits.max_ratelimit_wait)

        async def one() -> None:
            with lane(Lane.BACKGROUND):
                await http.request(Route("GET", "/users/@me"))

        await asyncio.gather(*(one() for _ in range(count)))
    starts = list(observer._starts)
    peak = _peak_1s(starts) if starts else 0
    duration = starts[-1] - starts[0] if len(starts) > 1 else 0.0
    average = (len(starts) - 1) / duration if duration > 0 else 0.0
    target = limits.global_rps_target
    ok = peak <= target and average >= 0.9 * target and observer.snapshot().global_429_10m == 0
    return BenchRow(
        "A",
        f"средн. ≥ {0.9 * target:.1f}/с, пик ≤ {target}, 0 global 429",
        f"средн. {average:.1f}/с, пик {peak}, global 429={observer.snapshot().global_429_10m}",
        ok,
    )


async def scenario_b(*, compressed: bool) -> BenchRow:
    members = 30 if compressed else 300
    limiter = InteractionLimiter(limit=1200, window=60, member_limit=12, member_window=10)
    started = perf_counter()
    accepted = sum(1 for user in range(members) if limiter.allow(1, user))
    elapsed = perf_counter() - started
    ok = accepted == members and elapsed <= 3.0
    return BenchRow(
        "B",
        f"{members} нажатий, все ответы ≤ 3 с",
        f"принято {accepted}/{members} за {elapsed:.3f} с",
        ok,
    )


async def scenario_c(*, compressed: bool) -> BenchRow:
    hidden = 5 if compressed else 50
    attempts = 8 if compressed else 60
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    bot.traffic = TrafficGovernor(
        bot,
        observer=observer,
        limits=limits,
        profile=RateProfile.EDGE,
        aimd_window=0.8,
        freeze_for=30.0,
    )
    created = 0
    async with FakeDiscord(
        hidden_after=hidden,
        hidden_retry_after=5.0,
        route_limit=1000,
        route_window=0.8,
        global_limit=10_000,
    ) as fake:
        refuse_if_live(fake.base)
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=1.0)
        for _ in range(attempts):
            try:
                with lane(Lane.USER):
                    await http.request(
                        Route("POST", "/guilds/{guild_id}/channels", guild_id=1),
                        json={"name": "room", "type": 2},
                    )
                created += 1
            except (discord.RateLimited, discord.HTTPException):
                pass
        count_429 = fake.statuses.count(429)
        ok = count_429 <= 2
        return BenchRow(
            "C",
            "≤ 2 ответа 429, остальные созданы или отложены",
            f"429={count_429}, создано {created}, попыток {attempts}",
            ok,
        )


async def scenario_d(*, compressed: bool) -> BenchRow:
    channels_n = 2 if compressed else 5
    cards = 12 if compressed else 200
    route_limit = 5
    route_window = 1.0
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    async with FakeDiscord(
        global_limit=10_000,
        route_limit=route_limit,
        route_window=route_window,
    ) as fake:
        refuse_if_live(fake.base)
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=limits.max_ratelimit_wait)
        sinks = {index: _HttpChannel(http, index) for index in range(1, channels_n + 1)}
        bot.get_channel = sinks.get
        pump = LogPump(bot, maxsize=10_000)
        started = perf_counter()
        for channel_id in sinks:
            for seq in range(cards):
                pump.submit(channel_id, content=f"c-{seq}")
        pump.start()
        await asyncio.wait_for(pump._queue.join(), timeout=120.0)
        await pump.aclose(drain_timeout=2.0)
        elapsed = max(1e-6, perf_counter() - started)
        goal = 0.9 * (route_limit / route_window)
        rates: list[float] = []
        ordered = True
        for sink in sinks.values():
            posts = max(1, len(sink.texts))
            rates.append(posts / elapsed)
            blob = "\n".join(sink.texts)
            found = [int(token.split("-", 1)[1]) for token in blob.split() if token.startswith("c-")]
            if found != list(range(cards)):
                ordered = False
        slowest = min(rates) if rates else 0.0
        hits_429 = fake.statuses.count(429)
        ok = slowest >= goal and hits_429 == 0 and ordered
        return BenchRow(
            "D",
            f"на канал ≥ {goal:.2f} сообщ/с, 0×429, порядок",
            f"мин {slowest:.2f}/с, 429={hits_429}, порядок={'да' if ordered else 'нет'}",
            ok,
        )


async def scenario_e(*, compressed: bool) -> BenchRow:
    del compressed
    bot = _bot()
    bot.traffic.suppress_for = 30.0
    async with FakeDiscord(force_status=403, global_limit=1000, route_limit=1000) as fake:
        refuse_if_live(fake.base)
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=30)
        route = Route("GET", "/guilds/{guild_id}", guild_id=9)
        for _ in range(3):
            try:
                with lane(Lane.BACKGROUND):
                    await http.request(route)
            except discord.Forbidden:
                pass
        extra = asyncio.create_task(_bg(http, route))
        await asyncio.sleep(0.15)
        extra.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await extra
        ok = len(fake.requests) == 3
        return BenchRow("E", "после 3×403 четвёртый подавлен", f"запросов {len(fake.requests)}", ok)


async def scenario_f(*, compressed: bool) -> BenchRow:
    observer = RateObserver()
    now = observer._clock()
    stop = 3 if compressed else 8
    limits = ProfileLimits(48, 24, 600, 1, 2, stop)
    for _ in range(stop):
        observer._invalid.append(now)
    bot = _bot(observer=observer, limits=limits)
    fuse = bot.traffic.fuse()
    async with FakeDiscord(global_limit=1000, route_limit=1000) as fake:
        refuse_if_live(fake.base)
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=30)
        blocked = asyncio.create_task(_bg(http, Route("GET", "/users/@me")))
        await asyncio.sleep(0.05)
        with lane(Lane.INTERACTION):
            await http.request(
                Route("POST", "/interactions/{interaction_id}/{token}/callback", interaction_id=1, token="tok")
            )
        background_sent = any(path.endswith("/@me") for _method, path in fake.requests)
        interaction_sent = any(path.endswith("/callback") for _method, path in fake.requests)
        blocked.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await blocked
        ok = fuse is FuseLevel.STOP and interaction_sent and not background_sent
        return BenchRow(
            "F",
            "fuse=stop, фон стоит, interaction проходит",
            f"fuse={fuse.value}, bg={background_sent}, ix={interaction_sent}",
            ok,
        )


async def _bg(http: Any, route: Route) -> None:
    with lane(Lane.BACKGROUND):
        await http.request(route)


async def run_all(*, compressed: bool, log: Callable[[str], None] | None = None) -> list[BenchRow]:
    emit = log or (lambda _line: None)
    rows: list[BenchRow] = []
    for name, fn in (
        ("A", scenario_a),
        ("B", scenario_b),
        ("C", scenario_c),
        ("D", scenario_d),
        ("E", scenario_e),
        ("F", scenario_f),
    ):
        emit(f"стенд {name}…")
        rows.append(await fn(compressed=compressed))
    return rows
