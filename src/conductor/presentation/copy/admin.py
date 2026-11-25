"""Тексты: admin."""

from __future__ import annotations

from .common import DAYS_UNIT, HOURS_UNIT, MINUTES_UNIT, REP_TIER_DEFAULTS

ADMIN_ONLY = "Команды бота доступны только администраторам сервера."


ADMIN_HOME_TITLE = "Настройка"

ADMIN_HOME_BODY = "Выберите страницу. Изменения пишутся сразу."

ADMIN_PAGES = "Страница"


def admin_pages_range(first: int, last: int) -> str:
    if first >= last:
        return ADMIN_PAGES
    return f"Страницы {first}–{last}"


ADMIN_BACK = "← Страницы"

ADMIN_READY = "настроено"

ADMIN_NOT_READY = "не настроено"

ADMIN_PARTIAL = "частично готово"

ADMIN_OFF = "не используется"

ADMIN_SHOW_ISSUES = "Показать все замечания"

ADMIN_ISSUES_TITLE = "Все замечания"

ADMIN_NO_ISSUES = "Замечаний нет — страницы в порядке."

ADMIN_FIX = "Починить"

ADMIN_OPEN_ISSUE = "Открыть"

READY_ISSUE_CHANNEL_MISSING = "канал пропал из Discord"

READY_ISSUE_ROLE_MISSING = "роль не привязана или удалена"

READY_ISSUE_PANEL_STALE = "витрина не жива"

READY_ISSUE_BOT_PERMS = "боту не хватает прав на канал"

READY_ISSUE_MEMBER_PERMS = "участникам не хватает прав на канал"

READY_ISSUE_UNBOUND = "нет привязки"

READY_ISSUE_EMPTY = "ещё не заполнено"

READY_ISSUE_LABELS = {
    "channel_missing": READY_ISSUE_CHANNEL_MISSING,
    "role_missing": READY_ISSUE_ROLE_MISSING,
    "panel_stale": READY_ISSUE_PANEL_STALE,
    "bot_perms": READY_ISSUE_BOT_PERMS,
    "member_perms": READY_ISSUE_MEMBER_PERMS,
    "unbound": READY_ISSUE_UNBOUND,
    "empty": READY_ISSUE_EMPTY,
}


def admin_status_text(state: str, reason: str = "") -> str:
    labels = {
        "ready": ADMIN_READY,
        "partial": ADMIN_PARTIAL,
        "blocked": ADMIN_NOT_READY,
        "off": ADMIN_OFF,
    }
    label = labels.get(state, ADMIN_NOT_READY)
    if reason:
        return f"{label} — {reason}"
    return label

ADMIN_NO_PAGES = "Нет страниц, к которым есть доступ."

ADMIN_PAGE_GENERAL = "Общие"

ADMIN_PAGE_LOGS = "Логи"

ADMIN_PAGE_RANKS = "Ранги"

ADMIN_PAGE_PERMS = "Права"

ADMIN_PAGE_STAFFING = "Кадры"

ADMIN_PAGE_MUTE = "Модерация"

ADMIN_PAGE_QUOTAS = "Квоты"

ADMIN_PAGE_OLD = "Old"

ADMIN_PAGE_REPUTATION = "Репутация"

ADMIN_PAGE_LADDER = "Лестница"

ADMIN_PAGE_ACHIEVEMENTS = "Достижения"

ADMIN_PAGE_GIVEAWAY = "Розыгрыши"

ADMIN_PAGE_VOICE = "Голос"

ADMIN_REP_WARN = "Замечание"

ADMIN_REP_TIMEOUT = "Тайм-аут"

ADMIN_REP_MUTE = "Мут"

ADMIN_REP_KICK = "Кик"

ADMIN_REP_CAP = "Потолок часа"

ADMIN_REP_GROWTH = "Рост"

ADMIN_REP_SINNER = "Порог грешника"

ADMIN_REP_SINNER_ROLE = "Роль грешника"

ADMIN_REP_DRY = "Сухой прогон"

