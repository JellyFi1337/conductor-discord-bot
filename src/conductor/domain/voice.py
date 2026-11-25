"""Правила голосовых сессий: длительность и сверка после рестарта.

Сессия — время участника в любом голосовом канале, не комната VoiceMaster.
Источник правды после рестарта: открытые строки в БД плюс текущие voice_states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from conductor.domain.activity import as_utc


def session_seconds(joined_at: datetime, left_at: datetime) -> int:
    start = as_utc(joined_at)
    end = as_utc(left_at)
    return max(0, int((end - start).total_seconds()))


@dataclass(frozen=True, slots=True)
class OpenVoiceSession:
    guild_id: int
    user_id: int
    channel_id: int
    joined_at: datetime
    last_event_at: datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class LiveVoice:
    guild_id: int
    user_id: int
    channel_id: int


@dataclass(frozen=True, slots=True)
class CloseSession:
    session: OpenVoiceSession
    left_at: datetime
    seconds: int


@dataclass(frozen=True, slots=True)
class KeepSession:
    session: OpenVoiceSession
    channel_id: int
    last_event_at: datetime


@dataclass(frozen=True, slots=True)
class OpenNew:
    live: LiveVoice
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    close: tuple[CloseSession, ...]
    keep: tuple[KeepSession, ...]
    open: tuple[OpenNew, ...]


def reconcile_sessions(
    open_sessions: tuple[OpenVoiceSession, ...],
    live: tuple[LiveVoice, ...],
    now: datetime | None = None,
) -> ReconcilePlan:
    """Закрыть сессии, которых нет в voice_states, по last_event_at, не по now.

    Кто всё ещё в голосовом — остаётся с исходным joined_at (минуты не теряются
    и не удваиваются). Лишние открытые строки на одного человека закрываются.
    """
    moment = as_utc(now) if now is not None else datetime.now(UTC)
    live_map = {(row.guild_id, row.user_id): row for row in live}
    kept: dict[tuple[int, int], OpenVoiceSession] = {}
    closes: list[CloseSession] = []
    keeps: list[KeepSession] = []

    for session in sorted(open_sessions, key=lambda row: (row.joined_at, row.id or 0)):
        key = (session.guild_id, session.user_id)
        if key in kept:
            left_at = as_utc(session.last_event_at)
            closes.append(
                CloseSession(session, left_at, session_seconds(session.joined_at, left_at)),
            )
            continue
        current = live_map.get(key)
        if current is None:
            left_at = as_utc(session.last_event_at)
            closes.append(
                CloseSession(session, left_at, session_seconds(session.joined_at, left_at)),
            )
            continue
        kept[key] = session
        keeps.append(KeepSession(session, current.channel_id, moment))

    opens = tuple(OpenNew(voice, moment) for voice in live if (voice.guild_id, voice.user_id) not in kept)
    return ReconcilePlan(tuple(closes), tuple(keeps), opens)
