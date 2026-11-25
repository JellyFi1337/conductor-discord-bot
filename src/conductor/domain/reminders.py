"""Напоминания: срок, лимит, одно место на запись. Без Discord и БД."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.activity import as_utc
from conductor.domain.gathering import parse_when

PER_USER_LIMIT = 10
TEXT_LIMIT = 200
MAX_AHEAD = timedelta(days=90)


class CreateVerdict(StrEnum):
    OK = "ok"
    BAD_WHEN = "bad_when"
    BAD_TEXT = "bad_text"
    LIMIT = "limit"


class CancelVerdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    ALREADY = "already"


@dataclass(frozen=True, slots=True)
class ReminderCard:
    reminder_id: int
    guild_id: int
    channel_id: int
    user_id: int
    text: str
    due_at: datetime


def parse_text(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > TEXT_LIMIT:
        return None
    return cleaned


def parse_due(text: str | None, *, now: datetime) -> datetime | None:
    if text is None or not str(text).strip():
        return None
    due = parse_when(str(text), now=now)
    if due is None:
        return None
    moment = as_utc(now)
    due = as_utc(due)
    if due <= moment:
        return None
    if due > moment + MAX_AHEAD:
        return None
    return due


def can_accept(count: int, *, limit: int = PER_USER_LIMIT) -> bool:
    return 0 <= int(count) < int(limit)


def excerpt(text: str, *, limit: int = 60) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
