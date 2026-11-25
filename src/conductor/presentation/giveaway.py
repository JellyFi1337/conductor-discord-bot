"""Панель розыгрыша, модалка старта и кнопки. Без sqlalchemy и без services."""

from __future__ import annotations

from typing import Any, ClassVar

import discord
from discord import ui

from conductor.domain.access import GIVEAWAY_MANAGE
from conductor.domain.giveaway import GiveawayCard, GiveawayRules, GiveawaySpec, parse_spec
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.layout import denied, empty, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.motion import Countdown, EmojiRegistry
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "gw"
GIVEAWAY_ICON = "🎉"

__all__ = [
    "PANEL_KIND",
    "GiveawayStartModal",
    "cancelled_view",
    "confirm_cancel_view",
    "finished_view",
    "live_view",
    "reveal_frames",
]


def live_view(card: GiveawayCard) -> ui.LayoutView:
    join = ui.Button(
        label=copy.giveaway_join_label(card.entries),
        style=discord.ButtonStyle.primary,
        custom_id=format_custom_id(PANEL_KIND, "join", card.giveaway_id),
    )
    end = ui.Button(
        label=copy.GIVEAWAY_END,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "end", card.giveaway_id),
    )
    cancel = ui.Button(
        label=copy.GIVEAWAY_CANCEL_VERB,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "cancel", card.giveaway_id),
    )
    fields: dict[str, object] = {
        copy.GIVEAWAY_WINNERS_N: card.winners,
        copy.GIVEAWAY_UNTIL: Countdown().render(card.ends_at),
        copy.GIVEAWAY_ENTRIES: card.entries,
    }
    terms = copy.giveaway_terms(card.rules)
    if terms:
        fields[copy.GIVEAWAY_TERMS] = terms
    if card.rules.weight:
        fields[copy.GIVEAWAY_WEIGHT] = copy.giveaway_weight_formula(card.rules)
    extra = copy.giveaway_consolation_live(card.rules)
    if extra:
        fields[copy.GIVEAWAY_CONSOLATION] = extra
    return panel(
        card.prize,
        fields=fields,
        footnote=copy.giveaway_host_line(card.host_id),
        rows=(ui.ActionRow(join, end, cancel),),
        tone=Tone.SPECIAL,
        icon=GIVEAWAY_ICON,
        timeout=None,
    )


def finished_view(card: GiveawayCard) -> ui.LayoutView:
    reroll = ui.Button(
        label=copy.GIVEAWAY_REROLL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "reroll", card.giveaway_id),
    )
    fields: dict[str, object] = {copy.GIVEAWAY_WINNERS: copy.giveaway_winners_line(card.winner_ids)}
    if card.consolation_ids or card.rules.consolation_count:
        fields[copy.GIVEAWAY_CONSOLATION] = copy.giveaway_consolation_line(
            card.consolation_ids,
            text=card.rules.consolation_text,
            role_id=card.rules.consolation_role_id,
        )
    return panel(
        f"{card.prize} — {copy.GIVEAWAY_OVER}",
        fields=fields,
        footnote=copy.giveaway_finished_footnote(card.host_id),
        rows=(ui.ActionRow(reroll),),
        tone=Tone.SPECIAL,
        timeout=None,
    )


def cancelled_view(card: GiveawayCard) -> ui.LayoutView:
    return panel(
        f"{card.prize} — {copy.GIVEAWAY_CANCELLED}",
        footnote=copy.giveaway_host_line(card.host_id),
        tone=Tone.MUTED,
        timeout=None,
        icon=Icon.LOCK,
    )


