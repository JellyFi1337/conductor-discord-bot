"""Постоянная панель владельца временной комнаты. Без sqlalchemy и без services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord
from discord import ui

from conductor.domain.access import VOICE_HUB_MANAGE, VOICE_STAFF_OVERRIDE
from conductor.domain.mutations import MutationScope
from conductor.domain.renames import Deferred
from conductor.domain.rooms import CHANNEL_NAME_LIMIT, ClaimVerdict, JoinAsk, MergeAsk, NameReject
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, run_guard
from conductor.presentation.icons import button_emoji
from conductor.presentation.layout import denied, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import (
    PersistentButton,
    PersistentSelect,
    format_custom_id,
    register_dynamic_item,
)
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "vc"

__all__ = [
    "PANEL_KIND",
    "RoomCard",
    "VoiceHubFixButton",
    "VoiceLockButton",
    "claim_denied_text",
    "owner_panel_view",
    "voice_hub_denial_card",
]


@dataclass(frozen=True, slots=True)
class RoomCard:
    room_id: int
    name: str
    owner_id: int
    humans: int
    limit: int
    locked: bool
    ghosted: bool = False
    requests: int = 0
    lfg_line: str | None = None
    merge_id: int = 0


def owner_panel_view(card: RoomCard) -> ui.LayoutView:
    ref = card.room_id
    lock_label = copy.VOICE_UNLOCK if card.locked else copy.VOICE_LOCK
    ghost_label = copy.VOICE_UNGHOST if card.ghosted else copy.VOICE_GHOST
    rows = (
        ui.ActionRow(
            ui.Button(
                label=copy.VOICE_NAME,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "name", ref),
            ),
            ui.Button(
                label=copy.VOICE_LIMIT,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "limit", ref),
            ),
            ui.Button(
                label=lock_label,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "lock", ref),
                emoji=button_emoji("vc_open" if card.locked else "vc_lock"),
            ),
            ui.Button(
                label=ghost_label,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "hide", ref),
                emoji=button_emoji("vc_show" if card.ghosted else "vc_ghost"),
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "let", ref),
                placeholder=copy.VOICE_PERMIT,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "den", ref),
                placeholder=copy.VOICE_REJECT,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "xfer", ref),
                placeholder=copy.VOICE_TRANSFER,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "kick", ref),
                placeholder=copy.VOICE_KICK,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, "ban", ref),
                placeholder=copy.VOICE_BAN,
                min_values=1,
                max_values=1,
            ),
        ),
        ui.ActionRow(
            ui.Button(
                label=copy.voice_requests_label(card.requests),
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, "reqs", ref),
            ),
            ui.Button(
                label=copy.VOICE_INVITE,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "inv", ref),
            ),
            ui.Button(
                label=copy.VOICE_CLAIM,
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, "claim", ref),
            ),
            ui.Button(
                label=copy.VOICE_TAKE,
                style=discord.ButtonStyle.danger,
                custom_id=format_custom_id(PANEL_KIND, "take", ref),
            ),
        ),
        ui.ActionRow(
            ui.Button(
                label=copy.VOICE_BITRATE,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "rate", ref),
            ),
            ui.Button(
                label=copy.VOICE_STATUS,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(PANEL_KIND, "stat", ref),
            ),
            ui.Button(
                label=copy.VOICE_ASK,
                style=discord.ButtonStyle.success,
                custom_id=format_custom_id(PANEL_KIND, "ask", ref),
            ),
        ),
        ui.ActionRow(
            ui.ChannelSelect(
                custom_id=format_custom_id(PANEL_KIND, "mrg", ref),
                placeholder=copy.VOICE_MERGE,
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.voice],
            ),
        ),
    )
    if card.merge_id:
        rows = (
            *rows,
            ui.ActionRow(
                ui.Button(
                    label=copy.VOICE_MERGE_ACCEPT,
                    style=discord.ButtonStyle.success,
                    custom_id=format_custom_id(PANEL_KIND, "okm", card.merge_id),
                ),
            ),
        )
    fields: dict[str, object] = {
        copy.VOICE_FIELD_OWNER: f"<@{card.owner_id}>",
        copy.VOICE_FIELD_INSIDE: copy.voice_inside(card.humans, card.limit),
        copy.VOICE_FIELD_LOCK: copy.voice_lock_state(card.locked),
    }
    if card.ghosted:
        fields[copy.VOICE_FIELD_GHOST] = copy.voice_ghost_state(True)
    if card.lfg_line:
        fields[copy.VOICE_FIELD_LFG] = card.lfg_line
    footnote = copy.VOICE_PANEL_FOOTNOTE
    return panel(
        copy.voice_room_title(card.name),
        fields=fields,
        footnote=footnote,
        rows=rows,
        timeout=None,
    )


def _rooms(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "rooms", None)


def _voice_channel_id(user: object) -> int | None:
    voice = getattr(user, "voice", None)
    channel = getattr(voice, "channel", None)
    return None if channel is None else getattr(channel, "id", None)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


def _name_denied(reason: str) -> str:
    if reason == NameReject.INVITE:
        return copy.VOICE_NAME_INVITE
    if reason == NameReject.AD:
        return copy.VOICE_NAME_AD
    return copy.VOICE_NAME_EMPTY


def claim_denied_text(verdict: ClaimVerdict) -> str:
    if verdict is ClaimVerdict.ALREADY_OWNER:
        return copy.VOICE_CLAIM_OWNER
    if verdict is ClaimVerdict.OWNER_HERE:
        return copy.VOICE_CLAIM_HERE
    if verdict is ClaimVerdict.HAS_ROOM:
        return copy.VOICE_CLAIM_HAS_ROOM
    if verdict is ClaimVerdict.NOT_IN_ROOM:
        return copy.VOICE_TRANSFER_MISSING
    return copy.VOICE_CLAIM_TOO_SOON


async def _load_room(rooms: object, *, guild_id: int, ref: str) -> GuardResource | None:
    getter = getattr(rooms, "guard_resource", None)
    if not callable(getter):
        return None
    return await getter(guild_id, ref)


class VoiceControlMixin:
    kind = PANEL_KIND
    access_key = VOICE_STAFF_OVERRIDE
    mutation_scope: MutationScope | None = None
    checks_hierarchy = False
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_room(_rooms(self._interaction), guild_id=guild_id, ref=ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None


@register_dynamic_item
class VoiceNameButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:name:(?P<ref>\d+)$"):
    action = "name"
    mutation_scope = MutationScope.CHANNELS_VOICE
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(NameModal(self.ref))


@register_dynamic_item
class VoiceLimitButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:limit:(?P<ref>\d+)$"):
    action = "limit"
    mutation_scope = MutationScope.CHANNELS_VOICE
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(LimitModal(self.ref))


@register_dynamic_item
class VoiceLockButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:lock:(?P<ref>\d+)$"):
    action = "lock"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        toggle = getattr(rooms, "toggle_lock", None)
        if not callable(toggle):
            return
        locked = await toggle(interaction.guild, self.ref)
        if locked is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        body = copy.VOICE_LOCKED if locked else copy.VOICE_OPENED
        await respond(interaction, panel(copy.VOICE_TITLE, body, tone=Tone.OK))


@register_dynamic_item
class VoiceClaimButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:claim:(?P<ref>\d+)$"):
    action = "claim"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        resource = await _load_room(_rooms(self._interaction), guild_id=guild_id, ref=ref)
        if resource is None:
            return None
        voice_id = _voice_channel_id(self._interaction.user)
        if voice_id is not None and voice_id == resource.channel_id:
            return GuardResource(
                ref=resource.ref,
                guild_id=resource.guild_id,
                owner_id=self._interaction.user.id,
                channel_id=resource.channel_id,
            )
        return resource

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        claim = getattr(rooms, "claim_room", None)
        if not callable(claim):
            return
        occupant = _voice_channel_id(interaction.user)
        resource = verdict.resource
        in_room = resource is not None and occupant == resource.channel_id
        result = await claim(
            interaction.guild,
            self.ref,
            interaction.user,
            now=datetime.now(UTC),
            occupant=in_room,
            staff=False,
        )
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_CLAIM_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


@register_dynamic_item
class VoiceTakeButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:take:(?P<ref>\d+)$"):
    action = "take"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        take = getattr(rooms, "take_as_staff", None)
        if not callable(take):
            return
        result = await take(interaction.guild, self.ref, interaction.user)
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_TAKE_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


class VoiceSelect(PersistentSelect, template=re.compile(r"^c:vc:noop:(?P<ref>\d+)$")):
    kind = PANEL_KIND
    action: str = "noop"
    mutation_scope = MutationScope.CHANNELS_VOICE
    access_key = VOICE_STAFF_OVERRIDE

    def __init__(self, ref: int | str, *, item: ui.UserSelect | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.UserSelect(
                custom_id=format_custom_id(PANEL_KIND, type(self).action, self.ref),
                placeholder=copy.VOICE_TRANSFER,
                min_values=1,
                max_values=1,
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_room(_rooms(self._interaction), guild_id=guild_id, ref=ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> VoiceSelect:
        select = item if isinstance(item, ui.UserSelect) else None
        return cls(match["ref"], item=select)


@register_dynamic_item
class VoiceTransferSelect(VoiceSelect, template=r"^c:vc:xfer:(?P<ref>\d+)$"):
    action = "xfer"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        transfer = getattr(rooms, "transfer_room", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(transfer) or target is None:
            return
        result = await transfer(interaction.guild, self.ref, target)
        if result is ClaimVerdict.OK:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_TRANSFER_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(claim_denied_text(result)))


@register_dynamic_item
class VoiceKickSelect(VoiceSelect, template=r"^c:vc:kick:(?P<ref>\d+)$"):
    action = "kick"
    mutation_scope = MutationScope.MEMBERS_MOVE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        kick = getattr(rooms, "kick_member", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(kick) or target is None:
            return
        error = await kick(interaction.guild, self.ref, target)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_KICK_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


@register_dynamic_item
class VoiceBanSelect(VoiceSelect, template=r"^c:vc:ban:(?P<ref>\d+)$"):
    action = "ban"
    mutation_scope = MutationScope.MEMBERS_MOVE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        block = getattr(rooms, "block_member", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(block) or target is None:
            return
        notice = await block(interaction.guild, self.ref, target)
        if notice is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_BAN_OK, tone=Tone.OK))
            return
        if notice == copy.VOICE_BAN_ADMIN:
            await respond(interaction, panel(copy.VOICE_TITLE, notice, tone=Tone.WARN))
            return
        await respond(interaction, denied(notice))


@register_dynamic_item
class VoicePermitSelect(VoiceSelect, template=r"^c:vc:let:(?P<ref>\d+)$"):
    action = "let"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        permit = getattr(rooms, "permit_member", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(permit) or target is None:
            return
        error = await permit(interaction.guild, self.ref, target)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_PERMITTED, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


@register_dynamic_item
class VoiceRejectSelect(VoiceSelect, template=r"^c:vc:den:(?P<ref>\d+)$"):
    action = "den"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        reject = getattr(rooms, "reject_member", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(reject) or target is None:
            return
        error = await reject(interaction.guild, self.ref, target)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_REJECTED, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


@register_dynamic_item
class VoiceGhostButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:hide:(?P<ref>\d+)$"):
    action = "hide"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        toggle = getattr(rooms, "toggle_ghost", None)
        if not callable(toggle):
            return
        ghosted = await toggle(interaction.guild, self.ref)
        if ghosted is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        body = copy.VOICE_GHOSTED if ghosted else copy.VOICE_VISIBLE
        await respond(interaction, panel(copy.VOICE_TITLE, body, tone=Tone.OK))


@register_dynamic_item
class VoiceInviteButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:inv:(?P<ref>\d+)$"):
    action = "inv"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        make = getattr(rooms, "one_time_invite", None)
        if not callable(make):
            return
        url = await make(interaction.guild, self.ref)
        if not url:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        await respond(interaction, panel(copy.VOICE_INVITE_OK, url, tone=Tone.OK))


@register_dynamic_item
class VoiceAskButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:ask:(?P<ref>\d+)$"):
    action = "ask"
    mutation_scope = None
    access_key = None

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        resource = await _load_room(_rooms(self._interaction), guild_id=guild_id, ref=ref)
        if resource is None:
            return None
        return GuardResource(
            ref=resource.ref,
            guild_id=resource.guild_id,
            owner_id=self._interaction.user.id,
            channel_id=resource.channel_id,
        )

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        ask = getattr(rooms, "ask_join", None)
        if not callable(ask):
            return
        result = await ask(interaction.guild, self.ref, interaction.user)
        if result is JoinAsk.OK:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_ASK_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(_ask_denied(result)))


@register_dynamic_item
class VoiceRequestsButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:reqs:(?P<ref>\d+)$"):
    action = "reqs"
    mutation_scope = None

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        listing = getattr(rooms, "list_requests", None)
        if not callable(listing):
            return
        items = await listing(interaction.guild, self.ref)
        await respond(interaction, requests_view(self.ref, items))


@register_dynamic_item
class VoiceAcceptButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:yes:(?P<ref>\d+)$"):
    action = "yes"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        getter = getattr(_rooms(self._interaction), "guard_request", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        resolve = getattr(rooms, "resolve_request", None)
        if not callable(resolve):
            return
        error = await resolve(interaction.guild, self.ref, allow=True)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_PERMITTED, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


@register_dynamic_item
class VoiceDenyButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:no:(?P<ref>\d+)$"):
    action = "no"
    mutation_scope = MutationScope.CHANNELS_VOICE

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        getter = getattr(_rooms(self._interaction), "guard_request", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        resolve = getattr(rooms, "resolve_request", None)
        if not callable(resolve):
            return
        error = await resolve(interaction.guild, self.ref, allow=False)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_REJECTED, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


def _ask_denied(result: JoinAsk) -> str:
    mapping = {
        JoinAsk.OPEN: copy.VOICE_ASK_OPEN,
        JoinAsk.PENDING: copy.VOICE_ASK_PENDING,
        JoinAsk.KICKED: copy.VOICE_ASK_KICKED,
        JoinAsk.OWNER: copy.VOICE_ASK_OWNER,
        JoinAsk.INSIDE: copy.VOICE_ASK_INSIDE,
    }
    return mapping.get(result, copy.VOICE_ASK_PENDING)


def requests_view(_room_ref: str, items: list[tuple[int, int]]) -> ui.LayoutView:
    if not items:
        return panel(copy.VOICE_REQUESTS_TITLE, copy.VOICE_REQUESTS_EMPTY, tone=Tone.MUTED)
    rows: list[ui.ActionRow] = []
    lines: list[str] = []
    for request_id, user_id in items[:4]:
        lines.append(f"<@{user_id}>")
        rows.append(
            ui.ActionRow(
                ui.Button(
                    label=copy.VOICE_ACCEPT,
                    style=discord.ButtonStyle.success,
                    custom_id=format_custom_id(PANEL_KIND, "yes", request_id),
                ),
                ui.Button(
                    label=copy.VOICE_DECLINE,
                    style=discord.ButtonStyle.danger,
                    custom_id=format_custom_id(PANEL_KIND, "no", request_id),
                ),
            )
        )
    extra = len(items) - 4
    footnote = copy.VOICE_PANEL_FOOTNOTE if extra <= 0 else f"ещё {extra}"
    return panel(copy.VOICE_REQUESTS_TITLE, lines, footnote=footnote, rows=tuple(rows), timeout=None)


class NameModal(SafeModal, title=copy.VOICE_NAME_MODAL):
    name = ui.Label(text=copy.VOICE_NAME_LABEL, component=ui.TextInput(max_length=CHANNEL_NAME_LIMIT))

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        rooms = _rooms(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_room(rooms, guild_id=guild_id, ref=ref)

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=VOICE_STAFF_OVERRIDE,
            scope=MutationScope.CHANNELS_VOICE,
        )
        if not verdict.ok:
            return
        rename = getattr(rooms, "rename_room", None)
        if not callable(rename):
            return
        result = await rename(interaction.guild, self.ref, _labeled_value(self.name))
        if isinstance(result, Deferred):
            await respond(interaction, panel(copy.VOICE_TITLE, copy.rename_deferred(result.until), tone=Tone.WARN))
            return
        if isinstance(result, str):
            await respond(interaction, denied(_name_denied(result)))
            return
        await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_RENAMED, tone=Tone.OK))


class LimitModal(SafeModal, title=copy.VOICE_LIMIT_MODAL):
    limit = ui.Label(text=copy.VOICE_LIMIT_LABEL, component=ui.TextInput(max_length=2, placeholder="0"))

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        rooms = _rooms(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_room(rooms, guild_id=guild_id, ref=ref)

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=VOICE_STAFF_OVERRIDE,
            scope=MutationScope.CHANNELS_VOICE,
        )
        if not verdict.ok:
            return
        setter = getattr(rooms, "set_user_limit", None)
        if not callable(setter):
            return
        ok = await setter(interaction.guild, self.ref, _labeled_value(self.limit))
        if not ok:
            await respond(interaction, denied(copy.VOICE_LIMIT_BAD))
            return
        await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_LIMIT_SET, tone=Tone.OK))


@register_dynamic_item
class VoiceBitrateButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:rate:(?P<ref>\d+)$"):
    action = "rate"
    mutation_scope = MutationScope.CHANNELS_VOICE
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(BitrateModal(self.ref))


@register_dynamic_item
class VoiceStatusButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:stat:(?P<ref>\d+)$"):
    action = "stat"
    mutation_scope = MutationScope.CHANNELS_VOICE
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        await interaction.response.send_modal(StatusModal(self.ref))


class VoiceChannelSelect(PersistentSelect, template=re.compile(r"^c:vc:chan:(?P<ref>\d+)$")):
    kind = PANEL_KIND
    action: str = "chan"
    mutation_scope = MutationScope.MEMBERS_MOVE
    access_key = VOICE_STAFF_OVERRIDE

    def __init__(self, ref: int | str, *, item: ui.ChannelSelect | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.ChannelSelect(
                custom_id=format_custom_id(PANEL_KIND, type(self).action, self.ref),
                placeholder=copy.VOICE_MERGE,
                min_values=1,
                max_values=1,
                channel_types=[discord.ChannelType.voice],
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _load_room(_rooms(self._interaction), guild_id=guild_id, ref=ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        return None

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> VoiceChannelSelect:
        select = item if isinstance(item, ui.ChannelSelect) else None
        return cls(match["ref"], item=select)


@register_dynamic_item
class VoiceMergeSelect(VoiceChannelSelect, template=r"^c:vc:mrg:(?P<ref>\d+)$"):
    action = "mrg"
    mutation_scope = MutationScope.MEMBERS_MOVE

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        request = getattr(rooms, "request_merge", None)
        values = getattr(self.item, "values", None) or ()
        target = values[0] if values else None
        if not callable(request) or target is None:
            return
        result = await request(interaction.guild, self.ref, target)
        if result is MergeAsk.OK:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_MERGE_OK, tone=Tone.OK))
            return
        await respond(interaction, denied(_merge_denied(result)))


@register_dynamic_item
class VoiceMergeAcceptButton(VoiceControlMixin, PersistentButton, template=r"^c:vc:okm:(?P<ref>\d+)$"):
    action = "okm"
    mutation_scope = MutationScope.MEMBERS_MOVE

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        getter = getattr(_rooms(self._interaction), "guard_merge", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: object, values: object = ()) -> None:
        rooms = _rooms(interaction)
        accept = getattr(rooms, "accept_merge", None)
        if not callable(accept):
            return
        error = await accept(interaction.guild, self.ref)
        if error is None:
            await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_MERGE_DONE, tone=Tone.OK))
            return
        await respond(interaction, denied(error))


def _merge_denied(result: MergeAsk) -> str:
    mapping = {
        MergeAsk.SELF: copy.VOICE_MERGE_SELF,
        MergeAsk.MISSING: copy.VOICE_MERGE_MISSING,
        MergeAsk.PENDING: copy.VOICE_MERGE_PENDING,
    }
    return mapping.get(result, copy.VOICE_MERGE_MISSING)


class BitrateModal(SafeModal, title=copy.VOICE_BITRATE_MODAL):
    bitrate = ui.Label(text=copy.VOICE_BITRATE_LABEL, component=ui.TextInput(max_length=3, placeholder="96"))

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        rooms = _rooms(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_room(rooms, guild_id=guild_id, ref=ref)

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=VOICE_STAFF_OVERRIDE,
            scope=MutationScope.CHANNELS_VOICE,
        )
        if not verdict.ok:
            return
        setter = getattr(rooms, "set_bitrate", None)
        if not callable(setter):
            return
        ok = await setter(interaction.guild, self.ref, _labeled_value(self.bitrate))
        if not ok:
            await respond(interaction, denied(copy.VOICE_BITRATE_BAD))
            return
        await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_BITRATE_OK, tone=Tone.OK))


class StatusModal(SafeModal, title=copy.VOICE_STATUS_MODAL):
    status = ui.Label(text=copy.VOICE_STATUS_LABEL, component=ui.TextInput(max_length=500, required=False))

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref

    async def submit(self, interaction: discord.Interaction) -> None:
        rooms = _rooms(interaction)

        async def load(*, guild_id: int, ref: str) -> GuardResource | None:
            return await _load_room(rooms, guild_id=guild_id, ref=ref)

        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=load,
            key=VOICE_STAFF_OVERRIDE,
            scope=MutationScope.CHANNELS_VOICE,
        )
        if not verdict.ok:
            return
        setter = getattr(rooms, "set_status", None)
        if not callable(setter):
            return
        result = await setter(interaction.guild, self.ref, _labeled_value(self.status))
        if result is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        if result is False:
            await respond(interaction, denied(copy.VOICE_STATUS_BAD))
            return
        await respond(interaction, panel(copy.VOICE_TITLE, copy.VOICE_STATUS_OK, tone=Tone.OK))


def voice_hub_denial_card(code: str) -> ui.LayoutView:
    button = VoiceHubFixButton("hub", label=copy.DIAGNOSE_REPAIR)
    return panel(
        copy.DIAGNOSE_FEATURE_VOICE,
        copy.voice_denial_text(code),
        tone=Tone.WARN,
        icon=Icon.WARN,
        rows=(ui.ActionRow(button),),
        timeout=None,
    )


@register_dynamic_item
class VoiceHubFixButton(PersistentButton, template=r"^c:vh:fix:(?P<ref>\d+|[a-z0-9_]{1,32})$"):
    kind = "vh"
    action = "fix"
    access_key = VOICE_HUB_MANAGE

    def __init__(self, ref: int | str, *, item: ui.Button | None = None, label: str | None = None) -> None:
        super().__init__(
            ref,
            item=item,
            kind=self.kind,
            action=self.action,
            label=label or copy.ADMIN_VOICE_FIX,
            style=discord.ButtonStyle.primary,
        )

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        # Ресурс кнопки — хаб этой гильдии; без него run_guard отвечает «устарело».
        if int(guild_id) <= 0 or ref != "hub":
            return None
        return GuardResource(ref=ref, guild_id=int(guild_id))

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        del verdict
        guild = interaction.guild
        repair = getattr(getattr(interaction.client, "rooms", None), "repair_hub", None)
        if guild is None or not callable(repair):
            return
        denial, _details = await repair(guild, int(interaction.user.id))
        if denial is None:
            body, tone = copy.VOICE_HUB_FIXED, Tone.OK
        else:
            body, tone = copy.voice_denial_text(str(getattr(denial, "value", denial))), Tone.WARN
        await respond(interaction, panel(copy.ADMIN_VOICE_FIX, body, tone=tone))
