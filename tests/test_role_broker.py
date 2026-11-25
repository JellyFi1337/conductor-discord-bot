from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import RoleChange
from conductor.db.session import session_scope
from conductor.domain.mutations import MutationScope
from conductor.domain.roles import RoleOpError
from conductor.services.queue import ActionQueue
from conductor.services.role_broker import (
    RoleBindOverlap,
    RoleBroker,
    RoleClaim,
    RoleSubsystem,
    combine_claims,
    filter_owned,
    net_member_roles,
    scope_for,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeRole:
    def __init__(self, role_id: int, *, name: str = "r", position: int = 1, managed: bool = False) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.position = position
        self.managed = managed

    def __ge__(self, other: object) -> bool:
        return self.position >= getattr(other, "position", 0)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeRole) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class FakeMember:
    def __init__(self, user_id: int, guild: FakeGuild, roles: list[FakeRole]) -> None:
        self.id = user_id
        self.guild = guild
        self.roles = list(roles)
        self.calls: list[tuple[str, tuple[int, ...], str | None]] = []

    async def add_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        self.calls.append(("add", tuple(role.id for role in roles), reason))
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        self.calls.append(("remove", tuple(role.id for role in roles), reason))
        self.roles = [role for role in self.roles if role not in roles]


class FakeGuild:
    def __init__(self, guild_id: int, *, top: FakeRole) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(top_role=top)
        self._members: dict[int, FakeMember] = {}
        self._roles: dict[int, FakeRole] = {top.id: top}

    def add_role(self, role: FakeRole) -> FakeRole:
        self._roles[role.id] = role
        return role

    def add_member(self, user_id: int, roles: list[FakeRole] | None = None) -> FakeMember:
        member = FakeMember(user_id, self, roles or [])
        self._members[user_id] = member
        return member

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return self._roles.get(role_id)


class NullOutbound:
    async def __aenter__(self) -> NullOutbound:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _broker(
    guild: FakeGuild,
    *,
    clock: FakeClock | None = None,
    interval: float = 600.0,
    sessions: object | None = None,
) -> RoleBroker:
    tick = clock or FakeClock()
    queue = ActionQueue(asyncio.create_task, clock=tick)
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == guild.id else None,
        queue=queue,
        outbound=NullOutbound(),
        caches=SimpleNamespace(invalidate_member=lambda *_a, **_k: None),
        sessions=sessions,
        config_by_guild=SimpleNamespace(get=lambda *_a, **_k: None),
    )
    return RoleBroker(bot, interval=interval, queue=queue)


def test_bind_refuses_id_owned_by_another_subsystem() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    broker = _broker(guild)
    assert broker.bind(1, RoleSubsystem.LOYALTY, (10,)) == frozenset()
    rejected = broker.bind(1, RoleSubsystem.STAFF, (10, 20))
    assert rejected == frozenset({10})
    assert broker.owned(1, RoleSubsystem.LOYALTY) == frozenset({10})
    assert broker.owned(1, RoleSubsystem.STAFF) == frozenset({20})
    assert broker.owner_of(1, 10) is RoleSubsystem.LOYALTY
    assert broker.owner_of(1, 20) is RoleSubsystem.STAFF
    with pytest.raises(RoleBindOverlap) as overlap:
        broker.bind(1, RoleSubsystem.REPUTATION, (10,), strict=True)
    assert overlap.value.role_id == 10
    assert overlap.value.owner is RoleSubsystem.LOYALTY
    assert broker.owned(1, RoleSubsystem.REPUTATION) == frozenset()


async def test_giveaway_cannot_claim_ladder_role() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    ladder = guild.add_role(FakeRole(10, position=5))
    prize = guild.add_role(FakeRole(20, position=5))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.ACTIVITY, (ladder.id,))
    rejected = broker.bind(1, RoleSubsystem.GIVEAWAY, (ladder.id, prize.id))
    assert ladder.id in rejected
    assert broker.owned(1, RoleSubsystem.GIVEAWAY) == frozenset({prize.id})
    result = await broker.apply(RoleClaim(1, 7, RoleSubsystem.GIVEAWAY, add=(ladder.id,), reason="giveaway"))
    assert ladder.id in result.dropped
    assert result.called is False
    assert ladder not in member.roles
    granted = await broker.apply(RoleClaim(1, 7, RoleSubsystem.GIVEAWAY, add=(prize.id,), reason="giveaway"))
    assert granted.ok is True
    assert prize in member.roles


