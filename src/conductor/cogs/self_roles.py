"""Регистрация селекта ролей по выбору."""

from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

import conductor.presentation.self_roles as _self_roles_ui  # noqa: F401

if TYPE_CHECKING:
    from conductor.bot import Conductor

__all__ = ["SelfRoles"]


class SelfRoles(commands.Cog):
    def __init__(self, bot: Conductor) -> None:
        self.bot = bot


async def setup(bot: Conductor) -> None:
    await bot.add_cog(SelfRoles(bot))
