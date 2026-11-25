"""Карточки автомодерации: hit, правила, заявка."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import discord
from discord import ui

from conductor.domain.access import AUTOMOD_REQUEST, CONFIG_MANAGE
from conductor.domain.automod import Trigger
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.layout import denied, event, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.text import FOOTER_ADMIN, channel_label, user_label
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "am"

__all__ = [
    "PANEL_KIND",
    "RequestModal",
    "hit_card",
    "request_card",
    "rules_card",
]


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return discord.utils.format_dt(moment, "R")


def rules_card(
    *,
    lines: Sequence[str],
    exempt: str,
    hits: str,
    file_path: str,
    changed_at: datetime | None,
    loaded_at: datetime | None,
    ref: int,
) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=copy.AUTOMOD_RELOAD,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "reload", ref),
        ),
        ui.Button(
            label=copy.AUTOMOD_REQUEST,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(PANEL_KIND, "request", ref),
        ),
    )
    return panel(
        copy.AUTOMOD_CARD_TITLE,
        list(lines),
        tone=Tone.INFO,
        icon=Icon.PANEL,
        fields={
            copy.AUTOMOD_FIELD_EXEMPT: exempt,
            copy.AUTOMOD_FIELD_FILE: copy.automod_file_line(file_path, _stamp(changed_at), _stamp(loaded_at)),
            copy.AUTOMOD_FIELD_HITS: hits,
        },
        rows=(row,),
        timeout=None,
    )


def request_card(
    *,
    request_id: int,
    author_id: int,
    rule: str,
    proposal: str,
    reason: str,
    status: str,
    buttons: bool = True,
) -> ui.LayoutView:
    rows: tuple[ui.ActionRow, ...] = ()
    if buttons and status == "new":
        rows = (
            ui.ActionRow(
                ui.Button(
                    label=copy.AUTOMOD_ACCEPT,
                    style=discord.ButtonStyle.success,
                    custom_id=format_custom_id(PANEL_KIND, "acc", request_id),
                ),
                ui.Button(
                    label=copy.AUTOMOD_REJECT,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "rej", request_id),
                ),
            ),
        )
    return panel(
        copy.AUTOMOD_REQUEST_TITLE,
        tone=Tone.WARN,
        fields={
            copy.MOD_FIELD_ACTOR: user_label(None, author_id),
            copy.AUTOMOD_FIELD_RULE: rule,
            copy.AUTOMOD_FIELD_PROPOSAL: proposal,
            copy.AUTOMOD_FIELD_WHY: reason,
            copy.AUTOMOD_FIELD_STATUS: copy.automod_status_label(status),
        },
        rows=rows,
        timeout=None,
    )


def hit_card(
    trigger: Trigger,
    *,
    user_id: int,
    channel_id: int,
    window_minutes: int,
    case_id: int | None = None,
    staff: str | None = None,
) -> ui.LayoutView:
    fields: dict[str, object] = {
        copy.MOD_FIELD_ACTOR: user_label(None, user_id),
        copy.MOD_FIELD_CHANNEL: channel_label(None, channel_id),
        copy.AUTOMOD_FIELD_RULE: copy.automod_rule_title(trigger.rule),
        copy.AUTOMOD_FIELD_USED: copy.automod_used_line(trigger.used, window_minutes),
    }
    if case_id is not None:
        fields[copy.AUTOMOD_FIELD_CASE] = f"#{int(case_id)}"
    body = staff if staff else copy.automod_hit_body(trigger.rule, trigger.used, window_minutes)
    return event(
        copy.AUTOMOD_HIT_TITLE,
        body,
        tone=Tone.DANGER,
        fields=fields,
        source=FOOTER_ADMIN,
    )


def _automod(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "automod", None)


def _panel_loader(service: Any) -> Any:
    async def load(*, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(service, "guard_panel", None)
        if not callable(loader):
            return GuardResource(ref=str(ref), guild_id=guild_id)
        return await loader(guild_id, ref)

    return load


class RequestModal(SafeModal, title=copy.AUTOMOD_REQUEST_MODAL):
    rule = ui.Label(
        text=copy.AUTOMOD_LABEL_RULE,
        component=ui.TextInput(max_length=80, placeholder="emoji_flood"),
    )
    proposal = ui.Label(
        text=copy.AUTOMOD_LABEL_PROPOSAL,
        component=ui.TextInput(max_length=200),
    )
    reason = ui.Label(
        text=copy.AUTOMOD_LABEL_REASON,
        component=ui.TextInput(style=discord.TextStyle.paragraph, max_length=400, required=False),
    )

    def __init__(self, guild_id: int) -> None:
        super().__init__()
        self.guild_id = int(guild_id)

    async def submit(self, interaction: discord.Interaction) -> None:
        service = _automod(interaction)
        submit = getattr(service, "submit_request", None)
        if not callable(submit):
            return
        view = await submit(
            self.guild_id,
            author_id=interaction.user.id,
            rule=str(self.rule.component.value or ""),
            proposal=str(self.proposal.component.value or ""),
            reason=str(self.reason.component.value or ""),
        )
        await respond(interaction, view)


class _AutomodPanel:
    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _panel_loader(_automod(self._interaction))(guild_id=guild_id, ref=ref)


@register_dynamic_item
class AutomodReloadButton(_AutomodPanel, PersistentButton, template=r"^c:am:reload:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "reload"
    access_key = CONFIG_MANAGE

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        service = _automod(interaction)
        reload = getattr(service, "reload_card", None)
        guild = interaction.guild
        if guild is None or not callable(reload):
            return
        await respond(interaction, await reload(guild))


@register_dynamic_item
class AutomodRequestButton(_AutomodPanel, PersistentButton, template=r"^c:am:request:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "request"
    access_key = AUTOMOD_REQUEST
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        if interaction.guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        await interaction.response.send_modal(RequestModal(interaction.guild.id))


@register_dynamic_item
class AutomodAcceptButton(_AutomodPanel, PersistentButton, template=r"^c:am:acc:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "acc"
    access_key = CONFIG_MANAGE

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await _decide(interaction, int(self.ref), accepted=True)


@register_dynamic_item
class AutomodRejectButton(_AutomodPanel, PersistentButton, template=r"^c:am:rej:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "rej"
    access_key = CONFIG_MANAGE

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await _decide(interaction, int(self.ref), accepted=False)


async def _decide(interaction: discord.Interaction, request_id: int, *, accepted: bool) -> None:
    service = _automod(interaction)
    decide = getattr(service, "decide_request", None)
    if not callable(decide):
        return
    await respond(interaction, await decide(request_id, accepted=accepted, actor_id=interaction.user.id))
