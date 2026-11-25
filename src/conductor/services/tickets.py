"""Тикеты: панель, открытие, взятие, закрытие с транскриптом."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    MemberRecord,
    ModCase,
    ModEvidence,
    Ticket,
    TicketCategory,
    TicketEvent,
    TicketPanel,
    TicketTranscript,
)
from conductor.db.session import session_scope
from conductor.domain.access import (
    STAFF_RANKS,
    TICKETS_APPROVE,
    TICKETS_CLOSE_ANY,
    TICKETS_CLOSE_OWN,
    TICKETS_NOTES,
    TICKETS_PRIORITY,
    TICKETS_REOPEN,
    AccessSnapshot,
    Rank,
    decide,
    resolve_rank,
)
from conductor.domain.activity import EventKind
from conductor.domain.create_lock import BUCKET_CHANNEL
from conductor.domain.ladder import Step, parse_step
from conductor.domain.moderation import (
    APPEALABLE_KINDS,
    AppealDecision,
    AppealVerdict,
    CaseKind,
    ExplainVerdict,
    ModVerdict,
    StoredCase,
    sanitize_detail_reason,
)
from conductor.domain.moderation import (
    can_appeal as can_appeal_case,
)
from conductor.domain.renames import Deferred
from conductor.domain.staff_feed import (
    ACTION_TICKET_APPEAL,
    ACTION_TICKET_APPROVE,
    ACTION_TICKET_APPROVE_REQ,
    ACTION_TICKET_CLAIM,
    ACTION_TICKET_CLOSE,
    ACTION_TICKET_OVERTURN,
    ACTION_TICKET_REASON,
    ACTION_TICKET_UPHOLD,
)
from conductor.domain.tickets import (
    DEFAULT_CATEGORY_SPECS,
    DEFAULT_OPEN_LIMIT,
    ESCALATE_COOLDOWN,
    LEVEL_OWNER,
    REVIEW_STATES,
    TRANSCRIPT_PROGRESS_AFTER,
    WORKING_STATES,
    AutocloseAction,
    CategoryKind,
    CategorySpec,
    ClaimVerdict,
    CloseVerdict,
    EscalateVerdict,
    FlowAction,
    FlowDecision,
    FlowRole,
    FormField,
    NotesVerdict,
    OpenDecision,
    OpenStep,
    OpenVerdict,
    Outcome,
    RateVerdict,
    ReopenVerdict,
    StoredCategory,
    StoredTicket,
    TicketClosed,
    TicketEventKind,
    TicketForm,
    TicketOpenMode,
    TicketPriority,
    TicketSettings,
    TicketState,
    activity_at,
    allowed_outcomes,
    append_staff_note,
    attachment_over_limit,
    autoclose_action,
    autoclose_deadline,
    can_appeal,
    can_open_ticket,
    claimed_rank_value,
    collect_form_fields,
    default_visible_ranks,
    dm_fail_due,
    dump_form_answers,
    dump_ticket_form,
    dump_ticket_settings,
    dump_visible_ranks,
    escalate_allowed,
    extract_subject_user_id,
    format_form_transcript,
    has_explain,
    is_review,
    is_staff_relay_skip,
    is_working,
    may_claim,
    may_close,
    may_edit_reason,
    may_manage_members,
    may_rate,
    may_reopen,
    may_see_notes,
    may_transfer,
    may_unclaim,
    member_tenure_days,
    next_appeal_level,
    next_ticket_number,
    notes_verdict,
    open_step,
    open_verdict,
    panel_record_ref,
    parse_form_answers,
    parse_kind,
    parse_priority,
    parse_rating,
    parse_staff_notes,
    parse_ticket_form,
    parse_ticket_settings,
    parse_topic,
    rank_may_see,
    remind_due,
    reopen_until,
    resolve_close_outcome,
    sanitize_channel_name,
    sanitize_hint,
    ticket_channel_author,
    ticket_channel_name,
    transition,
    used_appeal_levels,
    visible_rank_set,
)
from conductor.domain.traffic import Lane
from conductor.presentation import copy
from conductor.presentation.diagnose import log_channel_view
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import card_file, edit_card, paginate, panel, send_card, upload_cards, upload_limit
from conductor.presentation.mentions import going_mentions
from conductor.presentation.moderation import appeal_header_view, explain_card_view, explain_dm_view
from conductor.presentation.motion import MessageTarget, Progress, StageState
from conductor.presentation.tickets import (
    PANEL_KIND,
    TicketCard,
    TicketLogCard,
    close_request_view,
    public_panel_view,
    ticket_anon_relay_view,
    ticket_autoclose_warn_view,
    ticket_card_view,
    ticket_closed_dm_view,
    ticket_escalate_view,
    ticket_log_card_view,
    ticket_review_remind_view,
    ticket_staff_relay_view,
    ticket_welcome_view,
)
from conductor.presentation.tokens import Tone
from conductor.services.access import cached_snapshot
from conductor.services.broadcast import ping_many
from conductor.services.create_lock import continue_after_hold, find_active, hold_for, log_refused, note_success, trip
from conductor.services.panels import find_live_panel, mark_stale_by_ref, record_panel
from conductor.services.runtime import log_tick
from conductor.services.staff_feed import emit_staff
from conductor.services.traffic import lane
from conductor.services.transcripts import collect_transcript, transcript_stamp
from conductor.services.watchers_report import active_duty_ids

log = structlog.get_logger("conductor.tickets")

# `tickets.channel_id` после сноса канала закрытого тикета: колонка NOT NULL, а 0 — не id Discord.
CHANNEL_GONE = 0
LIVE_CHANNELS_TTL = 60.0

_CREATE_REASON = copy.REASON_TICKET
_CLOSE_REASON = copy.REASON_TICKET_CLOSE
_LOG_CREATE_STATES = frozenset(
    {
        TicketState.PENDING_APPROVAL,
        TicketState.CLOSED,
        TicketState.APPEAL_CHIEF,
        TicketState.APPEAL_OWNER,
    }
)


class TicketService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._relay_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._open_locks: dict[int, asyncio.Lock] = {}
        self._live_channels: dict[int, tuple[float, frozenset[int]]] = {}
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(60, "tickets-lifecycle", self.sweep_lifecycle)

    async def post_panel(
        self,
        guild: discord.Guild,
        *,
        channel: discord.TextChannel,
        archive: discord.TextChannel,
        actor_id: int,
        category: discord.CategoryChannel | None = None,
        mode: str = TicketOpenMode.CHANNEL,
        author_may_close: bool = False,
        log_channel_id: int | None = None,
    ) -> bool:
        async with session_scope(self._bot.sessions) as session:
            panel_row = await self._panel_for_channel(session, guild.id, channel.id)
            existing = TicketSettings() if panel_row is None else parse_ticket_settings(panel_row.settings)
            settings = TicketSettings(
                open_limit=_open_limit(self._bot),
                author_may_close=author_may_close,
                open_mode=mode if mode in {TicketOpenMode.CHANNEL, TicketOpenMode.THREAD} else TicketOpenMode.CHANNEL,
                archive_channel_id=archive.id,
                discord_category_id=None if category is None else category.id,
                log_channel_id=log_channel_id if log_channel_id else existing.log_channel_id,
            )
            if panel_row is None:
                panel_row = TicketPanel(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    settings=dump_ticket_settings(settings),
                    created_at=datetime.now(UTC),
                    created_by=actor_id,
                )
                session.add(panel_row)
                await session.flush()
            else:
                panel_row.settings = dump_ticket_settings(settings)
            await self._seed_categories(session, panel_row)
            panel_id = panel_row.id
            categories = await self._categories(session, panel_id)
            open_count = await self._guild_open_count(session, guild.id)
        view = public_panel_view(categories, open_count=open_count, limit=settings.open_limit)
        try:
            message = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("ticket_panel_send_failed", guild_id=guild.id, error=str(exc))
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(TicketPanel, panel_id)
            if row is not None:
                row.message_id = message.id
            await mark_stale_by_ref(session, guild_id=guild.id, kind=PANEL_KIND, ref=panel_record_ref(panel_id))
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=guild.id,
                channel_id=channel.id,
                message_id=message.id,
                ref=panel_record_ref(panel_id),
                created_by=actor_id,
            )
        return True

    async def panel_is_live(self, guild: discord.Guild, channel: discord.abc.GuildChannel) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await self._panel_for_channel(session, guild.id, int(channel.id))
            message_id = 0 if row is None else int(row.message_id or 0)
        if message_id <= 0:
            return False
        fetch = getattr(channel, "fetch_message", None)
        if not callable(fetch):
            return False
        try:
            found = await fetch(message_id)
        except discord.HTTPException:
            return False
        return found is not None

    async def open_preview(self, guild_id: int, ref: int | str) -> tuple[OpenStep, str, TicketForm, str]:
        try:
            category_id = int(ref)
        except (TypeError, ValueError):
            return OpenStep.OPEN, "", TicketForm(), ""
        async with session_scope(self._bot.sessions) as session:
            category = await session.get(TicketCategory, category_id)
            if category is None or category.guild_id != guild_id:
                return OpenStep.OPEN, "", TicketForm(), ""
            hint = sanitize_hint(category.hint)
            form = parse_ticket_form(category.form_json)
            return open_step(hint=hint, form=form), hint, form, category.name

    def _open_lock(self, guild_id: int) -> asyncio.Lock:
        """Номер тикета выбирается до создания канала — два открытия подряд идут по очереди."""
        lock = self._open_locks.get(int(guild_id))
        if lock is None:
            lock = self._open_locks[int(guild_id)] = asyncio.Lock()
        return lock

    async def open_ticket(
        self,
        guild: discord.Guild,
        member: discord.Member,
        ref: int | str,
        *,
        answers: Any = None,
    ) -> OpenDecision:
        try:
            category_id = int(ref)
        except (TypeError, ValueError):
            return OpenDecision(OpenVerdict.MISSING)
        async with self._open_lock(guild.id):
            return await self._open_ticket(guild, member, category_id, answers=answers)

    async def _open_ticket(
        self,
        guild: discord.Guild,
        member: discord.Member,
        category_id: int,
        *,
        answers: Any,
    ) -> OpenDecision:
        async with session_scope(self._bot.sessions) as session:
            category = await session.get(TicketCategory, category_id)
            if category is None or category.guild_id != guild.id:
                return OpenDecision(OpenVerdict.MISSING)
            panel_row = await session.get(TicketPanel, category.panel_id)
            if panel_row is None:
                return OpenDecision(OpenVerdict.MISSING)
            settings = parse_ticket_settings(panel_row.settings)
            open_count = await self._author_open_count(session, guild.id, member.id)
            if not can_open_ticket(open_count, limit=settings.open_limit):
                return OpenDecision(OpenVerdict.LIMIT)
            highest = await session.scalar(select(func.max(Ticket.number)).where(Ticket.guild_id == guild.id))
            number = next_ticket_number(highest)
            stored_category = _stored_category(category)
            anonymous = parse_kind(stored_category.kind) is CategoryKind.ANONYMOUS
            if anonymous:
                existing = await session.scalar(
                    select(Ticket.id).where(
                        Ticket.owner_id == member.id,
                        Ticket.anonymous.is_(True),
                        Ticket.state != TicketState.CLOSED,
                    )
                )
                if existing is not None:
                    return OpenDecision(OpenVerdict.ANON_OPEN)
            panel_id = panel_row.id
            panel_channel_id = panel_row.channel_id
        pairs = tuple((str(prompt), str(answer)) for prompt, answer in answers or ())
        form_answers = dump_form_answers(pairs)
        decision = open_verdict(
            stored_category.kind,
            await self._member_tenure_days(guild.id, member),
            await self._member_step(guild.id, member.id),
            stored_category,
        )
        if decision.verdict is not OpenVerdict.OK:
            return decision
        dm_channel_id = None
        if anonymous:
            dm_channel_id = await self._probe_anon_dm(member, number)
            if dm_channel_id is None:
                return OpenDecision(OpenVerdict.DM_CLOSED)
        visible = visible_rank_set(stored_category.visible_ranks, stored_category.kind)
        created = await self._create_destination(
            guild,
            member,
            stored_category.name,
            number,
            settings,
            panel_channel_id=panel_channel_id,
            visible=visible,
            anonymous=anonymous,
        )
        if created is None:
            hold = find_active(self._bot, guild.id, (BUCKET_CHANNEL,), time.time())
            if hold is not None and continue_after_hold(
                self._bot,
                ("ticket-create-defer", guild.id, member.id, category_id),
                hold,
                lambda: self.open_ticket(guild, member, category_id, answers=answers),
            ):
                return OpenDecision(OpenVerdict.HELD)
            return OpenDecision(OpenVerdict.MISSING)
        channel, channel_id = created
        stored = await self._insert_ticket(
            guild.id,
            number=number,
            owner_id=member.id,
            category_id=category_id,
            panel_id=panel_id,
            channel_id=channel_id,
            category=stored_category,
            form_answers=form_answers,
            subject_user_id=extract_subject_user_id(pairs, stored_category.excluded_user_field),
            anonymous=anonymous,
            dm_channel_id=dm_channel_id or None,
        )
        if stored is None:
            await self._safe_delete(channel)
            return OpenDecision(OpenVerdict.MISSING)
        await self._announce_opened(member, channel, stored)
        return OpenDecision(OpenVerdict.OK)

    async def open_case_appeal(
        self,
        guild: discord.Guild,
        member: discord.Member,
        case_id: int,
        *,
        text: str,
        now: datetime | None = None,
    ) -> AppealDecision:
        cleaned = sanitize_detail_reason(text)
        if cleaned is None:
            return AppealDecision(AppealVerdict.FAILED)
        async with self._open_lock(guild.id):
            return await self._open_case_appeal(guild, member, int(case_id), cleaned=cleaned, now=now)

    async def _open_case_appeal(
        self,
        guild: discord.Guild,
        member: discord.Member,
        case_id: int,
        *,
        cleaned: str,
        now: datetime | None,
    ) -> AppealDecision:
        moment = now or datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(case_id))
            if row is None or int(row.guild_id) != int(guild.id):
                return AppealDecision(AppealVerdict.MISSING)
            if int(row.owner_id) != int(member.id):
                return AppealDecision(AppealVerdict.FAILED)
            if row.revoked_at is not None:
                return AppealDecision(AppealVerdict.REVOKED)
            if row.appeal_ticket_id is not None:
                return AppealDecision(AppealVerdict.ALREADY)
            try:
                kind = CaseKind(row.kind)
            except ValueError:
                return AppealDecision(AppealVerdict.KIND)
            if kind not in APPEALABLE_KINDS:
                return AppealDecision(AppealVerdict.KIND)
            if not can_appeal_case(kind, row.created_at, moment, already_appealed=False):
                return AppealDecision(AppealVerdict.TOO_LATE)
            panel_row = await session.scalar(
                select(TicketPanel).where(TicketPanel.guild_id == guild.id).order_by(TicketPanel.id.desc())
            )
            if panel_row is None:
                return AppealDecision(AppealVerdict.FAILED)
            await self._seed_categories(session, panel_row)
            category = await self._appeal_category(session, panel_row)
            if category is None:
                return AppealDecision(AppealVerdict.FAILED)
            settings = parse_ticket_settings(panel_row.settings)
            highest = await session.scalar(select(func.max(Ticket.number)).where(Ticket.guild_id == guild.id))
            number = next_ticket_number(highest)
            stored_category = _stored_category(category)
            stored_case = _mod_case(row)
            detail = await session.scalar(select(ModEvidence.detail_reason).where(ModEvidence.case_id == row.id))
            punisher_id = int(row.created_by)
            category_id = category.id
            panel_id = panel_row.id
            panel_channel_id = panel_row.channel_id
        visible = visible_rank_set(stored_category.visible_ranks, stored_category.kind)
        created = await self._create_destination(
            guild,
            member,
            stored_category.name,
            number,
            settings,
            panel_channel_id=panel_channel_id,
            visible=visible,
            deny_user_ids=(punisher_id,),
        )
        if created is None:
            return AppealDecision(AppealVerdict.FAILED)
        channel, channel_id = created
        stored = await self._insert_ticket(
            guild.id,
            number=number,
            owner_id=member.id,
            category_id=category_id,
            panel_id=panel_id,
            channel_id=channel_id,
            category=stored_category,
            form_answers=dump_form_answers(((copy.CASE_APPEAL_LABEL, cleaned),)),
            subject_user_id=punisher_id,
        )
        if stored is None:
            await self._safe_delete(channel)
            return AppealDecision(AppealVerdict.FAILED)
        bound = await self._bind_appeal_ticket(stored_case.id, stored.id)
        if not bound:
            await self._safe_delete(channel)
            await self._discard_ticket(stored.id)
            return AppealDecision(AppealVerdict.ALREADY)
        await self._announce_opened(member, channel, stored)
        try:
            await self._discord(
                send_card(channel, appeal_header_view(stored_case, ticket_id=stored.id, detail=detail, appeal=cleaned))
            )
        except discord.HTTPException as exc:
            log.warning("appeal_header_failed", ticket_id=stored.id, error=str(exc))
        await self._sync_log_card(guild, stored.id, force=True)
        rank, _, _ = await self._actor_flags(guild, member)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=member.id,
            actor_rank=rank,
            action=ACTION_TICKET_APPEAL,
            target=member.id,
            ref=stored.id,
            details=cleaned,
        )
        await self._dm_explain(guild, punisher_id, stored.id)
        return AppealDecision(AppealVerdict.OK, ticket_id=stored.id, channel_id=stored.channel_id)

    async def keep_case_appeal(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        reason: str,
    ) -> CloseVerdict:
        cleaned = sanitize_detail_reason(reason)
        if cleaned is None:
            return CloseVerdict.NEED_OUTCOME
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return CloseVerdict.ALREADY
        if parse_kind(ticket.category_kind) is not CategoryKind.APPEAL:
            return CloseVerdict.NEED_OUTCOME
        case = await self._case_for_ticket(ticket.id)
        if _is_case_punisher(case, actor):
            return CloseVerdict.STRANGER
        return await self.close_ticket(
            guild, ticket.id, actor, reason=cleaned, outcome=Outcome.REJECTED, case_appeal=True, skip_verdict=True
        )

    async def accept_case_appeal(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        reason: str,
    ) -> ModVerdict:
        cleaned = sanitize_detail_reason(reason)
        if cleaned is None:
            return ModVerdict.REASON
        ticket = await self._load(guild.id, ref)
        if ticket is None or parse_kind(ticket.category_kind) is not CategoryKind.APPEAL:
            return ModVerdict.FAILED
        case = await self._case_for_ticket(ticket.id)
        if case is None:
            return ModVerdict.FAILED
        if _is_case_punisher(case, actor):
            return ModVerdict.DENIED
        moderation = getattr(self._bot, "moderation", None)
        revoke = getattr(moderation, "revoke", None)
        if not callable(revoke):
            return ModVerdict.FAILED
        outcome = await revoke(guild, case.id, actor, cleaned)
        if outcome.verdict not in {ModVerdict.OK, ModVerdict.REVOKED}:
            return outcome.verdict
        closed = await self.close_ticket(
            guild,
            ticket.id,
            actor,
            reason=cleaned,
            outcome=Outcome.ACCEPTED,
            case_appeal=True,
            skip_verdict=True,
        )
        if closed is not CloseVerdict.OK and closed is not CloseVerdict.ALREADY:
            return ModVerdict.FAILED
        return ModVerdict.OK

    async def explain_case_appeal(self, ticket_id: int, actor_id: int, text: str) -> ExplainVerdict:
        cleaned = sanitize_detail_reason(text)
        if cleaned is None:
            return ExplainVerdict.FAILED
        ticket = await self._load_any(ticket_id)
        if ticket is None:
            return ExplainVerdict.MISSING
        case = await self._case_for_ticket(ticket.id)
        if case is None or int(case.created_by) != int(actor_id):
            return ExplainVerdict.DENIED
        async with session_scope(self._bot.sessions) as session:
            kinds = (await session.scalars(select(TicketEvent.kind).where(TicketEvent.ticket_id == ticket.id))).all()
        if has_explain(kinds):
            return ExplainVerdict.ALREADY
        await self._record_event(ticket, kind=TicketEventKind.EXPLAIN, actor_id=int(actor_id), note=cleaned)
        guild = self._bot_guild(ticket.guild_id)
        channel = None if guild is None else self._ticket_channel(guild, ticket.channel_id)
        if channel is not None and hasattr(channel, "send"):
            try:
                await self._discord(
                    send_card(channel, explain_card_view(actor_id=int(actor_id), text=cleaned))
                )
            except discord.HTTPException as exc:
                log.warning("appeal_explain_failed", ticket_id=ticket.id, error=str(exc))
        return ExplainVerdict.OK

    async def guard_explain_dm(self, ref: int | str) -> GuardResource | None:
        ticket = await self._load_any(ref)
        if ticket is None:
            return None
        case = await self._case_for_ticket(ticket.id)
        if case is None:
            return None
        return GuardResource(ref=str(ticket.id), guild_id=ticket.guild_id, owner_id=case.created_by)

    async def appeal_evidence_file(self, case_id: int) -> tuple[str, str] | None:
        async with session_scope(self._bot.sessions) as session:
            case = await session.get(ModCase, int(case_id))
            if case is None:
                return None
            row = await session.scalar(select(ModEvidence).where(ModEvidence.case_id == int(case_id)))
            text = "" if row is None else str(row.transcript or "")
            if not text.strip():
                return None
            return copy.evidence_transcript_name(case.number), text

    async def mark_faq_resolved(self, guild_id: int, *, now: datetime | None = None) -> None:
        collector = getattr(self._bot, "collector", None)
        bump = getattr(collector, "bump", None)
        if not callable(bump):
            return
        bump(guild_id, now or datetime.now(UTC), EventKind.TICKET_FAQ)

    async def category_names(self, guild_id: int) -> list[str]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(TicketCategory.name)
                    .where(TicketCategory.guild_id == guild_id)
                    .order_by(TicketCategory.position, TicketCategory.name)
                )
            ).all()
        found: list[str] = []
        seen: set[str] = set()
        for name in rows:
            if name in seen:
                continue
            seen.add(name)
            found.append(name)
        return found

    async def set_category_form(self, guild_id: int, name: str, prompts: list[str]) -> bool:
        form = collect_form_fields(prompts)
        payload = dump_ticket_form(form)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
                )
            ).all()
            if not rows:
                return False
            for row in rows:
                row.form_json = payload
        return True

    async def set_category_hint(self, guild_id: int, name: str, text: str) -> bool:
        hint = sanitize_hint(text)
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
                )
            ).all()
            if not rows:
                return False
            for row in rows:
                row.hint = hint
        return True

    async def clear_category_help(self, guild_id: int, name: str) -> bool:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
                )
            ).all()
            if not rows:
                return False
            for row in rows:
                row.hint = ""
                row.form_json = "{}"
        return True

    async def category_help(self, guild_id: int, name: str) -> tuple[str, TicketForm] | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
            )
            if row is None:
                return None
            return sanitize_hint(row.hint), parse_ticket_form(row.form_json)

    async def list_categories(self, guild_id: int) -> list[StoredCategory]:
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(TicketCategory)
                    .where(TicketCategory.guild_id == guild_id)
                    .order_by(TicketCategory.position, TicketCategory.id)
                )
            ).all()
            return [_stored_category(row) for row in rows]

    async def add_category(self, guild_id: int, name: str) -> bool:
        text = name.strip()[:80]
        if not text:
            return False
        async with session_scope(self._bot.sessions) as session:
            panel_row = await session.scalar(
                select(TicketPanel).where(TicketPanel.guild_id == guild_id).order_by(TicketPanel.id.desc())
            )
            if panel_row is None:
                return False
            taken = await session.scalar(
                select(TicketCategory.id).where(TicketCategory.panel_id == panel_row.id, TicketCategory.name == text)
            )
            if taken is not None:
                return False
            highest = await session.scalar(
                select(func.max(TicketCategory.position)).where(TicketCategory.panel_id == panel_row.id)
            )
            session.add(
                TicketCategory(
                    panel_id=panel_row.id,
                    guild_id=guild_id,
                    name=text,
                    position=int(highest or 0) + 1,
                    kind=CategoryKind.GENERAL,
                    visible_ranks=dump_visible_ranks(default_visible_ranks(CategoryKind.GENERAL)),
                )
            )
        return True

    async def remove_category(self, guild_id: int, name: str) -> str:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
            )
            if row is None:
                return "missing"
            open_count = await session.scalar(
                select(func.count()).where(
                    Ticket.category_id == row.id,
                    Ticket.state != TicketState.CLOSED,
                )
            )
            if int(open_count or 0) > 0:
                return "open"
            await session.delete(row)
        return "ok"

    async def set_category_kind(self, guild_id: int, name: str, kind: str) -> bool:
        parsed = parse_kind(kind)
        async with session_scope(self._bot.sessions) as session:
            rows = await self._category_rows(session, guild_id, name)
            if not rows:
                return False
            for row in rows:
                row.kind = parsed
                row.hidden = parsed is CategoryKind.APPEAL
                if parsed is not CategoryKind.APPLICATION:
                    row.topic = ""
        return True

    async def set_category_ranks(self, guild_id: int, name: str, raw: str) -> bool:
        async with session_scope(self._bot.sessions) as session:
            rows = await self._category_rows(session, guild_id, name)
            if not rows:
                return False
            for row in rows:
                row.visible_ranks = dump_visible_ranks(visible_rank_set(raw, row.kind))
        return True

    async def set_category_topic(self, guild_id: int, name: str, topic: str) -> bool:
        async with session_scope(self._bot.sessions) as session:
            rows = await self._category_rows(session, guild_id, name)
            if not rows:
                return False
            for row in rows:
                if parse_kind(row.kind) is not CategoryKind.APPLICATION:
                    row.topic = ""
                    continue
                row.topic = parse_topic(topic)
        return True

    async def set_category_info(self, guild_id: int, name: str, text: str) -> bool:
        info = sanitize_hint(text)
        async with session_scope(self._bot.sessions) as session:
            rows = await self._category_rows(session, guild_id, name)
            if not rows:
                return False
            for row in rows:
                row.info_text = info
        return True

    async def _category_rows(self, session: AsyncSession, guild_id: int, name: str) -> list[TicketCategory]:
        return list(
            (
                await session.scalars(
                    select(TicketCategory).where(TicketCategory.guild_id == guild_id, TicketCategory.name == name)
                )
            ).all()
        )

    async def _member_tenure_days(self, guild_id: int, member: object) -> int:
        discord_days = member_tenure_days(getattr(member, "joined_at", None))
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(
                select(MemberRecord).where(
                    MemberRecord.guild_id == int(guild_id),
                    MemberRecord.user_id == int(getattr(member, "id", 0) or 0),
                )
            )
            recorded = None if row is None else row.first_joined_at
        return max(discord_days, member_tenure_days(recorded))

    async def _member_step(self, guild_id: int, user_id: int) -> str:
        ladder = getattr(self._bot, "ladder", None)
        card_of = getattr(ladder, "card", None)
        if not callable(card_of):
            return Step.GUEST.key
        card = await card_of(guild_id, user_id)
        held = getattr(card, "held", None)
        return parse_step(getattr(held, "key", held)).key

    def _emit_closed(self, event: TicketClosed) -> None:
        events = getattr(self._bot, "events", None)
        emit = getattr(events, "emit", None)
        if callable(emit):
            emit(event)

    async def claim_ticket(self, guild: discord.Guild, ref: int | str, actor: discord.Member) -> ClaimVerdict:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return ClaimVerdict.CLOSED
        rank, _, _ = await self._actor_flags(guild, actor)
        if not rank_may_see(
            rank,
            visible_rank_set(ticket.visible_ranks, ticket.category_kind),
            is_administrator=_is_admin(actor),
        ):
            return ClaimVerdict.HIDDEN
        verdict = may_claim(actor_id=actor.id, claimed_by=ticket.claimed_by, closed=_inactive(ticket))
        if verdict is not ClaimVerdict.OK:
            return verdict
        if not await self._set_claim(ticket.id, actor.id, int(rank), TicketState.CLAIMED, expect=None):
            return ClaimVerdict.TAKEN
        await self.refresh_card(guild, ticket.id)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor.id,
            actor_rank=rank,
            action=ACTION_TICKET_CLAIM,
            target=_staff_target(ticket),
            ref=ticket.id,
            details=ticket.category_name or None,
        )
        return ClaimVerdict.OK

    async def unclaim_ticket(self, guild: discord.Guild, ref: int | str, actor: discord.Member) -> ClaimVerdict:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return ClaimVerdict.CLOSED
        _, has_close_any, _ = await self._actor_flags(guild, actor)
        verdict = may_unclaim(
            actor_id=actor.id,
            claimed_by=ticket.claimed_by,
            has_close_any=has_close_any,
            closed=_inactive(ticket),
        )
        if verdict is not ClaimVerdict.OK:
            return verdict
        if not await self._set_claim(ticket.id, None, 0, TicketState.OPEN, expect=ticket.claimed_by):
            return ClaimVerdict.TAKEN
        await self.refresh_card(guild, ticket.id)
        return ClaimVerdict.OK

    async def transfer_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        target: discord.abc.User,
    ) -> ClaimVerdict:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return ClaimVerdict.CLOSED
        _, has_close_any, _ = await self._actor_flags(guild, actor)
        verdict = may_transfer(
            actor_id=actor.id,
            claimed_by=ticket.claimed_by,
            target_id=target.id,
            has_close_any=has_close_any,
            closed=_inactive(ticket),
        )
        if verdict is not ClaimVerdict.OK:
            return verdict
        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(role.id for role in getattr(target, "roles", ()) or ())
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        if not await self._set_claim(ticket.id, target.id, int(rank), TicketState.CLAIMED, expect=ticket.claimed_by):
            return ClaimVerdict.TAKEN
        await self.refresh_card(guild, ticket.id)
        return ClaimVerdict.OK

    async def add_member(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        target: discord.abc.User,
    ) -> bool:
        ticket = await self._managed(guild, ref, actor)
        if ticket is None:
            return False
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is None:
            return False
        if hasattr(channel, "set_permissions"):
            try:
                await self._discord(
                    channel.set_permissions(
                        target,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        reason=_CREATE_REASON,
                    )
                )
            except discord.HTTPException as exc:
                log.warning("ticket_add_failed", ticket_id=ticket.id, error=str(exc))
                return False
            return True
        add_user = getattr(channel, "add_user", None)
        if not callable(add_user):
            return False
        try:
            await self._discord(add_user(target))
        except discord.HTTPException as exc:
            log.warning("ticket_add_user_failed", ticket_id=ticket.id, error=str(exc))
            return False
        return True

    async def remove_member(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        target: discord.abc.User,
    ) -> bool | str:
        ticket = await self._managed(guild, ref, actor)
        if ticket is None:
            return False
        if target.id == ticket.author_id:
            return "author"
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is None:
            return False
        if hasattr(channel, "set_permissions"):
            try:
                await self._discord(channel.set_permissions(target, overwrite=None, reason=_CLOSE_REASON))
            except discord.HTTPException as exc:
                log.warning("ticket_remove_failed", ticket_id=ticket.id, error=str(exc))
                return False
            return True
        remove_user = getattr(channel, "remove_user", None)
        if not callable(remove_user):
            return False
        try:
            await self._discord(remove_user(target))
        except discord.HTTPException as exc:
            log.warning("ticket_remove_user_failed", ticket_id=ticket.id, error=str(exc))
            return False
        return True

    async def rename_ticket(
        self, guild: discord.Guild, ref: int | str, actor: discord.Member, name: str
    ) -> bool | Deferred:
        ticket = await self._managed(guild, ref, actor)
        if ticket is None:
            return False
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is None or not callable(getattr(channel, "edit", None)):
            return False
        wanted = sanitize_channel_name(name)
        renames = getattr(self._bot, "renames", None)
        rename = getattr(renames, "rename", None)
        if not callable(rename):
            return False
        result = await rename(channel, wanted, key="ticket", reason=_CREATE_REASON)
        if isinstance(result, Deferred):
            return result
        return str(getattr(channel, "name", "") or "") == wanted

    async def close_verdict(self, guild: discord.Guild, ref: int | str, actor: discord.Member) -> CloseVerdict:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return CloseVerdict.ALREADY
        if parse_kind(ticket.category_kind) is CategoryKind.APPEAL:
            return CloseVerdict.CASE_APPEAL
        return await self._may_close(guild, ticket, actor)

    async def actor_is_staff(self, guild: discord.Guild, actor: discord.Member) -> bool:
        _, has_close_any, has_close_own = await self._actor_flags(guild, actor)
        return has_close_any or has_close_own

    async def close_outcomes(self, guild: discord.Guild, ref: int | str) -> tuple[Outcome, ...]:
        ticket = await self._load(guild.id, ref)
        if ticket is None or parse_kind(ticket.category_kind) is CategoryKind.APPEAL:
            return ()
        return allowed_outcomes(ticket.category_kind)

    async def request_approval(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return FlowDecision(False, reason="wrong_state")
        decision = transition(
            ticket.state,
            FlowAction.APPROVE_REQ,
            await self._flow_role(guild, ticket, actor, FlowAction.APPROVE_REQ),
            ticket.category_kind,
            ticket.outcome,
            moment,
            ticket.closed_at,
        )
        if not decision.ok or decision.state is None:
            return decision
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or row.state != TicketState.CLAIMED:
                return FlowDecision(False, reason="wrong_state")
            row.state = decision.state
            session.add(_event_row(ticket, kind=decision.event_kind, actor_id=actor.id, now=moment))
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        await self._emit_staff_flow(guild, actor, ACTION_TICKET_APPROVE_REQ, ticket)
        await self._dm_author(guild, ticket, copy.TICKET_DM_APPROVE_REQ)
        return decision

    async def approve_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return FlowDecision(False, reason="wrong_state")
        decision = transition(
            ticket.state,
            FlowAction.APPROVE,
            await self._flow_role(guild, ticket, actor, FlowAction.APPROVE),
            ticket.category_kind,
            ticket.outcome,
            moment,
            ticket.closed_at,
        )
        if not decision.ok:
            return decision
        verdict = await self.close_ticket(
            guild,
            ticket.id,
            actor,
            outcome=Outcome.ACCEPTED,
            now=moment,
            skip_verdict=True,
            staff_action=ACTION_TICKET_APPROVE,
        )
        if verdict is not CloseVerdict.OK:
            return FlowDecision(False, reason="wrong_state")
        await self._record_event(ticket, kind=decision.event_kind, actor_id=actor.id, now=moment)
        await self._dm_author(guild, ticket, copy.TICKET_DM_APPROVE)
        return decision

    async def reject_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> FlowDecision:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return FlowDecision(False, reason="wrong_state")
        decision = transition(
            ticket.state,
            FlowAction.REJECT,
            await self._flow_role(guild, ticket, actor, FlowAction.REJECT),
            ticket.category_kind,
            ticket.outcome,
            moment,
            ticket.closed_at,
        )
        if not decision.ok:
            return decision
        verdict = await self.close_ticket(
            guild,
            ticket.id,
            actor,
            reason=reason,
            outcome=Outcome.REJECTED,
            now=moment,
            skip_verdict=True,
        )
        if verdict is not CloseVerdict.OK:
            return FlowDecision(False, reason="already" if verdict is CloseVerdict.ALREADY else "wrong_state")
        await self._record_event(ticket, kind=decision.event_kind, actor_id=actor.id, note=reason or "", now=moment)
        await self._dm_author(guild, ticket, copy.TICKET_DM_REJECT)
        return decision

    async def appeal_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return FlowDecision(False, reason="wrong_state")
        used = await self._used_appeal_levels(ticket.id)
        decision = transition(
            ticket.state,
            FlowAction.APPEAL,
            FlowRole.AUTHOR if actor.id == ticket.author_id else FlowRole.OTHER,
            ticket.category_kind,
            ticket.outcome,
            moment,
            ticket.closed_at,
            used_levels=used,
        )
        if not decision.ok or decision.state is None:
            return decision
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or row.state != TicketState.CLOSED:
                return FlowDecision(False, reason="wrong_state")
            row.state = decision.state
            session.add(_event_row(ticket, kind=decision.event_kind, actor_id=actor.id, now=moment))
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is not None:
            await self._apply_review_overwrites(
                guild, ticket, channel, include_owner=decision.state == TicketState.APPEAL_OWNER
            )
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        await self._emit_staff_flow(guild, actor, ACTION_TICKET_APPEAL, ticket)
        await self._dm_author(guild, ticket, copy.TICKET_DM_APPEAL)
        return decision

    async def uphold_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        return await self._review_ticket(guild, ref, actor, FlowAction.UPHOLD, now=now)

    async def overturn_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        return await self._review_ticket(guild, ref, actor, FlowAction.OVERTURN, now=now)

    async def _review_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        action: FlowAction,
        *,
        now: datetime | None = None,
    ) -> FlowDecision:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return FlowDecision(False, reason="wrong_state")
        decision = transition(
            ticket.state,
            action,
            await self._flow_role(guild, ticket, actor, action),
            ticket.category_kind,
            ticket.outcome,
            moment,
            ticket.closed_at,
            closer_is_actor=ticket.closed_by == actor.id,
            used_levels=await self._used_appeal_levels(ticket.id),
        )
        if not decision.ok or decision.state is None:
            return decision
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or not is_review(row.state):
                return FlowDecision(False, reason="wrong_state")
            self._forget_live_channels()
            if action is FlowAction.UPHOLD:
                row.state = TicketState.CLOSED
            else:
                row.state = TicketState.OPEN
                row.closed_at = None
                row.closed_by = None
                row.close_reason = None
                row.outcome = None
                row.rating = None
                row.close_requested_at = None
                row.autoclose_warned_at = None
                row.last_activity_at = moment
            session.add(_event_row(ticket, kind=decision.event_kind, actor_id=actor.id, now=moment))
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is not None:
            if action is FlowAction.UPHOLD:
                await self._lock_closed_channel(guild, ticket, channel)
            else:
                await self._unlock_reopened_channel(guild, ticket, channel)
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        staff_action = ACTION_TICKET_UPHOLD if action is FlowAction.UPHOLD else ACTION_TICKET_OVERTURN
        await self._emit_staff_flow(guild, actor, staff_action, ticket)
        dm = copy.TICKET_DM_UPHOLD if action is FlowAction.UPHOLD else copy.TICKET_DM_OVERTURN
        await self._dm_author(guild, ticket, dm)
        return decision

    async def close_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        reason: str | None = None,
        outcome: str | None = None,
        system: bool = False,
        now: datetime | None = None,
        skip_verdict: bool = False,
        staff_action: str | None = None,
        case_appeal: bool = False,
    ) -> CloseVerdict:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None or _closed(ticket):
            return CloseVerdict.ALREADY
        if system and not is_working(ticket.state):
            return CloseVerdict.ALREADY
        if parse_kind(ticket.category_kind) is CategoryKind.APPEAL and not case_appeal and not system:
            return CloseVerdict.CASE_APPEAL
        if not system and not skip_verdict:
            if not is_working(ticket.state):
                return CloseVerdict.ALREADY
            verdict = await self._may_close(guild, ticket, actor)
            if verdict is not CloseVerdict.OK:
                return verdict
        closer_id = int(getattr(actor, "id", 0) or 0)
        is_staff = False if system else await self.actor_is_staff(guild, actor)
        resolved = resolve_close_outcome(
            system=system,
            author_close=not is_staff and closer_id == ticket.author_id,
            raw=outcome,
            kind=ticket.category_kind,
        )
        if resolved is None:
            return CloseVerdict.NEED_OUTCOME
        if not await self._mark_closed(ticket.id, closer_id, reason, resolved, now=moment):
            return CloseVerdict.ALREADY
        self._emit_closed(
            TicketClosed(
                guild_id=ticket.guild_id,
                ticket_id=ticket.id,
                owner_id=ticket.author_id,
                category_kind=parse_kind(ticket.category_kind),
                outcome=resolved,
                closed_by=closer_id,
                subject_user_id=ticket.subject_user_id,
            )
        )
        closed = await self._load(guild.id, ticket.id) or ticket
        channel = self._ticket_channel(guild, ticket.channel_id)
        archive = await self._archive_channel(guild, ticket.panel_id)
        if channel is not None:
            payload, count = await self._transcript(channel, closed)
            await self._store_transcript(closed, payload, now=moment)
            await self._send_transcript(guild, closed, payload, count, archive)
            await self._lock_closed_channel(guild, closed, channel)
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        rank, _, _ = await self._actor_flags(guild, actor)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=closer_id,
            actor_rank=rank,
            action=staff_action or ACTION_TICKET_CLOSE,
            target=_staff_target(ticket),
            ref=ticket.id,
            details=reason,
        )
        if closed.anonymous:
            await self._dm_closed(guild, closed)
        return CloseVerdict.OK

    async def request_close(self, guild: discord.Guild, ref: int | str, actor: discord.Member) -> bool:
        ticket = await self._load(guild.id, ref)
        if ticket is None or _inactive(ticket) or actor.id != ticket.author_id:
            return False
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or not is_working(row.state):
                return False
            row.close_requested_at = datetime.now(UTC)
        channel = self._ticket_channel(guild, ticket.channel_id)
        send = None if channel is None else getattr(channel, "send", None)
        if callable(send):
            try:
                await self._discord(
                    send_card(channel, close_request_view(author_id=ticket.author_id))
                )
            except discord.HTTPException as exc:
                log.warning("ticket_close_request_failed", ticket_id=ticket.id, error=str(exc))
        return True

    async def set_priority(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        value: str,
    ) -> bool:
        ticket = await self._load(guild.id, ref)
        if ticket is None or _inactive(ticket):
            return False
        if not await self._has_key(guild, actor, TICKETS_PRIORITY):
            return False
        priority = parse_priority(value)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or not is_working(row.state):
                return False
            row.priority = priority
        await self.refresh_card(guild, ticket.id)
        return True

    async def list_notes(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
    ) -> list[tuple[int, str]] | None:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return None
        has_key = await self._has_key(guild, actor, TICKETS_NOTES)
        if (
            notes_verdict(
                actor_id=actor.id,
                author_id=ticket.author_id,
                has_notes_key=has_key,
                closed=_inactive(ticket),
            )
            is not NotesVerdict.OK
        ):
            return None
        return [(note.author_id, note.body) for note in parse_staff_notes(ticket.staff_notes)]

    async def add_note(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        body: str,
    ) -> bool:
        ticket = await self._load(guild.id, ref)
        if ticket is None or _inactive(ticket):
            return False
        has_key = await self._has_key(guild, actor, TICKETS_NOTES)
        if not may_see_notes(actor_id=actor.id, author_id=ticket.author_id, has_notes_key=has_key):
            return False
        payload = append_staff_note(ticket.staff_notes, author_id=actor.id, body=body, now=datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None or not is_working(row.state):
                return False
            row.staff_notes = payload
        return True

    async def escalate_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> EscalateVerdict:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None or _inactive(ticket):
            return EscalateVerdict.CLOSED
        if not escalate_allowed(ticket.escalated_at, moment):
            return EscalateVerdict.COOLDOWN
        seniors = await self._duty_seniors(guild)
        if not await self._mark_escalated(ticket.id, moment):
            return EscalateVerdict.COOLDOWN
        if not seniors:
            return EscalateVerdict.EMPTY
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is None or not hasattr(channel, "send"):
            return EscalateVerdict.OK
        try:
            await self._discord(ping_many(self._bot, channel, seniors, header=ticket_escalate_view()))
        except discord.HTTPException as exc:
            log.warning("ticket_escalate_failed", ticket_id=ticket.id, error=str(exc))
        return EscalateVerdict.OK

    async def rate_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        score: int,
    ) -> RateVerdict:
        parsed = parse_rating(score)
        if parsed is None:
            return RateVerdict.BAD
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return RateVerdict.OPEN
        verdict = may_rate(
            actor_id=actor.id,
            author_id=ticket.author_id,
            rating=ticket.rating,
            closed=_closed(ticket),
        )
        if verdict is not RateVerdict.OK:
            return verdict
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket.id,
                    Ticket.state == TicketState.CLOSED,
                    Ticket.rating.is_(None),
                )
                .values(rating=parsed)
            )
            if int(result.rowcount or 0) <= 0:
                return RateVerdict.ALREADY
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        return RateVerdict.OK

    async def reopen_ticket(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        *,
        now: datetime | None = None,
    ) -> ReopenVerdict:
        moment = now or datetime.now(UTC)
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return ReopenVerdict.NOT_CLOSED
        if not await self._has_key(guild, actor, TICKETS_REOPEN):
            return ReopenVerdict.NOT_CLOSED
        settings = await self._settings_for(ticket.panel_id)
        channel = self._ticket_channel(guild, ticket.channel_id)
        verdict = may_reopen(
            closed=_closed(ticket),
            closed_at=ticket.closed_at,
            now=moment,
            days=settings.reopen_days,
            channel_alive=channel is not None,
        )
        if verdict is not ReopenVerdict.OK:
            return verdict
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket.id, Ticket.state == TicketState.CLOSED)
                .values(
                    state=TicketState.OPEN,
                    # Старое взятие не переносится: иначе карточка говорит «взял X», а других не пускает.
                    claimed_by=None,
                    claimed_rank=0,
                    closed_at=None,
                    closed_by=None,
                    close_reason=None,
                    outcome=None,
                    rating=None,
                    close_requested_at=None,
                    autoclose_warned_at=None,
                    last_activity_at=moment,
                )
            )
            if int(result.rowcount or 0) <= 0:
                return ReopenVerdict.NOT_CLOSED
        self._forget_live_channels()
        if channel is not None:
            await self._unlock_reopened_channel(guild, ticket, channel)
        await self.refresh_card(guild, ticket.id)
        await self._sync_log_card(guild, ticket.id)
        return ReopenVerdict.OK

    async def on_guild_message(self, message: discord.Message) -> None:
        """Одно сообщение гильдии — ноль запросов к БД, если канал не тикет."""
        guild = message.guild
        if guild is None:
            return
        if int(message.channel.id) not in await self._live_channel_ids(int(guild.id)):
            return
        await self.touch_activity(guild.id, message.channel.id)
        await self.relay_staff(message)

    async def _live_channel_ids(self, guild_id: int) -> frozenset[int]:
        """Каналы незакрытых тикетов. Кэш сбрасывают мутации и TTL — промах стоит один SELECT."""
        cached = self._live_channels.get(guild_id)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        async with session_scope(self._bot.sessions) as session:
            rows = (
                await session.scalars(
                    select(Ticket.channel_id).where(Ticket.guild_id == guild_id, Ticket.state != TicketState.CLOSED)
                )
            ).all()
        ids = frozenset(int(value) for value in rows)
        self._live_channels[guild_id] = (now + LIVE_CHANNELS_TTL, ids)
        return ids

    def _forget_live_channels(self) -> None:
        self._live_channels.clear()

    async def touch_activity(self, guild_id: int, channel_id: int, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            await session.execute(
                update(Ticket)
                .where(
                    Ticket.guild_id == guild_id,
                    Ticket.channel_id == channel_id,
                    Ticket.state != TicketState.CLOSED,
                )
                .values(last_activity_at=moment, autoclose_warned_at=None)
            )

    async def sweep_lifecycle(self, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        jobs = await self._lifecycle_jobs(moment)
        getter = getattr(self._bot, "get_guild", None)
        done = 0
        for job in jobs:
            guild = None if not callable(getter) else getter(job["guild_id"])
            if guild is None:
                continue
            action = job["action"]
            ticket_id = int(job["ticket_id"])
            if action == AutocloseAction.WARN:
                await self._send_autoclose_warn(guild, ticket_id, job["deadline"])
            elif action == AutocloseAction.CLOSE:
                actor = getattr(guild, "me", None) or actor_stub()
                await self.close_ticket(guild, ticket_id, actor, system=True, now=moment)
            elif action == "expire":
                await self._expire_closed_channel(guild, ticket_id)
            elif action == "remind":
                await self._send_review_remind(guild, ticket_id, now=moment)
            else:
                continue
            done += 1
        log_tick(log, "tickets_lifecycle", changed=done)

    async def guard_ticket(self, guild_id: int, ref: int | str, *, owner: str = "author") -> GuardResource | None:
        ticket = await self._load(guild_id, ref)
        return None if ticket is None else _guard_resource(ticket, owner=owner)

    async def set_log_channel(self, guild_id: int, channel_id: int | None) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await self._latest_panel(session, guild_id)
            if row is None:
                return False
            settings = parse_ticket_settings(row.settings)
            log_id = channel_id if channel_id and channel_id > 0 else None
            row.settings = dump_ticket_settings(replace(settings, log_channel_id=log_id))
            return True

    async def diagnose_log(self, guild: discord.Guild) -> Any:
        async with session_scope(self._bot.sessions) as session:
            row = await self._latest_panel(session, guild.id)
            if row is None:
                return None
            settings = parse_ticket_settings(row.settings)
        return log_channel_view(guild, getattr(guild, "me", None), settings.log_channel_id)

    async def set_close_reason(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
        reason: str,
    ) -> bool:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return False
        rank, has_close_any, _ = await self._actor_flags(guild, actor)
        if not may_edit_reason(
            actor_id=actor.id,
            author_id=ticket.author_id,
            closed_by=ticket.closed_by,
            has_close_any=has_close_any,
            state=ticket.state,
        ):
            return False
        text = reason.strip()
        if not text:
            return False
        moment = datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket.id)
            if row is None:
                return False
            if not may_edit_reason(
                actor_id=actor.id,
                author_id=row.owner_id,
                closed_by=row.closed_by,
                has_close_any=has_close_any,
                state=row.state,
            ):
                return False
            row.close_reason = text
            session.add(_event_row(ticket, kind=TicketEventKind.REASON, actor_id=actor.id, note=text, now=moment))
        await self._sync_log_card(guild, ticket.id)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor.id,
            actor_rank=rank,
            action=ACTION_TICKET_REASON,
            target=_staff_target(ticket),
            ref=ticket.id,
            details=text,
        )
        return True

    async def transcript_file(
        self,
        guild: discord.Guild,
        ref: int | str,
        actor: discord.Member,
    ) -> tuple[bytes, str] | None:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return None
        rank, _, _ = await self._actor_flags(guild, actor)
        if not rank_may_see(
            rank,
            visible_rank_set(ticket.visible_ranks, ticket.category_kind),
            is_administrator=_is_admin(actor),
        ):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(TicketTranscript, ticket.id)
            if row is None:
                return None
            body = row.body
        return body.encode("utf-8"), copy.ticket_transcript_name(ticket.number)

    async def guard_ticket_dm(self, ref: int | str, *, owner: str = "author") -> GuardResource | None:
        ticket = await self._load_any(ref)
        return None if ticket is None else _guard_resource(ticket, owner=owner)

    async def resolve_guild(self, ref: int | str) -> Any:
        ticket = await self._load_any(ref)
        if ticket is None:
            return None
        getter = getattr(self._bot, "get_guild", None)
        return None if not callable(getter) else getter(ticket.guild_id)

    async def guard_category(self, guild_id: int, ref: int | str) -> GuardResource | None:
        try:
            category_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(TicketCategory, category_id)
            if row is None or row.guild_id != guild_id:
                return None
            return GuardResource(ref=str(row.id), guild_id=row.guild_id)

    async def card_for(self, guild: discord.Guild, ref: int | str) -> discord.ui.LayoutView | None:
        ticket = await self._load(guild.id, ref)
        if ticket is None:
            return None
        return ticket_card_view(await self._build_card(ticket))

    async def relay_dm(self, message: discord.Message) -> bool:
        if is_staff_relay_skip(message):
            return False
        ticket = await self._open_anonymous_for(int(message.author.id))
        if ticket is None:
            return False
        guild = self._guild_of(ticket.guild_id)
        channel = None if guild is None else self._ticket_channel(guild, ticket.channel_id)
        if channel is None or not callable(getattr(channel, "send", None)):
            return False
        body, files = await self._relay_payload(message)
        # Замок, не дребезг: два сообщения подряд доходят оба и в исходном порядке.
        async with self._relay_lock(("ticket-anon-in", ticket.id)):
            posted = await self._send_relay_pages(channel, ticket_anon_relay_view, body, files)
        if posted:
            await self.touch_activity(ticket.guild_id, ticket.channel_id)
        return posted

    async def relay_staff(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or is_staff_relay_skip(message):
            return False
        ticket = await self.ticket_by_channel(guild.id, message.channel.id)
        if ticket is None or not ticket.anonymous or message.author.id == ticket.author_id:
            return False
        actor = message.author
        getter = getattr(guild, "get_member", None)
        found = None if not callable(getter) else getter(int(getattr(actor, "id", 0) or 0))
        if found is not None:
            actor = found
        rank, _, _ = await self._actor_flags(guild, actor)
        if rank is Rank.NONE and not await self.actor_is_staff(guild, actor):
            return False
        body, _files = await self._relay_payload(message, files=False)
        async with self._relay_lock(("ticket-anon-out", ticket.id)):
            sent = await self._dm_staff_relay(guild, ticket, rank, body)
        if sent:
            return True
        await self._warn_dm_blocked(guild, ticket, actor.id)
        return False

    async def ticket_by_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        include_closed: bool = False,
    ) -> StoredTicket | None:
        async with session_scope(self._bot.sessions) as session:
            query = select(Ticket).where(Ticket.guild_id == guild_id, Ticket.channel_id == channel_id)
            if not include_closed:
                query = query.where(Ticket.state != TicketState.CLOSED)
            else:
                query = query.order_by(Ticket.id.desc())
            row = await session.scalar(query)
            if row is None:
                return None
            category = await session.get(TicketCategory, row.category_id)
            return _to_stored(row, None if category is None else _stored_category(category))

    async def refresh_card(self, guild: discord.Guild, ticket_id: int) -> None:
        ticket = await self._load(guild.id, ticket_id)
        if ticket is None:
            return
        channel = self._ticket_channel(guild, ticket.channel_id)
        fetch = None if channel is None else getattr(channel, "fetch_message", None)
        async with session_scope(self._bot.sessions) as session:
            live = await find_live_panel(session, guild_id=guild.id, kind=PANEL_KIND, ref=ticket_id)
        if live is None or not callable(fetch):
            return
        try:
            message = await fetch(live.message_id)
            await edit_card(message, ticket_card_view(await self._build_card(ticket)))
        except discord.HTTPException as exc:
            log.warning("ticket_card_refresh_failed", ticket_id=ticket_id, error=str(exc))

    async def _announce_opened(self, member: discord.Member, channel: Any, ticket: StoredTicket) -> None:
        stages = (
            copy.TICKET_STAGE_LIMIT,
            copy.TICKET_STAGE_CHANNEL,
            copy.TICKET_STAGE_SAVE,
            copy.TICKET_STAGE_CARD,
        )
        registry = getattr(self._bot, "motion", None)
        try:
            placeholder = panel(copy.TICKET_OPEN_TITLE, tone=Tone.RUNNING, timeout=None)
            message = await self._discord(
                send_card(channel, placeholder)
            )
        except discord.HTTPException as exc:
            log.warning("ticket_open_card_failed", guild_id=member.guild.id, ticket_id=ticket.id, error=str(exc))
            return
        progress = Progress(
            MessageTarget(message),
            title=copy.TICKET_OPEN_TITLE,
            show_after=0,
            min_interval=0,
            registry=registry if hasattr(registry, "render") else None,
        )
        async with progress:
            for name in stages[:-1]:
                await progress.stage(name, StageState.DONE)
            await progress.stage(stages[-1], StageState.RUNNING)
            await progress.finish(ticket_card_view(await self._build_card(ticket)))
        async with session_scope(self._bot.sessions) as session:
            await record_panel(
                session,
                kind=PANEL_KIND,
                guild_id=member.guild.id,
                channel_id=int(getattr(channel, "id", 0)),
                message_id=message.id,
                ref=ticket.id,
                created_by=member.id,
            )
        await self._greet_author(member, channel, ticket)

    async def _greet_author(self, member: discord.Member, channel: Any, ticket: StoredTicket) -> None:
        if ticket.anonymous:
            if ticket.dm_channel_id is None:
                await self._dm_author(member.guild, ticket, copy.ticket_dm_open(ticket.number))
            return
        appeal = parse_kind(ticket.category_kind) is CategoryKind.APPEAL
        view = ticket_welcome_view(ticket.author_id, ticket.category_name, appeal=appeal)
        try:
            await self._discord(send_card(channel, view, mentions=going_mentions((ticket.author_id,))))
        except discord.HTTPException as exc:
            log.warning("ticket_welcome_failed", ticket_id=ticket.id, error=str(exc))

    async def _create_destination(
        self,
        guild: discord.Guild,
        member: discord.Member,
        category_name: str,
        number: int,
        settings: TicketSettings,
        *,
        panel_channel_id: int,
        visible: frozenset[Rank] | None = None,
        anonymous: bool = False,
        deny_user_ids: tuple[int, ...] = (),
    ) -> tuple[Any, int] | None:
        name = ticket_channel_name(
            number,
            category_name,
            ticket_channel_author(member.display_name, anonymous=anonymous),
        )
        if settings.open_mode == TicketOpenMode.THREAD:
            parent = guild.get_channel(panel_channel_id)
            create_thread = None if parent is None else getattr(parent, "create_thread", None)
            if not callable(create_thread):
                return None
            try:
                thread = await self._discord(
                    create_thread(
                        name=name,
                        type=discord.ChannelType.private_thread,
                        invitable=False,
                        reason=copy.actor_reason(member.id, _CREATE_REASON),
                    )
                )
                if not anonymous:
                    await self._discord(thread.add_user(member))
            except discord.HTTPException as exc:
                log.warning("ticket_thread_failed", guild_id=guild.id, error=str(exc))
                return None
            return thread, thread.id
        snapshot = await cached_snapshot(self._bot, guild.id)
        overwrites = _overwrites(
            guild,
            member,
            snapshot,
            visible=visible,
            include_author=not anonymous,
            deny_user_ids=deny_user_ids,
        )
        category = None if settings.discord_category_id is None else guild.get_channel(settings.discord_category_id)
        if category is not None and not isinstance(category, discord.CategoryChannel):
            category = None
        hold = await hold_for(self._bot, guild.id, BUCKET_CHANNEL)
        if hold is not None:
            log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            return None
        try:
            with lane(Lane.USER):
                channel = await self._discord(
                    guild.create_text_channel(
                        name,
                        category=category,
                        overwrites=overwrites,
                        reason=copy.actor_reason(member.id, _CREATE_REASON),
                    )
                )
        except (discord.HTTPException, discord.RateLimited) as exc:
            hold = trip(self._bot, guild.id, BUCKET_CHANNEL, exc)
            if hold is not None:
                log_refused(guild.id, BUCKET_CHANNEL, hold, action="channel")
            log.warning("ticket_channel_failed", guild_id=guild.id, error=str(exc))
            return None
        note_success(self._bot, guild.id, BUCKET_CHANNEL)
        return channel, channel.id

    async def _insert_ticket(
        self,
        guild_id: int,
        *,
        number: int,
        owner_id: int,
        category_id: int,
        panel_id: int,
        channel_id: int,
        category: StoredCategory | str,
        form_answers: str = "",
        subject_user_id: int | None = None,
        anonymous: bool = False,
        dm_channel_id: int | None = None,
    ) -> StoredTicket | None:
        try:
            return await self._insert_ticket_row(
                guild_id,
                number=number,
                owner_id=owner_id,
                category_id=category_id,
                panel_id=panel_id,
                channel_id=channel_id,
                category=category,
                form_answers=form_answers,
                subject_user_id=subject_user_id,
                anonymous=anonymous,
                dm_channel_id=dm_channel_id,
            )
        except IntegrityError:
            # Номер уже занят (гонка вне замка или чужая запись) — вызывающий удалит канал.
            log.warning("ticket_number_taken", guild_id=guild_id, number=number)
            return None

    async def _insert_ticket_row(
        self,
        guild_id: int,
        *,
        number: int,
        owner_id: int,
        category_id: int,
        panel_id: int,
        channel_id: int,
        category: StoredCategory | str,
        form_answers: str,
        subject_user_id: int | None,
        anonymous: bool,
        dm_channel_id: int | None,
    ) -> StoredTicket:
        async with session_scope(self._bot.sessions) as session:
            row = Ticket(
                guild_id=guild_id,
                number=number,
                owner_id=owner_id,
                category_id=category_id,
                panel_id=panel_id,
                state=TicketState.OPEN,
                claimed_rank=0,
                channel_id=channel_id,
                created_at=datetime.now(UTC),
                last_activity_at=datetime.now(UTC),
                form_answers=form_answers,
                priority=TicketPriority.NORMAL,
                staff_notes="[]",
                subject_user_id=subject_user_id,
                anonymous=anonymous,
                dm_channel_id=dm_channel_id,
            )
            session.add(row)
            await session.flush()
            stored = _to_stored(row, category)
        self._forget_live_channels()
        return stored

    async def _mark_closed(
        self,
        ticket_id: int,
        actor_id: int,
        reason: str | None,
        outcome: Outcome,
        *,
        now: datetime | None = None,
    ) -> bool:
        text = None if reason is None else reason.strip() or None
        moment = now or datetime.now(UTC)
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id, Ticket.state != TicketState.CLOSED)
                .values(
                    state=TicketState.CLOSED,
                    closed_at=moment,
                    closed_by=actor_id,
                    close_reason=text,
                    outcome=outcome,
                    close_requested_at=None,
                )
            )
            changed = int(result.rowcount or 0) > 0
        if changed:
            self._forget_live_channels()
        return changed

    async def _set_claim(
        self, ticket_id: int, user_id: int | None, rank: int, state: str, *, expect: int | None
    ) -> bool:
        """Условный UPDATE: срабатывает, только если `claimed_by` всё ещё `expect`.

        Двое нажали «Взять» одновременно — второй получает False и TAKEN, а не тихую перезапись.
        """
        async with session_scope(self._bot.sessions) as session:
            guard = Ticket.claimed_by.is_(None) if expect is None else Ticket.claimed_by == int(expect)
            result = await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id, Ticket.state != TicketState.CLOSED, guard)
                .values(claimed_by=user_id, claimed_rank=rank, state=state)
            )
            return int(result.rowcount or 0) > 0

    async def _load(self, guild_id: int, ref: int | str) -> StoredTicket | None:
        ticket = await self._load_any(ref)
        if ticket is None or ticket.guild_id != guild_id:
            return None
        return ticket

    async def _load_any(self, ref: int | str) -> StoredTicket | None:
        try:
            ticket_id = int(ref)
        except (TypeError, ValueError):
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket_id)
            if row is None:
                return None
            category = await session.get(TicketCategory, row.category_id)
            return _to_stored(row, None if category is None else _stored_category(category))

    async def _managed(self, guild: discord.Guild, ref: int | str, actor: discord.Member) -> StoredTicket | None:
        ticket = await self._load(guild.id, ref)
        if ticket is None or _inactive(ticket):
            return None
        _, has_close_any, _ = await self._actor_flags(guild, actor)
        if (
            may_manage_members(
                actor_id=actor.id,
                claimed_by=ticket.claimed_by,
                has_close_any=has_close_any,
                closed=_inactive(ticket),
            )
            is not ClaimVerdict.OK
        ):
            return None
        return ticket

    async def _may_close(self, guild: discord.Guild, ticket: StoredTicket, actor: discord.Member) -> CloseVerdict:
        rank, has_close_any, has_close_own = await self._actor_flags(guild, actor)
        settings = await self._settings_for(ticket.panel_id)
        verdict = may_close(
            actor_id=actor.id,
            author_id=ticket.author_id,
            claimed_by=ticket.claimed_by,
            actor_rank=rank,
            claimed_rank=claimed_rank_value(ticket.claimed_rank),
            has_close_any=has_close_any,
            has_close_own=has_close_own,
            author_may_close=settings.author_may_close,
            closed=_inactive(ticket),
            close_requested=ticket.close_requested_at is not None,
        )
        if verdict is not CloseVerdict.OK:
            return verdict
        if actor.id != ticket.author_id and not rank_may_see(
            rank,
            visible_rank_set(ticket.visible_ranks, ticket.category_kind),
            is_administrator=_is_admin(actor),
        ):
            return CloseVerdict.HIDDEN
        return CloseVerdict.OK

    async def _actor_flags(self, guild: discord.Guild, actor: discord.Member) -> tuple[Rank, bool, bool]:
        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(role.id for role in getattr(actor, "roles", ()) or ())
        is_admin = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))
        rank = resolve_rank(role_ids, snapshot.rank_roles)
        close_any = decide(snapshot, key=TICKETS_CLOSE_ANY, member_role_ids=role_ids, is_administrator=is_admin).allowed
        close_own = decide(snapshot, key=TICKETS_CLOSE_OWN, member_role_ids=role_ids, is_administrator=is_admin).allowed
        return rank, close_any, close_own

    async def _settings_for(self, panel_id: int) -> TicketSettings:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(TicketPanel, panel_id)
            if row is None:
                return TicketSettings()
            return parse_ticket_settings(row.settings)

    async def _latest_panel(self, session: AsyncSession, guild_id: int) -> TicketPanel | None:
        return await session.scalar(
            select(TicketPanel).where(TicketPanel.guild_id == guild_id).order_by(TicketPanel.id.desc())
        )

    async def _store_transcript(self, ticket: StoredTicket, payload: bytes, *, now: datetime) -> None:
        text = payload.decode("utf-8")
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(TicketTranscript, ticket.id)
            if row is None:
                session.add(
                    TicketTranscript(
                        ticket_id=ticket.id,
                        guild_id=ticket.guild_id,
                        body=text,
                        created_at=now,
                    )
                )
                return
            row.body = text
            row.created_at = now
            row.guild_id = ticket.guild_id

    async def _save_log_message(self, ticket_id: int, message_id: int) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket_id)
            if row is not None:
                row.log_message_id = message_id

    async def _sync_log_card(self, guild: discord.Guild, ticket_id: int, *, force: bool = False) -> None:
        ticket = await self._load(guild.id, ticket_id)
        if ticket is None:
            return
        settings = await self._settings_for(ticket.panel_id)
        if settings.log_channel_id is None:
            return
        channel = guild.get_channel(settings.log_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        view = ticket_log_card_view(_log_card(ticket))
        message = None
        if ticket.log_message_id:
            fetch = getattr(channel, "fetch_message", None)
            if callable(fetch):
                try:
                    message = await self._discord(fetch(ticket.log_message_id))
                except discord.NotFound:
                    message = None
                except discord.HTTPException as exc:
                    log.warning("ticket_log_fetch_failed", ticket_id=ticket.id, error=str(exc))
                    message = None
        if message is not None:
            edit = getattr(message, "edit", None)
            if callable(edit):
                try:
                    await self._discord(edit_card(message, view))
                    return
                except discord.HTTPException as exc:
                    log.warning("ticket_log_edit_failed", ticket_id=ticket.id, error=str(exc))
        should_post = force or ticket.state in _LOG_CREATE_STATES or ticket.log_message_id is not None
        if not should_post:
            return
        try:
            sent = await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("ticket_log_send_failed", ticket_id=ticket.id, error=str(exc))
            return
        mid = getattr(sent, "id", None)
        if mid:
            await self._save_log_message(ticket.id, int(mid))

    async def _panel_for_channel(self, session: AsyncSession, guild_id: int, channel_id: int) -> TicketPanel | None:
        return await session.scalar(
            select(TicketPanel).where(TicketPanel.guild_id == guild_id, TicketPanel.channel_id == channel_id)
        )

    async def _seed_categories(self, session: AsyncSession, panel_row: TicketPanel) -> None:
        existing = (await session.scalars(select(TicketCategory).where(TicketCategory.panel_id == panel_row.id))).all()
        have = {row.name for row in existing}
        position = max((row.position for row in existing), default=-1) + 1
        added = False
        for spec in DEFAULT_CATEGORY_SPECS:
            if spec.name in have:
                continue
            session.add(_category_from_spec(panel_row, spec, position))
            position += 1
            added = True
        if added:
            await session.flush()

    async def _appeal_category(self, session: AsyncSession, panel_row: TicketPanel) -> TicketCategory | None:
        row = await session.scalar(
            select(TicketCategory).where(
                TicketCategory.panel_id == panel_row.id,
                TicketCategory.kind == CategoryKind.APPEAL,
            )
        )
        if row is not None:
            return row
        spec = next((item for item in DEFAULT_CATEGORY_SPECS if item.kind is CategoryKind.APPEAL), None)
        if spec is None:
            return None
        position = (
            int(
                await session.scalar(
                    select(func.max(TicketCategory.position)).where(TicketCategory.panel_id == panel_row.id)
                )
                or -1
            )
            + 1
        )
        row = _category_from_spec(panel_row, spec, position)
        session.add(row)
        await session.flush()
        return row

    async def _discard_ticket(self, ticket_id: int) -> None:
        """Строка тикета, у которого так и не появилось канала: без неё лимиты и автозакрытие не врут."""
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, int(ticket_id))
            if row is not None:
                await session.delete(row)
        self._forget_live_channels()

    async def _bind_appeal_ticket(self, case_id: int, ticket_id: int) -> bool:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(ModCase, int(case_id))
            if row is None:
                return False
            if row.appeal_ticket_id is not None:
                return False
            row.appeal_ticket_id = int(ticket_id)
        return True

    async def _case_for_ticket(self, ticket_id: int) -> StoredCase | None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(select(ModCase).where(ModCase.appeal_ticket_id == int(ticket_id)))
            return None if row is None else _mod_case(row)

    async def _dm_explain(self, guild: discord.Guild, user_id: int, ticket_id: int) -> None:
        bot_user = getattr(self._bot, "user", None)
        if bot_user is not None and int(getattr(bot_user, "id", 0) or 0) == int(user_id):
            return
        member = _guild_member(guild, user_id)
        target = member
        if target is None:
            fetch = getattr(self._bot, "fetch_user", None)
            if callable(fetch):
                try:
                    target = await self._discord(fetch(int(user_id)))
                except discord.HTTPException:
                    return
        send = getattr(target, "send", None)
        if not callable(send):
            return
        try:
            await self._discord(send_card(target, explain_dm_view(ticket_id)))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("appeal_explain_dm_failed", user_id=user_id, error=str(exc))

    def _bot_guild(self, guild_id: int) -> Any | None:
        getter = getattr(self._bot, "get_guild", None)
        return None if not callable(getter) else getter(int(guild_id))

    async def _categories(self, session: AsyncSession, panel_id: int) -> list[tuple[int, str]]:
        rows = (
            await session.scalars(
                select(TicketCategory)
                .where(TicketCategory.panel_id == panel_id)
                .order_by(TicketCategory.position, TicketCategory.id)
            )
        ).all()
        return [(row.id, row.name) for row in rows if not bool(getattr(row, "hidden", False))]

    async def _guild_open_count(self, session: AsyncSession, guild_id: int) -> int:
        value = await session.scalar(
            select(func.count()).where(Ticket.guild_id == guild_id, Ticket.state != TicketState.CLOSED)
        )
        return int(value or 0)

    async def _author_open_count(self, session: AsyncSession, guild_id: int, owner_id: int) -> int:
        value = await session.scalar(
            select(func.count()).where(
                Ticket.guild_id == guild_id,
                Ticket.owner_id == owner_id,
                Ticket.state != TicketState.CLOSED,
            )
        )
        return int(value or 0)

    async def _transcript(self, channel: Any, ticket: StoredTicket) -> tuple[bytes, int]:
        author = (
            copy.TICKET_ANON_TRANSCRIPT
            if ticket.anonymous
            else copy.ticket_author_value(ticket.author_id, anonymous=False)
        )
        header = copy.ticket_transcript_header(
            number=ticket.number,
            category=ticket.category_name,
            author=author,
            claimer=copy.ticket_claimer_value(ticket.claimed_by),
            opened=transcript_stamp(ticket.created_at),
            closed=transcript_stamp(ticket.closed_at),
            outcome=copy.ticket_outcome_label(ticket.outcome),
            reason=ticket.close_reason or "—",
        )
        form = format_form_transcript(parse_form_answers(ticket.form_answers))
        if form:
            header = f"{header}\n\n{form}"
        text, count = await collect_transcript(
            channel,
            header=header,
            anonymous_author_id=ticket.author_id if ticket.anonymous else None,
        )
        return text.encode("utf-8"), count

    async def _send_transcript(
        self,
        guild: discord.Guild,
        ticket: StoredTicket,
        payload: bytes,
        count: int,
        archive: Any,
    ) -> None:
        if archive is None or not hasattr(archive, "send"):
            log.warning("ticket_archive_missing", guild_id=guild.id, ticket_id=ticket.id)
            return
        answers = parse_form_answers(ticket.form_answers)
        cards = upload_cards(
            copy.TICKET_TRANSCRIPT_TITLE,
            copy.ticket_form_body(answers)
            or copy.ticket_card_title(ticket.number, ticket.category_name, anonymous=ticket.anonymous),
            filename=copy.ticket_transcript_name(ticket.number),
            data=payload,
            limit=upload_limit(guild),
            tone=Tone.MUTED,
            fields={
                copy.TICKET_FIELD_AUTHOR: copy.ticket_author_value(ticket.author_id, anonymous=ticket.anonymous),
                copy.TICKET_FIELD_CLAIMER: copy.ticket_claimer_value(ticket.claimed_by),
                copy.TICKET_FIELD_OPENED: _archive_when(ticket.created_at),
                copy.TICKET_FIELD_CLOSED: _archive_when(ticket.closed_at),
                copy.TICKET_FIELD_OUTCOME: copy.ticket_outcome_label(ticket.outcome),
                copy.TICKET_FIELD_REASON: ticket.close_reason or "—",
            },
            timeout=None,
        )
        if count >= TRANSCRIPT_PROGRESS_AFTER:
            placeholder = panel(copy.TICKET_CLOSE_TITLE, tone=Tone.RUNNING, timeout=None)
            try:
                message = await self._discord(send_card(archive, placeholder))
            except discord.HTTPException:
                message = None
            if message is not None:
                progress = Progress(
                    MessageTarget(message),
                    title=copy.TICKET_CLOSE_TITLE,
                    total=count,
                    show_after=0,
                    min_interval=0,
                )
                async with progress:
                    await progress.step(count, total=count, messages=count)
                    await progress.finish(cards[0])
                for extra in cards[1:]:
                    try:
                        await self._discord(send_card(archive, extra))
                    except discord.HTTPException as exc:
                        log.warning("ticket_transcript_failed", ticket_id=ticket.id, error=str(exc))
                        return
                return
        try:
            for card in cards:
                await self._discord(send_card(archive, card))
        except discord.HTTPException as exc:
            log.warning("ticket_transcript_failed", ticket_id=ticket.id, error=str(exc))

    async def _archive_channel(self, guild: discord.Guild, panel_id: int) -> Any:
        settings = await self._settings_for(panel_id)
        if settings.archive_channel_id is None:
            return None
        return guild.get_channel(settings.archive_channel_id)

    def _ticket_channel(self, guild: discord.Guild, channel_id: int) -> Any:
        found = guild.get_channel(channel_id)
        if found is not None:
            return found
        getter = getattr(guild, "get_thread", None)
        if callable(getter):
            return getter(channel_id)
        return None

    async def _safe_delete(self, channel: Any) -> None:
        delete = getattr(channel, "delete", None)
        if not callable(delete):
            return
        try:
            await self._discord(delete(reason=_CLOSE_REASON))
        except discord.HTTPException as exc:
            log.warning("ticket_delete_failed", error=str(exc))

    async def _lock_closed_channel(self, guild: discord.Guild, ticket: StoredTicket, channel: Any) -> None:
        author = None if ticket.anonymous else _guild_member(guild, ticket.author_id)
        setter = getattr(channel, "set_permissions", None)
        if callable(setter) and author is not None:
            try:
                await self._discord(
                    setter(
                        author,
                        view_channel=True,
                        send_messages=False,
                        read_message_history=True,
                        reason=_CLOSE_REASON,
                    )
                )
            except discord.HTTPException as exc:
                log.warning("ticket_lock_failed", ticket_id=ticket.id, error=str(exc))
        editor = getattr(channel, "edit", None)
        if callable(editor) and getattr(channel, "type", None) in {
            discord.ChannelType.private_thread,
            discord.ChannelType.public_thread,
        }:
            try:
                await self._discord(editor(locked=True, reason=_CLOSE_REASON))
            except discord.HTTPException as exc:
                log.warning("ticket_thread_lock_failed", ticket_id=ticket.id, error=str(exc))

    async def _unlock_reopened_channel(self, guild: discord.Guild, ticket: StoredTicket, channel: Any) -> None:
        author = None if ticket.anonymous else _guild_member(guild, ticket.author_id)
        setter = getattr(channel, "set_permissions", None)
        if callable(setter) and author is not None:
            try:
                await self._discord(
                    setter(
                        author,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        reason=_CREATE_REASON,
                    )
                )
            except discord.HTTPException as exc:
                log.warning("ticket_unlock_failed", ticket_id=ticket.id, error=str(exc))
        editor = getattr(channel, "edit", None)
        if callable(editor) and getattr(channel, "type", None) in {
            discord.ChannelType.private_thread,
            discord.ChannelType.public_thread,
        }:
            try:
                await self._discord(editor(locked=False, archived=False, reason=_CREATE_REASON))
            except discord.HTTPException as exc:
                log.warning("ticket_thread_unlock_failed", ticket_id=ticket.id, error=str(exc))

    async def _build_card(self, ticket: StoredTicket, *, now: datetime | None = None) -> TicketCard:
        used = await self._used_appeal_levels(ticket.id)
        return _card(ticket, used_levels=used, now=now or datetime.now(UTC))

    async def _used_appeal_levels(self, ticket_id: int) -> frozenset[str]:
        async with session_scope(self._bot.sessions) as session:
            kinds = (await session.scalars(select(TicketEvent.kind).where(TicketEvent.ticket_id == ticket_id))).all()
        return used_appeal_levels(kinds)

    async def _record_event(
        self,
        ticket: StoredTicket,
        *,
        kind: str,
        actor_id: int,
        note: str = "",
        now: datetime | None = None,
    ) -> None:
        async with session_scope(self._bot.sessions) as session:
            session.add(_event_row(ticket, kind=kind, actor_id=actor_id, note=note, now=now))

    async def _flow_role(
        self,
        guild: discord.Guild,
        ticket: StoredTicket,
        actor: discord.Member,
        action: FlowAction,
    ) -> FlowRole:
        if action is FlowAction.APPEAL:
            return FlowRole.AUTHOR if actor.id == ticket.author_id else FlowRole.OTHER
        if action is FlowAction.APPROVE_REQ or (action is FlowAction.REJECT and is_working(ticket.state)):
            if ticket.claimed_by == actor.id:
                return FlowRole.CLAIMER
            if await self._has_key(guild, actor, TICKETS_CLOSE_ANY):
                return FlowRole.CLOSE_ANY
            return FlowRole.OTHER
        pending_reject = action is FlowAction.REJECT and ticket.state == TicketState.PENDING_APPROVAL
        if action is FlowAction.APPROVE or pending_reject:
            if await self._has_key(guild, actor, TICKETS_APPROVE):
                return FlowRole.SPECIAL
            return FlowRole.OTHER
        if action in {FlowAction.UPHOLD, FlowAction.OVERTURN}:
            if ticket.state == TicketState.APPEAL_OWNER:
                return FlowRole.OWNER if actor.id == int(getattr(guild, "owner_id", 0) or 0) else FlowRole.OTHER
            rank, _, _ = await self._actor_flags(guild, actor)
            if rank is Rank.SPECIAL or await self._has_key(guild, actor, TICKETS_APPROVE):
                return FlowRole.SPECIAL
            if rank >= Rank.CHIEF:
                return FlowRole.CHIEF
            return FlowRole.OTHER
        return FlowRole.OTHER

    async def _apply_review_overwrites(
        self,
        guild: discord.Guild,
        ticket: StoredTicket,
        channel: Any,
        *,
        include_owner: bool,
    ) -> None:
        setter = getattr(channel, "set_permissions", None)
        if callable(setter):
            author = None if ticket.anonymous else _guild_member(guild, ticket.author_id)
            if author is not None:
                try:
                    await self._discord(
                        setter(
                            author,
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            reason=_CREATE_REASON,
                        )
                    )
                except discord.HTTPException as exc:
                    log.warning("ticket_appeal_author_failed", ticket_id=ticket.id, error=str(exc))
            snapshot = await cached_snapshot(self._bot, guild.id)
            seen: set[int] = set()
            for rank in (Rank.CHIEF, Rank.SPECIAL):
                for role_id in snapshot.rank_roles.get(rank, ()):
                    if role_id in seen:
                        continue
                    seen.add(role_id)
                    role = guild.get_role(role_id)
                    if role is None:
                        continue
                    try:
                        await self._discord(
                            setter(
                                role,
                                view_channel=True,
                                send_messages=True,
                                read_message_history=True,
                                reason=_CREATE_REASON,
                            )
                        )
                    except discord.HTTPException as exc:
                        log.warning("ticket_appeal_role_failed", ticket_id=ticket.id, error=str(exc))
            if include_owner:
                owner_id = int(getattr(guild, "owner_id", 0) or 0)
                owner = None if owner_id <= 0 or not hasattr(guild, "get_member") else guild.get_member(owner_id)
                if owner is None:
                    owner = getattr(guild, "owner", None)
                if owner is not None:
                    try:
                        await self._discord(
                            setter(
                                owner,
                                view_channel=True,
                                send_messages=True,
                                read_message_history=True,
                                reason=_CREATE_REASON,
                            )
                        )
                    except discord.HTTPException as exc:
                        log.warning("ticket_appeal_owner_failed", ticket_id=ticket.id, error=str(exc))
        editor = getattr(channel, "edit", None)
        if callable(editor) and getattr(channel, "type", None) in {
            discord.ChannelType.private_thread,
            discord.ChannelType.public_thread,
        }:
            try:
                await self._discord(editor(locked=False, archived=False, reason=_CREATE_REASON))
            except discord.HTTPException as exc:
                log.warning("ticket_appeal_thread_failed", ticket_id=ticket.id, error=str(exc))

    async def _dm_author(self, guild: discord.Guild, ticket: StoredTicket, body: str) -> bool:
        return await self._send_author_dm(
            guild,
            ticket,
            panel(copy.TICKET_TITLE, body, tone=Tone.INFO, timeout=None),
        )

    async def _dm_closed(self, guild: discord.Guild, ticket: StoredTicket) -> bool:
        return await self._send_author_dm(guild, ticket, ticket_closed_dm_view(await self._build_card(ticket)))

    async def _probe_anon_dm(self, member: discord.Member, number: int) -> int | None:
        send = getattr(member, "send", None)
        if not callable(send):
            return None
        try:
            message = await self._discord(
                send_card(member, panel(copy.TICKET_TITLE, copy.ticket_dm_open(number), tone=Tone.INFO, timeout=None))
            )
        except discord.Forbidden:
            return None
        except discord.HTTPException as exc:
            log.warning("ticket_anon_dm_failed", user_id=member.id, error=str(exc))
            return None
        channel = getattr(message, "channel", None)
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            channel_id = getattr(getattr(member, "dm_channel", None), "id", None)
        return 0 if channel_id is None else int(channel_id)

    async def _send_author_dm(self, guild: discord.Guild, ticket: StoredTicket, view: Any) -> bool:
        member = _guild_member(guild, ticket.author_id)
        send = None if member is None else getattr(member, "send", None)
        if not callable(send):
            return False
        try:
            await self._discord(send_card(member, view))
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            log.warning("ticket_dm_failed", ticket_id=ticket.id, error=str(exc))
            return False
        return True

    async def _dm_staff_relay(self, guild: discord.Guild, ticket: StoredTicket, rank: Rank, body: str) -> bool:
        member = _guild_member(guild, ticket.author_id)
        if member is None or not callable(getattr(member, "send", None)):
            return False
        try:
            return await self._send_relay_pages(
                member,
                lambda text, files=(): ticket_staff_relay_view(rank.key, text),
                body,
                (),
            )
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            log.warning("ticket_staff_relay_failed", ticket_id=ticket.id, error=str(exc))
            return False

    async def _warn_dm_blocked(self, guild: discord.Guild, ticket: StoredTicket, actor_id: int) -> None:
        last = await self._last_event_at(ticket.id, TicketEventKind.DM_CLOSED)
        if not dm_fail_due(last, datetime.now(UTC)):
            return
        channel = self._ticket_channel(guild, ticket.channel_id)
        send = None if channel is None else getattr(channel, "send", None)
        if not callable(send):
            return
        try:
            await self._discord(
                send_card(channel, panel(copy.TICKET_TITLE, copy.TICKET_DM_BLOCKED, tone=Tone.WARN, timeout=None))
            )
        except discord.HTTPException as exc:
            log.warning("ticket_dm_blocked_failed", ticket_id=ticket.id, error=str(exc))
            return
        await self._record_event(ticket, kind=TicketEventKind.DM_CLOSED, actor_id=actor_id)

    async def _last_event_at(self, ticket_id: int, kind: str) -> datetime | None:
        async with session_scope(self._bot.sessions) as session:
            return await session.scalar(
                select(func.max(TicketEvent.created_at)).where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.kind == kind,
                )
            )

    async def _open_anonymous_for(self, owner_id: int, *, guild_id: int | None = None) -> StoredTicket | None:
        guilds = tuple(getattr(self._bot, "guilds", ()) or ())
        shared: list[int] = []
        for guild in guilds:
            gid = int(getattr(guild, "id", 0) or 0)
            if not gid:
                continue
            getter = getattr(guild, "get_member", None)
            member = getter(int(owner_id)) if callable(getter) else None
            if member is not None:
                shared.append(gid)
        clauses = [
            Ticket.owner_id == owner_id,
            Ticket.anonymous.is_(True),
            Ticket.state != TicketState.CLOSED,
        ]
        if guild_id is not None:
            if shared and int(guild_id) not in shared:
                return None
            clauses.append(Ticket.guild_id == int(guild_id))
        elif shared:
            clauses.append(Ticket.guild_id.in_(tuple(shared)))
        elif guilds:
            return None
        async with session_scope(self._bot.sessions) as session:
            row = await session.scalar(select(Ticket).where(*clauses).order_by(Ticket.id.desc()))
            if row is None:
                return None
            category = await session.get(TicketCategory, row.category_id)
            return _to_stored(row, None if category is None else _stored_category(category))

    def _relay_lock(self, key: tuple[str, int]) -> asyncio.Lock:
        lock = self._relay_locks.get(key)
        if lock is None:
            lock = self._relay_locks[key] = asyncio.Lock()
        return lock

    def _guild_of(self, guild_id: int) -> Any:
        getter = getattr(self._bot, "get_guild", None)
        return None if not callable(getter) else getter(guild_id)

    async def _relay_payload(self, message: discord.Message, *, files: bool = True) -> tuple[str, tuple[Any, ...]]:
        chunks = [str(getattr(message, "content", None) or "").strip()]
        uploaded: list[Any] = []
        for item in getattr(message, "attachments", None) or ():
            size = int(getattr(item, "size", 0) or 0)
            url = getattr(item, "url", None) or getattr(item, "proxy_url", None)
            if attachment_over_limit(size) or not files:
                if url:
                    chunks.append(str(url))
                continue
            media = await _attachment_file(item)
            if media is not None:
                uploaded.append(media)
            elif url:
                chunks.append(str(url))
        body = "\n".join(part for part in chunks if part)
        return body, tuple(uploaded)

    async def _send_relay_pages(self, target: Any, factory: Any, body: str, files: tuple[Any, ...]) -> bool:
        pages = paginate(body or "—")
        sent = False
        for index, page in enumerate(pages):
            view = factory(page, files=files if index == 0 else ())
            try:
                await self._discord(send_card(target, view))
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                log.warning("ticket_relay_failed", error=str(exc))
                return sent
            sent = True
        return sent

    async def _emit_staff_flow(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        action: str,
        ticket: StoredTicket,
    ) -> None:
        rank, _, _ = await self._actor_flags(guild, actor)
        await emit_staff(
            self._bot,
            guild.id,
            actor_id=actor.id,
            actor_rank=rank,
            action=action,
            target=_staff_target(ticket),
            ref=ticket.id,
        )

    async def _send_review_remind(self, guild: discord.Guild, ticket_id: int, *, now: datetime) -> None:
        async with session_scope(self._bot.sessions) as session:
            row = await session.get(Ticket, ticket_id)
            if row is None or not is_review(row.state):
                return
            last = await session.scalar(
                select(func.max(TicketEvent.created_at)).where(
                    TicketEvent.ticket_id == ticket_id,
                    TicketEvent.kind.in_(
                        (
                            TicketEventKind.APPROVE_REQ,
                            TicketEventKind.APPEAL_CHIEF,
                            TicketEventKind.APPEAL_OWNER,
                            TicketEventKind.REMIND,
                        )
                    ),
                )
            )
            if last is not None and not remind_due(last, now):
                return
            session.add(
                TicketEvent(
                    ticket_id=row.id,
                    guild_id=row.guild_id,
                    kind=TicketEventKind.REMIND,
                    actor_id=0,
                    note="",
                    created_at=now,
                )
            )
            state = row.state
            channel_id = row.channel_id
        channel = self._ticket_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        owner_id = int(getattr(guild, "owner_id", 0) or 0) if state == TicketState.APPEAL_OWNER else 0
        mentions = going_mentions((owner_id,)) if owner_id else discord.AllowedMentions.none()
        view = ticket_review_remind_view(state, owner_id=owner_id or None)
        try:
            await self._discord(send_card(channel, view, mentions=mentions))
        except discord.HTTPException as exc:
            log.warning("ticket_review_remind_failed", ticket_id=ticket_id, error=str(exc))

    async def _has_key(self, guild: discord.Guild, actor: discord.Member, key: str) -> bool:
        snapshot = await cached_snapshot(self._bot, guild.id)
        role_ids = tuple(role.id for role in getattr(actor, "roles", ()) or ())
        is_admin = bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))
        return decide(snapshot, key=key, member_role_ids=role_ids, is_administrator=is_admin).allowed

    async def _duty_seniors(self, guild: discord.Guild) -> list[int]:
        async with session_scope(self._bot.sessions) as session:
            on_duty = await active_duty_ids(session, guild.id)
        snapshot = await cached_snapshot(self._bot, guild.id)
        seniors: list[int] = []
        getter = getattr(guild, "get_member", None)
        for user_id in on_duty:
            member = None if not callable(getter) else getter(user_id)
            if member is None:
                continue
            role_ids = tuple(role.id for role in getattr(member, "roles", ()) or ())
            if resolve_rank(role_ids, snapshot.rank_roles) >= Rank.SENIOR:
                seniors.append(user_id)
        return seniors

    async def _mark_escalated(self, ticket_id: int, now: datetime) -> bool:
        cutoff = now - ESCALATE_COOLDOWN
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.state.in_(tuple(WORKING_STATES)),
                    or_(Ticket.escalated_at.is_(None), Ticket.escalated_at <= cutoff),
                )
                .values(escalated_at=now)
            )
            return int(result.rowcount or 0) > 0

    async def _lifecycle_jobs(self, now: datetime) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        async with session_scope(self._bot.sessions) as session:
            open_rows = (
                await session.execute(
                    select(Ticket, TicketPanel.settings)
                    .join(TicketPanel, Ticket.panel_id == TicketPanel.id)
                    .where(Ticket.state.in_(tuple(WORKING_STATES)))
                )
            ).all()
            review_rows = list(
                (await session.scalars(select(Ticket).where(Ticket.state.in_(tuple(REVIEW_STATES))))).all()
            )
            review_ids = [row.id for row in review_rows]
            last_events: dict[int, datetime] = {}
            if review_ids:
                event_rows = (
                    await session.execute(
                        select(TicketEvent.ticket_id, func.max(TicketEvent.created_at))
                        .where(
                            TicketEvent.ticket_id.in_(review_ids),
                            TicketEvent.kind.in_(
                                (
                                    TicketEventKind.APPROVE_REQ,
                                    TicketEventKind.APPEAL_CHIEF,
                                    TicketEventKind.APPEAL_OWNER,
                                    TicketEventKind.REMIND,
                                )
                            ),
                        )
                        .group_by(TicketEvent.ticket_id)
                    )
                ).all()
                last_events = {int(ticket_id): stamp for ticket_id, stamp in event_rows if stamp is not None}
            # Закрытые с уже снесённым каналом (channel_id = 0) обработаны — их не перечитываем каждую минуту.
            closed_rows = (
                await session.execute(
                    select(Ticket, TicketPanel.settings)
                    .join(TicketPanel, Ticket.panel_id == TicketPanel.id)
                    .where(Ticket.state == TicketState.CLOSED, Ticket.channel_id != CHANNEL_GONE)
                )
            ).all()
        for row, raw_settings in open_rows:
            settings = parse_ticket_settings(raw_settings)
            last = activity_at(row.created_at, row.last_activity_at)
            if last is None:
                continue
            action = autoclose_action(last, now, hours=settings.autoclose_hours, warned_at=row.autoclose_warned_at)
            if action is AutocloseAction.WAIT:
                continue
            jobs.append(
                {
                    "action": action,
                    "guild_id": row.guild_id,
                    "ticket_id": row.id,
                    "deadline": autoclose_deadline(last, settings.autoclose_hours),
                }
            )
        for row in review_rows:
            last = last_events.get(row.id) or activity_at(row.created_at, row.last_activity_at)
            if not remind_due(last, now):
                continue
            jobs.append({"action": "remind", "guild_id": row.guild_id, "ticket_id": row.id, "deadline": last})
        for row, raw_settings in closed_rows:
            settings = parse_ticket_settings(raw_settings)
            if row.closed_at is None:
                continue
            if now <= reopen_until(row.closed_at, settings.reopen_days):
                continue
            jobs.append({"action": "expire", "guild_id": row.guild_id, "ticket_id": row.id, "deadline": row.closed_at})
        return jobs

    async def _send_autoclose_warn(self, guild: discord.Guild, ticket_id: int, deadline: datetime) -> None:
        async with session_scope(self._bot.sessions) as session:
            result = await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.state.in_(tuple(WORKING_STATES)),
                    Ticket.autoclose_warned_at.is_(None),
                )
                .values(autoclose_warned_at=datetime.now(UTC))
            )
            if int(result.rowcount or 0) <= 0:
                return
            row = await session.get(Ticket, ticket_id)
            channel_id = None if row is None else row.channel_id
        channel = None if channel_id is None else self._ticket_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        view = ticket_autoclose_warn_view(deadline)
        try:
            await self._discord(send_card(channel, view))
        except discord.HTTPException as exc:
            log.warning("ticket_autoclose_warn_failed", ticket_id=ticket_id, error=str(exc))

    async def _expire_closed_channel(self, guild: discord.Guild, ticket_id: int) -> None:
        ticket = await self._load(guild.id, ticket_id)
        if ticket is None or not _closed(ticket):
            return
        channel = self._ticket_channel(guild, ticket.channel_id)
        if channel is not None:
            await self._safe_delete(channel)
        async with session_scope(self._bot.sessions) as session:
            await mark_stale_by_ref(session, guild_id=guild.id, kind=PANEL_KIND, ref=ticket_id)
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id, Ticket.state == TicketState.CLOSED)
                .values(channel_id=CHANNEL_GONE)
            )

    async def _discord(self, awaitable: Any) -> Any:
        outbound = getattr(self._bot, "outbound", None)
        if outbound is None:
            return await awaitable
        async with outbound:
            return await awaitable


def _open_limit(bot: Any) -> int:
    settings = getattr(bot, "settings", None)
    value = getattr(settings, "tickets_per_category", DEFAULT_OPEN_LIMIT)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_OPEN_LIMIT
    return max(1, limit)


def _closed(ticket: StoredTicket) -> bool:
    return ticket.state == TicketState.CLOSED


def _is_case_punisher(case: StoredCase | None, actor: object) -> bool:
    if case is None:
        return False
    return int(getattr(actor, "id", 0) or 0) == int(case.created_by)


def _inactive(ticket: StoredTicket) -> bool:
    return not is_working(ticket.state)


def _card(
    ticket: StoredTicket,
    *,
    used_levels: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> TicketCard:
    moment = now or datetime.now(UTC)
    kind = parse_kind(ticket.category_kind)
    working = is_working(ticket.state)
    claimed = ticket.claimed_by is not None
    pending = ticket.state == TicketState.PENDING_APPROVAL
    closed = _closed(ticket)
    appealable = can_appeal(
        kind=kind,
        outcome=ticket.outcome,
        now=moment,
        closed_at=ticket.closed_at,
        used_levels=used_levels,
    )
    next_level = next_appeal_level(kind, used_levels) if appealable else None
    return TicketCard(
        ticket_id=ticket.id,
        number=ticket.number,
        category=ticket.category_name,
        author_id=ticket.author_id,
        claimed_by=ticket.claimed_by,
        created_at=ticket.created_at,
        answers=parse_form_answers(ticket.form_answers),
        priority=parse_priority(ticket.priority),
        closed=closed,
        rating=ticket.rating,
        outcome=ticket.outcome,
        close_reason=ticket.close_reason,
        info_text=ticket.info_text,
        state=ticket.state,
        topic=ticket.topic,
        category_kind=kind,
        show_approve_req=working and claimed and kind is CategoryKind.APPLICATION,
        show_approve=pending,
        show_reject=(working and claimed and kind is CategoryKind.APPLICATION) or pending,
        show_appeal=appealable,
        show_review=ticket.state in {TicketState.APPEAL_CHIEF, TicketState.APPEAL_OWNER},
        appeal_owner=next_level == LEVEL_OWNER or ticket.state == TicketState.APPEAL_OWNER,
        anonymous=ticket.anonymous,
    )


def _to_stored(row: Ticket, category: StoredCategory | str | None) -> StoredTicket:
    if isinstance(category, StoredCategory):
        name = category.name
        kind = category.kind
        info = category.info_text
        ranks = category.visible_ranks
        topic = category.topic
    elif isinstance(category, str):
        name = category
        kind = CategoryKind.GENERAL
        info = ""
        ranks = dump_visible_ranks(frozenset(STAFF_RANKS))
        topic = ""
    else:
        name = ""
        kind = CategoryKind.GENERAL
        info = ""
        ranks = dump_visible_ranks(frozenset(STAFF_RANKS))
        topic = ""
    return StoredTicket(
        guild_id=row.guild_id,
        number=row.number,
        author_id=row.owner_id,
        category_id=row.category_id,
        channel_id=row.channel_id,
        id=row.id,
        panel_id=row.panel_id,
        state=row.state,
        claimed_by=row.claimed_by,
        claimed_rank=row.claimed_rank,
        created_at=row.created_at,
        closed_at=row.closed_at,
        closed_by=row.closed_by,
        close_reason=row.close_reason,
        category_name=name,
        form_answers=row.form_answers or "",
        priority=row.priority or TicketPriority.NORMAL,
        last_activity_at=row.last_activity_at,
        autoclose_warned_at=row.autoclose_warned_at,
        escalated_at=row.escalated_at,
        close_requested_at=row.close_requested_at,
        rating=row.rating,
        staff_notes=row.staff_notes or "[]",
        outcome=row.outcome,
        subject_user_id=row.subject_user_id,
        category_kind=kind,
        info_text=info,
        visible_ranks=ranks,
        topic=topic,
        anonymous=bool(getattr(row, "anonymous", False)),
        dm_channel_id=getattr(row, "dm_channel_id", None),
        log_message_id=getattr(row, "log_message_id", None),
    )


def _stored_category(row: TicketCategory) -> StoredCategory:
    return StoredCategory(
        id=row.id,
        panel_id=row.panel_id,
        guild_id=row.guild_id,
        name=row.name,
        position=row.position,
        hint=row.hint or "",
        form_json=row.form_json or "{}",
        kind=parse_kind(row.kind),
        visible_ranks=row.visible_ranks or dump_visible_ranks(default_visible_ranks(row.kind)),
        topic=row.topic or "",
        info_text=row.info_text or "",
        hidden=bool(row.hidden),
        min_tenure_days=int(row.min_tenure_days or 0),
        min_ladder_step=row.min_ladder_step or Step.GUEST.key,
        excluded_user_field=row.excluded_user_field or "",
    )


def _category_from_spec(panel_row: TicketPanel, spec: CategorySpec, position: int) -> TicketCategory:
    form = TicketForm()
    if spec.form_prompts:
        if spec.kind is CategoryKind.COMPLAINT:
            form = TicketForm(fields=(FormField(prompt=spec.form_prompts[0], required=True, style="short"),))
        else:
            form = collect_form_fields(spec.form_prompts)
    return TicketCategory(
        panel_id=panel_row.id,
        guild_id=panel_row.guild_id,
        name=spec.name,
        position=position,
        kind=spec.kind,
        visible_ranks=dump_visible_ranks(default_visible_ranks(spec.kind)),
        topic=spec.topic,
        info_text=spec.info_text,
        hidden=spec.hidden,
        min_tenure_days=spec.min_tenure_days,
        min_ladder_step=spec.min_ladder_step,
        excluded_user_field=spec.excluded_user_field,
        form_json=dump_ticket_form(form),
    )


def _event_row(
    ticket: StoredTicket,
    *,
    kind: str,
    actor_id: int,
    note: str = "",
    now: datetime | None = None,
) -> TicketEvent:
    return TicketEvent(
        ticket_id=ticket.id,
        guild_id=ticket.guild_id,
        kind=kind,
        actor_id=actor_id,
        note=note,
        created_at=now or datetime.now(UTC),
    )


def actor_stub() -> Any:
    return SimpleNamespace(id=0, roles=(), guild_permissions=SimpleNamespace(administrator=False))


def _is_admin(actor: object) -> bool:
    return bool(getattr(getattr(actor, "guild_permissions", None), "administrator", False))


def _staff_target(ticket: StoredTicket) -> int | None:
    return None if ticket.anonymous else ticket.author_id


def _archive_when(value: datetime | None) -> str:
    if value is None:
        return "—"
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{transcript_stamp(moment)} · {discord.utils.format_dt(moment)}"


def _log_card(ticket: StoredTicket) -> TicketLogCard:
    return TicketLogCard(
        ticket_id=ticket.id,
        number=ticket.number,
        category=ticket.category_name,
        author_id=ticket.author_id,
        created_at=ticket.created_at,
        state=ticket.state,
        closed_by=ticket.closed_by,
        closed_at=ticket.closed_at,
        outcome=ticket.outcome,
        close_reason=ticket.close_reason,
        rating=ticket.rating,
        anonymous=ticket.anonymous,
    )


def _guard_resource(ticket: StoredTicket, *, owner: str) -> GuardResource:
    if owner == "author":
        owner_id = ticket.author_id
    elif owner == "claimer":
        owner_id = ticket.claimed_by
    elif owner == "closer":
        owner_id = ticket.closed_by
    else:
        owner_id = None
    return GuardResource(
        ref=str(ticket.id),
        guild_id=ticket.guild_id,
        owner_id=owner_id,
        closed=_closed(ticket),
        channel_id=ticket.channel_id,
    )


def _guild_member(guild: object, user_id: int) -> Any:
    getter = getattr(guild, "get_member", None)
    return None if not callable(getter) else getter(user_id)


async def _attachment_file(item: object) -> Any | None:
    reader = getattr(item, "read", None)
    name = str(getattr(item, "filename", None) or "file.bin")
    size = int(getattr(item, "size", 0) or 0)
    if attachment_over_limit(size) or not callable(reader):
        return None
    try:
        data = await reader()
    except (discord.HTTPException, OSError, TypeError, ValueError):
        return None
    if not isinstance(data, (bytes, bytearray)):
        return None
    return card_file(name, bytes(data))


def _overwrites(
    guild: discord.Guild,
    member: discord.Member,
    snapshot: AccessSnapshot,
    *,
    visible: frozenset[Rank] | None = None,
    include_author: bool = True,
    deny_user_ids: tuple[int, ...] = (),
) -> dict[Any, discord.PermissionOverwrite]:
    mapping: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if include_author:
        mapping[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )
    me = guild.me
    if me is not None:
        mapping[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        )
    seen: set[int] = set()
    allowed = frozenset(STAFF_RANKS) if visible is None else visible
    for rank in STAFF_RANKS:
        if rank not in allowed:
            continue
        for role_id in snapshot.rank_roles.get(rank, ()):
            if role_id in seen:
                continue
            seen.add(role_id)
            role = guild.get_role(role_id)
            if role is None:
                continue
            mapping[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    for user_id in deny_user_ids:
        mapping[discord.Object(id=int(user_id))] = discord.PermissionOverwrite(view_channel=False)
    return mapping


def _mod_case(row: ModCase) -> StoredCase:
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
