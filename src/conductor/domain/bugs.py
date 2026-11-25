"""Отчёт об ошибке: лимит, код E-XXXX, статусы разбора. Без Discord и БД."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

__all__ = [
    "CODE_PATTERN",
    "DID_LIMIT",
    "HAPPENED_LIMIT",
    "HOUR_LIMIT",
    "HOUR_WINDOW",
    "BugCard",
    "BugMatch",
    "BugStatus",
    "DecideVerdict",
    "SubmitVerdict",
    "clip_did",
    "clip_happened",
    "parse_code",
    "rate_ok",
]

DID_LIMIT = 400
HAPPENED_LIMIT = 1000
HOUR_LIMIT = 3
HOUR_WINDOW = timedelta(hours=1)
CODE_PATTERN = re.compile(r"^E-[0-9A-F]{4}$")


class BugStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    FIXED = "fixed"
    REJECTED = "rejected"


class SubmitVerdict(StrEnum):
    OK = "ok"
    RATE = "rate"
    NO_CHANNEL = "no_channel"
    BAD_TEXT = "bad_text"
    INACTIVE = "inactive"


class DecideVerdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class BugMatch:
    code: str
    error_type: str = ""
    origin: str = ""
    jump_url: str = ""


@dataclass(frozen=True, slots=True)
class BugCard:
    report_id: int
    guild_id: int
    user_id: int
    did: str
    happened: str
    version: str
    status: str
    created_at: datetime
    code: str = ""
    match: BugMatch | None = None
    decided_by: int | None = None
    decided_at: datetime | None = None


def parse_code(value: str) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if CODE_PATTERN.fullmatch(text):
        return text
    return None


def clip_did(value: str) -> str:
    return _clip(value, DID_LIMIT)


def clip_happened(value: str) -> str:
    return _clip(value, HAPPENED_LIMIT)


def rate_ok(count: int) -> bool:
    return int(count) < HOUR_LIMIT


def _clip(value: str, limit: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
