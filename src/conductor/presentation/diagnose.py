"""Карточка /diagnose: вкладки «настройка», «каркас», «готовность» и «сейчас». Без sqlalchemy и без services."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import discord
from discord import ui

from conductor import __version__
from conductor.domain.create_lock import BUCKETS
from conductor.domain.observe import TAB_NOW, TAB_READY, TAB_SETUP, TAB_SKELETON
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.domain.seed import (
    FEATURES,
    SKELETON,
    FeatureVerdict,
    SkeletonCheck,
    SkeletonStatus,
    entry_by_key,
    feature_verdicts,
    skeleton_entries,
)
from conductor.domain.traffic import INVALID_BUDGET, GovernorSnapshot, TrafficSnapshot
from conductor.presentation import copy
from conductor.presentation.icons import render_icon
from conductor.presentation.interact import acked, instant
from conductor.presentation.layout import collected_text, panel
from conductor.presentation.motion import EMOJI_PACK_SIZE, Countdown
from conductor.presentation.tokens import Icon, Tone

__all__ = [
    "CheckStatus",
    "DiagnoseLine",
    "DiagnoseSnapshot",
    "CreateHoldView",
    "RateObservedView",
    "LogChannelView",
    "ManagedRoleView",
    "apply_defaults_confirm",
    "restore_perms_confirm",
    "build_diagnose_lines",
    "build_now_lines",
    "build_ready_lines",
    "build_setup_lines",
    "build_skeleton_lines",
    "diagnose_card",
    "ready_needs_fix",
    "render_diagnose",
    "skeleton_page_count",
    "pack_create_holds",
    "pack_observer",
    "pack_rate_observed",
    "SELECT_PAGE_SIZE",
    "SKELETON_PAGE_SIZE",
]


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


_MARK = {
    CheckStatus.OK: Icon.DONE,
    CheckStatus.WARN: Icon.WARN,
    CheckStatus.FAIL: Icon.DENY,
}

TabSwitch = Callable[[discord.Interaction, str], Awaitable[None]]
ApplyClick = Callable[[discord.Interaction], Awaitable[None]]
RepairClick = Callable[[discord.Interaction, str], Awaitable[None]]
PageClick = Callable[[discord.Interaction, int], Awaitable[None]]

SKELETON_PAGE_SIZE = 15
SELECT_PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class DiagnoseLine:
    status: CheckStatus
    title: str
    action: str = ""


@dataclass(frozen=True, slots=True)
class LogChannelView:
    configured: bool
    name: str | None
    can_send: bool
    can_embed: bool


@dataclass(frozen=True, slots=True)
class ManagedRoleView:
    label: str
    position: int | None


@dataclass(frozen=True, slots=True)
class CreateHoldView:
    bucket: str
    source: str
    remaining: float
    persisted: bool


@dataclass(frozen=True, slots=True)
class RateObservedView:
    bucket: str
    status: int
    scope: str
    remaining: int | None
    reset_after: float | None
    age: float


def pack_create_holds(packed: Sequence[tuple[Any, bool]], *, now: float) -> tuple[CreateHoldView, ...]:
    return tuple(
        CreateHoldView(
            bucket=str(hold.bucket),
            source=str(hold.source),
            remaining=float(hold.remaining(now)),
            persisted=bool(persisted),
        )
        for hold, persisted in packed
    )


def pack_rate_observed(items: Sequence[Any], *, now: float) -> tuple[RateObservedView, ...]:
    return tuple(
        RateObservedView(
            bucket=str(item.bucket),
            status=int(item.status),
            scope=str(item.scope or ""),
            remaining=item.remaining,
            reset_after=item.reset_after,
            age=max(0.0, now - float(item.at)),
        )
        for item in items
    )


def pack_observer(
    observer: Any, guild_id: int, *, now: float
) -> tuple[tuple[RateObservedView, ...], TrafficSnapshot | None]:
    if observer is None:
        return (), None
    lookup = getattr(observer, "last", None)
    items: list[Any] = []
    if callable(lookup):
        for bucket in BUCKETS:
            found = lookup(int(guild_id), bucket)
            if found is not None:
                items.append(found)
    snapper = getattr(observer, "snapshot", None)
    traffic = snapper() if callable(snapper) else None
    if not isinstance(traffic, TrafficSnapshot):
        traffic = None
    return pack_rate_observed(items, now=now), traffic


@dataclass(frozen=True, slots=True)
class DiagnoseSnapshot:
    members_intent: bool
    message_content_intent: bool
    members_loaded: int
    member_count: int | None
    permissions: Mapping[str, bool]
    bot_top_position: int | None
    managed_roles: tuple[ManagedRoleView, ...]
    user_log: LogChannelView
    admin_log: LogChannelView
    latency_ms: int | None
    log_backlog: int
    log_sent: int
    log_failed: int
    log_dropped: int
    log_pump_alive: bool
    unread_count: int
    unread_names: tuple[str, ...]
    reconnects_hour: int = 0
    emoji_loaded: int = 0
    emoji_total: int = EMOJI_PACK_SIZE
    cache_hits: int = 0
    cache_misses: int = 0
    cache_size: int = 0
    message_cache_size: int = 0
    outbound_depth: int = 0
    outbound_limit: int = 8
    outbound_waiting: int = 0
    limiter_trips: int = 0
    rep_at_threshold: int = 0
    rep_would_ban: int = 0
    ladder_roles: tuple[ManagedRoleView, ...] = ()
    queue_jobs: int = 0
    queue_later: int = 0
    bg_tasks: int = 0
    scheduler_jobs: int = 0
    census_at: datetime | None = None
    db_ok: bool = True
    broker_streak: int = 0
    bot_mode: str = "live"
    guild_served: bool = True
    pulse_band: str | None = None
    pulse_assembled_at: datetime | None = None
    pulse_invalidated_at: datetime | None = None
    pulse_has_presence: bool = False
    pulse_alerts_enabled: bool = True
    pulse_alerts_last_at: datetime | None = None
    catalog_bots: tuple[str, ...] = ()
    scheme_drift: int = 0
    quota_capped: int = 0
    suspended_watchers: int = 0
    evidence_category: bool | str | None = None
    evidence_archive: str | None = None
    evidence_open: int = 0
    evidence_overdue: int = 0
    staff_feed: LogChannelView | None = None
    ticket_log: LogChannelView | None = None
    automod_file: bool | None = None
    automod_card: bool | None = None
    verification_writable: bool | None = None
    stats_board: str | None = None
    youtube_feeds: int | None = None
    youtube_last_error: str | None = None
    youtube_last_checked_at: datetime | None = None
    lfg_card: str | None = None
    voice_hub: str | None = None
    hub_denial: str | None = None
    evidence_denial: str | None = None
    showcases: Mapping[str, str] = field(default_factory=dict)
    structure_channel: bool = False
    rep_defaults: bool = False
    ladder_defaults: bool = False
    skeleton: tuple[SkeletonCheck, ...] = ()
    dm_messages_intent: bool = True
    schema_revision: str | None = None
    schema_head: str | None = None
    cache_usage: tuple[tuple[str, int, int], ...] = ()
    startup_ms: tuple[tuple[str, int], ...] = ()
    create_holds: tuple[CreateHoldView, ...] = ()
    rate_observed: tuple[RateObservedView, ...] = ()
    traffic: TrafficSnapshot | None = None
    governor: GovernorSnapshot | None = None
    mention_everyone_roles: tuple[str, ...] = ()


def render_diagnose(lines: Sequence[DiagnoseLine]) -> str:
    """Текст карточки /diagnose. Нужен тестам и сверке с UI-KIT §4."""
    return collected_text(diagnose_card(lines))


def diagnose_card(
    lines: Sequence[DiagnoseLine],
    *,
    tab: str = TAB_SETUP,
    on_tab: TabSwitch | None = None,
    on_apply: ApplyClick | None = None,
    on_restore: ApplyClick | None = None,
    on_repair: RepairClick | None = None,
    on_repair_all: ApplyClick | None = None,
    on_reapply: ApplyClick | None = None,
    on_map: ApplyClick | None = None,
    on_page: PageClick | None = None,
    broken: Sequence[SkeletonCheck] = (),
    page: int = 1,
    pages: int = 1,
) -> ui.LayoutView:
    statuses = [line.status for line in lines]
    if CheckStatus.FAIL in statuses:
        tone = Tone.DANGER
    elif CheckStatus.WARN in statuses:
        tone = Tone.WARN
    else:
        tone = Tone.INFO
    body: list[str] = []
    for line in lines:
        body.append(f"{render_icon(_MARK[line.status])} {line.title}")
        if line.action:
            body.append(line.action)
    title = copy.DIAGNOSE_TITLE
    rows: list[ui.ActionRow] = []
    if on_tab is not None:
        title = _tab_title(tab)
        rows.append(_tab_row(tab, on_tab))
    title = copy.diagnose_heading(title, __version__)
    if on_apply is not None and tab == TAB_SETUP:
        rows.append(_apply_row(on_apply))
    if on_restore is not None and tab == TAB_SETUP:
        rows.append(_restore_row(on_restore))
    if tab in {TAB_SKELETON, TAB_READY}:
        visible = {line.title for line in lines}
        page_broken = tuple(item for item in broken if item.label in visible) if tab == TAB_SKELETON else ()
        if tab == TAB_READY:
            page_broken = ()
        rows.extend(
            _repair_rows(
                page_broken,
                on_repair=on_repair if tab == TAB_SKELETON else None,
                on_repair_all=on_repair_all,
                on_reapply=on_reapply,
                on_map=on_map,
            )
        )
    if on_page is not None and pages > 1:
        rows.append(_page_row(page, pages, on_page))
    return panel(title, body, tone=tone, rows=tuple(rows[:5]))


def apply_defaults_confirm(
    *,
    on_yes: ApplyClick,
    on_no: ApplyClick,
) -> ui.LayoutView:
    yes = ui.Button(label=copy.DIAGNOSE_APPLY_DEFAULTS, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.DIAGNOSE_APPLY_CONFIRM,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def restore_perms_confirm(
    *,
    on_yes: ApplyClick,
    on_no: ApplyClick,
) -> ui.LayoutView:
    yes = ui.Button(label=copy.ADMIN_RESTORE_VERB, style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(
        copy.ADMIN_RESTORE_CONFIRM,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def _apply_row(on_apply: ApplyClick) -> ui.ActionRow:
    button = ui.Button(label=copy.DIAGNOSE_APPLY_DEFAULTS, style=discord.ButtonStyle.primary)

    async def pressed(interaction: discord.Interaction) -> None:
        await on_apply(interaction)

    button.callback = instant(pressed)
    return ui.ActionRow(button)


def _restore_row(on_restore: ApplyClick) -> ui.ActionRow:
    button = ui.Button(label=copy.ADMIN_RESTORE_DEFAULTS, style=discord.ButtonStyle.secondary)

    async def pressed(interaction: discord.Interaction) -> None:
        await on_restore(interaction)

    button.callback = instant(pressed)
    return ui.ActionRow(button)


def _tab_title(tab: str) -> str:
    if tab == TAB_NOW:
        return copy.DIAGNOSE_TITLE_NOW
    if tab == TAB_SKELETON:
        return copy.DIAGNOSE_TITLE_SKELETON
    if tab == TAB_READY:
        return copy.DIAGNOSE_TITLE_READY
    return copy.DIAGNOSE_TITLE_SETUP


def _tab_row(tab: str, on_tab: TabSwitch) -> ui.ActionRow:
    setup = ui.Button(
        label=copy.DIAGNOSE_TAB_SETUP,
        style=discord.ButtonStyle.primary if tab == TAB_SETUP else discord.ButtonStyle.secondary,
        disabled=tab == TAB_SETUP,
    )
    skeleton = ui.Button(
        label=copy.DIAGNOSE_TAB_SKELETON,
        style=discord.ButtonStyle.primary if tab == TAB_SKELETON else discord.ButtonStyle.secondary,
        disabled=tab == TAB_SKELETON,
    )
    ready = ui.Button(
        label=copy.DIAGNOSE_TAB_READY,
        style=discord.ButtonStyle.primary if tab == TAB_READY else discord.ButtonStyle.secondary,
        disabled=tab == TAB_READY,
    )
    now = ui.Button(
        label=copy.DIAGNOSE_TAB_NOW,
        style=discord.ButtonStyle.primary if tab == TAB_NOW else discord.ButtonStyle.secondary,
        disabled=tab == TAB_NOW,
    )

    async def go_setup(interaction: discord.Interaction) -> None:
        await on_tab(interaction, TAB_SETUP)

    async def go_skeleton(interaction: discord.Interaction) -> None:
        await on_tab(interaction, TAB_SKELETON)

    async def go_ready(interaction: discord.Interaction) -> None:
        await on_tab(interaction, TAB_READY)

    async def go_now(interaction: discord.Interaction) -> None:
        await on_tab(interaction, TAB_NOW)

    setup.callback = instant(go_setup)
    skeleton.callback = instant(go_skeleton)
    ready.callback = instant(go_ready)
    now.callback = instant(go_now)
    return ui.ActionRow(setup, skeleton, ready, now)


def _page_row(page: int, pages: int, on_page: PageClick) -> ui.ActionRow:
    current = max(1, min(int(page), pages))
    prev_btn = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=current <= 1)
    next_btn = ui.Button(label="→", style=discord.ButtonStyle.secondary, disabled=current >= pages)

    async def go_prev(interaction: discord.Interaction) -> None:
        await on_page(interaction, current - 1)

    async def go_next(interaction: discord.Interaction) -> None:
        await on_page(interaction, current + 1)

    prev_btn.callback = instant(go_prev)
    next_btn.callback = instant(go_next)
    return ui.ActionRow(prev_btn, next_btn)


def _repair_rows(
    broken: Sequence[SkeletonCheck],
    *,
    on_repair: RepairClick | None,
    on_repair_all: ApplyClick | None,
    on_reapply: ApplyClick | None,
    on_map: ApplyClick | None,
) -> tuple[ui.ActionRow, ...]:
    rows: list[ui.ActionRow] = []
    actions: list[ui.Button] = []
    if on_repair_all is not None:
        repair_all = ui.Button(label=copy.DIAGNOSE_REPAIR_ALL, style=discord.ButtonStyle.primary)

        async def run_all(interaction: discord.Interaction) -> None:
            await on_repair_all(interaction)

        repair_all.callback = instant(run_all)
        actions.append(repair_all)
    if on_reapply is not None:
        reapply = ui.Button(label=copy.DIAGNOSE_REAPPLY, style=discord.ButtonStyle.secondary)

        async def run_rights(interaction: discord.Interaction) -> None:
            await on_reapply(interaction)

        reapply.callback = instant(run_rights)
        actions.append(reapply)
    if on_map is not None:
        mapped = ui.Button(label=copy.SETUP_MAP_OPEN, style=discord.ButtonStyle.secondary)

        async def run_map(interaction: discord.Interaction) -> None:
            await on_map(interaction)

        mapped.callback = instant(run_map)
        actions.append(mapped)
    if actions:
        rows.append(ui.ActionRow(*actions[:5]))
    if on_repair is None or not broken:
        return tuple(rows)
    chunk = tuple(broken[:SELECT_PAGE_SIZE])
    options = [
        discord.SelectOption(
            label=item.label[:100],
            value=item.key,
            description=(copy.skeleton_hint(item.hint) if item.hint else "")[:100] or None,
        )
        for item in chunk
    ]
    if not options:
        return tuple(rows)
    select = ui.Select(placeholder=copy.DIAGNOSE_REPAIR_PICK, options=options, min_values=1, max_values=1)

    async def picked(interaction: discord.Interaction) -> None:
        key = select.values[0] if select.values else ""
        if not key:
            await interaction.response.defer()
            return
        await on_repair(interaction, key)

    select.callback = instant(picked)
    rows.append(ui.ActionRow(select))
    return tuple(rows)


def build_diagnose_lines(snap: DiagnoseSnapshot, *, tab: str | None = None, page: int = 1) -> list[DiagnoseLine]:
    if tab == TAB_NOW:
        return build_now_lines(snap)
    if tab == TAB_SKELETON:
        return build_skeleton_lines(snap, page=page)
    if tab == TAB_READY:
        return build_ready_lines(snap)
    if tab == TAB_SETUP:
        return build_setup_lines(snap)
    return [
        *build_setup_lines(snap),
        *build_skeleton_lines(snap, page=page),
        *build_ready_lines(snap),
        *build_now_lines(snap),
    ]


def _skeleton_checks(snap: DiagnoseSnapshot) -> tuple[SkeletonCheck, ...]:
    if snap.skeleton:
        return snap.skeleton
    return tuple(
        SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.FAIL, "missing") for entry in SKELETON
    )


def skeleton_page_count(snap: DiagnoseSnapshot, *, limit: int = SKELETON_PAGE_SIZE) -> int:
    rows = _grouped_skeleton_lines(snap)
    if not rows:
        return 1
    return max(1, math.ceil(len(rows) / max(1, int(limit))))


def ready_needs_fix(snap: DiagnoseSnapshot) -> bool:
    checks = _skeleton_checks(snap)
    by_key = {item.key: item for item in checks}
    for entry in skeleton_entries():
        item = by_key.get(entry.key)
        if item is None or item.status is not SkeletonStatus.OK:
            return True
    verdicts = feature_verdicts(checks, showcases=snap.showcases)
    if any(item.status is not SkeletonStatus.OK for item in verdicts):
        return True
    if snap.hub_denial:
        return True
    if snap.evidence_denial:
        return True
    return not snap.structure_channel


def build_skeleton_lines(
    snap: DiagnoseSnapshot, *, page: int = 1, limit: int = SKELETON_PAGE_SIZE
) -> list[DiagnoseLine]:
    rows = _grouped_skeleton_lines(snap)
    size = max(1, int(limit))
    total = max(1, math.ceil(len(rows) / size) if rows else 1)
    current = max(1, min(int(page), total))
    start = (current - 1) * size
    return rows[start : start + size]


def _grouped_skeleton_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    checks = {row.key: row for row in _skeleton_checks(snap)}
    marks = {
        SkeletonStatus.OK: CheckStatus.OK,
        SkeletonStatus.WARN: CheckStatus.WARN,
        SkeletonStatus.FAIL: CheckStatus.FAIL,
    }
    lines: list[DiagnoseLine] = []
    seen: set[str] = set()
    for feature, copy_name, keys in FEATURES:
        items = [checks[key] for key in keys if key in checks]
        if not items:
            continue
        seen.update(item.key for item in items)
        statuses = [marks[item.status] for item in items]
        if CheckStatus.FAIL in statuses:
            header = CheckStatus.FAIL
        elif CheckStatus.WARN in statuses:
            header = CheckStatus.WARN
        else:
            header = CheckStatus.OK
        lines.append(DiagnoseLine(header, str(getattr(copy, copy_name, feature))))
        problems = [item for item in items if item.status is not SkeletonStatus.OK]
        healthy = len(items) - len(problems)
        for item in problems:
            lines.append(
                DiagnoseLine(marks[item.status], item.label, copy.skeleton_hint(item.hint) if item.hint else "")
            )
        if healthy:
            lines.append(DiagnoseLine(CheckStatus.OK, copy.diagnose_skeleton_rest(healthy)))
    leftover = [row for row in _skeleton_checks(snap) if row.key not in seen]
    for item in leftover:
        lines.append(
            DiagnoseLine(marks[item.status], item.label, copy.skeleton_hint(item.hint) if item.hint else "")
        )
    return lines


def build_ready_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    checks = _skeleton_checks(snap)
    verdicts = feature_verdicts(checks, showcases=snap.showcases)
    marks = {
        SkeletonStatus.OK: CheckStatus.OK,
        SkeletonStatus.WARN: CheckStatus.WARN,
        SkeletonStatus.FAIL: CheckStatus.FAIL,
    }
    lines: list[DiagnoseLine] = []
    for verdict in verdicts:
        label = str(getattr(copy, verdict.label, verdict.feature))
        lines.append(_feature_ready_line(verdict, label, marks[verdict.status]))
    if snap.voice_hub is not None or snap.hub_denial is not None:
        lines.append(_voice_hub_line(snap.hub_denial or snap.voice_hub or "ok"))
    evidence = snap.evidence_denial if snap.evidence_denial is not None else snap.evidence_category
    if evidence is not None:
        lines.append(_evidence_category_line(evidence, overdue=snap.evidence_overdue))
    if snap.evidence_archive is not None:
        lines.append(_evidence_archive_line(snap.evidence_archive))
    if not snap.structure_channel:
        lines.append(
            DiagnoseLine(CheckStatus.WARN, copy.DIAGNOSE_STRUCTURE_OFF, copy.DIAGNOSE_REPAIR_ALL)
        )
    ok_count = sum(1 for item in verdicts if item.status is SkeletonStatus.OK)
    failed = any(item.status is SkeletonStatus.FAIL for item in verdicts)
    extra_fail = any(line.status is CheckStatus.FAIL for line in lines[len(verdicts) :])
    extra_warn = any(line.status is CheckStatus.WARN for line in lines[len(verdicts) :])
    if failed or extra_fail:
        summary = DiagnoseLine(CheckStatus.FAIL, copy.DIAGNOSE_READY_FAIL)
    elif ok_count == len(verdicts) and not extra_warn:
        summary = DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_READY_OK)
    else:
        summary = DiagnoseLine(
            CheckStatus.WARN,
            copy.diagnose_ready_partial(ok=ok_count, total=len(verdicts)),
        )
    lines.append(summary)
    return lines


def _feature_ready_line(verdict: FeatureVerdict, label: str, status: CheckStatus) -> DiagnoseLine:
    missing = tuple(getattr(verdict, "missing", ()) or ())
    broken = tuple(getattr(verdict, "broken", ()) or ())
    note = str(getattr(verdict, "note", "") or "")
    names = []
    for key in missing:
        entry = entry_by_key(key)
        names.append(entry.diagnose_label if entry is not None else key)
    broken_names = []
    for key in broken:
        entry = entry_by_key(key)
        broken_names.append(entry.diagnose_label if entry is not None else key)
    if missing:
        title = f"{label}: {copy.diagnose_ready_missing(names)}"
        return DiagnoseLine(status, title, copy.DIAGNOSE_REPAIR_ALL)
    if broken:
        title = f"{label}: {copy.diagnose_ready_broken(broken_names)}"
        return DiagnoseLine(status, title, copy.DIAGNOSE_REAPPLY)
    if note:
        return DiagnoseLine(status, f"{label}: {copy.DIAGNOSE_READY_SHOWCASE}", copy.DIAGNOSE_REPAIR_ALL)
    return DiagnoseLine(status, label)


def build_setup_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    lines: list[DiagnoseLine] = []
    lines.extend(_intent_lines(snap))
    lines.extend(_permission_lines(snap.permissions))
    lines.extend(_role_position_lines(snap.bot_top_position, snap.managed_roles))
    lines.extend(_log_setup_lines(snap))
    lines.extend(_mode_lines(snap.bot_mode, snap.guild_served))
    lines.append(_history_line(snap.unread_count, snap.unread_names))
    lines.extend(_ladder_role_lines(snap.bot_top_position, snap.ladder_roles))
    catalog = _catalog_bots_line(snap)
    if catalog is not None:
        lines.append(catalog)
    drift = _scheme_drift_line(snap.scheme_drift)
    if drift is not None:
        lines.append(drift)
    if snap.rep_defaults:
        lines.append(
            DiagnoseLine(
                CheckStatus.WARN,
                copy.DIAGNOSE_REP_DEFAULTS,
                copy.DIAGNOSE_REP_DEFAULTS_ACTION,
            )
        )
    if snap.ladder_defaults:
        lines.append(
            DiagnoseLine(
                CheckStatus.WARN,
                copy.DIAGNOSE_LADDER_DEFAULTS,
                copy.DIAGNOSE_LADDER_DEFAULTS_ACTION,
            )
        )
    if snap.automod_file is not None:
        lines.append(_automod_file_line(snap.automod_file))
    if snap.mention_everyone_roles:
        lines.append(_mention_everyone_line(snap.mention_everyone_roles))
    if snap.automod_card is not None:
        lines.append(_automod_card_line(snap.automod_card))
    if snap.verification_writable is not None:
        lines.append(_verification_write_line(snap.verification_writable))
    if snap.stats_board is not None:
        lines.append(_stats_board_line(snap.stats_board))
    if snap.lfg_card is not None:
        lines.append(_lfg_line(snap.lfg_card))
    if snap.voice_hub is not None:
        lines.append(_voice_hub_line(snap.voice_hub))
    schema = _schema_line(snap)
    if schema is not None:
        lines.append(schema)
    return lines


def build_now_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    lines: list[DiagnoseLine] = [
        _runtime_line(snap),
        _startup_line(snap),
        _queues_line(snap),
        *_capacity_lines(snap),
        _emoji_line(snap.emoji_loaded, snap.emoji_total),
        _census_line(snap.census_at),
        _pulse_line(snap),
        _pulse_alerts_line(snap),
        _db_line(snap.db_ok),
        _reputation_line(snap),
        _quota_line(snap.quota_capped),
        _suspended_watchers_line(snap.suspended_watchers),
    ]
    youtube = _youtube_line(snap)
    if youtube is not None:
        lines.append(youtube)
    if snap.broker_streak:
        lines.append(_broker_line(snap.broker_streak))
    lines.extend(_create_hold_lines(snap))
    lines.extend(_rate_observed_lines(snap))
    lines.extend(_traffic_lines(snap))
    return lines


def _intent_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    lines = [
        _intent_line(
            enabled=snap.members_intent,
            title_on=copy.INTENT_MEMBERS_ON,
            title_off=copy.INTENT_MEMBERS_OFF,
            action=copy.INTENT_MEMBERS_ACTION,
        ),
        _intent_line(
            enabled=snap.message_content_intent,
            title_on=copy.INTENT_CONTENT_ON,
            title_off=copy.INTENT_CONTENT_OFF,
            action=copy.INTENT_CONTENT_ACTION,
        ),
        _intent_line(
            enabled=snap.dm_messages_intent,
            title_on=copy.INTENT_DM_ON,
            title_off=copy.INTENT_DM_OFF,
            action=copy.INTENT_DM_ACTION,
        ),
    ]
    if snap.members_intent and snap.member_count is not None and snap.members_loaded < snap.member_count:
        lines.append(
            DiagnoseLine(
                CheckStatus.WARN,
                copy.members_partial(snap.members_loaded, snap.member_count),
                copy.MEMBERS_PARTIAL_ACTION,
            )
        )
    return lines


def _intent_line(*, enabled: bool, title_on: str, title_off: str, action: str) -> DiagnoseLine:
    if enabled:
        return DiagnoseLine(CheckStatus.OK, title_on)
    return DiagnoseLine(CheckStatus.FAIL, title_off, action)


def _permission_lines(permissions: Mapping[str, bool]) -> list[DiagnoseLine]:
    lines: list[DiagnoseLine] = []
    for item in REQUIRED_PERMISSIONS:
        if permissions.get(item.attr, False):
            lines.append(DiagnoseLine(CheckStatus.OK, copy.permission_ok(item.label)))
        else:
            lines.append(
                DiagnoseLine(CheckStatus.FAIL, copy.permission_fail(item.label), copy.permission_reason(item.attr))
            )
    return lines


def _role_position_lines(bot_top: int | None, managed: Sequence[ManagedRoleView]) -> list[DiagnoseLine]:
    if bot_top is None:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.ROLE_MISSING_BOT,
                copy.ROLE_MISSING_BOT_ACTION,
            )
        ]
    if not managed:
        return [
            DiagnoseLine(
                CheckStatus.WARN,
                copy.ROLES_UNBOUND,
                copy.ROLES_UNBOUND_ACTION,
            )
        ]
    missing = [role.label for role in managed if role.position is None]
    if missing:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.roles_not_found(missing),
                copy.ROLES_STALE_ACTION,
            )
        ]
    too_high = [role.label for role in managed if role.position is not None and role.position >= bot_top]
    if too_high:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.role_below(too_high),
                copy.ROLE_BELOW_ACTION,
            )
        ]
    return [DiagnoseLine(CheckStatus.OK, copy.ROLE_ABOVE_OK)]


def _log_setup_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    lines = [
        _log_channel_line(copy.LOG_USER, copy.DIAGNOSE_LOG_ACTION, snap.user_log),
        _log_channel_line(copy.LOG_ADMIN, copy.DIAGNOSE_LOG_ACTION, snap.admin_log),
    ]
    if snap.staff_feed is not None:
        lines.append(_log_channel_line(copy.LOG_STAFF, copy.DIAGNOSE_LOG_ACTION, snap.staff_feed))
    if snap.ticket_log is not None:
        lines.append(_log_channel_line(copy.LOG_TICKETS, copy.DIAGNOSE_TICKET_LOG_ACTION, snap.ticket_log))
    if snap.evidence_category is not None:
        lines.append(_evidence_category_line(snap.evidence_category, overdue=snap.evidence_overdue))
    if snap.evidence_archive is not None:
        lines.append(_evidence_archive_line(snap.evidence_archive))
    return lines


def _log_channel_line(kind: str, action: str, view: LogChannelView) -> DiagnoseLine:
    if not view.configured:
        return DiagnoseLine(
            CheckStatus.WARN,
            copy.log_unconfigured(kind),
            action,
        )
    if view.name is None:
        return DiagnoseLine(
            CheckStatus.FAIL,
            copy.log_invisible(kind),
            copy.LOG_CHANNEL_MISSING_ACTION,
        )
    if not view.can_send or not view.can_embed:
        missing = []
        if not view.can_send:
            missing.append(copy.LOG_PERM_SEND)
        if not view.can_embed:
            missing.append(copy.LOG_PERM_EMBED)
        return DiagnoseLine(
            CheckStatus.FAIL,
            copy.log_cannot_write(kind, view.name),
            copy.log_grant(missing),
        )
    return DiagnoseLine(CheckStatus.OK, copy.log_ok(kind, view.name))


def _mode_lines(bot_mode: str, guild_served: bool) -> list[DiagnoseLine]:
    if bot_mode == "test":
        title = copy.MODE_TEST
        action = copy.MODE_TEST_ACTION
    else:
        title = copy.MODE_LIVE
        action = copy.MODE_LIVE_ACTION
    lines = [DiagnoseLine(CheckStatus.OK, title, action)]
    if not guild_served:
        lines.append(DiagnoseLine(CheckStatus.WARN, copy.MODE_GUILD_IGNORED, copy.MODE_GUILD_IGNORED_ACTION))
    return lines


def _runtime_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    latency = copy.RUNTIME_NO_LATENCY if snap.latency_ms is None else f"{snap.latency_ms} {copy.RUNTIME_MS}"
    title = copy.runtime_title(
        latency=latency,
        backlog=snap.log_backlog,
        sent=snap.log_sent,
        failed=snap.log_failed,
        dropped=snap.log_dropped,
        reconnects=snap.reconnects_hour,
    )
    if snap.latency_ms is None:
        return DiagnoseLine(CheckStatus.FAIL, title, copy.RUNTIME_DISCONNECTED)
    if not snap.log_pump_alive:
        return DiagnoseLine(CheckStatus.FAIL, title, copy.RUNTIME_PUMP_DEAD)
    if snap.log_failed or snap.log_dropped or snap.latency_ms >= 1000:
        return DiagnoseLine(CheckStatus.WARN, title, copy.RUNTIME_UNHEALTHY)
    return DiagnoseLine(CheckStatus.OK, title)


def _history_line(unread: int, names: Sequence[str]) -> DiagnoseLine:
    if unread <= 0:
        return DiagnoseLine(CheckStatus.OK, copy.HISTORY_OK)
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.history_unread(unread, copy.history_suffix(names, unread)),
        copy.HISTORY_INGEST,
    )


def _emoji_line(loaded: int, total: int) -> DiagnoseLine:
    if total <= 0:
        total = EMOJI_PACK_SIZE
    if loaded >= total:
        return DiagnoseLine(CheckStatus.OK, copy.emojis_ok(loaded, total))
    if loaded <= 0:
        return DiagnoseLine(CheckStatus.WARN, copy.EMOJIS_FALLBACK, copy.EMOJIS_FALLBACK_ACTION)
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.emojis_partial(loaded, total),
        copy.EMOJIS_FALLBACK_ACTION,
    )


def _quota_line(count: int) -> DiagnoseLine:
    status = CheckStatus.WARN if count else CheckStatus.OK
    return DiagnoseLine(status, copy.diagnose_quota_capped(count))


def _suspended_watchers_line(count: int) -> DiagnoseLine:
    status = CheckStatus.WARN if count else CheckStatus.OK
    return DiagnoseLine(status, copy.diagnose_suspended_watchers(count))


def _evidence_category_line(configured: bool | str, *, overdue: int = 0) -> DiagnoseLine:
    if configured is True or configured == "ok":
        status = CheckStatus.WARN if overdue else CheckStatus.OK
        return DiagnoseLine(status, copy.DIAGNOSE_EVIDENCE_OK)
    if configured is False or configured in {"off", "no_category"}:
        return DiagnoseLine(CheckStatus.WARN, copy.DIAGNOSE_EVIDENCE_OFF, copy.DIAGNOSE_EVIDENCE_ACTION)
    return DiagnoseLine(CheckStatus.FAIL, copy.diagnose_evidence(str(configured)), copy.DIAGNOSE_EVIDENCE_ACTION)


def _evidence_archive_line(status: str) -> DiagnoseLine:
    title = copy.diagnose_evidence_archive(status)
    if status == "ok":
        return DiagnoseLine(CheckStatus.OK, title)
    return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_EVIDENCE_ARCHIVE_ACTION)


def _automod_file_line(present: bool) -> DiagnoseLine:
    if present:
        return DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_AUTOMOD_FILE_OK)
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.DIAGNOSE_AUTOMOD_FILE_OFF,
        copy.DIAGNOSE_AUTOMOD_FILE_ACTION,
    )


def _mention_everyone_line(names: Sequence[str]) -> DiagnoseLine:
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.diagnose_mention_everyone(names),
        copy.DIAGNOSE_MENTION_EVERYONE_ACTION,
    )


def _automod_card_line(live: bool) -> DiagnoseLine:
    if live:
        return DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_AUTOMOD_CARD_OK)
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.DIAGNOSE_AUTOMOD_CARD_STALE,
        copy.DIAGNOSE_AUTOMOD_CARD_ACTION,
    )


def _verification_write_line(writable: bool) -> DiagnoseLine:
    if writable:
        return DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_VERIFICATION_WRITE_OK)
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.DIAGNOSE_VERIFICATION_WRITE_OFF,
        copy.DIAGNOSE_VERIFICATION_WRITE_ACTION,
    )


def _youtube_line(snap: DiagnoseSnapshot) -> DiagnoseLine | None:
    if snap.youtube_feeds is None:
        return None
    if snap.youtube_last_error:
        return DiagnoseLine(
            CheckStatus.WARN,
            copy.diagnose_youtube_error(snap.youtube_feeds, snap.youtube_last_error),
            copy.DIAGNOSE_YT_ACTION,
        )
    if snap.youtube_feeds == 0:
        return DiagnoseLine(CheckStatus.WARN, copy.DIAGNOSE_YT_NONE, copy.DIAGNOSE_YT_ACTION)
    if snap.youtube_last_checked_at is None:
        return DiagnoseLine(CheckStatus.WARN, copy.diagnose_youtube_idle(snap.youtube_feeds), copy.DIAGNOSE_YT_ACTION)
    when = discord.utils.format_dt(snap.youtube_last_checked_at, "R")
    return DiagnoseLine(CheckStatus.OK, copy.diagnose_youtube_ok(snap.youtube_feeds, when))


def _stats_board_line(status: str) -> DiagnoseLine:
    title = copy.diagnose_stats_board(status)
    if status == "live":
        return DiagnoseLine(CheckStatus.OK, title)
    return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_STATS_BOARD_ACTION)


def _lfg_line(status: str) -> DiagnoseLine:
    title = copy.diagnose_lfg(status)
    if status == "live":
        return DiagnoseLine(CheckStatus.OK, title)
    return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_LFG_ACTION)


def _voice_hub_line(status: str) -> DiagnoseLine:
    title = copy.diagnose_voice_hub(status)
    if status == "ok":
        return DiagnoseLine(CheckStatus.OK, title)
    if status in {"held", "rate", "limit"}:
        return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_VOICE_HUB_ACTION)
    return DiagnoseLine(CheckStatus.FAIL, title, copy.DIAGNOSE_VOICE_HUB_ACTION)


def _reputation_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    status = CheckStatus.WARN if snap.rep_would_ban else CheckStatus.OK
    return DiagnoseLine(status, copy.diagnose_rep_line(at=snap.rep_at_threshold, bans=snap.rep_would_ban))


def _ladder_role_lines(bot_top: int | None, ladder: Sequence[ManagedRoleView]) -> list[DiagnoseLine]:
    if not ladder:
        return []
    if bot_top is None:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.ROLE_MISSING_BOT,
                copy.ROLE_MISSING_BOT_ACTION,
            )
        ]
    missing = [role.label for role in ladder if role.position is None]
    if missing:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.roles_not_found(missing),
                copy.LADDER_STALE_ACTION,
            )
        ]
    too_high = [role.label for role in ladder if role.position is not None and role.position >= bot_top]
    if too_high:
        return [
            DiagnoseLine(
                CheckStatus.FAIL,
                copy.ladder_below(too_high),
                copy.ROLE_BELOW_ACTION,
            )
        ]
    return [DiagnoseLine(CheckStatus.OK, copy.LADDER_ABOVE_OK)]


def _capacity_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    lines = [
        DiagnoseLine(
            CheckStatus.OK,
            copy.capacity_cache(
                hits=snap.cache_hits,
                misses=snap.cache_misses,
                size=snap.cache_size,
                messages=snap.message_cache_size,
            ),
        )
    ]
    occupancy = _cache_occupancy_line(snap)
    if occupancy is not None:
        lines.append(occupancy)
    trips = snap.limiter_trips
    lines.append(
        DiagnoseLine(
            CheckStatus.WARN if trips else CheckStatus.OK,
            copy.capacity_limits(
                depth=snap.outbound_depth,
                limit=snap.outbound_limit,
                waiting=snap.outbound_waiting,
                trips=trips,
            ),
            copy.CAPACITY_LIMITER_ACTION if trips else "",
        )
    )
    return lines


def _startup_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    return DiagnoseLine(CheckStatus.OK, copy.diagnose_startup(snap.startup_ms))


def _queues_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    title = copy.diagnose_queues(
        jobs=snap.queue_jobs,
        later=snap.queue_later,
        tasks=snap.bg_tasks,
        scheduled=snap.scheduler_jobs,
    )
    busy = snap.queue_jobs + snap.queue_later + snap.bg_tasks
    if not snap.log_pump_alive:
        return DiagnoseLine(CheckStatus.FAIL, title, copy.RUNTIME_PUMP_DEAD)
    if busy >= 50:
        return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_QUEUES_BUSY)
    return DiagnoseLine(CheckStatus.OK, title)


def _census_line(census_at: datetime | None) -> DiagnoseLine:
    if census_at is None:
        return DiagnoseLine(CheckStatus.WARN, copy.DIAGNOSE_CENSUS_NEVER, copy.DIAGNOSE_CENSUS_ACTION)
    when = Countdown().render(census_at)
    return DiagnoseLine(CheckStatus.OK, copy.diagnose_census(when))


def _pulse_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    if snap.pulse_assembled_at is None:
        return DiagnoseLine(CheckStatus.WARN, copy.DIAGNOSE_PULSE_NEVER, copy.DIAGNOSE_PULSE_ACTION)
    age = Countdown().render(snap.pulse_assembled_at)
    invalidated = (
        copy.PULSE_NEVER if snap.pulse_invalidated_at is None else Countdown().render(snap.pulse_invalidated_at)
    )
    band = copy.pulse_band(snap.pulse_band or "unknown")
    title = copy.diagnose_pulse(
        band=band,
        age=age,
        presence=snap.pulse_has_presence,
        invalidated=invalidated,
    )
    if snap.pulse_band == "unknown":
        return DiagnoseLine(CheckStatus.WARN, title, copy.DIAGNOSE_PULSE_UNKNOWN_ACTION)
    return DiagnoseLine(CheckStatus.OK, title)


def _scheme_drift_line(count: int) -> DiagnoseLine | None:
    if count <= 0:
        return None
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.diagnose_scheme_drift(count),
        copy.DIAGNOSE_SCHEME_DRIFT_ACTION,
    )


def _catalog_bots_line(snap: DiagnoseSnapshot) -> DiagnoseLine | None:
    if not snap.catalog_bots:
        return None
    return DiagnoseLine(
        CheckStatus.WARN,
        copy.diagnose_catalog_bot(snap.catalog_bots),
        copy.DIAGNOSE_CATALOG_BOT_ACTION,
    )


def _pulse_alerts_line(snap: DiagnoseSnapshot) -> DiagnoseLine:
    last = copy.DIAGNOSE_PULSE_ALERTS_NEVER
    if snap.pulse_alerts_last_at is not None:
        last = Countdown().render(snap.pulse_alerts_last_at)
    title = copy.diagnose_pulse_alerts(enabled=snap.pulse_alerts_enabled, last=last)
    if snap.pulse_alerts_enabled:
        return DiagnoseLine(CheckStatus.OK, title)
    return DiagnoseLine(CheckStatus.WARN, title)


def _db_line(ok: bool) -> DiagnoseLine:
    if ok:
        return DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_DB_OK)
    return DiagnoseLine(CheckStatus.FAIL, copy.DIAGNOSE_DB_DOWN, copy.DIAGNOSE_DB_ACTION)


def _schema_line(snap: DiagnoseSnapshot) -> DiagnoseLine | None:
    if snap.schema_revision is None and snap.schema_head is None:
        return None
    if snap.schema_revision and snap.schema_head and snap.schema_revision == snap.schema_head:
        return DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_SCHEMA_OK)
    return DiagnoseLine(
        CheckStatus.FAIL,
        copy.diagnose_schema_fail(snap.schema_revision, snap.schema_head),
        copy.DIAGNOSE_SCHEMA_ACTION,
    )


def _cache_occupancy_line(snap: DiagnoseSnapshot) -> DiagnoseLine | None:
    if not snap.cache_usage:
        return None
    full = any(cap > 0 and size >= cap for _name, size, cap in snap.cache_usage)
    return DiagnoseLine(
        CheckStatus.WARN if full else CheckStatus.OK,
        copy.capacity_cache_occupancy(snap.cache_usage),
    )


def _broker_line(streak: int) -> DiagnoseLine:
    return DiagnoseLine(CheckStatus.WARN, copy.diagnose_broker(streak), copy.ALERT_BROKER_ACTION)


def _create_hold_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    if not snap.create_holds:
        return [DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_HOLD_NONE)]
    lines: list[DiagnoseLine] = []
    for item in snap.create_holds:
        lines.append(
            DiagnoseLine(
                CheckStatus.WARN,
                copy.diagnose_create_hold(item.bucket, item.source, item.remaining, persisted=item.persisted),
            )
        )
    return lines


def _rate_observed_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    if not snap.rate_observed:
        return [DiagnoseLine(CheckStatus.OK, copy.DIAGNOSE_OBSERVED_NONE)]
    lines: list[DiagnoseLine] = []
    for item in snap.rate_observed:
        lines.append(
            DiagnoseLine(
                CheckStatus.OK,
                copy.diagnose_rate_observed(
                    item.bucket,
                    status=item.status,
                    scope=item.scope,
                    remaining=item.remaining,
                    reset_after=item.reset_after,
                    age=item.age,
                ),
            )
        )
    return lines


def _traffic_or_empty(snap: DiagnoseSnapshot) -> TrafficSnapshot:
    if snap.traffic is not None:
        return snap.traffic
    return TrafficSnapshot(0, 0, 0, 0, (), (), ())


def _traffic_lines(snap: DiagnoseSnapshot) -> list[DiagnoseLine]:
    traffic = _traffic_or_empty(snap)
    invalid_status = CheckStatus.WARN if traffic.invalid_10m else CheckStatus.OK
    global_status = CheckStatus.WARN if traffic.global_429_10m else CheckStatus.OK
    lines = [
        DiagnoseLine(CheckStatus.OK, copy.traffic_rps(traffic.rps_1s, traffic.rps_60s)),
        DiagnoseLine(invalid_status, copy.traffic_invalid(traffic.invalid_10m, INVALID_BUDGET)),
        DiagnoseLine(global_status, copy.traffic_global_429(traffic.global_429_10m)),
        DiagnoseLine(CheckStatus.OK, copy.traffic_top("429", traffic.top_429)),
        DiagnoseLine(CheckStatus.OK, copy.traffic_top("403", traffic.top_403)),
    ]
    gov = snap.governor
    if gov is None:
        return lines
    fuse_status = CheckStatus.OK if gov.fuse == "ok" else CheckStatus.WARN
    lines.append(DiagnoseLine(CheckStatus.OK, copy.traffic_profile(gov.profile)))
    lines.append(DiagnoseLine(CheckStatus.OK, copy.traffic_rps_goal(traffic.rps_1s, gov.rps_target)))
    lines.append(DiagnoseLine(fuse_status, copy.traffic_fuse(gov.fuse)))
    for item in gov.lanes:
        lines.append(DiagnoseLine(CheckStatus.OK, copy.traffic_lane_wait(item.lane, item.queue, item.p50, item.p95)))
    for item in gov.allowances:
        lines.append(
            DiagnoseLine(CheckStatus.OK, copy.traffic_aimd(item.bucket, item.allowance, frozen=item.frozen))
        )
    return lines


def latency_ms(latency: object) -> int | None:
    if not isinstance(latency, int | float) or isinstance(latency, bool):
        return None
    value = float(latency)
    if math.isnan(value) or value < 0:
        return None
    return round(value * 1000)


def permission_map(permissions: object | None) -> dict[str, bool]:
    return {item.attr: bool(getattr(permissions, item.attr, False)) for item in REQUIRED_PERMISSIONS}


def log_channel_view(guild: Any, me: object | None, channel_id: int | None) -> LogChannelView:
    if not channel_id:
        return LogChannelView(configured=False, name=None, can_send=False, can_embed=False)
    channel = guild.get_channel(channel_id) if hasattr(guild, "get_channel") else None
    if channel is None:
        return LogChannelView(configured=True, name=None, can_send=False, can_embed=False)
    perms = channel.permissions_for(me) if me is not None and hasattr(channel, "permissions_for") else None
    return LogChannelView(
        configured=True,
        name=getattr(channel, "name", None),
        can_send=bool(getattr(perms, "send_messages", False)),
        can_embed=bool(getattr(perms, "embed_links", False)),
    )


def unread_history_names(guild: Any, skip_ids: set[int], history: Any) -> list[str]:
    names: list[str] = []
    guild_id = getattr(guild, "id", 0)
    buckets = (
        getattr(guild, "text_channels", ()),
        getattr(guild, "voice_channels", ()),
        getattr(guild, "stage_channels", ()),
        getattr(guild, "threads", ()),
    )
    caught_up = getattr(history, "is_caught_up", None)
    for group in buckets:
        for channel in group or ():
            channel_id = getattr(channel, "id", None)
            if channel_id is None or channel_id in skip_ids:
                continue
            ok = callable(caught_up) and caught_up(guild_id, channel_id)
            if not ok:
                names.append(str(getattr(channel, "name", channel_id)))
    return names
