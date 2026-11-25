"""Карточки ролей по выбору и модалка опции."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord import ui

from conductor.domain.self_roles import MenuKind, SelfRoleOption
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.layout import denied, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.persistent import PersistentSelect, format_custom_id, register_dynamic_item
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "sr"

Done = Callable[[discord.Interaction, str], Any]

__all__ = [
    "PANEL_KIND",
    "SelfRoleOptionModal",
    "menu_card",
    "pick_card",
]


def menu_card(*, menu_id: int, title: str, kind: str, options: Sequence[SelfRoleOption]) -> ui.LayoutView:
    body: list[str] = [
        copy.SELFROLES_GAME_BODY if kind == MenuKind.GAME else copy.SELFROLES_GENERAL_BODY,
    ]
    for item in options:
        line = f"• {item.label}"
        if item.description:
            line = f"{line} — {item.description}"
        if kind == MenuKind.GAME and item.opened:
            line = f"{line} · {copy.selfroles_opens(item.opened)}"
        body.append(line)
    rows: tuple[ui.ActionRow, ...] = ()
    if options:
        select = ui.Select(
            custom_id=format_custom_id(PANEL_KIND, "pick", menu_id),
            placeholder=copy.SELFROLES_PICK,
            min_values=0,
            max_values=len(options),
            options=[_select_option(item) for item in options],
        )
        rows = (ui.ActionRow(select),)
    return panel(
        title or copy.SELFROLES_TITLE,
        body,
        tone=Tone.INFO,
        icon=Icon.PANEL,
        rows=rows,
        timeout=None,
    )


def pick_card(*, granted: Sequence[str], removed: Sequence[str], opened: Sequence[str] = ()) -> ui.LayoutView:
    lines = [copy.selfroles_result(granted=granted, removed=removed)]
    if opened:
        lines.append(copy.selfroles_opened(opened))
    return panel(copy.SELFROLES_TITLE, lines, tone=Tone.OK, icon=Icon.DONE)


def _select_option(item: SelfRoleOption) -> discord.SelectOption:
    emoji = _emoji(item.emoji)
    kwargs: dict[str, Any] = {
        "label": item.label[:100],
        "value": str(item.role_id),
    }
    if item.description:
        kwargs["description"] = item.description[:100]
    if emoji is not None:
        kwargs["emoji"] = emoji
    return discord.SelectOption(**kwargs)


def _emoji(raw: str | None) -> discord.PartialEmoji | str | None:
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw)
    except (TypeError, ValueError):
        return raw[:8]


def _service(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "self_roles", None)


def _panel_loader(service: Any) -> Any:
    async def load(*, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(service, "guard_panel", None)
        if not callable(loader):
            return GuardResource(ref=str(ref), guild_id=guild_id)
        return await loader(guild_id, ref)

    return load


@register_dynamic_item
class SelfRoleSelect(PersistentSelect, template=r"^c:sr:pick:(?P<ref>\d+)$"):
    kind = PANEL_KIND
    action = "pick"
    public = True

    def __init__(self, ref: str, *, item: ui.Select | None = None) -> None:
        self.ref = str(ref)
        if item is None:
            item = ui.Select(
                custom_id=format_custom_id(PANEL_KIND, "pick", self.ref),
                placeholder=copy.SELFROLES_PICK,
                min_values=0,
                max_values=1,
            )
        super().__init__(self.ref, item=item)

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        return await _panel_loader(_service(self._interaction))(guild_id=guild_id, ref=ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict, values: object = ()) -> None:
        _ = verdict
        service = _service(interaction)
        pick = getattr(service, "pick", None)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not callable(pick):
            return
        values = tuple(int(value) for value in (getattr(self.item, "values", None) or ()) if str(value).isdigit())
        view = await pick(guild, member, int(self.ref), values)
        await respond(interaction, view or denied(copy.STALE_BODY))

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: Any,
        /,
    ) -> SelfRoleSelect:
        select = item if isinstance(item, ui.Select) else None
        return cls(match["ref"], item=select)


class SelfRoleOptionModal(SafeModal, title=copy.ADMIN_PAGE_SELFROLES):
    def __init__(self, *, on_done: Done, defaults: Sequence[str] = ()) -> None:
        super().__init__()
        self.on_done = on_done
        padded = list(defaults) + [""] * 4
        self.label_field = ui.TextInput(max_length=80, default=padded[0] or None, required=False)
        self.desc_field = ui.TextInput(max_length=100, default=padded[1] or None, required=False)
        self.emoji_field = ui.TextInput(max_length=64, default=padded[2] or None, required=False)
        self.tag_field = ui.TextInput(max_length=16, default=padded[3] or "none", required=False)
        self.add_item(ui.Label(text=copy.ADMIN_SR_LABEL, component=self.label_field))
        self.add_item(ui.Label(text=copy.ADMIN_SR_DESC, component=self.desc_field))
        self.add_item(ui.Label(text=copy.ADMIN_SR_EMOJI, component=self.emoji_field))
        self.add_item(ui.Label(text=copy.ADMIN_SR_TAG, component=self.tag_field))

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(
            interaction,
            "\n".join(
                (
                    str(self.label_field.value or ""),
                    str(self.desc_field.value or ""),
                    str(self.emoji_field.value or ""),
                    str(self.tag_field.value or "none"),
                )
            ),
        )
