from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import UnofficialAdminNotice, WatcherSuspension
from conductor.db.session import session_scope
from conductor.domain.access import (
    WATCHERS_APPOINT,
    WATCHERS_READ,
    AccessDecision,
    AccessSnapshot,
    Rank,
    StaffingBlock,
    StaffingPolicy,
    appoint_permitted,
    appoint_role_diff,
    can,
    can_appoint,
    can_dismiss,
    empty_rank_roles,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text, empty
from conductor.services.access import ensure_defaults, load_snapshot
from conductor.services.role_broker import RoleApplyResult, RoleSubsystem
from conductor.services.watchers import (
    WatchersService,
    chunk_lines,
    clear_unofficial,
    duty_ids,
    first_unofficial_notice,
    load_policy,
    plan_appoint,
    roster_lines,
    save_policy,
    set_duty,
    should_flag_unofficial,
    staff_claim,
)


async def _async_noop(**_kwargs: object) -> None:
    return None


async def _async_funnel(*_args: object, **_kwargs: object) -> object:
    from conductor.domain.verification import FunnelCounts

    return FunnelCounts(joined=0, verified=0, trapped=0, left_within_24h=0)


class _Role:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class _Member:
    def __init__(self, user_id: int, name: str, role_ids: tuple[int, ...], *, bot: bool = False) -> None:
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.bot = bot
        self.roles = [_Role(role_id) for role_id in role_ids]


def test_roster_groups_by_rank_and_skips_bots() -> None:
    snapshot = AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.CHIEF: (10,), Rank.WATCHER: (20,), Rank.SPECIAL: (30,)},
        rules={},
        special_locked=True,
    )
    guild = SimpleNamespace(
        members=(
            _Member(1, "Ann", (10,)),
            _Member(2, "Bob", (20,)),
            _Member(3, "Bot", (30,), bot=True),
            _Member(4, "Civ", ()),
        )
    )
    lines = roster_lines(guild, snapshot, on_duty=frozenset({1}), cases={1: 4})  # type: ignore[arg-type]
    text = "\n".join(lines)
    assert copy.watchers_heading("chief") in text
    assert copy.watchers_heading("watcher") in text
    assert copy.watchers_heading("special") not in text
    assert copy.WATCHERS_ON_DUTY in text
    assert "4 дела за неделю" in text
    assert "<@2>" in text
    assert "<@3>" not in text
    assert "<@4>" not in text


def test_roster_chunks_ten_lines() -> None:
    rows = [f"line-{index}" for index in range(23)]
    pages = chunk_lines(rows)
    assert len(pages) == 3
    assert len(pages[0]) == 10
    assert len(pages[-1]) == 3


def test_empty_roster_is_archetype_g() -> None:
    text = collected_text(empty(copy.WATCHERS_EMPTY, hint=copy.WATCHERS_EMPTY_HINT))
    assert copy.EMPTY_TITLE in text
    assert copy.WATCHERS_EMPTY in text


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def test_unofficial_notice_fires_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            assert await first_unofficial_notice(session, 1, 9) is True
        async with session_scope(sessions) as session:
            assert await first_unofficial_notice(session, 1, 9) is False
            row = await session.scalar(select(UnofficialAdminNotice))
            assert row is not None
            assert row.user_id == 9
        async with session_scope(sessions) as session:
            await clear_unofficial(session, 1, 9)
        async with session_scope(sessions) as session:
            assert await first_unofficial_notice(session, 1, 9) is True
    finally:
        await engine.dispose()


def test_compatibility_follows_special_lock() -> None:
    from conductor.domain.access import ACTION_DEFAULTS, authorize

    open_gate = authorize(
        rank=Rank.NONE,
        key="watchers.read",
        allowed=ACTION_DEFAULTS["watchers.read"],
        is_administrator=True,
        special_locked=False,
    )
    closed = authorize(
        rank=Rank.NONE,
        key="watchers.read",
        allowed=ACTION_DEFAULTS["watchers.read"],
        is_administrator=True,
        special_locked=True,
    )
    assert open_gate.allowed is True
    assert open_gate.fallback_admin is True
    assert closed.allowed is False
    assert closed.fallback_admin is False


def _ranks() -> dict[Rank, tuple[int, ...]]:
    return empty_rank_roles() | {
        Rank.WATCHER: (1,),
        Rank.SENIOR: (2,),
        Rank.CHIEF: (3,),
        Rank.SPECIAL: (4,),
    }


