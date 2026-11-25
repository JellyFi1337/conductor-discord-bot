"""Страница /admin «Квоты»."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, MOD_SETTINGS, STAFF_RANKS, Rank
from conductor.domain.admin import (
    PAGE_QUOTAS,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
)
from conductor.domain.quotas import parse_quota_pair
from conductor.presentation import copy


def _field(rank: Rank) -> str:
    return copy.rank_label(rank.key)


async def _ready(_ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report

    return pack_report(())


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    table = await ctx.bot.moderation.quota_table(ctx.guild_id)
    fields = {}
    controls: list[PageControl] = []
    for rank in STAFF_RANKS:
        limits = table.for_rank(rank)
        fields[_field(rank)] = copy.quota_rank_limits(timeouts=limits.timeouts, bans=limits.bans)
        controls.append(PageControl("button", f"quota.{rank.key}", _field(rank)))
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.ADMIN_QUOTA_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    if control_id.startswith("quota.") and not control_id.endswith(".set"):
        key = control_id.removeprefix("quota.")
        try:
            rank = Rank[key.upper()]
        except KeyError:
            return AdminResult(ok=False, error=copy.ADMIN_QUOTA_BAD)
        table = await ctx.bot.moderation.quota_table(ctx.guild_id)
        limits = table.for_rank(rank)
        return AdminResult(
            ok=True,
            modal="numbers",
            target=f"quota.{rank.key}.set",
            title=_field(rank),
            labels=(copy.ADMIN_QUOTA_TIMEOUTS, copy.ADMIN_QUOTA_BANS),
            defaults=(str(limits.timeouts), str(limits.bans)),
        )
    if not control_id.endswith(".set"):
        return AdminResult(ok=False, error=copy.ADMIN_QUOTA_BAD)
    key = control_id.removeprefix("quota.").removesuffix(".set")
    try:
        rank = Rank[key.upper()]
    except KeyError:
        return AdminResult(ok=False, error=copy.ADMIN_QUOTA_BAD)
    parsed = parse_quota_pair(value)
    if parsed is None:
        return AdminResult(ok=False, error=copy.ADMIN_QUOTA_BAD)
    timeouts, bans = parsed
    table = await ctx.bot.moderation.quota_table(ctx.guild_id)
    before = table.for_rank(rank)
    await ctx.bot.moderation.set_quota_limits(ctx.guild_id, rank, timeouts=timeouts, bans=bans)
    return AdminResult(
        ok=True,
        field=_field(rank),
        before=copy.quota_rank_limits(timeouts=before.timeouts, bans=before.bans),
        after=copy.quota_rank_limits(timeouts=timeouts, bans=bans),
    )


PAGE = AdminPage(
    PAGE_QUOTAS,
    copy.ADMIN_PAGE_QUOTAS,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"quota": MOD_SETTINGS},
)
