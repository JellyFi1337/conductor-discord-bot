"""Тексты: social."""

from __future__ import annotations

from collections.abc import Sequence

ACHIEVEMENT_DEFAULTS = {
    "fixer": ("Фиксер 1", "Фиксер 2", "Фиксер 3"),
    "ideologist": ("Идеолог 1", "Идеолог 2", "Идеолог 3"),
    "vigilant": ("Бдительный 1", "Бдительный 2", "Бдительный 3", "Бдительный 4"),
}

ACHIEVEMENT_KEYS = {
    "fixer": "Фиксер",
    "ideologist": "Идеолог",
    "vigilant": "Бдительный",
}

ACHIEVEMENT_STAR = "★ Достижение: {name}"

ACHIEVEMENT_GRANT_BODY = "Новая ступень. +5 к репутации."

ACHIEVEMENT_EMPTY = "Пока нет прогресса."

ACHIEVEMENT_MAX = "максимум"

ACHIEVEMENT_UNTIL = "до {name}"

ACHIEVEMENT_FOOTNOTE = "Только высшая роль ключа. Репутацию за ступень не отбираем."

ACHIEVEMENT_ROLE_GONE = "Достижения: роль удалена с сервера"

ACHIEVEMENT_STALE_ACTION = "Роль удалили или id устарел. Привяжите заново: /admin panel → Достижения."


def achievement_key(key: str) -> str:
    return ACHIEVEMENT_KEYS.get(key, key)


def achievement_star(name: str) -> str:
    return ACHIEVEMENT_STAR.format(name=name)


def achievement_until(name: str) -> str:
    return ACHIEVEMENT_UNTIL.format(name=name)


def achievement_grant_body(user_id: int | None = None) -> str:
    if user_id is None:
        return ACHIEVEMENT_GRANT_BODY
    return f"{ACHIEVEMENT_GRANT_BODY}\n<@{int(user_id)}>"


YOUTUBE_CHECK_IDLE = "Новых видео нет."

YOUTUBE_CHECK_SEEDED = "Последнее видео запомнено, старые не публикуем."


def youtube_feed_line(title: str, yt_id: str, channel_id: int | None, error: str | None) -> str:
    name = title or yt_id
    dest = f"<#{int(channel_id)}>" if channel_id else "—"
    if error:
        return f"{name} · {dest} · {error}"
    return f"{name} · {dest}"


def youtube_check_posted(count: int) -> str:
    return f"Опубликовано: {count}"


def youtube_check_error(error: str) -> str:
    return f"YouTube недоступен: {error}"


GIVEAWAY_MODAL_TITLE = "Новый розыгрыш"

GIVEAWAY_FIELD_PRIZE = "Приз"

GIVEAWAY_FIELD_DURATION = "Срок"

GIVEAWAY_FIELD_WINNERS = "Победителей"

GIVEAWAY_DURATION_PLACEHOLDER = "30м, 1ч или 2д"

GIVEAWAY_JOIN = "Участвую"

GIVEAWAY_END = "Завершить"

GIVEAWAY_REROLL = "Перевыбрать"

GIVEAWAY_WINNERS_N = "Победителей"

GIVEAWAY_UNTIL = "Итог"

GIVEAWAY_ENTRIES = "Участников"

GIVEAWAY_WINNERS = "Победители"

GIVEAWAY_TERMS = "Условия"

GIVEAWAY_WEIGHT = "Вес билета"

GIVEAWAY_CONSOLATION = "Утешение"

GIVEAWAY_UNFIT = "Вы не подходите"

GIVEAWAY_NONE = "никого"

GIVEAWAY_OVER = "розыгрыш окончен"

GIVEAWAY_CANCELLED = "розыгрыш отменён"

GIVEAWAY_PICKING = "Выбираю…"

GIVEAWAY_DRUM = "Барабанная дробь"

GIVEAWAY_DONE_TITLE = "Розыгрыш окончен"

GIVEAWAY_REROLL_HINT = "перевыбор доступен 24 часа"

GIVEAWAY_JOINED = "Вы участвуете."

GIVEAWAY_ALREADY = "Вы уже участвуете."

GIVEAWAY_BOT = "Боты не участвуют в розыгрыше."

GIVEAWAY_BAD_SPEC = "Проверьте приз, срок и число победителей."

