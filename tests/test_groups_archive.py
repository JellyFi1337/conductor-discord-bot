from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GroupArchiveShelf, GroupHub, GroupMember, GuildRank, PrivateGroup
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.groups import (
    DEFAULT_ARCHIVE_DAYS,
    DEFAULT_SILENCE,
    ArchiveReason,
    CreateVerdict,
    GroupState,
    ManageVerdict,
    parse_spec,
)
from conductor.domain.seed import LAYOUT
from conductor.presentation.groups import GroupYesButton
from conductor.services.groups import GroupService
from conductor.services.queue import ActionQueue
from test_groups import (
    STAMP,
    FakeChannel,
    FakeClock,
    FakeGuild,
    FakeMember,
    FakeRole,
    _await_spawned,
    _bot,
    _seed_score,
    _spawn_box,
    _url,
)


async def _make_group(
    service: GroupService, guild: FakeGuild, host: FakeMember, *, voice: bool = False, title: str = "стол"
):
    spec = parse_spec(title=title, members="<@2>", owner_id=1, with_voice=voice)
    assert spec is not None
    if guild.get_member(2) is None:
        guild.add_member(FakeMember(2))
    guild.owner_id = host.id
    await _seed_score(service._bot.sessions, guild.id, host.id)
    verdict, channel_id = await service.create(guild, host, spec, now=STAMP)
    assert verdict is CreateVerdict.OK
    card = await service.group_by_channel(guild.id, int(channel_id or 0))
    assert card is not None
    joined = await service.accept_invite(guild, card.group_id, actor_id=2, now=STAMP)
    assert joined is ManageVerdict.OK
    card = await service.group_by_channel(guild.id, int(channel_id or 0))
    assert card is not None
    return card


class _JournalResponse:
    def __init__(self) -> None:
        self._done = False
        self.calls: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self.calls.append("defer")
        self._done = True

    async def send_message(self, **_kwargs: object) -> None:
        self.calls.append("send_message")
        self._done = True


class _JournalFollowup:
    async def send(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=1)


