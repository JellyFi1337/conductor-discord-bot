from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import MemberRecord, ReputationEvent, ReputationSettings
from conductor.db.session import session_scope
from conductor.domain.moderation import CaseCreated
from conductor.domain.reputation import (
    CEILING,
    FLOOR,
    START,
    AutobanVerdict,
    apply_growth,
    apply_hit,
    autoban_verdict,
    case_hit,
    clamp_score,
    desired_tier,
    is_frozen,
    is_sinner,
    pardon_score,
    score_bar,
    tier_for,
    timeout_hit,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.reputation import dossier_view
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult, RoleClaim, RoleSubsystem

REASON = "нарушение правил чата"
STAMP = datetime(2026, 9, 5, tzinfo=UTC)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def test_score_stays_inside_zero_and_hundred() -> None:
    assert clamp_score(-8) == FLOOR
    assert clamp_score(0) == FLOOR
    assert clamp_score(40) == 40
    assert clamp_score(100) == CEILING
    assert clamp_score(140) == CEILING
    assert apply_hit(score=3, hit=10, hour_used=0).score == FLOOR
    grown = apply_growth(
        score=100,
        unused_messages=50,
        unused_voice=0,
        last_growth_at=None,
        now=STAMP,
    )
    assert grown.delta == 0
    assert grown.score == CEILING


def test_hourly_cap_cuts_the_excess() -> None:
    plan = apply_hit(score=START, hit=10, hour_used=35)
    assert plan.applied == 5
    assert plan.cut == 5
    assert plan.score == 45
    full = apply_hit(score=START, hit=5, hour_used=40)
    assert full.applied == 0
    assert full.cut == 5
    assert full.score == START


def test_timeout_hit_scales_with_duration() -> None:
    assert timeout_hit(60) == 2
    assert timeout_hit(10 * 60) == 2
    assert timeout_hit(20 * 60) == 4
    assert timeout_hit(100 * 60) == 20
    assert timeout_hit(200 * 60) == 20
    assert timeout_hit(None) == 2
    assert case_hit("warn", None) == 5
    assert case_hit("mute", None) == 10
    assert case_hit("kick", None) == 0
    assert case_hit("ban", None) == 0
    assert case_hit("note", None) == 0
    assert case_hit("timeout", 45 * 60) == 8


def _bot(sessions) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        spawn=None,
        scheduler=None,
        guilds=(),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11)},
    )


def _case(*, case_id: int, kind: str = "warn", duration: int | None = None) -> CaseCreated:
    return CaseCreated(
        guild_id=1,
        case_id=case_id,
        number=case_id,
        target_id=3,
        actor_id=2,
        actor_rank=1,
        kind=kind,
        reason=REASON,
        duration_seconds=duration,
        created_at=STAMP,
    )


