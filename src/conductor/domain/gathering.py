"""Сбор: места, лист ожидания, чек-ин. Без Discord и БД."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.activity import as_utc
from conductor.domain.giveaway import parse_duration

TITLE_LIMIT = 200
MIN_SEATS = 0
SEATS_UNLIMITED = 0
MAX_SEATS = 10_000
DEFAULT_SEATS = 20
FLUSH_INTERVAL = 2.0
PING_COOLDOWN = timedelta(minutes=10)
BAR_WIDTH = 10


class RsvpStatus(StrEnum):
    GOING = "going"
    MAYBE = "maybe"
    CANT = "cant"


class GatheringState(StrEnum):
    OPEN = "open"
    CANCELLED = "cancelled"


class RsvpVerdict(StrEnum):
    SEATED = "seated"
    WAITLIST = "waitlist"
    SET = "set"
    ALREADY = "already"
    CHECKED = "checked"
    EARLY = "early"
    NOT_GOING = "not_going"
    CLOSED = "closed"
    BOT = "bot"
    MISSING = "missing"


class ManageVerdict(StrEnum):
    OK = "ok"
    DENIED = "denied"
    MISSING = "missing"
    ALREADY = "already"
    COOLDOWN = "cooldown"
    EMPTY = "empty"
    NO_HUB = "no_hub"


@dataclass(frozen=True, slots=True)
class Rsvp:
    user_id: int
    status: RsvpStatus
    waitlisted: bool = False
    created_at: datetime | None = None
    checked_in_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RsvpChange:
    entries: tuple[Rsvp, ...]
    verdict: RsvpVerdict
    promoted_id: int | None = None


@dataclass(frozen=True, slots=True)
class GatheringSpec:
    title: str
    starts_at: datetime
    seats: int = DEFAULT_SEATS


@dataclass(frozen=True, slots=True)
class GatheringCard:
    gathering_id: int
    title: str
    host_id: int
    starts_at: datetime
    seats: int
    entries: tuple[Rsvp, ...] = ()
    state: GatheringState = GatheringState.OPEN
    channel_id: int = 0
    message_id: int | None = None
    voice_channel_id: int | None = None
    last_ping_at: datetime | None = None
    seated_n: int | None = None
    waiting_n: int | None = None
    maybe_n: int | None = None
    cant_n: int | None = None


def parse_title(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > TITLE_LIMIT:
        return None
    return cleaned


def parse_seats(value: str | int | None) -> int | None:
    if value is None or value == "":
        return DEFAULT_SEATS
    try:
        seats = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seats < MIN_SEATS or seats > MAX_SEATS:
        return None
    return seats


def parse_when(text: str, *, now: datetime) -> datetime | None:
    raw = text.strip()
    if not raw:
        return None
    delta = parse_duration(raw)
    if delta is not None:
        return as_utc(now) + delta
    try:
        unix = int(raw)
    except ValueError:
        unix = 0
    if unix > 1_000_000_000:
        return datetime.fromtimestamp(unix, tz=UTC)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=UTC)
    return None


def parse_spec(*, title: str, when: str, seats: str | int | None = None, now: datetime) -> GatheringSpec | None:
    heading = parse_title(title)
    starts = parse_when(when, now=now)
    cap = parse_seats(seats)
    if heading is None or starts is None or cap is None:
        return None
    if starts <= as_utc(now):
        return None
    return GatheringSpec(title=heading, starts_at=starts, seats=cap)


def can_manage(*, actor_id: int, host_id: int, has_manage: bool) -> bool:
    return actor_id == host_id or has_manage


def gathering_state(*, cancelled_at: datetime | None) -> GatheringState:
    if cancelled_at is not None:
        return GatheringState.CANCELLED
    return GatheringState.OPEN


def checkin_at(starts_at: datetime) -> datetime:
    return as_utc(starts_at).replace(hour=0, minute=0, second=0, microsecond=0)


def checkin_open(starts_at: datetime, now: datetime) -> bool:
    return as_utc(now) >= checkin_at(starts_at)


def ping_ready(last_ping_at: datetime | None, now: datetime) -> bool:
    if last_ping_at is None:
        return True
    return as_utc(now) >= as_utc(last_ping_at) + PING_COOLDOWN


def seat_bar(seated: int, cap: int, width: int = BAR_WIDTH) -> str:
    if int(cap) == SEATS_UNLIMITED:
        return ""
    if width < 1:
        width = 1
    filled = round(width * min(max(seated, 0), cap) / cap)
    filled = min(max(filled, 0), width)
    return "▰" * filled + "▱" * (width - filled)


def _is_seated(item: Rsvp) -> bool:
    return item.status is RsvpStatus.GOING and not item.waitlisted


def seated_ids(entries: Sequence[Rsvp]) -> tuple[int, ...]:
    return tuple(item.user_id for item in entries if _is_seated(item))


def waitlist_ids(entries: Sequence[Rsvp]) -> tuple[int, ...]:
    waiting = [item for item in entries if item.status is RsvpStatus.GOING and item.waitlisted]
    waiting.sort(key=lambda item: as_utc(item.created_at) if item.created_at else datetime.min.replace(tzinfo=UTC))
    return tuple(item.user_id for item in waiting)


def status_ids(entries: Sequence[Rsvp], status: RsvpStatus) -> tuple[int, ...]:
    return tuple(item.user_id for item in entries if item.status is status and not item.waitlisted)


def checked_ids(entries: Sequence[Rsvp]) -> tuple[int, ...]:
    return tuple(item.user_id for item in entries if item.checked_in_at is not None)


def apply_rsvp(
    entries: Sequence[Rsvp],
    *,
    user_id: int,
    status: RsvpStatus,
    seats: int,
    now: datetime,
) -> RsvpChange:
    existing = next((item for item in entries if item.user_id == user_id), None)
    rest = [item for item in entries if item.user_id != user_id]
    if status is RsvpStatus.GOING:
        if existing is not None and existing.status is RsvpStatus.GOING:
            return RsvpChange(tuple(entries), RsvpVerdict.ALREADY)
        seated_n = sum(1 for item in rest if _is_seated(item))
        waitlisted = False if int(seats) == SEATS_UNLIMITED else seated_n >= int(seats)
        rest.append(Rsvp(user_id, RsvpStatus.GOING, waitlisted=waitlisted, created_at=as_utc(now)))
        verdict = RsvpVerdict.WAITLIST if waitlisted else RsvpVerdict.SEATED
        return RsvpChange(tuple(rest), verdict)
    if existing is not None and existing.status is status:
        return RsvpChange(tuple(entries), RsvpVerdict.ALREADY)
    was_seated = existing is not None and _is_seated(existing)
    rest.append(
        Rsvp(
            user_id,
            status,
            waitlisted=False,
            created_at=existing.created_at if existing is not None else as_utc(now),
        )
    )
    promoted = None
    if was_seated:
        rest, promoted = _promote(rest)
    return RsvpChange(tuple(rest), RsvpVerdict.SET, promoted)


def apply_checkin(
    entries: Sequence[Rsvp],
    *,
    user_id: int,
    starts_at: datetime,
    now: datetime,
) -> RsvpChange:
    if not checkin_open(starts_at, now):
        return RsvpChange(tuple(entries), RsvpVerdict.EARLY)
    found: list[Rsvp] = []
    hit: Rsvp | None = None
    for item in entries:
        if item.user_id == user_id:
            hit = item
        else:
            found.append(item)
    if hit is None or not _is_seated(hit):
        return RsvpChange(tuple(entries), RsvpVerdict.NOT_GOING)
    if hit.checked_in_at is not None:
        return RsvpChange(tuple(entries), RsvpVerdict.ALREADY)
    found.append(
        Rsvp(
            hit.user_id,
            hit.status,
            waitlisted=False,
            created_at=hit.created_at,
            checked_in_at=as_utc(now),
        )
    )
    return RsvpChange(tuple(found), RsvpVerdict.CHECKED)


def _promote(rows: list[Rsvp]) -> tuple[list[Rsvp], int | None]:
    waiting = sorted(
        (item for item in rows if item.status is RsvpStatus.GOING and item.waitlisted),
        key=lambda item: as_utc(item.created_at) if item.created_at else datetime.min.replace(tzinfo=UTC),
    )
    if not waiting:
        return rows, None
    chosen = waiting[0]
    updated: list[Rsvp] = []
    for item in rows:
        if item.user_id == chosen.user_id:
            updated.append(
                Rsvp(
                    item.user_id,
                    item.status,
                    waitlisted=False,
                    created_at=item.created_at,
                    checked_in_at=item.checked_in_at,
                )
            )
        else:
            updated.append(item)
    return updated, chosen.user_id
