"""Панель сбора, модалка старта и кнопки. Без sqlalchemy и без services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import discord
from discord import ui

from conductor.domain.access import EVENT_MANAGE
from conductor.domain.gathering import (
    DEFAULT_SEATS,
    SEATS_UNLIMITED,
    GatheringCard,
    GatheringSpec,
    GatheringState,
    RsvpStatus,
    can_manage,
    checkin_at,
    checkin_open,
    parse_spec,
    seat_bar,
    seated_ids,
    status_ids,
    waitlist_ids,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.interact import instant
from conductor.presentation.layout import denied, edit_response, empty, kv, panel, respond, update_message
from conductor.presentation.mentions import going_mentions
from conductor.presentation.modal import SafeModal
from conductor.presentation.motion import Countdown
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.roster import RosterView, roster_preview
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "ev"

__all__ = [
    "PANEL_KIND",
    "GatheringStartModal",
    "cancelled_view",
    "confirm_cancel_view",
    "going_mentions",
    "list_view",
    "live_view",
    "ping_view",
]


def _counts(card: GatheringCard) -> tuple[int, int, int, int]:
    going = card.seated_n if card.seated_n is not None else len(seated_ids(card.entries))
    waiting = card.waiting_n if card.waiting_n is not None else len(waitlist_ids(card.entries))
    maybe = card.maybe_n if card.maybe_n is not None else len(status_ids(card.entries, RsvpStatus.MAYBE))
    cant = card.cant_n if card.cant_n is not None else len(status_ids(card.entries, RsvpStatus.CANT))
    return going, waiting, maybe, cant


def _footnote(card: GatheringCard, *, now: Any = None) -> str:
    host = copy.event_host_line(card.host_id)
    opens = checkin_at(card.starts_at)
    if now is not None and checkin_open(card.starts_at, now):
        return f"{host} · {copy.EVENT_CHECKIN_OPEN}"
    return f"{host} · {copy.EVENT_CHECKIN_WAIT} {Countdown().render(opens)}"


def _fields(card: GatheringCard) -> dict[str, object]:
    going_ids = seated_ids(card.entries)
    waiting_ids = waitlist_ids(card.entries)
    going, waiting, _maybe, _cant = _counts(card)
    when = f"{discord.utils.format_dt(card.starts_at, 'F')} ({Countdown().render(card.starts_at)})"
    bar = "" if int(card.seats) == SEATS_UNLIMITED else seat_bar(going, card.seats)
    seats = copy.event_seats_line(going, card.seats, waiting, bar)
    fields: dict[str, object] = {
        copy.EVENT_WHEN: when,
        copy.EVENT_SEATS: seats,
    }
    preview = roster_preview(going_ids, going)
    if preview:
        fields[copy.EVENT_GOING] = preview
    if waiting and int(card.seats) != SEATS_UNLIMITED:
        wait_preview = roster_preview(waiting_ids, waiting)
        if wait_preview:
            fields[copy.EVENT_WAITLIST_HEAD] = wait_preview
    return fields


def live_view(card: GatheringCard, *, now: Any = None) -> ui.LayoutView:
    go = ui.Button(
        label=copy.EVENT_GOING,
        style=discord.ButtonStyle.primary,
        custom_id=format_custom_id(PANEL_KIND, "go", card.gathering_id),
    )
    maybe = ui.Button(
        label=copy.EVENT_MAYBE,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "maybe", card.gathering_id),
    )
    cant = ui.Button(
        label=copy.EVENT_CANT,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "cant", card.gathering_id),
    )
    roster = ui.Button(
        label=copy.ROSTER_ALL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "list", card.gathering_id),
    )
    ping = ui.Button(
        label=copy.EVENT_PING,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "ping", card.gathering_id),
    )
    voice = ui.Button(
        label=copy.EVENT_VOICE,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "voice", card.gathering_id),
    )
    stop = ui.Button(
        label=copy.EVENT_CANCEL,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "stop", card.gathering_id),
    )
    first: list[ui.Button] = [go, maybe, cant]
    if now is not None and checkin_open(card.starts_at, now):
        first.append(
            ui.Button(
                label=copy.EVENT_HERE,
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, "here", card.gathering_id),
            )
        )
    first.append(roster)
    going, _waiting, maybe_n, cant_n = _counts(card)
    return panel(
        card.title,
        (kv(_fields(card)), copy.event_counts_line(going=going, maybe=maybe_n, cant=cant_n)),
        footnote=_footnote(card, now=now),
        rows=(ui.ActionRow(*first), ui.ActionRow(ping, voice, stop)),
        tone=Tone.SPECIAL,
        icon=Icon.SHIFT,
        timeout=None,
    )


def cancelled_view(card: GatheringCard) -> ui.LayoutView:
    return panel(
        f"{card.title} — {copy.EVENT_OVER}",
        fields=_fields(card),
        footnote=copy.event_host_line(card.host_id),
        tone=Tone.MUTED,
        timeout=None,
        icon=Icon.LOCK,
    )


def confirm_cancel_view(gathering_id: int) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.EVENT_CANCEL,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "yes", gathering_id),
    )
    no = ui.Button(
        label=copy.CONFIRM_CANCEL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "no", gathering_id),
    )
    return panel(
        copy.EVENT_CONFIRM_CANCEL,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def list_view(card: GatheringCard, *, guild: discord.Guild | None = None, download: bool = False) -> ui.LayoutView:
    going = list(seated_ids(card.entries))
    waiting = list(waitlist_ids(card.entries))
    if int(card.seats) == SEATS_UNLIMITED:
        waiting = []
    if not going and not waiting:
        return empty(copy.EVENT_PING_EMPTY)
    if going and not waiting:
        return RosterView(copy.EVENT_GOING, going, guild=guild, download=download)
    if waiting and not going:
        return RosterView(copy.EVENT_WAITLIST_HEAD, waiting, guild=guild, download=download)
    return _list_choice_view(going, waiting, guild=guild, download=download)


def _list_choice_view(
    going: list[int],
    waiting: list[int],
    *,
    guild: discord.Guild | None,
    download: bool,
) -> ui.LayoutView:
    go_btn = ui.Button(label=copy.EVENT_GOING, style=discord.ButtonStyle.primary)
    wait_btn = ui.Button(label=copy.EVENT_WAITLIST_HEAD, style=discord.ButtonStyle.secondary)

    async def show_going(interaction: discord.Interaction) -> None:
        await update_message(interaction, RosterView(copy.EVENT_GOING, going, guild=guild, download=download))

    async def show_waiting(interaction: discord.Interaction) -> None:
        await update_message(
            interaction, RosterView(copy.EVENT_WAITLIST_HEAD, waiting, guild=guild, download=download)
        )

    go_btn.callback = instant(show_going)
    wait_btn.callback = instant(show_waiting)
    return panel(
        copy.EVENT_LIST,
        copy.EVENT_LIST_PICK,
        rows=(ui.ActionRow(go_btn, wait_btn),),
        tone=Tone.SPECIAL,
        icon=Icon.SHIFT,
        timeout=600.0,
    )


def ping_view(card: GatheringCard) -> ui.LayoutView:
    return panel(
        copy.EVENT_PING_TITLE,
        copy.event_ping_header(card.title),
        footnote=copy.event_host_line(card.host_id),
        tone=Tone.SPECIAL,
        icon=Icon.SHIFT,
        timeout=None,
    )


def _gatherings(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "gatherings", None)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class GatheringStartModal(SafeModal, title=copy.EVENT_MODAL_TITLE):
    def __init__(self, *, seats: int = DEFAULT_SEATS) -> None:
        super().__init__()
        self.seats = seats
        self._title = ui.Label(
            text=copy.EVENT_FIELD_TITLE,
            component=ui.TextInput(max_length=200, required=True, style=discord.TextStyle.short),
        )
        self._when = ui.Label(
            text=copy.EVENT_FIELD_WHEN,
            component=ui.TextInput(
                max_length=32,
                required=True,
                placeholder=copy.EVENT_WHEN_PLACEHOLDER,
                style=discord.TextStyle.short,
            ),
        )
        self._seats = ui.Label(
            text=copy.EVENT_FIELD_SEATS,
            component=ui.TextInput(
                max_length=5,
                required=False,
                default=str(int(seats)),
                placeholder="0",
                style=discord.TextStyle.short,
            ),
        )
        self.add_item(self._title)
        self.add_item(self._when)
        self.add_item(self._seats)

    def spec(self, *, now: Any) -> GatheringSpec | None:
        raw_seats = _labeled_value(self._seats).strip()
        return parse_spec(
            title=_labeled_value(self._title),
            when=_labeled_value(self._when),
            seats=raw_seats if raw_seats else self.seats,
            now=now,
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        spec = self.spec(now=datetime.now(UTC))
        if spec is None:
            await respond(interaction, denied(copy.EVENT_BAD_SPEC))
            return
        gatherings = _gatherings(interaction)
        start = getattr(gatherings, "start", None)
        guild = interaction.guild
        channel = interaction.channel
        send = getattr(channel, "send", None)
        if not callable(start) or guild is None or not callable(send):
            await respond(interaction, denied(copy.EVENT_NO_CHANNEL))
            return
        created = await start(guild, channel, actor_id=interaction.user.id, spec=spec)
        if created is None:
            await respond(interaction, denied(copy.EVENT_NO_CHANNEL))
            return
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_STARTED, tone=Tone.OK))


class GatheringMixin:
    kind = PANEL_KIND
    need: ClassVar[str] = "open"
    public: ClassVar[bool] = False
    access_key: ClassVar[str | None] = None
    mutation_scope: ClassVar[Any] = None
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        gatherings = _gatherings(self._interaction)
        getter = getattr(gatherings, "guard_resource", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref, need=type(self).need)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        return None


async def _ack_rsvp(interaction: discord.Interaction, result: object) -> None:
    verdict = getattr(result, "value", result)
    if verdict == "seated":
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_SEATED, tone=Tone.OK))
        return
    if verdict == "waitlist":
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_WAITLISTED, tone=Tone.WARN))
        return
    if verdict == "set":
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_SET, tone=Tone.OK))
        return
    if verdict == "already":
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_ALREADY, tone=Tone.OK))
        return
    if verdict == "checked":
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_CHECKED, tone=Tone.OK))
        return
    if verdict == "early":
        await respond(interaction, denied(copy.EVENT_EARLY))
        return
    if verdict == "not_going":
        await respond(interaction, denied(copy.EVENT_NOT_GOING))
        return
    if verdict == "bot":
        await respond(interaction, denied(copy.EVENT_BOT))
        return
    await respond(interaction, empty(copy.STALE_BODY))


class _RsvpButton(GatheringMixin):
    public = True
    access_key = None
    need = "open"
    status: ClassVar[RsvpStatus] = RsvpStatus.GOING

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        rsvp = getattr(gatherings, "rsvp", None)
        if not callable(rsvp) or interaction.guild is None:
            return
        result = await rsvp(interaction.guild, int(self.ref), interaction.user, type(self).status)
        await _ack_rsvp(interaction, result)


@register_dynamic_item
class GatheringGoButton(_RsvpButton, PersistentButton, template=r"^c:ev:go:(?P<ref>\d+)$"):
    action = "go"
    status = RsvpStatus.GOING


@register_dynamic_item
class GatheringMaybeButton(_RsvpButton, PersistentButton, template=r"^c:ev:maybe:(?P<ref>\d+)$"):
    action = "maybe"
    status = RsvpStatus.MAYBE


@register_dynamic_item
class GatheringCantButton(_RsvpButton, PersistentButton, template=r"^c:ev:cant:(?P<ref>\d+)$"):
    action = "cant"
    status = RsvpStatus.CANT


@register_dynamic_item
class GatheringHereButton(GatheringMixin, PersistentButton, template=r"^c:ev:here:(?P<ref>\d+)$"):
    action = "here"
    public = True
    access_key = None
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        checkin = getattr(gatherings, "checkin", None)
        if not callable(checkin) or interaction.guild is None:
            return
        result = await checkin(interaction.guild, int(self.ref), interaction.user)
        await _ack_rsvp(interaction, result)


@register_dynamic_item
class GatheringListButton(GatheringMixin, PersistentButton, template=r"^c:ev:list:(?P<ref>\d+)$"):
    action = "list"
    public = True
    access_key = None
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        load = getattr(gatherings, "card", None)
        if not callable(load) or interaction.guild is None:
            return
        card = await load(interaction.guild.id, int(self.ref))
        if card is None or card.state is GatheringState.CANCELLED:
            await respond(interaction, empty(copy.STALE_BODY))
            return
        download = can_manage(actor_id=interaction.user.id, host_id=card.host_id, has_manage=False)
        await respond(interaction, list_view(card, guild=interaction.guild, download=download))


@register_dynamic_item
class GatheringPingButton(GatheringMixin, PersistentButton, template=r"^c:ev:ping:(?P<ref>\d+)$"):
    action = "ping"
    access_key = EVENT_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        ping = getattr(gatherings, "ping", None)
        if not callable(ping) or interaction.guild is None:
            return

        async def progress(index: int, total: int) -> None:
            await edit_response(
                interaction,
                panel(copy.EVENT_DONE_TITLE, copy.event_ping_progress(index, total), tone=Tone.OK),
            )

        result = await ping(
            interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True, progress=progress
        )
        if result == "ok":
            await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_PING_OK, tone=Tone.OK))
            return
        if result == "empty":
            await respond(interaction, empty(copy.EVENT_PING_EMPTY))
            return
        if result == "cooldown":
            await respond(interaction, denied(copy.EVENT_PING_WAIT))
            return
        if result == "denied":
            await respond(interaction, denied(copy.EVENT_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GatheringVoiceButton(GatheringMixin, PersistentButton, template=r"^c:ev:voice:(?P<ref>\d+)$"):
    action = "voice"
    access_key = EVENT_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        open_voice = getattr(gatherings, "open_voice", None)
        if not callable(open_voice) or interaction.guild is None:
            return
        result, channel_id = await open_voice(
            interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True
        )
        if result == "ok":
            body = copy.EVENT_VOICE_OK if channel_id is None else f"{copy.EVENT_VOICE_OK} <#{channel_id}>"
            await respond(interaction, panel(copy.EVENT_DONE_TITLE, body, tone=Tone.OK))
            return
        if result == "already":
            body = copy.EVENT_VOICE_EXISTS if channel_id is None else f"{copy.EVENT_VOICE_EXISTS} <#{channel_id}>"
            await respond(interaction, panel(copy.EVENT_DONE_TITLE, body, tone=Tone.OK))
            return
        if result == "no_hub":
            await respond(interaction, denied(copy.EVENT_NO_HUB))
            return
        if result == "denied":
            await respond(interaction, denied(copy.EVENT_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GatheringStopButton(GatheringMixin, PersistentButton, template=r"^c:ev:stop:(?P<ref>\d+)$"):
    action = "stop"
    access_key = EVENT_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, confirm_cancel_view(int(self.ref)))


@register_dynamic_item
class GatheringYesButton(GatheringMixin, PersistentButton, template=r"^c:ev:yes:(?P<ref>\d+)$"):
    action = "yes"
    access_key = EVENT_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        gatherings = _gatherings(interaction)
        cancel = getattr(gatherings, "cancel", None)
        if not callable(cancel) or interaction.guild is None:
            return
        result = await cancel(interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True)
        if result == "ok":
            await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_CANCEL_OK, tone=Tone.OK))
            return
        if result == "denied":
            await respond(interaction, denied(copy.EVENT_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GatheringNoButton(GatheringMixin, PersistentButton, template=r"^c:ev:no:(?P<ref>\d+)$"):
    action = "no"
    access_key = EVENT_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, panel(copy.EVENT_DONE_TITLE, copy.EVENT_CANCEL_ABORT, tone=Tone.OK))
