"""Массовый пинг пачками не больше 100 упоминаний в сообщении."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import discord
import structlog
from discord import ui

from conductor.domain.traffic import Lane
from conductor.presentation.layout import Card, send_card
from conductor.presentation.mentions import mention_batches
from conductor.services.traffic import lane

log = structlog.get_logger("conductor.broadcast")

__all__ = ["BroadcastReport", "ping_many"]


@dataclass(frozen=True, slots=True)
class BroadcastReport:
    batches: int
    sent: int
    failed: int
    users: int


def _unique_ids(user_ids: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in user_ids:
        value = int(raw)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _present_ids(guild: object | None, user_ids: Sequence[int]) -> list[int]:
    ordered = _unique_ids(user_ids)
    if not getattr(guild, "chunked", False):
        return ordered
    getter = getattr(guild, "get_member", None)
    if not callable(getter):
        return ordered
    present: list[int] = []
    for user_id in ordered:
        if getter(user_id) is not None:
            present.append(user_id)
    return present


def _batch_card(text: str, mentions: discord.AllowedMentions) -> Card:
    view = Card(timeout=None, mentions=mentions)
    view.add_item(ui.Container(ui.TextDisplay(text)))
    return view


async def ping_many(
    bot: object,
    channel: Any,
    user_ids: Sequence[int],
    *,
    header: ui.LayoutView | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> BroadcastReport:
    """Заголовок один раз, затем пачки упоминаний под полосой BACKGROUND."""
    del bot
    ids = _present_ids(getattr(channel, "guild", None), user_ids)
    batches = mention_batches(ids)
    if header is None:
        log.warning("broadcast_no_header", users=len(ids), batches=len(batches))
        return BroadcastReport(batches=len(batches), sent=0, failed=0, users=0)
    with lane(Lane.BACKGROUND):
        try:
            await send_card(channel, header)
        except discord.HTTPException as exc:
            log.warning("broadcast_header_failed", error=str(exc))
            raise
        sent = 0
        failed = 0
        users = 0
        total = len(batches)
        for index, (text, mentions) in enumerate(batches, start=1):
            view = _batch_card(text, mentions)
            try:
                await send_card(channel, view)
            except discord.HTTPException as exc:
                log.warning("broadcast_batch_failed", index=index, total=total, error=str(exc))
                failed += 1
            else:
                sent += 1
                mentioned = getattr(mentions, "users", ()) or ()
                users += len(list(mentioned))
            if progress is not None:
                try:
                    await progress(index, total)
                except Exception:
                    log.exception("broadcast_progress_failed", index=index, total=total)
        return BroadcastReport(batches=total, sent=sent, failed=failed, users=users)
