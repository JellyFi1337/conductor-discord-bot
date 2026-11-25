"""Сроки жизни данных и имена файлов. Без Discord и без БД."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conductor.domain.activity import as_utc

CACHE_TTL_SECONDS = 3600
RETENTION_DAYS = 90
BACKUP_KEEP = 7
BACKUP_PERIOD = 24 * 3600
PURGE_PERIOD = 24 * 3600
VOICE_OPEN_HOURS = 48
PANEL_STALE_DAYS = 7
ROLE_CHANGE_DAYS = 180
STAFF_ACTION_DAYS = 365
AUTOMOD_HIT_DAYS = 90
WATCHER_NOTE_DAYS = 180
USER_LOG_KEEP_DAYS = 90
USER_LOG_PURGE_CAP = 500
USER_LOG_PURGE_BATCH = 50
USER_LOG_PURGE_PAUSE = 1.0

KIND_DAYS = "days"
KIND_KEEP = "keep"
KIND_DERIVED = "derived"

FILTER_STALE = "stale"
FILTER_SNOWFLAKE = "snowflake"
FILTER_NO_CASE = "no_case_id"
FILTER_VOICE = "voice"

SETTING_DATA = "data_retention"
SETTING_TRANSCRIPT = "ticket_transcript"

DISCORD_EPOCH_MS = 1_420_070_400_000

#: Не snowflake Discord. Поля owner/claim, которые нельзя удалить вместе со строкой.
FORGOTTEN_USER_ID = 0
FORGOTTEN_REASON = "участник удалён"

#: Forget только снимает id; строка — операция сервера, в JSON её нет отдельным ключом.
FORGET_SENTINEL_KEYS = frozenset(
    {
        "tickets_claimed",
        "tickets_closed",
        "giveaways_closed",
        "polls_closed",
        "polls_revealed",
        "gatherings_closed",
        "groups_archived",
    }
)

#: Имя счётчика forget → ключ JSON. Старые ключи экспорта не переименовывать.
FORGET_EXPORT_KEYS: dict[str, str] = {
    "members": "member",
    "member_daily": "daily",
    "member_channels": "channels",
    "interaction_edges": "edges",
    "voice_sessions": "voice_sessions",
    "loyalty_registry": "loyalty",
    "loyalty_role_events": "loyalty_role_events",
    "reputation": "reputation",
    "reputation_events": "reputation_events",
    "ladder": "ladder",
    "achievement_progress": "achievement_progress",
    "achievement_grants": "achievement_grants",
    "reminders": "reminders",
    "watcher_duty": "watcher_duty",
    "watcher_notes": "watcher_notes",
    "watcher_suspensions": "watcher_suspensions",
    "role_changes": "role_changes",
    "mod_cases": "mod_cases",
    "mod_evidence": "mod_evidence",
    "achievement_sources": "achievement_sources",
    "mod_intents": "mod_intents",
    "unofficial_admins": "unofficial_admins",
    "giveaway_entries": "giveaway_entries",
    "giveaway_winners": "giveaway_winners",
    "poll_votes": "poll_votes",
    "gathering_rsvps": "gathering_rsvps",
    "group_members": "group_members",
    "group_invites": "group_invites",
    "bug_reports": "bug_reports",
    "voice_room_kicks": "voice_room_kicks",
    "voice_room_requests": "voice_room_requests",
    "voice_room_lfg": "voice_room_lfg",
    "tickets_owned": "tickets",
    "ticket_events": "ticket_events",
    "ticket_transcripts": "ticket_transcripts",
    "voice_rooms": "voice_rooms",
    "giveaways": "giveaways",
    "polls": "polls",
    "gatherings": "gatherings",
    "groups": "groups",
    "lfg_posts": "lfg_posts",
    "lfg_members": "lfg_members",
    "recruit_links": "recruit_links",
    "automod_hits": "automod_hits",
    "automod_requests": "automod_requests",
    "member_watch": "member_watch",
    "staff_actions": "staff_actions",
}


@dataclass(frozen=True, slots=True)
class RetentionRule:
    kind: str
    days: int | None = None
    reason: str = ""
    parent: str | None = None
    filter: str | None = None
    column: str | None = None
    setting: str | None = None


def days(
    n: int,
    *,
    column: str = "created_at",
    filter: str | None = None,
    setting: str | None = None,
    reason: str = "",
) -> RetentionRule:
    return RetentionRule(
        KIND_DAYS,
        days=int(n),
        column=column,
        filter=filter,
        setting=setting,
        reason=reason,
    )


def keep(reason: str) -> RetentionRule:
    text = str(reason).strip()
    if not text:
        raise ValueError("у keep нужна причина")
    return RetentionRule(KIND_KEEP, reason=text)


def derived(parent: str) -> RetentionRule:
    return RetentionRule(KIND_DERIVED, parent=str(parent))


def _agg(*, column: str, filter: str | None = None) -> RetentionRule:
    return days(RETENTION_DAYS, column=column, filter=filter, setting=SETTING_DATA)


RETENTION: dict[str, RetentionRule] = {
    "guild_configs": keep("настройки гильдии"),
    "guild_layout": keep("привязки каркаса"),
    "rate_profiles": keep("выученный темп создания"),
    "members": keep("карточка участника"),
    "activity_buckets": _agg(column="period_start"),
    "loyalty_batches": _agg(column="created_at"),
    "loyalty_registry": keep("реестр ролей Old"),
    "loyalty_role_events": _agg(column="created_at"),
    "member_daily": _agg(column="day"),
    "channel_daily": _agg(column="day"),
    # Курсор истории на канал, не данные: удалить строку тихого канала — значит перечитать его целиком.
    "channel_scans": keep("курсоры дочитывания каналов"),
    "interaction_edges": _agg(column="last_at"),
    "member_channels": _agg(column="last_at"),
    "ingested_messages": _agg(column="message_id", filter=FILTER_SNOWFLAKE),
    "ingested_reactions": _agg(column="message_id", filter=FILTER_SNOWFLAKE),
    "voice_sessions": _agg(column="left_at", filter=FILTER_VOICE),
    "panels": days(PANEL_STALE_DAYS, filter=FILTER_STALE),
    "role_changes": days(ROLE_CHANGE_DAYS),
    "guild_ranks": keep("привязки рангов"),
    "guild_permissions": keep("ключи прав"),
    "guild_access_states": keep("снимки доступа"),
    "unofficial_admin_notices": keep("неофициальные администраторы"),
    "watcher_duty": keep("дежурства следящих"),
    "watcher_notes": days(WATCHER_NOTE_DAYS),
    "watcher_suspensions": keep("отстранения следящих"),
    "voice_hubs": keep("хаб голосовых комнат"),
    "voice_rooms": keep("живые комнаты"),
    "voice_room_kicks": derived("voice_rooms"),
    "voice_room_requests": derived("voice_rooms"),
    "voice_room_lfg": derived("voice_rooms"),
    "voice_room_merges": derived("voice_rooms"),
    "ticket_panels": keep("панели тикетов"),
    "ticket_categories": keep("типы тикетов"),
    "tickets": keep("тикеты"),
    "ticket_events": derived("tickets"),
    "ticket_transcripts": days(365, setting=SETTING_TRANSCRIPT),
    "mod_settings": keep("настройки модерации"),
    "mod_cases": keep("дела модерации"),
    "member_watch": keep("активное наблюдение"),
    "automod_hits": days(AUTOMOD_HIT_DAYS),
    "verification_settings": keep("настройки верификации"),
    "self_role_menus": keep("меню ролей"),
    "stats_boards": keep("карточка статистики"),
    "self_role_options": keep("опции меню ролей"),
    "automod_requests": days(AUTOMOD_HIT_DAYS),
    "staff_actions": days(STAFF_ACTION_DAYS),
    "mod_evidence": keep("доказательства дел"),
    "mod_intents": keep("ожидающие подтверждения"),
    "channel_locks": keep("замки каналов"),
    "reputation_settings": keep("настройки репутации"),
    "reputation_tiers": keep("уровни репутации"),
    "reputation": keep("счета репутации"),
    "ladder_settings": keep("настройки лестницы"),
    "ladder": keep("ступень лестницы"),
    "giveaway_settings": keep("настройки розыгрышей"),
    "giveaways": keep("розыгрыши"),
    "giveaway_entries": derived("giveaways"),
    "giveaway_winners": derived("giveaways"),
    "poll_settings": keep("настройки опросов"),
    "polls": keep("опросы"),
    "poll_votes": derived("polls"),
    "youtube_feeds": keep("ленты YouTube"),
    "lfg_settings": keep("настройки поиска команды"),
    "lfg_posts": keep("наборы в команду"),
    "lfg_members": derived("lfg_posts"),
    "gatherings": keep("сборы"),
    "gathering_rsvps": derived("gatherings"),
    "group_hubs": keep("хаб групп"),
    "groups": keep("группы"),
    "group_members": derived("groups"),
    "group_archive_shelves": keep("полки архива групп"),
    "group_invites": derived("groups"),
    "reminders": keep("личные напоминания"),
    "bug_reports": keep("отчёты об ошибках"),
    "pulse_alert_settings": keep("пороги сигналов пульса"),
    "reputation_events": _agg(column="created_at", filter=FILTER_NO_CASE),
    "achievement_defs": keep("определения достижений"),
    "achievement_progress": keep("прогресс достижений"),
    "achievement_grants": keep("выданные достижения"),
    "achievement_sources": keep("источники достижений"),
    "recruit_links": keep("рекрут-ссылки"),
    "rename_state": keep("окно переименования каналов"),
}


def retention_cutoff(now: datetime, *, days: int = RETENTION_DAYS) -> datetime | None:
    if days <= 0:
        return None
    return as_utc(now) - timedelta(days=int(days))


def voice_open_cutoff(now: datetime, *, hours: int = VOICE_OPEN_HOURS) -> datetime:
    return as_utc(now) - timedelta(hours=int(hours))


def user_log_cutoff(now: datetime, *, days: int) -> datetime | None:
    if days <= 0:
        return None
    return as_utc(now) - timedelta(days=int(days))


def should_drop_user_log_card(
    *,
    author_id: int,
    bot_id: int,
    created_at: datetime,
    cutoff: datetime,
) -> bool:
    return int(author_id) == int(bot_id) and as_utc(created_at) < as_utc(cutoff)


def snowflake_created_at(snowflake: int) -> datetime:
    millis = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def snowflake_before(when: datetime) -> int:
    millis = int(as_utc(when).timestamp() * 1000)
    return max(0, (millis - DISCORD_EPOCH_MS) << 22)


def snowflake_at(when: datetime) -> int:
    return snowflake_before(when)


def export_filename(user_id: int) -> str:
    return f"conductor-{int(user_id)}.json"


def backup_filename(now: datetime) -> str:
    return f"conductor-{as_utc(now).strftime('%Y%m%d-%H%M%S')}.db"


def backups_to_drop(paths: Sequence[Path], *, keep: int) -> tuple[Path, ...]:
    ordered = tuple(sorted(paths, key=lambda path: path.name))
    if keep <= 0:
        return ordered
    extra = len(ordered) - int(keep)
    if extra <= 0:
        return ()
    return ordered[:extra]


def json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    return value