GIVEAWAY_BAD_ROLE = "Эту роль нельзя ставить призом."

GIVEAWAY_NO_CHANNEL = "Розыгрыш публикуется только в текстовый канал."

GIVEAWAY_NO_OPEN = "В этом канале нет открытого розыгрыша."

GIVEAWAY_NO_ENDED = "В этом канале нет завершённого розыгрыша."

GIVEAWAY_CONFIRM = "Отменить розыгрыш? Участники не получат приз."

GIVEAWAY_CANCEL_VERB = "Отменить"

GIVEAWAY_CANCEL_OK = "Розыгрыш отменён."

GIVEAWAY_CANCEL_ABORT = "Розыгрыш продолжается."

GIVEAWAY_ENDED_OK = "Розыгрыш завершён."

GIVEAWAY_REROLL_OK = "Победители переизбраны."

GIVEAWAY_REROLL_EMPTY = "Некого выбирать: подходящих участников не осталось."

GIVEAWAY_REROLL_LATE = "Перевыбор доступен только 24 часа после итога."

GIVEAWAY_ALLOW_OK = "Роль добавлена в белый список призов."

GIVEAWAY_DENY_OK = "Роль убрана из белого списка призов."

GIVEAWAY_STARTED = "Розыгрыш опубликован в этот канал."

GIVEAWAY_STRANGER = "Завершать, отменять и перевыбирать может хост или ключ giveaway.manage."

GIVEAWAY_CONFIG_OK = "Формула веса билета сохранена."

GIVEAWAY_WEIGHT_OFF = "выключен"

GIVEAWAY_NEED_ROLE = "нужна роль {role}"

GIVEAWAY_NEED_MESSAGES = "сообщений {have} из {need}"

GIVEAWAY_NEED_ACCOUNT = "аккаунту {have} дн., нужно {need}"

GIVEAWAY_NEED_TENURE = "на сервере {have} дн., нужно {need}"

GIVEAWAY_NEED_SINNER = "грешникам нельзя"

GIVEAWAY_NEED_MIN_TIER = "уровень 0 не проходит"

GIVEAWAY_NEED_VOICE = "нужно быть в голосовом"


def giveaway_join_label(count: int) -> str:
    return f"{GIVEAWAY_JOIN} · {count}"


def giveaway_host_line(user_id: int) -> str:
    return f"провёл <@{user_id}>"


def giveaway_finished_footnote(host_id: int) -> str:
    return f"{giveaway_host_line(host_id)} · {GIVEAWAY_REROLL_HINT}"


def giveaway_winners_line(user_ids: Sequence[int]) -> str:
    if not user_ids:
        return GIVEAWAY_NONE
    return " · ".join(f"<@{user_id}>" for user_id in user_ids)


def giveaway_terms(rules: object) -> str:
    parts: list[str] = []
    role_id = getattr(rules, "require_role_id", None)
    if role_id:
        parts.append(f"роль <@&{int(role_id)}>")
    messages = int(getattr(rules, "min_messages", 0) or 0)
    if messages:
        parts.append(f"от {messages} сообщений")
    account = int(getattr(rules, "min_account_days", 0) or 0)
    if account:
        parts.append(f"аккаунту больше {account} дней")
    tenure = int(getattr(rules, "min_tenure_days", 0) or 0)
    if tenure:
        parts.append(f"стаж {tenure} дн. на сервере")
    if bool(getattr(rules, "min_tier", False) or getattr(rules, "not_sinner", False)):
        parts.append("не изгой")
    if bool(getattr(rules, "in_voice", False)):
        parts.append("сейчас в голосовом")
    return " · ".join(parts)


def giveaway_weight_formula(rules: object) -> str:
    activity = ""
    if bool(getattr(rules, "weight", False)):
        messages = int(getattr(rules, "messages_per_ticket", 50) or 50)
        voice = int(getattr(rules, "voice_minutes_per_ticket", 30) or 30)
        cap = int(getattr(rules, "max_tickets", 10) or 10)
        activity = f"1 + ⌊сообщений / {messages}⌋ + ⌊минут войса / {voice}⌋, не больше {cap}"
    if activity:
        return activity
    return GIVEAWAY_WEIGHT_OFF


