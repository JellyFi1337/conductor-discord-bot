"""Страница /admin «Статистика»."""

from __future__ import annotations

from conductor.db.models import StatsBoard
from conductor.db.session import session_scope
from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_STATS,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.domain.stats_board import ROLE_CAP, parse_role_ids
from conductor.presentation import copy


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


def _mention_roles(role_ids: tuple[int, ...]) -> str:
    if not role_ids:
        return "—"
    return " ".join(f"<@&{role_id}>" for role_id in role_ids)


def _channel(guild: object | None, channel_id: int | None):
    if guild is None or channel_id is None:
        return None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    return getter(int(channel_id))


async def _load(ctx: AdminCtx) -> tuple[int | None, tuple[int, ...]]:
    async with session_scope(ctx.bot.sessions) as session:
        row = await session.get(StatsBoard, ctx.guild_id)
    if row is None:
        return None, ()
    return row.channel_id, parse_role_ids(row.roles_json)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import (
        channel_issue,
        member_access_issue,
        pack_report,
        ready_issue,
        verified_roles,
    )

    channel_id, _roles = await _load(ctx)
    status = getattr(ctx.bot.stats_board, "card_status", None)
    live = callable(status) and await status(ctx.guild_id) == "live"
    issues = [channel_issue(ctx.guild, channel_id, fix_control="stats.channel", skeleton_key="stats")]
    if not live:
        issues.append(ready_issue("panel_stale", fix_control="stats.post", skeleton_key="stats"))
    issues.append(
        member_access_issue(
            ctx.guild,
            channel_id,
            roles=await verified_roles(ctx),
            need=("view_channel",),
            skeleton_key="stats",
            fix_control="stats.channel",
        )
    )
    return pack_report(issues, optional_key="stats")


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    channel_id, role_ids = await _load(ctx)
    return PageSnapshot(
        fields={
            copy.ADMIN_STATS_CHANNEL: _mention_channel(channel_id),
            copy.ADMIN_STATS_ROLES: _mention_roles(role_ids),
        },
        controls=(
            PageControl("channel", "stats.channel", copy.ADMIN_STATS_CHANNEL, channel_types=("text",)),
            PageControl("role", "stats.roles", copy.ADMIN_STATS_ROLES, min_values=0, max_values=ROLE_CAP),
            PageControl("button", "stats.post", copy.ADMIN_STATS_POST),
        ),
        footnote=copy.ADMIN_STATS_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    board = getattr(ctx.bot, "stats_board", None)
    if board is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id == "stats.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        before = await board.set_channel(ctx.guild_id, channel_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_STATS_CHANNEL,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "stats.roles":
        wanted = parse_role_ids(value)
        before = await board.set_roles(ctx.guild_id, wanted)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_STATS_ROLES,
            before=_mention_roles(before),
            after=_mention_roles(wanted),
        )
    if control_id == "stats.post":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        channel_id, _roles = await _load(ctx)
        channel = _channel(ctx.guild, channel_id)
        if channel is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        posted = await board.post(ctx.guild, channel, actor_id=ctx.actor_id)
        if not posted:
            return AdminResult(ok=False, error=copy.STALE_BODY)
        return AdminResult(ok=True, field=copy.ADMIN_STATS_POST, before="—", after=_mention_channel(channel_id))
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_STATS,
    copy.ADMIN_PAGE_STATS,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"stats": CONTENT_MANAGE},
)
