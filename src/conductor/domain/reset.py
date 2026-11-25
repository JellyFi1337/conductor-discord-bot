"""Что стереть, чтобы поднять бота с чистого листа. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

KEEP_NAMES = frozenset({"automod.yml.example", "permissions.yml.example", "blueprint.example.json"})
AUTOMOD_FILE = "automod.yml"
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
DATA_DIR_NAME = "data"
DATA_TEST_DIR_NAME = "data-test"
DUMP_DIR_NAME = "backups"


def keep_names(*, keep_automod: bool = False) -> frozenset[str]:
    if keep_automod:
        return KEEP_NAMES | {AUTOMOD_FILE}
    return KEEP_NAMES


def plan_sqlite_files(db_file: Path) -> tuple[Path, ...]:
    path = Path(db_file)
    candidates = (path, *(Path(str(path) + suffix) for suffix in SQLITE_SIDECARS))
    return tuple(item for item in candidates if item.exists())


def plan_tree(root: Path, *, keep: frozenset[str] = KEEP_NAMES) -> tuple[Path, ...]:
    folder = Path(root)
    if not folder.is_dir():
        return ()
    found: list[Path] = []
    for path in folder.rglob("*"):
        if path.name in keep:
            continue
        found.append(path)
    return tuple(found)


def default_roots(project_root: Path, data_dir: Path | None = None) -> tuple[Path, ...]:
    roots = [
        project_root / DATA_DIR_NAME,
        project_root / DATA_TEST_DIR_NAME,
        project_root / DUMP_DIR_NAME,
    ]
    if data_dir is not None:
        roots.insert(0, Path(data_dir))
    return unique_paths(roots)


def unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for raw in paths:
        key = Path(raw).resolve()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(Path(raw))
    return tuple(ordered)


def collect_reset_paths(
    roots: Sequence[Path],
    *,
    sqlite_paths: Sequence[Path] = (),
    keep_automod: bool = False,
) -> tuple[Path, ...]:
    keep = keep_names(keep_automod=keep_automod)
    found: set[Path] = set()
    for root in roots:
        folder = Path(root)
        if folder.is_file() or folder.is_symlink():
            if folder.name not in keep:
                found.add(folder.resolve())
            continue
        for path in plan_tree(folder, keep=keep):
            found.add(path.resolve())
    for raw in sqlite_paths:
        for path in plan_sqlite_files(Path(raw)):
            found.add(path.resolve())
    return delete_order(found)


def delete_order(paths: Iterable[Path]) -> tuple[Path, ...]:
    items = tuple(Path(path) for path in paths)
    files = tuple(sorted((path for path in items if not path.is_dir()), key=lambda path: path.as_posix()))
    dirs = tuple(
        sorted(
            (path for path in items if path.is_dir()),
            key=lambda path: (-len(path.parts), path.as_posix()),
        )
    )
    return files + dirs


def remove_reset_paths(paths: Sequence[Path]) -> tuple[tuple[Path, ...], tuple[tuple[Path, str], ...]]:
    removed: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for path in delete_order(paths):
        try:
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            removed.append(path)
        except OSError as error:
            errors.append((path, str(error)))
    return tuple(removed), tuple(errors)


def postgres_reset_sql() -> str:
    return (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid(); "
        "DROP SCHEMA public CASCADE; "
        "CREATE SCHEMA public; "
        "GRANT ALL ON SCHEMA public TO public;"
    )
