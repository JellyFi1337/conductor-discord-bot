"""Стереть базу и рабочие данные бота, чтобы настроить его с чистого листа."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from conductor.config import PROJECT_ROOT, Settings
from conductor.domain.reset import (
    collect_reset_paths,
    default_roots,
    postgres_reset_sql,
    remove_reset_paths,
)

BOT_SERVICES = ("bot", "bot-test")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Стирает SQLite, снимки, data-test, дампы backups/, "
            "рабочие файлы и схему Postgres. .env и эталоны yml не трогает. "
            "Роли и каналы в Discord не удаляет."
        )
    )
    parser.add_argument("--yes", action="store_true", help="выполнить, а не только показать план")
    parser.add_argument(
        "--keep-automod",
        action="store_true",
        help="оставить data/automod.yml (эталон и так не удаляется)",
    )
    parser.add_argument(
        "--skip-postgres",
        action="store_true",
        help="не трогать Postgres, только файлы",
    )
    return parser


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _running_services() -> set[str] | None:
    try:
        result = _compose("ps", "--status", "running", "--services")
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _stop_bot_containers(services: set[str]) -> None:
    running = [name for name in BOT_SERVICES if name in services]
    if not running:
        return
    sys.stdout.write(f"Останавливаю контейнеры: {', '.join(running)}\n")
    result = _compose("stop", *running)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        sys.stderr.write(f"Не удалось остановить контейнеры бота: {detail}\n")
        raise SystemExit(1)


def _reset_postgres(services: set[str] | None) -> str:
    if services is None:
        return "docker не найден — Postgres не трогал"
    if "postgres" not in services:
        return "контейнер postgres не запущен — схема не очищена; том можно снять через docker compose down -v"
    user = os.environ.get("POSTGRES_USER", "conductor")
    database = os.environ.get("POSTGRES_DB", "conductor")
    result = _compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        postgres_reset_sql(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        sys.stderr.write(f"Postgres не очищен: {detail}\n")
        raise SystemExit(1)
    return f"схема {database} опустошена"


def _print_paths(paths: tuple[Path, ...]) -> None:
    if not paths:
        sys.stdout.write("Файлов бота нет — каталоги уже пустые.\n")
        return
    sys.stdout.write(f"Будет удалено ({len(paths)}):\n")
    for path in paths:
        try:
            shown = path.relative_to(PROJECT_ROOT)
        except ValueError:
            shown = path
        marker = "/" if path.is_dir() else ""
        sys.stdout.write(f"  {shown.as_posix()}{marker}\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings()
    roots = default_roots(PROJECT_ROOT, settings.data_dir)
    sqlite = (settings.sqlite_path,) if settings.sqlite_path is not None else ()
    paths = collect_reset_paths(roots, sqlite_paths=sqlite, keep_automod=args.keep_automod)

    sys.stdout.write(
        "Чистый лист: база, бэкапы и рабочие файлы бота. "
        ".env не трогается. Каркас Discord (роли и каналы) не удаляется.\n"
    )
    _print_paths(paths)
    if not args.skip_postgres:
        sys.stdout.write("Postgres: опустошу схему, если контейнер запущен.\n")

    if not args.yes:
        sys.stdout.write("Это необратимо. Повторите команду с --yes.\n")
        sys.stdout.write("Сначала остановите бота: .\\stop.ps1\n")
        return 2

    services = _running_services()
    if services is not None:
        _stop_bot_containers(services)

    removed, errors = remove_reset_paths(paths)
    sys.stdout.write(f"Удалено файлов и папок: {len(removed)}\n")
    if errors:
        for path, detail in errors:
            sys.stderr.write(f"  не удалилось {path}: {detail}\n")
        sys.stderr.write("Остановите бота и запустите скрипт снова.\n")
        return 1

    if not args.skip_postgres:
        sys.stdout.write(f"Postgres: {_reset_postgres(services)}\n")

    sys.stdout.write(
        "Готово. Дальше: .\\start.ps1, затем /owner seed — список и подключение. "
        "Если Discord закрыл создание каналов, очистка базы бан не снимает.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
