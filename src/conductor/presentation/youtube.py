"""Модалка добавления YouTube-фида. Пост видео — обычный content, не V2."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord
from discord import ui

from conductor.domain.youtube import DEFAULT_TEMPLATE
from conductor.presentation import copy
from conductor.presentation.modal import SafeModal

Done = Callable[[discord.Interaction, str], Awaitable[None]]

__all__ = ["YoutubeFeedModal"]


class YoutubeFeedModal(SafeModal, title=copy.ADMIN_YT_ADD):
    def __init__(self, *, on_done: Done, defaults: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.on_done = on_done
        channel_default = defaults[0] if defaults else ""
        template_default = defaults[1] if len(defaults) > 1 else DEFAULT_TEMPLATE
        self._channel = ui.TextInput(
            max_length=32,
            default=channel_default or None,
            placeholder="UCxxxxxxxxxxxxxxxxxxxxxx",
        )
        self._template = ui.TextInput(
            max_length=300,
            default=template_default[:300],
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(ui.Label(text=copy.ADMIN_YT_FIELD_ID, component=self._channel))
        self.add_item(ui.Label(text=copy.ADMIN_YT_FIELD_TEMPLATE, component=self._template))

    async def submit(self, interaction: discord.Interaction) -> None:
        channel = str(self._channel.value or "").strip()
        template = str(self._template.value or "").strip()
        await self.on_done(interaction, f"{channel}\n{template}")
