"""Страница /admin «Розыгрыши»."""

from __future__ import annotations

from conductor.domain.access import GIVEAWAY_MANAGE
from conductor.domain.admin import (
    PAGE_GIVEAWAY,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_id_list,
    parse_int_group,
)
from conductor.presentation import copy


def _roles(ids: tuple[int, ...]) -> str:
    return ", ".join(f"<@&{int(role_id)}>" for role_id in ids) or "—"


async def _ready(_ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report

    return pack_report(())


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    messages, voice, cap = await ctx.bot.giveaways.weight_formula(ctx.guild_id)
    allowed = await ctx.bot.giveaways.whitelist(ctx.guild_id)
    return PageSnapshot(
        fields={
            copy.ADMIN_GIVE_WEIGHTS: copy.giveaway_config_body(messages, voice, cap),
            copy.ADMIN_GIVE_ROLES: _roles(allowed),
        },
        controls=(
            PageControl("button", "give.weights", copy.ADMIN_GIVE_WEIGHTS_BTN),
            PageControl("role", "give.roles", copy.ADMIN_GIVE_ROLES, min_values=1, max_values=25),
        ),
        footnote=copy.ADMIN_GIVE_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = ctx.bot.giveaways
    if control_id == "give.weights":
        messages, voice, cap = await service.weight_formula(ctx.guild_id)
        return AdminResult(
            ok=True,
            modal="numbers",
            target="give.weights.set",
            title=copy.ADMIN_GIVE_WEIGHTS,
            labels=(copy.CMD_GIVEAWAY_WEIGHT_MSG, copy.CMD_GIVEAWAY_WEIGHT_VOICE, copy.CMD_GIVEAWAY_WEIGHT_MAX),
            defaults=(str(messages), str(voice), str(cap)),
        )
    if control_id == "give.weights.set":
        parsed = parse_int_group(value.replace("\n", " "), count=3)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        before = await service.weight_formula(ctx.guild_id)
        updated = await service.set_weight_formula(
            ctx.guild_id,
            messages=parsed[0],
            voice_minutes=parsed[1],
            cap=parsed[2],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.GIVEAWAY_CONFIG_BAD)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_GIVE_WEIGHTS,
            before=copy.giveaway_config_body(*before),
            after=copy.giveaway_config_body(*updated),
        )
    if control_id == "give.roles":
        chosen = parse_id_list(value)
        if chosen is None or not chosen:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        before = await service.whitelist(ctx.guild_id)
        current = set(before)
        for role_id in chosen:
            if role_id in current:
                await service.deny_role(ctx.guild_id, role_id)
                current.discard(role_id)
                continue
            if not await service.allow_role(ctx.guild_id, role_id):
                return AdminResult(ok=False, error=copy.GIVEAWAY_BAD_ROLE)
            current.add(role_id)
        after = await service.whitelist(ctx.guild_id)
        return AdminResult(ok=True, field=copy.ADMIN_GIVE_ROLES, before=_roles(before), after=_roles(after))
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_GIVEAWAY,
    copy.ADMIN_PAGE_GIVEAWAY,
    GIVEAWAY_MANAGE,
    _ready,
    _snapshot,
    _handle,
    keys={"give": GIVEAWAY_MANAGE},
)
