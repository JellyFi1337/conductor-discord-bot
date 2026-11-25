from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable

import structlog
from pydantic import ValidationError

from conductor.bot import Conductor
from conductor.config import Settings, masked_url
from conductor.logging import configure_logging

log = structlog.get_logger("conductor")


DB_FAILURE_PAUSE = 15.0
_AUTH_ERRORS = frozenset({"InvalidPasswordError", "InvalidAuthorizationSpecificationError"})
_MISSING_DB_ERRORS = frozenset({"InvalidCatalogNameError"})
_UNREACHABLE_ERRORS = frozenset({"ConnectionRefusedError", "gaierror", "CannotConnectNowError", "TimeoutError"})


def _error_chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _database_hint(error: BaseException) -> str | None:
    """Понятная причина отказа базы на старте или None, если ошибка не про базу."""
    names = {type(item).__name__ for item in _error_chain(error)}
    if names & _AUTH_ERRORS:
        return (
            "База отклонила пароль. POSTGRES_PASSWORD в .env не совпадает с паролем, "
            "с которым создан том postgres_data: Postgres берёт пароль только при первом создании тома.\n"
            "Верните прежний пароль в .env или смените его в базе: "
            "docker compose exec postgres psql -U conductor -c \"ALTER USER conductor WITH PASSWORD '...'\"\n"
        )
    if names & _MISSING_DB_ERRORS:
        return "Базы POSTGRES_DB нет на сервере Postgres. Проверьте POSTGRES_DB в .env.\n"
    if names & _UNREACHABLE_ERRORS:
        return "Сервер базы недоступен. Проверьте, что контейнер postgres запущен и здоров: docker compose ps.\n"
    return None


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        sys.stderr.write("В .env есть неверные значения:\n")
        for issue in error.errors():
            key = ".".join(str(part) for part in issue["loc"]) or "?"
            sys.stderr.write(f"  {key}: {issue['msg']}\n")
        sys.stderr.write("Пустую строку можно оставить — она читается как «не задано».\n")
        raise SystemExit(2) from error


def _preflight(settings: Settings) -> None:
    if not settings.token_looks_real:
        if settings.bot_mode.value == "test":
            sys.stderr.write("В .env нет токена тестового бота: TEST_DISCORD_TOKEN (или DISCORD_TOKEN в .env.test).\n")
        else:
            sys.stderr.write(
                "В .env нет DISCORD_TOKEN основного бота. Скопируйте .env.example и вставьте оба токена.\n"
            )
        raise SystemExit(2)
    path = settings.sqlite_path
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            sys.stderr.write(f"Нет доступа к папке базы {path.parent}: {error}\n")
            raise SystemExit(2) from error
    if settings.fence is None:
        sys.stderr.write(
            "В .env нужен список серверов выбранного BOT_MODE: "
            "GUILD_IDS / TEST_GUILD_IDS или запасные PUBLIC_GUILD_ID / TEST_GUILD_ID. "
            "С любых других серверов бот выйдет.\n"
        )
        raise SystemExit(2)
    log.info(
        "preflight",
        mode=settings.mode_label,
        bot_mode=settings.bot_mode.value,
        guild_ids=sorted(settings.fence.guild_ids),
        force_sync=settings.force_sync,
        auto_migrate=settings.auto_migrate_enabled,
        database=masked_url(settings.database_url),
        token_configured=True,
    )


def _bind_stop_signals(loop: asyncio.AbstractEventLoop, on_stop: Callable[[], None]) -> None:
    """SIGINT/SIGTERM → one close() path. Second press kills the process so Ctrl+C cannot hang."""
    if sys.platform == "win32":

        def _notify(_signum: int, _frame: object | None) -> None:
            loop.call_soon_threadsafe(on_stop)

        signal.signal(signal.SIGINT, _notify)
        sigbreak = getattr(signal, "SIGBREAK", None)
        if sigbreak is not None:
            signal.signal(sigbreak, _notify)
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_stop)


_DB_USAGE = "usage: conductor db upgrade [revision] | current | revision [-m message]\n"


def _run_db(argv: list[str]) -> None:
    if not argv or argv[0] in {"-h", "--help"}:
        sys.stderr.write(_DB_USAGE)
        raise SystemExit(2)
    settings = _load_settings()
    configure_logging(settings.log_level)
    command = argv[0]
    if command == "upgrade":
        target = argv[1] if len(argv) > 1 else "head"
        asyncio.run(_db_upgrade(settings, target))
        return
    if command == "current":
        asyncio.run(_db_current(settings))
        return
    if command == "revision":
        message = "update"
        if "-m" in argv:
            index = argv.index("-m")
            if index + 1 >= len(argv):
                sys.stderr.write("conductor db revision: после -m нужен текст сообщения.\n")
                raise SystemExit(2)
            message = argv[index + 1]
        elif len(argv) > 1 and not argv[1].startswith("-"):
            message = argv[1]
        _db_revision(settings, message)
        return
    sys.stderr.write(_DB_USAGE)
    raise SystemExit(2)


async def _db_upgrade(settings: Settings, target: str) -> None:
    from conductor.db.engine import apply_migrations, create_engine

    engine = create_engine(settings.database_url)
    try:
        await apply_migrations(engine, target)
    finally:
        await engine.dispose()


async def _db_current(settings: Settings) -> None:
    from conductor.db.engine import create_engine, current_revision

    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.run_sync(current_revision)
        sys.stdout.write(f"{revision or 'none'}\n")
    finally:
        await engine.dispose()


def _db_revision(settings: Settings, message: str) -> None:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from conductor.db.engine import alembic_config

    ini = Path("alembic.ini")
    cfg = Config(str(ini)) if ini.is_file() else alembic_config(settings.database_url)
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.revision(cfg, message=message, autogenerate=True)


async def _serve(settings: Settings) -> None:
    bot = Conductor(settings)
    loop = asyncio.get_running_loop()
    presses = 0
    shutdown_tasks: list[asyncio.Task[None]] = []

    def on_stop() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            log.info("shutdown_requested")
            if not bot.is_closed():
                shutdown_tasks.append(loop.create_task(bot.close(), name="conductor-shutdown"))
            return
        log.warning("shutdown_forced")
        os._exit(1)

    _bind_stop_signals(loop, on_stop)
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "db":
        _run_db(argv[1:])
        return
    settings = _load_settings()
    configure_logging(settings.log_level)
    _preflight(settings)
    try:
        asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        log.info("shutdown_by_user")
    except Exception as error:
        hint = _database_hint(error)
        if hint is None:
            raise
        log.error("database_unavailable", error=type(error).__name__, database=masked_url(settings.database_url))
        sys.stderr.write(hint)
        # Пауза перед выходом: restart: unless-stopped не должен долбить базу каждые 3 с.
        time.sleep(DB_FAILURE_PAUSE)
        raise SystemExit(3) from error


if __name__ == "__main__":
    main()
