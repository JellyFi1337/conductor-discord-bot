"""Снимок критического сбоя: команда, место, актёр, происхождение. Без Discord и БД."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "NOTABLE_PERMISSION_ATTRS",
    "CriticalError",
    "ErrorKind",
    "error_code_of",
    "error_origin",
    "held_permission_attrs",
    "is_critical_error",
    "sanitize_error_text",
    "unwrap_error",
]

NOTABLE_PERMISSION_ATTRS: tuple[str, ...] = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "ban_members",
    "kick_members",
    "moderate_members",
    "manage_messages",
    "mention_everyone",
    "manage_webhooks",
    "view_audit_log",
    "send_messages",
)

_SKIP_TYPES = frozenset(
    {
        "CancelledError",
        "CheckFailure",
        "CommandAlreadyRunning",
        "CommandOnCooldown",
        "NoPrivateMessage",
        "TransformerError",
    }
)


class ErrorKind(StrEnum):
    COMMAND = "command"
    COMPONENT = "component"
    MODAL = "modal"
    LISTENER = "listener"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class CriticalError:
    code: str
    kind: ErrorKind
    action: str
    error_type: str
    error_text: str
    origin: str
    guild_id: int | None = None
    guild_name: str = ""
    channel_id: int | None = None
    channel_name: str = ""
    channel_kind: str = ""
    user_id: int | None = None
    user_name: str = ""
    is_developer: bool = False
    is_guild_owner: bool = False
    is_administrator: bool = False
    rank: str = "none"
    discord_perms: tuple[str, ...] = ()
    missing_bot_perms: tuple[str, ...] = ()
    extras: str = ""


def unwrap_error(error: BaseException) -> BaseException:
    seen: set[int] = set()
    current = error
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "original", None)
        if isinstance(original, BaseException):
            current = original
            continue
        cause = current.__cause__
        if isinstance(cause, BaseException):
            current = cause
            continue
        break
    return current


def is_critical_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _SKIP_TYPES:
            return False
        nxt = getattr(current, "original", None)
        current = nxt if isinstance(nxt, BaseException) else current.__cause__
    return True


def error_code_of(value: int) -> str:
    return f"E-{int(value) & 0xFFFF:04X}"


def sanitize_error_text(text: str, *, limit: int = 400) -> str:
    cleaned = " ".join(str(text or "").replace("`", "'").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def held_permission_attrs(permissions: object | None) -> tuple[str, ...]:
    if permissions is None:
        return ()
    return tuple(attr for attr in NOTABLE_PERMISSION_ATTRS if bool(getattr(permissions, attr, False)))


def error_origin(error: BaseException, *, root: str = "conductor") -> str:
    inner = unwrap_error(error)
    frames = traceback.extract_tb(inner.__traceback__)
    if not frames:
        frames = traceback.extract_tb(error.__traceback__)
    picked = None
    marker = f"{root}/"
    for frame in reversed(frames):
        path = frame.filename.replace("\\", "/")
        if marker in path and "/site-packages/" not in path:
            picked = frame
            break
    if picked is None and frames:
        picked = frames[-1]
    if picked is None:
        return ""
    return f"{Path(picked.filename).name}:{picked.lineno} {picked.name}"
