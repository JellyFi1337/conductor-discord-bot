from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import AchievementGrant, AchievementProgress, Reputation, ReputationEvent
from conductor.db.session import session_scope
from conductor.domain.achievements import (
    KEYS,
    AchievementDef,
    default_defs,
    newly_reached,
    parse_count_reason,
    parse_key,
    role_diff,
    stage_for,
)
from conductor.domain.tickets import CategoryKind, Outcome, TicketClosed
from conductor.presentation import copy
from conductor.presentation.achievements import grant_view
from conductor.presentation.layout import collected_text
from conductor.services.achievements import AchievementService
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.privacy import PrivacyService
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult, RoleClaim, RoleSubsystem

STAMP = datetime(2026, 9, 5, tzinfo=UTC)
FIXER = (101, 102, 103)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def test_newly_reached_skips_two_stages() -> None:
    defs = default_defs(copy.ACHIEVEMENT_DEFAULTS)
    fixer = tuple(row for row in defs if row.key == "fixer")
    reached = newly_reached(0, 5, fixer)
    assert [row.stage for row in reached] == [1, 2]
    assert stage_for(0, fixer) == 0
    assert stage_for(1, fixer) == 1
    assert stage_for(5, fixer) == 2
    assert stage_for(10, fixer) == 3
    assert parse_key("FIXER") == "fixer"
    assert parse_count_reason("10\nручная правка") == (10, "ручная правка")
    typed = (
        AchievementDef(key="fixer", stage=1, threshold=1, name="Фиксер 1", role_id=101),
        AchievementDef(key="fixer", stage=2, threshold=5, name="Фиксер 2", role_id=102),
        AchievementDef(key="fixer", stage=3, threshold=10, name="Фиксер 3", role_id=103),
    )
    add, remove = role_diff((101,), typed, 2)
    assert add == (102,)
    assert remove == (101,)


class _Member:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.roles: list[SimpleNamespace] = []
        self.sent: list[object] = []

    async def send(self, **kwargs: object) -> None:
        self.sent.append(kwargs)


class _Guild:
    def __init__(self) -> None:
        self.id = 1
        self.members: dict[int, _Member] = {}

    def get_member(self, user_id: int) -> _Member | None:
        return self.members.get(int(user_id))


class _FakeRoles:
    def __init__(self, guild: _Guild) -> None:
        self.guild = guild
        self.bound: list[tuple[int, RoleSubsystem, tuple[int, ...]]] = []
        self.claims: list[RoleClaim] = []

    def bind(self, guild_id: int, subsystem: RoleSubsystem, role_ids: object, **_kwargs: object) -> None:
        self.bound.append((guild_id, subsystem, tuple(role_ids)))

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        member = self.guild.get_member(claim.user_id)
        if member is not None:
            have = {int(role.id) for role in member.roles}
            have.update(claim.add)
            have.difference_update(claim.remove)
            member.roles = [SimpleNamespace(id=role_id) for role_id in have]
        return RoleApplyResult(ok=True)


def _bot(sessions, guild: _Guild) -> SimpleNamespace:
    spawned: list[object] = []

    def spawn(coro: object, name: str | None = None) -> None:
        _ = name
        spawned.append(coro)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        spawn=spawn,
        spawned=spawned,
        scheduler=None,
        guilds=(guild,),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11)},
        roles=_FakeRoles(guild),
        get_guild=lambda guild_id: guild if int(guild_id) == guild.id else None,
    )
    bot.reputation = ReputationService(bot)
    bot.achievements = AchievementService(bot)
    return bot


async def _drain(bot: SimpleNamespace) -> None:
    while bot.spawned:
        await bot.spawned.pop(0)


async def _bind_fixer(service: AchievementService) -> None:
    await service.set_role(1, "fixer", 1, FIXER[0])
    await service.set_role(1, "fixer", 2, FIXER[1])
    await service.set_role(1, "fixer", 3, FIXER[2])


def _closed(ticket_id: int) -> TicketClosed:
    return TicketClosed(
        guild_id=1,
        ticket_id=ticket_id,
        owner_id=7,
        category_kind=CategoryKind.BUG,
        outcome=Outcome.FIXED,
        closed_by=2,
    )


async def test_fifth_fixed_bug_promotes_fixer_two(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    member = _Member(7)
    guild.members[7] = member
    bot = _bot(sessions, guild)
    service = bot.achievements
    try:
        await _bind_fixer(service)
        for ticket_id in range(1, 6):
            bot.events.emit(_closed(ticket_id))
            await _drain(bot)
        card = await service.profile(1, 7)
        line = next(item for item in card.lines if item.key == "fixer")
        assert line.count == 5
        assert line.stage == 2
        assert line.stage_name == "Фиксер 2"
        claims = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.ACHIEVEMENTS]
        assert claims
        last = claims[-1]
        assert last.add == (FIXER[1],)
        assert FIXER[0] in last.remove
        async with session_scope(sessions) as session:
            score = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 7)
            )
            kinds = list(
                (
                    await session.scalars(
                        select(ReputationEvent.kind).where(
                            ReputationEvent.guild_id == 1,
                            ReputationEvent.user_id == 7,
                        )
                    )
                ).all()
            )
        assert score == 60
        assert kinds.count("achievement") == 2
        assert any("Фиксер 2" in collected_text(payload["view"]) for payload in member.sent)
        bot.roles.claims.clear()
        bot.events.emit(_closed(5))
        await _drain(bot)
        again = await service.profile(1, 7)
        assert next(item.count for item in again.lines if item.key == "fixer") == 5
        extra = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.ACHIEVEMENTS]
        assert extra == []
        async with session_scope(sessions) as session:
            events = await session.scalar(
                select(func.count())
                .select_from(ReputationEvent)
                .where(
                    ReputationEvent.guild_id == 1,
                    ReputationEvent.user_id == 7,
                    ReputationEvent.kind == "achievement",
                )
            )
        assert events == 2
    finally:
        await engine.dispose()