async def test_same_case_is_not_applied_twice(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    event = _case(case_id=7)
    try:
        first = await service.apply_case(event, now=STAMP)
        second = await service.apply_case(event, now=STAMP)
        dossier = await service.dossier(1, 3, now=STAMP)
        assert first is not None
        assert first.score == 45
        assert second is None
        assert dossier.score == 45
        assert dossier.tier == 2
        assert "▰" in score_bar(dossier.score, dossier.ceiling)
        text = collected_text(dossier_view("<@3>", dossier))
        assert copy.rep_title("<@3>") in text
        assert "**45**/100" in text
    finally:
        await engine.dispose()


async def test_hourly_cap_is_persisted_and_logged(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    logs: list[object] = []
    bot = _bot(sessions)
    bot.log_pump = SimpleNamespace(submit=lambda *_a, **kwargs: logs.append(kwargs.get("view")))
    service = ReputationService(bot)
    try:
        for index in range(8):
            plan = await service.apply_case(_case(case_id=index + 1), now=STAMP)
            assert plan is not None
            assert plan.cut == 0
        ninth = await service.apply_case(_case(case_id=9), now=STAMP)
        assert ninth is not None
        assert ninth.applied == 0
        assert ninth.cut == 5
        dossier = await service.dossier(1, 3, now=STAMP)
        assert dossier.score == 10
        assert logs
        assert copy.REP_CAP_TITLE in collected_text(logs[-1])
    finally:
        await engine.dispose()


async def test_growth_awards_one_point_from_messages(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    joined = STAMP
    try:
        await service.apply_case(_case(case_id=1), now=STAMP)
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=3,
                    username="ann",
                    display_name="Ann",
                    first_joined_at=joined,
                    last_joined_at=joined,
                    message_count=50,
                    voice_seconds=0,
                )
            )
        grown = await service.tick_guild(1, now=STAMP + timedelta(hours=1))
        dossier = await service.dossier(1, 3, now=STAMP + timedelta(hours=1))
        assert grown == 1
        assert dossier.score == 46
        again = await service.tick_guild(1, now=STAMP + timedelta(hours=1, minutes=10))
        assert again == 0
        assert (await service.dossier(1, 3, now=STAMP + timedelta(hours=1, minutes=10))).score == 46
    finally:
        await engine.dispose()


def test_tier_boundaries_and_frozen_does_not_grow() -> None:
    assert [tier_for(score) for score in (9, 10, 29, 30, 49, 50, 69, 70, 89, 90, 100)] == [
        0,
        1,
        1,
        2,
        2,
        3,
        3,
        4,
        4,
        5,
        5,
    ]
    assert is_frozen(0) is True
    assert is_frozen(1) is False
    assert is_sinner(9) is True
    assert is_sinner(10) is False


def test_desired_tier_drops_only_past_margin() -> None:
    assert desired_tier(49, 3) == 3
    assert desired_tier(48, 3) == 3
    assert desired_tier(47, 3) == 2
    assert desired_tier(50, 2) == 3
    grown = apply_growth(
        score=9,
        unused_messages=50,
        unused_voice=0,
        last_growth_at=None,
        now=STAMP,
    )
    assert grown.delta == 0
    assert grown.score == 9
    moved = apply_growth(
        score=10,
        unused_messages=50,
        unused_voice=0,
        last_growth_at=None,
        now=STAMP,
    )
    assert moved.delta == 1
    assert moved.score == 11
    assert pardon_score() == 50
    assert case_hit("kick", None) == 0
    assert (
        autoban_verdict(
            score=0,
            banned_for_reputation=False,
            dry_run=True,
            moderation_open=True,
            punishing=True,
        )
        is AutobanVerdict.DRY
    )
    assert (
        autoban_verdict(
            score=0,
            banned_for_reputation=False,
            dry_run=False,
            moderation_open=True,
            punishing=False,
        )
        is AutobanVerdict.SKIP
    )


class _BanGuild:
    def __init__(self) -> None:
        self.id = 1
        self.bans: list[tuple[int, str | None]] = []
        self.unbans: list[tuple[int, str | None]] = []
        self.members: dict[int, SimpleNamespace] = {}

    def get_member(self, user_id: int) -> SimpleNamespace | None:
        return self.members.get(user_id)

    async def ban(self, user: object, *, reason: str | None = None, delete_message_seconds: int = 0) -> None:
        self.bans.append((int(getattr(user, "id", 0)), reason))

    async def unban(self, user: object, *, reason: str | None = None) -> None:
        self.unbans.append((int(getattr(user, "id", 0)), reason))


class _FakeRoles:
    def __init__(self) -> None:
        self.bound: list[tuple[int, RoleSubsystem, tuple[int, ...]]] = []
        self.claims: list[RoleClaim] = []

    def bind(self, guild_id: int, subsystem: RoleSubsystem, role_ids: object, **_kwargs: object) -> None:
        self.bound.append((guild_id, subsystem, tuple(role_ids)))

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True)


def _open_bot(sessions, guild: _BanGuild) -> SimpleNamespace:
    bot = _bot(sessions)
    bot.roles = _FakeRoles()
    bot.get_guild = lambda _gid: guild
    bot.user = SimpleNamespace(id=99)
    return bot


