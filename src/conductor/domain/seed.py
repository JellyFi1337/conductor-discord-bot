"""Каркас сервера: SKELETON, план сида, допуск. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from conductor.domain.permissions import REQUIRED_PERMISSIONS, has_required_permissions
from conductor.domain.showcase import SHOWCASES, ShowcaseState

__all__ = [
    "BINDING_EXCEPTIONS",
    "CHANNEL_PAGES",
    "FEATURES",
    "INGEST_DEBOUNCE",
    "LAYOUT",
    "ROLE_GROUPS",
    "ROLE_PAGES",
    "SEED_BINDINGS",
    "SEED_NEEDS_CONFIRMATION",
    "SEED_ROLE_ALIASES",
    "SEED_STAGES",
    "SKELETON",
    "BlueprintStatus",
    "ExistingItem",
    "FeatureVerdict",
    "SeedGate",
    "SeedLayout",
    "SeedMode",
    "SeedPlan",
    "SeedReport",
    "SeedRole",
    "SeedVerdict",
    "SkeletonCheck",
    "SkeletonEntry",
    "SkeletonGate",
    "SkeletonKind",
    "SkeletonStatus",
    "creatable_keys",
    "dependency_order",
    "entries_for_feature",
    "entries_in_category",
    "entry_by_key",
    "feature_of",
    "feature_verdicts",
    "gate_for",
    "has_seed_permissions",
    "has_seed_role_headroom",
    "is_reusable_seed_channel",
    "is_same_name",
    "match_skeleton",
    "needs_connect",
    "plan",
    "required_keys",
    "layout_keys",
    "role_order",
    "seed_gap_notes",
    "seed_gate",
    "seed_guest_plan",
    "seed_role_aliases",
    "skeleton_entries",
    "skeleton_setting_fields",
    "blueprint_status",
    "colour_hex",
    "diagnose_status",
    "rights_key",
]


SEED_NEEDS_CONFIRMATION = True

INGEST_DEBOUNCE = 1.2

SEED_STAGES: tuple[str, ...] = (
    "bot_perms",
    "scan",
    "rights",
    "bind",
    "panels",
    "data",
    "census",
)


class SkeletonKind(StrEnum):
    CATEGORY = "category"
    TEXT = "text"
    VOICE = "voice"
    ROLE = "role"


class SkeletonGate(StrEnum):
    PUBLIC = "public"
    VERIFIED = "verified"
    STAFF = "staff"
    EVIDENCE = "evidence"
    GROUP_ARCHIVE = "group_archive"
    NONE = "none"


class SkeletonStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class SeedGate(StrEnum):
    OK = "ok"
    NOT_SERVED = "not_served"


class SeedMode(StrEnum):
    PLAN = "plan"
    ADOPT = "adopt"
    BUILD = "build"


class SeedVerdict(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    NOT_SERVED = "not_served"
    NO_PERMS = "no_perms"
    RATE_LIMITED = "rate_limited"
    HELD = "held"
    FAILED = "failed"


class BlueprintStatus(StrEnum):
    MISSING = "missing"
    FOUND = "found"
    BOUND = "bound"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class SkeletonEntry:
    key: str
    kind: SkeletonKind
    category: str | None
    name: str
    gate: SkeletonGate
    binding: str | None
    diagnose_label: str
    optional: bool = False
    topic: str = ""
    hoist: bool = True
    colour: int = 0
    aliases: tuple[str, ...] = ()
    also_bind: tuple[str, ...] = ()
    required: bool = True
    creatable: bool = True
    adoptable: bool = True
    feature: str = ""


@dataclass(frozen=True, slots=True)
class ExistingItem:
    bound_id: int | None = None
    exists: bool = False
    name: str = ""
    category_id: int | None = None
    overwrites_ok: bool = True
    position_ok: bool = True
    category_ok: bool = True
    found_by_name: bool = False
    found_id: int | None = None
    colour_ok: bool = True
    hoist_ok: bool = True


@dataclass(frozen=True, slots=True)
class SeedPlan:
    create: tuple[str, ...]
    reuse: tuple[str, ...]
    repair: tuple[str, ...]
    by_name: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkeletonCheck:
    key: str
    label: str
    status: SkeletonStatus
    hint: str = ""


@dataclass(frozen=True, slots=True)
class FeatureVerdict:
    feature: str
    label: str
    status: SkeletonStatus
    missing: tuple[str, ...]
    broken: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True, slots=True)
class SeedRole:
    name: str
    colour: int
    hoist: bool = True


@dataclass(frozen=True, slots=True)
class SeedLayout:
    # Оформление: одно эмодзи и разделитель «・». Корневое слово остаётся прежним —
    # по нему мастер сопоставления узнаёт канал, а старое имя живёт в алиасах.
    cat_entry: str = "🚪・Вход"
    cat_server: str = "🌐・Сервер"
    cat_talk: str = "💬・Общение"
    cat_rooms: str = "🔊・Комнаты"
    cat_groups: str = "👥・Группы"
    cat_group_archive: str = "🗄️・Архив групп"
    cat_tickets: str = "🎫・Обращения"
    cat_staff: str = "🔒・Следящие"
    cat_evidence: str = "🔍・Доказательства"
    rules: str = "📜・правила"
    verify: str = "✅・верификация"
    roles_channel: str = "🎭・роли"
    lfg: str = "🎮・поиск-команды"
    polls: str = "📊・опросы"
    video: str = "🎬・видео"
    stats: str = "📈・статистика"
    tickets: str = "🎫・тикеты"
    announce: str = "📢・анонсы"
    talk_chat: str = "💬・общий-чат"
    talk_media: str = "📸・медиа"
    talk_giveaways: str = "🎉・розыгрыши"
    talk_events: str = "📅・сборы"
    ticket_archive: str = "📦・архив-тикетов"
    ticket_log: str = "🧾・лог-тикетов"
    staff_feed: str = "⚡・действия"
    admin_logs: str = "🔐・лог-администрации"
    user_logs: str = "👣・лог-участников"
    automod: str = "🤖・автомод"
    evidence_archive: str = "📦・архив-доказательств"
    structure: str = "🧭・структура"
    bug_reports: str = "🐞・баг-репорты"
    hub: str = "➕ Создать комнату"
    role_special: str = "Специальный Следящий"
    role_chief: str = "Главный Следящий"
    role_senior: str = "Старший Следящий"
    role_watcher: str = "Следящий"
    role_ruby: str = "Ruby Old"
    role_emerald: str = "Emerald Old"
    role_sapphire: str = "Sapphire Old"
    role_amethyst: str = "Amethyst Old"
    role_mute: str = "Мут"
    role_verified: str = "Верифицирован"
    role_sinner: str = "Изгой"
    role_guest: str = "Гость"
    role_tiers: tuple[str, ...] = (
        "Изгой",
        "Подозрительный",
        "Обычный",
        "Надёжный",
        "Уважаемый",
        "Образцовый",
    )
    role_regular: str = "Постоялец"
    role_resident: str = "Житель"
    role_friend: str = "Друг сервера"
    role_sub_youtube: str = "Новые видео"
    role_sub_polls: str = "Опросы"
    role_sub_announce: str = "Анонсы"
    role_sub_lfg: str = "Поиск игры"
    role_achievements: tuple[str, ...] = (
        "Фиксер 1",
        "Фиксер 2",
        "Фиксер 3",
        "Идеолог 1",
        "Идеолог 2",
        "Идеолог 3",
        "Бдительный 1",
        "Бдительный 2",
        "Бдительный 3",
        "Бдительный 4",
    )

    def role_specs(self) -> tuple[SeedRole, ...]:
        return tuple(
            SeedRole(entry.name, entry.colour, hoist=entry.hoist)
            for entry in SKELETON
            if entry.kind is SkeletonKind.ROLE
        )

    def role_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.role_specs())

    def category_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in SKELETON if entry.kind is SkeletonKind.CATEGORY)

    def text_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in SKELETON if entry.kind is SkeletonKind.TEXT)

    def voice_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in SKELETON if entry.kind is SkeletonKind.VOICE)


LAYOUT = SeedLayout()

FEATURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("entry", "DIAGNOSE_FEATURE_ENTRY", ("cat_entry", "rules")),
    ("verification", "DIAGNOSE_FEATURE_VERIFICATION", ("verify", "role_verified")),
    (
        "self_roles",
        "DIAGNOSE_FEATURE_SELF_ROLES",
        ("roles", "role_sub_youtube", "role_sub_polls", "role_sub_announce", "role_sub_lfg"),
    ),
    ("voice", "DIAGNOSE_FEATURE_VOICE", ("cat_rooms", "hub")),
    ("tickets", "DIAGNOSE_FEATURE_TICKETS", ("cat_tickets", "tickets", "ticket_archive", "ticket_log")),
    ("evidence", "DIAGNOSE_FEATURE_EVIDENCE", ("cat_evidence", "evidence_archive")),
    ("logs", "DIAGNOSE_FEATURE_LOGS", ("cat_staff", "staff_feed", "admin_logs", "user_logs", "bug_reports")),
    ("lfg", "DIAGNOSE_FEATURE_LFG", ("lfg",)),
    ("polls", "DIAGNOSE_FEATURE_POLLS", ("polls",)),
    ("stats", "DIAGNOSE_FEATURE_STATS", ("stats",)),
    ("video", "DIAGNOSE_FEATURE_VIDEO", ("video",)),
    ("automod", "DIAGNOSE_FEATURE_AUTOMOD", ("automod",)),
    ("groups", "DIAGNOSE_FEATURE_GROUPS", ("cat_groups",)),
    ("group_archive", "DIAGNOSE_FEATURE_GROUP_ARCHIVE", ("cat_group_archive",)),
    (
        "talk",
        "DIAGNOSE_FEATURE_TALK",
        ("cat_talk", "announce", "talk_chat", "talk_media", "talk_giveaways", "talk_events"),
    ),
    (
        "core",
        "DIAGNOSE_FEATURE_CORE",
        (
            "cat_server",
            "structure",
            "role_special",
            "role_chief",
            "role_senior",
            "role_watcher",
            "role_ruby",
            "role_emerald",
            "role_sapphire",
            "role_amethyst",
            "role_friend",
            "role_resident",
            "role_regular",
            "role_guest",
            "role_exemplary",
            "role_respected",
            "role_reliable",
            "role_ordinary",
            "role_suspicious",
            "role_sinner",
            "role_fixer_3",
            "role_fixer_2",
            "role_fixer_1",
            "role_ideologist_3",
            "role_ideologist_2",
            "role_ideologist_1",
            "role_vigilant_4",
            "role_vigilant_3",
            "role_vigilant_2",
            "role_vigilant_1",
            "role_mute",
        ),
    ),
)

_FEATURE_BY_KEY: dict[str, str] = {key: feature for feature, _copy, keys in FEATURES for key in keys}

# Имена до оформления: сервер, собранный старой версией, узнаётся по ним.
_PLAIN_NAMES: dict[str, str] = {
    "cat_entry": "Вход",
    "cat_server": "Сервер",
    "cat_talk": "Общение",
    "cat_rooms": "Комнаты",
    "cat_groups": "Группы",
    "cat_group_archive": "Архив групп",
    "cat_tickets": "Обращения",
    "cat_staff": "Следящие",
    "cat_evidence": "Доказательства",
    "rules": "правила",
    "verify": "верификация",
    "roles": "роли",
    "lfg": "поиск-команды",
    "polls": "опросы",
    "video": "видео",
    "stats": "статистика",
    "tickets": "тикеты",
    "announce": "анонсы",
    "talk_chat": "общий-чат",
    "talk_media": "медиа",
    "talk_giveaways": "розыгрыши",
    "talk_events": "сборы",
    "ticket_archive": "архив-тикетов",
    "ticket_log": "лог-тикетов",
    "staff_feed": "действия",
    "admin_logs": "лог-администрации",
    "user_logs": "лог-участников",
    "automod": "автомод",
    "evidence_archive": "архив-доказательств",
    "structure": "структура",
    "bug_reports": "баг-репорты",
    "hub": "➕ создать комнату",
}


def _named_aliases(key: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    plain = _PLAIN_NAMES.get(key)
    if plain is None or plain in aliases:
        return aliases
    return (*aliases, plain)


def _cat(
    key: str,
    name: str,
    gate: SkeletonGate,
    binding: str,
    *,
    aliases: tuple[str, ...] = (),
    also_bind: tuple[str, ...] = (),
    required: bool = True,
    creatable: bool = True,
    adoptable: bool = True,
) -> SkeletonEntry:
    return SkeletonEntry(
        key=key,
        kind=SkeletonKind.CATEGORY,
        category=None,
        name=name,
        gate=gate,
        binding=binding,
        diagnose_label=name,
        aliases=_named_aliases(key, aliases),
        also_bind=also_bind,
        required=required,
        creatable=creatable,
        adoptable=adoptable,
        feature=_FEATURE_BY_KEY.get(key, ""),
    )


def _text(
    key: str,
    category: str,
    name: str,
    gate: SkeletonGate,
    binding: str,
    topic: str,
    *,
    aliases: tuple[str, ...] = (),
    also_bind: tuple[str, ...] = (),
    required: bool = True,
    creatable: bool = True,
    adoptable: bool = True,
) -> SkeletonEntry:
    return SkeletonEntry(
        key=key,
        kind=SkeletonKind.TEXT,
        category=category,
        name=name,
        gate=gate,
        binding=binding,
        diagnose_label=f"#{name}",
        topic=topic,
        aliases=_named_aliases(key, aliases),
        also_bind=also_bind,
        required=required,
        creatable=creatable,
        adoptable=adoptable,
        feature=_FEATURE_BY_KEY.get(key, ""),
    )


def _voice(
    key: str,
    category: str,
    name: str,
    gate: SkeletonGate,
    binding: str,
    *,
    aliases: tuple[str, ...] = (),
    also_bind: tuple[str, ...] = (),
    required: bool = True,
    creatable: bool = True,
    adoptable: bool = True,
) -> SkeletonEntry:
    return SkeletonEntry(
        key=key,
        kind=SkeletonKind.VOICE,
        category=category,
        name=name,
        gate=gate,
        binding=binding,
        diagnose_label=name,
        aliases=_named_aliases(key, aliases),
        also_bind=also_bind,
        required=required,
        creatable=creatable,
        adoptable=adoptable,
        feature=_FEATURE_BY_KEY.get(key, ""),
    )


def _role(
    key: str,
    name: str,
    binding: str,
    colour: int,
    *,
    optional: bool = False,
    hoist: bool = True,
    aliases: tuple[str, ...] = (),
    required: bool = True,
    creatable: bool = True,
    adoptable: bool = True,
) -> SkeletonEntry:
    return SkeletonEntry(
        key=key,
        kind=SkeletonKind.ROLE,
        category=None,
        name=name,
        gate=SkeletonGate.NONE,
        binding=binding,
        diagnose_label=name,
        optional=optional,
        hoist=hoist,
        colour=colour,
        aliases=aliases,
        required=required,
        creatable=creatable,
        adoptable=adoptable,
        feature=_FEATURE_BY_KEY.get(key, ""),
    )


SKELETON: tuple[SkeletonEntry, ...] = (
    _cat("cat_entry", LAYOUT.cat_entry, SkeletonGate.PUBLIC, "guild_layout:cat_entry"),
    _cat(
        "cat_server",
        LAYOUT.cat_server,
        SkeletonGate.VERIFIED,
        "guild_layout:cat_server",
        aliases=("Conductor · общее",),
    ),
    _cat(
        "cat_rooms",
        LAYOUT.cat_rooms,
        SkeletonGate.VERIFIED,
        "guild_layout:cat_rooms",
        aliases=("Conductor · голос",),
        also_bind=("voice_hubs.category_id",),
    ),
    _cat(
        "cat_groups",
        LAYOUT.cat_groups,
        SkeletonGate.VERIFIED,
        "group_hubs.category_id",
        aliases=("группы",),
        also_bind=("guild_layout:cat_groups",),
    ),
    _cat(
        "cat_group_archive",
        LAYOUT.cat_group_archive,
        SkeletonGate.GROUP_ARCHIVE,
        "group_hubs.archive_category_id",
        aliases=("архив групп",),
        required=False,
        creatable=True,
    ),
    _cat(
        "cat_tickets",
        LAYOUT.cat_tickets,
        SkeletonGate.STAFF,
        "guild_layout:cat_tickets",
        aliases=("Conductor · обращения",),
        also_bind=("ticket_panels.settings.discord_category_id",),
    ),
    _cat("cat_staff", LAYOUT.cat_staff, SkeletonGate.STAFF, "guild_layout:cat_staff", aliases=("Conductor · логи",)),
    _cat(
        "cat_evidence",
        LAYOUT.cat_evidence,
        SkeletonGate.EVIDENCE,
        "mod_settings.evidence_category_id",
        also_bind=("guild_layout:cat_evidence",),
    ),
    _text(
        "rules",
        "cat_entry",
        LAYOUT.rules,
        SkeletonGate.PUBLIC,
        "verification_settings.rules_channel_id",
        "Правила сервера",
        also_bind=("guild_layout:rules",),
    ),
    _text(
        "verify",
        "cat_entry",
        LAYOUT.verify,
        SkeletonGate.PUBLIC,
        "verification_settings.channel_id",
        "Подтверждение, что вы человек",
    ),
    _text(
        "roles",
        "cat_server",
        LAYOUT.roles_channel,
        SkeletonGate.VERIFIED,
        "self_role_menus.channel_id",
        "Роли по выбору",
    ),
    _text(
        "lfg",
        "cat_server",
        LAYOUT.lfg,
        SkeletonGate.VERIFIED,
        "lfg_settings.channel_id",
        "Поиск команды",
    ),
    _text(
        "polls",
        "cat_server",
        LAYOUT.polls,
        SkeletonGate.VERIFIED,
        "poll_settings.channel_id",
        "Опросы",
    ),
    _text(
        "video",
        "cat_server",
        LAYOUT.video,
        SkeletonGate.VERIFIED,
        "guild_layout:video",
        "Новые видео",
    ),
    _text(
        "stats",
        "cat_server",
        LAYOUT.stats,
        SkeletonGate.VERIFIED,
        "stats_boards.channel_id",
        "Статистика сервера",
    ),
    _text(
        "tickets",
        "cat_server",
        LAYOUT.tickets,
        SkeletonGate.VERIFIED,
        "ticket_panels.channel_id",
        "Обращения к следящим",
    ),
    _text(
        "announce",
        "cat_server",
        LAYOUT.announce,
        SkeletonGate.VERIFIED,
        "guild_layout:announce",
        "Анонсы владельца сервера",
        required=False,
    ),
    _cat(
        "cat_talk",
        LAYOUT.cat_talk,
        SkeletonGate.VERIFIED,
        "guild_layout:cat_talk",
        required=False,
    ),
    _text(
        "talk_chat",
        "cat_talk",
        LAYOUT.talk_chat,
        SkeletonGate.VERIFIED,
        "guild_layout:talk_chat",
        "Общение сервера",
        required=False,
    ),
    _text(
        "talk_media",
        "cat_talk",
        LAYOUT.talk_media,
        SkeletonGate.VERIFIED,
        "guild_layout:talk_media",
        "Картинки и видео",
        required=False,
    ),
    _text(
        "talk_giveaways",
        "cat_talk",
        LAYOUT.talk_giveaways,
        SkeletonGate.VERIFIED,
        "guild_layout:talk_giveaways",
        "Розыгрыши: /giveaway",
        required=False,
    ),
    _text(
        "talk_events",
        "cat_talk",
        LAYOUT.talk_events,
        SkeletonGate.VERIFIED,
        "guild_layout:talk_events",
        "Сборы: /event",
        required=False,
    ),
    _voice(
        "hub",
        "cat_rooms",
        LAYOUT.hub,
        SkeletonGate.VERIFIED,
        "voice_hubs.channel_id",
        aliases=("хаб",),
    ),
    _text(
        "ticket_archive",
        "cat_tickets",
        LAYOUT.ticket_archive,
        SkeletonGate.STAFF,
        "ticket_panels.settings.archive_channel_id",
        "Архив закрытых обращений",
    ),
    _text(
        "ticket_log",
        "cat_tickets",
        LAYOUT.ticket_log,
        SkeletonGate.STAFF,
        "ticket_panels.settings.log_channel_id",
        "Журнал обращений",
    ),
    _text(
        "staff_feed",
        "cat_staff",
        LAYOUT.staff_feed,
        SkeletonGate.STAFF,
        "guild_configs.staff_feed_channel_id",
        "Действия следящих",
    ),
    _text(
        "admin_logs",
        "cat_staff",
        LAYOUT.admin_logs,
        SkeletonGate.STAFF,
        "guild_configs.admin_log_channel_id",
        "Кадры, настройки, сигналы",
        aliases=("логи-админ",),
    ),
    _text(
        "user_logs",
        "cat_staff",
        LAYOUT.user_logs,
        SkeletonGate.STAFF,
        "guild_configs.user_log_channel_id",
        "Входы, выходы, ники, роли",
        aliases=("логи-участники",),
    ),
    _text(
        "automod",
        "cat_staff",
        LAYOUT.automod,
        SkeletonGate.STAFF,
        "guild_configs.automod_channel_id",
        "Правила автомодерации",
    ),
    _text(
        "structure",
        "cat_staff",
        LAYOUT.structure,
        SkeletonGate.STAFF,
        "guild_configs.structure_channel_id",
        "Каркас, привязки и панели Conductor",
    ),
    _text(
        "bug_reports",
        "cat_staff",
        LAYOUT.bug_reports,
        SkeletonGate.STAFF,
        "guild_layout:bug_reports",
        "Отчёты об ошибках",
        creatable=True,
    ),
    _text(
        "evidence_archive",
        "cat_evidence",
        LAYOUT.evidence_archive,
        SkeletonGate.EVIDENCE,
        "mod_settings.evidence_archive_channel_id",
        "Архив закрытых доказательств",
    ),
    _role("role_special", LAYOUT.role_special, "guild_ranks.role_id", 0xC45CFF, aliases=("Special",)),
    _role("role_chief", LAYOUT.role_chief, "guild_ranks.role_id", 0xF1C40F, aliases=("Chief",)),
    _role("role_senior", LAYOUT.role_senior, "guild_ranks.role_id", 0xE67E22, aliases=("Senior",)),
    _role("role_watcher", LAYOUT.role_watcher, "guild_ranks.role_id", 0x3498DB, aliases=("Watcher",)),
    _role("role_ruby", LAYOUT.role_ruby, "guild_configs.ruby_role_id", 0xE91E63, optional=True),
    _role("role_emerald", LAYOUT.role_emerald, "guild_configs.emerald_role_id", 0x2ECC71, optional=True),
    _role("role_sapphire", LAYOUT.role_sapphire, "guild_configs.sapphire_role_id", 0x206694, optional=True),
    _role("role_amethyst", LAYOUT.role_amethyst, "guild_configs.amethyst_role_id", 0x9B59B6, optional=True),
    _role("role_friend", LAYOUT.role_friend, "ladder_settings.friend_role_id", 0x1ABC9C),
    _role("role_resident", LAYOUT.role_resident, "ladder_settings.resident_role_id", 0x5DADE2),
    _role("role_regular", LAYOUT.role_regular, "ladder_settings.regular_role_id", 0xA569BD),
    _role("role_guest", LAYOUT.role_guest, "ladder_settings.guest_role_id", 0x95A5A6),
    _role("role_exemplary", LAYOUT.role_tiers[5], "reputation_tiers.role_id", 0xF4D03F),
    _role("role_respected", LAYOUT.role_tiers[4], "reputation_tiers.role_id", 0x16A085),
    _role("role_reliable", LAYOUT.role_tiers[3], "reputation_tiers.role_id", 0x2980B9),
    _role("role_ordinary", LAYOUT.role_tiers[2], "reputation_tiers.role_id", 0x7F8C8D),
    _role("role_suspicious", LAYOUT.role_tiers[1], "reputation_tiers.role_id", 0xC0392B),
    _role("role_sinner", LAYOUT.role_tiers[0], "reputation_tiers.role_id", 0x992D22),
    _role("role_fixer_3", LAYOUT.role_achievements[2], "achievement_defs.role_id", 0x145A32),
    _role("role_fixer_2", LAYOUT.role_achievements[1], "achievement_defs.role_id", 0x1E8449),
    _role("role_fixer_1", LAYOUT.role_achievements[0], "achievement_defs.role_id", 0x27AE60),
    _role("role_ideologist_3", LAYOUT.role_achievements[5], "achievement_defs.role_id", 0x6C3483),
    _role("role_ideologist_2", LAYOUT.role_achievements[4], "achievement_defs.role_id", 0x8E44AD),
    _role("role_ideologist_1", LAYOUT.role_achievements[3], "achievement_defs.role_id", 0xAF7AC5),
    _role("role_vigilant_4", LAYOUT.role_achievements[9], "achievement_defs.role_id", 0xA04000),
    _role("role_vigilant_3", LAYOUT.role_achievements[8], "achievement_defs.role_id", 0xCA6F1E),
    _role("role_vigilant_2", LAYOUT.role_achievements[7], "achievement_defs.role_id", 0xE67E22),
    _role("role_vigilant_1", LAYOUT.role_achievements[6], "achievement_defs.role_id", 0xF5B041),
    _role("role_sub_youtube", LAYOUT.role_sub_youtube, "self_role_options.role_id", 0xED4245),
    _role("role_sub_polls", LAYOUT.role_sub_polls, "self_role_options.role_id", 0x5865F2),
    _role("role_sub_announce", LAYOUT.role_sub_announce, "self_role_options.role_id", 0xFEE75C),
    _role("role_sub_lfg", LAYOUT.role_sub_lfg, "self_role_options.role_id", 0xEB459E),
    _role("role_verified", LAYOUT.role_verified, "verification_settings.verified_role_id", 0x57F287),
    _role("role_mute", LAYOUT.role_mute, "mod_settings.mute_role_id", 0x4E5D94, hoist=False),
)

SEED_BINDINGS: tuple[str, ...] = tuple(entry.key for entry in SKELETON if entry.binding)

SEED_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    LAYOUT.role_special: ("Special",),
    LAYOUT.role_chief: ("Chief",),
    LAYOUT.role_senior: ("Senior",),
    LAYOUT.role_watcher: ("Watcher",),
}

BINDING_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "guild_configs.recruit_channel_id",
        "reputation_settings.sinner_role_id",
        "giveaways.prize_role_id",
        "poll_settings.ping_role_id",
        "youtube_feeds.discord_channel_id",
        "youtube_feeds.ping_role_id",
        "youtube_feeds.yt_channel_id",
        "lfg_posts.game_role_id",
        "lfg_posts.voice_channel_id",
        "voice_rooms.text_channel_id",
        "voice_rooms.channel_id",
        "tickets.channel_id",
        "tickets.dm_channel_id",
        "groups.voice_channel_id",
        "gatherings.voice_channel_id",
        "automod_hits.channel_id",
        "automod_requests.channel_id",
        "challenge_week.channel_id",
    }
)

CHANNEL_PAGES: tuple[tuple[str, ...], ...] = (
    ("cat_entry",),
    ("cat_server",),
    ("cat_talk",),
    ("cat_rooms", "cat_groups", "cat_group_archive"),
    ("cat_tickets", "cat_staff", "cat_evidence"),
)

ROLE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ranks", ("role_special", "role_chief", "role_senior", "role_watcher")),
    ("old", ("role_ruby", "role_emerald", "role_sapphire", "role_amethyst")),
    ("ladder", ("role_friend", "role_resident", "role_regular", "role_guest")),
    (
        "reputation",
        ("role_exemplary", "role_respected", "role_reliable", "role_ordinary", "role_suspicious", "role_sinner"),
    ),
    (
        "achievements",
        (
            "role_fixer_3",
            "role_fixer_2",
            "role_fixer_1",
            "role_ideologist_3",
            "role_ideologist_2",
            "role_ideologist_1",
            "role_vigilant_4",
            "role_vigilant_3",
            "role_vigilant_2",
            "role_vigilant_1",
        ),
    ),
    ("subscriptions", ("role_sub_youtube", "role_sub_polls", "role_sub_announce", "role_sub_lfg")),
    ("system", ("role_verified", "role_mute")),
)

ROLE_PAGES: tuple[tuple[str, ...], ...] = (
    ("ranks", "system"),
    ("ladder", "reputation"),
    ("achievements", "subscriptions"),
    ("old",),
)

_ROLE_ORDER: tuple[str, ...] = (
    "role_special",
    "role_chief",
    "role_senior",
    "role_watcher",
    "role_ruby",
    "role_emerald",
    "role_sapphire",
    "role_amethyst",
    "role_friend",
    "role_resident",
    "role_regular",
    "role_guest",
    "role_exemplary",
    "role_respected",
    "role_reliable",
    "role_ordinary",
    "role_suspicious",
    "role_sinner",
    "role_fixer_3",
    "role_fixer_2",
    "role_fixer_1",
    "role_ideologist_3",
    "role_ideologist_2",
    "role_ideologist_1",
    "role_vigilant_4",
    "role_vigilant_3",
    "role_vigilant_2",
    "role_vigilant_1",
    "role_sub_youtube",
    "role_sub_polls",
    "role_sub_announce",
    "role_sub_lfg",
    "role_verified",
    "role_mute",
)


@dataclass(frozen=True, slots=True)
class SeedReport:
    verdict: SeedVerdict
    created: int = 0
    reused: int = 0
    repaired: int = 0
    created_roles: int = 0
    reused_roles: int = 0
    created_channels: int = 0
    reused_channels: int = 0
    created_categories: int = 0
    bound: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    special_granted: bool = False
    humans: int = 0
    by_name: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    held: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def skeleton_entries(*, with_old: bool = False) -> tuple[SkeletonEntry, ...]:
    return tuple(entry for entry in SKELETON if with_old or not entry.optional)


def entries_for_feature(feature: str, *, with_old: bool = False) -> tuple[SkeletonEntry, ...]:
    return tuple(entry for entry in skeleton_entries(with_old=with_old) if entry.feature == feature)


def feature_of(key: str) -> str:
    return _FEATURE_BY_KEY.get(key, "")


def layout_keys(*, with_old: bool = True) -> tuple[str, ...]:
    """Строки каркаса, чей id хранится в guild_layout. Порядок — как в SKELETON."""
    found: list[str] = []
    for entry in skeleton_entries(with_old=with_old):
        paths = (entry.binding or "", *entry.also_bind)
        if any(path.startswith("guild_layout:") for path in paths):
            found.append(entry.key)
    return tuple(found)


def required_keys(*, with_old: bool = False) -> frozenset[str]:
    return frozenset(entry.key for entry in skeleton_entries(with_old=with_old) if entry.required)


def creatable_keys(*, with_old: bool = False) -> frozenset[str]:
    return frozenset(entry.key for entry in skeleton_entries(with_old=with_old) if entry.creatable)


def dependency_order(keys: Iterable[str]) -> tuple[str, ...]:
    """Категории, затем каналы внутри них, затем роли. Внутри группы — порядок SKELETON."""
    wanted = tuple(dict.fromkeys(keys))
    by_key = {entry.key: entry for entry in SKELETON}
    index = {entry.key: i for i, entry in enumerate(SKELETON)}

    def rank(key: str) -> tuple[int, int, str]:
        entry = by_key.get(key)
        if entry is None:
            return (3, 10_000, key)
        if entry.kind is SkeletonKind.CATEGORY:
            group = 0
        elif entry.kind is SkeletonKind.ROLE:
            group = 2
        else:
            group = 1
        return (group, index.get(key, 10_000), key)

    return tuple(sorted(wanted, key=rank))


def entry_by_key(key: str) -> SkeletonEntry | None:
    return next((entry for entry in SKELETON if entry.key == key), None)


def entries_in_category(key: str, *, with_old: bool = False) -> tuple[SkeletonEntry, ...]:
    cat = entry_by_key(key)
    kids = tuple(entry for entry in skeleton_entries(with_old=with_old) if entry.category == key)
    if cat is None:
        return kids
    return (cat, *kids)


def match_skeleton(kind: SkeletonKind, name: str) -> SkeletonEntry | None:
    wanted = str(name or "").strip()
    if not wanted:
        return None
    alias_hit: SkeletonEntry | None = None
    for entry in SKELETON:
        if entry.kind is not kind:
            continue
        if is_same_name(entry.name, wanted):
            return entry
        if alias_hit is None and any(is_same_name(alias, wanted) for alias in entry.aliases):
            alias_hit = entry
    return alias_hit


def needs_connect(plan: SeedPlan) -> bool:
    return bool(plan.by_name or plan.repair)


def role_order(*, with_old: bool = False) -> tuple[str, ...]:
    allowed = {entry.key for entry in skeleton_entries(with_old=with_old)}
    return tuple(key for key in _ROLE_ORDER if key in allowed)


def gate_for(category: str) -> SkeletonGate:
    entry = entry_by_key(category)
    if entry is None:
        return SkeletonGate.NONE
    return entry.gate


def skeleton_setting_fields() -> frozenset[str]:
    found: set[str] = set()
    for entry in SKELETON:
        for path in (entry.binding, *entry.also_bind):
            if not path or path.startswith("guild_layout:") or ".settings." in path:
                continue
            found.add(path)
    return frozenset(found)


def plan(existing: Mapping[str, ExistingItem], *, with_old: bool = False) -> SeedPlan:
    create: list[str] = []
    reuse: list[str] = []
    repair: list[str] = []
    by_name: list[str] = []
    for entry in skeleton_entries(with_old=with_old):
        item = existing.get(entry.key, ExistingItem())
        if item.bound_id and item.exists:
            needs_repair = not item.overwrites_ok or not item.category_ok
            if entry.kind is SkeletonKind.ROLE:
                needs_repair = needs_repair or not item.colour_ok or not item.hoist_ok
            else:
                needs_repair = needs_repair or not item.position_ok
            if needs_repair:
                repair.append(entry.key)
            else:
                reuse.append(entry.key)
            continue
        if item.bound_id and not item.exists:
            create.append(entry.key)
            continue
        if item.found_by_name:
            reuse.append(entry.key)
            by_name.append(entry.key)
            continue
        create.append(entry.key)
    return SeedPlan(create=tuple(create), reuse=tuple(reuse), repair=tuple(repair), by_name=tuple(by_name))


def seed_gate(*, guild_id: int, served: bool) -> SeedGate:
    del guild_id
    return SeedGate.OK if served else SeedGate.NOT_SERVED


def seed_gap_notes(*, bound: Iterable[str], explained: Iterable[str]) -> tuple[str, ...]:
    skip = set(bound) | set(explained)
    return tuple(name for name in SEED_BINDINGS if name not in skip)


def has_seed_permissions(permissions: object | None = None, **_legacy: object) -> bool:
    if permissions is not None:
        return has_required_permissions(permissions)
    if _legacy:
        return all(bool(_legacy.get(item.attr, False)) for item in REQUIRED_PERMISSIONS)
    return False


def has_seed_role_headroom(*, bot_top_position: int | None, blocking_positions: Iterable[int]) -> bool:
    if bot_top_position is None or int(bot_top_position) <= 0:
        return False
    top = int(bot_top_position)
    return all(top > int(position) for position in blocking_positions)


def seed_role_aliases(wanted: str) -> tuple[str, ...]:
    return SEED_ROLE_ALIASES.get(wanted, ())


def seed_guest_plan(
    *,
    members: Iterable[tuple[int, bool, frozenset[int]]],
    guest_role_id: int,
    ladder_role_ids: Iterable[int],
) -> tuple[int, ...]:
    occupied = {int(role_id) for role_id in ladder_role_ids}
    guest = int(guest_role_id)
    planned: list[int] = []
    for user_id, is_bot, have in members:
        if is_bot:
            continue
        held = {int(role_id) for role_id in have}
        if held & occupied:
            continue
        if guest in held:
            continue
        planned.append(int(user_id))
    return tuple(planned)


def is_same_name(existing: str, wanted: str) -> bool:
    return str(existing or "").casefold() == str(wanted or "").casefold()


def is_reusable_seed_channel(*, category_id: int | None, seed_category_ids: Iterable[int]) -> bool:
    if category_id is None:
        return True
    return int(category_id) in {int(item) for item in seed_category_ids}


def colour_hex(value: int) -> str:
    return f"#{int(value) & 0xFFFFFF:06X}"


def rights_key(entry: SkeletonEntry) -> str:
    if entry.kind is SkeletonKind.ROLE:
        return "role"
    if entry.key == "rules":
        return "public_ro"
    if entry.key == "verify":
        return "public_rw"
    return entry.gate.value


def blueprint_status(entry: SkeletonEntry, item: ExistingItem) -> BlueprintStatus:
    if item.bound_id and item.exists:
        needs_repair = not item.overwrites_ok or not item.category_ok
        if entry.kind is SkeletonKind.ROLE:
            needs_repair = needs_repair or not item.colour_ok or not item.hoist_ok
        else:
            needs_repair = needs_repair or not item.position_ok
        return BlueprintStatus.REPAIR if needs_repair else BlueprintStatus.BOUND
    if item.found_by_name and item.exists:
        return BlueprintStatus.FOUND
    return BlueprintStatus.MISSING


def diagnose_status(entry: SkeletonEntry, item: ExistingItem) -> SkeletonCheck:
    if entry.optional and not item.bound_id and not item.exists and not item.found_by_name:
        return SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.WARN, "old")
    healthy = (
        item.bound_id
        and item.exists
        and item.overwrites_ok
        and item.position_ok
        and item.category_ok
        and item.colour_ok
        and item.hoist_ok
    )
    if healthy:
        return SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.OK)
    if item.bound_id and item.exists:
        hint = "rights"
        if not item.category_ok:
            hint = "wrong_category"
        elif not item.position_ok:
            hint = "bot_below"
        elif not item.overwrites_ok:
            hint = "members_hidden"
        return SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.WARN, hint)
    if item.found_by_name and item.exists:
        return SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.WARN, "by_name")
    return SkeletonCheck(entry.key, entry.diagnose_label, SkeletonStatus.FAIL, "missing")


def feature_verdicts(
    checks: Sequence[SkeletonCheck],
    *,
    showcases: Mapping[str, str] | None = None,
    with_old: bool = False,
) -> tuple[FeatureVerdict, ...]:
    """Сворачивает построчные проверки в вердикт по каждой функции из FEATURES."""
    by_key = {item.key: item for item in checks}
    states = showcases or {}
    found: list[FeatureVerdict] = []
    for feature, copy_name, keys in FEATURES:
        missing: list[str] = []
        broken: list[str] = []
        for key in keys:
            entry = entry_by_key(key)
            if entry is None:
                continue
            if entry.optional and not with_old:
                continue
            check = by_key.get(key)
            if check is None or check.status is SkeletonStatus.FAIL:
                if entry.required:
                    missing.append(key)
                else:
                    broken.append(key)
            elif check.status is SkeletonStatus.WARN:
                broken.append(key)
        showcase = _showcase_state(states, feature)
        note = ""
        if showcase and showcase not in {ShowcaseState.LIVE.value, ShowcaseState.OFF.value}:
            note = showcase
        elif showcase == ShowcaseState.OFF.value and not missing:
            note = ShowcaseState.MISSING.value
        if missing:
            status = SkeletonStatus.FAIL
        elif broken or note:
            status = SkeletonStatus.WARN
        else:
            status = SkeletonStatus.OK
        found.append(
            FeatureVerdict(
                feature,
                copy_name,
                status,
                tuple(missing),
                tuple(broken),
                note,
            )
        )
    return tuple(found)


def _showcase_state(showcases: Mapping[str, str], feature: str) -> str:
    raw = showcases.get(feature)
    if raw:
        return str(raw)
    for spec in SHOWCASES:
        if spec.feature == feature:
            value = showcases.get(spec.id)
            if value:
                return str(value)
    return ""