def test_chief_does_not_appoint_chief() -> None:
    policy = StaffingPolicy()
    assert (
        can_appoint(
            Rank.CHIEF,
            desired=Rank.CHIEF,
            target=Rank.NONE,
            target_is_bot=False,
            policy=policy,
        )
        is StaffingBlock.HIERARCHY
    )


def test_senior_does_not_appoint_senior() -> None:
    policy = StaffingPolicy(senior_appoint=True)
    assert (
        can_appoint(
            Rank.SENIOR,
            desired=Rank.SENIOR,
            target=Rank.NONE,
            target_is_bot=False,
            policy=policy,
        )
        is StaffingBlock.HIERARCHY
    )


def test_senior_appoints_watcher_only_with_setting() -> None:
    off = StaffingPolicy()
    on = StaffingPolicy(senior_appoint=True)
    kwargs = {"desired": Rank.WATCHER, "target": Rank.NONE, "target_is_bot": False}
    assert can_appoint(Rank.SENIOR, policy=off, **kwargs) is StaffingBlock.SENIOR
    assert can_appoint(Rank.SENIOR, policy=on, **kwargs) is None
    denied = AccessDecision(False, Rank.SENIOR, need=Rank.CHIEF)
    assert appoint_permitted(denied, Rank.SENIOR, off) is False
    assert appoint_permitted(denied, Rank.SENIOR, on) is True


def test_special_via_bot_is_off_by_default() -> None:
    assert (
        can_appoint(
            Rank.SPECIAL,
            desired=Rank.SPECIAL,
            target=Rank.CHIEF,
            target_is_bot=False,
            policy=StaffingPolicy(),
        )
        is StaffingBlock.SPECIAL
    )
    assert (
        can_appoint(
            Rank.SPECIAL,
            desired=Rank.SPECIAL,
            target=Rank.CHIEF,
            target_is_bot=False,
            policy=StaffingPolicy(special_appoint=True),
        )
        is None
    )


def test_appoint_strips_the_previous_rank_role() -> None:
    add, remove = appoint_role_diff(Rank.SENIOR, _ranks(), (1,))
    assert add == (2,)
    assert remove == (1,)


def test_appoint_shared_watcher_senior_id_is_not_removed() -> None:
    ranks = empty_rank_roles() | {Rank.WATCHER: (7,), Rank.SENIOR: (7,)}
    add, remove = appoint_role_diff(Rank.SENIOR, ranks, (7,))
    assert add == ()
    assert 7 not in remove
    assert set(add).isdisjoint(remove)
    add, remove = appoint_role_diff(Rank.SENIOR, ranks, ())
    assert add == (7,)
    assert 7 not in remove
    assert set(add).isdisjoint(remove)


def test_staff_claim_goes_through_the_broker_subsystem() -> None:
    claim = staff_claim(guild_id=1, user_id=2, add=(2,), remove=(1,), actor_id=9, reason="кадры")
    assert claim.subsystem is RoleSubsystem.STAFF
    assert claim.urgent is True
    assert claim.actor_id == 9


def test_duty_does_not_change_keys() -> None:
    assert can(Rank.WATCHER, key=WATCHERS_READ) is True
    assert can(Rank.WATCHER, key=WATCHERS_APPOINT) is False
    assert can(Rank.CHIEF, key=WATCHERS_APPOINT) is True


def test_dismiss_rejects_equal_and_bot() -> None:
    assert can_dismiss(Rank.CHIEF, target=Rank.CHIEF, target_is_bot=False) is StaffingBlock.HIERARCHY
    assert can_dismiss(Rank.CHIEF, target=Rank.WATCHER, target_is_bot=True) is StaffingBlock.BOT
    assert can_dismiss(Rank.CHIEF, target=Rank.WATCHER, target_is_bot=False) is None


