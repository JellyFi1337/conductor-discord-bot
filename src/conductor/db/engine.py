from __future__ import annotations

from pathlib import Path

import structlog
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from conductor.db.models import Base

log = structlog.get_logger("conductor.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
INITIAL_REVISION = "0001"
_SQLITE_MEMORY = ":memory:"


class SchemaBehindError(RuntimeError):
    """Текущая ревизия Alembic не равна head — бот без AUTO_MIGRATE не стартует."""

    def __init__(self, current: str | None, head: str | None) -> None:
        self.current = current
        self.head = head
        super().__init__(
            f"Схема БД отстаёт: сейчас {current or 'none'}, нужен {head}. "
            "Выполните conductor db upgrade или задайте AUTO_MIGRATE=true."
        )


def _sqlite_file_path(url: str) -> Path | None:
    if "sqlite" not in url:
        return None
    raw = url.split("///", 1)[-1] if "///" in url else url
    token = raw.strip()
    if token == _SQLITE_MEMORY or token.startswith(f"{_SQLITE_MEMORY}:"):
        return None
    return Path(token)


def _ensure_sqlite_parent(url: str) -> None:
    path = _sqlite_file_path(url)
    if path is None:
        return
    if path.parent.as_posix() not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str) -> AsyncEngine:
    _ensure_sqlite_parent(database_url)
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    if "sqlite" in database_url:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_fast(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA cache_size=-65536")
            cursor.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("prepend_sys_path", "src")
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("version_path_separator", "os")
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def is_legacy_database(sync_conn) -> bool:
    """A pre-Alembic Conductor DB: members exist, version table does not."""
    tables = set(inspect(sync_conn).get_table_names())
    return "members" in tables and "alembic_version" not in tables


def current_revision(sync_conn) -> str | None:
    return MigrationContext.configure(sync_conn).get_current_revision()


def script_head() -> str | None:
    return _script(alembic_config()).get_current_head()


def _script(cfg: Config) -> ScriptDirectory:
    return ScriptDirectory.from_config(cfg)


def _run_env(sync_conn, fn) -> None:
    cfg = alembic_config()
    script = _script(cfg)
    env = EnvironmentContext(cfg, script)
    env.configure(
        connection=sync_conn,
        target_metadata=Base.metadata,
        fn=fn,
        transactional_ddl=True,
        render_as_batch=sync_conn.dialect.name == "sqlite",
    )
    with env.begin_transaction():
        env.run_migrations()


def upgrade_sync(sync_conn, revision: str = "head") -> None:
    script = _script(alembic_config())

    def fn(rev: str | None, _context):
        return script._upgrade_revs(revision, rev)

    _run_env(sync_conn, fn)


def downgrade_sync(sync_conn, revision: str) -> None:
    script = _script(alembic_config())

    def fn(rev: str | None, _context):
        return script._downgrade_revs(revision, rev)

    _run_env(sync_conn, fn)


def stamp_sync(sync_conn, revision: str) -> None:
    script = _script(alembic_config())

    def fn(rev: str | None, _context):
        return script._stamp_revs(revision, rev)

    _run_env(sync_conn, fn)


async def apply_migrations(engine: AsyncEngine, revision: str = "head") -> str:
    async with engine.connect() as conn:
        legacy = await conn.run_sync(is_legacy_database)
        if legacy:
            await conn.run_sync(stamp_sync, INITIAL_REVISION)
            await conn.commit()
        await conn.run_sync(upgrade_sync, revision)
        await conn.commit()
    action = "stamp" if legacy else "upgrade"
    log.info("migrations_applied", action=action, revision=revision)
    return action


async def apply_downgrade(engine: AsyncEngine, revision: str) -> None:
    async with engine.connect() as conn:
        await conn.run_sync(downgrade_sync, revision)
        await conn.commit()
    log.info("migrations_downgraded", revision=revision)


async def init_db(engine: AsyncEngine, *, auto_migrate: bool = True) -> None:
    if not auto_migrate:
        log.info("auto_migrate_skipped")
        return
    await apply_migrations(engine)


async def require_schema_head(engine: AsyncEngine) -> None:
    """Отказаться стартовать, если ревизия БД не равна alembic head."""
    head = script_head()
    async with engine.connect() as conn:
        current = await conn.run_sync(current_revision)
    if current is not None and current == head:
        return
    error = SchemaBehindError(current, head)
    log.error("schema_behind", current=current, head=head, message=str(error))
    raise error
