from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from conductor.domain.create_lock import BUCKET_CHANNEL, BUCKET_EMOJI, BUCKET_ROLE
from conductor.domain.traffic import MAX_BUCKETS, TrafficSnapshot
from conductor.services.rate_observer import RateObserver


def test_discord_py_minor_is_pinned() -> None:
    # поведение acquire() и http_trace сверено именно для 2.7
    assert discord.version_info[:2] == (2, 7)


def _params(
    url: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> SimpleNamespace:
    return SimpleNamespace(
        method=method,
        url=url,
        response=SimpleNamespace(status=status, headers=headers or {}),
    )


async def test_observer_parses_create_paths() -> None:
    observer = RateObserver(clock=lambda: 50.0)
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/guilds/1/channels"))
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/guilds/1/roles", status=201))
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/guilds/2/emojis"))
    channel = observer.last(1, BUCKET_CHANNEL)
    role = observer.last(1, BUCKET_ROLE)
    emoji = observer.last(2, BUCKET_EMOJI)
    assert channel is not None and channel.status == 200
    assert role is not None and role.status == 201
    assert emoji is not None and emoji.bucket == BUCKET_EMOJI
    snap = observer.snapshot(1)
    assert isinstance(snap, TrafficSnapshot)
    templates = {item.template for item in snap.buckets}
    assert "POST /guilds/{id}/channels" in templates
    assert "POST /guilds/{id}/roles" in templates


async def test_observer_records_application_emoji_on_guild_zero() -> None:
    observer = RateObserver(clock=lambda: 4.0)
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/applications/9/emojis",
            status=429,
            headers={"Retry-After": "12", "X-RateLimit-Scope": "user"},
        ),
    )
    found = observer.last_429(0, BUCKET_EMOJI)
    assert found is not None
    assert found.guild_id == 0
    assert found.status == 429


async def test_observer_keeps_last_429_after_success() -> None:
    clock = {"t": 1.0}
    observer = RateObserver(clock=lambda: clock["t"])
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/guilds/1/channels",
            status=429,
            headers={"Retry-After": "20"},
        ),
    )
    clock["t"] = 2.0
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/guilds/1/channels", status=201))
    assert observer.last(1, BUCKET_CHANNEL).status == 201  # type: ignore[union-attr]
    assert observer.last_429(1, BUCKET_CHANNEL).status == 429  # type: ignore[union-attr]


async def test_observer_records_non_create_routes_but_not_as_create_last() -> None:
    observer = RateObserver(clock=lambda: 1.0)
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/channels/9/messages"))
    await observer._on_request_end(None, None, _params("https://discord.com/api/v10/guilds/1/channels", method="GET"))
    assert observer.last(1, BUCKET_CHANNEL) is None
    snap = observer.snapshot()
    assert len(snap.buckets) == 2


async def test_observer_records_shared_429() -> None:
    observer = RateObserver(clock=lambda: 9.0)
    await observer._on_request_end(
        None,
        None,
        _params(
            "/api/v10/guilds/4/channels",
            status=429,
            headers={
                "X-RateLimit-Scope": "shared",
                "Retry-After": "12.5",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset-After": "12.5",
            },
        ),
    )
    found = observer.last(4, BUCKET_CHANNEL)
    assert found is not None
    assert found.status == 429
    assert found.scope == "shared"
    assert found.retry_after == 12.5
    assert found.remaining == 0
    snap = observer.snapshot()
    assert snap.invalid_10m == 0
    assert snap.top_429 == ("POST /guilds/{id}/channels",)


async def test_observer_counts_invalid_except_shared() -> None:
    observer = RateObserver(clock=lambda: 3.0)
    await observer._on_request_end(
        None,
        None,
        _params("https://discord.com/api/v10/guilds/1/channels", status=403),
    )
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/guilds/1/channels",
            status=429,
            headers={"X-RateLimit-Scope": "user", "Retry-After": "1"},
        ),
    )
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/guilds/1/roles",
            status=429,
            headers={"X-RateLimit-Scope": "shared", "Retry-After": "1"},
        ),
    )
    snap = observer.snapshot()
    assert snap.invalid_10m == 2
    assert snap.top_403 == ("POST /guilds/{id}/channels",)


async def test_observer_counts_global_429_header() -> None:
    observer = RateObserver(clock=lambda: 8.0)
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/users/@me",
            status=429,
            method="GET",
            headers={"X-RateLimit-Global": "true", "X-RateLimit-Scope": "global", "Retry-After": "1"},
        ),
    )
    snap = observer.snapshot()
    assert snap.global_429_10m == 1
    assert snap.invalid_10m == 1


async def test_observer_caps_bucket_memory() -> None:
    observer = RateObserver(clock=lambda: 1.0)
    for index in range(5000):
        await observer._on_request_end(
            None,
            None,
            _params(f"https://discord.com/api/v10/channels/{index}/messages", method="GET"),
        )
    assert observer.stored_count() <= MAX_BUCKETS


async def test_observer_swallows_bad_headers_and_callback_errors() -> None:
    observer = RateObserver(clock=lambda: 3.0)
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/guilds/1/channels",
            status=429,
            headers={"Retry-After": "nope", "X-RateLimit-Remaining": "x"},
        ),
    )
    found = observer.last(1, BUCKET_CHANNEL)
    assert found is not None
    assert found.retry_after is None
    assert found.remaining is None
    bad = MagicMock()
    bad.method = "POST"
    bad.url = "https://discord.com/api/v10/guilds/1/channels"
    type(bad).response = property(lambda _self: (_ for _ in ()).throw(RuntimeError("boom")))
    await observer._on_request_end(None, None, bad)


async def test_observer_start_counts_rps() -> None:
    observer = RateObserver(clock=lambda: 20.0)
    await observer._on_request_start(None, None, None)
    await observer._on_request_start(None, None, None)
    snap = observer.snapshot()
    assert snap.rps_1s == 2
    assert snap.rps_60s == 2
