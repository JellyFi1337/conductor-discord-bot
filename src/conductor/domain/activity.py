from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


class PeriodKind(StrEnum):
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"


class EventKind(StrEnum):
    MESSAGE = "message"
    VOICE_MINUTE = "voice_minute"
    VOICE_JOIN = "voice_join"
    JOIN = "join"
    LEAVE = "leave"
    REACTION = "reaction"
    MESSAGE_DELETE = "message_delete"
    MESSAGE_EDIT = "message_edit"
    TICKET_FAQ = "ticket_faq"


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def hour_bucket(moment: datetime) -> datetime:
    moment = as_utc(moment)
    return moment.replace(minute=0, second=0, microsecond=0)


def day_bucket(moment: datetime) -> datetime:
    moment = as_utc(moment)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def month_bucket(moment: datetime) -> datetime:
    moment = as_utc(moment)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
