"""Тексты: logs."""

from __future__ import annotations

from collections.abc import Sequence

LOG_USER = "участников"

LOG_ADMIN = "администрации"

LOG_STAFF = "действий следящих"

LOG_TICKETS = "тикетов"

LOG_CHANNEL_MISSING_ACTION = "Проверьте, что канал существует и бот имеет к нему доступ."

LOG_PERM_SEND = "«Отправлять сообщения»"

LOG_PERM_EMBED = "«Встраивать ссылки»"

LOG_PACK_TITLE = "Пачка журнала"


def log_unconfigured(kind: str) -> str:
    return f"Канал логов {kind} не задан"


def log_set_action(command: str) -> str:
    return f"Укажите канал командой {command}."


def log_invisible(kind: str) -> str:
    return f"Канал логов {kind} задан, но бот его не видит"


def log_cannot_write(kind: str, name: str) -> str:
    return f"Нет права писать в логи {kind} (#{name})"


def log_grant(missing: Sequence[str]) -> str:
    return "Выдайте боту в этом канале: " + " и ".join(missing) + "."


def log_ok(kind: str, name: str) -> str:
    return f"Логи {kind}: #{name} — бот может писать"


AUDIT_LABELS: dict[str, str] = {
    "guild_update": "Изменения сервера",
    "channel_create": "Создан канал",
    "channel_update": "Изменён канал",
    "channel_delete": "Удалён канал",
    "overwrite_create": "Добавлены права канала",
    "overwrite_update": "Изменены права канала",
    "overwrite_delete": "Сняты права канала",
    "kick": "Кик с сервера",
    "member_prune": "Исключение неактивных",
    "ban": "Бан",
    "unban": "Разбан",
    "member_update": "Изменение участника",
    "member_role_update": "Выдача / снятие ролей",
    "member_move": "Перемещение по голосовым",
    "member_disconnect": "Кик из голосового",
    "bot_add": "Добавлен бот",
    "role_create": "Создана роль",
    "role_update": "Изменена роль",
    "role_delete": "Удалена роль",
    "invite_create": "Создан инвайт",
    "invite_update": "Изменён инвайт",
    "invite_delete": "Удалён инвайт",
    "webhook_create": "Создан вебхук",
    "webhook_update": "Изменён вебхук",
    "webhook_delete": "Удалён вебхук",
    "emoji_create": "Создан эмодзи",
    "emoji_update": "Изменён эмодзи",
    "emoji_delete": "Удалён эмодзи",
    "message_delete": "Модератор удалил сообщение",
    "message_bulk_delete": "Массовое удаление",
    "message_pin": "Сообщение закреплено",
    "message_unpin": "Сообщение откреплено",
    "integration_create": "Добавлена интеграция",
    "integration_update": "Изменена интеграция",
    "integration_delete": "Удалена интеграция",
    "stage_instance_create": "Трибуна создана",
    "stage_instance_update": "Трибуна изменена",
    "stage_instance_delete": "Трибуна удалена",
    "sticker_create": "Создан стикер",
    "sticker_update": "Изменён стикер",
    "sticker_delete": "Удалён стикер",
    "scheduled_event_create": "Ивент создан",
    "scheduled_event_update": "Ивент изменён",
    "scheduled_event_delete": "Ивент удалён",
    "thread_create": "Тред создан",
    "thread_update": "Тред изменён",
    "thread_delete": "Тред удалён",
    "app_command_permission_update": "Права слэш-команд",
    "soundboard_sound_create": "Звук панели создан",
    "soundboard_sound_update": "Звук панели изменён",
    "soundboard_sound_delete": "Звук панели удалён",
    "automod_rule_create": "Автомод: правило создано",
    "automod_rule_update": "Автомод: правило изменено",
    "automod_rule_delete": "Автомод: правило удалено",
    "onboarding_prompt_create": "Онбординг: вопрос создан",
    "onboarding_prompt_update": "Онбординг: вопрос изменён",
    "onboarding_prompt_delete": "Онбординг: вопрос удалён",
    "onboarding_create": "Онбординг создан",
    "onboarding_update": "Онбординг изменён",
    "home_settings_create": "Настройки главной созданы",
    "home_settings_update": "Настройки главной изменены",
}


LOG_JOIN_UNKNOWN = "бот не видел вход"

LOG_EMPTY = "*пусто*"

LOG_NICK = "Ник"

LOG_AVATAR = "Аватар"

LOG_AVATAR_UPDATED = "обновлён"

LOG_RULES = "Проверка правил"

LOG_RULES_OK = "прошёл"

LOG_BOOST = "Буст"

LOG_BOOST_OFF = "снят"

LOG_VOICE = "Голосовой канал"

LOG_VOICE_JOIN = "Вошёл в голосовой"

LOG_VOICE_LEAVE = "Вышел из голосового"

LOG_VOICE_MOVE = "Сменил голосовой"

LOG_NAME = "имя: "

LOG_ARCHIVED = "архивирован"

LOG_UNARCHIVED = "разархивирован"

LOG_CLOSED = "закрыт"

LOG_OPEN = "открыт"

LOG_TOPIC = "описание изменено"

LOG_NSFW = "возрастная метка: "

LOG_ON = "вкл"

LOG_OFF = "выкл"

LOG_OVERWRITES = "права канала изменены"

LOG_ICON = "иконка изменена"

LOG_VANITY = "короткая ссылка: "

LOG_PREMIUM = "уровень буста: "


def audit_title(action: str) -> str:
    return AUDIT_LABELS.get(action, action.replace("_", " "))


def log_named(label: str, value: object) -> str:
    return f"{label}{value}"


def log_nsfw(on: bool) -> str:
    return f"{LOG_NSFW}{LOG_ON if on else LOG_OFF}"


def log_archived(on: bool) -> str:
    return LOG_ARCHIVED if on else LOG_UNARCHIVED


def log_locked(on: bool) -> str:
    return LOG_CLOSED if on else LOG_OPEN


STRUCTURE_TITLE = "Каркас изменился"

STRUCTURE_RANK_TITLE = "Ранговая роль удалена"

STRUCTURE_RENAME_TITLE = "Канал каркаса переименован"

STRUCTURE_FIX = "Восстановить"

STRUCTURE_FIX_RANK = "Пересоздать роль и вернуть ранг"

STRUCTURE_PANEL_STALE = "Витрина погасла: сообщение в канале больше не существует."


def structure_lost_body(
    *,
    label: str,
    feature: str,
    bindings: object = (),
    panels: int = 0,
    holders: int = 0,
    rank: str = "",
    subsystem: str = "",
) -> str:
    lines = [f"Исчезло: {label}."]
    if feature:
        lines.append(f"Функция: {feature}.")
    cleared = [str(item) for item in bindings if item]
    if cleared:
        lines.append(f"Обнулены привязки: {', '.join(cleared)}.")
    if panels:
        lines.append(f"Погашено панелей: {panels}.")
    if rank:
        lines.append(f"Ранг {rank} снят с {holders} человек.")
    if subsystem:
        lines.append(f"Подсистема ролей: {subsystem}.")
    return "\n".join(lines)


def structure_renamed_body(*, label: str, old: str, new: str) -> str:
    return f"{label}: было «{old}», стало «{new}». Привязка по id сохранена."


def structure_more(rest: int) -> str:
    return f"и ещё {rest}"


def structure_restored(label: str, verdict: object) -> str:
    return f"{label}: {verdict}"
