"""Тексты: profile."""

from __future__ import annotations

from .moderation import MOD_REVOKED, mod_kind_label

PROFILE_TAB_REP = "Репутация"

PROFILE_TAB_ACTIVITY = "Активность"

PROFILE_TAB_ACHIEVEMENTS = "Достижения"

PROFILE_TENURE = "стаж"

PROFILE_REP = "репутация"

PROFILE_REP_SINNER = "грешник · {score}"

PROFILE_REP_TIER = "{name} · {score}"

PROFILE_STAFF = "служебное"

DOSSIER_OPEN = "Досье"

DOSSIER_TITLE = "Досье"

DOSSIER_ID = "id"

DOSSIER_SANCTIONS = "Сейчас"

DOSSIER_TIMEOUT = "тайм-аут до {when}"

DOSSIER_MUTED = "мут"

DOSSIER_WATCHING = "под наблюдением до {when}"

DOSSIER_CASES = "Последние дела"

DOSSIER_NOTES = "Заметки"

DOSSIER_TICKETS = "Открытые тикеты"

DOSSIER_TICKETS_EMPTY = "нет"

DOSSIER_HISTORY = "Вся история"

DOSSIER_WARN = "Замечание"

DOSSIER_TIMEOUT_BTN = "Тайм-аут"

DOSSIER_MUTE = "Мут"

DOSSIER_UNMUTE = "Снять мут"

DOSSIER_NOTE = "Заметка"

DOSSIER_WATCH = "Наблюдение"

DOSSIER_WATCH_CLEAR = "Снять наблюдение"

DOSSIER_KICK = "Кик"

DOSSIER_BAN = "Бан"

DOSSIER_DURATION_LABEL = "Срок"

DOSSIER_DURATION_HINT = "10m, 2h или 1d"

DOSSIER_WATCH_DAYS_LABEL = "Дни, не больше 30"

DOSSIER_WATCH_REASON_LABEL = "Повод, не короче 8 символов"

DOSSIER_WATCH_MODAL = "Наблюдение"

DOSSIER_WATCH_SET = "Человек под наблюдением"

DOSSIER_WATCH_OFF = "Наблюдение снято"

DOSSIER_WATCH_DAYS = "Срок наблюдения — целое число дней от 1 до 30."

DOSSIER_TIMEOUT_MODAL = "Тайм-аут"

DOSSIER_ROLES = "Роли по выбору"

PROFILE_ROLES = "Роли"

DOSSIER_REVOKE = "Отменить дело"

DOSSIER_REVOKE_MODAL = "Отменить дело"

DOSSIER_REVOKE_NUMBER = "Номер дела"

DOSSIER_REVOKE_MISSING = "Среди последних дел такого номера нет."


def dossier_case_line(
    number: int,
    kind: str,
    actor_id: int,
    when: str,
    *,
    revoked_by: int | None = None,
) -> str:
    line = f"**#{int(number)} · {mod_kind_label(kind)}** · <@{int(actor_id)}> · {when}"
    if revoked_by is None:
        return line
    return f"~~{line}~~ · {MOD_REVOKED} <@{int(revoked_by)}>"


def dossier_note_line(actor_id: int, reason: str, when: str) -> str:
    snippet = reason if len(reason) <= 80 else reason[:79] + "…"
    return f"<@{int(actor_id)}> · {when} — {snippet}"


def dossier_tickets_line(count: int, categories: tuple[str, ...]) -> str:
    if count <= 0:
        return DOSSIER_TICKETS_EMPTY
    names = ", ".join(categories)
    return f"{count} · {names}" if names else str(count)


def dossier_action_label(action: str) -> str:
    return {
        "warn": DOSSIER_WARN,
        "timeout": DOSSIER_TIMEOUT_BTN,
        "mute": DOSSIER_MUTE,
        "unmute": DOSSIER_UNMUTE,
        "note": DOSSIER_NOTE,
        "watch": DOSSIER_WATCH,
        "watch_clear": DOSSIER_WATCH_CLEAR,
        "kick": DOSSIER_KICK,
        "ban": DOSSIER_BAN,
        "revoke": DOSSIER_REVOKE,
        "history": DOSSIER_HISTORY,
    }.get(action, action)
