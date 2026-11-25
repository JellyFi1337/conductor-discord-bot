from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.domain.loyalty import (
    LoyaltySkip,
    LoyaltyTier,
    add_calendar_months,
    add_calendar_years,
    classify_member,
    loyalty_threshold_copy,
    qualifies_by_messages,
    registry_delta,
    resolve_tier,
    snapshot_tier,
)
from conductor.presentation import copy
from conductor.presentation.layout import TEXT_LIMIT, collected_text
from conductor.presentation.loyalty import grant_denial_view, preview_summary
from conductor.services.loyalty import GrantDenial, LoyaltyPlan, LoyaltyService, grouped_assignments
from conductor.services.old_roles import count_registry_mismatches
from conductor.services.role_broker import RoleApplyResult, RoleSubsystem

GUILD_CREATED = datetime(2022, 1, 15, 12, 0, tzinfo=UTC)


def test_ruby_includes_founding_members_even_after_two_years() -> None:
    now = datetime(2025, 9, 4, tzinfo=UTC)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=GUILD_CREATED, now=now) is LoyaltyTier.RUBY


def test_ruby_boundary_is_inclusive() -> None:
    deadline = add_calendar_months(GUILD_CREATED, 3)
    now = datetime(2025, 9, 4, tzinfo=UTC)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=deadline, now=now) is LoyaltyTier.RUBY
    just_after = deadline + timedelta(seconds=1)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=just_after, now=now) is LoyaltyTier.EMERALD


def test_emerald_requires_two_years_outside_ruby_window() -> None:
    joined = datetime(2022, 6, 1, tzinfo=UTC)
    now = datetime(2025, 9, 4, tzinfo=UTC)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=joined, now=now) is LoyaltyTier.EMERALD


def test_sapphire_is_one_year_but_not_two() -> None:
    now = datetime(2025, 9, 4, tzinfo=UTC)
    joined = add_calendar_years(now, -1)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=joined, now=now) is LoyaltyTier.SAPPHIRE


def test_amethyst_is_everyone_else_who_joined_before_today() -> None:
    now = datetime(2025, 9, 4, 18, 0, tzinfo=UTC)
    joined = datetime(2025, 9, 3, 9, 0, tzinfo=UTC)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=joined, now=now) is LoyaltyTier.AMETHYST


def test_joined_today_is_not_eligible() -> None:
    now = datetime(2025, 9, 4, 18, 0, tzinfo=UTC)
    joined = datetime(2025, 9, 4, 8, 0, tzinfo=UTC)
    assert resolve_tier(guild_created_at=GUILD_CREATED, joined_at=joined, now=now) is None


def test_candidates_need_at_least_ten_messages() -> None:
    assert qualifies_by_messages(0) is False
    assert qualifies_by_messages(1) is False
    assert qualifies_by_messages(9) is False
    assert qualifies_by_messages(10) is True
    assert loyalty_threshold_copy() == "не менее 10 сообщений"
    assert "более" not in loyalty_threshold_copy()
    assert "5" not in loyalty_threshold_copy()


def test_today_joiners_are_not_counted_as_silent() -> None:
    now = datetime(2025, 9, 4, 18, 0, tzinfo=UTC)
    joined_today = datetime(2025, 9, 4, 8, 0, tzinfo=UTC)
    quiet_today = classify_member(
        is_bot=False,
        message_count=2,
        joined_at=joined_today,
        guild_created_at=GUILD_CREATED,
        now=now,
    )
    talkative_today = classify_member(
        is_bot=False,
        message_count=40,
        joined_at=joined_today,
        guild_created_at=GUILD_CREATED,
        now=now,
    )
    assert quiet_today.skip is LoyaltySkip.TODAY
    assert talkative_today.skip is LoyaltySkip.TODAY


