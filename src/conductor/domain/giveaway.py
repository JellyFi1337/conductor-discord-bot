"""Розыгрыш: срок, пул, перевыбор, белый список приз-роли. Без Discord и БД."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.activity import as_utc

PRIZE_LIMIT = 200
MIN_WINNERS = 1
MAX_WINNERS = 25
MIN_DURATION = timedelta(minutes=1)
MAX_DURATION = timedelta(days=30)
REROLL_WINDOW = timedelta(hours=24)
COUNTER_INTERVAL = 5.0
_PLAIN = re.compile(r"^\d+$")
_TOKEN = re.compile(r"(\d+)\s*([dhmдчм])", re.IGNORECASE)


class GiveawayState(StrEnum):
    OPEN = "open"
    ENDED = "ended"
    CANCELLED = "cancelled"


class JoinVerdict(StrEnum):
    JOINED = "joined"
    ALREADY = "already"
    CLOSED = "closed"
    BOT = "bot"
    MISSING = "missing"
    UNFIT = "unfit"


class ManageVerdict(StrEnum):
    OK = "ok"
    DENIED = "denied"
    MISSING = "missing"
    ALREADY = "already"
    EXPIRED = "expired"
    EMPTY = "empty"


class ConditionKind(StrEnum):
    ROLE = "role"
    MESSAGES = "messages"
    ACCOUNT = "account"
    TENURE = "tenure"
    MIN_TIER = "min_tier"
    SINNER = "sinner"
    VOICE = "voice"


class WinnerPlace(StrEnum):
    MAIN = "main"
    EXTRA = "extra"


MESSAGES_PER_TICKET = 50
VOICE_MINUTES_PER_TICKET = 30
MAX_TICKETS = 10
MAX_DAYS = 3650
CONSOLATION_LIMIT = 200


@dataclass(frozen=True, slots=True)
class GiveawaySpec:
    prize: str
    winners: int
    duration: timedelta


@dataclass(frozen=True, slots=True)
class GiveawayRules:
    require_role_id: int | None = None
    min_messages: int = 0
    min_account_days: int = 0
    min_tenure_days: int = 0
    min_tier: bool = False
    in_voice: bool = False
    weight: bool = False
    messages_per_ticket: int = MESSAGES_PER_TICKET
    voice_minutes_per_ticket: int = VOICE_MINUTES_PER_TICKET
    max_tickets: int = MAX_TICKETS
    consolation_role_id: int | None = None
    consolation_text: str = ""
    consolation_count: int = 0


@dataclass(frozen=True, slots=True)
class ConditionGap:
    kind: ConditionKind
    have: int = 0
    need: int = 0
    role_id: int | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    user_id: int
    role_ids: frozenset[int] = frozenset()
    messages: int = 0
    voice_seconds: int = 0
    account_created_at: datetime | None = None
    joined_at: datetime | None = None
    sinner: bool = False
    in_voice: bool = False


@dataclass(frozen=True, slots=True)
class JoinOutcome:
    verdict: JoinVerdict
    gaps: tuple[ConditionGap, ...] = ()


@dataclass(frozen=True, slots=True)
class GiveawayCard:
    giveaway_id: int
    prize: str
    host_id: int
    winners: int
    ends_at: datetime
    entries: int
    state: GiveawayState = GiveawayState.OPEN
    winner_ids: tuple[int, ...] = ()
    prize_role_id: int | None = None
    ended_at: datetime | None = None
    channel_id: int = 0
    message_id: int | None = None
    rules: GiveawayRules = GiveawayRules()
    consolation_ids: tuple[int, ...] = ()


def parse_prize(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > PRIZE_LIMIT:
        return None
    return cleaned


def parse_winners(text: str | int) -> int | None:
    try:
        value = int(str(text).strip())
    except ValueError:
        return None
    if value < MIN_WINNERS or value > MAX_WINNERS:
        return None
    return value


def parse_duration(text: str) -> timedelta | None:
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return None
    if _PLAIN.fullmatch(raw):
        minutes = int(raw)
        delta = timedelta(minutes=minutes)
        return delta if MIN_DURATION <= delta <= MAX_DURATION else None
    total = timedelta()
    matched = 0
    for amount, unit in _TOKEN.findall(raw):
        matched += 1
        count = int(amount)
        if unit in {"d", "д"}:
            total += timedelta(days=count)
        elif unit in {"h", "ч"}:
            total += timedelta(hours=count)
        else:
            total += timedelta(minutes=count)
    rest = _TOKEN.sub("", raw)
    if matched == 0 or rest:
        return None
    if total < MIN_DURATION or total > MAX_DURATION:
        return None
    return total


def parse_spec(*, prize: str, winners: str | int, duration: str) -> GiveawaySpec | None:
    title = parse_prize(prize)
    count = parse_winners(winners)
    length = parse_duration(duration)
    if title is None or count is None or length is None:
        return None
    return GiveawaySpec(prize=title, winners=count, duration=length)


def dump_role_ids(role_ids: Iterable[int]) -> str:
    return json.dumps(sorted({int(role_id) for role_id in role_ids if int(role_id) > 0}))


def parse_role_ids(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    found: list[int] = []
    seen: set[int] = set()
    for item in data:
        try:
            role_id = int(item)
        except (TypeError, ValueError):
            continue
        if role_id <= 0 or role_id in seen:
            continue
        seen.add(role_id)
        found.append(role_id)
    return tuple(found)


def prize_role_allowed(role_id: int | None, *, whitelist: Collection[int], old_ids: Collection[int]) -> bool:
    if role_id is None:
        return True
    if role_id in set(old_ids):
        return False
    return role_id in set(whitelist)


def can_manage(*, actor_id: int, host_id: int, has_manage: bool) -> bool:
    return actor_id == host_id or has_manage


def can_reroll(*, ended_at: datetime | None, cancelled: bool, now: datetime) -> bool:
    if cancelled or ended_at is None:
        return False
    return as_utc(now) <= as_utc(ended_at) + REROLL_WINDOW


def pick_winners(
    pool: Sequence[int],
    count: int,
    *,
    exclude: Collection[int] = (),
    rng: random.Random | None = None,
) -> tuple[int, ...]:
    blocked = set(exclude)
    remaining = [int(user_id) for user_id in dict.fromkeys(pool) if int(user_id) not in blocked]
    dice = rng or random.Random()
    dice.shuffle(remaining)
    take = max(0, min(count, len(remaining)))
    return tuple(remaining[:take])


def pick_weighted(
    pool: Sequence[tuple[int, int]],
    count: int,
    *,
    exclude: Collection[int] = (),
    rng: random.Random | None = None,
) -> tuple[int, ...]:
    blocked = set(exclude)
    remaining = [
        (int(user_id), max(1, int(weight)))
        for user_id, weight in pool
        if int(user_id) not in blocked and int(weight) > 0
    ]
    seen: set[int] = set()
    unique: list[tuple[int, int]] = []
    for user_id, weight in remaining:
        if user_id in seen:
            continue
        seen.add(user_id)
        unique.append((user_id, weight))
    dice = rng or random.Random()
    winners: list[int] = []
    while unique and len(winners) < max(0, count):
        total = sum(weight for _user_id, weight in unique)
        ticket = dice.randrange(total)
        acc = 0
        chosen = 0
        for index, (user_id, weight) in enumerate(unique):
            acc += weight
            if ticket < acc:
                chosen = index
                winners.append(user_id)
                break
        unique.pop(chosen)
    return tuple(winners)


def age_days(start: datetime | None, now: datetime) -> int:
    if start is None:
        return 0
    return max(0, (as_utc(now) - as_utc(start)).days)


def uses_ticket_weight(rules: GiveawayRules) -> bool:
    return bool(rules.weight)


def ticket_weight(candidate: Candidate, rules: GiveawayRules) -> int:
    if not rules.weight:
        return 1
    per_messages = max(1, rules.messages_per_ticket)
    per_voice = max(1, rules.voice_minutes_per_ticket)
    cap = max(1, min(MAX_TICKETS, rules.max_tickets))
    extra = candidate.messages // per_messages
    extra += (max(0, candidate.voice_seconds) // 60) // per_voice
    return min(cap, 1 + extra)


def evaluate(candidate: Candidate, rules: GiveawayRules, *, now: datetime) -> tuple[ConditionGap, ...]:
    gaps: list[ConditionGap] = []
    role_id = rules.require_role_id
    if role_id is not None and role_id not in candidate.role_ids:
        gaps.append(ConditionGap(ConditionKind.ROLE, role_id=role_id))
    if rules.min_messages > 0 and candidate.messages < rules.min_messages:
        gaps.append(ConditionGap(ConditionKind.MESSAGES, have=candidate.messages, need=rules.min_messages))
    if rules.min_account_days > 0:
        have = age_days(candidate.account_created_at, now)
        if have < rules.min_account_days:
            gaps.append(ConditionGap(ConditionKind.ACCOUNT, have=have, need=rules.min_account_days))
    if rules.min_tenure_days > 0:
        have = age_days(candidate.joined_at, now)
        if have < rules.min_tenure_days:
            gaps.append(ConditionGap(ConditionKind.TENURE, have=have, need=rules.min_tenure_days))
    if rules.min_tier and candidate.sinner:
        gaps.append(ConditionGap(ConditionKind.MIN_TIER))
    if rules.in_voice and not candidate.in_voice:
        gaps.append(ConditionGap(ConditionKind.VOICE))
    return tuple(gaps)


def dump_rules(rules: GiveawayRules) -> str:
    return json.dumps(
        {
            "require_role_id": rules.require_role_id,
            "min_messages": rules.min_messages,
            "min_account_days": rules.min_account_days,
            "min_tenure_days": rules.min_tenure_days,
            "min_tier": rules.min_tier,
            "in_voice": rules.in_voice,
            "weight": rules.weight,
            "messages_per_ticket": rules.messages_per_ticket,
            "voice_minutes_per_ticket": rules.voice_minutes_per_ticket,
            "max_tickets": rules.max_tickets,
            "consolation_role_id": rules.consolation_role_id,
            "consolation_text": rules.consolation_text,
            "consolation_count": rules.consolation_count,
        },
        ensure_ascii=False,
    )


def _int(value: object, default: int = 0, *, lo: int = 0, hi: int = 10_000_000) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _opt_id(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_rules(raw: str | None) -> GiveawayRules:
    if not raw:
        return GiveawayRules()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return GiveawayRules()
    if not isinstance(data, dict):
        return GiveawayRules()
    text = str(data.get("consolation_text") or "").strip()
    if len(text) > CONSOLATION_LIMIT:
        text = text[:CONSOLATION_LIMIT]
    return GiveawayRules(
        require_role_id=_opt_id(data.get("require_role_id")),
        min_messages=_int(data.get("min_messages"), 0, hi=1_000_000),
        min_account_days=_int(data.get("min_account_days"), 0, hi=MAX_DAYS),
        min_tenure_days=_int(data.get("min_tenure_days"), 0, hi=MAX_DAYS),
        min_tier=bool(data.get("min_tier") or data.get("not_sinner")),
        in_voice=bool(data.get("in_voice")),
        weight=bool(data.get("weight")),
        messages_per_ticket=_int(data.get("messages_per_ticket"), MESSAGES_PER_TICKET, lo=1, hi=10_000),
        voice_minutes_per_ticket=_int(data.get("voice_minutes_per_ticket"), VOICE_MINUTES_PER_TICKET, lo=1, hi=10_000),
        max_tickets=_int(data.get("max_tickets"), MAX_TICKETS, lo=1, hi=MAX_TICKETS),
        consolation_role_id=_opt_id(data.get("consolation_role_id")),
        consolation_text=text,
        consolation_count=_int(data.get("consolation_count"), 0, hi=MAX_WINNERS),
    )


def giveaway_state(*, ended_at: datetime | None, cancelled_at: datetime | None) -> GiveawayState:
    if cancelled_at is not None:
        return GiveawayState.CANCELLED
    if ended_at is not None:
        return GiveawayState.ENDED
    return GiveawayState.OPEN