async def _sessions(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    return create_sessionmaker(engine), engine


async def test_duty_roundtrip_does_not_touch_permissions(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 1)
            before = await load_snapshot(session, 1)
            changed = await set_duty(session, 1, 7, True, actor_id=9, now=now)
            assert changed is True
            assert 7 in await duty_ids(session, 1)
            after = await load_snapshot(session, 1)
        assert after.rules[WATCHERS_READ].ranks == before.rules[WATCHERS_READ].ranks
        assert after.special_locked == before.special_locked
        async with session_scope(sessions) as session:
            assert await set_duty(session, 1, 7, True, actor_id=9, now=now) is False
            assert await set_duty(session, 1, 7, False, actor_id=9, now=now) is True
            assert await duty_ids(session, 1) == frozenset()
    finally:
        await engine.dispose()


async def test_policy_defaults_off(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 1)
            policy = await load_policy(session, 1)
            assert policy == StaffingPolicy()
            stored = await save_policy(session, 1, StaffingPolicy(senior_appoint=True))
            assert stored.senior_appoint is True
            assert stored.special_appoint is False
        async with session_scope(sessions) as session:
            again = await load_policy(session, 1)
            assert again.senior_appoint is True
            assert again.special_appoint is False
    finally:
        await engine.dispose()


def test_plan_appoint_needs_key_when_senior_setting_off() -> None:
    snapshot = AccessSnapshot(rank_roles=_ranks(), rules={}, special_locked=True)
    plan = plan_appoint(
        snapshot=snapshot,
        policy=StaffingPolicy(),
        actor=Rank.SENIOR,
        decision=AccessDecision(False, Rank.SENIOR, need=Rank.CHIEF),
        target_role_ids=(),
        target_is_bot=False,
        desired=Rank.WATCHER,
    )
    assert plan.need_key is True
    assert plan.ok is False


def test_plan_appoint_returns_diff_for_chief() -> None:
    snapshot = AccessSnapshot(rank_roles=_ranks(), rules={}, special_locked=True)
    plan = plan_appoint(
        snapshot=snapshot,
        policy=StaffingPolicy(),
        actor=Rank.CHIEF,
        decision=AccessDecision(True, Rank.CHIEF),
        target_role_ids=(),
        target_is_bot=False,
        desired=Rank.WATCHER,
    )
    assert plan.ok is True
    assert plan.add == (1,)
    assert plan.remove == ()


def test_should_flag_unofficial_only_locked_admin_without_rank() -> None:
    locked = AccessSnapshot(rank_roles=_ranks(), rules={}, special_locked=True)
    open_gate = AccessSnapshot(rank_roles=_ranks(), rules={}, special_locked=False)
    assert should_flag_unofficial(is_admin=True, snapshot=locked, rank=Rank.NONE) is True
    assert should_flag_unofficial(is_admin=True, snapshot=locked, rank=Rank.WATCHER) is False
    assert should_flag_unofficial(is_admin=False, snapshot=locked, rank=Rank.NONE) is False
    assert should_flag_unofficial(is_admin=True, snapshot=open_gate, rank=Rank.NONE) is False


class _StaffRoles:
    def __init__(self, result: RoleApplyResult | None = None) -> None:
        self.claims: list[object] = []
        self.result = result or RoleApplyResult(ok=True, called=True)

    async def apply(self, claim: object) -> RoleApplyResult:
        self.claims.append(claim)
        return self.result


async def test_apply_rank_keeps_expect_until_consumed() -> None:
    roles = _StaffRoles()
    service = WatchersService(SimpleNamespace(roles=roles))
    result = await service.apply_rank(
        guild_id=1,
        user_id=2,
        add=(3,),
        remove=(),
        actor_id=9,
        reason="кадры",
    )
    assert result.ok is True
    assert service.consume_expected(1, 2) is True
    assert service.consume_expected(1, 2) is False


async def test_apply_rank_drops_expect_when_broker_fails() -> None:
    roles = _StaffRoles(RoleApplyResult(ok=False, called=False, error="ниже"))
    service = WatchersService(SimpleNamespace(roles=roles))
    result = await service.apply_rank(
        guild_id=1,
        user_id=2,
        add=(3,),
        remove=(),
        actor_id=9,
        reason="кадры",
    )
    assert result.ok is False
    assert service.consume_expected(1, 2) is False


def test_can_suspend_hierarchy_and_self() -> None:
    from conductor.domain.watchers import can_suspend

    assert can_suspend(actor=Rank.CHIEF, target=Rank.WATCHER, actor_id=1, target_id=2) is True
    assert can_suspend(actor=Rank.CHIEF, target=Rank.SENIOR, actor_id=1, target_id=2) is True
    assert can_suspend(actor=Rank.CHIEF, target=Rank.SPECIAL, actor_id=1, target_id=2) is False
    assert can_suspend(actor=Rank.SPECIAL, target=Rank.CHIEF, actor_id=1, target_id=2) is True
    assert can_suspend(actor=Rank.SPECIAL, target=Rank.SPECIAL, actor_id=1, target_id=2) is True
    assert can_suspend(actor=Rank.SPECIAL, target=Rank.WATCHER, actor_id=9, target_id=9) is False
    assert can_suspend(actor=Rank.SENIOR, target=Rank.WATCHER, actor_id=1, target_id=2) is False


def test_roster_sorts_suspended_and_overdue_first() -> None:
    from conductor.domain.watchers import RosterPerson, WatcherMetrics, sort_roster

    now = datetime(2026, 9, 7, tzinfo=UTC)
    people = [
        RosterPerson(1, "<@1>", "Ann", Rank.WATCHER, True, None, WatcherMetrics()),
        RosterPerson(2, "<@2>", "Bob", Rank.CHIEF, False, None, WatcherMetrics(overdue=2)),
        RosterPerson(
            3,
            "<@3>",
            "Zed",
            Rank.WATCHER,
            False,
            None,
            WatcherMetrics(suspended_until=now),
        ),
    ]
    names = [item.display_name for item in sort_roster(people)]
    assert names == ["Zed", "Bob", "Ann"]


def _case(
    *,
    number: int,
    created_by: int,
    created_at: datetime,
    revoked_at: datetime | None = None,
    owner_id: int = 50,
) -> object:
    from conductor.db.models import ModCase

    return ModCase(
        guild_id=1,
        number=number,
        owner_id=owner_id,
        created_by=created_by,
        kind="timeout",
        reason="проверка дела",
        created_at=created_at,
        revoked_at=revoked_at,
        revoked_by=None if revoked_at is None else 9,
        revoke_reason=None if revoked_at is None else "ошибка",
    )


async def test_roster_metrics_count_revoked_overdue_tickets_and_actions(tmp_path: Path) -> None:
    from conductor.db.models import ModEvidence, StaffAction, Ticket, TicketPanel
    from conductor.services.watchers_report import roster_metrics

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            session.add(_case(number=1, created_by=2, created_at=now - timedelta(days=2)))
            session.add(
                _case(
                    number=2,
                    created_by=2,
                    created_at=now - timedelta(days=10),
                    revoked_at=now - timedelta(days=3),
                )
            )
            session.add(
                _case(
                    number=3,
                    created_by=2,
                    created_at=now - timedelta(days=5),
                    revoked_at=now - timedelta(days=1),
                )
            )
            session.add(_case(number=4, created_by=2, created_at=now - timedelta(days=20)))
            await session.flush()
            case_overdue = _case(number=5, created_by=2, created_at=now - timedelta(hours=80))
            session.add(case_overdue)
            await session.flush()
            session.add(
                ModEvidence(
                    guild_id=1,
                    case_id=int(case_overdue.id),
                    channel_id=700,
                    created_at=now - timedelta(hours=80),
                    sent_at=None,
                    closed_by=None,
                )
            )
            session.add(
                StaffAction(
                    guild_id=1,
                    actor_id=2,
                    action="case.timeout",
                    created_at=now - timedelta(hours=2),
                )
            )
            session.add(TicketPanel(guild_id=1, channel_id=10, created_at=now, created_by=9))
            await session.flush()
            panel = (await session.scalars(select(TicketPanel))).first()
            assert panel is not None
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=50,
                    category_id=1,
                    panel_id=int(panel.id),
                    state="closed",
                    closed_by=2,
                    closed_at=now - timedelta(days=2),
                    rating=4,
                    channel_id=11,
                    created_at=now - timedelta(days=3),
                )
            )
            session.add(
                Ticket(
                    guild_id=1,
                    number=2,
                    owner_id=51,
                    category_id=1,
                    panel_id=int(panel.id),
                    state="closed",
                    closed_by=2,
                    closed_at=now - timedelta(days=4),
                    rating=2,
                    channel_id=12,
                    created_at=now - timedelta(days=5),
                )
            )
        async with session_scope(sessions) as session:
            found = await roster_metrics(session, 1, now=now)
        row = found[2]
        assert row.cases_7d == 2
        assert row.revoked_30d == 2
        assert row.overdue == 1
        assert row.tickets_30d == 2
        assert row.rating_avg == 3.0
        assert row.last_action is not None
    finally:
        await engine.dispose()