async def test_zero_in_dry_run_does_not_ban(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    bot = _open_bot(sessions, guild)
    service = ReputationService(bot)
    try:
        panel = await service.update_config(1, warn_hit=100, max_hit_per_hour=200, dry_run=True)
        assert panel is not None
        plan = await service.apply_case(_case(case_id=1), now=STAMP)
        assert plan is not None
        assert plan.score == 0
        assert guild.bans == []
        at, would = await service.diagnose_counts(1)
        assert at == 1
        assert would == 1
    finally:
        await engine.dispose()


async def test_manual_ban_does_not_zero_score(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    service = ReputationService(_open_bot(sessions, guild))
    try:
        plan = await service.apply_case(_case(case_id=1, kind="ban"), now=STAMP)
        assert plan is not None
        assert plan.applied == 0
        assert plan.score == 50
        assert guild.bans == []
        dossier = await service.dossier(1, 3, now=STAMP)
        assert dossier.score == 50
        assert dossier.banned_for_reputation is False
    finally:
        await engine.dispose()


async def test_tier_change_is_one_broker_claim_and_reconcile_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    target = SimpleNamespace(id=3, roles=())
    guild.members[3] = target
    bot = _open_bot(sessions, guild)
    service = ReputationService(bot)
    try:
        await service.update_config(1, warn_hit=5, max_hit_per_hour=200)
        for index, role_id in enumerate((70, 71, 72, 73, 74, 75)):
            await service.set_tier_role(1, index, role_id)
        first = await service.dossier(1, 3, now=STAMP)
        assert first.score == 50
        assert first.tier == 3
        assert any(claim.add == (73,) and set(claim.remove) == {70, 71, 72, 74, 75} for claim in bot.roles.claims)
        target.roles = (SimpleNamespace(id=73),)
        bot.roles.claims.clear()
        await service.reconcile_member(1, 3)
        assert bot.roles.claims == []
        plan = await service.apply_case(_case(case_id=1), now=STAMP)
        assert plan is not None
        assert plan.score == 45
        assert plan.applied == 5
        claims = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.REPUTATION]
        assert len(claims) == 1
        assert claims[0].add == (72,)
        assert set(claims[0].remove) == {70, 71, 73, 74, 75}
        assert claims[0].urgent is False
        kick = await service.apply_case(_case(case_id=2, kind="kick"), now=STAMP)
        assert kick is not None
        assert kick.applied == 0
        assert kick.score == 45
    finally:
        await engine.dispose()


async def test_missing_tier_role_alerts_once_per_guild(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    guild.members[3] = SimpleNamespace(id=3, roles=())
    guild.members[4] = SimpleNamespace(id=4, roles=())
    bot = _open_bot(sessions, guild)
    submits: list[dict[str, object]] = []
    bot.log_pump = SimpleNamespace(submit=lambda _channel, **kwargs: submits.append(kwargs))

    class _Skipping(_FakeRoles):
        async def apply(self, claim: RoleClaim) -> RoleApplyResult:
            self.claims.append(claim)
            return RoleApplyResult(ok=True, skipped=tuple(claim.add))

    bot.roles = _Skipping()
    service = ReputationService(bot)
    try:
        await service.update_config(1, warn_hit=5, max_hit_per_hour=200)
        for index, role_id in enumerate((70, 71, 72, 73, 74, 75)):
            await service.set_tier_role(1, index, role_id)
        await service.dossier(1, 3, now=STAMP)
        await service.dossier(1, 4, now=STAMP)
        assert len(submits) == 1
        text = collected_text(submits[0]["view"])
        assert copy.REP_ROLE_GONE in text
        assert copy.REP_STALE_ACTION in text
        await service.bind_tiers(1)
        guild.members[5] = SimpleNamespace(id=5, roles=())
        await service.dossier(1, 5, now=STAMP)
        assert len(submits) == 2
    finally:
        await engine.dispose()


async def test_pardon_restores_score_and_unbans_only_own_ban(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    guild.members[3] = SimpleNamespace(id=3, roles=())
    bot = _open_bot(sessions, guild)
    service = ReputationService(bot)
    try:
        await service.update_config(1, warn_hit=100, max_hit_per_hour=200, dry_run=False)
        plan = await service.apply_case(_case(case_id=1), now=STAMP)
        assert plan is not None
        assert plan.score == 0
        assert len(guild.bans) == 1
        assert guild.bans[0][1] == "репутация 0"
        first = await service.pardon(guild, 3, actor_id=2, now=STAMP)
        assert first.score == 50
        assert first.own_ban is True
        assert first.unbanned is True
        assert len(guild.unbans) == 1
        other = await service.pardon(guild, 4, actor_id=2, now=STAMP)
        assert other.score == 50
        assert other.own_ban is False
        assert other.unbanned is False
        assert len(guild.unbans) == 1
    finally:
        await engine.dispose()


async def test_existing_settings_keep_old_start_until_diagnose_apply(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    try:
        async with session_scope(sessions) as session:
            session.add(ReputationSettings(guild_id=1, start=100, kick_hit=25))
        panel = await service.show_config(1)
        assert panel.config.start == 100
        assert panel.config.kick_hit == 25
        assert await service.defaults_drift(1) is True
        assert await service.apply_new_defaults(1) is True
        after = await service.show_config(1)
        assert after.config.start == 50
        assert after.config.kick_hit == 0
        assert await service.defaults_drift(1) is False
        dossier = await service.dossier(1, 3, now=STAMP)
        assert dossier.score == 50
        assert dossier.tier == 3
    finally:
        await engine.dispose()


async def test_fall_to_tier_zero_is_urgent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    guild.members[3] = SimpleNamespace(id=3, roles=(SimpleNamespace(id=73),))
    bot = _open_bot(sessions, guild)
    service = ReputationService(bot)
    try:
        await service.update_config(1, warn_hit=50, max_hit_per_hour=200)
        for index, role_id in enumerate((70, 71, 72, 73, 74, 75)):
            await service.set_tier_role(1, index, role_id)
        bot.roles.claims.clear()
        plan = await service.apply_case(_case(case_id=1), now=STAMP)
        assert plan is not None
        assert plan.score == 0
        claims = [claim for claim in bot.roles.claims if claim.subsystem is RoleSubsystem.REPUTATION]
        assert len(claims) == 1
        assert claims[0].add == (70,)
        assert claims[0].urgent is True
    finally:
        await engine.dispose()


async def test_refund_returns_applied_and_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    try:
        plan = await service.apply_case(_case(case_id=4), now=STAMP)
        assert plan is not None
        assert plan.applied == 5
        first = await service.refund(4, now=STAMP)
        assert first == 5
        dossier = await service.dossier(1, 3, now=STAMP)
        assert dossier.score == START
        second = await service.refund(4, now=STAMP)
        assert second == 5
        again = await service.dossier(1, 3, now=STAMP)
        assert again.score == START
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(ReputationEvent).where(ReputationEvent.case_id == 4))).all()
        kinds = sorted(row.kind for row in rows)
        assert kinds == ["revoke", "warn"]
    finally:
        await engine.dispose()


async def test_refund_of_capped_hit_returns_only_applied(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    try:
        for index in range(8):
            await service.apply_case(_case(case_id=index + 1), now=STAMP)
        ninth = await service.apply_case(_case(case_id=9), now=STAMP)
        assert ninth is not None
        assert ninth.applied == 0
        assert ninth.cut == 5
        refunded = await service.refund(9, now=STAMP)
        assert refunded == 0
    finally:
        await engine.dispose()


async def test_two_cases_same_second_stack_hits(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    try:
        first, second = await asyncio.gather(
            service.apply_case(_case(case_id=1), now=STAMP),
            service.apply_case(_case(case_id=2), now=STAMP),
        )
        dossier = await service.dossier(1, 3, now=STAMP)
        assert first is not None and second is not None
        assert {first.score, second.score} == {45, 40}
        assert dossier.score == 40
        async with session_scope(sessions) as session:
            events = list((await session.scalars(select(ReputationEvent))).all())
        after = sorted(int(row.score_after) for row in events)
        assert after == [40, 45]
    finally:
        await engine.dispose()


async def test_set_score_gives_and_takes(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = ReputationService(_bot(sessions))
    try:
        await service.dossier(1, 3, now=STAMP)
        before, after = await service.set_score(1, 3, 20, actor_id=9, now=STAMP)
        assert (before, after) == (START, 20)
        before, after = await service.set_score(1, 3, 80, actor_id=9, now=STAMP)
        assert (before, after) == (20, 80)
        before, after = await service.set_score(1, 3, 80, actor_id=9, now=STAMP)
        assert (before, after) == (80, 80)
        before, after = await service.set_score(1, 3, 999, actor_id=9, now=STAMP)
        assert (before, after) == (80, CEILING)
        assert copy.owner_rep_body(before=50, after=20) == "Было 50, стало 20."
        assert copy.OWNER_REP_SAME.format(score=80) in copy.owner_rep_body(before=80, after=80)
        async with session_scope(sessions) as session:
            events = list((await session.scalars(select(ReputationEvent))).all())
        kinds = [str(row.kind) for row in events]
        assert kinds == ["owner", "owner", "owner"]
        assert [int(row.delta) for row in events] == [-30, 60, 20]
    finally:
        await engine.dispose()
