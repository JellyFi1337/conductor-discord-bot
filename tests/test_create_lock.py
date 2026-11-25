from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from conductor.domain.create_lock import (
    BUCKET_CHANNEL,
    BUCKET_EMOJI,
    BUCKET_ROLE,
    CLOUDFLARE_FALLBACK,
    OBSERVE_MATCH_WINDOW,
    PERSIST_CAP,
    CreateHold,
    HoldSource,
    ObservedLimit,
    classify,
    hold_from_retry,
    hold_from_wait,
    persisted_until,
)
from conductor.presentation.motion import DeployReport
from conductor.services.create_lock import (
    LOCK_FILE,
    before_mutate,
    clear_guild,
    deploy_emojis,
    find_active,
    hold_for,
    note_success,
    remember,
    retry_after,
    trip,
)
from conductor.services.rate_observer import RateObserver


def test_hold_from_retry_and_remaining() -> None:
    hold = hold_from_retry(bucket=BUCKET_ROLE, retry_after=30, now=100.0)
    assert hold.active(100.0) is True
    assert hold.remaining(120.0) == 10.0
    assert hold.active(131.0) is False


def test_remember_survives_new_bot_via_file(tmp_path: Path) -> None:
    first = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    hold = remember(first, 99, BUCKET_ROLE, 80.0, now=10.0, source=HoldSource.DISCORD)
    assert hold.active(11.0) is True
    second = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    found = find_active(second, 99, (BUCKET_ROLE,), now=11.0)
    assert found is not None
    assert found.bucket == BUCKET_ROLE
    assert found.remaining(11.0) > 70


def test_hold_does_not_expire_before_retry_after() -> None:
    hold = hold_from_wait(bucket=BUCKET_CHANNEL, wait=0.5, now=1000.0)
    assert hold.active(1000.25)
    assert not hold.active(1000.5)


def test_retry_after_reads_discord_shape() -> None:
    assert retry_after(SimpleNamespace(retry_after=12.5)) == 12.5
    assert retry_after(SimpleNamespace(status=429, retry_after=None)) == 0.0
    assert retry_after(RuntimeError("no")) is None


async def test_before_mutate_respects_stored_hold() -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=None))
    remember(bot, 7, BUCKET_CHANNEL, 40.0, now=10.0, source=HoldSource.DISCORD)
    hold = await before_mutate(bot, 7, BUCKET_CHANNEL, now=11.0)
    assert hold is not None
    assert hold.bucket == BUCKET_CHANNEL


def _observed(*, status: int, scope: str = "", age: float, now: float = 100.0) -> ObservedLimit:
    return ObservedLimit(
        guild_id=1,
        bucket=BUCKET_CHANNEL,
        status=status,
        scope=scope,
        retry_after=12.0 if status == 429 else None,
        remaining=0 if status == 429 else 9,
        reset_after=45.0,
        at=now - age,
    )


@pytest.mark.parametrize(
    ("rate_limited", "status", "observed", "expected"),
    [
        (True, None, None, HoldSource.LIBRARY),
        (True, None, _observed(status=200, age=1.0), HoldSource.LIBRARY),
        (True, None, _observed(status=429, age=1.0), HoldSource.DISCORD),
        (True, None, _observed(status=429, scope="shared", age=1.0), HoldSource.DISCORD_SHARED),
        (True, None, _observed(status=429, age=OBSERVE_MATCH_WINDOW + 1), HoldSource.LIBRARY),
        (False, 429, None, HoldSource.DISCORD),
        (False, 429, _observed(status=429, scope="shared", age=0.0), HoldSource.DISCORD_SHARED),
        (False, 500, None, None),
        (False, None, None, None),
        (False, 200, _observed(status=429, age=1.0), HoldSource.DISCORD),
    ],
)
def test_classify_table(
    rate_limited: bool,
    status: int | None,
    observed: ObservedLimit | None,
    expected: HoldSource | None,
) -> None:
    assert classify(rate_limited=rate_limited, status=status, observed=observed, now=100.0) is expected