async def test_suspend_denies_keys_except_help_and_skips_roles(tmp_path: Path) -> None:
    from conductor.domain.access import HELP_READ, MOD_TIMEOUT
    from conductor.services.access import cached_decide, ensure_defaults, set_rank_role
    from conductor.services.cache import CacheHub
    from conductor.services.watchers_report import WatchersReportService

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    roles = _StaffRoles()
    posted: list[object] = []

    async def post(*_args: object, **kwargs: object) -> int:
        posted.append(kwargs)
        return 1

    dms: list[object] = []

    async def send(**_kwargs: object) -> None:
        dms.append(True)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        roles=roles,
        staff_feed=SimpleNamespace(post=post),
        get_user=lambda _uid: SimpleNamespace(send=send),
        outbound=None,
        scheduler=None,
        is_developer=lambda _uid: False,
    )
    service = WatchersReportService(bot)
    try:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 1)
            await set_rank_role(session, 1, Rank.WATCHER, 10)
        result = await service.suspend(
            1,
            2,
            actor_id=9,
            actor_rank=Rank.SPECIAL,
            target_rank=Rank.WATCHER,
            days_raw="7",
            reason="три отмены за месяц",
            now=now,
        )
        assert getattr(result, "until", None) is not None
        assert roles.claims == []
        assert dms
        assert posted
        denied = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=MOD_TIMEOUT,
            now=now,
        )
        assert denied.allowed is False
        assert denied.deny_copy is not None
        assert "приостановлен" in denied.deny_copy
        help_ok = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=HELP_READ,
            now=now,
        )
        assert help_ok.allowed is True
        async with session_scope(sessions) as session:
            row = await session.scalar(select(WatcherSuspension).where(WatcherSuspension.user_id == 2))
            assert row is not None
            row.reason = "не должно попасть в кэш"
        cached = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=MOD_TIMEOUT,
            now=now,
        )
        assert cached.allowed is False
        assert cached.deny_copy is not None
        assert "три отмены" in cached.deny_copy
        lifted = await service.lift(
            1,
            2,
            actor_id=9,
            actor_rank=Rank.SPECIAL,
            target_rank=Rank.WATCHER,
            now=now,
        )
        assert getattr(lifted, "user_id", None) == 2
        allowed = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=MOD_TIMEOUT,
            now=now,
        )
        assert allowed.allowed is True
    finally:
        await engine.dispose()


