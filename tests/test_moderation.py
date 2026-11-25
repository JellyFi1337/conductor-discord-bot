from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ChannelLock, ModCase, ModIntent, ModSettings
from conductor.db.session import session_scope
from conductor.domain.access import (
    MOD_BAN,
    AccessSnapshot,
    PermissionRule,
    Rank,
    StaffTargetBlock,
    empty_rank_roles,
)
from conductor.domain.moderation import (
    CaseCreated,
    CaseKind,
    ConfirmVerdict,
    DurationVerdict,
    ModOutcome,
    ModVerdict,
    bot_can_enact,
    can_appeal,
    confirm_intent,
    history_pages,
    is_unlock_duration,
    may_moderate,
    may_revoke,
    message_too_old,
    needs_confirmation,
    parse_timeout_duration,
    pick_purge_batch,
    protected_log_channels,
    purge_channel_blocked,
    revoke_plan,
    sanitize_reason,
    timeout_still_ours,
)
from conductor.domain.quotas import day_start
from conductor.domain.verification import TRAP_REASON
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.moderation import outcome_view
from conductor.presentation.motion import Progress
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.moderation import ModerationService, _count_used
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult, RoleSubsystem
from conductor.services.watchers import cases_this_week

WATCHER_ROLE = 11
SENIOR_ROLE = 22
CHIEF_ROLE = 33
REASON = "нарушение правил чата"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _member(user_id: int, role_ids: tuple[int, ...] = (), *, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )


def _guild(guild_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=guild_id)


def _ranked_snapshot() -> AccessSnapshot:
    return AccessSnapshot(
        rank_roles=empty_rank_roles()
        | {Rank.WATCHER: (WATCHER_ROLE,), Rank.SENIOR: (SENIOR_ROLE,), Rank.CHIEF: (CHIEF_ROLE,)},
        rules={},
        special_locked=True,
    )


def test_reason_shorter_than_eight_is_rejected() -> None:
    assert sanitize_reason("1234567") is None
    assert sanitize_reason("  short ") is None
    assert sanitize_reason(REASON) == REASON


def test_zero_and_snyat_unlock_the_channel() -> None:
    assert is_unlock_duration("0") is True
    assert is_unlock_duration("снять") is True
    assert is_unlock_duration(" Снять ") is True
    assert is_unlock_duration("0m") is False
    assert needs_confirmation(CaseKind.SOFTBAN) is False
    assert needs_confirmation(CaseKind.BAN) is True


def test_timeout_over_28_days_is_too_long() -> None:
    verdict, duration = parse_timeout_duration("29d")
    assert verdict is DurationVerdict.TOO_LONG
    assert duration == timedelta(days=29)
    ok, exact = parse_timeout_duration("28d")
    assert ok is DurationVerdict.OK
    assert exact == timedelta(days=28)
    assert parse_timeout_duration("40321")[0] is DurationVerdict.TOO_LONG


def test_watcher_cannot_moderate_a_senior() -> None:
    blocked = may_moderate(
        actor_rank=Rank.WATCHER,
        target_rank=Rank.SENIOR,
        target_is_bot=False,
        kind=CaseKind.WARN,
    )
    assert blocked is StaffTargetBlock.HIERARCHY
    note = may_moderate(
        actor_rank=Rank.WATCHER,
        target_rank=Rank.SENIOR,
        target_is_bot=False,
        kind=CaseKind.NOTE,
    )
    assert note is None


def test_history_pages_ten_per_page() -> None:
    assert history_pages(0) == 1
    assert history_pages(10) == 1
    assert history_pages(11) == 2


def _bot(sessions) -> SimpleNamespace:
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        guard=None,
        outbound=None,
        roles=_FakeRoles(),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={
            1: SimpleNamespace(user_log_channel_id=10, admin_log_channel_id=11),
        },
        queue=SimpleNamespace(schedule_later=lambda *_a, **_k: None, cancel_later=lambda *_a, **_k: None),
        get_guild=lambda _gid: None,
    )
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), _ranked_snapshot())
    return bot


