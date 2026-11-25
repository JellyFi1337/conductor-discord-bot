from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import (
    Base,
    GuildConfig,
    LoyaltyRegistry,
    MemberRecord,
    VoiceRoom,
    VoiceRoomKick,
)
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.ladder import Step
from conductor.domain.loyalty import LoyaltyTier
from conductor.domain.ops import (
    WIPE_STEPS,
    MassMode,
    MatchMode,
    MemberSkip,
    MemberState,
    ProtectKind,
    RoleAudience,
    RoleSkip,
    WipeSection,
    gate_member,
    plan_grant_member,
    plan_old_import,
    plan_strip_member,
    protect_ids,
    registry_after_mass,
    role_skip,
    steps_for,
    wipe_section,
)
from conductor.presentation import copy
from conductor.presentation.layout import TEXT_LIMIT, collected_text
from conductor.presentation.ops import grant_confirm, old_import_done, strip_confirm, wipe_confirm, wipe_done
from conductor.services.cache import CacheHub
from conductor.services.old_roles import load_registry, seed_registry_from_guild, upsert_registry_members
from conductor.services.ops import LIFT_TENURE_DAYS, OwnerOps

STAMP = datetime(2026, 9, 5, 12, tzinfo=UTC)


async def _async_noop(_guild_id: int) -> None:
    return None


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def test_wipe_catalog_covers_every_table() -> None:
    names = {step.table for step in WIPE_STEPS}
    assert names == set(Base.metadata.tables)
    assert steps_for(WipeSection.ALL) == WIPE_STEPS
    assert {step.table for step in steps_for(WipeSection.LOYALTY)} == {
        "loyalty_registry",
        "loyalty_batches",
        "loyalty_role_events",
    }
    assert wipe_section("nope") is None
    assert wipe_section("all") is WipeSection.ALL


def test_wipe_copy_covers_every_section() -> None:
    values = {section.value for section in WipeSection}
    assert "lifestyle" not in values
    assert set(copy.OWNER_WIPE_HINTS) == values
    assert set(copy.OWNER_WIPE_LABELS) == values
    assert set(copy.OWNER_WIPE_CHOICES) == values


def test_role_skip_and_member_plans() -> None:
    assert role_skip(is_default=True, managed=False, above_bot=False) is RoleSkip.EVERYONE
    assert role_skip(is_default=False, managed=True, above_bot=False) is RoleSkip.MANAGED
    assert role_skip(is_default=False, managed=False, above_bot=True) is RoleSkip.HIERARCHY
    assert role_skip(is_default=False, managed=False, above_bot=False) is None
    bot = plan_strip_member(user_id=1, is_bot=True, have=(2, 3), targets=(2,))
    assert bot.skip is MemberSkip.BOT
    selected = plan_strip_member(user_id=2, is_bot=False, have=(2, 3, 4), targets=(2, 9))
    assert selected.remove == (2,)
    everyone = plan_strip_member(user_id=2, is_bot=False, have=(2, 3), targets=None)
    assert everyone.remove == (2, 3)
    empty = plan_strip_member(user_id=2, is_bot=False, have=(2,), targets=(9,))
    assert empty.skip is MemberSkip.EMPTY
    grant = plan_grant_member(user_id=2, is_bot=False, have=(2,), targets=(2, 3))
    assert grant.add == (3,)
    filtered = plan_grant_member(
        user_id=2,
        is_bot=False,
        have=(2,),
        targets=(3,),
        audience=RoleAudience(require=frozenset({9})),
    )
    assert filtered.skip is MemberSkip.REQUIRE


