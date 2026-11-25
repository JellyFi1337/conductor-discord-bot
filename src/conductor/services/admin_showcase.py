"""Страница /admin «Витрины»: статус, отправка, смена канала."""

from __future__ import annotations

from conductor.domain.access import CONFIG_READ, SHOWCASE_POST
from conductor.domain.admin import (
    PAGE_SHOWCASE,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    ReadyState,
)
from conductor.domain.showcase import SHOWCASES, ShowcaseState, required_showcases, showcase_by_id
from conductor.presentation import copy
from conductor.presentation.showcase import showcase_line

__all__ = ["PAGE"]


def _service(ctx: AdminCtx):
    return getattr(ctx.bot, "showcase", None)


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report, ready_issue

    service = _service(ctx)
    guild = ctx.guild
    if service is None or guild is None:
        return pack_report([ready_issue("empty")])
    states = await service.statuses(guild)
    issues = []
    live = 0
    for spec in required_showcases():
        state = states.get(spec.id, ShowcaseState.OFF)
        if state is ShowcaseState.LIVE:
            live += 1
            continue
        if state is ShowcaseState.OFF:
            issues.append(ready_issue("unbound", fix_control=f"show.where:{spec.id}", skeleton_key=spec.channel_key))
        else:
            issues.append(ready_issue("panel_stale", fix_control=f"show.post:{spec.id}", skeleton_key=spec.channel_key))
    if not issues:
        return pack_report(())
    if live:
        return pack_report(issues, state=ReadyState.PARTIAL)
    return pack_report(issues)


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    service = _service(ctx)
    guild = ctx.guild
    states = {} if service is None or guild is None else await service.statuses(guild)
    bound = {} if service is None else await service.bound_ids(ctx.guild_id)
    fields: dict[str, object] = {}
    controls: list[PageControl] = []
    for spec in SHOWCASES:
        state = states.get(spec.id, ShowcaseState.OFF)
        fields[spec.label] = showcase_line(state, bound.get(spec.channel_key))
        controls.append(PageControl("button", f"show.post:{spec.id}", copy.SHOWCASE_POST))
        controls.append(PageControl("button", f"show.refresh:{spec.id}", copy.SHOWCASE_REFRESH))
        controls.append(
            PageControl(
                "channel",
                f"show.where:{spec.id}",
                copy.SHOWCASE_WHERE,
                channel_types=("text",),
            )
        )
    controls.append(PageControl("button", "show.post_missing", copy.SHOWCASE_POST_MISSING))
    return PageSnapshot(fields=fields, controls=tuple(controls), footnote=copy.SHOWCASE_HINT)


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    service = _service(ctx)
    if service is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if ctx.guild is None:
        return AdminResult(ok=False, error=copy.ADMIN_NEED_GUILD)
    if control_id == "show.post_missing":
        posted = await service.post_missing(ctx.guild, actor_id=ctx.actor_id)
        failed = next((item for item in posted if not item.ok), None)
        if failed is not None:
            return AdminResult(ok=False, error=failed.error or copy.STALE_BODY)
        return AdminResult(
            ok=True,
            field=copy.SHOWCASE_POST_MISSING,
            before="—",
            after=str(len(posted)),
        )
    kind, _, showcase_id = control_id.partition(":")
    spec = showcase_by_id(showcase_id)
    if kind != "show" or spec is None:
        return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)
    if control_id.startswith("show.post:"):
        result = await service.post(ctx.guild, spec, actor_id=ctx.actor_id)
        if not result.ok:
            return AdminResult(ok=False, error=result.error or copy.STALE_BODY)
        return AdminResult(ok=True, field=spec.label, before="—", after=copy.SHOWCASE_POST)
    if control_id.startswith("show.refresh:"):
        result = await service.refresh(ctx.guild, spec, actor_id=ctx.actor_id)
        if not result.ok:
            return AdminResult(ok=False, error=result.error or copy.STALE_BODY)
        return AdminResult(ok=True, field=spec.label, before="—", after=copy.SHOWCASE_REFRESH)
    if control_id.startswith("show.where:"):
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return AdminResult(ok=False, error=copy.ADMIN_BAD_CHANNEL)
        before = await service.set_channel(ctx.guild, spec, channel_id)
        return AdminResult(
            ok=True,
            field=spec.label,
            before=_mention(before),
            after=_mention(channel_id),
        )
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


def _mention(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "—"


PAGE = AdminPage(
    PAGE_SHOWCASE,
    copy.ADMIN_PAGE_SHOWCASE,
    CONFIG_READ,
    _ready,
    _snapshot,
    _handle,
    keys={"show": SHOWCASE_POST},
)
