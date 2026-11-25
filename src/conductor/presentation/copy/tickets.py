"""Тексты: tickets."""

from __future__ import annotations

from collections.abc import Sequence

from .common import HIERARCHY_DENIED, RANK_LABELS, STALE_BODY, _ru_days, hold_action, hold_text, rank_label
from .ladder import ladder_step

TICKET_TITLE = "Тикеты"

TICKET_PANEL_TITLE = "Обращение к следящим"

TICKET_PANEL_BODY = "Выберите тему. Откроется закрытый канал,\nкоторый видите вы и дежурные."

TICKET_FIELD_OPEN = "Открыто сейчас"

TICKET_FIELD_WAIT = "Среднее время ответа"

TICKET_FOOTNOTE = "один открытый тикет на человека"

TICKET_FOOTNOTE_LIMIT = "открытых тикетов на человека: {limit}"

TICKET_FIELD_AUTHOR = "Открыл"

TICKET_FIELD_OPENED = "Открыт"

TICKET_FIELD_CLOSED = "Закрыт"

TICKET_FIELD_STATE = "Состояние"

TICKET_FIELD_CLOSER = "Закрыл"

TICKET_FIELD_CLAIMER = "Взял"

TICKET_FIELD_PRIORITY = "Приоритет"

TICKET_FIELD_OUTCOME = "Исход"

TICKET_FIELD_REASON = "Причина"

TICKET_FIELD_RATING = "Оценка"

TICKET_FIELD_INFO = "Приложите"

TICKET_OUTCOME = "Исход закрытия"

TICKET_CONFIRM_OUTCOME = "Выберите исход, затем напишите причину."

TICKET_NEED_OUTCOME = "Сначала выберите исход закрытия."

TICKET_CASE_APPEAL = "Обжалование наказания закрывают «Оставить в силе» или «Отменить наказание»."

TICKET_KIND_HIDDEN = "Эту категорию открывает только бот."

TICKET_RANK_HIDDEN = "Эту категорию ведут специальные следящие."

TICKET_OUTCOME_RESOLVED = "решено"

TICKET_OUTCOME_FIXED = "исправлено"

TICKET_OUTCOME_ACCEPTED = "принято"

TICKET_OUTCOME_REJECTED = "отклонено"

TICKET_OUTCOME_POINTLESS = "бессмысленно"

TICKET_OUTCOME_DUPLICATE = "дубль"

TICKET_OUTCOME_SANCTIONED = "санкция"

TICKET_OUTCOME_NO_ACTION = "без действия"

TICKET_KIND_GENERAL = "вопрос"

TICKET_KIND_COMPLAINT = "жалоба"

TICKET_KIND_BUG = "баг"

TICKET_KIND_SUGGESTION = "предложение"

TICKET_KIND_ANONYMOUS = "анонимно"

TICKET_ANON_NAME = "Аноним"

TICKET_ANON_TRANSCRIPT = "Автор"

TICKET_ANON_RELAY = "Аноним:"

TICKET_STAFF_RELAY = "Следящий ({rank}):"

TICKET_DM_OPEN = "Тикет #{number} открыт. Пишите сюда — сообщения уйдут следящим без вашего имени."

TICKET_DM_CLOSED = "Откройте ЛС от участников сервера и попробуйте снова."

TICKET_ANON_OPEN = "У вас уже есть открытый анонимный тикет."

TICKET_DM_BLOCKED = "ЛС автора закрыты, ответы не доходят."

TICKET_KIND_APPLICATION = "заявка"

TICKET_KIND_APPEAL = "обжалование"

TICKET_TOPIC_MODERATOR = "следящий"

TICKET_TOPIC_TOURNAMENT = "турнир"

TICKET_TOPIC_CREW = "съёмочная"

TICKET_PRIORITY_LOW = "низкий"

TICKET_PRIORITY_NORMAL = "обычный"

TICKET_PRIORITY_HIGH = "высокий"

TICKET_CARD_FOOTNOTE = "следящие видят заметки кнопкой «Заметки»"

TICKET_TAKE = "Взять"

TICKET_DROP = "Снять"

TICKET_TRANSFER = "Передать"

TICKET_ADD = "Добавить"

TICKET_REMOVE = "Убрать"

TICKET_WELCOME_ASK = "Опишите вопрос здесь — персонал ответит в этом канале."

TICKET_WELCOME_BUTTONS = "Кнопки на карточке: «{close}» и «{ping}»."

TICKET_WELCOME_APPEAL_BUTTONS = "Кнопки на карточке: «{keep}», «{revoke}» и «{ping}»."

