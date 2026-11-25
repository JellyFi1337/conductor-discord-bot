from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.session import session_scope
from conductor.domain.access import (
    ACTION_DEFAULTS,
    ACTION_KEYS,
    ADMIN_PANEL_OPEN,
    CONFIG_MANAGE,
    HELP_READ,
    KEY_SPECS,
    MOD_BAN,
    MOD_KICK,
    REMIND_USE,
    TICKETS_CLAIM,
    TICKETS_CLOSE_OWN,
    TICKETS_NOTES,
    AccessSnapshot,
    KeyTier,
    LockoutBlocked,
    LockoutRisk,
    PermissionRule,
    Rank,
    StaffTargetBlock,
    authorize,
    bypasses_limits,
    can,
    can_punish,
    decide,
    empty_rank_roles,
    granting_role_ids,
    lockout_risk,
    parse_rank_list,
    rank_holding_role,
    resolve_rank,
    scheme_drift_keys,
    special_locked_now,
    staff_target_block,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text, denied
from conductor.services.access import (
    RankRoleTaken,
    apply_scheme_defaults,
    cached_decide,
    dump_permissions_yaml,
    ensure_defaults,
    example_permissions_path,
    export_payload,
    import_yaml,
    load_snapshot,
    log_limit_bypass,
    parse_permissions_yaml,
    set_key_extra_roles,
    set_key_ranks,
    set_rank_role,
    unlock_if_orphaned,
)
from conductor.services.cache import CacheHub

REPO = Path(__file__).resolve().parents[1]
ADMIN_PY = REPO / "src" / "conductor" / "cogs" / "admin.py"
ACCESS_PY = REPO / "src" / "conductor" / "domain" / "access.py"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _sessions(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    return create_sessionmaker(engine), engine


_OLD_TICKET_RANKS = parse_rank_list("watcher, senior, chief, special")
_OLD_KICK_RANKS = parse_rank_list("senior, chief, special")
_SCHEME_TICKET_RANKS = parse_rank_list("senior, chief, special")
_SCHEME_KICK_RANKS = parse_rank_list("chief, special")


def test_no_role_is_denied_for_config_manage() -> None:
    assert resolve_rank((), {}) is Rank.NONE
    assert can(Rank.NONE, key=CONFIG_MANAGE) is False


def test_rank_is_the_maximum_across_roles() -> None:
    ranks = {Rank.WATCHER: (11,), Rank.CHIEF: (33,), Rank.SPECIAL: (44,)}
    assert resolve_rank((11, 33), ranks) is Rank.CHIEF
    assert resolve_rank((11,), ranks) is Rank.WATCHER
    assert resolve_rank((99,), ranks) is Rank.NONE


@pytest.mark.parametrize("actor", list(Rank))
@pytest.mark.parametrize("target", list(Rank))
def test_can_punish_all_pairs(actor: Rank, target: Rank) -> None:
    assert can_punish(actor, target) is (actor > target)


def test_watcher_cannot_touch_senior_and_bot_is_rejected() -> None:
    watcher_vs_senior = staff_target_block(actor=Rank.WATCHER, target_rank=Rank.SENIOR, target_is_bot=False)
    chief_vs_chief = staff_target_block(actor=Rank.CHIEF, target_rank=Rank.CHIEF, target_is_bot=False)
    assert watcher_vs_senior is StaffTargetBlock.HIERARCHY
    assert staff_target_block(actor=Rank.SENIOR, target_rank=Rank.WATCHER, target_is_bot=False) is None
    assert staff_target_block(actor=Rank.SPECIAL, target_rank=Rank.NONE, target_is_bot=True) is StaffTargetBlock.BOT
    assert chief_vs_chief is StaffTargetBlock.HIERARCHY


def test_granting_role_ids_are_the_bound_roles_the_member_has() -> None:
    ranks = {Rank.WATCHER: (11, 12), Rank.CHIEF: (33,)}
    assert granting_role_ids((11, 99), Rank.WATCHER, ranks) == (11,)
    assert granting_role_ids((33,), Rank.WATCHER, ranks) == ()
    assert granting_role_ids((11,), Rank.NONE, ranks) == ()


def test_administrator_without_rank_has_no_keys_once_special_is_set() -> None:
    from conductor.domain.access import AccessSnapshot, PermissionRule, empty_rank_roles

    snapshot = AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.SPECIAL: (100,)},
        rules={CONFIG_MANAGE: PermissionRule(CONFIG_MANAGE, ACTION_DEFAULTS[CONFIG_MANAGE])},
        special_locked=True,
    )
    decision = decide(snapshot, key=CONFIG_MANAGE, member_role_ids=(), is_administrator=True)
    assert decision.allowed is False
    assert decision.fallback_admin is False


