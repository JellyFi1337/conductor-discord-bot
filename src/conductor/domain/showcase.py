"""Реестр витрин-панелей и чистый расчёт статуса. Без Discord и без БД."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SHOWCASES",
    "ShowcaseResult",
    "ShowcaseSpec",
    "ShowcaseState",
    "required_showcases",
    "showcase_by_id",
    "state_of",
]


class ShowcaseState(StrEnum):
    OFF = "off"
    MISSING = "missing"
    STALE = "stale"
    MOVED = "moved"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ShowcaseSpec:
    id: str
    label: str
    panel_kind: str
    channel_key: str
    feature: str
    access_key: str
    extra_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShowcaseResult:
    ok: bool
    state: ShowcaseState
    error: str | None = None
    channel_id: int | None = None
    message_id: int | None = None


SHOWCASES: tuple[ShowcaseSpec, ...] = (
    ShowcaseSpec(
        "verify",
        "Верификация",
        "vf",
        "verify",
        "verification",
        "showcase.post",
        extra_keys=("role_verified",),
    ),
    ShowcaseSpec(
        "self_roles",
        "Роли по выбору",
        "sr",
        "roles",
        "self_roles",
        "showcase.post",
        extra_keys=("role_sub_youtube", "role_sub_polls", "role_sub_announce", "role_sub_lfg"),
    ),
    ShowcaseSpec(
        "tickets",
        "Обращения",
        "tk",
        "tickets",
        "tickets",
        "showcase.post",
        extra_keys=("ticket_archive", "ticket_log"),
    ),
    ShowcaseSpec("lfg", "Поиск команды", "lf", "lfg", "lfg", "showcase.post"),
    ShowcaseSpec("stats", "Статистика", "st", "stats", "stats", "showcase.post"),
    ShowcaseSpec("automod", "Автомод", "am", "automod", "automod", "showcase.post"),
)


def showcase_by_id(showcase_id: str) -> ShowcaseSpec | None:
    return next((item for item in SHOWCASES if item.id == showcase_id), None)


def required_showcases() -> tuple[ShowcaseSpec, ...]:
    return SHOWCASES


def state_of(
    *,
    configured: bool,
    row_live: bool,
    message_alive: bool | None,
    channel_matches: bool,
) -> ShowcaseState:
    """message_alive is None — проверку не удалось: LIVE не понижается до STALE."""
    if not configured:
        return ShowcaseState.OFF
    if not row_live:
        return ShowcaseState.MISSING
    if message_alive is False:
        return ShowcaseState.STALE
    if not channel_matches:
        return ShowcaseState.MOVED
    return ShowcaseState.LIVE
