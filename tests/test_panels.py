from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Panel
from conductor.db.session import session_scope
from conductor.services.census import CENSUS_BATCH, MemberSnapshot, apply_census
from conductor.services.panels import (
    _discord_message_alive,
    find_usable_kind,
    mark_stale,
    probe_tick,
    record_panel,
    restore_panels,
)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def _member(user_id: int, *, joined: datetime, bot: bool = False) -> MemberSnapshot:
    return MemberSnapshot(
        user_id=user_id,
        username=f"u{user_id}",
        display_name=f"n{user_id}",
        is_bot=bot,
        joined_at=joined,
    )


async def test_restore_panels_without_probe_skips_fetch(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    fetches: list[int] = []

    async def probe(_channel_id: int, _message_id: int) -> bool:
        fetches.append(1)
        return False

    bot = SimpleNamespace(sessions=sessions, get_channel=lambda *_: None, add_view=lambda *_a, **_k: None)
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="tk",
                guild_id=1,
                channel_id=20,
                message_id=30,
                ref=1,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
        report = await restore_panels(bot)
        assert report.live == 1
        assert report.stale == 0
        assert fetches == []
        report = await restore_panels(bot, probe=probe)
        assert report.stale == 1
        assert fetches == [1]
    finally:
        await engine.dispose()


async def test_probe_tick_marks_missing_stale(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        get_channel=lambda *_: None,
        add_view=lambda *_a, **_k: None,
        _panel_probe_after=0,
    )
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="vc",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=4,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def missing(_channel_id: int, _message_id: int) -> bool:
            return False

        report = await probe_tick(bot, probe=missing)
        assert report.live == 0
        assert report.stale == 1
    finally:
        await engine.dispose()


async def test_probe_tick_skips_before_boot(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        get_channel=lambda *_: None,
        add_view=lambda *_a, **_k: None,
        _panel_probe_after=0,
        _booted=False,
    )
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="vc",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=4,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def missing(_channel_id: int, _message_id: int) -> bool:
            return False

        report = await probe_tick(bot, probe=missing)
        assert report.live == 0
        assert report.stale == 0
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(Panel))).one()
            assert row.state == "live"
    finally:
        await engine.dispose()


async def test_find_usable_kind_keeps_stale_message(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            stored = await record_panel(
                session,
                kind="vf",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=1,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
            await mark_stale(session, [int(stored.id)])
        async with session_scope(sessions) as session:
            found = await find_usable_kind(session, guild_id=1, kind="vf")
            assert found is not None
            assert int(found.message_id) == 31
            assert found.state == "stale"
    finally:
        await engine.dispose()


async def test_discord_message_alive_treats_forbidden_as_live() -> None:
    class Channel:
        async def fetch_message(self, _message_id: int) -> None:
            raise discord.Forbidden(MagicMock(status=403), "blocked")

    bot = SimpleNamespace(get_channel=lambda _channel_id: Channel())
    assert await _discord_message_alive(bot, 20, 31) is True

    async def fetch_channel(_channel_id: int) -> None:
        raise discord.Forbidden(MagicMock(status=403), "blocked")

    missing = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=fetch_channel)
    assert await _discord_message_alive(missing, 20, 31) is True

    class Gone:
        async def fetch_message(self, _message_id: int) -> None:
            raise discord.NotFound(MagicMock(status=404), "gone")

    stale = SimpleNamespace(get_channel=lambda _channel_id: Gone())
    assert await _discord_message_alive(stale, 20, 31) is False

    unknown = SimpleNamespace(get_channel=lambda _channel_id: None)
    assert await _discord_message_alive(unknown, 20, 31) is True

    class Silent:
        pass

    no_fetch = SimpleNamespace(get_channel=lambda _channel_id: Silent())
    assert await _discord_message_alive(no_fetch, 20, 31) is True


async def test_first_census_uses_batches(tmp_path: Path, monkeypatch: object) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    sleeps: list[int] = []

    async def fake_sleep(_delay: float) -> None:
        sleeps.append(1)

    monkeypatch.setattr("conductor.services.census.asyncio.sleep", fake_sleep)
    joined = datetime(2026, 1, 1, tzinfo=UTC)
    members = [_member(index, joined=joined) for index in range(CENSUS_BATCH + 1)]
    try:
        result = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=None,
            now=joined,
        )
        assert result.created == CENSUS_BATCH + 1
        assert len(sleeps) == 2
    finally:
        await engine.dispose()


async def test_repeat_census_only_changed(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    joined = datetime(2026, 1, 1, tzinfo=UTC)
    members = [_member(1, joined=joined), _member(2, joined=joined)]
    try:
        first = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=None,
            now=joined,
        )
        assert first.created == 2
        repeat = await apply_census(
            sessions,
            guild_id=1,
            members=members,
            census_at=joined,
            now=joined + timedelta(hours=1),
        )
        assert repeat.created == 0
        assert repeat.updated == 0
        changed = [
            members[0],
            MemberSnapshot(
                user_id=2,
                username="u2",
                display_name="n2",
                is_bot=False,
                joined_at=joined + timedelta(days=1),
            ),
        ]
        again = await apply_census(
            sessions,
            guild_id=1,
            members=changed,
            census_at=joined,
            now=joined + timedelta(days=1),
        )
        assert again.updated == 1
        assert again.created == 0
    finally:
        await engine.dispose()
