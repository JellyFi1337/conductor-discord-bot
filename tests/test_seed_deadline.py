from __future__ import annotations

import asyncio
from time import monotonic, time

import discord
import pytest
from discord.http import Route
from tests.fake_discord import FakeDiscord

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_ROLE, HoldSource
from conductor.domain.seed import SeedVerdict, skeleton_entries
from conductor.services.create_lock import find_active, remember
from conductor.services.limits import OutboundGate
from conductor.services.rate_observer import RateObserver
from conductor.services.seed import GuildSeed
from conductor.services.traffic import TrafficGovernor
from test_seed import _bot, _FakeGuild, _url


@pytest.fixture
async def setup(tmp_path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _bot(create_sessionmaker(engine), guild)
    bot.rate_observer = RateObserver()
    bot.traffic = TrafficGovernor(bot, observer=bot.rate_observer)
    bot.outbound = OutboundGate(governor=bot.traffic)
    try:
        yield bot, guild, GuildSeed(bot)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("route_limit,route_window", [(3, 0.2), (500, 34909.724454789)])
async def test_complete_build_uses_real_http_buckets_and_binds_every_object(setup, route_limit, route_window):
    bot, guild, service = setup
    async with FakeDiscord(route_limit=route_limit, route_window=route_window, global_limit=10_000) as fake:
        http = fake.http(trace=bot.traffic.trace_config())
        for method in ("create_category", "create_text_channel", "create_voice_channel", "create_role"):
            original = getattr(guild, method)
            resource = "roles" if method == "create_role" else "channels"

            async def create(name, *, _original=original, _resource=resource, **kwargs):
                await http.request(
                    Route("POST", "/guilds/{guild_id}/" + _resource, guild_id=guild.id), json={"name": name}
                )
                return await _original(name, **kwargs)

            setattr(guild, method, create)
        started = monotonic()
        report = await service.run(guild, actor_id=7, max_duration=20)
        assert monotonic() - started < 20
        assert report.created == len(skeleton_entries())
        assert not report.held
        assert not report.failed
        assert not report.missing
        assert 429 not in fake.statuses
        assert len(fake.requests) == len(skeleton_entries())
        snapshot = await service.snapshot(guild)
        assert all(snapshot[entry.key].bound_id for entry in skeleton_entries())
        repeat = await service.run(guild, actor_id=7)
        assert repeat.created == 0
        assert len(fake.requests) == len(skeleton_entries())


async def test_short_429_is_waited_and_retried_in_same_command(setup):
    bot, guild, service = setup
    original = guild.create_role
    attempts = []

    async def limited(name, **kwargs):
        attempts.append(monotonic())
        if len(attempts) == 1:
            raise discord.RateLimited(0.1)
        return await original(name, **kwargs)

    guild.create_role = limited
    report = await service.run(guild, actor_id=7, only_keys=frozenset({"role_guest"}), max_duration=8)
    assert report.created_roles == 1
    assert not report.held
    assert not report.failed
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= 0.1
    assert find_active(bot, guild.id, (BUCKET_ROLE,), time()) is None


async def test_known_short_hold_is_waited_without_queue(setup):
    bot, guild, service = setup
    remember(bot, guild.id, BUCKET_ROLE, 0.1, time(), source=HoldSource.DISCORD)
    started = monotonic()
    report = await service.run(guild, actor_id=7, only_keys=frozenset({"role_guest"}), max_duration=8)
    assert monotonic() - started >= 0.1
    assert report.created_roles == 1
    assert not report.held


async def test_long_hold_returns_promptly_and_other_buckets_complete(setup):
    bot, guild, service = setup
    remember(bot, guild.id, BUCKET_ROLE, 86400, time(), source=HoldSource.DISCORD)
    started = monotonic()
    report = await service.run(guild, actor_id=7, max_duration=8)
    assert monotonic() - started < 8
    assert report.created_roles == 0
    assert report.created_channels + report.created_categories == 32
    assert report.held
    assert find_active(bot, guild.id, (BUCKET_ROLE,), time()) is not None


async def test_deadline_cancels_request_preserves_bindings_and_allows_resume(setup):
    _bot_obj, guild, service = setup
    original = guild.create_role
    attempts = 0
    cancelled = asyncio.Event()

    async def stalled(name, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()
        return await original(name, **kwargs)

    guild.create_role = stalled
    keys = frozenset({"role_guest", "role_regular"})
    report = await service.run(guild, actor_id=7, only_keys=keys, max_duration=0.3)
    assert cancelled.is_set()
    assert report.verdict is SeedVerdict.PARTIAL
    assert report.created_roles == 1
    assert len(report.held) == 1
    snapshot = await service.snapshot(guild)
    saved = {key: snapshot[key].bound_id for key in keys if snapshot[key].bound_id}
    assert len(saved) == 1
    guild.create_role = original
    again = await service.run(guild, actor_id=7, only_keys=keys, max_duration=8)
    assert again.created_roles == 1
    assert not again.held
    final = await service.snapshot(guild)
    assert all(final[key].bound_id for key in keys)
    assert all(final[key].bound_id == value for key, value in saved.items())


async def test_waiting_for_another_build_counts_against_deadline(setup):
    _bot_obj, guild, service = setup
    lock = service._guild_lock(guild.id)
    async with lock:
        report = await service.run(guild, actor_id=7, max_duration=0.01)
        assert lock.locked()
        assert report.verdict is SeedVerdict.PARTIAL
        assert report.created == 0


async def test_deadline_during_library_retry_preserves_discord_hold(setup):
    bot, guild, service = setup
    async with FakeDiscord(hidden_after=0, hidden_retry_after=45, route_window=60) as fake:
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=600)
        original = guild.create_category

        async def limited(name, **kwargs):
            await http.request(Route("POST", "/guilds/{guild_id}/channels", guild_id=guild.id), json={"name": name})
            return await original(name, **kwargs)

        guild.create_category = limited
        keys = frozenset({"cat_entry"})
        report = await service.run(guild, actor_id=7, only_keys=keys, max_duration=0.3)
        assert report.verdict is SeedVerdict.PARTIAL
        assert fake.statuses == [429]
        assert find_active(bot, guild.id, (BUCKET_CHANNEL,), time()) is not None
        again = await service.run(guild, actor_id=7, only_keys=keys, max_duration=1)
        assert again.held == ("cat_entry",)
        assert fake.statuses == [429]


async def test_global_retry_does_not_leave_discord_client_locked(setup):
    bot, guild, service = setup
    async with FakeDiscord(force_global=True) as fake:
        fake.global_retry_after = 0.2
        http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=600)
        original = guild.create_category

        async def limited(name, **kwargs):
            await http.request(Route("POST", "/guilds/{guild_id}/channels", guild_id=guild.id), json={"name": name})
            return await original(name, **kwargs)

        guild.create_category = limited
        report = await service.run(guild, actor_id=7, only_keys=frozenset({"cat_entry"}), max_duration=0.1)
        assert report.held == ("cat_entry",)
        assert http._global_over.is_set()
        fake.force_global = False
        started = monotonic()
        await asyncio.wait_for(http.request(Route("GET", "/users/@me")), timeout=2)
        assert monotonic() - started >= 0.1
        assert fake.statuses == [429, 200]


async def test_slow_transport_cleanup_does_not_extend_command_deadline(setup):
    _bot_obj, guild, service = setup
    cleaning = asyncio.Event()
    cleaned = asyncio.Event()

    async def stalled(name, **kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cleaning.set()
            await asyncio.sleep(0.5)
            cleaned.set()
            raise

    guild.create_role = stalled
    started = monotonic()
    report = await service.run(guild, actor_id=7, only_keys=frozenset({"role_guest"}), max_duration=0.05)
    assert monotonic() - started < 0.4
    assert report.verdict is SeedVerdict.PARTIAL
    await asyncio.wait_for(cleaning.wait(), timeout=1)
    await asyncio.wait_for(cleaned.wait(), timeout=1)
