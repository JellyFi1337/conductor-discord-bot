"""Тексты: helptext."""

from __future__ import annotations

from .commands import (
    CMD_ADMIN_PANEL,
    CMD_BUG,
    CMD_DIAGNOSE,
    CMD_EVENT,
    CMD_GIVEAWAY,
    CMD_GROUP,
    CMD_GROUPS,
    CMD_HELP,
    CMD_LOYALTY,
    CMD_MOD_BAN,
    CMD_MOD_HISTORY,
    CMD_MOD_KICK,
    CMD_MOD_LOCK,
    CMD_MOD_MUTE,
    CMD_MOD_PARDON,
    CMD_MOD_PURGE,
    CMD_MOD_TIMEOUT,
    CMD_MOD_UNBAN,
    CMD_MOD_UNMUTE,
    CMD_MOD_WARN,
    CMD_OWNER_APPLY,
    CMD_OWNER_EMOJIS,
    CMD_OWNER_EXPORT,
    CMD_OWNER_GRANT,
    CMD_OWNER_LIFT,
    CMD_OWNER_LIMITS,
    CMD_OWNER_OLD_IMPORT,
    CMD_OWNER_REP,
    CMD_OWNER_SEED,
    CMD_OWNER_STRIP,
    CMD_OWNER_WIPE,
    CMD_POLL_REVEAL,
    CMD_POLL_START,
    CMD_PROFILE,
    CMD_RECRUIT_LINK,
    CMD_RECRUIT_REPORT,
    CMD_REMIND,
    CMD_STATS,
    CMD_VOICE,
    CMD_WATCHERS_APPOINT,
    CMD_WATCHERS_DISMISS,
    CMD_WATCHERS_DUTY,
    CMD_WATCHERS_LIST,
    CMD_WATCHERS_NOTE,
)
from .common import CMD_ADMIN_IMPORT, RANK_CHIEF, RANK_SENIOR, RANK_SPECIAL, RANK_WATCHER, rank_label

HELP_PLACEHOLDER = "Выберите раздел"

HELP_FOOTNOTE = "карточка только вам · вкладки ниже"

HELP_TAB_MINE = "Мои команды"

HELP_TAB_MEMO = "Памятка участника"

HELP_TAB_START = "Первые шаги"

HELP_MARK_BUTTON = "▸ кнопка"

HELP_EMPTY = "Для этого ранга команд нет."

HELP_PAGE_PREV = "Назад"

HELP_PAGE_NEXT = "Дальше"

HELP_WHO_ALL = "все"

HELP_WHO = {
    "all": "все",
    "owner": "владелец приложения",
    "bot_owner": "владелец бота",
    "duty": f"своя — {RANK_WATCHER}; чужая — {RANK_CHIEF}+",
    "appoint": f"{RANK_CHIEF}+; {RANK_SENIOR} — если включено в политике",
    "room": "владелец комнаты",
    "ticket_close": "автор или следящий",
    "ticket_staff": "взявший тикет",
    "ticket_author": "автор тикета",
    "rep_show": "своя — все; чужая — следящий+",
}

HELP_SECTION_LABELS = {
    "start": "Первые шаги",
    "setup": "Настройка",
    "stats": "Статистика",
    "loyalty": "Преданность",
    "staff": "Следящие",
    "voice": "Голос",
    "tickets": "Тикеты",
    "mod": "Модерация",
    "reputation": "Репутация",
    "community": "Сборы и розыгрыши",
    "life": "Быт",
    "data": "Данные",
}

HELP_START = (
    "Полный текст — в инструкции по настройке.\n"
    "На обслуживаемом сервере первый шаг — `/diagnose` → Настройка без красного.\n"
    "1. Пригласите нужного бота (основного или тестового) "
    "с правами из списка REQUIRED_PERMISSIONS. Роль бота выше ролей каркаса.\n"
    "2. `/admin` → Каркас → Собрать сервер. Свои каналы — сначала Сопоставить существующие. "
    f"Ранги: {RANK_SPECIAL}, {RANK_CHIEF}, {RANK_SENIOR}, {RANK_WATCHER}.\n"
    "3. `/admin` → Каркас → Витрины → Отправить все недостающие.\n"
    "4. Напишите правила в #правила и добавьте свои чаты в категорию «Сервер».\n"
    "5. `/diagnose` → Готовность: «сервер готов к работе». "
    "По желанию: Old через `/owner seed` с флагом with_old. "
    "Правила автомода — файл data/automod.yml, кнопка «Перечитать» в #автомод.\n"
    "Вывод каталожных ботов (DSMonitoring, Server Monitoring):\n"
    "1. Выгнать обоих ботов и снять их интеграции в настройках сервера.\n"
    "2. Закрыть каналы «бамп» / «мониторинг» или удалить.\n"
    "3. Убрать /like и ссылки площадок из правил.\n"
    "4. Сутки смотреть `/diagnose` и сигналы в `/admin panel` → Сигналы.\n"
    "Команд /like больше нет — вместо /info — /stats → Сейчас.\n"
    "Дальше откройте раздел в списке ниже."
)

