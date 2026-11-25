"""Новый сервис, cog и задача планировщика обязаны быть явно подключены."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import discord.http

from conductor import discord_compat
from conductor.bot import COGS, Conductor
from conductor.config import Settings
from conductor.presentation.persistent import dynamic_items, load_persistent_ui, register_dynamic_item

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _bot_attrs_from_cogs() -> frozenset[str]:
    names: set[str] = set()
    for extension in COGS:
        path = SRC / "cogs" / f"{extension.rsplit('.', 1)[-1]}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "bot"
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
            ):
                names.add(node.attr)
    return frozenset(names)


def test_every_cog_module_has_setup() -> None:
    for extension in COGS:
        module = importlib.import_module(extension)
        assert callable(getattr(module, "setup", None)), extension


async def test_cog_bot_attrs_exist_on_conductor(tmp_path: Path) -> None:
    bot = Conductor(Settings(_env_file=None, discord_token="t", database_url=_url(tmp_path)))
    try:
        missing = sorted(name for name in _bot_attrs_from_cogs() if not hasattr(bot, name))
        assert missing == []
        names = bot.scheduler.job_names
        assert names
        assert len(names) == len(set(names))
        assert "panels-probe" in names
        assert "heartbeat" in names
    finally:
        await bot.close()


async def test_conductor_has_rate_observer_and_http_trace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(discord.http, "Ratelimit", discord_compat._UpstreamRatelimit)
    bot = Conductor(Settings(_env_file=None, discord_token="t", database_url=_url(tmp_path)))
    try:
        assert getattr(bot, "traffic", None) is not None
        assert bot.traffic.limits.global_rps_target == 48
        assert getattr(bot, "rate_observer", None) is not None
        assert getattr(bot.http, "http_trace", None) is not None
        assert isinstance(bot.http.get_ratelimit("test"), discord_compat.AvailableTokenRatelimit)
    finally:
        await bot.close()


def test_dynamic_items_register_through_helper() -> None:
    items = load_persistent_ui()
    registered = dynamic_items()
    assert items
    assert items == registered
    sample = items[0]
    before = len(dynamic_items())
    register_dynamic_item(sample)
    assert len(dynamic_items()) == before
