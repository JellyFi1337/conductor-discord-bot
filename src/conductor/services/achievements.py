"""Достижения: тикеты, роли через брокер, +5 репутации, сверка."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import AchievementDef as DefRow
from conductor.db.models import AchievementGrant, AchievementProgress, AchievementSource
from conductor.db.session import session_scope
from conductor.domain.achievements import (
    KEYS,
    KIND_ACHIEVEMENT,
    REP_BONUS,
    AchievementCard,
    AchievementDef,
    AchievementLine,
    AchievementPanel,
    all_role_ids,
    default_defs,
    defs_for,
    key_for_close,
    newly_reached,
    next_need,
    parse_achievement_names,
    parse_key,
    role_diff,
    stage_for,
)
from conductor.domain.activity import as_utc
from conductor.domain.tickets import TicketClosed
from conductor.presentation import copy
from conductor.presentation.achievements import grant_view
from conductor.presentation.layout import send_card
from conductor.services.role_broker import (
    RoleBindOverlap,
    RoleClaim,
    RoleSubsystem,
    log_role_failure,
    report_skipped_roles,
)
from conductor.services.runtime import log_tick, member_on_guild

log = structlog.get_logger("conductor.achievements")

PERIOD = 1800.0
BATCH = 25


class AchievementService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._picked: dict[tuple[int, int], int] = {}
        events = getattr(bot, "events", None)
        subscribe = getattr(events, "subscribe", None)
        if callable(subscribe):
            subscribe(TicketClosed, self._on_closed)
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(PERIOD, "achievements-reconcile", self.reconcile_all)
        self._skipped_alerts: set[int] = set()

    def _on_closed(self, event: TicketClosed) -> None:
        key = key_for_close(event.category_kind, event.outcome)
        if key is None:
            return
        spawn = getattr(self._bot, "spawn", None)
        if not callable(spawn):
            return
        spawn(
            self.bump(event.guild_id, event.owner_id, key, 1, source=event.ticket_id),
            name=f"achieve-{event.ticket_id}",
        )

    def remember_user(self, guild_id: int, actor_id: int, user_id: int) -> None:
        self._picked[(int(guild_id), int(actor_id))] = int(user_id)

    def picked_user(self, guild_id: int, actor_id: int) -> int | None:
        return self._picked.get((int(guild_id), int(actor_id)))

    async def profile(self, guild_id: int, user_id: int) -> AchievementCard:
        async with session_scope(self._bot.sessions) as session:
            defs = await self._ensure_defs(session, guild_id)
            counts = await self._counts(session, guild_id, user_id)
        lines: list[AchievementLine] = []
        for key in KEYS:
            rows = defs_for(key, defs)
            count = counts.get(key, 0)
            current = stage_for(count, rows)
            have, need, nxt = next_need(count, rows)
            stage_name = next((row.name for row in rows if row.stage == current), None)
            lines.append(
                AchievementLine(
                    key=key,
                    title=copy.achievement_key(key),
                    count=count,
                    stage=current,
                    stage_name=stage_name,
                    next_name=None if nxt is None else nxt.name,
                    have=have,
                    need=need,
                )
            )
        return AchievementCard(user_id=user_id, lines=tuple(lines))

    async def show_config(self, guild_id: int) -> AchievementPanel:
        async with session_scope(self._bot.sessions) as session:
            defs = await self._ensure_defs(session, guild_id)
        return AchievementPanel(defs=defs)

    async def set_role(self, guild_id: int, key: str, stage: int, role_id: int) -> AchievementPanel:
        token = parse_key(key)
        if token is None:
            raise ValueError("bad key")
        async with session_scope(self._bot.sessions) as session:
            defs = await self._ensure_defs(session, guild_id)
            if any(row.role_id == role_id and not (row.key == token and row.stage == stage) for row in defs):
                raise RoleBindOverlap(role_id, RoleSubsystem.ACHIEVEMENTS)
            rows = await self._rows(session, guild_id)
            row = next((item for item in rows if item.key == token and item.stage == stage), None)
            if row is None:
                raise ValueError("bad stage")
            row.role_id = int(role_id)
            await session.flush()
            defs = await self._read_defs(session, guild_id)
        self._skipped_alerts.discard(int(guild_id))
        self._bind(guild_id, defs, strict=True)
        return AchievementPanel(defs=defs)

    async def set_names(self, guild_id: int, raw: str) -> AchievementPanel | None:
        parsed = parse_achievement_names(raw)
        if parsed is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            await self._ensure_defs(session, guild_id)
            rows = await self._rows(session, guild_id)
            for row in rows:
                labels = parsed.get(row.key, ())
                if row.stage - 1 < len(labels):
                    row.name = labels[row.stage - 1]
            await session.flush()
            defs = await self._read_defs(session, guild_id)
        return AchievementPanel(defs=defs)

    async def bind_roles(self, guild_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            defs = await self._ensure_defs(session, guild_id)
        self._skipped_alerts.discard(int(guild_id))
        self._bind(guild_id, defs)

    async def bump(
        self,
        guild_id: int,
        user_id: int,
        key: str,
        delta: int,
        *,
        source: int | None = None,
        now: datetime | None = None,
    ) -> AchievementCard | None:
        token = parse_key(key)
        if token is None:
            return None
        moment = as_utc(now or datetime.now(UTC))
        if source is not None and not await self._claim_source(guild_id, user_id, token, int(source)):
            return None
        reached, _before, _after = await self._apply_count(
            guild_id,
            user_id,
            token,
            None,
            delta=int(delta),
            now=moment,
        )
        await self._reward(guild_id, user_id, reached, now=moment)
        await self._sync_roles(guild_id, user_id)
        return await self.profile(guild_id, user_id)

    async def set_count(
        self,
        guild_id: int,
        user_id: int,
        key: str,
        count: int,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> AchievementCard | None:
        token = parse_key(key)
        if token is None or int(count) < 0:
            return None
        _ = reason
        moment = as_utc(now or datetime.now(UTC))
        reached, before, after = await self._apply_count(
            guild_id,
            user_id,
            token,
            int(count),
            delta=0,
            now=moment,
        )
        if after > before:
            await self._reward(guild_id, user_id, reached, now=moment)
        await self._sync_roles(guild_id, user_id)
        return await self.profile(guild_id, user_id)

    async def reconcile_all(self) -> int:
        done = 0
        for guild in getattr(self._bot, "guilds", ()):
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            try:
                done += await self.reconcile_guild(int(guild_id))
            except Exception:
                log.exception("achievements_reconcile_failed", guild_id=guild_id)
        log_tick(log, "achievements_reconcile_tick", changed=0, checked=done)
        return done

    async def reconcile_guild(self, guild_id: int) -> int:
        async with session_scope(self._bot.sessions) as session:
            await self._ensure_defs(session, guild_id)
            user_ids = list(
                (
                    await session.scalars(
                        select(AchievementProgress.user_id).where(AchievementProgress.guild_id == guild_id).distinct()
                    )
                ).all()
            )
        done = 0
        for index in range(0, len(user_ids), BATCH):
            for user_id in user_ids[index : index + BATCH]:
                await self._sync_roles(guild_id, int(user_id))
                done += 1
        return done

    async def _apply_count(
        self,
        guild_id: int,
        user_id: int,
        key: str,
        count: int | None,
        *,
        delta: int,
        now: datetime,
    ) -> tuple[tuple[AchievementDef, ...], int, int]:
        async with session_scope(self._bot.sessions) as session:
            defs = defs_for(key, await self._ensure_defs(session, guild_id))
            row = await session.scalar(
                select(AchievementProgress).where(
                    AchievementProgress.guild_id == guild_id,
                    AchievementProgress.user_id == user_id,
                    AchievementProgress.key == key,
                )
            )
            before = 0 if row is None else int(row.count)
            after = max(0, int(count) if count is not None else before + int(delta))
            if row is None:
                session.add(
                    AchievementProgress(
                        guild_id=guild_id,
                        user_id=user_id,
                        key=key,
                        count=after,
                        updated_at=now,
                    )
                )
            else:
                row.count = after
                row.updated_at = now
            if after < before:
                rows = (
                    await session.scalars(
                        select(AchievementGrant).where(
                            AchievementGrant.guild_id == guild_id,
                            AchievementGrant.user_id == user_id,
                            AchievementGrant.key == key,
                            AchievementGrant.stage > stage_for(after, defs),
                            AchievementGrant.revoked_at.is_(None),
                        )
                    )
                ).all()
                for grant in rows:
                    grant.revoked_at = now
            reached = newly_reached(before, after, defs)
            await session.flush()
        return reached, before, after

    async def _reward(
        self,
        guild_id: int,
        user_id: int,
        reached: Sequence[AchievementDef],
        *,
        now: datetime,
    ) -> None:
        if not reached:
            return
        adjust = getattr(getattr(self._bot, "reputation", None), "adjust", None)
        for row in reached:
            if not await self._grant(guild_id, user_id, row, now=now):
                continue
            if callable(adjust):
                await adjust(guild_id, user_id, REP_BONUS, kind=KIND_ACHIEVEMENT, now=now)
            self._announce(guild_id, user_id, row.name)

    async def _grant(self, guild_id: int, user_id: int, row: AchievementDef, *, now: datetime) -> bool:
        try:
            async with session_scope(self._bot.sessions) as session:
                existing = await session.scalar(
                    select(AchievementGrant).where(
                        AchievementGrant.guild_id == guild_id,
                        AchievementGrant.user_id == user_id,
                        AchievementGrant.key == row.key,
                        AchievementGrant.stage == row.stage,
                    )
                )
                if existing is not None:
                    return False
                session.add(
                    AchievementGrant(
                        guild_id=guild_id,
                        user_id=user_id,
                        key=row.key,
                        stage=row.stage,
                        granted_at=now,
                    )
                )
                await session.flush()
        except IntegrityError:
            return False
        return True

    async def _claim_source(self, guild_id: int, user_id: int, key: str, ticket_id: int) -> bool:
        try:
            async with session_scope(self._bot.sessions) as session:
                session.add(AchievementSource(guild_id=guild_id, ticket_id=ticket_id, user_id=user_id, key=key))
                await session.flush()
        except IntegrityError:
            return False
        return True

    async def _sync_roles(self, guild_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            defs = await self._ensure_defs(session, guild_id)
            counts = await self._counts(session, guild_id, user_id)
        have = self._member_role_ids(guild_id, user_id)
        add: list[int] = []
        remove: list[int] = []
        for key in KEYS:
            rows = defs_for(key, defs)
            wanted_add, wanted_remove = role_diff(have, rows, stage_for(counts.get(key, 0), rows))
            add.extend(wanted_add)
            remove.extend(wanted_remove)
        self._bind(guild_id, defs)
        unique_add = tuple(dict.fromkeys(add))
        unique_remove = tuple(dict.fromkeys(role_id for role_id in remove if role_id not in unique_add))
        if not unique_add and not unique_remove:
            return
        if not member_on_guild(self._bot, guild_id, user_id):
            return
        await self._apply(guild_id, user_id, add=unique_add, remove=unique_remove)

    async def _apply(
        self,
        guild_id: int,
        user_id: int,
        *,
        add: tuple[int, ...],
        remove: tuple[int, ...],
    ) -> None:
        broker = getattr(self._bot, "roles", None)
        apply = getattr(broker, "apply", None)
        if not callable(apply):
            return
        result = await apply(
            RoleClaim(
                guild_id=guild_id,
                user_id=user_id,
                subsystem=RoleSubsystem.ACHIEVEMENTS,
                add=add,
                remove=remove,
                reason=copy.REASON_ACHIEVEMENT,
            )
        )
        skipped = tuple(getattr(result, "skipped", ()) or ())
        if skipped:
            report_skipped_roles(
                self._bot,
                guild_id,
                skipped,
                seen=self._skipped_alerts,
                title=copy.ACHIEVEMENT_ROLE_GONE,
                hint=copy.ACHIEVEMENT_STALE_ACTION,
            )
        elif not getattr(result, "ok", True):
            log_role_failure(
                log,
                "achievement_role_failed",
                guild_id=guild_id,
                user_id=user_id,
                result=result,
            )

    def _announce(self, guild_id: int, user_id: int, name: str) -> None:
        view = grant_view(name, user_id)
        self._log_admin(guild_id, view)
        spawn = getattr(self._bot, "spawn", None)
        if callable(spawn):
            spawn(self._dm(guild_id, user_id, name), name=f"achieve-dm-{user_id}")

    async def _dm(self, guild_id: int, user_id: int, name: str) -> None:
        getter = getattr(self._bot, "get_guild", None)
        guild = getter(guild_id) if callable(getter) else None
        member = None if guild is None else guild.get_member(user_id)
        send = getattr(member, "send", None)
        if not callable(send):
            return
        try:
            await self._discord(send_card(member, grant_view(name)))
        except (discord.Forbidden, discord.HTTPException):
            return

    def _log_admin(self, guild_id: int, view: Any) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if not callable(submit) or channel_id is None:
            return
        submit(channel_id, view=view)

    async def _ensure_defs(self, session: AsyncSession, guild_id: int) -> tuple[AchievementDef, ...]:
        specs = default_defs(copy.ACHIEVEMENT_DEFAULTS)
        rows = await self._rows(session, guild_id)
        by_pair = {(str(row.key), int(row.stage)): row for row in rows}
        dirty = False
        for spec in specs:
            row = by_pair.get((spec.key, spec.stage))
            if row is None:
                session.add(
                    DefRow(
                        guild_id=guild_id,
                        key=spec.key,
                        stage=spec.stage,
                        threshold=spec.threshold,
                        name=spec.name,
                    )
                )
                dirty = True
                continue
            if int(row.threshold) != spec.threshold:
                row.threshold = spec.threshold
                dirty = True
        if dirty:
            await session.flush()
        return await self._read_defs(session, guild_id)

    async def _rows(self, session: AsyncSession, guild_id: int) -> list[DefRow]:
        return list(
            (
                await session.scalars(
                    select(DefRow).where(DefRow.guild_id == guild_id).order_by(DefRow.key, DefRow.stage)
                )
            ).all()
        )

    async def _read_defs(self, session: AsyncSession, guild_id: int) -> tuple[AchievementDef, ...]:
        return tuple(
            AchievementDef(
                key=str(row.key),
                stage=int(row.stage),
                threshold=int(row.threshold),
                name=str(row.name),
                role_id=None if row.role_id is None else int(row.role_id),
            )
            for row in await self._rows(session, guild_id)
        )

    async def _counts(self, session: AsyncSession, guild_id: int, user_id: int) -> dict[str, int]:
        rows = (
            await session.scalars(
                select(AchievementProgress).where(
                    AchievementProgress.guild_id == guild_id,
                    AchievementProgress.user_id == user_id,
                )
            )
        ).all()
        return {str(row.key): int(row.count) for row in rows}

    def _bind(self, guild_id: int, defs: Sequence[AchievementDef], *, strict: bool = False) -> None:
        broker = getattr(self._bot, "roles", None)
        bind = getattr(broker, "bind", None)
        if not callable(bind):
            return
        bind(guild_id, RoleSubsystem.ACHIEVEMENTS, all_role_ids(defs), strict=strict)

    def _member_role_ids(self, guild_id: int, user_id: int) -> tuple[int, ...]:
        getter = getattr(self._bot, "get_guild", None)
        guild = getter(guild_id) if callable(getter) else None
        if guild is None:
            return ()
        member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
        if member is None:
            return ()
        return tuple(int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None))

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
