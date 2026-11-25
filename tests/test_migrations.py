from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select

from conductor.db.engine import (
    INITIAL_REVISION,
    SchemaBehindError,
    alembic_config,
    apply_downgrade,
    apply_migrations,
    create_engine,
    create_sessionmaker,
    current_revision,
    init_db,
    is_legacy_database,
    require_schema_head,
    stamp_sync,
)
from conductor.db.models import Base, MemberRecord
from conductor.db.session import session_scope

ENGINE_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "db" / "engine.py"
LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "conductor.db"
PART11_REVISION = "0034"

pytestmark = pytest.mark.real_migrations


def _head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _named_indexes(inspector, table: str) -> set[str]:
    names: set[str] = set()
    for index in inspector.get_indexes(table):
        name = index.get("name")
        if name and not str(name).startswith("sqlite_autoindex"):
            names.add(str(name))
    return names


def _meta_indexes(table) -> set[str]:
    return {index.name for index in table.indexes if index.name}


def assert_schema_matches_metadata(sync_conn) -> None:
    inspector = inspect(sync_conn)
    db_tables = set(inspector.get_table_names()) - {"alembic_version"}
    meta_tables = set(Base.metadata.tables)
    assert db_tables == meta_tables
    for name, table in Base.metadata.tables.items():
        db_cols = {column["name"] for column in inspector.get_columns(name)}
        meta_cols = {column.name for column in table.columns}
        assert db_cols == meta_cols, name
        missing = _meta_indexes(table) - _named_indexes(inspector, name)
        assert not missing, f"{name}: missing indexes {sorted(missing)}"


async def test_empty_database_upgrade_matches_metadata(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        action = await apply_migrations(engine)
        assert action == "upgrade"
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == _head()
            await conn.run_sync(assert_schema_matches_metadata)
    finally:
        await engine.dispose()


async def test_legacy_database_is_stamped_without_losing_rows(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    sessions = create_sessionmaker(engine)
    joined = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        async with engine.begin() as conn:
            legacy_tables = [
                table
                for table in Base.metadata.sorted_tables
                if table.name
                not in {
                    "voice_sessions",
                    "guild_ranks",
                    "guild_permissions",
                    "guild_access_states",
                    "panels",
                    "role_changes",
                    "unofficial_admin_notices",
                    "watcher_duty",
                    "watcher_notes",
                    "watcher_suspensions",
                    "voice_hubs",
                    "voice_rooms",
                    "voice_room_kicks",
                    "voice_room_requests",
                    "voice_room_lfg",
                    "voice_room_merges",
                    "ticket_panels",
                    "ticket_categories",
                    "tickets",
                    "mod_settings",
                    "mod_cases",
                    "mod_intents",
                    "channel_locks",
                    "reputation_settings",
                    "reputation_tiers",
                    "reputation",
                    "reputation_events",
                    "ladder_settings",
                    "ladder",
                    "giveaway_settings",
                    "giveaways",
                    "giveaway_entries",
                    "giveaway_winners",
                    "polls",
                    "poll_votes",
                    "gatherings",
                    "gathering_rsvps",
                    "group_hubs",
                    "groups",
                    "group_members",
                    "group_archive_shelves",
                    "group_invites",
                    "reminders",
                    "showcases",
                    "pulse_docks",
                    "pulse_alert_settings",
                    "staff_actions",
                    "member_watch",
                    "automod_hits",
                    "automod_requests",
                    "verification_settings",
                    "self_role_menus",
                    "self_role_options",
                    "stats_boards",
                    "poll_settings",
                    "youtube_feeds",
                    "ticket_events",
                    "ticket_transcripts",
                    "guild_layout",
                }
            ]
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=legacy_tables))
            assert await conn.run_sync(is_legacy_database) is True
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=42,
                    username="ann",
                    display_name="Ann",
                    first_joined_at=joined,
                    last_joined_at=joined,
                )
            )
        action = await apply_migrations(engine)
        assert action == "stamp"
        async with engine.connect() as conn:
            assert await conn.run_sync(is_legacy_database) is False
            assert await conn.run_sync(current_revision) == _head()
            await conn.run_sync(assert_schema_matches_metadata)
        async with session_scope(sessions) as session:
            row = await session.scalar(select(MemberRecord).where(MemberRecord.user_id == 42))
            assert row is not None
            assert row.username == "ann"
    finally:
        await engine.dispose()


