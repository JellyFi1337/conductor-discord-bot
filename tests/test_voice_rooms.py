from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.session import session_scope
from conductor.domain.rooms import (
    CLAIM_AFTER,
    EMPTY_DELETE_DELAY,
    NAME_TEMPLATE_DEFAULT,
    ClaimVerdict,
    HubJoin,
    HubPreset,
    HubSettings,
    JoinAsk,
    MergeAsk,
    RoomDenial,
    RoomOutcome,
    StoredRoom,
    ask_merge,
    ask_to_join,
    check_room_name,
    claim_verdict,
    clone_uses_template,
    dump_hub_settings,
    hub_join_action,
    kicked_may_enter,
    owner_overwrite,
    parse_hub_settings,
    prepare_room_name,
    reconcile_rooms,
    render_room_name,
    room_channel_name,
    room_for_owner,
)
from conductor.services.queue import ActionQueue
from conductor.services.voice_rooms import RoomJanitor, VoiceRoomService, humans_in


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _room(channel_id: int, owner_id: int, *, hub_id: int = 1) -> StoredRoom:
    return StoredRoom(guild_id=7, channel_id=channel_id, owner_id=owner_id, hub_id=hub_id)


def test_hub_join_moves_when_the_owner_already_has_a_room() -> None:
    existing = _room(20, 5)
    assert hub_join_action(in_hub=True, is_bot=False, existing=None) is HubJoin.CREATE
    assert hub_join_action(in_hub=True, is_bot=False, existing=existing) is HubJoin.MOVE
    assert hub_join_action(in_hub=True, is_bot=True, existing=None) is HubJoin.IGNORE
    assert hub_join_action(in_hub=False, is_bot=False, existing=None) is HubJoin.IGNORE


def test_second_hub_entry_does_not_plan_a_second_room() -> None:
    rooms = (_room(20, 5),)
    assert room_for_owner(rooms, 5) is rooms[0]
    assert room_for_owner(rooms, 9) is None
    assert hub_join_action(in_hub=True, is_bot=False, existing=room_for_owner(rooms, 5)) is HubJoin.MOVE


def test_reconcile_deletes_empty_keeps_live_and_never_touches_hub() -> None:
    rooms = (_room(10, 1, hub_id=1), _room(11, 2, hub_id=1), _room(12, 3, hub_id=1), _room(1, 4, hub_id=1))
    plan = reconcile_rooms(
        rooms,
        hub_ids=(1,),
        existing_channel_ids=(1, 10, 11),
        occupied={1: 2, 10: 1, 11: 0},
    )
    assert plan.delete == (11,)
    assert plan.drop == (12,)
    assert plan.keep == (10,)
    assert 1 not in plan.delete
    assert 1 not in plan.drop


def test_reconcile_after_restart_is_idempotent() -> None:
    rooms = [_room(10, 1), _room(11, 2), _room(12, 3)]
    channels = {10: 1, 11: 0, 99: 4}

    def run() -> None:
        live = set(channels)
        plan = reconcile_rooms(
            rooms,
            hub_ids=(1,),
            existing_channel_ids=live,
            occupied=channels,
        )
        gone = set(plan.delete) | set(plan.drop)
        rooms[:] = [room for room in rooms if room.channel_id not in gone]
        for channel_id in plan.delete:
            channels.pop(channel_id, None)

    run()
    assert {room.channel_id for room in rooms} == {10}
    snapshot = list(rooms)
    channel_snapshot = dict(channels)
    run()
    assert rooms == snapshot
    assert channels == channel_snapshot


def test_humans_in_skips_bots() -> None:
    channel = SimpleNamespace(
        members=(SimpleNamespace(bot=False), SimpleNamespace(bot=True), SimpleNamespace(bot=False))
    )
    assert humans_in(channel) == 2
    assert humans_in(None) == 0


def test_room_name_fits_discord_limit() -> None:
    assert room_channel_name("  Ann  ") == "Ann"
    assert room_channel_name("") == "комната"
    assert len(room_channel_name("я" * 200)) == 100


@pytest.mark.asyncio
async def test_two_empty_exits_do_not_delete_twice() -> None:
    clock = FakeClock()
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    queue = ActionQueue(spawn, clock=clock)
    sweeps: list[tuple[int, int]] = []

    async def sweep(guild_id: int, channel_id: int) -> None:
        sweeps.append((guild_id, channel_id))

    janitor = RoomJanitor(queue, sweep, delay=EMPTY_DELETE_DELAY)
    janitor.mark_empty(7, 11)
    janitor.mark_empty(7, 11)
    clock.advance(EMPTY_DELETE_DELAY)
    assert await queue.fire_due() == 1
    if spawned:
        await asyncio.gather(*spawned)
    assert sweeps == [(7, 11)]


