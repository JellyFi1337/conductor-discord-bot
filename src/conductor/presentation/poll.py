"""Панель опроса, модалка старта и кнопки. Без sqlalchemy и без services."""

from __future__ import annotations

from typing import Any, ClassVar

import discord
from discord import ui

from conductor.domain.access import POLL_MANAGE
from conductor.domain.poll import (
    PollCard,
    PollKind,
    PollSpec,
    PollState,
    counts_for,
    parse_spec,
    shown_count,
    shown_voters,
    voters_for,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.layout import BUTTON_LABEL_LIMIT, denied, empty, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.motion import Countdown
from conductor.presentation.persistent import (
    PersistentButton,
    PersistentSelect,
    format_custom_id,
    register_dynamic_item,
)
from conductor.presentation.roster import roster_preview
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "pl"
POLL_ICON = "📊"
SELECT_FROM = 6

__all__ = [
    "PANEL_KIND",
    "PollStartModal",
    "closed_view",
    "confirm_delete_view",
    "live_view",
    "option_lines",
    "reveal_view",
]


def _clip(text: str) -> str:
    if len(text) <= BUTTON_LABEL_LIMIT:
        return text
    return text[: BUTTON_LABEL_LIMIT - 1] + "…"


def option_lines(card: PollCard, *, reveal: bool = False) -> tuple[str, ...]:
    tallies = counts_for(len(card.options), card.votes)
    lines: list[str] = []
    for index, label in enumerate(card.options):
        real = tallies[index] if index < len(tallies) else 0
        if reveal:
            count = real
            names = voters_for(card.votes, index)
        else:
            count = shown_count(card.kind, card.state, real)
            names = shown_voters(card.kind, card.state, voters_for(card.votes, index))
        preview = roster_preview(names, len(names))
        if preview:
            lines.append(f"• {label} — {count} · {preview}")
        else:
            lines.append(copy.poll_option_line(label, count))
    return tuple(lines)


def _fields(card: PollCard) -> dict[str, object]:
    total = shown_count(card.kind, card.state, len(card.votes))
    fields: dict[str, object] = {
        copy.POLL_TYPE: copy.poll_kind_label(card.kind),
        copy.POLL_UNTIL: Countdown().render(card.ends_at),
        copy.POLL_VOTES: total,
        copy.POLL_CHANGE: card.allow_change,
    }
    return fields


def _vote_rows(card: PollCard) -> tuple[ui.ActionRow, ...]:
    close = ui.Button(
        label=copy.POLL_CLOSE,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "close", card.poll_id),
    )
    delete = ui.Button(
        label=copy.POLL_DELETE_VERB,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "del", card.poll_id),
    )
    if len(card.options) >= SELECT_FROM:
        select = ui.Select(
            custom_id=format_custom_id(PANEL_KIND, "pick", card.poll_id),
            placeholder=copy.POLL_PICK,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=_clip(label), value=str(index)) for index, label in enumerate(card.options)
            ],
        )
        return (ui.ActionRow(select), ui.ActionRow(close, delete))
    buttons: list[ui.Button] = []
    for index, label in enumerate(card.options):
        buttons.append(
            ui.Button(
                label=_clip(label),
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, f"v{index}", card.poll_id),
            )
        )
    rows: list[ui.ActionRow] = []
    for start in range(0, len(buttons), 5):
        rows.append(ui.ActionRow(*buttons[start : start + 5]))
    rows.append(ui.ActionRow(close, delete))
    return tuple(rows)


def live_view(card: PollCard, *, ping_role_id: int | None = None) -> ui.LayoutView:
    return panel(
        card.question,
        option_lines(card),
        fields=_fields(card),
        footnote=copy.poll_live_footnote(card.host_id, ping_role_id),
        rows=_vote_rows(card),
        tone=Tone.SPECIAL,
        icon=POLL_ICON,
        timeout=None,
    )


def closed_view(card: PollCard) -> ui.LayoutView:
    title = f"{card.question} — {copy.POLL_OVER}"
    if card.state is PollState.DELETED:
        title = f"{card.question} — {copy.POLL_REMOVED}"
    return panel(
        title,
        option_lines(card),
        fields=_fields(card),
        footnote=copy.poll_host_line(card.host_id),
        tone=Tone.MUTED,
        timeout=None,
        icon=Icon.LOCK,
    )


def reveal_view(card: PollCard) -> ui.LayoutView:
    return panel(
        copy.POLL_REVEAL_TITLE,
        (copy.POLL_REVEAL_NOTICE, *option_lines(card, reveal=True)),
        footnote=copy.poll_host_line(card.host_id),
        tone=Tone.WARN,
        icon=Icon.WARN,
        timeout=None,
    )