TICKET_TRANSCRIPT_EDITED = "(изменено)"

TICKET_TRANSCRIPT_FORWARD = "переслано: {text}"

TICKET_CLOSE = "Закрыть тикет"

TICKET_RENAME = "Переименовать"

TICKET_RENAME_MODAL = "Имя канала"

TICKET_CLOSE_REQUEST_TITLE = "Автор просит закрыть"

TICKET_CLOSE_REQUEST_BODY = "{author} просит закрыть этот тикет."

TICKET_NOTES = "Заметки"

TICKET_ESCALATE = "Вызвать старшего"

TICKET_REOPEN = "Открыть снова"

TICKET_APPROVE_REQ = "На утверждение"

TICKET_APPROVE = "Утвердить"

TICKET_REJECT = "Отклонить"

TICKET_APPEAL = "Обжаловать"

TICKET_APPEAL_OWNER = "Обжаловать у владельца сервера"

TICKET_UPHOLD = "Оставить в силе"

TICKET_OVERTURN = "Отменить решение"

TICKET_PENDING_BODY = "Заявка ждёт решения специального следящего."

TICKET_APPEAL_CHIEF_TITLE = "Обжалование у Главного следящего"

TICKET_APPEAL_OWNER_TITLE = "Обжалование у владельца сервера"

TICKET_APPEAL_BODY = "Решение по закрытию пересматривают."

TICKET_FIELD_APPOINT = "После утверждения"

TICKET_APPOINT_HINT = "Если утвердите — назначьте следящего командой /watchers appoint."

TICKET_FLOW_CLOSER = "Вы закрыли это дело — его рассматривает другой человек."

TICKET_FLOW_TOO_LATE = "Срок обжалования истёк."

TICKET_FLOW_ALREADY = "Этот уровень обжалования уже использован."

TICKET_FLOW_ROLE = "Это действие вам недоступно."

TICKET_FLOW_KIND = "Для этой категории так нельзя."

TICKET_FLOW_STATE = "Сейчас это действие недоступно."

TICKET_FLOW_OUTCOME = "Обжаловать можно только отклонённое или бессмысленное закрытие."

TICKET_APPROVE_REQ_OK = "Заявка отправлена на утверждение."

TICKET_APPROVE_OK = "Заявка утверждена."

TICKET_APPEAL_OK = "Обжалование отправлено."

TICKET_UPHOLD_OK = "Решение оставлено в силе."

TICKET_OVERTURN_OK = "Решение отменено, тикет снова открыт."

TICKET_DM_APPROVE_REQ = "Ваша заявка отправлена на утверждение специальному следящему."

TICKET_DM_APPROVE = "Ваша заявка утверждена."

TICKET_DM_REJECT = "Ваша заявка отклонена."

TICKET_DM_APPEAL = "Ваше обжалование принято к рассмотрению."

TICKET_DM_UPHOLD = "Решение по вашему тикету оставлено в силе."

TICKET_DM_OVERTURN = "Решение по вашему тикету отменено, тикет снова открыт."

TICKET_REMIND_PENDING = "Заявка всё ещё ждёт решения специального следящего."

TICKET_REMIND_CHIEF = "Обжалование всё ещё у Главного следящего."

TICKET_REMIND_OWNER = "Обжалование всё ещё у владельца сервера."

TICKET_PRIORITY = "Приоритет"

TICKET_NOTE_ADD = "Добавить заметку"

TICKET_NOTE_MODAL = "Заметка по тикету"

TICKET_NOTE_LABEL = "Текст"

TICKET_NOTES_EMPTY = "Заметок пока нет."

TICKET_NOTES_DENIED = "Заметки тикета автору не видны."

TICKET_NOTES_ADDED = "Заметка сохранена."

TICKET_ESCALATE_PING = "Старшие на смене, тикет ждёт внимания."

TICKET_ESCALATE_OK = "Старшие на смене вызваны."

TICKET_ESCALATE_EMPTY = "Сейчас нет старших на смене."

TICKET_ESCALATE_WAIT = "Старшего уже вызывали. Подождите 10 минут."

TICKET_AUTOCLOSE_TITLE = "Тикет закроется из‑за тишины"

TICKET_FIELD_DEADLINE = "Срок"

TICKET_RATED = "Оценка принята."

TICKET_RATE_ALREADY = "Оценку уже ставили."

TICKET_REOPEN_OK = "Тикет снова открыт."

TICKET_REOPEN_GONE = "Канал тикета уже удалён."

TICKET_REOPEN_LATE = "Срок повторного открытия вышел."

TICKET_NO_CLOSED = "Здесь нет закрытого тикета."

