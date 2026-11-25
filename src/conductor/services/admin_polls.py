"""Страница /admin «Опросы»."""

from __future__ import annotations

from conductor.domain.access import POLL_MANAGE
from conductor.domain.admin import (
    PAGE_POLLS,
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


def _mention_role(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "—"


async def _load(ctx: AdminCtx) -> tuple[int | None, int | None]:
    polls = getattr(ctx.bot, "polls", None)
    getter = getattr(polls, "settings", None)
    if not callable(getter):
        return None, None
    return await getter(ctx.guild_id)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import channel_issue, pack_report

    channel_id, _role_id = await _load(ctx)
    return pack_report(
        [channel_issue(ctx.guild, channel_id, fix_control="polls.channel", skeleton_key="polls")],
        optional_key="polls",
    )


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    channel_id, role_id = await _load(ctx)
    return PageSnapshot(
        fields={
            copy.ADMIN_POLLS_CHANNEL: _mention_channel(channel_id),
            copy.ADMIN_POLLS_ROLE: _mention_role(role_id),
        },
        controls=(
            PageControl("channel", "polls.channel", copy.ADMIN_POLLS_CHANNEL, channel_types=("text",)),
            PageControl("role", "polls.role", copy.ADMIN_POLLS_ROLE, min_values=0, max_values=1),
        ),
        footnote=copy.ADMIN_POLLS_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    polls = getattr(ctx.bot, "polls", None)
    if polls is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id == "polls.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        before = await polls.set_channel(ctx.guild_id, channel_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_POLLS_CHANNEL,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "polls.role":
        raw = value.strip()
        if not raw:
            role_id = None
        else:
            try:
                role_id = int(raw.split()[0])
            except (TypeError, ValueError):
                return AdminResult(ok=False, error=copy.ADMIN_BAD_ROLE)
        before = await polls.set_ping_role(ctx.guild_id, role_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_POLLS_ROLE,
            before=_mention_role(before),
            after=_mention_role(role_id),
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_POLLS,
    copy.ADMIN_PAGE_POLLS,
    POLL_MANAGE,
    _ready,
    _snapshot,
    _handle,
    keys={"polls": POLL_MANAGE},
)
