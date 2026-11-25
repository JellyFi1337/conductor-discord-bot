"""Правила набора в #поиск-команды. Без Discord и без БД."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.discord_limits import VOICE_USER_LIMIT_MAX
from conductor.domain.rooms import CHANNEL_NAME_LIMIT, inspect_room_name, room_channel_name

NOTE_LIMIT = 100
NEED_MIN = 0
NEED_MAX = 100
NEED_UNLIMITED = 0
TTL_DEFAULT = 120
TTL_MIN = 15
TTL_MAX = 720
PING_COOLDOWN_DEFAULT = 15
PING_COOLDOWN_MIN = 1
PING_COOLDOWN_MAX = 180
USER_COOLDOWN_DEFAULT = 30
USER_COOLDOWN_MIN = 1
USER_COOLDOWN_MAX = 720
LFG_ROLE_COOLDOWN = timedelta(hours=1)
AUTHOR_WINDOW = timedelta(days=30)
STARTER_KIND = "lf"

_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|@everyone|@here", re.IGNORECASE)

__all__ = [
    "AUTHOR_WINDOW",
    "LFG_ROLE_COOLDOWN",
    "NEED_MAX",
    "NEED_MIN",
    "NEED_UNLIMITED",
    "NOTE_LIMIT",
    "PING_COOLDOWN_DEFAULT",
    "STARTER_KIND",
    "TTL_DEFAULT",
    "USER_COOLDOWN_DEFAULT",
    "CloseReason",
    "CloseVerdict",
    "JoinVerdict",
    "LeaveVerdict",
    "LfgCard",
    "LfgSettingsView",
    "NoteReject",
    "PostVerdict",
    "can_post",
    "card_hash",
    "check_note",
    "clamp_settings",
    "close_verdict",
    "is_full",
    "is_live",
    "join_verdict",
    "leave_verdict",
    "parse_need",
    "parse_settings",
    "ping_ready",
    "playable_games",
    "room_limit",
    "room_name",
    "should_attach_room",
]


class CloseReason(StrEnum):
    FULL = "full"
    AUTHOR = "author"
    EXPIRED = "expired"
    STAFF = "staff"


class PostVerdict(StrEnum):
    OK = "ok"
    LIVE = "live"
    COOLDOWN = "cooldown"
    NO_GAME = "no_game"
    BAD_NEED = "bad_need"
    BAD_NOTE = "bad_note"
    NO_CHANNEL = "no_channel"


class JoinVerdict(StrEnum):
    OK = "ok"
    CLOSED = "closed"
    FULL = "full"
    INSIDE = "inside"
    AUTHOR = "author"


class LeaveVerdict(StrEnum):
    OK = "ok"
    CLOSED = "closed"
    MISSING = "missing"
    AUTHOR = "author"


class CloseVerdict(StrEnum):
    OK = "ok"
    CLOSED = "closed"
    FORBIDDEN = "forbidden"


class NoteReject(StrEnum):
    EMPTY = "empty"
    LONG = "long"
    INVITE = "invite"
    AD = "ad"
    MENTION = "mention"


@dataclass(frozen=True, slots=True)
class LfgSettingsView:
    guild_id: int
    channel_id: int | None = None
    ttl_minutes: int = TTL_DEFAULT
    ping_cooldown_minutes: int = PING_COOLDOWN_DEFAULT
    per_user_cooldown_minutes: int = USER_COOLDOWN_DEFAULT


@dataclass(frozen=True, slots=True)
class LfgCard:
    post_id: int
    game: str
    author_id: int
    need: int
    signed: int
    note: str
    expires_at: datetime
    room_channel_id: int | None = None
    closed_reason: str | None = None
    signed_ids: tuple[int, ...] = ()
    game_pinged: bool = False
    lfg_pinged: bool = False
    footnote: str = ""


def _stamp(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_need(raw: str) -> int | None:
    text = str(raw or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < NEED_MIN or value > NEED_MAX:
        return None
    return value


def check_note(raw: str) -> tuple[bool, str, str]:
    text = " ".join(str(raw or "").split())
    if len(text) > NOTE_LIMIT:
        return False, "", NoteReject.LONG.value
    if _MENTION_RE.search(text):
        return False, "", NoteReject.MENTION.value
    reason = inspect_room_name(text)
    if reason is not None and reason.value != NoteReject.EMPTY.value:
        return False, "", reason.value
    return True, text, ""


def playable_games(options: Iterable[tuple[int, str]], have: Iterable[int]) -> tuple[tuple[int, str], ...]:
    held = {int(role_id) for role_id in have}
    return tuple((int(role_id), label) for role_id, label in options if int(role_id) in held)


def ping_ready(*, last_at: datetime | None, now: datetime, cooldown: timedelta) -> bool:
    if last_at is None:
        return True
    return _stamp(now) - _stamp(last_at) >= cooldown


def can_post(
    *,
    has_live: bool,
    last_created_at: datetime | None,
    now: datetime,
    cooldown: timedelta,
) -> PostVerdict:
    if has_live:
        return PostVerdict.LIVE
    if last_created_at is not None and _stamp(now) - _stamp(last_created_at) < cooldown:
        return PostVerdict.COOLDOWN
    return PostVerdict.OK


def join_verdict(
    *,
    closed: bool,
    is_author: bool,
    already: bool,
    signed: int,
    need: int,
) -> JoinVerdict:
    if closed:
        return JoinVerdict.CLOSED
    if is_author:
        return JoinVerdict.AUTHOR
    if already:
        return JoinVerdict.INSIDE
    if is_full(need, signed):
        return JoinVerdict.FULL
    return JoinVerdict.OK


def is_full(need: int, signed: int) -> bool:
    if int(need) == NEED_UNLIMITED:
        return False
    return int(signed) >= int(need)


def leave_verdict(*, closed: bool, is_author: bool, inside: bool) -> LeaveVerdict:
    if closed:
        return LeaveVerdict.CLOSED
    if is_author:
        return LeaveVerdict.AUTHOR
    if not inside:
        return LeaveVerdict.MISSING
    return LeaveVerdict.OK


def close_verdict(*, closed: bool, is_author: bool, is_staff: bool) -> CloseVerdict:
    if closed:
        return CloseVerdict.CLOSED
    if is_author or is_staff:
        return CloseVerdict.OK
    return CloseVerdict.FORBIDDEN


def is_live(*, closed_at: datetime | None, expires_at: datetime, now: datetime) -> bool:
    if closed_at is not None:
        return False
    return _stamp(now) < _stamp(expires_at)


def should_attach_room(*, owns_room: bool, in_hub: bool, in_room: bool) -> bool:
    return bool(owns_room or in_hub or in_room)


def room_name(game: str, username: str) -> str:
    label = " ".join(str(game or "").split()) or "игра"
    nick = " ".join(str(username or "").split()) or "комната"
    return room_channel_name(f"{label} · {nick}", limit=CHANNEL_NAME_LIMIT)


def room_limit(need: int) -> int:
    wanted = int(need)
    if wanted == NEED_UNLIMITED or wanted + 1 > VOICE_USER_LIMIT_MAX:
        return 0
    return wanted + 1


def clamp_settings(ttl: int, ping: int, user: int) -> tuple[int, int, int]:
    return (
        min(TTL_MAX, max(TTL_MIN, int(ttl))),
        min(PING_COOLDOWN_MAX, max(PING_COOLDOWN_MIN, int(ping))),
        min(USER_COOLDOWN_MAX, max(USER_COOLDOWN_MIN, int(user))),
    )


def parse_settings(raw: str) -> tuple[int, int, int] | None:
    parts = str(raw or "").split()
    if len(parts) != 3:
        return None
    try:
        ttl, ping, user = (int(part) for part in parts)
    except ValueError:
        return None
    return clamp_settings(ttl, ping, user)


def card_hash(
    *,
    game: str,
    author_id: int,
    need: int,
    signed: int,
    note: str,
    expires_at: datetime,
    room_channel_id: int | None,
    closed_reason: str | None,
    signed_ids: Iterable[int],
) -> str:
    stamp = _stamp(expires_at).isoformat()
    payload = "|".join(
        (
            game,
            str(int(author_id)),
            str(int(need)),
            str(int(signed)),
            note,
            stamp,
            str(int(room_channel_id or 0)),
            closed_reason or "",
            ",".join(str(int(user_id)) for user_id in signed_ids),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