def giveaway_consolation_live(rules: object) -> str:
    count = int(getattr(rules, "consolation_count", 0) or 0)
    if count <= 0:
        return ""
    bits: list[str] = [f"{count} места"]
    role_id = getattr(rules, "consolation_role_id", None)
    if role_id:
        bits.append(f"роль <@&{int(role_id)}>")
    text = str(getattr(rules, "consolation_text", "") or "").strip()
    if text:
        bits.append(text)
    return " · ".join(bits)


def giveaway_consolation_line(user_ids: Sequence[int], *, text: str = "", role_id: int | None = None) -> str:
    names = giveaway_winners_line(user_ids)
    extra: list[str] = [names]
    if role_id:
        extra.append(f"роль <@&{int(role_id)}>")
    cleaned = text.strip()
    if cleaned:
        extra.append(cleaned)
    return " · ".join(extra)


def giveaway_gap_text(gap: object) -> str:
    kind = str(getattr(gap, "kind", "") or "")
    if kind == "role":
        role_id = getattr(gap, "role_id", None)
        role = f"<@&{int(role_id)}>" if role_id else "нужная"
        return GIVEAWAY_NEED_ROLE.format(role=role)
    if kind == "messages":
        return GIVEAWAY_NEED_MESSAGES.format(have=getattr(gap, "have", 0), need=getattr(gap, "need", 0))
    if kind == "account":
        return GIVEAWAY_NEED_ACCOUNT.format(have=getattr(gap, "have", 0), need=getattr(gap, "need", 0))
    if kind == "tenure":
        return GIVEAWAY_NEED_TENURE.format(have=getattr(gap, "have", 0), need=getattr(gap, "need", 0))
    if kind in {"sinner", "min_tier"}:
        return GIVEAWAY_NEED_MIN_TIER
    if kind == "voice":
        return GIVEAWAY_NEED_VOICE
    return kind


def giveaway_unfit(gaps: Sequence[object]) -> str:
    lines = [giveaway_gap_text(gap) for gap in gaps]
    return "\n".join(f"• {line}" for line in lines if line) or GIVEAWAY_UNFIT


def giveaway_config_body(messages: int, voice: int, cap: int) -> str:
    return f"1 + ⌊сообщений / {messages}⌋ + ⌊минут войса / {voice}⌋, не больше {cap}"


POLL_KIND_PUBLIC = "обычный"

POLL_KIND_ANONYMOUS = "анонимный"

POLL_KIND_HIDDEN = "скрытый до конца"

POLL_TYPE = "Тип"

POLL_UNTIL = "До"

POLL_VOTES = "Голоса"

POLL_CHANGE = "Смена голоса"

POLL_OPTIONS = "Варианты"

POLL_CLOSE = "Закрыть"

POLL_PICK = "Выберите вариант"

POLL_OVER = "опрос закрыт"

POLL_REMOVED = "опрос удалён"

POLL_MODAL_TITLE = "Новый опрос"

POLL_FIELD_QUESTION = "Вопрос"

POLL_FIELD_OPTIONS = "Варианты, каждый с новой строки"

POLL_FIELD_DURATION = "Срок"

POLL_DURATION_PLACEHOLDER = "30m · 2h · 1d"

POLL_JOINED = "Голос учтён."

POLL_CHANGED = "Голос изменён."

POLL_ALREADY = "Вы уже голосовали за этот вариант."

POLL_LOCKED = "Смена голоса в этом опросе выключена."

POLL_BOT = "Боты не голосуют."

POLL_BAD_SPEC = "Нужны вопрос, срок и от 2 до 10 вариантов."

POLL_NO_CHANNEL = "Опрос публикуется только в текстовый канал."

POLL_NO_OPEN = "В этом канале нет открытого опроса."

POLL_NO_ANON = "В этом канале нет анонимного опроса."

POLL_STILL_OPEN = "Сначала закройте опрос."

POLL_NOT_ANON = "Раскрывать можно только анонимный опрос."

POLL_REVEAL_ALREADY = "Этот опрос уже раскрыт."

POLL_STARTED = "Опрос опубликован в этот канал."

POLL_CLOSED_OK = "Опрос закрыт."

POLL_DELETED_OK = "Опрос удалён."

POLL_REVEAL_OK = "Голоса раскрыты в канале опроса."

