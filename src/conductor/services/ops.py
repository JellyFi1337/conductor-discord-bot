"""Owner-операции: wipe гильдии, массовые роли через брокер, импорт Old в реестр."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import delete, or_, select

from conductor.db.models import Base, GuildConfig, MemberRecord
from conductor.db.session import session_scope
from conductor.domain.access import staff_role_ids
from conductor.domain.activity import as_utc
from conductor.domain.ladder import Step, tenure_days
from conductor.domain.loyalty import LoyaltyTier
from conductor.domain.ops import (
    MassMode,
    MatchMode,
    MemberRolePlan,
    MemberSkip,
    MemberState,
    OldImportPlan,
    ProtectKind,
    RoleAudience,
    WipeSection,
    WipeStep,
    plan_grant_member,
    plan_old_import,
    plan_strip_member,
    protect_ids,
    registry_after_mass,
    role_skip,
    steps_for,
    wipe_section,
)
from conductor.domain.reputation import START
from conductor.presentation import copy
from conductor.services.access import cached_snapshot
from conductor.services.guild_config import get_or_create_config
from conductor.services.old_roles import (
    drop_registry_users,
    load_registry,
    old_role_id_set,
    old_roles_from_config,
    upsert_registry_members,
)
from conductor.services.role_broker import RoleClaim, RoleSubsystem

log = structlog.get_logger("conductor.ops")

ProgressTick = Callable[[int, int], Any]

LIFT_TENURE_DAYS = 400

__all__ = [
    "LIFT_TENURE_DAYS",
    "LiftReport",
    "MassPreview",
    "MassRoleReport",
    "OldImportReport",
    "OwnerOps",
    "WipeReport",
    "count_grant",
    "count_strip",
    "filter_text",
    "match_mode",
    "protect_kind",
    "slash_audience",
    "unique_roles",
]


@dataclass(frozen=True, slots=True)
class LiftReport:
    user_id: int
    tenure_days: int
    step: str
    score: int
    verified: bool


@dataclass(frozen=True, slots=True)
class WipeReport:
    section: WipeSection
    tables: dict[str, int]

    @property
    def rows(self) -> int:
        return sum(self.tables.values())


@dataclass(frozen=True, slots=True)
class MassRoleReport:
    mode: MassMode
    members: int
    changed: int
    failed: int
    skipped_bot: int
    skipped_empty: int
    skipped_require: int
    skipped_except: int
    skipped_protect: int
    skipped_timeout: int
    skipped_pending: int
    skipped_boost: int
    skipped_roles: tuple[str, ...]
    registry: int

    @property
    def skipped(self) -> int:
        return (
            self.skipped_bot
            + self.skipped_empty
            + self.skipped_require
            + self.skipped_except
            + self.skipped_protect
            + self.skipped_timeout
            + self.skipped_pending
            + self.skipped_boost
        )


@dataclass(frozen=True, slots=True)
class MassPreview:
    selected: list[discord.Role]
    kind: ProtectKind
    audience: RoleAudience
    members: int
    labels: str
    filters: str


@dataclass(frozen=True, slots=True)
class OldImportReport:
    plan: OldImportPlan
    added: int
    updated: int
    enforced: int
    failed: int
    unbound: bool


def _model_map() -> dict[str, type[Any]]:
    found: dict[str, type[Any]] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        name = getattr(cls, "__tablename__", None)
        if name:
            found[str(name)] = cls
    return found


class OwnerOps:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._models = _model_map()

    async def wipe(self, guild_id: int, section: WipeSection | str) -> WipeReport:
        resolved = section if isinstance(section, WipeSection) else wipe_section(str(section))
        if resolved is None:
            raise ValueError(section)
        tables: dict[str, int] = {}
        async with session_scope(self._bot.sessions) as session:
            if resolved is WipeSection.LOYALTY:
                config = await session.get(GuildConfig, guild_id)
                if config is not None and config.loyalty_registry_seeded_at is None:
                    config.loyalty_registry_seeded_at = datetime.now(UTC)
            for step in steps_for(resolved):
                tables[step.table] = await _wipe_step(session, self._models, guild_id, step)
        await self._after_wipe(guild_id, resolved)
        log.info("owner_wipe", guild_id=guild_id, section=resolved.value, rows=sum(tables.values()))
        return WipeReport(resolved, tables)

    async def lift_newcomer(self, guild: discord.Guild, member: discord.Member, *, actor_id: int) -> LiftReport:
        now = datetime.now(UTC)
        joined = now - timedelta(days=LIFT_TENURE_DAYS)
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(MemberRecord).where(MemberRecord.guild_id == guild.id, MemberRecord.user_id == member.id)
            )
            if row is None:
                session.add(
                    MemberRecord(
                        guild_id=guild.id,
                        user_id=member.id,
                        username=str(member)[:40],
                        display_name=str(getattr(member, "display_name", "") or "")[:80],
                        is_bot=bool(getattr(member, "bot", False)),
                        currently_member=True,
                        first_joined_at=joined,
                        last_joined_at=joined,
                    )
                )
            elif row.first_joined_at is None or as_utc(row.first_joined_at) > joined:
                row.first_joined_at = joined
        caches = getattr(self._bot, "caches", None)
        invalidate = getattr(caches, "invalidate_joined", None)
        if callable(invalidate):
            invalidate(guild.id, member.id)
        ladder = getattr(self._bot, "ladder", None)
        grant = getattr(ladder, "grant_held", None)
        step = Step.FRIEND
        if callable(grant):
            step = await grant(guild.id, member.id, Step.FRIEND, now=now)
        reputation = getattr(self._bot, "reputation", None)
        dossier_of = getattr(reputation, "dossier", None)
        adjust = getattr(reputation, "adjust", None)
        score = START
        if callable(dossier_of):
            dossier = await dossier_of(guild.id, member.id, now=now)
            score = int(getattr(dossier, "score", START) or START)
            if score < START and callable(adjust):
                score = int(await adjust(guild.id, member.id, START - score, kind="owner", now=now))
        verified = False
        verify = getattr(getattr(self._bot, "verification", None), "grant", None)
        if callable(verify):
            await verify(guild, member)
            verified = True
        log.info("owner_lift", guild_id=guild.id, user_id=member.id, actor_id=actor_id)
        return LiftReport(
            user_id=int(member.id),
            tenure_days=tenure_days(joined, now),
            step=step.key if hasattr(step, "key") else str(step),
            score=score,
            verified=bool(verified),
        )

    async def strip(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        roles: Sequence[discord.Role] = (),
        all_roles: bool = False,
        audience: RoleAudience | None = None,
        protect: ProtectKind = ProtectKind.NONE,
        on_progress: ProgressTick | None = None,
    ) -> MassRoleReport:
        if all_roles == bool(roles):
            raise ValueError("strip_mode")
        await _chunk(guild)
        gate = await self.resolve_audience(guild, audience, protect)
        assignable, skipped = _assignable(guild, roles if roles else _member_roles(guild))
        mode = MassMode.ALL if all_roles else MassMode.SELECTED
        targets = frozenset(role.id for role in assignable)
        if not targets:
            return _empty_roles_report(mode, skipped)
        plans = [
            plan_strip_member(
                user_id=member.id,
                is_bot=bool(member.bot),
                have=_held(member),
                targets=targets,
                audience=gate,
                state=_state(member),
            )
            for member in guild.members
        ]
        return await self._apply_plans(
            guild,
            plans,
            mode=mode,
            actor_id=actor_id,
            add_ids=frozenset(),
            remove_ids=targets,
            strip_all=all_roles,
            skipped_roles=skipped,
            reason=copy.REASON_OWNER_STRIP,
            on_progress=on_progress,
        )

    async def grant(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        roles: Sequence[discord.Role],
        audience: RoleAudience | None = None,
        protect: ProtectKind = ProtectKind.NONE,
        on_progress: ProgressTick | None = None,
    ) -> MassRoleReport:
        await _chunk(guild)
        gate = await self.resolve_audience(guild, audience, protect)
        assignable, skipped = _assignable(guild, roles)
        if not assignable:
            return _empty_roles_report(MassMode.SELECTED, skipped)
        targets = tuple(role.id for role in assignable)
        plans = [
            plan_grant_member(
                user_id=member.id,
                is_bot=bool(member.bot),
                have=_held(member),
                targets=targets,
                audience=gate,
                state=_state(member),
            )
            for member in guild.members
        ]
        return await self._apply_plans(
            guild,
            plans,
            mode=MassMode.SELECTED,
            actor_id=actor_id,
            add_ids=frozenset(targets),
            remove_ids=frozenset(),
            strip_all=False,
            skipped_roles=skipped,
            reason=copy.REASON_OWNER_GRANT,
            on_progress=on_progress,
        )

    async def import_old(
        self,
        guild: discord.Guild,
        *,
        role: discord.Role,
        tier: LoyaltyTier,
        on_progress: ProgressTick | None = None,
    ) -> OldImportReport:
        await _chunk(guild)
        holders = [(member.id, bool(member.bot)) for member in role.members]
        async with session_scope(self._bot.sessions) as session:
            config = await get_or_create_config(session, guild.id)
            existing = await load_registry(session, guild.id)
            plan = plan_old_import(holders, existing, tier)
            added, updated = await upsert_registry_members(
                session,
                guild_id=guild.id,
                assignments=plan.assignments,
                config=config,
            )
            unbound = not old_roles_from_config(config)
        enforced = 0
        failed = 0
        work = list(plan.assignments)
        total = len(work)
        loyalty = getattr(self._bot, "loyalty", None)
        enforce = getattr(loyalty, "enforce_member", None)
        if callable(enforce) and not unbound:
            for index, user_id in enumerate(work, start=1):
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    continue
                try:
                    await enforce(member)
                    enforced += 1
                except Exception:
                    failed += 1
                    log.exception("owner_old_import_enforce_failed", guild_id=guild.id, user_id=user_id)
                if on_progress is not None:
                    await on_progress(index, total)
        log.info(
            "owner_old_import",
            guild_id=guild.id,
            role_id=role.id,
            tier=tier.value,
            added=added,
            updated=updated,
        )
        return OldImportReport(plan, added, updated, enforced, failed, unbound)

    async def _apply_plans(
        self,
        guild: discord.Guild,
        plans: Sequence[MemberRolePlan],
        *,
        mode: MassMode,
        actor_id: int,
        add_ids: frozenset[int],
        remove_ids: frozenset[int],
        strip_all: bool,
        skipped_roles: tuple[str, ...],
        reason: str,
        on_progress: ProgressTick | None,
    ) -> MassRoleReport:
        skipped_bot = sum(1 for plan in plans if plan.skip is MemberSkip.BOT)
        skipped_empty = sum(1 for plan in plans if plan.skip is MemberSkip.EMPTY)
        skipped_require = sum(1 for plan in plans if plan.skip is MemberSkip.REQUIRE)
        skipped_except = sum(1 for plan in plans if plan.skip is MemberSkip.EXCEPT)
        skipped_protect = sum(1 for plan in plans if plan.skip is MemberSkip.PROTECT)
        skipped_timeout = sum(1 for plan in plans if plan.skip is MemberSkip.TIMEOUT)
        skipped_pending = sum(1 for plan in plans if plan.skip is MemberSkip.PENDING)
        skipped_boost = sum(1 for plan in plans if plan.skip is MemberSkip.BOOST)
        work = [plan for plan in plans if not plan.empty]
        registry = await self._sync_registry(guild, work, add_ids=add_ids, remove_ids=remove_ids, strip_all=strip_all)
        changed = 0
        failed = 0
        total = len(work)
        for index, plan in enumerate(work, start=1):
            result = await self._bot.roles.apply(
                RoleClaim(
                    guild_id=guild.id,
                    user_id=plan.user_id,
                    subsystem=RoleSubsystem.OWNER,
                    add=plan.add,
                    remove=plan.remove,
                    reason=reason,
                    urgent=True,
                    actor_id=actor_id,
                )
            )
            if result.ok:
                changed += 1
            else:
                failed += 1
            if on_progress is not None:
                await on_progress(index, total)
        return MassRoleReport(
            mode=mode,
            members=len(work),
            changed=changed,
            failed=failed,
            skipped_bot=skipped_bot,
            skipped_empty=skipped_empty,
            skipped_require=skipped_require,
            skipped_except=skipped_except,
            skipped_protect=skipped_protect,
            skipped_timeout=skipped_timeout,
            skipped_pending=skipped_pending,
            skipped_boost=skipped_boost,
            skipped_roles=skipped_roles,
            registry=registry,
        )

    async def preview_strip(
        self,
        guild: discord.Guild,
        *,
        selected: Sequence[discord.Role],
        all_roles: bool,
        require: Sequence[discord.Role],
        require_mode: MatchMode,
        exclude: Sequence[discord.Role],
        exclude_mode: MatchMode,
        protect: ProtectKind,
        skip_timeout: bool,
        skip_pending: bool,
        skip_boost: bool,
    ) -> MassPreview:
        await _chunk(guild)
        audience = await self.resolve_audience(
            guild,
            slash_audience(
                require=require,
                require_mode=require_mode,
                exclude=exclude,
                exclude_mode=exclude_mode,
                skip_timeout=skip_timeout,
                skip_pending=skip_pending,
                skip_boost=skip_boost,
            ),
            protect,
        )
        targets = {item.id for item in selected} if selected else None
        return MassPreview(
            selected=list(selected),
            kind=protect,
            audience=audience,
            members=count_strip(guild, targets, audience),
            labels=copy.OWNER_ROLES_ALL if all_roles else " ".join(item.mention for item in selected),
            filters=filter_text(
                require=require,
                require_mode=require_mode,
                exclude=exclude,
                exclude_mode=exclude_mode,
                protect=protect,
                skip_timeout=skip_timeout,
                skip_pending=skip_pending,
                skip_boost=skip_boost,
            ),
        )

    async def preview_grant(
        self,
        guild: discord.Guild,
        *,
        selected: Sequence[discord.Role],
        require: Sequence[discord.Role],
        require_mode: MatchMode,
        exclude: Sequence[discord.Role],
        exclude_mode: MatchMode,
        protect: ProtectKind,
        skip_timeout: bool,
        skip_pending: bool,
        skip_boost: bool,
    ) -> MassPreview:
        await _chunk(guild)
        audience = await self.resolve_audience(
            guild,
            slash_audience(
                require=require,
                require_mode=require_mode,
                exclude=exclude,
                exclude_mode=exclude_mode,
                skip_timeout=skip_timeout,
                skip_pending=skip_pending,
                skip_boost=skip_boost,
            ),
            protect,
        )
        return MassPreview(
            selected=list(selected),
            kind=protect,
            audience=audience,
            members=count_grant(guild, tuple(item.id for item in selected), audience),
            labels=" ".join(item.mention for item in selected),
            filters=filter_text(
                require=require,
                require_mode=require_mode,
                exclude=exclude,
                exclude_mode=exclude_mode,
                protect=protect,
                skip_timeout=skip_timeout,
                skip_pending=skip_pending,
                skip_boost=skip_boost,
            ),
        )

    async def resolve_audience(
        self,
        guild: discord.Guild,
        audience: RoleAudience | None,
        protect: ProtectKind,
    ) -> RoleAudience:
        base = audience or RoleAudience()
        if protect is ProtectKind.NONE:
            return base
        staff: tuple[int, ...] = ()
        if protect in {ProtectKind.STAFF, ProtectKind.STAFF_OLD}:
            snapshot = await cached_snapshot(self._bot, guild.id)
            staff = staff_role_ids(snapshot.rank_roles)
        old = old_role_id_set(getattr(self._bot, "config_by_guild", {}).get(guild.id))
        return base.with_protect(protect_ids(protect, staff=staff, old=old))

    async def _sync_registry(
        self,
        guild: discord.Guild,
        plans: Sequence[MemberRolePlan],
        *,
        add_ids: frozenset[int],
        remove_ids: frozenset[int],
        strip_all: bool,
    ) -> int:
        config = getattr(self._bot, "config_by_guild", {}).get(guild.id)
        old_roles = old_roles_from_config(config)
        if not old_roles:
            return 0
        role_to_tier = {role_id: tier for tier, role_id in old_roles.items()}
        add_tiers = frozenset(role_to_tier[role_id] for role_id in add_ids if role_id in role_to_tier)
        remove_tiers = frozenset(role_to_tier[role_id] for role_id in remove_ids if role_id in role_to_tier)
        if not strip_all and not add_tiers and not remove_tiers:
            return 0
        async with session_scope(self._bot.sessions) as session:
            existing = await load_registry(session, guild.id)
            drop: list[int] = []
            upsert: dict[int, LoyaltyTier] = {}
            for plan in plans:
                nxt = registry_after_mass(
                    current=existing.get(plan.user_id),
                    add_tiers=add_tiers,
                    remove_tiers=remove_tiers,
                    strip_all=strip_all,
                )
                if nxt is None:
                    if plan.user_id in existing:
                        drop.append(plan.user_id)
                elif existing.get(plan.user_id) is not nxt:
                    upsert[plan.user_id] = nxt
            gone = await drop_registry_users(session, guild_id=guild.id, user_ids=drop)
            added, updated = await upsert_registry_members(
                session,
                guild_id=guild.id,
                assignments=upsert,
                config=await session.get(GuildConfig, guild.id),
            )
        return gone + added + updated

    async def _after_wipe(self, guild_id: int, section: WipeSection) -> None:
        caches = getattr(self._bot, "caches", None)
        if caches is not None:
            invalidate_config = getattr(caches, "invalidate_config", None)
            invalidate_access = getattr(caches, "invalidate_access", None)
            invalidate_rooms = getattr(caches, "invalidate_guild_rooms", None)
            if callable(invalidate_config):
                invalidate_config(guild_id)
            if callable(invalidate_access):
                invalidate_access(guild_id)
            if callable(invalidate_rooms) and section in {WipeSection.ALL, WipeSection.VOICE}:
                invalidate_rooms(guild_id)
        configs = getattr(self._bot, "config_by_guild", None)
        if isinstance(configs, dict) and section in {WipeSection.ALL, WipeSection.CONFIG}:
            configs.pop(guild_id, None)
        refresh = getattr(self._bot, "refresh_config", None)
        if callable(refresh) and section in {
            WipeSection.ALL,
            WipeSection.CONFIG,
            WipeSection.ACCESS,
            WipeSection.LOYALTY,
        }:
            await refresh(guild_id)


async def _wipe_step(session: Any, models: dict[str, type[Any]], guild_id: int, step: WipeStep) -> int:
    model = models[step.table]
    if step.parent is None:
        result = await session.execute(delete(model).where(model.guild_id == guild_id))
        return int(result.rowcount or 0)
    parent = models[step.parent]
    ids = select(parent.id).where(parent.guild_id == guild_id)
    clause = getattr(model, step.fk).in_(ids)
    if step.extra_fk:
        clause = or_(clause, getattr(model, step.extra_fk).in_(ids))
    result = await session.execute(delete(model).where(clause))
    return int(result.rowcount or 0)


def _held(member: discord.Member) -> set[int]:
    return {role.id for role in getattr(member, "roles", ())}


def _state(member: discord.Member) -> MemberState:
    timed_out = getattr(member, "is_timed_out", None)
    return MemberState(
        timed_out=bool(timed_out()) if callable(timed_out) else False,
        pending=bool(getattr(member, "pending", False)),
        boosting=getattr(member, "premium_since", None) is not None,
    )


def _member_roles(guild: discord.Guild) -> list[discord.Role]:
    seen: dict[int, discord.Role] = {}
    for member in guild.members:
        if member.bot:
            continue
        for role in member.roles:
            seen[role.id] = role
    return list(seen.values())


def _assignable(
    guild: discord.Guild,
    roles: Sequence[discord.Role],
) -> tuple[list[discord.Role], tuple[str, ...]]:
    me = guild.me
    kept: list[discord.Role] = []
    skipped: list[str] = []
    seen: set[int] = set()
    for role in roles:
        if role.id in seen:
            continue
        seen.add(role.id)
        skip = role_skip(
            is_default=bool(role.is_default()),
            managed=bool(role.managed) or (callable(getattr(role, "is_integration", None)) and role.is_integration()),
            above_bot=me is not None and role >= me.top_role,
        )
        if skip is None:
            kept.append(role)
        else:
            skipped.append(role.mention)
    return kept, tuple(skipped)


def _empty_roles_report(mode: MassMode, skipped: tuple[str, ...]) -> MassRoleReport:
    return MassRoleReport(
        mode=mode,
        members=0,
        changed=0,
        failed=0,
        skipped_bot=0,
        skipped_empty=0,
        skipped_require=0,
        skipped_except=0,
        skipped_protect=0,
        skipped_timeout=0,
        skipped_pending=0,
        skipped_boost=0,
        skipped_roles=skipped,
        registry=0,
    )


async def _chunk(guild: discord.Guild) -> None:
    if not guild.chunked:
        await guild.chunk()


def unique_roles(*roles: discord.Role | None) -> list[discord.Role]:
    seen: set[int] = set()
    found: list[discord.Role] = []
    for role in roles:
        if role is None or role.id in seen:
            continue
        seen.add(role.id)
        found.append(role)
    return found


def match_mode(value: str | None) -> MatchMode:
    if value is None:
        return MatchMode.ANY
    try:
        return MatchMode(value)
    except ValueError:
        return MatchMode.ANY


def protect_kind(value: str | None) -> ProtectKind:
    if value is None:
        return ProtectKind.NONE
    try:
        return ProtectKind(value)
    except ValueError:
        return ProtectKind.NONE


def slash_audience(
    *,
    require: Sequence[discord.Role],
    require_mode: MatchMode,
    exclude: Sequence[discord.Role],
    exclude_mode: MatchMode,
    skip_timeout: bool,
    skip_pending: bool,
    skip_boost: bool,
) -> RoleAudience:
    return RoleAudience(
        require=frozenset(role.id for role in require),
        require_mode=require_mode,
        exclude=frozenset(role.id for role in exclude),
        exclude_mode=exclude_mode,
        skip_timeout=skip_timeout,
        skip_pending=skip_pending,
        skip_boost=skip_boost,
    )


def filter_text(
    *,
    require: Sequence[discord.Role],
    require_mode: MatchMode,
    exclude: Sequence[discord.Role],
    exclude_mode: MatchMode,
    protect: ProtectKind,
    skip_timeout: bool,
    skip_pending: bool,
    skip_boost: bool,
) -> str:
    return copy.mass_filter_hint(
        have=" ".join(role.mention for role in require),
        have_mode=require_mode.value,
        without=" ".join(role.mention for role in exclude),
        without_mode=exclude_mode.value,
        protect=protect.value,
        skip_timeout=skip_timeout,
        skip_pending=skip_pending,
        skip_boost=skip_boost,
    )


def count_strip(guild: discord.Guild, targets: set[int] | None, audience: RoleAudience) -> int:
    wanted = targets
    if wanted is None:
        wanted = {
            role.id
            for member in guild.members
            if not member.bot
            for role in member.roles
            if not role.is_default() and not role.managed
        }
    return sum(
        1
        for member in guild.members
        if not plan_strip_member(
            user_id=member.id,
            is_bot=bool(member.bot),
            have=_held(member),
            targets=wanted,
            audience=audience,
            state=_state(member),
        ).empty
    )


def bound_tier(config: GuildConfig | None, role_id: int) -> LoyaltyTier | None:
    for candidate, bound in old_roles_from_config(config).items():
        if bound == role_id:
            return candidate
    return None


def count_grant(guild: discord.Guild, targets: tuple[int, ...], audience: RoleAudience) -> int:
    return sum(
        1
        for member in guild.members
        if not plan_grant_member(
            user_id=member.id,
            is_bot=bool(member.bot),
            have=_held(member),
            targets=targets,
            audience=audience,
            state=_state(member),
        ).empty
    )