ADMIN_REP_DRY_SUBJECT = "сухой прогон"

ADMIN_REP_APPEAL = "Апелляция"

ADMIN_REP_APPEAL_BTN = "Изменить контакт апелляции"

ADMIN_REP_HITS = "Изменить списания"

ADMIN_REP_GROWTH_BTN = "Настроить рост"

ADMIN_REP_NAMES = "Переименовать уровни"

ADMIN_REP_NAMES_HINT = "По одной строке на уровень, сверху вниз: 0…5."

ADMIN_REP_NAMES_BAD = "Нужны ровно шесть непустых строк — по имени на уровень."

ADMIN_REP_HITS_HINT = (
    "Списания: замечание, шаг, мин, макс, мут, кик, потолок часа — числа через пробел. Диапазоны уровней не меняются."
)

ADMIN_REP_TIER = "Уровень {tier}"


def admin_rep_tier(tier: int) -> str:
    if 0 <= int(tier) < len(REP_TIER_DEFAULTS):
        return f"{int(tier)} · {REP_TIER_DEFAULTS[int(tier)]}"
    return ADMIN_REP_TIER.format(tier=tier)


def admin_rep_tier_field(*, name: str, span: str) -> str:
    return f"{name} · {span}"


ADMIN_LADDER_REGULAR = "Постоялец"

ADMIN_LADDER_RESIDENT = "Житель"

ADMIN_LADDER_FRIEND = "Друг"

ADMIN_LADDER_THRESHOLDS = "Постоялец / житель"

ADMIN_LADDER_REGULAR_BTN = "Изменить пороги: постоялец"

ADMIN_LADDER_RESIDENT_BTN = "Изменить пороги: житель"

ADMIN_LADDER_FRIEND_BTN = "Изменить пороги: друг"

ADMIN_LADDER_HINT = "0 — условие выключено."

ADMIN_ACHIEVE_USER = "Участник"

ADMIN_ACHIEVE_KEY = "Ключ"

ADMIN_ACHIEVE_COUNT = "Счётчик"

ADMIN_ACHIEVE_COUNT_BTN = "Изменить счётчик"

ADMIN_ACHIEVE_REASON = "Причина"

ADMIN_ACHIEVE_NAMES = "Переименовать ступени"

ADMIN_ACHIEVE_NAMES_HINT = "10 строк: Фиксер 1–3, Идеолог 1–3, Бдительный 1–4."

ADMIN_ACHIEVE_NAMES_BAD = "Нужны ровно десять непустых строк — по имени на ступень."

ADMIN_ACHIEVE_HINT = "Выберите участника, затем счётчик. Роль — только высшая ступень ключа."


ADMIN_GIVE_WEIGHTS = "Вес билета"

ADMIN_GIVE_WEIGHTS_BTN = "Изменить вес билетов"

ADMIN_GIVE_ROLES = "Призы"

ADMIN_VOICE_HUB = "Хаб"

ADMIN_VOICE_PRESET = "Пресет"

ADMIN_VOICE_TEMPLATE = "Шаблон имени"

ADMIN_VOICE_TEMPLATE_BTN = "Изменить шаблон имени"

ADMIN_VOICE_CLONE = "Канал-шаблон"

ADMIN_VOICE_NAME = "По имени"

ADMIN_VOICE_CLONE_OPT = "Клон"

ADMIN_VOICE_NEED_TEMPLATE = "Сначала выберите канал-шаблон."

ADMIN_VOICE_TEMPLATE_N = "Шаблон не может содержать {n}: нумерация больше не поддерживается."

ADMIN_VOICE_HEALTH = "Готовность хаба"

ADMIN_VOICE_FIX = "Починить хаб"

ADMIN_PAGE_TICKETS = "Тикеты"

ADMIN_PAGE_AUTOMOD = "Автомод"

ADMIN_PAGE_VERIFICATION = "Верификация"

ADMIN_PAGE_SELFROLES = "Роли по выбору"

