"""Лестница активности: пороги заказчика, заморозка уровня 0, без понижения. Без Discord и БД."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from conductor.domain.activity import as_utc

DAY = timedelta(days=1)
HOUR = 3600

REGULAR_MESSAGES = 50
REGULAR_VOICE_SECONDS = 0
REGULAR_REACTIONS = 0
RESIDENT_MESSAGES = 500
RESIDENT_VOICE_SECONDS = 50 * HOUR
RESIDENT_DAYS = 30
RESIDENT_STRONG_MESSAGES = 0
RESIDENT_STRONG_VOICE_SECONDS = 0
FRIEND_TENURE_DAYS = 365
FRIEND_MESSAGES = 1000
FRIEND_VOICE_SECONDS = 100 * HOUR
FRIEND_REACTIONS = 0


class Step(IntEnum):
    GUEST = 0
    REGULAR = 1
    RESIDENT = 2
    FRIEND = 3

    @property
    def key(self) -> str:
        return _STEP_KEYS[self]


_STEP_KEYS: dict[Step, str] = {
    Step.GUEST: "guest",
    Step.REGULAR: "regular",
    Step.RESIDENT: "resident",
    Step.FRIEND: "friend",
}
_STEPS_BY_KEY: dict[str, Step] = {key: step for step, key in _STEP_KEYS.items()}
STEPS: tuple[Step, ...] = tuple(Step)


class GapKind(StrEnum):
    MESSAGES = "messages"
    VOICE = "voice"
    REACTIONS = "reactions"
    TENURE = "tenure"


@dataclass(frozen=True, slots=True)
class LadderConfig:
    regular_messages: int = REGULAR_MESSAGES
    regular_voice_seconds: int = REGULAR_VOICE_SECONDS
    regular_reactions: int = REGULAR_REACTIONS
    resident_messages: int = RESIDENT_MESSAGES
    resident_voice_seconds: int = RESIDENT_VOICE_SECONDS
    resident_days: int = RESIDENT_DAYS
    resident_strong_messages: int = RESIDENT_STRONG_MESSAGES
    resident_strong_voice_seconds: int = RESIDENT_STRONG_VOICE_SECONDS
    friend_tenure_days: int = FRIEND_TENURE_DAYS
    friend_messages: int = FRIEND_MESSAGES
    friend_voice_seconds: int = FRIEND_VOICE_SECONDS
    friend_reactions: int = FRIEND_REACTIONS


DEFAULT_CONFIG = LadderConfig()


@dataclass(frozen=True, slots=True)
class LadderRoles:
    guest: int | None = None
    regular: int | None = None
    resident: int | None = None
    friend: int | None = None

    def for_step(self, step: Step) -> int | None:
        return {
            Step.GUEST: self.guest,
            Step.REGULAR: self.regular,
            Step.RESIDENT: self.resident,
            Step.FRIEND: self.friend,
        }[step]

    def bound(self) -> tuple[int, ...]:
        return tuple(role_id for role_id in (self.guest, self.regular, self.resident, self.friend) if role_id)

    def mapping(self) -> dict[Step, int]:
        found: dict[Step, int] = {}
        for step in STEPS:
            role_id = self.for_step(step)
            if role_id:
                found[step] = role_id
        return found


@dataclass(frozen=True, slots=True)
class LadderStats:
    messages: int = 0
    reactions: int = 0
    voice_seconds: int = 0


@dataclass(frozen=True, slots=True)
class LadderGap:
    kind: GapKind
    remaining: int


@dataclass(frozen=True, slots=True)
class LadderNeed:
    step: Step | None
    messages_have: int
    messages_need: int
    voice_have: int
    voice_need: int
    reactions_have: int
    reactions_need: int
    tenure_have: int
    tenure_need: int
    gap: LadderGap | None
    frozen: bool


@dataclass(frozen=True, slots=True)
class LadderCard:
    user_id: int
    held: Step
    earned: Step
    desired: Step
    sinner: bool
    stats: LadderStats
    need: LadderNeed
    tenure_days: int


@dataclass(frozen=True, slots=True)
class LadderPanel:
    config: LadderConfig
    roles: LadderRoles


def parse_step(value: str | Step | None) -> Step:
    if isinstance(value, Step):
        return value
    if value is None:
        return Step.GUEST
    return _STEPS_BY_KEY.get(str(value).strip().lower(), Step.GUEST)


def next_step(held: Step) -> Step | None:
    index = int(held) + 1
    if index >= len(STEPS):
        return None
    return STEPS[index]


def max_step(left: Step, right: Step) -> Step:
    return left if left >= right else right


def tenure_days(joined_at: datetime | None, now: datetime) -> int:
    if joined_at is None:
        return 0
    delta = as_utc(now) - as_utc(joined_at)
    return max(0, int(delta.total_seconds() // DAY.total_seconds()))


def message_score(stats: LadderStats) -> int:
    return stats.messages


def settings_need_defaults(config: LadderConfig) -> bool:
    return config != DEFAULT_CONFIG


def qualifies_regular(stats: LadderStats, config: LadderConfig = DEFAULT_CONFIG) -> bool:
    return (
        _or_ok(message_score(stats), config.regular_messages)
        or _or_ok(stats.voice_seconds, config.regular_voice_seconds)
        or _or_ok(stats.reactions, config.regular_reactions)
    )


def qualifies_resident(
    stats: LadderStats,
    config: LadderConfig = DEFAULT_CONFIG,
    *,
    now: datetime,
    joined_at: datetime | None,
) -> bool:
    scored = message_score(stats)
    tenure = tenure_days(joined_at, now)
    core = _all_and(
        (scored, config.resident_messages),
        (stats.voice_seconds, config.resident_voice_seconds),
        (tenure, config.resident_days),
    )
    strong = _or_ok(scored, config.resident_strong_messages) or _or_ok(
        stats.voice_seconds, config.resident_strong_voice_seconds
    )
    return core or strong


def qualifies_friend(
    stats: LadderStats,
    config: LadderConfig = DEFAULT_CONFIG,
    *,
    now: datetime,
    joined_at: datetime | None,
) -> bool:
    scored = message_score(stats)
    tenure = tenure_days(joined_at, now)
    if not _and_ok(tenure, config.friend_tenure_days):
        return False
    if not _and_ok(scored, config.friend_messages):
        return False
    if _enabled(config.friend_reactions):
        return _or_ok(stats.voice_seconds, config.friend_voice_seconds) or _or_ok(
            stats.reactions, config.friend_reactions
        )
    core = (config.friend_messages, config.friend_voice_seconds, config.friend_tenure_days)
    if not any(_enabled(value) for value in core):
        return False
    return _and_ok(stats.voice_seconds, config.friend_voice_seconds)


def earned_step(
    stats: LadderStats,
    config: LadderConfig = DEFAULT_CONFIG,
    *,
    now: datetime,
    joined_at: datetime | None,
) -> Step:
    if qualifies_friend(stats, config, now=now, joined_at=joined_at):
        return Step.FRIEND
    if qualifies_resident(stats, config, now=now, joined_at=joined_at):
        return Step.RESIDENT
    if qualifies_regular(stats, config):
        return Step.REGULAR
    return Step.GUEST


def desired_step(*, earned: Step, held: Step, sinner: bool) -> Step:
    """Уровень 0 замораживает ступень. Понижения нет: берём максимум из заслуженной и уже выданной."""
    if sinner:
        return held
    return max_step(earned, held)


def held_from_roles(have: Iterable[int], roles: LadderRoles) -> Step:
    have_ids = set(have)
    found = Step.GUEST
    for step in STEPS:
        role_id = roles.for_step(step)
        if role_id and role_id in have_ids:
            found = step
    return found


def role_plan(
    *,
    have: Iterable[int],
    desired: Step,
    roles: LadderRoles,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    have_ids = set(have)
    want = roles.for_step(desired)
    owned = set(roles.bound())
    add = (want,) if want and want not in have_ids else ()
    remove = tuple(sorted(role_id for role_id in owned if role_id in have_ids and role_id != want))
    return add, remove


def blocking_gap(
    held: Step,
    stats: LadderStats,
    config: LadderConfig,
    *,
    now: datetime,
    joined_at: datetime | None,
) -> LadderGap | None:
    nxt = next_step(held)
    if nxt is None:
        return None
    if earned_step(stats, config, now=now, joined_at=joined_at) >= nxt:
        return None
    if nxt is Step.REGULAR:
        return _closest_or(
            (GapKind.MESSAGES, stats.messages, config.regular_messages),
            (GapKind.VOICE, stats.voice_seconds, config.regular_voice_seconds),
            (GapKind.REACTIONS, stats.reactions, config.regular_reactions),
        )
    if nxt is Step.RESIDENT:
        if _or_ok(stats.messages, config.resident_strong_messages) or _or_ok(
            stats.voice_seconds, config.resident_strong_voice_seconds
        ):
            return None
        return _closest_and(
            (GapKind.MESSAGES, stats.messages, config.resident_messages),
            (GapKind.VOICE, stats.voice_seconds, config.resident_voice_seconds),
            (GapKind.TENURE, tenure_days(joined_at, now), config.resident_days),
        )
    tenure = tenure_days(joined_at, now)
    if _enabled(config.friend_tenure_days) and tenure < config.friend_tenure_days:
        return LadderGap(GapKind.TENURE, config.friend_tenure_days - tenure)
    if _enabled(config.friend_messages) and stats.messages < config.friend_messages:
        return LadderGap(GapKind.MESSAGES, config.friend_messages - stats.messages)
    if _enabled(config.friend_reactions):
        return _closest_or(
            (GapKind.VOICE, stats.voice_seconds, config.friend_voice_seconds),
            (GapKind.REACTIONS, stats.reactions, config.friend_reactions),
        )
    if _enabled(config.friend_voice_seconds) and stats.voice_seconds < config.friend_voice_seconds:
        return LadderGap(GapKind.VOICE, config.friend_voice_seconds - stats.voice_seconds)
    return None


def next_need(
    held: Step,
    stats: LadderStats,
    config: LadderConfig,
    *,
    now: datetime,
    joined_at: datetime | None,
    sinner: bool,
) -> LadderNeed:
    nxt = next_step(held)
    tenure = tenure_days(joined_at, now)
    gap = blocking_gap(held, stats, config, now=now, joined_at=joined_at)
    if nxt is Step.REGULAR:
        return LadderNeed(
            step=nxt,
            messages_have=stats.messages,
            messages_need=config.regular_messages,
            voice_have=stats.voice_seconds,
            voice_need=config.regular_voice_seconds,
            reactions_have=stats.reactions,
            reactions_need=config.regular_reactions,
            tenure_have=tenure,
            tenure_need=0,
            gap=gap,
            frozen=sinner,
        )
    if nxt is Step.RESIDENT:
        return LadderNeed(
            step=nxt,
            messages_have=stats.messages,
            messages_need=config.resident_messages,
            voice_have=stats.voice_seconds,
            voice_need=config.resident_voice_seconds,
            reactions_have=stats.reactions,
            reactions_need=0,
            tenure_have=tenure,
            tenure_need=config.resident_days,
            gap=gap,
            frozen=sinner,
        )
    if nxt is Step.FRIEND:
        return LadderNeed(
            step=nxt,
            messages_have=stats.messages,
            messages_need=config.friend_messages,
            voice_have=stats.voice_seconds,
            voice_need=config.friend_voice_seconds,
            reactions_have=stats.reactions,
            reactions_need=config.friend_reactions,
            tenure_have=tenure,
            tenure_need=config.friend_tenure_days,
            gap=gap,
            frozen=sinner,
        )
    return LadderNeed(
        step=None,
        messages_have=stats.messages,
        messages_need=0,
        voice_have=stats.voice_seconds,
        voice_need=0,
        reactions_have=stats.reactions,
        reactions_need=0,
        tenure_have=tenure,
        tenure_need=0,
        gap=None,
        frozen=sinner,
    )


def parse_ladder_config(
    current: LadderConfig,
    *,
    regular_messages: int | None = None,
    regular_voice_minutes: int | None = None,
    regular_reactions: int | None = None,
    resident_messages: int | None = None,
    resident_voice_hours: int | None = None,
    resident_days: int | None = None,
    resident_strong_messages: int | None = None,
    resident_strong_voice_hours: int | None = None,
    friend_days: int | None = None,
    friend_messages: int | None = None,
    friend_voice_hours: int | None = None,
    friend_reactions: int | None = None,
) -> LadderConfig | None:
    regular_msg = _bound(current.regular_messages if regular_messages is None else regular_messages, 0, 1_000_000)
    regular_voice = _minutes(
        current.regular_voice_seconds // 60 if regular_voice_minutes is None else regular_voice_minutes
    )
    regular_react = _bound(current.regular_reactions if regular_reactions is None else regular_reactions, 0, 1_000_000)
    resident_msg = _bound(current.resident_messages if resident_messages is None else resident_messages, 0, 1_000_000)
    resident_voice = _hours(
        current.resident_voice_seconds // HOUR if resident_voice_hours is None else resident_voice_hours
    )
    resident_tenure = _bound(current.resident_days if resident_days is None else resident_days, 0, 3650)
    strong_msg = _bound(
        current.resident_strong_messages if resident_strong_messages is None else resident_strong_messages,
        0,
        1_000_000,
    )
    strong_voice = _hours(
        current.resident_strong_voice_seconds // HOUR
        if resident_strong_voice_hours is None
        else resident_strong_voice_hours
    )
    friend_tenure = _bound(current.friend_tenure_days if friend_days is None else friend_days, 0, 3650)
    friend_msg = _bound(current.friend_messages if friend_messages is None else friend_messages, 0, 1_000_000)
    friend_voice = _hours(current.friend_voice_seconds // HOUR if friend_voice_hours is None else friend_voice_hours)
    friend_react = _bound(current.friend_reactions if friend_reactions is None else friend_reactions, 0, 1_000_000)
    values = (
        regular_msg,
        regular_voice,
        regular_react,
        resident_msg,
        resident_voice,
        resident_tenure,
        strong_msg,
        strong_voice,
        friend_tenure,
        friend_msg,
        friend_voice,
        friend_react,
    )
    if None in values:
        return None
    return LadderConfig(
        regular_messages=regular_msg,
        regular_voice_seconds=regular_voice * 60,
        regular_reactions=regular_react,
        resident_messages=resident_msg,
        resident_voice_seconds=resident_voice * HOUR,
        resident_days=resident_tenure,
        resident_strong_messages=strong_msg,
        resident_strong_voice_seconds=strong_voice * HOUR,
        friend_tenure_days=friend_tenure,
        friend_messages=friend_msg,
        friend_voice_seconds=friend_voice * HOUR,
        friend_reactions=friend_react,
    )


def ladder_bar(have: int, need: int, width: int = 10) -> str:
    high = max(1, need)
    clamped = min(max(have, 0), high)
    filled = round(width * clamped / high) if width > 0 else 0
    filled = min(max(filled, 0), max(width, 0))
    bar = "▰" * filled + "▱" * (max(width, 0) - filled)
    return f"{bar} {have}/{need}"


def _enabled(need: int) -> bool:
    return int(need) > 0


def _or_ok(have: int, need: int) -> bool:
    return _enabled(need) and have >= need


def _and_ok(have: int, need: int) -> bool:
    return not _enabled(need) or have >= need


def _all_and(*pairs: tuple[int, int]) -> bool:
    enabled = [(have, need) for have, need in pairs if _enabled(need)]
    if not enabled:
        return False
    return all(have >= need for have, need in enabled)


def _closest_or(*rows: tuple[GapKind, int, int]) -> LadderGap | None:
    open_paths = [LadderGap(kind, max(0, need - have)) for kind, have, need in rows if _enabled(need) and have < need]
    if not open_paths:
        return None
    return min(open_paths, key=lambda gap: gap.remaining)


def _closest_and(*rows: tuple[GapKind, int, int]) -> LadderGap | None:
    missing = [LadderGap(kind, max(0, need - have)) for kind, have, need in rows if _enabled(need) and have < need]
    if not missing:
        return None
    return min(missing, key=lambda gap: gap.remaining)


def _bound(value: int, low: int, high: int) -> int | None:
    if value < low or value > high:
        return None
    return value


def _minutes(value: int) -> int | None:
    return _bound(value, 0, 24 * 60 * 365)


def _hours(value: int) -> int | None:
    return _bound(value, 0, 24 * 365)
