from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildRank, PrivateGroup
from conductor.db.session import session_scope
from conductor.domain.access import Rank, is_special_or_admin
from conductor.domain.groups import (
    CreateGate,
    CreateVerdict,
    group_quota,
    may_create_group,
    parse_spec,
)
from conductor.services.groups import GroupService
from conductor.services.queue import ActionQueue
from test_groups import (
    STAMP,
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


def test_may_create_group_threshold_and_privilege() -> None:
    assert (
        may_create_group(
            has_key=False, privileged=False, score=50, min_reputation=50, reputation_enabled=True
        )
        is CreateGate.REPUTATION
    )
    assert (
        may_create_group(
            has_key=False, privileged=False, score=51, min_reputation=50, reputation_enabled=True
        )
        is CreateGate.OK
    )
    assert (
        may_create_group(
            has_key=False, privileged=False, score=80, min_reputation=50, reputation_enabled=False
        )
        is CreateGate.REPUTATION_OFF
    )
    assert (
        may_create_group(
            has_key=False, privileged=True, score=0, min_reputation=50, reputation_enabled=True
        )
        is CreateGate.OK
    )
    assert is_special_or_admin(rank=Rank.SPECIAL, is_administrator=False, is_guild_owner=False)
    assert is_special_or_admin(rank=Rank.NONE, is_administrator=True, is_guild_owner=False)
    assert is_special_or_admin(rank=Rank.NONE, is_administrator=False, is_guild_owner=True)
    assert not is_special_or_admin(rank=Rank.CHIEF, is_administrator=False, is_guild_owner=False)


def test_group_quota_window_and_privilege() -> None:
    now = STAMP
    daily = group_quota(
        created_at=(now,),
        open_count=1,
        now=now,
        daily_limit=1,
        open_limit=3,
        privileged=False,
    )
    assert daily.ok is False
    assert daily.reason == "daily"
    assert daily.retry_at == now + timedelta(hours=24)
    opened = group_quota(
        created_at=(),
        open_count=3,
        now=now,
        daily_limit=1,
        open_limit=3,
        privileged=False,
    )
    assert opened.ok is False
    assert opened.reason == "open"
    free = group_quota(
        created_at=(now, now),
        open_count=9,
        now=now,
        daily_limit=1,
        open_limit=3,
        privileged=True,
    )
    assert free.ok is True
    stale = group_quota(
        created_at=(now - timedelta(hours=25),),
        open_count=0,
        now=now,
        daily_limit=1,
        open_limit=3,
        privileged=False,
    )
    assert stale.ok is True


async def test_special_with_zero_score_creates(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    special = guild.add_role(FakeRole(11))
    host.roles = (special,)
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        async with session_scope(sessions) as session:
            session.add(GuildRank(guild_id=1, rank="special", role_id=11))
        await _seed_score(sessions, guild.id, host.id, score=0)
        spec = parse_spec(title="стол", members=None, owner_id=1)
        assert spec is not None
        report = await service.create(guild, host, spec, now=STAMP)
        assert report.verdict is CreateVerdict.OK
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_score_fifty_denied_fifty_one_ok(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id, score=50)
        spec = parse_spec(title="стол", members=None, owner_id=1)
        assert spec is not None
        denied = await service.create(guild, host, spec, now=STAMP)
        assert denied.verdict is CreateVerdict.REPUTATION
        await _seed_score(sessions, guild.id, host.id, score=51)
        ok = await service.create(guild, host, spec, now=STAMP)
        assert ok.verdict is CreateVerdict.OK
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_second_group_in_24h_hits_daily(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        first = parse_spec(title="один", members=None, owner_id=1)
        second = parse_spec(title="два", members=None, owner_id=1)
        assert first is not None and second is not None
        ok = await service.create(guild, host, first, now=STAMP)
        assert ok.verdict is CreateVerdict.OK
        blocked = await service.create(guild, host, second, now=STAMP + timedelta(hours=1))
        assert blocked.verdict is CreateVerdict.QUOTA_DAILY
        assert blocked.retry_at == STAMP + timedelta(hours=24)
        later = await service.create(guild, host, second, now=STAMP + timedelta(hours=25))
        assert later.verdict is CreateVerdict.OK
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_fourth_open_group_hits_open_limit(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        for index in range(3):
            spec = parse_spec(title=f"стол {index}", members=None, owner_id=1)
            assert spec is not None
            report = await service.create(
                guild, host, spec, now=STAMP + timedelta(hours=25 * index)
            )
            assert report.verdict is CreateVerdict.OK
        extra = parse_spec(title="четвёртая", members=None, owner_id=1)
        assert extra is not None
        blocked = await service.create(guild, host, extra, now=STAMP + timedelta(hours=80))
        assert blocked.verdict is CreateVerdict.QUOTA_OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_create_race_with_daily_limit_one(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    host = guild.add_member(FakeMember(1))
    spawned, spawn = _spawn_box()
    service = GroupService(_bot(sessions, guild, ActionQueue(spawn, clock=FakeClock())))
    try:
        await _seed_score(sessions, guild.id, host.id)
        first = parse_spec(title="гонка-а", members=None, owner_id=1)
        second = parse_spec(title="гонка-б", members=None, owner_id=1)
        assert first is not None and second is not None
        left, right = await asyncio.gather(
            service.create(guild, host, first, now=STAMP),
            service.create(guild, host, second, now=STAMP),
        )
        verdicts = {left.verdict, right.verdict}
        assert CreateVerdict.OK in verdicts
        assert CreateVerdict.QUOTA_DAILY in verdicts
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(PrivateGroup))).all()
        assert len(rows) == 1
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
