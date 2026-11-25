"""Каталог пользовательских строк.

Тон вычитывается здесь, а не по cog'ам. Второй язык — второй модуль рядом, не правки команд."""

from __future__ import annotations

from datetime import datetime

from conductor.domain.seed import LAYOUT
from conductor.presentation.icons import RANK_EMOJI, marked

RANK_WATCHER = LAYOUT.role_watcher

RANK_SENIOR = LAYOUT.role_senior

RANK_CHIEF = LAYOUT.role_chief

RANK_SPECIAL = LAYOUT.role_special


YES = "да"

NO = "нет"

ALL = "все"


def yn(value: bool) -> str:
    return YES if value else NO


CMD_IMPORT = "Загрузить права из YAML-файла в базу сервера"

CMD_ADMIN_IMPORT = CMD_IMPORT

TABS = "Вкладка"

STATS_TABS = TABS

PROFILE_TABS = TABS

REP_TIER_DEFAULTS = (
    "Изгой",
    "Подозрительный",
    "Обычный",
    "Надёжный",
    "Уважаемый",
    "Образцовый",
)

LOYALTY_TABS = TABS

CENSUS_NEVER = "ещё не снималась"

STATS_CENSUS_NEVER = CENSUS_NEVER


GUILD_IGNORED = "Бот в режиме теста и не обслуживает этот сервер."


# --- Архетипы G ---

EMPTY_TITLE = "Ничего не нашлось"

DENIED_TITLE = "Не хватает прав"

FAILURE_TITLE = "Не получилось"

FAILURE_BODY = "Discord не ответил вовремя. Повторите через минуту."

FAILURE_LOGGED = "записан в лог бота"

CONFIRM_CANCEL = "Отмена"

WAIT_TITLE = "Подождите"

WAIT_BODY = "Слишком много нажатий. Повторите через минуту."

STALE_CARD = "Карточка устарела"

STALE_BODY = "Этот ресурс закрыт или панель больше не действует."

PANEL_GUILD_ONLY = "Эта панель работает только на сервере."

HIERARCHY_DENIED = "Нельзя применить действие к равному или старшему."

FORGED_REF_DENIED = "Эта кнопка принадлежит другому ресурсу."


# --- /diagnose ---
MODE_LIVE = "Основной бот: только серверы из своего списка"

MODE_LIVE_ACTION = "С любого другого сервера этот бот выйдет сам. Тестовый — отдельное приложение."

MODE_TEST = "Тестовый бот: только серверы из своего списка"

MODE_TEST_ACTION = "С любого другого сервера этот бот выйдет сам. Основной — отдельное приложение."

MODE_GUILD_IGNORED = "Этот сервер не в GUILD_IDS этого бота"

MODE_GUILD_IGNORED_ACTION = "Добавьте id в список серверов этого процесса и перезапустите."

LIMIT_BYPASS_TITLE = "Обход лимита"

LIMIT_BYPASS_SOURCE = "логи администрации"


def limit_bypass_body(user_id: int, what: str) -> str:
    return f"<@{int(user_id)}> обошёл лимит: {what}"


MEMBERS_PARTIAL_ACTION = "Проверьте, что Server Members Intent включён в портале приложений, затем перезапустите бота."


def members_partial(loaded: int, total: int) -> str:
    return f"Состав не полный: {loaded} из {total}"


def listing_page(page: int, pages: int) -> str:
    return f"страница {page}/{pages}"


ROSTER_ALL = "Весь список"

ROSTER_DOWNLOAD = "Скачать списком"

ROSTER_PREV = "←"

ROSTER_NEXT = "→"

ROSTER_FILE = "spisok.txt"

ROSTER_EMPTY = "Список пуст."


def roster_more(extra: int) -> str:
    return f"и ещё {int(extra)}"


def upload_part(index: int, total: int) -> str:
    return f"часть {index} из {total}"


def denied_need(need: str) -> str:
    return f"нужно: {need}"


_DENIED_ACTORS = {
    "watcher": "следящие",
    "senior": "старшие следящие",
    "chief": "главные следящие",
}


def denied_actors(rank: str) -> str:
    if rank == "special":
        return f"Это делает {RANK_SPECIAL}."
    who = _DENIED_ACTORS.get(rank)
    if who is None:
        return CONFIG_DENIED
    return f"Это делают {who} и выше."


def failure_footnote(code: str) -> str:
    return f"код {code} · {FAILURE_LOGGED}"


PROGRESS_COUNT = "Сообщений"


def grouped(value: int) -> str:
    return f"{value:,}".replace(",", " ")


CAPACITY_LIMITER_ACTION = "Лимит 50 обновлений в минуту на сервер. Подождите и повторите."


def capacity_cache(*, hits: int, misses: int, size: int, messages: int) -> str:
    return f"Кэш: попадания {hits} · промахи {misses} · записи {size} · сообщений {messages}"


def capacity_cache_occupancy(items: tuple[tuple[str, int, int], ...] | list[tuple[str, int, int]]) -> str:
    parts = [f"{name} {size}/{cap}" for name, size, cap in items]
    return "Занятость кэшей: " + " · ".join(parts)


def purge_report(rows: int, tables: int) -> str:
    return f"чистка: {rows} строк из {tables} таблиц"


def capacity_limits(*, depth: int, limit: int, waiting: int, trips: int) -> str:
    return f"Исходящие Discord {depth} из {limit} (ждут {waiting}) · лимитер нажатий сработал {trips}"