class _FakeRoles:
    def __init__(self) -> None:
        self.bound: list[tuple[int, RoleSubsystem, tuple[int, ...]]] = []
        self.claims: list[object] = []

    def bind(self, guild_id: int, subsystem: RoleSubsystem, role_ids: object) -> None:
        self.bound.append((guild_id, subsystem, tuple(role_ids)))

    async def apply(self, claim: object) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True)


async def test_short_reason_does_not_open_a_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    seen: list[CaseCreated] = []
    bot.events.subscribe(CaseCreated, seen.append)
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.WARN,
            reason="short",
        )
        assert outcome.verdict is ModVerdict.REASON
        assert outcome.case is None
        assert seen == []
        text = collected_text(outcome_view(outcome))
        assert copy.MOD_REASON_SHORT in text
    finally:
        await engine.dispose()


async def test_timeout_over_28_days_is_explained_without_discord(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    seen: list[CaseCreated] = []
    bot.events.subscribe(CaseCreated, seen.append)
    calls: list[object] = []

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        calls.append((duration, reason))

    target = _member(3)
    target.timeout = timeout
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="29d",
        )
        assert outcome.verdict is ModVerdict.TOO_LONG
        assert calls == []
        assert seen == []
        text = collected_text(outcome_view(outcome))
        assert copy.MOD_TIMEOUT_TOO_LONG in text
        assert copy.mod_denied("too_long") == copy.MOD_TIMEOUT_TOO_LONG
    finally:
        await engine.dispose()


async def test_watcher_cannot_touch_a_senior(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    seen: list[CaseCreated] = []
    bot.events.subscribe(CaseCreated, seen.append)
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3, (SENIOR_ROLE,)),
            kind=CaseKind.WARN,
            reason=REASON,
        )
        assert outcome.verdict is ModVerdict.HIERARCHY
        assert seen == []
        text = collected_text(outcome_view(outcome))
        assert copy.HIERARCHY_DENIED in text
    finally:
        await engine.dispose()


async def test_case_created_emitted_once_per_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    seen: list[CaseCreated] = []
    bot.events.subscribe(CaseCreated, seen.append)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        first = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.WARN,
            reason=REASON,
            now=stamp,
        )
        assert first.verdict is ModVerdict.OK
        assert first.case is not None
        assert first.case.number == 1
        assert len(seen) == 1
        event = seen[0]
        assert event.case_id == first.case.id
        assert event.kind == CaseKind.WARN
        assert event.reason == REASON
        assert event.target_id == 3
        assert event.actor_id == 2
        second = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.NOTE,
            reason=REASON,
            now=stamp,
        )
        assert second.verdict is ModVerdict.OK
        assert second.case is not None
        assert second.case.number == 2
        assert len(seen) == 2
        history = await service.history(1, 3)
        assert [item.number for item in history] == [2, 1]
    finally:
        await engine.dispose()


async def test_system_apply_skips_hierarchy_and_sends_dm1(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    dms: list[tuple[int, bool, int]] = []

    class _Evidence:
        async def deliver_dm1(self, guild: object, case: object, target: object, *, force: bool = False) -> bool:
            dms.append((int(case.id), force, int(target.id)))
            return True

    bot.evidence = _Evidence()
    timed: list[object] = []

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        timed.append((duration, reason))

    target = _member(3, (SENIOR_ROLE,))
    target.timeout = timeout
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(10),
            target,
            kind=CaseKind.TIMEOUT,
            reason="автомод: emoji_flood (11 за 60)",
            duration_raw="10м",
            system=True,
        )
        assert outcome.verdict is ModVerdict.OK
        assert outcome.case is not None
        assert outcome.case.created_by == 10
        assert outcome.case.actor_rank == int(Rank.SPECIAL)
        assert timed
        assert "по команде 10" in str(timed[0][1])
        assert dms == [(outcome.case.id, True, 3)]
    finally:
        await engine.dispose()


async def test_mute_without_role_is_a_denial(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    seen: list[CaseCreated] = []
    bot.events.subscribe(CaseCreated, seen.append)
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.MUTE,
            reason=REASON,
        )
        assert outcome.verdict is ModVerdict.MUTE_ROLE
        assert seen == []
        assert bot.roles.claims == []
        text = collected_text(outcome_view(outcome))
        assert copy.MOD_NO_MUTE_ROLE in text
    finally:
        await engine.dispose()


