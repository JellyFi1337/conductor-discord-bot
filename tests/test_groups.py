from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GroupMember, GuildRank, PrivateGroup, Reputation, Ticket, VoiceRoom
from conductor.db.session import session_scope
from conductor.domain.access import Rank
from conductor.domain.groups import (
    DEFAULT_SILENCE,
    MAX_MEMBERS,
    STAFF_RANKS,
    ArchiveReason,
    CreateVerdict,
    GroupCard,
    GroupState,
    ManageVerdict,
    can_archive,
    can_invite,
    can_leave,
    group_channel_name,
    invite_verdict,
    parse_invitee,
    parse_spec,
    silence_due,
)
from conductor.presentation.groups import GroupInviteButton, GroupStopButton, header_view
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.services.cache import CacheHub
from conductor.services.groups import GroupService
from conductor.services.queue import ActionQueue

STAMP = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id


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
    def __init__(self, channel_id: int, *, kind: str = "text") -> None:
        self.id = channel_id
        self.kind = kind
        self.name = ""
        self.guild: FakeGuild | None = None
        self.category: FakeChannel | None = None
        self.channels: list[FakeChannel] = []
        self.topic: str | None = None
        self.last_message_id: int | None = None
        self.type = discord.ChannelType.category if kind == "category" else discord.ChannelType.text
        if kind == "voice":
            self.type = discord.ChannelType.voice
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.overwrites: dict[object, object] = {}
        self.deleted = False
        self.grants: list[tuple[object, object]] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        message = FakeMessage(100 + len(self.sent))
        self.sent.append(message)
        self.messages[message.id] = message
        self.last_message_id = message.id
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage | None:
        return self.messages.get(int(message_id))

    async def set_permissions(self, target: object, **kwargs: object) -> None:
        self.grants.append((target, kwargs.get("overwrite")))
        overwrite = kwargs.get("overwrite")
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite

    async def edit(self, **kwargs: object) -> FakeChannel:
        if "category" in kwargs:
            previous = self.category
            if previous is not None and self in previous.channels:
                previous.channels.remove(self)
            category = kwargs.get("category")
            self.category = category if isinstance(category, FakeChannel) else None
            if self.category is not None and self not in self.category.channels:
                self.category.channels.append(self)
        if "overwrites" in kwargs:
            self.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        if "topic" in kwargs:
            self.topic = None if kwargs.get("topic") is None else str(kwargs.get("topic"))
        return self

    async def history(self, *, limit: int = 100):
        for message in list(self.sent)[: int(limit)]:
            yield message

    async def delete(self, **_kwargs: object) -> None:
        self.deleted = True


class FakeMember:
    def __init__(self, user_id: int, *, bot: bool = False, guild: FakeGuild | None = None) -> None:
        self.id = user_id
        self.bot = bot
        self.guild = guild
        self.roles: tuple[object, ...] = ()
        self.dms: list[object] = []
        self.name = str(user_id)
        self.display_name = str(user_id)
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.closed_dm = False

    async def send(self, **kwargs: object) -> FakeMessage:
        if self.closed_dm:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "blocked")
        message = FakeMessage(1)
        self.dms.append(kwargs)
        return message

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.name = "тест"
        self.owner_id = 0
        self.channels: dict[int, FakeChannel] = {}
        self.members: dict[int, FakeMember] = {}
        self.roles: dict[int, FakeRole] = {}
        self.default_role = FakeRole(0)
        self.me = FakeMember(99, bot=True, guild=self)
        self.members[99] = self.me
        self._next = 200
        self.created: list[tuple[str, FakeChannel]] = []

    def add_member(self, member: FakeMember) -> FakeMember:
        member.guild = self
        self.members[member.id] = member
        return member

    def add_role(self, role: FakeRole) -> FakeRole:
        self.roles[role.id] = role
        return role

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.roles.get(role_id)

    def _alloc(self) -> int:
        self._next += 1
        return self._next

    async def create_category(self, name: str, **kwargs: object) -> FakeChannel:
        channel = FakeChannel(self._alloc(), kind="category")
        channel.name = str(name)
        channel.guild = self
        channel.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        self.channels[channel.id] = channel
        self.created.append(("category", channel))
        return channel

    async def create_text_channel(self, name: str, **kwargs: object) -> FakeChannel:
        channel = FakeChannel(self._alloc(), kind="text")
        channel.name = str(name)
        channel.guild = self
        channel.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        category = kwargs.get("category")
        if isinstance(category, FakeChannel):
            channel.category = category
            category.channels.append(channel)
        self.channels[channel.id] = channel
        self.created.append(("text", channel))
        return channel

    async def create_voice_channel(self, name: str, **kwargs: object) -> FakeChannel:
        channel = FakeChannel(self._alloc(), kind="voice")
        channel.name = str(name)
        channel.guild = self
        channel.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        category = kwargs.get("category")
        if isinstance(category, FakeChannel):
            channel.category = category
            category.channels.append(channel)
        self.channels[channel.id] = channel
        self.created.append(("voice", channel))
        return channel


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _seed_score(sessions, guild_id: int, user_id: int, score: int = 51) -> None:
    async with session_scope(sessions) as session:
        row = await session.scalar(
            select(Reputation).where(Reputation.guild_id == int(guild_id), Reputation.user_id == int(user_id))
        )
        if row is None:
            session.add(Reputation(guild_id=int(guild_id), user_id=int(user_id), score=int(score)))
            return
        row.score = int(score)


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


