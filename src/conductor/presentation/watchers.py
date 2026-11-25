"""Карточки состава следящих, приостановка и дайджест. Без sqlalchemy."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import ui

from conductor.domain.access import WATCHERS_NOTE, WATCHERS_SUSPEND, Rank
from conductor.domain.watchers import NOTE_LIMIT, RosterPerson, can_suspend, sort_roster
from conductor.presentation import copy
from conductor.presentation.guard import GuardVerdict
from conductor.presentation.interact import instant
from conductor.presentation.layout import denied, kv, listing, panel, respond, update_message
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.text import stamp
from conductor.presentation.tokens import Tone

_PANEL_KIND = "wh"


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


def format_roster_lines(people: Sequence[RosterPerson]) -> list[str]:
    lines: list[str] = []
    for person in sort_roster(people):
        metrics = person.metrics
        suspended = None
        if metrics.suspended_until is not None:
            suspended = copy.watchers_suspended_until(stamp(metrics.suspended_until, "F"))
        last = None if metrics.last_action is None else stamp(metrics.last_action, "R")
        lines.append(
            copy.watchers_member_line(
                person.mention,
                on_duty=person.on_duty,
                cases=metrics.cases_7d,
                today=person.today,
                rank=copy.rank_label(person.rank.key),
                revoked=metrics.revoked_30d,
                overdue=metrics.overdue,
                tickets=metrics.tickets_30d,
                rating=metrics.rating_avg,
                last_action=last,
                suspended=suspended,
            )
        )
    return lines


def roster_card(title: str, pages: list[list[str]], page: int) -> ui.LayoutView:
    total = max(1, len(pages))
    page = max(1, min(page, total))
    chunk = pages[page - 1] if pages else []
    body = listing(chunk, page, total)
    rows: tuple[ui.ActionRow, ...] = ()
    if total > 1:
        prev = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=page <= 1)
        nxt = ui.Button(label="→", style=discord.ButtonStyle.secondary, disabled=page >= total)

        async def back(interaction: discord.Interaction) -> None:
            await update_message(interaction, roster_card(title, pages, page - 1))

        async def forward(interaction: discord.Interaction) -> None:
            await update_message(interaction, roster_card(title, pages, page + 1))

        prev.callback = instant(back)
        nxt.callback = instant(forward)
        rows = (ui.ActionRow(prev, nxt),)
    return panel(title, body, tone=Tone.INFO, rows=rows)


def watcher_card(
    *,
    rank: Rank,
    role: str,
    on_duty: bool,
    quota: str | None,
    metrics: object,
    notes: Sequence[object],
    actions: Sequence[object],
    actor: Rank,
    actor_id: int,
    target_id: int,
    target_is_bot: bool,
) -> ui.LayoutView:
    cases = int(getattr(metrics, "cases_7d", 0) or 0)
    revoked = int(getattr(metrics, "revoked_30d", 0) or 0)
    overdue = int(getattr(metrics, "overdue", 0) or 0)
    tickets = int(getattr(metrics, "tickets_30d", 0) or 0)
    rating = getattr(metrics, "rating_avg", None)
    last_at = getattr(metrics, "last_action", None)
    until = getattr(metrics, "suspended_until", None)
    reason = getattr(metrics, "suspend_reason", None)
    note_text = copy.WATCHERS_NO_ACTIONS
    if notes:
        note_text = "\n".join(
            copy.watchers_note_line(stamp(row.created_at, "t"), f"<@{row.author_id}>", row.body) for row in notes
        )
    feed = copy.WATCHERS_NO_ACTIONS
    if actions:
        feed = "\n".join(
            copy.watchers_action_line(
                stamp(row.created_at, "R"),
                copy.staff_action_label(row.action),
                row.details,
            )
            for row in actions
        )
    fields = {
        copy.WATCHERS_FIELD_RANK: copy.rank_label(rank.key),
        copy.WATCHERS_FIELD_ROLE: role,
        copy.WATCHERS_FIELD_DUTY: copy.WATCHERS_ON_DUTY if on_duty else copy.WATCHERS_OFF_DUTY,
        copy.WATCHERS_FIELD_CASES: str(cases),
        copy.WATCHERS_FIELD_REVOKED: str(revoked),
        copy.WATCHERS_FIELD_OVERDUE: str(overdue),
        copy.WATCHERS_FIELD_TICKETS: copy.watchers_tickets_closed(tickets, rating),
        copy.WATCHERS_FIELD_LAST: "—" if last_at is None else stamp(last_at, "R"),
        copy.WATCHERS_FIELD_ACTIONS: feed,
        copy.WATCHERS_FIELD_NOTES: note_text,
    }
    if quota:
        fields[copy.WATCHERS_FIELD_QUOTA] = quota
    if until is not None:
        fields[copy.WATCHERS_FIELD_SUSPENDED] = copy.watchers_suspended_until(stamp(until, "F"))
        if reason:
            fields[copy.WATCHERS_FIELD_SUSPENDED] += f" · {reason}"
    buttons: list[ui.Button] = []
    if not target_is_bot and can_suspend(actor=actor, target=rank, actor_id=actor_id, target_id=target_id):
        if until is None:
            buttons.append(
                ui.Button(
                    label=copy.WATCHERS_BTN_SUSPEND,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(_PANEL_KIND, "susp", target_id),
                )
            )
        else:
            buttons.append(
                ui.Button(
                    label=copy.WATCHERS_BTN_LIFT,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(_PANEL_KIND, "lift", target_id),
                )
            )
    if actor >= Rank.SENIOR and not target_is_bot:
        buttons.append(
            ui.Button(
                label=copy.WATCHERS_BTN_NOTE,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(_PANEL_KIND, "note", target_id),
            )
        )
    rows: tuple[ui.ActionRow, ...] = (ui.ActionRow(*buttons),) if buttons else ()
    return panel(copy.WATCHERS_INFO_TITLE, kv(fields), tone=Tone.INFO, rows=rows)


def _report(interaction: discord.Interaction) -> object | None:
    return getattr(interaction.client, "watchers_report", None)


class WatcherSuspendModal(SafeModal, title=copy.WATCHERS_SUSPEND_MODAL):
    days = ui.Label(
        text=copy.WATCHERS_SUSPEND_DAYS,
        component=ui.TextInput(min_length=1, max_length=2, required=True, placeholder=copy.WATCHERS_SUSPEND_DAYS_HINT),
    )
    reason = ui.Label(
        text=copy.WATCHERS_SUSPEND_REASON,
        component=ui.TextInput(min_length=4, max_length=400, required=True),
    )

    def __init__(self, user_id: int, actor_rank: Rank, target_rank: Rank) -> None:
        super().__init__()
        self.user_id = user_id
        self.actor_rank = actor_rank
        self.target_rank = target_rank

    async def submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        report = _report(interaction)
        method = getattr(report, "suspend", None)
        if guild is None or not callable(method):
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        result = await method(
            guild.id,
            self.user_id,
            actor_id=interaction.user.id,
            actor_rank=self.actor_rank,
            target_rank=self.target_rank,
            days_raw=_labeled_value(self.days),
            reason=_labeled_value(self.reason),
        )
        if result == "days":
            await respond(interaction, denied(copy.WATCHERS_SUSPEND_BAD_DAYS))
            return
        if result == "reason":
            await respond(interaction, denied(copy.WATCHERS_SUSPEND_BAD_REASON))
            return
        if result == "hierarchy":
            await respond(interaction, denied(copy.HIERARCHY_DENIED))
            return
        if result == "already":
            await respond(interaction, denied(copy.WATCHERS_SUSPEND_ALREADY))
            return
        await respond(
            interaction,
            panel(
                copy.WATCHERS_SUSPEND_OK,
                copy.watchers_suspend_dm(stamp(result.until, "F"), result.reason),
                tone=Tone.WARN,
            ),
        )


class WatcherNoteModal(SafeModal, title=copy.WATCHERS_NOTE_MODAL):
    text = ui.Label(
        text=copy.WATCHERS_NOTE_LABEL,
        component=ui.TextInput(min_length=1, max_length=NOTE_LIMIT, required=True),
    )

    def __init__(self, user_id: int, actor_rank: Rank) -> None:
        super().__init__()
        self.user_id = user_id
        self.actor_rank = actor_rank

    async def submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        report = _report(interaction)
        method = getattr(report, "write_note", None)
        if guild is None or not callable(method):
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        stored = await method(
            guild.id,
            self.user_id,
            author_id=interaction.user.id,
            actor_rank=self.actor_rank,
            body=_labeled_value(self.text),
        )
        await respond(interaction, panel(copy.WATCHERS_NOTE_SAVED, stored.body, tone=Tone.OK))


class _WatcherTargetMixin:
    async def load_resource(self, *, guild_id: int, ref: str) -> object:
        method = getattr(_report(self._interaction), "guard_target", None)
        if not callable(method):
            return None
        return await method(guild_id, int(ref))


@register_dynamic_item
class WatcherSuspendButton(_WatcherTargetMixin, PersistentButton, template=r"^c:wh:susp:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "susp"
    access_key = WATCHERS_SUSPEND
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        resource = verdict.resource
        target_rank = Rank.NONE if resource is None else (resource.target_rank or Rank.NONE)
        target_id = int(self.ref)
        if not can_suspend(
            actor=verdict.rank,
            target=target_rank,
            actor_id=interaction.user.id,
            target_id=target_id,
        ):
            body = copy.WATCHERS_SUSPEND_SELF if target_id == interaction.user.id else copy.HIERARCHY_DENIED
            await respond(interaction, denied(body))
            return
        await interaction.response.send_modal(WatcherSuspendModal(target_id, verdict.rank, target_rank))


@register_dynamic_item
class WatcherLiftButton(_WatcherTargetMixin, PersistentButton, template=r"^c:wh:lift:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "lift"
    access_key = WATCHERS_SUSPEND

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        resource = verdict.resource
        target_rank = Rank.NONE if resource is None else (resource.target_rank or Rank.NONE)
        target_id = int(self.ref)
        if not can_suspend(
            actor=verdict.rank,
            target=target_rank,
            actor_id=interaction.user.id,
            target_id=target_id,
        ):
            body = copy.WATCHERS_SUSPEND_SELF if target_id == interaction.user.id else copy.HIERARCHY_DENIED
            await respond(interaction, denied(body))
            return
        guild = interaction.guild
        method = getattr(_report(interaction), "lift", None)
        if guild is None or not callable(method):
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        result = await method(
            guild.id,
            target_id,
            actor_id=interaction.user.id,
            actor_rank=verdict.rank,
            target_rank=target_rank,
        )
        if result == "hierarchy":
            await respond(interaction, denied(copy.HIERARCHY_DENIED))
            return
        if result == "missing":
            await respond(interaction, denied(copy.WATCHERS_SUSPEND_MISSING))
            return
        await respond(interaction, panel(copy.WATCHERS_LIFT_OK, copy.WATCHERS_LIFT_MANUAL, tone=Tone.OK))


@register_dynamic_item
class WatcherNoteButton(_WatcherTargetMixin, PersistentButton, template=r"^c:wh:note:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "note"
    access_key = WATCHERS_NOTE
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await interaction.response.send_modal(WatcherNoteModal(int(self.ref), verdict.rank))
