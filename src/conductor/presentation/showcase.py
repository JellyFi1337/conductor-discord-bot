"""Отображение статуса витрин: иконка и строка. Без sqlalchemy и без services."""

from __future__ import annotations

from conductor.domain.showcase import ShowcaseState
from conductor.presentation import copy
from conductor.presentation.tokens import Icon

__all__ = ["SHOWCASE_ICONS", "showcase_line"]

SHOWCASE_ICONS: dict[ShowcaseState, str] = {
    ShowcaseState.LIVE: Icon.DONE,
    ShowcaseState.MOVED: Icon.WARN,
    ShowcaseState.STALE: Icon.WARN,
    ShowcaseState.MISSING: Icon.DENY,
    ShowcaseState.OFF: Icon.DENY,
}


def showcase_line(state: ShowcaseState, channel_id: int | None) -> str:
    icon = SHOWCASE_ICONS[state]
    label = copy.SHOWCASE_STATES.get(state.value, state.value)
    mention = f"<#{int(channel_id)}>" if channel_id else "—"
    return f"{icon} {label} · {mention}"
