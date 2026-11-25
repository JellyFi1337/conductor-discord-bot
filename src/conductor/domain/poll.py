"""Опрос: типы, пул вариантов, смена голоса. Без Discord и БД."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.giveaway import parse_duration

QUESTION_LIMIT = 200
OPTION_LIMIT = 80
MIN_OPTIONS = 2
MAX_OPTIONS = 10
FLUSH_INTERVAL = 5.0


class PollKind(StrEnum):
    PUBLIC = "public"
    ANONYMOUS = "anonymous"
    HIDDEN = "hidden"


class PollState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    DELETED = "deleted"


class VoteVerdict(StrEnum):
    CAST = "cast"
    CHANGED = "changed"
    ALREADY = "already"
    LOCKED = "locked"
    CLOSED = "closed"
    BOT = "bot"
    MISSING = "missing"
    BAD_OPTION = "bad_option"


class ManageVerdict(StrEnum):
    OK = "ok"
    DENIED = "denied"
    MISSING = "missing"
    ALREADY = "already"
    OPEN = "open"
    NOT_ANON = "not_anon"


@dataclass(frozen=True, slots=True)
class PollVote:
    user_id: int
    option_index: int


@dataclass(frozen=True, slots=True)
class PollSpec:
    question: str
    options: tuple[str, ...]
    duration: timedelta
    kind: PollKind = PollKind.PUBLIC
    allow_change: bool = True


@dataclass(frozen=True, slots=True)
class PollCard:
    poll_id: int
    question: str
    host_id: int
    kind: PollKind
    allow_change: bool
    options: tuple[str, ...]
    ends_at: datetime
    votes: tuple[PollVote, ...] = ()
    state: PollState = PollState.OPEN
    channel_id: int = 0
    message_id: int | None = None
    closed_at: datetime | None = None
    deanonymized_at: datetime | None = None


def parse_question(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > QUESTION_LIMIT:
        return None
    return cleaned


def parse_options(text: str) -> tuple[str, ...] | None:
    found: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        label = " ".join(raw.split())
        if not label:
            continue
        if len(label) > OPTION_LIMIT:
            return None
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(label)
    if len(found) < MIN_OPTIONS or len(found) > MAX_OPTIONS:
        return None
    return tuple(found)


def parse_kind(value: str | PollKind | None) -> PollKind | None:
    if value is None:
        return PollKind.PUBLIC
    try:
        return PollKind(str(value).strip().lower())
    except ValueError:
        return None


def parse_spec(
    *,
    question: str,
    options: str,
    duration: str,
    kind: str | PollKind = PollKind.PUBLIC,
    allow_change: bool = True,
) -> PollSpec | None:
    title = parse_question(question)
    choices = parse_options(options)
    length = parse_duration(duration)
    flavour = parse_kind(kind)
    if title is None or choices is None or length is None or flavour is None:
        return None
    return PollSpec(
        question=title,
        options=choices,
        duration=length,
        kind=flavour,
        allow_change=bool(allow_change),
    )


def dump_options(options: Sequence[str]) -> str:
    return json.dumps(list(options), ensure_ascii=False)


def load_options(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    labels: list[str] = []
    for item in data:
        label = " ".join(str(item).split())
        if label and len(label) <= OPTION_LIMIT:
            labels.append(label)
    return tuple(labels[:MAX_OPTIONS])


def can_manage(*, actor_id: int, host_id: int, has_manage: bool) -> bool:
    return actor_id == host_id or has_manage


def poll_state(*, closed_at: datetime | None, deleted_at: datetime | None) -> PollState:
    if deleted_at is not None:
        return PollState.DELETED
    if closed_at is not None:
        return PollState.CLOSED
    return PollState.OPEN


def apply_vote(
    *,
    previous: int | None,
    choice: int,
    option_count: int,
    allow_change: bool,
    open: bool,
) -> VoteVerdict:
    if not open:
        return VoteVerdict.CLOSED
    if choice < 0 or choice >= option_count:
        return VoteVerdict.BAD_OPTION
    if previous is None:
        return VoteVerdict.CAST
    if previous == choice:
        return VoteVerdict.ALREADY
    if not allow_change:
        return VoteVerdict.LOCKED
    return VoteVerdict.CHANGED


def counts_for(option_count: int, votes: Sequence[PollVote]) -> tuple[int, ...]:
    tallies = [0] * max(0, option_count)
    for vote in votes:
        if 0 <= vote.option_index < len(tallies):
            tallies[vote.option_index] += 1
    return tuple(tallies)


def voters_for(votes: Sequence[PollVote], option_index: int) -> tuple[int, ...]:
    return tuple(vote.user_id for vote in votes if vote.option_index == option_index)


def shown_count(kind: PollKind, state: PollState, real: int) -> int:
    if kind is PollKind.HIDDEN and state is PollState.OPEN:
        return 0
    return real


def shown_voters(kind: PollKind, state: PollState, voter_ids: Sequence[int]) -> tuple[int, ...]:
    if kind is PollKind.ANONYMOUS:
        return ()
    if kind is PollKind.HIDDEN and state is PollState.OPEN:
        return ()
    return tuple(int(user_id) for user_id in voter_ids)