def test_persisted_until_caps_discord_and_skips_others() -> None:
    now = 50.0
    discord_hold = CreateHold(bucket=BUCKET_CHANNEL, until=now + 10_000, retry_after=10_000, source=HoldSource.DISCORD)
    assert persisted_until(discord_hold, now) == now + PERSIST_CAP
    for source in (HoldSource.LIBRARY, HoldSource.DISCORD_SHARED, HoldSource.LOCAL):
        hold = CreateHold(bucket=BUCKET_CHANNEL, until=now + 80, retry_after=80, source=source)
        assert persisted_until(hold, now) is None


def test_trip_rate_limited_without_observation_is_library(tmp_path: Path) -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), rate_observer=None)
    hold = trip(bot, 1, BUCKET_CHANNEL, discord.RateLimited(45.0), now=10.0)
    assert hold is not None
    assert hold.source is HoldSource.LIBRARY
    assert not (tmp_path / LOCK_FILE).exists()


def test_trip_observed_429_is_discord_capped(tmp_path: Path) -> None:
    observer = SimpleNamespace(
        last_429=lambda _gid, _bucket: ObservedLimit(
            guild_id=1,
            bucket=BUCKET_CHANNEL,
            status=429,
            scope="",
            retry_after=90.0,
            remaining=0,
            reset_after=90.0,
            at=10.0,
        )
    )
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), rate_observer=observer)
    hold = trip(bot, 1, BUCKET_CHANNEL, discord.RateLimited(90.0), now=10.0)
    assert hold is not None
    assert hold.source is HoldSource.DISCORD
    raw = (tmp_path / LOCK_FILE).read_text(encoding="utf-8")
    assert '"source": "discord"' in raw
    assert hold.until <= 10.0 + 90.0
    found = find_active(SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path)), 1, (BUCKET_CHANNEL,), now=11.0)
    assert found is not None
    assert found.until <= 11.0 + PERSIST_CAP


def test_trip_shared_scope_stays_in_memory(tmp_path: Path) -> None:
    observer = SimpleNamespace(
        last_429=lambda _gid, _bucket: ObservedLimit(
            guild_id=1,
            bucket=BUCKET_CHANNEL,
            status=429,
            scope="shared",
            retry_after=20.0,
            remaining=0,
            reset_after=20.0,
            at=10.0,
        )
    )
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), rate_observer=observer)
    hold = trip(bot, 1, BUCKET_CHANNEL, discord.RateLimited(20.0), now=10.0)
    assert hold is not None
    assert hold.source is HoldSource.DISCORD_SHARED
    assert not (tmp_path / LOCK_FILE).exists()
    assert find_active(bot, 1, (BUCKET_CHANNEL,), now=11.0) is hold


def test_trip_cloudflare_uses_fallback(tmp_path: Path) -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), rate_observer=None)
    hold = trip(bot, 1, BUCKET_CHANNEL, SimpleNamespace(status=429, retry_after=None), now=10.0)
    assert hold is not None
    assert hold.source is HoldSource.DISCORD
    assert hold.retry_after == CLOUDFLARE_FALLBACK


def test_note_success_clears_memory_and_file(tmp_path: Path) -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    remember(bot, 4, BUCKET_CHANNEL, 80.0, now=10.0, source=HoldSource.DISCORD)
    assert find_active(bot, 4, (BUCKET_CHANNEL,), now=11.0) is not None
    note_success(bot, 4, BUCKET_CHANNEL, now=11.0)
    assert find_active(bot, 4, (BUCKET_CHANNEL,), now=11.0) is None
    raw = (tmp_path / LOCK_FILE).read_text(encoding="utf-8")
    assert "channel" not in raw or raw.strip() in {"{}", ""}


