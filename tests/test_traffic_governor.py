from __future__ import annotations

import asyncio
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord.http import Route
from tests.fake_discord import FakeDiscord

from conductor.db.engine import create_engine, create_sessionmaker, init_db
from conductor.domain.create_lock import BUCKET_CHANNEL
from conductor.domain.traffic import (
    GLOBAL_CUT,
    PROFILES,
    BucketState,
    FuseLevel,
    Lane,
    ProfileLimits,
    RateProfile,
)
from conductor.services.create_lock import before_mutate, trip
from conductor.services.rate_observer import RateObserver
from conductor.services.traffic import TrafficGovernor, lane


def _bot(
    *, observer: RateObserver | None = None, limits: ProfileLimits | None = None, **extra: object
) -> SimpleNamespace:
    watch = observer or RateObserver()
    profile = RateProfile.EDGE
    resolved = limits or PROFILES[profile]
    settings = SimpleNamespace(rate_profile=profile, traffic_limits=resolved)
    bot = SimpleNamespace(settings=settings, rate_observer=watch, spawn=None, **extra)
    bot.traffic = TrafficGovernor(bot, observer=watch, limits=resolved, profile=profile)
    return bot


def _max_in_window(stamps: list[float], width: float = 1.0) -> int:
    ordered = sorted(stamps)
    best = 0
    right = 0
    for left, start in enumerate(ordered):
        while right < len(ordered) and ordered[right] < start + width:
            right += 1
        best = max(best, right - left)
    return best


async def test_background_demand_stays_at_global_rps_target() -> None:
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    async with FakeDiscord(global_limit=10_000, route_limit=10_000) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=limits.max_ratelimit_wait)

        async def one() -> None:
            with lane(Lane.BACKGROUND):
                await http.request(Route("GET", "/users/@me"))

        await asyncio.gather(*(one() for _ in range(200)))
    starts = list(observer._starts)
    assert starts
    peak = _max_in_window(starts)
    assert peak <= limits.global_rps_target
    duration = starts[-1] - starts[0]
    assert duration > 0
    average = (len(starts) - 1) / duration
    assert average >= 0.9 * limits.global_rps_target
    assert observer.snapshot().global_429_10m == 0
    assert 429 not in fake.statuses


@pytest.mark.parametrize("route_window", [45.0, 34909.724454789])
async def test_route_remaining_positive_second_call_is_not_ratelimited(route_window: float) -> None:
    bot = _bot()
    async with FakeDiscord(route_limit=10, route_window=route_window, global_limit=100) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=600)
        route = Route("GET", "/users/@me")
        await http.request(route)
        await http.request(route)
    assert fake.statuses == [200, 200]


async def test_interaction_route_is_not_delayed_when_tokens_are_gone() -> None:
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(limits=limits)
    async with FakeDiscord(global_limit=10_000, route_limit=10_000) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=limits.max_ratelimit_wait)

        async def background() -> None:
            with lane(Lane.BACKGROUND):
                await http.request(Route("GET", "/users/@me"))

        tasks = [asyncio.create_task(background()) for _ in range(80)]
        await asyncio.sleep(0.02)
        started = perf_counter()
        with lane(Lane.INTERACTION):
            await http.request(
                Route("POST", "/interactions/{interaction_id}/{token}/callback", interaction_id=1, token="tok")
            )
        assert perf_counter() - started < 0.25
        await asyncio.gather(*tasks)


async def test_user_defer_when_reset_exceeds_budget(tmp_path) -> None:
    observer = RateObserver(clock=lambda: 100.0)
    observer._states[("POST /guilds/{id}/channels", "7")] = BucketState(
        template="POST /guilds/{id}/channels",
        major="7",
        bucket_hash="x",
        limit=10,
        remaining=0,
        reset_after=60,
        scope="user",
        status=200,
        at=100.0,
    )
    settings = SimpleNamespace(
        data_dir=tmp_path,
        rate_profile=RateProfile.EDGE,
        traffic_limits=PROFILES[RateProfile.EDGE],
    )
    bot = SimpleNamespace(settings=settings, rate_observer=observer, spawn=None)
    gov = TrafficGovernor(bot, observer=observer, limits=PROFILES[RateProfile.EDGE], clock=lambda: 100.0)
    bot.traffic = gov
    with lane(Lane.USER):
        hold = await before_mutate(bot, 7, BUCKET_CHANNEL, now=100.0)
    assert hold is not None
    assert hold.retry_after == pytest.approx(60)


