from __future__ import annotations

from pathlib import Path

from conductor.domain.reset import (
    AUTOMOD_FILE,
    KEEP_NAMES,
    collect_reset_paths,
    default_roots,
    delete_order,
    plan_sqlite_files,
    plan_tree,
    postgres_reset_sql,
    remove_reset_paths,
)


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_tree_keeps_examples_and_lists_runtime(tmp_path: Path) -> None:
    _touch(tmp_path / "automod.yml.example")
    _touch(tmp_path / "permissions.yml.example")
    _touch(tmp_path / AUTOMOD_FILE)
    _touch(tmp_path / "conductor.db")
    _touch(tmp_path / ".command-hash")
    _touch(tmp_path / ".heartbeat")
    _touch(tmp_path / ".create-lock.json")
    _touch(tmp_path / "backups" / "conductor-20260907-065433.db")

    planned = plan_tree(tmp_path)
    names = {path.relative_to(tmp_path).as_posix() for path in planned}

    assert names.isdisjoint(KEEP_NAMES)
    assert AUTOMOD_FILE in names
    assert "conductor.db" in names
    assert ".command-hash" in names
    assert ".heartbeat" in names
    assert ".create-lock.json" in names
    assert "backups/conductor-20260907-065433.db" in names
    assert "backups" in names


def test_collect_keeps_automod_when_asked(tmp_path: Path) -> None:
    _touch(tmp_path / AUTOMOD_FILE)
    _touch(tmp_path / "conductor.db")

    kept = collect_reset_paths((tmp_path,), keep_automod=True)
    names = {path.name for path in kept}
    assert AUTOMOD_FILE not in names
    assert "conductor.db" in names


def test_plan_sqlite_files_includes_sidecars(tmp_path: Path) -> None:
    db = _touch(tmp_path / "conductor.db")
    wal = _touch(tmp_path / "conductor.db-wal")
    _touch(tmp_path / "other.db")

    found = plan_sqlite_files(db)
    assert db in found
    assert wal in found
    assert tmp_path / "conductor.db-shm" not in found


def test_default_roots_dedupes_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    roots = default_roots(tmp_path, data)
    resolved = [path.resolve() for path in roots]
    assert resolved.count(data.resolve()) == 1
    assert (tmp_path / "data-test") in roots
    assert (tmp_path / "backups") in roots


def test_delete_order_files_before_dirs(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    file = _touch(folder / "conductor-1.db")
    ordered = delete_order((folder, file))
    assert ordered[0] == file
    assert ordered[-1] == folder


def test_remove_reset_paths_leaves_examples(tmp_path: Path) -> None:
    example = _touch(tmp_path / "automod.yml.example", "keep")
    db = _touch(tmp_path / "conductor.db")
    backup = _touch(tmp_path / "backups" / "conductor-1.db")
    paths = collect_reset_paths((tmp_path,))

    removed, errors = remove_reset_paths(paths)

    assert not errors
    assert example.is_file()
    assert example.read_text(encoding="utf-8") == "keep"
    assert not db.exists()
    assert not backup.exists()
    assert not (tmp_path / "backups").exists()
    assert {path.resolve() for path in removed} >= {db.resolve(), backup.resolve()}


def test_postgres_reset_sql_rebuilds_public() -> None:
    sql = postgres_reset_sql()
    assert "DROP SCHEMA public CASCADE" in sql
    assert "CREATE SCHEMA public" in sql
    assert "pg_terminate_backend" in sql


def test_reset_keeps_every_git_tracked_example_in_data() -> None:
    """wipe_data не должен стирать эталоны, которые лежат в git под data/."""
    import subprocess

    from conductor.config import PROJECT_ROOT

    tracked = subprocess.run(
        ["git", "ls-files", "data"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    names = {Path(item).name for item in tracked}
    assert names, "git ls-files вернул пусто — тест запущен вне репозитория"
    assert names <= KEEP_NAMES
