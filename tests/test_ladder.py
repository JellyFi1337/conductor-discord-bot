from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.cogs.diagnostics import (
    CheckStatus,
    DiagnoseSnapshot,
    LogChannelView,
    ManagedRoleView,
    build_diagnose_lines,
)
from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import LadderProgress, LadderSettings, MemberDaily, MemberRecord, Reputation, VoiceSession
from conductor.db.session import session_scope
from conductor.domain.ladder import (
    DEFAULT_CONFIG,
    HOUR,
    LadderRoles,
    LadderStats,
    Step,
    blocking_gap,
    desired_step,
    earned_step,
    next_need,
    parse_ladder_config,
    role_plan,
)
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.domain.reputation import ScoreChanged
from conductor.presentation import copy
from conductor.presentation.ladder import me_view
from conductor.presentation.layout import collected_text
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.ladder import LadderService
from conductor.services.role_broker import RoleApplyResult, RoleClaim, RoleSubsystem

STAMP = datetime(2026, 9, 5, tzinfo=UTC)
JOINED = STAMP - timedelta(days=120)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _at(days: int) -> datetime:
    return STAMP - timedelta(days=days)


def test_customer_thresholds_table() -> None:
    cases = (
        (0, 0, 0, 0, Step.GUEST),
        (49, 0, 0, 400, Step.GUEST),
        (50, 0, 0, 0, Step.REGULAR),
        (600, 0, 0, 400, Step.REGULAR),
        (500, 50 * HOUR, 0, 29, Step.REGULAR),
        (500, 50 * HOUR, 0, 30, Step.RESIDENT),
        (500, 50 * HOUR, 0, 31, Step.RESIDENT),
        (1000, 100 * HOUR, 0, 364, Step.RESIDENT),
        (1000, 100 * HOUR, 0, 365, Step.FRIEND),
        (0, 10 * HOUR, 0, 400, Step.GUEST),
        (0, 0, 999, 400, Step.GUEST),
        (5000, 0, 0, 400, Step.REGULAR),
    )
    for messages, voice, reactions, days, expected in cases:
        stats = LadderStats(messages=messages, voice_seconds=voice, reactions=reactions)
        assert earned_step(stats, DEFAULT_CONFIG, now=STAMP, joined_at=_at(days)) is expected, (
            messages,
            voice,
            reactions,
            days,
            expected,
        )


def test_zero_threshold_does_not_open_a_path() -> None:
    voice_only = earned_step(LadderStats(voice_seconds=10 * HOUR), DEFAULT_CONFIG, now=STAMP, joined_at=_at(400))
    assert voice_only is Step.GUEST
    reactions_only = earned_step(LadderStats(reactions=999), DEFAULT_CONFIG, now=STAMP, joined_at=_at(400))
    assert reactions_only is Step.GUEST
    strong_off = earned_step(LadderStats(messages=5000), DEFAULT_CONFIG, now=STAMP, joined_at=_at(400))
    assert strong_off is Step.REGULAR
    parsed = parse_ladder_config(DEFAULT_CONFIG, regular_voice_minutes=0, resident_days=0, friend_reactions=0)
    assert parsed is not None
    assert parsed.regular_voice_seconds == 0
    assert parsed.resident_days == 0
    no_days = earned_step(
        LadderStats(messages=500, voice_seconds=50 * HOUR),
        parsed,
        now=STAMP,
        joined_at=_at(1),
    )
    assert no_days is Step.RESIDENT
    friend_by_react = earned_step(
        LadderStats(messages=1000, reactions=9999),
        DEFAULT_CONFIG,
        now=STAMP,
        joined_at=_at(365),
    )
    assert friend_by_react is Step.REGULAR


def test_sinner_freezes_step() -> None:
    earned = Step.RESIDENT
    held = Step.REGULAR
    assert desired_step(earned=earned, held=held, sinner=False) is Step.RESIDENT
    assert desired_step(earned=earned, held=held, sinner=True) is Step.REGULAR
    assert desired_step(earned=Step.FRIEND, held=Step.GUEST, sinner=True) is Step.GUEST


