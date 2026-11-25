"""Тексты: stats."""

from __future__ import annotations

from collections.abc import Sequence

from conductor.domain.pulse import WEIGHT_LINE

from .common import grouped

STATS_TAB_NOW = "Сейчас"

STATS_TAB_OVERVIEW = "Сводка"

STATS_TAB_MEMBERS = "Участники"

STATS_TAB_CHANNELS = "Каналы"

STATS_TAB_TIES = "Связи"

STATS_WAIT = "Собираю сводку"

STATS_WAIT_BODY = "считаю по серверу"

STATS_TIES_BTN = "Связи"

STATS_PERSON_BODY = "Сводка по уже прочитанным сообщениям, ответам, упоминаниям и реакциям."

STATS_TIES_WEIGHT = "Вес: ответ ×3, упоминание ×2, реакция ×1."

STATS_TIES_EMPTY = "Пока пусто. Сбор истории с реакциями в /admin panel → Данные."

STATS_TIES_NONE = "Связей в прочитанной истории нет."

STATS_HISTORY_EMPTY = "Пусто. Соберите историю в /admin panel → Данные."

STATS_MEMBERS_EMPTY = "Пока пусто. Соберите историю в /admin panel → Данные."

STATS_CHANNELS_EMPTY = "Нет данных. Соберите историю в /admin panel → Данные."

STATS_NO_TRACE = "В прочитанной истории этого человека нет. Сначала /admin panel → Данные."

STATS_SERVER_CREATED = "Сервер создан"

STATS_LAST_CENSUS = "Последняя перепись"

STATS_HISTORY = "Прочитанная история"

STATS_COMPOSITION = "Состав"

STATS_JOINS = "Свежие входы"

STATS_TENURE = "Стаж"

STATS_JOIN_WAVES = "Волны входа"

STATS_MSG_PEAKS = "Пики сообщений"

STATS_VOICE_PEAKS = "Пики голоса, мин."

STATS_HOURS = "Часы UTC"

STATS_AUTHORS = "Уникальные авторы"

STATS_ZONE = "UTC"


def stats_authors(*, today: int, week: int, zone: str = STATS_ZONE) -> str:
    return f"сегодня ({zone}) **{today}** · 7 дней **{week}**"


def pulse_zone(zone: str = STATS_ZONE) -> str:
    return zone

STATS_DENSE = "Самые плотные связи"

STATS_TOP_MESSAGES = "Топ по сообщениям"

STATS_BY_HISTORY = "По прочитанной истории"

STATS_SENT = "Отправлено"

STATS_LINKS = "Связи"

STATS_FIRST = "Первое сообщение"

STATS_LAST = "Последнее сообщение"

STATS_WHERE = "Где писал"

STATS_WITH = "С кем связан"

STATS_NO_TRACE_FIELD = "Нет следа"

STATS_TOP_PAIRS = "Топ пар"

STATS_PARTNERS = "Партнёры"

STATS_TIES_TITLE = "Связи на сервере"


def stats_snap(when: str) -> str:
    return f"снимок {when}"


DATA_PURGE_TITLE = "Чистка"

SIGNAL_TITLE = "Сигнал пульса"

SIGNAL_LOOK = "Смотрите /stats → Сейчас и Сводка."

SIGNAL_OFF = "Сигналы пульса выключены."

SIGNAL_ON = "Сигналы пульса включены."

HISTORY_OK = "История каналов прочитана"

HISTORY_INGEST = "Соберите историю в /admin panel → Данные."


def history_unread(count: int, sample: str) -> str:
    return f"Непрочитанных каналов истории: {count}{sample}"


def history_suffix(names: Sequence[str], unread: int) -> str:
    sample = ", ".join(f"#{name}" for name in names)
    extra = f" и ещё {unread - len(names)}" if unread > len(names) else ""
    return f" ({sample}{extra})" if sample else ""


INGEST_TITLE = "Читаю историю"

INGEST_BODY = "Собираю сообщения, ответы и реакции."

INGEST_FOOTNOTE = "это может занять несколько минут"

INGEST_UNIT = "каналов"

INGEST_DONE_TITLE = "История собрана"

INGEST_NEXT = "дальше: /stats → досье и Связи"