def test_log_channels_are_excluded_from_purge() -> None:
    protected = protected_log_channels(10, 11)
    assert purge_channel_blocked(10, protected) is True
    assert purge_channel_blocked(11, protected) is True
    assert purge_channel_blocked(12, protected) is False


def test_messages_older_than_14_days_are_cut() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    old = now - timedelta(days=14)
    older = now - timedelta(days=15)
    fresh = now - timedelta(days=1)
    assert message_too_old(old, now) is True
    assert message_too_old(older, now) is True
    assert message_too_old(fresh, now) is False
    picked = pick_purge_batch(
        (
            (3, fresh, 8),
            (2, old, 8),
            (1, older, 8),
        ),
        now=now,
    )
    assert picked.delete_ids == (3,)
    assert picked.skipped_old == 1
    assert picked.stop is True
    assert copy.MOD_PURGE_FOOTNOTE


def test_watcher_cannot_kick_a_senior() -> None:
    blocked = may_moderate(
        actor_rank=Rank.WATCHER,
        target_rank=Rank.SENIOR,
        target_is_bot=False,
        kind=CaseKind.KICK,
    )
    assert blocked is StaffTargetBlock.HIERARCHY


class _BanGuild:
    def __init__(self) -> None:
        self.id = 1
        self.bans: list[tuple[int, int, str | None]] = []
        self.members: dict[int, object] = {}

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)

    async def ban(self, target: object, delete_message_seconds: int = 0, reason: str | None = None) -> None:
        self.bans.append((int(target.id), delete_message_seconds, reason))

    async def unban(self, target: object, reason: str | None = None) -> None:
        return None


async def test_second_confirm_does_not_ban_twice(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _BanGuild()
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        prepared = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=stamp)
        assert prepared.verdict is ModVerdict.OK
        assert prepared.pending_id is not None
        first = await service.confirm_heavy(guild, prepared.pending_id, now=stamp)
        assert first.verdict is ModVerdict.OK
        assert first.case is not None
        assert len(guild.bans) == 1
        second = await service.confirm_heavy(guild, prepared.pending_id, now=stamp)
        assert second.verdict is ModVerdict.SPENT
        assert len(guild.bans) == 1
        assert confirm_intent(consumed_at=stamp, created_at=stamp, now=stamp) is ConfirmVerdict.SPENT
    finally:
        await engine.dispose()


async def test_confirm_heavy_preflight_blocks_higher_role(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    bot.user = SimpleNamespace(id=99)
    guild = _BanGuild()
    guild.me = SimpleNamespace(
        id=99,
        top_role=SimpleNamespace(position=2),
        guild_permissions=SimpleNamespace(ban_members=True, kick_members=True),
    )
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)
    target.top_role = SimpleNamespace(position=10)
    guild.members[2] = actor
    guild.members[3] = target
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        prepared = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=stamp)
        assert prepared.verdict is ModVerdict.POWER
        assert prepared.pending_id is None
        assert guild.bans == []
        async with session_scope(sessions) as session:
            assert (await session.scalar(select(ModCase))) is None
            assert (await session.scalar(select(ModIntent))) is None
    finally:
        await engine.dispose()


