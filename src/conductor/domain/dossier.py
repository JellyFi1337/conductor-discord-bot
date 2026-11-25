"""Досье участника и наблюдение. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.access import (
    MOD_BAN,
    MOD_HISTORY,
    MOD_KICK,
    MOD_MUTE,
    MOD_NOTE,
    MOD_REVOKE,
    MOD_TIMEOUT,
    MOD_UNMUTE,
    MOD_WARN,
    MOD_WATCH,
    Rank,
    can_punish,
)

WATCH_DAYS_MIN = 1
WATCH_DAYS_MAX = 30
WATCH_SWEEP_SECONDS = 3600
DOSSIER_CASES = 5
DOSSIER_NOTES = 3
_ANON_MARKERS = ("аноним", "anonymous")


class DossierAction(StrEnum):
    WARN = "warn"
    TIMEOUT = "timeout"
    MUTE = "mute"
    UNMUTE = "unmute"
    NOTE = "note"
    WATCH = "watch"
    WATCH_CLEAR = "watch_clear"
    KICK = "kick"
    BAN = "ban"
    REVOKE = "revoke"
    HISTORY = "history"


ACTION_KEYS: dict[DossierAction, str] = {
    DossierAction.WARN: MOD_WARN,
    DossierAction.TIMEOUT: MOD_TIMEOUT,
    DossierAction.MUTE: MOD_MUTE,
    DossierAction.UNMUTE: MOD_UNMUTE,
    DossierAction.NOTE: MOD_NOTE,
    DossierAction.WATCH: MOD_WATCH,
    DossierAction.WATCH_CLEAR: MOD_WATCH,
    DossierAction.KICK: MOD_KICK,
    DossierAction.BAN: MOD_BAN,
    DossierAction.REVOKE: MOD_REVOKE,
    DossierAction.HISTORY: MOD_HISTORY,
}

_SANCTIONS: tuple[DossierAction, ...] = (
    DossierAction.WARN,
    DossierAction.TIMEOUT,
    DossierAction.MUTE,
    DossierAction.UNMUTE,
    DossierAction.NOTE,
    DossierAction.WATCH,
    DossierAction.WATCH_CLEAR,
    DossierAction.KICK,
    DossierAction.BAN,
)


@dataclass(frozen=True, slots=True)
class StoredWatch:
    guild_id: int
    user_id: int
    set_by: int
    reason: str
    until: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DossierCaseLine:
    number: int
    kind: str
    actor_id: int
    created_at: datetime | None
    reason: str = ""
    revoked_at: datetime | None = None
    revoked_by: int | None = None


@dataclass(frozen=True, slots=True)
class DossierTickets:
    count: int = 0
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DossierSnapshot:
    user_id: int
    name: str
    tenure_days: int
    ladder_step: str
    rep_score: int
    sinner: bool = False
    tier_name: str = ""
    timeout_until: datetime | None = None
    muted: bool = False
    watch: StoredWatch | None = None
    cases: tuple[DossierCaseLine, ...] = ()
    notes: tuple[DossierCaseLine, ...] = ()
    tickets: DossierTickets = DossierTickets()
    actions: tuple[DossierAction, ...] = ()
    self_view: bool = False
    target_is_bot: bool = False
    self_roles: tuple[str, ...] = ()
    lfg_posts: int = 0


def parse_watch_days(raw: str) -> int | None:
    text = str(raw or "").strip().lower().replace("д", "").replace("d", "").replace(" ", "")
    if not text.isdigit():
        return None
    days = int(text)
    if days < WATCH_DAYS_MIN or days > WATCH_DAYS_MAX:
        return None
    return days


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def watch_until(now: datetime, days: int) -> datetime:
    return _aware(now) + timedelta(days=int(days))


def is_watch_active(watch: StoredWatch | None, now: datetime) -> bool:
    if watch is None:
        return False
    return _aware(watch.until) > _aware(now)


def is_anonymous_category(name: str) -> bool:
    token = str(name or "").strip().lower()
    return any(marker in token for marker in _ANON_MARKERS)


def visible_actions(
    *,
    keys: Collection[str],
    actor: Rank,
    target: Rank,
    self_view: bool,
    target_is_bot: bool,
    watching: bool = False,
) -> tuple[DossierAction, ...]:
    if self_view or target_is_bot:
        return ()
    granted = set(keys)
    found: list[DossierAction] = []
    if can_punish(actor, target):
        for action in _SANCTIONS:
            if action is DossierAction.WATCH_CLEAR and not watching:
                continue
            if action is DossierAction.WATCH and watching:
                continue
            if ACTION_KEYS[action] not in granted:
                continue
            found.append(action)
    if ACTION_KEYS[DossierAction.REVOKE] in granted:
        found.append(DossierAction.REVOKE)
    return tuple(found)