def test_developer_bypasses_special_lock() -> None:
    decision = authorize(
        rank=Rank.NONE,
        key=CONFIG_MANAGE,
        allowed=ACTION_DEFAULTS[CONFIG_MANAGE],
        is_administrator=False,
        special_locked=True,
        is_developer=True,
    )
    assert decision.allowed is True
    assert decision.developer is True
    assert decision.rank is Rank.SPECIAL


def test_administrator_fallback_works_until_special_is_bound() -> None:
    decision = authorize(
        rank=Rank.NONE,
        key=CONFIG_MANAGE,
        allowed=ACTION_DEFAULTS[CONFIG_MANAGE],
        is_administrator=True,
        special_locked=False,
    )
    assert decision.allowed is True
    assert decision.fallback_admin is True


def test_extra_role_grants_key_without_rank() -> None:
    assert can(
        Rank.NONE,
        key=CONFIG_MANAGE,
        allowed=ACTION_DEFAULTS[CONFIG_MANAGE],
        extra_role_ids=(7,),
        member_role_ids=(7,),
    )
    assert (
        can(
            Rank.NONE,
            key=CONFIG_MANAGE,
            allowed=ACTION_DEFAULTS[CONFIG_MANAGE],
            extra_role_ids=(7,),
            member_role_ids=(8,),
        )
        is False
    )


def test_remind_use_is_open_to_everyone() -> None:
    assert can(Rank.NONE, key="remind.use") is True
    assert can(Rank.WATCHER, key="remind.use") is True


def test_pulse_staff_stays_with_watchers() -> None:
    assert can(Rank.NONE, key="pulse.staff") is False
    assert can(Rank.WATCHER, key="pulse.staff") is True


def test_empty_yaml_parses_without_exception() -> None:
    for raw in ("", "   \n", "{}", "---", "# only comment\n", "ranks:\nactions: {}\n"):
        payload = parse_permissions_yaml(raw)
        assert payload.ranks == {}
        assert payload.actions == {}


def test_registry_keys_match_the_shipped_example() -> None:
    """`data/permissions.yml.example` — единственная опубликованная копия реестра."""
    example = parse_permissions_yaml(example_permissions_path().read_text(encoding="utf-8"))
    assert tuple(example.actions) == ACTION_KEYS
    assert tuple(item.key for item in KEY_SPECS) == ACTION_KEYS
    for spec in KEY_SPECS:
        assert spec.label.strip(), spec.key
        assert spec.domain.strip(), spec.key
        assert spec.tier in KeyTier
        assert parse_rank_list(example.actions[spec.key]) == spec.defaults, spec.key
    dumped = dump_permissions_yaml(example)
    roundtrip = parse_permissions_yaml(dumped)
    assert set(roundtrip.actions) == set(ACTION_KEYS)


def test_every_registry_key_is_used_outside_registry() -> None:
    access_text = ACCESS_PY.read_text(encoding="utf-8")
    start = access_text.index("KEY_SPECS")
    end = access_text.index("ACTION_DEFAULTS")
    blobs = [access_text[:start] + access_text[end:]]
    for path in (REPO / "src").rglob("*.py"):
        if path.resolve() == ACCESS_PY.resolve():
            continue
        blobs.append(path.read_text(encoding="utf-8"))
    haystack = "\n".join(blobs)
    missing = [
        key
        for key in ACTION_KEYS
        if f'"{key}"' not in haystack
        and f"'{key}'" not in haystack
        and key.upper().replace(".", "_") not in haystack
    ]
    assert missing == [], missing


