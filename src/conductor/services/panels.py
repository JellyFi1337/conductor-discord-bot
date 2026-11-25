"""Хранение постоянных панелей и проверка, что сообщение ещё живо."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import Panel
from conductor.db.session import session_scope
from conductor.presentation.persistent import PANEL_LIVE, PANEL_STALE
from conductor.services.runtime import guild_is_active, log_tick

if TYPE_CHECKING:
    from conductor.bot import Conductor

log = structlog.get_logger("conductor.panels")

MessageProbe = Callable[[int, int], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RestoreReport:
    live: int
    stale: int
    checked: int = 0


async def record_panel(
    session: AsyncSession,
    *,
    kind: str,
    guild_id: int,
    channel_id: int,
    message_id: int,
    ref: int | str,
    created_by: int,
    now: datetime | None = None,
) -> Panel:
    row = await session.scalar(select(Panel).where(Panel.guild_id == guild_id, Panel.message_id == message_id))
    stamp = now or datetime.now(UTC)
    if row is None:
        row = Panel(
            kind=kind,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            ref=str(ref),
            created_by=created_by,
            created_at=stamp,
            state=PANEL_LIVE,
        )
        session.add(row)
        await session.flush()
        return row
    row.kind = kind
    row.channel_id = channel_id
    row.ref = str(ref)
    row.created_by = created_by
    row.state = PANEL_LIVE
    await session.flush()
    return row


async def list_live_panels(session: AsyncSession) -> list[Panel]:
    rows = (await session.scalars(select(Panel).where(Panel.state == PANEL_LIVE))).all()
    return list(rows)


async def find_live_kind(
    session: AsyncSession,
    *,
    guild_id: int,
    kind: str,
) -> Panel | None:
    return await session.scalar(
        select(Panel)
        .where(Panel.guild_id == guild_id, Panel.kind == kind, Panel.state == PANEL_LIVE)
        .order_by(Panel.created_at.desc())
    )


async def find_live_panel(
    session: AsyncSession,
    *,
    guild_id: int,
    kind: str,
    ref: int | str,
) -> Panel | None:
    return await session.scalar(
        select(Panel)
        .where(
            Panel.guild_id == guild_id,
            Panel.kind == kind,
            Panel.ref == str(ref),
            Panel.state == PANEL_LIVE,
        )
        .order_by(Panel.created_at.desc())
    )


async def find_usable_kind(
    session: AsyncSession,
    *,
    guild_id: int,
    kind: str,
) -> Panel | None:
    """Живая панель с сообщением, иначе последняя с реальным message_id."""
    live = await find_live_kind(session, guild_id=guild_id, kind=kind)
    if live is not None and int(live.message_id) > 0:
        return live
    stored = await session.scalar(
        select(Panel)
        .where(Panel.guild_id == guild_id, Panel.kind == kind, Panel.message_id > 0)
        .order_by(Panel.created_at.desc())
    )
    return stored if stored is not None else live


async def find_usable_panel(
    session: AsyncSession,
    *,
    guild_id: int,
    kind: str,
    ref: int | str,
) -> Panel | None:
    live = await find_live_panel(session, guild_id=guild_id, kind=kind, ref=ref)
    if live is not None and int(live.message_id) > 0:
        return live
    stored = await session.scalar(
        select(Panel)
        .where(
            Panel.guild_id == guild_id,
            Panel.kind == kind,
            Panel.ref == str(ref),
            Panel.message_id > 0,
        )
        .order_by(Panel.created_at.desc())
    )
    return stored if stored is not None else live


async def mark_stale_by_ref(
    session: AsyncSession,
    *,
    guild_id: int,
    kind: str,
    ref: int | str,
) -> int:
    result = await session.execute(
        update(Panel)
        .where(
            Panel.guild_id == guild_id,
            Panel.kind == kind,
            Panel.ref == str(ref),
            Panel.state == PANEL_LIVE,
        )
        .values(state=PANEL_STALE)
    )
    return int(result.rowcount or 0)


async def mark_stale_by_channel(session: AsyncSession, *, guild_id: int, channel_id: int) -> int:
    result = await session.execute(
        update(Panel)
        .where(
            Panel.guild_id == guild_id,
            Panel.channel_id == channel_id,
            Panel.state == PANEL_LIVE,
        )
        .values(state=PANEL_STALE)
    )
    return int(result.rowcount or 0)


async def mark_stale(session: AsyncSession, panel_ids: list[int]) -> int:
    if not panel_ids:
        return 0
    result = await session.execute(
        update(Panel).where(Panel.id.in_(panel_ids), Panel.state == PANEL_LIVE).values(state=PANEL_STALE)
    )
    return int(result.rowcount or 0)


async def mark_live(session: AsyncSession, panel_ids: list[int]) -> int:
    if not panel_ids:
        return 0
    result = await session.execute(
        update(Panel).where(Panel.id.in_(panel_ids), Panel.state == PANEL_STALE).values(state=PANEL_LIVE)
    )
    return int(result.rowcount or 0)


PROBE_BATCH = 20


async def restore_panels(
    bot: Conductor,
    *,
    probe: MessageProbe | None = None,
) -> RestoreReport:
    async with session_scope(bot.sessions) as session:
        live = await list_live_panels(session)
        snapshot = [(row.id, row.kind, row.guild_id, row.channel_id, row.message_id, row.ref) for row in live]
    if probe is None:
        kept = sum(1 for _panel_id, _kind, guild_id, *_rest in snapshot if guild_is_active(bot, guild_id))
        return RestoreReport(live=kept, stale=0, checked=len(snapshot))
    return await _probe_rows(bot, snapshot, probe=probe)


async def probe_tick(
    bot: Conductor,
    *,
    limit: int = PROBE_BATCH,
    probe: MessageProbe | None = None,
) -> RestoreReport:
    if getattr(bot, "_booted", True) is False:
        return RestoreReport(live=0, stale=0, checked=0)
    after = int(getattr(bot, "_panel_probe_after", 0) or 0)
    async with session_scope(bot.sessions) as session:
        live = await list_live_panels(session)
        ordered = sorted(live, key=lambda row: int(row.id))
        tail = [row for row in ordered if int(row.id) > after]
        head = [row for row in ordered if int(row.id) <= after]
        wrapped = False
        if tail:
            chosen = tail[:limit]
            if len(chosen) < limit and head:
                chosen = [*chosen, *head[: limit - len(chosen)]]
                wrapped = True
        else:
            chosen = head[:limit]
            wrapped = after > 0 and bool(chosen)
        snapshot = [(row.id, row.kind, row.guild_id, row.channel_id, row.message_id, row.ref) for row in chosen]
    if snapshot:
        bot._panel_probe_after = int(snapshot[-1][0])
    if wrapped:
        log.debug("panels_probe_circle", checked=len(snapshot), after=after)
    check = probe or (lambda channel_id, message_id: _discord_message_alive(bot, channel_id, message_id))
    return await _probe_rows(bot, snapshot, probe=check)


async def _probe_rows(
    bot: Conductor,
    snapshot: list[tuple[int, str, int, int, int, str]],
    *,
    probe: MessageProbe,
) -> RestoreReport:
    stale_ids: list[int] = []
    stale_meta: list[tuple[int, str, int, int, str]] = []
    kept = 0
    for panel_id, kind, guild_id, channel_id, message_id, ref in snapshot:
        if not guild_is_active(bot, guild_id):
            continue
        alive = await probe(channel_id, message_id)
        if not alive:
            stale_ids.append(panel_id)
            stale_meta.append((panel_id, kind, guild_id, channel_id, ref))
            continue
        kept += 1
    if stale_ids:
        async with session_scope(bot.sessions) as session:
            await mark_stale(session, stale_ids)
        log.info("panels_marked_stale", count=len(stale_ids), ids=stale_ids)
        await _notify_stale_panels(bot, stale_meta)
    else:
        log_tick(log, "panels_probe", changed=0, checked=len(snapshot))
    return RestoreReport(live=kept, stale=len(stale_ids), checked=len(snapshot))


async def _notify_stale_panels(bot: Any, rows: list[tuple[int, str, int, int, str]]) -> None:
    if not rows:
        return
    from conductor.domain.seed import SkeletonKind
    from conductor.services.structure import StructureLoss

    watch = getattr(bot, "structure", None)
    notify = getattr(watch, "notify_many", None)
    get_guild = getattr(bot, "get_guild", None)
    if not callable(notify) or not callable(get_guild):
        return
    grouped: dict[int, list[StructureLoss]] = {}
    for panel_id, _kind, guild_id, channel_id, _ref in rows:
        grouped.setdefault(int(guild_id), []).append(
            StructureLoss(
                key="",
                label="",
                kind=SkeletonKind.TEXT,
                object_id=int(channel_id),
                feature="",
                bindings_cleared=(),
                panels_stale=1,
                problem=f"panel:{panel_id}",
            )
        )
    for guild_id, losses in grouped.items():
        guild = get_guild(guild_id)
        if guild is None:
            continue
        try:
            await notify(guild, tuple(losses))
        except Exception:
            log.exception("panels_structure_notify_failed", guild_id=int(guild_id))


async def _discord_message_alive(bot: Any, channel_id: int, message_id: int) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        fetch = getattr(bot, "fetch_channel", None)
        if not callable(fetch):
            return True
        try:
            channel = await fetch(channel_id)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            log.warning("panel_channel_check_failed", channel_id=channel_id)
            return True
    fetch_message = getattr(channel, "fetch_message", None)
    if not callable(fetch_message):
        return True
    try:
        await fetch_message(message_id)
    except discord.NotFound:
        return False
    except discord.HTTPException:
        log.warning("panel_message_check_failed", channel_id=channel_id, message_id=message_id)
        return True
    return True