def test_audience_gates_grant_and_strip() -> None:
    without = RoleAudience(exclude=frozenset({5}))
    blocked = plan_grant_member(user_id=2, is_bot=False, have=(5, 8), targets=(3,), audience=without)
    assert blocked.skip is MemberSkip.EXCEPT
    allowed = plan_grant_member(user_id=2, is_bot=False, have=(8,), targets=(3,), audience=without)
    assert allowed.add == (3,)
    need_all = RoleAudience(require=frozenset({2, 4}), require_mode=MatchMode.ALL)
    missing = plan_strip_member(user_id=2, is_bot=False, have=(2, 9), targets=(9,), audience=need_all)
    assert missing.skip is MemberSkip.REQUIRE
    both = plan_strip_member(user_id=2, is_bot=False, have=(2, 4, 9), targets=(9,), audience=need_all)
    assert both.remove == (9,)
    except_all = RoleAudience(exclude=frozenset({5, 6}), exclude_mode=MatchMode.ALL)
    keep = plan_grant_member(user_id=2, is_bot=False, have=(5,), targets=(3,), audience=except_all)
    assert keep.add == (3,)
    drop = plan_grant_member(user_id=2, is_bot=False, have=(5, 6), targets=(3,), audience=except_all)
    assert drop.skip is MemberSkip.EXCEPT
    staff = RoleAudience(protect=frozenset({11}))
    guarded = plan_grant_member(user_id=2, is_bot=False, have=(11,), targets=(3,), audience=staff)
    assert guarded.skip is MemberSkip.PROTECT
    timed = plan_grant_member(
        user_id=2,
        is_bot=False,
        have=(8,),
        targets=(3,),
        audience=RoleAudience(skip_timeout=True),
        state=MemberState(timed_out=True),
    )
    assert timed.skip is MemberSkip.TIMEOUT
    assert protect_ids(ProtectKind.STAFF, staff=(1, 2), old=(9,)) == frozenset({1, 2})
    assert protect_ids(ProtectKind.OLD, staff=(1,), old=(9, 0)) == frozenset({9})
    assert protect_ids(ProtectKind.STAFF_OLD, staff=(1,), old=(9,)) == frozenset({1, 9})
    assert protect_ids(ProtectKind.NONE, staff=(1,), old=(9,)) == frozenset()
    assert gate_member({1}, RoleAudience(require=frozenset({2}))) is MemberSkip.REQUIRE


def test_registry_after_mass_and_old_import_plan() -> None:
    assert (
        registry_after_mass(
            current=LoyaltyTier.RUBY,
            add_tiers=(),
            remove_tiers=(),
            strip_all=True,
        )
        is None
    )
    assert (
        registry_after_mass(
            current=LoyaltyTier.RUBY,
            add_tiers=(LoyaltyTier.EMERALD,),
            remove_tiers=(),
            strip_all=False,
        )
        is LoyaltyTier.EMERALD
    )
    assert (
        registry_after_mass(
            current=LoyaltyTier.RUBY,
            add_tiers=(),
            remove_tiers=(LoyaltyTier.RUBY,),
            strip_all=False,
        )
        is None
    )
    assert (
        registry_after_mass(
            current=LoyaltyTier.RUBY,
            add_tiers=(),
            remove_tiers=(LoyaltyTier.EMERALD,),
            strip_all=False,
        )
        is LoyaltyTier.RUBY
    )
    plan = plan_old_import(
        ((1, False), (2, True), (3, False), (1, False)),
        {3: LoyaltyTier.RUBY},
        LoyaltyTier.RUBY,
    )
    assert plan.skipped_bot == 1
    assert plan.skipped_same == 1
    assert plan.added == 1
    assert plan.updated == 0
    assert plan.assignments == {1: LoyaltyTier.RUBY}
    again = plan_old_import(((3, False),), {3: LoyaltyTier.AMETHYST}, LoyaltyTier.RUBY)
    assert again.updated == 1
    assert again.assignments[3] is LoyaltyTier.RUBY