async def test_confirm_heavy_failed_ban_revokes_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    bot.user = SimpleNamespace(id=99)
    dms: list[object] = []

    async def deliver_dm1(*_args: object, **_kwargs: object) -> bool:
        return True

    async def send(**kwargs: object) -> None:
        dms.append(kwargs)

    bot.evidence = SimpleNamespace(deliver_dm1=deliver_dm1, open_channel=None)
    guild = _BanGuild()
    guild.me = SimpleNamespace(
        id=99,
        top_role=SimpleNamespace(position=10),
        guild_permissions=SimpleNamespace(ban_members=True, kick_members=True),
    )

    async def boom(target: object, delete_message_seconds: int = 0, reason: str | None = None) -> None:
        raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "forbidden")

    guild.ban = boom  # type: ignore[method-assign]
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)
    target.top_role = SimpleNamespace(position=1)
    target.send = send
    guild.members[2] = actor
    guild.members[3] = target
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        prepared = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=stamp)
        assert prepared.pending_id is not None
        outcome = await service.confirm_heavy(guild, prepared.pending_id, now=stamp)
        assert outcome.verdict is ModVerdict.FAILED
        async with session_scope(sessions) as session:
            row = await session.scalar(select(ModCase))
        assert row is not None
        assert row.revoked_by == 99
        assert row.revoke_reason == copy.MOD_FAILED
        assert dms
        text = collected_text(dms[0]["view"])
        assert copy.CASE_REVOKED_TITLE in text
        assert copy.MOD_FAILED in text
    finally:
        await engine.dispose()


async def test_system_ban_enacts_without_confirm(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    bot.staff_feed = SimpleNamespace(post=_noop)
    bot.evidence = SimpleNamespace(deliver_dm1=_noop)
    guild = _BanGuild()
    actor = _member(10)
    target = _member(3)
    service = ModerationService(bot)
    try:
        refused = await service.apply(guild, actor, target, kind=CaseKind.BAN, reason=TRAP_REASON)
        assert refused.verdict is ModVerdict.FAILED
        assert guild.bans == []
        outcome = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.BAN,
            reason=TRAP_REASON,
            system=True,
        )
        assert outcome.verdict is ModVerdict.OK
        assert outcome.case is not None
        assert outcome.case.reason == TRAP_REASON
        assert outcome.case.actor_rank == int(Rank.SPECIAL)
        assert guild.bans == [(3, 0, copy.actor_reason(10, TRAP_REASON))]
    finally:
        await engine.dispose()


async def test_confirm_heavy_refuses_when_ban_key_is_gone(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _BanGuild()
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        prepared = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=stamp)
        assert prepared.verdict is ModVerdict.OK
        assert prepared.pending_id is not None
        bot.caches.permissions.set(
            (1, SNAPSHOT_CACHE_KEY),
            AccessSnapshot(
                rank_roles=_ranked_snapshot().rank_roles,
                rules={MOD_BAN: PermissionRule(MOD_BAN, frozenset(), frozenset())},
                special_locked=True,
            ),
        )
        bot.caches.invalidate_member(1, 2)
        outcome = await service.confirm_heavy(guild, prepared.pending_id, now=stamp)
        assert outcome.verdict is ModVerdict.DENIED
        assert outcome.case is None
        assert guild.bans == []
        text = collected_text(outcome_view(outcome))
        assert copy.CONFIG_DENIED in text
        async with session_scope(sessions) as session:
            row = await session.get(ModIntent, prepared.pending_id)
            assert row is not None
            assert row.consumed_at is None
    finally:
        await engine.dispose()


