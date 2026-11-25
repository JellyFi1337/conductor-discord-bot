"""Правила тикетов. Без Discord и без БД."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.access import STAFF_RANKS, Rank, can_punish, parse_rank_list
from conductor.domain.ladder import Step, parse_step
from conductor.domain.privacy import FORGOTTEN_REASON

DEFAULT_OPEN_LIMIT = 1
CHANNEL_NAME_LIMIT = 100
COMPLAINT_SUBJECT_FIELD = "На кого жалуетесь"
DEFAULT_VISIBLE_RANKS_JSON = '["watcher","senior","chief","special"]'
TRANSCRIPT_PROGRESS_AFTER = 40
DEFAULT_AUTOCLOSE_HOURS = 48
DEFAULT_REOPEN_DAYS = 7
TICKET_TRANSCRIPT_DAYS = 365
AUTOCLOSE_WARN_BEFORE = timedelta(hours=1)
ESCALATE_COOLDOWN = timedelta(minutes=10)
NOTE_LIMIT = 400
MAX_STAFF_NOTES = 20

_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


class TicketState(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    CLOSED = "closed"
    PENDING_APPROVAL = "pending_approval"
    APPEAL_CHIEF = "appeal_chief"
    APPEAL_OWNER = "appeal_owner"


class TicketOpenMode(StrEnum):
    CHANNEL = "channel"
    THREAD = "thread"


class CloseVerdict(StrEnum):
    OK = "ok"
    STRANGER = "stranger"
    AUTHOR_BLOCKED = "author_blocked"
    HIERARCHY = "hierarchy"
    ALREADY = "already"
    HIDDEN = "hidden"
    NEED_OUTCOME = "need_outcome"
    CASE_APPEAL = "case_appeal"


class ClaimVerdict(StrEnum):
    OK = "ok"
    ALREADY = "already"
    CLOSED = "closed"
    TAKEN = "taken"
    MISSING = "missing"
    HIDDEN = "hidden"


class OpenVerdict(StrEnum):
    OK = "ok"
    LIMIT = "limit"
    MISSING = "missing"
    TENURE = "tenure"
    LADDER = "ladder"
    GATES = "gates"
    HIDDEN = "hidden"
    DM_CLOSED = "dm_closed"
    ANON_OPEN = "anon_open"
    HELD = "held"


class CategoryKind(StrEnum):
    GENERAL = "general"
    COMPLAINT = "complaint"
    BUG = "bug"
    SUGGESTION = "suggestion"
    ANONYMOUS = "anonymous"
    APPLICATION = "application"
    APPEAL = "appeal"


class ApplicationTopic(StrEnum):
    MODERATOR = "moderator"
    TOURNAMENT = "tournament"
    CREW = "crew"


class Outcome(StrEnum):
    RESOLVED = "resolved"
    FIXED = "fixed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    POINTLESS = "pointless"
    DUPLICATE = "duplicate"
    SANCTIONED = "sanctioned"
    NO_ACTION = "no_action"


@dataclass(frozen=True, slots=True)
class OpenDecision:
    verdict: OpenVerdict
    need_days: int = 0
    have_days: int = 0
    need_step: str = Step.GUEST.key
    have_step: str = Step.GUEST.key
    topic: str = ""
    kind: str = CategoryKind.GENERAL


@dataclass(frozen=True, slots=True)
class TicketClosed:
    guild_id: int
    ticket_id: int
    owner_id: int
    category_kind: str
    outcome: str
    closed_by: int
    subject_user_id: int | None = None


class OpenStep(StrEnum):
    OPEN = "open"
    HINT = "hint"
    FORM = "form"


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class AutocloseAction(StrEnum):
    WAIT = "wait"
    WARN = "warn"
    CLOSE = "close"


class EscalateVerdict(StrEnum):
    OK = "ok"
    COOLDOWN = "cooldown"
    CLOSED = "closed"
    EMPTY = "empty"


class RateVerdict(StrEnum):
    OK = "ok"
    ALREADY = "already"
    STRANGER = "stranger"
    OPEN = "open"
    BAD = "bad"


class ReopenVerdict(StrEnum):
    OK = "ok"
    TOO_LATE = "too_late"
    CHANNEL_GONE = "channel_gone"
    NOT_CLOSED = "not_closed"


class NotesVerdict(StrEnum):
    OK = "ok"
    DENIED = "denied"
    CLOSED = "closed"


MAX_FORM_FIELDS = 5
FORM_PROMPT_LIMIT = 45
FORM_ANSWER_LIMIT = 400
HINT_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class TicketSettings:
    open_limit: int = DEFAULT_OPEN_LIMIT
    author_may_close: bool = False
    open_mode: str = TicketOpenMode.CHANNEL
    archive_channel_id: int | None = None
    discord_category_id: int | None = None
    log_channel_id: int | None = None
    autoclose_hours: int = DEFAULT_AUTOCLOSE_HOURS
    reopen_days: int = DEFAULT_REOPEN_DAYS


@dataclass(frozen=True, slots=True)
class StoredTicket:
    guild_id: int
    number: int
    author_id: int
    category_id: int
    channel_id: int
    id: int = 0
    panel_id: int = 0
    state: str = TicketState.OPEN
    claimed_by: int | None = None
    claimed_rank: int = 0
    created_at: datetime | None = None
    closed_at: datetime | None = None
    closed_by: int | None = None
    close_reason: str | None = None
    category_name: str = ""
    form_answers: str = ""
    priority: str = TicketPriority.NORMAL
    last_activity_at: datetime | None = None
    autoclose_warned_at: datetime | None = None
    escalated_at: datetime | None = None
    close_requested_at: datetime | None = None
    rating: int | None = None
    staff_notes: str = "[]"
    outcome: str | None = None
    subject_user_id: int | None = None
    category_kind: str = CategoryKind.GENERAL
    info_text: str = ""
    visible_ranks: str = DEFAULT_VISIBLE_RANKS_JSON
    topic: str = ""
    anonymous: bool = False
    dm_channel_id: int | None = None
    log_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredCategory:
    id: int
    panel_id: int
    guild_id: int
    name: str
    position: int = 0
    hint: str = ""
    form_json: str = ""
    kind: str = CategoryKind.GENERAL
    visible_ranks: str = DEFAULT_VISIBLE_RANKS_JSON
    topic: str = ""
    info_text: str = ""
    hidden: bool = False
    min_tenure_days: int = 0
    min_ladder_step: str = Step.GUEST.key
    excluded_user_field: str = ""


@dataclass(frozen=True, slots=True)
class StoredPanel:
    id: int
    guild_id: int
    channel_id: int
    settings: TicketSettings
    title: str = ""
    body: str = ""


def parse_ticket_settings(raw: str) -> TicketSettings:
    if not raw or not raw.strip():
        return TicketSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        return TicketSettings()
    if not isinstance(data, dict):
        return TicketSettings()
    limit = data.get("open_limit", DEFAULT_OPEN_LIMIT)
    if not isinstance(limit, int) or limit < 1:
        limit = DEFAULT_OPEN_LIMIT
    mode = data.get("open_mode", TicketOpenMode.CHANNEL)
    if mode not in {TicketOpenMode.CHANNEL, TicketOpenMode.THREAD}:
        mode = TicketOpenMode.CHANNEL
    archive = data.get("archive_channel_id")
    category = data.get("discord_category_id")
    log_channel = data.get("log_channel_id")
    return TicketSettings(
        open_limit=limit,
        author_may_close=bool(data.get("author_may_close", False)),
        open_mode=str(mode),
        archive_channel_id=archive if isinstance(archive, int) and archive > 0 else None,
        discord_category_id=category if isinstance(category, int) and category > 0 else None,
        log_channel_id=log_channel if isinstance(log_channel, int) and log_channel > 0 else None,
        autoclose_hours=_positive_int(data.get("autoclose_hours"), DEFAULT_AUTOCLOSE_HOURS, lo=1, hi=720),
        reopen_days=_positive_int(data.get("reopen_days"), DEFAULT_REOPEN_DAYS, lo=1, hi=90),
    )


def dump_ticket_settings(settings: TicketSettings) -> str:
    data: dict[str, object] = {
        "open_limit": settings.open_limit,
        "author_may_close": settings.author_may_close,
        "open_mode": settings.open_mode,
        "autoclose_hours": settings.autoclose_hours,
        "reopen_days": settings.reopen_days,
    }
    if settings.archive_channel_id:
        data["archive_channel_id"] = settings.archive_channel_id
    if settings.discord_category_id:
        data["discord_category_id"] = settings.discord_category_id
    if settings.log_channel_id:
        data["log_channel_id"] = settings.log_channel_id
    return json.dumps(data, ensure_ascii=False)


def _positive_int(value: object, default: int, *, lo: int, hi: int) -> int:
    if not isinstance(value, int) or value < lo:
        return default
    return min(value, hi)


def can_open_ticket(open_count: int, *, limit: int = DEFAULT_OPEN_LIMIT) -> bool:
    return open_count < max(1, limit)


def next_ticket_number(highest: int | None) -> int:
    current = 0 if highest is None else max(0, highest)
    return current + 1


ANON_CHANNEL_AUTHOR = "anon"
ANON_ATTACH_MAX = 8 * 1024 * 1024
DM_FAIL_COOLDOWN = timedelta(minutes=10)


def ticket_channel_author(display_name: str, *, anonymous: bool) -> str:
    return ANON_CHANNEL_AUTHOR if anonymous else display_name


def ticket_channel_name(number: int, category: str, author: str) -> str:
    slug = _SLUG_RE.sub("-", f"{number}-{category}-{author}").strip("-").lower()
    text = slug or f"ticket-{number}"
    return text[:CHANNEL_NAME_LIMIT]


def sanitize_channel_name(name: str) -> str:
    text = _SLUG_RE.sub("-", name.strip()).strip("-")
    if not text:
        return "ticket"
    return text[:CHANNEL_NAME_LIMIT]


def panel_record_ref(panel_id: int) -> str:
    return f"p{panel_id}"


def claimed_rank_value(raw: int | None) -> Rank:
    try:
        return Rank(int(raw or 0))
    except ValueError:
        return Rank.NONE


def may_close(
    *,
    actor_id: int,
    author_id: int,
    claimed_by: int | None,
    actor_rank: Rank,
    claimed_rank: Rank,
    has_close_any: bool,
    has_close_own: bool,
    author_may_close: bool,
    closed: bool,
    close_requested: bool = False,
) -> CloseVerdict:
    if closed:
        return CloseVerdict.ALREADY
    if has_close_any:
        return CloseVerdict.OK
    if has_close_own:
        if claimed_by is None or claimed_by == actor_id:
            return CloseVerdict.OK
        if not can_punish(actor_rank, claimed_rank):
            return CloseVerdict.HIERARCHY
        return CloseVerdict.OK
    if actor_id == author_id:
        if author_may_close or close_requested:
            return CloseVerdict.OK
        return CloseVerdict.AUTHOR_BLOCKED
    return CloseVerdict.STRANGER


def may_edit_reason(
    *,
    actor_id: int,
    author_id: int,
    closed_by: int | None,
    has_close_any: bool,
    state: str,
) -> bool:
    if actor_id == author_id:
        return False
    if state not in {TicketState.CLOSED, TicketState.APPEAL_CHIEF, TicketState.APPEAL_OWNER}:
        return False
    return has_close_any or (closed_by is not None and actor_id == closed_by)


def forget_transcript_body(body: str, *, user_id: int, names: Sequence[str]) -> str:
    text = body
    for token in (f"<@{user_id}>", f"<@!{user_id}>"):
        text = text.replace(token, FORGOTTEN_REASON)
    for name in sorted((item for item in names if item), key=len, reverse=True):
        text = text.replace(name, FORGOTTEN_REASON)
    return text


def may_claim(
    *,
    actor_id: int,
    claimed_by: int | None,
    closed: bool,
) -> ClaimVerdict:
    if closed:
        return ClaimVerdict.CLOSED
    if claimed_by == actor_id:
        return ClaimVerdict.ALREADY
    if claimed_by is not None:
        return ClaimVerdict.TAKEN
    return ClaimVerdict.OK


def may_unclaim(*, actor_id: int, claimed_by: int | None, has_close_any: bool, closed: bool) -> ClaimVerdict:
    if closed:
        return ClaimVerdict.CLOSED
    if claimed_by is None:
        return ClaimVerdict.MISSING
    if claimed_by == actor_id or has_close_any:
        return ClaimVerdict.OK
    return ClaimVerdict.TAKEN


def may_transfer(
    *,
    actor_id: int,
    claimed_by: int | None,
    target_id: int,
    has_close_any: bool,
    closed: bool,
) -> ClaimVerdict:
    if closed:
        return ClaimVerdict.CLOSED
    if target_id == actor_id and claimed_by == actor_id:
        return ClaimVerdict.ALREADY
    if claimed_by == actor_id or has_close_any:
        return ClaimVerdict.OK
    return ClaimVerdict.TAKEN


def may_manage_members(
    *,
    actor_id: int,
    claimed_by: int | None,
    has_close_any: bool,
    closed: bool,
) -> ClaimVerdict:
    if closed:
        return ClaimVerdict.CLOSED
    if claimed_by == actor_id or has_close_any:
        return ClaimVerdict.OK
    return ClaimVerdict.TAKEN


@dataclass(frozen=True, slots=True)
class FormField:
    prompt: str
    required: bool = True
    style: str = "paragraph"


@dataclass(frozen=True, slots=True)
class TicketForm:
    fields: tuple[FormField, ...] = ()


def parse_ticket_form(raw: str) -> TicketForm:
    if not raw or not raw.strip():
        return TicketForm()
    try:
        data = json.loads(raw)
    except ValueError:
        return TicketForm()
    items: object
    if isinstance(data, dict):
        items = data.get("fields", ())
    elif isinstance(data, list):
        items = data
    else:
        return TicketForm()
    if not isinstance(items, list):
        return TicketForm()
    fields: list[FormField] = []
    for item in items:
        prompt = ""
        required = True
        style = "paragraph"
        if isinstance(item, str):
            prompt = item
        elif isinstance(item, dict):
            prompt = str(item.get("prompt", ""))
            required = bool(item.get("required", True))
            found = item.get("style", "paragraph")
            style = "short" if found == "short" else "paragraph"
        text = prompt.strip()[:FORM_PROMPT_LIMIT]
        if not text:
            continue
        fields.append(FormField(prompt=text, required=required, style=style))
        if len(fields) >= MAX_FORM_FIELDS:
            break
    return TicketForm(fields=tuple(fields))


def dump_ticket_form(form: TicketForm) -> str:
    if not form.fields:
        return "{}"
    return json.dumps(
        {
            "fields": [
                {"prompt": field.prompt, "required": field.required, "style": field.style} for field in form.fields
            ]
        },
        ensure_ascii=False,
    )


def collect_form_fields(prompts: Sequence[str]) -> TicketForm:
    fields: list[FormField] = []
    for prompt in prompts:
        text = prompt.strip()[:FORM_PROMPT_LIMIT]
        if not text:
            continue
        fields.append(FormField(prompt=text))
        if len(fields) >= MAX_FORM_FIELDS:
            break
    return TicketForm(fields=tuple(fields))


def parse_form_answers(raw: str) -> tuple[tuple[str, str], ...]:
    if not raw or not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(data, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in data:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            prompt = str(item[0]).strip()
            answer = str(item[1]).strip()
        elif isinstance(item, dict):
            prompt = str(item.get("prompt", "")).strip()
            answer = str(item.get("answer", "")).strip()
        else:
            continue
        if not prompt or not answer:
            continue
        pairs.append((prompt[:FORM_PROMPT_LIMIT], answer[:FORM_ANSWER_LIMIT]))
        if len(pairs) >= MAX_FORM_FIELDS:
            break
    return tuple(pairs)


def dump_form_answers(pairs: Sequence[tuple[str, str]]) -> str:
    cleaned = parse_form_answers(json.dumps([[prompt, answer] for prompt, answer in pairs], ensure_ascii=False))
    if not cleaned:
        return ""
    return json.dumps([list(pair) for pair in cleaned], ensure_ascii=False)


def sanitize_hint(text: str) -> str:
    return text.strip()[:HINT_LIMIT]


def open_step(*, hint: str, form: TicketForm) -> OpenStep:
    if sanitize_hint(hint):
        return OpenStep.HINT
    if form.fields:
        return OpenStep.FORM
    return OpenStep.OPEN


def format_form_transcript(pairs: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"{prompt}: {answer}" for prompt, answer in pairs if prompt and answer)


def parse_priority(raw: str | None) -> TicketPriority:
    text = "" if raw is None else str(raw).strip().lower()
    try:
        return TicketPriority(text)
    except ValueError:
        return TicketPriority.NORMAL


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def activity_at(ticket_created: datetime | None, last_activity: datetime | None) -> datetime | None:
    return last_activity or ticket_created


def autoclose_deadline(last_activity: datetime, hours: int) -> datetime:
    return _utc(last_activity) + timedelta(hours=max(1, hours))


def autoclose_action(
    last_activity: datetime,
    now: datetime,
    *,
    hours: int,
    warned_at: datetime | None,
) -> AutocloseAction:
    close_at = autoclose_deadline(last_activity, hours)
    warn_at = close_at - AUTOCLOSE_WARN_BEFORE
    current = _utc(now)
    if current >= close_at:
        return AutocloseAction.CLOSE
    if current >= warn_at and warned_at is None:
        return AutocloseAction.WARN
    return AutocloseAction.WAIT


def escalate_allowed(
    last_at: datetime | None,
    now: datetime,
    *,
    cooldown: timedelta = ESCALATE_COOLDOWN,
) -> bool:
    if last_at is None:
        return True
    return _utc(now) >= _utc(last_at) + cooldown


def parse_rating(value: object) -> int | None:
    try:
        score = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if score < 1 or score > 5:
        return None
    return score


def may_rate(*, actor_id: int, author_id: int, rating: int | None, closed: bool) -> RateVerdict:
    if not closed:
        return RateVerdict.OPEN
    if actor_id != author_id:
        return RateVerdict.STRANGER
    if rating is not None:
        return RateVerdict.ALREADY
    return RateVerdict.OK


def reopen_until(closed_at: datetime, days: int) -> datetime:
    return _utc(closed_at) + timedelta(days=max(1, days))


def may_reopen(
    *,
    closed: bool,
    closed_at: datetime | None,
    now: datetime,
    days: int,
    channel_alive: bool,
) -> ReopenVerdict:
    if not closed or closed_at is None:
        return ReopenVerdict.NOT_CLOSED
    if _utc(now) > reopen_until(closed_at, days):
        return ReopenVerdict.TOO_LATE
    if not channel_alive:
        return ReopenVerdict.CHANNEL_GONE
    return ReopenVerdict.OK


def may_see_notes(*, actor_id: int, author_id: int, has_notes_key: bool) -> bool:
    return has_notes_key and actor_id != author_id


def notes_verdict(*, actor_id: int, author_id: int, has_notes_key: bool, closed: bool) -> NotesVerdict:
    if closed:
        return NotesVerdict.CLOSED
    if not may_see_notes(actor_id=actor_id, author_id=author_id, has_notes_key=has_notes_key):
        return NotesVerdict.DENIED
    return NotesVerdict.OK


@dataclass(frozen=True, slots=True)
class StaffNote:
    author_id: int
    body: str
    created_at: str


def parse_staff_notes(raw: str) -> tuple[StaffNote, ...]:
    if not raw or not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(data, list):
        return ()
    notes: list[StaffNote] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            author_id = int(item.get("author_id"))
        except (TypeError, ValueError):
            continue
        body = str(item.get("body", "")).strip()[:NOTE_LIMIT]
        stamp = str(item.get("created_at", "")).strip()
        if not body or not stamp:
            continue
        notes.append(StaffNote(author_id=author_id, body=body, created_at=stamp))
        if len(notes) >= MAX_STAFF_NOTES:
            break
    return tuple(notes)


def dump_staff_notes(notes: Sequence[StaffNote]) -> str:
    payload = [{"author_id": note.author_id, "body": note.body, "created_at": note.created_at} for note in notes]
    return json.dumps(payload, ensure_ascii=False)


def append_staff_note(
    raw: str,
    *,
    author_id: int,
    body: str,
    now: datetime,
) -> str:
    text = body.strip()[:NOTE_LIMIT]
    if not text:
        return dump_staff_notes(parse_staff_notes(raw))
    notes = list(parse_staff_notes(raw))
    notes.append(StaffNote(author_id=author_id, body=text, created_at=now.isoformat()))
    return dump_staff_notes(notes[-MAX_STAFF_NOTES:])


_USER_MENTION_RE = re.compile(r"<@!?(\d{15,20})>")
_SNOWFLAKE_RE = re.compile(r"^\d{15,20}$")

INFO_APPLICATION_MODERATOR = "Напишите, почему хотите стать следящим, и сколько времени готовы дежурить."
INFO_APPLICATION_TOURNAMENT = "Опишите формат турнира, даты и что нужно от администрации."
INFO_APPLICATION_CREW = "Опишите проект и какие съёмки планируете на сервере."


@dataclass(frozen=True, slots=True)
class CategorySpec:
    name: str
    kind: CategoryKind
    topic: str = ""
    info_text: str = ""
    hidden: bool = False
    min_tenure_days: int = 0
    min_ladder_step: str = Step.GUEST.key
    excluded_user_field: str = ""
    form_prompts: tuple[str, ...] = ()


DEFAULT_CATEGORY_SPECS: tuple[CategorySpec, ...] = (
    CategorySpec(
        "Жалоба",
        CategoryKind.COMPLAINT,
        min_tenure_days=1,
        excluded_user_field=COMPLAINT_SUBJECT_FIELD,
        form_prompts=(COMPLAINT_SUBJECT_FIELD,),
    ),
    CategorySpec("Вопрос", CategoryKind.GENERAL),
    CategorySpec("Баг сервера", CategoryKind.BUG),
    CategorySpec("Предложение", CategoryKind.SUGGESTION),
    CategorySpec("Анонимно", CategoryKind.ANONYMOUS),
    CategorySpec(
        "Заявка: следящий",
        CategoryKind.APPLICATION,
        topic=ApplicationTopic.MODERATOR,
        info_text=INFO_APPLICATION_MODERATOR,
        min_tenure_days=30,
        min_ladder_step=Step.REGULAR.key,
    ),
    CategorySpec(
        "Заявка: турнир",
        CategoryKind.APPLICATION,
        topic=ApplicationTopic.TOURNAMENT,
        info_text=INFO_APPLICATION_TOURNAMENT,
    ),
    CategorySpec(
        "Заявка: съёмочная",
        CategoryKind.APPLICATION,
        topic=ApplicationTopic.CREW,
        info_text=INFO_APPLICATION_CREW,
    ),
    CategorySpec("Обжалование", CategoryKind.APPEAL, hidden=True),
)
DEFAULT_CATEGORIES: tuple[str, ...] = tuple(spec.name for spec in DEFAULT_CATEGORY_SPECS if not spec.hidden)


def parse_kind(raw: str | CategoryKind | None) -> CategoryKind:
    if isinstance(raw, CategoryKind):
        return raw
    try:
        return CategoryKind((raw or "").strip().lower())
    except ValueError:
        return CategoryKind.GENERAL


def parse_outcome(raw: str | Outcome | None) -> Outcome | None:
    if isinstance(raw, Outcome):
        return raw
    text = "" if raw is None else str(raw).strip().lower()
    if not text:
        return None
    try:
        return Outcome(text)
    except ValueError:
        return None


def parse_topic(raw: str | None) -> str:
    text = "" if raw is None else str(raw).strip().lower()
    try:
        return ApplicationTopic(text)
    except ValueError:
        return ""


def default_visible_ranks(kind: CategoryKind | str) -> frozenset[Rank]:
    parsed = parse_kind(kind)
    if parsed in {CategoryKind.BUG, CategoryKind.SUGGESTION}:
        return frozenset({Rank.SPECIAL})
    if parsed is CategoryKind.APPEAL:
        return frozenset({Rank.CHIEF, Rank.SPECIAL})
    return frozenset(STAFF_RANKS)


def dump_visible_ranks(ranks: Iterable[Rank]) -> str:
    keys = [rank.key for rank in STAFF_RANKS if rank in set(ranks)]
    if not keys:
        keys = [rank.key for rank in STAFF_RANKS]
    return json.dumps(keys, ensure_ascii=False)


def parse_visible_ranks(raw: str | None, *, kind: CategoryKind | str = CategoryKind.GENERAL) -> frozenset[Rank]:
    fallback = default_visible_ranks(kind)
    if raw is None or not str(raw).strip():
        return fallback
    text = str(raw).strip()
    tokens: list[str]
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            return fallback
        if not isinstance(data, list):
            return fallback
        tokens = [str(item).strip() for item in data if str(item).strip()]
    else:
        tokens = [part.strip() for part in text.replace(";", ",").replace(" ", ",").split(",") if part.strip()]
    if not tokens:
        return fallback
    try:
        ranks = parse_rank_list(tokens)
    except ValueError:
        return fallback
    found = frozenset(rank for rank in ranks if rank in STAFF_RANKS)
    return found or fallback


def visible_rank_set(raw: str | None, kind: CategoryKind | str = CategoryKind.GENERAL) -> frozenset[Rank]:
    return parse_visible_ranks(raw, kind=kind)


def rank_may_see(rank: Rank, visible: frozenset[Rank], *, is_administrator: bool = False) -> bool:
    if is_administrator:
        return True
    return rank in visible


def allowed_outcomes(kind: CategoryKind | str) -> tuple[Outcome, ...]:
    parsed = parse_kind(kind)
    if parsed is CategoryKind.BUG:
        return (Outcome.FIXED, Outcome.DUPLICATE, Outcome.POINTLESS, Outcome.NO_ACTION)
    if parsed is CategoryKind.SUGGESTION:
        return (Outcome.ACCEPTED, Outcome.REJECTED, Outcome.DUPLICATE, Outcome.POINTLESS)
    if parsed is CategoryKind.COMPLAINT:
        return (Outcome.SANCTIONED, Outcome.NO_ACTION, Outcome.DUPLICATE, Outcome.POINTLESS)
    if parsed in {CategoryKind.APPLICATION, CategoryKind.APPEAL}:
        return (Outcome.ACCEPTED, Outcome.REJECTED)
    return (Outcome.RESOLVED, Outcome.POINTLESS, Outcome.DUPLICATE, Outcome.NO_ACTION)


def resolve_close_outcome(
    *,
    system: bool,
    author_close: bool,
    raw: str | None,
    kind: CategoryKind | str,
) -> Outcome | None:
    if system:
        return Outcome.NO_ACTION
    if author_close:
        return Outcome.RESOLVED
    parsed = parse_outcome(raw)
    if parsed is None or parsed not in allowed_outcomes(kind):
        return None
    return parsed


def has_explain(event_kinds: Iterable[str]) -> bool:
    return any(str(kind) == TicketEventKind.EXPLAIN for kind in event_kinds)


def member_tenure_days(joined_at: datetime | None, now: datetime | None = None) -> int:
    if joined_at is None:
        return 0
    current = datetime.now(UTC) if now is None else now
    return max(0, (_utc(current) - _utc(joined_at)).days)


def step_meets(have: str | Step, need: str | Step) -> bool:
    return parse_step(have) >= parse_step(need)


def open_verdict(
    kind: CategoryKind | str,
    tenure_days: int,
    ladder_step: str,
    category: StoredCategory | CategorySpec,
) -> OpenDecision:
    parsed = parse_kind(kind)
    hidden = bool(getattr(category, "hidden", False))
    min_days = max(0, int(getattr(category, "min_tenure_days", 0) or 0))
    min_step = parse_step(getattr(category, "min_ladder_step", Step.GUEST.key)).key
    topic = str(getattr(category, "topic", "") or "")
    have_days = max(0, int(tenure_days))
    have_step = parse_step(ladder_step).key
    if hidden or parsed is CategoryKind.APPEAL:
        return OpenDecision(OpenVerdict.HIDDEN, kind=parsed, topic=topic)
    tenure_ok = have_days >= min_days
    ladder_ok = step_meets(have_step, min_step)
    if tenure_ok and ladder_ok:
        verdict = OpenVerdict.OK
    elif not tenure_ok and not ladder_ok:
        verdict = OpenVerdict.GATES
    elif not tenure_ok:
        verdict = OpenVerdict.TENURE
    else:
        verdict = OpenVerdict.LADDER
    return OpenDecision(
        verdict,
        need_days=min_days,
        have_days=have_days,
        need_step=min_step,
        have_step=have_step,
        topic=topic,
        kind=parsed,
    )


def parse_subject_user_id(text: str) -> int | None:
    mention = _USER_MENTION_RE.search(text)
    if mention:
        return int(mention.group(1))
    stripped = text.strip()
    if _SNOWFLAKE_RE.match(stripped):
        return int(stripped)
    return None


def extract_subject_user_id(answers: Sequence[tuple[str, str]], field_name: str) -> int | None:
    needle = field_name.strip().lower()
    if not needle:
        return None
    for prompt, answer in answers:
        if prompt.strip().lower() == needle:
            return parse_subject_user_id(answer)
    return None


APPEAL_WINDOW = timedelta(days=DEFAULT_REOPEN_DAYS)
REVIEW_REMIND = timedelta(hours=48)
LEVEL_CHIEF = "chief"
LEVEL_OWNER = "owner"
WORKING_STATES = frozenset({TicketState.OPEN, TicketState.CLAIMED})
REVIEW_STATES = frozenset({TicketState.PENDING_APPROVAL, TicketState.APPEAL_CHIEF, TicketState.APPEAL_OWNER})
APPEALABLE_KINDS = frozenset({CategoryKind.APPLICATION, CategoryKind.COMPLAINT, CategoryKind.APPEAL})
APPEALABLE_OUTCOMES = frozenset({Outcome.REJECTED, Outcome.POINTLESS})


class FlowAction(StrEnum):
    APPROVE_REQ = "approve_req"
    APPROVE = "approve"
    REJECT = "reject"
    APPEAL = "appeal"
    UPHOLD = "uphold"
    OVERTURN = "overturn"


class FlowRole(StrEnum):
    AUTHOR = "author"
    CLAIMER = "claimer"
    CLOSE_ANY = "close_any"
    SPECIAL = "special"
    CHIEF = "chief"
    OWNER = "owner"
    OTHER = "other"


class TicketEventKind(StrEnum):
    APPROVE_REQ = "approve_req"
    APPROVE = "approve"
    REJECT = "reject"
    APPEAL_CHIEF = "appeal_chief"
    APPEAL_OWNER = "appeal_owner"
    UPHOLD = "uphold"
    OVERTURN = "overturn"
    REMIND = "remind"
    DM_CLOSED = "dm_closed"
    REASON = "reason"
    EXPLAIN = "explain"


_STAFF_SENDERS = frozenset({FlowRole.CLAIMER, FlowRole.CLOSE_ANY})
_CHIEF_REVIEWERS = frozenset({FlowRole.CHIEF, FlowRole.SPECIAL})
_ENTER_OR_REMIND = frozenset(
    {
        TicketEventKind.APPROVE_REQ,
        TicketEventKind.APPEAL_CHIEF,
        TicketEventKind.APPEAL_OWNER,
        TicketEventKind.REMIND,
    }
)


@dataclass(frozen=True, slots=True)
class FlowDecision:
    ok: bool
    state: str | None = None
    reason: str = ""
    event_kind: str = ""
    appeal_level: str = ""


def parse_ticket_state(raw: str | TicketState | None) -> str:
    if isinstance(raw, TicketState):
        return raw
    text = "" if raw is None else str(raw).strip().lower()
    try:
        return TicketState(text)
    except ValueError:
        return text


def is_working(state: str | TicketState | None) -> bool:
    return parse_ticket_state(state) in WORKING_STATES


def is_review(state: str | TicketState | None) -> bool:
    return parse_ticket_state(state) in REVIEW_STATES


def used_appeal_levels(event_kinds: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for kind in event_kinds:
        if kind == TicketEventKind.APPEAL_CHIEF:
            found.add(LEVEL_CHIEF)
        elif kind == TicketEventKind.APPEAL_OWNER:
            found.add(LEVEL_OWNER)
    return frozenset(found)


def next_appeal_level(kind: CategoryKind | str, used_levels: Iterable[str] = ()) -> str | None:
    used = frozenset(used_levels)
    if parse_kind(kind) is CategoryKind.APPEAL:
        return None if LEVEL_OWNER in used else LEVEL_OWNER
    if LEVEL_CHIEF not in used:
        return LEVEL_CHIEF
    if LEVEL_OWNER not in used:
        return LEVEL_OWNER
    return None


def can_appeal(
    *,
    kind: CategoryKind | str,
    outcome: str | Outcome | None,
    now: datetime,
    closed_at: datetime | None,
    used_levels: Iterable[str] = (),
) -> bool:
    if parse_kind(kind) not in APPEALABLE_KINDS:
        return False
    if parse_outcome(outcome) not in APPEALABLE_OUTCOMES:
        return False
    if closed_at is None:
        return False
    if _utc(now) > _utc(closed_at) + APPEAL_WINDOW:
        return False
    return next_appeal_level(kind, used_levels) is not None


def remind_due(last_at: datetime | None, now: datetime) -> bool:
    if last_at is None:
        return True
    return _utc(now) >= _utc(last_at) + REVIEW_REMIND


def is_enter_or_remind(kind: str) -> bool:
    return kind in _ENTER_OR_REMIND


def _denied(reason: str) -> FlowDecision:
    return FlowDecision(False, reason=reason)


def _allowed(state: TicketState, event_kind: TicketEventKind, *, level: str = "") -> FlowDecision:
    return FlowDecision(True, state=state, event_kind=event_kind, appeal_level=level)


def transition(
    state: str,
    action: str | FlowAction,
    actor_role_in_ticket: str | FlowRole,
    kind: str | CategoryKind,
    outcome: str | Outcome | None,
    now: datetime,
    closed_at: datetime | None,
    *,
    closer_is_actor: bool = False,
    used_levels: Iterable[str] = (),
) -> FlowDecision:
    try:
        act = FlowAction(action)
    except ValueError:
        return _denied("wrong_state")
    try:
        role = FlowRole(actor_role_in_ticket)
    except ValueError:
        role = FlowRole.OTHER
    current = parse_ticket_state(state)
    parsed_kind = parse_kind(kind)
    if act is FlowAction.APPROVE_REQ:
        if current != TicketState.CLAIMED:
            return _denied("wrong_state")
        if parsed_kind is not CategoryKind.APPLICATION:
            return _denied("wrong_kind")
        if role not in _STAFF_SENDERS:
            return _denied("wrong_role")
        return _allowed(TicketState.PENDING_APPROVAL, TicketEventKind.APPROVE_REQ)
    if act is FlowAction.APPROVE:
        if current != TicketState.PENDING_APPROVAL:
            return _denied("wrong_state")
        if role is not FlowRole.SPECIAL:
            return _denied("wrong_role")
        return _allowed(TicketState.CLOSED, TicketEventKind.APPROVE)
    if act is FlowAction.REJECT:
        if current == TicketState.PENDING_APPROVAL:
            if role is not FlowRole.SPECIAL:
                return _denied("wrong_role")
            return _allowed(TicketState.CLOSED, TicketEventKind.REJECT)
        if current != TicketState.CLAIMED:
            return _denied("wrong_state")
        if parsed_kind is not CategoryKind.APPLICATION:
            return _denied("wrong_kind")
        if role not in _STAFF_SENDERS:
            return _denied("wrong_role")
        return _allowed(TicketState.CLOSED, TicketEventKind.REJECT)
    if act is FlowAction.APPEAL:
        if current != TicketState.CLOSED:
            return _denied("wrong_state")
        if role is not FlowRole.AUTHOR:
            return _denied("wrong_role")
        if parsed_kind not in APPEALABLE_KINDS:
            return _denied("wrong_kind")
        if parse_outcome(outcome) not in APPEALABLE_OUTCOMES:
            return _denied("wrong_outcome")
        if closed_at is None or _utc(now) > _utc(closed_at) + APPEAL_WINDOW:
            return _denied("too_late")
        level = next_appeal_level(parsed_kind, used_levels)
        if level is None:
            return _denied("already")
        if level == LEVEL_CHIEF:
            return _allowed(TicketState.APPEAL_CHIEF, TicketEventKind.APPEAL_CHIEF, level=level)
        return _allowed(TicketState.APPEAL_OWNER, TicketEventKind.APPEAL_OWNER, level=level)
    if act in {FlowAction.UPHOLD, FlowAction.OVERTURN}:
        if closer_is_actor:
            return _denied("closer")
        event = TicketEventKind.UPHOLD if act is FlowAction.UPHOLD else TicketEventKind.OVERTURN
        target = TicketState.CLOSED if act is FlowAction.UPHOLD else TicketState.OPEN
        if current == TicketState.APPEAL_CHIEF:
            if role not in _CHIEF_REVIEWERS:
                return _denied("wrong_role")
            return _allowed(target, event)
        if current == TicketState.APPEAL_OWNER:
            if role is not FlowRole.OWNER:
                return _denied("wrong_role")
            return _allowed(target, event)
        return _denied("wrong_state")
    return _denied("wrong_state")


def is_staff_relay_skip(message: object) -> bool:
    author = getattr(message, "author", None)
    if author is None or bool(getattr(author, "bot", False)):
        return True
    if getattr(message, "interaction_metadata", None) is not None:
        return True
    if bool(getattr(getattr(message, "flags", None), "voice", False)):
        return True
    content = str(getattr(message, "content", None) or "")
    if content.startswith("/"):
        return True
    attachments = getattr(message, "attachments", None) or ()
    return not content.strip() and not attachments


def attachment_over_limit(size: int, *, limit: int = ANON_ATTACH_MAX) -> bool:
    return int(size) > limit


def dm_fail_due(last_at: datetime | None, now: datetime) -> bool:
    if last_at is None:
        return True
    return _utc(now) >= _utc(last_at) + DM_FAIL_COOLDOWN