POLL_STRANGER = "Закрывать и удалять может автор или ключ poll.manage."

POLL_CONFIRM_DELETE = "Удалить опрос? Карточка исчезнет из канала."

POLL_DELETE_VERB = "Удалить"

POLL_DELETE_ABORT = "Опрос остаётся."

POLL_REVEAL_TITLE = "Опрос раскрыт"

POLL_REVEAL_NOTICE = "Анонимный опрос раскрыт. Ниже — кто как голосовал."

POLL_REVEAL_LOG = "раскрыл анонимный опрос"

POLL_DONE_TITLE = "Опрос"

POLL_HOST = "задал"


def poll_kind_label(kind: object) -> str:
    value = str(getattr(kind, "value", kind) or "")
    if value == "anonymous":
        return POLL_KIND_ANONYMOUS
    if value == "hidden":
        return POLL_KIND_HIDDEN
    return POLL_KIND_PUBLIC


def poll_host_line(user_id: int) -> str:
    return f"{POLL_HOST} <@{user_id}>"


def poll_live_footnote(host_id: int, ping_role_id: int | None = None) -> str:
    host = poll_host_line(host_id)
    if not ping_role_id:
        return host
    return f"<@&{int(ping_role_id)}> · {host}"


def poll_started_link(url: str) -> str:
    return f"Опрос опубликован: {url}"


def poll_option_line(label: str, count: int, voter_ids: Sequence[int] = ()) -> str:
    if voter_ids:
        names = " · ".join(f"<@{user_id}>" for user_id in voter_ids)
        return f"• {label} — {count} · {names}"
    return f"• {label} — {count}"


def poll_reveal_log(actor_id: int, question: str, votes: int) -> str:
    return f"<@{actor_id}> {POLL_REVEAL_LOG} «{question}» · {votes} голосов"


EVENT_WHEN = "Когда"

EVENT_SEATS = "Мест"

EVENT_GOING = "Иду"

EVENT_MAYBE = "Может"

EVENT_CANT = "Не могу"

EVENT_WAIT = "лист ожидания"

EVENT_LIST = "Список"

EVENT_PING = "Пинг идущих"

EVENT_VOICE = "Открыть войс"

EVENT_CANCEL = "Отменить"

EVENT_HERE = "На месте"

EVENT_OVER = "сбор отменён"

EVENT_MODAL_TITLE = "Новый сбор"

EVENT_FIELD_TITLE = "Название"

EVENT_FIELD_WHEN = "Когда"

EVENT_FIELD_SEATS = "Мест (0 — без ограничения, до 10 000)"

EVENT_WHEN_PLACEHOLDER = "2h · 1d · 2026-09-12 18:00"

EVENT_SEATED = "Вы в списке идущих."

EVENT_WAITLISTED = "Мест нет — вы в листе ожидания."

EVENT_SET = "Ответ записан."

EVENT_ALREADY = "Так вы уже ответили."

EVENT_CHECKED = "Чек-ин принят."

EVENT_EARLY = "Чек-ин откроется в день события."

EVENT_NOT_GOING = "Чек-ин только для тех, кто в списке идущих."

EVENT_BOT = "Боты не записываются на сбор."

EVENT_BAD_SPEC = "Проверьте название, срок и число мест."

EVENT_NO_CHANNEL = "Сбор публикуется только в текстовый канал."

EVENT_NO_OPEN = "В этом канале нет открытого сбора."

EVENT_STARTED = "Сбор опубликован в этот канал."

EVENT_CANCEL_OK = "Сбор отменён."

EVENT_CANCEL_ABORT = "Сбор продолжается."

EVENT_CONFIRM_CANCEL = "Отменить сбор? Записавшиеся не получат пинг."

EVENT_STRANGER = "Отменять сбор может организатор или ключ event.manage."

EVENT_PING_OK = "Пинг отправлен только идущим."

EVENT_PING_EMPTY = "Некого пинговать: идущих нет."

EVENT_PING_WAIT = "Пинг идущих можно повторить через 10 минут."

EVENT_PING_TITLE = "Сбор"

EVENT_LIST_PICK = "Откройте идущих или лист ожидания."

EVENT_VOICE_OK = "Временная комната открыта на организатора."