async def test_upgrade_downgrade_upgrade_roundtrip(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        await apply_migrations(engine)
        await apply_downgrade(engine, "base")
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            assert "members" not in tables
            assert await conn.run_sync(current_revision) is None
        await apply_migrations(engine)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == _head()
            await conn.run_sync(assert_schema_matches_metadata)
    finally:
        await engine.dispose()


async def test_auto_migrate_false_leaves_schema_untouched(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        await init_db(engine, auto_migrate=False)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
        assert "members" not in tables
        assert "alembic_version" not in tables
    finally:
        await engine.dispose()


async def test_require_schema_head_accepts_current_head(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        await apply_migrations(engine)
        await require_schema_head(engine)
    finally:
        await engine.dispose()


async def test_require_schema_head_rejects_empty_database(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        with pytest.raises(SchemaBehindError) as caught:
            await require_schema_head(engine)
        assert caught.value.current is None
        assert caught.value.head == _head()
        assert "conductor db upgrade" in str(caught.value)
    finally:
        await engine.dispose()


async def test_rate_profiles_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        await apply_migrations(engine)
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            assert "rate_profiles" in tables
            assert await conn.run_sync(current_revision) == _head()
        await apply_downgrade(engine, "0070")
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            assert "rate_profiles" not in tables
            assert await conn.run_sync(current_revision) == "0070"
        await apply_migrations(engine)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == _head()
            columns = await conn.run_sync(
                lambda sync_conn: {item["name"] for item in inspect(sync_conn).get_columns("rate_profiles")}
            )
            assert columns == {
                "guild_id",
                "bucket",
                "allowance",
                "frozen_until",
                "last_429_at",
                "updated_at",
            }
    finally:
        await engine.dispose()


async def test_require_schema_head_rejects_stale_revision(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    try:
        await apply_migrations(engine)
        async with engine.connect() as conn:
            await conn.run_sync(stamp_sync, INITIAL_REVISION)
            await conn.commit()
        with pytest.raises(SchemaBehindError) as caught:
            await require_schema_head(engine)
        assert caught.value.current == INITIAL_REVISION
        assert caught.value.head == _head()
    finally:
        await engine.dispose()


def test_hand_rolled_schema_patches_are_gone() -> None:
    source = ENGINE_PY.read_text(encoding="utf-8")
    assert "_add_missing_columns" not in source
    assert "_MEMBER_COLUMNS" not in source
    assert "_SCAN_COLUMNS" not in source
    assert "_CONFIG_COLUMNS" not in source
    assert "_ensure_indexes" not in source
    assert "ALTER TABLE" not in source
    assert "create_all" not in source


def _schema_columns(sync_conn) -> dict[str, frozenset[str]]:
    inspector = inspect(sync_conn)
    return {
        name: frozenset(column["name"] for column in inspector.get_columns(name))
        for name in inspector.get_table_names()
        if name != "alembic_version"
    }


def _copy_sqlite(source: Path, dest: Path) -> None:
    copy2(source, dest)
    for suffix in ("-wal", "-shm"):
        extra = source.with_name(source.name + suffix)
        if extra.is_file():
            copy2(extra, dest.with_name(dest.name + suffix))


async def test_empty_upgrade_head_downgrade_matches_part11(tmp_path: Path) -> None:
    reference = create_engine(_url(tmp_path / "part11"))
    try:
        await apply_migrations(reference, PART11_REVISION)
        async with reference.connect() as conn:
            expected = await conn.run_sync(_schema_columns)
            assert await conn.run_sync(current_revision) == PART11_REVISION
    finally:
        await reference.dispose()

    engine = create_engine(_url(tmp_path / "roundtrip"))
    try:
        await apply_migrations(engine)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == _head()
        await apply_downgrade(engine, PART11_REVISION)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == PART11_REVISION
            assert await conn.run_sync(_schema_columns) == expected
    finally:
        await engine.dispose()


async def test_live_database_copy_upgrade_head_downgrade_to_part11(tmp_path: Path) -> None:
    if not LIVE_DB.is_file():
        pytest.skip("нет data/conductor.db")
    dest = tmp_path / "conductor.db"
    _copy_sqlite(LIVE_DB, dest)
    reference = create_engine(_url(tmp_path / "part11"))
    try:
        await apply_migrations(reference, PART11_REVISION)
        async with reference.connect() as conn:
            expected = await conn.run_sync(_schema_columns)
    finally:
        await reference.dispose()

    engine = create_engine(f"sqlite+aiosqlite:///{dest.as_posix()}")
    try:
        await apply_migrations(engine)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == _head()
        await apply_downgrade(engine, PART11_REVISION)
        async with engine.connect() as conn:
            assert await conn.run_sync(current_revision) == PART11_REVISION
            assert await conn.run_sync(_schema_columns) == expected
    finally:
        await engine.dispose()