@pytest.mark.asyncio
async def test_return_before_deadline_cancels_delete() -> None:
    clock = FakeClock()
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    queue = ActionQueue(spawn, clock=clock)
    fired = 0

    async def sweep(_guild_id: int, _channel_id: int) -> None:
        nonlocal fired
        fired += 1

    janitor = RoomJanitor(queue, sweep, delay=EMPTY_DELETE_DELAY)
    janitor.mark_empty(7, 11)
    clock.advance(10)
    assert janitor.mark_occupied(7, 11) is True
    clock.advance(EMPTY_DELETE_DELAY)
    assert await queue.fire_due() == 0
    if spawned:
        await asyncio.gather(*spawned)
    assert fired == 0


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def test_owner_cannot_have_two_rooms(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(sessions=sessions, queue=ActionQueue(asyncio.create_task), caches=SimpleNamespace())
    service = VoiceRoomService(bot)
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
            hub = await service.hub_for(session, 1)
            assert hub is not None
            assert hub.channel_id == 9
        assert await service._insert_room(1, channel_id=10, owner_id=5, hub_id=9) is not None
        assert await service._insert_room(1, channel_id=11, owner_id=5, hub_id=9) is None
        async with session_scope(sessions) as session:
            rooms = await service.rooms_for(session, 1)
        assert len(rooms) == 1
        assert rooms[0].channel_id == 10
    finally:
        await engine.dispose()


def test_name_filter_rejects_invite_and_ads() -> None:
    invite = check_room_name("зайди discord.gg/nitro")
    assert invite.ok is False
    assert invite.reason == "invite"
    ad = check_room_name("бесплатный nitro https://example.com")
    assert ad.ok is False
    assert ad.reason in {"ad", "invite"}
    ok = check_room_name("катка ранкед", prefix="🎮")
    assert ok.ok is True
    assert ok.name.startswith("🎮")


def test_claim_before_and_after_threshold() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    common = {
        "actor_id": 2,
        "owner_id": 1,
        "actor_has_room": False,
        "absent_since": start,
    }
    assert claim_verdict(**common, owner_in_channel=False, now=start + timedelta(minutes=1)) is ClaimVerdict.TOO_SOON
    assert claim_verdict(**common, owner_in_channel=False, now=start + CLAIM_AFTER) is ClaimVerdict.OK
    assert claim_verdict(**common, owner_in_channel=True, now=start + timedelta(hours=1)) is ClaimVerdict.OWNER_HERE


def test_owner_overwrite_has_channel_rights_but_not_manage_permissions() -> None:
    flags = owner_overwrite()
    assert flags == {
        "view_channel": True,
        "connect": True,
        "speak": True,
        "stream": True,
        "send_messages": True,
        "read_message_history": True,
        "manage_messages": True,
        "manage_channels": True,
        "move_members": True,
        "mute_members": True,
        "deafen_members": True,
        "priority_speaker": True,
    }
    assert "manage_permissions" not in flags
    assert "manage_roles" not in flags


async def test_staff_take_is_logged_and_lock_does_not_steal(tmp_path: Path) -> None:
    from conductor.db.models import VoiceRoom
    from conductor.presentation.copy import VOICE_STAFF_LOG
    from conductor.presentation.layout import collected_text

    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    logs: list[dict[str, object]] = []
    bot = SimpleNamespace(
        sessions=sessions,
        queue=ActionQueue(asyncio.create_task),
        caches=SimpleNamespace(room_owner=SimpleNamespace(set=lambda *_a, **_k: None)),
        log_pump=SimpleNamespace(submit=lambda _cid, **kwargs: logs.append(kwargs)),
        config_by_guild=SimpleNamespace(get=lambda _gid: SimpleNamespace(admin_log_channel_id=99)),
        outbound=None,
        guard=None,
    )
    service = VoiceRoomService(bot)
    try:
        stored = await service._insert_room(1, channel_id=10, owner_id=5, hub_id=9)
        assert stored is not None
        async with session_scope(sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            row.locked = True
        async with session_scope(sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            assert row.owner_id == 5
            assert row.locked is True
        guild = SimpleNamespace(id=1, get_channel=lambda *_: None)
        result = await service.take_as_staff(guild, stored.id, SimpleNamespace(id=9))  # type: ignore[arg-type]
        assert result is ClaimVerdict.OK
        async with session_scope(sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            assert row.owner_id == 9
            assert row.locked is True
        assert logs
        text = collected_text(logs[0]["view"])  # type: ignore[arg-type]
        assert VOICE_STAFF_LOG in text
        assert "voice.staff_override" in text
    finally:
        await engine.dispose()


def test_kicked_cannot_enter() -> None:
    assert kicked_may_enter(kicked=True) is False
    assert kicked_may_enter(kicked=False) is True


def test_join_ask_is_not_created_twice() -> None:
    common = {
        "actor_id": 2,
        "owner_id": 1,
        "locked": True,
        "kicked": False,
        "inside": False,
    }
    assert ask_to_join(**common, pending=False) is JoinAsk.OK
    assert ask_to_join(**common, pending=True) is JoinAsk.PENDING
    assert ask_to_join(**{**common, "kicked": True}, pending=False) is JoinAsk.KICKED


def _access_bot(sessions: object) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=ActionQueue(asyncio.create_task),
        caches=SimpleNamespace(room_owner=SimpleNamespace(set=lambda *_a, **_k: None)),
        outbound=None,
        guard=None,
        get_guild=lambda *_a, **_k: None,
    )


def _voice_guild(channel_id: int, user_id: int) -> SimpleNamespace:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.name = "катка"
    channel.members = []
    channel.user_limit = 0
    channel.overwrites_for.return_value = SimpleNamespace(connect=None, view_channel=None)
    channel.set_permissions = AsyncMock()
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.voice = None
    member.move_to = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.get_channel.return_value = channel
    guild.get_member.return_value = member
    member.guild = guild
    return SimpleNamespace(guild=guild, channel=channel, member=member)


async def test_join_request_is_not_created_twice(tmp_path: Path) -> None:
    from conductor.db.models import VoiceRoom

    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = VoiceRoomService(_access_bot(sessions))
    try:
        stored = await service._insert_room(1, channel_id=10, owner_id=5, hub_id=9)
        assert stored is not None
        async with session_scope(sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            row.locked = True
        actor = SimpleNamespace(id=2)
        guild = SimpleNamespace(id=1, get_channel=lambda *_: None, get_member=lambda *_: None)
        first = await service.ask_join(guild, stored.id, actor)  # type: ignore[arg-type]
        second = await service.ask_join(guild, stored.id, actor)  # type: ignore[arg-type]
        assert first is JoinAsk.OK
        assert second is JoinAsk.PENDING
        listed = await service.list_requests(guild, stored.id)  # type: ignore[arg-type]
        assert len(listed) == 1
        assert listed[0][1] == 2
    finally:
        await engine.dispose()


def test_name_template_fills_username_and_game() -> None:
    assert render_room_name("{username}", username="Ann", game="Valorant") == "Ann"
    assert render_room_name("{game}", username="Ann", game="Dota 2") == "Dota 2"
    name, updated = prepare_room_name(HubSettings(preset=HubPreset.NAME), username="Ann", game="")
    assert "{n}" not in name
    assert "Ann" in name
    assert not any(ch.isdigit() for ch in name)
    assert updated.preset == HubPreset.NAME


def test_clone_preset_skips_the_hub_itself() -> None:
    assert clone_uses_template(preset=HubPreset.CLONE, template_channel_id=20, hub_id=9) is True
    assert clone_uses_template(preset=HubPreset.CLONE, template_channel_id=9, hub_id=9) is False
    assert clone_uses_template(preset=HubPreset.NAME, template_channel_id=20, hub_id=9) is False
    raw = dump_hub_settings(HubSettings(preset=HubPreset.CLONE, template_channel_id=20, template=NAME_TEMPLATE_DEFAULT))
    parsed = parse_hub_settings(raw)
    assert parsed.preset == HubPreset.CLONE
    assert parsed.template_channel_id == 20


def test_merge_need_both_sides() -> None:
    assert ask_merge(from_id=1, to_id=1, pending_from=None) is MergeAsk.SELF
    assert ask_merge(from_id=1, to_id=2, pending_from=9) is MergeAsk.PENDING
    assert ask_merge(from_id=1, to_id=2, pending_from=None) is MergeAsk.OK


async def test_preset_is_stored_and_merge_is_unique(tmp_path: Path) -> None:
    from conductor.db.models import VoiceHub

    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = VoiceRoomService(_access_bot(sessions))
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        guild = SimpleNamespace(id=1, get_channel=lambda *_: None)
        settings = await service.configure_preset(
            guild,
            preset=HubPreset.NAME,
        )
        assert settings is not None
        assert settings.preset == HubPreset.NAME
        async with session_scope(sessions) as session:
            hub = await session.get(VoiceHub, 1)
            assert hub is not None
            stored = parse_hub_settings(hub.settings)
        assert stored.preset == HubPreset.NAME
        room = await service._insert_room(1, channel_id=10, owner_id=5, hub_id=9)
        assert room is not None
        other = await service._insert_room(1, channel_id=11, owner_id=6, hub_id=9)
        assert other is not None
        target = SimpleNamespace(id=11)
        assert await service.request_merge(guild, room.id, target) is MergeAsk.OK  # type: ignore[arg-type]
        assert await service.request_merge(guild, room.id, target) is MergeAsk.PENDING  # type: ignore[arg-type]
        assert await service.request_merge(guild, room.id, SimpleNamespace(id=10)) is MergeAsk.SELF  # type: ignore[arg-type]
    finally:
        await engine.dispose()


class _Role:
    def __init__(self, role_id: int) -> None:
        self.id = role_id

    def __hash__(self) -> int:
        return hash(("role", self.id))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Role) and other.id == self.id


def _overwrite_for(channel: object, user_id: int) -> discord.PermissionOverwrite | None:
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        if getattr(target, "id", None) == user_id:
            return overwrite
    return None


def _bind_overwrites(channel: MagicMock) -> None:
    channel.overwrites = {}
    channel.edits = []

    def overwrites_for(target: object) -> discord.PermissionOverwrite:
        found = _overwrite_for(channel, int(target.id))
        if found is not None:
            return discord.PermissionOverwrite(**dict(found))
        return discord.PermissionOverwrite()

    async def set_permissions(
        target: object,
        overwrite: object = discord.utils.MISSING,
        reason: str | None = None,
        **perms: object,
    ) -> None:
        del reason
        if overwrite is None:
            for key in list(channel.overwrites):
                if key is target or getattr(key, "id", None) == getattr(target, "id", None):
                    channel.overwrites.pop(key, None)
            return
        stored = overwrite if overwrite is not discord.utils.MISSING else discord.PermissionOverwrite(**perms)
        for key in list(channel.overwrites):
            if getattr(key, "id", None) == getattr(target, "id", None):
                channel.overwrites.pop(key, None)
        channel.overwrites[target] = stored

    async def edit(**kwargs: object) -> None:
        channel.edits.append(kwargs)
        if "overwrites" in kwargs:
            channel.overwrites = dict(kwargs["overwrites"])  # type: ignore[arg-type]
        if "name" in kwargs:
            channel.name = str(kwargs["name"])
        if "user_limit" in kwargs:
            channel.user_limit = int(kwargs["user_limit"])  # type: ignore[arg-type]
        if "bitrate" in kwargs:
            channel.bitrate = int(kwargs["bitrate"])  # type: ignore[arg-type]

    async def send(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=800 + int(channel.id), edit=AsyncMock())

    channel.overwrites_for = overwrites_for
    channel.set_permissions = set_permissions
    channel.edit = edit
    channel.send = send


def _fake_member(user_id: int, guild: MagicMock) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = False
    member.guild = guild
    member.display_name = f"user{user_id}"
    member.activities = ()
    member.voice = None
    member.move_to = AsyncMock()
    member.roles = []
    member.guild_permissions = SimpleNamespace(administrator=False)
    return member


def _fake_channel(channel_id: int, guild: MagicMock, *, voice: bool) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel if voice else discord.TextChannel)
    channel.id = channel_id
    channel.name = "катка" if voice else "катка-чат"
    channel.members = []
    channel.user_limit = 0
    channel.bitrate = 64000
    channel.category = None
    channel.guild = guild
    _bind_overwrites(channel)
    return channel


def _overwrite_box() -> SimpleNamespace:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1
    guild.default_role = _Role(1)
    channels: dict[int, MagicMock] = {}
    members: dict[int, MagicMock] = {}
    created: list[tuple[str, MagicMock]] = []
    seq = {"n": 100}

    def alloc() -> int:
        seq["n"] += 1
        return seq["n"]

    def get_channel(channel_id: int) -> MagicMock | None:
        return channels.get(int(channel_id))

    def get_member(user_id: int) -> MagicMock | None:
        return members.get(int(user_id))

    live: list[MagicMock] = []

    async def create_voice_channel(name: str, **kwargs: object) -> MagicMock:
        channel = _fake_channel(alloc(), guild, voice=True)
        channel.name = name
        channel.bitrate = int(kwargs.get("bitrate") or 64000)
        category = kwargs.get("category")
        channel.category = category
        channel.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]

        async def delete(*, reason: str | None = None) -> None:
            del reason
            cat = getattr(channel, "category", None)
            if cat is not None:
                kids = [item for item in list(getattr(cat, "channels", []) or []) if item is not channel]
                cat.channels = kids
            channels.pop(channel.id, None)
            if channel in live:
                live.remove(channel)

        channel.delete = delete
        if category is not None:
            kids = list(getattr(category, "channels", []) or [])
            kids.append(channel)
            category.channels = kids
        channels[channel.id] = channel
        live.append(channel)
        created.append(("voice", channel))
        return channel

    async def create_text_channel(name: str, **kwargs: object) -> MagicMock:
        channel = _fake_channel(alloc(), guild, voice=False)
        channel.name = name
        channel.category = kwargs.get("category")
        channel.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        channels[channel.id] = channel
        live.append(channel)
        created.append(("text", channel))
        return channel

    async def create_category(name: str, **kwargs: object) -> MagicMock:
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = alloc()
        category.name = name
        category.channels = []
        category.guild = guild
        category.category = None
        category.category_id = None
        category.position = kwargs.get("position", 0)
        category.overwrites = dict(kwargs.get("overwrites") or {})  # type: ignore[arg-type]
        _bind_overwrites(category)

        async def delete(*, reason: str | None = None) -> None:
            del reason
            channels.pop(category.id, None)
            if category in live:
                live.remove(category)

        category.delete = AsyncMock(side_effect=delete)
        channels[category.id] = category
        live.append(category)
        created.append(("category", category))
        return category

    guild.create_voice_channel = create_voice_channel
    guild.create_text_channel = create_text_channel
    guild.create_category = create_category
    guild.get_channel = get_channel
    guild.get_member = get_member
    guild.channels = live
    guild.me = MagicMock(spec=discord.Member)
    guild.me.id = 99
    guild.default_role = _Role(1)
    guild.default_role.permissions = SimpleNamespace(view_channel=True, connect=True)
    roles = {1: guild.default_role}

    def get_role(role_id: int) -> _Role | None:
        return roles.get(int(role_id))

    guild.get_role = get_role
    owner = _fake_member(5, guild)
    other = _fake_member(8, guild)
    members[5] = owner
    members[8] = other
    return SimpleNamespace(
        guild=guild,
        owner=owner,
        other=other,
        channels=channels,
        members=members,
        created=created,
        roles=roles,
    )


async def _seeded_room(
    tmp_path: Path, *, text: bool = False
) -> tuple[object, VoiceRoomService, StoredRoom, SimpleNamespace, MagicMock]:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = VoiceRoomService(_access_bot(sessions))
    stored = await service._insert_room(1, channel_id=10, owner_id=5, hub_id=9)
    assert stored is not None
    box = _overwrite_box()
    voice = _fake_channel(10, box.guild, voice=True)
    voice.overwrites[box.owner] = discord.PermissionOverwrite(**owner_overwrite())
    box.channels[10] = voice
    if text:
        linked = _fake_channel(20, box.guild, voice=False)
        linked.overwrites[box.owner] = discord.PermissionOverwrite(view_channel=True, manage_messages=True)
        box.channels[20] = linked
        # Старая комната из прошлых версий: спутник записан прямо в БД, код его больше не пишет.
        from conductor.db.models import VoiceRoom

        async with session_scope(sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            row.text_channel_id = 20
        stored = StoredRoom(
            guild_id=stored.guild_id,
            channel_id=stored.channel_id,
            owner_id=stored.owner_id,
            hub_id=stored.hub_id,
            id=stored.id,
            locked=stored.locked,
            owner_absent_since=stored.owner_absent_since,
            ghosted=stored.ghosted,
            text_channel_id=20,
        )
    return engine, service, stored, box, voice


def _attach_live_hub(
    box: SimpleNamespace,
    *,
    hub_id: int = 9,
    category_id: int = 8,
    move_members: bool = True,
    connect: bool = True,
) -> SimpleNamespace:
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = category_id
    category.channels = []
    category.guild = box.guild
    category.category = None
    category.category_id = None
    category.permissions_for = lambda _me: SimpleNamespace(
        manage_channels=True,
        move_members=move_members,
        connect=connect,
    )
    _bind_overwrites(category)
    hub = _fake_channel(hub_id, box.guild, voice=True)
    hub.category = category
    hub.category_id = category_id
    box.channels[category_id] = category
    box.channels[hub_id] = hub
    box.category = category
    box.hub = hub
    return box


def _assert_owner_voice(channel: object, user_id: int) -> None:
    overwrite = _overwrite_for(channel, user_id)
    assert overwrite is not None
    for name in owner_overwrite():
        assert getattr(overwrite, name) is True
    assert overwrite.manage_permissions is None
    assert overwrite.manage_roles is None


async def test_create_applies_owner_overwrite(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        "conductor.services.voice_rooms.note_success",
        lambda *args, **kwargs: calls.append(args),
    )
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = VoiceRoomService(_access_bot(sessions))
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box)
        created = await service.create_for(box.owner)
        assert created.ok is True
        assert created.channel_id is not None
        voice = next(channel for kind, channel in box.created if kind == "voice")
        assert not any(kind == "text" for kind, _ in box.created)
        _assert_owner_voice(voice, box.owner.id)
        assert calls
    finally:
        await engine.dispose()


async def test_multiple_voice_rooms_and_text_companions_use_remaining_long_window(tmp_path: Path) -> None:
    from discord.http import Route
    from tests.fake_discord import FakeDiscord

    from conductor.services.limits import OutboundGate
    from conductor.services.rate_observer import RateObserver
    from conductor.services.traffic import TrafficGovernor

    engine, sessions, service = await _room_service(tmp_path)
    bot = service._bot
    bot.rate_observer = RateObserver()
    bot.traffic = TrafficGovernor(bot, observer=bot.rate_observer)
    bot.outbound = OutboundGate(governor=bot.traffic)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box)
        async with FakeDiscord(route_limit=10, route_window=34909.724454789) as fake:
            http = fake.http(trace=bot.traffic.trace_config(), max_ratelimit_timeout=600)
            route = Route("POST", "/guilds/{guild_id}/channels", guild_id=1)
            # Окно уже известно после создания категории каркаса.
            await http.request(route, json={"name": "category"})
            for method in ("create_voice_channel",):
                original = getattr(box.guild, method)

                async def create(name, *, _original=original, **kwargs):
                    await http.request(route, json={"name": name})
                    return await _original(name, **kwargs)

                setattr(box.guild, method, create)
            for member in (box.owner, box.other):
                result = await asyncio.wait_for(service.create_for(member), timeout=5)
                assert result.ok
            assert [kind for kind, _ in box.created].count("voice") == 2
            assert [kind for kind, _ in box.created].count("text") == 0
            assert fake.statuses == [201] * 3
            async with session_scope(sessions) as session:
                rooms = await service.rooms_for(session, 1)
            assert len(rooms) == 2
            assert all(room.text_channel_id is None for room in rooms)
    finally:
        await engine.dispose()


