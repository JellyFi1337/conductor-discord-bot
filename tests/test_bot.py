from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import discord

from conductor import __version__
from conductor.bot import Conductor, apply_application_owners

BOT_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "bot.py"


def _bot(*, extra: tuple[int, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(owner_id=None, owner_ids=None, settings=SimpleNamespace(owner_user_ids=extra))


def test_application_owner_is_developer() -> None:
    bot = _bot(extra=(7,))
    apply_application_owners(bot, SimpleNamespace(team=None, owner=SimpleNamespace(id=42)))
    assert Conductor.is_developer(bot, 42)
    assert Conductor.is_developer(bot, 7)
    assert not Conductor.is_developer(bot, 1)


def test_team_admins_and_developers_are_developers() -> None:
    bot = _bot()
    info = SimpleNamespace(
        team=SimpleNamespace(
            members=(
                SimpleNamespace(id=10, role=discord.TeamMemberRole.admin),
                SimpleNamespace(id=11, role=discord.TeamMemberRole.developer),
                SimpleNamespace(id=12, role=object()),
            )
        ),
        owner=SimpleNamespace(id=99),
    )
    apply_application_owners(bot, info)
    assert Conductor.is_developer(bot, 10)
    assert Conductor.is_developer(bot, 11)
    assert not Conductor.is_developer(bot, 12)
    assert not Conductor.is_developer(bot, 99)


def test_setup_hook_checks_schema_and_applies_owners() -> None:
    source = BOT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BOT_PY))
    setup = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "setup_hook"),
        None,
    )
    assert setup is not None
    names: set[str] = set()
    for child in ast.walk(setup):
        func = child.func if isinstance(child, ast.Call) else None
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    assert "application_info" in names
    assert "apply_application_owners" in names
    assert "init_db" in names
    assert "require_schema_head" in names
    assert "load_persistent_ui" in names
    body = ast.get_source_segment(source, setup)
    assert body is not None
    assert "if not self.settings.auto_migrate_enabled:" in body
    assert "await require_schema_head(self.engine)" in body


def test_startup_ready_emits_once_with_version(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class _Log:
        def info(self, event: str, **kwargs: object) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr("conductor.bot.log", _Log())
    monkeypatch.setattr("conductor.bot.script_head", lambda: "0073")
    bot = SimpleNamespace(
        _startup_ready=False,
        _boot_started=0.0,
        traffic=SimpleNamespace(profile=SimpleNamespace(value="edge")),
        settings=SimpleNamespace(rate_profile="edge"),
        guilds=(object(),),
    )
    Conductor._emit_startup_ready(bot)
    Conductor._emit_startup_ready(bot)
    assert len(events) == 1
    name, fields = events[0]
    assert name == "startup_ready"
    assert fields["version"] == __version__
    assert fields["alembic"] == "0073"
    assert fields["guilds"] == 1
