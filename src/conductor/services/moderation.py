"""Дела модерации: запись, санкция Discord, мут через брокер."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import structlog
from sqlalchemy import delete, func, select

from conductor.db.models import ChannelLock, MemberWatch, ModCase, ModIntent, ModSettings
from conductor.db.session import session_scope
from conductor.domain.access import MOD_REVOKE, Rank, StaffTargetBlock, bypasses_limits, resolve_rank
from conductor.domain.dossier import (
    WATCH_SWEEP_SECONDS,
    StoredWatch,
    is_watch_active,
    parse_watch_days,
    watch_until,
)
from conductor.domain.moderation import (
    HEAVY_KINDS,
    PURGE_BATCH,
    SOFTBAN_DELETE_SECONDS,
    CaseCreated,
    CaseKind,
    ConfirmVerdict,
    DurationVerdict,
    ModOutcome,
    ModVerdict,
    StoredCase,
    StoredIntent,
    action_key,
    bot_can_enact,
    clamp_slowmode,
    confirm_intent,
    lock_unlock_delay,
    may_moderate,
    may_revoke,
    needs_confirmation,
    needs_evidence,
    next_case_number,
    parse_timeout_duration,
    pick_purge_batch,
    protected_log_channels,
    purge_channel_blocked,
    revoke_plan,
    sanitize_detail_reason,
    sanitize_reason,
    timeout_still_ours,
    timeout_until,
)
from conductor.domain.quotas import (
    COUNTED_KINDS,
    QuotaDenied,
    QuotaGroup,
    QuotaTable,
    QuotaUsed,
    RankLimit,
    at_daily_limit,
    day_start,
    dump_quota_json,
    group_of,
    parse_quota_json,
    quota_verdict,
)
from conductor.domain.staff_feed import (
    ACTION_BYPASS,
    ACTION_CASE_REVOKE,
    ACTION_QUOTA_DENIED,
    ACTION_WATCH_HIT,
    ACTION_WATCH_SET,
    case_action,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import event, send_card
from conductor.presentation.moderation import case_receipt, purge_log_view, revoke_actor_dm_view, revoke_target_dm_view
from conductor.presentation.tokens import Tone
from conductor.services.access import cached_decide, cached_snapshot, log_limit_bypass
from conductor.services.history import skip_ids_from_config
from conductor.services.role_broker import RoleClaim, RoleSubsystem
from conductor.services.runtime import log_tick
from conductor.services.staff_feed import actor_rank_of, emit_staff

log = structlog.get_logger("conductor.moderation")


class ModerationService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(WATCH_SWEEP_SECONDS, "watch-sweep", self.sweep_watches)

    async def apply(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        *,
        kind: CaseKind,
        reason: str,
        duration_raw: str | None = None,
        now: datetime | None = None,
        system: bool = False,
    ) -> ModOutcome:
        moment = now or datetime.now(UTC)
        cleaned = sanitize_reason(reason)
        if cleaned is None:
            return ModOutcome(ModVerdict.REASON)
        if needs_confirmation(kind) and not system:
            return ModOutcome(ModVerdict.FAILED)
        duration: timedelta | None = None
        if kind is CaseKind.TIMEOUT:
            verdict, duration = parse_timeout_duration(duration_raw or "")
            if verdict is DurationVerdict.BAD or duration is None:
                return ModOutcome(ModVerdict.DURATION)
            if verdict is DurationVerdict.TOO_LONG:
                return ModOutcome(ModVerdict.TOO_LONG)
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_rank = (
            Rank.SPECIAL
            if system
            else resolve_rank(tuple(role.id for role in getattr(actor, "roles", ()) or ()), snapshot.rank_roles)
        )
        target_rank = resolve_rank(tuple(role.id for role in getattr(target, "roles", ()) or ()), snapshot.rank_roles)
        blocked = may_moderate(
            actor_rank=actor_rank,
            target_rank=target_rank,
            target_is_bot=bool(getattr(target, "bot", False)),
            kind=kind,
        )
        if blocked is StaffTargetBlock.BOT:
            return ModOutcome(ModVerdict.BOT)
        if not system:
            probe = getattr(self._bot, "is_developer", None)
            if blocked is StaffTargetBlock.HIERARCHY and not (callable(probe) and probe(actor.id)):
                return ModOutcome(ModVerdict.HIERARCHY)
            quota = await self._quota_gate(guild, actor, actor_rank, kind, now=moment)
            if quota is not None:
                return quota
        if kind in {CaseKind.TIMEOUT, CaseKind.KICK, CaseKind.BAN, CaseKind.SOFTBAN} and not self._discord_can_enact(
            guild, target, kind
        ):
            return ModOutcome(ModVerdict.POWER)
        until = None if duration is None else timeout_until(moment, duration)
        audit = copy.actor_reason(getattr(actor, "id", None), cleaned)
        stored = await self._insert_case(
            guild.id,
            owner_id=target.id,
            created_by=actor.id,
            actor_rank=int(actor_rank),
            kind=kind,
            reason=cleaned,
            duration_seconds=None if duration is None else int(duration.total_seconds()),
            now=moment,
        )
        if stored is None:
            return ModOutcome(ModVerdict.FAILED)
        acted = await self._enact(guild, target, kind, audit, duration)
        if acted is not ModVerdict.OK:
            await self._discard_case(stored.id)
            log.error(
                "mod_enact_failed_after_insert",
                guild_id=guild.id,
                case_id=stored.id,
                kind=kind.value,
                verdict=acted.value,
            )
            return ModOutcome(acted)
        note = None
        try:
            self._emit(stored)
            self._log_case(guild.id, stored, until=until)
            await self._feed_case(stored, now=moment)
            if system:
                evidence = self._evidence()
                deliver = getattr(evidence, "deliver_dm1", None)
                if callable(deliver):
                    await deliver(guild, stored, target, force=True)
            else:
                note = await self._open_evidence(guild, stored, actor, target, now=moment)
        except Exception:
            log.exception("mod_after_enact_failed", guild_id=guild.id, case_id=stored.id)
        return ModOutcome(ModVerdict.OK, case=stored, until=until, note=note)

    async def history(self, guild_id: int, user_id: int) -> list[StoredCase]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(ModCase)
                    .where(ModCase.guild_id == guild_id, ModCase.owner_id == user_id)
                    .order_by(ModCase.number.desc())
                )
            ).all()
            return [_to_stored(row) for row in rows]

    async def get_case(self, case_id: int) -> StoredCase | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(case_id))
            return None if row is None else _to_stored(row)

    async def case_by_number(self, guild_id: int, owner_id: int, number: int) -> StoredCase | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(ModCase).where(
                    ModCase.guild_id == int(guild_id),
                    ModCase.owner_id == int(owner_id),
                    ModCase.number == int(number),
                )
            )
            return None if row is None else _to_stored(row)

    async def guard_case_owner(self, ref: int | str) -> GuardResource | None:
        try:
            case_id = int(ref)
        except (TypeError, ValueError):
            return None
        case = await self.get_case(case_id)
        if case is None:
            return None
        return GuardResource(ref=str(case.id), guild_id=case.guild_id, owner_id=case.owner_id)

    async def revoke(
        self,
        guild: discord.Guild,
        case_id: int,
        by: discord.Member,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> ModOutcome:
        cleaned = sanitize_detail_reason(reason)
        if cleaned is None:
            return ModOutcome(ModVerdict.REASON)
        moment = now or datetime.now(UTC)
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_roles = tuple(role.id for role in getattr(by, "roles", ()) or ())
        actor_rank = resolve_rank(actor_roles, snapshot.rank_roles)
        probe = getattr(self._bot, "is_developer", None)
        is_dev = bool(callable(probe) and probe(by.id))
        is_admin = bool(getattr(getattr(by, "guild_permissions", None), "administrator", False))
        allowed = await cached_decide(
            self._bot,
            guild_id=guild.id,
            user_id=int(by.id),
            member_role_ids=actor_roles,
            is_administrator=is_admin,
            key=MOD_REVOKE,
            now=moment,
        )
        if not allowed.allowed and not is_dev:
            return ModOutcome(ModVerdict.DENIED)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(case_id))
            if row is None or int(row.guild_id) != int(guild.id):
                return ModOutcome(ModVerdict.FAILED)
            if row.revoked_at is not None:
                return ModOutcome(ModVerdict.REVOKED, case=_to_stored(row))
            try:
                case_actor = Rank(int(row.actor_rank))
            except ValueError:
                case_actor = Rank.NONE
            bot_id = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0)
            system_case = bool(bot_id) and int(row.created_by) == bot_id
            if not may_revoke(actor=actor_rank, case_actor=case_actor, system=system_case) and not is_dev:
                return ModOutcome(ModVerdict.HIERARCHY, case=_to_stored(row))
            row.revoked_at = moment
            row.revoked_by = int(by.id)
            row.revoke_reason = cleaned
            await session.flush()
            stored = _to_stored(row)
        plan = revoke_plan(stored)
        await self._undo_discord(guild, stored, cleaned, now=moment)
        refunded = 0
        refund = getattr(getattr(self._bot, "reputation", None), "refund", None)
        if callable(refund):
            refunded = int(await refund(stored.id, now=moment) or 0)
        await self._send_revoke_dms(guild, stored, by, cleaned)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=int(by.id),
            actor_rank=actor_rank if actor_rank is not Rank.NONE else await actor_rank_of(self._bot, guild, by.id),
            action=ACTION_CASE_REVOKE,
            target=stored.owner_id,
            ref=stored.id,
            details=cleaned,
        )
        return ModOutcome(ModVerdict.OK, case=stored, refund=refunded if refunded else plan.refund)

    async def _undo_discord(
        self,
        guild: discord.Guild,
        case: StoredCase,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        plan = revoke_plan(case)
        member = _guild_member(guild, case.owner_id)
        if member is None:
            return
        if plan.undo_discord == "timeout":
            until = getattr(member, "timed_out_until", None) or getattr(member, "communication_disabled_until", None)
            if not timeout_still_ours(
                created_at=case.created_at,
                duration_seconds=case.duration_seconds,
                member_until=until,
                now=now,
            ):
                return
            timeout = getattr(member, "timeout", None)
            if not callable(timeout):
                return
            try:
                await self._discord(timeout(None, reason=reason))
            except discord.HTTPException as exc:
                log.warning("mod_revoke_timeout_failed", guild_id=guild.id, case_id=case.id, error=str(exc))
            return
        if plan.undo_discord == "mute":
            await self._mute(guild, member, reason, add=False)

    async def _send_revoke_dms(
        self,
        guild: discord.Guild,
        case: StoredCase,
        by: discord.Member,
        reason: str,
    ) -> None:
        target = await self._resolve_user(guild, case.owner_id)
        if target is not None:
            await self._send_dm(target, revoke_target_dm_view(case, reason=reason))
        actor = await self._resolve_user(guild, case.created_by)
        if actor is not None and int(getattr(actor, "id", 0) or 0) != int(by.id):
            await self._send_dm(actor, revoke_actor_dm_view(case, by_id=int(by.id), reason=reason))

    async def _resolve_user(self, guild: discord.Guild, user_id: int) -> Any | None:
        member = _guild_member(guild, user_id)
        if member is not None:
            return member
        fetch = getattr(self._bot, "fetch_user", None)
        if callable(fetch):
            try:
                return await self._discord(fetch(int(user_id)))
            except discord.HTTPException:
                return None
        getter = getattr(self._bot, "get_user", None)
        return None if not callable(getter) else getter(int(user_id))

    async def _send_dm(self, target: object, view: object) -> bool:
        send = getattr(target, "send", None)
        if not callable(send):
            return False
        try:
            await self._discord(send_card(target, view))
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def set_watch(
        self,
        guild_id: int,
        user_id: int,
        *,
        set_by: int,
        actor_rank: Rank,
        reason: str,
        days_raw: str,
        now: datetime | None = None,
    ) -> StoredWatch | None:
        cleaned = sanitize_reason(reason)
        days = parse_watch_days(days_raw)
        if cleaned is None or days is None:
            return None
        moment = now or datetime.now(UTC)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        until = watch_until(moment, days)
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(MemberWatch).where(MemberWatch.guild_id == int(guild_id), MemberWatch.user_id == int(user_id))
            )
            if row is None:
                row = MemberWatch(
                    guild_id=int(guild_id),
                    user_id=int(user_id),
                    set_by=int(set_by),
                    reason=cleaned,
                    until=until,
                    created_at=moment,
                )
                session.add(row)
            else:
                row.set_by = int(set_by)
                row.reason = cleaned
                row.until = until
                row.created_at = moment
            await session.flush()
            stored = _to_watch(row)
        await emit_staff(
            self._bot,
            guild_id,
            actor_id=int(set_by),
            actor_rank=actor_rank,
            action=ACTION_WATCH_SET,
            target=int(user_id),
            details=cleaned,
        )
        return stored

    async def clear_watch(self, guild_id: int, user_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(MemberWatch).where(MemberWatch.guild_id == int(guild_id), MemberWatch.user_id == int(user_id))
            )
            if row is None:
                return False
            await session.delete(row)
        return True

    async def active_watch(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> StoredWatch | None:
        moment = now or datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(MemberWatch).where(MemberWatch.guild_id == int(guild_id), MemberWatch.user_id == int(user_id))
            )
            if row is None:
                return None
            stored = _to_watch(row)
        if not is_watch_active(stored, moment):
            return None
        return stored

    async def watched_count(self, guild_id: int, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        async with session_scope(self._bot.sessions) as session:
            value = await session.scalar(
                select(func.count()).where(MemberWatch.guild_id == int(guild_id), MemberWatch.until > moment)
            )
        return int(value or 0)

    async def sweep_watches(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(delete(MemberWatch).where(MemberWatch.until <= moment))
        removed = int(result.rowcount or 0)
        log_tick(log, "watch_sweep", changed=removed)
        return removed

    async def set_mute_role(self, guild_id: int, role_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            if row is None:
                session.add(ModSettings(guild_id=guild_id, mute_role_id=role_id))
            else:
                row.mute_role_id = role_id
        self._bind_mute(guild_id, role_id)

    async def bind_mute(self, guild_id: int) -> None:
        role_id = await self._mute_role_id(guild_id)
        self._bind_mute(guild_id, role_id)

    async def quota_table(self, guild_id: int) -> QuotaTable:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            raw = None if row is None else row.quota_json
        return parse_quota_json(raw)

    async def set_quota_limits(self, guild_id: int, rank: Rank, *, timeouts: int, bans: int) -> QuotaTable:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            table = parse_quota_json(None if row is None else row.quota_json)
            limits = dict(table.limits)
            limits[rank] = RankLimit(timeouts, bans)
            stored = QuotaTable(limits)
            payload = dump_quota_json(stored)
            if row is None:
                session.add(ModSettings(guild_id=guild_id, quota_json=payload))
            else:
                row.quota_json = payload
        return stored

    async def used_today(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> QuotaUsed:
        moment = now or datetime.now(UTC)
        start = day_start(moment, self._timezone(guild_id))
        bot_id = self._bot_id()
        async with session_scope(self._bot.sessions) as session:
            return await _count_used(session, guild_id, user_id, start=start, bot_id=bot_id)

    async def used_today_map(self, guild_id: int, *, now: datetime | None = None) -> dict[int, QuotaUsed]:
        moment = now or datetime.now(UTC)
        start = day_start(moment, self._timezone(guild_id))
        bot_id = self._bot_id()
        async with session_scope(self._bot.sessions) as session:
            return await _count_used_map(session, guild_id, start=start, bot_id=bot_id)

    async def quota_capped_count(self, guild: discord.Guild, *, now: datetime | None = None) -> int:
        snapshot = await cached_snapshot(self._bot, guild.id)
        table = await self.quota_table(guild.id)
        used = await self.used_today_map(guild.id, now=now)
        total = 0
        for member in getattr(guild, "members", ()) or ():
            if bool(getattr(member, "bot", False)):
                continue
            roles = tuple(role.id for role in getattr(member, "roles", ()) or ())
            rank = resolve_rank(roles, snapshot.rank_roles)
            if rank is Rank.NONE:
                continue
            if at_daily_limit(rank, used.get(int(member.id), QuotaUsed()), table):
                total += 1
        return total

    async def begin_heavy(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.abc.User,
        *,
        kind: CaseKind,
        reason: str,
        now: datetime | None = None,
    ) -> ModOutcome:
        if not needs_confirmation(kind):
            return ModOutcome(ModVerdict.FAILED)
        blocked = await self._preflight(guild, actor, target, kind=kind, reason=reason, now=now)
        if isinstance(blocked, ModOutcome):
            return blocked
        cleaned, _actor_rank, moment = blocked
        async with session_scope(self._bot.sessions) as session:
            row = ModIntent(
                guild_id=guild.id,
                actor_id=actor.id,
                target_id=target.id,
                kind=kind,
                reason=cleaned,
                created_at=moment,
            )
            session.add(row)
            await session.flush()
            pending_id = int(row.id)
        return ModOutcome(ModVerdict.OK, pending_id=pending_id)

    async def confirm_heavy(self, guild: discord.Guild, intent_id: int, *, now: datetime | None = None) -> ModOutcome:
        moment = now or datetime.now(UTC)
        intent = await self._load_intent(intent_id, guild.id)
        if intent is None:
            return ModOutcome(ModVerdict.SPENT)
        status = confirm_intent(consumed_at=intent.consumed_at, created_at=intent.created_at, now=moment)
        if status is ConfirmVerdict.SPENT:
            return ModOutcome(ModVerdict.SPENT)
        if status is ConfirmVerdict.EXPIRED:
            await self._consume_intent(intent_id, guild.id, moment)
            return ModOutcome(ModVerdict.EXPIRED)
        blocked = await self._reauthorize_heavy(guild, intent)
        if blocked is not None:
            return blocked
        quota = await self._quota_for_intent(guild, intent, now=moment)
        if quota is not None:
            return quota
        kind = CaseKind(intent.kind)
        target = await self._resolve_target(guild, intent.target_id, kind)
        if target is None:
            return ModOutcome(ModVerdict.FAILED)
        blocked_discord = _bot_can_enact(guild, target, kind)
        if blocked_discord is not None:
            return ModOutcome(blocked_discord)
        taken = await self._consume_intent(intent_id, guild.id, moment)
        if taken is None:
            return ModOutcome(ModVerdict.SPENT)
        intent = taken
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_roles = ()
        actor_member = guild.get_member(intent.actor_id)
        if actor_member is not None:
            actor_roles = tuple(role.id for role in actor_member.roles)
        actor_rank = resolve_rank(actor_roles, snapshot.rank_roles)
        actor_is_bot = self._bot_id() == intent.actor_id
        stored: StoredCase | None = None
        if needs_evidence(kind, bool(actor_is_bot)):
            stored = await self._insert_case(
                guild.id,
                owner_id=intent.target_id,
                created_by=intent.actor_id,
                actor_rank=int(actor_rank),
                kind=kind,
                reason=intent.reason,
                duration_seconds=None,
                now=moment,
            )
            if stored is None:
                return ModOutcome(ModVerdict.FAILED)
            evidence = self._evidence()
            dm1_ok = False
            if evidence is not None:
                dm1_ok = await evidence.deliver_dm1(guild, stored, target)
            acted = await self._enact_heavy(
                guild, target, kind, copy.actor_reason(intent.actor_id, intent.reason)
            )
            if acted is not ModVerdict.OK:
                await self._abandon_heavy(guild, stored, target, moment)
                return ModOutcome(acted)
            note = None
            if evidence is not None:
                result = await evidence.open_channel(
                    guild,
                    stored,
                    actor_member or SimpleNamespace(id=intent.actor_id),
                    target,
                    dm1_ok=dm1_ok,
                    now=moment,
                )
                note = getattr(result, "error", None)
            self._emit(stored)
            self._log_case(guild.id, stored, until=None)
            await self._feed_case(stored, now=moment)
            return ModOutcome(ModVerdict.OK, case=stored, note=note)
        acted = await self._enact_heavy(guild, target, kind, copy.actor_reason(intent.actor_id, intent.reason))
        if acted is not ModVerdict.OK:
            return ModOutcome(acted)
        stored = await self._insert_case(
            guild.id,
            owner_id=intent.target_id,
            created_by=intent.actor_id,
            actor_rank=int(actor_rank),
            kind=kind,
            reason=intent.reason,
            duration_seconds=None,
            now=moment,
        )
        if stored is None:
            return ModOutcome(ModVerdict.FAILED)
        self._emit(stored)
        self._log_case(guild.id, stored, until=None)
        await self._feed_case(stored, now=moment)
        return ModOutcome(ModVerdict.OK, case=stored)

    async def cancel_heavy(self, guild_id: int, intent_id: int, *, now: datetime | None = None) -> None:
        await self._consume_intent(intent_id, guild_id, now or datetime.now(UTC))

    async def guard_intent(self, guild_id: int, ref: int | str) -> GuardResource | None:
        try:
            intent_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModIntent, intent_id)
            if row is None or int(row.guild_id) != guild_id:
                return None
            created_at = row.created_at
            consumed_at = row.consumed_at
            actor_id = int(row.actor_id)
            target_id = int(row.target_id)
        verdict = confirm_intent(consumed_at=consumed_at, created_at=created_at, now=datetime.now(UTC))
        snapshot = await cached_snapshot(self._bot, guild_id)
        guild = self._bot.get_guild(guild_id)
        member = None if guild is None else guild.get_member(target_id)
        roles = tuple(role.id for role in getattr(member, "roles", ()) or ())
        target_rank = resolve_rank(roles, snapshot.rank_roles)
        return GuardResource(
            ref=str(intent_id),
            guild_id=guild_id,
            owner_id=actor_id,
            closed=verdict is not ConfirmVerdict.OK,
            target_id=target_id,
            target_rank=target_rank,
        )

    async def intent_kind(self, guild_id: int, ref: int | str) -> str | None:
        try:
            intent_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModIntent, intent_id)
            if row is None or int(row.guild_id) != guild_id:
                return None
            return str(row.kind)

    async def purge(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.abc.GuildChannel,
        *,
        reason: str,
        user_id: int | None = None,
        after_id: int | None = None,
        now: datetime | None = None,
        on_batch: Any | None = None,
    ) -> ModOutcome:
        moment = now or datetime.now(UTC)
        cleaned = sanitize_reason(reason)
        if cleaned is None:
            return ModOutcome(ModVerdict.REASON)
        config = getattr(self._bot, "config_by_guild", {}).get(guild.id)
        protected = skip_ids_from_config(config) or protected_log_channels(
            getattr(config, "user_log_channel_id", None),
            getattr(config, "admin_log_channel_id", None),
        )
        if purge_channel_blocked(channel.id, protected):
            return ModOutcome(ModVerdict.LOG_CHANNEL)
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_rank = resolve_rank(tuple(role.id for role in actor.roles), snapshot.rank_roles)
        deleted, skipped = await self._purge_loop(
            channel,
            user_id=user_id,
            after_id=after_id,
            reason=cleaned,
            now=moment,
            on_batch=on_batch,
        )
        stored = await self._insert_case(
            guild.id,
            owner_id=0 if user_id is None else user_id,
            created_by=actor.id,
            actor_rank=int(actor_rank),
            kind=CaseKind.PURGE,
            reason=cleaned,
            duration_seconds=None,
            now=moment,
        )
        if stored is None:
            return ModOutcome(ModVerdict.FAILED, deleted=deleted, skipped=skipped)
        self._emit(stored)
        self._log_purge(guild.id, stored, channel=channel, actor=actor, deleted=deleted, skipped=skipped)
        await self._feed_case(stored, target_id=int(channel.id), channel=True, now=moment)
        return ModOutcome(ModVerdict.OK, case=stored, deleted=deleted, skipped=skipped)

    async def lock_channel(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.abc.GuildChannel,
        *,
        reason: str,
        duration_raw: str,
        slowmode: int | None = None,
        now: datetime | None = None,
    ) -> ModOutcome:
        moment = now or datetime.now(UTC)
        cleaned = sanitize_reason(reason)
        if cleaned is None:
            return ModOutcome(ModVerdict.REASON)
        verdict, duration = parse_timeout_duration(duration_raw)
        if verdict is DurationVerdict.BAD or duration is None:
            return ModOutcome(ModVerdict.DURATION)
        if verdict is DurationVerdict.TOO_LONG:
            return ModOutcome(ModVerdict.TOO_LONG)
        delay_seconds = None if slowmode is None else clamp_slowmode(slowmode)
        if slowmode is not None and delay_seconds is None:
            return ModOutcome(ModVerdict.SLOWMODE)
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_rank = resolve_rank(tuple(role.id for role in actor.roles), snapshot.rank_roles)
        until = timeout_until(moment, duration)
        previous_slowmode = int(getattr(channel, "slowmode_delay", 0) or 0)
        previous_send = _everyone_send(channel, guild)
        applied = delay_seconds if delay_seconds is not None else previous_slowmode
        locked = await self._apply_lock(
            channel, guild, reason=copy.actor_reason(actor.id, cleaned), slowmode=delay_seconds
        )
        if not locked:
            return ModOutcome(ModVerdict.FAILED)
        async with session_scope(self._bot.sessions) as session:
            active = (
                await session.scalars(
                    select(ChannelLock).where(
                        ChannelLock.guild_id == guild.id,
                        ChannelLock.channel_id == channel.id,
                        ChannelLock.unlocked_at.is_(None),
                    )
                )
            ).all()
            for row in active:
                row.unlocked_at = moment
            session.add(
                ChannelLock(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    created_by=actor.id,
                    reason=cleaned,
                    slowmode=applied,
                    previous_slowmode=previous_slowmode,
                    previous_send=previous_send,
                    created_at=moment,
                    unlock_at=until,
                )
            )
        self._schedule_unlock(guild.id, channel.id, lock_unlock_delay(until, moment))
        stored = await self._insert_case(
            guild.id,
            owner_id=channel.id,
            created_by=actor.id,
            actor_rank=int(actor_rank),
            kind=CaseKind.LOCK,
            reason=cleaned,
            duration_seconds=int(duration.total_seconds()),
            now=moment,
        )
        if stored is None:
            return ModOutcome(ModVerdict.FAILED, until=until)
        self._emit(stored)
        self._log_case(guild.id, stored, until=until)
        await self._feed_case(stored, channel=True, now=moment)
        return ModOutcome(ModVerdict.OK, case=stored, until=until)

    async def unlock_channel(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
        *,
        now: datetime | None = None,
        reason: str = copy.REASON_LOCK_CLEAR,
    ) -> ModOutcome:
        moment = now or datetime.now(UTC)
        row = await self._active_lock(guild.id, channel.id)
        if row is None:
            return ModOutcome(ModVerdict.MISSING)
        restored = await self._restore_lock(channel, guild, row, reason=reason)
        if not restored:
            return ModOutcome(ModVerdict.FAILED)
        await self._mark_unlocked(row.id, moment)
        self._cancel_unlock(guild.id, channel.id)
        return ModOutcome(ModVerdict.OK)

    async def restore_locks(self, guild: discord.Guild) -> None:
        now = datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(ChannelLock).where(
                        ChannelLock.guild_id == guild.id,
                        ChannelLock.unlocked_at.is_(None),
                    )
                )
            ).all()
            pending = [(int(row.channel_id), row.unlock_at) for row in rows]
        for channel_id, unlock_at in pending:
            self._schedule_unlock(guild.id, channel_id, lock_unlock_delay(unlock_at, now))

    async def unlock_due(self, guild_id: int, channel_id: int) -> None:
        guild = self._bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        await self.unlock_channel(guild, channel, reason=copy.REASON_LOCK_EXPIRED)

    async def _preflight(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: object,
        *,
        kind: CaseKind,
        reason: str,
        now: datetime | None,
    ) -> ModOutcome | tuple[str, Rank, datetime]:
        moment = now or datetime.now(UTC)
        cleaned = sanitize_reason(reason)
        if cleaned is None:
            return ModOutcome(ModVerdict.REASON)
        snapshot = await cached_snapshot(self._bot, guild.id)
        actor_rank = resolve_rank(tuple(role.id for role in getattr(actor, "roles", ()) or ()), snapshot.rank_roles)
        target_rank = resolve_rank(tuple(role.id for role in getattr(target, "roles", ()) or ()), snapshot.rank_roles)
        blocked = may_moderate(
            actor_rank=actor_rank,
            target_rank=target_rank,
            target_is_bot=bool(getattr(target, "bot", False)),
            kind=kind,
        )
        if blocked is StaffTargetBlock.BOT:
            return ModOutcome(ModVerdict.BOT)
        probe = getattr(self._bot, "is_developer", None)
        if blocked is StaffTargetBlock.HIERARCHY and not (callable(probe) and probe(actor.id)):
            return ModOutcome(ModVerdict.HIERARCHY)
        if kind in {CaseKind.TIMEOUT, CaseKind.KICK, CaseKind.BAN, CaseKind.SOFTBAN} and not self._discord_can_enact(
            guild, target, kind
        ):
            return ModOutcome(ModVerdict.POWER)
        quota = await self._quota_gate(guild, actor, actor_rank, kind, now=moment)
        if quota is not None:
            return quota
        return cleaned, actor_rank, moment

    async def _quota_for_intent(
        self,
        guild: discord.Guild,
        intent: StoredIntent,
        *,
        now: datetime,
    ) -> ModOutcome | None:
        actor = guild.get_member(intent.actor_id)
        snapshot = await cached_snapshot(self._bot, guild.id)
        roles = tuple(role.id for role in getattr(actor, "roles", ()) or ())
        rank = resolve_rank(roles, snapshot.rank_roles)
        person = actor if actor is not None else SimpleNamespace(id=intent.actor_id)
        return await self._quota_gate(guild, person, rank, CaseKind(intent.kind), now=now)

    async def _quota_gate(
        self,
        guild: discord.Guild,
        actor: object,
        rank: Rank,
        kind: CaseKind,
        *,
        now: datetime,
    ) -> ModOutcome | None:
        group = group_of(kind)
        if group is None:
            return None
        user_id = int(getattr(actor, "id", 0) or 0)
        permissions = getattr(actor, "guild_permissions", None)
        probe = getattr(self._bot, "is_developer", None)
        # Разработчик без штатной роли иначе упирается в «0 из 0», хотя иерархию выше он уже обошёл.
        bypass = bypasses_limits(
            is_administrator=bool(getattr(permissions, "administrator", False)),
            is_guild_owner=bool(getattr(guild, "owner_id", None) == user_id),
        ) or bool(callable(probe) and probe(user_id))
        table = await self.quota_table(guild.id)
        used = await self.used_today(guild.id, user_id, now=now)
        tz = self._timezone(guild.id)
        verdict = quota_verdict(rank, used.for_group(group), table, kind, False, now=now, tz=tz)
        if bypass:
            if isinstance(verdict, QuotaDenied) and log_limit_bypass(
                guild.id, user_id, f"quota.{group.value}", bot=self._bot
            ):
                await emit_staff(
                    self._bot,
                    guild.id,
                    actor_id=user_id,
                    actor_rank=rank,
                    action=ACTION_BYPASS,
                    details=f"quota.{group.value}",
                )
            return None
        if isinstance(verdict, QuotaDenied):
            if self._log_quota_denied(guild.id, user_id, verdict):
                await emit_staff(
                    self._bot,
                    guild.id,
                    actor_id=user_id,
                    actor_rank=rank,
                    action=ACTION_QUOTA_DENIED,
                    details=f"{verdict.group}:{verdict.used}/{verdict.limit}",
                )
            return _quota_outcome(verdict)
        return None

    def _timezone(self, guild_id: int) -> str:
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        zone = getattr(config, "timezone", None)
        return str(zone) if zone else "UTC"

    def _bot_id(self) -> int | None:
        user = getattr(self._bot, "user", None)
        ident = getattr(user, "id", None)
        return None if ident is None else int(ident)

    def _evidence(self) -> Any:
        return getattr(self._bot, "evidence", None)

    async def _open_evidence(
        self,
        guild: discord.Guild,
        case: StoredCase,
        actor: object,
        target: object,
        *,
        now: datetime | None = None,
    ) -> str | None:
        service = self._evidence()
        open_for = getattr(service, "open_for_case", None)
        if not callable(open_for):
            return None
        result = await open_for(guild, case, actor, target, now=now)
        return getattr(result, "error", None)

    def _log_quota_denied(self, guild_id: int, user_id: int, denied: QuotaDenied) -> bool:
        caches = getattr(self._bot, "caches", None)
        cache = getattr(caches, "limit_bypass", None)
        key = (int(guild_id), int(user_id), "quota.denied")
        if cache is not None and cache.has(key):
            return False
        if cache is not None:
            cache.set(key, True)
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if callable(submit) and channel_id is not None:
            submit(
                channel_id,
                view=event(
                    copy.QUOTA_DENIED_TITLE,
                    copy.quota_denied_log(
                        user_id=user_id,
                        used=denied.used,
                        limit=denied.limit,
                        group=denied.group,
                    ),
                    tone=Tone.WARN,
                    source=copy.LIMIT_BYPASS_SOURCE,
                ),
            )
        return True

    async def _reauthorize_heavy(self, guild: discord.Guild, intent: StoredIntent) -> ModOutcome | None:
        kind = CaseKind(intent.kind)
        key = action_key(kind)
        if key is None:
            return ModOutcome(ModVerdict.FAILED)
        actor = guild.get_member(intent.actor_id)
        actor_roles = tuple(role.id for role in getattr(actor, "roles", ()) or ())
        permissions = getattr(actor, "guild_permissions", None)
        decision = await cached_decide(
            self._bot,
            guild_id=guild.id,
            user_id=intent.actor_id,
            member_role_ids=actor_roles,
            is_administrator=bool(getattr(permissions, "administrator", False)),
            key=key,
        )
        if not decision.allowed and not decision.developer:
            return ModOutcome(ModVerdict.DENIED)
        target = guild.get_member(intent.target_id)
        snapshot = await cached_snapshot(self._bot, guild.id)
        target_rank = resolve_rank(
            tuple(role.id for role in getattr(target, "roles", ()) or ()),
            snapshot.rank_roles,
        )
        blocked = may_moderate(
            actor_rank=decision.rank,
            target_rank=target_rank,
            target_is_bot=bool(getattr(target, "bot", False)),
            kind=kind,
        )
        if blocked is StaffTargetBlock.BOT:
            return ModOutcome(ModVerdict.BOT)
        if blocked is StaffTargetBlock.HIERARCHY and not decision.developer:
            return ModOutcome(ModVerdict.HIERARCHY)
        return None

    async def _load_intent(self, intent_id: int, guild_id: int) -> StoredIntent | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModIntent, intent_id)
            if row is None or int(row.guild_id) != guild_id:
                return None
            return _to_intent(row)

    async def _consume_intent(self, intent_id: int, guild_id: int, now: datetime) -> StoredIntent | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModIntent, intent_id)
            if row is None or int(row.guild_id) != guild_id or row.consumed_at is not None:
                return None
            row.consumed_at = now
            await session.flush()
            return _to_intent(row)

    async def _resolve_target(self, guild: discord.Guild, user_id: int, kind: CaseKind) -> object | None:
        if kind is CaseKind.UNBAN:
            return discord.Object(id=user_id)
        member = guild.get_member(user_id)
        if member is not None:
            return member
        fetch = getattr(guild, "fetch_member", None)
        if not callable(fetch):
            return None
        try:
            return await self._discord(fetch(user_id))
        except discord.HTTPException:
            return None

    async def _enact_heavy(self, guild: discord.Guild, target: object, kind: CaseKind, reason: str) -> ModVerdict:
        try:
            if kind is CaseKind.KICK:
                kick = getattr(target, "kick", None)
                if not callable(kick):
                    return ModVerdict.FAILED
                await self._discord(kick(reason=reason))
            elif kind is CaseKind.BAN:
                await self._discord(guild.ban(target, delete_message_seconds=0, reason=reason))
            elif kind is CaseKind.UNBAN:
                await self._discord(guild.unban(target, reason=reason))
            elif kind is CaseKind.SOFTBAN:
                await self._discord(guild.ban(target, delete_message_seconds=SOFTBAN_DELETE_SECONDS, reason=reason))
                await self._discord(guild.unban(target, reason=reason))
            else:
                return ModVerdict.FAILED
        except discord.HTTPException as exc:
            log.warning("mod_heavy_failed", guild_id=guild.id, kind=kind, error=str(exc))
            return ModVerdict.FAILED
        return ModVerdict.OK

    async def _abandon_heavy(
        self,
        guild: discord.Guild,
        stored: StoredCase,
        target: object,
        moment: datetime,
    ) -> None:
        bot_id = self._bot_id() or 0
        reason = copy.MOD_FAILED
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(stored.id))
            if row is None or row.revoked_at is not None:
                return
            row.revoked_at = moment
            row.revoked_by = bot_id
            row.revoke_reason = reason
            stored = _to_stored(row)
        refund = getattr(getattr(self._bot, "reputation", None), "refund", None)
        if callable(refund):
            await refund(stored.id, now=moment)
        await self._send_dm(target, revoke_target_dm_view(stored, reason=reason))

    async def _purge_loop(
        self,
        channel: object,
        *,
        user_id: int | None,
        after_id: int | None,
        reason: str,
        now: datetime,
        on_batch: Any | None,
    ) -> tuple[int, int]:
        deleted = 0
        skipped = 0
        batch_n = 0
        before: object | None = None
        while True:
            rows, messages = await self._history_page(channel, after_id=after_id, before=before)
            if not rows:
                break
            picked = pick_purge_batch(rows, now=now, user_id=user_id, limit=PURGE_BATCH)
            skipped += picked.skipped_old
            batch = [messages[message_id] for message_id in picked.delete_ids if message_id in messages]
            if batch:
                ok = await self._delete_batch(channel, batch, reason=reason)
                if ok:
                    deleted += len(batch)
                    batch_n += 1
                    if callable(on_batch):
                        await on_batch(deleted, skipped, batch_n)
            if picked.stop or (not batch and picked.skipped_old):
                break
            if not rows:
                break
            before = discord.Object(id=rows[-1][0])
            if len(rows) < PURGE_BATCH and not picked.delete_ids:
                break
            if len(rows) < PURGE_BATCH and user_id is None:
                break
        return deleted, skipped

    async def _history_page(
        self,
        channel: object,
        *,
        after_id: int | None,
        before: object | None,
    ) -> tuple[list[tuple[int, datetime, int]], dict[int, object]]:
        history = getattr(channel, "history", None)
        if not callable(history):
            return [], {}
        kwargs: dict[str, object] = {"limit": PURGE_BATCH}
        if after_id is not None:
            kwargs["after"] = discord.Object(id=after_id)
        if before is not None:
            kwargs["before"] = before
        rows: list[tuple[int, datetime, int]] = []
        messages: dict[int, object] = {}
        try:
            async for message in history(**kwargs):
                created = getattr(message, "created_at", None)
                author = getattr(message, "author", None)
                message_id = int(getattr(message, "id", 0))
                if created is None or author is None or not message_id:
                    continue
                created_at = created if created.tzinfo else created.replace(tzinfo=UTC)
                rows.append((message_id, created_at, int(author.id)))
                messages[message_id] = message
        except discord.HTTPException as exc:
            log.warning("mod_purge_history_failed", error=str(exc))
            return [], {}
        return rows, messages

    async def _delete_batch(self, channel: object, messages: list[object], *, reason: str) -> bool:
        try:
            if len(messages) == 1:
                delete = getattr(messages[0], "delete", None)
                if not callable(delete):
                    return False
                await self._discord(delete(reason=reason))
                return True
            bulk = getattr(channel, "delete_messages", None)
            if not callable(bulk):
                return False
            await self._discord(bulk(messages, reason=reason))
            return True
        except discord.HTTPException as exc:
            log.warning("mod_purge_delete_failed", error=str(exc))
            return False

    async def _apply_lock(
        self,
        channel: object,
        guild: discord.Guild,
        *,
        reason: str,
        slowmode: int | None,
    ) -> bool:
        everyone = getattr(guild, "default_role", None)
        overwrites_for = getattr(channel, "overwrites_for", None)
        set_permissions = getattr(channel, "set_permissions", None)
        if everyone is None or not callable(overwrites_for) or not callable(set_permissions):
            return False
        try:
            overwrite = overwrites_for(everyone)
            overwrite.send_messages = False
            await self._discord(set_permissions(everyone, overwrite=overwrite, reason=reason))
            if slowmode is not None:
                edit = getattr(channel, "edit", None)
                if callable(edit):
                    await self._discord(edit(slowmode_delay=slowmode, reason=reason))
        except discord.HTTPException as exc:
            log.warning("mod_lock_failed", guild_id=guild.id, error=str(exc))
            return False
        return True

    async def _restore_lock(self, channel: object, guild: discord.Guild, row: ChannelLock, *, reason: str) -> bool:
        everyone = getattr(guild, "default_role", None)
        overwrites_for = getattr(channel, "overwrites_for", None)
        set_permissions = getattr(channel, "set_permissions", None)
        if everyone is None or not callable(overwrites_for) or not callable(set_permissions):
            return False
        try:
            overwrite = overwrites_for(everyone)
            overwrite.send_messages = row.previous_send
            await self._discord(set_permissions(everyone, overwrite=overwrite, reason=reason))
            edit = getattr(channel, "edit", None)
            if callable(edit):
                await self._discord(edit(slowmode_delay=int(row.previous_slowmode), reason=reason))
        except discord.HTTPException as exc:
            log.warning("mod_unlock_failed", guild_id=guild.id, error=str(exc))
            return False
        return True

    async def _active_lock(self, guild_id: int, channel_id: int) -> ChannelLock | None:
        async with session_scope(self._bot.sessions) as session:
            row = (
                await session.scalars(
                    select(ChannelLock)
                    .where(
                        ChannelLock.guild_id == guild_id,
                        ChannelLock.channel_id == channel_id,
                        ChannelLock.unlocked_at.is_(None),
                    )
                    .order_by(ChannelLock.id.desc())
                )
            ).first()
            if row is None:
                return None
            session.expunge(row)
            return row

    async def _mark_unlocked(self, lock_id: int, now: datetime) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ChannelLock, lock_id)
            if row is not None:
                row.unlocked_at = now

    def _schedule_unlock(self, guild_id: int, channel_id: int, delay: float) -> None:
        queue = getattr(self._bot, "queue", None)
        schedule = getattr(queue, "schedule_later", None)
        if not callable(schedule):
            return

        def job(gid: int = guild_id, cid: int = channel_id) -> Any:
            return self.unlock_due(gid, cid)

        schedule(("mod-unlock", guild_id, channel_id), delay, job)

    def _cancel_unlock(self, guild_id: int, channel_id: int) -> None:
        queue = getattr(self._bot, "queue", None)
        cancel = getattr(queue, "cancel_later", None)
        if callable(cancel):
            cancel(("mod-unlock", guild_id, channel_id))

    def _log_purge(
        self,
        guild_id: int,
        case: StoredCase,
        *,
        channel: object,
        actor: object,
        deleted: int,
        skipped: int,
    ) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if not callable(submit) or channel_id is None:
            return
        mention = getattr(channel, "mention", f"#{getattr(channel, 'id', 0)}")
        actor_mention = getattr(actor, "mention", f"<@{getattr(actor, 'id', 0)}>")
        submit(
            channel_id,
            view=purge_log_view(case, channel=mention, actor=actor_mention, deleted=deleted, skipped=skipped),
        )

    async def _enact(
        self,
        guild: discord.Guild,
        target: discord.Member,
        kind: CaseKind,
        reason: str,
        duration: timedelta | None,
    ) -> ModVerdict:
        if kind in HEAVY_KINDS:
            return await self._enact_heavy(guild, target, kind, reason)
        if kind is CaseKind.TIMEOUT:
            timeout = getattr(target, "timeout", None)
            if not callable(timeout) or duration is None:
                return ModVerdict.FAILED
            try:
                await self._discord(timeout(duration, reason=reason))
            except Exception as exc:
                log.warning("mod_timeout_failed", guild_id=guild.id, user_id=target.id, error=str(exc))
                return ModVerdict.FAILED
            return ModVerdict.OK
        if kind is CaseKind.MUTE:
            return await self._mute(guild, target, reason, add=True)
        if kind is CaseKind.UNMUTE:
            return await self._mute(guild, target, reason, add=False)
        return ModVerdict.OK

    async def _mute(self, guild: discord.Guild, target: discord.Member, reason: str, *, add: bool) -> ModVerdict:
        role_id = await self._mute_role_id(guild.id)
        if role_id is None:
            return ModVerdict.MUTE_ROLE
        self._bind_mute(guild.id, role_id)
        broker = getattr(self._bot, "roles", None)
        apply = getattr(broker, "apply", None)
        if not callable(apply):
            return ModVerdict.FAILED
        claim = RoleClaim(
            guild_id=guild.id,
            user_id=target.id,
            subsystem=RoleSubsystem.MODERATION,
            add=(role_id,) if add else (),
            remove=() if add else (role_id,),
            reason=reason[:512],
            urgent=True,
            actor_id=None,
        )
        result = await apply(claim)
        if not getattr(result, "ok", False):
            log.warning("mod_mute_failed", guild_id=guild.id, user_id=target.id, error=getattr(result, "error", None))
            return ModVerdict.FAILED
        return ModVerdict.OK

    def _discord_can_enact(self, guild: object, target: object, kind: CaseKind) -> bool:
        me = getattr(guild, "me", None)
        owner_id = getattr(guild, "owner_id", None)
        target_id = getattr(target, "id", None)
        is_owner = owner_id is not None and target_id is not None and int(owner_id) == int(target_id)
        perms = None if me is None else getattr(me, "guild_permissions", None)
        can_moderate = None
        if perms is not None:
            admin = bool(getattr(perms, "administrator", False))
            if kind is CaseKind.TIMEOUT:
                can_moderate = admin or bool(getattr(perms, "moderate_members", False))
            elif kind is CaseKind.KICK:
                can_moderate = admin or bool(getattr(perms, "kick_members", False))
            else:
                can_moderate = admin or bool(getattr(perms, "ban_members", False))
        target_perms = getattr(target, "guild_permissions", None)
        target_is_admin = bool(getattr(target_perms, "administrator", False)) if target_perms is not None else False
        return bot_can_enact(
            bot_top=None if me is None else getattr(getattr(me, "top_role", None), "position", None),
            target_top=getattr(getattr(target, "top_role", None), "position", None),
            target_is_owner=is_owner,
            bot_can_moderate=True if can_moderate is None else can_moderate,
            target_is_admin=kind is CaseKind.TIMEOUT and target_is_admin,
        )

    async def _discard_case(self, case_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(case_id))
            if row is not None:
                await session.delete(row)

    async def _insert_case(
        self,
        guild_id: int,
        *,
        owner_id: int,
        created_by: int,
        actor_rank: int,
        kind: CaseKind,
        reason: str,
        duration_seconds: int | None,
        now: datetime,
    ) -> StoredCase | None:
        async with session_scope(self._bot.sessions) as session:
            highest = await session.scalar(select(func.max(ModCase.number)).where(ModCase.guild_id == guild_id))
            row = ModCase(
                guild_id=guild_id,
                number=next_case_number(highest),
                owner_id=owner_id,
                created_by=created_by,
                actor_rank=actor_rank,
                kind=kind,
                reason=reason,
                duration_seconds=duration_seconds,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return _to_stored(row)

    async def mute_role_id(self, guild_id: int) -> int | None:
        return await self._mute_role_id(guild_id)

    async def _mute_role_id(self, guild_id: int) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            if row is None or row.mute_role_id is None:
                return None
            return int(row.mute_role_id)

    def _bind_mute(self, guild_id: int, role_id: int | None) -> None:
        broker = getattr(self._bot, "roles", None)
        bind = getattr(broker, "bind", None)
        if not callable(bind):
            return
        bind(guild_id, RoleSubsystem.MODERATION, () if role_id is None else (role_id,))

    def _emit(self, case: StoredCase) -> None:
        events = getattr(self._bot, "events", None)
        emit = getattr(events, "emit", None)
        if not callable(emit) or case.created_at is None:
            return
        emit(
            CaseCreated(
                guild_id=case.guild_id,
                case_id=case.id,
                number=case.number,
                target_id=case.owner_id,
                actor_id=case.created_by,
                actor_rank=case.actor_rank,
                kind=case.kind,
                reason=case.reason,
                duration_seconds=case.duration_seconds,
                created_at=case.created_at,
            )
        )

    def _log_case(self, guild_id: int, case: StoredCase, *, until: datetime | None = None) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if not callable(submit) or channel_id is None:
            return
        submit(channel_id, view=case_receipt(case, until=until))

    async def _feed_case(
        self,
        case: StoredCase,
        *,
        target_id: int | None = None,
        channel: bool = False,
        now: datetime | None = None,
    ) -> None:
        who = case.owner_id if target_id is None else target_id
        await emit_staff(
            self._bot,
            case.guild_id,
            actor_id=case.created_by,
            actor_rank=case.actor_rank,
            action=case_action(case.kind),
            target=None if not who else int(who),
            ref=case.id,
            details=case.reason,
            channel=channel,
        )
        await self._watch_hit(case, now=now)

    async def _watch_hit(self, case: StoredCase, *, now: datetime | None = None) -> None:
        if int(case.owner_id or 0) <= 0:
            return
        watch = await self.active_watch(case.guild_id, case.owner_id, now=now)
        if watch is None:
            return
        await emit_staff(
            self._bot,
            case.guild_id,
            actor_id=case.created_by,
            actor_rank=case.actor_rank,
            action=ACTION_WATCH_HIT,
            target=case.owner_id,
            ref=case.id,
            details=watch.reason,
        )

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _to_watch(row: MemberWatch) -> StoredWatch:
    until = row.until
    created = row.created_at
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return StoredWatch(
        guild_id=int(row.guild_id),
        user_id=int(row.user_id),
        set_by=int(row.set_by),
        reason=row.reason,
        until=until,
        created_at=created,
    )


def _to_stored(row: ModCase) -> StoredCase:
    return StoredCase(
        id=row.id,
        guild_id=row.guild_id,
        number=row.number,
        owner_id=row.owner_id,
        created_by=row.created_by,
        actor_rank=row.actor_rank,
        kind=row.kind,
        reason=row.reason,
        duration_seconds=row.duration_seconds,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoke_reason=row.revoke_reason,
        appeal_ticket_id=row.appeal_ticket_id,
    )


def _guild_member(guild: object, user_id: int) -> Any:
    getter = getattr(guild, "get_member", None)
    return None if not callable(getter) else getter(int(user_id))


def _bot_can_enact(guild: object, target: object, kind: CaseKind) -> ModVerdict | None:
    if kind not in {CaseKind.KICK, CaseKind.BAN, CaseKind.SOFTBAN, CaseKind.UNBAN}:
        return None
    me = getattr(guild, "me", None)
    if me is None:
        return None
    perms = getattr(me, "guild_permissions", None)
    need_ban = kind in {CaseKind.BAN, CaseKind.SOFTBAN, CaseKind.UNBAN}
    if perms is not None:
        flag = "ban_members" if need_ban else "kick_members"
        if not bool(getattr(perms, flag, False)):
            return ModVerdict.FAILED
    if kind is CaseKind.UNBAN:
        return None
    mine = getattr(getattr(me, "top_role", None), "position", None)
    theirs = getattr(getattr(target, "top_role", None), "position", None)
    if mine is None or theirs is None:
        return None
    if int(mine) <= int(theirs):
        return ModVerdict.HIERARCHY
    return None


def _to_intent(row: ModIntent) -> StoredIntent:
    return StoredIntent(
        id=row.id,
        guild_id=row.guild_id,
        actor_id=row.actor_id,
        target_id=row.target_id,
        kind=row.kind,
        reason=row.reason,
        created_at=row.created_at,
        consumed_at=row.consumed_at,
    )


def _quota_outcome(denied: QuotaDenied) -> ModOutcome:
    return ModOutcome(
        ModVerdict.QUOTA,
        quota_limit=denied.limit,
        quota_used=denied.used,
        quota_resets_at=denied.resets_at,
        quota_group=denied.group,
    )


async def _count_used(
    session: Any,
    guild_id: int,
    user_id: int,
    *,
    start: datetime,
    bot_id: int | None,
) -> QuotaUsed:
    if bot_id is not None and int(user_id) == int(bot_id):
        return QuotaUsed()
    rows = (
        await session.execute(
            select(ModCase.kind, func.count())
            .where(
                ModCase.guild_id == guild_id,
                ModCase.created_by == user_id,
                ModCase.created_at >= start,
                ModCase.kind.in_([kind.value for kind in COUNTED_KINDS]),
            )
            .group_by(ModCase.kind)
        )
    ).all()
    return _used_from_rows(rows)


async def _count_used_map(
    session: Any,
    guild_id: int,
    *,
    start: datetime,
    bot_id: int | None,
) -> dict[int, QuotaUsed]:
    clauses = [
        ModCase.guild_id == guild_id,
        ModCase.created_at >= start,
        ModCase.kind.in_([kind.value for kind in COUNTED_KINDS]),
    ]
    if bot_id is not None:
        clauses.append(ModCase.created_by != bot_id)
    rows = (
        await session.execute(
            select(ModCase.created_by, ModCase.kind, func.count())
            .where(*clauses)
            .group_by(ModCase.created_by, ModCase.kind)
        )
    ).all()
    found: dict[int, dict[str, int]] = {}
    for user_id, kind, count in rows:
        bucket = found.setdefault(int(user_id), {"timeouts": 0, "bans": 0})
        group = group_of(kind)
        if group is QuotaGroup.TIMEOUTS:
            bucket["timeouts"] += int(count)
        elif group is QuotaGroup.BANS:
            bucket["bans"] += int(count)
    return {user_id: QuotaUsed(**counts) for user_id, counts in found.items()}


def _used_from_rows(rows: Any) -> QuotaUsed:
    timeouts = 0
    bans = 0
    for kind, count in rows:
        group = group_of(kind)
        if group is QuotaGroup.TIMEOUTS:
            timeouts += int(count)
        elif group is QuotaGroup.BANS:
            bans += int(count)
    return QuotaUsed(timeouts=timeouts, bans=bans)


def _everyone_send(channel: object, guild: object) -> bool | None:
    everyone = getattr(guild, "default_role", None)
    overwrites_for = getattr(channel, "overwrites_for", None)
    if everyone is None or not callable(overwrites_for):
        return None
    overwrite = overwrites_for(everyone)
    return getattr(overwrite, "send_messages", None)
