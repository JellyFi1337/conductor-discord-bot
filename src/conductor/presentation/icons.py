"""Имена эмодзи приложения и резолвер для карточек.

Реестр живёт в motion.EmojiRegistry. Сюда бот кладёт resolve после refresh.
Без привязки mark() возвращает запасной текст — тесты и холодный старт не ломаются.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from conductor.presentation.tokens import Icon

ICON_EMOJI: dict[str, str] = {
    Icon.PANEL: "ico_panel",
    Icon.DONE: "tick",
    Icon.WARN: "ico_warn",
    Icon.DENY: "cross",
    Icon.WAIT: "load",
    Icon.LOCK: "ico_lock",
    Icon.CASE: "ico_case",
    Icon.STAR: "ico_star",
    Icon.SHIFT: "ico_shift",
    Icon.STEP: "ico_step",
}

TIER_EMOJI: dict[str, str] = {
    "ruby_old": "gem_ruby",
    "emerald_old": "gem_emerald",
    "sapphire_old": "gem_sapphire",
    "amethyst_old": "gem_amethyst",
}

STEP_EMOJI: dict[str, str] = {
    "guest": "lad_guest",
    "regular": "lad_regular",
    "resident": "lad_resident",
    "friend": "lad_friend",
}

RANK_EMOJI: dict[str, str] = {
    "watcher": "rnk_watch",
    "senior": "rnk_senior",
    "chief": "rnk_chief",
    "special": "rnk_special",
}

_resolve: Callable[[str], str] | None = None
_button: Callable[[str], Any] | None = None


def bind(
    *,
    resolve: Callable[[str], str] | None = None,
    button: Callable[[str], Any] | None = None,
) -> None:
    global _resolve, _button
    _resolve = resolve
    _button = button


def render_icon(mark: str) -> str:
    if not mark:
        return ""
    if _resolve is None:
        return mark
    return _resolve(mark)


def button_emoji(name: str) -> Any:
    if not name or _button is None:
        return None
    return _button(name)


def marked(name: str, text: str) -> str:
    """Префикс загруженным эмодзи. Если значка нет — только текст."""
    if not name:
        return text
    mark = render_icon(name)
    if not mark or mark == name:
        return text
    return f"{mark} {text}"
