"""Визуальные токены: тон, значок, полоса. Сырых 0x в cog'ах нет."""

from __future__ import annotations

from enum import Enum


class Icon:
    """Ровно один значок в начале заголовка. Второго в строке не бывает."""

    PANEL = "▸"
    DONE = "✓"
    WARN = "⚠"
    DENY = "✕"
    WAIT = "⏳"
    LOCK = "🔒"
    CASE = "⚑"
    STAR = "★"
    SHIFT = "◷"
    STEP = "▫"


class Tone(Enum):
    """Акцент контейнера: цвет и значок заголовка. Семь, и седьмой только для «идёт»."""

    INFO = ("info", 0x5865F2, Icon.PANEL)
    OK = ("ok", 0x57F287, Icon.DONE)
    WARN = ("warn", 0xFEE75C, Icon.WARN)
    DANGER = ("danger", 0xED4245, Icon.DENY)
    MUTED = ("muted", 0x99AAB5, Icon.LOCK)
    SPECIAL = ("special", 0xEB459F, Icon.STAR)
    RUNNING = ("running", 0x4E5D94, Icon.WAIT)

    @property
    def colour(self) -> int:
        return self.value[1]

    @property
    def icon(self) -> str:
        return self.value[2]


def meter(done: int, total: int, width: int = 10) -> str:
    """Полоса ширины `width` и доля рядом. Без числа на телефоне не читается."""
    if width < 1:
        width = 1
    if total <= 0:
        filled = 0
        pct = 0
    else:
        clamped = min(max(done, 0), total)
        filled = round(width * clamped / total)
        filled = min(max(filled, 0), width)
        pct = round(100 * clamped / total)
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar}  {pct}%"
