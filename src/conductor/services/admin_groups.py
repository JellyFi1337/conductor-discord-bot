"""Страница /admin «Группы»."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, CONTENT_MANAGE
from conductor.domain.admin import (
    PAGE_GROUPS,
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
    groups = getattr(ctx.bot, "groups", None)
    getter = getattr(groups, "hub_settings", None)
    if not callable(getter):
        return None
    return await getter(ctx.guild_id)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report

    return pack_report([], optional_key="groups")


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    settings = await _load(ctx)
    silence = 7 if settings is None else max(1, int(settings.silence_after.total_seconds() // 86400))
    archive_days = 30 if settings is None else int(settings.archive_days)
    reputation = 50 if settings is None else int(settings.min_reputation)
    daily = 1 if settings is None else int(settings.daily_limit)
    open_limit = 3 if settings is None else int(settings.open_limit)
    archive_id = None if settings is None else settings.archive_category_id
    return PageSnapshot(
        fields={
            copy.ADMIN_GROUPS_NUMBERS: copy.groups_numbers_line(silence, archive_days),
            copy.ADMIN_GROUPS_LIMITS: copy.groups_limits_line(reputation, daily, open_limit),
            copy.ADMIN_GROUPS_ARCHIVE_CAT: _mention_channel(archive_id),
        },
        controls=(
            PageControl("button", "groups.numbers", copy.ADMIN_GROUPS_NUMBERS_BTN),
            PageControl("button", "groups.limits", copy.ADMIN_GROUPS_LIMITS_BTN),
            PageControl("channel", "groups.archive", copy.ADMIN_GROUPS_ARCHIVE_CAT, channel_types=("category",)),
            PageControl("button", "groups.sweep", copy.ADMIN_GROUPS_SWEEP),
        ),
        footnote=copy.ADMIN_GROUPS_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    groups = getattr(ctx.bot, "groups", None)
    if groups is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id == "groups.numbers":
        settings = await _load(ctx)
        silence = 7 if settings is None else max(1, int(settings.silence_after.total_seconds() // 86400))
        archive_days = 30 if settings is None else int(settings.archive_days)
        return AdminResult(
            ok=True,
            modal="numbers",
            target="groups.numbers.set",
            title=copy.ADMIN_GROUPS_NUMBERS,
            labels=(copy.ADMIN_GROUPS_SILENCE, copy.ADMIN_GROUPS_ARCHIVE_DAYS),
            defaults=(str(silence), str(archive_days)),
        )
    if control_id == "groups.numbers.set":
        parsed = parse_int_group(value.replace("\n", " "), count=2)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        settings = await _load(ctx)
        before = (
            7 if settings is None else max(1, int(settings.silence_after.total_seconds() // 86400)),
            30 if settings is None else int(settings.archive_days),
        )
        await groups.set_numbers(ctx.guild_id, parsed[0], parsed[1])
        after = await _load(ctx)
        silence = parsed[0] if after is None else max(1, int(after.silence_after.total_seconds() // 86400))
        archive_days = parsed[1] if after is None else int(after.archive_days)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_GROUPS_NUMBERS,
            before=copy.groups_numbers_line(*before),
            after=copy.groups_numbers_line(silence, archive_days),
        )
    if control_id == "groups.limits":
        settings = await _load(ctx)
        reputation = 50 if settings is None else int(settings.min_reputation)
        daily = 1 if settings is None else int(settings.daily_limit)
        open_limit = 3 if settings is None else int(settings.open_limit)
        return AdminResult(
            ok=True,
            modal="numbers",
            target="groups.limits.set",
            title=copy.ADMIN_GROUPS_LIMITS,
            labels=(copy.ADMIN_GROUPS_REP, copy.ADMIN_GROUPS_DAILY, copy.ADMIN_GROUPS_OPEN),
            defaults=(str(reputation), str(daily), str(open_limit)),
        )
    if control_id == "groups.limits.set":
        parsed = parse_int_group(value.replace("\n", " "), count=3)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        settings = await _load(ctx)
        before = (
            50 if settings is None else int(settings.min_reputation),
            1 if settings is None else int(settings.daily_limit),
            3 if settings is None else int(settings.open_limit),
        )
        await groups.set_limits(ctx.guild_id, parsed[0], parsed[1], parsed[2])
        after = await _load(ctx)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_GROUPS_LIMITS,
            before=copy.groups_limits_line(*before),
            after=copy.groups_limits_line(
                parsed[0] if after is None else int(after.min_reputation),
                parsed[1] if after is None else int(after.daily_limit),
                parsed[2] if after is None else int(after.open_limit),
            ),
        )
    if control_id == "groups.archive":
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        before = await groups.bind_archive_category(ctx.guild_id, channel_id)
        return AdminResult(
            ok=True,
            field=copy.ADMIN_GROUPS_ARCHIVE_CAT,
            before=_mention_channel(before),
            after=_mention_channel(channel_id),
        )
    if control_id == "groups.sweep":
        if ctx.guild is None:
            return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
        await groups.sweep_hourly(now=None, guild_id=ctx.guild_id)
        return AdminResult(ok=True, field=copy.ADMIN_GROUPS_SWEEP, before="—", after=copy.ADMIN_GROUPS_SWEEP_DONE)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_GROUPS,
    copy.ADMIN_PAGE_GROUPS,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"groups": CONTENT_MANAGE},
)
