import discord
import pytest
from discord.http import Route
from tests.fake_discord import FakeDiscord

from conductor.services.rate_observer import RateObserver


async def test_fake_discord_long_retry_raises_ratelimited() -> None:
    async with FakeDiscord(hidden_after=1, hidden_retry_after=45.0, route_limit=50) as fake:
        http = fake.http()
        route = Route("POST", "/guilds/{guild_id}/channels", guild_id=1)
        created = await http.request(route, json={"name": "a", "type": 0})
        assert created["id"] == "1"
        with pytest.raises(discord.RateLimited) as caught:
            await http.request(route, json={"name": "b", "type": 0})
        assert caught.value.retry_after > 30


async def test_observer_on_stand_counts_rps_and_invalid() -> None:
    observer = RateObserver()
    async with FakeDiscord(force_status=403) as fake:
        http = fake.http(trace=observer.trace_config())
        with pytest.raises(discord.Forbidden):
            await http.request(Route("GET", "/users/@me"))
    snap = observer.snapshot()
    assert snap.rps_1s >= 1
    assert snap.rps_60s >= 1
    assert snap.invalid_10m >= 1

    user = RateObserver()
    async with FakeDiscord(hidden_after=0, hidden_retry_after=45.0, scope="user") as fake:
        http = fake.http(trace=user.trace_config())
        with pytest.raises(discord.RateLimited):
            await http.request(Route("POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "a"})
    assert user.snapshot().invalid_10m >= 1
    assert user.last_429(1, "channel") is not None

    shared = RateObserver()
    async with FakeDiscord(hidden_after=0, hidden_retry_after=45.0, scope="shared") as fake:
        http = fake.http(trace=shared.trace_config())
        with pytest.raises(discord.RateLimited):
            await http.request(Route("POST", "/guilds/{guild_id}/channels", guild_id=1), json={"name": "a"})
    assert shared.snapshot().invalid_10m == 0
    found = shared.last_429(1, "channel")
    assert found is not None
    assert found.scope == "shared"
