"""Приватная группа: состав, архивация, срок молчания. Без Discord и БД."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from conductor.domain.access import Rank
from conductor.domain.activity import as_utc

TITLE_LIMIT = 80
CHANNEL_NAME_LIMIT = 100
MAX_MEMBERS = 20
HUB_NAME = "группы"
DEFAULT_SILENCE = timedelta(days=7)
DEFAULT_ARCHIVE_DAYS = 30
DEFAULT_MIN_REPUTATION = 50
DEFAULT_DAILY_LIMIT = 1
DEFAULT_OPEN_LIMIT = 3
ACTIVITY_WINDOW = timedelta(minutes=5)
SILENCE_WARN_BEFORE = timedelta(days=1)
SYSTEM_ACTOR = 0
DAILY_WINDOW = timedelta(hours=24)
INVITE_TTL = timedelta(days=7)
INVITE_COOLDOWN = timedelta(hours=24)
STAFF_RANKS: tuple[Rank, ...] = (Rank.CHIEF, Rank.SPECIAL)

_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ID_RE = re.compile(r"\d{15,20}")

__all__ = [
    "ACTIVITY_WINDOW",
    "CHANNEL_NAME_LIMIT",
    "DAILY_WINDOW",
    "DEFAULT_ARCHIVE_DAYS",
    "DEFAULT_DAILY_LIMIT",
    "DEFAULT_MIN_REPUTATION",
    "DEFAULT_OPEN_LIMIT",
    "DEFAULT_SILENCE",
    "HUB_NAME",
    "INVITE_COOLDOWN",
    "INVITE_TTL",
    "MAX_MEMBERS",
    "SILENCE_WARN_BEFORE",
    "STAFF_RANKS",
    "SYSTEM_ACTOR",
    "TITLE_LIMIT",
    "ArchiveReason",
    "CreateGate",
    "CreateReport",
    "CreateVerdict",
    "GroupCard",
    "GroupHubSettings",
    "GroupInviteCard",
    "GroupListing",
    "GroupSpec",
    "GroupState",
    "InviteSendResult",
    "InviteStatus",
    "ManageVerdict",
    "QuotaVerdict",
    "activity_write_due",
    "can_archive",
    "can_invite",
    "can_leave",
    "can_purge",
    "can_restore",
    "can_send_invite",
    "group_channel_name",
    "group_quota",
    "group_state",
    "invite_verdict",
    "may_create_group",
    "parse_invitee",
    "parse_member_ids",
    "parse_spec",
    "parse_title",
    "purge_at",
    "seconds_until_silence",
    "seconds_until_silence_warn",
    "silence_deadline",
    "silence_due",
    "silence_warn_due",
    "with_member",
    "without_member",
]


class GroupState(StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"


class ArchiveReason(StrEnum):
    OWNER = "owner"
    SILENCE = "silence"
    STAFF = "staff"
    OWNER_LEFT = "owner_left"


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class CreateVerdict(StrEnum):
    OK = "ok"
    BAD_SPEC = "bad_spec"
    LOCKED = "locked"
    FAILED = "failed"
    REPUTATION = "reputation"
    REPUTATION_OFF = "reputation_off"
    QUOTA_DAILY = "quota_daily"
    QUOTA_OPEN = "quota_open"


class CreateGate(StrEnum):
    OK = "ok"
    REPUTATION = "reputation"
    REPUTATION_OFF = "reputation_off"


class ManageVerdict(StrEnum):
    OK = "ok"
    DENIED = "denied"
    MISSING = "missing"
    ALREADY = "already"
    BOT = "bot"
    FULL = "full"
    OWNER = "owner"
    STRANGER = "stranger"
    FAILED = "failed"
    COOLDOWN = "cooldown"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class GroupSpec:
    title: str
    member_ids: tuple[int, ...]
    with_voice: bool = False


@dataclass(frozen=True, slots=True)
class GroupHubSettings:
    category_id: int
    archive_category_id: int | None = None
    silence_after: timedelta = DEFAULT_SILENCE
    archive_days: int = DEFAULT_ARCHIVE_DAYS
    min_reputation: int = DEFAULT_MIN_REPUTATION
    daily_limit: int = DEFAULT_DAILY_LIMIT
    open_limit: int = DEFAULT_OPEN_LIMIT


@dataclass(frozen=True, slots=True)
class GroupCard:
    group_id: int
    title: str
    owner_id: int
    member_ids: tuple[int, ...]
    channel_id: int
    voice_channel_id: int | None = None
    message_id: int | None = None
    state: GroupState = GroupState.OPEN
    created_at: datetime | None = None
    last_activity_at: datetime | None = None
    archived_at: datetime | None = None
    archive_reason: ArchiveReason | None = None
    purge_after: datetime | None = None
    silence_after: timedelta = DEFAULT_SILENCE


@dataclass(frozen=True, slots=True)
class QuotaVerdict:
    ok: bool
    reason: str | None = None
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CreateReport:
    verdict: CreateVerdict
    channel_id: int | None = None
    title: str = ""
    invites_sent: int = 0
    invites_closed: int = 0
    retry_at: datetime | None = None
    score: int | None = None
    min_reputation: int = DEFAULT_MIN_REPUTATION
    open_count: int = 0
    open_limit: int = DEFAULT_OPEN_LIMIT

    def __iter__(self):
        yield self.verdict
        yield self.channel_id


@dataclass(frozen=True, slots=True)
class InviteSendResult:
    verdict: ManageVerdict
    dm_sent: bool = False
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GroupInviteCard:
    group_id: int
    title: str
    owner_id: int
    guild_id: int
    channel_id: int
    dm_sent: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GroupListing:
    groups: tuple[GroupCard, ...]
    invites: tuple[GroupInviteCard, ...]
    open_count: int
    open_limit: int
    retry_at: datetime | None
    privileged: bool
    viewer_id: int = 0


def parse_title(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > TITLE_LIMIT:
        return None
    return cleaned


def parse_member_ids(text: str | None, *, owner_id: int) -> tuple[int, ...]:
    found: list[int] = [int(owner_id)]
    seen = {int(owner_id)}
    raw = "" if text is None else text
    tokens = [int(match.group(1)) for match in _MENTION_RE.finditer(raw)]
    if not tokens:
        tokens = [int(match.group(0)) for match in _ID_RE.finditer(raw)]
    for user_id in tokens:
        if user_id <= 0 or user_id in seen:
            continue
        if len(found) >= MAX_MEMBERS:
            break
        seen.add(user_id)
        found.append(user_id)
    return tuple(found)


def parse_invitee(text: str) -> int | None:
    raw = text.strip()
    if not raw:
        return None
    mention = _MENTION_RE.fullmatch(raw)
    if mention is not None:
        return int(mention.group(1))
    try:
        user_id = int(raw)
    except ValueError:
        hits = _MENTION_RE.findall(raw) or _ID_RE.findall(raw)
        if not hits:
            return None
        user_id = int(hits[0])
    if user_id <= 0:
        return None
    return user_id


def parse_spec(*, title: str, members: str | None, owner_id: int, with_voice: bool = False) -> GroupSpec | None:
    heading = parse_title(title)
    if heading is None:
        return None
    return GroupSpec(title=heading, member_ids=parse_member_ids(members, owner_id=owner_id), with_voice=with_voice)


def group_channel_name(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.strip()).strip("-").lower()
    text = slug or "group"
    return text[:CHANNEL_NAME_LIMIT]


def group_state(*, archived_at: datetime | None) -> GroupState:
    if archived_at is not None:
        return GroupState.ARCHIVED
    return GroupState.OPEN


def can_invite(*, actor_id: int, owner_id: int) -> bool:
    return actor_id == owner_id


def can_archive(*, actor_id: int, owner_id: int, is_staff: bool) -> bool:
    return actor_id == owner_id or is_staff


def can_purge(*, is_staff: bool) -> bool:
    return bool(is_staff)


def can_restore(*, is_staff: bool) -> bool:
    return bool(is_staff)


def can_leave(*, actor_id: int, owner_id: int, member_ids: Sequence[int]) -> ManageVerdict:
    if actor_id == owner_id:
        return ManageVerdict.OWNER
    if actor_id not in set(member_ids):
        return ManageVerdict.STRANGER
    return ManageVerdict.OK


def invite_verdict(*, user_id: int, member_ids: Sequence[int], is_bot: bool) -> ManageVerdict:
    if is_bot:
        return ManageVerdict.BOT
    if user_id in set(member_ids):
        return ManageVerdict.ALREADY
    if len(member_ids) >= MAX_MEMBERS:
        return ManageVerdict.FULL
    return ManageVerdict.OK


def can_send_invite(
    *,
    user_id: int,
    member_ids: Sequence[int],
    is_bot: bool,
    existing: InviteStatus | None,
    decided_at: datetime | None,
    now: datetime,
    pending_count: int,
) -> ManageVerdict:
    verdict = invite_verdict(user_id=int(user_id), member_ids=member_ids, is_bot=is_bot)
    if verdict is not ManageVerdict.OK:
        return verdict
    occupied = len(member_ids) + max(0, int(pending_count))
    if existing is InviteStatus.PENDING:
        return ManageVerdict.ALREADY
    if occupied >= MAX_MEMBERS:
        return ManageVerdict.FULL
    if existing is InviteStatus.DECLINED and decided_at is not None:
        ready = as_utc(decided_at) + INVITE_COOLDOWN
        if as_utc(now) < ready:
            return ManageVerdict.COOLDOWN
    return ManageVerdict.OK


def may_create_group(
    *,
    has_key: bool,
    privileged: bool,
    score: int | None,
    min_reputation: int,
    reputation_enabled: bool,
) -> CreateGate:
    if has_key or privileged:
        return CreateGate.OK
    if not reputation_enabled:
        return CreateGate.REPUTATION_OFF
    if score is not None and int(score) > int(min_reputation):
        return CreateGate.OK
    return CreateGate.REPUTATION


def group_quota(
    *,
    created_at: Sequence[datetime],
    open_count: int,
    now: datetime,
    daily_limit: int,
    open_limit: int,
    privileged: bool,
) -> QuotaVerdict:
    if privileged:
        return QuotaVerdict(ok=True)
    moment = as_utc(now)
    window_start = moment - DAILY_WINDOW
    recent = sorted(as_utc(stamp) for stamp in created_at if as_utc(stamp) >= window_start)
    if int(daily_limit) > 0 and len(recent) >= int(daily_limit):
        return QuotaVerdict(ok=False, reason="daily", retry_at=recent[0] + DAILY_WINDOW)
    if int(open_limit) > 0 and int(open_count) >= int(open_limit):
        return QuotaVerdict(ok=False, reason="open")
    return QuotaVerdict(ok=True)


def with_member(member_ids: Sequence[int], user_id: int) -> tuple[int, ...]:
    if user_id in set(member_ids):
        return tuple(member_ids)
    if len(member_ids) >= MAX_MEMBERS:
        return tuple(member_ids)
    return tuple(member_ids) + (int(user_id),)


def without_member(member_ids: Sequence[int], user_id: int) -> tuple[int, ...]:
    return tuple(item for item in member_ids if item != user_id)


def silence_deadline(last_activity: datetime, *, silence_after: timedelta = DEFAULT_SILENCE) -> datetime:
    return as_utc(last_activity) + silence_after


def silence_due(
    last_activity: datetime,
    now: datetime,
    *,
    silence_after: timedelta = DEFAULT_SILENCE,
) -> bool:
    return as_utc(now) >= silence_deadline(last_activity, silence_after=silence_after)


def silence_warn_due(
    last_activity: datetime,
    now: datetime,
    *,
    silence_after: timedelta = DEFAULT_SILENCE,
    warn_before: timedelta = SILENCE_WARN_BEFORE,
) -> bool:
    deadline = silence_deadline(last_activity, silence_after=silence_after)
    return as_utc(now) >= deadline - warn_before


def seconds_until_silence(
    last_activity: datetime,
    now: datetime,
    *,
    silence_after: timedelta = DEFAULT_SILENCE,
) -> float:
    remain = silence_deadline(last_activity, silence_after=silence_after) - as_utc(now)
    return max(0.0, remain.total_seconds())


def seconds_until_silence_warn(
    last_activity: datetime,
    now: datetime,
    *,
    silence_after: timedelta = DEFAULT_SILENCE,
    warn_before: timedelta = SILENCE_WARN_BEFORE,
) -> float:
    deadline = silence_deadline(last_activity, silence_after=silence_after) - warn_before
    remain = deadline - as_utc(now)
    return max(0.0, remain.total_seconds())


def purge_at(now: datetime, *, archive_days: int) -> datetime | None:
    days = int(archive_days)
    if days <= 0:
        return None
    return as_utc(now) + timedelta(days=days)


def activity_write_due(
    last_write: datetime | None,
    now: datetime,
    *,
    window: timedelta = ACTIVITY_WINDOW,
) -> bool:
    if last_write is None:
        return True
    return as_utc(now) >= as_utc(last_write) + window