async def test_chief_cannot_suspend_special_via_service(tmp_path: Path) -> None:
    from conductor.services.watchers_report import WatchersReportService

    sessions, engine = await _sessions(tmp_path)
    bot = SimpleNamespace(sessions=sessions, caches=None, scheduler=None, staff_feed=None)
    service = WatchersReportService(bot)
    try:
        result = await service.suspend(
            1,
            4,
            actor_id=3,
            actor_rank=Rank.CHIEF,
            target_rank=Rank.SPECIAL,
            days_raw="7",
            reason="проверка иерархии",
        )
        assert result == "hierarchy"
    finally:
        await engine.dispose()


async def test_sweep_lifts_expired_suspension(tmp_path: Path) -> None:
    from conductor.db.models import WatcherSuspension
    from conductor.services.watchers_report import WatchersReportService

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    posted: list[object] = []

    async def post(*_args: object, **kwargs: object) -> int:
        posted.append(kwargs)
        return 1

    bot = SimpleNamespace(
        sessions=sessions,
        caches=SimpleNamespace(invalidate_member=lambda *_a, **_k: None),
        staff_feed=SimpleNamespace(post=post),
        get_user=lambda _uid: SimpleNamespace(send=_async_noop),
        outbound=None,
        scheduler=None,
    )
    service = WatchersReportService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(
                WatcherSuspension(
                    guild_id=1,
                    user_id=2,
                    set_by=9,
                    reason="срок",
                    until=now - timedelta(minutes=1),
                    created_at=now - timedelta(days=7),
                )
            )
        lifted = await service.sweep(now=now)
        assert lifted == 1
        async with session_scope(sessions) as session:
            row = await session.scalar(select(WatcherSuspension))
            assert row is not None
            assert row.lifted_at is not None
        assert any(item.get("action") == "watcher.lift" for item in posted if isinstance(item, dict))
    finally:
        await engine.dispose()