TICKET_CLOSEREQUEST_OK = "Запрос на закрытие отправлен следящим."

TICKET_RATE_PROMPT = "Оцените помощь"

TICKET_CONFIRM_BODY = "Закрыть тикет и сохранить переписку в архив?"

TICKET_REASON_MODAL = "Почему закрываете"

TICKET_REASON_LABEL = "Причина"

TICKET_LOG_REASON = "Изменить причину"

TICKET_LOG_TRANSCRIPT = "Транскрипт"

TICKET_LOG_REASON_MODAL = "Причина закрытия"

TICKET_LOG_REASON_DONE = "Причина изменена."

TICKET_LOG_REASON_DENIED = "Причину закрытия может менять закрывший или старший следящий, но не автор."

TICKET_LOG_TRANSCRIPT_MISSING = "Транскрипт ещё не сохранён."

TICKET_LOG_STATE_CLOSED = "закрыт"

TICKET_LOG_STATE_PENDING = "ждёт утверждения"

TICKET_LOG_STATE_APPEAL_CHIEF = "обжалование у главного следящего"

TICKET_LOG_STATE_APPEAL_OWNER = "обжалование у владельца сервера"

TICKET_LOG_STATE_OPEN = "открыт"

TICKET_OPEN_TITLE = "Открываю тикет"

TICKET_CREATE_HELD = "Сейчас нельзя открыть канал тикета. Подождите и повторите."

TICKET_STAGE_LIMIT = "Проверяю лимит"

TICKET_STAGE_SAVE = "Сохраняю тикет"

TICKET_STAGE_CARD = "Пишу карточку"

TICKET_CLOSE_TITLE = "Закрываю тикет"

TICKET_TRANSCRIPT_TITLE = "Транскрипт тикета"

TICKET_CLOSED = "Тикет закрыт"

TICKET_POSTED = "Панель тикетов опубликована."

TICKET_NO_TICKET = "Здесь нет открытого тикета."

TICKET_NO_PANEL = "Сначала выложите панель в /admin panel → Тикеты."

TICKET_LIMIT = "У вас уже есть открытый тикет."

TICKET_TAKEN = "Тикет уже взял другой следящий."

TICKET_CLAIM_OK = "Тикет у вас."

TICKET_CLAIM_ALREADY = "Вы уже ведёте этот тикет."

TICKET_UNCLAIM_OK = "Тикет снова в очереди."

TICKET_UNCLAIM_NONE = "Тикет никто не взял."

TICKET_TRANSFER_OK = "Тикет передан."

TICKET_ADD_OK = "Человек добавлен в тикет."

TICKET_REMOVE_OK = "Человек убран из тикета."

TICKET_RENAMED = "Канал тикета переименован."

TICKET_CONFIRM_HINT = "Подтвердите закрытие на карточке тикета."

TICKET_AUTHOR_BLOCKED = "Автор не может закрыть этот тикет."

TICKET_STRANGER = "Закрыть тикет может автор или следящий."

TICKET_KEEP_AUTHOR = "Автора тикета убрать нельзя."

TICKET_TRANSCRIPT_EMPTY = "(пусто)"

TICKET_FAQ = "это решило"

TICKET_NEED = "всё равно нужен тикет"

TICKET_FAQ_OK = "Хорошо. Тикет не открыт."

TICKET_FORM_TITLE = "Открытие тикета"

TICKET_FORM_SAVED = "Вопросы категории сохранены."

TICKET_HINT_SAVED = "Подсказка категории сохранена."

TICKET_FORM_CLEARED = "Форма и подсказка сброшены."

TICKET_NO_CATEGORY = "Такой категории на панели нет."

TICKET_FORM_EMPTY = "У категории нет вопросов и подсказки."

TICKET_FORM_FIELD = "Вопрос {n}"

TICKET_FORM_QUESTIONS_MODAL = "Вопросы категории"

TICKET_HINT_TITLE = "Сначала посмотрите подсказку"

TICKET_FIELD_HINT = "Подсказка"

TICKET_FIELD_QUESTIONS = "Вопросы"


def ticket_open_count(count: int) -> str:
    return str(count)


def ticket_wait_value() -> str:
    return "—"


def ticket_card_title(number: int, category: str, *, anonymous: bool = False) -> str:
    if anonymous:
        return f"{TICKET_ANON_NAME} #{number} · {category}"
    return f"Тикет #{number} · {category}"


def ticket_log_title(number: int, category: str) -> str:
    return f"Тикет #{number} · {category}"


