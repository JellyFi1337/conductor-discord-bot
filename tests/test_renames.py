from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import RenameState
from conductor.db.session import session_scope
from conductor.domain.renames import Deferred
from conductor.services.renames import RenameService

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeChannel:
    def __init__(self, name: str, *, guild_id: int = 1, channel_id: int = 20) -> None:
        self.name = name
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.edits: list[str] = []
        self.error: BaseException | None = None

    async def edit(self, *, name: str, reason: str = "") -> None:
        if self.error is not None:
            raise self.error
        self.edits.append(name)
        self.name = name


async def _service(tmp_path: Path, clock: Clock, channel: FakeChannel) -> RenameService:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        scheduler=None,
        queue=None,
        get_channel=lambda channel_id: channel if channel.id == channel_id else None,
        engine=engine,
    )
    return RenameService(bot, clock=clock)


def test_edit_name_only_in_renames() -> None:
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "edit(name=" not in text:
            continue
        if path.name == "renames.py":
            continue
        hits.append(path.relative_to(SRC).as_posix())
    assert hits == []


async def test_rename_noop_same_name(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("room")
    service = await _service(tmp_path, clock, channel)
    try:
        result = await service.rename(channel, "room", key="voice")
        assert result is None
        assert channel.edits == []
    finally:
        await service._bot.engine.dispose()


async def test_rename_limit_defers_third(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("a")
    service = await _service(tmp_path, clock, channel)
    try:
        assert await service.rename(channel, "one", key="voice") is None
        assert await service.rename(channel, "two", key="voice") is None
        third = await service.rename(channel, "three", key="voice")
        assert isinstance(third, Deferred)
        assert third.until == clock.now + timedelta(minutes=10)
        assert channel.name == "two"
        assert channel.edits == ["one", "two"]
    finally:
        await service._bot.engine.dispose()


async def test_rename_429_defers(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("a")
    error = discord.HTTPException(MagicMock(status=429), "rate")
    error.retry_after = 45
    channel.error = error
    service = await _service(tmp_path, clock, channel)
    try:
        result = await service.rename(channel, "next", key="voice")
        assert isinstance(result, Deferred)
        assert result.until == clock.now + timedelta(seconds=45)
        assert channel.edits == []
    finally:
        await service._bot.engine.dispose()


async def test_rename_ratelimited_exception_defers(tmp_path: Path) -> None:
    """`discord.RateLimited` is not an HTTPException; it must defer, not escape."""
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("a")
    channel.error = discord.RateLimited(120.0)
    service = await _service(tmp_path, clock, channel)
    try:
        result = await service.rename(channel, "next", key="voice")
        assert isinstance(result, Deferred)
        assert result.until == clock.now + timedelta(seconds=120)
        assert channel.edits == []
    finally:
        await service._bot.engine.dispose()


async def test_retry_forgets_deleted_channel(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("a")
    service = await _service(tmp_path, clock, channel)
    try:
        await service.rename(channel, "one", key="voice")
        await service.rename(channel, "two", key="voice")
        deferred = await service.rename(channel, "three", key="voice")
        assert isinstance(deferred, Deferred)
        clock.now = deferred.until
        service._bot.get_channel = lambda _channel_id: None
        assert await service.retry_due() == 0
        async with session_scope(service._bot.sessions) as session:
            assert (await session.scalars(select(RenameState))).all() == []
    finally:
        await service._bot.engine.dispose()


async def test_rename_retry_applies_pending(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    channel = FakeChannel("a")
    service = await _service(tmp_path, clock, channel)
    try:
        await service.rename(channel, "one", key="voice")
        await service.rename(channel, "two", key="voice")
        deferred = await service.rename(channel, "three", key="voice")
        assert isinstance(deferred, Deferred)
        clock.now = deferred.until
        applied = await service.retry_due()
        assert applied == 1
        assert channel.name == "three"
        assert "three" in channel.edits
    finally:
        await service._bot.engine.dispose()
