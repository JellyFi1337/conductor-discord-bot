from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.session import session_scope
from conductor.domain.loyalty import LoyaltyTier
from conductor.services.old_roles import (
    authorized_role_id,
    count_registry_mismatches,
    load_registry,
    old_role_id_set,
    old_roles_from_config,
    upsert_registry_members,
)


def _config(**roles: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        ruby_role_id=roles.get("ruby", 1),
        emerald_role_id=roles.get("emerald", 2),
        sapphire_role_id=roles.get("sapphire"),
        amethyst_role_id=roles.get("amethyst", 4),
    )


def test_old_roles_from_config_skips_empty() -> None:
    mapping = old_roles_from_config(_config(sapphire=None))
    assert mapping[LoyaltyTier.RUBY] == 1
    assert mapping[LoyaltyTier.EMERALD] == 2
    assert LoyaltyTier.SAPPHIRE not in mapping
    assert mapping[LoyaltyTier.AMETHYST] == 4


def test_old_role_id_set_empty_without_config() -> None:
    assert old_role_id_set(None) == set()
    assert old_role_id_set(_config(sapphire=None)) == {1, 2, 4}


def test_authorized_role_id_reads_bound_tier() -> None:
    config = _config()
    registry = {10: LoyaltyTier.RUBY}
    assert authorized_role_id(config, registry, 10) == 1  # type: ignore[arg-type]
    assert authorized_role_id(config, registry, 99) is None  # type: ignore[arg-type]


def test_count_registry_mismatches_skips_bots_and_matches() -> None:
    config = _config()
    registry = {10: LoyaltyTier.RUBY}
    members = [
        (10, {1}, False),
        (11, {1}, False),
        (12, {1}, True),
        (13, set(), False),
    ]
    assert count_registry_mismatches(members, config, registry) == 1  # type: ignore[arg-type]


async def test_load_registry_roundtrip(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    try:
        async with session_scope(sessions) as session:
            await upsert_registry_members(
                session,
                guild_id=1,
                assignments={7: LoyaltyTier.EMERALD},
                config=None,
            )
        async with session_scope(sessions) as session:
            loaded = await load_registry(session, 1)
        assert loaded[7] is LoyaltyTier.EMERALD
    finally:
        await engine.dispose()
