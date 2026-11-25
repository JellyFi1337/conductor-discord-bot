"""Модалки, которые отвечают Discord до обращения к сервису."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

import discord
from discord import ui

from conductor.presentation.interact import ack, defer_update
from conductor.presentation.layout import report_ui_error

__all__ = ["DeferMode", "SafeModal"]


class DeferMode(StrEnum):
    """Как SafeModal подтверждает взаимодействие до submit()."""

    THINKING = "thinking"
    UPDATE = "update"
    NONE = "none"


class SafeModal(ui.Modal):
    """Модалка: defer до работы, ошибки — в CriticalLog."""

    defer_mode: ClassVar[DeferMode] = DeferMode.THINKING
    ephemeral: ClassVar[bool] = True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if not await self._acknowledge(interaction):
                return
            await self.submit(interaction)
        except Exception as exc:
            await self.on_error(interaction, exc)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_ui_error(interaction, error)

    async def submit(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    async def _acknowledge(self, interaction: discord.Interaction) -> bool:
        if self.defer_mode is DeferMode.NONE:
            return True
        if self.defer_mode is DeferMode.THINKING:
            return await ack(interaction, ephemeral=self.ephemeral)
        return await defer_update(interaction)