async def test_hidden_create_429_reduces_allowance_and_holds_requests() -> None:
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    bot.traffic = TrafficGovernor(
        bot,
        observer=observer,
        limits=limits,
        profile=RateProfile.EDGE,
        aimd_window=6.0,
        freeze_for=60.0,
    )
    # Третья попытка встречает скрытый лимит, следующие удерживаются по Retry-After.
    async with FakeDiscord(
        hidden_after=2,
        # discord.py ограничивает max_ratelimit_timeout снизу 30 секундами.
        hidden_retry_after=45.0,
        route_limit=1000,
        route_window=10.0,
        global_limit=10_000,
    ) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=1.0)
        for _ in range(24):
            # Через те же ворота, что и боевой путь: сид, комнаты и тикеты сначала
            # спрашивают before_mutate и только потом идут в Discord.
            if await before_mutate(bot, 1, BUCKET_CHANNEL) is not None:
                continue
            try:
                with lane(Lane.BACKGROUND):
                    await http.request(
                        Route("POST", "/guilds/{guild_id}/channels", guild_id=1),
                        json={"name": "a", "type": 0},
                    )
            except (discord.RateLimited, discord.HTTPException) as exc:
                assert trip(bot, 1, BUCKET_CHANNEL, exc) is not None
    assert fake.statuses == [201, 201, 429]
    allowance, _spacing = bot.traffic.mutation_pace(1, BUCKET_CHANNEL)
    assert allowance < 30
    state = bot.traffic._aimd[(1, BUCKET_CHANNEL)]
    assert state.frozen_until > bot.traffic._clock()


async def test_rate_profiles_survive_governor_restart(tmp_path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    sessions = create_sessionmaker(engine)
    try:
        await init_db(engine, auto_migrate=True)
        observer = RateObserver()
        bot = SimpleNamespace(
            settings=SimpleNamespace(rate_profile=RateProfile.EDGE, traffic_limits=PROFILES[RateProfile.EDGE]),
            rate_observer=observer,
            sessions=sessions,
            spawn=None,
        )
        first = TrafficGovernor(bot, observer=observer, limits=PROFILES[RateProfile.EDGE])
        first.note_hidden_429(3, BUCKET_CHANNEL)
        await first.flush()
        dropped, _ = first.mutation_pace(3, BUCKET_CHANNEL)
        second = TrafficGovernor(bot, observer=observer, limits=PROFILES[RateProfile.EDGE])
        await second.hydrate()
        restored, _ = second.mutation_pace(3, BUCKET_CHANNEL)
        assert restored == dropped
        assert restored == 25
    finally:
        await engine.dispose()


async def test_three_forbidden_suppress_fourth_request() -> None:
    bot = _bot()
    bot.traffic.suppress_for = 60.0
    async with FakeDiscord(force_status=403, global_limit=1000, route_limit=1000) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=30)
        route = Route("GET", "/guilds/{guild_id}", guild_id=9)
        for _ in range(3):
            with pytest.raises(discord.Forbidden), lane(Lane.BACKGROUND):
                await http.request(route)
        stuck = asyncio.create_task(_request(http, route))
        await asyncio.sleep(0.1)
        assert len(fake.requests) == 3
        stuck.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck


async def test_invalid_stop_blocks_user_but_not_interaction() -> None:
    observer = RateObserver()
    now = observer._clock()
    limits = ProfileLimits(48, 24, 600, 1, 2, 3)
    for _ in range(3):
        observer._invalid.append(now)
    bot = _bot(observer=observer, limits=limits)
    assert bot.traffic.fuse() is FuseLevel.STOP
    async with FakeDiscord(global_limit=1000, route_limit=1000) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=30)
        blocked = asyncio.create_task(_request(http, Route("GET", "/users/@me"), Lane.USER))
        await asyncio.sleep(0.05)
        with lane(Lane.INTERACTION):
            await http.request(
                Route("POST", "/interactions/{interaction_id}/{token}/callback", interaction_id=1, token="tok")
            )
        assert any(path.endswith("/callback") for _method, path in fake.requests)
        assert not any(path.endswith("/@me") for _method, path in fake.requests)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked


async def test_global_429_cuts_rps_target_by_twenty_percent() -> None:
    observer = RateObserver()
    limits = PROFILES[RateProfile.EDGE]
    bot = _bot(observer=observer, limits=limits)
    async with FakeDiscord(force_global=True) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=1.0)
        with pytest.raises(discord.RateLimited), lane(Lane.BACKGROUND):
            await http.request(Route("GET", "/users/@me"))
    assert bot.traffic.view().rps_target == pytest.approx(limits.global_rps_target * GLOBAL_CUT)


async def _request(http: Any, route: Route, item: Lane = Lane.BACKGROUND) -> None:
    with lane(item):
        await http.request(route)


async def test_user_voice_create_overtakes_background_backlog() -> None:
    bot = _bot()
    async with FakeDiscord(global_limit=10_000, route_limit=10_000) as fake:
        http = fake.http(trace=bot.traffic.trace_config())
        background = [asyncio.create_task(_request(http, Route("GET", "/users/@me"))) for _ in range(60)]
        await asyncio.sleep(0.02)
        await _request(http, Route("POST", "/guilds/{guild_id}/channels", guild_id=1), Lane.USER)
        voice_index = next(i for i, (method, _path) in enumerate(fake.requests) if method == "POST")
        assert voice_index < 10
        await asyncio.gather(*background)
        assert 429 not in fake.statuses
