"""Карточки и кнопки отчётов об ошибках. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import discord
from discord import ui

from conductor.domain.access import BUG_TRIAGE
from conductor.domain.bugs import (
    DID_LIMIT,
    HAPPENED_LIMIT,
    BugCard,
    BugStatus,
    DecideVerdict,
    SubmitVerdict,
    parse_code,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import denied, panel, respond, update_message
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import (
    CUSTOM_ID_RE,
    PersistentButton,
    format_custom_id,
    register_dynamic_item,
)
from conductor.presentation.tokens import Tone

PANEL_KIND = "bug"

__all__ = ["PANEL_KIND", "BugReportModal", "closed_view", "report_view"]


def report_view(card: BugCard) -> ui.LayoutView:
    fields: dict[str, object] = {
        copy.BUG_FIELD_AUTHOR: f"<@{card.user_id}>",
        copy.BUG_FIELD_VERSION: card.version,
        copy.BUG_FIELD_CODE: card.code or "—",
        copy.BUG_FIELD_DID: card.did,
        copy.BUG_FIELD_HAPPENED: card.happened,
        copy.BUG_FIELD_STATUS: copy.bug_status_label(card.status),
    }
    if card.match is not None:
        fields[copy.BUG_FIELD_MATCH] = copy.bug_match_line(
            error_type=card.match.error_type,
            origin=card.match.origin,
            jump_url=card.match.jump_url,
        )
    rows: tuple[ui.ActionRow, ...] = ()
    if card.status == BugStatus.NEW.value:
        rows = (
            ui.ActionRow(
                ui.Button(
                    label=copy.BUG_ACCEPT,
                    style=discord.ButtonStyle.success,
                    custom_id=format_custom_id(PANEL_KIND, "accept", card.report_id),
                ),
                ui.Button(
                    label=copy.BUG_FIX,
                    style=discord.ButtonStyle.primary,
                    custom_id=format_custom_id(PANEL_KIND, "fix", card.report_id),
                ),
                ui.Button(
                    label=copy.BUG_REJECT,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "reject", card.report_id),
                ),
            ),
        )
    return panel(
        copy.BUG_TITLE,
        tone=Tone.WARN if card.status == BugStatus.NEW.value else Tone.INFO,
        fields=fields,
        rows=rows,
        timeout=None,
    )


def closed_view(card: BugCard) -> ui.LayoutView:
    return report_view(card)


def thanks_view() -> ui.LayoutView:
    return panel(copy.BUG_THANKS_TITLE, copy.BUG_THANKS_BODY, tone=Tone.OK)


def _bugs(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "bugs", None)


def _loader(interaction: discord.Interaction) -> Callable[..., Awaitable[GuardResource | None]]:
    async def load(*, guild_id: int, ref: str) -> GuardResource | None:
        service = _bugs(interaction)
        method = getattr(service, "guard_report", None)
        if not callable(method):
            return GuardResource(ref=ref, guild_id=guild_id)
        return await method(guild_id, ref)

    return load


class BugReportModal(SafeModal, title=copy.BUG_MODAL_TITLE):
    def __init__(self, *, code: str = "") -> None:
        super().__init__()
        preset = parse_code(code) or ""
        self._did = ui.Label(
            text=copy.BUG_DID_LABEL,
            description=copy.BUG_DID_HINT,
            component=ui.TextInput(
                max_length=DID_LIMIT,
                required=True,
                style=discord.TextStyle.paragraph,
            ),
        )
        self._happened = ui.Label(
            text=copy.BUG_HAPPENED_LABEL,
            description=copy.BUG_HAPPENED_HINT,
            component=ui.TextInput(
                max_length=HAPPENED_LIMIT,
                required=True,
                style=discord.TextStyle.paragraph,
            ),
        )
        self._code = ui.Label(
            text=copy.BUG_CODE_LABEL,
            description=copy.BUG_CODE_HINT,
            component=ui.TextInput(
                max_length=8,
                required=False,
                default=preset,
                placeholder="E-1A2B",
                style=discord.TextStyle.short,
            ),
        )
        self.add_item(self._did)
        self.add_item(self._happened)
        self.add_item(self._code)

    async def submit(self, interaction: discord.Interaction) -> None:
        service = _bugs(interaction)
        publish = getattr(service, "submit", None)
        guild = interaction.guild
        if not callable(publish) or guild is None:
            await respond(interaction, denied(copy.BUG_USE_SERVER))
            return
        result = await publish(
            guild,
            interaction.user,
            did=_labeled_value(self._did),
            happened=_labeled_value(self._happened),
            code=_labeled_value(self._code),
        )
        verdict = getattr(result, "verdict", None)
        if verdict is SubmitVerdict.RATE:
            await respond(interaction, denied(copy.BUG_RATE))
            return
        if verdict is SubmitVerdict.NO_CHANNEL:
            await respond(interaction, denied(copy.BUG_NO_CHANNEL))
            return
        if verdict is SubmitVerdict.BAD_TEXT:
            await respond(interaction, denied(copy.BUG_BAD_TEXT))
            return
        if verdict is not SubmitVerdict.OK:
            await respond(interaction, denied(copy.BUG_NO_CHANNEL))
            return
        await respond(interaction, thanks_view())


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class _BugButton(PersistentButton, template=CUSTOM_ID_RE):
    kind = PANEL_KIND

    async def load_resource(self, *, guild_id: int, ref: str) -> object | None:
        return await _loader(self._interaction)(guild_id=guild_id, ref=ref)


@register_dynamic_item
class BugNewButton(_BugButton, template=r"^c:bug:new:(?P<ref>E-[0-9A-Fa-f]{4})$"):
    action = "new"
    public = True
    acks_first = False

    async def load_resource(self, *, guild_id: int, ref: str) -> object | None:
        if guild_id <= 0:
            return None
        return GuardResource(ref=ref, guild_id=guild_id)

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        if interaction.guild is None:
            await respond(interaction, denied(copy.BUG_USE_SERVER))
            return
        await interaction.response.send_modal(BugReportModal(code=self.ref))


class _BugTriageButton(_BugButton, template=CUSTOM_ID_RE):
    access_key = BUG_TRIAGE
    target_status: ClassVar[BugStatus] = BugStatus.ACCEPTED

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        service = _bugs(interaction)
        decide = getattr(service, "decide", None)
        guild = interaction.guild
        if not callable(decide) or guild is None:
            await respond(interaction, denied(copy.BUG_STALE))
            return
        result = await decide(guild, interaction.user, self.ref, type(self).target_status)
        if result.verdict is not DecideVerdict.OK or result.card is None:
            await respond(interaction, denied(copy.BUG_STALE))
            return
        await update_message(interaction, closed_view(result.card))


@register_dynamic_item
class BugAcceptButton(_BugTriageButton, template=r"^c:bug:accept:(?P<ref>\d+)$"):
    action = "accept"
    target_status = BugStatus.ACCEPTED


@register_dynamic_item
class BugFixButton(_BugTriageButton, template=r"^c:bug:fix:(?P<ref>\d+)$"):
    action = "fix"
    target_status = BugStatus.FIXED


@register_dynamic_item
class BugRejectButton(_BugTriageButton, template=r"^c:bug:reject:(?P<ref>\d+)$"):
    action = "reject"
    target_status = BugStatus.REJECTED
