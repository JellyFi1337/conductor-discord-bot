"""Ранги и ключи прав. Чистые функции: без Discord и без БД."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum

EVERYONE = "все"


class Rank(IntEnum):
    NONE = 0
    WATCHER = 1
    SENIOR = 2
    CHIEF = 3
    SPECIAL = 4

    @property
    def key(self) -> str:
        return self.name.lower()


STAFF_RANKS: tuple[Rank, ...] = (Rank.WATCHER, Rank.SENIOR, Rank.CHIEF, Rank.SPECIAL)
BINDABLE_RANKS: tuple[Rank, ...] = STAFF_RANKS


def parse_rank_token(token: str) -> Rank:
    name = token.strip().lower()
    if name == Rank.NONE.key:
        return Rank.NONE
    for rank in STAFF_RANKS:
        if name == rank.key:
            return rank
    raise ValueError(token)


def parse_rank_list(text: str | Sequence[str]) -> frozenset[Rank]:
    parts: list[str]
    if isinstance(text, str):
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty")
        parts = [piece.strip() for piece in stripped.replace(";", ",").split(",")]
    else:
        parts = [str(piece).strip() for piece in text]
    tokens = [part for part in parts if part]
    if not tokens:
        raise ValueError("empty")
    if any(token.lower() in {EVERYONE, "all", "*"} for token in tokens):
        return frozenset(Rank)
    found = {parse_rank_token(token) for token in tokens}
    if Rank.NONE in found:
        raise ValueError("none")
    return frozenset(found)


def ranks_for_storage(ranks: Collection[Rank]) -> tuple[str, ...]:
    if _is_everyone(ranks):
        return (EVERYONE,)
    return tuple(rank.key for rank in STAFF_RANKS if rank in ranks)


def _is_everyone(ranks: Collection[Rank]) -> bool:
    return Rank.NONE in ranks or set(ranks) >= set(Rank)


def _defaults_from_cell(cell: str) -> frozenset[Rank]:
    return parse_rank_list(cell)


class KeyTier(StrEnum):
    READ = "read"
    OPERATE = "operate"
    GOVERN = "govern"


@dataclass(frozen=True, slots=True)
class KeySpec:
    key: str
    tier: KeyTier
    defaults: frozenset[Rank]
    label: str
    domain: str


def _spec(key: str, tier: KeyTier, defaults: str, label: str, domain: str) -> KeySpec:
    return KeySpec(key, tier, _defaults_from_cell(defaults), label, domain)


KEY_SPECS: tuple[KeySpec, ...] = (
    _spec("config.manage", KeyTier.GOVERN, "special", "Права и каркас", "config"),
    _spec("diagnose.run", KeyTier.GOVERN, "special, chief", "Диагностика", "diagnose"),
    _spec("help.read", KeyTier.READ, EVERYONE, "Справка", "help"),
    _spec("stats.read", KeyTier.READ, "senior, chief, special", "Чтение статистики", "stats"),
    _spec("stats.ingest", KeyTier.OPERATE, "chief, special", "Пересчёт статистики", "stats"),
    _spec("loyalty.manage", KeyTier.GOVERN, "special", "Реестр Old", "loyalty"),
    _spec("watchers.read", KeyTier.READ, "watcher, senior, chief, special", "Состав следящих", "watchers"),
    _spec("watchers.appoint", KeyTier.GOVERN, "chief, special", "Назначение следящих", "watchers"),
    _spec("watchers.dismiss", KeyTier.GOVERN, "chief, special", "Снятие следящих", "watchers"),
    _spec("watchers.duty_other", KeyTier.OPERATE, "chief, special", "Чужое дежурство", "watchers"),
    _spec("watchers.note", KeyTier.OPERATE, "senior, chief, special", "Заметка следящему", "watchers"),
    _spec("voice.hub_manage", KeyTier.OPERATE, "chief, special", "Голосовой хаб", "voice"),
    _spec("voice.staff_override", KeyTier.OPERATE, "senior, chief, special", "Вход в чужую комнату", "voice"),
    _spec("voice.preset", KeyTier.OPERATE, "chief, special", "Пресет комнат", "voice"),
    _spec("tickets.panel_post", KeyTier.OPERATE, "chief, special", "Панель тикетов", "tickets"),
    _spec("tickets.claim", KeyTier.OPERATE, "senior, chief, special", "Взять тикет", "tickets"),
    _spec("tickets.close_own", KeyTier.OPERATE, "senior, chief, special", "Закрыть свой тикет", "tickets"),
    _spec("tickets.close_any", KeyTier.OPERATE, "senior, chief, special", "Закрыть любой тикет", "tickets"),
    _spec("tickets.form_manage", KeyTier.OPERATE, "chief, special", "Формы тикетов", "tickets"),
    _spec("tickets.priority", KeyTier.OPERATE, "senior, chief, special", "Приоритет тикета", "tickets"),
    _spec("tickets.notes", KeyTier.OPERATE, "senior, chief, special", "Заметки тикета", "tickets"),
    _spec("tickets.reopen", KeyTier.OPERATE, "senior, chief, special", "Открыть тикет снова", "tickets"),
    _spec("tickets.approve", KeyTier.GOVERN, "special", "Утверждение тикета", "tickets"),
    _spec("mod.warn", KeyTier.OPERATE, "watcher, senior, chief, special", "Замечание", "mod"),
    _spec("mod.timeout", KeyTier.OPERATE, "watcher, senior, chief, special", "Тайм-аут", "mod"),
    _spec("mod.mute", KeyTier.OPERATE, "watcher, senior, chief, special", "Мут", "mod"),
    _spec("mod.unmute", KeyTier.OPERATE, "watcher, senior, chief, special", "Снять мут", "mod"),
    _spec("mod.note", KeyTier.OPERATE, "watcher, senior, chief, special", "Заметка модерации", "mod"),
    _spec("mod.history", KeyTier.OPERATE, "watcher, senior, chief, special", "История дел", "mod"),
    _spec("mod.watch", KeyTier.OPERATE, "watcher, senior, chief, special", "Надзор", "mod"),
    _spec("mod.kick", KeyTier.OPERATE, "chief, special", "Кик", "mod"),
    _spec("mod.ban", KeyTier.OPERATE, "chief, special", "Бан", "mod"),
    _spec("mod.unban", KeyTier.OPERATE, "chief, special", "Разбан", "mod"),
    _spec("mod.purge", KeyTier.OPERATE, "watcher, senior, chief, special", "Очистка сообщений", "mod"),
    _spec("mod.channel_lock", KeyTier.OPERATE, "senior, chief, special", "Замок канала", "mod"),
    _spec("mod.evidence_close", KeyTier.OPERATE, "chief, special", "Закрыть улики", "mod"),
    _spec("rep.show_other", KeyTier.READ, "watcher, senior, chief, special", "Чужая репутация", "rep"),
    _spec("rep.config", KeyTier.GOVERN, "special", "Настройка репутации", "rep"),
    _spec("rep.pardon", KeyTier.GOVERN, "special", "Помилование", "rep"),
    _spec("activity.config", KeyTier.OPERATE, "chief, special", "Лестница активности", "activity"),
    _spec("achievements.manage", KeyTier.GOVERN, "special", "Достижения", "achievements"),
    _spec("recruit.manage", KeyTier.OPERATE, "chief, special", "Рекрутинг", "recruit"),
    _spec("recruit.read", KeyTier.READ, "senior, chief, special", "Чтение рекрутинга", "recruit"),
    _spec("giveaway.create", KeyTier.OPERATE, "senior, chief, special", "Создать розыгрыш", "giveaway"),
    _spec("giveaway.manage", KeyTier.OPERATE, "senior, chief, special", "Вести розыгрыш", "giveaway"),
    _spec("poll.create", KeyTier.OPERATE, "watcher, senior, chief, special", "Создать опрос", "poll"),
    _spec("poll.manage", KeyTier.OPERATE, "senior, chief, special", "Вести опрос", "poll"),
    _spec("poll.deanonymize", KeyTier.GOVERN, "special", "Снять анонимность опроса", "poll"),
    _spec("event.create", KeyTier.OPERATE, "watcher, senior, chief, special", "Создать сбор", "event"),
    _spec("event.manage", KeyTier.OPERATE, "senior, chief, special", "Вести сбор", "event"),
    _spec("group.create", KeyTier.OPERATE, "senior, chief, special", "Создать группу", "group"),
    _spec("group.archive.read", KeyTier.READ, "chief, special", "Читать архив групп", "group"),
    _spec("group.archive.manage", KeyTier.GOVERN, "special", "Вернуть и удалить архив групп", "group"),
    _spec("remind.use", KeyTier.READ, EVERYONE, "Напоминания", "remind"),
    _spec("bug.triage", KeyTier.OPERATE, "senior, chief, special", "Разбор баг-репортов", "bug"),
    _spec("data.manage", KeyTier.GOVERN, "special", "Выгрузка данных", "data"),
    _spec("data.forget", KeyTier.GOVERN, "special", "Забыть человека", "data"),
    _spec("pulse.staff", KeyTier.READ, "watcher, senior, chief, special", "Пульс следящих", "pulse"),
    _spec("pulse.alerts", KeyTier.OPERATE, "chief, special", "Сигналы пульса", "pulse"),
    _spec("automod.request", KeyTier.OPERATE, "chief, special", "Заявки автомода", "automod"),
    _spec("mod.revoke", KeyTier.OPERATE, "chief, special", "Отмена дела", "mod"),
    _spec("watchers.suspend", KeyTier.GOVERN, "chief, special", "Приостановка следящего", "watchers"),
    _spec("config.read", KeyTier.READ, "senior, chief, special", "Просмотр настроек", "config"),
    _spec("mod.settings", KeyTier.OPERATE, "chief, special", "Настройки модерации", "mod"),
    _spec("content.manage", KeyTier.OPERATE, "chief, special", "Контент сервера", "content"),
    _spec("showcase.post", KeyTier.OPERATE, "chief, special", "Витрины сервера", "showcase"),
    _spec("skeleton.read", KeyTier.READ, "senior, chief, special", "Просмотр каркаса", "skeleton"),
    _spec("skeleton.manage", KeyTier.OPERATE, "chief, special", "Починка каркаса", "skeleton"),
    _spec("skeleton.build", KeyTier.OPERATE, "chief, special", "Сборка сервера", "skeleton"),
    _spec("skeleton.bind", KeyTier.GOVERN, "special", "Привязка каркаса", "skeleton"),
    _spec("admin.panel", KeyTier.READ, "senior, chief, special", "Панель /admin", "admin"),
)


ACTION_DEFAULTS: dict[str, frozenset[Rank]] = {item.key: item.defaults for item in KEY_SPECS}
ACTION_KEYS: tuple[str, ...] = tuple(ACTION_DEFAULTS)
CONFIG_MANAGE = "config.manage"
STATS_READ = "stats.read"
STATS_INGEST = "stats.ingest"
LOYALTY_MANAGE = "loyalty.manage"
DIAGNOSE_RUN = "diagnose.run"
HELP_READ = "help.read"
WATCHERS_READ = "watchers.read"
WATCHERS_APPOINT = "watchers.appoint"
WATCHERS_DISMISS = "watchers.dismiss"
WATCHERS_DUTY_OTHER = "watchers.duty_other"
WATCHERS_NOTE = "watchers.note"
VOICE_HUB_MANAGE = "voice.hub_manage"
VOICE_STAFF_OVERRIDE = "voice.staff_override"
VOICE_PRESET = "voice.preset"
TICKETS_PANEL_POST = "tickets.panel_post"
TICKETS_CLAIM = "tickets.claim"
TICKETS_CLOSE_OWN = "tickets.close_own"
TICKETS_CLOSE_ANY = "tickets.close_any"
TICKETS_FORM_MANAGE = "tickets.form_manage"
TICKETS_PRIORITY = "tickets.priority"
TICKETS_NOTES = "tickets.notes"
TICKETS_REOPEN = "tickets.reopen"
TICKETS_APPROVE = "tickets.approve"
MOD_WARN = "mod.warn"
MOD_TIMEOUT = "mod.timeout"
MOD_MUTE = "mod.mute"
MOD_UNMUTE = "mod.unmute"
MOD_NOTE = "mod.note"
MOD_HISTORY = "mod.history"
MOD_WATCH = "mod.watch"
MOD_KICK = "mod.kick"
MOD_BAN = "mod.ban"
MOD_UNBAN = "mod.unban"
MOD_PURGE = "mod.purge"
MOD_CHANNEL_LOCK = "mod.channel_lock"
MOD_EVIDENCE_CLOSE = "mod.evidence_close"
REP_SHOW_OTHER = "rep.show_other"
REP_CONFIG = "rep.config"
REP_PARDON = "rep.pardon"
ACTIVITY_CONFIG = "activity.config"
ACHIEVEMENTS_MANAGE = "achievements.manage"
RECRUIT_MANAGE = "recruit.manage"
RECRUIT_READ = "recruit.read"
GIVEAWAY_CREATE = "giveaway.create"
GIVEAWAY_MANAGE = "giveaway.manage"
POLL_CREATE = "poll.create"
POLL_MANAGE = "poll.manage"
POLL_DEANONYMIZE = "poll.deanonymize"
EVENT_CREATE = "event.create"
EVENT_MANAGE = "event.manage"
GROUP_CREATE = "group.create"
GROUP_ARCHIVE_READ = "group.archive.read"
GROUP_ARCHIVE_MANAGE = "group.archive.manage"
REMIND_USE = "remind.use"
BUG_TRIAGE = "bug.triage"
DATA_MANAGE = "data.manage"
DATA_FORGET = "data.forget"
PULSE_STAFF = "pulse.staff"
PULSE_ALERTS = "pulse.alerts"
AUTOMOD_REQUEST = "automod.request"
MOD_REVOKE = "mod.revoke"
WATCHERS_SUSPEND = "watchers.suspend"
CONFIG_READ = "config.read"
MOD_SETTINGS = "mod.settings"
CONTENT_MANAGE = "content.manage"
SHOWCASE_POST = "showcase.post"
SKELETON_READ = "skeleton.read"
SKELETON_MANAGE = "skeleton.manage"
SKELETON_BUILD = "skeleton.build"
SKELETON_BIND = "skeleton.bind"
ADMIN_PANEL_OPEN = "admin.panel"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    key: str
    ranks: frozenset[Rank]
    extra_role_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class AccessSnapshot:
    rank_roles: Mapping[Rank, tuple[int, ...]]
    rules: Mapping[str, PermissionRule]
    special_locked: bool
    special_ever_bound: bool = False

    def rule(self, key: str) -> PermissionRule:
        found = self.rules.get(key)
        if found is not None:
            return found
        allowed = ACTION_DEFAULTS.get(key)
        if allowed is None:
            return PermissionRule(key, frozenset(), frozenset())
        return PermissionRule(key, allowed, frozenset())


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    rank: Rank
    fallback_admin: bool = False
    need: Rank | None = None
    developer: bool = False
    deny_copy: str | None = None
    suspended: bool = False


def resolve_rank(member_role_ids: Iterable[int], ranks: Mapping[Rank, Iterable[int]]) -> Rank:
    have = set(member_role_ids)
    found = Rank.NONE
    for rank, role_ids in ranks.items():
        if rank is Rank.NONE:
            continue
        if have.intersection(role_ids) and rank > found:
            found = rank
    return found


def can(
    rank: Rank,
    *,
    key: str | None = None,
    allowed: Collection[Rank] | None = None,
    extra_role_ids: Collection[int] = (),
    member_role_ids: Collection[int] = (),
) -> bool:
    granted = allowed if allowed is not None else ACTION_DEFAULTS[key or ""]
    if _is_everyone(granted):
        return True
    if extra_role_ids and set(member_role_ids) & set(extra_role_ids):
        return True
    return rank in granted and rank is not Rank.NONE


def can_punish(actor: Rank, target: Rank) -> bool:
    return actor > target


class StaffTargetBlock(StrEnum):
    BOT = "bot"
    HIERARCHY = "hierarchy"


def staff_target_block(
    *,
    actor: Rank,
    target_rank: Rank,
    target_is_bot: bool,
) -> StaffTargetBlock | None:
    """Отказ цели кадрового или модерационного действия. Смотреть карточку можно и без этого."""
    if target_is_bot:
        return StaffTargetBlock.BOT
    if not can_punish(actor, target_rank):
        return StaffTargetBlock.HIERARCHY
    return None


def granting_role_ids(
    member_role_ids: Iterable[int],
    rank: Rank,
    rank_roles: Mapping[Rank, Iterable[int]],
) -> tuple[int, ...]:
    if rank is Rank.NONE:
        return ()
    have = set(member_role_ids)
    return tuple(role_id for role_id in rank_roles.get(rank, ()) if role_id in have)


@dataclass(frozen=True, slots=True)
class StaffingPolicy:
    """Кадровые исключения. Оба флага выключены, пока их не включат явно."""

    senior_appoint: bool = False
    special_appoint: bool = False


class StaffingBlock(StrEnum):
    BOT = "bot"
    HIERARCHY = "hierarchy"
    SPECIAL = "special"
    SENIOR = "senior"
    NO_RANK = "no_rank"
    UNBOUND = "unbound"


def appoint_permitted(decision: AccessDecision, actor: Rank, policy: StaffingPolicy) -> bool:
    """Ключ `watchers.appoint` или настройка «старший назначает следящего»."""
    if decision.allowed:
        return True
    return actor is Rank.SENIOR and policy.senior_appoint


def can_appoint(
    actor: Rank,
    *,
    desired: Rank,
    target: Rank,
    target_is_bot: bool,
    policy: StaffingPolicy,
) -> StaffingBlock | None:
    if target_is_bot:
        return StaffingBlock.BOT
    if desired not in STAFF_RANKS:
        return StaffingBlock.HIERARCHY
    if actor is Rank.NONE or target >= actor:
        return StaffingBlock.HIERARCHY
    if desired is Rank.SPECIAL:
        if not policy.special_appoint:
            return StaffingBlock.SPECIAL
        if actor is not Rank.SPECIAL:
            return StaffingBlock.HIERARCHY
        return None
    if desired >= actor:
        return StaffingBlock.HIERARCHY
    if actor is Rank.SENIOR:
        if desired is not Rank.WATCHER:
            return StaffingBlock.HIERARCHY
        if not policy.senior_appoint:
            return StaffingBlock.SENIOR
        return None
    if actor is Rank.CHIEF and desired not in {Rank.WATCHER, Rank.SENIOR}:
        return StaffingBlock.HIERARCHY
    if actor is Rank.WATCHER:
        return StaffingBlock.HIERARCHY
    return None


def can_dismiss(
    actor: Rank,
    *,
    target: Rank,
    target_is_bot: bool,
) -> StaffingBlock | None:
    if target_is_bot:
        return StaffingBlock.BOT
    if target is Rank.NONE:
        return StaffingBlock.NO_RANK
    if not can_punish(actor, target):
        return StaffingBlock.HIERARCHY
    return None


def staff_role_ids(rank_roles: Mapping[Rank, Iterable[int]]) -> tuple[int, ...]:
    found: list[int] = []
    seen: set[int] = set()
    for rank in STAFF_RANKS:
        for role_id in rank_roles.get(rank, ()):
            value = int(role_id)
            if value in seen:
                continue
            seen.add(value)
            found.append(value)
    return tuple(found)


def rank_holding_role(
    rank_roles: Mapping[Rank, Iterable[int]],
    role_id: int,
    *,
    except_rank: Rank | None = None,
) -> Rank | None:
    """Какой ранг уже держит этот Discord id. except_rank — при перезаписи своей привязки."""
    wanted = int(role_id)
    for rank in STAFF_RANKS:
        if rank is except_rank:
            continue
        if wanted in {int(value) for value in rank_roles.get(rank, ())}:
            return rank
    return None


def appoint_role_diff(
    desired: Rank,
    rank_roles: Mapping[Rank, Iterable[int]],
    member_role_ids: Iterable[int],
) -> tuple[tuple[int, ...], tuple[int, ...]] | StaffingBlock:
    """Одна ранговая роль, все прочие роли контура снимаются."""
    desired_ids = tuple(int(role_id) for role_id in rank_roles.get(desired, ()))
    if not desired_ids:
        return StaffingBlock.UNBOUND
    have = {int(role_id) for role_id in member_role_ids}
    desired_set = set(desired_ids)
    add = tuple(role_id for role_id in desired_ids if role_id not in have)
    remove = tuple(role_id for role_id in staff_role_ids(rank_roles) if role_id in have and role_id not in desired_set)
    return add, remove


def dismiss_role_diff(
    rank_roles: Mapping[Rank, Iterable[int]],
    member_role_ids: Iterable[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    have = {int(role_id) for role_id in member_role_ids}
    remove = tuple(role_id for role_id in staff_role_ids(rank_roles) if role_id in have)
    return (), remove


def needed_rank(allowed: Collection[Rank]) -> Rank | None:
    if _is_everyone(allowed):
        return None
    staff = [rank for rank in STAFF_RANKS if rank in allowed]
    return staff[0] if staff else Rank.SPECIAL


def _normalized_ranks(ranks: Collection[Rank]) -> frozenset[Rank]:
    if _is_everyone(ranks):
        return frozenset(Rank)
    return frozenset(ranks)


def scheme_drift_keys(snapshot: AccessSnapshot) -> tuple[str, ...]:
    """Ключи, чьи ранги в снимке не совпадают с текущей схемой умолчаний."""
    return tuple(
        key
        for key, default in ACTION_DEFAULTS.items()
        if _normalized_ranks(snapshot.rule(key).ranks) != _normalized_ranks(default)
    )


def bypasses_limits(*, is_administrator: bool, is_guild_owner: bool) -> bool:
    """Единственное место, где галка Administrator что-то значит: обход лимитов, не команд."""
    return bool(is_administrator or is_guild_owner)


def is_special_or_admin(*, rank: Rank, is_administrator: bool, is_guild_owner: bool) -> bool:
    """Специальный следящий, право «Администратор» или владелец сервера."""
    return rank is Rank.SPECIAL or bypasses_limits(
        is_administrator=is_administrator, is_guild_owner=is_guild_owner
    )


def special_locked_now(
    rank_roles: Mapping[Rank, Iterable[int]],
    *,
    ever_bound: bool,
) -> bool:
    _ = ever_bound
    return bool(tuple(rank_roles.get(Rank.SPECIAL, ())))


class LockoutRisk(StrEnum):
    NONE = "none"
    NO_HOLDER = "no_holder"
    SELF_LOCKOUT = "self_lockout"
    GOVERN_KEY = "govern_key"


GOVERN_KEYS: frozenset[str] = frozenset({CONFIG_MANAGE, DIAGNOSE_RUN})


class LockoutBlocked(Exception):
    def __init__(self, risk: LockoutRisk, key: str) -> None:
        self.risk = risk
        self.key = key
        super().__init__(risk.value, key)


def lockout_risk(
    *,
    key: str,
    wanted: Collection[Rank],
    actor: Rank,
    rank_roles: Mapping[Rank, Iterable[int]],
) -> LockoutRisk:
    if key not in GOVERN_KEYS:
        return LockoutRisk.NONE
    holders = [
        rank
        for rank in wanted
        if rank is not Rank.NONE and tuple(rank_roles.get(rank, ()))
    ]
    if not holders:
        return LockoutRisk.NO_HOLDER
    if actor not in wanted:
        return LockoutRisk.SELF_LOCKOUT
    return LockoutRisk.GOVERN_KEY


def authorize(
    *,
    rank: Rank,
    key: str,
    allowed: Collection[Rank],
    extra_role_ids: Collection[int] = (),
    member_role_ids: Collection[int] = (),
    is_administrator: bool,
    special_locked: bool,
    is_developer: bool = False,
    suspended: bool = False,
) -> AccessDecision:
    if is_developer:
        return AccessDecision(True, Rank.SPECIAL, developer=True)
    if suspended and key != HELP_READ:
        return AccessDecision(False, Rank.NONE, suspended=True)
    granted = can(
        rank,
        key=key,
        allowed=allowed,
        extra_role_ids=extra_role_ids,
        member_role_ids=member_role_ids,
    )
    admin_fallback = not special_locked and is_administrator
    if granted:
        return AccessDecision(True, rank)
    if admin_fallback:
        return AccessDecision(True, rank, fallback_admin=True)
    return AccessDecision(False, rank, need=needed_rank(allowed))


def decide(
    snapshot: AccessSnapshot,
    *,
    key: str,
    member_role_ids: Iterable[int],
    is_administrator: bool,
    is_developer: bool = False,
    suspended: bool = False,
) -> AccessDecision:
    role_ids = tuple(member_role_ids)
    rank = resolve_rank(role_ids, snapshot.rank_roles)
    rule = snapshot.rule(key)
    return authorize(
        rank=rank,
        key=key,
        allowed=rule.ranks,
        extra_role_ids=rule.extra_role_ids,
        member_role_ids=role_ids,
        is_administrator=is_administrator,
        special_locked=snapshot.special_locked,
        is_developer=is_developer,
        suspended=suspended,
    )


def empty_rank_roles() -> dict[Rank, tuple[int, ...]]:
    return {rank: () for rank in STAFF_RANKS}
