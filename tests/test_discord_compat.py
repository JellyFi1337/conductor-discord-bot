from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import discord.http
import pytest
from discord.http import Route
from tests.fake_discord import FakeDiscord

from conductor.discord_compat import AvailableTokenRatelimit, install_ratelimit_compat

LONG_WINDOW = 34909.724454789


def response(*, limit=10, remaining=9, window=LONG_WINDOW):
    return SimpleNamespace(headers={
        "X-Ratelimit-Limit": str(limit),
        "X-Ratelimit-Remaining": str(remaining),
        "X-Ratelimit-Reset-After": str(window),
    })


async def test_unknown_bucket_wakes_parallel_requests_with_available_tokens():
    bucket = AvailableTokenRatelimit(600)
    await bucket.acquire()
    pending = [asyncio.create_task(bucket.acquire()) for _ in range(9)]
    await asyncio.sleep(0)
    bucket.update(response())
    await bucket.__aexit__(None, None, None)
    await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
    assert bucket.remaining == 0
    assert bucket.outgoing == 9
    with pytest.raises(discord.RateLimited):
        await bucket.acquire()


async def test_exhausted_long_window_does_not_refill_or_swallow_pending_errors():
    bucket = AvailableTokenRatelimit(600)
    await bucket.acquire()
    pending = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    bucket.update(response(limit=1, remaining=0))
    await bucket.__aexit__(None, None, None)
    with pytest.raises(discord.RateLimited):
        await asyncio.wait_for(pending, timeout=1)
    assert bucket.remaining == 0
    for _ in range(2):
        with pytest.raises(discord.RateLimited):
            await bucket.acquire()
    bucket.expires = asyncio.get_running_loop().time() - 1
    await bucket.acquire()
    assert bucket.remaining == 0
    assert bucket.outgoing == 1


async def test_cancelled_waiter_does_not_consume_available_token():
    bucket = AvailableTokenRatelimit(600)
    await bucket.acquire()
    cancelled = asyncio.create_task(bucket.acquire())
    pending = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    bucket.update(response(limit=2, remaining=1))
    await bucket.__aexit__(None, None, None)
    await asyncio.wait_for(pending, timeout=1)
    assert bucket.remaining == 0
    assert bucket.outgoing == 1


async def test_out_of_order_headers_cannot_restore_reserved_tokens():
    bucket = AvailableTokenRatelimit(600)
    await bucket.acquire()
    bucket.update(response())
    await bucket.__aexit__(None, None, None)
    for _ in range(3):
        await bucket.acquire()
    assert bucket.remaining == 6
    for remaining in (7, 8, 6):
        bucket.update(response(remaining=remaining))
        await bucket.__aexit__(None, None, None)
        assert bucket.remaining == 6


@pytest.mark.parametrize("resource", ["channels", "roles", "emojis"])
async def test_http_uses_entire_long_window_then_stops_before_extra_request(resource):
    async with FakeDiscord(route_limit=3, route_window=LONG_WINDOW) as fake:
        http = fake.http(max_ratelimit_timeout=600)
        route = Route("POST", "/guilds/{guild_id}/" + resource, guild_id=1)
        for _ in range(3):
            await http.request(route, json={"name": "example"})
        for _ in range(2):
            with pytest.raises(discord.RateLimited):
                await http.request(route, json={"name": "example"})
        assert fake.statuses == [201, 201, 201]


async def test_short_exhausted_window_waits_and_resumes():
    async with FakeDiscord(route_limit=2, route_window=0.05) as fake:
        http = fake.http(max_ratelimit_timeout=600)
        route = Route("POST", "/guilds/{guild_id}/channels", guild_id=1)
        await asyncio.wait_for(asyncio.gather(*(http.request(route) for _ in range(6))), timeout=3)
        assert fake.statuses == [201] * 6


async def test_successful_last_request_returns_before_short_window_reset():
    async with FakeDiscord(route_limit=1, route_window=0.5) as fake:
        http = fake.http(max_ratelimit_timeout=600)
        route = Route("POST", "/guilds/{guild_id}/channels", guild_id=1)
        # ID должен попасть в сервис сразу, а не потеряться при истечении
        # бюджета команды внутри ожидания SDK после успешного ответа.
        await asyncio.wait_for(http.request(route), timeout=0.2)
        blocked = asyncio.create_task(http.request(route))
        await asyncio.sleep(0.05)
        assert not blocked.done()
        assert fake.statuses == [201]
        await asyncio.wait_for(blocked, timeout=1)
        assert fake.statuses == [201, 201]


def test_install_is_idempotent():
    install_ratelimit_compat()
    install_ratelimit_compat()
    assert discord.http.Ratelimit is AvailableTokenRatelimit