async def test_begin_heavy_rejects_watcher_on_senior(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    try:
        outcome = await service.begin_heavy(
            _BanGuild(),
            _member(2, (WATCHER_ROLE,)),
            _member(3, (SENIOR_ROLE,)),
            kind=CaseKind.BAN,
            reason=REASON,
        )
        assert outcome.verdict is ModVerdict.HIERARCHY
        assert outcome.pending_id is None
    finally:
        await engine.dispose()


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class _MemoryTarget:
    def __init__(self) -> None:
        self.views: list[object] = []

    async def edit(self, view: object) -> None:
        self.views.append(view)


async def test_purge_progress_skips_frames_inside_edit_budget() -> None:
    clock = _FakeClock()
    target = _MemoryTarget()
    progress = Progress(target, title=copy.MOD_PURGE_TITLE, show_after=0, min_interval=2.0, clock=clock)
    started = time.perf_counter()
    await progress.start()
    for done in range(20):
        clock.advance(0.05)
        await progress.step(done * 50, total=1000, messages=done)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert len(target.views) == 1
    clock.advance(2.0)
    await progress.step(1000, total=1000, messages=20)
    assert len(target.views) == 2
    done = outcome_view(ModOutcome(ModVerdict.LOG_CHANNEL))
    await progress.finish(done)
    assert target.views[-1] is done


class _Msg:
    def __init__(self, message_id: int, created_at: datetime, author_id: int) -> None:
        self.id = message_id
        self.created_at = created_at
        self.author = SimpleNamespace(id=author_id)
        self.removed = 0

    async def delete(self, reason: str | None = None) -> None:
        self.removed += 1


class _HistChannel:
    def __init__(self, channel_id: int, messages: list[_Msg]) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self._messages = messages
        self.deleted: list[list[object]] = []

    def history(self, **kwargs: object):
        return self._walk(**kwargs)

    async def _walk(self, *, limit: int = 100, after: object = None, before: object = None):
        after_id = getattr(after, "id", after)
        before_id = getattr(before, "id", before)
        items = sorted(self._messages, key=lambda item: item.id, reverse=True)
        if after_id:
            items = [item for item in items if item.id > int(after_id)]
        if before_id:
            items = [item for item in items if item.id < int(before_id)]
        for message in items[: int(limit)]:
            yield message

    async def delete_messages(self, messages: list[object], reason: str | None = None) -> None:
        self.deleted.append(list(messages))


async def test_purge_skips_log_channel_and_old_messages(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        blocked = await service.purge(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            SimpleNamespace(id=10, mention="#logs"),
            reason=REASON,
            now=now,
        )
        assert blocked.verdict is ModVerdict.LOG_CHANNEL
        text = collected_text(outcome_view(blocked))
        assert copy.MOD_LOG_CHANNEL in text
        fresh = _Msg(3, now - timedelta(days=1), 9)
        old = _Msg(2, now - timedelta(days=15), 9)
        channel = _HistChannel(12, [fresh, old])
        outcome = await service.purge(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            channel,
            reason=REASON,
            now=now,
        )
        assert outcome.verdict is ModVerdict.OK
        assert outcome.deleted == 1
        assert outcome.skipped == 1
        assert fresh.removed == 1
        assert channel.deleted == []
    finally:
        await engine.dispose()


async def test_unlock_channel_cancels_mod_unlock_job(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    cancelled: list[object] = []
    bot = _bot(sessions)
    bot.queue = SimpleNamespace(
        schedule_later=lambda *_a, **_k: None,
        cancel_later=lambda key: cancelled.append(key),
    )
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 5, tzinfo=UTC)
    overwrite = SimpleNamespace(send_messages=False)

    class _LockChannel:
        id = 40

        def overwrites_for(self, _role: object) -> SimpleNamespace:
            return overwrite

        async def set_permissions(self, _role: object, *, overwrite: object, reason: str | None = None) -> None:
            return None

        async def edit(self, **_kwargs: object) -> None:
            return None

    try:
        async with session_scope(sessions) as session:
            session.add(
                ChannelLock(
                    guild_id=1,
                    channel_id=40,
                    created_by=2,
                    reason=REASON,
                    slowmode=5,
                    previous_slowmode=0,
                    previous_send=True,
                    created_at=stamp,
                    unlock_at=stamp + timedelta(hours=1),
                )
            )
        outcome = await service.unlock_channel(
            SimpleNamespace(id=1, default_role=SimpleNamespace()),
            _LockChannel(),
            now=stamp,
        )
        assert outcome.verdict is ModVerdict.OK
        assert cancelled == [("mod-unlock", 1, 40)]
        assert overwrite.send_messages is True
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(ChannelLock).where(ChannelLock.channel_id == 40))).one()
            unlocked_at = row.unlocked_at
            assert unlocked_at is not None
            if unlocked_at.tzinfo is None:
                unlocked_at = unlocked_at.replace(tzinfo=UTC)
            assert unlocked_at == stamp
    finally:
        await engine.dispose()


