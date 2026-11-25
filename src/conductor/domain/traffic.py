"""Нормализация REST-маршрутов Discord, профили темпа и допуск. Без IO."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from conductor.domain.create_lock import (
    BUCKET_CHANNEL,
    BUCKET_DELETE,
    BUCKET_EMOJI,
    BUCKET_ROLE,
)
from conductor.domain.discord_limits import GUILD_CHANNELS

INVALID_BUDGET = 10_000
INVALID_WINDOW = 600.0
MAX_BUCKETS = 2000
TOP_STATUS = 5
BUSY_BUCKETS = 20
AIMD_WINDOW = 600.0
AIMD_MIN_SPACING = 0.25
AIMD_DEMAND_RATIO = 0.8
AIMD_FREEZE = 86_400.0
GLOBAL_CUT = 0.8
GLOBAL_CUT_HOLD = 600.0
GLOBAL_RECOVER = 600.0
GUILD_CHANNEL_CAP = GUILD_CHANNELS
GUILD_ROLE_CAP = 250
SUPPRESS_HITS = 3
SUPPRESS_WINDOW = 600.0
SUPPRESS_FOR = 3600.0
USER_BUDGET = 10.0
STAFF_BUDGET = 30.0
CLICK_WINDOW = 10.0
DEFER_ROUTE = "defer"
MESSAGE_TEMPLATE = "POST /channels/{id}/messages"
CHANNEL_PATCH_TEMPLATE = "PATCH /channels/{id}"

_API_PREFIX = re.compile(r"^/api/v\d+", re.IGNORECASE)
_MAJOR = frozenset({"guilds", "channels", "webhooks"})
_TOKEN_AFTER = frozenset({"interactions", "webhooks"})


class RateProfile(StrEnum):
    EDGE = "edge"
    BALANCED = "balanced"
    SAFE = "safe"


class Lane(IntEnum):
    INTERACTION = 0
    USER = 1
    STAFF = 2
    BACKGROUND = 3


class FuseLevel(StrEnum):
    OK = "ok"
    SOFT = "soft"
    THROTTLE = "throttle"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    global_rps_target: int
    outbound_concurrency: int
    max_ratelimit_wait: float
    invalid_soft: int
    invalid_throttle: int
    invalid_stop: int
    click_member_limit: int = 12
    click_member_window: float = CLICK_WINDOW
    click_guild_limit: int = 1200
    role_coalesce: float = 3.0


@dataclass(frozen=True, slots=True)
class BucketState:
    template: str
    major: str
    bucket_hash: str
    limit: int
    remaining: int
    reset_after: float
    scope: str
    status: int
    at: float


@dataclass(frozen=True, slots=True)
class TrafficSnapshot:
    rps_1s: int
    rps_60s: int
    invalid_10m: int
    global_429_10m: int
    top_429: tuple[str, ...]
    top_403: tuple[str, ...]
    buckets: tuple[BucketState, ...]


@dataclass(frozen=True, slots=True)
class Admit:
    pass


@dataclass(frozen=True, slots=True)
class Wait:
    seconds: float


@dataclass(frozen=True, slots=True)
class Defer:
    seconds: float


@dataclass(frozen=True, slots=True)
class AimdState:
    allowance: int
    frozen_until: float
    last_429_at: float | None
    window_start: float
    demand: int
    had_429: bool


@dataclass(frozen=True, slots=True)
class LaneWait:
    lane: str
    queue: int
    p50: float
    p95: float


@dataclass(frozen=True, slots=True)
class AimdView:
    bucket: str
    allowance: int
    frozen: bool


@dataclass(frozen=True, slots=True)
class GovernorSnapshot:
    profile: str
    rps_target: float
    fuse: str
    lanes: tuple[LaneWait, ...]
    allowances: tuple[AimdView, ...]


PROFILES: dict[RateProfile, ProfileLimits] = {
    RateProfile.EDGE: ProfileLimits(48, 24, 600.0, 2000, 3500, 4500, 12, CLICK_WINDOW, 1200, 3.0),
    RateProfile.BALANCED: ProfileLimits(40, 12, 120.0, 1500, 2500, 3500, 8, CLICK_WINDOW, 800, 30.0),
    RateProfile.SAFE: ProfileLimits(25, 8, 30.0, 1000, 1500, 2000, 5, CLICK_WINDOW, 400, 600.0),
}

AIMD_CAPS: dict[RateProfile, dict[str, int]] = {
    RateProfile.EDGE: {
        BUCKET_CHANNEL: 120,
        BUCKET_ROLE: 40,
        BUCKET_DELETE: 120,
        BUCKET_EMOJI: 40,
    },
    RateProfile.BALANCED: {
        BUCKET_CHANNEL: 80,
        BUCKET_ROLE: 24,
        BUCKET_DELETE: 80,
        BUCKET_EMOJI: 28,
    },
    RateProfile.SAFE: {
        BUCKET_CHANNEL: 50,
        BUCKET_ROLE: 12,
        BUCKET_DELETE: 50,
        BUCKET_EMOJI: 20,
    },
}

CREATE_TEMPLATES: dict[str, str] = {
    "POST /guilds/{id}/channels": BUCKET_CHANNEL,
    "POST /guilds/{id}/roles": BUCKET_ROLE,
    "POST /guilds/{id}/emojis": BUCKET_EMOJI,
    "POST /applications/{id}/emojis": BUCKET_EMOJI,
}

LANE_BUDGET: dict[Lane, float] = {
    Lane.INTERACTION: float("inf"),
    Lane.USER: USER_BUDGET,
    Lane.STAFF: STAFF_BUDGET,
    Lane.BACKGROUND: 0.0,
}

current_lane: ContextVar[Lane] = ContextVar("conductor_lane", default=Lane.BACKGROUND)
request_deadline: ContextVar[float | None] = ContextVar("conductor_request_deadline", default=None)
wait_notice: ContextVar[Callable[[float], object] | None] = ContextVar("conductor_wait_notice", default=None)


def route_key(method: str, path: str) -> tuple[str, str]:
    raw = str(path).split("?", 1)[0]
    raw = _API_PREFIX.sub("", raw)
    parts = [item for item in raw.split("/") if item]
    template: list[str] = []
    major = ""
    expect_token = False
    for index, part in enumerate(parts):
        if expect_token:
            template.append("{token}")
            expect_token = False
            continue
        prev = parts[index - 1] if index else ""
        if part.isdigit():
            template.append("{id}")
            if prev in _MAJOR and not major:
                major = part
            if prev in _TOKEN_AFTER:
                expect_token = True
            continue
        template.append(part)
    verb = str(method or "").upper()
    path_tmpl = "/" + "/".join(template) if template else "/"
    return f"{verb} {path_tmpl}", major


def is_interaction_route(template: str) -> bool:
    return "/{token}" in str(template)


def is_invalid(status: int, scope: str) -> bool:
    code = int(status)
    if code in {401, 403}:
        return True
    # 429 со scope shared — общий лимит ресурса, Discord его нам не засчитывает.
    return code == 429 and str(scope) != "shared"


def apply_overrides(
    base: ProfileLimits,
    *,
    global_rps_target: int | None = None,
    outbound_concurrency: int | None = None,
    max_ratelimit_wait: float | None = None,
    invalid_request_stop: int | None = None,
) -> ProfileLimits:
    rps = base.global_rps_target if global_rps_target is None else int(global_rps_target)
    conc = base.outbound_concurrency if outbound_concurrency is None else int(outbound_concurrency)
    wait = base.max_ratelimit_wait if max_ratelimit_wait is None else float(max_ratelimit_wait)
    stop = base.invalid_stop if invalid_request_stop is None else int(invalid_request_stop)
    return ProfileLimits(
        global_rps_target=max(1, min(50, rps)),
        outbound_concurrency=max(1, min(50, conc)),
        max_ratelimit_wait=max(1.0, min(600.0, wait)),
        invalid_soft=base.invalid_soft,
        invalid_throttle=base.invalid_throttle,
        invalid_stop=max(1, min(INVALID_BUDGET, stop)),
        click_member_limit=base.click_member_limit,
        click_member_window=base.click_member_window,
        click_guild_limit=base.click_guild_limit,
        role_coalesce=base.role_coalesce,
    )


def lane_budget(lane: Lane) -> float:
    return float(LANE_BUDGET.get(lane, 0.0))


def admit(
    lane: Lane,
    bucket: BucketState | None,
    now: float,
    limits: ProfileLimits | None = None,
) -> Admit | Wait | Defer:
    del limits
    if lane is Lane.INTERACTION:
        return Admit()
    if bucket is None:
        return Admit()
    reset = max(0.0, float(bucket.reset_after) - (float(now) - float(bucket.at)))
    if reset <= 0.0 or int(bucket.remaining) > 0:
        return Admit()
    budget = lane_budget(lane)
    if reset <= budget:
        return Wait(reset)
    return Defer(reset)


def seed_allowance(bucket: str) -> int:
    # Начальная оценка для исторической AIMD-диагностики; запросы ею не ограничены.
    return {BUCKET_ROLE: 12, BUCKET_CHANNEL: 50, BUCKET_EMOJI: 20, BUCKET_DELETE: 50}.get(str(bucket), 50)


def aimd_cap(profile: RateProfile, bucket: str) -> int:
    caps = AIMD_CAPS.get(profile, AIMD_CAPS[RateProfile.SAFE])
    return int(caps.get(str(bucket), seed_allowance(bucket)))


def aimd_spacing(allowance: int, *, window: float = AIMD_WINDOW) -> float:
    return max(AIMD_MIN_SPACING, float(window) / max(1, int(allowance)))


def aimd_drop(allowance: int) -> int:
    return max(1, int(int(allowance) * 0.5))


def aimd_advance(
    state: AimdState,
    now: float,
    *,
    window: float,
    cap: int,
    demand: int = 0,
    hit_429: bool = False,
    freeze_for: float = AIMD_FREEZE,
) -> AimdState:
    start = float(state.window_start)
    frozen = float(state.frozen_until)
    allowance = int(state.allowance)
    total = int(state.demand)
    had_429 = bool(state.had_429)
    last_429 = state.last_429_at
    moment = float(now)
    # Ноль — допустимая метка времени на поддельных часах: «не задано» — только отрицательное.
    if start < 0:
        start = moment
    elapsed = moment - start
    if elapsed >= float(window):
        grow = (
            (not had_429)
            and moment >= frozen
            and allowance < cap
            and total >= AIMD_DEMAND_RATIO * max(1, allowance)
        )
        if grow:
            allowance = min(cap, allowance + 1)
        start = moment
        total = 0
        had_429 = False
    total += max(0, int(demand))
    if hit_429:
        allowance = aimd_drop(allowance)
        frozen = moment + float(freeze_for)
        last_429 = moment
        had_429 = True
    return AimdState(
        allowance=allowance,
        frozen_until=frozen,
        last_429_at=last_429,
        window_start=start,
        demand=total,
        had_429=had_429,
    )


def fuse_level(invalid_10m: int, limits: ProfileLimits) -> FuseLevel:
    count = int(invalid_10m)
    if count >= limits.invalid_stop:
        return FuseLevel.STOP
    if count >= limits.invalid_throttle:
        return FuseLevel.THROTTLE
    if count >= limits.invalid_soft:
        return FuseLevel.SOFT
    return FuseLevel.OK


def damped_rps(target: int, now: float, cut_until: float) -> float:
    goal = float(max(1, int(target)))
    moment = float(now)
    until = float(cut_until)
    if moment < until:
        return goal * GLOBAL_CUT
    recovered = moment - until
    if recovered >= GLOBAL_RECOVER:
        return goal
    return goal * (GLOBAL_CUT + (1.0 - GLOBAL_CUT) * (recovered / GLOBAL_RECOVER))


def suppress_until(
    hits: tuple[float, ...],
    now: float,
    *,
    window: float = SUPPRESS_WINDOW,
    need: int = SUPPRESS_HITS,
    hold: float = SUPPRESS_FOR,
) -> float | None:
    moment = float(now)
    recent = tuple(stamp for stamp in hits if moment - float(stamp) <= float(window))
    if len(recent) >= int(need):
        return moment + float(hold)
    return None


def resource_capped(bucket: str, *, channel_count: int | None = None, role_count: int | None = None) -> bool:
    name = str(bucket)
    if name == BUCKET_CHANNEL and channel_count is not None:
        return int(channel_count) >= GUILD_CHANNEL_CAP
    if name == BUCKET_ROLE and role_count is not None:
        return int(role_count) >= GUILD_ROLE_CAP
    return False


def split_concurrency(total: int) -> dict[Lane, int]:
    n = max(3, int(total))
    user = max(1, n // 2)
    staff = max(1, n // 3)
    background = n - user - staff
    if background < 1:
        background = 1
        if user > 1:
            user -= 1
        elif staff > 1:
            staff -= 1
    return {Lane.USER: user, Lane.STAFF: staff, Lane.BACKGROUND: background}


def percentile(samples: tuple[float, ...], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(float(item) for item in samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = min(len(ordered) - 1, max(0, int(round((float(pct) / 100.0) * (len(ordered) - 1)))))
    return ordered[rank]


__all__ = [
    "AIMD_CAPS",
    "AIMD_DEMAND_RATIO",
    "AIMD_FREEZE",
    "AIMD_MIN_SPACING",
    "AIMD_WINDOW",
    "Admit",
    "AimdState",
    "AimdView",
    "BUSY_BUCKETS",
    "BucketState",
    "CHANNEL_PATCH_TEMPLATE",
    "CLICK_WINDOW",
    "CREATE_TEMPLATES",
    "DEFER_ROUTE",
    "Defer",
    "FuseLevel",
    "GLOBAL_CUT",
    "GLOBAL_CUT_HOLD",
    "GLOBAL_RECOVER",
    "GUILD_CHANNEL_CAP",
    "GUILD_ROLE_CAP",
    "GovernorSnapshot",
    "INVALID_BUDGET",
    "INVALID_WINDOW",
    "LANE_BUDGET",
    "Lane",
    "LaneWait",
    "MAX_BUCKETS",
    "MESSAGE_TEMPLATE",
    "PROFILES",
    "ProfileLimits",
    "RateProfile",
    "STAFF_BUDGET",
    "SUPPRESS_FOR",
    "SUPPRESS_HITS",
    "SUPPRESS_WINDOW",
    "TOP_STATUS",
    "TrafficSnapshot",
    "USER_BUDGET",
    "Wait",
    "admit",
    "aimd_advance",
    "aimd_cap",
    "aimd_drop",
    "aimd_spacing",
    "apply_overrides",
    "current_lane",
    "damped_rps",
    "fuse_level",
    "is_interaction_route",
    "is_invalid",
    "lane_budget",
    "percentile",
    "resource_capped",
    "route_key",
    "seed_allowance",
    "split_concurrency",
    "suppress_until",
    "wait_notice",
]