def test_customer_scheme_defaults() -> None:
    assert ACTION_DEFAULTS[TICKETS_CLAIM] == _SCHEME_TICKET_RANKS
    assert ACTION_DEFAULTS[TICKETS_CLOSE_OWN] == _SCHEME_TICKET_RANKS
    assert ACTION_DEFAULTS[TICKETS_NOTES] == _SCHEME_TICKET_RANKS
    assert ACTION_DEFAULTS[MOD_KICK] == _SCHEME_KICK_RANKS
    assert can(Rank.WATCHER, key=TICKETS_CLAIM) is False
    assert can(Rank.SENIOR, key=TICKETS_CLAIM) is True
    assert can(Rank.SENIOR, key=MOD_KICK) is False
    assert can(Rank.CHIEF, key=MOD_KICK) is True


def test_bypasses_limits_only_for_administrator_or_owner() -> None:
    assert bypasses_limits(is_administrator=False, is_guild_owner=False) is False
    assert bypasses_limits(is_administrator=True, is_guild_owner=False) is True
    assert bypasses_limits(is_administrator=False, is_guild_owner=True) is True
    assert bypasses_limits(is_administrator=True, is_guild_owner=True) is True


def test_administrator_without_rank_still_cannot_ban() -> None:
    decision = authorize(
        rank=Rank.NONE,
        key=MOD_BAN,
        allowed=ACTION_DEFAULTS[MOD_BAN],
        is_administrator=True,
        special_locked=True,
    )
    assert decision.allowed is False
    assert decision.fallback_admin is False
    assert bypasses_limits(is_administrator=True, is_guild_owner=False) is True


def test_scheme_drift_counts_keys_that_differ_from_defaults() -> None:
    fresh = AccessSnapshot(rank_roles=empty_rank_roles(), rules={}, special_locked=False)
    assert scheme_drift_keys(fresh) == ()
    old = AccessSnapshot(
        rank_roles=empty_rank_roles(),
        rules={
            TICKETS_CLAIM: PermissionRule(TICKETS_CLAIM, _OLD_TICKET_RANKS),
            TICKETS_CLOSE_OWN: PermissionRule(TICKETS_CLOSE_OWN, _OLD_TICKET_RANKS),
            TICKETS_NOTES: PermissionRule(TICKETS_NOTES, _OLD_TICKET_RANKS),
            MOD_KICK: PermissionRule(MOD_KICK, _OLD_KICK_RANKS),
        },
        special_locked=False,
    )
    assert scheme_drift_keys(old) == (TICKETS_CLAIM, TICKETS_CLOSE_OWN, TICKETS_NOTES, MOD_KICK)


def test_denial_actors_come_from_live_needed_rank() -> None:
    old_kick = AccessSnapshot(
        rank_roles=empty_rank_roles(),
        rules={MOD_KICK: PermissionRule(MOD_KICK, _OLD_KICK_RANKS)},
        special_locked=True,
    )
    live = decide(old_kick, key=MOD_KICK, member_role_ids=(), is_administrator=False)
    assert live.need is Rank.SENIOR
    assert copy.denied_actors(live.need.key) == "Это делают старшие следящие и выше."
    scheme = decide(
        AccessSnapshot(rank_roles=empty_rank_roles(), rules={}, special_locked=True),
        key=MOD_KICK,
        member_role_ids=(),
        is_administrator=False,
    )
    assert scheme.need is Rank.CHIEF
    assert copy.denied_actors(scheme.need.key) == "Это делают главные следящие и выше."


def test_denial_card_names_the_needed_rank() -> None:
    text = collected_text(denied(copy.CONFIG_DENIED, need=copy.RANK_SPECIAL))
    assert copy.DENIED_TITLE in text
    assert copy.CONFIG_DENIED in text
    assert copy.denied_need(copy.RANK_SPECIAL) in text


def test_config_cog_is_not_admin_gated() -> None:
    source = ADMIN_PY.read_text(encoding="utf-8")
    assert "admin_only" not in source
    assert "default_permissions" not in source


