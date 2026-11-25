"""Снимок /diagnose и оповещения в канал следящих с дедупликацией раз в час."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from conductor.domain.access import staff_role_ids
from conductor.domain.activity import as_utc
from conductor.domain.loyalty import TIER_LABELS, TIER_PRIORITY
from conductor.domain.observe import (
    PULSE_ALERT_KINDS,
    AlertKind,
    alert_key,
    broker_trips,
    next_streak,
    should_emit,
)
from conductor.domain.pulse import PulseAlertRules, PulseSignal, catalog_bot_names, evaluate_pulse_alerts
from conductor.presentation import copy
from conductor.presentation.diagnose import (
    CreateHoldView,
    DiagnoseSnapshot,
    LogChannelView,
    ManagedRoleView,
    RateObservedView,
    build_setup_lines,
    latency_ms,
    log_channel_view,
    permission_map,
    unread_history_names,
)
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.guild_config import role_id_for
from conductor.services.history import skip_ids_from_config
from conductor.services.runtime import guild_is_active, log_tick

log = structlog.get_logger("conductor.observe")

TICK_PERIOD = 60.0


class ObserveService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._last: dict[tuple[str, int, str], datetime] = {}
        self._broker: dict[int, int] = {}
        self._dropped_seen = 0
        pump = getattr(bot, "log_pump", None)
        if pump is not None:
            pump.on_drop = self._on_drop
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(TICK_PERIOD, "observe", self.tick_all)

    def broker_streak(self, guild_id: int) -> int:
        return int(self._broker.get(int(guild_id), 0))

    def note_broker(self, guild_id: int, *, ok: bool, now: datetime | None = None) -> None:
        streak = next_streak(self._broker.get(int(guild_id), 0), ok=ok)
        self._broker[int(guild_id)] = streak
        if broker_trips(streak):
            self.report(int(guild_id), AlertKind.BROKER, copy.ALERT_BROKER, now=now)

    async def schema_revisions(self) -> tuple[str | None, str | None]:
        from conductor.db.engine import current_revision, script_head

        head = script_head()
        engine = getattr(self._bot, "engine", None)
        if engine is None:
            return None, head
        try:
            async with engine.connect() as conn:
                current = await conn.run_sync(current_revision)
        except Exception:
            log.warning("observe_schema_unread")
            return None, head
        return current, head

    async def ping_db(self) -> bool:
        engine = getattr(self._bot, "engine", None)
        if engine is None:
            return False
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            log.warning("observe_db_down")
            return False
        return True

    async def tick_all(self, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        posted = 0
        db_ok = await self.ping_db()
        if not db_ok:
            posted += self._fanout(AlertKind.DATABASE, copy.ALERT_DB, now=moment)
        dropped = int(getattr(getattr(self._bot, "log_pump", None), "dropped", 0) or 0)
        if dropped > self._dropped_seen:
            self._dropped_seen = dropped
            posted += self._fanout(AlertKind.QUEUE_FULL, copy.ALERT_QUEUE, now=moment)
        else:
            self._dropped_seen = dropped
        for guild in tuple(getattr(self._bot, "guilds", ())):
            if not guild_is_active(self._bot, int(guild.id)):
                continue
            posted += await self._tick_guild(guild, db_ok=db_ok, now=moment)
        log_tick(log, "observe_tick", changed=posted)
        return posted

    def report(
        self,
        guild_id: int,
        kind: AlertKind,
        body: str,
        *,
        detail: str = "",
        now: datetime | None = None,
        title: str | None = None,
        tone: Tone | None = None,
    ) -> bool:
        moment = as_utc(now or datetime.now(UTC))
        key = alert_key(kind, guild_id, detail)
        if not should_emit(self._last.get(key), now=moment):
            return False
        channel_id = self._admin_channel(guild_id)
        if channel_id is None:
            return False
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return False
        view = panel(
            title or copy.ALERT_TITLE,
            body,
            tone=tone or Tone.DANGER,
            icon=Icon.WARN,
            footnote=copy.ALERT_FOOTNOTE,
            timeout=None,
        )
        submit(channel_id, view=view)
        self._last[key] = moment
        log.info("observe_alert", guild_id=guild_id, kind=kind.value, detail=detail or None)
        return True

    def last_pulse_alert_at(self, guild_id: int) -> datetime | None:
        found: datetime | None = None
        wanted = {kind.value for kind in PULSE_ALERT_KINDS}
        for (kind, gid, _detail), when in self._last.items():
            if int(gid) != int(guild_id) or kind not in wanted:
                continue
            if found is None or when > found:
                found = when
        return found

    def collect_snapshot(
        self,
        guild: Any,
        config: Any | None,
        *,
        rep_at_threshold: int = 0,
        rep_would_ban: int = 0,
        ladder_roles: tuple[ManagedRoleView, ...] = (),
        db_ok: bool = True,
    ) -> DiagnoseSnapshot:
        return collect_diagnose_snapshot(
            self._bot,
            guild,
            config,
            rep_at_threshold=rep_at_threshold,
            rep_would_ban=rep_would_ban,
            ladder_roles=ladder_roles,
            db_ok=db_ok,
            broker_streak=self.broker_streak(getattr(guild, "id", 0)),
        )

    def _on_drop(self) -> None:
        self._fanout(AlertKind.QUEUE_FULL, copy.ALERT_QUEUE)

    def _fanout(self, kind: AlertKind, body: str, *, now: datetime | None = None) -> int:
        posted = 0
        for guild in tuple(getattr(self._bot, "guilds", ())):
            if not guild_is_active(self._bot, int(guild.id)):
                continue
            if self.report(int(guild.id), kind, body, now=now):
                posted += 1
        return posted

    async def _tick_guild(self, guild: Any, *, db_ok: bool, now: datetime) -> int:
        config = getattr(getattr(self._bot, "config_by_guild", {}), "get", lambda _gid: None)(guild.id)
        snap = collect_diagnose_snapshot(self._bot, guild, config, db_ok=db_ok)
        posted = 0
        for line in build_setup_lines(snap):
            title = line.title
            if copy.ROLE_BELOW_HINT in title:
                if self.report(guild.id, AlertKind.ROLE_BELOW, copy.ALERT_ROLE, now=now):
                    posted += 1
            elif title.startswith(copy.PERM_MISSING):
                label = title.removeprefix(copy.PERM_MISSING).strip()
                if self.report(
                    guild.id,
                    AlertKind.PERMISSION,
                    copy.alert_permission(label),
                    detail=label,
                    now=now,
                ):
                    posted += 1
        posted += await self._tick_pulse(guild, now)
        return posted

    async def _tick_pulse(self, guild: Any, now: datetime) -> int:
        pulse = getattr(self._bot, "pulse", None)
        snapshot = getattr(pulse, "snapshot", None)
        if not callable(snapshot):
            return 0
        rules = await self._pulse_rules(int(guild.id))
        if not rules.enabled:
            return 0
        snap = await snapshot(guild, now=now)
        posted = 0
        for signal in evaluate_pulse_alerts(snap.inputs, rules, now=now):
            if self.report(
                int(guild.id),
                signal.kind,
                _signal_body(signal),
                now=now,
                title=copy.SIGNAL_TITLE,
                tone=Tone.WARN,
            ):
                posted += 1
        return posted

    async def _pulse_rules(self, guild_id: int) -> PulseAlertRules:
        pulse = getattr(self._bot, "pulse", None)
        loader = getattr(pulse, "alert_rules", None)
        if callable(loader):
            return await loader(guild_id)
        cached = getattr(pulse, "last_alert_rules", None)
        if callable(cached):
            return cached(guild_id)
        return PulseAlertRules()

    def _admin_channel(self, guild_id: int) -> int | None:
        config = getattr(self._bot, "config_by_guild", {}).get(guild_id)
        return None if config is None else config.admin_log_channel_id


def collect_diagnose_snapshot(
    bot: Any,
    guild: Any,
    config: Any | None,
    *,
    rep_at_threshold: int = 0,
    rep_would_ban: int = 0,
    ladder_roles: tuple[ManagedRoleView, ...] = (),
    db_ok: bool = True,
    broker_streak: int = 0,
    scheme_drift: int = 0,
    quota_capped: int = 0,
    suspended_watchers: int = 0,
    evidence_category: bool | str | None = None,
    evidence_archive: str | None = None,
    evidence_open: int = 0,
    evidence_overdue: int = 0,
    automod_file: bool | None = None,
    automod_card: bool | None = None,
    verification_writable: bool | None = None,
    stats_board: str | None = None,
    youtube_feeds: int | None = None,
    youtube_last_error: str | None = None,
    youtube_last_checked_at: datetime | None = None,
    lfg_card: str | None = None,
    voice_hub: str | None = None,
    hub_denial: str | None = None,
    evidence_denial: str | None = None,
    showcases: Mapping[str, str] | None = None,
    structure_channel: bool = False,
    ticket_log: LogChannelView | None = None,
    rep_defaults: bool = False,
    ladder_defaults: bool = False,
    skeleton: tuple = (),
    schema_revision: str | None = None,
    schema_head: str | None = None,
    create_holds: tuple[CreateHoldView, ...] = (),
    rate_observed: tuple[RateObservedView, ...] = (),
    traffic: Any | None = None,
) -> DiagnoseSnapshot:
    intents = getattr(bot, "intents", None)
    me = getattr(guild, "me", None)
    permissions = permission_map(getattr(me, "guild_permissions", None) if me is not None else None)
    top_role = getattr(me, "top_role", None) if me is not None else None
    pump = getattr(bot, "log_pump", None)
    alive = bool(getattr(pump, "alive", False))
    if not alive:
        task = getattr(pump, "_task", None) if pump is not None else None
        alive = task is not None and not task.done()
    skip_ids = skip_ids_from_config(config)
    unread = unread_history_names(guild, skip_ids, getattr(bot, "history", None))
    members = getattr(guild, "members", ()) or ()
    motion = getattr(bot, "motion", None)
    caches = getattr(bot, "caches", None)
    messages = getattr(bot, "messages", None)
    outbound = getattr(bot, "outbound", None)
    limiter = getattr(bot, "interactions", None)
    queue = getattr(bot, "queue", None)
    scheduler = getattr(bot, "scheduler", None)
    if not broker_streak:
        observe = getattr(bot, "observe", None)
        lookup = getattr(observe, "broker_streak", None)
        if callable(lookup):
            broker_streak = int(lookup(getattr(guild, "id", 0)) or 0)
    return DiagnoseSnapshot(
        members_intent=bool(getattr(intents, "members", False)),
        message_content_intent=bool(getattr(intents, "message_content", False)),
        dm_messages_intent=bool(getattr(intents, "dm_messages", False)),
        members_loaded=len(members),
        member_count=getattr(guild, "member_count", None),
        permissions=permissions,
        bot_top_position=None if top_role is None else int(top_role.position),
        managed_roles=_managed_roles(guild, config),
        user_log=log_channel_view(guild, me, None if config is None else config.user_log_channel_id),
        admin_log=log_channel_view(guild, me, None if config is None else config.admin_log_channel_id),
        staff_feed=log_channel_view(
            guild, me, None if config is None else getattr(config, "staff_feed_channel_id", None)
        ),
        bot_mode=str(getattr(getattr(getattr(bot, "fence", None), "mode", None), "value", "live")),
        guild_served=bool(getattr(bot, "serves", lambda _gid: True)(getattr(guild, "id", 0))),
        latency_ms=latency_ms(getattr(bot, "latency", None)),
        log_backlog=int(getattr(pump, "backlog", 0) or 0),
        log_sent=int(getattr(pump, "sent", 0) or 0),
        log_failed=int(getattr(pump, "failed", 0) or 0),
        log_dropped=int(getattr(pump, "dropped", 0) or 0),
        log_pump_alive=alive,
        unread_count=len(unread),
        unread_names=tuple(unread[:5]),
        reconnects_hour=_reconnects_hour(bot),
        emoji_loaded=int(getattr(motion, "loaded_count", 0) or 0),
        emoji_total=int(getattr(motion, "total_count", 0) or 0),
        cache_hits=int(getattr(caches, "hits", 0) or 0),
        cache_misses=int(getattr(caches, "misses", 0) or 0),
        cache_size=int(getattr(caches, "size", 0) or 0),
        cache_usage=_cache_usage(caches),
        message_cache_size=len(messages) if messages is not None else 0,
        outbound_depth=int(getattr(outbound, "depth", 0) or 0),
        outbound_limit=int(getattr(outbound, "limit", 8) or 8),
        outbound_waiting=int(getattr(outbound, "waiting", 0) or 0),
        limiter_trips=int(getattr(limiter, "trips", 0) or 0),
        rep_at_threshold=rep_at_threshold,
        rep_would_ban=rep_would_ban,
        ladder_roles=ladder_roles,
        queue_jobs=int(getattr(queue, "job_count", 0) or 0),
        queue_later=int(getattr(queue, "later_count", 0) or 0),
        bg_tasks=len(getattr(bot, "_bg_tasks", ()) or ()),
        scheduler_jobs=int(getattr(scheduler, "job_count", 0) or 0),
        census_at=getattr(config, "census_at", None),
        db_ok=db_ok,
        broker_streak=broker_streak,
        pulse_band=_pulse_attr(bot, "last_band", getattr(guild, "id", 0)),
        pulse_assembled_at=_pulse_attr(bot, "last_assembled_at", getattr(guild, "id", 0)),
        pulse_invalidated_at=_pulse_attr(bot, "last_invalidated_at", getattr(guild, "id", 0)),
        pulse_has_presence=_pulse_presence(bot),
        pulse_alerts_enabled=_pulse_alerts_enabled(bot, getattr(guild, "id", 0)),
        pulse_alerts_last_at=_pulse_alerts_last(bot, getattr(guild, "id", 0)),
        catalog_bots=_catalog_bots(guild),
        scheme_drift=scheme_drift,
        quota_capped=quota_capped,
        suspended_watchers=suspended_watchers,
        evidence_category=evidence_category,
        evidence_archive=evidence_archive,
        evidence_open=evidence_open,
        evidence_overdue=evidence_overdue,
        automod_file=_automod_file_present(bot) if automod_file is None else automod_file,
        automod_card=automod_card,
        verification_writable=verification_writable,
        stats_board=stats_board,
        youtube_feeds=youtube_feeds,
        youtube_last_error=youtube_last_error,
        youtube_last_checked_at=youtube_last_checked_at,
        lfg_card=lfg_card,
        voice_hub=voice_hub,
        hub_denial=hub_denial,
        evidence_denial=evidence_denial,
        showcases=dict(showcases or {}),
        structure_channel=structure_channel,
        ticket_log=ticket_log,
        rep_defaults=rep_defaults,
        ladder_defaults=ladder_defaults,
        skeleton=tuple(skeleton),
        schema_revision=schema_revision,
        schema_head=schema_head,
        startup_ms=tuple(getattr(bot, "startup_ms", {}).items()),
        create_holds=create_holds,
        rate_observed=rate_observed,
        traffic=traffic,
        governor=_governor_view(bot, getattr(guild, "id", 0)),
        mention_everyone_roles=_mention_everyone_roles(bot, guild),
    )


def _mention_everyone_roles(bot: Any, guild: Any) -> tuple[str, ...]:
    staff_ids: set[int] = set()
    cache = getattr(getattr(bot, "caches", None), "permissions", None)
    getter = getattr(cache, "get", None)
    if callable(getter):
        snapshot = getter((int(getattr(guild, "id", 0) or 0), SNAPSHOT_CACHE_KEY))
        roles = getattr(snapshot, "rank_roles", None)
        if roles is not None:
            staff_ids.update(int(item) for item in staff_role_ids(roles))
    names: list[str] = []
    default = getattr(guild, "default_role", None)
    if default is not None and _role_can_mention_everyone(default):
        names.append("@everyone")
    for role in getattr(guild, "roles", ()) or ():
        role_id = int(getattr(role, "id", 0) or 0)
        if role is default or role_id in staff_ids:
            continue
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and is_default():
            continue
        if not _role_can_mention_everyone(role):
            continue
        name = str(getattr(role, "name", "") or role_id)
        names.append(name)
    return tuple(names)


def _reconnects_hour(bot: Any) -> int:
    lookup = getattr(bot, "resumes_last_hour", None)
    if not callable(lookup):
        return 0
    try:
        return int(lookup() or 0)
    except Exception:
        log.exception("reconnects_hour_failed")
        return 0


def _role_can_mention_everyone(role: Any) -> bool:
    permissions = getattr(role, "permissions", None)
    return bool(getattr(permissions, "mention_everyone", False))


def _governor_view(bot: Any, guild_id: object) -> Any:
    viewer = getattr(getattr(bot, "traffic", None), "view", None)
    if not callable(viewer):
        return None
    try:
        return viewer(int(guild_id or 0))
    except Exception:
        log.exception("governor_view_failed")
        return None


def _cache_usage(caches: Any) -> tuple[tuple[str, int, int], ...]:
    occupancy = getattr(caches, "occupancy", None)
    if not callable(occupancy):
        return ()
    return tuple(occupancy())


def _automod_file_present(bot: Any) -> bool | None:
    rules = getattr(bot, "automod_rules", None)
    if rules is None:
        return None
    missing = getattr(rules, "file_missing", None)
    if missing is None:
        return None
    return not bool(missing)


def _pulse_attr(bot: Any, name: str, guild_id: object) -> Any:
    pulse = getattr(bot, "pulse", None)
    method = getattr(pulse, name, None)
    if not callable(method):
        return None
    return method(int(guild_id or 0))


def _catalog_bots(guild: Any) -> tuple[str, ...]:
    names: list[str] = []
    for member in getattr(guild, "members", ()) or ():
        if not bool(getattr(member, "bot", False)):
            continue
        name = getattr(member, "name", None)
        if name:
            names.append(str(name))
    return catalog_bot_names(names)


def _pulse_alerts_enabled(bot: Any, guild_id: object) -> bool:
    pulse = getattr(bot, "pulse", None)
    getter = getattr(pulse, "last_alert_rules", None)
    if not callable(getter):
        return True
    rules = getter(int(guild_id or 0))
    return bool(getattr(rules, "enabled", True))


def _pulse_alerts_last(bot: Any, guild_id: object) -> datetime | None:
    observe = getattr(bot, "observe", None)
    getter = getattr(observe, "last_pulse_alert_at", None)
    if not callable(getter):
        return None
    return getter(int(guild_id or 0))


def _signal_body(signal: PulseSignal) -> str:
    look = copy.SIGNAL_LOOK
    if signal.kind is AlertKind.VOICE_EMPTY_AT_PEAK:
        return copy.signal_voice(median=signal.median or 0, voice=int(signal.value)) + f"\n{look}"
    if signal.kind is AlertKind.JOIN_LEAVE_INVERT:
        return copy.signal_invert(leaves=int(signal.value), threshold=int(signal.threshold)) + f"\n{look}"
    if signal.kind is AlertKind.AUTHORS_DROP:
        return (
            copy.signal_authors(authors=int(signal.value), median=signal.median or 0, ratio=signal.threshold)
            + f"\n{look}"
        )
    if signal.kind is AlertKind.BOOSTS_DROP:
        return copy.signal_boosts(now=int(signal.value), was=int(signal.median or 0)) + f"\n{look}"
    return look


def _pulse_presence(bot: Any) -> bool:
    pulse = getattr(bot, "pulse", None)
    method = getattr(pulse, "has_presence_intent", None)
    if callable(method):
        return bool(method())
    return bool(getattr(getattr(bot, "intents", None), "presences", False))


def _managed_roles(guild: Any, config: Any | None) -> tuple[ManagedRoleView, ...]:
    if config is None:
        return ()
    found: list[ManagedRoleView] = []
    get_role = getattr(guild, "get_role", None)
    for tier in TIER_PRIORITY:
        role_id = role_id_for(config, tier)
        if not role_id:
            continue
        role = get_role(role_id) if callable(get_role) else None
        found.append(
            ManagedRoleView(
                label=TIER_LABELS[tier],
                position=None if role is None else int(role.position),
            )
        )
    return tuple(found)
