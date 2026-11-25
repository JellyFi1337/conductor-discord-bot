from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import discord
import structlog
from discord import ui

from conductor.domain.outcome import Teardown
from conductor.presentation.layout import report_ui_error, respond

log = structlog.get_logger("conductor.interact")
STALE_INTERACTION = frozenset({40060, 10062, 50027})
GONE = frozenset({10003, 10008})

Handler = Callable[[discord.Interaction], Awaitable[None]]

__all__ = [
    "GONE",
    "STALE_INTERACTION",
    "ack",
    "acked",
    "defer_update",
    "instant",
    "interaction_age_ms",
    "respond_late",
    "run_teardown",
    "split_teardown",
    "stale_log_fields",
]


def interaction_age_ms(interaction: discord.Interaction) -> int | None:
    created = getattr(interaction, "created_at", None)
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - created).total_seconds() * 1000))


def stale_log_fields(interaction: discord.Interaction) -> dict[str, object]:
    command = getattr(getattr(interaction, "command", None), "qualified_name", None)
    data = getattr(interaction, "data", None)
    custom_id = None
    if isinstance(data, dict):
        custom_id = data.get("custom_id")
    elif data is not None:
        custom_id = getattr(data, "custom_id", None)
    return {
        "command": command,
        "custom_id": custom_id,
        "age_ms": interaction_age_ms(interaction),
    }


def _log_stale(interaction: discord.Interaction, code: int) -> None:
    log.warning("interaction_stale", code=code, **stale_log_fields(interaction))


async def ack(interaction: discord.Interaction, *, ephemeral: bool = True) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except discord.HTTPException as exc:
        if exc.code in STALE_INTERACTION:
            _log_stale(interaction, exc.code)
            return interaction.response.is_done()
        raise


async def defer_update(interaction: discord.Interaction) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer()
        return True
    except discord.HTTPException as exc:
        if exc.code in STALE_INTERACTION:
            _log_stale(interaction, exc.code)
            return interaction.response.is_done()
        raise


def acked(handler: Handler, *, ephemeral: bool = True) -> Handler:
    """Ack, затем handler. Ошибки — в CriticalLog."""

    @wraps(handler)
    async def wrapped(interaction: discord.Interaction) -> None:
        try:
            if not await ack(interaction, ephemeral=ephemeral):
                return
            await handler(interaction)
        except Exception as exc:
            await report_ui_error(interaction, exc)

    return wrapped


def instant(handler: Handler) -> Handler:
    """Без ack: handler сам отвечает или открывает модалку. Ошибки — в CriticalLog."""

    @wraps(handler)
    async def wrapped(interaction: discord.Interaction) -> None:
        try:
            await handler(interaction)
        except Exception as exc:
            await report_ui_error(interaction, exc)

    return wrapped


async def respond_late(
    interaction: discord.Interaction,
    view: ui.LayoutView,
    *,
    ephemeral: bool = True,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> None:
    """Как respond, но 10003/10008/устаревшее взаимодействие только пишутся в лог."""
    try:
        await respond(interaction, view, ephemeral=ephemeral, mentions=mentions, **extra)
    except discord.HTTPException as exc:
        if exc.code in STALE_INTERACTION | GONE:
            log.info("interaction_gone", code=exc.code, **stale_log_fields(interaction))
            return
        raise


def split_teardown(result: object) -> tuple[object, Teardown | None]:
    if isinstance(result, tuple) and len(result) == 2:
        body, teardown = result
        if teardown is None or callable(teardown):
            return body, teardown  # type: ignore[return-value]
    return result, None


async def run_teardown(teardown: Teardown | None) -> None:
    if teardown is None:
        return
    try:
        await teardown()
    except Exception:
        log.exception("teardown_failed")
