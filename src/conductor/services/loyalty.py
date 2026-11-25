from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import LoyaltyBatch, MemberRecord
from conductor.db.session import session_scope
from conductor.domain.loyalty import (
    TIER_LABELS,
    TIER_PRIORITY,
    LoyaltySkip,
    LoyaltyTier,
    classify_member,
    registry_delta,
)
from conductor.presentation import copy
from conductor.presentation.events import loyalty_revert_card
from conductor.services.guild_config import get_or_create_config, role_id_for
from conductor.services.old_roles import (
    authorized_role_id,
    load_registry,
    old_role_id_set,
    record_role_event,
    replace_registry,
    seed_registry_from_guild,
)
from conductor.services.role_broker import RoleBrokerError, RoleClaim, RoleSubsystem, log_role_failure

log = structlog.get_logger("conductor.loyalty")


@dataclass(slots=True)
class LoyaltyPlan:
    as_of: datetime
    guild_created_at: datetime
    counts: dict[LoyaltyTier, int] = field(default_factory=dict)
    assignments: dict[int, LoyaltyTier] = field(default_factory=dict)
    skipped_today: int = 0
    skipped_bots: int = 0
    skipped_silent: int = 0
    considered: int = 0

    @property
    def eligible(self) -> int:
        return len(self.assignments)

    @property
    def accounted(self) -> int:
        return self.skipped_bots + self.skipped_silent + self.skipped_today + self.eligible

    def to_json(self) -> str:
        return json.dumps(
            {
                "as_of": self.as_of.isoformat(),
                "guild_created_at": self.guild_created_at.isoformat(),
                "counts": {tier.value: self.counts.get(tier, 0) for tier in TIER_PRIORITY},
                "assignments": {str(uid): tier.value for uid, tier in self.assignments.items()},
                "skipped_today": self.skipped_today,
                "skipped_bots": self.skipped_bots,
                "skipped_silent": self.skipped_silent,
                "considered": self.considered,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> LoyaltyPlan:
        data = json.loads(raw)
        assignments = {int(uid): LoyaltyTier(tier) for uid, tier in data["assignments"].items()}
        counts = {LoyaltyTier(k): int(v) for k, v in data["counts"].items()}
        return cls(
            as_of=datetime.fromisoformat(data["as_of"]),
            guild_created_at=datetime.fromisoformat(data["guild_created_at"]),
            counts=counts,
            assignments=assignments,
            skipped_today=int(data.get("skipped_today", 0)),
            skipped_bots=int(data.get("skipped_bots", 0)),
            skipped_silent=int(data.get("skipped_silent", 0)),
            considered=int(data.get("considered", 0)),
        )


async def build_plan(
    session: AsyncSession,
    *,
    guild_id: int,
    guild_created_at: datetime,
    now: datetime | None = None,
    only_current_members: bool = True,
) -> LoyaltyPlan:
    now = now or datetime.now(UTC)
    plan = LoyaltyPlan(as_of=now, guild_created_at=guild_created_at)
    plan.counts = dict.fromkeys(TIER_PRIORITY, 0)
    stmt = select(MemberRecord).where(MemberRecord.guild_id == guild_id)
    if only_current_members:
        stmt = stmt.where(MemberRecord.currently_member.is_(True))
    members = (await session.scalars(stmt)).all()
    plan.considered = len(members)
    for member in members:
        verdict = classify_member(
            is_bot=member.is_bot,
            message_count=member.message_count or 0,
            joined_at=member.first_joined_at,
            guild_created_at=guild_created_at,
            now=now,
        )
        if verdict.skip is LoyaltySkip.BOT:
            plan.skipped_bots += 1
            continue
        if verdict.skip is LoyaltySkip.TODAY:
            plan.skipped_today += 1
            continue
        if verdict.skip is LoyaltySkip.SILENT:
            plan.skipped_silent += 1
            continue
        assert verdict.tier is not None
        plan.assignments[member.user_id] = verdict.tier
        plan.counts[verdict.tier] += 1
    return plan


def grouped_assignments(plan: LoyaltyPlan) -> dict[LoyaltyTier, list[int]]:
    grouped: dict[LoyaltyTier, list[int]] = defaultdict(list)
    for user_id, tier in plan.assignments.items():
        grouped[tier].append(user_id)
    return grouped


async def save_confirmed_batch(
    session: AsyncSession,
    *,
    guild_id: int,
    created_by: int,
    plan: LoyaltyPlan,
) -> LoyaltyBatch:
    existing = (
        await session.scalars(
            select(LoyaltyBatch).where(
                LoyaltyBatch.guild_id == guild_id,
                LoyaltyBatch.status == "confirmed",
            )
        )
    ).all()
    for batch in existing:
        batch.status = "cancelled"
    batch = LoyaltyBatch(
        guild_id=guild_id,
        created_by=created_by,
        status="confirmed",
        payload_json=plan.to_json(),
        created_at=datetime.now(UTC),
    )
    session.add(batch)
    await session.flush()
    return batch


async def get_confirmed_batch(session: AsyncSession, guild_id: int) -> LoyaltyBatch | None:
    return await session.scalar(
        select(LoyaltyBatch)
        .where(LoyaltyBatch.guild_id == guild_id, LoyaltyBatch.status == "confirmed")
        .order_by(LoyaltyBatch.id.desc())
    )


@dataclass(frozen=True, slots=True)
class GrantDenial:
    kind: str
    missing: tuple[str, ...] = ()
    too_high: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GrantPrep:
    denial: GrantDenial | None
    plan: LoyaltyPlan | None = None
    batch_id: int | None = None
    role_ids: dict[LoyaltyTier, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApplyStats:
    applied: int
    skipped: int
    errors: int


class LoyaltyService:
    """Сверка реестра Old и выдача по плану. Роли — только через брокер."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._enforcing: set[tuple[int, int]] = set()
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _admin_channel(self, guild_id: int) -> int | None:
        config = getattr(self._bot, "config_by_guild", {}).get(guild_id)
        return None if config is None else config.admin_log_channel_id

    async def protect_guild(self, guild: Any) -> None:
        async with self._lock(guild.id):
            members = [(member.id, {role.id for role in member.roles}, member.bot) for member in guild.members]
            async with session_scope(self._bot.sessions) as session:
                config = await get_or_create_config(session, guild.id)
                seeded = await seed_registry_from_guild(
                    session,
                    guild_id=guild.id,
                    members=members,
                    config=config,
                )
            if seeded:
                refresh = getattr(self._bot, "refresh_config", None)
                if callable(refresh):
                    await refresh(guild.id)
                log.info("loyalty_registry_seeded", guild_id=guild.id, rows=seeded)
            async with session_scope(self._bot.sessions) as session:
                registry = await load_registry(session, guild.id)
            for member in guild.members:
                if member.bot:
                    continue
                await self.enforce_member(member, registry=registry)

    async def enforce_member(
        self,
        member: Any,
        *,
        actor_id: int | None = None,
        granted: tuple[int, ...] = (),
        removed: tuple[int, ...] = (),
        registry: dict[int, LoyaltyTier] | None = None,
    ) -> None:
        key = (member.guild.id, member.id)
        if key in self._enforcing:
            return
        config = getattr(self._bot, "config_by_guild", {}).get(member.guild.id)
        old_ids = old_role_id_set(config)
        if config is None or not old_ids:
            return
        if registry is None:
            async with session_scope(self._bot.sessions) as session:
                registry = await load_registry(session, member.guild.id)
        authorized = authorized_role_id(config, registry, member.id)
        delta = registry_delta(
            {role.id for role in member.roles},
            authorized_role_id=authorized,
            old_role_ids=old_ids,
        )
        if delta.empty:
            return
        add = [role for role_id in delta.add_ids if (role := member.guild.get_role(role_id)) is not None]
        remove = [role for role_id in delta.remove_ids if (role := member.guild.get_role(role_id)) is not None]
        if not add and not remove:
            return
        self._enforcing.add(key)
        reverted = False
        try:
            await self._restore(member, add=add, remove=remove)
            reverted = True
        except (discord.HTTPException, RoleBrokerError):
            log.exception("old_role_revert_failed", guild_id=member.guild.id, user_id=member.id)
        finally:
            self._enforcing.discard(key)
        actions = [("granted", role_id) for role_id in granted] + [("removed", role_id) for role_id in removed]
        if not actions:
            actions = [("granted", role.id) for role in add] + [("removed", role.id) for role in remove]
        async with session_scope(self._bot.sessions) as session:
            for action, role_id in actions:
                await record_role_event(
                    session,
                    guild_id=member.guild.id,
                    user_id=member.id,
                    actor_id=actor_id,
                    role_id=role_id,
                    action=action,
                    reverted=reverted,
                )
        self._log_revert(
            member,
            actor_id=actor_id,
            granted=granted,
            removed=removed,
            add=add,
            remove=remove,
            reverted=reverted,
        )

    async def _restore(self, member: Any, *, add: list[Any], remove: list[Any]) -> None:
        result = await self._bot.roles.apply(
            RoleClaim(
                guild_id=member.guild.id,
                user_id=member.id,
                subsystem=RoleSubsystem.LOYALTY,
                add=tuple(role.id for role in add),
                remove=tuple(role.id for role in remove),
                reason=copy.REASON_LOYALTY_REVERT,
                urgent=True,
            )
        )
        if result.http is not None:
            raise result.http
        if not result.ok:
            raise RoleBrokerError(result.error or copy.LOYALTY_REVERT_FAIL)

    def _log_revert(
        self,
        member: Any,
        *,
        actor_id: int | None,
        granted: tuple[int, ...],
        removed: tuple[int, ...],
        add: list[Any],
        remove: list[Any],
        reverted: bool,
    ) -> None:
        actor = member.guild.get_member(actor_id) if actor_id else None
        description = copy.LOYALTY_REVERT_OK if reverted else copy.LOYALTY_REVERT_STUCK
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        if not callable(submit):
            return
        submit(
            self._admin_channel(member.guild.id),
            view=loyalty_revert_card(
                member,
                actor=actor,
                actor_id=actor_id,
                description=description,
                granted=granted,
                removed=removed,
                bot_removed=" ".join(role.mention for role in remove) or None,
                bot_restored=" ".join(role.mention for role in add) or None,
            ),
        )

    async def has_confirmed_plan(self, guild_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            return await get_confirmed_batch(session, guild_id) is not None

    async def decide_grant(self, guild_id: int) -> GrantDenial | None:
        if await self.has_confirmed_plan(guild_id):
            return None
        return GrantDenial("no_plan")

    async def prepare_grant(self, guild: Any, me: Any) -> GrantPrep:
        denial: GrantDenial | None = None
        plan: LoyaltyPlan | None = None
        role_ids: dict[LoyaltyTier, int] = {}
        batch_id: int | None = None
        async with session_scope(self._bot.sessions) as session:
            batch = await get_confirmed_batch(session, guild.id)
            config = await get_or_create_config(session, guild.id)
            if batch is None:
                denial = GrantDenial("no_plan")
            else:
                plan = LoyaltyPlan.from_json(batch.payload_json)
                missing: list[str] = []
                too_high: list[str] = []
                for tier in TIER_PRIORITY:
                    role_id = role_id_for(config, tier)
                    role = guild.get_role(role_id) if role_id else None
                    if role is None:
                        missing.append(TIER_LABELS[tier])
                    else:
                        role_ids[tier] = int(role.id)
                        if role >= me.top_role:
                            too_high.append(role.mention)
                if missing:
                    denial = GrantDenial("unbound", missing=tuple(missing))
                elif too_high:
                    denial = GrantDenial("too_high", too_high=tuple(too_high))
                else:
                    await replace_registry(session, guild_id=guild.id, assignments=plan.assignments)
                    batch_id = batch.id
        return GrantPrep(denial=denial, plan=plan, batch_id=batch_id, role_ids=role_ids)

    async def apply_plan(self, guild: Any, plan: LoyaltyPlan, role_ids: dict[LoyaltyTier, int]) -> ApplyStats:
        role_map = {tier: guild.get_role(role_id) for tier, role_id in role_ids.items()}
        if any(role is None for role in role_map.values()):
            return ApplyStats(0, 0, len(plan.assignments))
        applied = skipped = errors = 0
        loyalty_roles = list(role_map.values())
        for user_id, tier in plan.assignments.items():
            member = guild.get_member(user_id)
            target = role_map[tier]
            if member is None or target is None:
                skipped += 1
                continue
            try:
                extra = [role for role in loyalty_roles if role in member.roles and role != target]
                result = await self._bot.roles.apply(
                    RoleClaim(
                        guild_id=guild.id,
                        user_id=user_id,
                        subsystem=RoleSubsystem.LOYALTY,
                        add=() if target in member.roles else (target.id,),
                        remove=tuple(role.id for role in extra),
                        reason=copy.REASON_LOYALTY_GRANT,
                    )
                )
                if result.missing_member:
                    skipped += 1
                elif not result.ok:
                    errors += 1
                    log_role_failure(
                        log,
                        "loyalty_grant_failed",
                        guild_id=guild.id,
                        user_id=user_id,
                        result=result,
                    )
                else:
                    applied += 1
            except (discord.HTTPException, RoleBrokerError) as exc:
                errors += 1
                log.warning("loyalty_grant_failed", guild_id=guild.id, user_id=user_id, error=str(exc))
        return ApplyStats(applied, skipped, errors)

    async def mark_applied(self, batch_id: int, actor_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            stored = await session.get(LoyaltyBatch, batch_id)
            if stored is not None:
                stored.status = "applied"
                stored.applied_at = datetime.now(UTC)
                stored.applied_by = actor_id