async def test_transfer_swaps_owner_overwrite_in_one_edit(tmp_path: Path) -> None:
    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    voice.members = [box.owner, box.other]
    try:
        result = await service.transfer_room(box.guild, stored.id, box.other)
        assert result is ClaimVerdict.OK
        assert _overwrite_for(voice, box.owner.id) is None
        _assert_owner_voice(voice, box.other.id)
        assert len([edit for edit in voice.edits if "overwrites" in edit]) == 1
    finally:
        await engine.dispose()


async def test_claim_swaps_owner_overwrite(tmp_path: Path) -> None:
    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    start = datetime(2026, 1, 1, 12, 0)
    try:
        async with session_scope(service._bot.sessions) as session:
            from conductor.db.models import VoiceRoom

            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            row.owner_absent_since = start
        result = await service.claim_room(
            box.guild,
            stored.id,
            box.other,
            now=start + CLAIM_AFTER,
            occupant=True,
        )
        assert result is ClaimVerdict.OK
        assert _overwrite_for(voice, box.owner.id) is None
        _assert_owner_voice(voice, box.other.id)
        assert len([edit for edit in voice.edits if "overwrites" in edit]) == 1
    finally:
        await engine.dispose()


async def test_staff_take_swaps_owner_overwrite(tmp_path: Path) -> None:
    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    try:
        result = await service.take_as_staff(box.guild, stored.id, box.other)
        assert result is ClaimVerdict.OK
        assert _overwrite_for(voice, box.owner.id) is None
        _assert_owner_voice(voice, box.other.id)
        assert len([edit for edit in voice.edits if "overwrites" in edit]) == 1
    finally:
        await engine.dispose()


async def test_lock_and_ghost_keep_owner_overwrite(tmp_path: Path) -> None:
    engine, service, stored, box, voice = await _seeded_room(tmp_path, text=False)
    try:
        locked = await service.toggle_lock(box.guild, stored.id)
        assert locked is True
        _assert_owner_voice(voice, box.owner.id)
        everyone = voice.overwrites_for(box.guild.default_role)
        assert everyone.connect is False
        ghosted = await service.toggle_ghost(box.guild, stored.id)
        assert ghosted is True
        _assert_owner_voice(voice, box.owner.id)
        everyone = voice.overwrites_for(box.guild.default_role)
        assert everyone.view_channel is False
        assert everyone.connect is False
    finally:
        await engine.dispose()


async def test_panel_card_reads_live_channel_values(tmp_path: Path) -> None:
    from conductor.presentation.copy import voice_inside, voice_room_title
    from conductor.presentation.layout import collected_text

    engine, service, stored, box, voice = await _seeded_room(tmp_path, text=False)
    voice.name = "ручное имя"
    voice.user_limit = 4
    voice.bitrate = 96000
    try:
        view = await service._panel_card(box.guild, stored, voice)
        text = collected_text(view)
        assert voice_room_title("ручное имя") in text
        assert voice_inside(0, 4) in text
        before = list(voice.edits)
        await service.refresh_panel(box.guild, stored.id)
        assert voice.edits == before
    finally:
        await engine.dispose()


