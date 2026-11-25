"""Страница /admin «Роли по выбору»."""

from __future__ import annotations

from dataclasses import dataclass, replace

import discord

from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_SELFROLES,
    ROLE_CREATE_VALUE,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.domain.create_lock import BUCKET_ROLE
from conductor.domain.self_roles import DEFAULT_GENERAL_OPTIONS, OptionTag
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.services.access import cached_snapshot
from conductor.services.create_lock import hold_for, log_refused, note_success, trip
from conductor.services.role_broker import RoleSubsystem
from conductor.services.traffic import lane


@dataclass(frozen=True, slots=True)
class _Draft:
    menu_id: int | None = None
    pending_role_id: int | None = None


_DRAFTS: dict[int, _Draft] = {}


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _draft(guild_id: int) -> _Draft:
    return _DRAFTS.get(int(guild_id), _Draft())


def _store(guild_id: int, draft: _Draft) -> None:
    _DRAFTS[int(guild_id)] = draft


def _option_labels(options: object) -> str:
    labels = [item.label for item in options]
    return " · ".join(labels) if labels else "—"


def _defaults_for(tag: OptionTag) -> tuple[str, str, str, str]:
    label = next((name for name, known in DEFAULT_GENERAL_OPTIONS if known is tag), "")
    return (label, "", "", tag.value)


async def _begin_add(ctx: AdminCtx, value: str) -> AdminResult:
    service = ctx.bot.self_roles
    draft = _draft(ctx.guild_id)
    if ctx.guild is None:
        return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
    if draft.menu_id is None:
        return AdminResult(ok=False, error=copy.SELFROLES_NEED_MENU)
    try:
        role_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
    snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
    rejected = service.option_role_error(ctx.guild_id, role_id, guild=ctx.guild, snapshot=snapshot)
    if rejected:
        return AdminResult(ok=False, error=rejected)
    _store(ctx.guild_id, replace(draft, pending_role_id=role_id))
    return AdminResult(
        ok=True,
        modal="self_role",
        target="sr.add.set",
        defaults=_defaults_for(OptionTag.NONE),
    )


