from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from conductor.db import engine as engine_module
from conductor.presentation.icons import bind

# Modules that exercise Alembic itself and must run the real chain every time.
_REAL_MIGRATION_MODULES = frozenset({"tests.test_migrations", "test_migrations"})


@pytest.fixture(autouse=True)
def _reset_application_icons() -> None:
    bind()
    yield
    bind()


@pytest.fixture(scope="session")
def schema_dump(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, ...]:
    """SQL of a fresh database at Alembic head, produced once per session.

    Running the full migration chain costs ~1 s on SQLite; most service tests
    only need an empty schema at head. The chain runs once into a template file,
    and the template is replayed as plain DDL/DML for every other test.
    """
    template = tmp_path_factory.mktemp("schema") / "template.db"
    sync = create_sync_engine(f"sqlite:///{template.as_posix()}")
    try:
        with sync.connect() as conn:
            engine_module.upgrade_sync(conn, "head")
            conn.commit()
    finally:
        sync.dispose()
    raw = sqlite3.connect(template)
    try:
        statements = tuple(
            statement
            for statement in raw.iterdump()
            if statement not in {"BEGIN TRANSACTION;", "COMMIT;"} and "sqlite_sequence" not in statement
        )
    finally:
        raw.close()
    return statements


@pytest.fixture(autouse=True)
def _fast_head_schema(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    schema_dump: tuple[str, ...],
) -> Iterator[None]:
    """Replace the head upgrade of an empty SQLite database with a template replay.

    Opt out per test with ``@pytest.mark.real_migrations``; ``tests/test_migrations.py``
    always runs the real chain.
    """
    module = getattr(request.node, "module", None)
    module_name = getattr(module, "__name__", "")
    if module_name in _REAL_MIGRATION_MODULES or request.node.get_closest_marker("real_migrations"):
        yield
        return

    original = engine_module.upgrade_sync

    def fast_upgrade(sync_conn, revision: str = "head") -> None:
        if revision != "head" or sync_conn.dialect.name != "sqlite" or inspect(sync_conn).get_table_names():
            original(sync_conn, revision)
            return
        for statement in schema_dump:
            sync_conn.exec_driver_sql(statement)

    monkeypatch.setattr(engine_module, "upgrade_sync", fast_upgrade)
    yield
