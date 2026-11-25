"""Отчёт по следящим и правила приостановки. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from conductor.domain.access import Rank
from conductor.domain.moderation import evidence_escalate_at

SUSPEND_DAYS_MIN = 1
SUSPEND_DAYS_MAX = 30
SUSPEND_REASON_MIN = 4
SUSPEND_REASON_MAX = 400
NOTE_LIMIT = 500
ROSTER_FEED_LIMIT = 15
DIGEST_WEEKDAY = 0
DIGEST_HOUR = 9
UNANSWERED_AFTER = timedelta(hours=24)

__all__ = [
    "DIGEST_HOUR",
    "DIGEST_WEEKDAY",
    "NOTE_LIMIT",
    "ROSTER_FEED_LIMIT",
    "SUSPEND_DAYS_MAX",
    "SUSPEND_DAYS_MIN",
    "SUSPEND_REASON_MAX",
    "SUSPEND_REASON_MIN",
    "UNANSWERED_AFTER",
    "RosterPerson",
    "StaffActionView",
    "SuspensionView",
    "WatcherMetrics",
    "can_suspend",
    "digest_is_due",
    "digest_week_key",
    "evidence_is_overdue",
    "parse_suspend_days",
    "parse_suspend_reason",
    "roster_sort_key",
    "sort_roster",
    "suspension_active",
    "suspension_until",
]


@dataclass(frozen=True, slots=True)
class WatcherMetrics:
    cases_7d: int = 0
    revoked_30d: int = 0
    overdue: int = 0
    tickets_30d: int = 0
    rating_sum: int = 0
    rating_n: int = 0
    last_action: datetime | None = None
    suspended_until: datetime | None = None
    suspend_reason: str | None = None

    @property
    def rating_avg(self) -> float | None:
        if self.rating_n <= 0:
            return None
        return self.rating_sum / self.rating_n


@dataclass(frozen=True, slots=True)
class RosterPerson:
    user_id: int
    mention: str
    display_name: str
    rank: Rank
    on_duty: bool
    today: str | None
    metrics: WatcherMetrics


@dataclass(frozen=True, slots=True)
class SuspensionView:
    user_id: int
    set_by: int
    reason: str
    until: datetime
    lifted_at: datetime | None = None
    lifted_by: int | None = None


@dataclass(frozen=True, slots=True)
class StaffActionView:
    action: str
    created_at: datetime
    details: str | None = None
    target_id: int | None = None


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def suspension_active(until: datetime, lifted_at: datetime | None, now: datetime) -> bool:
    if lifted_at is not None:
        return False
    return _aware(until) > _aware(now)


def suspension_until(now: datetime, days: int) -> datetime:
    return _aware(now) + timedelta(days=int(days))


def parse_suspend_days(raw: str) -> int | None:
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if days < SUSPEND_DAYS_MIN or days > SUSPEND_DAYS_MAX:
        return None
    return days


def parse_suspend_reason(raw: str) -> str | None:
    cleaned = " ".join(str(raw).split())
    if len(cleaned) < SUSPEND_REASON_MIN or len(cleaned) > SUSPEND_REASON_MAX:
        return None
    return cleaned


def can_suspend(*, actor: Rank, target: Rank, actor_id: int, target_id: int) -> bool:
    if int(actor_id) == int(target_id):
        return False
    if actor is Rank.SPECIAL:
        return True
    if actor is Rank.CHIEF:
        return target < Rank.CHIEF
    return False


def evidence_is_overdue(created_at: datetime, hours: int, now: datetime) -> bool:
    return _aware(now) >= evidence_escalate_at(created_at, hours)


def roster_sort_key(person: RosterPerson) -> tuple[bool, bool, int, str]:
    metrics = person.metrics
    return (
        metrics.suspended_until is None,
        metrics.overdue <= 0,
        -int(person.rank),
        person.display_name.lower(),
    )


def sort_roster(people: Sequence[RosterPerson]) -> list[RosterPerson]:
    return sorted(people, key=roster_sort_key)


def digest_is_due(local: datetime) -> bool:
    """`local` — уже время гильдии; переводить его в UTC здесь нельзя, иначе 09:00 станет 09:00 UTC."""
    return local.weekday() == DIGEST_WEEKDAY and local.hour == DIGEST_HOUR


def digest_week_key(local: datetime) -> str:
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"
