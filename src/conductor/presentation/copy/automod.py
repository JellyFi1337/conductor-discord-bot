"""Тексты: automod."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from conductor.domain.automod import (
    AutomodAction,
    AutomodReason,
    DeleteMode,
    MessageFacts,
    Trigger,
)
from conductor.domain.discord_limits import AUDIT_REASON

from .common import rank_label

AUTOMOD_FILE_REJECTED_TITLE = "Файл правил не принят"

AUTOMOD_HIT_TITLE = "Сработала автомодерация"

AUTOMOD_FIELD_RULE = "Правило"

AUTOMOD_FIELD_USED = "Счёт"

AUTOMOD_FIELD_CASE = "Дело"

AUTOMOD_BYPASS = "обход лимитов автомода"

AUTOMOD_CARD_TITLE = "Автомодерация"

AUTOMOD_RELOAD = "Перечитать файл"

AUTOMOD_REQUEST = "Заявка на изменение"

AUTOMOD_ACCEPT = "Принята"

AUTOMOD_REJECT = "Отклонена"

AUTOMOD_FIELD_FILE = "Файл"

AUTOMOD_FIELD_EXEMPT = "Исключения"

AUTOMOD_FIELD_HITS = "Срабатывания"

AUTOMOD_FIELD_PROPOSAL = "Предложение"

AUTOMOD_FIELD_WHY = "Обоснование"

AUTOMOD_FIELD_STATUS = "Статус"

AUTOMOD_STATUS_NEW = "ожидает"

AUTOMOD_STATUS_ACCEPTED = "принята"

AUTOMOD_STATUS_REJECTED = "отклонена"

AUTOMOD_REQUEST_TITLE = "Заявка на автомод"

AUTOMOD_REQUEST_MODAL = "Заявка на изменение"

AUTOMOD_LABEL_RULE = "Правило"

AUTOMOD_LABEL_PROPOSAL = "Предлагаемое значение"

AUTOMOD_LABEL_REASON = "Обоснование"

AUTOMOD_REQUEST_NEED_REASON = "Нужно обоснование: что меняем и почему."

AUTOMOD_OWNER_ONLY = "Решение по заявке принимает только владелец бота."

AUTOMOD_RELOAD_OK = "Файл перечитан, карточка обновлена."

AUTOMOD_RELOAD_KEEP = "Файл не принят, действуют прежние правила."

AUTOMOD_RELOAD_MISSING = "Файла нет — карточка показывает встроенные умолчания."

AUTOMOD_REQUEST_SENT = "Заявка ушла владельцу бота."

AUTOMOD_REQUEST_DECIDED = "Заявка уже решена."

AUTOMOD_AUTHOR_ACCEPTED = "Владелец бота принял заявку. Файл правят руками, затем жмут «Перечитать»."

AUTOMOD_AUTHOR_REJECTED = "Владелец бота отклонил заявку."

_AUTOMOD_RULE_LABELS = {
    "emoji_flood": "эмодзи",
    "invite_link": "приглашение",
    "everyone_mention": "упоминание everyone",
    "duplicate": "повторы",
    "user_mentions": "упоминания участников",
    "role_mentions": "упоминания ролей",
    "gif_flood": "гифки",
}

AUTOMOD_RULE_TITLES = {
    "emoji_flood": "флуд эмодзи",
    "invite_link": "приглашение на чужой сервер",
    "everyone_mention": "упоминание @everyone",
    "duplicate": "повторы сообщений",
    "user_mentions": "упоминания участников",
    "role_mentions": "упоминания ролей",
    "gif_flood": "флуд гифками",
}

AUTOMOD_TICKET_HINT = "Если это ошибка — откройте тикет в канале обращений."

AUTOMOD_INVITE_FAIL_CLOSED = "проверку приглашения не удалось выполнить — ссылка считается чужой"


def automod_file_rejected(path: str, error: str) -> str:
    return f"файл правил не принят: {path} · {error}"


def automod_rule_label(rule: str) -> str:
    return _AUTOMOD_RULE_LABELS.get(rule, rule)


def automod_rule_title(rule: str) -> str:
    return AUTOMOD_RULE_TITLES.get(rule, automod_rule_label(rule))


def automod_hit_body(rule: str, used: int, window: int) -> str:
    return f"{automod_rule_title(rule)} · {used} за {window} мин"


def automod_reason(
    trigger: Trigger,
    facts: MessageFacts,
    *,
    rule_title: str,
    channel_name: str,
    until: datetime | None,
    ticket_hint: str = "",
    fail_closed: str = "",
    window_minutes: int = 60,
) -> AutomodReason:
    channel = _channel_tag(channel_name)
    action = _action_phrase(trigger)
    deleted = _delete_phrase(trigger)
    found = _found_phrase(trigger, facts, window_minutes)
    snippet = sanitize_snippet(facts.text)
    audit_bits = [
        "Автомод",
        rule_title,
        f"{trigger.used} из {trigger.limit} допустимо",
        action,
    ]
    if deleted:
        audit_bits.append(deleted)
    audit_bits.append(channel)
    audit = _clip(" · ".join(part for part in audit_bits if part), AUDIT_REASON)
    member_lines = [
        f"Нарушено правило: {rule_title}.",
        f"Найдено: {found}.",
        f"Канал: {channel}.",
        f"Действие: {_member_action(trigger, until)}.",
    ]
    if snippet:
        member_lines.append(f"Отрывок: {snippet}")
    if ticket_hint:
        member_lines.append(ticket_hint)
    staff_lines = [
        f"{rule_title} · {trigger.used} из {trigger.limit} за {window_minutes} мин",
        f"участник `{facts.user_id}` · канал `{facts.channel_id}` · сообщение `{facts.message_id}`",
        f"найдено: {found}",
        f"действие: {_member_action(trigger, until)}",
    ]
    if fail_closed:
        staff_lines.append(fail_closed)
    if snippet:
        staff_lines.append(f"отрывок: {snippet}")
    return AutomodReason(
        audit=audit,
        member=_break_pings("\n".join(member_lines)),
        staff=_break_pings("\n".join(staff_lines)),
    )


def sanitize_snippet(text: str, *, limit: int = 200) -> str:
    raw = str(text or "")
    raw = re.sub(r"<@!?\d+>", "@участник", raw)
    raw = re.sub(r"<@&\d+>", "@роль", raw)
    raw = re.sub(r"@everyone", "everyone", raw, flags=re.IGNORECASE)
    raw = re.sub(r"@here", "here", raw, flags=re.IGNORECASE)
    raw = " ".join(raw.split())
    return _clip(raw, limit)


def _break_pings(text: str) -> str:
    return str(text or "").replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def _clip(text: str, limit: int) -> str:
    raw = str(text or "")
    if limit <= 0 or len(raw) <= limit:
        return raw
    if limit == 1:
        return "…"
    return raw[: limit - 1] + "…"


def _channel_tag(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return "#канал"
    if raw.startswith("#"):
        return raw
    return f"#{raw}"


def _action_phrase(trigger: Trigger) -> str:
    if trigger.action is AutomodAction.TIMEOUT:
        return f"тайм-аут {trigger.timeout_minutes} мин"
    if trigger.action is AutomodAction.WARN:
        return "замечание"
    if trigger.action is AutomodAction.DELETE_ONLY:
        return "без санкции"
    return trigger.action.value


def _delete_phrase(trigger: Trigger) -> str:
    if trigger.delete is DeleteMode.MESSAGE:
        return "сообщение удалено"
    if trigger.delete is DeleteMode.DUPLICATES:
        return "повторы удалены"
    return ""


def _member_action(trigger: Trigger, until: datetime | None) -> str:
    bits: list[str] = []
    deleted = _delete_phrase(trigger)
    if deleted:
        bits.append(deleted)
    if trigger.action is AutomodAction.TIMEOUT:
        if until is not None:
            stamp = int(until.timestamp())
            bits.append(f"тайм-аут до <t:{stamp}:f>")
        else:
            bits.append(f"тайм-аут {trigger.timeout_minutes} мин")
    elif trigger.action is AutomodAction.WARN:
        bits.append("замечание")
    return ", ".join(bits) if bits else "без санкции"


def _found_phrase(trigger: Trigger, facts: MessageFacts, window_minutes: int) -> str:
    if trigger.rule == "everyone_mention":
        return "@everyone"
    if trigger.rule == "invite_link":
        code = facts.foreign_codes[0] if facts.foreign_codes else ""
        if code:
            return f"приглашение discord.gg/{code} на другой сервер"
        return "приглашение на другой сервер"
    if trigger.rule == "emoji_flood":
        return f"{trigger.used} эмодзи за {window_minutes} мин при лимите {trigger.limit}"
    if trigger.rule == "duplicate":
        return f"{trigger.used} одинаковых сообщений за {window_minutes} мин при лимите {trigger.limit}"
    if trigger.rule == "user_mentions":
        return f"{trigger.used} упоминаний участников за {window_minutes} мин при лимите {trigger.limit}"
    if trigger.rule == "role_mentions":
        return f"{trigger.used} упоминаний ролей за {window_minutes} мин при лимите {trigger.limit}"
    if trigger.rule == "gif_flood":
        return f"{trigger.used} гифок за {window_minutes} мин при лимите {trigger.limit}"
    return f"{trigger.used} за {window_minutes} мин при лимите {trigger.limit}"


def automod_used_line(used: int, window: int) -> str:
    return f"{used} за {window} мин"


def automod_rule_sentence(
    name: str,
    limit: int,
    action: str,
    delete: str,
    timeout: int,
    window: int,
) -> str:
    head = {
        "emoji_flood": f"больше {limit} эмодзи за {window} минут",
        "invite_link": (
            f"{limit} приглашение на чужой сервер за {window} минут"
            if limit == 1
            else f"{limit} приглашений на чужой сервер за {window} минут"
        ),
        "everyone_mention": (
            f"{limit} упоминание @everyone за {window} минут"
            if limit == 1
            else f"{limit} упоминаний @everyone за {window} минут"
        ),
        "duplicate": f"больше {limit} повторов за {window} минут",
        "user_mentions": f"больше {limit} упоминаний участников за {window} минут",
        "role_mentions": f"больше {limit} упоминаний ролей за {window} минут",
        "gif_flood": f"больше {limit} гифок за {window} минут",
    }.get(name, f"больше {limit} {automod_rule_label(name)} за {window} минут")
    sanction = {
        "timeout": f"тайм-аут {timeout} мин",
        "warn": "замечание",
        "delete_only": "без санкции",
        "off": "выключено",
    }.get(action, action)
    removed = {
        "message": "сообщение удаляется",
        "duplicates": "повторы удаляются",
        "none": "",
    }.get(delete, "")
    if removed:
        return f"{head} → {sanction}, {removed}"
    return f"{head} → {sanction}"


def automod_exempt_line(
    ranks: Sequence[str],
    *,
    administrators: bool,
    roles: Sequence[int] = (),
    channels: Sequence[int] = (),
) -> str:
    parts: list[str] = []
    if ranks:
        parts.append("ранги " + ", ".join(rank_label(item) for item in ranks))
    if administrators:
        parts.append("администраторы")
    if roles:
        parts.append("роли " + ", ".join(f"<@&{int(role_id)}>" for role_id in roles))
    if channels:
        parts.append("каналы " + ", ".join(f"<#{int(channel_id)}>" for channel_id in channels))
    return "исключения: " + " · ".join(parts) if parts else "исключения: нет"


def automod_hits_line(counts: Mapping[str, int]) -> str:
    bits = [f"{automod_rule_label(name)} {int(counts.get(name, 0))}" for name in _AUTOMOD_RULE_LABELS]
    return "срабатывания за 7 дней: " + " · ".join(bits)


def automod_file_line(path: str, changed: str, loaded: str) -> str:
    return f"файл: `{path}` · изменён {changed} · перечитан {loaded}"


def automod_status_label(status: str) -> str:
    return {
        "new": AUTOMOD_STATUS_NEW,
        "accepted": AUTOMOD_STATUS_ACCEPTED,
        "rejected": AUTOMOD_STATUS_REJECTED,
    }.get(status, status)


AUTOMOD_YAML = "не разобран YAML: "

AUTOMOD_FILE = "не прочитан файл: "

AUTOMOD_ERROR = "ошибка"


def automod_yaml_error(exc: object) -> str:
    return f"{AUTOMOD_YAML}{exc}"


def automod_file_error(exc: object) -> str:
    return f"{AUTOMOD_FILE}{exc}"


AUTOMOD_CFG_ERRORS = {
    "root_not_object": "корень должен быть объектом",
    "required": "обязательное поле",
    "expected_object": "ожидался объект",
    "unknown_rule": "неизвестное правило",
    "unknown_field": "неизвестное поле",
    "expected_int": "ожидалось целое число",
    "expected_bool": "ожидалось true или false",
    "expected_number": "ожидалось число",
    "must_be_positive": "должно быть > 0",
    "expected_string": "ожидалась строка",
    "invalid_value": "недопустимое значение",
    "expected_list": "ожидался список",
    "unknown_rank": "неизвестный ранг",
    "rank_none_forbidden": "ранг none нельзя исключать",
    "expected_id": "ожидалось целое id",
    "negative_id": "id не может быть отрицательным",
}


def automod_cfg_error(error: object) -> str:
    code = str(getattr(error, "message", error) or "")
    path = str(getattr(error, "path", "") or "")
    text = f"должно быть ≥ {code.split(':', 1)[1]}" if code.startswith("min:") else AUTOMOD_CFG_ERRORS.get(code, code)
    if path:
        return f"{path}: {text}"
    return text