def test_room_outcome_ok_requires_channel() -> None:
    assert RoomOutcome(channel_id=10).ok is True
    assert RoomOutcome(denial=RoomDenial.NO_HUB).ok is False
    assert RoomOutcome().ok is False


async def _room_service(tmp_path: Path) -> tuple[object, object, VoiceRoomService]:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    return engine, sessions, VoiceRoomService(_access_bot(sessions))


async def _arrange_denial(
    service: VoiceRoomService,
    sessions: object,
    box: SimpleNamespace,
    denial: RoomDenial,
) -> None:
    from time import time

    from conductor.domain.create_lock import BUCKET_CHANNEL, HoldSource
    from conductor.domain.rooms import CREATE_DEBOUNCE, create_job_key
    from conductor.services.create_lock import remember

    if denial is RoomDenial.NO_HUB:
        return
    async with session_scope(sessions) as session:
        await service.set_hub(session, 1, channel_id=9, category_id=8)
    if denial is RoomDenial.HUB_GONE:
        return
    if denial is RoomDenial.NO_CATEGORY:
        hub = _fake_channel(9, box.guild, voice=True)
        hub.category = None
        hub.category_id = None
        box.channels[9] = hub
        return
    _attach_live_hub(box)
    if denial is RoomDenial.BOT_PERMS:
        box.category.permissions_for = lambda _me: SimpleNamespace(
            manage_channels=True, move_members=False, connect=True
        )
        return
    if denial is RoomDenial.HELD:
        remember(service._bot, 1, BUCKET_CHANNEL, 60.0, time(), source=HoldSource.DISCORD)
        return
    if denial is RoomDenial.RATE:
        service._bot.queue.debounce(create_job_key(1, box.owner.id), CREATE_DEBOUNCE)
        return
    if denial is RoomDenial.LIMIT:
        from conductor.domain.discord_limits import GUILD_CHANNELS

        box.guild.channels = [SimpleNamespace(id=i) for i in range(GUILD_CHANNELS)]
        return
    if denial is RoomDenial.FAILED:
        box.guild.create_voice_channel = AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="fail"), "fail")
        )


