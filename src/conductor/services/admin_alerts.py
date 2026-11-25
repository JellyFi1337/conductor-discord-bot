"""Страница /admin «Сигналы»."""

from __future__ import annotations

from conductor.domain.access import PULSE_ALERTS
from conductor.domain.admin import (
    PAGE_ALERTS,
    AdminCtx,
    AdminPage,
    AdminResult,
    PageControl,
    PageSnapshot,
    ReadyReport,
    parse_int_group,
)
from conductor.domain.pulse import AlertConfigVerdict
from conductor.presentation import copy


def _error(verdict: AlertConfigVerdict) -> str:
    if verdict is AlertConfigVerdict.BAD_VOICE:
        return copy.PULSE_ALERTS_BAD_VOICE
    if verdict is AlertConfigVerdict.BAD_LEAVES:
        return copy.PULSE_ALERTS_BAD_LEAVES
    if verdict is AlertConfigVerdict.BAD_AUTHORS:
        return copy.PULSE_ALERTS_BAD_AUTHORS
    if verdict is AlertConfigVerdict.BAD_JOINS:
        return copy.PULSE_ALERTS_BAD_JOINS
    return copy.CONFIG_DENIED


async def _ready(ctx: AdminCtx) -> ReadyReport:
    from conductor.services.admin_pages import pack_report

    rules = await ctx.bot.pulse.alert_rules(ctx.guild_id)
    if not rules.enabled:
        return pack_report((), off=True)
    return pack_report(())


async def _snapshot(ctx: AdminCtx, leaf: int) -> PageSnapshot:
    _ = leaf
    rules = await ctx.bot.pulse.alert_rules(ctx.guild_id)
    return PageSnapshot(
        fields={
            copy.ADMIN_ALERTS_STATE: copy.yn(rules.enabled),
            copy.ADMIN_ALERTS_VOICE: rules.voice_floor,
            copy.ADMIN_ALERTS_LEAVE: rules.leave_floor,
            copy.ADMIN_ALERTS_AUTHORS: f"{rules.authors_pct}%",
            copy.ADMIN_ALERTS_JOINS: rules.join_spike_floor,
        },
        controls=(
            PageControl(
                "button",
                "alerts.toggle",
                copy.admin_toggle(rules.enabled, copy.ADMIN_ALERTS_SUBJECT),
            ),
            PageControl("button", "alerts.floors", copy.ADMIN_ALERTS_FLOORS),
        ),
        footnote=copy.ADMIN_ALERTS_HINT,
    )


async def _handle(ctx: AdminCtx, control_id: str, value: str) -> AdminResult:
    current = await ctx.bot.pulse.alert_rules(ctx.guild_id)
    if control_id == "alerts.toggle":
        verdict, rules = await ctx.bot.pulse.set_alerts(ctx.guild_id, enabled=not current.enabled)
        if verdict is not AlertConfigVerdict.OK:
            return AdminResult(ok=False, error=_error(verdict))
        return AdminResult(
            ok=True,
            field=copy.ADMIN_ALERTS_STATE,
            before=copy.yn(current.enabled),
            after=copy.yn(rules.enabled),
        )
    if control_id == "alerts.floors":
        return AdminResult(
            ok=True,
            modal="numbers",
            target="alerts.floors.set",
            title=copy.ADMIN_ALERTS_FLOORS,
            labels=(
                copy.CMD_PULSE_VOICE_FLOOR,
                copy.CMD_PULSE_LEAVE_FLOOR,
                copy.CMD_PULSE_AUTHORS_PCT,
                copy.CMD_PULSE_JOIN_SPIKE,
            ),
            defaults=(
                str(current.voice_floor),
                str(current.leave_floor),
                str(current.authors_pct),
                str(current.join_spike_floor),
            ),
        )
    if control_id == "alerts.floors.set":
        parsed = parse_int_group(value.replace("\n", " "), count=4)
        if parsed is None:
            return AdminResult(ok=False, error=copy.ADMIN_BAD_GROUP)
        verdict, rules = await ctx.bot.pulse.set_alerts(
            ctx.guild_id,
            enabled=current.enabled,
            voice_floor=parsed[0],
            leave_floor=parsed[1],
            authors_pct=parsed[2],
            join_spike_floor=parsed[3],
        )
        if verdict is not AlertConfigVerdict.OK:
            return AdminResult(ok=False, error=_error(verdict))
        before = f"{current.voice_floor} {current.leave_floor} {current.authors_pct} {current.join_spike_floor}"
        after = f"{rules.voice_floor} {rules.leave_floor} {rules.authors_pct} {rules.join_spike_floor}"
        return AdminResult(ok=True, field=copy.ADMIN_ALERTS_FLOORS, before=before, after=after)
    return AdminResult(ok=False, error=copy.CONFIG_UNKNOWN_KEY)


PAGE = AdminPage(
    PAGE_ALERTS,
    copy.ADMIN_PAGE_ALERTS,
    PULSE_ALERTS,
    _ready,
    _snapshot,
    _handle,
    keys={"alerts": PULSE_ALERTS},
)