def test_tenured_member_below_threshold_is_silent() -> None:
    now = datetime(2025, 9, 4, 18, 0, tzinfo=UTC)
    joined = datetime(2025, 9, 3, 9, 0, tzinfo=UTC)
    quiet = classify_member(
        is_bot=False,
        message_count=9,
        joined_at=joined,
        guild_created_at=GUILD_CREATED,
        now=now,
    )
    ready = classify_member(
        is_bot=False,
        message_count=10,
        joined_at=joined,
        guild_created_at=GUILD_CREATED,
        now=now,
    )
    assert quiet.skip is LoyaltySkip.SILENT
    assert ready.skip is None
    assert ready.tier is LoyaltyTier.AMETHYST


def test_registry_delta_strips_unauthorized_and_restores_missing() -> None:
    old_ids = {10, 20, 30}
    assert registry_delta({10, 99}, authorized_role_id=10, old_role_ids=old_ids).empty is True
    delta = registry_delta({10, 20}, authorized_role_id=10, old_role_ids=old_ids)
    assert delta.add_ids == ()
    assert delta.remove_ids == (20,)
    restored = registry_delta(set(), authorized_role_id=10, old_role_ids=old_ids)
    assert restored.add_ids == (10,)
    assert restored.remove_ids == ()
    stripped = registry_delta({20}, authorized_role_id=None, old_role_ids=old_ids)
    assert stripped.remove_ids == (20,)
    assert snapshot_tier({20, 10}, {LoyaltyTier.RUBY: 10, LoyaltyTier.EMERALD: 20}) is LoyaltyTier.RUBY


def test_preview_card_stays_under_layout_budget() -> None:
    plan = LoyaltyPlan(
        as_of=datetime(2025, 9, 4, tzinfo=UTC),
        guild_created_at=GUILD_CREATED,
        counts={
            LoyaltyTier.RUBY: 80,
            LoyaltyTier.EMERALD: 0,
            LoyaltyTier.SAPPHIRE: 0,
            LoyaltyTier.AMETHYST: 0,
        },
        assignments={user_id: LoyaltyTier.RUBY for user_id in range(80)},
    )
    summary = preview_summary(plan)
    text = collected_text(summary)
    assert len(text) < TEXT_LIMIT
    assert loyalty_threshold_copy() in text
    assert "10+" not in text
    assert "более 5" not in text
    assert grouped_assignments(plan)[LoyaltyTier.RUBY]


def test_plan_json_roundtrip_keeps_exclusive_assignments() -> None:
    plan = LoyaltyPlan(
        as_of=datetime(2025, 9, 4, tzinfo=UTC),
        guild_created_at=GUILD_CREATED,
        counts={
            LoyaltyTier.RUBY: 1,
            LoyaltyTier.EMERALD: 1,
            LoyaltyTier.SAPPHIRE: 0,
            LoyaltyTier.AMETHYST: 2,
        },
        assignments={
            1: LoyaltyTier.RUBY,
            2: LoyaltyTier.EMERALD,
            3: LoyaltyTier.AMETHYST,
            4: LoyaltyTier.AMETHYST,
        },
        skipped_today=1,
        skipped_bots=2,
        skipped_silent=3,
        considered=10,
    )
    restored = LoyaltyPlan.from_json(plan.to_json())
    assert restored.assignments == plan.assignments
    assert set(restored.assignments.values()) <= set(LoyaltyTier)
    assert restored.counts[LoyaltyTier.RUBY] == 1
    assert restored.skipped_silent == 3
    assert restored.eligible == 4
    assert restored.accounted == restored.considered


def test_count_registry_mismatches_ignores_bots() -> None:
    config = SimpleNamespace(ruby_role_id=10, emerald_role_id=20, sapphire_role_id=None, amethyst_role_id=None)
    registry = {1: LoyaltyTier.RUBY, 3: LoyaltyTier.RUBY}
    members = [(1, {10, 20}, False), (2, {10}, True), (3, {10}, False)]
    assert count_registry_mismatches(members, config, registry) == 1


class _Role:
    def __init__(self, role_id: int) -> None:
        self.id = role_id
        self.mention = f"<@&{role_id}>"

    def __eq__(self, other: object) -> bool:
        return getattr(other, "id", None) == self.id