def _bot(sessions, guild: FakeGuild, queue: ActionQueue) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=queue,
        caches=CacheHub(),
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        guard=None,
        outbound=None,
        roles=None,
    )


def test_parse_spec_keeps_owner_and_mentions() -> None:
    spec = parse_spec(title="клановый стол", members="<@2> <@3>", owner_id=1)
    assert spec is not None
    assert spec.member_ids == (1, 2, 3)
    assert spec.with_voice is False
    assert parse_spec(title="", members=None, owner_id=1) is None
    assert parse_invitee("<@42>") == 42
    assert parse_invitee("42") == 42
    assert group_channel_name("Клановый стол") == "клановый-стол"
    assert Rank.WATCHER not in STAFF_RANKS
    assert Rank.CHIEF in STAFF_RANKS and Rank.SPECIAL in STAFF_RANKS


def test_invite_leave_archive_rules() -> None:
    assert can_invite(actor_id=1, owner_id=1) is True
    assert can_invite(actor_id=2, owner_id=1) is False
    assert can_archive(actor_id=1, owner_id=1, is_staff=False) is True
    assert can_archive(actor_id=9, owner_id=1, is_staff=True) is True
    assert can_archive(actor_id=9, owner_id=1, is_staff=False) is False
    assert can_leave(actor_id=1, owner_id=1, member_ids=(1, 2)) is ManageVerdict.OWNER
    assert can_leave(actor_id=9, owner_id=1, member_ids=(1, 2)) is ManageVerdict.STRANGER
    assert can_leave(actor_id=2, owner_id=1, member_ids=(1, 2)) is ManageVerdict.OK
    assert invite_verdict(user_id=3, member_ids=(1, 2), is_bot=True) is ManageVerdict.BOT
    assert invite_verdict(user_id=2, member_ids=(1, 2), is_bot=False) is ManageVerdict.ALREADY
    full = tuple(range(MAX_MEMBERS))
    assert invite_verdict(user_id=99, member_ids=full, is_bot=False) is ManageVerdict.FULL
    assert silence_due(STAMP, STAMP + DEFAULT_SILENCE - timedelta(seconds=1)) is False
    assert silence_due(STAMP, STAMP + DEFAULT_SILENCE) is True


def test_header_is_not_a_ticket_or_voice_panel() -> None:
    card = GroupCard(group_id=4, title="стол", owner_id=1, member_ids=(1, 2), channel_id=8, last_activity_at=STAMP)
    text = collected_text(header_view(card))
    assert "стол" in text
    assert "владелец <@1>" in text
    assert "архивация" in text
    ids = [getattr(item, "custom_id", "") for item in header_view(card).walk_children()]
    assert "c:gr:inv:4" in ids
    assert "c:gr:leave:4" in ids
    assert "c:gr:stop:4" in ids
    assert parse_custom_id("c:gr:inv:4") is not None
    assert GroupInviteButton in dynamic_items()
    assert GroupStopButton in dynamic_items()