def test_can_appeal_by_kind_and_deadline() -> None:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    created = now - timedelta(days=3)
    assert can_appeal(CaseKind.WARN, created, now, already_appealed=False) is True
    assert can_appeal(CaseKind.TIMEOUT, created, now, already_appealed=False) is True
    assert can_appeal(CaseKind.MUTE, created, now, already_appealed=False) is True
    assert can_appeal(CaseKind.KICK, created, now, already_appealed=False) is False
    assert can_appeal(CaseKind.BAN, created, now, already_appealed=False) is False
    assert can_appeal(CaseKind.WARN, created, now, already_appealed=True) is False
    assert can_appeal(CaseKind.WARN, now - timedelta(days=8), now, already_appealed=False) is False
    assert revoke_plan(CaseKind.TIMEOUT).undo_discord == "timeout"
    assert revoke_plan(CaseKind.MUTE).undo_discord == "mute"
    assert revoke_plan(CaseKind.WARN).undo_discord is None
    assert may_revoke(actor=Rank.CHIEF, case_actor=Rank.WATCHER) is True
    assert may_revoke(actor=Rank.CHIEF, case_actor=Rank.SPECIAL) is False
    assert may_revoke(actor=Rank.SPECIAL, case_actor=Rank.SPECIAL) is True
    # Automod cases carry the SPECIAL rank but are authored by the bot: any chief may undo them.
    assert may_revoke(actor=Rank.CHIEF, case_actor=Rank.SPECIAL, system=True) is True
    assert may_revoke(actor=Rank.SENIOR, case_actor=Rank.SPECIAL, system=True) is False


def test_timeout_still_ours_matches_this_case() -> None:
    created = datetime(2026, 9, 7, 12, tzinfo=UTC)
    until = created + timedelta(hours=2)
    now = created + timedelta(minutes=10)
    assert timeout_still_ours(created_at=created, duration_seconds=7200, member_until=until, now=now) is True
    later = until + timedelta(hours=1)
    assert timeout_still_ours(created_at=created, duration_seconds=7200, member_until=later, now=now) is False
    assert timeout_still_ours(created_at=created, duration_seconds=7200, member_until=until, now=until) is False


def test_history_line_strikes_revoked_case() -> None:
    plain = copy.mod_history_line(4, "warn", REASON)
    struck = copy.mod_history_line(4, "warn", REASON, revoked_by=8)
    assert "~~" not in plain
    assert struck.startswith("~~")
    assert "отменено <@8>" in struck


async def test_revoke_clears_timeout_and_keeps_quota(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    bot.reputation = ReputationService(bot)
    timeouts: list[object] = []

    async def timeout(duration: object, reason: str | None = None) -> None:
        timeouts.append(duration)

    async def send(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=1)

    stamp = datetime(2026, 9, 7, 12, tzinfo=UTC)
    target = _member(3)
    target.timeout = timeout
    target.timed_out_until = stamp + timedelta(hours=2)
    target.send = send
    actor = _member(2, (WATCHER_ROLE,))
    actor.send = send
    chief = _member(8, (CHIEF_ROLE,))
    chief.send = send
    members = {2: actor, 3: target, 8: chief}

    def get_member(user_id: int) -> SimpleNamespace | None:
        return members.get(user_id)

    guild = SimpleNamespace(id=1, get_member=get_member)
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="2h",
            now=stamp,
        )
        assert outcome.verdict is ModVerdict.OK
        assert outcome.case is not None
        assert timeouts
        timeouts.clear()
        async with session_scope(sessions) as session:
            used_before = await _count_used(session, 1, 2, start=day_start(stamp, "UTC"), bot_id=None)
        revoked = await service.revoke(guild, outcome.case.id, chief, "ошибка следящего, снимаем наказание", now=stamp)
        assert revoked.verdict is ModVerdict.OK
        assert None in timeouts
        assert revoked.case is not None
        assert revoked.case.revoked_at is not None
        again = await service.revoke(guild, outcome.case.id, chief, "ошибка следящего, снимаем наказание", now=stamp)
        assert again.verdict is ModVerdict.REVOKED
        async with session_scope(sessions) as session:
            used_after = await _count_used(session, 1, 2, start=day_start(stamp, "UTC"), bot_id=None)
            row = await session.get(ModCase, outcome.case.id)
            week = await cases_this_week(session, 1, now=stamp)
        assert used_after.timeouts == used_before.timeouts == 1
        assert week.get(2, 0) == 0
        assert row is not None and row.revoked_at is not None
        special = await service.apply(
            guild,
            chief,
            target,
            kind=CaseKind.WARN,
            reason=REASON,
            now=stamp,
            system=True,
        )
        assert special.case is not None
        denied = await service.revoke(guild, special.case.id, chief, "ошибка следящего, снимаем наказание", now=stamp)
        assert denied.verdict is ModVerdict.HIERARCHY
    finally:
        await engine.dispose()


