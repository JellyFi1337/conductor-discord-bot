"""Карточки мастера /loyalty. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from io import BytesIO

import discord
from discord import ui

from conductor.domain.loyalty import (
    TIER_LABELS,
    TIER_PRIORITY,
    LoyaltyTier,
    below_threshold_label,
    loyalty_threshold_copy,
)
from conductor.presentation import copy
from conductor.presentation.admin import confirm_card
from conductor.presentation.icons import TIER_EMOJI, marked
from conductor.presentation.interact import instant
from conductor.presentation.layout import panel
from conductor.presentation.text import FOOTER_LOYALTY, stamp, user_plain
from conductor.presentation.tokens import Tone

TAB_REGISTRY = "registry"
TAB_WIZARD = "wizard"

TabSwitch = Callable[[discord.Interaction, str], Awaitable[None]]
StepClick = Callable[[discord.Interaction], Awaitable[None]]


def tier_label(tier: LoyaltyTier) -> str:
    return marked(TIER_EMOJI[tier.value], TIER_LABELS[tier])


def tier_short(tier: LoyaltyTier) -> str:
    return marked(TIER_EMOJI[tier.value], TIER_LABELS[tier].removesuffix(" Old"))


def plan_rules(guild: discord.Guild) -> str:
    return copy.loyalty_plan_rules(
        order=" → ".join(tier_short(tier) for tier in TIER_PRIORITY),
        rule=loyalty_threshold_copy(),
        created=stamp(guild.created_at, "D"),
    )


def plan_count_fields(plan: object) -> dict[str, object]:
    counts = getattr(plan, "counts", {})
    fields: dict[str, object] = {tier_label(tier): counts.get(tier, 0) for tier in TIER_PRIORITY}
    fields[copy.LOYALTY_FIELD_ELIGIBLE] = getattr(plan, "eligible", 0)
    fields[copy.LOYALTY_FIELD_BOTS] = getattr(plan, "skipped_bots", 0)
    fields[below_threshold_label()] = getattr(plan, "skipped_silent", 0)
    fields[copy.LOYALTY_FIELD_TODAY] = getattr(plan, "skipped_today", 0)
    eligible = getattr(plan, "eligible", 0)
    silent = getattr(plan, "skipped_silent", 0)
    today = getattr(plan, "skipped_today", 0)
    bots = getattr(plan, "skipped_bots", 0)
    accounted = getattr(plan, "accounted", 0)
    considered = getattr(plan, "considered", 0)
    fields[copy.LOYALTY_FIELD_BALANCES] = copy.LOYALTY_BALANCES.format(
        eligible=eligible, silent=silent, today=today, bots=bots, accounted=accounted, considered=considered
    )
    return fields


def preview_file(guild: discord.Guild, grouped: dict[LoyaltyTier, list[int]]) -> discord.File:
    lines: list[str] = []
    for tier in TIER_PRIORITY:
        ids = grouped.get(tier, [])
        lines.append(f"{TIER_LABELS[tier]} ({len(ids)})")
        if not ids:
            lines.append("—")
        else:
            for user_id in ids:
                lines.append(user_plain(guild.get_member(user_id), user_id))
        lines.append("")
    payload = "\n".join(lines).encode("utf-8")
    return discord.File(BytesIO(payload), filename="loyalty-preview.txt")


def preview_summary(plan: object) -> ui.LayoutView:
    return panel(
        copy.LOYALTY_PREVIEW_TITLE,
        copy.LOYALTY_PREVIEW_BODY.format(rule=loyalty_threshold_copy()),
        tone=Tone.SPECIAL,
        fields=plan_count_fields(plan),
        footnote=FOOTER_LOYALTY,
    )


def empty_census_card() -> ui.LayoutView:
    return panel(
        copy.LOYALTY_NO_DATA_TITLE,
        copy.LOYALTY_NO_DATA,
        tone=Tone.SPECIAL,
        footnote=FOOTER_LOYALTY,
    )


def need_roles_card() -> ui.LayoutView:
    return panel(
        copy.LOYALTY_NEED_PERMS_TITLE,
        copy.LOYALTY_NEED_ROLES,
        tone=Tone.DANGER,
        footnote=FOOTER_LOYALTY,
    )


def grant_denial_view(denial: object) -> ui.LayoutView:
    kind = getattr(denial, "kind", "")
    if kind == "no_plan":
        return panel(copy.LOYALTY_NO_PLAN_TITLE, copy.LOYALTY_NO_PLAN, tone=Tone.SPECIAL, footnote=FOOTER_LOYALTY)
    if kind == "unbound":
        missing = ", ".join(getattr(denial, "missing", ()))
        return panel(
            copy.LOYALTY_UNBOUND_TITLE,
            copy.LOYALTY_UNBOUND_MISSING.format(missing=missing),
            tone=Tone.DANGER,
            footnote=FOOTER_LOYALTY,
        )
    too_high = ", ".join(getattr(denial, "too_high", ()))
    return panel(
        copy.LOYALTY_LOW_TITLE,
        copy.LOYALTY_LOW_BODY.format(too_high=too_high),
        tone=Tone.DANGER,
        footnote=FOOTER_LOYALTY,
    )


def grant_confirm_card(
    *,
    on_yes: Callable[[discord.Interaction], Awaitable[None]],
    on_no: Callable[[discord.Interaction], Awaitable[None]],
) -> ui.LayoutView:
    return confirm_card(question=copy.LOYALTY_GRANT_CONFIRM, verb=copy.LOYALTY_GRANT_VERB, on_yes=on_yes, on_no=on_no)


def granted_card(batch_id: int, stats: object) -> ui.LayoutView:
    return panel(
        copy.loyalty_granted(batch_id),
        copy.loyalty_granted_body(
            applied=int(getattr(stats, "applied", 0)),
            skipped=int(getattr(stats, "skipped", 0)),
            errors=int(getattr(stats, "errors", 0)),
        ),
        tone=Tone.SPECIAL,
        footnote=FOOTER_LOYALTY,
    )


def frozen_card(batch_id: int, plan: object) -> ui.LayoutView:
    return panel(
        copy.loyalty_frozen(batch_id),
        copy.LOYALTY_FROZEN_BODY.format(count=getattr(plan, "eligible", 0), rule=loyalty_threshold_copy()),
        tone=Tone.SPECIAL,
        fields=plan_count_fields(plan),
        footnote=f"{FOOTER_LOYALTY} · {copy.LOYALTY_GRANT_FOOT}",
    )


def candidates_card(guild: discord.Guild, plan: object) -> ui.LayoutView:
    return panel(
        copy.LOYALTY_CANDIDATES_TITLE,
        plan_rules(guild),
        tone=Tone.SPECIAL,
        fields=plan_count_fields(plan),
        footnote=f"{FOOTER_LOYALTY} · {copy.LOYALTY_GRANT_FOOT}",
    )


def registry_fields(
    *,
    counts: Mapping[LoyaltyTier, int],
    mismatches: int,
    seeded_at: object,
    old_ids: set[int],
) -> dict[str, object]:
    fields: dict[str, object] = {tier_label(tier): counts.get(tier, 0) for tier in TIER_PRIORITY}
    fields[copy.LOYALTY_FIELD_ROWS] = sum(counts.values())
    fields[copy.LOYALTY_FIELD_MISMATCH] = mismatches
    if seeded_at is not None:
        fields[copy.LOYALTY_FIELD_FIRST] = stamp(seeded_at)  # type: ignore[arg-type]
    elif not old_ids:
        fields[copy.LOYALTY_UNBOUND_TITLE] = copy.LOYALTY_UNBOUND_ROLES
    return fields


def wizard_card(
    *,
    tab: str,
    title: str,
    body: str = "",
    fields: Mapping[str, object] | None = None,
    on_tab: TabSwitch | None = None,
    on_candidates: StepClick | None = None,
    on_preview: StepClick | None = None,
    on_freeze: StepClick | None = None,
    on_grant: StepClick | None = None,
) -> ui.LayoutView:
    rows: list[ui.ActionRow] = []
    if on_tab is not None:
        rows.append(_tab_row(tab, on_tab))
    if tab == TAB_WIZARD:
        buttons = (
            (copy.LOYALTY_BTN_CANDIDATES, on_candidates),
            (copy.LOYALTY_BTN_PREVIEW, on_preview),
            (copy.LOYALTY_BTN_FREEZE, on_freeze),
            (copy.LOYALTY_BTN_GRANT, on_grant),
        )
        shown = [ui.Button(label=label, style=discord.ButtonStyle.secondary) for label, _cb in buttons]
        for button, (_label, callback) in zip(shown, buttons, strict=True):
            if callback is not None:
                button.callback = instant(callback)
        rows.append(ui.ActionRow(*shown))
    footnote = FOOTER_LOYALTY if tab == TAB_REGISTRY else f"{FOOTER_LOYALTY} · {copy.LOYALTY_GRANT_FOOT}"
    return panel(
        title,
        body,
        tone=Tone.SPECIAL,
        fields=fields,
        footnote=footnote,
        rows=tuple(rows),
    )


def _tab_row(tab: str, on_tab: TabSwitch) -> ui.ActionRow:
    options = (
        (TAB_REGISTRY, copy.LOYALTY_TAB_REGISTRY),
        (TAB_WIZARD, copy.LOYALTY_TAB_WIZARD),
    )
    select = ui.Select(
        placeholder=copy.LOYALTY_TABS,
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=label, value=value, default=value == tab) for value, label in options],
    )

    async def picked(interaction: discord.Interaction) -> None:
        await on_tab(interaction, select.values[0] if select.values else tab)

    select.callback = instant(picked)
    return ui.ActionRow(select)