def test_no_automatic_demotion() -> None:
    assert desired_step(earned=Step.GUEST, held=Step.RESIDENT, sinner=False) is Step.RESIDENT
    assert desired_step(earned=Step.REGULAR, held=Step.FRIEND, sinner=False) is Step.FRIEND
    assert desired_step(earned=Step.GUEST, held=Step.RESIDENT, sinner=True) is Step.RESIDENT


def test_role_plan_is_idempotent() -> None:
    roles = LadderRoles(guest=10, regular=11, resident=12, friend=13)
    first_add, first_remove = role_plan(have=(10,), desired=Step.REGULAR, roles=roles)
    assert first_add == (11,)
    assert first_remove == (10,)
    again_add, again_remove = role_plan(have=(11,), desired=Step.REGULAR, roles=roles)
    assert again_add == ()
    assert again_remove == ()


def test_remainder_is_honest() -> None:
    stats = LadderStats(messages=612, voice_seconds=41 * HOUR)
    need = next_need(Step.REGULAR, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED, sinner=False)
    assert need.step is Step.RESIDENT
    assert need.messages_need == 500
    assert need.voice_need == 50 * HOUR
    assert need.tenure_need == 30
    gap = blocking_gap(Step.REGULAR, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED)
    assert gap is not None
    assert gap.kind.value == "voice"
    assert gap.remaining == 9 * HOUR
    frozen = next_need(Step.REGULAR, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED, sinner=True)
    assert frozen.frozen is True
    top = next_need(Step.FRIEND, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED, sinner=False)
    assert top.step is None
    assert top.gap is None


def test_activity_card_shows_remainder() -> None:
    stats = LadderStats(messages=612, voice_seconds=41 * HOUR)
    need = next_need(Step.REGULAR, stats, DEFAULT_CONFIG, now=STAMP, joined_at=JOINED, sinner=False)
    card = SimpleNamespace(
        user_id=3,
        held=Step.REGULAR,
        earned=Step.REGULAR,
        desired=Step.REGULAR,
        sinner=False,
        stats=stats,
        need=need,
        tenure_days=120,
    )
    text = collected_text(me_view("<@3>", card))  # type: ignore[arg-type]
    assert copy.LADDER_STEP_REGULAR in text
    assert "612/500" in text
    assert "41/50" in text
    assert copy.ladder_tenure_line(have=120, need=30) in text
    assert copy.LADDER_STEP_RESIDENT in text
    assert copy.LADDER_NO_DEMOTION in text
    assert copy.LADDER_REACTIONS not in text
    assert "None" not in text


def test_diagnose_ladder_role_above_bot_is_fail() -> None:
    snap = DiagnoseSnapshot(
        members_intent=True,
        message_content_intent=True,
        members_loaded=10,
        member_count=10,
        permissions={item.attr: True for item in REQUIRED_PERMISSIONS},
        bot_top_position=5,
        managed_roles=(ManagedRoleView("Ruby Old", 4),),
        user_log=LogChannelView(configured=True, name="logs", can_send=True, can_embed=True),
        admin_log=LogChannelView(configured=True, name="logs", can_send=True, can_embed=True),
        bot_mode="live",
        guild_served=True,
        latency_ms=42,
        log_backlog=0,
        log_sent=3,
        log_failed=0,
        log_dropped=0,
        log_pump_alive=True,
        unread_count=0,
        unread_names=(),
        ladder_roles=(ManagedRoleView(copy.LADDER_STEP_REGULAR, 8),),
    )
    lines = build_diagnose_lines(snap)
    fails = [line for line in lines if line.status is CheckStatus.FAIL]
    assert any("лестницы" in line.title for line in fails)
    assert any(line.action == copy.ROLE_BELOW_ACTION for line in fails)


class _Member:
    def __init__(self, user_id: int, role_ids: tuple[int, ...]) -> None:
        self.id = user_id
        self.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]


class _Guild:
    def __init__(self) -> None:
        self.id = 1
        self.members: dict[int, _Member] = {3: _Member(3, (10,))}

    def get_member(self, user_id: int) -> _Member | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=role_id, position=role_id)