def test_hydrate_drops_old_format_expired_and_caps(tmp_path: Path) -> None:
    path = tmp_path / LOCK_FILE
    path.write_text(
        '{"1": {"channel": {"until": 90.0, "retry_after": 80.0}},'
        ' "2": {"role": {"until": 5.0, "retry_after": 1.0, "source": "discord"}},'
        ' "3": {"channel": {"until": 259300.0, "retry_after": 259200.0, "source": "discord"}}}',
        encoding="utf-8",
    )
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    now = 100.0
    assert find_active(bot, 1, (BUCKET_CHANNEL,), now=now) is None
    assert find_active(bot, 2, (BUCKET_ROLE,), now=now) is None
    found = find_active(bot, 3, (BUCKET_CHANNEL,), now=now)
    assert found is not None
    assert found.until == pytest.approx(now + PERSIST_CAP)
    rewritten = path.read_text(encoding="utf-8")
    assert '"1"' not in rewritten
    assert '"2"' not in rewritten
    assert '"3"' in rewritten


async def test_old_local_quota_does_not_block_creations(tmp_path: Path) -> None:
    now = 2000.0
    bot = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        create_holds={},
    )
    hold = await hold_for(bot, 7, BUCKET_CHANNEL, now=now + 1.0)
    assert hold is None
    for _ in range(150):
        assert await hold_for(bot, 7, BUCKET_CHANNEL, now=now + 1.0) is None
    assert not (tmp_path / LOCK_FILE).exists()


def test_clear_guild_removes_memory_and_file(tmp_path: Path) -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    remember(bot, 8, BUCKET_CHANNEL, 80.0, now=10.0, source=HoldSource.DISCORD)
    remember(bot, 9, BUCKET_ROLE, 80.0, now=10.0, source=HoldSource.DISCORD)
    clear_guild(bot, 8, now=11.0)
    assert find_active(bot, 8, (BUCKET_CHANNEL,), now=11.0) is None
    assert find_active(bot, 9, (BUCKET_ROLE,), now=11.0) is not None


def test_create_hold_defaults_to_library() -> None:
    hold = CreateHold(bucket=BUCKET_CHANNEL, until=10.0, retry_after=5.0)
    assert hold.source is HoldSource.LIBRARY


def test_remember_requires_source() -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=None))
    with pytest.raises(TypeError):
        remember(bot, 1, BUCKET_CHANNEL, 80.0, 10.0)  # type: ignore[misc]


def test_note_success_does_not_create_missing_file(tmp_path: Path) -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    note_success(bot, 4, BUCKET_CHANNEL, now=11.0)
    assert not (tmp_path / LOCK_FILE).exists()


def _params(url: str, *, status: int = 200, headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        method="POST",
        url=url,
        response=SimpleNamespace(status=status, headers=headers or {}),
    )


async def test_trip_uses_last_429_not_later_success(tmp_path: Path) -> None:
    clock = {"t": 10.0}
    observer = RateObserver(clock=lambda: clock["t"])
    await observer._on_request_end(
        None,
        None,
        _params(
            "https://discord.com/api/v10/guilds/1/channels",
            status=429,
            headers={"Retry-After": "90", "X-RateLimit-Scope": "user"},
        ),
    )
    clock["t"] = 11.0
    await observer._on_request_end(
        None,
        None,
        _params("https://discord.com/api/v10/guilds/1/channels", status=201),
    )
    assert observer.last(1, BUCKET_CHANNEL) is not None
    assert observer.last(1, BUCKET_CHANNEL).status == 201  # type: ignore[union-attr]
    assert observer.last_429(1, BUCKET_CHANNEL) is not None
    assert observer.last_429(1, BUCKET_CHANNEL).status == 429  # type: ignore[union-attr]
    bot = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), rate_observer=observer)
    hold = trip(bot, 1, BUCKET_CHANNEL, discord.RateLimited(90.0), now=11.0)
    assert hold is not None
    assert hold.source is HoldSource.DISCORD


async def test_deploy_emojis_rate_limited_without_429_is_library(tmp_path: Path) -> None:
    report = DeployReport(
        created=0, skipped=0, loaded=0, total=1, failed=1, retry_after=45.0, rate_limited=True
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        motion=SimpleNamespace(deploy=AsyncMock(return_value=report)),
        rate_observer=None,
    )
    result = await deploy_emojis(bot)
    assert result is report
    hold = find_active(bot, 0, (BUCKET_EMOJI,), now=time())
    assert hold is not None
    assert hold.source is HoldSource.LIBRARY
    assert not (tmp_path / LOCK_FILE).exists()
