"""Страница /admin «Достижения»."""

from __future__ import annotations

from conductor.domain.access import ACHIEVEMENTS_MANAGE, CONFIG_READ
from conductor.domain.achievements import KEYS, defs_for, parse_count_reason, parse_key
from conductor.domain.admin import (
    PAGE_ACHIEVEMENTS,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.presentation import copy
from conductor.services.role_broker import RoleBindOverlap

_PAGES = len(KEYS)


def _role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


def _mention(user_id: int | None) -> str:
    return f"<@{int(user_id)}>" if user_id else "—"


def _leaf(value: int) -> int:
    return min(max(1, int(value)), _PAGES)


def _leaf_of(control_id: str, default: int = 1) -> int:
    if ":" not in control_id:
        return default
    try:
        return _leaf(int(control_id.rsplit(":", 1)[1]))
    except ValueError:
        return default


def _key_at(leaf: int) -> str:
    return KEYS[_leaf(leaf) - 1]


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, ready_issue, role_issue

    panel = await ctx.bot.achievements.show_config(ctx.guild_id)
    if not panel.defs:
        return pack_report([ready_issue("empty")])
    issues = [role_issue(ctx.guild, item.role_id) for item in panel.defs]
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    current = _leaf(leaf)
    key = _key_at(current)
    panel = await ctx.bot.achievements.show_config(ctx.guild_id)
    rows = defs_for(key, panel.defs)
    picked = ctx.bot.achievements.picked_user(ctx.guild_id, ctx.actor_id)
    fields: dict[str, object] = {
        copy.ADMIN_ACHIEVE_USER: _mention(picked),
        copy.ADMIN_ACHIEVE_KEY: copy.achievement_key(key),
    }
    if picked is not None:
        card = await ctx.bot.achievements.profile(ctx.guild_id, picked)
        line = next((item for item in card.lines if item.key == key), None)
        if line is not None:
            fields[copy.ADMIN_ACHIEVE_COUNT] = line.count
    for row in rows:
        fields[row.name] = _role(row.role_id)
    from conductor.services.admin_pages import role_pick_control
    from conductor.services.role_broker import RoleSubsystem

    taken = tuple(int(row.role_id) for row in rows if row.role_id)
    controls: list[PageControl] = [
        PageControl("user", f"achieve.user:{current}", copy.ADMIN_ACHIEVE_USER),
    ]
    for row in rows:
        others = tuple(role_id for role_id in taken if role_id != int(row.role_id or 0))
        controls.append(
            await role_pick_control(
                ctx,
                f"achieve.role:{row.key}:{row.stage}:{current}",
                row.name,
                subsystem=RoleSubsystem.ACHIEVEMENTS,
                exclude_ids=others,
                current=None if row.role_id is None else str(int(row.role_id)),
            )
        )
    if current > 1:
        controls.append(PageControl("button", f"achieve.prev:{current - 1}", copy.ADMIN_PREV))
    if current < _PAGES:
        controls.append(PageControl("button", f"achieve.next:{current + 1}", copy.ADMIN_NEXT))
    controls.append(PageControl("button", f"achieve.count:{current}", copy.ADMIN_ACHIEVE_COUNT_BTN))
    controls.append(PageControl("button", f"achieve.names:{current}", copy.ADMIN_ACHIEVE_NAMES))
    return PageSnapshot(
        fields=fields,
        controls=tuple(controls),
        footnote=copy.listing_page(current, _PAGES) + " · " + copy.ADMIN_ACHIEVE_HINT,
        leaf=current,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = ctx.bot.achievements
    leaf = _leaf_of(control_id)
    key = _key_at(leaf)
    if control_id.startswith("achieve.prev:") or control_id.startswith("achieve.next:"):
        return AdminResult(ok=True, leaf=leaf)
    if control_id.startswith("achieve.user:"):
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_USER, leaf=leaf)
        before = service.picked_user(ctx.guild_id, ctx.actor_id)
        service.remember_user(ctx.guild_id, ctx.actor_id, user_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_ACHIEVE_USER,
            before=_mention(before),
            after=_mention(user_id),
            leaf=leaf,
        )
    if control_id.startswith("achieve.role:"):
        payload = control_id.removeprefix("achieve.role:").split(":")
        token = parse_key(payload[0] if payload else "")
        try:
            stage = int(payload[1]) if len(payload) > 1 else 0
            role_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE, leaf=leaf)
        if token is None:
            return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)
        current = await service.show_config(ctx.guild_id)
        before = next((item.role_id for item in current.defs if item.key == token and item.stage == stage), None)
        try:
            updated = await service.set_role(ctx.guild_id, token, stage, role_id)
        except RoleBindOverlap as taken:
            owner = copy.role_subsystem_label(taken.owner.value)
            return AdminResult(ok=False, error=copy.config_role_taken(owner), leaf=leaf)
        field = next((item.name for item in updated.defs if item.key == token and item.stage == stage), token)
        return AdminResult(ok=True, field=field, before=_role(before), after=_role(role_id), leaf=leaf)
    if control_id.startswith("achieve.count:") and not control_id.startswith("achieve.count.set:"):
        user_id = service.picked_user(ctx.guild_id, ctx.actor_id)
        if user_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_USER, leaf=leaf)
        card = await service.profile(ctx.guild_id, user_id)
        line = next((item for item in card.lines if item.key == key), None)
        return AdminResult(
            ok=True,
            modal="achieve_count",
            target=f"achieve.count.set:{leaf}",
            title=copy.ADMIN_ACHIEVE_COUNT_BTN,
            labels=(copy.ADMIN_ACHIEVE_COUNT, copy.ADMIN_ACHIEVE_REASON),
            defaults=(str(0 if line is None else line.count),),
            leaf=leaf,
        )
    if control_id.startswith("achieve.count.set:"):
        user_id = service.picked_user(ctx.guild_id, ctx.actor_id)
        if user_id is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_USER, leaf=leaf)
        parsed = parse_count_reason(value)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER, leaf=leaf)
        count, reason = parsed
        card = await service.profile(ctx.guild_id, user_id)
        before = next((item.count for item in card.lines if item.key == key), 0)
        updated = await service.set_count(ctx.guild_id, user_id, key, count, reason=reason)
        if updated is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_NUMBER, leaf=leaf)
        after = next((item.count for item in updated.lines if item.key == key), count)
        shown = str(after) if not reason else f"{after} · {reason}"
        return AdminResult(
            ok=True,
            field=copy.ADMIN_ACHIEVE_COUNT,
            before=str(before),
            after=shown,
            leaf=leaf,
        )
    if control_id.startswith("achieve.names:") and not control_id.startswith("achieve.names.set:"):
        current = await service.show_config(ctx.guild_id)
        defaults = "\n".join(item.name for item in current.defs) or "\n".join(
            name for names in copy.ACHIEVEMENT_DEFAULTS.values() for name in names
        )
        return AdminResult(
            ok=True,
            modal="lines",
            target=f"achieve.names.set:{leaf}",
            title=copy.ADMIN_ACHIEVE_NAMES,
            labels=(copy.ADMIN_ACHIEVE_NAMES_HINT,),
            defaults=(defaults,),
            leaf=leaf,
        )
    if control_id.startswith("achieve.names.set:"):
        current = await service.show_config(ctx.guild_id)
        updated = await service.set_names(ctx.guild_id, value)
        if updated is None:
            return AdminResult(ok=False, error=copy.ADMIN_ACHIEVE_NAMES_BAD, leaf=leaf)
        before = " · ".join(item.name for item in current.defs)
        after = " · ".join(item.name for item in updated.defs)
        return AdminResult(ok=True, field=copy.ADMIN_ACHIEVE_NAMES, before=before, after=after, leaf=leaf)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY, leaf=leaf)


PAGE = AdminPage(
    PAGE_ACHIEVEMENTS,
    copy.ADMIN_PAGE_ACHIEVEMENTS,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"achieve": ACHIEVEMENTS_MANAGE},
)