class _FakeRoles:
    def __init__(self, guild: _Guild) -> None:
        self.guild = guild
        self.bound: list[tuple[int, RoleSubsystem, tuple[int, ...]]] = []
        self.claims: list[RoleClaim] = []
        self.calls = 0

    def bind(self, guild_id: int, subsystem: RoleSubsystem, role_ids: object) -> None:
        self.bound.append((guild_id, subsystem, tuple(role_ids)))

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        self.calls += 1
        member = self.guild.get_member(claim.user_id)
        if member is not None:
            have = {role.id for role in member.roles}
            have.update(claim.add)
            have.difference_update(claim.remove)
            member.roles = [SimpleNamespace(id=role_id) for role_id in have]
        return RoleApplyResult(ok=True, called=bool(claim.add or claim.remove), add=claim.add, remove=claim.remove)


class _SkippingRoles(_FakeRoles):
    def __init__(self, guild: _Guild, missing: frozenset[int]) -> None:
        super().__init__(guild)
        self.missing = missing

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        self.calls += 1
        skipped = tuple(role_id for role_id in dict.fromkeys((*claim.add, *claim.remove)) if role_id in self.missing)
        add = tuple(role_id for role_id in claim.add if role_id not in self.missing)
        remove = tuple(role_id for role_id in claim.remove if role_id not in self.missing)
        member = self.guild.get_member(claim.user_id)
        if member is not None:
            have = {role.id for role in member.roles}
            have.update(add)
            have.difference_update(remove)
            member.roles = [SimpleNamespace(id=role_id) for role_id in have]
        return RoleApplyResult(
            ok=True,
            called=bool(add or remove),
            add=add,
            remove=remove,
            skipped=skipped,
        )


def _bot(sessions, guild: _Guild) -> SimpleNamespace:
    roles = _FakeRoles(guild)
    submits: list[dict[str, object]] = []

    def submit(_channel_id: object, **kwargs: object) -> None:
        submits.append(kwargs)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        spawn=None,
        scheduler=None,
        queue=None,
        guilds=(guild,),
        roles=roles,
        get_guild=lambda _gid: guild,
        log_pump=SimpleNamespace(submit=submit),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11, user_log_channel_id=10)},
        submits=submits,
    )
    return bot


async def _seed_messages(
    sessions,
    *,
    messages: int,
    voice: int = 0,
    reactions: int = 0,
    user_id: int = 3,
) -> None:
    async with session_scope(sessions) as session:
        session.add(
            MemberRecord(
                guild_id=1,
                user_id=user_id,
                username="ann",
                display_name="Ann",
                first_joined_at=JOINED,
                last_joined_at=JOINED,
            )
        )
        session.add(
            MemberDaily(
                guild_id=1,
                user_id=user_id,
                day=STAMP,
                messages=messages,
                voice_seconds=voice,
                reactions=reactions,
            )
        )
        if voice:
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=user_id,
                    channel_id=99,
                    joined_at=STAMP - timedelta(seconds=voice),
                    left_at=STAMP,
                    duration_seconds=voice,
                    last_event_at=STAMP,
                )
            )


