"""Карточка досье и модалки действий. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

import discord
from discord import ui

from conductor.domain.dossier import DOSSIER_CASES, DossierAction, DossierSnapshot
from conductor.domain.moderation import (
    DETAIL_REASON_MAX,
    DETAIL_REASON_MIN,
    HISTORY_PAGE,
    REASON_MAX,
    CaseKind,
    sanitize_detail_reason,
    sanitize_reason,
)
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import denied, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.moderation import CaseReasonModal, deliver_case, history_card
from conductor.presentation.tokens import Icon, Tone

DossierActionFn = Callable[[discord.Interaction, DossierAction], Awaitable[None]]
DossierHistoryFn = Callable[[discord.Interaction], Awaitable[None]]

_REASON_LIMIT = REASON_MAX
_BUTTON_STYLE = {
    DossierAction.KICK: discord.ButtonStyle.danger,
    DossierAction.BAN: discord.ButtonStyle.danger,
    DossierAction.WATCH_CLEAR: discord.ButtonStyle.secondary,
    DossierAction.REVOKE: discord.ButtonStyle.danger,
}


def dossier_card(
    snap: DossierSnapshot,
    *,
    on_action: DossierActionFn | None = None,
    on_history: DossierHistoryFn | None = None,
) -> ui.LayoutView:
    fields: dict[str, object] = {}
    sanctions = _sanctions(snap)
    if sanctions:
        fields[copy.DOSSIER_SANCTIONS] = "\n".join(sanctions)
    if snap.cases:
        fields[copy.DOSSIER_CASES] = "\n".join(_case_line(item) for item in snap.cases)
    if snap.notes:
        fields[copy.DOSSIER_NOTES] = "\n".join(_note_line(item) for item in snap.notes)
    fields[copy.DOSSIER_TICKETS] = copy.dossier_tickets_line(snap.tickets.count, snap.tickets.categories)
    rows = _action_rows(snap.actions, on_action=on_action, on_history=on_history)
    return panel(
        f"{copy.DOSSIER_TITLE} · {snap.name}",
        _header(snap),
        tone=Tone.WARN if sanctions else Tone.INFO,
        fields=fields,
        rows=rows,
        icon=Icon.CASE,
    )


def _header(snap: DossierSnapshot) -> list[str]:
    if snap.tier_name:
        score = copy.PROFILE_REP_TIER.format(name=snap.tier_name, score=snap.rep_score)
    elif snap.sinner:
        score = copy.PROFILE_REP_TIER.format(name=copy.REP_TIER_DEFAULTS[0], score=snap.rep_score)
    else:
        score = str(snap.rep_score)
    lines = [
        f"**{snap.name}** · `{snap.user_id}`",
        f"{copy.PROFILE_TENURE} **{snap.tenure_days}** {copy.LADDER_DAYS} · {copy.ladder_step(snap.ladder_step)}",
        f"{copy.PROFILE_REP} **{score}**",
    ]
    lines.extend(snap.self_roles)
    if snap.lfg_posts:
        lines.append(copy.lfg_author_line(snap.lfg_posts))
    return lines


def _sanctions(snap: DossierSnapshot) -> list[str]:
    lines: list[str] = []
    if snap.timeout_until is not None:
        lines.append(copy.DOSSIER_TIMEOUT.format(when=discord.utils.format_dt(snap.timeout_until, "R")))
    if snap.muted:
        lines.append(copy.DOSSIER_MUTED)
    if snap.watch is not None:
        lines.append(copy.DOSSIER_WATCHING.format(when=discord.utils.format_dt(snap.watch.until, "R")))
    return lines


def _case_line(item: object) -> str:
    when = _when(getattr(item, "created_at", None))
    revoked_by = getattr(item, "revoked_by", None) if getattr(item, "revoked_at", None) else None
    return copy.dossier_case_line(
        int(item.number),
        str(item.kind),
        int(item.actor_id),
        when,
        revoked_by=None if revoked_by is None else int(revoked_by),
    )


def _note_line(item: object) -> str:
    when = _when(getattr(item, "created_at", None))
    return copy.dossier_note_line(int(item.actor_id), str(getattr(item, "reason", "") or ""), when)


def _when(stamp: datetime | None) -> str:
    if stamp is None:
        return "—"
    return discord.utils.format_dt(stamp, "R")


def _action_rows(
    actions: Sequence[DossierAction],
    *,
    on_action: DossierActionFn | None,
    on_history: DossierHistoryFn | None,
) -> tuple[ui.ActionRow, ...]:
    buttons: list[ui.Button] = []
    if on_action is not None:
        for action in actions:
            button = ui.Button(
                label=copy.dossier_action_label(action.value),
                style=_BUTTON_STYLE.get(action, discord.ButtonStyle.primary),
            )
            button.callback = instant(_bind_action(on_action, action))
            buttons.append(button)
    if on_history is not None:
        history = ui.Button(label=copy.DOSSIER_HISTORY, style=discord.ButtonStyle.secondary)
        history.callback = instant(on_history)
        buttons.append(history)
    rows: list[ui.ActionRow] = []
    for index in range(0, len(buttons), 5):
        rows.append(ui.ActionRow(*buttons[index : index + 5]))
    return tuple(rows)


def _bind_action(on_action: DossierActionFn, action: DossierAction) -> Callable[[discord.Interaction], Awaitable[None]]:
    async def pressed(interaction: discord.Interaction) -> None:
        await on_action(interaction, action)

    return pressed


class TimeoutReasonModal(SafeModal, title=copy.DOSSIER_TIMEOUT_MODAL):
    duration = ui.Label(
        text=copy.DOSSIER_DURATION_LABEL,
        component=ui.TextInput(
            min_length=1,
            max_length=8,
            required=True,
            placeholder=copy.DOSSIER_DURATION_HINT,
        ),
    )
    reason = ui.Label(
        text=copy.MOD_REASON_LABEL,
        component=ui.TextInput(min_length=8, max_length=_REASON_LIMIT, required=True),
    )

    def __init__(self, target: discord.abc.User) -> None:
        super().__init__()
        self.target = target

    async def submit(self, interaction: discord.Interaction) -> None:
        await deliver_case(
            interaction,
            self.target,
            kind=CaseKind.TIMEOUT,
            reason=_labeled_value(self.reason),
            duration=_labeled_value(self.duration),
        )


class WatchModal(SafeModal, title=copy.DOSSIER_WATCH_MODAL):
    days = ui.Label(
        text=copy.DOSSIER_WATCH_DAYS_LABEL,
        component=ui.TextInput(min_length=1, max_length=2, required=True, placeholder="7"),
    )
    reason = ui.Label(
        text=copy.DOSSIER_WATCH_REASON_LABEL,
        component=ui.TextInput(min_length=8, max_length=_REASON_LIMIT, required=True),
    )

    def __init__(self, target: discord.abc.User) -> None:
        super().__init__()
        self.target = target

    async def submit(self, interaction: discord.Interaction) -> None:
        await deliver_watch(
            interaction,
            self.target,
            days=_labeled_value(self.days),
            reason=_labeled_value(self.reason),
        )


async def deliver_watch(
    interaction: discord.Interaction,
    target: discord.abc.User,
    *,
    days: str,
    reason: str,
) -> None:
    guild = interaction.guild
    moderation = getattr(interaction.client, "moderation", None)
    setter = getattr(moderation, "set_watch", None)
    if guild is None or not callable(setter):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    if sanitize_reason(reason) is None:
        await respond(interaction, denied(copy.MOD_REASON_SHORT))
        return
    rank_of = getattr(getattr(interaction.client, "dossier", None), "actor_rank", None)
    actor_rank = await rank_of(interaction) if callable(rank_of) else 0
    stored = await setter(
        guild.id,
        target.id,
        set_by=int(getattr(interaction.user, "id", 0) or 0),
        actor_rank=actor_rank,
        reason=reason,
        days_raw=days,
    )
    if stored is None:
        await respond(interaction, denied(copy.DOSSIER_WATCH_DAYS))
        return
    await respond(
        interaction,
        panel(copy.DOSSIER_WATCH_SET, target.mention, tone=Tone.OK, icon=Icon.DONE),
    )


async def deliver_watch_clear(interaction: discord.Interaction, target: discord.abc.User) -> None:
    guild = interaction.guild
    clearer = getattr(getattr(interaction.client, "moderation", None), "clear_watch", None)
    if guild is None or not callable(clearer):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    await clearer(guild.id, target.id)
    await respond(interaction, panel(copy.DOSSIER_WATCH_OFF, target.mention, tone=Tone.OK, icon=Icon.DONE))


async def deliver_history(interaction: discord.Interaction, target: discord.abc.User) -> None:
    guild = interaction.guild
    history = getattr(getattr(interaction.client, "moderation", None), "history", None)
    if guild is None or not callable(history):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    cases = await history(guild.id, target.id)
    if not cases:
        await respond(interaction, panel(copy.MOD_HISTORY_EMPTY, tone=Tone.INFO))
        return
    lines = [
        copy.mod_history_line(
            item.number,
            item.kind,
            item.reason,
            revoked_by=getattr(item, "revoked_by", None) if getattr(item, "revoked_at", None) else None,
        )
        for item in cases
    ]
    pages = [lines[index : index + HISTORY_PAGE] for index in range(0, len(lines), HISTORY_PAGE)]
    await respond(interaction, history_card(copy.MOD_HISTORY_TITLE.format(user=target.mention), pages, 1))


async def open_action_modal(interaction: discord.Interaction, target: discord.abc.User, action: DossierAction) -> None:
    if action is DossierAction.TIMEOUT:
        await interaction.response.send_modal(TimeoutReasonModal(target))
        return
    if action is DossierAction.WATCH:
        await interaction.response.send_modal(WatchModal(target))
        return
    if action is DossierAction.WATCH_CLEAR:
        await deliver_watch_clear(interaction, target)
        return
    if action is DossierAction.REVOKE:
        await interaction.response.send_modal(DossierRevokeModal(target))
        return
    await interaction.response.send_modal(CaseReasonModal(CaseKind(action.value), target))


class DossierRevokeModal(SafeModal, title=copy.DOSSIER_REVOKE_MODAL):
    number = ui.Label(
        text=copy.DOSSIER_REVOKE_NUMBER,
        component=ui.TextInput(min_length=1, max_length=8, required=True),
    )
    reason = ui.Label(
        text=copy.CASE_REVOKE_REASON,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, target: discord.abc.User) -> None:
        super().__init__()
        self.target = target

    async def submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        moderation = getattr(interaction.client, "moderation", None)
        revoke = getattr(moderation, "revoke", None)
        history = getattr(moderation, "history", None)
        if guild is None or not callable(revoke) or not callable(history):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        raw = _labeled_value(self.number).strip()
        if not raw.isdigit():
            await respond(interaction, denied(copy.DOSSIER_REVOKE_MISSING))
            return
        number = int(raw)
        cases = await history(guild.id, self.target.id)
        recent = {int(item.number) for item in cases[:DOSSIER_CASES]}
        match = next((item for item in cases if int(item.number) == number), None)
        if match is None or number not in recent:
            await respond(interaction, denied(copy.DOSSIER_REVOKE_MISSING))
            return
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        outcome = await revoke(guild, match.id, interaction.user, cleaned)
        if outcome.verdict is not None and str(outcome.verdict) == "ok":
            await respond(interaction, panel(copy.CASE_REVOKED_TITLE, tone=Tone.OK, icon=Icon.DONE))
            return
        await respond(interaction, denied(copy.mod_denied(str(outcome.verdict))))


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)
