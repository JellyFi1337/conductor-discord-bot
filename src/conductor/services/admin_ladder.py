"""Страница /admin «Лестница»."""

from __future__ import annotations

from conductor.domain.access import ACTIVITY_CONFIG
from conductor.domain.admin import (
    PAGE_LADDER,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_int_group,
)
from conductor.domain.ladder import STEPS
from conductor.presentation import copy


def _role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, role_issue

    panel = await ctx.bot.ladder.show_config(ctx.guild_id)
    issues = [
        role_issue(
            ctx.guild,
            panel.roles.for_step(step),
            fix_control=f"ladder.{step.key}",
            skeleton_key=f"role_{step.key}",
        )
        for step in STEPS
    ]
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    panel = await ctx.bot.ladder.show_config(ctx.guild_id)
    config = panel.config
    roles = panel.roles
    fields = {copy.ladder_step(step): _role(roles.for_step(step)) for step in STEPS}
    fields[copy.ADMIN_LADDER_REGULAR] = copy.admin_ladder_regular_line(
        messages=config.regular_messages,
        voice_minutes=config.regular_voice_seconds // 60,
        reactions=config.regular_reactions,
    )
    fields[copy.ADMIN_LADDER_RESIDENT] = copy.admin_ladder_resident_line(
        messages=config.resident_messages,
        voice_hours=config.resident_voice_seconds // 3600,
        days=config.resident_days,
        strong_messages=config.resident_strong_messages,
        strong_voice_hours=config.resident_strong_voice_seconds // 3600,
    )
    fields[copy.ADMIN_LADDER_FRIEND] = copy.admin_ladder_friend_line(
        days=config.friend_tenure_days,
        messages=config.friend_messages,
        voice_hours=config.friend_voice_seconds // 3600,
        reactions=config.friend_reactions,
    )
    bound = {step: roles.for_step(step) for step in STEPS}
    from conductor.services.admin_pages import role_pick_control
    from conductor.services.role_broker import RoleSubsystem

    controls = []
    for step in STEPS:
        others = tuple(int(role_id) for other, role_id in bound.items() if other is not step and role_id)
        current = bound[step]
        controls.append(
            await role_pick_control(
                ctx,
                f"ladder.{step.key}",
                copy.ladder_step(step),
                subsystem=RoleSubsystem.ACTIVITY,
                exclude_ids=others,
                current=None if current is None else str(int(current)),
            )
        )
    controls = [
        *controls,
        PageControl("button", "ladder.regular_group", copy.ADMIN_LADDER_REGULAR_BTN),
        PageControl("button", "ladder.resident_group", copy.ADMIN_LADDER_RESIDENT_BTN),
        PageControl("button", "ladder.friend_group", copy.ADMIN_LADDER_FRIEND_BTN),
    ]
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_LADDER_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = ctx.bot.ladder
    current = await service.show_config(ctx.guild_id)
    if control_id == "ladder.regular_group":
        config = current.config
        return AdminResult(
            ok=True,
            modal="numbers",
            target="ladder.regular_group.set",
            title=copy.ADMIN_LADDER_REGULAR,
            labels=(
                copy.CMD_ACTIVITY_REG_MSG,
                copy.CMD_ACTIVITY_REG_VOICE,
                copy.CMD_ACTIVITY_REG_REACT,
            ),
            defaults=(
                str(config.regular_messages),
                str(config.regular_voice_seconds // 60),
                str(config.regular_reactions),
            ),
        )
    if control_id == "ladder.resident_group":
        config = current.config
        return AdminResult(
            ok=True,
            modal="numbers",
            target="ladder.resident_group.set",
            title=copy.ADMIN_LADDER_RESIDENT,
            labels=(
                copy.CMD_ACTIVITY_RES_MSG,
                copy.CMD_ACTIVITY_RES_VOICE,
                copy.CMD_ACTIVITY_RES_DAYS,
                copy.CMD_ACTIVITY_RES_STRONG_MSG,
                copy.CMD_ACTIVITY_RES_STRONG_VOICE,
            ),
            defaults=(
                str(config.resident_messages),
                str(config.resident_voice_seconds // 3600),
                str(config.resident_days),
                str(config.resident_strong_messages),
                str(config.resident_strong_voice_seconds // 3600),
            ),
        )
    if control_id == "ladder.friend_group":
        config = current.config
        return AdminResult(
            ok=True,
            modal="numbers",
            target="ladder.friend_group.set",
            title=copy.ADMIN_LADDER_FRIEND_BTN,
            labels=(
                copy.CMD_ACTIVITY_FRIEND_DAYS,
                copy.CMD_ACTIVITY_FRIEND_MSG,
                copy.CMD_ACTIVITY_FRIEND_VOICE,
                copy.CMD_ACTIVITY_FRIEND_REACT,
            ),
            defaults=(
                str(config.friend_tenure_days),
                str(config.friend_messages),
                str(config.friend_voice_seconds // 3600),
                str(config.friend_reactions),
            ),
        )
    if control_id.startswith("ladder.") and control_id.split(".", 1)[1] in {step.key for step in STEPS}:
        token = control_id.removeprefix("ladder.")
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        from conductor.services.admin_pages import owned_pick_error
        from conductor.services.role_broker import RoleSubsystem

        rejected = owned_pick_error(ctx, role_id, RoleSubsystem.ACTIVITY)
        if rejected:
            return AdminResult(ok=False, error=rejected)
        kwargs = {f"{token}_role_id": role_id}
        updated = await service.update_config(ctx.guild_id, **kwargs)
        if updated is None:
            return AdminResult(ok=False, error=copy.LADDER_CONFIG_BAD)
        step = next(item for item in STEPS if item.key == token)
        return AdminResult(
            ok=True,
            field=copy.ladder_step(step),
            before=_role(current.roles.for_step(step)),
            after=_role(role_id),
        )
    if control_id == "ladder.regular_group.set":
        parsed = parse_int_group(value.replace("\n", " "), count=3)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        updated = await service.update_config(
            ctx.guild_id,
            regular_messages=parsed[0],
            regular_voice_minutes=parsed[1],
            regular_reactions=parsed[2],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.LADDER_CONFIG_BAD)
        before = (
            f"{current.config.regular_messages} {current.config.regular_voice_seconds // 60} "
            f"{current.config.regular_reactions}"
        )
        return AdminResult(
            ok=True,
            field=copy.ADMIN_LADDER_REGULAR,
            before=before,
            after=" ".join(str(item) for item in parsed),
        )
    if control_id == "ladder.resident_group.set":
        parsed = parse_int_group(value.replace("\n", " "), count=5)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        updated = await service.update_config(
            ctx.guild_id,
            resident_messages=parsed[0],
            resident_voice_hours=parsed[1],
            resident_days=parsed[2],
            resident_strong_messages=parsed[3],
            resident_strong_voice_hours=parsed[4],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.LADDER_CONFIG_BAD)
        before = (
            f"{current.config.resident_messages} {current.config.resident_voice_seconds // 3600} "
            f"{current.config.resident_days} {current.config.resident_strong_messages} "
            f"{current.config.resident_strong_voice_seconds // 3600}"
        )
        return AdminResult(
            ok=True,
            field=copy.ADMIN_LADDER_RESIDENT,
            before=before,
            after=" ".join(str(item) for item in parsed),
        )
    if control_id == "ladder.friend_group.set":
        parsed = parse_int_group(value.replace("\n", " "), count=4)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        updated = await service.update_config(
            ctx.guild_id,
            friend_days=parsed[0],
            friend_messages=parsed[1],
            friend_voice_hours=parsed[2],
            friend_reactions=parsed[3],
        )
        if updated is None:
            return AdminResult(ok=False, error=copy.LADDER_CONFIG_BAD)
        before = (
            f"{current.config.friend_tenure_days} {current.config.friend_messages} "
            f"{current.config.friend_voice_seconds // 3600} {current.config.friend_reactions}"
        )
        return AdminResult(
            ok=True,
            field=copy.ADMIN_LADDER_FRIEND_BTN,
            before=before,
            after=" ".join(str(item) for item in parsed),
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_LADDER,
    copy.ADMIN_PAGE_LADDER,
    ACTIVITY_CONFIG,
    _ready,
    _snapshot,
    _handle,
    keys={"ladder": ACTIVITY_CONFIG},
)