async def test_repeat_reconcile_does_not_call_broker_twice(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        await service.update_config(1, guest_role_id=10, regular_role_id=11)
        await _seed_messages(sessions, messages=80)
        changed = await service.reconcile(1, 3, now=STAMP)
        assert changed
        assert bot.roles.calls == 1
        assert bot.roles.claims[0].subsystem is RoleSubsystem.ACTIVITY
        again = await service.reconcile(1, 3, now=STAMP)
        assert again == ()
        assert bot.roles.calls == 1
    finally:
        await engine.dispose()


async def test_missing_step_role_alerts_once_and_still_stores(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    guild.members[4] = _Member(4, (10,))
    bot = _bot(sessions, guild)
    bot.roles = _SkippingRoles(guild, frozenset({11}))
    service = LadderService(bot)
    try:
        await service.update_config(1, guest_role_id=10, regular_role_id=11)
        await _seed_messages(sessions, messages=80, user_id=3)
        await _seed_messages(sessions, messages=80, user_id=4)
        first = await service.reconcile(1, 3, now=STAMP)
        second = await service.reconcile(1, 4, now=STAMP)
        assert first
        assert second
        assert {role.id for role in guild.members[3].roles} == set()
        assert {role.id for role in guild.members[4].roles} == set()
        async with session_scope(sessions) as session:
            steps = {
                int(row.user_id): str(row.step)
                for row in (await session.scalars(select(LadderProgress))).all()
            }
        assert steps == {3: "regular", 4: "regular"}
        assert len(bot.submits) == 1
        text = collected_text(bot.submits[0]["view"])
        assert copy.LADDER_ROLE_GONE in text
        assert copy.LADDER_STALE_ACTION in text
        await service.bind_roles(1)
        guild.members[5] = _Member(5, (10,))
        await _seed_messages(sessions, messages=80, user_id=5)
        await service.reconcile(1, 5, now=STAMP)
        assert len(bot.submits) == 2
    finally:
        await engine.dispose()


async def test_sinner_blocks_promotion(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        await service.update_config(1, guest_role_id=10, regular_role_id=11)
        await _seed_messages(sessions, messages=80)
        async with session_scope(sessions) as session:
            session.add(Reputation(guild_id=1, user_id=3, score=5))
        changed = await service.reconcile(1, 3, now=STAMP)
        assert changed == ()
        assert bot.roles.calls == 0
        assert {role.id for role in guild.members[3].roles} == {10}
        card = await service.card(1, 3, now=STAMP)
        assert card.sinner is True
        assert card.held is Step.GUEST
        assert card.earned is Step.REGULAR
        assert card.desired is Step.GUEST
        assert copy.LADDER_FROZEN in copy.ladder_footnote(card)
    finally:
        await engine.dispose()


async def test_no_demotion_when_counters_drop(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    guild.members[3] = _Member(3, (12,))
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        await service.update_config(1, guest_role_id=10, regular_role_id=11, resident_role_id=12)
        await _seed_messages(sessions, messages=1)
        changed = await service.reconcile(1, 3, now=STAMP)
        assert changed == ()
        assert bot.roles.calls == 0
        assert {role.id for role in guild.members[3].roles} == {12}
        card = await service.card(1, 3, now=STAMP)
        assert card.held is Step.RESIDENT
        assert card.earned is Step.GUEST
        assert card.desired is Step.RESIDENT
    finally:
        await engine.dispose()


async def test_restore_enqueues_from_database_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        await _seed_messages(sessions, messages=80)
        first = await service.restore()
        assert first == 1
        assert len(service._jobs) == 1
        second = await service.restore()
        assert second == 1
        assert len(service._jobs) == 1
    finally:
        await engine.dispose()


async def test_unique_queue_coalesces_two_enqueues(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        await service.update_config(1, guest_role_id=10, regular_role_id=11)
        await _seed_messages(sessions, messages=80)
        service.enqueue(1, 3)
        service.enqueue(1, 3)
        done = await service.drain(now=STAMP)
        assert done == 1
        assert bot.roles.calls == 1
        bot.events.emit(ScoreChanged(guild_id=1, user_id=3, score=80, sinner=False))
        assert len(service._jobs) == 1
    finally:
        await engine.dispose()


async def test_existing_settings_keep_old_until_diagnose_apply(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _Guild()
    bot = _bot(sessions, guild)
    service = LadderService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(
                LadderSettings(
                    guild_id=1,
                    regular_messages=50,
                    regular_voice_seconds=1800,
                    regular_reactions=20,
                    resident_messages=1000,
                    resident_voice_seconds=180000,
                    resident_days=30,
                    resident_strong_messages=5000,
                    resident_strong_voice_seconds=720000,
                    friend_tenure_days=90,
                    friend_messages=3000,
                    friend_voice_seconds=360000,
                    friend_reactions=500,
                )
            )
        assert await service.defaults_drift(1) is True
        panel = await service.show_config(1)
        assert panel.config.regular_voice_seconds == 1800
        assert panel.config.resident_messages == 1000
        assert panel.config.friend_tenure_days == 90
        await _seed_messages(sessions, messages=0, voice=30 * 60)
        old = await service.card(1, 3, now=STAMP)
        assert old.earned is Step.REGULAR
        assert await service.apply_new_defaults(1) is True
        assert await service.defaults_drift(1) is False
        fresh = await service.show_config(1)
        assert fresh.config.regular_voice_seconds == 0
        assert fresh.config.resident_messages == 500
        assert fresh.config.resident_days == 30
        assert fresh.config.friend_tenure_days == 365
        after = await service.card(1, 3, now=STAMP)
        assert after.earned is Step.GUEST
    finally:
        await engine.dispose()
