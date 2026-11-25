"""Постоянная панель тикетов и карточка внутри обращения. Без sqlalchemy и без services."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord
from discord import ui

from conductor.domain.access import (
    MOD_REVOKE,
    TICKETS_APPROVE,
    TICKETS_CLAIM,
    TICKETS_CLOSE_ANY,
    TICKETS_CLOSE_OWN,
    TICKETS_NOTES,
    TICKETS_PRIORITY,
    TICKETS_REOPEN,
)
from conductor.domain.moderation import DETAIL_REASON_MAX, DETAIL_REASON_MIN, ModVerdict, sanitize_detail_reason
from conductor.domain.mutations import MutationScope
from conductor.domain.renames import Deferred
from conductor.domain.tickets import (
    CHANNEL_NAME_LIMIT,
    FORM_ANSWER_LIMIT,
    FORM_PROMPT_LIMIT,
    MAX_FORM_FIELDS,
    NOTE_LIMIT,
    ApplicationTopic,
    CategoryKind,
    ClaimVerdict,
    CloseVerdict,
    EscalateVerdict,
    FlowDecision,
    OpenDecision,
    OpenStep,
    OpenVerdict,
    Outcome,
    RateVerdict,
    ReopenVerdict,
    TicketForm,
    TicketPriority,
    TicketState,
    parse_kind,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, run_guard
from conductor.presentation.interact import ack, run_teardown, split_teardown
from conductor.presentation.layout import (
    CardFile,
    denied,
    empty,
    panel,
    respond,
    upload_cards,
    upload_limit,
)
from conductor.presentation.layout import (
    update_message as _edit,
)
from conductor.presentation.mentions import going_mentions
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import (
    PersistentButton,
    PersistentSelect,
    format_custom_id,
    register_dynamic_item,
)
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "tk"
_REASON_LIMIT = 400

__all__ = [
    "PANEL_KIND",
    "TicketCard",
    "close_denied_text",
    "close_request_view",
    "public_panel_view",
    "ticket_autoclose_warn_view",
    "ticket_card_view",
    "ticket_welcome_view",
    "ticket_confirm_view",
    "ticket_escalate_view",
    "ticket_hint_view",
    "ticket_notes_view",
    "ticket_review_remind_view",
    "ticket_closed_dm_view",
    "ticket_anon_relay_view",
    "ticket_staff_relay_view",
    "ticket_log_card_view",
    "TicketLogCard",
    "FormQuestionsModal",
]


@dataclass(frozen=True, slots=True)
class TicketCard:
    ticket_id: int
    number: int
    category: str
    author_id: int
    claimed_by: int | None
    created_at: datetime | None = None
    answers: tuple[tuple[str, str], ...] = ()
    priority: str = TicketPriority.NORMAL
    closed: bool = False
    rating: int | None = None
    outcome: str | None = None
    close_reason: str | None = None
    info_text: str = ""
    state: str = TicketState.OPEN
    topic: str = ""
    category_kind: str = ""
    show_approve_req: bool = False
    show_approve: bool = False
    show_reject: bool = False
    show_appeal: bool = False
    show_review: bool = False
    appeal_owner: bool = False
    anonymous: bool = False


@dataclass(frozen=True, slots=True)
class TicketLogCard:
    ticket_id: int
    number: int
    category: str
    author_id: int
    created_at: datetime | None
    state: str
    closed_by: int | None = None
    closed_at: datetime | None = None
    outcome: str | None = None
    close_reason: str | None = None
    rating: int | None = None
    anonymous: bool = False


def public_panel_view(
    categories: Sequence[tuple[int, str]],
    *,
    open_count: int,
    limit: int,
) -> ui.LayoutView:
    rows = _category_rows(categories)
    return panel(
        copy.TICKET_PANEL_TITLE,
        copy.TICKET_PANEL_BODY,
        tone=Tone.WARN,
        icon=Icon.PANEL,
        fields={
            copy.TICKET_FIELD_OPEN: copy.ticket_open_count(open_count),
            copy.TICKET_FIELD_WAIT: copy.ticket_wait_value(),
        },
        footnote=copy.ticket_panel_footnote(limit),
        rows=rows,
        timeout=None,
    )


def ticket_log_card_view(card: TicketLogCard) -> ui.LayoutView:
    opened = "—" if card.created_at is None else discord.utils.format_dt(card.created_at, "F")
    closer = "—"
    if card.closed_by:
        stamp = "" if card.closed_at is None else f" · {discord.utils.format_dt(card.closed_at, 'F')}"
        closer = f"<@{card.closed_by}>{stamp}"
    ref = card.ticket_id
    return panel(
        copy.ticket_log_title(card.number, card.category),
        tone=Tone.MUTED,
        icon=Icon.CASE,
        fields={
            copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(card.author_id, anonymous=card.anonymous),
            copy.TICKET_FIELD_OPENED: opened,
            copy.TICKET_FIELD_STATE: copy.ticket_log_state(card.state),
            copy.TICKET_FIELD_CLOSER: closer,
            copy.TICKET_FIELD_OUTCOME: copy.ticket_outcome_label(card.outcome),
            copy.TICKET_FIELD_REASON: card.close_reason or "—",
            copy.TICKET_FIELD_RATING: copy.ticket_rating_stars(card.rating),
        },
        rows=(
            ui.ActionRow(
                ui.Button(
                    label=copy.TICKET_LOG_REASON,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "reason", ref),
                ),
                ui.Button(
                    label=copy.TICKET_LOG_TRANSCRIPT,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "transcript", ref),
                ),
            ),
        ),
        timeout=None,
    )


def ticket_card_view(card: TicketCard) -> ui.LayoutView:
    if card.state == TicketState.PENDING_APPROVAL or card.show_approve:
        return _pending_card_view(card)
    if card.show_review or card.state in {TicketState.APPEAL_CHIEF, TicketState.APPEAL_OWNER}:
        return _appeal_card_view(card)
    if card.closed:
        return _closed_card_view(card)
    ref = card.ticket_id
    opened = "—" if card.created_at is None else discord.utils.format_dt(card.created_at, "R")
    case_appeal = parse_kind(card.category_kind) is CategoryKind.APPEAL
    staff_buttons = [
        ui.Button(
            label=copy.TICKET_TAKE,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(PANEL_KIND, "take", ref),
        ),
        ui.Button(
            label=copy.TICKET_DROP,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "drop", ref),
        ),
        ui.Button(
            label=copy.TICKET_NOTES,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "note", ref),
        ),
        ui.Button(
            label=copy.TICKET_ESCALATE,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "ping", ref),
        ),
    ]
    if not case_appeal:
        staff_buttons.append(
            ui.Button(
                label=copy.TICKET_CLOSE,
                style=discord.ButtonStyle.danger,
                custom_id=format_custom_id(PANEL_KIND, "close", ref),
            )
        )
    rename_buttons: list[ui.Button] = [
        ui.Button(
            label=copy.TICKET_RENAME,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "rename", ref),
        ),
    ]
    if case_appeal:
        rename_buttons.extend(
            (
                ui.Button(
                    label=copy.CASE_KEEP,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "keep", ref),
                ),
                ui.Button(
                    label=copy.CASE_REVOKE_BTN,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "caserevoke", ref),
                ),
            )
        )
    rows: tuple[ui.ActionRow, ...] = (
        ui.ActionRow(*staff_buttons),
        ui.ActionRow(*rename_buttons),
        ui.ActionRow(
            ui.Select(
                custom_id=format_custom_id(PANEL_KIND, "prio", ref),
                placeholder=copy.TICKET_PRIORITY,
                min_values=1,
                max_values=1,
                options=_priority_options(card.priority),
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "xfer", ref),
                placeholder=copy.TICKET_TRANSFER,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "add", ref),
                placeholder=copy.TICKET_ADD,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "rem", ref),
                placeholder=copy.TICKET_REMOVE,
                min_values=1,
                max_values=1,
            ),
        ),
    )
    flow = _application_flow_row(card)
    if flow is not None:
        rows = (*rows, flow)
    return panel(
        copy.ticket_card_title(card.number, card.category, anonymous=card.anonymous),
        copy.ticket_form_body(card.answers),
        tone=Tone.WARN,
        fields={
            copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(card.author_id, anonymous=card.anonymous, extra=opened),
            copy.TICKET_FIELD_CLAIMER: copy.ticket_claimer_value(card.claimed_by),
            copy.TICKET_FIELD_PRIORITY: copy.ticket_priority_label(card.priority),
            **({copy.TICKET_FIELD_INFO: card.info_text} if card.info_text else {}),
        },
        footnote=copy.TICKET_CARD_FOOTNOTE,
        rows=rows,
        timeout=None,
    )


def ticket_welcome_view(author_id: int, category_name: str, *, appeal: bool = False) -> ui.LayoutView:
    if appeal:
        buttons = copy.TICKET_WELCOME_APPEAL_BUTTONS.format(
            keep=copy.CASE_KEEP,
            revoke=copy.CASE_REVOKE_BTN,
            ping=copy.TICKET_ESCALATE,
        )
    else:
        buttons = copy.TICKET_WELCOME_BUTTONS.format(close=copy.TICKET_CLOSE, ping=copy.TICKET_ESCALATE)
    title = category_name.strip() or copy.TICKET_TITLE
    return panel(title, copy.ticket_welcome_body(author_id, buttons), tone=Tone.INFO, timeout=None)


def _closed_card_view(card: TicketCard) -> ui.LayoutView:
    ref = card.ticket_id
    rows: list[ui.ActionRow] = []
    if card.rating is None and not card.anonymous:
        rows.append(
            ui.ActionRow(
                *[
                    ui.Button(
                        label=str(score),
                        style=discord.ButtonStyle.secondary,
                        custom_id=format_custom_id(PANEL_KIND, f"r{score}", ref),
                    )
                    for score in range(1, 6)
                ]
            )
        )
    closed_buttons = [
        ui.Button(
            label=copy.TICKET_REOPEN,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(PANEL_KIND, "reop", ref),
        )
    ]
    if card.show_appeal and not card.anonymous:
        closed_buttons.append(
            ui.Button(
                label=copy.ticket_appeal_label(owner=card.appeal_owner),
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "appeal", ref),
            )
        )
    rows.append(ui.ActionRow(*closed_buttons))
    fields: dict[str, object] = {
        copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(card.author_id, anonymous=card.anonymous),
        copy.TICKET_FIELD_PRIORITY: copy.ticket_priority_label(card.priority),
        copy.TICKET_FIELD_OUTCOME: copy.ticket_outcome_label(card.outcome),
        copy.TICKET_FIELD_REASON: card.close_reason or "—",
    }
    if card.rating is not None:
        fields[copy.TICKET_RATE_PROMPT] = str(card.rating)
    return panel(
        copy.ticket_card_title(card.number, card.category, anonymous=card.anonymous),
        copy.TICKET_CLOSED if card.rating is not None or card.anonymous else copy.TICKET_RATE_PROMPT,
        tone=Tone.OK,
        fields=fields,
        rows=tuple(rows),
        timeout=None,
    )


def _application_flow_row(card: TicketCard) -> ui.ActionRow | None:
    if not card.show_approve_req and not card.show_reject:
        return None
    buttons: list[ui.Button] = []
    if card.show_approve_req:
        buttons.append(
            ui.Button(
                label=copy.TICKET_APPROVE_REQ,
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, "approve_req", card.ticket_id),
            )
        )
    if card.show_reject:
        buttons.append(
            ui.Button(
                label=copy.TICKET_REJECT,
                style=discord.ButtonStyle.danger,
                custom_id=format_custom_id(PANEL_KIND, "reject", card.ticket_id),
            )
        )
    return ui.ActionRow(*buttons)


def _pending_card_view(card: TicketCard) -> ui.LayoutView:
    ref = card.ticket_id
    opened = "—" if card.created_at is None else discord.utils.format_dt(card.created_at, "R")
    fields: dict[str, object] = {
        copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(card.author_id, anonymous=card.anonymous, extra=opened),
        copy.TICKET_FIELD_CLAIMER: copy.ticket_claimer_value(card.claimed_by),
        copy.TICKET_FIELD_PRIORITY: copy.ticket_priority_label(card.priority),
    }
    if card.info_text:
        fields[copy.TICKET_FIELD_INFO] = card.info_text
    if card.topic == ApplicationTopic.MODERATOR:
        fields[copy.TICKET_FIELD_APPOINT] = copy.TICKET_APPOINT_HINT
    return panel(
        copy.ticket_card_title(card.number, card.category, anonymous=card.anonymous),
        copy.TICKET_PENDING_BODY,
        tone=Tone.WARN,
        fields=fields,
        rows=(
            ui.ActionRow(
                ui.Button(
                    label=copy.TICKET_APPROVE,
                    style=discord.ButtonStyle.success,
                    custom_id=format_custom_id(PANEL_KIND, "approve", ref),
                ),
                ui.Button(
                    label=copy.TICKET_REJECT,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "reject", ref),
                ),
            ),
        ),
        timeout=None,
    )


def _appeal_card_view(card: TicketCard) -> ui.LayoutView:
    ref = card.ticket_id
    return panel(
        copy.ticket_review_title(card.state),
        copy.TICKET_APPEAL_BODY,
        tone=Tone.WARN,
        fields={
            copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(card.author_id, anonymous=card.anonymous),
            copy.TICKET_FIELD_OUTCOME: copy.ticket_outcome_label(card.outcome),
            copy.TICKET_FIELD_REASON: card.close_reason or "—",
        },
        rows=(
            ui.ActionRow(
                ui.Button(
                    label=copy.TICKET_UPHOLD,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "uphold", ref),
                ),
                ui.Button(
                    label=copy.TICKET_OVERTURN,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "overturn", ref),
                ),
            ),
        ),
        timeout=None,
    )


def ticket_closed_dm_view(card: TicketCard) -> ui.LayoutView:
    ref = card.ticket_id
    rows: list[ui.ActionRow] = []
    if card.rating is None:
        rows.append(
            ui.ActionRow(
                *[
                    ui.Button(
                        label=str(score),
                        style=discord.ButtonStyle.secondary,
                        custom_id=format_custom_id(PANEL_KIND, f"r{score}", ref),
                    )
                    for score in range(1, 6)
                ]
            )
        )
    if card.show_appeal:
        rows.append(
            ui.ActionRow(
                ui.Button(
                    label=copy.ticket_appeal_label(owner=card.appeal_owner),
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "appeal", ref),
                )
            )
        )
    return panel(
        copy.ticket_card_title(card.number, card.category, anonymous=card.anonymous),
        copy.TICKET_CLOSED,
        tone=Tone.OK,
        fields={
            copy.TICKET_FIELD_OUTCOME: copy.ticket_outcome_label(card.outcome),
            copy.TICKET_FIELD_REASON: card.close_reason or "—",
        },
        rows=tuple(rows),
        timeout=None,
    )


def ticket_anon_relay_view(body: str, *, files: Sequence[CardFile] = ()) -> ui.LayoutView:
    return panel(copy.TICKET_ANON_RELAY, body, files=files, timeout=None)


def ticket_staff_relay_view(rank: str, body: str) -> ui.LayoutView:
    return panel(copy.ticket_staff_relay(rank), body, timeout=None)


def ticket_review_remind_view(state: str, *, owner_id: int | None = None) -> ui.LayoutView:
    mentions = () if owner_id is None else (owner_id,)
    body = copy.ticket_review_remind(state)
    if owner_id is not None:
        body = f"{body}\n<@{owner_id}>"
    return panel(
        copy.TICKET_TITLE,
        body,
        tone=Tone.WARN,
        mentions=going_mentions(mentions),
        timeout=None,
        icon=Icon.WARN,
    )


def _priority_options(current: str) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=copy.TICKET_PRIORITY_LOW,
            value=TicketPriority.LOW,
            default=current == TicketPriority.LOW,
        ),
        discord.SelectOption(
            label=copy.TICKET_PRIORITY_NORMAL,
            value=TicketPriority.NORMAL,
            default=current == TicketPriority.NORMAL,
        ),
        discord.SelectOption(
            label=copy.TICKET_PRIORITY_HIGH,
            value=TicketPriority.HIGH,
            default=current == TicketPriority.HIGH,
        ),
    ]


def ticket_hint_view(category_id: int, *, title: str, hint: str) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=copy.TICKET_FAQ,
            style=discord.ButtonStyle.success,
            custom_id=format_custom_id(PANEL_KIND, "faq", category_id),
        ),
        ui.Button(
            label=copy.TICKET_NEED,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(PANEL_KIND, "need", category_id),
        ),
    )
    return panel(
        title or copy.TICKET_HINT_TITLE,
        hint,
        tone=Tone.WARN,
        rows=(row,),
        timeout=None,
    )


def close_request_view(*, author_id: int) -> ui.LayoutView:
    return panel(
        copy.TICKET_CLOSE_REQUEST_TITLE,
        copy.ticket_close_request_body(author_id),
        tone=Tone.WARN,
        timeout=None,
        icon=Icon.WARN,
    )


def ticket_confirm_view(ticket_id: int, *, outcomes: Sequence[Outcome] = ()) -> ui.LayoutView:
    rows: list[ui.ActionRow] = []
    if outcomes:
        rows.append(
            ui.ActionRow(
                ui.Select(
                    custom_id=format_custom_id(PANEL_KIND, "outc", ticket_id),
                    placeholder=copy.TICKET_OUTCOME,
                    min_values=1,
                    max_values=1,
                    options=_outcome_options(outcomes),
                )
            )
        )
        rows.append(
            ui.ActionRow(
                ui.Button(
                    label=copy.CONFIRM_CANCEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "no", ticket_id),
                )
            )
        )
        body = copy.TICKET_CONFIRM_OUTCOME
    else:
        rows.append(
            ui.ActionRow(
                ui.Button(
                    label=copy.TICKET_CLOSE,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "yes", ticket_id),
                ),
                ui.Button(
                    label=copy.CONFIRM_CANCEL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(PANEL_KIND, "no", ticket_id),
                ),
            )
        )
        body = copy.TICKET_CONFIRM_BODY
    return panel(
        body,
        tone=Tone.DANGER,
        rows=tuple(rows),
        timeout=60.0,
        icon=Icon.WARN,
    )


def _outcome_options(outcomes: Sequence[Outcome]) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=copy.ticket_outcome_label(outcome), value=outcome) for outcome in outcomes]


def ticket_autoclose_warn_view(deadline: datetime) -> ui.LayoutView:
    return panel(
        copy.TICKET_AUTOCLOSE_TITLE,
        fields={copy.TICKET_FIELD_DEADLINE: discord.utils.format_dt(deadline, "R")},
        tone=Tone.WARN,
        timeout=None,
        icon=Icon.WARN,
    )


def ticket_escalate_view() -> ui.LayoutView:
    return panel(
        copy.TICKET_ESCALATE,
        copy.TICKET_ESCALATE_PING,
        tone=Tone.WARN,
        timeout=None,
    )


def ticket_notes_view(ticket_id: int, notes: Sequence[tuple[int, str]]) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=copy.TICKET_NOTE_ADD,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(PANEL_KIND, "nadd", ticket_id),
        )
    )
    return panel(
        copy.TICKET_NOTES,
        copy.ticket_notes_body(notes),
        tone=Tone.INFO,
        rows=(row,),
        timeout=None,
    )


def close_denied_text(verdict: CloseVerdict) -> str:
    return copy.ticket_close_denied(verdict.value)


def claim_denied_text(verdict: ClaimVerdict) -> str:
    return copy.ticket_claim_denied(verdict.value)


def _category_rows(categories: Sequence[tuple[int, str]]) -> tuple[ui.ActionRow, ...]:
    buttons = [
        ui.Button(
            label=name[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "open", category_id),
        )
        for category_id, name in categories
    ]
    rows: list[ui.ActionRow] = []
    for start in range(0, len(buttons), 4):
        rows.append(ui.ActionRow(*buttons[start : start + 4]))
    return tuple(rows)


def _tickets(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "tickets", None)


async def _guild_for_ticket(interaction: discord.Interaction, ref: str) -> discord.Guild | None:
    if interaction.guild is not None:
        return interaction.guild
    tickets = _tickets(interaction)
    resolver = getattr(tickets, "resolve_guild", None)
    if not callable(resolver):
        return None
    return await resolver(ref)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


def _selected_user(item: object) -> discord.abc.User | None:
    values = getattr(item, "values", None) or ()
    if not values:
        return None
    found = values[0]
    return found if isinstance(found, discord.abc.User) else None


async def _load_ticket(
    tickets: object,
    *,
    guild_id: int,
    ref: str,
    owner: str,
) -> GuardResource | None:
    getter = getattr(tickets, "guard_ticket", None)
    if not callable(getter):
        return None
    return await getter(guild_id, ref, owner=owner)


async def _load_category(tickets: object, *, guild_id: int, ref: str) -> GuardResource | None:
    getter = getattr(tickets, "guard_category", None)
    if not callable(getter):
        return None
    return await getter(guild_id, ref)


async def _begin_open(
    interaction: discord.Interaction,
    ref: str,
    *,
    answers: Sequence[tuple[str, str]] | None = None,
) -> None:
    tickets = _tickets(interaction)
    open_ticket = getattr(tickets, "open_ticket", None)
    if not callable(open_ticket) or interaction.guild is None:
        return
    if not interaction.response.is_done():
        await ack(interaction)
    result = await open_ticket(interaction.guild, interaction.user, ref, answers=answers)
    verdict = result.verdict if isinstance(result, OpenDecision) else result
    if verdict is OpenVerdict.OK:
        await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_OPEN_TITLE, tone=Tone.OK))
        return
    if verdict is OpenVerdict.LIMIT:
        await respond(interaction, denied(copy.TICKET_LIMIT))
        return
    if verdict in {OpenVerdict.TENURE, OpenVerdict.LADDER, OpenVerdict.GATES} and isinstance(result, OpenDecision):
        await respond(interaction, denied(copy.ticket_open_gates(result)))
        return
    if verdict is OpenVerdict.HIDDEN:
        await respond(interaction, denied(copy.TICKET_KIND_HIDDEN))
        return
    if verdict is OpenVerdict.DM_CLOSED:
        await respond(interaction, denied(copy.TICKET_DM_CLOSED))
        return
    if verdict is OpenVerdict.ANON_OPEN:
        await respond(interaction, denied(copy.TICKET_ANON_OPEN))
        return
    if verdict is OpenVerdict.HELD:
        await respond(interaction, panel(copy.WAIT_TITLE, copy.TICKET_CREATE_HELD, tone=Tone.MUTED))
        return
    await respond(interaction, denied(copy.STALE_BODY))


async def _offer_open(interaction: discord.Interaction, ref: str, *, skip_hint: bool = False) -> None:
    tickets = _tickets(interaction)
    preview = getattr(tickets, "open_preview", None)
    if not callable(preview) or interaction.guild is None:
        return
    step, hint, form, name = await preview(interaction.guild.id, ref)
    if not skip_hint and step is OpenStep.HINT:
        await respond(interaction, ticket_hint_view(int(ref), title=name, hint=hint))
        return
    if form.fields and (skip_hint or step is OpenStep.FORM):
        await interaction.response.send_modal(TicketFormModal(ref, form, name))
        return
    await _begin_open(interaction, ref)


class TicketCategoryMixin:
    kind = PANEL_KIND
    access_key = None
    mutation_scope: MutationScope | None = None
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        tickets = _tickets(self._interaction)
        actor_id = self._interaction.user.id
        resource = await _load_category(tickets, guild_id=guild_id, ref=ref)
        if resource is None:
            return None
        return GuardResource(
            ref=resource.ref,
            guild_id=resource.guild_id,
            owner_id=actor_id,
            channel_id=resource.channel_id,
        )

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None


class TicketControlMixin:
    kind = PANEL_KIND
    access_key: str | None = TICKETS_CLOSE_OWN
    mutation_scope: MutationScope | None = None
    owner_mode = "author"
    allow_closed = False
    public = False
    allow_dm = False
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        tickets = _tickets(self._interaction)
        if guild_id == 0:
            getter = getattr(tickets, "guard_ticket_dm", None)
            resource = None if not callable(getter) else await getter(ref, owner=type(self).owner_mode)
        else:
            resource = await _load_ticket(tickets, guild_id=guild_id, ref=ref, owner=type(self).owner_mode)
        if resource is None or not type(self).allow_closed:
            return resource
        return GuardResource(
            ref=resource.ref,
            guild_id=resource.guild_id,
            owner_id=resource.owner_id,
            closed=False,
            channel_id=resource.channel_id,
        )

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None


@register_dynamic_item
class TicketOpenButton(TicketCategoryMixin, PersistentButton, template=r"^c:tk:open:(?P<ref>\d+)$"):
    action = "open"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await _offer_open(interaction, self.ref)


@register_dynamic_item
class TicketFaqButton(TicketCategoryMixin, PersistentButton, template=r"^c:tk:faq:(?P<ref>\d+)$"):
    action = "faq"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        mark = getattr(tickets, "mark_faq_resolved", None)
        if callable(mark) and interaction.guild is not None:
            await mark(interaction.guild.id)
        await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_FAQ_OK, tone=Tone.OK))


@register_dynamic_item
class TicketNeedButton(TicketCategoryMixin, PersistentButton, template=r"^c:tk:need:(?P<ref>\d+)$"):
    action = "need"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await _offer_open(interaction, self.ref, skip_hint=True)


@register_dynamic_item
class TicketTakeButton(TicketControlMixin, PersistentButton, template=r"^c:tk:take:(?P<ref>\d+)$"):
    action = "take"
    access_key = TICKETS_CLAIM
    mutation_scope = None
    owner_mode = "none"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        claim = getattr(tickets, "claim_ticket", None)
        if not callable(claim) or interaction.guild is None:
            return
        result = await claim(interaction.guild, self.ref, interaction.user)
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_CLAIM_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


@register_dynamic_item
class TicketDropButton(TicketControlMixin, PersistentButton, template=r"^c:tk:drop:(?P<ref>\d+)$"):
    action = "drop"
    access_key = TICKETS_CLOSE_ANY
    mutation_scope = None
    owner_mode = "claimer"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        drop = getattr(tickets, "unclaim_ticket", None)
        if not callable(drop) or interaction.guild is None:
            return
        result = await drop(interaction.guild, self.ref, interaction.user)
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_UNCLAIM_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


@register_dynamic_item
class TicketCloseButton(TicketControlMixin, PersistentButton, template=r"^c:tk:close:(?P<ref>\d+)$"):
    action = "close"
    access_key = TICKETS_CLOSE_OWN
    mutation_scope = None
    owner_mode = "author"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        check = getattr(tickets, "close_verdict", None)
        if not callable(check) or interaction.guild is None:
            return
        result = await check(interaction.guild, self.ref, interaction.user)
        if result is CloseVerdict.AUTHOR_BLOCKED:
            request = getattr(tickets, "request_close", None)
            if callable(request) and await request(interaction.guild, self.ref, interaction.user):
                await respond(
                    interaction,
                    panel(copy.TICKET_TITLE, copy.TICKET_CLOSEREQUEST_OK, tone=Tone.OK),
                )
                return
            await respond(interaction, denied(close_denied_text(result)))
            return
        if result is not CloseVerdict.OK:
            await respond(interaction, denied(close_denied_text(result)))
            return
        staff = getattr(tickets, "actor_is_staff", None)
        outcomes: tuple[Outcome, ...] = ()
        if callable(staff) and await staff(interaction.guild, interaction.user):
            listed = getattr(tickets, "close_outcomes", None)
            if callable(listed):
                outcomes = tuple(await listed(interaction.guild, self.ref))
        await _edit(interaction, ticket_confirm_view(int(self.ref), outcomes=outcomes))


@register_dynamic_item
class TicketApproveReqButton(TicketControlMixin, PersistentButton, template=r"^c:tk:approve_req:(?P<ref>\d+)$"):
    action = "approve_req"
    access_key = TICKETS_CLOSE_ANY
    owner_mode = "claimer"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        request = getattr(tickets, "request_approval", None)
        if not callable(request) or interaction.guild is None:
            return
        result = await request(interaction.guild, self.ref, interaction.user)
        await _respond_flow(interaction, result, copy.TICKET_APPROVE_REQ_OK)


@register_dynamic_item
class TicketApproveButton(TicketControlMixin, PersistentButton, template=r"^c:tk:approve:(?P<ref>\d+)$"):
    action = "approve"
    access_key = TICKETS_APPROVE
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "none"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        approve = getattr(tickets, "approve_ticket", None)
        if not callable(approve) or interaction.guild is None:
            return
        result = await approve(interaction.guild, self.ref, interaction.user)
        await _respond_flow(interaction, result, copy.TICKET_APPROVE_OK)


@register_dynamic_item
class TicketRejectButton(TicketControlMixin, PersistentButton, template=r"^c:tk:reject:(?P<ref>\d+)$"):
    action = "reject"
    access_key = TICKETS_CLOSE_ANY
    owner_mode = "claimer"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(CloseReasonModal(self.ref, outcome=Outcome.REJECTED, flow="reject"))


@register_dynamic_item
class TicketAppealButton(TicketControlMixin, PersistentButton, template=r"^c:tk:appeal:(?P<ref>\d+)$"):
    action = "appeal"
    access_key = None
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "author"
    allow_closed = True
    allow_dm = True

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        appeal = getattr(tickets, "appeal_ticket", None)
        guild = await _guild_for_ticket(interaction, self.ref)
        if not callable(appeal) or guild is None:
            return
        result = await appeal(guild, self.ref, interaction.user)
        await _respond_flow(interaction, result, copy.TICKET_APPEAL_OK)


class CaseKeepModal(SafeModal, title=copy.CASE_KEEP_MODAL):
    reason = ui.Label(
        text=copy.CASE_KEEP_REASON,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        tickets = _tickets(interaction)
        keep = getattr(tickets, "keep_case_appeal", None)
        if not callable(keep) or interaction.guild is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        result = await keep(interaction.guild, self.ref, interaction.user, reason=cleaned)
        if result is CloseVerdict.OK:
            await respond(interaction, panel(copy.TICKET_CLOSED, tone=Tone.OK))
            return
        await respond(interaction, denied(close_denied_text(result)))


class CaseRevokeModal(SafeModal, title=copy.CASE_REVOKE_MODAL):
    reason = ui.Label(
        text=copy.CASE_REVOKE_REASON,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        tickets = _tickets(interaction)
        accept = getattr(tickets, "accept_case_appeal", None)
        if not callable(accept) or interaction.guild is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        result = await accept(interaction.guild, self.ref, interaction.user, reason=cleaned)
        if result is ModVerdict.OK:
            await respond(interaction, panel(copy.CASE_REVOKED_TITLE, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.mod_denied(str(result))))


@register_dynamic_item
class CaseKeepButton(TicketControlMixin, PersistentButton, template=r"^c:tk:keep:(?P<ref>\d+)$"):
    action = "keep"
    access_key = TICKETS_CLOSE_ANY
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "none"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(CaseKeepModal(self.ref))


@register_dynamic_item
class CaseTicketRevokeButton(TicketControlMixin, PersistentButton, template=r"^c:tk:caserevoke:(?P<ref>\d+)$"):
    action = "caserevoke"
    access_key = MOD_REVOKE
    mutation_scope = MutationScope.MEMBERS_MODERATION
    owner_mode = "none"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(CaseRevokeModal(self.ref))


@register_dynamic_item
class TicketUpholdButton(TicketControlMixin, PersistentButton, template=r"^c:tk:uphold:(?P<ref>\d+)$"):
    action = "uphold"
    access_key = None
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "none"
    public = True

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        uphold = getattr(tickets, "uphold_ticket", None)
        if not callable(uphold) or interaction.guild is None:
            return
        result = await uphold(interaction.guild, self.ref, interaction.user)
        await _respond_flow(interaction, result, copy.TICKET_UPHOLD_OK)


@register_dynamic_item
class TicketOverturnButton(TicketControlMixin, PersistentButton, template=r"^c:tk:overturn:(?P<ref>\d+)$"):
    action = "overturn"
    access_key = None
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "none"
    public = True

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        overturn = getattr(tickets, "overturn_ticket", None)
        if not callable(overturn) or interaction.guild is None:
            return
        result = await overturn(interaction.guild, self.ref, interaction.user)
        await _respond_flow(interaction, result, copy.TICKET_OVERTURN_OK)


@register_dynamic_item
class TicketRenameButton(TicketControlMixin, PersistentButton, template=r"^c:tk:rename:(?P<ref>\d+)$"):
    action = "rename"
    access_key = TICKETS_CLOSE_ANY
    mutation_scope = None
    owner_mode = "claimer"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(TicketRenameModal(self.ref))


@register_dynamic_item
class TicketYesButton(TicketControlMixin, PersistentButton, template=r"^c:tk:yes:(?P<ref>\d+)$"):
    action = "yes"
    access_key = TICKETS_CLOSE_OWN
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "author"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        staff = getattr(tickets, "actor_is_staff", None)
        if callable(staff) and interaction.guild is not None and await staff(interaction.guild, interaction.user):
            await respond(interaction, denied(copy.TICKET_NEED_OUTCOME))
            return
        await _finish_close(interaction, self.ref, reason=None)


@register_dynamic_item
class TicketNoButton(TicketControlMixin, PersistentButton, template=r"^c:tk:no:(?P<ref>\d+)$"):
    action = "no"
    access_key = TICKETS_CLOSE_OWN
    mutation_scope = None
    owner_mode = "author"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        card = getattr(tickets, "card_for", None)
        if not callable(card) or interaction.guild is None:
            return
        view = await card(interaction.guild, self.ref)
        if view is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        await _edit(interaction, view)


class TicketSelect(PersistentSelect, template=re.compile(r"^c:tk:noop:(?P<ref>\d+)$")):
    kind = PANEL_KIND
    action: str = "noop"
    access_key: str | None = TICKETS_CLOSE_ANY
    mutation_scope: MutationScope | None = MutationScope.CHANNELS_TICKETS
    owner_mode = "claimer"
    acks_first = True

    def __init__(self, ref: int | str, *, item: ui.UserSelect | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, type(self).action, self.ref),
                placeholder=copy.TICKET_TRANSFER,
                min_values=1,
                max_values=1,
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_ticket(_tickets(self._interaction), guild_id=guild_id, ref=ref, owner=type(self).owner_mode)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> TicketSelect:
        select = item if isinstance(item, ui.UserSelect) else None
        return cls(match["ref"], item=select)


@register_dynamic_item
class TicketTransferSelect(TicketSelect, template=r"^c:tk:xfer:(?P<ref>\d+)$"):
    action = "xfer"
    mutation_scope = None

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        transfer = getattr(tickets, "transfer_ticket", None)
        target = _selected_user(self.item)
        if not callable(transfer) or target is None or interaction.guild is None:
            return
        result = await transfer(interaction.guild, self.ref, interaction.user, target)
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_TRANSFER_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


@register_dynamic_item
class TicketAddSelect(TicketSelect, template=r"^c:tk:add:(?P<ref>\d+)$"):
    action = "add"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        add = getattr(tickets, "add_member", None)
        target = _selected_user(self.item)
        if not callable(add) or target is None or interaction.guild is None:
            return
        ok = await add(interaction.guild, self.ref, interaction.user, target)
        if ok:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_ADD_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.STALE_BODY))


@register_dynamic_item
class TicketRemoveSelect(TicketSelect, template=r"^c:tk:rem:(?P<ref>\d+)$"):
    action = "rem"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        remove = getattr(tickets, "remove_member", None)
        target = _selected_user(self.item)
        if not callable(remove) or target is None or interaction.guild is None:
            return
        result = await remove(interaction.guild, self.ref, interaction.user, target)
        if result is True:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_REMOVE_OK, tone=Tone.OK))
            return
        if result == "author":
            await respond(interaction, denied(copy.TICKET_KEEP_AUTHOR))
            return
        await respond(interaction, denied(copy.STALE_BODY))


@register_dynamic_item
class TicketNotesButton(TicketControlMixin, PersistentButton, template=r"^c:tk:note:(?P<ref>\d+)$"):
    action = "note"
    access_key = TICKETS_NOTES
    mutation_scope = None
    owner_mode = "none"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        listing = getattr(tickets, "list_notes", None)
        if not callable(listing) or interaction.guild is None:
            return
        notes = await listing(interaction.guild, self.ref, interaction.user)
        if notes is None:
            await respond(interaction, denied(copy.TICKET_NOTES_DENIED))
            return
        await respond(interaction, ticket_notes_view(int(self.ref), notes))


@register_dynamic_item
class TicketNoteAddButton(TicketControlMixin, PersistentButton, template=r"^c:tk:nadd:(?P<ref>\d+)$"):
    action = "nadd"
    access_key = TICKETS_NOTES
    mutation_scope = None
    owner_mode = "none"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(TicketNoteModal(self.ref))


@register_dynamic_item
class TicketEscalateButton(TicketControlMixin, PersistentButton, template=r"^c:tk:ping:(?P<ref>\d+)$"):
    action = "ping"
    access_key = TICKETS_CLAIM
    mutation_scope = None
    owner_mode = "author"

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        escalate = getattr(tickets, "escalate_ticket", None)
        if not callable(escalate) or interaction.guild is None:
            return
        result = await escalate(interaction.guild, self.ref, interaction.user)
        if result is EscalateVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_ESCALATE_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.ticket_escalate_denied(result.value)))


@register_dynamic_item
class TicketReopenButton(TicketControlMixin, PersistentButton, template=r"^c:tk:reop:(?P<ref>\d+)$"):
    action = "reop"
    access_key = TICKETS_REOPEN
    mutation_scope = MutationScope.CHANNELS_TICKETS
    owner_mode = "none"
    allow_closed = True

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        reopen = getattr(tickets, "reopen_ticket", None)
        if not callable(reopen) or interaction.guild is None:
            return
        result = await reopen(interaction.guild, self.ref, interaction.user)
        if result is ReopenVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_REOPEN_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.ticket_reopen_denied(result.value)))


@register_dynamic_item
class TicketReasonButton(TicketControlMixin, PersistentButton, template=r"^c:tk:reason:(?P<ref>\d+)$"):
    action = "reason"
    access_key = TICKETS_CLOSE_ANY
    mutation_scope = None
    owner_mode = "closer"
    allow_closed = True
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(TicketReasonModal(self.ref))


@register_dynamic_item
class TicketTranscriptButton(TicketControlMixin, PersistentButton, template=r"^c:tk:transcript:(?P<ref>\d+)$"):
    action = "transcript"
    access_key = None
    mutation_scope = None
    owner_mode = "none"
    allow_closed = True
    public = True

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        getter = getattr(tickets, "transcript_file", None)
        if not callable(getter) or interaction.guild is None:
            return
        payload = await getter(interaction.guild, self.ref, interaction.user)
        if payload is None:
            await respond(interaction, denied(copy.TICKET_LOG_TRANSCRIPT_MISSING))
            return
        data, name = payload
        cards = upload_cards(
            copy.TICKET_TRANSCRIPT_TITLE,
            filename=name,
            data=data,
            limit=upload_limit(interaction.guild),
            tone=Tone.MUTED,
        )
        for card in cards:
            await respond(interaction, card)


@register_dynamic_item
class TicketRateButton(TicketControlMixin, PersistentButton, template=r"^c:tk:r(?P<score>[1-5]):(?P<ref>\d+)$"):
    action = "r1"
    access_key = None
    mutation_scope = None
    owner_mode = "author"
    allow_closed = True
    allow_dm = True
    score = 1

    def __init__(self, ref: int | str, *, item: ui.Button | None = None, score: int = 1) -> None:
        self.score = score
        super().__init__(ref, item=item)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        rate = getattr(tickets, "rate_ticket", None)
        guild = await _guild_for_ticket(interaction, self.ref)
        if not callable(rate) or guild is None:
            return
        result = await rate(guild, self.ref, interaction.user, self.score)
        if result is RateVerdict.OK:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_RATED, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.ticket_rate_denied(result.value)))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> TicketRateButton:
        button = item if isinstance(item, ui.Button) else None
        return cls(match["ref"], item=button, score=int(match["score"]))


class TicketPrioritySelect(PersistentSelect, template=re.compile(r"^c:tk:prio:(?P<ref>\d+)$")):
    kind = PANEL_KIND
    action = "prio"
    access_key: str | None = TICKETS_PRIORITY
    mutation_scope: MutationScope | None = None
    owner_mode = "none"

    def __init__(self, ref: int | str, *, item: ui.Select | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.Select(
                custom_id=format_custom_id(PANEL_KIND, "prio", self.ref),
                placeholder=copy.TICKET_PRIORITY,
                min_values=1,
                max_values=1,
                options=_priority_options(TicketPriority.NORMAL),
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_ticket(_tickets(self._interaction), guild_id=guild_id, ref=ref, owner=type(self).owner_mode)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        tickets = _tickets(interaction)
        set_priority = getattr(tickets, "set_priority", None)
        chosen_values = tuple(values) if values else tuple(getattr(self.item, "values", None) or ())
        chosen = str(chosen_values[0]) if chosen_values else TicketPriority.NORMAL
        if not callable(set_priority) or interaction.guild is None:
            return
        ok = await set_priority(interaction.guild, self.ref, interaction.user, chosen)
        if ok:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.ticket_priority_label(chosen), tone=Tone.OK))
            return
        await respond(interaction, denied(copy.STALE_BODY))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> TicketPrioritySelect:
        select = item if isinstance(item, ui.Select) else None
        return cls(match["ref"], item=select)


register_dynamic_item(TicketPrioritySelect)


class TicketRenameModal(SafeModal, title=copy.TICKET_RENAME_MODAL):
    name = ui.Label(
        text=copy.TICKET_RENAME_LABEL,
        component=ui.TextInput(max_length=CHANNEL_NAME_LIMIT, required=True),
    )

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_ticket(tickets, guild_id=guild_id, ref=ref, owner="claimer")

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=TICKETS_CLOSE_ANY,
            scope=MutationScope.CHANNELS_TICKETS,
        )
        if not verdict.ok:
            return
        rename = getattr(tickets, "rename_ticket", None)
        if not callable(rename) or interaction.guild is None:
            return
        ok = await rename(interaction.guild, self.ref, interaction.user, _labeled_value(self.name))
        if isinstance(ok, Deferred):
            await respond(interaction, panel(copy.TICKET_TITLE, copy.rename_deferred(ok.until), tone=Tone.WARN))
            return
        if ok:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_RENAMED, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.STALE_BODY))


class TicketOutcomeSelect(PersistentSelect, template=re.compile(r"^c:tk:outc:(?P<ref>\d+)$")):
    kind = PANEL_KIND
    action = "outc"
    access_key: str | None = TICKETS_CLOSE_OWN
    mutation_scope: MutationScope | None = MutationScope.CHANNELS_TICKETS
    owner_mode = "author"
    acks_first = False

    def __init__(self, ref: int | str, *, item: ui.Select | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.Select(
                custom_id=format_custom_id(PANEL_KIND, "outc", self.ref),
                placeholder=copy.TICKET_OUTCOME,
                min_values=1,
                max_values=1,
                options=_outcome_options((Outcome.RESOLVED,)),
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_ticket(_tickets(self._interaction), guild_id=guild_id, ref=ref, owner=type(self).owner_mode)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        chosen_values = tuple(values) if values else tuple(getattr(self.item, "values", None) or ())
        chosen = str(chosen_values[0]) if chosen_values else ""
        if not chosen:
            await respond(interaction, denied(copy.TICKET_NEED_OUTCOME))
            return
        await interaction.response.send_modal(CloseReasonModal(self.ref, outcome=chosen))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> TicketOutcomeSelect:
        select = item if isinstance(item, ui.Select) else None
        return cls(match["ref"], item=select)


register_dynamic_item(TicketOutcomeSelect)


class CloseReasonModal(SafeModal, title=copy.TICKET_REASON_MODAL):
    reason = ui.Label(
        text=copy.TICKET_REASON_LABEL,
        component=ui.TextInput(max_length=_REASON_LIMIT, required=True),
    )

    def __init__(self, ref: str, *, outcome: str | None = None, flow: str | None = None) -> None:
        super().__init__()
        self.ref = ref
        self.outcome = outcome
        self.flow = flow

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)
        owner = "claimer" if self.flow == "reject" else "author"
        key = TICKETS_CLOSE_ANY if self.flow == "reject" else TICKETS_CLOSE_OWN

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_ticket(tickets, guild_id=guild_id, ref=ref, owner=owner)

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=key,
            scope=MutationScope.CHANNELS_TICKETS,
        )
        if not verdict.ok:
            return
        reason = _labeled_value(self.reason)
        if self.flow == "reject":
            reject = getattr(tickets, "reject_ticket", None)
            if not callable(reject) or interaction.guild is None:
                return
            result = await reject(interaction.guild, self.ref, interaction.user, reason=reason)
            await _respond_flow(interaction, result, copy.TICKET_CLOSED)
            return
        await _finish_close(interaction, self.ref, reason=reason, outcome=self.outcome)


class TicketReasonModal(SafeModal, title=copy.TICKET_LOG_REASON_MODAL):
    reason = ui.Label(
        text=copy.TICKET_REASON_LABEL,
        component=ui.TextInput(max_length=_REASON_LIMIT, required=True),
    )

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            resource = await _load_ticket(tickets, guild_id=guild_id, ref=ref, owner="closer")
            if resource is None:
                return None
            return GuardResource(
                ref=resource.ref,
                guild_id=resource.guild_id,
                owner_id=resource.owner_id,
                closed=False,
                channel_id=resource.channel_id,
            )

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=TICKETS_CLOSE_ANY,
            scope=None,
        )
        if not verdict.ok:
            return
        setter = getattr(tickets, "set_close_reason", None)
        if not callable(setter) or interaction.guild is None:
            return
        ok = await setter(interaction.guild, self.ref, interaction.user, _labeled_value(self.reason))
        if ok:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_LOG_REASON_DONE, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.TICKET_LOG_REASON_DENIED))


async def _respond_flow(interaction: discord.Interaction, result: object, ok_text: str) -> None:
    if isinstance(result, FlowDecision) and result.ok:
        await respond(interaction, panel(copy.TICKET_TITLE, ok_text, tone=Tone.OK))
        return
    reason = result.reason if isinstance(result, FlowDecision) else ""
    await respond(interaction, denied(copy.ticket_flow_denied(reason)))


async def _finish_close(
    interaction: discord.Interaction,
    ref: str,
    *,
    reason: str | None,
    outcome: str | None = None,
) -> None:
    tickets = _tickets(interaction)
    close = getattr(tickets, "close_ticket", None)
    if not callable(close) or interaction.guild is None:
        return
    result = await close(interaction.guild, ref, interaction.user, reason=reason, outcome=outcome)
    body, teardown = split_teardown(result)
    if body is CloseVerdict.OK:
        await respond(interaction, panel(copy.TICKET_CLOSED, tone=Tone.OK))
        await run_teardown(teardown)
        return
    await respond(interaction, denied(close_denied_text(body)))


class TicketFormModal(SafeModal):
    def __init__(self, ref: str, form: TicketForm, category: str) -> None:
        title = (category or copy.TICKET_FORM_TITLE)[:45] or copy.TICKET_FORM_TITLE
        super().__init__(title=title)
        self.ref = ref
        self._prompts = [field.prompt for field in form.fields[:MAX_FORM_FIELDS]]
        self._labels: list[ui.Label] = []
        for field in form.fields[:MAX_FORM_FIELDS]:
            style = discord.TextStyle.short if field.style == "short" else discord.TextStyle.paragraph
            label = ui.Label(
                text=field.prompt[:FORM_PROMPT_LIMIT],
                component=ui.TextInput(
                    max_length=FORM_ANSWER_LIMIT,
                    required=field.required,
                    style=style,
                ),
            )
            self.add_item(label)
            self._labels.append(label)

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)
        actor_id = interaction.user.id

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            resource = await _load_category(tickets, guild_id=guild_id, ref=ref)
            if resource is None:
                return None
            return GuardResource(
                ref=resource.ref,
                guild_id=resource.guild_id,
                owner_id=actor_id,
                channel_id=resource.channel_id,
            )

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=None,
            scope=MutationScope.CHANNELS_TICKETS,
        )
        if not verdict.ok:
            return
        answers = [(prompt, _labeled_value(label)) for prompt, label in zip(self._prompts, self._labels, strict=True)]
        await _begin_open(interaction, self.ref, answers=answers)


class TicketNoteModal(SafeModal, title=copy.TICKET_NOTE_MODAL):
    body = ui.Label(
        text=copy.TICKET_NOTE_LABEL,
        component=ui.TextInput(max_length=NOTE_LIMIT, required=True, style=discord.TextStyle.paragraph),
    )

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_ticket(tickets, guild_id=guild_id, ref=ref, owner="none")

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=TICKETS_NOTES,
            scope=None,
        )
        if not verdict.ok:
            return
        add = getattr(tickets, "add_note", None)
        if not callable(add) or interaction.guild is None:
            return
        ok = await add(interaction.guild, self.ref, interaction.user, _labeled_value(self.body))
        if ok:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_NOTES_ADDED, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.TICKET_NOTES_DENIED))


class FormQuestionsModal(SafeModal, title=copy.TICKET_FORM_QUESTIONS_MODAL):
    def __init__(
        self,
        category: str,
        existing: Sequence[str] = (),
        *,
        on_done: Callable[[discord.Interaction, str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self.category = category
        self.on_done = on_done
        self._labels: list[ui.Label] = []
        for index in range(MAX_FORM_FIELDS):
            default = existing[index] if index < len(existing) else ""
            label = ui.Label(
                text=copy.ticket_form_field_label(index + 1),
                component=ui.TextInput(
                    max_length=FORM_PROMPT_LIMIT,
                    required=False,
                    default=default,
                    style=discord.TextStyle.short,
                ),
            )
            self.add_item(label)
            self._labels.append(label)

    async def submit(self, interaction: discord.Interaction) -> None:
        tickets = _tickets(interaction)
        save = getattr(tickets, "set_category_form", None)
        if not callable(save) or interaction.guild is None:
            return
        prompts = [_labeled_value(label) for label in self._labels]
        updated = await save(interaction.guild.id, self.category, prompts)
        if self.on_done is not None:
            if not updated:
                await respond(interaction, empty(copy.TICKET_NO_CATEGORY))
                return
            await self.on_done(interaction, self.category)
            return
        if updated:
            await respond(interaction, panel(copy.TICKET_TITLE, copy.TICKET_FORM_SAVED, tone=Tone.OK))
            return
        await respond(interaction, empty(copy.TICKET_NO_CATEGORY))
