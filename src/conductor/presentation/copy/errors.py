"""Тексты критического журнала сбоев."""

from __future__ import annotations

from collections.abc import Iterable

ERROR_TITLE_COMMAND = "Сбой команды"

ERROR_TITLE_COMPONENT = "Сбой панели"

ERROR_TITLE_MODAL = "Сбой формы"

ERROR_TITLE_LISTENER = "Сбой события"

ERROR_TITLE_TASK = "Сбой фоновой задачи"

ERROR_LOG_SOURCE = "критический сбой"

ERROR_FIELD_CODE = "Код"

ERROR_FIELD_ACTION = "Команда"

ERROR_FIELD_WHERE = "Место"

ERROR_FIELD_WHO = "Кто"

ERROR_FIELD_PERMS = "Права Discord"

ERROR_FIELD_BOT = "Не хватает боту"

ERROR_FIELD_ORIGIN = "Вызов"

ERROR_FIELD_DETAIL = "Сбой"

ERROR_KIND_COMMAND = "слэш-команда"

ERROR_KIND_COMPONENT = "кнопка или селект"

ERROR_KIND_MODAL = "модальное окно"

ERROR_KIND_LISTENER = "событие Discord"

ERROR_KIND_TASK = "фоновая задача"

ERROR_DEVELOPER = "разработчик бота"

ERROR_GUILD_OWNER = "владелец сервера"

ERROR_DISCORD_ADMIN = "Администратор Discord"

ERROR_DM = "личные сообщения"

ERROR_NO_PERMS = "нет заметных прав"

ERROR_BOT_OK = "все служебные есть"

ERROR_PERM_LABELS = {
    "administrator": "Администратор",
    "manage_guild": "Управлять сервером",
    "manage_roles": "Управлять ролями",
    "manage_channels": "Управлять каналами",
    "ban_members": "Банить",
    "kick_members": "Кикать",
    "moderate_members": "Тайм-аут",
    "manage_messages": "Управлять сообщениями",
    "mention_everyone": "Упоминать everyone",
    "manage_webhooks": "Вебхуки",
    "view_audit_log": "Журнал аудита",
    "send_messages": "Писать",
}

_ERROR_TITLES = {
    "command": ERROR_TITLE_COMMAND,
    "component": ERROR_TITLE_COMPONENT,
    "modal": ERROR_TITLE_MODAL,
    "listener": ERROR_TITLE_LISTENER,
    "task": ERROR_TITLE_TASK,
}

_ERROR_KINDS = {
    "command": ERROR_KIND_COMMAND,
    "component": ERROR_KIND_COMPONENT,
    "modal": ERROR_KIND_MODAL,
    "listener": ERROR_KIND_LISTENER,
    "task": ERROR_KIND_TASK,
}


def error_title(kind: str) -> str:
    return _ERROR_TITLES.get(kind, ERROR_TITLE_COMMAND)


def error_kind_label(kind: str) -> str:
    return _ERROR_KINDS.get(kind, kind)


def error_perm_label(attr: str) -> str:
    return ERROR_PERM_LABELS.get(attr, attr)


def error_perm_list(attrs: Iterable[str]) -> str:
    labels = [error_perm_label(attr) for attr in attrs]
    return ", ".join(labels) if labels else ERROR_NO_PERMS


def error_slash(name: str) -> str:
    title = str(name or "").strip()
    if not title:
        return "—"
    if title.startswith("/"):
        return title
    return f"/{title}"
