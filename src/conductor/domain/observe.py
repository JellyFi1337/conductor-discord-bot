"""Аварии, дедупликация оповещений и отпечаток проблем каркаса. Без Discord и БД."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256

from conductor.domain.activity import as_utc

ALERT_INTERVAL = timedelta(hours=1)
JOIN_SPIKE_INTERVAL = timedelta(minutes=30)
BROKER_STREAK = 3
STRUCTURE_CARD_CAP = 3
TAB_SETUP = "setup"
TAB_SKELETON = "skeleton"
TAB_READY = "ready"
TAB_NOW = "now"

__all__ = [
    "ALERT_INTERVAL",
    "BROKER_STREAK",
    "JOIN_SPIKE_INTERVAL",
    "PULSE_ALERT_KINDS",
    "STRUCTURE_CARD_CAP",
    "TAB_NOW",
    "TAB_READY",
    "TAB_SETUP",
    "TAB_SKELETON",
    "AlertKind",
    "alert_key",
    "broker_trips",
    "cap_cards",
    "fingerprint_changed",
    "next_streak",
    "should_emit",
    "structure_fingerprint",
]


class AlertKind(StrEnum):
    QUEUE_FULL = "log_queue_full"
    DATABASE = "database_down"
    ROLE_BELOW = "role_below"
    PERMISSION = "permission_lost"
    BROKER = "broker_rejects"
    VOICE_EMPTY_AT_PEAK = "voice_empty_at_peak"
    JOIN_LEAVE_INVERT = "join_leave_invert"
    AUTHORS_DROP = "authors_drop"
    BOOSTS_DROP = "boosts_drop"
    JOIN_SPIKE = "join_spike"
    STRUCTURE_LOST = "structure_lost"
    STRUCTURE_RENAMED = "structure_renamed"


PULSE_ALERT_KINDS: frozenset[AlertKind] = frozenset(
    {
        AlertKind.VOICE_EMPTY_AT_PEAK,
        AlertKind.JOIN_LEAVE_INVERT,
        AlertKind.AUTHORS_DROP,
        AlertKind.BOOSTS_DROP,
    }
)


def alert_key(kind: AlertKind, guild_id: int, detail: str = "") -> tuple[str, int, str]:
    return (kind.value, int(guild_id), str(detail))


def should_emit(last: datetime | None, *, now: datetime, interval: timedelta = ALERT_INTERVAL) -> bool:
    if last is None:
        return True
    return as_utc(now) - as_utc(last) >= interval


def structure_fingerprint(items: Sequence[tuple[str, str]]) -> str:
    """Устойчивый хеш набора проблем: ключ и вид, без зависимости от порядка."""
    blob = "\n".join(f"{key}\t{kind}" for key, kind in sorted((str(key), str(kind)) for key, kind in items))
    return sha256(blob.encode("utf-8")).hexdigest()


def fingerprint_changed(previous: str | None, current: str) -> bool:
    """Уведомить, только если отпечаток отличается от уже отправленного."""
    return previous != current


def cap_cards[T](items: Sequence[T], *, limit: int = STRUCTURE_CARD_CAP) -> tuple[tuple[T, ...], int]:
    """Первые limit элементов и число свёрнутых в сводку."""
    shown = tuple(items[:limit])
    rest = max(0, len(items) - limit)
    return shown, rest


def next_streak(streak: int, *, ok: bool) -> int:
    if ok:
        return 0
    return int(streak) + 1


def broker_trips(streak: int, *, need: int = BROKER_STREAK) -> bool:
    return int(streak) >= int(need)