def ticket_log_state(state: str) -> str:
    return {
        "pending_approval": TICKET_LOG_STATE_PENDING,
        "appeal_chief": TICKET_LOG_STATE_APPEAL_CHIEF,
        "appeal_owner": TICKET_LOG_STATE_APPEAL_OWNER,
        "closed": TICKET_LOG_STATE_CLOSED,
    }.get(state, TICKET_LOG_STATE_OPEN)


def ticket_rating_stars(rating: int | None) -> str:
    if rating is None:
        return "—"
    filled = max(0, min(5, int(rating)))
    return ("★" * filled) + ("☆" * (5 - filled))


def ticket_author_value(author_id: int, *, anonymous: bool, extra: str = "") -> str:
    name = TICKET_ANON_NAME if anonymous else f"<@{author_id}>"
    if extra:
        return f"{name} · {extra}"
    return name


def ticket_welcome_body(author_id: int, buttons: str) -> str:
    return f"<@{int(author_id)}>\n{TICKET_WELCOME_ASK}\n{buttons}"


def ticket_transcript_header(
    *,
    number: int,
    category: str,
    author: str,
    claimer: str,
    opened: str,
    closed: str,
    outcome: str,
    reason: str,
) -> str:
    return (
        f"Тикет #{number} · {category}\n"
        f"{TICKET_FIELD_AUTHOR}: {author}\n"
        f"{TICKET_FIELD_CLAIMER}: {claimer}\n"
        f"{TICKET_FIELD_OPENED}: {opened}\n"
        f"{TICKET_FIELD_CLOSED}: {closed}\n"
        f"{TICKET_FIELD_OUTCOME}: {outcome}\n"
        f"{TICKET_FIELD_REASON}: {reason}"
    )


def ticket_transcript_forward(text: str) -> str:
    return TICKET_TRANSCRIPT_FORWARD.format(text=text)


def ticket_dm_open(number: int) -> str:
    return TICKET_DM_OPEN.format(number=number)


def ticket_staff_relay(rank: str) -> str:
    return TICKET_STAFF_RELAY.format(rank=RANK_LABELS.get(rank, rank))


def ticket_claimer_value(user_id: int | None) -> str:
    return f"<@{user_id}>" if user_id else "—"


def ticket_transcript_name(number: int) -> str:
    return f"ticket-{number}.txt"


def ticket_close_request_body(author_id: int) -> str:
    return TICKET_CLOSE_REQUEST_BODY.format(author=f"<@{author_id}>")


def ticket_close_denied(code: str) -> str:
    return {
        "stranger": TICKET_STRANGER,
        "author_blocked": TICKET_AUTHOR_BLOCKED,
        "hierarchy": HIERARCHY_DENIED,
        "already": STALE_BODY,
        "hidden": TICKET_RANK_HIDDEN,
        "need_outcome": TICKET_NEED_OUTCOME,
        "case_appeal": TICKET_CASE_APPEAL,
    }.get(code, TICKET_STRANGER)


def ticket_flow_denied(code: str) -> str:
    return {
        "closer": TICKET_FLOW_CLOSER,
        "too_late": TICKET_FLOW_TOO_LATE,
        "already": TICKET_FLOW_ALREADY,
        "wrong_role": TICKET_FLOW_ROLE,
        "wrong_kind": TICKET_FLOW_KIND,
        "wrong_state": TICKET_FLOW_STATE,
        "wrong_outcome": TICKET_FLOW_OUTCOME,
    }.get(code, TICKET_FLOW_ROLE)


def ticket_appeal_label(*, owner: bool) -> str:
    return TICKET_APPEAL_OWNER if owner else TICKET_APPEAL


def ticket_review_title(state: str) -> str:
    if state == "appeal_owner":
        return TICKET_APPEAL_OWNER_TITLE
    if state == "appeal_chief":
        return TICKET_APPEAL_CHIEF_TITLE
    return TICKET_PENDING_BODY


def ticket_review_remind(state: str) -> str:
    if state == "appeal_owner":
        return TICKET_REMIND_OWNER
    if state == "appeal_chief":
        return TICKET_REMIND_CHIEF
    return TICKET_REMIND_PENDING


def ticket_claim_denied(code: str) -> str:
    return {
        "already": TICKET_CLAIM_ALREADY,
        "taken": TICKET_TAKEN,
        "closed": STALE_BODY,
        "missing": TICKET_UNCLAIM_NONE,
        "hidden": TICKET_RANK_HIDDEN,
    }.get(code, TICKET_TAKEN)


def ticket_panel_footnote(limit: int) -> str:
    if limit <= 1:
        return TICKET_FOOTNOTE
    return TICKET_FOOTNOTE_LIMIT.format(limit=limit)


