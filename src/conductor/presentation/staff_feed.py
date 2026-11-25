"""Карточка F канала действий следящих. Без sqlalchemy и без services."""

from __future__ import annotations

from discord import ui

from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone


def staff_feed_card(
    *,
    actor_id: int,
    rank: str,
    action: str,
    where: str | None = None,
    details: str | None = None,
    chief_role_id: int | None = None,
    ping_chief: bool = False,
) -> ui.LayoutView:
    fields: dict[str, object] = {
        copy.STAFF_FEED_WHO: f"<@{int(actor_id)}> · {rank}",
        copy.STAFF_FEED_ACTION: copy.staff_action_label(action),
    }
    if where:
        fields[copy.STAFF_FEED_WHERE] = where
    if details:
        fields[copy.STAFF_FEED_DETAILS] = details
    body = copy.staff_feed_chief(chief_role_id) if chief_role_id else ""
    return event(
        copy.STAFF_FEED_TITLE,
        body,
        tone=Tone.DANGER if ping_chief else Tone.INFO,
        fields=fields,
        source=copy.STAFF_FEED_SOURCE,
    )