EVENT_VOICE_EXISTS = "У организатора уже есть временная комната."

EVENT_NO_HUB = "Нет хаба войса — сначала настройте его."

EVENT_DONE_TITLE = "Сбор"

EVENT_HOST = "организатор"

EVENT_CHECKIN_OPEN = "чек-ин открыт"

EVENT_CHECKIN_WAIT = "чек-ин откроется"

EVENT_WAITLIST_HEAD = "Лист ожидания"

EVENT_CHECKED_HEAD = "На месте"


def event_host_line(user_id: int) -> str:
    return f"{EVENT_HOST} <@{user_id}>"


def event_seats_line(seated: int, cap: int, waiting: int, bar: str) -> str:
    if int(cap) == 0:
        return f"идут: {int(seated)}"
    extra = f" · {EVENT_WAIT} {waiting}" if waiting else ""
    prefix = f"{bar} " if bar else ""
    return f"{prefix}{int(seated)}/{int(cap)}{extra}"


def event_counts_line(*, going: int, maybe: int, cant: int) -> str:
    return f"**{EVENT_GOING}:** {going} · **{EVENT_MAYBE}:** {maybe} · **{EVENT_CANT}:** {cant}"


def event_ping_body(user_ids: Sequence[int]) -> str:
    return " ".join(f"<@{user_id}>" for user_id in user_ids)


def event_ping_header(title: str) -> str:
    return f"Организатор зовёт идущих: {title}"


def event_ping_progress(done: int, total: int) -> str:
    return f"Пинг: {int(done)} из {int(total)}"


def event_mention_line(user_ids: Sequence[int], *, extra: int = 0) -> str:
    names = " · ".join(f"<@{user_id}>" for user_id in user_ids)
    if extra:
        return f"{names} · ещё {extra}" if names else f"ещё {extra}"
    return names


GROUP_INVITE = "Пригласить"

GROUP_LEAVE = "Выйти"

GROUP_ARCHIVE = "Архивировать"

GROUP_OWNER = "владелец"

GROUP_MEMBERS = "Участники"

GROUP_SILENCE = "архивация"

GROUP_TITLE = "Группа"

GROUP_OVER = "группа в архиве"

GROUP_MODAL_INVITE = "Пригласить в группу"

GROUP_FIELD_USER = "Кто"

GROUP_USER_PLACEHOLDER = "@участник или id"

GROUP_CREATED = "Группа создана. Канал видят только участники."

GROUP_INVITED = "Приглашение отправлено."

GROUP_INVITE_CLOSED = "Не удалось написать — человек найдёт приглашение в /groups."

GROUP_INVITE_ACCEPT = "Вступить"

GROUP_INVITE_DECLINE = "Отказаться"

GROUP_INVITE_SERVER = "Сервер"

GROUP_INVITE_OPEN = "Перейти"

GROUP_INVITE_EXPIRED = "Приглашение истекло."

GROUP_INVITE_ACCEPTED = "Вы в группе — перейти."

GROUP_INVITE_DECLINED = "Вы отклонили приглашение."

GROUP_JOINED = "присоединился"

GROUP_MINE = "Мои группы"

GROUP_NONE = "У вас нет открытых групп и приглашений."

GROUP_QUOTA_FREE = "без лимитов"

GROUP_LATER = "позже"

GROUP_CREATE_HINT = (
    "Неделя без сообщений — группа уйдёт в архив. История хранится для разбора жалоб."
)

GROUP_REPUTATION_OFF = "Создать группу могут держатели права или специальные следящие."

GROUP_LEFT = "Вы вышли из группы."

GROUP_ARCHIVED = "Группа перенесена в архив. Для участников она закрыта."

GROUP_CONFIRM_ARCHIVE = "Архивировать группу? Канал уйдёт в архив, история сохранится для разбора."

GROUP_ARCHIVE_ABORT = "Группа остаётся открытой."

GROUP_STRANGER = "Архивировать может владелец или персонал."

GROUP_OWNER_LEAVE = "Владелец не выходит — архивируйте группу."

GROUP_NOT_MEMBER = "Вы не в этой группе."

GROUP_ALREADY = "Этот человек уже в группе."

GROUP_BOT = "Бота в группу не приглашают."

GROUP_FULL = "В группе больше нет мест."