def test_domain_access_has_no_discord_or_sqlalchemy() -> None:
    source = ACCESS_PY.read_text(encoding="utf-8")
    assert "discord" not in source
    assert "sqlalchemy" not in source


async def test_empty_guild_seeds_without_crash(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            snapshot = await load_snapshot(session, 1)
        assert set(snapshot.rules) == set(ACTION_KEYS)
        assert snapshot.special_locked is False
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 1)
            again = await load_snapshot(session, 1)
        assert set(again.rules) == set(ACTION_KEYS)
    finally:
        await engine.dispose()


async def test_ensure_defaults_survives_parallel_inserts(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)

    async def seed() -> None:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 7)

    try:
        await asyncio.gather(seed(), seed(), seed())
        async with session_scope(sessions) as session:
            snapshot = await load_snapshot(session, 7)
        assert set(snapshot.rules) == set(ACTION_KEYS)
    finally:
        await engine.dispose()


async def test_empty_yaml_import_does_not_raise(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            snapshot = await import_yaml(session, 9, "")
        assert set(snapshot.rules) == set(ACTION_KEYS)
        assert snapshot.rank_roles[Rank.SPECIAL] == ()
    finally:
        await engine.dispose()


async def test_special_lock_clears_when_special_unbound(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_rank_role(session, 3, Rank.SPECIAL, 555)
            locked = await load_snapshot(session, 3)
        assert locked.special_locked is True
        assert special_locked_now(locked.rank_roles, ever_bound=True) is True
        async with session_scope(sessions) as session:
            cleared = await import_yaml(session, 3, "ranks:\n  special: []\nactions: {}\n")
        assert cleared.special_locked is False
        assert cleared.rank_roles[Rank.SPECIAL] == ()
        decision = decide(cleared, key=CONFIG_MANAGE, member_role_ids=(), is_administrator=True)
        assert decision.allowed is True
        async with session_scope(sessions) as session:
            await set_rank_role(session, 3, Rank.SPECIAL, 777)
            assert await unlock_if_orphaned(session, 3, known_role_ids=()) is True
            orphaned = await load_snapshot(session, 3)
        assert orphaned.rank_roles[Rank.SPECIAL] == ()
        assert orphaned.special_locked is False
    finally:
        await engine.dispose()


def test_rank_holding_role_skips_the_rank_being_rewritten() -> None:
    ranks = empty_rank_roles() | {Rank.WATCHER: (10,), Rank.SENIOR: (20,)}
    assert rank_holding_role(ranks, 10) is Rank.WATCHER
    assert rank_holding_role(ranks, 10, except_rank=Rank.WATCHER) is None
    assert rank_holding_role(ranks, 10, except_rank=Rank.SENIOR) is Rank.WATCHER
    assert rank_holding_role(ranks, 99) is None


def test_config_overlap_card_names_the_owner() -> None:
    view = denied(copy.config_role_taken(copy.rank_label(Rank.WATCHER.key)))
    text = collected_text(view)
    assert copy.RANK_WATCHER in text
    assert "другую" in text


async def test_set_rank_role_refuses_id_owned_by_another_rank(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_rank_role(session, 5, Rank.WATCHER, 10)
        async with session_scope(sessions) as session:
            with pytest.raises(RankRoleTaken) as taken:
                await set_rank_role(session, 5, Rank.SENIOR, 10)
            assert taken.value.owner is Rank.WATCHER
            snapshot = await load_snapshot(session, 5)
        assert snapshot.rank_roles[Rank.WATCHER] == (10,)
        assert snapshot.rank_roles[Rank.SENIOR] == ()
    finally:
        await engine.dispose()


async def test_config_write_then_read_without_restart(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_rank_role(session, 4, Rank.CHIEF, 22)
            await set_key_ranks(session, 4, "stats.read", parse_rank_list("special"))
            snapshot = await load_snapshot(session, 4)
        assert snapshot.rank_roles[Rank.CHIEF] == (22,)
        assert snapshot.rule("stats.read").ranks == frozenset({Rank.SPECIAL})
        yaml_text = dump_permissions_yaml(export_payload(snapshot))
        assert "chief: [22]" in yaml_text
        assert "stats.read" in yaml_text
        cache = CacheHub()
        cache.permissions.set((4, "*"), snapshot)
        cache.ranks.set((4, 8), Rank.CHIEF)
        cache.invalidate_access(4)
        assert cache.permissions.get((4, "*")) is None
        assert cache.ranks.get((4, 8)) is None
    finally:
        await engine.dispose()


async def test_example_file_import_fills_defaults(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        raw = example_permissions_path().read_text(encoding="utf-8")
        async with session_scope(sessions) as session:
            snapshot = await import_yaml(session, 2, raw)
        assert snapshot.rule(CONFIG_MANAGE).ranks == ACTION_DEFAULTS[CONFIG_MANAGE]
        assert snapshot.special_locked is False
    finally:
        await engine.dispose()


async def test_cached_decide_logs_fallback_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions, engine = await _sessions(tmp_path)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        "conductor.services.access.log_fallback_admin",
        lambda **kwargs: seen.append(kwargs),
    )
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        is_developer=lambda _uid: False,
        roles=SimpleNamespace(bind=lambda *_args, **_kwargs: None),
    )
    try:
        decision = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=7,
            member_role_ids=(),
            is_administrator=True,
            key=CONFIG_MANAGE,
        )
        assert decision.allowed is True
        assert decision.fallback_admin is True
        assert seen == [{"guild_id": 1, "user_id": 7, "key": CONFIG_MANAGE}]
        seen.clear()
        async with session_scope(sessions) as session:
            await set_rank_role(session, 1, Rank.SPECIAL, 100)
        bot.caches.invalidate_access(1)
        locked = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=7,
            member_role_ids=(),
            is_administrator=True,
            key=CONFIG_MANAGE,
        )
        assert locked.allowed is False
        assert locked.fallback_admin is False
        assert seen == []
    finally:
        await engine.dispose()


async def test_ensure_defaults_keeps_stored_ranks(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_key_ranks(session, 12, TICKETS_CLAIM, _OLD_TICKET_RANKS)
            await ensure_defaults(session, 12)
            snapshot = await load_snapshot(session, 12)
        assert snapshot.rule(TICKETS_CLAIM).ranks == _OLD_TICKET_RANKS
        assert TICKETS_CLAIM in scheme_drift_keys(snapshot)
    finally:
        await engine.dispose()


async def test_apply_scheme_defaults_is_idempotent(tmp_path: Path) -> None:
    from sqlalchemy import select

    from conductor.db.models import GuildPermission

    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_key_ranks(session, 11, TICKETS_CLAIM, _OLD_TICKET_RANKS)
            await set_key_ranks(session, 11, MOD_KICK, _OLD_KICK_RANKS)
            row = await session.scalar(
                select(GuildPermission).where(
                    GuildPermission.guild_id == 11,
                    GuildPermission.key == TICKETS_CLAIM,
                )
            )
            assert row is not None
            row.extra_role_ids = "[99]"
            drifted = await load_snapshot(session, 11)
        assert {TICKETS_CLAIM, MOD_KICK} <= set(scheme_drift_keys(drifted))
        async with session_scope(sessions) as session:
            first = await apply_scheme_defaults(session, 11)
            second = await apply_scheme_defaults(session, 11)
        assert scheme_drift_keys(first) == ()
        assert scheme_drift_keys(second) == ()
        assert first.rule(TICKETS_CLAIM).ranks == ACTION_DEFAULTS[TICKETS_CLAIM]
        assert first.rule(MOD_KICK).ranks == ACTION_DEFAULTS[MOD_KICK]
        assert 99 in first.rule(TICKETS_CLAIM).extra_role_ids
        assert can(Rank.WATCHER, allowed=first.rule(TICKETS_CLAIM).ranks) is False
        assert can(Rank.SENIOR, allowed=first.rule(MOD_KICK).ranks) is False
    finally:
        await engine.dispose()


def test_log_limit_bypass_dedups_per_person_and_kind() -> None:
    submitted: list[object] = []
    bot = SimpleNamespace(
        caches=CacheHub(),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=99)},
        log_pump=SimpleNamespace(submit=lambda *args, **kwargs: submitted.append((args, kwargs))),
    )
    assert log_limit_bypass(1, 7, "timeout", bot=bot) is True  # type: ignore[arg-type]
    assert log_limit_bypass(1, 7, "timeout", bot=bot) is False  # type: ignore[arg-type]
    assert log_limit_bypass(1, 7, "ban", bot=bot) is True  # type: ignore[arg-type]
    assert log_limit_bypass(1, 8, "timeout", bot=bot) is True  # type: ignore[arg-type]
    assert len(submitted) == 3
    first_view = submitted[0][1]["view"]  # type: ignore[index]
    text = collected_text(first_view)
    assert copy.LIMIT_BYPASS_TITLE in text
    assert "timeout" in text
    assert "<@7>" in text


