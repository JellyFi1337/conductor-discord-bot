from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GroupInvite, GroupMember
from conductor.db.session import session_scope
from conductor.domain.groups import (
    ArchiveReason,
    CreateVerdict,
    GroupState,
    InviteStatus,
    ManageVerdict,
    parse_spec,
)
from conductor.services.groups import GroupService
from conductor.services.queue import ActionQueue
from test_groups import (
    STAMP,
    FakeClock,
    FakeGuild,
    FakeMember,
    _await_spawned,
    _bot,
    _seed_score,
    _spawn_box,
    _url,
)


async def test_create_invites_three_pending_one_member(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    for user_id in (2, 3, 4):
        guild.add_member(FakeMember(user_id))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2> <@3> <@4>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        assert report.verdict is CreateVerdict.OK
        assert report.invites_sent == 3
        assert report.invites_closed == 0
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        assert card.member_ids == (1,)
        async with session_scope(sessions) as session:
            invites = (await session.scalars(select(GroupInvite))).all()
        assert len(invites) == 3
        assert {int(row.user_id) for row in invites} == {2, 3, 4}
        assert all(row.status == InviteStatus.PENDING.value for row in invites)
        assert all(row.dm_sent is True for row in invites)
        assert len(guild.get_member(2).dms) == 1
        assert len(guild.get_member(3).dms) == 1
        assert len(guild.get_member(4).dms) == 1
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_accept_grants_and_marks_accepted(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guest = guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        ok = await service.accept_invite(guild, card.group_id, actor_id=2, now=STAMP)
        assert ok is ManageVerdict.OK
        live = await service.card(guild.id, card.group_id)
        assert live is not None
        assert 2 in live.member_ids
        channel = guild.channels[card.channel_id]
        assert guest in channel.overwrites
        async with session_scope(sessions) as session:
            invite = await session.get(GroupInvite, (card.group_id, 2))
            members = (await session.scalars(select(GroupMember))).all()
        assert invite is not None
        assert invite.status == InviteStatus.ACCEPTED.value
        assert {int(row.user_id) for row in members} == {1, 2}
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_decline_blocks_reinvite_for_24h(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        declined = await service.decline_invite(guild, card.group_id, actor_id=2, now=STAMP)
        assert declined is ManageVerdict.OK
        again = await service.invite(guild, card.group_id, actor_id=1, user_id=2, now=STAMP + timedelta(hours=1))
        assert again.verdict is ManageVerdict.COOLDOWN
        later = await service.invite(guild, card.group_id, actor_id=1, user_id=2, now=STAMP + timedelta(hours=24))
        assert later.verdict is ManageVerdict.OK
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_closed_dm_stays_pending_in_mine(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guest = guild.add_member(FakeMember(2))
    guest.closed_dm = True
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        assert report.verdict is CreateVerdict.OK
        assert report.invites_sent == 0
        assert report.invites_closed == 1
        listing = await service.mine(guild, guest, now=STAMP)
        assert len(listing.invites) == 1
        assert listing.invites[0].dm_sent is False
        async with session_scope(sessions) as session:
            invite = await session.get(GroupInvite, (listing.invites[0].group_id, 2))
        assert invite is not None
        assert invite.status == InviteStatus.PENDING.value
        assert invite.dm_sent is False
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_accept_denied_when_full(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        async with session_scope(sessions) as session:
            for user_id in range(10, 29):
                session.add(GroupMember(group_id=card.group_id, user_id=user_id))
        blocked = await service.accept_invite(guild, card.group_id, actor_id=2, now=STAMP)
        assert blocked is ManageVerdict.FULL
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_archive_expires_pending_invites(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guild.add_member(FakeMember(2))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members="<@2>", owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        ok, teardown = await service.archive(
            guild, card.group_id, actor_id=1, reason=ArchiveReason.OWNER, now=STAMP
        )
        assert ok is ManageVerdict.OK
        if teardown is not None:
            await teardown()
        live = await service.card(guild.id, card.group_id)
        assert live is not None
        assert live.state is GroupState.ARCHIVED
        async with session_scope(sessions) as session:
            invite = await session.get(GroupInvite, (card.group_id, 2))
        assert invite is not None
        assert invite.status == InviteStatus.EXPIRED.value
        expired = await service.accept_invite(guild, card.group_id, actor_id=2, now=STAMP)
        assert expired is ManageVerdict.EXPIRED
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_header_invite_does_not_add_immediately(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    guild.add_member(FakeMember(5))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        spec = parse_spec(title="стол", members=None, owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        card = await service.group_by_channel(guild.id, int(report.channel_id or 0))
        assert card is not None
        sent = await service.invite(guild, card.group_id, actor_id=1, user_id=5, now=STAMP)
        assert sent.verdict is ManageVerdict.OK
        live = await service.card(guild.id, card.group_id)
        assert live is not None
        assert 5 not in live.member_ids
        async with session_scope(sessions) as session:
            invite = await session.get(GroupInvite, (card.group_id, 5))
        assert invite is not None
        assert invite.status == InviteStatus.PENDING.value
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