GROUP_BAD_SPEC = "Проверьте название и список участников."

GROUP_BAD_INVITE = "Укажите участника сервера: @имя или id."

GROUP_LOCKED = "Сейчас нельзя создать группу. Подождите и повторите."

GROUP_FAILED = "Не удалось создать каналы группы."

GROUP_VOICE = "войс"

GROUP_RESTORE = "Вернуть из архива"

GROUP_PURGE = "Удалить навсегда"

GROUP_CONFIRM_PURGE = "Удалить группу из архива навсегда? История пропадёт."

GROUP_PURGED = "Группа удалена из архива."

GROUP_RESTORED = "Группа возвращена из архива."

GROUP_STORED_FOREVER = "хранится бессрочно"

GROUP_ARCHIVE_NO_SHELF = "На сервере нет места для полки архива."

GROUP_REASON_OWNER = "владелец"

GROUP_REASON_SILENCE = "неделя тишины"

GROUP_REASON_STAFF = "персонал"

GROUP_REASON_OWNER_LEFT = "выход владельца"

GROUP_ARCHIVE_HEADER = "Архив группы"


def group_owner_line(user_id: int) -> str:
    return f"{GROUP_OWNER} <@{user_id}>"


def group_members_line(user_ids: Sequence[int]) -> str:
    names = " · ".join(f"<@{user_id}>" for user_id in user_ids)
    return f"**{GROUP_MEMBERS}:** {names}" if names else f"**{GROUP_MEMBERS}:** —"


def group_silence_line(deadline: object) -> str:
    return f"{GROUP_SILENCE} {deadline}"


def group_archive_reason_label(reason: str | None) -> str:
    mapping = {
        "owner": GROUP_REASON_OWNER,
        "silence": GROUP_REASON_SILENCE,
        "staff": GROUP_REASON_STAFF,
        "owner_left": GROUP_REASON_OWNER_LEFT,
    }
    return mapping.get(str(reason or ""), GROUP_REASON_OWNER)


def group_archive_topic(title: str, owner_id: int, archived_at: object, reason: str | None) -> str:
    when = archived_at.strftime("%Y-%m-%d") if hasattr(archived_at, "strftime") else str(archived_at)
    reason_label = group_archive_reason_label(reason)
    return f"Архив группы «{title}» · владелец {int(owner_id)} · в архиве с {when} · {reason_label}"


def group_archive_dm(title: str, reason: str | None, days: int) -> str:
    keep = GROUP_STORED_FOREVER if days <= 0 else f"история хранится {int(days)} дн. для разбора жалоб, затем удаляется"
    return (
        f"Группа «{title}» перенесена в архив ({group_archive_reason_label(reason)}). "
        f"Для вас она закрыта; {keep}."
    )


def group_silence_warn(deadline: object) -> str:
    stamp = int(deadline.timestamp()) if hasattr(deadline, "timestamp") else deadline
    return f"Если до <t:{stamp}:R> никто не напишет, группа уйдёт в архив."


def group_purge_line(purge_after: object | None) -> str:
    if purge_after is None:
        return GROUP_STORED_FOREVER
    stamp = int(purge_after.timestamp()) if hasattr(purge_after, "timestamp") else purge_after
    return f"удалится <t:{stamp}:R>"


def group_created_body(title: str, channel_id: int, sent: int, closed: int) -> str:
    extra = group_invites_line(sent, closed)
    return f"Группа «{title}» создана → <#{int(channel_id)}>\n{extra}\n{GROUP_CREATE_HINT}"


def group_invites_line(sent: int, closed: int) -> str:
    if closed:
        return f"Приглашения: отправлено {int(sent)}, не доставлено {int(closed)} (закрыты ЛС)."
    if sent:
        return f"Приглашения: отправлено {int(sent)}."
    return "Приглашения не рассылались."


def group_invite_dm(owner_id: int, title: str, guild_name: str) -> str:
    return f"<@{int(owner_id)}> приглашает вас в группу «{title}» на сервере {guild_name}."


def group_joined_line(user_id: int) -> str:
    return f"<@{int(user_id)}> {GROUP_JOINED}."


def group_reputation_denied(score: int | None, min_reputation: int) -> str:
    have = "—" if score is None else str(int(score))
    return f"Чтобы создать группу, нужна репутация выше {int(min_reputation)}. Сейчас {have}."