def ticket_form_quote(prompt: str, answer: str) -> str:
    return f"> **{prompt}:** {answer}"


def ticket_form_body(answers: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(ticket_form_quote(prompt, answer) for prompt, answer in answers if prompt and answer)


def ticket_form_field_label(index: int) -> str:
    return TICKET_FORM_FIELD.format(n=index)


def ticket_questions_value(prompts: Sequence[str]) -> str:
    if not prompts:
        return "—"
    return "\n".join(f"{index}. {prompt}" for index, prompt in enumerate(prompts, start=1))


def ticket_priority_label(value: str) -> str:
    return {
        "low": TICKET_PRIORITY_LOW,
        "high": TICKET_PRIORITY_HIGH,
    }.get(value, TICKET_PRIORITY_NORMAL)


def ticket_outcome_label(value: str | None) -> str:
    return {
        "resolved": TICKET_OUTCOME_RESOLVED,
        "fixed": TICKET_OUTCOME_FIXED,
        "accepted": TICKET_OUTCOME_ACCEPTED,
        "rejected": TICKET_OUTCOME_REJECTED,
        "pointless": TICKET_OUTCOME_POINTLESS,
        "duplicate": TICKET_OUTCOME_DUPLICATE,
        "sanctioned": TICKET_OUTCOME_SANCTIONED,
        "no_action": TICKET_OUTCOME_NO_ACTION,
    }.get("" if value is None else value, "—")


def ticket_kind_label(value: str) -> str:
    return {
        "general": TICKET_KIND_GENERAL,
        "complaint": TICKET_KIND_COMPLAINT,
        "bug": TICKET_KIND_BUG,
        "suggestion": TICKET_KIND_SUGGESTION,
        "anonymous": TICKET_KIND_ANONYMOUS,
        "application": TICKET_KIND_APPLICATION,
        "appeal": TICKET_KIND_APPEAL,
    }.get(value, TICKET_KIND_GENERAL)


def ticket_topic_label(value: str) -> str:
    return {
        "moderator": TICKET_TOPIC_MODERATOR,
        "tournament": TICKET_TOPIC_TOURNAMENT,
        "crew": TICKET_TOPIC_CREW,
    }.get(value, "—")


def ticket_visible_ranks_value(raw: str) -> str:
    from conductor.domain.tickets import visible_rank_set

    ranks = visible_rank_set(raw)
    if not ranks:
        return "—"
    return ", ".join(rank_label(rank.key) for rank in sorted(ranks, key=lambda item: int(item)))


def ticket_open_gates(decision: object) -> str:
    need_days = int(getattr(decision, "need_days", 0) or 0)
    have_days = int(getattr(decision, "have_days", 0) or 0)
    need_step = ladder_step(getattr(decision, "need_step", "guest"))
    have_step = ladder_step(getattr(decision, "have_step", "guest"))
    topic = str(getattr(decision, "topic", "") or "")
    verdict = str(getattr(decision, "verdict", "") or "")
    who = "заявку следящего" if topic == "moderator" else "эту категорию"
    tenure = f"после {_ru_days(need_days)} на сервере (у вас {have_days})"
    ladder = f"ступени «{need_step}»"
    if verdict == "tenure":
        return f"{who.capitalize()} можно открыть {tenure}."
    if verdict == "ladder":
        return f"{who.capitalize()} можно открыть со {ladder} (у вас {have_step})."
    return f"{who.capitalize()} можно открыть {tenure} и {ladder}."


def ticket_notes_body(notes: Sequence[tuple[int, str]]) -> str:
    if not notes:
        return TICKET_NOTES_EMPTY
    return "\n".join(f"<@{author_id}>: {body}" for author_id, body in notes)


def ticket_rate_denied(code: str) -> str:
    return {
        "already": TICKET_RATE_ALREADY,
        "stranger": TICKET_STRANGER,
        "open": STALE_BODY,
        "bad": STALE_BODY,
    }.get(code, STALE_BODY)


def ticket_reopen_denied(code: str) -> str:
    return {
        "too_late": TICKET_REOPEN_LATE,
        "channel_gone": TICKET_REOPEN_GONE,
        "not_closed": TICKET_NO_CLOSED,
    }.get(code, STALE_BODY)


def ticket_escalate_denied(code: str) -> str:
    return {
        "cooldown": TICKET_ESCALATE_WAIT,
        "empty": TICKET_ESCALATE_EMPTY,
        "closed": STALE_BODY,
    }.get(code, STALE_BODY)


def ticket_hold_text(source: str, seconds: float, *, route: str = "") -> str:
    return hold_text(source, seconds, action=hold_action("channel"), route=route)