@pytest.mark.parametrize("denial", list(RoomDenial))
async def test_create_for_reaches_every_denial(tmp_path: Path, denial: RoomDenial) -> None:
    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        await _arrange_denial(service, sessions, box, denial)
        outcome = await service.create_for(box.owner)
        assert outcome.ok is False
        assert outcome.denial is denial
        assert outcome.channel_id is None
    finally:
        await engine.dispose()


async def test_hub_health_heals_moved_category(tmp_path: Path) -> None:
    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box, category_id=8)
        moved = MagicMock(spec=discord.CategoryChannel)
        moved.id = 12
        moved.channels = []
        moved.guild = box.guild
        moved.category = None
        moved.category_id = None
        moved.permissions_for = lambda _me: SimpleNamespace(
            manage_channels=True, move_members=True, connect=True
        )
        _bind_overwrites(moved)
        box.channels[12] = moved
        box.hub.category = moved
        box.hub.category_id = 12
        denial, _details = await service.hub_health(box.guild)
        assert denial is None
        async with session_scope(sessions) as session:
            row = await service.hub_for(session, 1)
            assert row is not None
            assert row.category_id == 12
        later = MagicMock(spec=discord.CategoryChannel)
        later.id = 13
        later.channels = []
        later.guild = box.guild
        later.permissions_for = lambda _me: SimpleNamespace(
            manage_channels=True, move_members=True, connect=True
        )
        _bind_overwrites(later)
        box.channels[13] = later
        box.hub.category = later
        box.hub.category_id = 13
        denial, _details = await service.hub_health(box.guild)
        assert denial is None
        async with session_scope(sessions) as session:
            row = await service.hub_for(session, 1)
            assert row is not None
            assert row.category_id == 12
    finally:
        await engine.dispose()