INGEST_BUSY_TITLE = "Уже идёт"

INGEST_BUSY_BODY = "Чтение истории уже запущено. Дождитесь окончания."

INGEST_RESET = "Первый проход сбросил старые счётчики, чтобы не было двойного учёта."


def ingest_now(channel: str) -> str:
    return f"сейчас: #{channel}" if channel else INGEST_FOOTNOTE


def ingest_done_body(
    *,
    messages: int,
    channels: int,
    authors: int,
    ties: int,
    skipped: int = 0,
    reactions: int = 0,
    reset: bool = False,
    timed_out: int = 0,
) -> str:
    lines = [
        f"**{grouped(messages)}** сообщения · **{grouped(channels)}** каналов",
        f"авторов **{grouped(authors)}** · связей **{grouped(ties)}**",
    ]
    extras: list[str] = []
    if skipped:
        extras.append(f"нет доступа: {grouped(skipped)}")
    if reactions:
        extras.append(f"реакций **{grouped(reactions)}**")
    if extras:
        lines.append(" · ".join(extras))
    if reset:
        lines.append(INGEST_RESET)
    if timed_out:
        lines.append(f"Каналов с зависанием API: **{grouped(timed_out)}** — повторите сбор в /admin panel → Данные.")
    return "\n".join(lines)


PULSE_SIGNAL_VOICE = "голос"

PULSE_SIGNAL_INVERT = "выходы"

PULSE_SIGNAL_AUTHORS = "авторы"

PULSE_SIGNAL_BOOSTS = "бусты"

PULSE_ALERTS_BAD_VOICE = "Порог голоса — от 1 до 1000."

PULSE_ALERTS_BAD_LEAVES = "Порог выходов — от 1 до 1000."

PULSE_ALERTS_BAD_AUTHORS = "Процент авторов — от 1 до 100."

PULSE_ALERTS_BAD_JOINS = "Порог всплеска входов — от 1 до 1000."


PULSE_TITLE = "Сейчас на сервере"

PULSE_WAIT = "Собираю пульс"

PULSE_WAIT_BODY = "считаю состав и активность"

PULSE_UNKNOWN = "Оценки нет: мало данных за неделю."

PULSE_GATHERING_FALLBACK = "сбор"

PULSE_STAFF_TITLE = "Служебно"

PULSE_STAFF_FLOW = "Входы и выходы за 7 дней"

PULSE_STAFF_AUTHORS = "Уникальные авторы за сутки"

PULSE_STAFF_TICKETS = "Открытые тикеты"

PULSE_STAFF_CENSUS = "Возраст переписи"

PULSE_STAFF_INFLOW = "Откуда пришли за 7 дней"

PULSE_CENSUS_FRESH = "меньше часа"

PULSE_FOOTNOTE = f"как считается: {WEIGHT_LINE}"


_PULSE_BANDS = {
    "unknown": "нет оценки",
    "quiet": "тихий",
    "steady": "ровный",
    "alive": "живой",
    "peak": "пик",
}

_PULSE_FLAGS = {
    "no_census": "перепись ещё не снималась",
    "stale_census": "перепись старше 7 суток",
    "no_presence": "онлайн не считается — интент Presence выключен",
    "no_history": "нет сообщений за 7 дней",
    "no_voice_sessions": "нет закрытых голосовых сессий за 7 дней",
}


def pulse_band(band: str) -> str:
    return _PULSE_BANDS.get(band, band)


def pulse_flag(flag: str) -> str:
    return _PULSE_FLAGS.get(flag, flag)


def pulse_weights() -> str:
    return PULSE_FOOTNOTE.removeprefix("как считается: ")


def pulse_people(
    *,
    humans: int,
    bots: int,
    boosts: int,
    voice: int,
    online: int | None,
) -> str:
    line = f"Люди **{humans}** · боты **{bots}** · бусты **{boosts}** · в голосе **{voice}**"
    if online is None:
        return line
    return f"{line} · онлайн **{online}**"


def pulse_health(*, band: str, bar: str, score: int) -> str:
    return f"{band} {bar} **{score}**"


def pulse_gathering(title: str, when: str) -> str:
    return f"**Следующий сбор:** {title} {when}"