def test_lockout_risk_ignores_non_govern_keys() -> None:
    ranks = empty_rank_roles() | {Rank.CHIEF: (1,)}
    assert (
        lockout_risk(key=MOD_BAN, wanted={Rank.CHIEF}, actor=Rank.SPECIAL, rank_roles=ranks) is LockoutRisk.NONE
    )


def test_administrator_locked_out_except_open_keys() -> None:
    snapshot = AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.SPECIAL: (100,)},
        rules={},
        special_locked=True,
    )
    open_keys = {HELP_READ, REMIND_USE}
    for key in ACTION_KEYS:
        decision = decide(snapshot, key=key, member_role_ids=(), is_administrator=True)
        if key in open_keys:
            assert decision.allowed is True, key
        else:
            assert decision.allowed is False, key
    assert bypasses_limits(is_administrator=True, is_guild_owner=False) is True


def test_suspended_authorize_and_decide_agree() -> None:
    snapshot = AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.CHIEF: (7,)},
        rules={},
        special_locked=True,
    )
    for key in (CONFIG_MANAGE, ADMIN_PANEL_OPEN, HELP_READ, "mod.warn"):
        for suspended in (False, True):
            for is_admin in (False, True):
                for is_dev in (False, True):
                    via_decide = decide(
                        snapshot,
                        key=key,
                        member_role_ids=(7,),
                        is_administrator=is_admin,
                        is_developer=is_dev,
                        suspended=suspended,
                    )
                    via_auth = authorize(
                        rank=Rank.CHIEF,
                        key=key,
                        allowed=snapshot.rule(key).ranks,
                        extra_role_ids=snapshot.rule(key).extra_role_ids,
                        member_role_ids=(7,),
                        is_administrator=is_admin,
                        special_locked=True,
                        is_developer=is_dev,
                        suspended=suspended,
                    )
                    assert via_decide.allowed == via_auth.allowed


