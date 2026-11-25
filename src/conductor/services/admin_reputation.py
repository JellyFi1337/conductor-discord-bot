"""Страница /admin «Репутация»."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, REP_CONFIG
from conductor.domain.admin import (
    PAGE_REPUTATION,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_int_group,
)
from conductor.domain.reputation import TIER_COUNT
from conductor.presentation import copy
from conductor.services.role_broker import RoleBindOverlap


def _role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


def _tier_field(tier: object) -> str:
    return copy.admin_rep_tier_field(
        name=str(getattr(tier, "name", "") or copy.admin_rep_tier(int(getattr(tier, "tier", 0)))),
        span=copy.rep_tier_span(
            lower=int(getattr(tier, "lower", 0)),
            upper=int(getattr(tier, "upper", 0)),
            last=int(getattr(tier, "tier", 0)) == TIER_COUNT - 1,
        ),
    )


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, role_issue

    panel = await ctx.bot.reputation.show_config(ctx.guild_id)
    if len(panel.tiers) != TIER_COUNT:
        from conductor.services.admin_pages import ready_issue

        return pack_report([ready_issue("empty")])
    issues = [
        role_issue(ctx.guild, item.role_id, fix_control=f"rep.tier:{item.tier}")
        for item in panel.tiers
    ]
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    panel = await ctx.bot.reputation.show_config(ctx.guild_id)
    config = panel.config
    voice = config.voice_seconds_per_point // 60
    fields: dict[str, object] = {_tier_field(item): _role(item.role_id) for item in panel.tiers}
    fields[copy.ADMIN_REP_WARN] = config.warn_hit
    fields[copy.ADMIN_REP_TIMEOUT] = f"{config.timeout_per_step} · {config.timeout_min}…{config.timeout_max}"
    fields[copy.ADMIN_REP_MUTE] = config.mute_hit
    fields[copy.ADMIN_REP_KICK] = config.kick_hit
    fields[copy.ADMIN_REP_CAP] = config.max_hit_per_hour
    fields[copy.ADMIN_REP_GROWTH] = copy.per_minutes(config.messages_per_point, voice)
    fields[copy.ADMIN_REP_DRY] = copy.yn(config.dry_run)
    fields[copy.ADMIN_REP_APPEAL] = panel.appeal_contact or "—"
    bound = {int(item.tier): item.role_id for item in panel.tiers}
    from conductor.services.admin_pages import role_pick_control
    from conductor.services.role_broker import RoleSubsystem

    controls = []
    for item in panel.tiers:
        others = tuple(int(role_id) for tier, role_id in bound.items() if tier != int(item.tier) and role_id)
        current = item.role_id
        controls.append(
            await role_pick_control(
                ctx,
                f"rep.tier:{item.tier}",
                copy.admin_rep_tier(item.tier),
                subsystem=RoleSubsystem.REPUTATION,
                exclude_ids=others,
                current=None if current is None else str(int(current)),
            )
        )
    controls = [
        *controls,
        PageControl("button", "rep.hits", copy.ADMIN_REP_HITS),
        PageControl("button", "rep.growth", copy.ADMIN_REP_GROWTH_BTN),
        PageControl("button", "rep.names", copy.ADMIN_REP_NAMES),
        PageControl(
            "button",
            "rep.dry",
            copy.admin_toggle(config.dry_run, copy.ADMIN_REP_DRY_SUBJECT),
        ),
        PageControl("button", "rep.appeal", copy.ADMIN_REP_APPEAL_BTN),
    ]
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_REP_HITS_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = ctx.bot.reputation
    current = await service.show_config(ctx.guild_id)
    if control_id.startswith("rep.tier:"):
        try:
            tier = int(control_id.split(":", 1)[1])
            role_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        before = next((item.role_id for item in current.tiers if item.tier == tier), None)
        try:
            updated = await service.set_tier_role(ctx.guild_id, tier, role_id)
        except RoleBindOverlap as taken:
            owner = copy.role_subsystem_label(taken.owner.value)
            return AdminResult(ok=False, error=copy.config_role_taken(owner))
        field = next((_tier_field(item) for item in updated.tiers if item.tier == tier), copy.admin_rep_tier(tier))
        return AdminResult(ok=True, field=field, before=_role(before), after=_role(role_id))
    if control_id == "rep.hits":
        return AdminResult(ok=True, modal="text", target="rep.hits.set")
    if control_id == "rep.growth":
        voice = current.config.voice_seconds_per_point // 60
        return AdminResult(
            ok=True,
            modal="numbers",
            target="rep.growth.set",
            title=copy.ADMIN_REP_GROWTH_BTN,
            labels=(copy.CMD_REP_MESSAGES, copy.CMD_REP_VOICE),
            defaults=(str(current.config.messages_per_point), str(voice)),
        )
    if control_id == "rep.names":
        defaults = "\n".join(item.name for item in current.tiers) or "\n".join(copy.REP_TIER_DEFAULTS)
        return AdminResult(
            ok=True,
            modal="lines",
            target="rep.names.set",
            title=copy.ADMIN_REP_NAMES,
            labels=(copy.ADMIN_REP_NAMES_HINT,),
            defaults=(defaults,),
        )
    if control_id == "rep.appeal":
        return AdminResult(ok=True, modal="text", target="rep.appeal.set")
    if control_id == "rep.dry":
        updated = await service.update_config(ctx.guild_id, dry_run=not current.config.dry_run)
        if updated is None:
            return AdminResult(ok=False, error=copy.REP_CONFIG_BAD)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_REP_DRY,
            before=copy.yn(current.config.dry_run),
            after=copy.yn(updated.config.dry_run),
        )
    if control_id == "rep.hits.set":
        parsed = parse_int_group(value, count=7)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        updated = await service.update_config(
            ctx.guild_id,
            warn_hit=parsed[0],
            timeout_per_step=parsed[1],
            timeout_min=parsed[2],
            timeout_max=parsed[3],
            mute_hit=parsed[4],
            kick_hit=parsed[5],
            max_hit_per_hour=parsed[6],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.REP_CONFIG_BAD)
        before = (
            f"{current.config.warn_hit} {current.config.timeout_per_step} "
            f"{current.config.timeout_min} {current.config.timeout_max} "
            f"{current.config.mute_hit} {current.config.kick_hit} {current.config.max_hit_per_hour}"
        )
        after = " ".join(str(item) for item in parsed)
        return AdminResult(ok=True, field=copy.ADMIN_REP_HITS, before=before, after=after)
    if control_id == "rep.growth.set":
        parsed = parse_int_group(value.replace("\n", " "), count=2)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        updated = await service.update_config(
            ctx.guild_id,
            messages_per_point=parsed[0],
            voice_minutes=parsed[1],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.REP_CONFIG_BAD)
        before = f"{current.config.messages_per_point} / {current.config.voice_seconds_per_point // 60}"
        return AdminResult(ok=True, field=copy.ADMIN_REP_GROWTH, before=before, after=f"{parsed[0]} / {parsed[1]}")
    if control_id == "rep.names.set":
        updated = await service.set_tier_names(ctx.guild_id, value)
        if updated is None:
            return AdminResult(ok=False, error=copy.ADMIN_REP_NAMES_BAD)
        before = " · ".join(item.name for item in current.tiers)
        after = " · ".join(item.name for item in updated.tiers)
        return AdminResult(ok=True, field=copy.ADMIN_REP_NAMES, before=before, after=after)
    if control_id == "rep.appeal.set":
        updated = await service.update_config(ctx.guild_id, appeal_contact=value)
        if updated is None:
            return AdminResult(ok=False, error=copy.REP_CONFIG_BAD)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_REP_APPEAL,
            before=current.appeal_contact or "—",
            after=updated.appeal_contact or "—",
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_REPUTATION,
    copy.ADMIN_PAGE_REPUTATION,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"rep": REP_CONFIG},
)