class _FakeRoles:
    def __init__(self) -> None:
        self.claims: list[object] = []

    async def apply(self, claim: object) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True, called=True)


async def test_apply_plan_skips_missing_and_counts_ok() -> None:
    ruby = _Role(10)
    roles = _FakeRoles()
    guild = SimpleNamespace(
        id=1,
        get_role=lambda role_id: ruby if role_id == 10 else None,
        get_member=lambda user_id: None if user_id == 2 else SimpleNamespace(id=user_id, roles=[]),
    )
    service = LoyaltyService(SimpleNamespace(roles=roles))
    plan = LoyaltyPlan(
        as_of=datetime(2026, 9, 5, tzinfo=UTC),
        guild_created_at=GUILD_CREATED,
        counts={LoyaltyTier.RUBY: 2},
        assignments={1: LoyaltyTier.RUBY, 2: LoyaltyTier.RUBY},
        considered=2,
    )
    stats = await service.apply_plan(guild, plan, {LoyaltyTier.RUBY: 10})
    assert stats.applied == 1
    assert stats.skipped == 1
    assert stats.errors == 0
    assert len(roles.claims) == 1
    assert roles.claims[0].subsystem is RoleSubsystem.LOYALTY


async def test_grant_without_frozen_plan_is_denied(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}")
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = LoyaltyService(SimpleNamespace(sessions=sessions))
    try:
        denial = await service.decide_grant(1)
        assert denial == GrantDenial("no_plan")
        text = collected_text(grant_denial_view(denial))
        assert copy.LOYALTY_NO_PLAN_TITLE in text
        assert copy.LOYALTY_NO_PLAN in text
        assert await service.has_confirmed_plan(1) is False
    finally:
        await engine.dispose()


async def test_enforce_member_skips_when_old_roles_unbound() -> None:
    roles = _FakeRoles()
    service = LoyaltyService(
        SimpleNamespace(
            sessions=None,
            config_by_guild={},
            roles=roles,
            log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
        )
    )
    member = SimpleNamespace(
        id=3,
        bot=False,
        roles=(),
        guild=SimpleNamespace(id=1, get_role=lambda _rid: None, get_member=lambda _uid: None),
    )
    await service.enforce_member(member)
    assert roles.claims == []


async def test_loyalty_audit_log_debounced() -> None:
    import asyncio

    from conductor.cogs.loyalty import Loyalty
    from conductor.services.queue import ActionQueue

    clock = [0.0]
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, name: str = "") -> asyncio.Task[object]:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    queue = ActionQueue(spawn, clock=lambda: clock[0])
    calls: list[int] = []

    class _Logs:
        def __init__(self) -> None:
            calls.append(1)

        def __aiter__(self) -> object:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    old = SimpleNamespace(id=10)
    guild = SimpleNamespace(
        id=7,
        me=SimpleNamespace(id=99, guild_permissions=SimpleNamespace(view_audit_log=True)),
        audit_logs=lambda **_kwargs: _Logs(),
    )
    config = SimpleNamespace(ruby_role_id=10, emerald_role_id=None, sapphire_role_id=None, amethyst_role_id=None)
    enforced: list[int] = []

    class _Service:
        async def enforce_member(self, member: object, **_kwargs: object) -> None:
            enforced.append(getattr(member, "id", 0))

    bot = SimpleNamespace(
        queue=queue,
        config_by_guild={7: config},
        loyalty=_Service(),
    )
    cog = Loyalty(bot)  # type: ignore[arg-type]
    for index in range(50):
        before = SimpleNamespace(roles=(), guild=guild, bot=False, id=index + 1)
        after = SimpleNamespace(roles=(old,), guild=guild, bot=False, id=index + 1)
        await cog.on_member_update(before, after)  # type: ignore[arg-type]
    assert calls == []
    clock[0] = 5.0
    await queue.fire_due()
    if spawned:
        await asyncio.gather(*spawned)
    assert calls == [1]
    assert len(enforced) == 50