async def test_revoke_removes_mute_role(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    stamp = datetime(2026, 9, 7, 12, tzinfo=UTC)
    target = _member(3)
    actor = _member(2, (WATCHER_ROLE,))
    chief = _member(8, (CHIEF_ROLE,))
    members = {2: actor, 3: target, 8: chief}
    guild = SimpleNamespace(id=1, get_member=lambda uid: members.get(uid))
    service = ModerationService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(ModSettings(guild_id=1, mute_role_id=55))
        muted = await service.apply(guild, actor, target, kind=CaseKind.MUTE, reason=REASON, now=stamp)
        assert muted.verdict is ModVerdict.OK
        assert muted.case is not None
        bot.roles.claims.clear()
        revoked = await service.revoke(guild, muted.case.id, chief, "ошибка следящего, снимаем наказание", now=stamp)
        assert revoked.verdict is ModVerdict.OK
        assert bot.roles.claims
        claim = bot.roles.claims[-1]
        assert 55 in claim.remove
    finally:
        await engine.dispose()


def test_actor_reason_names_the_commander() -> None:
    assert copy.actor_reason(10, "дело") == "по команде 10: дело"
    assert copy.actor_reason(None, "дело") == "дело"


def test_bot_can_enact_requires_higher_role() -> None:
    assert bot_can_enact(bot_top=5, target_top=3, target_is_owner=False) is True
    assert bot_can_enact(bot_top=3, target_top=5, target_is_owner=False) is False
    assert bot_can_enact(bot_top=5, target_top=3, target_is_owner=True) is False
    assert bot_can_enact(bot_top=5, target_top=3, target_is_owner=False, bot_can_moderate=False) is False
    assert bot_can_enact(bot_top=5, target_top=3, target_is_owner=False, target_is_admin=True) is False
    assert bot_can_enact(bot_top=None, target_top=None, target_is_owner=False) is True


async def test_timeout_below_target_does_not_open_a_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    bot.is_developer = lambda _uid: True
    timed: list[object] = []

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        timed.append((duration, reason))

    target = _member(3, (CHIEF_ROLE,))
    target.timeout = timeout
    target.top_role = SimpleNamespace(position=20)
    target.guild_permissions = SimpleNamespace(administrator=True)
    actor = _member(2, (WATCHER_ROLE,))
    guild = SimpleNamespace(
        id=1,
        owner_id=99,
        me=SimpleNamespace(
            top_role=SimpleNamespace(position=5),
            guild_permissions=SimpleNamespace(moderate_members=True, administrator=False),
        ),
    )
    service = ModerationService(bot)
    try:
        outcome = await service.apply(guild, actor, target, kind=CaseKind.TIMEOUT, reason=REASON, duration_raw="10м")
        assert outcome.verdict is ModVerdict.POWER
        assert outcome.case is None
        assert timed == []
        history = await service.history(1, 3)
        assert history == []
        text = collected_text(outcome_view(outcome))
        assert copy.MOD_BOT_BELOW in text
    finally:
        await engine.dispose()


async def test_timeout_discord_failure_discards_the_case(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    opened: list[int] = []

    class _Evidence:
        async def open_for_case(self, *_args: object, **_kwargs: object) -> None:
            opened.append(1)

    bot.evidence = _Evidence()

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        raise RuntimeError("403")

    target = _member(3)
    target.timeout = timeout
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            _guild(),
            _member(2, (WATCHER_ROLE,)),
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="10м",
        )
        assert outcome.verdict is ModVerdict.FAILED
        assert outcome.case is None
        assert opened == []
        history = await service.history(1, 3)
        assert history == []
    finally:
        await engine.dispose()
