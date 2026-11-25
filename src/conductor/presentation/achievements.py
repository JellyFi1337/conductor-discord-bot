"""Карточки достижений. Без sqlalchemy и без services."""

from __future__ import annotations

from discord import ui

from conductor.domain.achievements import AchievementCard, AchievementLine
from conductor.domain.ladder import ladder_bar
from conductor.presentation import copy
from conductor.presentation.layout import panel
from conductor.presentation.tokens import Icon, Tone


def grant_view(name: str, user_id: int | None = None) -> ui.LayoutView:
    return panel(
        copy.achievement_star(name),
        copy.achievement_grant_body(user_id),
        tone=Tone.SPECIAL,
        icon=Icon.STAR,
        timeout=None,
    )


def profile_lines(card: AchievementCard) -> list[str]:
    lines: list[str] = []
    for item in card.lines:
        lines.extend(_line(item))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines or [copy.ACHIEVEMENT_EMPTY]


def _line(item: AchievementLine) -> list[str]:
    title = f"**{item.title}:** {item.count}"
    if item.stage_name:
        title = f"{title} · {item.stage_name}"
    rows = [title, ladder_bar(item.have, item.need)]
    if item.next_name:
        rows.append(copy.achievement_until(item.next_name))
    else:
        rows.append(copy.ACHIEVEMENT_MAX)
    return rows
