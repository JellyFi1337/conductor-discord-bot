"""Страница /admin «Верификация»."""

from __future__ import annotations

from conductor.db.models import VerificationSettings
from conductor.db.session import session_scope
from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_VERIFICATION,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.presentation import copy
from conductor.services.role_broker import RoleBindOverlap, RoleSubsystem


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _mention_role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


def _channel(guild: object | None, channel_id: int | None):
    if guild is None or channel_id is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))


async def _load(ctx: AdminCtx) -> VerificationSettings:
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(VerificationSettings, ctx.guild_id)
        if row is None:
            row = VerificationSettings(guild_id=ctx.guild_id)
            session.add(row)
            await session.flush()
        return VerificationSettings(
            guild_id=int(row.guild_id),
            channel_id=row.channel_id,
            rules_channel_id=row.rules_channel_id,
            verified_role_id=row.verified_role_id,
            panel_id=row.panel_id,
            warning_message_id=row.warning_message_id,
        )


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import (
        channel_issue,
        member_access_issue,
        pack_report,
        ready_issue,
        role_issue,
    )

    row = await _load(ctx)
    everyone = None if ctx.guild is None else getattr(ctx.guild, "default_role", None)
    issues = [
        channel_issue(ctx.guild, row.channel_id, fix_control="vf.channel", skeleton_key="verify"),
        role_issue(ctx.guild, row.verified_role_id, fix_control="vf.role", skeleton_key="role_verified"),
    ]
    if not row.panel_id:
        issues.append(ready_issue("panel_stale", fix_control="vf.post", skeleton_key="verify"))
    if everyone is not None:
        issues.append(
            member_access_issue(
                ctx.guild,
                row.channel_id,
                roles=(everyone,),
                need=("view_channel", "send_messages"),
                skeleton_key="verify",
                fix_control="vf.channel",
            )
        )
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    row = await _load(ctx)
    return PageSnapshot(
        fields={
            copy.ADMIN_VF_CHANNEL: _mention_channel(row.channel_id),
            copy.ADMIN_VF_ROLE: _mention_role(row.verified_role_id),
        },
        controls=(
            PageControl("channel", "vf.channel", copy.ADMIN_VF_CHANNEL, channel_types=("text",)),
            PageControl("role", "vf.role", copy.ADMIN_VF_ROLE),
            PageControl("button", "vf.post", copy.ADMIN_VF_POST),
        ),
        footnote=copy.ADMIN_VF_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "vf.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        system_channel_id = getattr(ctx.guild, "system_channel_id", None)
        if system_channel_id is not None and int(system_channel_id) == channel_id:
            return AdminResult(ok=False, error=copy.ADMIN_VF_SYSTEM_CHANNEL)
        async with session_scope(ctx.bot.sessions) as session:
            row = await session.get(VerificationSettings, ctx.guild_id)
            if row is None:
                row = VerificationSettings(guild_id=ctx.guild_id)
                session.add(row)
            before = row.channel_id
            row.channel_id = channel_id
        await ctx.bot.verification.bind(ctx.guild_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_VF_CHANNEL,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "vf.role":
        try:
            role_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        owner = ctx.bot.roles.owner_of(ctx.guild_id, role_id, except_subsystem=RoleSubsystem.VERIFICATION)
        if owner is not None:
            return AdminResult(ok=False, error=copy.config_role_taken(copy.role_subsystem_label(owner.value)))
        try:
            ctx.bot.roles.bind(ctx.guild_id, RoleSubsystem.VERIFICATION, (role_id,), strict=True)
        except RoleBindOverlap as exc:
            return AdminResult(ok=False, error=copy.config_role_taken(copy.role_subsystem_label(exc.owner.value)))
        async with session_scope(ctx.bot.sessions) as session:
            row = await session.get(VerificationSettings, ctx.guild_id)
            if row is None:
                row = VerificationSettings(guild_id=ctx.guild_id)
                session.add(row)
            before = row.verified_role_id
            row.verified_role_id = role_id
        await ctx.bot.verification.bind(ctx.guild_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_VF_ROLE,
            before=_mention_role(before),
            after=_mention_role(role_id),
        )
    if control_id == "vf.post":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        posted = await ctx.bot.verification.post_cards(ctx.guild, actor_id=ctx.actor_id)
        if not posted:
            row = await _load(ctx)
            if not row.channel_id:
                return AdminResult(ok=False, error=copy.VERIFICATION_NEED_CHANNEL)
            if not row.verified_role_id:
                return AdminResult(ok=False, error=copy.VERIFICATION_NEED_ROLE)
            return AdminResult(ok=False, error=copy.STALE_BODY)
        return AdminResult(ok=True, field=copy.ADMIN_VF_POST, before="—", after=copy.ADMIN_VF_POST)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_VERIFICATION,
    copy.ADMIN_PAGE_VERIFICATION,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"vf": CONTENT_MANAGE},
)
