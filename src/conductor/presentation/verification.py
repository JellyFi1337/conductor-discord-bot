"""Карточки верификации: кнопка, предупреждение, первые шаги."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord
from discord import ui

from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.layout import panel, respond
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "vf"

__all__ = [
    "PANEL_KIND",
    "onboarding_card",
    "rules_card",
    "warning_card",
]


def rules_card(*, ref: int) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=copy.VERIFICATION_OK,
            style=discord.ButtonStyle.success,
            custom_id=format_custom_id(PANEL_KIND, "ok", ref),
        )
    )
    return panel(
        copy.VERIFICATION_TITLE,
        list(copy.VERIFICATION_RULES),
        tone=Tone.INFO,
        icon=Icon.PANEL,
        rows=(row,),
        timeout=None,
    )


def warning_card() -> ui.LayoutView:
    return panel(
        copy.VERIFICATION_WARN_TITLE,
        copy.VERIFICATION_WARN_BODY,
        tone=Tone.DANGER,
        icon=Icon.WARN,
        timeout=None,
    )


def onboarding_card(lines: Sequence[str]) -> ui.LayoutView:
    return panel(copy.ONBOARDING_TITLE, list(lines), tone=Tone.OK, icon=Icon.DONE)


def _service(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "verification", None)


def _panel_loader(service: Any) -> Any:
    async def load(*, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(service, "guard_panel", None)
        if not callable(loader):
            return GuardResource(ref=str(ref), guild_id=guild_id)
        return await loader(guild_id, ref)

    return load


@register_dynamic_item
class VerifyButton(PersistentButton, template=r"^c:vf:ok:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "ok"
    public = True

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _panel_loader(_service(self._interaction))(guild_id=guild_id, ref=ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        service = _service(interaction)
        grant = getattr(service, "grant", None)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not callable(grant):
            return
        await respond(interaction, await grant(guild, member))
