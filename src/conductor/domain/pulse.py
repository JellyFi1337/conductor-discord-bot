"""Снимок пульса, оценка здоровья и пороги сигналов. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from conductor.domain.activity import as_utc
from conductor.domain.observe import AlertKind

WEIGHT_AUTHORS = 0.35
WEIGHT_VOICE = 0.25
WEIGHT_RETENTION = 0.20
WEIGHT_TREND = 0.20

# 15% людей, писавших за сутки, — потолок «авторы», не «один из 600 = 100».
AUTHOR_SATURATION = 0.15
# 20 минут голоса на человека за сутки — потолок «голос».
VOICE_MINUTES_SATURATION = 20.0
RETENTION_CAP = 2.0
TREND_CAP = 2.0
STALE_CENSUS_HOURS = 7 * 24

BAND_QUIET = 25
BAND_STEADY = 50
BAND_ALIVE = 75

WEIGHT_LINE = "авторы 35 · голос 25 · удержание 20 · тренд 20"

VOICE_MEDIAN_MIN = 5
LEAVE_INVERT_MIN = 5
AUTHORS_DROP_RATIO = 0.5
AUTHORS_PCT_DEFAULT = 50
JOIN_SPIKE_FLOOR = 15
FLOOR_MIN = 1
FLOOR_MAX = 1000
AUTHORS_PCT_MIN = 1
AUTHORS_PCT_MAX = 100
BOOST_LOOKBACK_HOURS = 24
BOOST_SAMPLE_MIN_HOURS = 20


class HealthBand(StrEnum):
    UNKNOWN = "unknown"
    QUIET = "quiet"
    STEADY = "steady"
    ALIVE = "alive"
    PEAK = "peak"


class QualityFlag(StrEnum):
    NO_CENSUS = "no_census"
    STALE_CENSUS = "stale_census"
    NO_PRESENCE = "no_presence"
    NO_HISTORY = "no_history"
    NO_VOICE_SESSIONS = "no_voice_sessions"


class CountStyle(StrEnum):
    PLAIN = "plain"
    COMPACT = "k"


@dataclass(frozen=True, slots=True)
class PulseInputs:
    humans: int
    bots: int
    boosters: int
    premium_tier: int
    voice_now: int
    online: int | None
    unique_authors_24h: int
    messages_24h: int
    messages_7d_avg: float
    voice_minutes_24h: int
    joins_7d: int
    leaves_7d: int
    tickets_open: int
    duty_watchers: int
    next_gathering_at: datetime | None
    next_gathering_title: str | None
    next_gathering_id: int | None
    has_presence_intent: bool
    census_age_hours: float | None
    has_message_history: bool
    has_voice_sessions: bool
    joins_7d_coded: int = 0
    joins_7d_vanity: int = 0
    joins_7d_unknown: int = 0
    joins_24h: int = 0
    leaves_24h: int = 0
    authors_7d: tuple[int, ...] = ()
    peak_hour: int | None = None
    peak_voice_7d: tuple[int, ...] = ()
    boosters_24h_ago: int | None = None


@dataclass(frozen=True, slots=True)
class HealthParts:
    authors: int
    voice: int
    retention: int
    trend: int


@dataclass(frozen=True, slots=True)
class HealthReport:
    score: int
    band: HealthBand
    parts: HealthParts
    flags: tuple[QualityFlag, ...]


@dataclass(frozen=True, slots=True)
class PulseSnapshot:
    inputs: PulseInputs
    report: HealthReport
    assembled_at: datetime
    from_cache: bool = False


def format_count(n: int, style: str | CountStyle = CountStyle.PLAIN) -> str:
    value = max(0, int(n))
    kind = style if isinstance(style, CountStyle) else CountStyle(str(style))
    if kind is CountStyle.PLAIN or value < 1000:
        return str(value)
    compact = value / 1000
    return f"{compact:.1f}K"


def public_online(inputs: PulseInputs) -> int | None:
    if not inputs.has_presence_intent:
        return None
    return inputs.online


def quality_flags(inputs: PulseInputs) -> tuple[QualityFlag, ...]:
    flags: list[QualityFlag] = []
    if inputs.census_age_hours is None:
        flags.append(QualityFlag.NO_CENSUS)
    elif inputs.census_age_hours > STALE_CENSUS_HOURS:
        flags.append(QualityFlag.STALE_CENSUS)
    if not inputs.has_presence_intent:
        flags.append(QualityFlag.NO_PRESENCE)
    if not inputs.has_message_history:
        flags.append(QualityFlag.NO_HISTORY)
    if not inputs.has_voice_sessions:
        flags.append(QualityFlag.NO_VOICE_SESSIONS)
    return tuple(flags)


def health_score(inputs: PulseInputs, now: datetime | None = None) -> HealthReport:
    _ = now
    people = max(int(inputs.humans), 1)
    authors = _part(int(inputs.unique_authors_24h) / people, AUTHOR_SATURATION)
    voice = _part(int(inputs.voice_minutes_24h) / people, VOICE_MINUTES_SATURATION)
    retention = _part(int(inputs.joins_7d) / max(int(inputs.leaves_7d), 1), RETENTION_CAP)
    trend = _part(float(inputs.messages_24h) / max(float(inputs.messages_7d_avg), 1.0), TREND_CAP)
    parts = HealthParts(authors=authors, voice=voice, retention=retention, trend=trend)
    flags = quality_flags(inputs)
    if QualityFlag.NO_HISTORY in flags and QualityFlag.NO_VOICE_SESSIONS in flags:
        return HealthReport(score=0, band=HealthBand.UNKNOWN, parts=parts, flags=flags)
    raw = WEIGHT_AUTHORS * authors + WEIGHT_VOICE * voice + WEIGHT_RETENTION * retention + WEIGHT_TREND * trend
    score = min(100, max(0, round(raw)))
    return HealthReport(score=score, band=_band(score), parts=parts, flags=flags)


def census_age_hours(census_at: datetime | None, now: datetime) -> float | None:
    if census_at is None:
        return None
    delta = as_utc(now) - as_utc(census_at)
    return max(0.0, delta.total_seconds() / 3600.0)


def _part(ratio: float, saturation: float) -> int:
    if saturation <= 0:
        return 0
    return min(100, max(0, round(100.0 * max(ratio, 0.0) / saturation)))


def _band(score: int) -> HealthBand:
    if score < BAND_QUIET:
        return HealthBand.QUIET
    if score < BAND_STEADY:
        return HealthBand.STEADY
    if score < BAND_ALIVE:
        return HealthBand.ALIVE
    return HealthBand.PEAK


class AlertConfigVerdict(StrEnum):
    OK = "ok"
    BAD_VOICE = "bad_voice"
    BAD_LEAVES = "bad_leaves"
    BAD_AUTHORS = "bad_authors"
    BAD_JOINS = "bad_joins"


@dataclass(frozen=True, slots=True)
class PulseAlertRules:
    enabled: bool = True
    voice_floor: int = VOICE_MEDIAN_MIN
    leave_floor: int = LEAVE_INVERT_MIN
    authors_pct: int = AUTHORS_PCT_DEFAULT
    join_spike_floor: int = JOIN_SPIKE_FLOOR

    @property
    def authors_ratio(self) -> float:
        return int(self.authors_pct) / 100.0


@dataclass(frozen=True, slots=True)
class PulseSignal:
    kind: AlertKind
    value: float
    median: float | None
    threshold: float


def parse_floor(value: int | None, default: int) -> tuple[int | None, bool]:
    if value is None:
        return default, True
    parsed = int(value)
    if parsed < FLOOR_MIN or parsed > FLOOR_MAX:
        return None, False
    return parsed, True


def parse_authors_pct(value: int | None) -> tuple[int | None, bool]:
    if value is None:
        return AUTHORS_PCT_DEFAULT, True
    parsed = int(value)
    if parsed < AUTHORS_PCT_MIN or parsed > AUTHORS_PCT_MAX:
        return None, False
    return parsed, True


def series_median(values: Sequence[int | float]) -> float | None:
    numbers = [float(item) for item in values]
    if not numbers:
        return None
    ordered = sorted(numbers)
    size = len(ordered)
    mid = size // 2
    if size % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def below_median_ratio(value: int | float, series: Sequence[int | float], *, ratio: float) -> bool:
    median = series_median(series)
    if median is None or median <= 0:
        return False
    return float(value) < median * ratio


def peak_hour_of(hours: Sequence[tuple[int, int]] | dict[int, int]) -> int | None:
    items = tuple(hours.items()) if isinstance(hours, dict) else tuple(hours)
    if not items:
        return None
    hour, count = max(items, key=lambda item: (item[1], -item[0]))
    if count <= 0:
        return None
    return int(hour)


def evaluate_pulse_alerts(
    inputs: PulseInputs,
    rules: PulseAlertRules,
    *,
    now: datetime,
) -> tuple[PulseSignal, ...]:
    if not rules.enabled:
        return ()
    found: list[PulseSignal] = []
    hour = as_utc(now).hour
    voice_median = series_median(inputs.peak_voice_7d)
    if (
        inputs.peak_hour is not None
        and hour == int(inputs.peak_hour)
        and int(inputs.voice_now) == 0
        and voice_median is not None
        and voice_median >= rules.voice_floor
    ):
        found.append(
            PulseSignal(
                kind=AlertKind.VOICE_EMPTY_AT_PEAK,
                value=0.0,
                median=voice_median,
                threshold=float(rules.voice_floor),
            )
        )
    leaves = int(inputs.leaves_24h)
    joins = int(inputs.joins_24h)
    if leaves > joins and leaves >= rules.leave_floor:
        found.append(
            PulseSignal(
                kind=AlertKind.JOIN_LEAVE_INVERT,
                value=float(leaves),
                median=None,
                threshold=float(rules.leave_floor),
            )
        )
    authors_series = inputs.authors_7d
    if sum(authors_series) > 0 and below_median_ratio(
        inputs.unique_authors_24h, authors_series, ratio=rules.authors_ratio
    ):
        found.append(
            PulseSignal(
                kind=AlertKind.AUTHORS_DROP,
                value=float(inputs.unique_authors_24h),
                median=series_median(authors_series),
                threshold=rules.authors_ratio,
            )
        )
    was = inputs.boosters_24h_ago
    if was is not None and int(inputs.boosters) < int(was):
        found.append(
            PulseSignal(
                kind=AlertKind.BOOSTS_DROP,
                value=float(inputs.boosters),
                median=float(was),
                threshold=1.0,
            )
        )
    return tuple(found)


CATALOG_BOT_NAMES = frozenset({"dsmonitoring", "server monitoring"})


def normalize_bot_name(name: str) -> str:
    text = str(name).strip()
    if "#" in text:
        head, tail = text.rsplit("#", 1)
        if tail.isdigit():
            text = head
    return " ".join(text.casefold().split())


def is_catalog_bot_name(name: str) -> bool:
    return normalize_bot_name(name) in CATALOG_BOT_NAMES


def catalog_bot_names(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(name for name in names if is_catalog_bot_name(name))
