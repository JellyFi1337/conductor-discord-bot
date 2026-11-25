"""Постоянные компоненты: схема custom_id и DynamicItem без привязки к фичам."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import discord
from discord import ui

CUSTOM_ID_PATTERN = r"c:(?P<kind>[a-z]{2,4}):(?P<action>[a-z0-9_]+):(?P<ref>\d+|E-[0-9A-Fa-f]{4}|[a-z0-9_]{1,32})"
CUSTOM_ID_RE = re.compile(rf"^{CUSTOM_ID_PATTERN}$")

PANEL_LIVE = "live"
PANEL_STALE = "stale"

# kind в custom_id ↔ домен ключа прав / область мутаций.
# sk — каркас, shw — витрины, bp — файл настройки: зарезервированы до появления кнопок.
PANEL_KINDS: tuple[str, ...] = (
    "vc",
    "tk",
    "gw",
    "ev",
    "gi",
    "gr",
    "pl",
    "rep",
    "md",
    "am",
    "vf",
    "sr",
    "st",
    "lf",
    "wh",
    "vh",
    "sk",
    "shw",
    "bp",
    "bug",
)

__all__ = [
    "CUSTOM_ID_PATTERN",
    "CUSTOM_ID_RE",
    "CustomId",
    "PANEL_KINDS",
    "PANEL_LIVE",
    "PANEL_STALE",
    "PersistentButton",
    "PersistentSelect",
    "ack_press",
    "dynamic_items",
    "format_custom_id",
    "load_persistent_ui",
    "parse_custom_id",
    "register_dynamic_item",
]

_DYNAMIC: list[type[ui.DynamicItem[Any]]] = []


@dataclass(frozen=True, slots=True)
class CustomId:
    kind: str
    action: str
    ref: str

    @property
    def ref_int(self) -> int | None:
        if not self.ref.isdigit():
            return None
        return int(self.ref)

    @property
    def ref_key(self) -> str:
        return self.ref

    def __str__(self) -> str:
        return format_custom_id(self.kind, self.action, self.ref)


def format_custom_id(kind: str, action: str, ref: int | str) -> str:
    return f"c:{kind}:{action}:{ref}"


def parse_custom_id(value: str) -> CustomId | None:
    match = CUSTOM_ID_RE.fullmatch(value)
    if match is None:
        return None
    return CustomId(kind=match["kind"], action=match["action"], ref=match["ref"])


def register_dynamic_item(cls: type[ui.DynamicItem[Any]]) -> type[ui.DynamicItem[Any]]:
    if cls not in _DYNAMIC:
        _DYNAMIC.append(cls)
    return cls


def dynamic_items() -> tuple[type[ui.DynamicItem[Any]], ...]:
    return tuple(_DYNAMIC)


_PERSISTENT_UI = (
    "conductor.presentation.automod",
    "conductor.presentation.bugs",
    "conductor.presentation.gathering",
    "conductor.presentation.giveaway",
    "conductor.presentation.groups",
    "conductor.presentation.lfg",
    "conductor.presentation.moderation",
    "conductor.presentation.poll",
    "conductor.presentation.self_roles",
    "conductor.presentation.structure",
    "conductor.presentation.tickets",
    "conductor.presentation.verification",
    "conductor.presentation.voice_panel",
    "conductor.presentation.watchers",
)


def load_persistent_ui() -> tuple[type[ui.DynamicItem[Any]], ...]:
    """Импортирует все модули с DynamicItem, чтобы кнопки жили после рестарта."""
    import importlib

    for name in _PERSISTENT_UI:
        importlib.import_module(name)
    return dynamic_items()


async def ack_press(item: Any, interaction: discord.Interaction) -> bool:
    if not getattr(type(item), "acks_first", True):
        return True
    from conductor.presentation.interact import ack

    return await ack(interaction)


class _PersistentPress:
    """Общий вход: ack → guard → on_action. callback не переопределяют подклассы."""

    kind: ClassVar[str] = ""
    action: ClassVar[str] = ""
    access_key: ClassVar[str | None] = None
    mutation_scope: ClassVar[Any] = None
    checks_hierarchy: ClassVar[bool] = False
    public: ClassVar[bool] = False
    allow_dm: ClassVar[bool] = False
    acks_first: ClassVar[bool] = True
    ref: str
    _interaction: discord.Interaction

    async def load_resource(self, *, guild_id: int, ref: str) -> object | None:
        return None

    async def hierarchy_needed(self, interaction: discord.Interaction) -> bool:
        del interaction
        return type(self).checks_hierarchy

    async def _guarded(self, interaction: discord.Interaction) -> object | None:
        from conductor.presentation.guard import run_guard

        self._interaction = interaction
        if not await ack_press(self, interaction):
            return None
        verdict = await run_guard(
            interaction,
            ref=self.ref,
            load=self.load_resource,
            key=type(self).access_key,
            scope=type(self).mutation_scope,
            hierarchy=await self.hierarchy_needed(interaction),
            public=type(self).public,
            allow_dm=type(self).allow_dm,
        )
        if not verdict.ok:
            return None
        return verdict

    async def _report_press(self, interaction: discord.Interaction, error: Exception) -> None:
        from conductor.presentation.layout import report_ui_error

        await report_ui_error(interaction, error, item=getattr(self, "item", None))


class PersistentButton(_PersistentPress, ui.DynamicItem[ui.Button], template=CUSTOM_ID_RE):
    """База для кнопок фич. Не регистрируется сама — подклассы вызывают register_dynamic_item."""

    def __init__(
        self,
        ref: int | str,
        *,
        item: ui.Button | None = None,
        kind: str | None = None,
        action: str | None = None,
        label: str = "\u200b",
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        self.ref = str(ref)
        self.kind_code = kind or type(self).kind
        self.action_name = action or type(self).action
        if item is None:
            item = ui.Button(
                label=label,
                style=style,
                custom_id=format_custom_id(self.kind_code or "xx", self.action_name or "noop", self.ref),
            )
        super().__init__(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            verdict = await self._guarded(interaction)
            if verdict is None:
                return
            await self.on_action(interaction, verdict)
        except Exception as exc:
            await self._report_press(interaction, exc)

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        return None

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> PersistentButton:
        button = item if isinstance(item, ui.Button) else None
        groups = match.groupdict()
        return cls(groups["ref"], item=button, kind=groups.get("kind"), action=groups.get("action"))


class PersistentSelect(_PersistentPress, ui.DynamicItem[Any], template=CUSTOM_ID_RE):
    """База для постоянных селектов. callback финальный, как у PersistentButton."""

    def __init__(self, ref: int | str, *, item: ui.Item[Any]) -> None:
        self.ref = str(ref)
        super().__init__(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            verdict = await self._guarded(interaction)
            if verdict is None:
                return
            values: Sequence[Any] = tuple(getattr(self.item, "values", None) or ())
            await self.on_action(interaction, verdict, values)
        except Exception as exc:
            await self._report_press(interaction, exc)

    async def on_action(
        self,
        interaction: discord.Interaction,
        verdict: object,
        values: Sequence[Any] = (),
    ) -> None:
        return None

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> PersistentSelect:
        groups = match.groupdict()
        return cls(groups["ref"], item=item)
