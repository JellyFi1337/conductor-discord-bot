"""Страница /admin «Поиск команды»."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_LFG,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_int_group,
)
from conductor.presentation import copy


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


async def _load(ctx: AdminCtx):
    lfg = getattr(ctx.bot, "lfg", None)
    getter = getattr(lfg, "settings", None)
    if not callable(getter):
        return None
    return await getter(ctx.guild_id)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import (
        channel_issue,
        member_access_issue,
        pack_report,
        ready_issue,
        verified_roles,
    )

    settings = await _load(ctx)
    channel_id = None if settings is None else getattr(settings, "channel_id", None)
    status = getattr(getattr(ctx.bot, "lfg", None), "card_status", None)
    live = callable(status) and await status(ctx.guild_id) == "live"
    issues = [channel_issue(ctx.guild, channel_id, fix_control="lfg.channel", skeleton_key="lfg")]
    if not live:
        issues.append(ready_issue("panel_stale", fix_control="lfg.post", skeleton_key="lfg"))
    issues.append(
        member_access_issue(
            ctx.guild,
            channel_id,
            roles=await verified_roles(ctx),
            need=("view_channel",),
            skeleton_key="lfg",
            fix_control="lfg.channel",
        )
    )
    return pack_report(issues, optional_key="lfg")


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    settings = await _load(ctx)
    ttl = getattr(settings, "ttl_minutes", 120)
    ping = getattr(settings, "ping_cooldown_minutes", 15)
    user = getattr(settings, "per_user_cooldown_minutes", 30)
    channel_id = None if settings is None else getattr(settings, "channel_id", None)
    return PageSnapshot(
        fields={
            copy.ADMIN_LFG_CHANNEL: _mention_channel(channel_id),
            copy.ADMIN_LFG_NUMBERS: copy.lfg_numbers_line(ttl, ping, user),
        },
        controls=(
            PageControl("channel", "lfg.channel", copy.ADMIN_LFG_CHANNEL, channel_types=("text",)),
            PageControl("button", "lfg.numbers", copy.ADMIN_LFG_NUMBERS_BTN),
            PageControl("button", "lfg.post", copy.ADMIN_LFG_POST),
        ),
        footnote=copy.ADMIN_LFG_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    lfg = getattr(ctx.bot, "lfg", None)
    if lfg is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id == "lfg.channel":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        before = await lfg.set_channel(ctx.guild_id, channel_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_LFG_CHANNEL,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "lfg.numbers":
        settings = await _load(ctx)
        ttl = getattr(settings, "ttl_minutes", 120)
        ping = getattr(settings, "ping_cooldown_minutes", 15)
        user = getattr(settings, "per_user_cooldown_minutes", 30)
        return AdminResult(
            ok=True,
            modal="numbers",
            target="lfg.numbers.set",
            title=copy.ADMIN_LFG_NUMBERS,
            labels=(copy.ADMIN_LFG_TTL, copy.ADMIN_LFG_PING, copy.ADMIN_LFG_USER),
            defaults=(str(ttl), str(ping), str(user)),
        )
    if control_id == "lfg.numbers.set":
        parsed = parse_int_group(value.replace("\n", " "), count=3)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        settings = await _load(ctx)
        before = (
            getattr(settings, "ttl_minutes", 120),
            getattr(settings, "ping_cooldown_minutes", 15),
            getattr(settings, "per_user_cooldown_minutes", 30),
        )
        await lfg.set_numbers(ctx.guild_id, parsed[0], parsed[1], parsed[2])
        after = await _load(ctx)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_LFG_NUMBERS,
            before=copy.lfg_numbers_line(*before),
            after=copy.lfg_numbers_line(
                getattr(after, "ttl_minutes", parsed[0]),
                getattr(after, "ping_cooldown_minutes", parsed[1]),
                getattr(after, "per_user_cooldown_minutes", parsed[2]),
            ),
        )
    if control_id == "lfg.post":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        settings = await _load(ctx)
        channel_id = None if settings is None else getattr(settings, "channel_id", None)
        getter = getattr(ctx.guild, "get_channel", None)
        channel = getter(int(channel_id)) if callable(getter) and channel_id else None
        if channel is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        posted = await lfg.post_starter(ctx.guild, channel, actor_id=ctx.actor_id)
        if not posted:
            return AdminResult(ok=False, error=copy.STALE_BODY)
        return AdminResult(ok=True, field=copy.ADMIN_LFG_POST, before="—", after=_mention_channel(channel_id))
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_LFG,
    copy.ADMIN_PAGE_LFG,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"lfg": CONTENT_MANAGE},
)
