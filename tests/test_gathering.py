from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Gathering, GatheringRsvp
from conductor.db.session import session_scope
from conductor.domain.discord_limits import MENTIONS_PER_MESSAGE, TEXT_DISPLAY_TOTAL
from conductor.domain.gathering import (
    FLUSH_INTERVAL,
    GatheringCard,
    GatheringSpec,
    GatheringState,
    ManageVerdict,
    Rsvp,
    RsvpStatus,
    RsvpVerdict,
    apply_checkin,
    apply_rsvp,
    can_manage,
    checkin_open,
    parse_seats,
    parse_spec,
    ping_ready,
    seated_ids,
    waitlist_ids,
)
from conductor.domain.rooms import RoomDenial, RoomOutcome
from conductor.presentation.gathering import (
    GatheringGoButton,
    GatheringStopButton,
    going_mentions,
    live_view,
)
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.services.gathering import GatheringService
from conductor.services.queue import ActionQueue

STAMP = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
STARTS = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits = 0
        self.views: list[object] = []

    async def edit(self, **kwargs: object) -> FakeMessage:
        self.edits += 1
        self.views.append(kwargs.get("view"))
        return self


class FakeChannel:
    def __init__(self, channel_id: int = 5) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.payloads: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        self.payloads.append(kwargs)
        message = FakeMessage(100 + len(self.sent))
        self.sent.append(message)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage | None:
        return self.messages.get(int(message_id))


class FakeMember:
    def __init__(self, user_id: int, *, bot: bool = False, guild: FakeGuild | None = None) -> None:
        self.id = user_id
        self.bot = bot
        self.guild = guild


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels: dict[int, FakeChannel] = {}
        self.members: dict[int, FakeMember] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self.channels[channel.id] = channel
        return channel

    def add_member(self, member: FakeMember) -> FakeMember:
        member.guild = self
        self.members[member.id] = member
        return member

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)


class FakeRooms:
    def __init__(self, channel_id: int | None = 77) -> None:
        self.channel_id = channel_id
        self.calls: list[FakeMember] = []

    async def create_for(self, member: FakeMember) -> RoomOutcome:
        self.calls.append(member)
        if self.channel_id is None:
            return RoomOutcome(denial=RoomDenial.NO_HUB)
        return RoomOutcome(channel_id=self.channel_id)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _no_sleep(_delay: float) -> None:
    return None


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


def _bot(sessions, guild: FakeGuild, queue: ActionQueue, rooms: FakeRooms | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=queue,
        scheduler=None,
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        sleep=_no_sleep,
        rooms=rooms or FakeRooms(),
    )


def _spec(*, title: str = "Клановый вечер", seats: int = 20) -> GatheringSpec:
    parsed = parse_spec(title=title, when="48h", seats=seats, now=STAMP)
    assert parsed is not None
    return parsed


def _card(*, seats: int = 20, entries: tuple[Rsvp, ...] = ()) -> GatheringCard:
    return GatheringCard(
        gathering_id=3,
        title="Клановый вечер",
        host_id=1,
        starts_at=STARTS,
        seats=seats,
        entries=entries,
        channel_id=5,
        message_id=100,
    )


def test_parse_spec_rejects_past_and_bad_seats() -> None:
    assert parse_seats(None) == 20
    assert parse_seats("0") == 0
    assert parse_seats(0) == 0
    assert parse_seats("10000") == 10000
    assert parse_seats(10001) is None
    assert parse_seats("-1") is None
    assert parse_spec(title="Вечер", when="10m", seats=8, now=STAMP) is not None
    assert parse_spec(title="Вечер", when="2026-09-04 18:00", seats=8, now=STAMP) is None
    assert parse_spec(title="", when="10m", seats=8, now=STAMP) is None


def test_overflow_goes_to_waitlist_and_promotes_oldest() -> None:
    first = apply_rsvp((), user_id=11, status=RsvpStatus.GOING, seats=2, now=STAMP)
    assert first.verdict is RsvpVerdict.SEATED
    second = apply_rsvp(first.entries, user_id=12, status=RsvpStatus.GOING, seats=2, now=STAMP + timedelta(seconds=1))
    assert second.verdict is RsvpVerdict.SEATED
    third = apply_rsvp(second.entries, user_id=13, status=RsvpStatus.GOING, seats=2, now=STAMP + timedelta(seconds=2))
    assert third.verdict is RsvpVerdict.WAITLIST
    assert seated_ids(third.entries) == (11, 12)
    assert waitlist_ids(third.entries) == (13,)
    again = apply_rsvp(third.entries, user_id=13, status=RsvpStatus.GOING, seats=2, now=STAMP + timedelta(seconds=3))
    assert again.verdict is RsvpVerdict.ALREADY
    left = apply_rsvp(third.entries, user_id=11, status=RsvpStatus.MAYBE, seats=2, now=STAMP + timedelta(seconds=4))
    assert left.verdict is RsvpVerdict.SET
    assert left.promoted_id == 13
    assert seated_ids(left.entries) == (12, 13)
    assert waitlist_ids(left.entries) == ()