HELP_INTRO = {
    "setup": "Сначала /admin → Каркас → Собрать сервер, потом витрины и /diagnose → Готовность.",
    "stats": "Перепись нужна до ролей Old и досье. Команд /like и /bump больше нет — вместо /info — /stats → Сейчас.",
    "loyalty": "Одна роль Old на человека. Выдача только через мастер /loyalty.",
    "staff": "Ранговые роли меняет только брокер.",
    "voice": "Сначала хаб. Панель живёт в комнате. Владелец получает права на канал.",
    "tickets": "Панель в #тикеты. Аноним — через ЛС. Баг и предложение видит special.",
    "mod": "Причина не короче 8 символов. Досье — правый клик по человеку.",
    "reputation": "Старт 50, шесть уровней. Счёт свой виден всем. Чужой — следящим.",
    "community": "Розыгрыш и сбор — в этом канале. Опрос уходит в #опросы.",
    "life": "Личное доступно всем. Роли — в #роли. Напоминание — /remind.",
    "data": "Forget — один человек в /admin → Данные. Wipe — секция этого сервера, только владелец бота.",
}


def help_title(section: str) -> str:
    return f"Справка · {section}"


def help_who_from(rank: str) -> str:
    return f"{rank_label(rank)}+"


def help_command_line(path: str, blurb: str, who: str) -> str:
    return f"**{path}** — {blurb} · {who}"


def help_page_label(page: int, pages: int) -> str:
    return f"стр. {page}/{pages}"


HELP_BLURB = {
    "/help": CMD_HELP,
    "/diagnose": CMD_DIAGNOSE,
    "/admin panel": CMD_ADMIN_PANEL,
    "/admin import": CMD_ADMIN_IMPORT,
    "/stats": CMD_STATS,
    "/loyalty": CMD_LOYALTY,
    "/watchers list": CMD_WATCHERS_LIST,
    "/watchers appoint": CMD_WATCHERS_APPOINT,
    "/watchers dismiss": CMD_WATCHERS_DISMISS,
    "/watchers duty": CMD_WATCHERS_DUTY,
    "/watchers note": CMD_WATCHERS_NOTE,
    "/voice": CMD_VOICE,
    "/mod warn": CMD_MOD_WARN,
    "/mod timeout": CMD_MOD_TIMEOUT,
    "/mod mute": CMD_MOD_MUTE,
    "/mod unmute": CMD_MOD_UNMUTE,
    "/mod history": CMD_MOD_HISTORY,
    "/mod kick": CMD_MOD_KICK,
    "/mod ban": CMD_MOD_BAN,
    "/mod unban": CMD_MOD_UNBAN,
    "/mod purge": CMD_MOD_PURGE,
    "/mod lock": CMD_MOD_LOCK,
    "/mod pardon": CMD_MOD_PARDON,
    "/profile": CMD_PROFILE,
    "/giveaway": CMD_GIVEAWAY,
    "/poll start": CMD_POLL_START,
    "/poll deanonymize": CMD_POLL_REVEAL,
    "/event": CMD_EVENT,
    "/group": CMD_GROUP,
    "/groups": CMD_GROUPS,
    "/bug": CMD_BUG,
    "/remind": CMD_REMIND,
    "/recruit link": CMD_RECRUIT_LINK,
    "/recruit report": CMD_RECRUIT_REPORT,
    "/owner wipe": CMD_OWNER_WIPE,
    "/owner strip": CMD_OWNER_STRIP,
    "/owner grant": CMD_OWNER_GRANT,
    "/owner old-import": CMD_OWNER_OLD_IMPORT,
    "/owner seed": CMD_OWNER_SEED,
    "/owner lift": CMD_OWNER_LIFT,
    "/owner rep": CMD_OWNER_REP,
    "/owner emojis": CMD_OWNER_EMOJIS,
    "/owner limits": CMD_OWNER_LIMITS,
    "/owner export": CMD_OWNER_EXPORT,
    "/owner apply": CMD_OWNER_APPLY,
}