async def test_govern_key_without_holder_does_not_write(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 4)
            before = await load_snapshot(session, 4)
            with pytest.raises(LockoutBlocked) as blocked:
                await set_key_ranks(session, 4, CONFIG_MANAGE, (Rank.WATCHER,), actor=Rank.SPECIAL)
            assert blocked.value.risk is LockoutRisk.NO_HOLDER
            after = await load_snapshot(session, 4)
        assert after.rule(CONFIG_MANAGE).ranks == before.rule(CONFIG_MANAGE).ranks
        assert after.rule(CONFIG_MANAGE).ranks == ACTION_DEFAULTS[CONFIG_MANAGE]
    finally:
        await engine.dispose()


async def test_extra_roles_roundtrip_export_import(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    try:
        async with session_scope(sessions) as session:
            await set_rank_role(session, 8, Rank.SPECIAL, 11)
            await set_key_extra_roles(session, 8, "mod.purge", (42, 43))
            snapshot = await load_snapshot(session, 8)
            text = dump_permissions_yaml(export_payload(snapshot))
            imported = await import_yaml(session, 8, text)
        assert imported.rule("mod.purge").extra_role_ids == frozenset({42, 43})
        assert imported.rank_roles[Rank.SPECIAL] == (11,)
        assert can(
            Rank.NONE,
            key="mod.purge",
            extra_role_ids=imported.rule("mod.purge").extra_role_ids,
            member_role_ids=(42,),
        )
    finally:
        await engine.dispose()