def test_unlimited_seats_have_no_waitlist() -> None:
    entries: tuple[Rsvp, ...] = ()
    for index in range(5000):
        change = apply_rsvp(
            entries,
            user_id=10_000 + index,
            status=RsvpStatus.GOING,
            seats=0,
            now=STAMP + timedelta(seconds=index),
        )
        assert change.verdict is RsvpVerdict.SEATED
        entries = change.entries
    assert len(seated_ids(entries)) == 5000
    assert waitlist_ids(entries) == ()


def test_checkin_is_closed_before_the_utc_day_and_waitlist_cannot_check_in() -> None:
    seated = (Rsvp(11, RsvpStatus.GOING, created_at=STAMP),)
    early = apply_checkin(seated, user_id=11, starts_at=STARTS, now=STAMP)
    assert early.verdict is RsvpVerdict.EARLY
    assert checkin_open(STARTS, STAMP) is False
    day = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
    assert checkin_open(STARTS, day) is True
    waiting = (
        Rsvp(11, RsvpStatus.GOING, created_at=STAMP),
        Rsvp(13, RsvpStatus.GOING, waitlisted=True, created_at=STAMP + timedelta(seconds=1)),
    )
    blocked = apply_checkin(waiting, user_id=13, starts_at=STARTS, now=day)
    assert blocked.verdict is RsvpVerdict.NOT_GOING
    ok = apply_checkin(waiting, user_id=11, starts_at=STARTS, now=day)
    assert ok.verdict is RsvpVerdict.CHECKED
    already = apply_checkin(ok.entries, user_id=11, starts_at=STARTS, now=day + timedelta(hours=1))
    assert already.verdict is RsvpVerdict.ALREADY


def test_ping_ready_and_host_or_manage_key() -> None:
    assert ping_ready(None, STAMP) is True
    assert ping_ready(STAMP, STAMP + timedelta(minutes=9)) is False
    assert ping_ready(STAMP, STAMP + timedelta(minutes=10)) is True
    assert can_manage(actor_id=1, host_id=1, has_manage=False) is True
    assert can_manage(actor_id=99, host_id=1, has_manage=True) is True
    assert can_manage(actor_id=99, host_id=1, has_manage=False) is False


def test_live_card_shows_seats_waitlist_and_checkin() -> None:
    card = _card(
        seats=20,
        entries=(
            Rsvp(11, RsvpStatus.GOING, created_at=STAMP),
            Rsvp(12, RsvpStatus.GOING, waitlisted=True, created_at=STAMP),
            Rsvp(13, RsvpStatus.MAYBE, created_at=STAMP),
        ),
    )
    text = collected_text(live_view(card, now=STAMP))
    assert "Клановый вечер" in text
    assert "Когда" in text
    assert "Мест" in text
    assert "1/20" in text
    assert "лист ожидания 1" in text
    assert "**Иду:** 1" in text
    assert "чек-ин откроется" in text
    ids = [getattr(item, "custom_id", "") for item in live_view(card, now=STAMP).walk_children()]
    assert "c:ev:go:3" in ids
    assert "c:ev:stop:3" in ids
    assert "c:ev:here:3" not in ids
    assert parse_custom_id("c:ev:go:3") is not None
    assert GatheringGoButton in dynamic_items()
    assert GatheringStopButton in dynamic_items()
    open_ids = [getattr(item, "custom_id", "") for item in live_view(card, now=STARTS).walk_children()]
    assert "c:ev:here:3" in open_ids
    mentions = going_mentions((11, 12))
    assert mentions.everyone is False
    assert mentions.roles is False
    assert [user.id for user in mentions.users] == [11, 12]