async def test_hub_health_without_move_members_is_bot_perms(tmp_path: Path) -> None:
    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box, move_members=False)
        denial, details = await service.hub_health(box.guild)
        assert denial is RoomDenial.BOT_PERMS
        assert details.get("target") == "bot"
    finally:
        await engine.dispose()


async def test_hub_health_without_verified_connect_is_denial(tmp_path: Path) -> None:
    from conductor.db.models import VerificationSettings

    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
            session.add(VerificationSettings(guild_id=1, verified_role_id=42))
        _attach_live_hub(box)
        role = _Role(42)
        role.permissions = SimpleNamespace(view_channel=True, connect=True)
        box.roles[42] = role
        box.hub.overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=False)
        denial, details = await service.hub_health(box.guild)
        assert denial is RoomDenial.BOT_PERMS
        assert details.get("target") == "verified"
    finally:
        await engine.dispose()


async def test_handle_move_denial_does_not_block_empty_logic(tmp_path: Path) -> None:
    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        stored = await service._insert_room(1, channel_id=10, owner_id=8, hub_id=9)
        assert stored is not None
        _attach_live_hub(box, move_members=False)
        left = _fake_channel(10, box.guild, voice=True)
        left.members = []
        box.channels[10] = left
        marked: list[tuple[int, int]] = []
        service.janitor.mark_empty = lambda guild_id, channel_id: marked.append((guild_id, channel_id))
        box.owner.send = AsyncMock()
        await service.handle_move(
            box.owner,
            SimpleNamespace(channel=left),
            SimpleNamespace(channel=box.hub),
        )
        assert (1, 10) in marked
    finally:
        await engine.dispose()


async def test_second_hub_join_inside_debounce_is_rate(tmp_path: Path) -> None:
    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box)
        box.guild.create_voice_channel = AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="fail"), "fail")
        )
        noted: list[RoomDenial | None] = []
        real = service._note_denial

        async def capture(member: object, outcome: RoomOutcome) -> None:
            noted.append(outcome.denial)
            await real(member, outcome)

        service._note_denial = capture  # type: ignore[method-assign]
        box.owner.send = AsyncMock()
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=box.hub)
        await service.handle_move(box.owner, before, after)
        await service.handle_move(box.owner, before, after)
        assert noted[:2] == [RoomDenial.FAILED, RoomDenial.RATE]
    finally:
        await engine.dispose()


async def test_kick_disconnects_without_ban(tmp_path: Path) -> None:
    from conductor.db.models import VoiceRoomKick

    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    guest = box.other
    guest.voice = SimpleNamespace(channel=voice)
    voice.members = [box.owner, guest]
    try:
        error = await service.kick_member(box.guild, stored.id, guest)
        assert error is None
        guest.move_to.assert_awaited()
        assert guest.move_to.await_args.args[0] is None
        async with session_scope(service._bot.sessions) as session:
            row = await session.get(VoiceRoomKick, (stored.id, guest.id))
        assert row is None
        assert await service._kicked_may_enter(stored.id, guest.id) is True
        await service.handle_move(guest, SimpleNamespace(channel=None), SimpleNamespace(channel=voice))
        assert guest.move_to.await_count == 1
    finally:
        await engine.dispose()