async def test_channel_is_private_and_not_a_ticket_or_room(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr("conductor.services.groups.note_success", lambda *args, **kwargs: calls.append(args))
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guest = guild.add_member(FakeMember(2))
    watcher = guild.add_role(FakeRole(12))
    chief = guild.add_role(FakeRole(10))
    special = guild.add_role(FakeRole(11))
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GroupService(_bot(sessions, guild, queue))
    try:
        async with session_scope(sessions) as session:
            session.add(GuildRank(guild_id=1, rank="watcher", role_id=12))
            session.add(GuildRank(guild_id=1, rank="chief", role_id=10))
            session.add(GuildRank(guild_id=1, rank="special", role_id=11))
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1, with_voice=True)
        assert spec is not None
        verdict, channel_id = await service.create(guild, host, spec, now=STAMP)
        assert verdict is CreateVerdict.OK
        assert channel_id is not None
        texts = [item for kind, item in guild.created if kind == "text"]
        voices = [item for kind, item in guild.created if kind == "voice"]
        assert len(texts) == 1 and len(voices) == 1
        assert calls
        channel = texts[0]
        assert channel.overwrites[guild.default_role].view_channel is False
        assert channel.overwrites[host].view_channel is True
        assert guest not in channel.overwrites
        assert channel.overwrites[chief].view_channel is True
        assert channel.overwrites[special].view_channel is True
        assert watcher not in channel.overwrites
        card = await service.group_by_channel(guild.id, channel.id)
        assert card is not None
        assert card.owner_id == 1
        assert card.voice_channel_id == voices[0].id
        async with session_scope(sessions) as session:
            assert await session.scalar(select(Ticket).where(Ticket.channel_id == channel.id)) is None
            assert await session.scalar(select(VoiceRoom).where(VoiceRoom.channel_id == channel.id)) is None
            assert await session.scalar(select(VoiceRoom).where(VoiceRoom.channel_id == voices[0].id)) is None
            row = await session.scalar(select(PrivateGroup).where(PrivateGroup.channel_id == channel.id))
        assert row is not None
        assert row.owner_id == 1
        restored = GroupService(_bot(sessions, guild, queue))
        again = await restored.card(guild.id, card.group_id)
        assert again is not None
        assert again.owner_id == 1
        assert again.state is GroupState.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_stranger_cannot_archive_owner_cannot_leave(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = GroupService(_bot(sessions, guild, queue))
    spec = parse_spec(title="стол", members="<@2>", owner_id=1)
    assert spec is not None
    try:
        await _seed_score(sessions, guild.id, host.id)
        verdict, channel_id = await service.create(guild, host, spec, now=STAMP)
        assert verdict is CreateVerdict.OK
        card = await service.group_by_channel(guild.id, int(channel_id or 0))
        assert card is not None
        joined = await service.accept_invite(guild, card.group_id, actor_id=2, now=STAMP)
        assert joined is ManageVerdict.OK
        denied, denied_teardown = await service.archive(
            guild, card.group_id, actor_id=9, reason=ArchiveReason.OWNER
        )
        assert denied is ManageVerdict.DENIED
        assert denied_teardown is None
        owner_leave = can_leave(actor_id=1, owner_id=1, member_ids=(1, 2))
        assert owner_leave is ManageVerdict.OWNER
        left = await service.leave(guild, card.group_id, actor_id=2)
        assert left is ManageVerdict.OK
        async with session_scope(sessions) as session:
            row = await session.get(PrivateGroup, card.group_id)
            members = (await session.scalars(select(GroupMember).where(GroupMember.group_id == card.group_id))).all()
        assert row is not None
        assert row.archived_at is None
        assert {int(item.user_id) for item in members} == {1}
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        assert teardown is not None
        assert guild.channels[card.channel_id].deleted is False
        await teardown()
        gone = await service.group_by_channel(guild.id, card.channel_id)
        assert gone is not None
        assert gone.state is GroupState.ARCHIVED
        assert guild.channels[card.channel_id].deleted is False
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_silence_archives_from_the_scheduler(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    bot = _bot(sessions, guild, queue)
    service = GroupService(bot)
    spec = parse_spec(title="стол", members=None, owner_id=1)
    assert spec is not None
    try:
        await _seed_score(sessions, guild.id, host.id)
        verdict, channel_id = await service.create(guild, host, spec, now=STAMP)
        assert verdict is CreateVerdict.OK
        clock.advance(DEFAULT_SILENCE.total_seconds() - 1)
        assert await queue.fire_due() == 1
        clock.advance(1)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        card = await service.group_by_channel(guild.id, int(channel_id or 0))
        assert card is not None
        assert card.state is GroupState.ARCHIVED
        revived = GroupService(bot)
        count = await revived.restore(now=STAMP + timedelta(days=1))
        assert count == 0
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
