"""Реестр полей файла настройки и чистый разбор плана. Без Discord и без БД."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from conductor.domain.access import (
    ACTION_KEYS,
    BINDABLE_RANKS,
    CONFIG_MANAGE,
    LockoutRisk,
    Rank,
    lockout_risk,
    parse_rank_list,
    rank_holding_role,
)
from conductor.domain.admin import COMMON_TIMEZONES, parse_duration, parse_timezone
from conductor.domain.ladder import STEPS
from conductor.domain.loyalty import TIER_LABELS, LoyaltyTier
from conductor.domain.reputation import TIER_COUNT
from conductor.domain.rooms import CHANNEL_NAME_LIMIT, HubPreset
from conductor.domain.seed import LAYOUT, SKELETON, SkeletonKind
from conductor.domain.self_roles import LABEL_MAX, sanitize_label
from conductor.domain.showcase import SHOWCASES
from conductor.domain.tickets import DEFAULT_CATEGORY_SPECS, TicketOpenMode

__all__ = [
    "BLUEPRINT",
    "BLUEPRINT_MAX_BYTES",
    "BLUEPRINT_VERSION",
    "COLOR_KEY",
    "EXPORT_ROOTS",
    "HELP_EXPORTED_KEY",
    "HELP_GUILD_KEY",
    "HELP_KEY",
    "HELP_RULES_KEY",
    "HELP_VERSION_KEY",
    "HELP_WHAT_KEY",
    "NAME_KEY",
    "ROOT_FEATURES",
    "ROOT_GENERAL",
    "ROOT_RIGHTS",
    "ROOT_ROLES",
    "ROOT_SHOWCASES",
    "ROOT_SKELETON",
    "BlueprintChange",
    "BlueprintIssue",
    "BlueprintPlan",
    "BlueprintReport",
    "FieldKind",
    "FieldSpec",
    "IssueLevel",
    "attachment_error",
    "plan_blueprint",
    "parse_blueprint",
    "put_path",
    "render_docs",
    "skeleton_fields",
    "spec_by_path",
    "strip_docs",
    "wanted_from_bytes",
]

BLUEPRINT_VERSION = 1
BLUEPRINT_MAX_BYTES = 1_048_576
HELP_KEY = "_справка"
HELP_WHAT_KEY = "что это"
HELP_RULES_KEY = "правила"
HELP_VERSION_KEY = "версия_формата"
HELP_EXPORTED_KEY = "выгружено"
HELP_GUILD_KEY = "сервер"
NAME_KEY = "имя"
COLOR_KEY = "цвет"
ROOT_SKELETON = "каркас"
ROOT_ROLES = "роли"
ROOT_RIGHTS = "права"
ROOT_FEATURES = "функции"
ROOT_GENERAL = "общее"
ROOT_SHOWCASES = "витрины"
EXPORT_ROOTS = (
    ROOT_SKELETON,
    ROOT_ROLES,
    ROOT_RIGHTS,
    ROOT_FEATURES,
    ROOT_GENERAL,
    ROOT_SHOWCASES,
)
_ALLOWED_TYPES = ("json", "text/plain", "octet-stream")
_SNOWFLAKE_MAX = (1 << 63) - 1
_BINDING_KEYS = frozenset({"id", NAME_KEY, COLOR_KEY})
_RANK_CHOICES = tuple(rank.key for rank in BINDABLE_RANKS) + ("все",)


class FieldKind(StrEnum):
    CHANNEL = "channel"
    CATEGORY = "category"
    VOICE = "voice"
    ROLE = "role"
    INT = "int"
    BOOL = "bool"
    TEXT = "text"
    DURATION = "duration"
    ENUM = "enum"
    ID_LIST = "id_list"
    ROLE_LIST = "role_list"
    BINDING = "binding"
    RANK_LIST = "rank_list"


class IssueLevel(StrEnum):
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    path: str
    kind: FieldKind
    label: str
    table: str
    column: str
    skeleton_key: str = ""
    choices: tuple[str, ...] = ()
    lo: int | None = None
    hi: int | None = None
    required: bool = False
    page: str = ""


@dataclass(frozen=True, slots=True)
class BlueprintIssue:
    level: IssueLevel
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class BlueprintChange:
    path: str
    before: str
    after: str
    kind: FieldKind


@dataclass(frozen=True, slots=True)
class BlueprintPlan:
    changes: tuple[BlueprintChange, ...]
    issues: tuple[BlueprintIssue, ...]
    unknown: tuple[str, ...]
    creates: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return any(item.level is IssueLevel.ERROR for item in self.issues)


@dataclass(frozen=True, slots=True)
class BlueprintReport:
    applied: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _f(
    path: str,
    kind: FieldKind,
    label: str,
    table: str,
    column: str,
    *,
    skeleton_key: str = "",
    choices: tuple[str, ...] = (),
    lo: int | None = None,
    hi: int | None = None,
    required: bool = False,
    page: str = "",
) -> FieldSpec:
    return FieldSpec(
        path,
        kind,
        label,
        table,
        column,
        skeleton_key=skeleton_key,
        choices=choices,
        lo=lo,
        hi=hi,
        required=required,
        page=page,
    )


def _binding_parts(binding: str | None) -> tuple[str, str]:
    if not binding:
        return "", ""
    if ":" in binding:
        table, column = binding.split(":", 1)
        return table, column
    table, _, column = binding.partition(".")
    return table, column


def _entry_kind(kind: SkeletonKind) -> FieldKind:
    if kind is SkeletonKind.ROLE:
        return FieldKind.ROLE
    if kind is SkeletonKind.CATEGORY:
        return FieldKind.CATEGORY
    if kind is SkeletonKind.VOICE:
        return FieldKind.VOICE
    return FieldKind.CHANNEL


def skeleton_fields() -> tuple[FieldSpec, ...]:
    found: list[FieldSpec] = []
    for entry in SKELETON:
        section = "роли" if entry.kind is SkeletonKind.ROLE else "каркас"
        table, column = _binding_parts(entry.binding)
        found.append(
            _f(
                f"{section}.{entry.key}",
                FieldKind.BINDING,
                entry.diagnose_label,
                table,
                column,
                skeleton_key=entry.key,
                required=entry.required,
                page="skeleton",
            )
        )
    return tuple(found)


def _feature_fields() -> tuple[FieldSpec, ...]:
    ranks = tuple(
        _f(
            f"права.ранги.{rank.key}",
            FieldKind.ROLE_LIST,
            LAYOUT.role_watcher
            if rank is Rank.WATCHER
            else LAYOUT.role_senior
            if rank is Rank.SENIOR
            else LAYOUT.role_chief
            if rank is Rank.CHIEF
            else LAYOUT.role_special,
            "guild_ranks",
            rank.key,
            page="ranks",
        )
        for rank in BINDABLE_RANKS
    )
    keys = tuple(
        _f(
            f"права.ключи.{key}",
            FieldKind.RANK_LIST,
            key,
            "guild_permissions",
            key,
            choices=_RANK_CHOICES,
            page="perms",
        )
        for key in ACTION_KEYS
    )
    extras = tuple(
        _f(
            f"права.дополнительные_роли.{key}",
            FieldKind.ROLE_LIST,
            key,
            "guild_permissions",
            f"{key}.extra",
            page="perms",
        )
        for key in ACTION_KEYS
    )
    showcases = tuple(
        _f(
            f"витрины.{spec.id}",
            FieldKind.CHANNEL,
            spec.label,
            "panels",
            spec.channel_key,
            skeleton_key=spec.channel_key,
            page="showcase",
        )
        for spec in SHOWCASES
    )
    tickets = tuple(
        _f(
            f"функции.обращения.форма.{spec.name}",
            FieldKind.TEXT,
            spec.name,
            "ticket_categories",
            spec.name,
            page="tickets",
        )
        for spec in DEFAULT_CATEGORY_SPECS
        if spec.name
    )
    old = tuple(
        _f(
            f"функции.old.{tier.value}",
            FieldKind.ROLE,
            TIER_LABELS[tier],
            "guild_configs",
            f"{tier.name.lower()}_role_id",
            page="old",
        )
        for tier in LoyaltyTier
    )
    quotas = tuple(
        _f(
            f"функции.квоты.{rank.key}",
            FieldKind.TEXT,
            LAYOUT.role_watcher
            if rank is Rank.WATCHER
            else LAYOUT.role_senior
            if rank is Rank.SENIOR
            else LAYOUT.role_chief
            if rank is Rank.CHIEF
            else LAYOUT.role_special,
            "mod_settings",
            f"quota.{rank.key}",
            page="quotas",
        )
        for rank in BINDABLE_RANKS
    )
    ladder = tuple(
        _f(
            f"функции.лестница.{step.key}",
            FieldKind.ROLE,
            LAYOUT.role_guest
            if step.key == "guest"
            else LAYOUT.role_regular
            if step.key == "regular"
            else LAYOUT.role_resident
            if step.key == "resident"
            else LAYOUT.role_friend,
            "ladder_roles",
            step.key,
            page="ladder",
        )
        for step in STEPS
    )
    rep_tiers = tuple(
        _f(
            f"функции.репутация.уровень_{index}",
            FieldKind.ROLE,
            f"Уровень {index}",
            "reputation_tiers",
            str(index),
            page="reputation",
        )
        for index in range(TIER_COUNT)
    )
    achievements = tuple(
        _f(
            f"функции.достижения.{index}",
            FieldKind.ROLE,
            name,
            "achievement_defs",
            str(index),
            page="achievements",
        )
        for index, name in enumerate(LAYOUT.role_achievements)
    )
    static = (
        _f(
            "общее.часовой_пояс",
            FieldKind.ENUM,
            "Часовой пояс (дайджест)",
            "guild_configs",
            "timezone",
            choices=COMMON_TIMEZONES,
            page="general",
        ),
        _f(
            "функции.логи.участники",
            FieldKind.CHANNEL,
            "Лог участников",
            "guild_configs",
            "user_log_channel_id",
            page="logs",
        ),
        _f(
            "функции.логи.администрация",
            FieldKind.CHANNEL,
            "Лог администрации",
            "guild_configs",
            "admin_log_channel_id",
            page="logs",
        ),
        _f(
            "функции.логи.действия",
            FieldKind.CHANNEL,
            "Канал действий",
            "guild_configs",
            "staff_feed_channel_id",
            page="logs",
        ),
        _f(
            "функции.логи.хранить",
            FieldKind.INT,
            "Хранить карточки, дн",
            "guild_configs",
            "user_log_keep_days",
            lo=0,
            hi=3650,
            page="logs",
        ),
        _f(
            "функции.кадры.старший",
            FieldKind.BOOL,
            "Старший назначает следящего",
            "staffing_policy",
            "senior_appoint",
            page="staffing",
        ),
        _f(
            "функции.кадры.special",
            FieldKind.BOOL,
            "Special через бота",
            "staffing_policy",
            "special_appoint",
            page="staffing",
        ),
        _f("функции.молчание.роль", FieldKind.ROLE, "Роль мута", "mod_settings", "mute_role_id", page="mute"),
        _f(
            "функции.молчание.категория",
            FieldKind.CATEGORY,
            "Категория доказательств",
            "mod_settings",
            "evidence_category_id",
            page="mute",
        ),
        _f(
            "функции.молчание.архив",
            FieldKind.CHANNEL,
            "Архив дел",
            "mod_settings",
            "evidence_archive_channel_id",
            page="mute",
        ),
        _f(
            "функции.молчание.часы",
            FieldKind.INT,
            "Напоминание, ч",
            "mod_settings",
            "evidence_remind_hours",
            lo=1,
            hi=168,
            page="mute",
        ),
        _f(
            "функции.репутация.замечание",
            FieldKind.INT,
            "Замечание",
            "reputation_config",
            "warn_hit",
            lo=1,
            hi=100,
            page="reputation",
        ),
        _f("функции.репутация.тайм-аут", FieldKind.TEXT, "Тайм-аут", "reputation_config", "timeout", page="reputation"),
        _f(
            "функции.репутация.мут",
            FieldKind.INT,
            "Мут",
            "reputation_config",
            "mute_hit",
            lo=1,
            hi=100,
            page="reputation",
        ),
        _f(
            "функции.репутация.кик",
            FieldKind.INT,
            "Кик",
            "reputation_config",
            "kick_hit",
            lo=1,
            hi=100,
            page="reputation",
        ),
        _f(
            "функции.репутация.потолок",
            FieldKind.INT,
            "Потолок часа",
            "reputation_config",
            "max_hit_per_hour",
            lo=1,
            hi=100,
            page="reputation",
        ),
        _f("функции.репутация.рост", FieldKind.TEXT, "Рост", "reputation_config", "growth", page="reputation"),
        _f(
            "функции.репутация.сухой",
            FieldKind.BOOL,
            "Сухой прогон",
            "reputation_config",
            "dry_run",
            page="reputation",
        ),
        _f(
            "функции.репутация.апелляция",
            FieldKind.TEXT,
            "Апелляция",
            "reputation_config",
            "appeal_contact",
            page="reputation",
        ),
        _f("функции.лестница.постоялец", FieldKind.TEXT, "Постоялец", "ladder_config", "regular", page="ladder"),
        _f("функции.лестница.житель", FieldKind.TEXT, "Житель", "ladder_config", "resident", page="ladder"),
        _f("функции.лестница.друг", FieldKind.TEXT, "Друг", "ladder_config", "friend", page="ladder"),
        _f("функции.достижения.участник", FieldKind.TEXT, "Участник", "", "", page="achievements"),
        _f("функции.достижения.ключ", FieldKind.TEXT, "Ключ", "", "", page="achievements"),
        _f("функции.достижения.счётчик", FieldKind.INT, "Счётчик", "", "", page="achievements"),
        _f("функции.розыгрыши.вес", FieldKind.TEXT, "Вес билета", "giveaway_settings", "weights", page="giveaway"),
        _f("функции.розыгрыши.роли", FieldKind.ROLE_LIST, "Призы", "giveaway_settings", "roles", page="giveaway"),
        _f("функции.голос.хаб", FieldKind.VOICE, "Хаб", "voice_hubs", "channel_id", page="voice"),
        _f("функции.голос.готовность", FieldKind.TEXT, "Готовность хаба", "", "", page="voice"),
        _f(
            "функции.голос.пресет",
            FieldKind.ENUM,
            "Пресет",
            "voice_hubs",
            "settings.preset",
            choices=(HubPreset.NAME, HubPreset.CLONE),
            page="voice",
        ),
        _f("функции.голос.шаблон", FieldKind.TEXT, "Шаблон имени", "voice_hubs", "settings.template", page="voice"),
        _f(
            "функции.голос.клон",
            FieldKind.VOICE,
            "Канал-шаблон",
            "voice_hubs",
            "settings.template_channel_id",
            page="voice",
        ),
        _f(
            "функции.обращения.панель",
            FieldKind.CHANNEL,
            "Канал панели",
            "ticket_panels",
            "channel_id",
            page="tickets",
        ),
        _f(
            "функции.обращения.архив",
            FieldKind.CHANNEL,
            "Архив",
            "ticket_panels",
            "settings.archive_channel_id",
            page="tickets",
        ),
        _f(
            "функции.обращения.лог",
            FieldKind.CHANNEL,
            "Лог тикетов",
            "ticket_panels",
            "settings.log_channel_id",
            page="tickets",
        ),
        _f(
            "функции.обращения.категория",
            FieldKind.CATEGORY,
            "Категория Discord",
            "ticket_panels",
            "settings.discord_category_id",
            page="tickets",
        ),
        _f(
            "функции.обращения.режим",
            FieldKind.ENUM,
            "Режим",
            "ticket_panels",
            "settings.open_mode",
            choices=(TicketOpenMode.CHANNEL, TicketOpenMode.THREAD),
            page="tickets",
        ),
        _f(
            "функции.обращения.автор",
            FieldKind.BOOL,
            "Автор может закрыть",
            "ticket_panels",
            "settings.author_may_close",
            page="tickets",
        ),
        _f("функции.обращения.категории", FieldKind.TEXT, "Категории", "ticket_categories", "names", page="tickets"),
        _f(
            "функции.автомод.канал",
            FieldKind.CHANNEL,
            "Канал карточки",
            "guild_configs",
            "automod_channel_id",
            page="automod",
        ),
        _f(
            "функции.верификация.канал",
            FieldKind.CHANNEL,
            "Канал",
            "verification_settings",
            "channel_id",
            page="verification",
        ),
        _f(
            "функции.верификация.роль",
            FieldKind.ROLE,
            "Роль",
            "verification_settings",
            "verified_role_id",
            page="verification",
        ),
        _f("функции.роли_по_выбору.меню", FieldKind.TEXT, "Меню", "self_role_menus", "title", page="selfroles"),
        _f(
            "функции.роли_по_выбору.добавить",
            FieldKind.TEXT,
            "Добавить роль",
            "self_role_options",
            "labels",
            page="selfroles",
        ),
        _f(
            "функции.роли_по_выбору.канал",
            FieldKind.CHANNEL,
            "Канал карточки",
            "self_role_menus",
            "channel_id",
            page="selfroles",
        ),
        _f("функции.статистика.канал", FieldKind.CHANNEL, "Канал", "stats_boards", "channel_id", page="stats"),
        _f(
            "функции.статистика.роли",
            FieldKind.ROLE_LIST,
            "Роли для подсчёта",
            "stats_boards",
            "roles_json",
            page="stats",
        ),
        _f("функции.опросы.канал", FieldKind.CHANNEL, "Канал", "poll_settings", "channel_id", page="polls"),
        _f("функции.опросы.роль", FieldKind.ROLE, "Роль упоминания", "poll_settings", "ping_role_id", page="polls"),
        _f("функции.youtube.фиды", FieldKind.TEXT, "Фиды", "youtube_feeds", "list", page="youtube"),
        _f("функции.youtube.добавить", FieldKind.TEXT, "Добавить фид", "youtube_feeds", "pending", page="youtube"),
        _f(
            "функции.youtube.канал",
            FieldKind.CHANNEL,
            "Канал Discord",
            "youtube_feeds",
            "discord_channel_id",
            page="youtube",
        ),
        _f("функции.youtube.роль", FieldKind.ROLE, "Роль упоминания", "youtube_feeds", "ping_role_id", page="youtube"),
        _f("функции.поиск_команды.канал", FieldKind.CHANNEL, "Канал", "lfg_settings", "channel_id", page="lfg"),
        _f("функции.поиск_команды.сроки", FieldKind.TEXT, "Сроки", "lfg_settings", "numbers", page="lfg"),
        _f("функции.группы.сроки", FieldKind.TEXT, "Сроки", "group_hubs", "numbers", page="groups"),
        _f("функции.группы.квоты", FieldKind.TEXT, "Порог и квоты", "group_hubs", "limits", page="groups"),
        _f(
            "функции.группы.архив",
            FieldKind.CATEGORY,
            "Категория архива",
            "group_hubs",
            "archive_category_id",
            page="groups",
        ),
        _f(
            "функции.набор.канал",
            FieldKind.CHANNEL,
            "Канал рекрутинга",
            "guild_configs",
            "recruit_channel_id",
            page="recruit",
        ),
        _f("функции.сигналы.состояние", FieldKind.BOOL, "Сигналы", "pulse_alert_settings", "enabled", page="alerts"),
        _f(
            "функции.сигналы.голос",
            FieldKind.INT,
            "Порог голоса",
            "pulse_alert_settings",
            "voice_floor",
            lo=0,
            hi=999,
            page="alerts",
        ),
        _f(
            "функции.сигналы.выходы",
            FieldKind.INT,
            "Порог выходов",
            "pulse_alert_settings",
            "leave_floor",
            lo=0,
            hi=999,
            page="alerts",
        ),
        _f(
            "функции.сигналы.авторы",
            FieldKind.INT,
            "Авторы",
            "pulse_alert_settings",
            "authors_pct",
            lo=0,
            hi=100,
            page="alerts",
        ),
        _f(
            "функции.сигналы.входы",
            FieldKind.INT,
            "Всплеск входов",
            "pulse_alert_settings",
            "join_spike_floor",
            lo=0,
            hi=999,
            page="alerts",
        ),
        _f("функции.данные.участник", FieldKind.TEXT, "Участник", "", "", page="data"),
        _f("функции.данные.перепись", FieldKind.TEXT, "Перепись", "guild_configs", "census_at", page="data"),
        _f("функции.данные.история", FieldKind.TEXT, "Собрать историю", "", "", page="data"),
        _f("каркас.обзор.готов", FieldKind.TEXT, "сервер готов к работе", "", "", page="skeleton"),
        _f("каркас.обзор.не_готов", FieldKind.TEXT, "сервер не готов", "", "", page="skeleton"),
        _f(
            "каркас.уведомления",
            FieldKind.CHANNEL,
            "уведомления в #структура включены",
            "guild_configs",
            "structure_channel_id",
            page="skeleton",
        ),
        _f(
            "каркас.уведомления_выкл",
            FieldKind.CHANNEL,
            "уведомления в #структура выключены",
            "guild_configs",
            "structure_channel_id",
            page="skeleton",
        ),
    )
    return (
        *ranks,
        *keys,
        *extras,
        *showcases,
        *tickets,
        *old,
        *quotas,
        *ladder,
        *rep_tiers,
        *achievements,
        *static,
    )


BLUEPRINT: tuple[FieldSpec, ...] = (*skeleton_fields(), *_feature_fields())
_BY_PATH: dict[str, FieldSpec] = {spec.path: spec for spec in BLUEPRINT}


def attachment_error(*, size: int, content_type: str | None) -> str | None:
    if int(size) > BLUEPRINT_MAX_BYTES:
        return "too_large"
    token = str(content_type or "").lower()
    if token and not any(part in token for part in _ALLOWED_TYPES):
        return "bad_content_type"
    return None


def spec_by_path(path: str) -> FieldSpec | None:
    found = _BY_PATH.get(path)
    if found is not None:
        return found
    if path.startswith("права.ключи."):
        key = path.removeprefix("права.ключи.")
        return _BY_PATH.get(f"права.ключи.{key}")
    return None


def strip_docs(payload: Mapping[str, Any]) -> dict[str, Any]:
    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {str(key): walk(value) for key, value in node.items() if not str(key).startswith("_")}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    cleaned = walk(payload)
    return cleaned if isinstance(cleaned, dict) else {}


def render_docs(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)

    def place(node: dict[str, Any], parts: Sequence[str], spec: FieldSpec) -> None:
        if not parts:
            return
        key = parts[0]
        if len(parts) == 1:
            current = node.get(key)
            if isinstance(current, dict):
                current.setdefault("_описание", spec.label)
                return
            node[key] = {"_описание": spec.label, "значение": current}
            return
        child = node.get(key)
        if not isinstance(child, dict):
            child = {} if child is None else {"значение": child}
            node[key] = child
        place(child, parts[1:], spec)

    for spec in BLUEPRINT:
        place(data, spec.path.split("."), spec)
    return data


def wanted_from_bytes(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    body, _issues = parse_blueprint(decoded)
    return body


def parse_blueprint(raw: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[BlueprintIssue, ...]]:
    issues: list[BlueprintIssue] = []
    helptext = raw.get(HELP_KEY) if isinstance(raw.get(HELP_KEY), Mapping) else {}
    version = helptext.get(HELP_VERSION_KEY, BLUEPRINT_VERSION) if isinstance(helptext, Mapping) else BLUEPRINT_VERSION
    version_path = f"{HELP_KEY}.{HELP_VERSION_KEY}"
    try:
        numeric = int(version)
    except (TypeError, ValueError):
        numeric = BLUEPRINT_VERSION + 1
        issues.append(BlueprintIssue(IssueLevel.ERROR, version_path, "bad_version"))
    if numeric > BLUEPRINT_VERSION:
        issues.append(BlueprintIssue(IssueLevel.ERROR, version_path, "bad_version"))
    body = strip_docs(raw)
    for path, value in _walk_values(body):
        spec = spec_by_path(path)
        if spec is None:
            continue
        issues.extend(_type_issues(spec, value))
    return body, tuple(issues)


def plan_blueprint(
    current: Mapping[str, Any],
    wanted: Mapping[str, Any],
    *,
    resolved: Mapping[str, int | None],
    rank_roles: Mapping[Rank, Iterable[int]] | None = None,
    actor: Rank | None = None,
) -> BlueprintPlan:
    issues: list[BlueprintIssue] = []
    changes: list[BlueprintChange] = []
    unknown: list[str] = []
    creates: list[str] = []
    roles: dict[int, str] = {}
    for path, value in _walk_values(wanted):
        spec = spec_by_path(path)
        if spec is None:
            if path and not _is_prefix(path):
                unknown.append(path)
            elif isinstance(value, Mapping) and not value:
                issues.append(BlueprintIssue(IssueLevel.WARN, path, "empty_section"))
            continue
        issues.extend(_type_issues(spec, value))
        if value is None and spec.required:
            issues.append(BlueprintIssue(IssueLevel.ERROR, path, "required_null"))
            continue
        if spec.kind is FieldKind.BINDING:
            binding = value if isinstance(value, Mapping) else {}
            object_id = resolved.get(path)
            name = str(binding.get(NAME_KEY) or "").strip()
            if object_id is None and name and spec.skeleton_key:
                creates.append(spec.skeleton_key)
            if object_id is not None:
                holder = None if rank_roles is None else rank_holding_role(rank_roles, object_id)
                if holder is not None and spec.kind is FieldKind.BINDING and path.startswith(f"{ROOT_ROLES}."):
                    other = f"{ROOT_ROLES}.role_{holder.key}"
                    if other != path:
                        issues.append(BlueprintIssue(IssueLevel.ERROR, path, "rank_taken"))
                seen = roles.get(int(object_id))
                if seen and seen != path:
                    issues.append(BlueprintIssue(IssueLevel.ERROR, path, "rank_taken"))
                roles[int(object_id)] = path
        if spec.kind is FieldKind.RANK_LIST and path.startswith("права.ключи."):
            key = path.removeprefix("права.ключи.")
            try:
                wanted_ranks = parse_rank_list(value if isinstance(value, (list, tuple)) else str(value))
            except ValueError:
                wanted_ranks = frozenset()
            if rank_roles is not None and key == CONFIG_MANAGE:
                risk = lockout_risk(
                    key=key,
                    wanted=wanted_ranks,
                    actor=Rank.SPECIAL if actor is None else actor,
                    rank_roles=rank_roles,
                )
                if risk is LockoutRisk.NO_HOLDER:
                    issues.append(BlueprintIssue(IssueLevel.ERROR, path, "lockout"))
        before = _display(_get_path(current, path))
        after = _display(_resolved_value(spec, value, resolved.get(path)))
        if before != after:
            changes.append(BlueprintChange(path, before, after, spec.kind))
    return BlueprintPlan(tuple(changes), tuple(issues), tuple(dict.fromkeys(unknown)), tuple(dict.fromkeys(creates)))


def _is_prefix(path: str) -> bool:
    token = path + "."
    return any(spec.path == path or spec.path.startswith(token) for spec in BLUEPRINT)


def _is_binding(node: Mapping[str, Any]) -> bool:
    keys = set(node)
    return bool(keys) and keys <= _BINDING_KEYS


def _walk_values(payload: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value and not _is_binding(value):
            yield from _walk_values(value, path)
            continue
        yield path, value


def _get_path(payload: Mapping[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def put_path(payload: dict[str, Any], path: str, value: Any) -> None:
    node: dict[str, Any] = payload
    parts = path.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _resolved_value(spec: FieldSpec, value: Any, object_id: int | None) -> Any:
    if spec.kind is FieldKind.BINDING and isinstance(value, Mapping):
        if object_id is not None:
            return int(object_id)
        return value.get("имя") or value.get("id")
    if object_id is not None:
        return int(object_id)
    return value


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, Mapping):
        object_id = value.get("id")
        if object_id is not None:
            return str(object_id)
        return str(value.get("имя") or "")
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _type_issues(spec: FieldSpec, value: Any) -> tuple[BlueprintIssue, ...]:
    if value is None:
        return ()
    path = spec.path
    if spec.kind is FieldKind.BINDING:
        if not isinstance(value, Mapping):
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        object_id = value.get("id")
        if object_id is not None and not _is_snowflake(object_id):
            return (BlueprintIssue(IssueLevel.ERROR, path, "not_snowflake"),)
        colour = value.get("цвет")
        if colour is not None and not isinstance(colour, str):
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        return ()
    if spec.kind is FieldKind.BOOL:
        if isinstance(value, bool):
            return ()
        return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
    if spec.kind in {FieldKind.CHANNEL, FieldKind.CATEGORY, FieldKind.VOICE, FieldKind.ROLE}:
        if not _is_snowflake(value):
            return (BlueprintIssue(IssueLevel.ERROR, path, "not_snowflake"),)
        return ()
    if spec.kind is FieldKind.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        if spec.lo is not None and value < spec.lo:
            return (BlueprintIssue(IssueLevel.ERROR, path, "out_of_range"),)
        if spec.hi is not None and value > spec.hi:
            return (BlueprintIssue(IssueLevel.ERROR, path, "out_of_range"),)
        return ()
    if spec.kind is FieldKind.ENUM:
        token = str(value)
        if spec.choices and token not in spec.choices:
            if spec.path.endswith("часовой_пояс") and parse_timezone(token) is not None:
                return ()
            if spec.path.endswith("пресет") and token == "sequential":
                return (BlueprintIssue(IssueLevel.WARN, path, "numbering_gone"),)
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_choice"),)
        return ()
    if spec.kind is FieldKind.DURATION:
        if not isinstance(value, str) or parse_duration(value) is None:
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        return ()
    if spec.kind in {FieldKind.ID_LIST, FieldKind.ROLE_LIST}:
        if not isinstance(value, list):
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        if any(not _is_snowflake(item) for item in value):
            return (BlueprintIssue(IssueLevel.ERROR, path, "not_snowflake"),)
        return ()
    if spec.kind is FieldKind.RANK_LIST:
        try:
            parse_rank_list(value if isinstance(value, (list, tuple)) else str(value))
        except ValueError:
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_choice"),)
        return ()
    if spec.kind is FieldKind.TEXT:
        if not isinstance(value, str):
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        cleaned = sanitize_label(value)
        if spec.column.endswith("template") and cleaned and len(cleaned) > CHANNEL_NAME_LIMIT:
            return (BlueprintIssue(IssueLevel.ERROR, path, "out_of_range"),)
        if spec.column.endswith("template") and "{n}" in value:
            return (BlueprintIssue(IssueLevel.WARN, path, "numbering_gone"),)
        if cleaned is None and value.strip():
            return (BlueprintIssue(IssueLevel.ERROR, path, "bad_type"),)
        if cleaned is not None and len(cleaned) > LABEL_MAX and "шаблон" not in spec.path:
            return ()
        return ()
    return ()


def _is_snowflake(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        else:
            return False
    return 0 <= int(value) <= _SNOWFLAKE_MAX
