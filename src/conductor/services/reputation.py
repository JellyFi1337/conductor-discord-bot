"""Счёт репутации: дела, рост, шесть уровней, автобан, помилование."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
import structlog
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import MemberRecord, ModCase, Reputation, ReputationEvent, ReputationSettings, ReputationTier
from conductor.db.session import dialect_insert, session_scope
from conductor.domain.activity import as_utc
from conductor.domain.moderation import CaseCreated, CaseKind, next_case_number
from conductor.domain.reputation import (
    DEFAULT_CONFIG,
    KICK_HIT,
    KIND_ACHIEVEMENT,
    KIND_GROWTH,
    KIND_PARDON,
    KIND_REVOKE,
    START,
    TIER_COUNT,
    AutobanVerdict,
    HitPlan,
    MonthHit,
    PardonResult,
    RepConfig,
    RepDossier,
    RepPanel,
    RepTier,
    ScoreChanged,
    apply_growth,
    apply_hit,
    autoban_verdict,
    case_hit,
    clamp_score,
    default_tiers,
    desired_tier,
    growth_wait,
    is_frozen,
    month_hits,
    next_tier_need,
    pardon_score,
    parse_rep_config,
    parse_tier_names,
    punishing_kind,
    settings_need_defaults,
    tier_for,
)
from conductor.domain.staff_feed import ACTION_PARDON
from conductor.presentation import copy
from conductor.presentation.reputation import autoban_view, cap_cut_view, dry_run_view, would_ban_view
from conductor.services.role_broker import (
    RoleBindOverlap,
    RoleClaim,
    RoleSubsystem,
    log_role_failure,
    report_skipped_roles,
)
from conductor.services.runtime import log_tick, member_on_guild
from conductor.services.staff_feed import actor_rank_of, emit_staff

log = structlog.get_logger("conductor.reputation")

GROWTH_PERIOD = 600.0
TIER_PERIOD = 600.0
TIER_BATCH = 25


class ReputationService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        events = getattr(bot, "events", None)
        subscribe = getattr(events, "subscribe", None)
        if callable(subscribe):
            subscribe(CaseCreated, self._enqueue_case)
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(GROWTH_PERIOD, "rep-growth", self.tick_all)
            every(TIER_PERIOD, "rep-tiers", self.reconcile_all)
        self._score_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._skipped_alerts: set[int] = set()

    def _score_lock(self, guild_id: int, user_id: int) -> asyncio.Lock:
        return self._score_locks.setdefault((int(guild_id), int(user_id)), asyncio.Lock())

    def _enqueue_case(self, event: CaseCreated) -> None:
        spawn = getattr(self._bot, "spawn", None)
        if not callable(spawn):
            return
        spawn(self.apply_case(event), name=f"rep-case-{event.case_id}")

    async def apply_case(self, event: CaseCreated, *, now: datetime | None = None) -> HitPlan | None:
        async with self._score_lock(event.guild_id, event.target_id):
            return await self._apply_case(event, now=now)

    async def _apply_case(self, event: CaseCreated, *, now: datetime | None = None) -> HitPlan | None:
        moment = as_utc(now or datetime.now(UTC))
        appeal = copy.APPEAL_DEFAULT
        banned = False
        previous_tier = 0
        tiers: tuple[RepTier, ...] = ()
        try:
            async with session_scope(self._bot.sessions) as session:
                if await self._event_for_case(session, event.guild_id, event.case_id) is not None:
                    return None
                config, tiers, appeal = await self._load(session, event.guild_id)
                row = await self._ensure_score(session, event.guild_id, event.target_id, config)
                banned = bool(row.banned_for_reputation)
                previous_tier = tier_for(int(row.score), tiers)
                hit = case_hit(event.kind, event.duration_seconds, config)
                used = await self._hour_used(session, event.guild_id, event.target_id, moment)
                plan = apply_hit(score=row.score, hit=hit, hour_used=used, config=config)
                actual = await self._bump_score(
                    session, event.guild_id, event.target_id, -plan.applied, config
                )
                plan = HitPlan(applied=plan.applied, cut=plan.cut, score=actual)
                session.expire(row, ["score"])
                session.add(
                    ReputationEvent(
                        guild_id=event.guild_id,
                        user_id=event.target_id,
                        case_id=event.case_id,
                        kind=event.kind,
                        delta=-plan.applied,
                        cut=plan.cut,
                        score_after=actual,
                        created_at=moment,
                    )
                )
        except IntegrityError:
            log.info("reputation_case_duplicate", guild_id=event.guild_id, case_id=event.case_id)
            return None
        if plan.cut:
            log.info(
                "reputation_hit_capped",
                guild_id=event.guild_id,
                user_id=event.target_id,
                case_id=event.case_id,
                applied=plan.applied,
                cut=plan.cut,
                score=plan.score,
            )
            self._log_admin(event.guild_id, cap_cut_view(event.target_id, plan, dry_run=config.dry_run))
        elif config.dry_run and plan.applied:
            self._log_admin(event.guild_id, dry_run_view(event.target_id, plan))
        await self._sync_tiers(
            event.guild_id,
            event.target_id,
            plan.score,
            config,
            tiers,
            previous_tier=previous_tier,
        )
        self._emit_score(event.guild_id, event.target_id, plan.score, tiers)
        await self._maybe_autoban(
            event.guild_id,
            event.target_id,
            score=plan.score,
            banned=banned,
            config=config,
            punishing=punishing_kind(event.kind),
            appeal=appeal,
            now=moment,
        )
        return plan

    async def dossier(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> RepDossier:
        moment = as_utc(now or datetime.now(UTC))
        created = False
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            existing = await session.scalar(
                select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
            )
            created = existing is None
            row = await self._ensure_score(session, guild_id, user_id, config)
            unused_messages, unused_voice = await self._unused(session, row)
            wait = growth_wait(
                unused_messages=unused_messages,
                unused_voice=unused_voice,
                last_growth_at=row.last_growth_at,
                now=moment,
                config=config,
                score=row.score,
            )
            month = await self._month(session, guild_id, user_id, moment)
            tier = tier_for(row.score, tiers)
            need, nxt = next_tier_need(row.score, tiers)
            name = next((item.name for item in tiers if item.tier == tier), "")
            dossier = RepDossier(
                user_id=user_id,
                score=row.score,
                ceiling=config.ceiling,
                month=month,
                wait=wait,
                dry_run=config.dry_run,
                sinner=is_frozen(tier),
                banned_for_reputation=bool(row.banned_for_reputation),
                tier=tier,
                tier_name=name,
                next_name=None if nxt is None else nxt.name,
                until_next=need,
            )
        if created:
            await self._sync_tiers(guild_id, user_id, dossier.score, config, tiers)
        return dossier

    async def show_config(self, guild_id: int) -> RepPanel:
        async with session_scope(self._bot.sessions) as session:
            config, tiers, appeal = await self._load(session, guild_id)
            return RepPanel(config=config, appeal_contact=appeal, tiers=tiers)

    async def update_config(
        self,
        guild_id: int,
        *,
        warn_hit: int | None = None,
        timeout_per_step: int | None = None,
        timeout_min: int | None = None,
        timeout_max: int | None = None,
        mute_hit: int | None = None,
        kick_hit: int | None = None,
        max_hit_per_hour: int | None = None,
        messages_per_point: int | None = None,
        voice_minutes: int | None = None,
        dry_run: bool | None = None,
        appeal_contact: str | None = None,
        start: int | None = None,
    ) -> RepPanel | None:
        async with session_scope(self._bot.sessions) as session:
            current, tiers, appeal = await self._load(session, guild_id)
            parsed = parse_rep_config(
                current,
                warn_hit=warn_hit,
                timeout_per_step=timeout_per_step,
                timeout_min=timeout_min,
                timeout_max=timeout_max,
                mute_hit=mute_hit,
                kick_hit=kick_hit,
                max_hit_per_hour=max_hit_per_hour,
                messages_per_point=messages_per_point,
                voice_minutes=voice_minutes,
                dry_run=dry_run,
                start=start,
            )
            if parsed is None:
                return None
            row = await session.get(ReputationSettings, guild_id)
            if row is None:
                row = ReputationSettings(guild_id=guild_id)
                session.add(row)
            self._write_settings(row, parsed)
            if appeal_contact is not None:
                cleaned = appeal_contact.strip()
                row.appeal_contact = cleaned or copy.APPEAL_DEFAULT
                appeal = row.appeal_contact
            panel = RepPanel(config=parsed, appeal_contact=appeal, tiers=tiers)
        self._skipped_alerts.discard(int(guild_id))
        self._bind_tiers(guild_id, panel.tiers)
        return panel

    async def set_tier_role(self, guild_id: int, tier: int, role_id: int) -> RepPanel:
        if tier < 0 or tier >= TIER_COUNT:
            raise ValueError(tier)
        wanted = int(role_id)
        async with session_scope(self._bot.sessions) as session:
            _config, tiers, appeal = await self._load(session, guild_id)
            if any(item.tier != tier and item.role_id == wanted for item in tiers):
                raise RoleBindOverlap(wanted, RoleSubsystem.REPUTATION)
            prospective = tuple(
                wanted if item.tier == tier else int(item.role_id)
                for item in tiers
                if item.tier == tier or item.role_id
            )
        self._bind_ids(guild_id, prospective, strict=True)
        async with session_scope(self._bot.sessions) as session:
            config, _tiers, appeal = await self._load(session, guild_id)
            row = await session.scalar(
                select(ReputationTier).where(ReputationTier.guild_id == guild_id, ReputationTier.tier == tier)
            )
            if row is None:
                spec = default_tiers(copy.REP_TIER_DEFAULTS)[tier]
                row = ReputationTier(
                    guild_id=guild_id,
                    tier=tier,
                    lower=spec.lower,
                    upper=spec.upper,
                    name=spec.name,
                )
                session.add(row)
            row.role_id = wanted
            await session.flush()
            tiers = await self._read_tiers(session, guild_id)
            panel = RepPanel(config=config, appeal_contact=appeal, tiers=tiers)
        self._skipped_alerts.discard(int(guild_id))
        self._bind_tiers(guild_id, panel.tiers, strict=True)
        return panel

    async def set_tier_names(self, guild_id: int, raw: str) -> RepPanel | None:
        names = parse_tier_names(raw)
        if names is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            config, _tiers, appeal = await self._load(session, guild_id)
            rows = list(
                (
                    await session.scalars(
                        select(ReputationTier).where(ReputationTier.guild_id == guild_id).order_by(ReputationTier.tier)
                    )
                ).all()
            )
            by_tier = {int(row.tier): row for row in rows}
            for index, name in enumerate(names):
                row = by_tier.get(index)
                if row is None:
                    spec = default_tiers(names)[index]
                    session.add(
                        ReputationTier(
                            guild_id=guild_id,
                            tier=index,
                            lower=spec.lower,
                            upper=spec.upper,
                            name=name,
                        )
                    )
                    continue
                row.name = name
            await session.flush()
            tiers = await self._read_tiers(session, guild_id)
            return RepPanel(config=config, appeal_contact=appeal, tiers=tiers)

    async def defaults_drift(self, guild_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ReputationSettings, guild_id)
            if row is None:
                return False
            return settings_need_defaults(int(row.start), int(row.kick_hit))

    async def apply_new_defaults(self, guild_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ReputationSettings, guild_id)
            if row is None:
                return False
            row.start = START
            row.kick_hit = KICK_HIT
            return True

    async def diagnose_counts(self, guild_id: int) -> tuple[int, int]:
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            rows = list((await session.scalars(select(Reputation).where(Reputation.guild_id == guild_id))).all())
        at_threshold = sum(1 for row in rows if is_frozen(tier_for(int(row.score), tiers)))
        would_ban = sum(1 for row in rows if int(row.score) <= config.floor and not row.banned_for_reputation)
        return at_threshold, would_ban

    async def pardon(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        actor_id: int,
        now: datetime | None = None,
    ) -> PardonResult:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild.id)
            row = await self._ensure_score(session, guild.id, user_id, config)
            own_ban = bool(row.banned_for_reputation)
            previous = int(row.score)
            previous_tier = tier_for(previous, tiers)
            restored = pardon_score(config)
            row.score = restored
            row.banned_for_reputation = False
            session.add(
                ReputationEvent(
                    guild_id=guild.id,
                    user_id=user_id,
                    case_id=None,
                    kind=KIND_PARDON,
                    delta=restored - previous,
                    cut=0,
                    score_after=restored,
                    created_at=moment,
                )
            )
        await self._sync_tiers(guild.id, user_id, restored, config, tiers, previous_tier=previous_tier)
        self._emit_score(guild.id, user_id, restored, tiers)
        unbanned = False
        if own_ban and not config.dry_run and self._moderation_open():
            unbanned = await self._unban(guild, user_id, reason=copy.reason_rep_pardon(actor_id))
        elif own_ban and config.dry_run:
            self._log_admin(guild.id, would_ban_view(user_id, score=restored, pardon=True))
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor_id,
            actor_rank=await actor_rank_of(self._bot, guild, actor_id),
            action=ACTION_PARDON,
            target=user_id,
        )
        return PardonResult(score=restored, unbanned=unbanned, own_ban=own_ban)

    async def refund(self, case_id: int, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        restored = 0
        applied = 0
        guild_id = 0
        user_id = 0
        previous_tier = 0
        tiers: tuple[RepTier, ...] = ()
        config = DEFAULT_CONFIG
        try:
            async with session_scope(self._bot.sessions) as session:
                rows = list(
                    (
                        await session.scalars(select(ReputationEvent).where(ReputationEvent.case_id == int(case_id)))
                    ).all()
                )
                revoke_row = next((row for row in rows if row.kind == KIND_REVOKE), None)
                if revoke_row is not None:
                    return int(revoke_row.delta)
                original = next((row for row in rows if row.kind != KIND_REVOKE), None)
                if original is None:
                    return 0
                applied = abs(int(original.delta))
                guild_id = int(original.guild_id)
                user_id = int(original.user_id)
                config, tiers, _appeal = await self._load(session, guild_id)
                score = await self._ensure_score(session, guild_id, user_id, config)
                previous_tier = tier_for(int(score.score), tiers)
                restored = await self._bump_score(session, guild_id, user_id, applied, config)
                session.expire(score, ["score"])
                session.add(
                    ReputationEvent(
                        guild_id=guild_id,
                        user_id=user_id,
                        case_id=int(case_id),
                        kind=KIND_REVOKE,
                        delta=applied,
                        cut=0,
                        score_after=restored,
                        created_at=moment,
                    )
                )
        except IntegrityError:
            async with session_scope(self._bot.sessions) as session:
                existing = await self._event_for_case(session, guild_id, int(case_id), kind=KIND_REVOKE)
                return 0 if existing is None else int(existing.delta)
        if applied:
            await self._sync_tiers(guild_id, user_id, restored, config, tiers, previous_tier=previous_tier)
            self._emit_score(guild_id, user_id, restored, tiers)
        return applied

    async def adjust(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        *,
        kind: str = KIND_ACHIEVEMENT,
        now: datetime | None = None,
    ) -> int:
        moment = as_utc(now or datetime.now(UTC))
        token = str(kind or KIND_ACHIEVEMENT).strip().lower() or KIND_ACHIEVEMENT
        async with self._score_lock(guild_id, user_id):
            return await self._adjust(guild_id, user_id, delta, token=token, moment=moment)

    async def set_score(
        self,
        guild_id: int,
        user_id: int,
        score: int,
        *,
        actor_id: int | None = None,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """Поставить счёт: разницу выдаёт или забирает. Автобан отсюда не запускается."""
        moment = as_utc(now or datetime.now(UTC))
        async with self._score_lock(int(guild_id), int(user_id)):
            async with session_scope(self._bot.sessions) as session:
                config, _tiers, _appeal = await self._load(session, int(guild_id))
                row = await self._ensure_score(session, int(guild_id), int(user_id), config)
                before = int(row.score)
                wanted = clamp_score(int(score), config)
            after = before
            if wanted != before:
                after = await self._adjust(
                    int(guild_id), int(user_id), wanted - before, token="owner", moment=moment
                )
        log.info(
            "owner_rep",
            guild_id=int(guild_id),
            user_id=int(user_id),
            actor_id=actor_id,
            before=before,
            after=after,
        )
        return before, after

    async def _adjust(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        *,
        token: str,
        moment: datetime,
    ) -> int:
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            row = await self._ensure_score(session, guild_id, user_id, config)
            previous_tier = tier_for(int(row.score), tiers)
            applied = clamp_score(int(row.score) + int(delta), config) - int(row.score)
            score = await self._bump_score(session, guild_id, user_id, applied, config)
            session.expire(row, ["score"])
            session.add(
                ReputationEvent(
                    guild_id=guild_id,
                    user_id=user_id,
                    case_id=None,
                    kind=token[:16],
                    delta=applied,
                    cut=0,
                    score_after=score,
                    created_at=moment,
                )
            )
        await self._sync_tiers(guild_id, user_id, score, config, tiers, previous_tier=previous_tier)
        self._emit_score(guild_id, user_id, score, tiers)
        return score

    async def tick_all(self) -> None:
        now = datetime.now(UTC)
        grown = 0
        for guild in getattr(self._bot, "guilds", ()):
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            try:
                grown += await self.tick_guild(int(guild_id), now=now)
            except Exception:
                log.exception("reputation_growth_failed", guild_id=guild_id)
        log_tick(log, "reputation_growth_tick", changed=grown)

    async def tick_guild(self, guild_id: int, *, now: datetime | None = None) -> int:
        moment = as_utc(now or datetime.now(UTC))
        grown = 0
        changed: list[tuple[int, int, int]] = []
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            rows = list(
                (
                    await session.scalars(
                        select(Reputation).where(
                            Reputation.guild_id == guild_id,
                            Reputation.score < config.ceiling,
                        )
                    )
                ).all()
            )
            for row in rows:
                unused_messages, unused_voice = await self._unused(session, row)
                previous_tier = tier_for(int(row.score), tiers)
                plan = apply_growth(
                    score=row.score,
                    unused_messages=unused_messages,
                    unused_voice=unused_voice,
                    last_growth_at=row.last_growth_at,
                    now=moment,
                    config=config,
                )
                if plan.delta <= 0:
                    continue
                actual = await self._bump_score(session, guild_id, int(row.user_id), plan.delta, config)
                session.expire(row, ["score"])
                row.credited_messages += plan.consume_messages
                row.credited_voice_seconds += plan.consume_voice
                row.last_growth_at = plan.grew_at
                session.add(
                    ReputationEvent(
                        guild_id=guild_id,
                        user_id=row.user_id,
                        case_id=None,
                        kind=KIND_GROWTH,
                        delta=plan.delta,
                        cut=0,
                        score_after=actual,
                        created_at=moment,
                    )
                )
                changed.append((int(row.user_id), actual, previous_tier))
                grown += 1
        for user_id, score, previous_tier in changed:
            await self._sync_tiers(guild_id, user_id, score, config, tiers, previous_tier=previous_tier)
            self._emit_score(guild_id, user_id, score, tiers)
        return grown

    async def bind_tiers(self, guild_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            _config, tiers, _appeal = await self._load(session, guild_id)
        self._skipped_alerts.discard(int(guild_id))
        self._bind_tiers(guild_id, tiers)

    async def bind_sinner(self, guild_id: int) -> None:
        await self.bind_tiers(guild_id)

    async def reconcile_all(self) -> int:
        done = 0
        for guild in getattr(self._bot, "guilds", ()):
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                continue
            try:
                done += await self.reconcile_guild(int(guild_id))
            except Exception:
                log.exception("reputation_tiers_failed", guild_id=guild_id)
        log_tick(log, "reputation_tiers_tick", changed=0, checked=done)
        return done

    async def reconcile_guild(self, guild_id: int) -> int:
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            rows = list((await session.scalars(select(Reputation).where(Reputation.guild_id == guild_id))).all())
            pairs = [(int(row.user_id), int(row.score)) for row in rows]
        done = 0
        for index in range(0, len(pairs), TIER_BATCH):
            chunk = pairs[index : index + TIER_BATCH]
            for user_id, score in chunk:
                await self._sync_tiers(guild_id, user_id, score, config, tiers)
                done += 1
        return done

    async def reconcile_member(self, guild_id: int, user_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            config, tiers, _appeal = await self._load(session, guild_id)
            row = await session.scalar(
                select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
            )
            if row is None:
                return
            score = int(row.score)
        await self._sync_tiers(guild_id, user_id, score, config, tiers)

    async def _maybe_autoban(
        self,
        guild_id: int,
        user_id: int,
        *,
        score: int,
        banned: bool,
        config: RepConfig,
        punishing: bool,
        appeal: str,
        now: datetime,
    ) -> None:
        verdict = autoban_verdict(
            score=score,
            banned_for_reputation=banned,
            dry_run=config.dry_run,
            moderation_open=self._moderation_open(),
            punishing=punishing,
        )
        if verdict is AutobanVerdict.SKIP:
            return
        if verdict is AutobanVerdict.DRY:
            log.info("reputation_autoban_dry", guild_id=guild_id, user_id=user_id, score=score)
            self._log_admin(guild_id, would_ban_view(user_id, score=score, pardon=False))
            return
        if verdict is AutobanVerdict.LOCKED:
            log.info("reputation_autoban_locked", guild_id=guild_id, user_id=user_id)
            return
        guild = self._guild(guild_id)
        if guild is None:
            return
        target = guild.get_member(user_id)
        if target is None:
            target = discord.Object(id=user_id)
        try:
            await self._discord(guild.ban(target, reason=copy.AUTOBAN_REASON, delete_message_seconds=0))
        except Exception as exc:
            log.warning("reputation_autoban_failed", guild_id=guild_id, user_id=user_id, error=str(exc))
            return
        stored = await self._record_autoban(guild_id, user_id, now)
        if stored is None:
            return
        self._emit_case(stored)
        self._log_admin(guild_id, autoban_view(stored, appeal=appeal))

    async def _record_autoban(self, guild_id: int, user_id: int, now: datetime) -> Any:
        actor_id = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0)
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
            )
            if row is not None:
                row.banned_for_reputation = True
            highest = await session.scalar(select(func.max(ModCase.number)).where(ModCase.guild_id == guild_id))
            case = ModCase(
                guild_id=guild_id,
                number=next_case_number(highest),
                owner_id=user_id,
                created_by=actor_id,
                actor_rank=0,
                kind=CaseKind.BAN.value,
                reason=copy.AUTOBAN_REASON,
                duration_seconds=None,
                created_at=now,
            )
            session.add(case)
            await session.flush()
            return CaseCreated(
                guild_id=guild_id,
                case_id=int(case.id),
                number=int(case.number),
                target_id=user_id,
                actor_id=actor_id,
                actor_rank=0,
                kind=CaseKind.BAN.value,
                reason=copy.AUTOBAN_REASON,
                duration_seconds=None,
                created_at=now,
            )

    def _emit_case(self, event: CaseCreated) -> None:
        events = getattr(self._bot, "events", None)
        emit = getattr(events, "emit", None)
        if callable(emit):
            emit(event)

    def _emit_score(self, guild_id: int, user_id: int, score: int, tiers: Sequence[RepTier]) -> None:
        events = getattr(self._bot, "events", None)
        emit = getattr(events, "emit", None)
        if not callable(emit):
            return
        current = tier_for(score, tiers)
        frozen = is_frozen(current)
        emit(
            ScoreChanged(
                guild_id=guild_id,
                user_id=user_id,
                score=score,
                sinner=frozen,
                tier=current,
                frozen=frozen,
            )
        )

    async def _sync_tiers(
        self,
        guild_id: int,
        user_id: int,
        score: int,
        config: RepConfig,
        tiers: Sequence[RepTier],
        *,
        previous_tier: int | None = None,
    ) -> None:
        target = desired_tier(
            score,
            previous_tier if previous_tier is not None else tier_for(score, tiers),
            tiers,
        )
        role_ids = tuple(item.role_id for item in tiers if item.role_id)
        wanted = next((item.role_id for item in tiers if item.tier == target and item.role_id), None)
        add = (wanted,) if wanted else ()
        remove = tuple(role_id for role_id in role_ids if role_id != wanted)
        self._bind_tiers(guild_id, tiers)
        if config.dry_run:
            log.info(
                "reputation_tier_dry",
                guild_id=guild_id,
                user_id=user_id,
                tier=target,
                role_id=wanted,
            )
            return
        if not role_ids:
            return
        if not member_on_guild(self._bot, guild_id, user_id):
            return
        have = set(self._member_role_ids(guild_id, user_id))
        already = (wanted in have if wanted else True) and not (set(remove) & have)
        if already:
            return
        broker = getattr(self._bot, "roles", None)
        apply = getattr(broker, "apply", None)
        if not callable(apply):
            return
        urgent = previous_tier is not None and previous_tier != 0 and target == 0
        claim = RoleClaim(
            guild_id=guild_id,
            user_id=user_id,
            subsystem=RoleSubsystem.REPUTATION,
            add=add,
            remove=remove,
            reason=copy.reason_rep_level(target),
            urgent=urgent,
        )
        result = await apply(claim)
        skipped = tuple(getattr(result, "skipped", ()) or ())
        if skipped:
            report_skipped_roles(
                self._bot,
                guild_id,
                skipped,
                seen=self._skipped_alerts,
                title=copy.REP_ROLE_GONE,
                hint=copy.REP_STALE_ACTION,
            )
        elif not getattr(result, "ok", False):
            log_role_failure(
                log,
                "reputation_tier_role_failed",
                guild_id=guild_id,
                user_id=user_id,
                result=result,
            )

    async def _unban(self, guild: discord.Guild, user_id: int, *, reason: str) -> bool:
        try:
            await self._discord(guild.unban(discord.Object(id=user_id), reason=reason[:512]))
        except Exception as exc:
            log.warning("reputation_unban_failed", guild_id=guild.id, user_id=user_id, error=str(exc))
            return False
        return True

    async def _load(self, session: AsyncSession, guild_id: int) -> tuple[RepConfig, tuple[RepTier, ...], str]:
        row = await session.get(ReputationSettings, guild_id)
        if row is None:
            config = DEFAULT_CONFIG
            appeal = copy.APPEAL_DEFAULT
        else:
            config = RepConfig(
                start=row.start,
                floor=row.floor,
                ceiling=row.ceiling,
                warn_hit=row.warn_hit,
                timeout_step_seconds=row.timeout_step_seconds,
                timeout_per_step=row.timeout_per_step,
                timeout_min=row.timeout_min,
                timeout_max=row.timeout_max,
                mute_hit=row.mute_hit,
                kick_hit=row.kick_hit,
                max_hit_per_hour=row.max_hit_per_hour,
                messages_per_point=row.messages_per_point,
                voice_seconds_per_point=row.voice_seconds_per_point,
                growth_interval=timedelta(seconds=max(1, row.growth_interval_seconds)),
                dry_run=bool(row.dry_run),
            )
            appeal = (row.appeal_contact or "").strip() or copy.APPEAL_DEFAULT
        tiers = await self._ensure_tiers(session, guild_id)
        return config, tiers, appeal

    async def _ensure_tiers(self, session: AsyncSession, guild_id: int) -> tuple[RepTier, ...]:
        specs = default_tiers(copy.REP_TIER_DEFAULTS)
        rows = list(
            (
                await session.scalars(
                    select(ReputationTier).where(ReputationTier.guild_id == guild_id).order_by(ReputationTier.tier)
                )
            ).all()
        )
        by_tier = {int(row.tier): row for row in rows}
        dirty = False
        for spec in specs:
            row = by_tier.get(spec.tier)
            if row is None:
                session.add(
                    ReputationTier(
                        guild_id=guild_id,
                        tier=spec.tier,
                        lower=spec.lower,
                        upper=spec.upper,
                        name=spec.name,
                    )
                )
                dirty = True
                continue
            if int(row.lower) != spec.lower or int(row.upper) != spec.upper:
                row.lower = spec.lower
                row.upper = spec.upper
                dirty = True
        if dirty:
            await session.flush()
        return await self._read_tiers(session, guild_id)

    async def _read_tiers(self, session: AsyncSession, guild_id: int) -> tuple[RepTier, ...]:
        rows = list(
            (
                await session.scalars(
                    select(ReputationTier).where(ReputationTier.guild_id == guild_id).order_by(ReputationTier.tier)
                )
            ).all()
        )
        return tuple(
            RepTier(
                tier=int(row.tier),
                lower=int(row.lower),
                upper=int(row.upper),
                name=str(row.name),
                role_id=None if row.role_id is None else int(row.role_id),
            )
            for row in rows
        )

    def _write_settings(self, row: ReputationSettings, config: RepConfig) -> None:
        row.start = config.start
        row.floor = config.floor
        row.ceiling = config.ceiling
        row.warn_hit = config.warn_hit
        row.timeout_step_seconds = config.timeout_step_seconds
        row.timeout_min = config.timeout_min
        row.timeout_max = config.timeout_max
        row.timeout_per_step = config.timeout_per_step
        row.mute_hit = config.mute_hit
        row.kick_hit = config.kick_hit
        row.max_hit_per_hour = config.max_hit_per_hour
        row.messages_per_point = config.messages_per_point
        row.voice_seconds_per_point = config.voice_seconds_per_point
        row.growth_interval_seconds = int(config.growth_interval.total_seconds())
        row.dry_run = config.dry_run

    async def _ensure_score(
        self,
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        config: RepConfig,
    ) -> Reputation:
        row = await session.scalar(
            select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
        )
        if row is not None:
            return row
        messages, voice = await self._activity(session, guild_id, user_id)
        await session.execute(
            dialect_insert(session)(Reputation)
            .values(
                guild_id=int(guild_id),
                user_id=int(user_id),
                score=config.start,
                credited_messages=messages,
                credited_voice_seconds=voice,
                banned_for_reputation=False,
            )
            .on_conflict_do_nothing(index_elements=("guild_id", "user_id"))
        )
        row = await session.scalar(
            select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
        )
        assert row is not None
        return row

    async def _bump_score(
        self,
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        delta: int,
        config: RepConfig,
    ) -> int:
        low = min(int(config.floor), int(config.ceiling))
        high = max(int(config.floor), int(config.ceiling))
        raw = Reputation.score + int(delta)
        clamped = case((raw < low, low), (raw > high, high), else_=raw)
        result = await session.execute(
            update(Reputation)
            .where(Reputation.guild_id == int(guild_id), Reputation.user_id == int(user_id))
            .values(score=clamped)
            .returning(Reputation.score)
        )
        found = result.scalar_one_or_none()
        if found is None:
            await self._ensure_score(session, guild_id, user_id, config)
            result = await session.execute(
                update(Reputation)
                .where(Reputation.guild_id == int(guild_id), Reputation.user_id == int(user_id))
                .values(score=clamped)
                .returning(Reputation.score)
            )
            found = result.scalar_one()
        return int(found)

    async def _activity(self, session: AsyncSession, guild_id: int, user_id: int) -> tuple[int, int]:
        member = await session.scalar(
            select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
        )
        if member is None:
            return 0, 0
        return int(member.message_count or 0), int(member.voice_seconds or 0)

    async def _unused(self, session: AsyncSession, row: Reputation) -> tuple[int, int]:
        messages, voice = await self._activity(session, row.guild_id, row.user_id)
        return max(0, messages - row.credited_messages), max(0, voice - row.credited_voice_seconds)

    async def _hour_used(self, session: AsyncSession, guild_id: int, user_id: int, now: datetime) -> int:
        start = now - timedelta(hours=1)
        rows = (
            await session.scalars(
                select(ReputationEvent).where(
                    ReputationEvent.guild_id == guild_id,
                    ReputationEvent.user_id == user_id,
                    ReputationEvent.created_at >= start,
                    ReputationEvent.delta < 0,
                )
            )
        ).all()
        return sum(-int(row.delta) for row in rows)

    async def _month(
        self,
        session: AsyncSession,
        guild_id: int,
        user_id: int,
        now: datetime,
    ) -> tuple[MonthHit, ...]:
        start = now - timedelta(days=30)
        revoked = select(ModCase.id).where(ModCase.guild_id == guild_id, ModCase.revoked_at.is_not(None))
        rows = (
            await session.scalars(
                select(ReputationEvent).where(
                    ReputationEvent.guild_id == guild_id,
                    ReputationEvent.user_id == user_id,
                    ReputationEvent.created_at >= start,
                    ReputationEvent.delta < 0,
                    (ReputationEvent.case_id.is_(None)) | (ReputationEvent.case_id.notin_(revoked)),
                )
            )
        ).all()
        return month_hits((row.kind, int(row.delta)) for row in rows)

    async def _event_for_case(
        self,
        session: AsyncSession,
        guild_id: int,
        case_id: int,
        *,
        kind: str | None = None,
    ) -> ReputationEvent | None:
        clauses = [
            ReputationEvent.guild_id == guild_id,
            ReputationEvent.case_id == case_id,
        ]
        if kind is None:
            clauses.append(ReputationEvent.kind != KIND_REVOKE)
        else:
            clauses.append(ReputationEvent.kind == kind)
        return await session.scalar(select(ReputationEvent).where(*clauses))

    def _bind_tiers(self, guild_id: int, tiers: Sequence[RepTier], *, strict: bool = False) -> None:
        self._bind_ids(guild_id, tuple(item.role_id for item in tiers if item.role_id), strict=strict)

    def _bind_ids(self, guild_id: int, role_ids: Sequence[int], *, strict: bool = False) -> None:
        broker = getattr(self._bot, "roles", None)
        bind = getattr(broker, "bind", None)
        if not callable(bind):
            return
        bind(guild_id, RoleSubsystem.REPUTATION, tuple(int(role_id) for role_id in role_ids if role_id), strict=strict)

    def _member_role_ids(self, guild_id: int, user_id: int) -> tuple[int, ...]:
        guild = self._guild(guild_id)
        if guild is None:
            return ()
        member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
        if member is None:
            return ()
        return tuple(int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None))

    def _moderation_open(self) -> bool:
        return True

    def _guild(self, guild_id: int) -> Any:
        getter = getattr(self._bot, "get_guild", None)
        if not callable(getter):
            return None
        return getter(guild_id)

    def _log_admin(self, guild_id: int, view: Any) -> None:
        pump = getattr(self._bot, "log_pump", None)
        submit = getattr(pump, "submit", None)
        configs = getattr(self._bot, "config_by_guild", None)
        config = None if configs is None else configs.get(guild_id)
        channel_id = None if config is None else getattr(config, "admin_log_channel_id", None)
        if not callable(submit) or channel_id is None:
            return
        submit(channel_id, view=view)

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable
