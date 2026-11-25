"""Правила модерации: дело, причина, срок, иерархия. Без Discord и без БД."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.access import (
    MOD_BAN,
    MOD_KICK,
    MOD_UNBAN,
    Rank,
    StaffTargetBlock,
    staff_target_block,
)

REASON_MIN = 8
REASON_MAX = 400
DETAIL_REASON_MIN = 20
DETAIL_REASON_MAX = 1000
EVIDENCE_REMIND_HOURS_DEFAULT = 24
EVIDENCE_REMIND_HOURS_MIN = 1
EVIDENCE_REMIND_HOURS_MAX = 168
EVIDENCE_REPAIR_INTERVAL = timedelta(hours=1)
TIMEOUT_MAX = timedelta(days=28)
HISTORY_PAGE = 10
CONFIRM_TTL = timedelta(seconds=60)
PURGE_BATCH = 100
PURGE_MAX_AGE = timedelta(days=14)
SOFTBAN_DELETE_SECONDS = 7 * 24 * 3600
SLOWMODE_MAX = 21600

_DURATION_TOKEN = re.compile(r"(\d+)([dhmдчм])", re.IGNORECASE)
_DIGITS = re.compile(r"^\d+$")
_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


class CaseKind(StrEnum):
    WARN = "warn"
    TIMEOUT = "timeout"
    MUTE = "mute"
    UNMUTE = "unmute"
    NOTE = "note"
    KICK = "kick"
    BAN = "ban"
    UNBAN = "unban"
    SOFTBAN = "softban"
    PURGE = "purge"
    LOCK = "lock"


HEAVY_KINDS = frozenset({CaseKind.KICK, CaseKind.BAN, CaseKind.UNBAN})
EVIDENCE_KINDS = frozenset(
    {CaseKind.WARN, CaseKind.TIMEOUT, CaseKind.MUTE, CaseKind.KICK, CaseKind.BAN, CaseKind.SOFTBAN}
)
DM_DELIVERABLE_KINDS = frozenset({CaseKind.WARN, CaseKind.TIMEOUT, CaseKind.MUTE})
HEAVY_ACTION_KEYS: dict[CaseKind, str] = {
    CaseKind.KICK: MOD_KICK,
    CaseKind.BAN: MOD_BAN,
    CaseKind.UNBAN: MOD_UNBAN,
}
UNLOCK_TOKENS = frozenset({"0", "снять"})


class EvidenceDenial(StrEnum):
    NO_CATEGORY = "no_category"
    CATEGORY_GONE = "category_gone"
    NOT_A_CATEGORY = "not_a_category"
    BOT_PERMS = "bot_perms"
    HELD = "held"
    FAILED = "failed"


APPEAL_WINDOW = timedelta(days=7)
APPEALABLE_KINDS = frozenset({CaseKind.WARN, CaseKind.TIMEOUT, CaseKind.MUTE})
_NO_HIERARCHY = frozenset({CaseKind.NOTE, CaseKind.UNBAN, CaseKind.PURGE, CaseKind.LOCK})
_BOT_OK = frozenset({CaseKind.NOTE, CaseKind.UNBAN, CaseKind.PURGE, CaseKind.LOCK})


def action_key(kind: CaseKind | str) -> str | None:
    try:
        return HEAVY_ACTION_KEYS.get(CaseKind(kind))
    except ValueError:
        return None


class DurationVerdict(StrEnum):
    OK = "ok"
    BAD = "bad"
    TOO_LONG = "too_long"


class ConfirmVerdict(StrEnum):
    OK = "ok"
    SPENT = "spent"
    EXPIRED = "expired"
    MISSING = "missing"


class AppealVerdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    KIND = "kind"
    TOO_LATE = "too_late"
    ALREADY = "already"
    REVOKED = "revoked"
    FAILED = "failed"


class ExplainVerdict(StrEnum):
    OK = "ok"
    MISSING = "missing"
    ALREADY = "already"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AppealDecision:
    verdict: AppealVerdict
    ticket_id: int | None = None
    channel_id: int | None = None


class ModVerdict(StrEnum):
    OK = "ok"
    REASON = "reason"
    DURATION = "duration"
    TOO_LONG = "too_long"
    BOT = "bot"
    HIERARCHY = "hierarchy"
    POWER = "power"
    MUTE_ROLE = "mute_role"
    MUTATIONS = "mutations"
    FAILED = "failed"
    SPENT = "spent"
    EXPIRED = "expired"
    LOG_CHANNEL = "log_channel"
    SLOWMODE = "slowmode"
    MISSING = "missing"
    DENIED = "denied"
    QUOTA = "quota"
    REVOKED = "revoked"
    APPEALED = "appealed"


@dataclass(frozen=True, slots=True)
class CaseCreated:
    guild_id: int
    case_id: int
    number: int
    target_id: int
    actor_id: int
    actor_rank: int
    kind: str
    reason: str
    duration_seconds: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredCase:
    id: int
    guild_id: int
    number: int
    owner_id: int
    created_by: int
    actor_rank: int
    kind: str
    reason: str
    duration_seconds: int | None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: int | None = None
    revoke_reason: str | None = None
    appeal_ticket_id: int | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True, slots=True)
class ModOutcome:
    verdict: ModVerdict
    case: StoredCase | None = None
    until: datetime | None = None
    mutation_scope: str | None = None
    pending_id: int | None = None
    deleted: int = 0
    skipped: int = 0
    quota_limit: int | None = None
    quota_used: int | None = None
    quota_resets_at: datetime | None = None
    quota_group: str | None = None
    refund: int = 0
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PurgePick:
    delete_ids: tuple[int, ...]
    skipped_old: int
    stop: bool


@dataclass(frozen=True, slots=True)
class StoredIntent:
    id: int
    guild_id: int
    actor_id: int
    target_id: int
    kind: str
    reason: str
    created_at: datetime
    consumed_at: datetime | None = None


def sanitize_reason(text: str) -> str | None:
    cleaned = text.strip()
    if len(cleaned) < REASON_MIN:
        return None
    return cleaned[:REASON_MAX]


def sanitize_detail_reason(text: str) -> str | None:
    cleaned = text.strip()
    if len(cleaned) < DETAIL_REASON_MIN:
        return None
    return cleaned[:DETAIL_REASON_MAX]


def needs_evidence(kind: CaseKind | str, actor_is_bot: bool) -> bool:
    if actor_is_bot:
        return False
    try:
        return CaseKind(kind) in EVIDENCE_KINDS
    except ValueError:
        return False


def dm_deliverable(kind: CaseKind | str) -> bool:
    try:
        return CaseKind(kind) in DM_DELIVERABLE_KINDS
    except ValueError:
        return False


def may_evidence_reason(*, actor_id: int, created_by: int, has_close_key: bool) -> bool:
    return int(actor_id) == int(created_by) or has_close_key


def may_evidence_close(*, has_close_key: bool) -> bool:
    return has_close_key


def evidence_channel_name(number: int, username: str) -> str:
    slug = _SLUG_RE.sub("-", str(username)).strip("-").lower() or "user"
    return f"дело-{int(number)}-{slug}"[:100]


def evidence_remind_at(created_at: datetime, hours: int) -> datetime:
    return _aware(created_at) + timedelta(hours=max(1, int(hours)))


def evidence_escalate_at(created_at: datetime, hours: int) -> datetime:
    return _aware(created_at) + timedelta(hours=max(1, int(hours)) * 3)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def clamp_remind_hours(value: int) -> int | None:
    if value < EVIDENCE_REMIND_HOURS_MIN or value > EVIDENCE_REMIND_HOURS_MAX:
        return None
    return value


def next_case_number(highest: int | None) -> int:
    current = 0 if highest is None else max(0, highest)
    return current + 1


def needs_hierarchy(kind: CaseKind | str) -> bool:
    return CaseKind(kind) not in _NO_HIERARCHY


def needs_confirmation(kind: CaseKind | str) -> bool:
    return CaseKind(kind) in HEAVY_KINDS


def may_moderate(
    *,
    actor_rank: Rank,
    target_rank: Rank,
    target_is_bot: bool,
    kind: CaseKind | str,
) -> StaffTargetBlock | None:
    resolved = CaseKind(kind)
    if target_is_bot and resolved not in _BOT_OK:
        return StaffTargetBlock.BOT
    if not needs_hierarchy(resolved):
        return None
    if target_rank is Rank.NONE:
        return None
    return staff_target_block(actor=actor_rank, target_rank=target_rank, target_is_bot=False)


def bot_can_enact(
    *,
    bot_top: int | None,
    target_top: int | None,
    target_is_owner: bool,
    bot_can_moderate: bool | None = True,
    target_is_admin: bool = False,
) -> bool:
    """Discord примет санкцию только если бот выше цели и это не владелец сервера.

    `target_is_admin` — только для тайм-аута: Discord не глушит участников с Administrator.
    """
    if target_is_owner or target_is_admin:
        return False
    if bot_can_moderate is False:
        return False
    if bot_top is None or target_top is None:
        return True
    return int(bot_top) > int(target_top)


def is_unlock_duration(raw: str) -> bool:
    return raw.strip().lower().replace(" ", "") in UNLOCK_TOKENS


def parse_timeout_duration(raw: str) -> tuple[DurationVerdict, timedelta | None]:
    text = raw.strip().lower().replace(" ", "")
    if not text:
        return DurationVerdict.BAD, None
    if _DIGITS.fullmatch(text):
        minutes = int(text)
        if minutes < 1:
            return DurationVerdict.BAD, None
        duration = timedelta(minutes=minutes)
    else:
        total = timedelta()
        consumed = 0
        for match in _DURATION_TOKEN.finditer(text):
            amount = int(match.group(1))
            unit = match.group(2)
            consumed += len(match.group(0))
            if unit in {"d", "д"}:
                total += timedelta(days=amount)
            elif unit in {"h", "ч"}:
                total += timedelta(hours=amount)
            else:
                total += timedelta(minutes=amount)
        if consumed != len(text) or total.total_seconds() < 60:
            return DurationVerdict.BAD, None
        duration = total
    if duration > TIMEOUT_MAX:
        return DurationVerdict.TOO_LONG, duration
    return DurationVerdict.OK, duration


def timeout_until(now: datetime, duration: timedelta) -> datetime:
    return now + duration


def history_pages(total: int, *, size: int = HISTORY_PAGE) -> int:
    if total < 1:
        return 1
    return (total + size - 1) // size


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def confirm_intent(
    *,
    consumed_at: datetime | None,
    created_at: datetime,
    now: datetime,
    ttl: timedelta = CONFIRM_TTL,
) -> ConfirmVerdict:
    if consumed_at is not None:
        return ConfirmVerdict.SPENT
    if _utc(now) - _utc(created_at) > ttl:
        return ConfirmVerdict.EXPIRED
    return ConfirmVerdict.OK


def protected_log_channels(*channel_ids: int | None) -> frozenset[int]:
    return frozenset(channel_id for channel_id in channel_ids if channel_id)


def purge_channel_blocked(channel_id: int, protected: Iterable[int]) -> bool:
    return channel_id in set(protected)


def message_too_old(created_at: datetime, now: datetime) -> bool:
    return _utc(created_at) <= _utc(now) - PURGE_MAX_AGE


def pick_purge_batch(
    rows: Sequence[tuple[int, datetime, int]],
    *,
    now: datetime,
    user_id: int | None = None,
    limit: int = PURGE_BATCH,
) -> PurgePick:
    delete_ids: list[int] = []
    skipped = 0
    stop = False
    for message_id, created_at, author_id in rows:
        if user_id is not None and author_id != user_id:
            continue
        if message_too_old(created_at, now):
            skipped += 1
            stop = True
            break
        delete_ids.append(message_id)
        if len(delete_ids) >= limit:
            break
    return PurgePick(tuple(delete_ids), skipped, stop)


def clamp_slowmode(seconds: int) -> int | None:
    if seconds < 0 or seconds > SLOWMODE_MAX:
        return None
    return seconds


def lock_unlock_delay(unlock_at: datetime, now: datetime) -> float:
    return max(0.0, (_utc(unlock_at) - _utc(now)).total_seconds())


@dataclass(frozen=True, slots=True)
class RevokePlan:
    undo_discord: str | None
    refund: int


def can_appeal(
    kind: CaseKind | str,
    created_at: datetime | None,
    now: datetime,
    already_appealed: bool,
) -> bool:
    if already_appealed or created_at is None:
        return False
    try:
        parsed = CaseKind(kind)
    except ValueError:
        return False
    if parsed not in APPEALABLE_KINDS:
        return False
    return _aware(now) <= _aware(created_at) + APPEAL_WINDOW


def revoke_plan(kind: CaseKind | str | object, *, refund: int = 0) -> RevokePlan:
    token = getattr(kind, "kind", kind)
    try:
        parsed = CaseKind(str(token))
    except ValueError:
        parsed = None
    undo = None
    if parsed is CaseKind.TIMEOUT:
        undo = "timeout"
    elif parsed is CaseKind.MUTE:
        undo = "mute"
    return RevokePlan(undo_discord=undo, refund=max(0, int(refund)))


def may_revoke(*, actor: Rank, case_actor: Rank, system: bool = False) -> bool:
    """Отменить дело может спец или тот, кто старше автора.

    Дела автомода записаны рангом «спец», но автор у них — бот: ложное срабатывание
    снимает любой чиф и выше, иначе апелляция на автомод упирается в одного спеца.
    """
    if actor is Rank.SPECIAL:
        return True
    if system:
        return actor >= Rank.CHIEF
    return actor > case_actor


def timeout_still_ours(
    *,
    created_at: datetime | None,
    duration_seconds: int | None,
    member_until: datetime | None,
    now: datetime,
    slack_seconds: int = 5,
) -> bool:
    if created_at is None or duration_seconds is None or member_until is None:
        return False
    until = _aware(created_at) + timedelta(seconds=int(duration_seconds))
    moment = _aware(now)
    if moment >= until:
        return False
    return abs((_aware(member_until) - until).total_seconds()) <= max(0, int(slack_seconds))