def confirm_delete_view(poll_id: int) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.POLL_DELETE_VERB,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "yes", poll_id),
    )
    no = ui.Button(
        label=copy.CONFIRM_CANCEL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "no", poll_id),
    )
    return panel(
        copy.POLL_CONFIRM_DELETE,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def _polls(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "polls", None)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class PollStartModal(SafeModal, title=copy.POLL_MODAL_TITLE):
    def __init__(self, *, kind: PollKind = PollKind.PUBLIC, allow_change: bool = True) -> None:
        super().__init__()
        self.kind = kind
        self.allow_change = allow_change
        self._question = ui.Label(
            text=copy.POLL_FIELD_QUESTION,
            component=ui.TextInput(max_length=200, required=True, style=discord.TextStyle.short),
        )
        self._options = ui.Label(
            text=copy.POLL_FIELD_OPTIONS,
            component=ui.TextInput(max_length=1000, required=True, style=discord.TextStyle.paragraph),
        )
        self._duration = ui.Label(
            text=copy.POLL_FIELD_DURATION,
            component=ui.TextInput(
                max_length=16,
                required=True,
                placeholder=copy.POLL_DURATION_PLACEHOLDER,
                style=discord.TextStyle.short,
            ),
        )
        self.add_item(self._question)
        self.add_item(self._options)
        self.add_item(self._duration)

    def spec(self) -> PollSpec | None:
        return parse_spec(
            question=_labeled_value(self._question),
            options=_labeled_value(self._options),
            duration=_labeled_value(self._duration),
            kind=self.kind,
            allow_change=self.allow_change,
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        spec = self.spec()
        if spec is None:
            await respond(interaction, denied(copy.POLL_BAD_SPEC))
            return
        polls = _polls(interaction)
        start = getattr(polls, "start", None)
        guild = interaction.guild
        if not callable(start) or guild is None:
            await respond(interaction, denied(copy.POLL_NO_CHANNEL))
            return
        created = await start(guild, interaction.channel, actor_id=interaction.user.id, spec=spec)
        if created is None:
            await respond(interaction, denied(copy.POLL_NO_CHANNEL))
            return
        body = copy.POLL_STARTED
        jump = getattr(created, "jump_url", None)
        if getattr(created, "redirected", False) and jump:
            body = copy.poll_started_link(str(jump))
        await respond(interaction, panel(copy.POLL_DONE_TITLE, body, tone=Tone.OK))


class PollMixin:
    kind = PANEL_KIND
    need: ClassVar[str] = "open"
    public: ClassVar[bool] = False
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        polls = _polls(self._interaction)
        getter = getattr(polls, "guard_resource", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref, need=type(self).need)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        return None


async def _ack_vote(interaction: discord.Interaction, result: object) -> None:
    verdict = getattr(result, "value", result)
    if verdict == "cast":
        await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_JOINED, tone=Tone.OK))
        return
    if verdict == "changed":
        await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_CHANGED, tone=Tone.OK))
        return
    if verdict == "already":
        await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_ALREADY, tone=Tone.OK))
        return
    if verdict == "locked":
        await respond(interaction, denied(copy.POLL_LOCKED))
        return
    if verdict == "bot":
        await respond(interaction, denied(copy.POLL_BOT))
        return
    await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class PollVoteButton(PollMixin, PersistentButton, template=r"^c:pl:v(?P<choice>\d):(?P<ref>\d+)$"):
    action = "v0"
    public = True
    access_key = None
    need = "open"

    def __init__(self, ref: int | str, *, item: ui.Button | None = None, choice: int = 0) -> None:
        self.choice = int(choice)
        super().__init__(ref, item=item, action=f"v{self.choice}")

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        polls = _polls(interaction)
        vote = getattr(polls, "vote", None)
        if not callable(vote) or interaction.guild is None:
            return
        result = await vote(interaction.guild, int(self.ref), interaction.user, self.choice)
        await _ack_vote(interaction, result)

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: Any,
        /,
    ) -> PollVoteButton:
        button = item if isinstance(item, ui.Button) else None
        return cls(match["ref"], item=button, choice=int(match["choice"]))


@register_dynamic_item
class PollPickSelect(PollMixin, PersistentSelect, template=r"^c:pl:pick:(?P<ref>\d+)$"):
    action = "pick"
    public = True
    access_key = None
    need = "open"
    mutation_scope = None

    def __init__(self, ref: int | str, *, item: ui.Select | None = None) -> None:
        if item is None:
            item = ui.Select(
                custom_id=format_custom_id(PANEL_KIND, "pick", str(ref)),
                placeholder=copy.POLL_PICK,
                min_values=1,
                max_values=1,
            )
        super().__init__(ref, item=item)

    async def on_action(
        self,
        interaction: discord.Interaction,
        verdict: GuardVerdict,
        values: object = (),
    ) -> None:
        polls = _polls(interaction)
        vote = getattr(polls, "vote", None)
        picked = tuple(values) if values else tuple(getattr(self.item, "values", None) or ())
        if not callable(vote) or interaction.guild is None or not picked:
            return
        try:
            choice = int(picked[0])
        except (TypeError, ValueError):
            await respond(interaction, empty(copy.STALE_BODY))
            return
        result = await vote(interaction.guild, int(self.ref), interaction.user, choice)
        await _ack_vote(interaction, result)


@register_dynamic_item
class PollCloseButton(PollMixin, PersistentButton, template=r"^c:pl:close:(?P<ref>\d+)$"):
    action = "close"
    access_key = POLL_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        polls = _polls(interaction)
        close = getattr(polls, "close", None)
        if not callable(close) or interaction.guild is None:
            return
        result = await close(interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True)
        if result == "ok":
            await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_CLOSED_OK, tone=Tone.OK))
            return
        if result == "denied":
            await respond(interaction, denied(copy.POLL_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class PollDeleteButton(PollMixin, PersistentButton, template=r"^c:pl:del:(?P<ref>\d+)$"):
    action = "del"
    access_key = POLL_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, confirm_delete_view(int(self.ref)))


@register_dynamic_item
class PollYesButton(PollMixin, PersistentButton, template=r"^c:pl:yes:(?P<ref>\d+)$"):
    action = "yes"
    access_key = POLL_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        polls = _polls(interaction)
        delete = getattr(polls, "delete", None)
        if not callable(delete) or interaction.guild is None:
            return
        result = await delete(interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True)
        if result == "ok":
            await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_DELETED_OK, tone=Tone.OK))
            return
        if result == "denied":
            await respond(interaction, denied(copy.POLL_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class PollNoButton(PollMixin, PersistentButton, template=r"^c:pl:no:(?P<ref>\d+)$"):
    action = "no"
    access_key = POLL_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, panel(copy.POLL_DONE_TITLE, copy.POLL_DELETE_ABORT, tone=Tone.OK))
