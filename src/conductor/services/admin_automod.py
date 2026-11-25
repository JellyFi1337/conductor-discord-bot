"""Страница /admin «Автомод»."""

from __future__ import annotations

from conductor.db.session import session_scope
from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_AUTOMOD,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.presentation import copy
from conductor.services.guild_config import get_or_create_config


def _mention(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _channel(guild: object | None, channel_id: int | None):
    if guild is None or channel_id is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))


async def _channel_id(ctx: AdminCtx) -> int | None:
    async with session_scope(ctx.bot.sessions) as session:
        row = await get_or_create_config(session, ctx.guild_id)
        return row.automod_channel_id


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import channel_issue, pack_report, ready_issue

    channel_id = await _channel_id(ctx)
    status = getattr(getattr(ctx.bot, "automod", None), "card_status", None)
    live = callable(status) and await status(ctx.guild_id) == "live"
    issues = [channel_issue(ctx.guild, channel_id, fix_control="automod.channel", skeleton_key="automod")]
    if not live:
        issues.append(ready_issue("panel_stale", fix_control="automod.post", skeleton_key="automod"))
    return pack_report(issues, optional_key="automod")


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    channel_id = await _channel_id(ctx)
    return PageSnapshot(
        fields={copy.ADMIN_AUTOMOD_CHANNEL: _mention(channel_id)},
        controls=(
            PageControl("channel", "automod.channel", copy.ADMIN_AUTOMOD_CHANNEL, channel_types=("text",)),
            PageControl("button", "automod.post", copy.ADMIN_AUTOMOD_POST),
            PageControl("button", "automod.reload", copy.ADMIN_AUTOMOD_RELOAD),
        ),
        footnote=copy.ADMIN_AUTOMOD_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id == "automod.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        async with session_scope(ctx.bot.sessions) as session:
            row = await get_or_create_config(session, ctx.guild_id)
            before = row.automod_channel_id
            row.automod_channel_id = channel_id
        await ctx.bot.refresh_config(ctx.guild_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_AUTOMOD_CHANNEL,
            before=_mention(before),
            after=_mention(channel_id),
        )
    if control_id == "automod.post":
        channel_id = await _channel_id(ctx)
        channel = _channel(ctx.guild, channel_id)
        if channel is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        posted = await ctx.bot.automod.post_card(ctx.guild, channel, actor_id=ctx.actor_id)
        if not posted:
            return AdminResult(ok=False, error=copy.STALE_BODY)
        return AdminResult(ok=True, field=copy.ADMIN_AUTOMOD_POST, before="—", after=_mention(channel_id))
    if control_id == "automod.reload":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        await ctx.bot.automod.reload_card(ctx.guild)
        return AdminResult(ok=True, field=copy.ADMIN_AUTOMOD_RELOAD, before="—", after=copy.AUTOMOD_RELOAD_OK)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_AUTOMOD,
    copy.ADMIN_PAGE_AUTOMOD,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"automod": CONTENT_MANAGE},
)
