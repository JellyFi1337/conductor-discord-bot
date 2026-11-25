"""Канал действий следящих: строка в БД, затем карточка через log_pump."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from conductor.db.models import StaffAction
from conductor.db.session import session_scope
from conductor.domain.access import Rank, resolve_rank
from conductor.domain.staff_feed import (
    ACTION_EXTERNAL,
    sanitize_staff_action,
    sanitize_staff_details,
    sanitize_staff_ref,
    should_ping_chief,
)
from conductor.presentation import copy
from conductor.presentation.mentions import role_mentions
from conductor.presentation.staff_feed import staff_feed_card
from conductor.services.access import cached_snapshot


async def emit_staff(
    bot: Any,
    guild_id: int,
    *,
    actor_id: int,
    actor_rank: Rank | int,
    action: str,
    target: int | None = None,
    ref: int | str | None = None,
    details: str | None = None,
    ping_chief: bool = False,
    channel: bool = False,
    now: datetime | None = None,
) -> int | None:
    feed = getattr(bot, "staff_feed", None)
    post = getattr(feed, "post", None)
    if not callable(post):
        return None
    return await post(
        guild_id,
        actor_id=actor_id,
        actor_rank=actor_rank,
        action=action,
        target=target,
        ref=ref,
        details=details,
        ping_chief=ping_chief,
        channel=channel,
        now=now,
    )


class StaffFeed:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def post(
        self,
        guild_id: int,
        *,
        actor_id: int,
        actor_rank: Rank | int,
        action: str,
        target: int | None = None,
        ref: int | str | None = None,
        details: str | None = None,
        ping_chief: bool = False,
        channel: bool = False,
        now: datetime | None = None,
    ) -> int | None:
        code = sanitize_staff_action(action)
        note = sanitize_staff_details(details)
        token = sanitize_staff_ref(ref)
        rank = int(actor_rank)
        moment = now or datetime.now(UTC)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        ping = should_ping_chief(code, ping_chief=ping_chief)
        async with session_scope(self._bot.sessions) as session:
            if code == ACTION_EXTERNAL and token is not None:
                existing = await session.scalar(
                    select(StaffAction.id).where(
                        StaffAction.guild_id == int(guild_id),
                        StaffAction.action == ACTION_EXTERNAL,
                        StaffAction.ref == token,
                    )
                )
                if existing is not None:
                    return None
            row = StaffAction(
                guild_id=int(guild_id),
                actor_id=int(actor_id),
                actor_rank=rank,
                action=code,
                target_id=None if target is None else int(target),
                ref=token,
                details=note,
                created_at=moment,
            )
            session.add(row)
            await session.flush()
            action_id = int(row.id)
        await self._card(
            guild_id,
            actor_id=int(actor_id),
            rank=rank,
            action=code,
            target=None if target is None else int(target),
            details=note,
            ping_chief=ping,
            channel=channel,
        )
        return action_id

    async def _card(
        self,
        guild_id: int,
        *,
        actor_id: int,
        rank: int,
        action: str,
        target: int | None,
        details: str | None,
        ping_chief: bool,
        channel: bool,
    ) -> None:
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "staff_feed_channel_id", None)
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit) or channel_id is None:
            return
        chief_id = await self._chief_role(guild_id)
        mentions = role_mentions(chief_id if ping_chief else None)
        where = None
        if target is not None:
            where = f"<#{target}>" if channel else f"<@{target}>"
        submit(
            int(channel_id),
            view=staff_feed_card(
                actor_id=actor_id,
                rank=_rank_label(rank),
                action=action,
                where=where,
                details=details,
                chief_role_id=chief_id,
                ping_chief=ping_chief,
            ),
            allowed_mentions=mentions,
            silent=not ping_chief,
        )

    async def _chief_role(self, guild_id: int) -> int | None:
        snapshot = await cached_snapshot(self._bot, guild_id)
        role_ids = snapshot.rank_roles.get(Rank.CHIEF, ())
        if not role_ids:
            return None
        return int(role_ids[0])


async def actor_rank_of(bot: Any, guild: object, actor_id: int) -> Rank:
    if getattr(bot, "caches", None) is None:
        return Rank.NONE
    snapshot = await cached_snapshot(bot, int(getattr(guild, "id", 0) or 0))
    member = None
    lookup = getattr(guild, "get_member", None)
    if callable(lookup):
        member = lookup(int(actor_id))
    roles = tuple(role.id for role in getattr(member, "roles", ()) or ())
    return resolve_rank(roles, snapshot.rank_roles)


def _rank_label(actor_rank: int) -> str:
    try:
        rank = Rank(int(actor_rank))
    except ValueError:
        rank = Rank.NONE
    return copy.rank_label(rank.key)