def group_quota_daily(retry: object) -> str:
    return f"Новую группу можно создать {retry}."


def group_quota_open(open_limit: int) -> str:
    return f"У вас уже {int(open_limit)} открытые группы — архивируйте одну."


def group_quota_line(
    open_count: int,
    open_limit: int,
    retry: object | None,
    *,
    privileged: bool,
) -> str:
    if privileged:
        return GROUP_QUOTA_FREE
    when = "сейчас" if retry is None else str(retry)
    return f"открыто {int(open_count)} из {int(open_limit)} · новая группа — {when}"


def group_invite_cooldown(retry: object) -> str:
    return f"Повторно пригласить можно {retry}."


def group_mine_line(title: str, channel_id: int, owner: bool) -> str:
    role = GROUP_OWNER if owner else GROUP_MEMBERS
    return f"«{title}» → <#{int(channel_id)}> · {role}"


def group_pending_line(title: str, owner_id: int) -> str:
    return f"«{title}» · пригласил <@{int(owner_id)}>"


REMIND_TITLE = "Напоминание"

REMIND_LIST_TITLE = "Напоминания"

REMIND_WHEN = "Когда"

REMIND_TEXT = "Текст"

REMIND_NUMBER = "Номер"

REMIND_SAVED = "Напомню в этом канале."

REMIND_CANCELLED = "Напоминание отменено."

REMIND_EMPTY = "Нет неотправленных напоминаний."

REMIND_EMPTY_HINT = "Поставьте через /remind"

REMIND_BAD_WHEN = "Срок — от минуты до 90 дней: 10m, 2h или 2026-09-05 18:00."

REMIND_BAD_TEXT = "Текст — от одного символа до 200."

REMIND_LIMIT = "У вас уже 10 неотправленных напоминаний."

REMIND_MISSING = "Такого напоминания нет."

REMIND_ALREADY = "Это напоминание уже отправлено или отменено."

REMIND_NO_CHANNEL = "Команда работает в текстовом канале."

REMIND_FOOTNOTE = "срок показывает клиент"

REMIND_DUE = "Напоминание"


def remind_list_line(reminder_id: int, text: str, when: str) -> str:
    return f"**#{reminder_id}** · {text} · {when}"


def remind_cancel_label(reminder_id: int) -> str:
    return f"Отменить #{reminder_id}"


LFG_STARTER_TITLE = "▸ Ищу команду"

LFG_STARTER_BODY = "Выберите игру и сколько человек нужно. Быть в голосе не обязательно."

LFG_STARTER_BUTTON = "Ищу команду"

LFG_PICK_TITLE = "Какая игра"

LFG_PICK_BODY = "Только ваши игровые роли."

LFG_PICK_PLACEHOLDER = "Игра"

LFG_MODAL_TITLE = "Набор"

LFG_NEED_LABEL = "Сколько игроков нужно"
LFG_NEED_HINT = "0 — без ограничения, до 100"

LFG_NOTE_LABEL = "Комментарий"

LFG_JOIN = "Иду"

LFG_LEAVE = "Передумал"

LFG_CLOSE = "Закрыть"

LFG_ROOM = "В комнату"

LFG_FIELD_AUTHOR = "Кто ищет"

LFG_FIELD_NEED = "Нужно"

LFG_FIELD_LIST = "Записались"

LFG_FIELD_NOTE = "Комментарий"

LFG_FULL_TITLE = "Собрались"

LFG_CLOSED_TITLE = "Набор закрыт"

LFG_EXPIRED_TITLE = "Сгорел"

LFG_SKIP_PING = "роль не упомянута: недавно уже звали"

LFG_NO_ROLES = "Сначала выберите игры в {channel}."

LFG_NO_ROLES_PLAIN = "Сначала выберите игры в канале ролей."

LFG_NO_CHANNEL = "Канал набора не задан."

LFG_LIVE = "У вас уже есть открытый набор."

LFG_COOLDOWN = "Подождите, прежде чем открыть новый набор."

LFG_NO_GAME = "Сначала выберите эту игру в канале ролей."

LFG_NEED_BAD = "Сколько игроков нужно — число от 0 до 100."

