"""Карточки и кнопки набора в #поиск-команды. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import discord
import structlog
from discord import ui

from conductor.domain.discord_limits import VOICE_USER_LIMIT_MAX
from conductor.domain.lfg import NEED_UNLIMITED, NOTE_LIMIT, LfgCard
from conductor.presentation import copy
from conductor.presentation.interact import instant
from conductor.presentation.layout import denied, empty, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import (
    CUSTOM_ID_RE,
    PersistentButton,
    format_custom_id,
    register_dynamic_item,
)
from conductor.presentation.roster import RosterView, roster_preview
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "lf"
log = structlog.get_logger("conductor.lfg")

__all__ = ["PANEL_KIND", "closed_view", "game_pick_view", "live_view", "post_footnote", "starter_view"]


def starter_view(guild_id: int) -> ui.LayoutView:
    button = ui.Button(
        label=copy.LFG_STARTER_BUTTON,
        style=discord.ButtonStyle.primary,
        custom_id=format_custom_id(PANEL_KIND, "new", guild_id),
    )
    return panel(
        copy.LFG_STARTER_TITLE,
        copy.LFG_STARTER_BODY,
        tone=Tone.SPECIAL,
        rows=(ui.ActionRow(button),),
        timeout=None,
        icon=Icon.STAR,
    )


def live_view(card: LfgCard) -> ui.LayoutView:
    ref = card.post_id
    buttons = [
        ui.Button(
            label=copy.LFG_JOIN,
            style=discord.ButtonStyle.success,
            custom_id=format_custom_id(PANEL_KIND, "join", ref),
        ),
        ui.Button(
            label=copy.LFG_LEAVE,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "leave", ref),
        ),
        ui.Button(
            label=copy.LFG_CLOSE,
            style=discord.ButtonStyle.danger,
            custom_id=format_custom_id(PANEL_KIND, "close", ref),
        ),
        ui.Button(
            label=copy.ROSTER_ALL,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(PANEL_KIND, "list", ref),
        ),
    ]
    if card.room_channel_id:
        buttons.append(
            ui.Button(
                label=copy.LFG_ROOM,
                style=discord.ButtonStyle.primary,
                custom_id=format_custom_id(PANEL_KIND, "room", ref),
            )
        )
    return panel(
        card.game,
        _body(card),
        fields=_fields(card),
        footnote=card.footnote or None,
        rows=(ui.ActionRow(*buttons),),
        tone=Tone.SPECIAL,
        timeout=None,
        icon=Icon.STAR,
    )


def closed_view(card: LfgCard) -> ui.LayoutView:
    return panel(
        copy.lfg_closed_title(card.closed_reason),
        _body(card),
        fields=_fields(card),
        tone=Tone.MUTED,
        timeout=None,
        icon=Icon.LOCK,
    )


def game_pick_view(
    options: tuple[tuple[int, str], ...],
    on_pick: Callable[[discord.Interaction, int], Awaitable[None]],
) -> ui.LayoutView:
    select = ui.Select(
        placeholder=copy.LFG_PICK_PLACEHOLDER,
        options=[discord.SelectOption(label=label[:100], value=str(role_id)) for role_id, label in options[:25]],
        min_values=1,
        max_values=1,
    )

    async def picked(interaction: discord.Interaction) -> None:
        raw = select.values[0] if select.values else ""
        try:
            role_id = int(raw)
        except (TypeError, ValueError):
            await respond(interaction, denied(copy.LFG_NO_GAME))
            return
        await on_pick(interaction, role_id)

    select.callback = instant(picked)
    return panel(
        copy.LFG_PICK_TITLE,
        copy.LFG_PICK_BODY,
        rows=(ui.ActionRow(select),),
        timeout=60.0,
        icon=Icon.STAR,
    )


def post_footnote(
    *,
    expires_text: str,
    game_role_id: int,
    lfg_role_id: int | None,
    game_pinged: bool,
    lfg_pinged: bool,
) -> str:
    parts: list[str] = []
    if game_pinged:
        parts.append(f"<@&{int(game_role_id)}>")
    if lfg_pinged and lfg_role_id:
        parts.append(f"<@&{int(lfg_role_id)}>")
    if not game_pinged:
        parts.append(copy.LFG_SKIP_PING)
    parts.append(f"сгорит {expires_text}")
    return " · ".join(part for part in parts if part)


def _body(card: LfgCard) -> list[str]:
    lines = [f"{copy.LFG_FIELD_AUTHOR} · <@{card.author_id}>", copy.lfg_need_line(card.need, card.signed)]
    if int(card.need) != NEED_UNLIMITED and int(card.need) + 1 > VOICE_USER_LIMIT_MAX:
        lines.append(copy.LFG_VOICE_CAP)
    return lines


def _fields(card: LfgCard) -> dict[str, object]:
    preview = roster_preview(card.signed_ids, card.signed)
    fields: dict[str, object] = {
        copy.LFG_FIELD_LIST: preview if preview else copy.LFG_EMPTY_LIST,
    }
    if card.note:
        fields[copy.LFG_FIELD_NOTE] = card.note
    return fields


def _lfg(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "lfg", None)


def _loader(interaction: discord.Interaction, name: str) -> Callable[..., Awaitable[object | None]]:
    """Ресурс кнопки берётся у сервиса: `guard_panel` для стартера, `guard_post` для поста."""
    service = _lfg(interaction)
    method = getattr(service, name, None)

    async def load(*, guild_id: int, ref: str) -> object | None:
        if not callable(method):
            return None
        return await method(guild_id, ref)

    return load


class _LfgButton(PersistentButton, template=CUSTOM_ID_RE):
    """Guard с ресурсом из сервиса. Сама не регистрируется."""

    kind = PANEL_KIND
    public = True
    loader_name: ClassVar[str] = "guard_post"

    async def load_resource(self, *, guild_id: int, ref: str) -> object | None:
        return await _loader(self._interaction, type(self).loader_name)(guild_id=guild_id, ref=ref)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class LfgModal(SafeModal, title=copy.LFG_MODAL_TITLE):
    def __init__(self, game_role_id: int) -> None:
        super().__init__()
        self.game_role_id = int(game_role_id)
        self._need = ui.Label(
            text=copy.LFG_NEED_LABEL,
            description=copy.LFG_NEED_HINT,
            component=ui.TextInput(max_length=3, required=True, placeholder="0", style=discord.TextStyle.short),
        )
        self._note = ui.Label(
            text=copy.LFG_NOTE_LABEL,
            component=ui.TextInput(max_length=NOTE_LIMIT, required=False, style=discord.TextStyle.short),
        )
        self.add_item(self._need)
        self.add_item(self._note)

    async def submit(self, interaction: discord.Interaction) -> None:
        lfg = _lfg(interaction)
        publish = getattr(lfg, "publish", None)
        guild = interaction.guild
        if not callable(publish) or guild is None or not isinstance(interaction.user, discord.Member):
            await respond(interaction, denied(copy.LFG_NO_CHANNEL))
            return
        result = await publish(
            guild,
            interaction.user,
            game_role_id=self.game_role_id,
            need=_labeled_value(self._need),
            note=_labeled_value(self._note),
        )
        error = str(getattr(result, "error", "") or "")
        if error == "bad_note":
            await respond(interaction, denied(copy.lfg_note_denied(str(getattr(result, "note_reason", "") or ""))))
            return
        if error:
            await respond(interaction, denied(copy.lfg_post_denied(error)))
            return
        await respond(interaction, panel(copy.LFG_STARTER_TITLE, copy.LFG_STARTER_BODY, tone=Tone.OK))


@register_dynamic_item
class LfgNewButton(_LfgButton, template=r"^c:lf:new:(?P<ref>\d+)$"):
    action = "new"
    loader_name = "guard_panel"
    # Дальше может открыться модалка — после defer это невозможно.
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        lfg = _lfg(interaction)
        begin = getattr(lfg, "begin", None)
        if not callable(begin) or interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await respond(interaction, denied(copy.LFG_NO_CHANNEL))
            return
        try:
            await begin(interaction)
        except Exception:
            log.exception("lfg_begin_failed", guild_id=interaction.guild.id)
            if not interaction.response.is_done():
                await respond(interaction, denied(copy.STALE_BODY))


@register_dynamic_item
class LfgJoinButton(_LfgButton, template=r"^c:lf:join:(?P<ref>\d+)$"):
    action = "join"

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        await _run_action(interaction, "join", self.ref)


@register_dynamic_item
class LfgLeaveButton(_LfgButton, template=r"^c:lf:leave:(?P<ref>\d+)$"):
    action = "leave"

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        await _run_action(interaction, "leave", self.ref)


@register_dynamic_item
class LfgCloseButton(_LfgButton, template=r"^c:lf:close:(?P<ref>\d+)$"):
    action = "close"

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        await _run_action(interaction, "close", self.ref)


@register_dynamic_item
class LfgListButton(_LfgButton, template=r"^c:lf:list:(?P<ref>\d+)$"):
    action = "list"

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        lfg = _lfg(interaction)
        load = getattr(lfg, "signed_ids", None)
        if not callable(load) or interaction.guild is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        ids = await load(interaction.guild, int(self.ref))
        if not ids:
            await respond(interaction, empty(copy.LFG_EMPTY_LIST))
            return
        await respond(interaction, RosterView(copy.LFG_FIELD_LIST, ids, guild=interaction.guild))


@register_dynamic_item
class LfgRoomButton(_LfgButton, template=r"^c:lf:room:(?P<ref>\d+)$"):
    action = "room"

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        _ = verdict
        lfg = _lfg(interaction)
        hint = getattr(lfg, "room_hint", None)
        if not callable(hint) or interaction.guild is None:
            await respond(interaction, denied(copy.LFG_ROOM_MISSING))
            return
        text = await hint(interaction.guild, int(self.ref))
        if not text:
            await respond(interaction, denied(copy.LFG_ROOM_MISSING))
            return
        await respond(interaction, panel(copy.LFG_ROOM, text, tone=Tone.OK))


async def _run_action(interaction: discord.Interaction, action: str, ref: str) -> None:
    lfg = _lfg(interaction)
    method = getattr(lfg, action, None)
    guild = interaction.guild
    if not callable(method) or guild is None or not isinstance(interaction.user, discord.Member):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    result = await method(guild, int(ref), interaction.user)
    error = str(getattr(result, "error", "") or "")
    if action == "join":
        if error:
            await respond(interaction, denied(copy.lfg_join_denied(error)))
            return
        room = getattr(result, "room_mention", None)
        body = copy.LFG_JOIN_OK if not room else copy.LFG_ROOM_HINT.format(channel=room)
        await respond(interaction, panel(copy.LFG_JOIN, body, tone=Tone.OK))
        return
    if action == "leave":
        if error:
            await respond(interaction, denied(copy.lfg_leave_denied(error)))
            return
        await respond(interaction, panel(copy.LFG_LEAVE, copy.LFG_LEAVE_OK, tone=Tone.OK))
        return
    if error:
        await respond(interaction, denied(copy.lfg_close_denied(error)))
        return
    await respond(interaction, panel(copy.LFG_CLOSE, copy.LFG_CLOSE_OK, tone=Tone.OK))