ADMIN_PAGE_STATS = "Статистика"

ADMIN_PAGE_POLLS = "Опросы"

ADMIN_PAGE_YOUTUBE = "YouTube"

ADMIN_PAGE_LFG = "Поиск команды"

ADMIN_PAGE_GROUPS = "Группы"

ADMIN_PAGE_RECRUIT = "Рекрутинг"

ADMIN_PAGE_ALERTS = "Сигналы"

ADMIN_RECRUIT_CHANNEL = "Канал рекрутинга"

ADMIN_RECRUIT_HINT = "Бессрочные инвайты по умолчанию создаются в этом канале."

ADMIN_PAGE_DATA = "Данные"

ADMIN_PAGE_SHOWCASE = "Витрины"

SHOWCASE_POST = "Отправить"

SHOWCASE_REFRESH = "Обновить"

SHOWCASE_POST_MISSING = "Отправить все недостающие"

SHOWCASE_WHERE = "Канал витрины"

SHOWCASE_HINT = "Состояние читается из Discord, не из строки в базе. LIVE не ставится, если сообщение удалено."

SHOWCASE_NEED_CHANNEL = "Сначала привяжите канал витрины."

SHOWCASE_NEED_LIVE = "Нет живого сообщения — сначала отправьте витрину."

SHOWCASE_STATE_OFF = "не настроена"

SHOWCASE_STATE_MISSING = "не отправлена"

SHOWCASE_STATE_STALE = "сообщение удалено"

SHOWCASE_STATE_MOVED = "в другом канале"

SHOWCASE_STATE_LIVE = "жива"

SHOWCASE_STATES = {
    "off": SHOWCASE_STATE_OFF,
    "missing": SHOWCASE_STATE_MISSING,
    "stale": SHOWCASE_STATE_STALE,
    "moved": SHOWCASE_STATE_MOVED,
    "live": SHOWCASE_STATE_LIVE,
}

ADMIN_AUTOMOD_CHANNEL = "Канал карточки"

ADMIN_AUTOMOD_POST = "Выложить карточку"

ADMIN_AUTOMOD_RELOAD = "Перечитать"

ADMIN_AUTOMOD_HINT = "Правила правятся в data/automod.yml. Карточка только показывает и принимает заявки."

ADMIN_TICKET_CHANNEL = "Канал панели"

ADMIN_TICKET_ARCHIVE = "Архив"

ADMIN_TICKET_LOG = "Лог тикетов"

ADMIN_TICKET_CATEGORY = "Категория Discord"

ADMIN_TICKET_MODE = "Режим"

ADMIN_TICKET_MODE_CHANNEL = "канал"

ADMIN_TICKET_MODE_THREAD = "ветка"

ADMIN_TICKET_AUTHOR_CLOSE = "Автор может закрыть"

ADMIN_TICKET_POST = "Выложить панель"

ADMIN_TICKET_FORMS = "Категории"

ADMIN_TICKET_HINT = "Сначала выложите панель — появятся категории и формы."

ADMIN_TICKET_ADD = "Добавить категорию"

ADMIN_TICKET_REMOVE = "Убрать категорию"

ADMIN_TICKET_PICK = "Категория"

ADMIN_TICKET_KIND = "Тип"

ADMIN_TICKET_RANKS = "Кто видит"

ADMIN_TICKET_TOPIC = "Тема заявки"

ADMIN_TICKET_INFO = "Текст в шапке"

ADMIN_TICKET_HAS_OPEN = "Нельзя убрать: есть открытые тикеты."

ADMIN_TICKET_DUP = "Категория с таким именем уже есть."

ADMIN_TICKET_BAD_NAME = "Нужно имя категории."

ADMIN_ALERTS_STATE = "Сигналы"

def admin_toggle(on: bool, subject: str) -> str:
    """Подпись переключателя: кнопка называет действие, а не состояние (UI-KIT §1.4)."""
    return f"Выключить {subject}" if on else f"Включить {subject}"


ADMIN_ALERTS_SUBJECT = "сигналы"

