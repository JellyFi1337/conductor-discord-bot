"""Правила автомодерации: схема файла, счётчики и окно. Без Discord и без БД."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from conductor.domain.access import Rank, is_special_or_admin, parse_rank_token

__all__ = [
    "ACTIONS",
    "AUTOMOD_RULES",
    "EDIT_RULES",
    "AutomodAction",
    "AutomodConfig",
    "AutomodReason",
    "ConfigError",
    "DEFAULT_MAPPING",
    "DELETE_MODES",
    "DeleteMode",
    "Exempt",
    "ExemptCause",
    "MessageFacts",
    "Newcomer",
    "RequestStatus",
    "RollingWindow",
    "Rule",
    "Trigger",
    "after_trigger",
    "card_fingerprint",
    "may_decide_automod",
    "rule_specs",
    "sanitize_request",
    "count_emoji",
    "count_gifs",
    "default_config",
    "effective_limit",
    "evaluate",
    "exempt_cause",
    "extract_urls",
    "fill_missing_rules",
    "foreign_invites",
    "has_everyone",
    "has_invite",
    "invite_codes",
    "is_newcomer",
    "normalize_for_duplicate",
    "parse_config",
    "rule_exempt",
]

AUTOMOD_RULES: tuple[str, ...] = (
    "emoji_flood",
    "invite_link",
    "everyone_mention",
    "duplicate",
    "user_mentions",
    "role_mentions",
    "gif_flood",
)
_RULE_SET = frozenset(AUTOMOD_RULES)
_TOP_KEYS = frozenset({"window_minutes", "timeout_minutes", "exempt", "newcomer", "rules"})
_EXEMPT_KEYS = frozenset({"administrators", "ranks", "roles", "channels"})
_NEWCOMER_KEYS = frozenset({"hours", "factor"})
_RULE_KEYS = frozenset({"limit", "action", "delete", "timeout_minutes", "exempt"})
EDIT_RULES: tuple[str, ...] = ("everyone_mention", "invite_link")

_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{17,20}>")
_INVITE_HOST = (
    r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me|"
    r"discord\.io|invite\.gg|disboard\.org/server/join)"
)
_INVITE = re.compile(rf"(?i){_INVITE_HOST}/([A-Za-z0-9-]{{2,32}})")
_DOT_SPACES = re.compile(r"\s*\.\s*")
_DOT_WORD = re.compile(r"\(\s*dot\s*\)", re.IGNORECASE)
_DOT_SPACED = re.compile(r"\s+dot\s+", re.IGNORECASE)
_FENCE = re.compile(r"```[\s\S]*?```")
_INLINE = re.compile(r"`[^`]*`")
# Discord пингует только строчные @everyone / @here; «@Here we go» — не тег.
_EVERYONE = re.compile(r"(?<![\w<])@(everyone|here)\b")
_INVISIBLES = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_USER_MENTION = re.compile(r"<@!?\d+>")
_ROLE_MENTION = re.compile(r"<@&\d+>")
_GIF_HOSTS = ("tenor.com", "giphy.com")
_STAFF_EXEMPT: dict[str, object] = {
    "administrators": True,
    "ranks": ["watcher", "senior", "chief", "special"],
    "roles": [],
    "channels": [],
}
_STRICT_EXEMPT: dict[str, object] = {
    "administrators": True,
    "ranks": ["special"],
    "roles": [],
    "channels": [],
}
# Правила, где исключены только special и администраторы, даже если в файле нет своего exempt.
_STRICT_RULES = frozenset({"invite_link", "everyone_mention"})
_KEYCAP_BASE = frozenset("#*0123456789")
_REGIONAL = range(0x1F1E6, 0x1F1FF + 1)
_SKIN = range(0x1F3FB, 0x1F3FF + 1)
_ZWJ = 0x200D
_VS16 = 0xFE0F
_KEYCAP = 0x20E3

DEFAULT_MAPPING: dict[str, object] = {
    "window_minutes": 60,
    "timeout_minutes": 10,
    "exempt": dict(_STAFF_EXEMPT),
    "newcomer": {"hours": 24, "factor": 0.5},
    "rules": {
        "emoji_flood": {"limit": 10, "action": "timeout", "delete": "message"},
        "invite_link": {
            "limit": 1,
            "action": "timeout",
            "delete": "message",
            "timeout_minutes": 30,
            "exempt": dict(_STRICT_EXEMPT),
        },
        "everyone_mention": {
            "limit": 1,
            "action": "timeout",
            "delete": "message",
            "timeout_minutes": 60,
            "exempt": dict(_STRICT_EXEMPT),
        },
        "duplicate": {"limit": 3, "action": "timeout", "delete": "duplicates"},
        "user_mentions": {"limit": 5, "action": "timeout", "delete": "none"},
        "role_mentions": {"limit": 3, "action": "timeout", "delete": "none"},
        "gif_flood": {"limit": 5, "action": "timeout", "delete": "none"},
    },
}


class AutomodAction(StrEnum):
    TIMEOUT = "timeout"
    WARN = "warn"
    DELETE_ONLY = "delete_only"
    OFF = "off"


class DeleteMode(StrEnum):
    NONE = "none"
    MESSAGE = "message"
    DUPLICATES = "duplicates"


class ExemptCause(StrEnum):
    CHANNEL = "channel"
    RANK = "rank"
    ROLE = "role"
    LIMITS = "limits"


class RequestStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


ACTIONS = frozenset(item.value for item in AutomodAction)
DELETE_MODES = frozenset(item.value for item in DeleteMode)


@dataclass(frozen=True, slots=True)
class ConfigError:
    path: str
    message: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.message}"
        return self.message


@dataclass(frozen=True, slots=True)
class Exempt:
    administrators: bool = True
    ranks: tuple[str, ...] = ()
    roles: tuple[int, ...] = ()
    channels: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Newcomer:
    hours: int = 24
    factor: float = 0.5

    @property
    def days(self) -> int:
        """0 — коэффициент новичка выключен (`hours == 0`)."""
        return 0 if self.hours <= 0 else (self.hours + 23) // 24


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    limit: int
    action: AutomodAction
    delete: DeleteMode
    timeout_minutes: int | None = None
    exempt: Exempt | None = None


@dataclass(frozen=True, slots=True)
class AutomodConfig:
    window_minutes: int
    timeout_minutes: int
    exempt: Exempt
    newcomer: Newcomer
    rules: dict[str, Rule]

    @property
    def window(self) -> timedelta:
        return timedelta(minutes=self.window_minutes)


@dataclass(frozen=True, slots=True)
class MessageFacts:
    guild_id: int
    user_id: int
    channel_id: int
    message_id: int
    text: str = ""
    attachment_names: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    user_mentions: int = 0
    role_mentions: int = 0
    member_since: datetime | None = None
    everyone: bool = False
    foreign_invites: int = 0
    foreign_codes: tuple[str, ...] = ()
    invite_fail_closed: bool = False


@dataclass(frozen=True, slots=True)
class AutomodReason:
    audit: str
    member: str
    staff: str


@dataclass(frozen=True, slots=True)
class Trigger:
    rule: str
    used: int
    limit: int
    action: AutomodAction
    delete: DeleteMode
    timeout_minutes: int


@dataclass(slots=True)
class _Stamp:
    at: datetime
    amount: int = 1
    message_id: int = 0
    text: str = ""


@dataclass(slots=True)
class RollingWindow:
    """События одного человека за текущее окно. Живёт в TTLCache."""

    _events: dict[str, list[_Stamp]] = field(default_factory=dict)
    _cased: dict[str, datetime] = field(default_factory=dict)

    def prune(self, now: datetime, window: timedelta) -> None:
        cutoff = _aware(now) - window
        for name, items in list(self._events.items()):
            kept = [item for item in items if _aware(item.at) > cutoff]
            if kept:
                self._events[name] = kept
            else:
                self._events.pop(name, None)
        self._cased = {name: when for name, when in self._cased.items() if _aware(when) > cutoff}

    def add(
        self,
        rule: str,
        now: datetime,
        amount: int = 1,
        *,
        message_id: int = 0,
        text: str = "",
    ) -> None:
        if amount <= 0:
            return
        self._events.setdefault(rule, []).append(
            _Stamp(at=_aware(now), amount=int(amount), message_id=int(message_id), text=text),
        )

    def used(self, rule: str, *, text: str | None = None) -> int:
        items = self._events.get(rule, ())
        if text is None:
            return sum(item.amount for item in items)
        return sum(item.amount for item in items if item.text == text)

    def message_ids(self, rule: str, *, text: str | None = None) -> tuple[int, ...]:
        items = self._events.get(rule, ())
        if text is None:
            found = [item.message_id for item in items if item.message_id]
        else:
            found = [item.message_id for item in items if item.message_id and item.text == text]
        return tuple(found)

    def reset(self, rule: str, *, text: str | None = None) -> None:
        if text is None:
            self._events.pop(rule, None)
            return
        kept = [item for item in self._events.get(rule, ()) if item.text != text]
        if kept:
            self._events[rule] = kept
        else:
            self._events.pop(rule, None)

    def mark_cased(self, rule: str, now: datetime) -> None:
        self._cased[rule] = _aware(now)

    def was_cased(self, rule: str, now: datetime, window: timedelta) -> bool:
        when = self._cased.get(rule)
        if when is None:
            return False
        return _aware(now) - _aware(when) < window


def default_config() -> AutomodConfig:
    parsed = parse_config(DEFAULT_MAPPING)
    if isinstance(parsed, list):
        raise RuntimeError("builtin automod rules are invalid")
    return parsed


def fill_missing_rules(config: AutomodConfig) -> AutomodConfig:
    """Правила, которых нет в файле сервера, берутся из встроенных умолчаний."""
    missing = [name for name in AUTOMOD_RULES if name not in config.rules]
    if not missing:
        return config
    builtin = default_config().rules
    rules = dict(config.rules)
    for name in missing:
        extra = builtin.get(name)
        if extra is not None:
            rules[name] = extra
    return AutomodConfig(
        window_minutes=config.window_minutes,
        timeout_minutes=config.timeout_minutes,
        exempt=config.exempt,
        newcomer=config.newcomer,
        rules=rules,
    )


def parse_config(mapping: object) -> AutomodConfig | list[ConfigError]:
    errors: list[ConfigError] = []
    if not isinstance(mapping, dict):
        return [ConfigError("", "root_not_object")]
    _unknown(mapping, _TOP_KEYS, "", errors)
    window = _int(mapping.get("window_minutes"), "window_minutes", errors, minimum=1)
    timeout = _int(mapping.get("timeout_minutes"), "timeout_minutes", errors, minimum=1)
    exempt = _exempt(mapping.get("exempt"), errors)
    newcomer = _newcomer(mapping.get("newcomer"), errors)
    rules = _rules(mapping.get("rules"), errors)
    if errors:
        return errors
    assert window is not None and timeout is not None
    assert exempt is not None and newcomer is not None
    return AutomodConfig(
        window_minutes=window,
        timeout_minutes=timeout,
        exempt=exempt,
        newcomer=newcomer,
        rules=rules,
    )


def count_emoji(text: str) -> int:
    raw = str(text or "")
    if not raw:
        return 0
    custom = _CUSTOM_EMOJI.findall(raw)
    rest = _CUSTOM_EMOJI.sub("\x00", raw)
    return len(custom) + _count_unicode_emoji(rest)


def has_invite(text: str) -> bool:
    return bool(invite_codes(text))


def invite_codes(text: str) -> tuple[str, ...]:
    raw = _normalize_invite_text(text)
    found: list[str] = []
    seen: set[str] = set()
    for match in _INVITE.finditer(raw):
        code = str(match.group(1) or "")
        key = code.casefold()
        if not code or key in seen:
            continue
        seen.add(key)
        found.append(code)
    return tuple(found)


def foreign_invites(codes: Collection[str], allowed: Collection[str]) -> tuple[str, ...]:
    own = {str(item).casefold() for item in allowed}
    return tuple(code for code in codes if str(code).casefold() not in own)


def has_everyone(text: str) -> bool:
    raw = unicodedata.normalize("NFKC", str(text or ""))
    raw = raw.translate(_INVISIBLES)
    raw = _FENCE.sub(" ", raw)
    raw = _INLINE.sub(" ", raw)
    return bool(_EVERYONE.search(raw))


def _normalize_invite_text(text: str) -> str:
    raw = str(text or "")
    raw = raw.replace("[.]", ".").replace("(.)", ".")
    raw = _DOT_WORD.sub(".", raw)
    raw = _DOT_SPACED.sub(".", raw)
    return _DOT_SPACES.sub(".", raw)


def extract_urls(text: str) -> tuple[str, ...]:
    return tuple(_URL.findall(str(text or "")))


def count_user_mentions(text: str) -> int:
    return len(_USER_MENTION.findall(str(text or "")))


def count_role_mentions(text: str) -> int:
    return len(_ROLE_MENTION.findall(str(text or "")))


def exempt_cause(
    *,
    channel_id: int,
    rank: str,
    role_ids: Collection[int],
    is_administrator: bool,
    is_guild_owner: bool,
    config: AutomodConfig,
    log_channel_ids: Collection[int] = (),
    exempt: Exempt | None = None,
) -> ExemptCause | None:
    block = exempt if exempt is not None else config.exempt
    if int(channel_id) in {int(item) for item in log_channel_ids} or int(channel_id) in set(block.channels):
        return ExemptCause.CHANNEL
    if rank in block.ranks:
        return ExemptCause.RANK
    granted = set(block.roles)
    if any(int(role_id) in granted for role_id in role_ids):
        return ExemptCause.ROLE
    rank_enum = _rank_enum(rank)
    if block.administrators and is_special_or_admin(
        rank=rank_enum,
        is_administrator=is_administrator,
        is_guild_owner=is_guild_owner,
    ):
        return ExemptCause.RANK if rank_enum is Rank.SPECIAL else ExemptCause.LIMITS
    return None


def rule_exempt(rule: Rule | None, config: AutomodConfig) -> Exempt:
    if rule is not None and rule.exempt is not None:
        return rule.exempt
    return config.exempt


def normalize_for_duplicate(text: str) -> str:
    chars: list[str] = []
    for ch in str(text or "").casefold():
        chars.append(ch if ch.isalnum() or ch.isspace() else " ")
    return " ".join("".join(chars).split())


def count_gifs(attachment_names: Sequence[str], urls: Sequence[str]) -> int:
    total = 0
    for name in attachment_names:
        if str(name or "").lower().split("?", 1)[0].endswith(".gif"):
            total += 1
    for url in urls:
        if _gif_url(str(url or "")):
            total += 1
    return total


def is_newcomer(member_since: datetime | None, now: datetime, hours: int) -> bool:
    if member_since is None or hours <= 0:
        return False
    return _aware(now) - _aware(member_since) < timedelta(hours=int(hours))


def effective_limit(rule: Rule, is_new: bool, factor: float = 0.5) -> int:
    if not is_new or factor == 1:
        return rule.limit
    return max(1, math.ceil(rule.limit * factor))


def evaluate(
    facts: MessageFacts,
    state: RollingWindow,
    config: AutomodConfig,
    now: datetime,
    *,
    only: Collection[str] | None = None,
) -> list[Trigger]:
    state.prune(now, config.window)
    newcomer = is_newcomer(facts.member_since, now, config.newcomer.hours)
    wanted = None if only is None else frozenset(only)
    found: list[Trigger] = []
    for name in AUTOMOD_RULES:
        if wanted is not None and name not in wanted:
            continue
        rule = config.rules.get(name)
        if rule is None or rule.action is AutomodAction.OFF:
            continue
        used = _record(name, facts, state, now)
        if used <= 0:
            continue
        limit = effective_limit(rule, newcomer, config.newcomer.factor)
        if not _exceeds(name, used, limit):
            continue
        minutes = rule.timeout_minutes if rule.timeout_minutes is not None else config.timeout_minutes
        found.append(
            Trigger(
                rule=name,
                used=used,
                limit=limit,
                action=rule.action,
                delete=rule.delete,
                timeout_minutes=minutes,
            )
        )
    return found


def rule_specs(config: AutomodConfig) -> tuple[tuple[str, int, str, str, int, int], ...]:
    found: list[tuple[str, int, str, str, int, int]] = []
    for name in AUTOMOD_RULES:
        rule = config.rules.get(name)
        if rule is None:
            continue
        minutes = rule.timeout_minutes if rule.timeout_minutes is not None else config.timeout_minutes
        found.append((name, rule.limit, rule.action.value, rule.delete.value, minutes, config.window_minutes))
    return tuple(found)


def card_fingerprint(
    *,
    lines: Sequence[str],
    exempt: str,
    hits: Sequence[tuple[str, int]],
    mtime: int | None,
    loaded_at: int | None,
) -> str:
    payload = "\n".join(
        (
            *lines,
            exempt,
            *(f"{name}:{count}" for name, count in hits),
            str(mtime),
            str(loaded_at),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sanitize_request(*, rule: str, proposal: str, reason: str) -> tuple[str, str, str] | None:
    cleaned_rule = " ".join(str(rule or "").split())
    cleaned_proposal = " ".join(str(proposal or "").split())
    cleaned_reason = " ".join(str(reason or "").split())
    if not cleaned_reason or not cleaned_rule or not cleaned_proposal:
        return None
    return cleaned_rule[:80], cleaned_proposal[:200], cleaned_reason[:400]


def may_decide_automod(*, is_owner: bool) -> bool:
    return bool(is_owner)


def after_trigger(state: RollingWindow, rule: str, *, text: str = "") -> None:
    if rule == "duplicate" and text:
        state.reset(rule, text=text)
        return
    state.reset(rule)


def _record(name: str, facts: MessageFacts, state: RollingWindow, now: datetime) -> int:
    if name == "emoji_flood":
        amount = count_emoji(facts.text)
        state.add(name, now, amount, message_id=facts.message_id)
        return state.used(name)
    if name == "invite_link":
        amount = max(0, int(facts.foreign_invites))
        if amount <= 0:
            return state.used(name)
        state.add(name, now, amount, message_id=facts.message_id)
        return state.used(name)
    if name == "everyone_mention":
        if not facts.everyone:
            return state.used(name)
        state.add(name, now, 1, message_id=facts.message_id)
        return state.used(name)
    if name == "duplicate":
        normalized = normalize_for_duplicate(facts.text)
        if not normalized:
            return 0
        state.add(name, now, 1, message_id=facts.message_id, text=normalized)
        return state.used(name, text=normalized)
    if name == "user_mentions":
        state.add(name, now, int(facts.user_mentions), message_id=facts.message_id)
        return state.used(name)
    if name == "role_mentions":
        state.add(name, now, int(facts.role_mentions), message_id=facts.message_id)
        return state.used(name)
    if name == "gif_flood":
        amount = count_gifs(facts.attachment_names, facts.urls)
        state.add(name, now, amount, message_id=facts.message_id)
        return state.used(name)
    return 0


def _exceeds(name: str, used: int, limit: int) -> bool:
    if name in {"invite_link", "everyone_mention"}:
        return used >= limit
    return used > limit


def _exempt(raw: object, errors: list[ConfigError], *, path: str = "exempt") -> Exempt | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if not isinstance(raw, dict):
        errors.append(ConfigError(path, "expected_object"))
        return None
    _unknown(raw, _EXEMPT_KEYS, path, errors)
    administrators = _bool(raw.get("administrators"), f"{path}.administrators", errors)
    ranks = _ranks(raw.get("ranks"), f"{path}.ranks", errors)
    roles = _id_list(raw.get("roles"), f"{path}.roles", errors)
    channels = _id_list(raw.get("channels"), f"{path}.channels", errors)
    if administrators is None or ranks is None or roles is None or channels is None:
        return None
    return Exempt(administrators=administrators, ranks=tuple(ranks), roles=tuple(roles), channels=tuple(channels))


def _newcomer(raw: object, errors: list[ConfigError]) -> Newcomer | None:
    if raw is None:
        errors.append(ConfigError("newcomer", "required"))
        return None
    if not isinstance(raw, dict):
        errors.append(ConfigError("newcomer", "expected_object"))
        return None
    _unknown(raw, _NEWCOMER_KEYS, "newcomer", errors)
    hours = _int(raw.get("hours"), "newcomer.hours", errors, minimum=0)
    factor = _factor(raw.get("factor"), "newcomer.factor", errors)
    if hours is None or factor is None:
        return None
    return Newcomer(hours=hours, factor=factor)


def _rules(raw: object, errors: list[ConfigError]) -> dict[str, Rule]:
    if raw is None:
        errors.append(ConfigError("rules", "required"))
        return {}
    if not isinstance(raw, dict):
        errors.append(ConfigError("rules", "expected_object"))
        return {}
    found: dict[str, Rule] = {}
    for key, value in raw.items():
        name = str(key)
        path = f"rules.{name}"
        if name not in _RULE_SET:
            errors.append(ConfigError(path, "unknown_rule"))
            continue
        rule = _rule(name, value, path, errors)
        if rule is not None:
            found[name] = rule
    return found


def _rule(name: str, raw: object, path: str, errors: list[ConfigError]) -> Rule | None:
    if not isinstance(raw, dict):
        errors.append(ConfigError(path, "expected_object"))
        return None
    _unknown(raw, _RULE_KEYS, path, errors)
    limit = _int(raw.get("limit"), f"{path}.limit", errors, minimum=1)
    action = _enum(raw.get("action"), f"{path}.action", errors, ACTIONS)
    delete = _enum(raw.get("delete"), f"{path}.delete", errors, DELETE_MODES)
    own_timeout = raw.get("timeout_minutes")
    timeout: int | None = None
    if own_timeout is not None:
        timeout = _int(own_timeout, f"{path}.timeout_minutes", errors, minimum=1)
    exempt: Exempt | None = None
    if "exempt" in raw:
        exempt = _exempt(raw.get("exempt"), errors, path=f"{path}.exempt")
        if exempt is None:
            return None
    elif name in _STRICT_RULES:
        # Старые automod.yml без своего exempt не должны откатывать правило к мягкому глобальному.
        exempt = _exempt(dict(_STRICT_EXEMPT), errors, path=f"{path}.exempt")
    if limit is None or action is None or delete is None:
        return None
    if own_timeout is not None and timeout is None:
        return None
    return Rule(
        name=name,
        limit=limit,
        action=AutomodAction(action),
        delete=DeleteMode(delete),
        timeout_minutes=timeout,
        exempt=exempt,
    )


def _unknown(mapping: Mapping[object, object], allowed: frozenset[str], prefix: str, errors: list[ConfigError]) -> None:
    for key in mapping:
        name = str(key)
        if name in allowed:
            continue
        path = f"{prefix}.{name}" if prefix else name
        errors.append(ConfigError(path, "unknown_field"))


def _int(raw: object, path: str, errors: list[ConfigError], *, minimum: int) -> int | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        errors.append(ConfigError(path, "expected_int"))
        return None
    if raw < minimum:
        errors.append(ConfigError(path, f"min:{minimum}"))
        return None
    return raw


def _bool(raw: object, path: str, errors: list[ConfigError]) -> bool | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if not isinstance(raw, bool):
        errors.append(ConfigError(path, "expected_bool"))
        return None
    return raw


def _factor(raw: object, path: str, errors: list[ConfigError]) -> float | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        errors.append(ConfigError(path, "expected_number"))
        return None
    value = float(raw)
    if value <= 0:
        errors.append(ConfigError(path, "must_be_positive"))
        return None
    return value


def _enum(raw: object, path: str, errors: list[ConfigError], allowed: frozenset[str]) -> str | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if not isinstance(raw, str):
        errors.append(ConfigError(path, "expected_string"))
        return None
    if raw not in allowed:
        errors.append(ConfigError(path, "invalid_value"))
        return None
    return raw


def _ranks(raw: object, path: str, errors: list[ConfigError]) -> list[str] | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if not isinstance(raw, list):
        errors.append(ConfigError(path, "expected_list"))
        return None
    found: list[str] = []
    ok = True
    for index, item in enumerate(raw):
        token = str(item)
        item_path = f"{path}[{index}]"
        try:
            rank = parse_rank_token(token)
        except ValueError:
            errors.append(ConfigError(item_path, "unknown_rank"))
            ok = False
            continue
        if rank is Rank.NONE:
            errors.append(ConfigError(item_path, "rank_none_forbidden"))
            ok = False
            continue
        if rank.key not in found:
            found.append(rank.key)
    return found if ok else None


def _id_list(raw: object, path: str, errors: list[ConfigError]) -> list[int] | None:
    if raw is None:
        errors.append(ConfigError(path, "required"))
        return None
    if not isinstance(raw, list):
        errors.append(ConfigError(path, "expected_list"))
        return None
    found: list[int] = []
    ok = True
    for index, item in enumerate(raw):
        value = _snowflake(item, f"{path}[{index}]", errors)
        if value is None:
            ok = False
            continue
        if value not in found:
            found.append(value)
    return found if ok else None


def _snowflake(raw: object, path: str, errors: list[ConfigError]) -> int | None:
    if isinstance(raw, bool):
        errors.append(ConfigError(path, "expected_id"))
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        errors.append(ConfigError(path, "expected_id"))
        return None
    if value < 0:
        errors.append(ConfigError(path, "negative_id"))
        return None
    return value


def _gif_url(url: str) -> bool:
    lowered = url.lower()
    path = lowered.split("?", 1)[0]
    if path.endswith(".gif") or ".gif" in path:
        return True
    if any(host in lowered for host in _GIF_HOSTS):
        return True
    return "media.discordapp.net" in lowered and "gif" in lowered


def _count_unicode_emoji(text: str) -> int:
    codes = [ord(ch) for ch in text]
    i = 0
    total = 0
    length = len(codes)
    while i < length:
        cp = codes[i]
        if cp in _REGIONAL:
            total += 1
            i += 1
            if i < length and codes[i] in _REGIONAL:
                i += 1
            continue
        if _is_keycap(codes, i) or _is_emoji_base(cp):
            total += 1
            i = _skip_sequence(codes, i)
            continue
        i += 1
    return total


def _is_emoji_base(cp: int) -> bool:
    return (
        0x1F300 <= cp <= 0x1FAFF
        or cp in {0x1F004, 0x1F0CF, 0x3030, 0x303D, 0x3297, 0x3299}
        or 0x2300 <= cp <= 0x23FF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B50 <= cp <= 0x2B55
        or 0x2B1B <= cp <= 0x2B1C
    )


def _is_keycap(codes: Sequence[int], index: int) -> bool:
    if chr(codes[index]) not in _KEYCAP_BASE:
        return False
    nxt = index + 1
    if nxt < len(codes) and codes[nxt] == _VS16:
        nxt += 1
    return nxt < len(codes) and codes[nxt] == _KEYCAP


def _skip_sequence(codes: Sequence[int], index: int) -> int:
    i = index + 1
    length = len(codes)
    while i < length:
        cp = codes[i]
        if cp in {_VS16, _KEYCAP} or cp in _SKIN:
            i += 1
            continue
        if cp != _ZWJ:
            break
        i += 1
        if i >= length:
            break
        i += 1
        while i < length and (codes[i] in {_VS16, _KEYCAP} or codes[i] in _SKIN):
            i += 1
    return i


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _rank_enum(rank: str) -> Rank:
    try:
        return parse_rank_token(rank)
    except ValueError:
        return Rank.NONE