async def test_block_denies_before_disconnect(tmp_path: Path) -> None:
    from conductor.db.models import VoiceRoomKick

    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    guest = box.other
    guest.voice = SimpleNamespace(channel=voice)
    voice.members = [box.owner, guest]
    order: list[str] = []

    async def set_permissions(*args: object, **kwargs: object) -> None:
        del args, kwargs
        order.append("deny")

    async def move_to(*args: object, **kwargs: object) -> None:
        del args, kwargs
        order.append("move")

    voice.set_permissions = AsyncMock(side_effect=set_permissions)
    guest.move_to = AsyncMock(side_effect=move_to)
    try:
        error = await service.block_member(box.guild, stored.id, guest)
        assert error is None
        assert order == ["deny", "move"]
        assert guest.move_to.await_args.args[0] is None
        async with session_scope(service._bot.sessions) as session:
            row = await session.get(VoiceRoomKick, (stored.id, guest.id))
        assert row is not None
        box.guild.create_voice_channel = AsyncMock(side_effect=AssertionError("hub must not create a room"))
        await service.handle_move(guest, SimpleNamespace(channel=None), SimpleNamespace(channel=voice))
        assert guest.move_to.await_count == 2
        assert guest.move_to.await_args.args[0] is None
    finally:
        await engine.dispose()


async def test_block_administrator_explains_bypass(tmp_path: Path) -> None:
    from conductor.presentation import copy

    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    guest = box.other
    guest.guild_permissions = SimpleNamespace(administrator=True)
    guest.voice = SimpleNamespace(channel=voice)
    voice.members = [box.owner, guest]
    try:
        notice = await service.block_member(box.guild, stored.id, guest)
        assert notice == copy.VOICE_BAN_ADMIN
        guest.move_to.assert_awaited()
        assert guest.move_to.await_args.args[0] is None
    finally:
        await engine.dispose()


async def test_cannot_kick_watcher(tmp_path: Path) -> None:
    from conductor.domain.access import AccessSnapshot, Rank
    from conductor.presentation import copy

    engine, service, stored, box, voice = await _seeded_room(tmp_path)
    guest = box.other
    guest.roles = [SimpleNamespace(id=777)]
    guest.voice = SimpleNamespace(channel=voice)
    voice.members = [box.owner, guest]
    snapshot = AccessSnapshot(rank_roles={Rank.WATCHER: (777,)}, rules={}, special_locked=False)
    service._bot.caches.permissions = SimpleNamespace(get=lambda _key: snapshot, set=lambda *_a, **_k: None)
    try:
        error = await service.kick_member(box.guild, stored.id, guest)
        assert error == copy.VOICE_KICK_STAFF
        guest.move_to.assert_not_called()
        blocked = await service.block_member(box.guild, stored.id, guest)
        assert blocked == copy.VOICE_KICK_STAFF
    finally:
        await engine.dispose()


async def test_cleanup_companions_is_idempotent(tmp_path: Path) -> None:
    from conductor.db.models import VoiceRoom

    engine, service, stored, box, _voice = await _seeded_room(tmp_path, text=True)
    linked = box.channels[20]
    linked.delete = AsyncMock()
    try:
        first = await service.cleanup_companions(box.guild)
        assert first == 1
        linked.delete.assert_awaited()
        async with session_scope(service._bot.sessions) as session:
            row = await session.get(VoiceRoom, stored.id)
            assert row is not None
            assert row.text_channel_id is None
        linked.delete.reset_mock()
        second = await service.cleanup_companions(box.guild)
        assert second == 0
        linked.delete.assert_not_called()
    finally:
        await engine.dispose()


async def test_full_category_opens_extra_same_name(tmp_path: Path) -> None:
    from conductor.db.models import VoiceHub

    engine, sessions, service = await _room_service(tmp_path)
    box = _overwrite_box()
    service._bot.get_guild = lambda _gid: box.guild
    try:
        async with session_scope(sessions) as session:
            await service.set_hub(session, 1, channel_id=9, category_id=8)
        _attach_live_hub(box)
        box.category.channels = [MagicMock() for _ in range(50)]
        box.category.name = "комнаты"
        box.category.position = 3
        created = await service.create_for(box.owner)
        assert created.ok is True
        extras = [channel for kind, channel in box.created if kind == "category"]
        assert len(extras) == 1
        extra = extras[0]
        assert extra.name == "комнаты"
        voice = next(channel for kind, channel in box.created if kind == "voice")
        assert voice.category is extra
        async with session_scope(sessions) as session:
            hub = await session.get(VoiceHub, 1)
            assert hub is not None
            settings = parse_hub_settings(hub.settings)
        assert extra.id in settings.extra_category_ids
        voice.members = []
        deleted = await service.sweep_empty(1, voice.id)
        assert deleted is True
        extra.delete.assert_awaited()
        async with session_scope(sessions) as session:
            hub = await session.get(VoiceHub, 1)
            assert hub is not None
            settings = parse_hub_settings(hub.settings)
        assert extra.id not in settings.extra_category_ids
    finally:
        await engine.dispose()


def test_move_to_only_inside_move_and_disconnect() -> None:
    import ast
    import inspect
    from pathlib import Path

    root = Path("src/conductor/services")
    allowed = {"_move", "_disconnect"}
    found: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "move_to" and fn.name not in allowed:
                    found.append(f"{path.as_posix()}:{fn.name}")
    assert found == []
    for name in ("kick_member", "block_member"):
        source = inspect.getsource(getattr(VoiceRoomService, name))
        assert "_move(" not in source
    handle = inspect.getsource(VoiceRoomService.handle_move)
    assert "_disconnect(member)" in handle
    assert "dest = hub_id" not in handle