ADMIN_ALERTS_HINT = (
    "Бот пишет в лог администрации, когда сервер ведёт себя необычно: пусто в голосе в пик, "
    "выходов больше входов, упало число авторов, всплеск входов. Пороги — числа, ниже или выше "
    "которых бот считает это отклонением."
)

ADMIN_GIVE_HINT = (
    "Вес билета — сколько билетов даёт роль и ступень репутации. Призы — роли, которые бот выдаёт победителю."
)

ADMIN_VOICE_HINT = (
    "Хаб — голосовой канал, вход в который создаёт человеку свою комнату. Пресет: по имени — "
    "имена по шаблону, копия — комната повторяет права и битрейт образца."
)

ADMIN_SENIOR_APPOINT_SUBJECT = "назначение следящих старшим"

ADMIN_SPECIAL_APPOINT_SUBJECT = "выдачу ранга Специального через бота"

ADMIN_TICKET_CLOSE_SUBJECT = "закрытие тикета автором"

ADMIN_NOTIFY_SUBJECT = "уведомления о структуре"


ADMIN_ALERTS_VOICE = "Порог голоса"

ADMIN_ALERTS_LEAVE = "Порог выходов"

ADMIN_ALERTS_AUTHORS = "Авторы"

ADMIN_ALERTS_JOINS = "Всплеск входов"

ADMIN_ALERTS_FLOORS = "Изменить пороги"

ADMIN_VF_CHANNEL = "Канал"

ADMIN_VF_ROLE = "Роль"

ADMIN_VF_POST = "Выложить карточки"

ADMIN_VF_HINT = "Кнопка выдаёт роль. Сообщение в канале — бан."

ADMIN_VF_SYSTEM_CHANNEL = (
    "Это канал системных сообщений Discord — в него пишет сам Discord от имени новичков. Выберите другой."
)

ADMIN_SR_MENU = "Меню"

ADMIN_SR_ADD = "Добавить роль"

ADMIN_SR_REMOVE = "Убрать"

ADMIN_SR_POST = "Выложить карточку"

ADMIN_SR_CHANNEL = "Канал карточки"

ADMIN_SR_HINT = "Селект — полный набор этого меню. Чужие роли не трогаются."

ADMIN_STATS_CHANNEL = "Канал"

ADMIN_STATS_ROLES = "Роли для подсчёта"

ADMIN_STATS_POST = "Выложить"

ADMIN_STATS_HINT = "До 15 ролей. Повтор в тот же канал правит карточку. Обновление раз в 10 минут."

ADMIN_POLLS_CHANNEL = "Канал"

ADMIN_POLLS_ROLE = "Роль упоминания"

ADMIN_POLLS_HINT = (
    "Пустая роль — тег «Опросы» из меню подписок. Без канала опрос публикуется там, откуда вызвали команду."
)

ADMIN_YT_ADD = "Добавить фид"

ADMIN_YT_REMOVE = "Убрать"

ADMIN_YT_CHECK = "Проверить сейчас"

ADMIN_YT_CHANNEL = "Канал Discord"

ADMIN_YT_ROLE = "Роль упоминания"

ADMIN_YT_FEEDS = "Фиды"

ADMIN_YT_FIELD_ID = "Id канала YouTube"

ADMIN_YT_FIELD_TEMPLATE = "Шаблон"

ADMIN_YT_BAD_ID = "Нужен id вида UC и 22 знака, не @handle."

ADMIN_YT_DUP = "Этот канал уже добавлен."

ADMIN_YT_NEED_FEED = "Сначала выберите фид."

ADMIN_YT_HINT = (
    "Id канала — UC и ещё 22 знака. Смотрите youtube.com/channel/UC…, не @handle. "
    "Первый опрос только запоминает последнее видео."
)

ADMIN_LFG_CHANNEL = "Канал"

ADMIN_LFG_TTL = "Срок карточки, мин"

ADMIN_LFG_PING = "Пауза пинга роли, мин"