def test_filter_owned_hides_foreign_roles() -> None:
    assert filter_owned((10, 20, 30), {10, 30}) == (10, 30)


def test_net_member_roles_is_idempotent() -> None:
    add, remove = net_member_roles(have={1, 2}, add={1, 3}, remove={2, 4})
    assert add == (3,)
    assert remove == (2,)
    again, empty = net_member_roles(have={1, 3}, add={1, 3}, remove=set())
    assert again == ()
    assert empty == ()


def test_combine_claims_nets_same_subsystem() -> None:
    left = RoleClaim(1, 2, RoleSubsystem.LOYALTY, add=(10,), remove=(20,))
    right = RoleClaim(1, 2, RoleSubsystem.LOYALTY, add=(20,), remove=(10,))
    merged = combine_claims(left, right)
    assert merged.add == (20,)
    assert merged.remove == (10,)


def test_moderation_subsystem_uses_members_moderation_scope() -> None:
    for subsystem in RoleSubsystem:
        assert scope_for(RoleClaim(1, 2, subsystem, add=(10,))) is not None
    claim = RoleClaim(1, 2, RoleSubsystem.MODERATION, add=(10,))
    assert scope_for(claim) is MutationScope.MEMBERS_MODERATION


async def test_owner_subsystem_skips_owned_filter() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    extra = guild.add_role(FakeRole(20, name="any", position=5))
    member = guild.add_member(7)
    broker = _broker(guild)
    result = await broker.apply(
        RoleClaim(1, 7, RoleSubsystem.OWNER, add=(extra.id,), reason="owner", urgent=True),
    )
    assert result.ok is True
    assert result.dropped == ()
    assert extra in member.roles
    assert broker._locks == {}
    assert broker._pending == {}
    assert scope_for(RoleClaim(1, 7, RoleSubsystem.OWNER, add=(20,))) is MutationScope.ROLES_OWNER


async def test_subsystem_cannot_touch_foreign_roles() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    loyalty = guild.add_role(FakeRole(10, name="old", position=5))
    other = guild.add_role(FakeRole(20, name="rep", position=5))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    broker.bind(1, RoleSubsystem.REPUTATION, (20,))
    result = await broker.apply(
        RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(loyalty.id, other.id), reason="x"),
    )
    assert result.ok is True
    assert result.dropped == (20,)
    assert member.calls == [("add", (10,), "x")]
    assert other not in member.roles
    assert loyalty in member.roles


async def test_two_subsystems_merge_into_one_discord_call() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    ruby = guild.add_role(FakeRole(10, position=5))
    badge = guild.add_role(FakeRole(20, position=5))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    broker.bind(1, RoleSubsystem.ACTIVITY, (20,))
    broker.merge(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(ruby.id,), reason="loyalty"))
    broker.merge(RoleClaim(1, 7, RoleSubsystem.ACTIVITY, add=(badge.id,), reason="activity"))
    result = await broker.commit(1, 7)
    assert result.ok is True
    assert result.called is True
    assert member.calls == [("add", (10, 20), "loyalty; activity")]
    assert ruby in member.roles and badge in member.roles


async def test_repeat_intent_does_not_call_discord() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    role = guild.add_role(FakeRole(10, position=5))
    member = guild.add_member(7, [role])
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    first = await broker.apply(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(10,), reason="grant"))
    second = await broker.apply(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(10,), reason="grant"))
    assert first.ok is True
    assert first.called is False
    assert second.ok is True
    assert second.called is False
    assert member.calls == []


async def test_role_above_bot_is_rejected_with_reason() -> None:
    top = FakeRole(99, position=10)
    guild = FakeGuild(1, top=top)
    high = guild.add_role(FakeRole(10, name="high", position=10))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    result = await broker.apply(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(high.id,), reason="grant"))
    assert result.ok is False
    assert result.called is False
    assert result.error is not None
    assert "выше роли бота" in result.error
    assert member.calls == []


