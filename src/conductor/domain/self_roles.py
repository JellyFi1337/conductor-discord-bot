"""Роли по выбору: диф набора, теги и отпечаток карточки. Без Discord и без БД."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DESC_MAX",
    "LABEL_MAX",
    "MENU_OPTION_MAX",
    "DEFAULT_GENERAL_OPTIONS",
    "DEFAULT_MENUS",
    "MenuKind",
    "OptionTag",
    "SelfRoleOption",
    "card_fingerprint",
    "default_label_for",
    "desired_diff",
    "parse_tag",
    "sanitize_description",
    "sanitize_emoji",
    "sanitize_label",
]

LABEL_MAX = 80
DESC_MAX = 100
MENU_OPTION_MAX = 25
EMOJI_MAX = 64


class MenuKind(StrEnum):
    GENERAL = "general"
    GAME = "game"


class OptionTag(StrEnum):
    NONE = "none"
    POLLS = "polls"
    YOUTUBE = "youtube"
    ANNOUNCE = "announce"
    LFG = "lfg"


DEFAULT_MENUS: tuple[tuple[MenuKind, str], ...] = (
    (MenuKind.GENERAL, "Подписки"),
    (MenuKind.GAME, "Игры"),
)

DEFAULT_GENERAL_OPTIONS: tuple[tuple[str, OptionTag], ...] = (
    ("Новые видео", OptionTag.YOUTUBE),
    ("Опросы", OptionTag.POLLS),
    ("Анонсы", OptionTag.ANNOUNCE),
    ("Поиск игры", OptionTag.LFG),
)


@dataclass(frozen=True, slots=True)
class SelfRoleOption:
    option_id: int
    menu_id: int
    role_id: int
    label: str
    description: str = ""
    emoji: str | None = None
    tag: OptionTag = OptionTag.NONE
    position: int = 0
    opened: tuple[str, ...] = ()


def desired_diff(
    *,
    have: Iterable[int],
    menu: Iterable[int],
    want: Iterable[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    held = {int(role_id) for role_id in have}
    owned = {int(role_id) for role_id in menu}
    desired = {int(role_id) for role_id in want if int(role_id) in owned}
    add = tuple(sorted(desired - held))
    remove = tuple(sorted((held & owned) - desired))
    return add, remove


def parse_tag(raw: str | None) -> OptionTag | None:
    token = str(raw or "").strip().lower()
    if not token:
        return OptionTag.NONE
    for item in OptionTag:
        if item.value == token:
            return item
    return None


def default_label_for(tag: OptionTag) -> str | None:
    for label, known in DEFAULT_GENERAL_OPTIONS:
        if known is tag:
            return label
    return None


def sanitize_label(raw: str | None) -> str | None:
    cleaned = " ".join(str(raw or "").split())
    if not cleaned:
        return None
    return cleaned[:LABEL_MAX]


def sanitize_description(raw: str | None) -> str:
    return " ".join(str(raw or "").split())[:DESC_MAX]


def sanitize_emoji(raw: str | None) -> str | None:
    cleaned = str(raw or "").strip()
    if not cleaned:
        return None
    return cleaned[:EMOJI_MAX]


def card_fingerprint(
    *,
    kind: str,
    title: str,
    options: Sequence[tuple[int, str, str, str | None, tuple[str, ...]]],
) -> str:
    parts = [str(kind), str(title)]
    for role_id, label, description, emoji, opened in options:
        parts.append(f"{int(role_id)}|{label}|{description}|{emoji or ''}|{'·'.join(opened)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
