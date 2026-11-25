"""Канал доказательств и ЛС о наказании."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from time import time
from types import SimpleNamespace
from typing import Any

import discord
import structlog
from sqlalchemy import select, update

from conductor.db.models import GuildLayout, ModCase, ModEvidence, ModSettings
from conductor.db.session import session_scope
from conductor.domain.access import Rank
from conductor.domain.create_lock import BUCKET_CHANNEL, CreateHold
from conductor.domain.moderation import (
    EVIDENCE_REMIND_HOURS_DEFAULT,
    EVIDENCE_REPAIR_INTERVAL,
    EvidenceDenial,
    StoredCase,
    clamp_remind_hours,
    dm_deliverable,
    evidence_channel_name,
    evidence_escalate_at,
    evidence_remind_at,
    needs_evidence,
    sanitize_detail_reason,
)
from conductor.domain.observe import should_emit
from conductor.domain.outcome import Teardown
from conductor.domain.seed import SeedMode
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import panel, send_card, upload_cards, upload_limit
from conductor.presentation.mentions import going_mentions, role_mentions
from conductor.presentation.moderation import case_dm_view, evidence_channel_view, evidence_denial_card
from conductor.presentation.tokens import Tone
from conductor.services.access import cached_snapshot
from conductor.services.create_lock import continue_after_hold, hold_for, log_refused, note_success, trip
from conductor.services.runtime import log_tick
from conductor.services.structure import note_self_deleted
from conductor.services.traffic import lane
from conductor.services.transcripts import collect_transcript, transcript_stamp

log = structlog.get_logger("conductor.evidence")

_CREATE_REASON = copy.REASON_EVIDENCE
_SWEEP_SECONDS = 600
_PENDING_AGE = timedelta(days=7)
_PENDING_CREATE_CAP = 3
_PENDING_BACKOFF = (600.0, 3600.0, 21600.0)
_NON_HOLD_DENIALS = frozenset(
    {
        EvidenceDenial.NO_CATEGORY,
        EvidenceDenial.CATEGORY_GONE,
        EvidenceDenial.NOT_A_CATEGORY,
        EvidenceDenial.BOT_PERMS,
        EvidenceDenial.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    ok: bool
    error: str | None = None
    dm1_ok: bool = False
    dm2_ok: bool = False
    denial: EvidenceDenial | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _Settings:
    category_id: int | None
    archive_id: int | None
    remind_hours: int


@dataclass(frozen=True, slots=True)
class _OpenRow:
    id: int
    guild_id: int
    case_id: int
    channel_id: int | None
    dm1_ok: bool
    created_at: datetime
    sent_at: datetime | None = None
    closed_by: int | None = None


class EvidenceService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._category_repaired: dict[int, datetime] = {}
        self._structure_denial_at: dict[tuple[int, str], datetime] = {}
        self._pending_backoff: dict[int, tuple[float, int]] = {}
        every = getattr(getattr(bot, "scheduler", None), "every", None)
        if callable(every):
            every(_SWEEP_SECONDS, "evidence-sweep", self.sweep)

    async def health(self, guild_id: int, *, guild: object | None = None) -> tuple[EvidenceDenial | None, int, int]:
        """(причина или None, открытых доказательств, просроченных)."""
        live = guild if guild is not None else self._guild(guild_id)
        settings = await self._settings(int(guild_id))
        denial = self._category_denial(live, settings.category_id)
        opened, overdue = await self._open_counts(int(guild_id))
        return denial, opened, overdue

    async def archive_status(self, guild_id: int, *, guild: object | None = None) -> str:
        settings = await self._settings(int(guild_id))
        if settings.archive_id is None:
            return "off"
        live = guild if guild is not None else self._guild(guild_id)
        if live is None:
            return "ok"
        getter = getattr(live, "get_channel", None)
        if not callable(getter):
            return "ok"
        return "ok" if getter(int(settings.archive_id)) is not None else "gone"

    async def deliver_dm1(
        self,
        guild: discord.Guild,
        case: StoredCase,
        target: object,
        *,
        force: bool = False,
    ) -> bool:
        if force:
            if not dm_deliverable(case.kind):
                return False
        elif not needs_evidence(case.kind, self._actor_is_bot(case.created_by)):
            return False
        contact = None
        if not dm_deliverable(case.kind):
            show = getattr(getattr(self._bot, "reputation", None), "show_config", None)
            if callable(show):
                panel_cfg = await show(guild.id)
                contact = str(getattr(panel_cfg, "appeal_contact", "") or "") or None
        return await self._send_dm(target, self._dm1_view(case, appeal_contact=contact))

    async def open_for_case(
        self,
        guild: discord.Guild,
        case: StoredCase,
        actor: object,
        target: object,
        *,
        dm1_ok: bool | None = None,
        now: datetime | None = None,
    ) -> EvidenceResult:
        if not needs_evidence(case.kind, self._actor_is_bot(case.created_by)):
            return EvidenceResult(ok=True)
        delivered = dm1_ok if dm1_ok is not None else await self.deliver_dm1(guild, case, target)
        return await self.open_channel(guild, case, actor, target, dm1_ok=delivered, now=now)

    async def open_channel(
        self,
        guild: discord.Guild,
        case: StoredCase,
        actor: object,
        target: object,
        *,
        dm1_ok: bool,
        now: datetime | None = None,
    ) -> EvidenceResult:
        if not needs_evidence(case.kind, self._actor_is_bot(case.created_by)):
            return EvidenceResult(ok=True, dm1_ok=dm1_ok)
        moment = now or datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            existing = await session.scalar(select(ModEvidence).where(ModEvidence.case_id == case.id))
            if existing is None:
                row = ModEvidence(
                    guild_id=guild.id,
                    case_id=case.id,
                    channel_id=None,
                    created_at=moment,
                    dm1_ok=dm1_ok,
                )
                session.add(row)
                await session.flush()
                evidence_id = int(row.id)
            else:
                existing.dm1_ok = dm1_ok
                evidence_id = int(existing.id)
        channel_id: int | None = None
        channel, denial, hold = await self._create_channel(guild, case, actor, now=moment)
        if channel is not None:
            channel_id = int(channel.id)
            await self._post_card(channel, case, actor, target, evidence_id=evidence_id, dm1_ok=dm1_ok)
        elif denial is not None:
            await self._note_denial(guild, actor, case, denial)
        async with session_scope(self._bot.sessions) as session:
            stored = await session.get(ModEvidence, evidence_id)
            if stored is not None:
                stored.channel_id = channel_id
                stored.dm1_ok = dm1_ok
        if denial is not None:
            if denial is EvidenceDenial.HELD:
                remaining = 0.0 if hold is None else hold.remaining(time())
                error = copy.evidence_held_saved(remaining)
            else:
                error = copy.evidence_denial_text(denial.value)
            return EvidenceResult(
                ok=False,
                error=error,
                dm1_ok=dm1_ok,
                denial=denial,
            )
        return EvidenceResult(ok=True, dm1_ok=dm1_ok)

    async def send_reason(
        self,
        guild: discord.Guild,
        evidence_id: int,
        actor: object,
        raw: str,
        *,
        now: datetime | None = None,
    ) -> tuple[EvidenceResult, Teardown | None]:
        cleaned = sanitize_detail_reason(raw)
        if cleaned is None:
            return EvidenceResult(ok=False, error=copy.EVIDENCE_REASON_SHORT), None
        return await self._finish_close(guild, evidence_id, actor, detail=cleaned, send_dm=True, now=now)

    async def close_without_dm(
        self,
        guild: discord.Guild,
        evidence_id: int,
        actor: object,
        *,
        now: datetime | None = None,
    ) -> tuple[EvidenceResult, Teardown | None]:
        return await self._finish_close(guild, evidence_id, actor, detail=None, send_dm=False, now=now)

    async def _finish_close(
        self,
        guild: discord.Guild,
        evidence_id: int,
        actor: object,
        *,
        detail: str | None,
        send_dm: bool,
        now: datetime | None,
    ) -> tuple[EvidenceResult, Teardown | None]:
        packed = await self._load(guild.id, evidence_id)
        if packed is None:
            return EvidenceResult(ok=False, error=copy.STALE_BODY), None
        row, case = packed
        if row.sent_at is not None or row.closed_by is not None:
            return EvidenceResult(ok=False, error=copy.EVIDENCE_ALREADY_CLOSED), None
        moment = now or datetime.now(UTC)
        actor_id = int(getattr(actor, "id", 0) or 0)
        if not await self._claim_close(row.id, actor_id, moment, detail=detail):
            return EvidenceResult(ok=False, error=copy.EVIDENCE_ALREADY_CLOSED), None
        try:
            transcript, archived = await self._prepare_close(guild, row, case, detail, closed_at=moment)
        except discord.HTTPException:
            await self._release_close(row.id)
            return EvidenceResult(ok=False, error=copy.EVIDENCE_CLOSE_FAILED), None
        dm2_ok = False
        if send_dm and dm_deliverable(case.kind) and detail is not None:
            target = guild.get_member(case.owner_id)
            dm2_ok = await self._send_dm(target, self._dm2_view(case, detail))
        async with session_scope(self._bot.sessions) as session:
            stored = await session.get(ModEvidence, row.id)
            if stored is not None:
                stored.dm2_ok = dm2_ok
                stored.transcript = transcript
        if send_dm:
            summary = copy.evidence_reason_sent(dm_ok=dm2_ok, archived=archived)
        else:
            summary = copy.evidence_closed_sent(archived=archived)
        return (
            EvidenceResult(ok=True, dm1_ok=row.dm1_ok, dm2_ok=dm2_ok, detail=summary),
            self._channel_teardown(guild, row, case),
        )

    async def guard_resource(self, guild_id: int, ref: int | str) -> GuardResource | None:
        return await self._guard(guild_id, ref, owner=True)

    async def guard_close_resource(self, guild_id: int, ref: int | str) -> GuardResource | None:
        return await self._guard(guild_id, ref, owner=False)

    async def sweep(self, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(ModEvidence).where(
                        ModEvidence.channel_id.is_not(None),
                        ModEvidence.sent_at.is_(None),
                        ModEvidence.closed_by.is_(None),
                    )
                )
            ).all()
            found = [
                (
                    int(row.id),
                    int(row.guild_id),
                    int(row.channel_id or 0),
                    row.created_at,
                    row.reminded_at,
                    row.escalated_at,
                )
                for row in rows
            ]
        changed = 0
        for evidence_id, guild_id, channel_id, created_at, reminded_at, escalated_at in found:
            changed += await self._sweep_one(
                evidence_id,
                guild_id,
                channel_id,
                created_at,
                reminded_at,
                escalated_at,
                now=moment,
            )
        changed += await self._sweep_pending(now=moment)
        log_tick(log, "evidence_sweep", changed=changed)

    async def _sweep_pending(self, *, now: datetime) -> int:
        cutoff = now - _PENDING_AGE
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(ModEvidence).where(
                        ModEvidence.channel_id.is_(None),
                        ModEvidence.sent_at.is_(None),
                        ModEvidence.closed_by.is_(None),
                        ModEvidence.created_at >= cutoff,
                    )
                )
            ).all()
            pending = [(int(row.id), int(row.guild_id), int(row.case_id), int(row.dm1_ok or 0)) for row in rows]
        created_by_guild: dict[int, int] = {}
        opened_n = 0
        for evidence_id, guild_id, case_id, dm1_ok in pending:
            if created_by_guild.get(guild_id, 0) >= _PENDING_CREATE_CAP:
                continue
            opened = await self._open_pending(
                evidence_id,
                guild_id,
                case_id,
                bool(dm1_ok),
                now=now,
            )
            if opened is None:
                continue
            created_by_guild[guild_id] = created_by_guild.get(guild_id, 0) + 1
            if opened:
                opened_n += 1
        return opened_n

    async def _open_pending(
        self,
        evidence_id: int,
        guild_id: int,
        case_id: int,
        dm1_ok: bool,
        *,
        now: datetime,
    ) -> bool | None:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        armed = self._pending_backoff.get(int(evidence_id))
        if armed is not None and now.timestamp() < armed[0]:
            return None
        async with session_scope(self._bot.sessions) as session:
            case_row = await session.get(ModCase, case_id)
            if case_row is None:
                return None
            case = _case_from_row(case_row)
        if case.revoked_at is not None:
            return None
        if not needs_evidence(case.kind, self._actor_is_bot(case.created_by)):
            return None
        actor = _person(guild, case.created_by)
        target = _person(guild, case.owner_id)
        channel, denial, _hold = await self._create_channel(guild, case, actor, now=now)
        if channel is None or denial is not None:
            if denial in _NON_HOLD_DENIALS or denial is None:
                self._arm_pending_backoff(evidence_id, now)
            return False
        self._pending_backoff.pop(int(evidence_id), None)
        await self._post_card(channel, case, actor, target, evidence_id=evidence_id, dm1_ok=dm1_ok)
        async with session_scope(self._bot.sessions) as session:
            stored = await session.get(ModEvidence, evidence_id)
            if stored is not None:
                stored.channel_id = int(channel.id)
        await self._notify_channel_ready(guild, actor, channel)
        return True

    def _arm_pending_backoff(self, evidence_id: int, now: datetime) -> None:
        previous = self._pending_backoff.get(int(evidence_id))
        stage = 0 if previous is None else min(int(previous[1]) + 1, len(_PENDING_BACKOFF) - 1)
        self._pending_backoff[int(evidence_id)] = (now.timestamp() + _PENDING_BACKOFF[stage], stage)

    async def _notify_channel_ready(self, guild: discord.Guild, actor: object, channel: object) -> None:
        mention = f"<#{getattr(channel, 'id', 0)}>"
        view = panel(copy.DIAGNOSE_FEATURE_EVIDENCE, copy.evidence_channel_ready(mention), tone=Tone.OK)
        await self._send_dm(actor, view)
        self._post_structure(guild, view)

    async def _sweep_one(
        self,
        evidence_id: int,
        guild_id: int,
        channel_id: int,
        created_at: datetime,
        reminded_at: datetime | None,
        escalated_at: datetime | None,
        *,
        now: datetime,
    ) -> int:
        settings = await self._settings(guild_id)
        hours = settings.remind_hours
        guild = self._guild(guild_id)
        if guild is None:
            return 0
        channel = guild.get_channel(channel_id)
        if channel is None:
            await self._claim_close(evidence_id, 0, now, detail=None)
            return 1
        packed = await self._load_open(guild_id, evidence_id)
        if packed is None:
            return 0
        _row, case = packed
        if escalated_at is None and now >= evidence_escalate_at(created_at, hours):
            snapshot = await cached_snapshot(self._bot, guild_id)
            role_ids = snapshot.rank_roles.get(Rank.CHIEF, ())
            role_id = int(role_ids[0]) if role_ids else 0
            mentions = role_mentions(role_id or None)
            body = copy.EVIDENCE_ESCALATE_TITLE
            if role_id:
                body = copy.evidence_escalate_body(role_id)
            await self._post(channel, panel(copy.EVIDENCE_ESCALATE_TITLE, body, tone=Tone.DANGER), mentions)
            await self._mark(evidence_id, reminded=now if reminded_at is None else None, escalated=now)
            return 1
        if reminded_at is None and now >= evidence_remind_at(created_at, hours):
            mentions = going_mentions((int(case.created_by),))
            await self._post(
                channel,
                panel(copy.EVIDENCE_REMIND_TITLE, copy.evidence_remind_body(case.created_by), tone=Tone.WARN),
                mentions,
            )
            await self._mark(evidence_id, reminded=now)
            return 1
        return 0

    async def _mark(
        self,
        evidence_id: int,
        *,
        reminded: datetime | None = None,
        escalated: datetime | None = None,
    ) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModEvidence, evidence_id)
            if row is None:
                return
            if reminded is not None:
                row.reminded_at = reminded
            if escalated is not None:
                row.escalated_at = escalated

    async def _guard(self, guild_id: int, ref: int | str, *, owner: bool) -> GuardResource | None:
        try:
            evidence_id = int(ref)
        except (TypeError, ValueError):
            return None
        packed = await self._load_open(guild_id, evidence_id)
        if packed is None:
            return None
        row, case = packed
        return GuardResource(
            ref=str(row.id),
            guild_id=row.guild_id,
            owner_id=case.created_by if owner else None,
            closed=False,
            target_id=case.owner_id,
            channel_id=row.channel_id,
        )

    async def _load(self, guild_id: int, evidence_id: int) -> tuple[_OpenRow, StoredCase] | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModEvidence, evidence_id)
            if row is None or int(row.guild_id) != int(guild_id):
                return None
            case = await session.get(ModCase, row.case_id)
            if case is None:
                return None
            opened = _OpenRow(
                id=int(row.id),
                guild_id=int(row.guild_id),
                case_id=int(row.case_id),
                channel_id=None if row.channel_id is None else int(row.channel_id),
                dm1_ok=bool(row.dm1_ok),
                created_at=row.created_at,
                sent_at=row.sent_at,
                closed_by=None if row.closed_by is None else int(row.closed_by),
            )
            stored = StoredCase(
                id=case.id,
                guild_id=case.guild_id,
                number=case.number,
                owner_id=case.owner_id,
                created_by=case.created_by,
                actor_rank=case.actor_rank,
                kind=case.kind,
                reason=case.reason,
                duration_seconds=case.duration_seconds,
                created_at=case.created_at,
            )
            return opened, stored

    async def _load_open(self, guild_id: int, evidence_id: int) -> tuple[_OpenRow, StoredCase] | None:
        packed = await self._load(guild_id, evidence_id)
        if packed is None:
            return None
        row, case = packed
        if row.sent_at is not None or row.closed_by is not None:
            return None
        return packed

    async def _settings(self, guild_id: int) -> _Settings:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            if row is None:
                return _Settings(None, None, EVIDENCE_REMIND_HOURS_DEFAULT)
            hours = int(row.evidence_remind_hours or EVIDENCE_REMIND_HOURS_DEFAULT)
            return _Settings(
                None if row.evidence_category_id is None else int(row.evidence_category_id),
                None if row.evidence_archive_channel_id is None else int(row.evidence_archive_channel_id),
                hours,
            )

    async def set_remind_hours(self, guild_id: int, hours: int) -> int | None:
        parsed = clamp_remind_hours(hours)
        if parsed is None:
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, guild_id)
            if row is None:
                session.add(ModSettings(guild_id=guild_id, evidence_remind_hours=parsed))
            else:
                row.evidence_remind_hours = parsed
        return parsed

    async def repair_category(self, guild: discord.Guild, actor_id: int) -> EvidenceDenial | None:
        await self._repair_cat_evidence(guild, actor_id=int(actor_id))
        seed = getattr(self._bot, "seed", None)
        reapply = getattr(seed, "reapply_rights", None)
        if callable(reapply):
            await reapply(guild, actor_id=int(actor_id), keys=frozenset({"cat_evidence"}))
        denial, _opened, _overdue = await self.health(int(guild.id), guild=guild)
        return denial

    async def _create_channel(
        self,
        guild: discord.Guild,
        case: StoredCase,
        actor: object,
        *,
        now: datetime | None = None,
    ) -> tuple[Any | None, EvidenceDenial | None, CreateHold | None]:
        category, denial = await self._ensure_category(guild, now=now)
        if category is None:
            return None, denial or EvidenceDenial.NO_CATEGORY, None
        perms = _bot_category_perms(guild, category)
        if perms is not None:
            return None, perms, None
        create = getattr(guild, "create_text_channel", None)
        if not callable(create):
            return None, EvidenceDenial.FAILED, None
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _overwrites(guild, actor, snapshot)
        name = evidence_channel_name(case.number, _username(guild, case.owner_id))
        hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
        if hold is not None:
            log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            continue_after_hold(
                self._bot,
                ("evidence-create-defer", guild.id, int(case.id)),
                hold,
                lambda: self._retry_channel(guild, case, actor),
            )
            return None, EvidenceDenial.HELD, hold
        try:
            with lane(Lane.USER):
                created = await self._discord(
                    create(
                        name,
                        category=category,
                        overwrites=overwrites,
                        reason=copy.actor_reason(getattr(actor, "id", None), _CREATE_REASON),
                    )
                )
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
                return None, EvidenceDenial.HELD, hold
            return None, EvidenceDenial.FAILED, None
        if created is None:
            return None, EvidenceDenial.FAILED, None
        note_success(self._bot, guild.id, BUCKET_CHANNEL)
        return created, None, None

    async def _retry_channel(self, guild: discord.Guild, case: StoredCase, actor: object) -> None:
        channel, _denial, _hold = await self._create_channel(guild, case, actor)
        if channel is None:
            return
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(select(ModEvidence).where(ModEvidence.case_id == case.id))
            if row is None:
                return
            row.channel_id = int(channel.id)
            evidence_id = int(row.id)
            dm1_ok = bool(row.dm1_ok)
        target = guild.get_member(int(case.owner_id))
        await self._post_card(channel, case, actor, target, evidence_id=evidence_id, dm1_ok=dm1_ok)

    async def _ensure_category(
        self, guild: discord.Guild, *, now: datetime | None = None
    ) -> tuple[Any | None, EvidenceDenial | None]:
        moment = now or datetime.now(UTC)
        guild_id = int(guild.id)
        settings = await self._settings(guild_id)
        category, denial = _inspect_category(guild, settings.category_id)
        if category is not None:
            return category, None
        if denial is EvidenceDenial.NOT_A_CATEGORY:
            return None, denial
        layout_id = await self._layout_id(guild_id, "cat_evidence")
        if layout_id is not None and layout_id != settings.category_id:
            category, layout_denial = _inspect_category(guild, layout_id)
            if category is not None:
                await self._bind_category(guild_id, int(layout_id))
                return category, None
            if layout_denial is EvidenceDenial.NOT_A_CATEGORY:
                return None, layout_denial
        if should_emit(self._category_repaired.get(guild_id), now=moment, interval=EVIDENCE_REPAIR_INTERVAL):
            self._category_repaired[guild_id] = moment
            await self._repair_cat_evidence(guild)
            settings = await self._settings(guild_id)
            category, denial = _inspect_category(guild, settings.category_id)
            if category is not None:
                return category, None
            if denial is EvidenceDenial.NOT_A_CATEGORY:
                return None, denial
            layout_id = await self._layout_id(guild_id, "cat_evidence")
            if layout_id is not None:
                category, layout_denial = _inspect_category(guild, layout_id)
                if category is not None:
                    await self._bind_category(guild_id, int(layout_id))
                    return category, None
                if layout_denial is EvidenceDenial.NOT_A_CATEGORY:
                    return None, layout_denial
        if denial is not None:
            return None, denial
        return None, EvidenceDenial.NO_CATEGORY

    async def _layout_id(self, guild_id: int, key: str) -> int | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(GuildLayout).where(GuildLayout.guild_id == int(guild_id), GuildLayout.key == key)
            )
        return None if row is None else int(row.channel_id)

    async def _bind_category(self, guild_id: int, category_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModSettings, int(guild_id))
            if row is None:
                session.add(ModSettings(guild_id=int(guild_id), evidence_category_id=int(category_id)))
            else:
                row.evidence_category_id = int(category_id)

    async def _repair_cat_evidence(self, guild: discord.Guild, *, actor_id: int | None = None) -> None:
        seed = getattr(self._bot, "seed", None)
        repair = getattr(seed, "repair", None)
        if not callable(repair):
            return
        user_id = actor_id
        if user_id is None:
            user_id = int(getattr(getattr(self._bot, "user", None), "id", 0) or 0)
        await repair(guild, "cat_evidence", actor_id=int(user_id), mode=SeedMode.BUILD)

    async def _note_denial(
        self,
        guild: discord.Guild,
        actor: object,
        case: StoredCase,
        denial: EvidenceDenial,
    ) -> None:
        log.warning("evidence_denied", guild_id=guild.id, case_id=case.id, denial=denial.value)
        view = panel(
            copy.DIAGNOSE_FEATURE_EVIDENCE,
            copy.evidence_denial_text(denial.value),
            tone=Tone.WARN,
        )
        await self._send_dm(actor, view)
        now = datetime.now(UTC)
        structure_key = (int(guild.id), denial.value)
        if not should_emit(self._structure_denial_at.get(structure_key), now=now, interval=EVIDENCE_REPAIR_INTERVAL):
            return
        self._post_structure(guild, evidence_denial_card(denial.value))
        self._structure_denial_at[structure_key] = now

    def _post_structure(self, guild: discord.Guild, view: object) -> None:
        submit = getattr(getattr(self._bot, "log_pump", None), "submit", None)
        if not callable(submit):
            return
        channel_id = self._structure_channel_id(guild.id)
        if channel_id is None:
            return
        submit(channel_id, view=view)

    def _structure_channel_id(self, guild_id: int) -> int | None:
        configs = getattr(self._bot, "config_by_guild", None)
        config = configs.get(int(guild_id)) if configs is not None else None
        if config is None:
            return None
        raw = getattr(config, "structure_channel_id", None) or getattr(config, "admin_log_channel_id", None)
        return None if raw is None else int(raw)

    def _category_denial(self, guild: object | None, category_id: int | None) -> EvidenceDenial | None:
        category, denial = _inspect_category(guild, category_id)
        if denial is not None:
            return denial
        return _bot_category_perms(guild, category)

    async def _open_counts(self, guild_id: int, *, now: datetime | None = None) -> tuple[int, int]:
        moment = now or datetime.now(UTC)
        settings = await self._settings(int(guild_id))
        hours = settings.remind_hours
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(ModEvidence).where(
                        ModEvidence.guild_id == int(guild_id),
                        ModEvidence.sent_at.is_(None),
                        ModEvidence.closed_by.is_(None),
                    )
                )
            ).all()
            stamps = tuple(row.created_at for row in rows)
        opened = len(stamps)
        overdue = sum(1 for created_at in stamps if moment >= evidence_escalate_at(created_at, hours))
        return opened, overdue

    async def _post_card(
        self,
        channel: object,
        case: StoredCase,
        actor: object,
        target: object,
        *,
        evidence_id: int,
        dm1_ok: bool,
    ) -> None:
        duration = None if not case.duration_seconds else copy.format_mod_duration(case.duration_seconds)
        view = evidence_channel_view(
            case,
            evidence_id=evidence_id,
            actor=_mention(actor, case.created_by),
            target=_mention(target, case.owner_id),
            duration=duration,
            dm1_ok=dm1_ok,
            expect_detail=dm_deliverable(case.kind),
        )
        await self._post(channel, view, discord.AllowedMentions.none())

    async def _prepare_close(
        self,
        guild: discord.Guild,
        row: ModEvidence | _OpenRow,
        case: StoredCase,
        detail: str | None,
        *,
        closed_at: datetime,
    ) -> tuple[str, bool]:
        channel = None if row.channel_id is None else guild.get_channel(int(row.channel_id))
        header = copy.evidence_transcript_header(
            number=case.number,
            kind=case.kind,
            target=_person_label(guild, case.owner_id),
            actor=_person_label(guild, case.created_by),
            opened=transcript_stamp(case.created_at or row.created_at),
            closed=transcript_stamp(closed_at),
            reason=case.reason,
            detail=detail,
        )
        text, _count = await collect_transcript(channel, header=header)
        archive_id = (await self._settings(guild.id)).archive_id
        archive = None if archive_id is None else guild.get_channel(int(archive_id))
        archived = archive is not None
        if archive is not None and not _is_category(archive) and hasattr(archive, "send"):
            body = detail or case.reason
            cards = upload_cards(
                copy.EVIDENCE_TRANSCRIPT_TITLE,
                copy.mod_case_title(case.number, case.kind),
                filename=copy.evidence_transcript_name(case.number),
                data=text.encode("utf-8"),
                limit=upload_limit(guild),
                tone=Tone.MUTED,
                fields={
                    copy.MOD_FIELD_REASON: body,
                    copy.TICKET_FIELD_OPENED: _archive_when(case.created_at or row.created_at),
                    copy.TICKET_FIELD_CLOSED: _archive_when(closed_at),
                },
                timeout=None,
            )
            for card in cards:
                await self._post(archive, card, discord.AllowedMentions.none(), required=True)
        return text, archived

    def _channel_teardown(self, guild: discord.Guild, row: ModEvidence, case: StoredCase) -> Teardown:
        async def teardown() -> None:
            try:
                await self._teardown_channel(guild, row, case)
            except Exception:
                log.exception("evidence_teardown_failed", evidence_id=row.id)

        return teardown

    async def _teardown_channel(
        self,
        guild: discord.Guild,
        row: ModEvidence,
        case: StoredCase,
    ) -> None:
        del case
        channel = None if row.channel_id is None else guild.get_channel(int(row.channel_id))
        archive_id = (await self._settings(guild.id)).archive_id
        archive = None if archive_id is None else guild.get_channel(int(archive_id))
        if archive is not None and _is_category(archive):
            await self._move_to_archive(channel, archive)
            return
        if channel is not None:
            delete = getattr(channel, "delete", None)
            if callable(delete):
                try:
                    note_self_deleted(self._bot, int(guild.id), int(row.channel_id or channel.id))
                    await self._discord(delete(reason=_CREATE_REASON))
                except discord.HTTPException as exc:
                    log.warning("evidence_delete_failed", channel_id=row.channel_id, error=str(exc))
                    raise

    async def _move_to_archive(self, channel: object | None, archive: object) -> None:
        if channel is None:
            return
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            return
        try:
            posted = edit(category=archive, reason=_CREATE_REASON)
            if isawaitable(posted):
                await self._discord(posted)
        except discord.HTTPException as exc:
            log.warning("evidence_archive_move_failed", error=str(exc))
            raise

    async def _claim_close(
        self,
        evidence_id: int,
        actor_id: int,
        moment: datetime,
        *,
        detail: str | None,
    ) -> bool:
        values: dict[str, object] = {"sent_at": moment, "closed_by": int(actor_id)}
        if detail is not None:
            values["detail_reason"] = detail
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(ModEvidence)
                .where(
                    ModEvidence.id == int(evidence_id),
                    ModEvidence.sent_at.is_(None),
                    ModEvidence.closed_by.is_(None),
                )
                .values(**values)
                .returning(ModEvidence.id)
            )
            return result.scalar_one_or_none() is not None

    async def _release_close(self, evidence_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            await session.execute(
                update(ModEvidence)
                .where(ModEvidence.id == int(evidence_id))
                .values(sent_at=None, closed_by=None, detail_reason=None, transcript=None, dm2_ok=False)
            )

    def _dm1_view(self, case: StoredCase, *, appeal_contact: str | None = None) -> discord.ui.LayoutView:
        rank = _rank_label(case.actor_rank)
        duration = None if not case.duration_seconds else copy.format_mod_duration(case.duration_seconds)
        return case_dm_view(
            case,
            rank=rank,
            duration=duration,
            appeal_button=dm_deliverable(case.kind),
            appeal_contact=appeal_contact,
        )

    def _dm2_view(self, case: StoredCase, detail: str) -> discord.ui.LayoutView:
        return case_dm_view(
            case,
            rank=_rank_label(case.actor_rank),
            detail=detail,
            appeal_button=dm_deliverable(case.kind),
        )

    async def _send_dm(self, target: object, view: discord.ui.LayoutView) -> bool:
        send = getattr(target, "send", None)
        if not callable(send):
            return False
        try:
            await self._discord(send_card(target, view))
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            log.warning("evidence_dm_failed", error=str(exc))
            return False
        return True

    async def _post(
        self,
        channel: object,
        view: object,
        mentions: discord.AllowedMentions,
        *,
        required: bool = False,
    ) -> Any:
        send = getattr(channel, "send", None)
        if not callable(send):
            if required:
                raise discord.HTTPException(SimpleNamespace(status=400, reason="send"), "no send")
            return None
        try:
            return await self._discord(send_card(channel, view, mentions=mentions))
        except discord.HTTPException as exc:
            log.warning("evidence_post_failed", error=str(exc))
            if required:
                raise
            return None

    def _actor_is_bot(self, user_id: int) -> bool:
        bot_id = getattr(getattr(self._bot, "user", None), "id", None)
        return bot_id is not None and int(bot_id) == int(user_id)

    def _guild(self, guild_id: int) -> discord.Guild | None:
        lookup = getattr(self._bot, "get_guild", None)
        if not callable(lookup):
            return None
        return lookup(int(guild_id))

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _case_from_row(row: ModCase) -> StoredCase:
    return StoredCase(
        id=int(row.id),
        guild_id=int(row.guild_id),
        number=int(row.number),
        owner_id=int(row.owner_id),
        created_by=int(row.created_by),
        actor_rank=int(row.actor_rank or 0),
        kind=str(row.kind),
        reason=str(row.reason or ""),
        duration_seconds=row.duration_seconds,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
        revoke_reason=row.revoke_reason,
        appeal_ticket_id=row.appeal_ticket_id,
    )


def _person(guild: object, user_id: int) -> object:
    getter = getattr(guild, "get_member", None)
    found = getter(int(user_id)) if callable(getter) else None
    if found is not None:
        return found
    return SimpleNamespace(id=int(user_id), mention=f"<@{int(user_id)}>", display_name=str(user_id))


def _rank_label(actor_rank: int) -> str:
    try:
        rank = Rank(int(actor_rank))
    except ValueError:
        rank = Rank.NONE
    return copy.rank_label(rank.key)


def _mention(person: object, fallback_id: int) -> str:
    mention = getattr(person, "mention", None)
    if mention:
        return str(mention)
    return f"<@{int(fallback_id)}>"


def _person_label(guild: object, user_id: int) -> str:
    found = _person(guild, user_id)
    name = getattr(found, "display_name", None) or getattr(found, "name", None) or str(user_id)
    return f"{name} ({int(user_id)})"


def _archive_when(value: datetime | None) -> str:
    if value is None:
        return "—"
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{transcript_stamp(moment)} · {discord.utils.format_dt(moment)}"


def _username(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    name = getattr(member, "display_name", None) or getattr(member, "name", None)
    return str(name) if name else str(user_id)


def _is_category(channel: object | None) -> bool:
    if channel is None:
        return False
    kind = getattr(channel, "type", None)
    return kind is discord.ChannelType.category or str(kind) == "category"


def _inspect_category(guild: object | None, category_id: int | None) -> tuple[Any | None, EvidenceDenial | None]:
    if category_id is None:
        return None, EvidenceDenial.NO_CATEGORY
    if guild is None:
        return None, None
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None, None
    channel = getter(int(category_id))
    if channel is None:
        return None, EvidenceDenial.CATEGORY_GONE
    if not _is_category(channel):
        return None, EvidenceDenial.NOT_A_CATEGORY
    return channel, None


def _bot_category_perms(guild: object | None, category: object | None) -> EvidenceDenial | None:
    if guild is None or category is None:
        return None
    me = getattr(guild, "me", None)
    probe = getattr(category, "permissions_for", None)
    if me is None or not callable(probe):
        return None
    perms = probe(me)
    if not bool(getattr(perms, "manage_channels", True)):
        return EvidenceDenial.BOT_PERMS
    return None


def _overwrites(
    guild: discord.Guild,
    actor: object,
    snapshot: object,
) -> dict[Any, discord.PermissionOverwrite]:
    mapping: dict[Any, discord.PermissionOverwrite] = {}
    # Роли — с type=Role: discord.py шлёт тип по `target.type`, а у голого Object это не роль,
    # и запрет @everyone вместе с доступом чифов/спецов ушли бы как overwrite на «участника».
    everyone = _snowflake(getattr(guild, "default_role", None), role=True)
    if everyone is not None:
        mapping[everyone] = discord.PermissionOverwrite(view_channel=False)
    me = _snowflake(getattr(guild, "me", None))
    if me is not None:
        mapping[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        )
    actor_member = actor if hasattr(actor, "roles") else guild.get_member(int(getattr(actor, "id", 0) or 0))
    actor_key = _snowflake(actor_member)
    if actor_key is not None:
        mapping[actor_key] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    seen: set[int] = set()
    rank_roles = getattr(snapshot, "rank_roles", {}) or {}
    for rank in (Rank.CHIEF, Rank.SPECIAL):
        for role_id in rank_roles.get(rank, ()):
            if role_id in seen:
                continue
            seen.add(int(role_id))
            role = _snowflake(guild.get_role(int(role_id)), role=True)
            if role is None:
                continue
            mapping[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    return mapping


def _snowflake(item: object | None, *, role: bool = False) -> discord.Object | None:
    if item is None:
        return None
    raw = getattr(item, "id", None)
    if raw is None:
        return None
    return discord.Object(id=int(raw), type=discord.Role if role else discord.Member)