async def test_managed_role_is_rejected() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    managed = guild.add_role(FakeRole(10, name="boost", position=5, managed=True))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    result = await broker.apply(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(managed.id,), reason="grant"))
    assert result.ok is False
    assert result.code is RoleOpError.MANAGED
    assert "интеграцией" in (result.error or "")
    assert member.calls == []


async def test_missing_member_is_skipped() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    guild.add_role(FakeRole(10, position=5))
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    result = await broker.apply(RoleClaim(1, 8, RoleSubsystem.LOYALTY, add=(10,), reason="grant"))
    assert result.ok is False
    assert result.missing_member is True
    assert result.code is RoleOpError.NOT_MEMBER


async def test_enforcement_applies_urgent_loyalty() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    role = guild.add_role(FakeRole(10, position=5))
    member = guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    result = await broker.apply(
        RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(role.id,), reason="revert", urgent=True),
    )
    assert result.ok is True
    assert member.calls == [("add", (10,), "revert")]


async def test_missing_role_is_skipped_and_rest_applied() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    guest = guild.add_role(FakeRole(10, position=5))
    member = guild.add_member(7, [guest])
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.ACTIVITY, (10, 11))
    result = await broker.apply(
        RoleClaim(1, 7, RoleSubsystem.ACTIVITY, add=(11,), remove=(10,), reason="ladder"),
    )
    assert result.ok is True
    assert result.skipped == (11,)
    assert result.called is True
    assert guest not in member.roles
    assert member.calls == [("remove", (10,), "ladder")]


async def test_missing_only_roles_succeed_with_skipped() -> None:
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    guild.add_member(7)
    broker = _broker(guild)
    broker.bind(1, RoleSubsystem.ACTIVITY, (11,))
    result = await broker.apply(RoleClaim(1, 7, RoleSubsystem.ACTIVITY, add=(11,), reason="ladder"))
    assert result.ok is True
    assert result.skipped == (11,)
    assert result.called is False
    assert result.error is None


async def test_submit_rate_limit_defers_second_change() -> None:
    clock = FakeClock()
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    first = guild.add_role(FakeRole(10, position=5))
    second = guild.add_role(FakeRole(11, position=5))
    member = guild.add_member(7)
    broker = _broker(guild, clock=clock, interval=10.0)
    broker.bind(1, RoleSubsystem.ACTIVITY, (10, 11))
    await broker.apply(RoleClaim(1, 7, RoleSubsystem.ACTIVITY, add=(first.id,), reason="one"))
    assert member.calls == [("add", (10,), "one")]
    broker.submit(RoleClaim(1, 7, RoleSubsystem.ACTIVITY, add=(second.id,), reason="two"))
    assert broker._queue.until(("roles", 1, 7)) == 10.0
    clock.advance(10.0)
    result = await broker.commit(1, 7)
    assert result.ok is True
    assert member.calls[-1] == ("add", (11,), "two")


async def test_urgent_submit_ignores_cooldown() -> None:
    clock = FakeClock()
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    first = guild.add_role(FakeRole(10, position=5))
    second = guild.add_role(FakeRole(11, position=5))
    member = guild.add_member(7)
    broker = _broker(guild, clock=clock, interval=10.0)
    broker.bind(1, RoleSubsystem.LOYALTY, (10, 11))
    await broker.apply(RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(first.id,), reason="grant"))
    result = await broker.apply(
        RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(second.id,), reason="revert", urgent=True),
    )
    assert result.ok is True
    assert result.called is True
    assert ("add", (11,), "revert") in member.calls


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def test_role_changes_are_journaled(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild(1, top=FakeRole(99, position=100))
    role = guild.add_role(FakeRole(10, position=5))
    guild.add_member(7)
    broker = _broker(guild, sessions=sessions)
    broker.bind(1, RoleSubsystem.LOYALTY, (10,))
    try:
        await broker.apply(
            RoleClaim(1, 7, RoleSubsystem.LOYALTY, add=(role.id,), reason="выдача", actor_id=42),
        )
        async with session_scope(sessions) as session:
            row = await session.scalar(select(RoleChange))
            assert row is not None
            assert row.subsystem == "loyalty"
            assert row.ok is True
            assert json.loads(row.add_role_ids) == [10]
            assert row.reason == "выдача"
            assert row.actor_id == 42
            assert row.error is None
    finally:
        await engine.dispose()