async def _create_named_role(ctx: AdminCtx, name: str) -> int | AdminResult:
    label = name.strip()
    if not label:
        return AdminResult(ok=False, error=copy.SELFROLES_BAD_LABEL)
    if ctx.guild is None:
        return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
    create = getattr(ctx.guild, "create_role", None)
    if not callable(create):
        return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
    hold = await hold_for(ctx.bot, ctx.guild_id, BUCKET_ROLE)
    if hold is not None:
        log_refused(ctx.guild_id, BUCKET_ROLE, hold, action="role")
        return AdminResult(ok=False, error=copy.seed_blocked(
            "role", hold.retry_after, source=str(hold.source), route=hold.route
        ))
    try:
        with lane(Lane.STAFF):
            role = await create(name=label[:100], reason=copy.REASON_SELF_ROLES)
    except (discord.HTTPException, discord.RateLimited) as exc:
        hold = trip(ctx.bot, ctx.guild_id, BUCKET_ROLE, exc)
        if hold is not None:
            log_refused(ctx.guild_id, BUCKET_ROLE, hold, action="role")
            return AdminResult(ok=False, error=copy.seed_blocked(
                "role", hold.retry_after, source=str(hold.source), route=hold.route
            ))
        return AdminResult(ok=False, error=copy.ADMIN_ROLE_CREATE_FAIL)
    try:
        role_id = int(getattr(role, "id", 0) or 0)
    except (TypeError, ValueError):
        role_id = 0
    if not role_id:
        return AdminResult(ok=False, error=copy.ADMIN_ROLE_CREATE_FAIL)
    note_success(ctx.bot, ctx.guild_id, BUCKET_ROLE)
    return role_id


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import (
        channel_issue,
        member_access_issue,
        pack_report,
        ready_issue,
        verified_roles,
    )

    menus = await ctx.bot.self_roles.menus(ctx.guild_id)
    posted = [menu for menu in menus if menu.channel_id and menu.panel_id]
    if not posted:
        channel_id = next((int(menu.channel_id) for menu in menus if menu.channel_id), None)
        if channel_id is None:
            return pack_report([ready_issue("unbound", fix_control="sr.post", skeleton_key="roles")])
        return pack_report([ready_issue("panel_stale", fix_control="sr.post", skeleton_key="roles")])
    issues = [
        channel_issue(ctx.guild, int(posted[0].channel_id), fix_control="sr.post", skeleton_key="roles"),
        member_access_issue(
            ctx.guild,
            int(posted[0].channel_id),
            roles=await verified_roles(ctx),
            need=("view_channel",),
            skeleton_key="roles",
            fix_control="sr.post",
        ),
    ]
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    service = ctx.bot.self_roles
    await service.ensure_defaults(ctx.guild_id)
    menus = await service.menus(ctx.guild_id)
    draft = _draft(ctx.guild_id)
    chosen = next((menu for menu in menus if int(menu.id) == int(draft.menu_id or 0)), None)
    if chosen is None and menus:
        chosen = menus[0]
        _store(ctx.guild_id, replace(draft, menu_id=int(chosen.id)))
        draft = _draft(ctx.guild_id)
    options = () if chosen is None else await service.options(int(chosen.id))
    fields = {
        copy.ADMIN_SR_MENU: chosen.title if chosen is not None else "—",
        copy.ADMIN_SR_ADD: _option_labels(options),
        copy.ADMIN_SR_CHANNEL: _mention_channel(None if chosen is None else chosen.channel_id),
    }
    controls: list[PageControl] = []
    if menus:
        controls.append(
            PageControl(
                "select",
                "sr.menu",
                copy.ADMIN_SR_MENU,
                options=tuple((menu.title, str(menu.id)) for menu in menus),
                current=None if chosen is None else str(chosen.id),
            )
        )
        exclude_ids = tuple(int(item.role_id) for item in options)
        from conductor.services.admin_pages import role_pick_control

        controls.append(
            await role_pick_control(
                ctx,
                "sr.add",
                copy.ADMIN_SR_ADD,
                subsystem=RoleSubsystem.SELFROLES,
                exclude_ids=exclude_ids,
                allow_create=True,
            )
        )
        if options:
            controls.append(
                PageControl(
                    "select",
                    "sr.remove",
                    copy.ADMIN_SR_REMOVE,
                    options=tuple((item.label, str(item.option_id)) for item in options),
                )
            )
        controls.append(
            PageControl("channel", "sr.post", copy.ADMIN_SR_POST, channel_types=("text",)),
        )
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_SR_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = ctx.bot.self_roles
    draft = _draft(ctx.guild_id)
    if control_id == "sr.menu":
        try:
            menu_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.SELFROLES_NEED_MENU)
        _store(ctx.guild_id, replace(draft, menu_id=menu_id, pending_role_id=None))
        return AdminResult(ok=True)
    if control_id == "sr.add":
        if value == ROLE_CREATE_VALUE:
            return AdminResult(ok=True, modal="text", target="sr.add.create")
        return await _begin_add(ctx, value)
    if control_id == "sr.add.create":
        created = await _create_named_role(ctx, value)
        if isinstance(created, AdminResult):
            return created
        return await _begin_add(ctx, str(created))
    if control_id == "sr.add.set":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        if draft.menu_id is None or draft.pending_role_id is None:
            return AdminResult(ok=False, error=copy.SELFROLES_NEED_MENU)
        parts = value.split("\n")
        label = parts[0] if parts else ""
        description = parts[1] if len(parts) > 1 else ""
        emoji = parts[2] if len(parts) > 2 else ""
        tag = parts[3] if len(parts) > 3 else "none"
        snapshot = await cached_snapshot(ctx.bot, ctx.guild_id)
        error = await service.add_option(
            ctx.guild_id,
            int(draft.menu_id),
            int(draft.pending_role_id),
            label=label,
            description=description,
            emoji=emoji,
            tag=tag,
            guild=ctx.guild,
            snapshot=snapshot,
        )
        if error:
            return AdminResult(ok=False, error=error)
        role_id = int(draft.pending_role_id)
        _store(ctx.guild_id, replace(draft, pending_role_id=None))
        return AdminResult(ok=True, field=copy.ADMIN_SR_ADD, before="—", after=f"<@&{role_id}>")
    if control_id == "sr.remove":
        try:
            option_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        error = await service.remove_option(ctx.guild_id, option_id)
        if error:
            return AdminResult(ok=False, error=error)
        return AdminResult(ok=True, field=copy.ADMIN_SR_REMOVE, before=str(option_id), after="—")
    if control_id == "sr.post":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        if draft.menu_id is None:
            return AdminResult(ok=False, error=copy.SELFROLES_NEED_MENU)
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        error = await service.post_menu(ctx.guild, int(draft.menu_id), channel_id, actor_id=ctx.actor_id)
        if error:
            return AdminResult(ok=False, error=error)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_SR_POST,
            before="—",
            after=_mention_channel(channel_id),
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_SELFROLES,
    copy.ADMIN_PAGE_SELFROLES,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"sr": CONTENT_MANAGE},
)
