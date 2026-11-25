"""Карточки канала #структура: потеря привязки и кнопка восстановления."""

from __future__ import annotations

from typing import Any

import discord
from discord import ui

from conductor.domain.access import CONFIG_MANAGE
from conductor.domain.seed import FEATURES, entry_by_key
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import panel, respond
from conductor.presentation.persistent import PersistentButton, register_dynamic_item
from conductor.presentation.tokens import Icon, Tone

__all__ = [
    "StructureFixButton",
    "structure_lost_card",
    "structure_more_card",
    "structure_renamed_card",
]


def feature_label(feature: str) -> str:
    for key, copy_name, _keys in FEATURES:
        if key == feature:
            return str(getattr(copy, copy_name, feature))
    return feature


def structure_lost_card(loss: Any) -> ui.LayoutView:
    rank = getattr(loss, "rank", None)
    danger = rank is not None
    tone = Tone.DANGER if danger else Tone.WARN
    verb = copy.STRUCTURE_FIX_RANK if danger else copy.STRUCTURE_FIX
    title = copy.STRUCTURE_RANK_TITLE if danger else copy.STRUCTURE_TITLE
    body = copy.structure_lost_body(
        label=str(getattr(loss, "label", "") or ""),
        feature=feature_label(str(getattr(loss, "feature", "") or "")),
        bindings=getattr(loss, "bindings_cleared", ()),
        panels=int(getattr(loss, "panels_stale", 0) or 0),
        holders=len(tuple(getattr(loss, "holders", ()) or ())),
        rank=str(getattr(rank, "key", "") or ""),
        subsystem=str(getattr(loss, "subsystem", "") or ""),
    )
    if getattr(loss, "panels_stale", 0) and not getattr(loss, "key", ""):
        body = copy.STRUCTURE_PANEL_STALE
    button = StructureFixButton(str(getattr(loss, "key", "") or "0"), label=verb)
    return panel(
        title,
        body,
        tone=tone,
        icon=Icon.DENY if danger else Icon.WARN,
        rows=(ui.ActionRow(button),) if getattr(loss, "key", "") else (),
        timeout=None,
    )


def structure_renamed_card(*, label: str, old: str, new: str) -> ui.LayoutView:
    return panel(
        copy.STRUCTURE_RENAME_TITLE,
        copy.structure_renamed_body(label=label, old=old, new=new),
        tone=Tone.INFO,
        icon=Icon.PANEL,
        timeout=None,
    )


def structure_more_card(rest: int) -> ui.LayoutView:
    return panel(
        copy.STRUCTURE_TITLE,
        copy.structure_more(rest),
        tone=Tone.WARN,
        icon=Icon.WARN,
        timeout=None,
    )


@register_dynamic_item
class StructureFixButton(PersistentButton, template=r"^c:sk:fix:(?P<ref>\d+|[a-z0-9_]{1,32})$"):
    kind = "sk"
    action = "fix"
    access_key = CONFIG_MANAGE

    def __init__(self, ref: int | str, *, item: ui.Button | None = None, label: str | None = None) -> None:
        super().__init__(
            ref,
            item=item,
            kind=self.kind,
            action=self.action,
            label=label or copy.STRUCTURE_FIX,
            style=discord.ButtonStyle.primary,
        )

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        # Ресурс кнопки — строка каркаса этой гильдии; без него run_guard отвечает «устарело».
        if int(guild_id) <= 0 or entry_by_key(ref) is None:
            return None
        return GuardResource(ref=ref, guild_id=int(guild_id))

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        del verdict
        guild = interaction.guild
        watch = getattr(interaction.client, "structure", None)
        restore = getattr(watch, "restore", None)
        if guild is None or not callable(restore):
            return
        report = await restore(guild, self.ref, actor_id=int(interaction.user.id))
        entry = entry_by_key(self.ref)
        label = entry.diagnose_label if entry is not None else self.ref
        body = copy.structure_restored(label, getattr(report, "verdict", ""))
        await respond(interaction, panel(copy.STRUCTURE_TITLE, body))
