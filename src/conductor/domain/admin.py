"""Правила панели /admin: идентификаторы страниц, разбор полей. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from conductor.domain.access import Rank
from conductor.domain.moderation import DurationVerdict, parse_timeout_duration

PERM_PAGE = 10
ROLE_CREATE_VALUE = "__create__"
PICK_EMPTY_VALUE = "__empty__"

PAGE_GENERAL = "general"
PAGE_LOGS = "logs"
PAGE_RANKS = "ranks"
PAGE_PERMS = "perms"
PAGE_STAFFING = "staffing"
PAGE_MUTE = "mute"
PAGE_QUOTAS = "quotas"
PAGE_OLD = "old"
PAGE_REPUTATION = "reputation"
PAGE_LADDER = "ladder"
PAGE_ACHIEVEMENTS = "achievements"
PAGE_GIVEAWAY = "giveaway"
PAGE_VOICE = "voice"
PAGE_TICKETS = "tickets"
PAGE_AUTOMOD = "automod"
PAGE_VERIFICATION = "verification"
PAGE_SELFROLES = "selfroles"
PAGE_STATS = "stats"
PAGE_POLLS = "polls"
PAGE_YOUTUBE = "youtube"
PAGE_LFG = "lfg"
PAGE_GROUPS = "groups"
PAGE_RECRUIT = "recruit"
PAGE_ALERTS = "alerts"
PAGE_SKELETON = "skeleton"
PAGE_DATA = "data"
PAGE_SHOWCASE = "showcase"


@dataclass(frozen=True, slots=True)
class AdminCtx:
    bot: Any
    guild_id: int
    actor_id: int
    guild: Any | None = None
    member_role_ids: tuple[int, ...] = ()
    is_administrator: bool = False
    is_developer: bool = False
    suspended: bool = False


@dataclass(frozen=True, slots=True)
class PageControl:
    kind: str
    id: str
    label: str
    options: tuple[tuple[str, ...], ...] = ()
    current: str | None = None
    min_values: int = 1
    max_values: int = 1
    channel_types: tuple[str, ...] = ()
    exclude_ids: tuple[int, ...] = ()
    allow_create: bool = False
    hint_owned: bool = True


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    fields: dict[str, object]
    controls: tuple[PageControl, ...]
    footnote: str | None = None
    leaf: int = 1


@dataclass(frozen=True, slots=True)
class AdminResult:
    ok: bool = True
    error: str | None = None
    field: str | None = None
    before: str | None = None
    after: str | None = None
    modal: str | None = None
    target: str | None = None
    lo: int | None = None
    hi: int | None = None
    file: bytes | None = None
    filename: str | None = None
    leaf: int | None = None
    labels: tuple[str, ...] = ()
    defaults: tuple[str, ...] = ()
    title: str | None = None
    confirm: str | None = None
    progress: str | None = None


class ReadyState(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class ReadyIssue:
    code: str
    label: str
    fix_control: str = ""
    skeleton_key: str = ""


@dataclass(frozen=True, slots=True)
class ReadyReport:
    state: ReadyState
    issues: tuple[ReadyIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state is ReadyState.READY


@dataclass(frozen=True, slots=True)
class AdminPage:
    id: str
    label: str
    key: str
    ready: Callable[[AdminCtx], Awaitable[ReadyReport]]
    snapshot: Callable[[AdminCtx, int], Awaitable[PageSnapshot]]
    handle: Callable[[AdminCtx, str, str], Awaitable[AdminResult]]
    keys: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ControlFilter:
    allowed: tuple[PageControl, ...]
    hidden: tuple[tuple[str, Rank | None], ...]


def page_access_keys(page: AdminPage) -> frozenset[str]:
    return frozenset((page.key, *page.keys.values()))


def control_key(page: AdminPage, control_id: str) -> str:
    mapping = page.keys
    if not mapping:
        return page.key
    if control_id in mapping:
        return mapping[control_id]
    cursor = control_id
    while True:
        cut = cursor.rsplit(":", 1)[0] if ":" in cursor else cursor.rsplit(".", 1)[0] if "." in cursor else ""
        if not cut or cut == cursor:
            break
        if cut in mapping:
            return mapping[cut]
        cursor = cut
    return page.key


COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Moscow",
    "Europe/Kyiv",
    "Europe/Minsk",
    "Europe/Kaliningrad",
    "Europe/Samara",
    "Europe/Volgograd",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Novosibirsk",
    "Asia/Krasnoyarsk",
    "Asia/Irkutsk",
    "Asia/Yakutsk",
    "Asia/Vladivostok",
    "Asia/Magadan",
    "Asia/Kamchatka",
    "Europe/Berlin",
    "Europe/London",
    "Europe/Warsaw",
    "Europe/Istanbul",
    "Asia/Almaty",
    "Asia/Tashkent",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Tokyo",
)


def usable_timezones() -> tuple[str, ...]:
    return tuple(name for name in COMMON_TIMEZONES if parse_timezone(name) is not None)


def parse_timezone(raw: str) -> str | None:
    name = str(raw or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ModuleNotFoundError, ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def parse_int_range(raw: str, *, lo: int, hi: int) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < lo or value > hi:
        return None
    return value


def parse_duration(raw: str) -> timedelta | None:
    verdict, duration = parse_timeout_duration(raw)
    if verdict is not DurationVerdict.OK or duration is None:
        return None
    return duration


def parse_int_group(raw: str, *, count: int) -> tuple[int, ...] | None:
    parts = str(raw or "").replace(",", " ").replace(";", " ").split()
    if len(parts) != count:
        return None
    found: list[int] = []
    for part in parts:
        try:
            found.append(int(part))
        except ValueError:
            return None
    return tuple(found)


def parse_id_list(raw: str) -> tuple[int, ...] | None:
    text = str(raw or "").strip()
    if not text:
        return ()
    found: list[int] = []
    for part in text.replace(",", " ").replace(";", " ").split():
        try:
            value = int(part)
        except ValueError:
            return None
        if value <= 0:
            return None
        found.append(value)
    return tuple(found)


def perm_chunks(keys: Sequence[str], *, size: int = PERM_PAGE) -> tuple[tuple[str, ...], ...]:
    width = max(1, int(size))
    rows = tuple(tuple(keys[index : index + width]) for index in range(0, len(keys), width))
    return rows or ((),)