ADMIN_LFG_USER = "Пауза автора, мин"

ADMIN_LFG_NUMBERS = "Сроки"

ADMIN_LFG_NUMBERS_BTN = "Изменить сроки"

ADMIN_LFG_POST = "Выложить карточку"

ADMIN_LFG_HINT = "Карточка «Ищу команду» живёт в этом канале. Автор не обязан быть в голосе."

ADMIN_GROUPS_SILENCE = "Тишина, дни"

ADMIN_GROUPS_ARCHIVE_DAYS = "Хранение архива, дни"

ADMIN_GROUPS_NUMBERS = "Сроки"

ADMIN_GROUPS_NUMBERS_BTN = "Изменить сроки"

ADMIN_GROUPS_LIMITS = "Порог и квоты"

ADMIN_GROUPS_LIMITS_BTN = "Изменить порог и квоты"

ADMIN_GROUPS_REP = "Репутация"

ADMIN_GROUPS_DAILY = "Новых в сутки"

ADMIN_GROUPS_OPEN = "Открытых сразу"

ADMIN_GROUPS_ARCHIVE_CAT = "Категория архива"

ADMIN_GROUPS_SWEEP = "Проверить архив сейчас"

ADMIN_GROUPS_HINT = "0 дней хранения — архив бессрочный. Порог и квоты действуют при создании группы."

ADMIN_GROUPS_SWEEP_DONE = "Проход архива выполнен."

ADMIN_SR_LABEL = "Подпись"

ADMIN_SR_DESC = "Описание"

ADMIN_SR_EMOJI = "Эмодзи"

ADMIN_SR_TAG = "Тег"

ADMIN_DATA_USER = "Участник"

ADMIN_DATA_EXPORT = "Выгрузить данные"

ADMIN_DATA_FORGET = "Забыть участника"

ADMIN_DATA_CENSUS = "Перепись"

ADMIN_DATA_INGEST = "Собрать историю"

ADMIN_DATA_IDLE = "нет"

ADMIN_DATA_PER_CHANNEL = "На канал (0 = все)"

ADMIN_DATA_REACTIONS = "Реакции (1/0)"

ADMIN_DATA_HINT = "Сначала выберите участника для экспорта или забывания."

ADMIN_BAD_USER = "Не понял участника."

ADMIN_NEED_USER = "Сначала выберите участника."

ADMIN_NEED_GUILD = "Нужен сервер."

ADMIN_FIELD_TIMEZONE = "Часовой пояс (дайджест)"

ADMIN_FIELD_USER_LOG = "Лог участников"

ADMIN_FIELD_USER_LOG_KEEP = "Хранить карточки, дн"

ADMIN_LOGS_KEEP_BTN = "Изменить срок хранения"

ADMIN_FIELD_ADMIN_LOG = "Лог администрации"

ADMIN_FIELD_STAFF_FEED = "Канал действий"

ADMIN_FIELD_PERMS = "Права"

ADMIN_FIELD_SENIOR_APPOINT = "Старший назначает следящего"

ADMIN_FIELD_SPECIAL_APPOINT = "Special через бота"

ADMIN_FIELD_MUTE = "Роль мута"

ADMIN_FIELD_EVIDENCE_CATEGORY = "Категория доказательств"

ADMIN_FIELD_EVIDENCE_ARCHIVE = "Архив дел"

ADMIN_FIELD_EVIDENCE_HOURS = "Напоминание, ч"

ADMIN_EVIDENCE_HOURS_BTN = "Изменить срок напоминания"

ADMIN_EVIDENCE_HINT = "Без категории команда работает как раньше: ЛС уходит, канал дела не создаётся."

ADMIN_QUOTA_TIMEOUTS = "Тайм-ауты / сутки"

ADMIN_QUOTA_BANS = "Баны / сутки"

ADMIN_QUOTA_HINT = "−1 — без лимита. Тайм-ауты считают timeout и mute, баны — ban и kick."