def test_live_card_unlimited_and_thousand_fit_limit() -> None:
    entries = tuple(Rsvp(user_id, RsvpStatus.GOING, created_at=STAMP) for user_id in range(1, 1001))
    card = _card(seats=0, entries=entries)
    text = collected_text(live_view(card, now=STAMP))
    assert len(text) <= TEXT_DISPLAY_TOTAL
    assert "идут: 1000" in text
    assert "▰" not in text
    assert "▱" not in text
    assert "лист ожидания" not in text
    assert "и ещё" in text


async def test_stranger_cannot_cancel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GatheringService(_bot(sessions, guild, queue))
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert gathering_id is not None
        result = await service.cancel(guild, gathering_id, actor_id=99, has_manage=False)
        assert result is ManageVerdict.DENIED
        async with session_scope(sessions) as session:
            row = await session.get(Gathering, gathering_id)
        assert row is not None
        assert row.cancelled_at is None
        card = await service.open_in_channel(guild.id, channel.id)
        assert card is not None
        assert card.state is GatheringState.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_waitlist_promotes_when_a_seated_going_drops(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    service = GatheringService(_bot(sessions, guild, queue))
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=_spec(seats=2), now=STAMP)
        assert gathering_id is not None
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(11), RsvpStatus.GOING, now=STAMP) is RsvpVerdict.SEATED
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(12), RsvpStatus.GOING, now=STAMP + timedelta(seconds=1))
            is RsvpVerdict.SEATED
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(13), RsvpStatus.GOING, now=STAMP + timedelta(seconds=2))
            is RsvpVerdict.WAITLIST
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(14), RsvpStatus.MAYBE, now=STAMP + timedelta(seconds=3))
            is RsvpVerdict.SET
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(14), RsvpStatus.GOING, now=STAMP + timedelta(seconds=4))
            is RsvpVerdict.WAITLIST
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(11), RsvpStatus.CANT, now=STAMP + timedelta(seconds=5))
            is RsvpVerdict.SET
        )
        clock.advance(FLUSH_INTERVAL)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        card = await service.card(guild.id, gathering_id)
        assert card is not None
        assert set(seated_ids(card.entries)) == {12, 13}
        assert waitlist_ids(card.entries) == (14,)
        async with session_scope(sessions) as session:
            rows = (
                await session.scalars(select(GatheringRsvp).where(GatheringRsvp.gathering_id == gathering_id))
            ).all()
        by_user = {int(row.user_id): row for row in rows}
        assert by_user[13].waitlisted is False
        assert by_user[14].waitlisted is True
        assert by_user[11].status == "cant"
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_ping_mentions_only_seated_going(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GatheringService(_bot(sessions, guild, queue))
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=_spec(seats=1), now=STAMP)
        assert gathering_id is not None
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(11), RsvpStatus.GOING, now=STAMP) is RsvpVerdict.SEATED
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(12), RsvpStatus.GOING, now=STAMP + timedelta(seconds=1))
            is RsvpVerdict.WAITLIST
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(13), RsvpStatus.MAYBE, now=STAMP + timedelta(seconds=2))
            is RsvpVerdict.SET
        )
        result = await service.ping(guild, gathering_id, actor_id=1, has_manage=False, now=STAMP)
        assert result is ManageVerdict.OK
        ping = channel.payloads[-1]
        mentions = ping["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.roles is False
        assert [user.id for user in mentions.users] == [11]
        text = collected_text(ping["view"])
        assert "@everyone" not in text
        assert "@here" not in text
        assert "<@11>" in text
        assert "<@12>" not in text
        again = await service.ping(guild, gathering_id, actor_id=1, has_manage=False, now=STAMP + timedelta(minutes=9))
        assert again is ManageVerdict.COOLDOWN
        empty = await service.start(
            guild,
            guild.add_channel(FakeChannel(channel_id=6)),
            actor_id=1,
            spec=_spec(seats=2),
            now=STAMP,
        )
        assert empty is not None
        assert await service.ping(guild, empty, actor_id=1, has_manage=False, now=STAMP) is ManageVerdict.EMPTY
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_ping_thousand_going_uses_batches_and_keeps_cooldown(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GatheringService(_bot(sessions, guild, queue))
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=_spec(seats=0), now=STAMP)
        assert gathering_id is not None
        async with session_scope(sessions) as session:
            session.add_all(
                [
                    GatheringRsvp(
                        gathering_id=int(gathering_id),
                        user_id=2_000 + index,
                        status=RsvpStatus.GOING.value,
                        waitlisted=False,
                        created_at=STAMP,
                        updated_at=STAMP,
                    )
                    for index in range(1000)
                ]
            )
        result = await service.ping(guild, gathering_id, actor_id=1, has_manage=False, now=STAMP)
        assert result is ManageVerdict.OK
        ping_payloads = channel.payloads[1:]
        assert len(ping_payloads) == 11
        header = collected_text(ping_payloads[0]["view"])  # type: ignore[arg-type]
        assert "Организатор зовёт идущих:" in header
        sizes: list[int] = []
        for payload in ping_payloads[1:]:
            mentions = payload["allowed_mentions"]
            users = list(mentions.users)
            assert mentions.everyone is False
            assert len(users) <= MENTIONS_PER_MESSAGE
            sizes.append(len(users))
        assert sizes == [100] * 10
        again = await service.ping(guild, gathering_id, actor_id=1, has_manage=False, now=STAMP + timedelta(minutes=9))
        assert again is ManageVerdict.COOLDOWN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_open_voice_creates_room_on_the_host(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    rooms = FakeRooms(channel_id=77)
    service = GatheringService(_bot(sessions, guild, queue, rooms))
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert gathering_id is not None
        verdict, voice_id = await service.open_voice(guild, gathering_id, actor_id=99, has_manage=True)
        assert verdict is ManageVerdict.OK
        assert voice_id == 77
        assert rooms.calls == [host]
        async with session_scope(sessions) as session:
            row = await session.get(Gathering, gathering_id)
        assert row is not None
        assert row.voice_channel_id == 77
        again, existing = await service.open_voice(guild, gathering_id, actor_id=1, has_manage=False)
        assert again is ManageVerdict.ALREADY
        assert existing == 77
        rooms.channel_id = None
        missing_id = await service.start(
            guild,
            guild.add_channel(FakeChannel(channel_id=8)),
            actor_id=1,
            spec=_spec(),
            now=STAMP,
        )
        assert missing_id is not None
        no_hub, none_id = await service.open_voice(guild, missing_id, actor_id=1, has_manage=False)
        assert no_hub is ManageVerdict.NO_HUB
        assert none_id is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_checkin_on_event_day_for_seated_going(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GatheringService(_bot(sessions, guild, queue))
    spec = GatheringSpec(title="Клановый вечер", starts_at=STARTS, seats=1)
    try:
        gathering_id = await service.start(guild, channel, actor_id=1, spec=spec, now=STAMP)
        assert gathering_id is not None
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(11), RsvpStatus.GOING, now=STAMP) is RsvpVerdict.SEATED
        )
        assert (
            await service.rsvp(guild, gathering_id, FakeMember(12), RsvpStatus.GOING, now=STAMP + timedelta(seconds=1))
            is RsvpVerdict.WAITLIST
        )
        assert await service.checkin(guild, gathering_id, FakeMember(11), now=STAMP) is RsvpVerdict.EARLY
        day = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
        assert await service.checkin(guild, gathering_id, FakeMember(12), now=day) is RsvpVerdict.NOT_GOING
        assert await service.checkin(guild, gathering_id, FakeMember(11), now=day) is RsvpVerdict.CHECKED
        async with session_scope(sessions) as session:
            row = await session.scalar(
                select(GatheringRsvp).where(GatheringRsvp.gathering_id == gathering_id, GatheringRsvp.user_id == 11)
            )
        assert row is not None
        assert row.checked_in_at is not None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_restore_reschedules_flush_from_database(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    message = FakeMessage(100)
    channel.messages[100] = message
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    service = GatheringService(_bot(sessions, guild, queue))
    try:
        async with session_scope(sessions) as session:
            session.add(
                Gathering(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    message_id=100,
                    owner_id=1,
                    title="Клановый вечер",
                    starts_at=STAMP + timedelta(hours=2),
                    created_at=STAMP,
                )
            )
            session.add(
                Gathering(
                    guild_id=guild.id,
                    channel_id=6,
                    message_id=101,
                    owner_id=1,
                    title="Отмена",
                    starts_at=STAMP + timedelta(hours=3),
                    created_at=STAMP,
                    cancelled_at=STAMP,
                )
            )
            session.add(
                Gathering(
                    guild_id=guild.id,
                    channel_id=7,
                    message_id=None,
                    owner_id=1,
                    title="Черновик",
                    starts_at=STAMP + timedelta(hours=4),
                    created_at=STAMP,
                )
            )
        count = await service.restore(now=STAMP)
        assert count == 1
        assert queue.later_count == 2
        clock.advance(FLUSH_INTERVAL)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        assert message.edits == 1
        assert queue.later_count == 1
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
