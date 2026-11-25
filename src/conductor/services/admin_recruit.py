"""Страница /admin «Рекрутинг»."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_RECRUIT,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.presentation import copy


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import channel_issue, pack_report

    recruit = getattr(ctx.bot, "recruit", None)
    getter = getattr(recruit, "channel_id", None)
    if not callable(getter):
        return pack_report((), off=True)
    channel_id = await getter(ctx.guild_id)
    if channel_id is None:
        return pack_report((), off=True)
    return pack_report([channel_issue(ctx.guild, channel_id, fix_control="recruit.channel")])


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    recruit = getattr(ctx.bot, "recruit", None)
    getter = getattr(recruit, "channel_id", None)
    channel_id = await getter(ctx.guild_id) if callable(getter) else None
    return PageSnapshot(
        fields={copy.ADMIN_RECRUIT_CHANNEL: _mention_channel(channel_id)},
        controls=(PageControl("channel", "recruit.channel", copy.ADMIN_RECRUIT_CHANNEL, channel_types=("text",)),),
        footnote=copy.ADMIN_RECRUIT_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    recruit = getattr(ctx.bot, "recruit", None)
    if recruit is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id != "recruit.channel":
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    try:
        channel_id = int(value)
    except (TypeError, ValueError):
        return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
    blocked = await recruit.channel_error(ctx.guild_id, channel_id)
    if blocked:
        return AdminResult(ok=False, error=blocked)
    before = await recruit.set_channel(ctx.guild_id, channel_id)
    return AdminResult(
        ok=True,
        field=copy.ADMIN_RECRUIT_CHANNEL,
        before=_mention_channel(before),
        after=_mention_channel(channel_id),
    )


PAGE = AdminPage(
    PAGE_RECRUIT,
    copy.ADMIN_PAGE_RECRUIT,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"recruit": CONTENT_MANAGE},
)