def test_owner_cards_fit_layout() -> None:
    async def _noop(_click: object) -> None:
        return None

    wipe = wipe_confirm(section=WipeSection.ALL, on_yes=_noop, on_no=_noop)
    assert copy.OWNER_WIPE_CONFIRM in collected_text(wipe)
    assert len(collected_text(wipe)) < TEXT_LIMIT
    done = wipe_done(section=WipeSection.TELEMETRY, rows=4)
    assert "телеметрию" in collected_text(done)
    strip = strip_confirm(
        mode=MassMode.ALL,
        roles="все",
        members=3,
        filters=copy.mass_filter_hint(without="<@&5>", protect="staff"),
        on_yes=_noop,
        on_no=_noop,
    )
    text = collected_text(strip)
    assert copy.OWNER_STRIP_VERB in text
    assert "Не трогаем" in text
    assert "следящие" in text
    grant = grant_confirm(
        roles="<@&3>",
        members=2,
        filters=copy.mass_filter_hint(have="<@&8>", have_mode="all"),
        on_yes=_noop,
        on_no=_noop,
    )
    assert "все:" in collected_text(grant)
    imported = old_import_done(
        added=2,
        updated=1,
        same=0,
        bots=1,
        enforced=2,
        failed=0,
        unbound=True,
    )
    assert "/admin panel" in collected_text(imported)