def copy_presence_on() -> str:
    return "Presence есть"


def copy_presence_off() -> str:
    return "Presence выключен"


NEVER = "ещё не было"

DIAGNOSE_PULSE_ALERTS_NEVER = NEVER

PULSE_CENSUS_NEVER = CENSUS_NEVER

PULSE_NEVER = NEVER

CMD_CONFIG_IMPORT = CMD_IMPORT

CONFIG_DENIED = "Нет ключа для этого действия."

ADMIN_REP_RANGE = "{low}–{high}"

DURATION_BAD = "Не понял срок. Пример: 30м, 2ч, 1д или число минут."

ADMIN_BAD_DURATION = DURATION_BAD


RANK_NONE = "нет ранга"


RANK_LABELS = {
    "none": RANK_NONE,
    "watcher": RANK_WATCHER,
    "senior": RANK_SENIOR,
    "chief": RANK_CHIEF,
    "special": RANK_SPECIAL,
}


def rank_label(rank: str) -> str:
    return marked(RANK_EMOJI.get(rank, ""), RANK_LABELS.get(rank, rank))


def forged_ref_log(custom_id: str) -> str:
    return f"Попытка нажать кнопку с чужим идентификатором: `{custom_id}`."


STAGE_CHANNEL = "Создаю канал"

VOICE_STAGE_CHANNEL = STAGE_CHANNEL

RENAME_LABEL = "Новое имя"

VOICE_NAME_LABEL = RENAME_LABEL


def rename_deferred(until: datetime) -> str:
    return f"имя обновится <t:{int(until.timestamp())}:R>"


TICKET_RENAME_LABEL = RENAME_LABEL

TICKET_STAGE_CHANNEL = STAGE_CHANNEL


def _ru_days(count: int) -> str:
    value = abs(int(count))
    mod10 = value % 10
    mod100 = value % 100
    if mod10 == 1 and mod100 != 11:
        word = "день"
    elif mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}:
        word = "дня"
    else:
        word = "дней"
    return f"{value} {word}"


MOD_DURATION_BAD = DURATION_BAD

CANCELLED = "Отменено."

MOD_CANCELLED = CANCELLED


def format_mod_duration(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts)


CONFIG_BAD = "Значение вне допустимого диапазона."

REP_CONFIG_BAD = CONFIG_BAD

LADDER_CONFIG_BAD = CONFIG_BAD

GIVEAWAY_CONFIG_BAD = CONFIG_BAD


def _or_parts(*pairs: tuple[int, str]) -> str:
    return " или ".join(f"{count} {unit}" for count, unit in pairs if count > 0)


def _and_parts(*pairs: tuple[int, str]) -> str:
    return " и ".join(f"{count} {unit}" for count, unit in pairs if count > 0)


OWNER_CANCELLED = CANCELLED

PROGRESS_UNIT_MEMBERS = "участников"


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

GAME_FALLBACK = "игра"

UNKNOWN = "неизвестно"

MINUTES_UNIT = " мин"

HOURS_UNIT = " ч"

DAYS_UNIT = " дн. "


def minutes_short(value: int) -> str:
    return f"{int(value)}м"


def per_minutes(count: object, minutes: object) -> str:
    return f"{count} / {minutes}{MINUTES_UNIT}"


HOLD_ACTION_CHANNEL = "создавать каналы"

HOLD_ACTION_ROLE = "создавать роли"

HOLD_ACTION_EMOJI = "создавать эмодзи"

HOLD_ACTION_DELETE = "удалять каналы"

HOLD_DISCORD = "Discord ограничил {action} на этом сервере. Повтор — примерно через {wait}."

HOLD_LIBRARY = "Discord просит делать {action} реже. Окно освободится через {wait}."

HOLD_SHARED = "Discord временно перегружен на этом действии. Повтор через {wait}."

HOLD_LOCAL = "Бот держит выученный темп создания: повтор через {wait}."

HOLD_TEMPLATES = {
    "discord": HOLD_DISCORD,
    "library": HOLD_LIBRARY,
    "shared": HOLD_SHARED,
    "local": HOLD_LOCAL,
}

HOLD_CAP_CHANNEL = "На сервере уже 500 каналов. Discord больше не создаст новый."

HOLD_CAP_ROLE = "На сервере уже 250 ролей. Discord больше не создаст новую."

HOLD_ACTIONS = {
    "channel": HOLD_ACTION_CHANNEL,
    "role": HOLD_ACTION_ROLE,
    "emoji": HOLD_ACTION_EMOJI,
    "delete": HOLD_ACTION_DELETE,
}


def hold_action(bucket: str) -> str:
    return HOLD_ACTIONS.get(str(bucket), HOLD_ACTION_CHANNEL)


def hold_wait(seconds: object) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        value = 0.0
    if value >= 3600:
        hours = max(1, int(round(value / 3600)))
        return f"{hours} ч"
    if value >= 60:
        minutes = max(1, int(round(value / 60)))
        return f"{minutes} мин"
    if value <= 0:
        return "несколько секунд"
    seconds_i = max(1, int(round(value)))
    return f"{seconds_i} с"


def hold_text(source: str, seconds: float, *, action: str, route: str = "") -> str:
    if route == "guild_channel_cap":
        return HOLD_CAP_CHANNEL
    if route == "guild_role_cap":
        return HOLD_CAP_ROLE
    template = HOLD_TEMPLATES.get(str(source), HOLD_LIBRARY)
    return template.format(action=action, wait=hold_wait(seconds))