def confirm_cancel_view(giveaway_id: int) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.GIVEAWAY_CANCEL_VERB,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "yes", giveaway_id),
    )
    no = ui.Button(
        label=copy.CONFIRM_CANCEL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "no", giveaway_id),
    )
    return panel(
        copy.GIVEAWAY_CONFIRM,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def reveal_frames(card: GiveawayCard, *, registry: EmojiRegistry | None = None) -> tuple[ui.LayoutView, ...]:
    motion = registry or EmojiRegistry()
    load = motion.render("load")
    dots = motion.render("dots")
    first = panel(
        copy.GIVEAWAY_DONE_TITLE,
        f"{load} {copy.GIVEAWAY_PICKING}",
        tone=Tone.SPECIAL,
        icon=GIVEAWAY_ICON,
        timeout=None,
    )
    second = panel(
        copy.GIVEAWAY_DONE_TITLE,
        (
            f"**{copy.GIVEAWAY_ENTRIES}:** {card.entries} · **{copy.GIVEAWAY_WINNERS_N}:** {card.winners}",
            f"{dots} {copy.GIVEAWAY_DRUM}",
        ),
        tone=Tone.SPECIAL,
        icon=GIVEAWAY_ICON,
        timeout=None,
    )
    return (first, second, finished_view(card))


def _giveaways(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "giveaways", None)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class GiveawayStartModal(SafeModal, title=copy.GIVEAWAY_MODAL_TITLE):
    def __init__(self, *, prize_role_id: int | None = None, rules: GiveawayRules | None = None) -> None:
        super().__init__()
        self.prize_role_id = prize_role_id
        self.rules = rules or GiveawayRules()
        self._prize = ui.Label(
            text=copy.GIVEAWAY_FIELD_PRIZE,
            component=ui.TextInput(max_length=200, required=True, style=discord.TextStyle.short),
        )
        self._duration = ui.Label(
            text=copy.GIVEAWAY_FIELD_DURATION,
            component=ui.TextInput(
                max_length=16,
                required=True,
                placeholder=copy.GIVEAWAY_DURATION_PLACEHOLDER,
                style=discord.TextStyle.short,
            ),
        )
        self._winners = ui.Label(
            text=copy.GIVEAWAY_FIELD_WINNERS,
            component=ui.TextInput(max_length=2, required=True, default="1", style=discord.TextStyle.short),
        )
        self.add_item(self._prize)
        self.add_item(self._duration)
        self.add_item(self._winners)

    def spec(self) -> GiveawaySpec | None:
        return parse_spec(
            prize=_labeled_value(self._prize),
            winners=_labeled_value(self._winners),
            duration=_labeled_value(self._duration),
        )

    async def submit(self, interaction: discord.Interaction) -> None:
        spec = self.spec()
        if spec is None:
            await respond(interaction, denied(copy.GIVEAWAY_BAD_SPEC))
            return
        giveaways = _giveaways(interaction)
        start = getattr(giveaways, "start", None)
        guild = interaction.guild
        channel = interaction.channel
        send = getattr(channel, "send", None)
        if not callable(start) or guild is None or not callable(send):
            await respond(interaction, denied(copy.GIVEAWAY_NO_CHANNEL))
            return
        created = await start(
            guild,
            channel,
            actor_id=interaction.user.id,
            spec=spec,
            prize_role_id=self.prize_role_id,
            rules=self.rules,
        )
        if created is None:
            await respond(interaction, denied(copy.GIVEAWAY_BAD_ROLE))
            return
        await respond(interaction, panel(copy.GIVEAWAY_DONE_TITLE, copy.GIVEAWAY_STARTED, tone=Tone.OK))


class GiveawayMixin:
    kind = PANEL_KIND
    need: ClassVar[str] = "open"
    public: ClassVar[bool] = False
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        giveaways = _giveaways(self._interaction)
        getter = getattr(giveaways, "guard_resource", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref, need=type(self).need)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        return None


@register_dynamic_item
class GiveawayJoinButton(GiveawayMixin, PersistentButton, template=r"^c:gw:join:(?P<ref>\d+)$"):
    action = "join"
    public = True
    access_key = None
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        giveaways = _giveaways(interaction)
        join = getattr(giveaways, "join", None)
        if not callable(join) or interaction.guild is None:
            return
        result = await join(interaction.guild, int(self.ref), interaction.user)
        verdict = getattr(result, "verdict", result)
        if verdict == "joined":
            await respond(interaction, panel(copy.GIVEAWAY_JOIN, copy.GIVEAWAY_JOINED, tone=Tone.OK))
            return
        if verdict == "already":
            await respond(interaction, panel(copy.GIVEAWAY_JOIN, copy.GIVEAWAY_ALREADY, tone=Tone.OK))
            return
        if verdict == "bot":
            await respond(interaction, denied(copy.GIVEAWAY_BOT))
            return
        if verdict == "unfit":
            await respond(interaction, denied(copy.giveaway_unfit(getattr(result, "gaps", ()))))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GiveawayEndButton(GiveawayMixin, PersistentButton, template=r"^c:gw:end:(?P<ref>\d+)$"):
    action = "end"
    access_key = GIVEAWAY_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        giveaways = _giveaways(interaction)
        conclude = getattr(giveaways, "conclude", None)
        if not callable(conclude) or interaction.guild is None:
            return
        result = await conclude(int(self.ref), actor_id=interaction.user.id)
        if result == "ok":
            await respond(interaction, panel(copy.GIVEAWAY_DONE_TITLE, copy.GIVEAWAY_ENDED_OK, tone=Tone.OK))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GiveawayRerollButton(GiveawayMixin, PersistentButton, template=r"^c:gw:reroll:(?P<ref>\d+)$"):
    action = "reroll"
    access_key = GIVEAWAY_MANAGE
    need = "ended"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        giveaways = _giveaways(interaction)
        reroll = getattr(giveaways, "reroll", None)
        if not callable(reroll) or interaction.guild is None:
            return
        result = await reroll(interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True)
        if result == "ok":
            await respond(interaction, panel(copy.GIVEAWAY_DONE_TITLE, copy.GIVEAWAY_REROLL_OK, tone=Tone.OK))
            return
        if result == "empty":
            await respond(interaction, empty(copy.GIVEAWAY_REROLL_EMPTY))
            return
        if result == "expired":
            await respond(interaction, denied(copy.GIVEAWAY_REROLL_LATE))
            return
        if result == "denied":
            await respond(interaction, denied(copy.GIVEAWAY_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GiveawayCancelButton(GiveawayMixin, PersistentButton, template=r"^c:gw:cancel:(?P<ref>\d+)$"):
    action = "cancel"
    access_key = GIVEAWAY_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, confirm_cancel_view(int(self.ref)))


@register_dynamic_item
class GiveawayYesButton(GiveawayMixin, PersistentButton, template=r"^c:gw:yes:(?P<ref>\d+)$"):
    action = "yes"
    access_key = GIVEAWAY_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        giveaways = _giveaways(interaction)
        cancel = getattr(giveaways, "cancel", None)
        if not callable(cancel) or interaction.guild is None:
            return
        result = await cancel(interaction.guild, int(self.ref), actor_id=interaction.user.id, has_manage=True)
        if result == "ok":
            await respond(interaction, panel(copy.GIVEAWAY_DONE_TITLE, copy.GIVEAWAY_CANCEL_OK, tone=Tone.OK))
            return
        if result == "denied":
            await respond(interaction, denied(copy.GIVEAWAY_STRANGER))
            return
        await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GiveawayNoButton(GiveawayMixin, PersistentButton, template=r"^c:gw:no:(?P<ref>\d+)$"):
    action = "no"
    access_key = GIVEAWAY_MANAGE
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, panel(copy.GIVEAWAY_DONE_TITLE, copy.GIVEAWAY_CANCEL_ABORT, tone=Tone.OK))