async def test_owner_archive_moves_channel_and_sends_dm(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guest = guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        card = await _make_group(service, guild, host)
        answered: list[str] = []
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        answered.append("respond")
        assert ok is ManageVerdict.OK
        assert teardown is not None
        text = guild.channels[card.channel_id]
        assert text.deleted is False
        await teardown()
        answered.append("teardown")
        assert answered == ["respond", "teardown"]
        assert text.deleted is False
        assert text.category is not None
        assert text.category.name == LAYOUT.cat_group_archive
        assert host not in text.overwrites
        assert guest not in text.overwrites
        everyone = text.overwrites[guild.default_role]
        assert everyone.view_channel is False
        bot_rule = text.overwrites[guild.me]
        assert bot_rule.send_messages is True
        assert bot_rule.manage_channels is True
        again = await service.card(guild.id, card.group_id)
        assert again is not None
        assert again.state is GroupState.ARCHIVED
        assert again.archive_reason is ArchiveReason.OWNER
        assert host.dms and guest.dms
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_forged_custom_id_cannot_archive(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    stranger = guild.add_member(FakeMember(9))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    service = GroupService(bot)
    bot.groups = service
    bot.is_developer = lambda _uid: False
    try:
        card = await _make_group(service, guild, host)
        denied, teardown = await service.archive(
            guild, card.group_id, actor_id=9, reason=ArchiveReason.STAFF, now=STAMP
        )
        assert denied is ManageVerdict.DENIED
        assert teardown is None
        source = inspect.getsource(GroupYesButton.on_action)
        assert "has_manage=True" not in source
        interaction = SimpleNamespace(
            id=1,
            response=_JournalResponse(),
            followup=_JournalFollowup(),
            client=bot,
            guild=guild,
            user=SimpleNamespace(id=stranger.id, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
            command=None,
            data={"custom_id": f"c:gr:yes:{card.group_id}"},
            created_at=datetime.now(UTC),
            type=SimpleNamespace(name="component"),
            namespace=None,
            channel=guild.channels[card.channel_id],
        )
        button = GroupYesButton(card.group_id)
        await button.callback(interaction)  # type: ignore[arg-type]
        live = await service.card(guild.id, card.group_id)
        assert live is not None
        assert live.state is GroupState.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_empty_voice_deleted_voice_with_messages_moved(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        empty = await _make_group(service, guild, host, voice=True, title="пусто")
        assert empty.voice_channel_id is not None
        empty_voice = guild.channels[empty.voice_channel_id]
        ok, teardown = await service.archive(
            guild, empty.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        assert empty_voice.deleted is True
        kept = await _make_group(service, guild, host, voice=True, title="живой")
        assert kept.voice_channel_id is not None
        voice = guild.channels[kept.voice_channel_id]
        await voice.send()
        ok, teardown = await service.archive(
            guild, kept.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        assert voice.deleted is False
        assert voice.category is not None
        assert voice.category.name == LAYOUT.cat_group_archive
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_full_shelf_creates_same_name_category(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        first = await _make_group(service, guild, host, title="одна")
        ok, teardown = await service.archive(
            guild, first.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        shelf = guild.channels[first.channel_id].category
        assert shelf is not None
        shelf.channels = [FakeChannel(900 + index) for index in range(50)]
        second = await _make_group(service, guild, host, title="две")
        ok, teardown = await service.archive(
            guild, second.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        extra = guild.channels[second.channel_id].category
        assert extra is not None
        assert extra.id != shelf.id
        assert extra.name == shelf.name
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(GroupArchiveShelf))).all()
        assert {int(row.category_id) for row in rows} == {extra.id}
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_purge_deletes_and_logs_stranger_denied(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    feed = SimpleNamespace(posts=[])

    async def post(_guild_id: int, **kwargs: object) -> int:
        feed.posts.append(kwargs)
        return 1

    bot.staff_feed = SimpleNamespace(post=post)
    service = GroupService(bot)
    try:
        card = await _make_group(service, guild, host)
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert teardown is not None
        await teardown()
        denied, none = await service.purge(guild, card.group_id, actor_id=9)
        assert denied is ManageVerdict.DENIED
        assert none is None
        special = guild.add_role(FakeRole(11))
        actor = guild.add_member(FakeMember(7))
        actor.roles = (special,)
        async with session_scope(sessions) as session:
            session.add(GuildRank(guild_id=1, rank="special", role_id=11))
        bot.caches.permissions.clear()
        ok, teardown = await service.purge(guild, card.group_id, actor_id=7)
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        assert guild.channels[card.channel_id].deleted is True
        async with session_scope(sessions) as session:
            assert await session.get(PrivateGroup, card.group_id) is None
            members = (await session.scalars(select(GroupMember).where(GroupMember.group_id == card.group_id))).all()
            assert members == []
        assert feed.posts
        assert "стол" in str(feed.posts[0].get("details"))
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_sweep_purge_removes_expired(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    bot.get_guild = lambda gid: guild if gid == guild.id else None
    service = GroupService(bot)
    try:
        card = await _make_group(service, guild, host)
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert teardown is not None
        await teardown()
        later = STAMP + timedelta(days=DEFAULT_ARCHIVE_DAYS + 1)
        count = await service.sweep_purge(now=later)
        assert count == 1
        async with session_scope(sessions) as session:
            assert await session.get(PrivateGroup, card.group_id) is None
        assert guild.channels[card.channel_id].deleted is True
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_sweep_silence_archives_week_not_six_days(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    bot.get_guild = lambda gid: guild if gid == guild.id else None
    service = GroupService(bot)
    try:
        quiet = await _make_group(service, guild, host, title="тишина")
        live = await _make_group(service, guild, host, title="живая")
        async with session_scope(sessions) as session:
            row = await session.get(PrivateGroup, live.group_id)
            assert row is not None
            row.last_activity_at = STAMP + timedelta(days=2)
        six = await service.sweep_silence(now=STAMP + timedelta(days=6))
        assert six == 0
        eight = await service.sweep_silence(now=STAMP + timedelta(days=8))
        assert eight == 1
        quiet_card = await service.card(guild.id, quiet.group_id)
        live_card = await service.card(guild.id, live.group_id)
        assert quiet_card is not None and quiet_card.state is GroupState.ARCHIVED
        assert live_card is not None and live_card.state is GroupState.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_silence_warning_once_and_resets_on_activity(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    bot.get_guild = lambda gid: guild if gid == guild.id else None
    service = GroupService(bot)
    try:
        card = await _make_group(service, guild, host)
        warn_at = STAMP + DEFAULT_SILENCE - timedelta(days=1)
        first = await service.sweep_warnings(now=warn_at)
        assert first == 1
        channel = guild.channels[card.channel_id]
        assert len(channel.sent) >= 2
        second = await service.sweep_warnings(now=warn_at + timedelta(hours=1))
        assert second == 0
        await service.touch_activity(guild.id, card.channel_id, now=warn_at + timedelta(hours=2))
        async with session_scope(sessions) as session:
            row = await session.get(PrivateGroup, card.group_id)
            assert row is not None
            assert row.silence_warned_at is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_touch_activity_throttles_writes(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        card = await _make_group(service, guild, host)
        for index in range(100):
            await service.touch_activity(guild.id, card.channel_id, now=STAMP + timedelta(seconds=index))
        async with session_scope(sessions) as session:
            row = await session.get(PrivateGroup, card.group_id)
            assert row is not None
            assert as_utc(row.last_activity_at) == STAMP
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_restore_from_archive_returns_member_overwrites(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guest = guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    bot = _bot(sessions, guild, ActionQueue(spawn, clock=FakeClock()))
    service = GroupService(bot)
    try:
        card = await _make_group(service, guild, host)
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert teardown is not None
        await teardown()
        special = guild.add_role(FakeRole(11))
        actor = guild.add_member(FakeMember(7))
        actor.roles = (special,)
        async with session_scope(sessions) as session:
            session.add(GuildRank(guild_id=1, rank="special", role_id=11))
        bot.caches.permissions.clear()
        denied, none = await service.restore_from_archive(guild, card.group_id, actor_id=9)
        assert denied is ManageVerdict.DENIED
        assert none is None
        ok, teardown = await service.restore_from_archive(guild, card.group_id, actor_id=7, now=STAMP)
        assert ok is ManageVerdict.OK
        assert teardown is not None
        await teardown()
        text = guild.channels[card.channel_id]
        assert text.overwrites[host].view_channel is True
        assert text.overwrites[guest].view_channel is True
        live = await service.card(guild.id, card.group_id)
        assert live is not None
        assert live.state is GroupState.OPEN
        async with session_scope(sessions) as session:
            row = await session.get(PrivateGroup, card.group_id)
            assert row is not None
            assert row.archived_at is None
            hub = await session.get(GroupHub, guild.id)
            assert hub is not None
            assert hub.archive_days == DEFAULT_ARCHIVE_DAYS
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
