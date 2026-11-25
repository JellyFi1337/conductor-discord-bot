"""Входы карточки статистики и отпечаток без часов на стене. Без Discord и без БД."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from conductor.domain.activity import as_utc

__all__ = [
    "CHANNEL_LIMIT",
    "ROLE_CAP",
    "BoardInputs",
    "ChannelCount",
    "RankCount",
    "RoleCount",
    "card_fingerprint",
    "dump_role_ids",
    "parse_role_ids",
    "voice_hours",
]

ROLE_CAP = 15
CHANNEL_LIMIT = 10


@dataclass(frozen=True, slots=True)
class RankCount:
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class RoleCount:
    role_id: int
    count: int


@dataclass(frozen=True, slots=True)
class ChannelCount:
    channel_id: int
    messages: int
    authors: int


@dataclass(frozen=True, slots=True)
class BoardInputs:
    ranks: tuple[RankCount, ...] = ()
    duty: int = 0
    humans: int = 0
    bots: int = 0
    boosters: int = 0
    roles: tuple[RoleCount, ...] = ()
    channels: tuple[ChannelCount, ...] = ()
    voice_seconds: int = 0
    tickets_open: int = 0
    gathering_title: str | None = None
    gathering_at: datetime | None = None
    gathering_id: int | None = None


def parse_role_ids(raw: object) -> tuple[int, ...]:
    values: list[int] = []
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            parsed = _as_id(item)
            if parsed is not None:
                values.append(parsed)
    else:
        text = str(raw).strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return ()
            if not isinstance(loaded, list):
                return ()
            for item in loaded:
                parsed = _as_id(item)
                if parsed is not None:
                    values.append(parsed)
        else:
            for part in text.replace(",", " ").replace(";", " ").split():
                parsed = _as_id(part)
                if parsed is None:
                    return ()
                values.append(parsed)
    found: list[int] = []
    seen: set[int] = set()
    for role_id in values:
        if role_id in seen:
            continue
        seen.add(role_id)
        found.append(role_id)
        if len(found) >= ROLE_CAP:
            break
    return tuple(found)


def dump_role_ids(role_ids: Sequence[int]) -> str:
    return json.dumps(list(parse_role_ids(tuple(role_ids))))


def voice_hours(seconds: int) -> float:
    return max(0, int(seconds)) / 3600.0


def card_fingerprint(inputs: BoardInputs) -> str:
    when = ""
    if inputs.gathering_at is not None:
        when = str(int(as_utc(inputs.gathering_at).timestamp()))
    parts = [
        *(f"rank:{item.key}:{item.count}" for item in inputs.ranks),
        f"duty:{int(inputs.duty)}",
        f"people:{int(inputs.humans)}:{int(inputs.bots)}:{int(inputs.boosters)}",
        *(f"role:{item.role_id}:{item.count}" for item in inputs.roles),
        *(f"ch:{item.channel_id}:{item.messages}:{item.authors}" for item in inputs.channels),
        f"voice:{int(inputs.voice_seconds)}",
        f"tickets:{int(inputs.tickets_open)}",
        f"gather:{inputs.gathering_id or 0}:{inputs.gathering_title or ''}:{when}",
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _as_id(raw: object) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value