async def test_manual_set_promotes_then_drops_role_keeps_score(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    member = _Member(7)
    guild.members[7] = member
    bot = _bot(sessions, guild)
    service = bot.achievements
    try:
        await _bind_fixer(service)
        jumped = await service.set_count(1, 7, "fixer", 5, reason="скачок", now=STAMP)
        await _drain(bot)
        assert jumped is not None
        line = next(item for item in jumped.lines if item.key == "fixer")
        assert line.stage == 2
        async with session_scope(sessions) as session:
            mid = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 7)
            )
        assert mid == 60
        await service.set_count(1, 7, "fixer", 10, reason="финиш", now=STAMP)
        await _drain(bot)
        card = await service.profile(1, 7)
        assert next(item.stage for item in card.lines if item.key == "fixer") == 3
        claims = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.ACHIEVEMENTS]
        assert claims[-1].add == (FIXER[2],)
        assert FIXER[1] in claims[-1].remove
        async with session_scope(sessions) as session:
            after = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 7)
            )
        assert after == 65
        await service.set_count(1, 7, "fixer", 0, reason="сброс", now=STAMP)
        await _drain(bot)
        gone = await service.profile(1, 7)
        assert next(item.count for item in gone.lines if item.key == "fixer") == 0
        assert next(item.stage for item in gone.lines if item.key == "fixer") == 0
        assert {int(role.id) for role in member.roles} & set(FIXER) == set()
        async with session_scope(sessions) as session:
            kept = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 7)
            )
            grants = list(
                (
                    await session.scalars(
                        select(AchievementGrant).where(
                            AchievementGrant.guild_id == 1, AchievementGrant.user_id == 7
                        )
                    )
                ).all()
            )
        assert kept == 65
        assert grants
        assert all(row.revoked_at is not None for row in grants)
        await service.set_count(1, 7, "fixer", 5, reason="снова", now=STAMP)
        await _drain(bot)
        async with session_scope(sessions) as session:
            again = await session.scalar(
                select(Reputation.score).where(Reputation.guild_id == 1, Reputation.user_id == 7)
            )
            events = await session.scalar(
                select(func.count())
                .select_from(ReputationEvent)
                .where(
                    ReputationEvent.guild_id == 1,
                    ReputationEvent.user_id == 7,
                    ReputationEvent.kind == "achievement",
                )
            )
        assert again == 65
        assert events == 3
    finally:
        await engine.dispose()


async def test_reconcile_skips_when_roles_already_match(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    member = _Member(7)
    guild.members[7] = member
    bot = _bot(sessions, guild)
    service = bot.achievements
    try:
        await _bind_fixer(service)
        await service.set_count(1, 7, "fixer", 5, now=STAMP)
        await _drain(bot)
        bot.roles.claims.clear()
        done = await service.reconcile_guild(1)
        assert done == 1
        extra = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.ACHIEVEMENTS]
        assert extra == []
        view = grant_view("Фиксер 2", 7)
        text = collected_text(view)
        assert copy.achievement_star("Фиксер 2") in text
        assert "<@7>" in text
        assert KEYS == ("fixer", "ideologist", "vigilant")
    finally:
        await engine.dispose()


async def test_missing_achievement_role_alerts_once_per_guild(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    guild.members[7] = _Member(7)
    guild.members[8] = _Member(8)
    bot = _bot(sessions, guild)
    submits: list[dict[str, object]] = []
    bot.log_pump = SimpleNamespace(submit=lambda _channel, **kwargs: submits.append(kwargs))

    class _Skipping(_FakeRoles):
        async def apply(self, claim: RoleClaim) -> RoleApplyResult:
            self.claims.append(claim)
            return RoleApplyResult(ok=True, skipped=tuple(claim.add))

    bot.roles = _Skipping(guild)
    service = bot.achievements
    try:
        await _bind_fixer(service)
        await service.set_count(1, 7, "fixer", 1, now=STAMP)
        await service.set_count(1, 8, "fixer", 1, now=STAMP)
        await _drain(bot)
        gone = [item for item in submits if copy.ACHIEVEMENT_ROLE_GONE in collected_text(item["view"])]
        assert len(gone) == 1
        text = collected_text(gone[0]["view"])
        assert copy.ACHIEVEMENT_STALE_ACTION in text
        await service.bind_roles(1)
        guild.members[9] = _Member(9)
        await service.set_count(1, 9, "fixer", 1, now=STAMP)
        await _drain(bot)
        gone = [item for item in submits if copy.ACHIEVEMENT_ROLE_GONE in collected_text(item["view"])]
        assert len(gone) == 2
    finally:
        await engine.dispose()


async def test_forget_drops_progress_and_grants(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    try:
        await bot.achievements.set_count(1, 7, "fixer", 5, now=STAMP)
        await _drain(bot)
        privacy = PrivacyService(bot)
        report = await privacy.forget(1, 7)
        assert report.tables["achievement_progress"] == 1
        assert report.tables["achievement_grants"] == 2
        async with session_scope(sessions) as session:
            progress = await session.scalar(
                select(func.count())
                .select_from(AchievementProgress)
                .where(AchievementProgress.guild_id == 1, AchievementProgress.user_id == 7)
            )
            grants = await session.scalar(
                select(func.count())
                .select_from(AchievementGrant)
                .where(AchievementGrant.guild_id == 1, AchievementGrant.user_id == 7)
            )
        assert progress == 0
        assert grants == 0
    finally:
        await engine.dispose()