LFG_NOTE_LONG = "Комментарий короче 100 символов."

LFG_NOTE_INVITE = "В комментарии нельзя ставить приглашение."

LFG_NOTE_AD = "В комментарии нельзя ставить ссылку или рекламу."

LFG_NOTE_MENTION = "В комментарии нельзя упоминать людей и роли."

LFG_JOIN_OK = "Вы в наборе."

LFG_JOIN_AUTHOR = "Автор набор не занимает."

LFG_JOIN_INSIDE = "Вы уже в наборе."

LFG_JOIN_FULL = "Набор уже собран."

LFG_JOIN_CLOSED = "Набор закрыт."

LFG_LEAVE_OK = "Вас вычеркнули."

LFG_LEAVE_MISSING = "Вас не было в наборе."

LFG_LEAVE_AUTHOR = "Автор набор не покидает — закройте его."

LFG_CLOSE_OK = "Набор закрыт."

LFG_CLOSE_FORBIDDEN = "Закрыть может автор или следящий."

LFG_CLOSE_ALREADY = "Набор уже закрыт."

LFG_ROOM_HINT = "Зайдите в {channel}."

LFG_ROOM_MISSING = "Комнаты ещё нет."

LFG_EMPTY_LIST = "пока никого"

LFG_VOICE_CAP = "голосовая комната — без лимита мест: Discord допускает не больше 99"


def lfg_need_line(need: int, signed: int) -> str:
    if int(need) == 0:
        return f"Нужно: без ограничения · записались: {int(signed)}"
    return f"Записались: {int(signed)} из {int(need)}"


def lfg_room_line(game: str, signed: int, need: int) -> str:
    if int(need) == 0:
        return f"набор: {game} · {int(signed)}"
    return f"набор: {game} · {int(signed)}/{int(need)}"


def lfg_author_line(count: int) -> str:
    return f"наборов за 30 дней: **{int(count)}**"


def lfg_no_roles(channel: str | None) -> str:
    if channel:
        return LFG_NO_ROLES.format(channel=channel)
    return LFG_NO_ROLES_PLAIN


def lfg_note_denied(reason: str) -> str:
    return {
        "long": LFG_NOTE_LONG,
        "invite": LFG_NOTE_INVITE,
        "ad": LFG_NOTE_AD,
        "mention": LFG_NOTE_MENTION,
    }.get(reason, LFG_NOTE_AD)


def lfg_post_denied(reason: str) -> str:
    return {
        "live": LFG_LIVE,
        "cooldown": LFG_COOLDOWN,
        "no_game": LFG_NO_GAME,
        "bad_need": LFG_NEED_BAD,
        "no_channel": LFG_NO_CHANNEL,
    }.get(reason, LFG_NO_CHANNEL)


def lfg_join_denied(reason: str) -> str:
    return {
        "closed": LFG_JOIN_CLOSED,
        "full": LFG_JOIN_FULL,
        "inside": LFG_JOIN_INSIDE,
        "author": LFG_JOIN_AUTHOR,
    }.get(reason, LFG_JOIN_CLOSED)


def lfg_leave_denied(reason: str) -> str:
    return {
        "closed": LFG_JOIN_CLOSED,
        "missing": LFG_LEAVE_MISSING,
        "author": LFG_LEAVE_AUTHOR,
    }.get(reason, LFG_LEAVE_MISSING)


def lfg_close_denied(reason: str) -> str:
    return {
        "closed": LFG_CLOSE_ALREADY,
        "forbidden": LFG_CLOSE_FORBIDDEN,
    }.get(reason, LFG_CLOSE_FORBIDDEN)


def lfg_closed_title(reason: str | None) -> str:
    if reason == "full":
        return LFG_FULL_TITLE
    if reason == "expired":
        return LFG_EXPIRED_TITLE
    return LFG_CLOSED_TITLE


def lfg_numbers_line(ttl: int, ping: int, user: int) -> str:
    return f"{int(ttl)} / {int(ping)} / {int(user)}"


def groups_numbers_line(silence_days: int, archive_days: int) -> str:
    return f"{int(silence_days)} / {int(archive_days)}"


def groups_limits_line(reputation: int, daily: int, open_limit: int) -> str:
    return f"{int(reputation)} / {int(daily)} / {int(open_limit)}"
