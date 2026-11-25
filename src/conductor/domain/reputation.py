"""Счёт репутации: падения по делам, часовой предел, рост, шесть уровней. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.activity import as_utc

START = 50
FLOOR = 0
CEILING = 100
WARN_HIT = 5
TIMEOUT_STEP_SECONDS = 10 * 60
TIMEOUT_PER_STEP = 2
TIMEOUT_MIN = 2
TIMEOUT_MAX = 20
MUTE_HIT = 10
KICK_HIT = 0
MAX_HIT_PER_HOUR = 40
MESSAGES_PER_POINT = 50
VOICE_SECONDS_PER_POINT = 30 * 60
GROWTH_INTERVAL = timedelta(hours=1)
SINNER_THRESHOLD = 15
MONTH = timedelta(days=30)
TIER_COUNT = 6
TIER_BOUNDS: tuple[tuple[int, int], ...] = (
    (0, 10),
    (10, 30),
    (30, 50),
    (50, 70),
    (70, 90),
    (90, 101),
)

KIND_WARN = "warn"
KIND_TIMEOUT = "timeout"
KIND_MUTE = "mute"
KIND_KICK = "kick"
KIND_GROWTH = "growth"
KIND_PARDON = "pardon"
KIND_REVOKE = "revoke"
KIND_ACHIEVEMENT = "achievement"
PARDON_BONUS = 5


@dataclass(frozen=True, slots=True)
class RepConfig:
    start: int = START
    floor: int = FLOOR
    ceiling: int = CEILING
    warn_hit: int = WARN_HIT
    timeout_step_seconds: int = TIMEOUT_STEP_SECONDS
    timeout_per_step: int = TIMEOUT_PER_STEP
    timeout_min: int = TIMEOUT_MIN
    timeout_max: int = TIMEOUT_MAX
    mute_hit: int = MUTE_HIT
    kick_hit: int = KICK_HIT
    max_hit_per_hour: int = MAX_HIT_PER_HOUR
    messages_per_point: int = MESSAGES_PER_POINT
    voice_seconds_per_point: int = VOICE_SECONDS_PER_POINT
    growth_interval: timedelta = GROWTH_INTERVAL
    sinner_threshold: int = SINNER_THRESHOLD
    dry_run: bool = False


DEFAULT_CONFIG = RepConfig()


@dataclass(frozen=True, slots=True)
class RepTier:
    tier: int
    lower: int
    upper: int
    name: str
    role_id: int | None = None


@dataclass(frozen=True, slots=True)
class HitPlan:
    applied: int
    cut: int
    score: int


@dataclass(frozen=True, slots=True)
class GrowthPlan:
    delta: int
    score: int
    consume_messages: int
    consume_voice: int
    grew_at: datetime | None


class AutobanVerdict(StrEnum):
    SKIP = "skip"
    DRY = "dry"
    LOCKED = "locked"
    BAN = "ban"


@dataclass(frozen=True, slots=True)
class GrowthWait:
    ready: bool
    until: datetime | None
    messages_left: int
    voice_seconds_left: int
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class MonthHit:
    kind: str
    total: int


@dataclass(frozen=True, slots=True)
class RepDossier:
    user_id: int
    score: int
    ceiling: int
    month: tuple[MonthHit, ...]
    wait: GrowthWait
    dry_run: bool
    sinner: bool = False
    banned_for_reputation: bool = False
    tier: int = 3
    tier_name: str = ""
    next_name: str | None = None
    until_next: int | None = None


@dataclass(frozen=True, slots=True)
class RepPanel:
    config: RepConfig
    appeal_contact: str = ""
    tiers: tuple[RepTier, ...] = ()


@dataclass(frozen=True, slots=True)
class PardonResult:
    score: int
    unbanned: bool
    own_ban: bool


@dataclass(frozen=True, slots=True)
class ScoreChanged:
    guild_id: int
    user_id: int
    score: int
    sinner: bool
    tier: int = 0
    frozen: bool = False


def default_tiers(names: Sequence[str] | None = None) -> tuple[RepTier, ...]:
    if names is not None and len(names) == TIER_COUNT:
        labels = tuple(names)
    else:
        labels = tuple(str(index) for index in range(TIER_COUNT))
    return tuple(
        RepTier(tier=index, lower=low, upper=high, name=labels[index]) for index, (low, high) in enumerate(TIER_BOUNDS)
    )


TIER_HYSTERESIS = 2


def tier_for(score: int, tiers: Sequence[RepTier] | None = None) -> int:
    value = clamp_score(score)
    rows = tuple(tiers) if tiers else default_tiers()
    if not rows:
        return 0
    last = max(row.tier for row in rows)
    for row in rows:
        if row.tier == last:
            if value >= row.lower:
                return row.tier
        elif row.lower <= value < row.upper:
            return row.tier
    return 0


def desired_tier(
    score: int,
    held: int,
    tiers: Sequence[RepTier] | None = None,
    *,
    margin: int = TIER_HYSTERESIS,
) -> int:
    """Снять роль только если счёт ушёл за нижнюю границу ступени с запасом."""
    earned = tier_for(score, tiers)
    held_tier = int(held)
    if earned >= held_tier:
        return earned
    rows = tuple(tiers) if tiers else default_tiers()
    bound = next((row.lower for row in rows if row.tier == held_tier), None)
    if bound is None:
        return earned
    if clamp_score(score) < int(bound) - max(0, int(margin)):
        return earned
    return held_tier


def is_frozen(tier: int) -> bool:
    return int(tier) == 0


def is_sinner(score: int, config: RepConfig = DEFAULT_CONFIG) -> bool:
    _ = config
    return is_frozen(tier_for(score))


def pardon_score(config: RepConfig = DEFAULT_CONFIG) -> int:
    return clamp_score(config.start, config)


def next_tier_need(score: int, tiers: Sequence[RepTier] | None = None) -> tuple[int | None, RepTier | None]:
    rows = tuple(tiers) if tiers else default_tiers()
    current = tier_for(score, rows)
    nxt = next((row for row in rows if row.tier == current + 1), None)
    if nxt is None:
        return None, None
    return max(0, nxt.lower - clamp_score(score)), nxt


def settings_need_defaults(start: int, kick_hit: int) -> bool:
    return int(start) != START or int(kick_hit) != KICK_HIT


def parse_tier_names(raw: str) -> tuple[str, ...] | None:
    lines = [line.strip() for line in str(raw or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) != TIER_COUNT:
        return None
    if any(not line or len(line) > 80 for line in lines):
        return None
    return tuple(lines)


def autoban_verdict(
    *,
    score: int,
    banned_for_reputation: bool,
    dry_run: bool,
    moderation_open: bool,
    punishing: bool,
) -> AutobanVerdict:
    if not punishing or banned_for_reputation or score > FLOOR:
        return AutobanVerdict.SKIP
    if dry_run:
        return AutobanVerdict.DRY
    if not moderation_open:
        return AutobanVerdict.LOCKED
    return AutobanVerdict.BAN


def punishing_kind(kind: str) -> bool:
    return kind.strip().lower() in {KIND_WARN, KIND_TIMEOUT, KIND_MUTE, KIND_KICK}


def clamp_score(score: int, config: RepConfig = DEFAULT_CONFIG) -> int:
    low = min(config.floor, config.ceiling)
    high = max(config.floor, config.ceiling)
    return min(high, max(low, score))


def timeout_hit(duration_seconds: int | None, config: RepConfig = DEFAULT_CONFIG) -> int:
    seconds = max(0, duration_seconds or 0)
    step = max(1, config.timeout_step_seconds)
    raw = (seconds // step) * max(0, config.timeout_per_step)
    low = max(0, config.timeout_min)
    high = max(low, config.timeout_max)
    return min(high, max(low, raw))


def case_hit(kind: str, duration_seconds: int | None, config: RepConfig = DEFAULT_CONFIG) -> int:
    token = kind.strip().lower()
    if token == KIND_WARN:
        return max(0, config.warn_hit)
    if token == KIND_TIMEOUT:
        return timeout_hit(duration_seconds, config)
    if token == KIND_MUTE:
        return max(0, config.mute_hit)
    if token == KIND_KICK:
        return max(0, config.kick_hit)
    return 0


def apply_hit(
    *,
    score: int,
    hit: int,
    hour_used: int,
    config: RepConfig = DEFAULT_CONFIG,
) -> HitPlan:
    want = max(0, hit)
    room = max(0, config.max_hit_per_hour - max(0, hour_used))
    applied = min(want, room)
    return HitPlan(applied=applied, cut=want - applied, score=clamp_score(score - applied, config))


def apply_growth(
    *,
    score: int,
    unused_messages: int,
    unused_voice: int,
    last_growth_at: datetime | None,
    now: datetime,
    config: RepConfig = DEFAULT_CONFIG,
) -> GrowthPlan:
    moment = as_utc(now)
    if score >= config.ceiling or is_frozen(tier_for(score)):
        return GrowthPlan(delta=0, score=score, consume_messages=0, consume_voice=0, grew_at=last_growth_at)
    wait = growth_wait(
        unused_messages=unused_messages,
        unused_voice=unused_voice,
        last_growth_at=last_growth_at,
        now=moment,
        config=config,
        score=score,
    )
    if not wait.ready:
        return GrowthPlan(delta=0, score=score, consume_messages=0, consume_voice=0, grew_at=last_growth_at)
    messages = max(0, unused_messages)
    voice = max(0, unused_voice)
    take_messages = config.messages_per_point if messages >= config.messages_per_point else 0
    take_voice = 0 if take_messages else config.voice_seconds_per_point
    if take_messages <= 0 and voice < config.voice_seconds_per_point:
        return GrowthPlan(delta=0, score=score, consume_messages=0, consume_voice=0, grew_at=last_growth_at)
    return GrowthPlan(
        delta=1,
        score=clamp_score(score + 1, config),
        consume_messages=take_messages,
        consume_voice=take_voice,
        grew_at=moment,
    )


def growth_wait(
    *,
    unused_messages: int,
    unused_voice: int,
    last_growth_at: datetime | None,
    now: datetime,
    config: RepConfig = DEFAULT_CONFIG,
    score: int | None = None,
) -> GrowthWait:
    moment = as_utc(now)
    msg_need = max(1, config.messages_per_point)
    voice_need = max(1, config.voice_seconds_per_point)
    unused_msg = max(0, unused_messages)
    unused_v = max(0, unused_voice)
    messages_left = 0 if unused_msg >= msg_need else msg_need - unused_msg
    voice_left = 0 if unused_v >= voice_need else voice_need - unused_v
    if score is not None and is_frozen(tier_for(score)):
        return GrowthWait(
            ready=False,
            until=None,
            messages_left=messages_left,
            voice_seconds_left=voice_left,
            blocked=True,
        )
    earned = unused_msg >= msg_need or unused_v >= voice_need
    until = None
    if last_growth_at is not None:
        until = as_utc(last_growth_at) + config.growth_interval
        if moment < until:
            return GrowthWait(ready=False, until=until, messages_left=messages_left, voice_seconds_left=voice_left)
    return GrowthWait(ready=earned, until=until, messages_left=messages_left, voice_seconds_left=voice_left)


def month_hits(events: Iterable[tuple[str, int]], *, kinds: Sequence[str] | None = None) -> tuple[MonthHit, ...]:
    order = tuple(kinds) if kinds is not None else (KIND_WARN, KIND_TIMEOUT, KIND_MUTE, KIND_KICK)
    totals: dict[str, int] = {kind: 0 for kind in order}
    for kind, delta in events:
        token = kind.strip().lower()
        if token not in totals:
            continue
        if delta >= 0:
            continue
        totals[token] += -delta
    return tuple(MonthHit(kind=kind, total=totals[kind]) for kind in order if totals[kind])


def hour_used(events: Iterable[tuple[datetime, int]], *, now: datetime) -> int:
    moment = as_utc(now)
    start = moment - timedelta(hours=1)
    total = 0
    for created_at, applied in events:
        if as_utc(created_at) < start:
            continue
        total += max(0, applied)
    return total


def parse_rep_config(
    current: RepConfig,
    *,
    warn_hit: int | None = None,
    timeout_per_step: int | None = None,
    timeout_min: int | None = None,
    timeout_max: int | None = None,
    mute_hit: int | None = None,
    kick_hit: int | None = None,
    max_hit_per_hour: int | None = None,
    messages_per_point: int | None = None,
    voice_minutes: int | None = None,
    sinner_threshold: int | None = None,
    dry_run: bool | None = None,
    start: int | None = None,
) -> RepConfig | None:
    warn = _bound(current.warn_hit if warn_hit is None else warn_hit, 0, 100)
    mute = _bound(current.mute_hit if mute_hit is None else mute_hit, 0, 100)
    kick = _bound(current.kick_hit if kick_hit is None else kick_hit, 0, 100)
    step = _bound(current.timeout_per_step if timeout_per_step is None else timeout_per_step, 0, 40)
    tmin = _bound(current.timeout_min if timeout_min is None else timeout_min, 0, 40)
    tmax = _bound(current.timeout_max if timeout_max is None else timeout_max, 0, 40)
    cap = _bound(current.max_hit_per_hour if max_hit_per_hour is None else max_hit_per_hour, 1, 200)
    messages = _bound(current.messages_per_point if messages_per_point is None else messages_per_point, 1, 10_000)
    voice_now = current.voice_seconds_per_point // 60
    voice = _bound(voice_now if voice_minutes is None else voice_minutes, 1, 24 * 60)
    _ = sinner_threshold
    origin = _bound(current.start if start is None else start, 0, 100)
    if None in {warn, mute, kick, step, tmin, tmax, cap, messages, voice, origin}:
        return None
    if tmin > tmax:
        return None
    return RepConfig(
        start=origin,
        floor=current.floor,
        ceiling=current.ceiling,
        warn_hit=warn,
        timeout_step_seconds=current.timeout_step_seconds,
        timeout_per_step=step,
        timeout_min=tmin,
        timeout_max=tmax,
        mute_hit=mute,
        kick_hit=kick,
        max_hit_per_hour=cap,
        messages_per_point=messages,
        voice_seconds_per_point=voice * 60,
        growth_interval=current.growth_interval,
        sinner_threshold=current.sinner_threshold,
        dry_run=current.dry_run if dry_run is None else dry_run,
    )


def score_bar(score: int, ceiling: int, width: int = 10) -> str:
    high = max(1, ceiling)
    clamped = min(max(score, 0), high)
    filled = round(width * clamped / high) if width > 0 else 0
    filled = min(max(filled, 0), max(width, 0))
    bar = "▰" * filled + "▱" * (max(width, 0) - filled)
    return f"{bar} **{score}**/{high}"


def _bound(value: int, low: int, high: int) -> int | None:
    if value < low or value > high:
        return None
    return value