ADMIN_QUOTA_BAD = "Нужны два числа: тайм-ауты и баны. −1 — без лимита, иначе 0…999."

ADMIN_TZ_OTHER = "Ввести другой пояс"

ADMIN_EDIT = "Изменить"

ADMIN_IMPORT_EXAMPLE = "Импорт из примера"

ADMIN_EXPORT = "Экспорт"

ADMIN_RESTORE_DEFAULTS = "Вернуть умолчания прав"

ADMIN_RESTORE_CONFIRM = "Вернуть всем ключам умолчания схемы? Ранговые привязки не изменятся."

ADMIN_RESTORE_VERB = "Вернуть"

ADMIN_PREV = "Назад"

ADMIN_NEXT = "Дальше"

ADMIN_BAD_TZ = "Такого часового пояса нет. Нужен IANA, например Europe/Moscow."

ADMIN_BAD_CHANNEL = "Не понял канал."

ADMIN_BAD_ROLE = "Не понял роль."

ADMIN_ROLE_NONE = "свободных ролей нет — создайте новую"

ADMIN_ROLE_CREATE = "создать новую роль"

ADMIN_CHANNEL_NONE = "нет каналов в этой категории"

ADMIN_ROLE_CREATE_FAIL = "Не удалось создать роль."


def admin_role_owned(owner: str) -> str:
    return f"занято: {owner}"


ADMIN_BAD_NUMBER = "Число вне диапазона."

ADMIN_BAD_IDS = "Нужны положительные id через пробел или запятую."

ADMIN_BAD_GROUP = "Нужно ровно столько чисел, сколько полей в группе, через пробел."

ADMIN_LOG_IS_HUB = "Этот канал уже хаб голосовых комнат."

ADMIN_LOG_TITLE = "Настройка"

ADMIN_LOG_SOURCE = "панель /admin"

ADMIN_MODAL_NUMBER = "Число"

ADMIN_MODAL_TEXT = "Текст"

ADMIN_MODAL_IDS = "Список id"

ADMIN_MODAL_DURATION = "Срок"

ADMIN_MODAL_KEY = "Ключ прав"

ADMIN_LABEL_NUMBER = "Число"

ADMIN_LABEL_TEXT = "Значение"

ADMIN_LABEL_IDS = "id через пробел"

ADMIN_LABEL_DURATION = "Срок"

ADMIN_LABEL_KEY = "Ключ"

ADMIN_LABEL_RANKS = "Ранги"


def admin_ticket_questions(name: str) -> str:
    return f"Вопросы · {name}"


def admin_ticket_hint(name: str) -> str:
    return f"Подсказка · {name}"


def admin_ticket_clear(name: str) -> str:
    return f"Сбросить · {name}"


def admin_ticket_info(name: str) -> str:
    return f"Шапка · {name}"


def admin_ticket_remove(name: str) -> str:
    return f"Убрать · {name}"


def admin_changed(page: str, field: str, before: str, after: str, actor_id: int) -> str:
    return f"настройка изменена: {page} · {field} · {before} → {after} · <@{actor_id}>"


def admin_ladder_regular_line(*, messages: object, voice_minutes: object, reactions: object) -> str:
    return f"{messages} / {voice_minutes}{MINUTES_UNIT} / {reactions}"


def admin_ladder_resident_line(
    *,
    messages: object,
    voice_hours: object,
    days: object,
    strong_messages: object,
    strong_voice_hours: object,
) -> str:
    return (
        f"{messages} / {voice_hours}{HOURS_UNIT} / "
        f"{days}{DAYS_UNIT} / {strong_messages} / "
        f"{strong_voice_hours}{HOURS_UNIT}"
    )


def admin_ladder_friend_line(*, days: object, messages: object, voice_hours: object, reactions: object) -> str:
    return f"{days}{DAYS_UNIT} / {messages} / {voice_hours}{HOURS_UNIT} / {reactions}"


def admin_hidden_actions(count: int, rank: str) -> str:
    return f"скрыто действий: {count} — нужен ранг {rank}"
