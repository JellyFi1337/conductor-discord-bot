"""Тексты: отчёты об ошибках."""

from __future__ import annotations

BUG_MODAL_TITLE = "Сообщить об ошибке"

BUG_DID_LABEL = "Что делали"

BUG_DID_HINT = "Команда, кнопка или шаг до сбоя"

BUG_HAPPENED_LABEL = "Что произошло"

BUG_HAPPENED_HINT = "Что увидели вместо ожидаемого"

BUG_CODE_LABEL = "Код ошибки"

BUG_CODE_HINT = "Необязательно. Из карточки, например E-1A2B"

BUG_REPORT_BUTTON = BUG_MODAL_TITLE

BUG_USE_SERVER = "Эту команду вызывайте на сервере, не в личке."

BUG_RATE = "Уже три отчёта за час. Подождите и напишите снова."

BUG_NO_CHANNEL = "Канал баг-репортов ещё не собран. Скажите следящим."

BUG_BAD_TEXT = "Напишите, что делали и что произошло."

BUG_THANKS_TITLE = "Отчёт принят"

BUG_THANKS_BODY = "Персонал увидит его в канале баг-репортов. Спасибо."

BUG_TITLE = "Отчёт об ошибке"

BUG_FIELD_AUTHOR = "Отправил"

BUG_FIELD_VERSION = "Версия"

BUG_FIELD_CODE = "Код"

BUG_FIELD_DID = BUG_DID_LABEL

BUG_FIELD_HAPPENED = BUG_HAPPENED_LABEL

BUG_FIELD_MATCH = "Связь с журналом"

BUG_FIELD_STATUS = "Статус"

BUG_ACCEPT = "Принято"

BUG_FIX = "Исправлено"

BUG_REJECT = "Не баг"

BUG_STATUS_NEW = "новый"

BUG_STATUS_ACCEPTED = "взят в работу"

BUG_STATUS_FIXED = "починен"

BUG_STATUS_REJECTED = "не баг"

BUG_DM_ACCEPTED = "Ваш отчёт об ошибке принят в работу."

BUG_DM_FIXED = "Ваш отчёт об ошибке отмечен как исправленный."

BUG_DM_REJECTED = "Ваш отчёт разобрали: это не ошибка бота."

BUG_STALE = "Этот отчёт уже разобран или его нет."


def bug_exception(error_type: str, origin: str) -> str:
    kind = error_type.strip() or "исключение"
    where = origin.strip() or "модуле"
    return f"исключение: {kind} в {where}"


def bug_match_line(*, error_type: str, origin: str, jump_url: str) -> str:
    if jump_url.strip():
        return jump_url.strip()
    if error_type.strip() or origin.strip():
        return bug_exception(error_type, origin)
    return "—"


def bug_status_label(status: str) -> str:
    return {
        "new": BUG_STATUS_NEW,
        "accepted": BUG_STATUS_ACCEPTED,
        "fixed": BUG_STATUS_FIXED,
        "rejected": BUG_STATUS_REJECTED,
    }.get(status, status)


def bug_dm_body(status: str) -> str:
    return {
        "accepted": BUG_DM_ACCEPTED,
        "fixed": BUG_DM_FIXED,
        "rejected": BUG_DM_REJECTED,
    }.get(status, BUG_DM_ACCEPTED)