async def test_active_duty_excludes_suspended(tmp_path: Path) -> None:
    from conductor.db.models import WatcherSuspension
    from conductor.services.watchers import set_duty
    from conductor.services.watchers_report import active_duty_ids

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            await set_duty(session, 1, 2, True, actor_id=9, now=now)
            session.add(
                WatcherSuspension(
                    guild_id=1,
                    user_id=2,
                    set_by=9,
                    reason="стоп",
                    until=now + timedelta(days=3),
                    created_at=now,
                )
            )
            assert await duty_ids(session, 1) == frozenset({2})
            assert await active_duty_ids(session, 1, now=now) == frozenset()
    finally:
        await engine.dispose()


async def test_digest_posts_once_per_iso_week(tmp_path: Path) -> None:
    from conductor.db.models import GuildConfig
    from conductor.domain.verification import FunnelCounts
    from conductor.services.guild_config import remember_tz
    from conductor.services.watchers_report import WatchersReportService

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 9, 5, tzinfo=UTC)
    sent: list[object] = []

    class Pump:
        def submit(self, channel_id: int, **kwargs: object) -> None:
            sent.append((channel_id, kwargs.get("view")))

    async def funnel(_guild_id: int, _days: int, *, now: datetime | None = None) -> FunnelCounts:
        return FunnelCounts(joined=2, verified=1, trapped=0, left_within_24h=0)

    bot = SimpleNamespace(
        sessions=sessions,
        guilds=[SimpleNamespace(id=1)],
        serves=lambda _gid: True,
        config_by_guild={},
        verification=SimpleNamespace(funnel=funnel),
        user=SimpleNamespace(id=99),
        log_pump=Pump(),
        scheduler=None,
        caches=None,
    )
    service = WatchersReportService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, timezone="UTC", staff_feed_channel_id=44))
        remember_tz(1, "UTC")
        assert await service.digest_tick(now=now) == 1
        assert len(sent) == 1
        assert await service.digest_tick(now=now) == 0
        assert len(sent) == 1
    finally:
        await engine.dispose()


def test_digest_due_uses_guild_local_time() -> None:
    """Monday 09:xx in the guild's zone, not in UTC (regression: local was re-converted to UTC)."""
    from zoneinfo import ZoneInfo

    from conductor.domain.watchers import digest_is_due, digest_week_key

    moscow = ZoneInfo("Europe/Moscow")
    local = datetime(2026, 9, 7, 9, 5, tzinfo=moscow)  # 06:05 UTC
    assert digest_is_due(local)
    assert not digest_is_due(local.astimezone(UTC))
    # Monday 00:30 local is still Sunday in UTC-8; the week key must follow local.
    la = ZoneInfo("America/Los_Angeles")
    early = datetime(2026, 1, 5, 0, 30, tzinfo=la)
    assert digest_week_key(early) == "2026-W02"
    assert digest_week_key(early.astimezone(UTC)) == "2026-W02"
    boundary = datetime(2027, 1, 4, 0, 30, tzinfo=la)  # Monday local; in UTC it is still Sunday, 2026-W53
    assert digest_week_key(boundary) == "2027-W01"


async def test_digest_dedups_after_restart(tmp_path: Path) -> None:
    from conductor.db.models import GuildConfig
    from conductor.services.guild_config import remember_tz
    from conductor.services.watchers_report import WatchersReportService

    sessions, engine = await _sessions(tmp_path)
    now = datetime(2026, 9, 7, 9, 5, tzinfo=UTC)
    sent: list[object] = []

    class Pump:
        def submit(self, _channel_id: int, **_kwargs: object) -> None:
            sent.append(True)

    bot = SimpleNamespace(
        sessions=sessions,
        guilds=[SimpleNamespace(id=1)],
        serves=lambda _gid: True,
        config_by_guild={},
        verification=SimpleNamespace(funnel=_async_funnel),
        user=SimpleNamespace(id=99),
        log_pump=Pump(),
        scheduler=None,
        caches=None,
    )
    service = WatchersReportService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, timezone="UTC", staff_feed_channel_id=44, staff_digest_week="2026-W37"))
        remember_tz(1, "UTC")
        assert await service.digest_tick(now=now) == 0
        assert sent == []
    finally:
        await engine.dispose()