def pulse_duty(count: int) -> str:
    return f"**На смене:** {count}"


def pulse_flow(joins: int, leaves: int) -> str:
    return f"входы **{joins}** · выходы **{leaves}**"


def pulse_inflow(coded: int, vanity: int, unknown: int) -> str:
    return f"входы 7д: по рекрут-ссылкам **{coded}** · vanity **{vanity}** · без кода **{unknown}**"


def signal_voice(*, median: float, voice: int) -> str:
    return f"В час пик в голосе **{voice}**. Обычно (медиана 7д) **{median:.0f}**."


def signal_invert(*, leaves: int, threshold: int) -> str:
    return f"За сутки выходов **{leaves}** больше, чем входов, и не меньше **{threshold}**."


def signal_authors(*, authors: int, median: float, ratio: float) -> str:
    pct = max(1, round(ratio * 100))
    return f"Авторов за сутки **{authors}** — меньше {pct}% медианы 7д (**{median:.0f}**)."


def signal_boosts(*, now: int, was: int) -> str:
    return f"Бустов **{now}**, сутки назад **{was}**."


def pulse_alerts_body(rules: object) -> list[str]:
    enabled = bool(getattr(rules, "enabled", False))
    state = SIGNAL_ON if enabled else SIGNAL_OFF
    return [
        state,
        f"голос пуст при медиане ≥ **{getattr(rules, 'voice_floor', 5)}**",
        f"инверсия выходов от **{getattr(rules, 'leave_floor', 5)}**",
        f"авторы ниже **{getattr(rules, 'authors_pct', 50)}%** медианы",
        f"всплеск входов от **{getattr(rules, 'join_spike_floor', 15)}** за 10 минут",
    ]


def pulse_census_hours(hours: int) -> str:
    return f"{hours} ч"


def pulse_census_days(days: int) -> str:
    return f"{days} дн."


STATS_BOARD_TITLE = "Статистика"

STATS_BOARD_WATCHERS = "Следящие"

STATS_BOARD_MEMBERS = "Участники"

STATS_BOARD_CHANNELS = "Каналы за 7 дней"

STATS_BOARD_VOICE = "Голос за 7 дней"

STATS_BOARD_ROLES = "по ролям:"

STATS_BOARD_CHANNELS_EMPTY = "нет сообщений за неделю"

STATS_BOARD_FOOTER = "обновляется каждые 10 минут"


def stats_board_people(*, humans: int, bots: int, boosts: int) -> str:
    return f"люди **{humans}** · боты **{bots}** · бусты **{boosts}**"


def stats_board_rank_line(label: str, count: int) -> str:
    return f"{label} **{count}**"


def stats_board_role_line(role_id: int, count: int) -> str:
    return f"<@&{int(role_id)}> **{count}**"


def stats_board_channel_line(channel_id: int, messages: int, authors: int) -> str:
    return f"<#{int(channel_id)}> — **{messages}** · авторов **{authors}**"


def stats_board_voice(hours: float) -> str:
    value = max(0.0, float(hours))
    shown = str(int(value)) if value == int(value) else f"{value:.1f}"
    return f"голос **{shown}** ч"


def stats_board_tickets(count: int) -> str:
    return f"открытых тикетов **{count}**"


DATA_FORGET_TITLE = "Данные удалены"

DATA_FORGET_CONFIRM = "Удалить персональные данные?"

DATA_FORGET_HINT = (
    "Имя, тексты и связи исчезнут. Открытый тикет, комната или розыгрыш останутся "
    "без хозяина — это операции сервера, не карточка человека. "
    "Счётчики сервера без имён останутся."
)

DATA_FORGET_VERB = "Удалить"

DATA_FORGET_CANCELLED = "Удаление отменено."

DATA_FORGET_EMPTY = "Об этом человеке бот ничего не знал."

DATA_FORGET_KEPT = "Тикеты, комнаты, розыгрыши, опросы, сборы и группы остаются как операции, без id человека."


def data_forget_body(*, rows: int, cache: int) -> str:
    if rows <= 0 and cache <= 0:
        return DATA_FORGET_EMPTY
    return f"Строк в базе: {rows}. Сообщений в кэше: {cache}.\n{DATA_FORGET_KEPT}"