async def test_wipe_stays_inside_one_guild(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    refreshed: list[int] = []

    async def refresh(guild_id: int) -> None:
        refreshed.append(guild_id)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        config_by_guild={1: object(), 2: object()},
        refresh_config=refresh,
    )
    ops = OwnerOps(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, ruby_role_id=10))
            session.add(GuildConfig(guild_id=2, ruby_role_id=20))
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
            session.add(
                MemberRecord(
                    guild_id=2,
                    user_id=8,
                    username="bob",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
            session.add(LoyaltyRegistry(guild_id=1, user_id=7, tier=LoyaltyTier.RUBY.value, updated_at=STAMP))
            session.add(LoyaltyRegistry(guild_id=2, user_id=8, tier=LoyaltyTier.RUBY.value, updated_at=STAMP))
            room = VoiceRoom(
                guild_id=1,
                channel_id=100,
                owner_id=7,
                hub_id=1,
                created_at=STAMP,
            )
            session.add(room)
            await session.flush()
            session.add(VoiceRoomKick(room_id=room.id, user_id=9))
        report = await ops.wipe(1, WipeSection.ALL)
        assert report.tables["members"] == 1
        assert report.tables["loyalty_registry"] == 1
        assert report.tables["voice_room_kicks"] == 1
        assert report.tables["voice_rooms"] == 1
        assert report.tables["guild_configs"] == 1
        async with session_scope(sessions) as session:
            people = (await session.scalars(select(MemberRecord))).all()
            registry = (await session.scalars(select(LoyaltyRegistry))).all()
            configs = (await session.scalars(select(GuildConfig))).all()
            kicks = await session.scalar(select(func.count()).select_from(VoiceRoomKick))
        assert [(row.guild_id, row.user_id) for row in people] == [(2, 8)]
        assert [(row.guild_id, row.user_id) for row in registry] == [(2, 8)]
        assert [row.guild_id for row in configs] == [2]
        assert kicks == 0
        assert 1 not in bot.config_by_guild
        assert refreshed == [1]
    finally:
        await engine.dispose()


async def test_wipe_loyalty_leaves_other_sections(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        config_by_guild={1: object()},
        refresh_config=_async_noop,
    )
    ops = OwnerOps(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, ruby_role_id=10))
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
            session.add(LoyaltyRegistry(guild_id=1, user_id=7, tier=LoyaltyTier.RUBY.value, updated_at=STAMP))
        report = await ops.wipe(1, "loyalty")
        assert report.section is WipeSection.LOYALTY
        assert report.tables["loyalty_registry"] == 1
        async with session_scope(sessions) as session:
            people = await session.scalar(select(func.count()).select_from(MemberRecord))
            registry = await session.scalar(select(func.count()).select_from(LoyaltyRegistry))
            config = await session.get(GuildConfig, 1)
        assert people == 1
        assert registry == 0
        assert config is not None
        assert config.loyalty_registry_seeded_at is not None
    finally:
        await engine.dispose()


async def test_seed_does_not_refill_after_first_snapshot(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            config = GuildConfig(
                guild_id=1,
                ruby_role_id=10,
                loyalty_registry_seeded_at=STAMP,
            )
            session.add(config)
            added = await seed_registry_from_guild(
                session,
                guild_id=1,
                members=[(7, {10}, False)],
                config=config,
            )
            assert added == 0
            mapping = await load_registry(session, 1)
        assert mapping == {}
    finally:
        await engine.dispose()


async def test_upsert_registry_members_adds_and_updates(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            config = GuildConfig(guild_id=1)
            session.add(config)
            session.add(LoyaltyRegistry(guild_id=1, user_id=3, tier=LoyaltyTier.AMETHYST.value, updated_at=STAMP))
            added, updated = await upsert_registry_members(
                session,
                guild_id=1,
                assignments={1: LoyaltyTier.RUBY, 3: LoyaltyTier.RUBY},
                config=config,
            )
            assert added == 1
            assert updated == 1
            assert config.loyalty_registry_seeded_at is not None
            mapping = await load_registry(session, 1)
            assert mapping == {1: LoyaltyTier.RUBY, 3: LoyaltyTier.RUBY}
    finally:
        await engine.dispose()


async def test_lift_newcomer_writes_tenure_and_grants(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    granted: list[tuple[int, int, Step]] = []
    scores: list[int] = []
    verified: list[int] = []

    async def grant_held(guild_id: int, user_id: int, step: Step, *, now: datetime | None = None) -> Step:
        _ = now
        granted.append((guild_id, user_id, step))
        return step

    async def dossier(guild_id: int, user_id: int, *, now: datetime | None = None) -> SimpleNamespace:
        _ = guild_id, user_id, now
        return SimpleNamespace(score=12)

    async def adjust(guild_id: int, user_id: int, delta: int, *, kind: str, now: datetime | None = None) -> int:
        _ = guild_id, user_id, kind, now
        scores.append(delta)
        return 50

    async def grant(guild: object, member: object) -> None:
        _ = guild
        verified.append(int(member.id))

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        ladder=SimpleNamespace(grant_held=grant_held),
        reputation=SimpleNamespace(dossier=dossier, adjust=adjust),
        verification=SimpleNamespace(grant=grant),
    )
    ops = OwnerOps(bot)
    guild = SimpleNamespace(id=1)
    member = SimpleNamespace(id=7, display_name="ann", bot=False)
    try:
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
        report = await ops.lift_newcomer(guild, member, actor_id=99)  # type: ignore[arg-type]
        assert report.user_id == 7
        assert report.tenure_days >= LIFT_TENURE_DAYS
        assert report.step == Step.FRIEND.key
        assert report.score == 50
        assert report.verified is True
        assert granted == [(1, 7, Step.FRIEND)]
        assert scores == [38]
        assert verified == [7]
        async with session_scope(sessions) as session:
            row = await session.scalar(
                select(MemberRecord).where(MemberRecord.guild_id == 1, MemberRecord.user_id == 7)
            )
        assert row is not None
        assert row.first_joined_at is not None
        assert (STAMP - as_utc(row.first_joined_at)).days >= 300
    finally:
        await engine.dispose()


def test_seed_mode_command_copy() -> None:
    from inspect import signature

    from conductor.cogs.ops import Owner
    from conductor.domain.seed import SeedMode, SeedPlan, needs_connect

    assert copy.CMD_OWNER_SEED_MODE_BUILD == "создать недостающее"
    assert copy.CMD_OWNER_SEED_MODE_ADOPT == "только подтянуть"
    assert SeedMode.BUILD.value == "build"
    assert "mode" in signature(Owner.seed.callback).parameters
    empty = SeedPlan(create=("rules",), reuse=(), repair=())
    assert needs_connect(empty) is False
    assert bool(empty.create) is True

