from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Reminder
from conductor.db.session import session_scope
from conductor.domain.reminders import (
    PER_USER_LIMIT,
    CancelVerdict,
    CreateVerdict,
    ReminderCard,
    can_accept,
    parse_due,
    parse_text,
)
from conductor.presentation.layout import collected_text
from conductor.presentation.reminders import created_view, due_view
from conductor.services.queue import ActionQueue
from conductor.services.reminders import ReminderService

STAMP = datetime(2026, 9, 5, 6, 53, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeMessage:
    def __init__(self) -> None:
        self.id = 1
        self.views: list[object] = []

    async def edit(self, **kwargs: object) -> FakeMessage:
        self.views.append(kwargs.get("view"))
        return self


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[FakeMessage] = []
        self.mentions: list[object] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        message = FakeMessage()
        self.sent.append(message)
        self.mentions.append(kwargs.get("allowed_mentions"))
        return message


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.sent: list[FakeMessage] = []

    async def send(self, **_kwargs: object) -> FakeMessage:
        message = FakeMessage()
        self.sent.append(message)
        return message


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels: dict[int, FakeChannel] = {}
        self.members: dict[int, FakeUser] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeUser | None:
        return self.members.get(user_id)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _spawn_box() -> tuple[list[asyncio.Task[object]], object]:
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    return spawned, spawn


async def _await_spawned(spawned: list[asyncio.Task[object]]) -> None:
    if spawned:
        await asyncio.gather(*spawned)
        spawned.clear()


def _bot(sessions, guild: FakeGuild, queue: ActionQueue, *, user: FakeUser | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=queue,
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        get_user=lambda uid: user if user is not None and uid == user.id else None,
        outbound=None,
    )


def test_parse_relative_and_absolute_and_rejects_past() -> None:
    assert parse_due("10m", now=STAMP) == STAMP + timedelta(minutes=10)
    assert parse_due("2h", now=STAMP) == STAMP + timedelta(hours=2)
    assert parse_due("2026-09-06 12:00", now=STAMP) == datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    assert parse_due("2020-01-01", now=STAMP) is None
    assert parse_due("2027-01-01", now=STAMP) is None
    assert parse_due("", now=STAMP) is None
    assert parse_text("  купи хлеб  ") == "купи хлеб"
    assert parse_text("") is None
    assert parse_text("x" * 201) is None
    assert can_accept(9) is True
    assert can_accept(PER_USER_LIMIT) is False


def test_created_card_uses_relative_timestamp() -> None:
    text = collected_text(
        created_view(
            ReminderCard(
                reminder_id=3,
                guild_id=1,
                channel_id=5,
                user_id=11,
                text="проверить духовку",
                due_at=STAMP + timedelta(minutes=10),
            )
        )
    )
    assert "проверить духовку" in text
    assert "<t:" in text
    assert ":R>" in text
    assert "@everyone" not in text


def test_due_card_quotes_and_mentions_author() -> None:
    card = ReminderCard(
        reminder_id=3,
        guild_id=1,
        channel_id=5,
        user_id=11,
        text="первая строка",
        due_at=STAMP,
    )
    text = collected_text(due_view(card))
    assert "<@11>" in text
    assert "> первая строка" in text
    assert ":R>" in text


async def test_create_delivers_once_after_due(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(5))
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    service = ReminderService(_bot(sessions, guild, queue))
    try:
        verdict, card = await service.create(1, 5, 11, when="10m", text="хлеб", now=STAMP)
        assert verdict is CreateVerdict.OK
        assert card is not None
        assert await queue.fire_due() == 0
        clock.advance(10 * 60)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        assert len(channel.sent) == 1
        mentions = channel.mentions[0]
        assert mentions is not None
        assert getattr(mentions, "everyone", True) is False
        async with session_scope(sessions) as session:
            row = await session.scalar(select(Reminder).where(Reminder.id == card.reminder_id))
        assert row is not None
        assert row.delivered_at is not None
        clock.advance(10)
        assert await queue.fire_due() == 0
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_cancel_stops_delivery_and_limit_applies(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(5))
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    service = ReminderService(_bot(sessions, guild, queue))
    try:
        first, card = await service.create(1, 5, 11, when="10m", text="один", now=STAMP, limit=1)
        assert first is CreateVerdict.OK
        assert card is not None
        blocked, _none = await service.create(1, 5, 11, when="10m", text="два", now=STAMP, limit=1)
        assert blocked is CreateVerdict.LIMIT
        cancelled, found = await service.cancel(1, 11, card.reminder_id, now=STAMP)
        assert cancelled is CancelVerdict.OK
        assert found is not None
        clock.advance(10 * 60)
        assert await queue.fire_due() == 0
        assert channel.sent == []
        missing, _ = await service.cancel(1, 11, card.reminder_id, now=STAMP)
        assert missing is CancelVerdict.ALREADY
        stranger, _ = await service.cancel(1, 99, card.reminder_id, now=STAMP)
        assert stranger is CancelVerdict.MISSING
        listed = await service.pending(1, 11)
        assert listed == ()
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_restore_delivers_overdue_from_db(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(5))
    spawned, spawn = _spawn_box()
    idle = ActionQueue(spawn, clock=FakeClock())
    first = ReminderService(_bot(sessions, guild, idle))
    try:
        verdict, card = await first.create(1, 5, 11, when="10m", text="после рестарта", now=STAMP)
        assert verdict is CreateVerdict.OK
        assert card is not None
        clock = FakeClock()
        live = ActionQueue(spawn, clock=clock)
        restored = ReminderService(_bot(sessions, guild, live))
        count = await restored.restore(now=STAMP + timedelta(minutes=11))
        assert count == 1
        assert await live.fire_due() == 1
        await _await_spawned(spawned)
        assert len(channel.sent) == 1
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_missing_channel_falls_back_to_dm(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    user = FakeUser(11)
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    service = ReminderService(_bot(sessions, guild, queue, user=user))
    try:
        verdict, card = await service.create(1, 5, 11, when="10m", text="в лс", now=STAMP)
        assert verdict is CreateVerdict.OK
        assert card is not None
        clock.advance(10 * 60)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        assert user.sent
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
