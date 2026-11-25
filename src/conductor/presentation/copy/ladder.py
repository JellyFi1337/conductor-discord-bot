"""Тексты: ladder."""

from __future__ import annotations

from collections.abc import Sequence

from conductor.presentation.icons import STEP_EMOJI, marked

from .common import _and_parts, _or_parts, grouped

LADDER_TITLE = "Ваш путь на сервере"

LADDER_NOW = "Сейчас"

LADDER_MESSAGES = "Сообщения"

LADDER_VOICE = "Голосовой"

LADDER_TENURE = "На сервере"

LADDER_REACTIONS = "Реакции"

LADDER_HOURS = "ч"

LADDER_MINUTES = "мин"

LADDER_DAYS = "дн"

LADDER_FROZEN = "ступень заморожена"

LADDER_NO_DEMOTION = "ступень не понижается"

LADDER_TOP = "высшая ступень"

LADDER_READY = "условия «{step}» уже выполнены"

LADDER_LEFT = "до «{step}» осталось {gap}"

LADDER_CONFIG_TITLE = "Лестница активности"

LADDER_CONFIG_FOOTNOTE = "меняется в /admin panel → Лестница"

LADDER_UNBOUND = "Роли лестницы активности не привязаны"

LADDER_UNBOUND_ACTION = "Привяжите ступени в /admin panel → Лестница."

LADDER_STALE_ACTION = "Роль удалили или id устарел. Привяжите заново: /admin panel → Лестница."

LADDER_ROLE_GONE = "Лестница: роль удалена с сервера"

LADDER_ABOVE_OK = "Роль бота выше ролей лестницы активности"


LADDER_STEP_GUEST = "Гость"

LADDER_STEP_REGULAR = "Постоялец"

LADDER_STEP_RESIDENT = "Житель"

LADDER_STEP_FRIEND = "Друг сервера"

LADDER_STEPS = {
    "guest": LADDER_STEP_GUEST,
    "regular": LADDER_STEP_REGULAR,
    "resident": LADDER_STEP_RESIDENT,
    "friend": LADDER_STEP_FRIEND,
}


def ladder_title(target: str) -> str:
    return LADDER_TITLE if target in {"", "вы"} else f"{LADDER_TITLE} · {target}"


def ladder_step(step: object) -> str:
    key = getattr(step, "key", None) or str(step)
    return marked(STEP_EMOJI.get(str(key), ""), LADDER_STEPS.get(str(key), str(step)))


def ladder_below(names: Sequence[str]) -> str:
    return "Роль бота ниже ролей лестницы: " + ", ".join(names)


def ladder_gap_messages(count: int) -> str:
    return f"{grouped(count)} сообщений"


def ladder_gap_minutes(count: int) -> str:
    return f"{grouped(count)} мин голосового"


def ladder_gap_hours(count: int) -> str:
    return f"{grouped(count)} ч голосового"


def ladder_gap_reactions(count: int) -> str:
    return f"{grouped(count)} реакций"


def ladder_gap_days(count: int) -> str:
    return f"{grouped(count)} дней на сервере"


def ladder_tenure_line(*, have: int, need: int) -> str:
    return f"{grouped(have)} дней из {grouped(need)}"


def ladder_footnote(card: object) -> str:
    parts: list[str] = []
    if bool(getattr(card, "sinner", False)):
        parts.append(LADDER_FROZEN)
    need = getattr(card, "need", None)
    nxt = None if need is None else getattr(need, "step", None)
    gap = None if need is None else getattr(need, "gap", None)
    if nxt is None:
        parts.append(LADDER_TOP)
    elif gap is None:
        parts.append(LADDER_READY.format(step=ladder_step(nxt)))
    else:
        parts.append(LADDER_LEFT.format(step=ladder_step(nxt), gap=ladder_gap_phrase(gap)))
    parts.append(LADDER_NO_DEMOTION)
    return " · ".join(parts)


def ladder_gap_phrase(gap: object) -> str:
    remaining = int(getattr(gap, "remaining", 0) or 0)
    kind = getattr(getattr(gap, "kind", None), "value", getattr(gap, "kind", ""))
    if kind == "messages":
        return ladder_gap_messages(remaining)
    if kind == "voice":
        if remaining >= 3600:
            return ladder_gap_hours(remaining // 3600)
        return ladder_gap_minutes(max(1, remaining // 60))
    if kind == "reactions":
        return ladder_gap_reactions(remaining)
    return ladder_gap_days(remaining)


def ladder_config_body(panel: object) -> str:
    config = getattr(panel, "config", panel)
    roles = getattr(panel, "roles", None)

    def _role(step: str) -> str:
        role_id = None if roles is None else getattr(roles, step, None)
        return f"<@&{role_id}>" if role_id else "не задана"

    regular = _or_parts(
        (int(getattr(config, "regular_messages", 0) or 0), "сообщ."),
        (int(getattr(config, "regular_voice_seconds", 0) or 0) // 60, "мин"),
        (int(getattr(config, "regular_reactions", 0) or 0), "реакций"),
    )
    resident = _and_parts(
        (int(getattr(config, "resident_messages", 0) or 0), "сообщ."),
        (int(getattr(config, "resident_voice_seconds", 0) or 0) // 3600, "ч"),
        (int(getattr(config, "resident_days", 0) or 0), "дн."),
    )
    strong = _or_parts(
        (int(getattr(config, "resident_strong_messages", 0) or 0), "сообщ."),
        (int(getattr(config, "resident_strong_voice_seconds", 0) or 0) // 3600, "ч"),
    )
    if strong:
        resident = f"{resident}; или {strong}" if resident else strong
    friend = _and_parts(
        (int(getattr(config, "friend_tenure_days", 0) or 0), "дн."),
        (int(getattr(config, "friend_messages", 0) or 0), "сообщ."),
        (int(getattr(config, "friend_voice_seconds", 0) or 0) // 3600, "ч"),
    )
    react = int(getattr(config, "friend_reactions", 0) or 0)
    if react > 0:
        extra = f"{react} реакций"
        friend = f"{friend} или {extra}" if friend else extra
    return (
        f"**{ladder_step('guest')}:** {_role('guest')}\n"
        f"**{ladder_step('regular')}:** {_role('regular')} · {regular or '—'}\n"
        f"**{ladder_step('resident')}:** {_role('resident')} · {resident or '—'}\n"
        f"**{ladder_step('friend')}:** {_role('friend')} · {friend or '—'}"
    )
