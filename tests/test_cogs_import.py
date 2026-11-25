"""Коги должны импортироваться: discord.py разбирает аннотации при создании Command."""

from __future__ import annotations

import importlib

import pytest

from conductor.bot import COGS
from conductor.presentation.access import keyed


@pytest.mark.parametrize("extension", COGS)
def test_cog_module_imports(extension: str) -> None:
    importlib.import_module(extension)


def test_keyed_keeps_callback_globals() -> None:
    async def sample(interaction: object, tier: object) -> None:
        return None

    wrapped = keyed("config.manage")(sample)
    assert wrapped is sample
    assert sample.access_key == "config.manage"
    assert len(sample.__discord_app_commands_checks__) == 1
