"""Тексты: diagnose."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from conductor.domain.permissions import REQUIRED_PERMISSIONS

from .admin import READY_ISSUE_PANEL_STALE
from .common import copy_presence_off, copy_presence_on, grouped, hold_wait
from .moderation import evidence_denial_text
from .reputation import REP_DIAGNOSE
from .stats import PULSE_SIGNAL_AUTHORS, PULSE_SIGNAL_BOOSTS, PULSE_SIGNAL_INVERT, PULSE_SIGNAL_VOICE
from .voice import voice_denial_text

DIAGNOSE_TITLE = "Диагностика"

DIAGNOSE_TITLE_SETUP = "Диагностика · настройка"

DIAGNOSE_TITLE_SKELETON = "Диагностика · каркас"

DIAGNOSE_TITLE_READY = "Диагностика · готовность"

DIAGNOSE_TITLE_NOW = "Диагностика · сейчас"


def diagnose_heading(title: str, version: str) -> str:
    return f"{title} · {version}"


DIAGNOSE_TAB_SETUP = "Настройка"

DIAGNOSE_TAB_SKELETON = "Каркас"

DIAGNOSE_TAB_READY = "Готовность"

DIAGNOSE_TAB_NOW = "Сейчас"

DIAGNOSE_FEATURE_ENTRY = "Вход"

DIAGNOSE_FEATURE_VERIFICATION = "Верификация"

DIAGNOSE_FEATURE_SELF_ROLES = "Роли по выбору"

DIAGNOSE_FEATURE_VOICE = "Голосовые комнаты"

DIAGNOSE_FEATURE_TICKETS = "Обращения"

DIAGNOSE_FEATURE_EVIDENCE = "Доказательства"

DIAGNOSE_FEATURE_LOGS = "Логи"

DIAGNOSE_FEATURE_LFG = "Поиск команды"

DIAGNOSE_FEATURE_POLLS = "Опросы"

DIAGNOSE_FEATURE_STATS = "Статистика"

DIAGNOSE_FEATURE_VIDEO = "Видео"

DIAGNOSE_FEATURE_AUTOMOD = "Автомодерация"

DIAGNOSE_FEATURE_GROUPS = "Группы"

DIAGNOSE_FEATURE_GROUP_ARCHIVE = "Архив групп"

DIAGNOSE_FEATURE_TALK = "Общение"

DIAGNOSE_FEATURE_CORE = "Ядро"

DIAGNOSE_REPAIR = "Починить"

DIAGNOSE_REPAIR_ALL = "Починить всё"

DIAGNOSE_REAPPLY = "Пересобрать права"

DIAGNOSE_REPAIR_PICK = "Починить строку"

DIAGNOSE_APPLY_DEFAULTS = "Применить новые умолчания"

DIAGNOSE_APPLY_CONFIRM = (
    "Записать новые умолчания? Изменятся только отличающиеся поля (права, репутация и лестница). "
    "Счета и ступени не пересчитываются."
)

DIAGNOSE_SCHEME_DRIFT_ACTION = "Кнопка ниже запишет текущую схему в отличающиеся ключи."

DIAGNOSE_REP_DEFAULTS = "умолчания репутации изменились: старт 50, кик без списания"

DIAGNOSE_REP_DEFAULTS_ACTION = "Кнопка ниже запишет старт 50 и списание за кик 0. Счета не меняются."

DIAGNOSE_LADDER_DEFAULTS = "умолчания лестницы изменились"

DIAGNOSE_LADDER_DEFAULTS_ACTION = (
    "Кнопка ниже запишет пороги заказчика: постоялец 50 сообщений, житель 500/50 ч/30 дн., "
    "друг 1000/100 ч/365 дн. Ступени не пересчитываются."
)


def diagnose_scheme_drift(count: int) -> str:
    return f"умолчания прав изменились: {count} ключей отличаются от текущей схемы"


DIAGNOSE_DB_OK = "База отвечает"

DIAGNOSE_DB_DOWN = "База недоступна"

DIAGNOSE_DB_ACTION = "Проверьте диск и DATABASE_URL. Команды ещё отвечают, телеметрия не пишется."

DIAGNOSE_SCHEMA_OK = "Схема БД = head миграций"

DIAGNOSE_SCHEMA_ACTION = "На Postgres выполните conductor db upgrade или задайте AUTO_MIGRATE=true."

DIAGNOSE_CENSUS_NEVER = "Последняя сверка состава: ещё не было"

DIAGNOSE_CENSUS_ACTION = "Снимите перепись в /admin panel → Данные."

DIAGNOSE_QUEUES_BUSY = "Очереди и фон загружены. Если растёт — смотрите лог процесса."

ALERT_TITLE = "Авария"

ALERT_QUEUE = "Очередь логов переполнена: карточки в каналы следящих теряются."

ALERT_DB = "База недоступна: телеметрия не пишется, команды продолжают отвечать."

ALERT_ROLE = "Роль бота опустилась ниже управляемых ролей."

ALERT_BROKER = "Брокер ролей отклоняет заявки подряд."

ALERT_BROKER_ACTION = "Проверьте позицию роли бота и права Manage Roles."

ALERT_FOOTNOTE = "одно и то же не повторяется чаще раза в час"

PERMISSION_ORDER: tuple[str, ...] = tuple(item.attr for item in REQUIRED_PERMISSIONS)

PERMISSION_LABELS = {item.attr: item.label for item in REQUIRED_PERMISSIONS}

PERMISSION_ACTIONS = {
    "manage_roles": (
        "Выдайте боту право «Управлять ролями» (Manage Roles) и поставьте его роль выше ролей каркаса."
    ),
    "manage_channels": (
        "Выдайте боту право «Управлять каналами» (Manage Channels) — без него бот не выставит права каркаса."
    ),
    "ban_members": (
        "Выдайте боту право «Банить участников» (Ban Members) — без него ловушка верификации не банит."
    ),
    "kick_members": "Выдайте боту право «Исключать участников» (Kick Members).",
    "moderate_members": (
        "Выдайте боту право «Отстранять участников» (Moderate Members) — без него нет тайм-аута."
    ),
    "move_members": (
        "Выдайте боту право «Перемещать участников» (Move Members) — без него хаб комнат не работает."
    ),
    "manage_messages": (
        "Выдайте боту право «Управлять сообщениями» (Manage Messages) — чистка и закреп панелей."
    ),
    "view_audit_log": (
        "Выдайте боту право «Просмотр журнала аудита» (View Audit Log) в настройках сервера."
    ),
    "create_instant_invite": (
        "Выдайте боту право «Создавать приглашение» (Create Instant Invite) — рекрут-ссылки."
    ),
}


def permission_reason(attr: str) -> str:
    return PERMISSION_ACTIONS.get(attr, "")


INTENT_MEMBERS_ON = "Интент Server Members включён"

INTENT_MEMBERS_OFF = "Интент Server Members выключен"

INTENT_MEMBERS_ACTION = (
    "Включите Privileged Gateway Intent «Server Members Intent» в Discord Developer Portal и перезапустите бота."
)

INTENT_CONTENT_ON = "Интент Message Content включён"

INTENT_CONTENT_OFF = "Интент Message Content выключен"

INTENT_CONTENT_ACTION = (
    "Включите Privileged Gateway Intent «Message Content Intent» в Discord Developer Portal и перезапустите бота."
)

INTENT_DM_ON = "Интент Direct Messages включён"

INTENT_DM_OFF = "Интент Direct Messages выключен"

INTENT_DM_ACTION = "Перезапустите бота: в коде должен быть включён intents.dm_messages."


RUNTIME_NO_LATENCY = "нет данных"

RUNTIME_DISCONNECTED = "Нет задержки шлюза — бот не подключён. Перезапустите процесс."

RUNTIME_PUMP_DEAD = "Очередь логов не запущена. Перезапустите бота; если повторится — смотрите лог процесса."

RUNTIME_UNHEALTHY = (
    "Смотрите лог процесса: сбои отправки или полная очередь. При задержке шлюза больше секунды проверьте сеть."
)


PERM_HAS = "есть"

PERM_MISSING = "Нет права"


def permission_ok(label: str) -> str:
    return f"Право {label} {PERM_HAS}"


def permission_fail(label: str) -> str:
    return f"{PERM_MISSING} {label}"


def runtime_title(
    *, latency: str, backlog: int, sent: int, failed: int, dropped: int, reconnects: int = 0
) -> str:
    return (
        f"Шлюз {latency} · очередь логов {backlog} · отправлено {sent} · "
        f"сбои {failed} · отброшено {dropped} · переподключений {reconnects}"
    )


def diagnose_schema_fail(current: str | None, head: str | None) -> str:
    return f"Схема БД {current or '—'} ≠ head {head or '—'}"


def diagnose_queues(*, jobs: int, later: int, tasks: int, scheduled: int) -> str:
    return f"Очереди: работы {jobs} · отложенные {later} · фон {tasks} · планировщик {scheduled}"


def diagnose_census(when: str) -> str:
    return f"Последняя сверка состава: {when}"


def diagnose_startup(items: Mapping[str, int] | Sequence[tuple[str, int]]) -> str:
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    if not pairs:
        return "Старт: ещё не было"
    parts = [f"{name} {ms} мс" for name, ms in pairs]
    return "Старт: " + " · ".join(parts)


def diagnose_broker(streak: int) -> str:
    return f"Брокер ролей: отказов подряд {streak}"


def diagnose_pulse(*, band: str, age: str, presence: bool, invalidated: str) -> str:
    intent = copy_presence_on() if presence else copy_presence_off()
    return f"Пульс: {band} · снимок {age} · {intent} · сброс кэша {invalidated}"


DIAGNOSE_PULSE_NEVER = "Пульс ещё не снимался"

DIAGNOSE_PULSE_ACTION = "Откройте /stats → Сейчас."

DIAGNOSE_PULSE_UNKNOWN_ACTION = "Соберите историю в /admin panel → Данные или подождите живой голос."

DIAGNOSE_PULSE_ALERTS_OFF = "Сигналы пульса выключены"

DIAGNOSE_CATALOG_BOT = "каталожный бот ещё здесь"

DIAGNOSE_CATALOG_BOT_ACTION = "Выгоните его в настройках сервера."


def diagnose_pulse_alerts(*, enabled: bool, last: str) -> str:
    if not enabled:
        return DIAGNOSE_PULSE_ALERTS_OFF
    names = " · ".join((PULSE_SIGNAL_VOICE, PULSE_SIGNAL_INVERT, PULSE_SIGNAL_AUTHORS, PULSE_SIGNAL_BOOSTS))
    return f"Сигналы пульса: {names} · последний {last}"


def diagnose_catalog_bot(names: Sequence[str]) -> str:
    labels = [str(name) for name in names if str(name).strip()]
    if not labels:
        return DIAGNOSE_CATALOG_BOT
    return f"{DIAGNOSE_CATALOG_BOT}: {', '.join(labels)}"


def alert_permission(label: str) -> str:
    return f"Потеряно право {label}."


DIAGNOSE_YT_NONE = "YouTube: фидов нет"

DIAGNOSE_YT_ACTION = "Добавьте фид в /admin → YouTube."

DIAGNOSE_LOG_ACTION = "Страница /admin → Логи"

DIAGNOSE_TICKET_LOG_ACTION = "Страница /admin → Тикеты"

DIAGNOSE_EVIDENCE_OK = "Категория доказательств задана"

DIAGNOSE_EVIDENCE_OFF = "Категория доказательств не задана"

DIAGNOSE_EVIDENCE_ACTION = "Страница /admin → Молчание"

DIAGNOSE_EVIDENCE_ARCHIVE_OK = "архив доказательств задан"

DIAGNOSE_EVIDENCE_ARCHIVE_OFF = "архив доказательств не задан — закрытые каналы удаляются"

DIAGNOSE_EVIDENCE_ARCHIVE_ACTION = "Страница /admin → Молчание · архив"

DIAGNOSE_AUTOMOD_FILE_OK = "Файл правил автомодерации загружен"

DIAGNOSE_AUTOMOD_FILE_OFF = "Файл правил автомодерации не найден — встроенные умолчания"

DIAGNOSE_AUTOMOD_FILE_ACTION = "Положите data/automod.yml (см. data/automod.yml.example)"

DIAGNOSE_AUTOMOD_CARD_OK = "Карточка автомода на месте"

DIAGNOSE_AUTOMOD_CARD_STALE = "Карточка автомода удалена"

DIAGNOSE_AUTOMOD_CARD_ACTION = "Страница /admin → Автомод"

DIAGNOSE_MENTION_EVERYONE_ACTION = "Снимите право «Упоминать @everyone» у роли @everyone и не-персональных ролей"


def diagnose_mention_everyone(names: Sequence[str]) -> str:
    shown = ", ".join(str(item) for item in names[:8])
    extra = len(names) - 8
    if extra > 0:
        shown = f"{shown} и ещё {extra}"
    return f"право упоминать @everyone: {shown}"

DIAGNOSE_STATS_BOARD_LIVE = "канал статистики: жив"

DIAGNOSE_STATS_BOARD_GONE = "канал статистики: удалён"

DIAGNOSE_STATS_BOARD_OFF = "канал статистики: не задан"

DIAGNOSE_STATS_BOARD_ACTION = "Страница /admin → Статистика"

DIAGNOSE_LFG_LIVE = "поиск команды: карточка жива"

DIAGNOSE_LFG_GONE = "поиск команды: карточка удалена"

DIAGNOSE_LFG_OFF = "поиск команды: канал не задан"

DIAGNOSE_LFG_ACTION = "Задайте канал в /admin → Поиск команды."

DIAGNOSE_VOICE_HUB_OK = "голосовой хаб: готов"

DIAGNOSE_VOICE_HUB_ACTION = "Страница /admin → Голос · Починить хаб"

DIAGNOSE_VERIFICATION_WRITE_OK = "В канале верификации могут писать все"

DIAGNOSE_VERIFICATION_WRITE_OFF = "в канале верификации никто не может писать, ловушка не работает"

DIAGNOSE_VERIFICATION_WRITE_ACTION = "Страница /admin → Верификация · разрешите @everyone писать"


def diagnose_stats_board(status: str) -> str:
    if status == "live":
        return DIAGNOSE_STATS_BOARD_LIVE
    if status == "stale":
        return DIAGNOSE_STATS_BOARD_GONE
    return DIAGNOSE_STATS_BOARD_OFF


def diagnose_lfg(status: str) -> str:
    if status == "live":
        return DIAGNOSE_LFG_LIVE
    if status == "stale":
        return DIAGNOSE_LFG_GONE
    return DIAGNOSE_LFG_OFF


def diagnose_voice_hub(status: str) -> str:
    if status == "ok":
        return DIAGNOSE_VOICE_HUB_OK
    return f"голосовой хаб: {voice_denial_text(status)}"


def diagnose_evidence(status: str) -> str:
    if status in {"ok", "true"}:
        return DIAGNOSE_EVIDENCE_OK
    if status in {"off", "false", "no_category"}:
        return DIAGNOSE_EVIDENCE_OFF
    return f"доказательства: {evidence_denial_text(status)}"


def diagnose_evidence_archive(status: str) -> str:
    if status == "ok":
        return DIAGNOSE_EVIDENCE_ARCHIVE_OK
    return DIAGNOSE_EVIDENCE_ARCHIVE_OFF


def diagnose_youtube_ok(feeds: int, when: str) -> str:
    return f"YouTube: фидов {feeds} · проверка {when}"


def diagnose_youtube_idle(feeds: int) -> str:
    return f"YouTube: фидов {feeds} · ещё не проверяли"


def diagnose_youtube_error(feeds: int, error: str) -> str:
    return f"YouTube: фидов {feeds} · ошибка {error}"


def diagnose_quota_capped(count: int) -> str:
    if count <= 0:
        return "В лимит квот сегодня никто не упёрся"
    return f"В лимит квот сегодня упёрлись: {count}"


def diagnose_suspended_watchers(count: int) -> str:
    return f"приостановленных следящих: {count}"


def diagnose_rep_line(*, at: int, bans: int) -> str:
    return REP_DIAGNOSE.format(at=grouped(at), bans=grouped(bans))


SKELETON_HINT_MISSING = "отсутствует"

SKELETON_HINTS = {
    "missing": SKELETON_HINT_MISSING,
    "by_name": "найдено по имени, не привязано",
    "rights": "права или категория расходятся",
    "old": "не созданы вручную (with_old)",
    "members_hidden": "участники не видят канал",
    "bot_below": "роль бота ниже",
    "wrong_category": "канал в чужой категории",
}

RUNTIME_MS = "мс"


def skeleton_hint(code: str) -> str:
    return SKELETON_HINTS.get(code, code)


def diagnose_skeleton_rest(count: int) -> str:
    return f"остальные {count} — в порядке"


DIAGNOSE_READY_OK = "сервер готов к работе"

DIAGNOSE_READY_FAIL = "сервер не готов"

DIAGNOSE_READY_SHOWCASE = READY_ISSUE_PANEL_STALE

DIAGNOSE_STRUCTURE_OFF = "канал структуры не задан"


def diagnose_ready_partial(*, ok: int, total: int) -> str:
    return f"частично: работают {ok} функций из {total}"


def diagnose_ready_missing(keys: Sequence[str]) -> str:
    shown = ", ".join(keys[:4])
    if len(keys) > 4:
        shown = f"{shown}…"
    return f"нет {shown}"


def diagnose_ready_broken(keys: Sequence[str]) -> str:
    shown = ", ".join(keys[:4])
    if len(keys) > 4:
        shown = f"{shown}…"
    return f"права: {shown}"


DIAGNOSE_HOLD_FILE = "в файле"

DIAGNOSE_HOLD_MEMORY = "только память"

DIAGNOSE_HOLD_NONE = "блокировок создания нет"

DIAGNOSE_OBSERVED_NONE = "ответов Discord по созданию каналов и ролей ещё не было"


def diagnose_create_hold(bucket: str, source: str, remaining: object, *, persisted: bool) -> str:
    place = DIAGNOSE_HOLD_FILE if persisted else DIAGNOSE_HOLD_MEMORY
    return f"создание {bucket}: {source}, ещё {hold_wait(remaining)}, {place}"


def diagnose_rate_observed(
    bucket: str,
    *,
    status: int,
    scope: str,
    remaining: int | None,
    reset_after: float | None,
    age: object,
) -> str:
    scope_text = scope or "—"
    left = "—" if remaining is None else str(remaining)
    reset = "—" if reset_after is None else hold_wait(reset_after)
    return (
        f"последний {bucket}: HTTP {status}, scope {scope_text}, "
        f"remaining {left}, reset {reset}, {hold_wait(age)} назад"
    )


def traffic_rps(rps_1s: int, rps_60s: int) -> str:
    return f"запросов в секунду: {int(rps_1s)} сейчас, {int(rps_60s)} за минуту"


def traffic_invalid(count: int, budget: int) -> str:
    return f"недопустимых запросов за 10 мин: {int(count)} из {int(budget)}"


def traffic_global_429(count: int) -> str:
    return f"глобальных 429 за 10 мин: {int(count)}"


def traffic_top(kind: str, templates: tuple[str, ...]) -> str:
    if not templates:
        return f"топ маршрутов {kind}: нет"
    return f"топ маршрутов {kind}: {', '.join(templates)}"


def traffic_bucket(template: str, remaining: int, limit: int, reset_after: object) -> str:
    return f"{template}: remaining {int(remaining)} из {int(limit)}, сброс через {hold_wait(reset_after)}"


TRAFFIC_FUSE_SOFT = (
    "Недопустимых запросов к Discord уже много: бот сам сдерживает темп, чтобы не словить блок IP."
)


def traffic_profile(name: object) -> str:
    return f"профиль темпа: {name}"


def traffic_rps_goal(current: object, target: object) -> str:
    return f"глобальный темп к цели: {float(current):.1f} из {float(target):.0f} запрос/с"


def traffic_fuse(level: object) -> str:
    labels = {"ok": "норма", "soft": "мягкий", "throttle": "урезан", "stop": "остановлен"}
    key = str(level)
    return f"предохранитель недопустимых: {labels.get(key, key)}"


def traffic_lane_wait(lane: object, queue: object, p50: object, p95: object) -> str:
    return (
        f"полоса {lane}: очередь {int(queue)}, "
        f"ожидание p50 {float(p50):.2f} с, p95 {float(p95):.2f} с"
    )


def traffic_aimd(bucket: object, allowance: object, *, frozen: bool) -> str:
    extra = ", рост заморожен на сутки" if frozen else ""
    return f"оценка AIMD {bucket}: {int(allowance)} (диагностика, не квота){extra}"


def traffic_wait_progress(seconds: object) -> str:
    return f"Discord попросил подождать {hold_wait(seconds)}, создание продолжится."
