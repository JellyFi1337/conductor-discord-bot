"""Экспорт, забвение, ретенция производных таблиц и снимок SQLite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import structlog
from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conductor.db.models import (
    AchievementGrant,
    AchievementProgress,
    AchievementSource,
    ActivityBucket,
    AutomodHit,
    AutomodRequest,
    Base,
    BugReport,
    ChannelDaily,
    Gathering,
    GatheringRsvp,
    Giveaway,
    GiveawayEntry,
    GiveawayWinner,
    GroupInvite,
    GroupMember,
    GuildConfig,
    InteractionEdge,
    LadderProgress,
    LfgMember,
    LfgPost,
    LoyaltyRegistry,
    LoyaltyRoleEvent,
    MemberChannel,
    MemberDaily,
    MemberRecord,
    MemberWatch,
    ModCase,
    ModEvidence,
    ModIntent,
    Poll,
    PollVote,
    PrivateGroup,
    RecruitLink,
    Reminder,
    Reputation,
    ReputationEvent,
    RoleChange,
    StaffAction,
    Ticket,
    TicketEvent,
    TicketTranscript,
    UnofficialAdminNotice,
    VoiceRoom,
    VoiceRoomKick,
    VoiceRoomLfg,
    VoiceRoomRequest,
    VoiceSession,
    WatcherDuty,
    WatcherNote,
    WatcherSuspension,
)
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.privacy import (
    BACKUP_PERIOD,
    FILTER_NO_CASE,
    FILTER_SNOWFLAKE,
    FILTER_STALE,
    FILTER_VOICE,
    FORGOTTEN_REASON,
    FORGOTTEN_USER_ID,
    KIND_DAYS,
    PURGE_PERIOD,
    RETENTION,
    SETTING_DATA,
    SETTING_TRANSCRIPT,
    USER_LOG_KEEP_DAYS,
    USER_LOG_PURGE_BATCH,
    USER_LOG_PURGE_CAP,
    USER_LOG_PURGE_PAUSE,
    backup_filename,
    backups_to_drop,
    export_filename,
    json_safe,
    retention_cutoff,
    should_drop_user_log_card,
    snowflake_before,
    user_log_cutoff,
    voice_open_cutoff,
)
from conductor.domain.tickets import TICKET_TRANSCRIPT_DAYS, forget_transcript_body
from conductor.presentation import copy
from conductor.presentation.layout import event
from conductor.presentation.tokens import Tone
from conductor.services.runtime import log_tick

log = structlog.get_logger("conductor.privacy")

_MEMBER_FIELDS = (
    "username",
    "display_name",
    "is_bot",
    "currently_member",
    "first_joined_at",
    "last_joined_at",
    "left_at",
    "account_created_at",
    "booster_since",
    "last_seen_at",
    "last_message_at",
    "message_count",
    "voice_seconds",
    "invite_code",
    "first_message_at",
    "attachment_count",
    "sticker_count",
    "reply_count",
    "link_count",
)


@dataclass(frozen=True, slots=True)
class ForgetReport:
    tables: dict[str, int]
    cache: int = 0

    @property
    def total(self) -> int:
        return int(self.cache) + self.db_rows

    @property
    def db_rows(self) -> int:
        return sum(self.tables.values())


class PrivacyService:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        scheduler = getattr(bot, "scheduler", None)
        every = getattr(scheduler, "every", None)
        if callable(every):
            every(PURGE_PERIOD, "data-purge", self.purge_tick)
            every(BACKUP_PERIOD, "sqlite-backup", self.backup_tick)

    async def export_person(self, guild_id: int, user_id: int, *, now: datetime | None = None) -> tuple[bytes, str]:
        moment = as_utc(now or datetime.now(UTC))
        async with session_scope(self._bot.sessions) as session:
            payload = await _collect(session, guild_id, user_id, now=moment, settings=_settings_view(self._bot))
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return text.encode("utf-8"), export_filename(user_id)

    async def forget(self, guild_id: int, user_id: int) -> ForgetReport:
        async with session_scope(self._bot.sessions) as session:
            tables = await _forget_rows(session, guild_id, user_id)
        cache = 0
        drop = getattr(getattr(self._bot, "messages", None), "drop_author", None)
        if callable(drop):
            cache = int(drop(guild_id, user_id) or 0)
        invalidate = getattr(getattr(self._bot, "caches", None), "invalidate_member", None)
        if callable(invalidate):
            invalidate(guild_id, user_id)
        forget_voice = getattr(self._bot, "forget_voice", None)
        if callable(forget_voice):
            forget_voice(guild_id, user_id)
        log.info("data_forgotten", guild_id=guild_id, user_id=user_id, rows=sum(tables.values()), cache=cache)
        return ForgetReport(tables, cache)

    async def purge_tick(self, *, now: datetime | None = None, pause: float | None = None) -> int:
        settings = getattr(self._bot, "settings", None)
        moment = as_utc(now or datetime.now(UTC))
        days = int(getattr(settings, "data_retention_days", 0) or 0)
        transcript_days = int(getattr(settings, "ticket_transcript_days", TICKET_TRANSCRIPT_DAYS) or 0)
        wait = USER_LOG_PURGE_PAUSE if pause is None else float(pause)
        async with session_scope(self._bot.sessions) as session:
            removed, report, jobs = await _purge_retention(
                session,
                moment,
                derived_days=days,
                transcript_days=transcript_days,
            )
        await _sweep_user_logs(self._bot, jobs, moment, pause=wait)
        _report_purge(self._bot, report)
        log_tick(log, "data_purged", changed=removed, rows=removed, tables=len(report))
        return removed

    async def backup_tick(self, *, now: datetime | None = None) -> Path | None:
        settings = getattr(self._bot, "settings", None)
        keep = int(getattr(settings, "sqlite_backup_keep", 0) or 0)
        source = getattr(settings, "sqlite_path", None)
        if keep <= 0 or source is None:
            log_tick(log, "sqlite_backup", changed=0)
            return None
        source = Path(source)
        if not source.is_file():
            log_tick(log, "sqlite_backup", changed=0)
            return None
        folder = source.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / backup_filename(now or datetime.now(UTC))
        quoted = "'" + dest.as_posix().replace("'", "''") + "'"
        try:
            async with aiosqlite.connect(source.as_posix(), isolation_level=None) as db:
                check = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await check.close()
                vacuum = await db.execute(f"VACUUM INTO {quoted}")
                await vacuum.close()
        except Exception:
            log.exception("sqlite_backup_failed", dest=str(dest))
            return None
        dropped = backups_to_drop(tuple(folder.glob("conductor-*.db")), keep=keep)
        for old in dropped:
            old.unlink(missing_ok=True)
        log_tick(log, "sqlite_backup", changed=1, path=str(dest), kept=keep, dropped=len(dropped))
        return dest


def _settings_view(bot: Any) -> dict[str, object]:
    settings = getattr(bot, "settings", None)
    return {
        "cache_ttl_seconds": int(getattr(settings, "message_cache_ttl", 3600) or 3600),
        "derived_retention_days": int(getattr(settings, "data_retention_days", 90) or 0),
        "ticket_transcript_days": int(getattr(settings, "ticket_transcript_days", TICKET_TRANSCRIPT_DAYS) or 0),
        "sqlite_backup_keep": int(getattr(settings, "sqlite_backup_keep", 0) or 0),
    }


async def _collect(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    *,
    now: datetime,
    settings: dict[str, object],
) -> dict[str, object]:
    member = await session.scalar(
        select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
    )
    loyalty = await session.scalar(
        select(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id, LoyaltyRegistry.user_id == user_id)
    )
    reputation = await session.scalar(
        select(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)
    )
    ladder = await session.scalar(
        select(LadderProgress).where(LadderProgress.guild_id == guild_id, LadderProgress.user_id == user_id)
    )
    duty = await session.get(WatcherDuty, (guild_id, user_id))
    unofficial = await session.get(UnofficialAdminNotice, (guild_id, user_id))
    buckets = await session.scalar(select(ActivityBucket.id).where(ActivityBucket.guild_id == guild_id).limit(1))
    channel_days = await session.scalar(select(ChannelDaily.id).where(ChannelDaily.guild_id == guild_id).limit(1))
    notes = await _user_rows(
        session,
        WatcherNote,
        guild_id,
        or_(WatcherNote.user_id == user_id, WatcherNote.author_id == user_id),
    )
    suspensions = await _user_rows(
        session,
        WatcherSuspension,
        guild_id,
        or_(
            WatcherSuspension.user_id == user_id,
            WatcherSuspension.set_by == user_id,
            WatcherSuspension.lifted_by == user_id,
        ),
    )
    edges = await _user_rows(
        session,
        InteractionEdge,
        guild_id,
        or_(InteractionEdge.source_id == user_id, InteractionEdge.target_id == user_id),
    )
    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "exported_at": json_safe(now),
        "retention": settings,
        "member": None if member is None else _fields(member, *_MEMBER_FIELDS),
        "daily": _dump(
            await _user_rows(session, MemberDaily, guild_id, MemberDaily.user_id == user_id),
            "day",
            "messages",
            "voice_seconds",
            "reactions",
        ),
        "channels": _dump(
            await _user_rows(session, MemberChannel, guild_id, MemberChannel.user_id == user_id),
            "channel_id",
            "messages",
            "attachments",
            "replies",
            "last_at",
        ),
        "edges": _dump(edges, "source_id", "target_id", "kind", "count", "last_at"),
        "voice_sessions": _dump(
            await _user_rows(session, VoiceSession, guild_id, VoiceSession.user_id == user_id),
            "channel_id",
            "joined_at",
            "left_at",
            "duration_seconds",
        ),
        "loyalty": None if loyalty is None else _fields(loyalty, "tier", "updated_at"),
        "loyalty_role_events": _dump(
            await _user_rows(session, LoyaltyRoleEvent, guild_id, LoyaltyRoleEvent.user_id == user_id),
            "role_id",
            "action",
            "reverted",
            "created_at",
        ),
        "reputation": None if reputation is None else _fields(reputation, "score", "banned_for_reputation"),
        "reputation_events": _dump(
            await _user_rows(session, ReputationEvent, guild_id, ReputationEvent.user_id == user_id),
            "kind",
            "delta",
            "cut",
            "score_after",
            "case_id",
            "created_at",
        ),
        "ladder": None if ladder is None else _fields(ladder, "step", "updated_at"),
        "achievement_progress": _dump(
            await _user_rows(session, AchievementProgress, guild_id, AchievementProgress.user_id == user_id),
            "key",
            "count",
            "updated_at",
        ),
        "achievement_grants": _dump(
            await _user_rows(session, AchievementGrant, guild_id, AchievementGrant.user_id == user_id),
            "key",
            "stage",
            "granted_at",
            "revoked_at",
        ),
        "reminders": _dump(
            await _user_rows(session, Reminder, guild_id, Reminder.user_id == user_id),
            "body",
            "due_at",
            "created_at",
            "delivered_at",
            "cancelled_at",
        ),
        "bug_reports": _dump(
            await _user_rows(session, BugReport, guild_id, BugReport.user_id == user_id),
            "code",
            "did",
            "happened",
            "version",
            "status",
            "created_at",
        ),
        "watcher_duty": None if duty is None else _fields(duty, "since", "set_by"),
        "watcher_notes": _dump(notes, "user_id", "author_id", "body", "created_at"),
        "watcher_suspensions": _dump(suspensions, "user_id", "set_by", "reason", "until", "lifted_at", "lifted_by"),
        "role_changes": _dump(
            await _user_rows(session, RoleChange, guild_id, RoleChange.user_id == user_id),
            "subsystem",
            "add_role_ids",
            "remove_role_ids",
            "reason",
            "ok",
            "created_at",
        ),
        "mod_cases": _dump(
            await _user_rows(session, ModCase, guild_id, ModCase.owner_id == user_id),
            "number",
            "kind",
            "reason",
            "created_at",
            "revoked_at",
            "revoke_reason",
        ),
        "mod_evidence": _dump(
            await _user_rows(
                session,
                ModEvidence,
                guild_id,
                ModEvidence.case_id.in_(
                    select(ModCase.id).where(ModCase.guild_id == guild_id, ModCase.owner_id == user_id)
                ),
            ),
            "case_id",
            "created_at",
            "sent_at",
            "detail_reason",
            "transcript",
        ),
        "achievement_sources": _dump(
            await _user_rows(session, AchievementSource, guild_id, AchievementSource.user_id == user_id),
            "ticket_id",
            "key",
        ),
        "mod_intents": _dump(
            await _user_rows(session, ModIntent, guild_id, ModIntent.target_id == user_id),
            "kind",
            "reason",
            "created_at",
        ),
        "unofficial_admins": None if unofficial is None else _fields(unofficial, "noticed_at"),
        "tickets": _dump(
            await _user_rows(session, Ticket, guild_id, Ticket.owner_id == user_id),
            "number",
            "state",
            "claimed_by",
            "form_answers",
            "created_at",
            "closed_at",
        ),
        "ticket_events": _dump(
            await _user_rows(session, TicketEvent, guild_id, TicketEvent.actor_id == user_id),
            "ticket_id",
            "kind",
            "note",
            "created_at",
        ),
        "ticket_transcripts": _dump(
            await _owned_transcripts(session, guild_id, user_id),
            "ticket_id",
            "body",
            "created_at",
        ),
        "giveaway_entries": _dump(
            await _child_rows(session, GiveawayEntry, GiveawayEntry.giveaway_id, Giveaway, guild_id, user_id),
            "giveaway_id",
            "created_at",
        ),
        "giveaway_winners": _dump(
            await _child_rows(session, GiveawayWinner, GiveawayWinner.giveaway_id, Giveaway, guild_id, user_id),
            "giveaway_id",
            "round",
            "place",
            "created_at",
        ),
        "poll_votes": _dump(
            await _child_rows(session, PollVote, PollVote.poll_id, Poll, guild_id, user_id),
            "poll_id",
            "option_index",
            "created_at",
        ),
        "gathering_rsvps": _dump(
            await _child_rows(session, GatheringRsvp, GatheringRsvp.gathering_id, Gathering, guild_id, user_id),
            "gathering_id",
            "status",
            "waitlisted",
            "created_at",
        ),
        "group_members": _dump(
            await _child_rows(session, GroupMember, GroupMember.group_id, PrivateGroup, guild_id, user_id),
            "group_id",
        ),
        "group_invites": _dump(
            await _child_rows(session, GroupInvite, GroupInvite.group_id, PrivateGroup, guild_id, user_id),
            "group_id",
            "status",
            "created_at",
        ),
        "voice_room_kicks": _dump(
            await _child_rows(session, VoiceRoomKick, VoiceRoomKick.room_id, VoiceRoom, guild_id, user_id),
            "room_id",
        ),
        "voice_room_requests": _dump(
            await _child_rows(session, VoiceRoomRequest, VoiceRoomRequest.room_id, VoiceRoom, guild_id, user_id),
            "room_id",
            "created_at",
        ),
        "voice_room_lfg": _dump(
            await _child_rows(session, VoiceRoomLfg, VoiceRoomLfg.room_id, VoiceRoom, guild_id, user_id),
            "room_id",
        ),
        "voice_rooms": _dump(
            await _user_rows(session, VoiceRoom, guild_id, VoiceRoom.owner_id == user_id),
            "channel_id",
            "hub_id",
            "created_at",
        ),
        "giveaways": _dump(
            await _user_rows(session, Giveaway, guild_id, Giveaway.owner_id == user_id),
            "id",
            "channel_id",
            "prize",
            "ends_at",
            "created_at",
        ),
        "polls": _dump(
            await _user_rows(session, Poll, guild_id, Poll.owner_id == user_id),
            "id",
            "channel_id",
            "question",
            "ends_at",
            "created_at",
        ),
        "gatherings": _dump(
            await _user_rows(session, Gathering, guild_id, Gathering.owner_id == user_id),
            "id",
            "channel_id",
            "title",
            "starts_at",
            "created_at",
        ),
        "groups": _dump(
            await _user_rows(session, PrivateGroup, guild_id, PrivateGroup.owner_id == user_id),
            "id",
            "channel_id",
            "title",
            "created_at",
        ),
        "lfg_posts": _dump(
            await _user_rows(session, LfgPost, guild_id, LfgPost.author_id == user_id),
            "id",
            "channel_id",
            "game_role_id",
            "need",
            "created_at",
        ),
        "lfg_members": _dump(
            await _child_rows(session, LfgMember, LfgMember.post_id, LfgPost, guild_id, user_id),
            "post_id",
            "joined_at",
        ),
        "recruit_links": _dump(
            await _user_rows(session, RecruitLink, guild_id, RecruitLink.owner_user_id == user_id),
            "code",
            "channel_id",
            "created_at",
            "revoked_at",
        ),
        "automod_hits": _dump(
            await _user_rows(session, AutomodHit, guild_id, AutomodHit.user_id == user_id),
            "rule",
            "channel_id",
            "message_id",
            "case_id",
            "created_at",
        ),
        "automod_requests": _dump(
            await _user_rows(
                session,
                AutomodRequest,
                guild_id,
                or_(AutomodRequest.author_id == user_id, AutomodRequest.decided_by == user_id),
            ),
            "author_id",
            "rule",
            "proposal",
            "status",
            "created_at",
        ),
        "member_watch": _dump(
            await _user_rows(
                session,
                MemberWatch,
                guild_id,
                or_(MemberWatch.user_id == user_id, MemberWatch.set_by == user_id),
            ),
            "user_id",
            "set_by",
            "reason",
            "until",
            "created_at",
        ),
        "staff_actions": _dump(
            await _user_rows(
                session,
                StaffAction,
                guild_id,
                or_(StaffAction.actor_id == user_id, StaffAction.target_id == user_id),
            ),
            "actor_id",
            "action",
            "target_id",
            "ref",
            "details",
            "created_at",
        ),
        "anonymized_kept": {
            "activity_buckets": buckets is not None,
            "channel_daily": channel_days is not None,
        },
    }


def _fields(row: object, *names: str) -> dict[str, object]:
    return {name: json_safe(getattr(row, name)) for name in names}


def _dump(rows: list[Any], *names: str) -> list[dict[str, object]]:
    return [_fields(row, *names) for row in rows]


async def _user_rows(session: AsyncSession, model: Any, guild_id: int, clause: Any) -> list[Any]:
    return list((await session.scalars(select(model).where(model.guild_id == guild_id, clause))).all())


async def _child_rows(
    session: AsyncSession,
    child: Any,
    fk: Any,
    parent: Any,
    guild_id: int,
    user_id: int,
) -> list[Any]:
    return list(
        (
            await session.scalars(
                select(child).where(
                    child.user_id == user_id,
                    fk.in_(select(parent.id).where(parent.guild_id == guild_id)),
                )
            )
        ).all()
    )


async def _owned_transcripts(session: AsyncSession, guild_id: int, user_id: int) -> list[Any]:
    owned = select(Ticket.id).where(Ticket.guild_id == guild_id, Ticket.owner_id == user_id)
    return list((await session.scalars(select(TicketTranscript).where(TicketTranscript.ticket_id.in_(owned)))).all())


async def _forget_rows(session: AsyncSession, guild_id: int, user_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    names = await _member_names(session, guild_id, user_id)
    counts["ticket_transcripts"] = await _scrub_transcripts(session, guild_id, user_id, names)
    counts["members"] = await _anonymize_member(session, guild_id, user_id)
    simple = (
        ("member_daily", delete(MemberDaily).where(MemberDaily.guild_id == guild_id, MemberDaily.user_id == user_id)),
        (
            "member_channels",
            delete(MemberChannel).where(MemberChannel.guild_id == guild_id, MemberChannel.user_id == user_id),
        ),
        (
            "interaction_edges",
            delete(InteractionEdge).where(
                InteractionEdge.guild_id == guild_id,
                or_(InteractionEdge.source_id == user_id, InteractionEdge.target_id == user_id),
            ),
        ),
        (
            "voice_sessions",
            delete(VoiceSession).where(VoiceSession.guild_id == guild_id, VoiceSession.user_id == user_id),
        ),
        (
            "loyalty_registry",
            delete(LoyaltyRegistry).where(LoyaltyRegistry.guild_id == guild_id, LoyaltyRegistry.user_id == user_id),
        ),
        (
            "loyalty_role_events",
            delete(LoyaltyRoleEvent).where(LoyaltyRoleEvent.guild_id == guild_id, LoyaltyRoleEvent.user_id == user_id),
        ),
        ("reputation", delete(Reputation).where(Reputation.guild_id == guild_id, Reputation.user_id == user_id)),
        (
            "reputation_events",
            delete(ReputationEvent).where(ReputationEvent.guild_id == guild_id, ReputationEvent.user_id == user_id),
        ),
        (
            "ladder",
            delete(LadderProgress).where(LadderProgress.guild_id == guild_id, LadderProgress.user_id == user_id),
        ),
        (
            "achievement_progress",
            delete(AchievementProgress).where(
                AchievementProgress.guild_id == guild_id, AchievementProgress.user_id == user_id
            ),
        ),
        (
            "achievement_grants",
            delete(AchievementGrant).where(AchievementGrant.guild_id == guild_id, AchievementGrant.user_id == user_id),
        ),
        ("reminders", delete(Reminder).where(Reminder.guild_id == guild_id, Reminder.user_id == user_id)),
        ("bug_reports", delete(BugReport).where(BugReport.guild_id == guild_id, BugReport.user_id == user_id)),
        ("watcher_duty", delete(WatcherDuty).where(WatcherDuty.guild_id == guild_id, WatcherDuty.user_id == user_id)),
        (
            "watcher_notes",
            delete(WatcherNote).where(
                WatcherNote.guild_id == guild_id,
                or_(WatcherNote.user_id == user_id, WatcherNote.author_id == user_id),
            ),
        ),
        (
            "watcher_suspensions",
            delete(WatcherSuspension).where(
                WatcherSuspension.guild_id == guild_id,
                or_(
                    WatcherSuspension.user_id == user_id,
                    WatcherSuspension.set_by == user_id,
                    WatcherSuspension.lifted_by == user_id,
                ),
            ),
        ),
        ("role_changes", delete(RoleChange).where(RoleChange.guild_id == guild_id, RoleChange.user_id == user_id)),
        ("automod_hits", delete(AutomodHit).where(AutomodHit.guild_id == guild_id, AutomodHit.user_id == user_id)),
        (
            "automod_requests",
            delete(AutomodRequest).where(
                AutomodRequest.guild_id == guild_id,
                or_(AutomodRequest.author_id == user_id, AutomodRequest.decided_by == user_id),
            ),
        ),
        (
            "member_watch",
            delete(MemberWatch).where(
                MemberWatch.guild_id == guild_id,
                or_(MemberWatch.user_id == user_id, MemberWatch.set_by == user_id),
            ),
        ),
        (
            # Раньше дел: транскрипт ЛС забываемого лежит в mod_evidence, а сама таблица под `keep`.
            "mod_evidence",
            delete(ModEvidence).where(
                ModEvidence.guild_id == guild_id,
                ModEvidence.case_id.in_(
                    select(ModCase.id).where(ModCase.guild_id == guild_id, ModCase.owner_id == user_id)
                ),
            ),
        ),
        ("mod_cases", delete(ModCase).where(ModCase.guild_id == guild_id, ModCase.owner_id == user_id)),
        ("mod_intents", delete(ModIntent).where(ModIntent.guild_id == guild_id, ModIntent.target_id == user_id)),
        (
            "achievement_sources",
            delete(AchievementSource).where(
                AchievementSource.guild_id == guild_id, AchievementSource.user_id == user_id
            ),
        ),
        (
            "unofficial_admins",
            delete(UnofficialAdminNotice).where(
                UnofficialAdminNotice.guild_id == guild_id, UnofficialAdminNotice.user_id == user_id
            ),
        ),
    )
    for name, stmt in simple:
        counts[name] = await _gone(session, stmt)
    counts["giveaway_entries"] = await _gone(
        session,
        delete(GiveawayEntry).where(
            GiveawayEntry.user_id == user_id,
            GiveawayEntry.giveaway_id.in_(select(Giveaway.id).where(Giveaway.guild_id == guild_id)),
        ),
    )
    counts["giveaway_winners"] = await _gone(
        session,
        delete(GiveawayWinner).where(
            GiveawayWinner.user_id == user_id,
            GiveawayWinner.giveaway_id.in_(select(Giveaway.id).where(Giveaway.guild_id == guild_id)),
        ),
    )
    counts["poll_votes"] = await _gone(
        session,
        delete(PollVote).where(
            PollVote.user_id == user_id,
            PollVote.poll_id.in_(select(Poll.id).where(Poll.guild_id == guild_id)),
        ),
    )
    counts["gathering_rsvps"] = await _gone(
        session,
        delete(GatheringRsvp).where(
            GatheringRsvp.user_id == user_id,
            GatheringRsvp.gathering_id.in_(select(Gathering.id).where(Gathering.guild_id == guild_id)),
        ),
    )
    counts["group_members"] = await _gone(
        session,
        delete(GroupMember).where(
            GroupMember.user_id == user_id,
            GroupMember.group_id.in_(select(PrivateGroup.id).where(PrivateGroup.guild_id == guild_id)),
        ),
    )
    counts["group_invites"] = await _gone(
        session,
        delete(GroupInvite).where(
            or_(GroupInvite.user_id == user_id, GroupInvite.invited_by == user_id),
            GroupInvite.group_id.in_(select(PrivateGroup.id).where(PrivateGroup.guild_id == guild_id)),
        ),
    )
    rooms = select(VoiceRoom.id).where(VoiceRoom.guild_id == guild_id)
    counts["voice_room_kicks"] = await _gone(
        session, delete(VoiceRoomKick).where(VoiceRoomKick.user_id == user_id, VoiceRoomKick.room_id.in_(rooms))
    )
    counts["voice_room_requests"] = await _gone(
        session,
        delete(VoiceRoomRequest).where(VoiceRoomRequest.user_id == user_id, VoiceRoomRequest.room_id.in_(rooms)),
    )
    counts["voice_room_lfg"] = await _gone(
        session, delete(VoiceRoomLfg).where(VoiceRoomLfg.user_id == user_id, VoiceRoomLfg.room_id.in_(rooms))
    )
    counts["tickets_owned"] = await _changed(
        session,
        update(Ticket)
        .where(Ticket.guild_id == guild_id, Ticket.owner_id == user_id)
        .values(
            owner_id=FORGOTTEN_USER_ID,
            form_answers="",
            staff_notes="[]",
            close_reason=FORGOTTEN_REASON,
        ),
    )
    counts["tickets_claimed"] = await _changed(
        session,
        update(Ticket).where(Ticket.guild_id == guild_id, Ticket.claimed_by == user_id).values(claimed_by=None),
    )
    counts["tickets_closed"] = await _changed(
        session,
        update(Ticket)
        .where(Ticket.guild_id == guild_id, Ticket.closed_by == user_id)
        .values(closed_by=None, close_reason=FORGOTTEN_REASON),
    )
    counts["ticket_events"] = await _changed(
        session,
        update(TicketEvent)
        .where(TicketEvent.guild_id == guild_id, TicketEvent.actor_id == user_id)
        .values(actor_id=FORGOTTEN_USER_ID, note=""),
    )
    counts["voice_rooms"] = await _anonymize_voice_rooms(session, guild_id, user_id)
    counts["giveaways"] = await _anonymize_owner(session, Giveaway, guild_id, user_id)
    counts["giveaways_closed"] = await _clear_actor(session, Giveaway, guild_id, user_id, "closed_by")
    counts["polls"] = await _anonymize_owner(session, Poll, guild_id, user_id)
    counts["polls_closed"] = await _clear_actor(session, Poll, guild_id, user_id, "closed_by")
    counts["polls_revealed"] = await _clear_actor(session, Poll, guild_id, user_id, "deanonymized_by")
    counts["gatherings"] = await _anonymize_owner(session, Gathering, guild_id, user_id)
    counts["gatherings_closed"] = await _clear_actor(session, Gathering, guild_id, user_id, "closed_by")
    counts["groups"] = await _anonymize_owner(session, PrivateGroup, guild_id, user_id)
    counts["groups_archived"] = await _clear_actor(session, PrivateGroup, guild_id, user_id, "archived_by")
    counts["lfg_members"] = await _gone(
        session,
        delete(LfgMember).where(
            LfgMember.user_id == user_id,
            LfgMember.post_id.in_(select(LfgPost.id).where(LfgPost.guild_id == guild_id)),
        ),
    )
    counts["lfg_posts"] = await _changed(
        session,
        update(LfgPost)
        .where(LfgPost.guild_id == guild_id, LfgPost.author_id == user_id)
        .values(author_id=FORGOTTEN_USER_ID),
    )
    counts["recruit_links"] = await _changed(
        session,
        update(RecruitLink)
        .where(RecruitLink.guild_id == guild_id, RecruitLink.owner_user_id == user_id)
        .values(owner_user_id=FORGOTTEN_USER_ID),
    )
    counts["staff_actions"] = await _changed(
        session,
        update(StaffAction)
        .where(
            StaffAction.guild_id == guild_id,
            or_(StaffAction.actor_id == user_id, StaffAction.target_id == user_id),
        )
        .values(
            actor_id=case((StaffAction.actor_id == user_id, FORGOTTEN_USER_ID), else_=StaffAction.actor_id),
            target_id=case((StaffAction.target_id == user_id, FORGOTTEN_USER_ID), else_=StaffAction.target_id),
        ),
    )
    return counts


async def _member_names(session: AsyncSession, guild_id: int, user_id: int) -> tuple[str, ...]:
    row = await session.scalar(
        select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
    )
    if row is None:
        return ()
    return tuple(name for name in (row.display_name, row.username) if name)


async def _scrub_transcripts(session: AsyncSession, guild_id: int, user_id: int, names: Sequence[str]) -> int:
    rows = list((await session.scalars(select(TicketTranscript).where(TicketTranscript.guild_id == guild_id))).all())
    changed = 0
    for row in rows:
        updated = forget_transcript_body(row.body, user_id=user_id, names=names)
        if updated != row.body:
            row.body = updated
            changed += 1
    return changed


_MODELS_BY_TABLE: dict[str, type] | None = None


def _models_by_table() -> dict[str, type]:
    global _MODELS_BY_TABLE
    if _MODELS_BY_TABLE is None:
        found: dict[str, type] = {}
        for mapper in Base.registry.mappers:
            cls = mapper.class_
            name = getattr(cls, "__tablename__", None)
            if name:
                found[str(name)] = cls
        _MODELS_BY_TABLE = found
    return _MODELS_BY_TABLE


def _rule_days(rule: Any, *, derived_days: int, transcript_days: int) -> int | None:
    if rule.setting == SETTING_DATA:
        return derived_days if derived_days > 0 else None
    if rule.setting == SETTING_TRANSCRIPT:
        return transcript_days if transcript_days > 0 else None
    return rule.days


async def _purge_retention(
    session: AsyncSession,
    now: datetime,
    *,
    derived_days: int,
    transcript_days: int,
) -> tuple[int, dict[str, int], list[tuple[int, int, int]]]:
    closed = await _close_open_voice(session, now)
    if closed:
        await session.flush()
    counts: dict[str, int] = {}
    models = _models_by_table()
    for table, rule in RETENTION.items():
        if rule.kind != KIND_DAYS:
            continue
        days = _rule_days(rule, derived_days=derived_days, transcript_days=transcript_days)
        if days is None:
            continue
        cutoff = retention_cutoff(now, days=days)
        if cutoff is None:
            continue
        model = models[table]
        removed = await _purge_rule(session, model, rule, cutoff)
        if removed:
            counts[table] = counts.get(table, 0) + removed
    jobs = await _user_log_jobs(session)
    report = dict(counts)
    if closed:
        report["voice_sessions"] = report.get("voice_sessions", 0) + closed
    return sum(counts.values()), report, jobs


async def _close_open_voice(session: AsyncSession, now: datetime) -> int:
    cutoff = voice_open_cutoff(now)
    rows = list(
        (
            await session.scalars(
                select(VoiceSession).where(
                    VoiceSession.left_at.is_(None),
                    VoiceSession.last_event_at < cutoff,
                )
            )
        ).all()
    )
    for row in rows:
        left = row.last_event_at
        row.left_at = left
        row.duration_seconds = max(0, int((as_utc(left) - as_utc(row.joined_at)).total_seconds()))
        row.truncated = True
    return len(rows)


async def _purge_rule(session: AsyncSession, model: Any, rule: Any, cutoff: datetime) -> int:
    column_name = rule.column or "created_at"
    col = getattr(model, column_name)
    filt = rule.filter
    if filt == FILTER_SNOWFLAKE:
        clause = col < snowflake_before(cutoff)
    elif filt == FILTER_STALE:
        clause = and_(model.state == "stale", col < cutoff)
    elif filt == FILTER_NO_CASE:
        clause = and_(model.case_id.is_(None), col < cutoff)
    elif filt == FILTER_VOICE:
        clause = and_(model.left_at.is_not(None), model.left_at < cutoff)
    else:
        clause = col < cutoff
        nullable = bool(getattr(col, "nullable", False))
        if nullable:
            clause = and_(col.is_not(None), clause)
    return await _gone(session, delete(model).where(clause))


async def _user_log_jobs(session: AsyncSession) -> list[tuple[int, int, int]]:
    rows = list((await session.scalars(select(GuildConfig))).all())
    jobs: list[tuple[int, int, int]] = []
    for row in rows:
        channel_id = row.user_log_channel_id
        keep_days = int(row.user_log_keep_days if row.user_log_keep_days is not None else USER_LOG_KEEP_DAYS)
        if channel_id is None:
            continue
        jobs.append((int(row.guild_id), int(channel_id), keep_days))
    return jobs


async def _sweep_user_logs(
    bot: Any,
    jobs: Sequence[tuple[int, int, int]],
    now: datetime,
    *,
    pause: float,
) -> int:
    bot_user = getattr(bot, "user", None)
    bot_id = getattr(bot_user, "id", None)
    if bot_id is None:
        return 0
    serves = getattr(bot, "serves", None)
    deleted = 0
    for guild_id, channel_id, keep_days in jobs:
        if deleted >= USER_LOG_PURGE_CAP:
            break
        if callable(serves) and not serves(guild_id):
            continue
        cutoff = user_log_cutoff(now, days=keep_days)
        if cutoff is None:
            continue
        channel = _user_log_channel(bot, guild_id, channel_id)
        if channel is None:
            continue
        history = getattr(channel, "history", None)
        if not callable(history):
            continue
        # Только старше порога: без `before` канал читался бы с самого свежего сообщения каждый день.
        async for message in history(limit=None, before=cutoff, oldest_first=True):
            if deleted >= USER_LOG_PURGE_CAP:
                break
            author = getattr(message, "author", None)
            author_id = getattr(author, "id", None)
            created = getattr(message, "created_at", None)
            if author_id is None or created is None:
                continue
            if not should_drop_user_log_card(
                author_id=int(author_id),
                bot_id=int(bot_id),
                created_at=created,
                cutoff=cutoff,
            ):
                continue
            drop = getattr(message, "delete", None)
            if not callable(drop):
                continue
            await drop()
            deleted += 1
            await asyncio.sleep(pause)
            if deleted % USER_LOG_PURGE_BATCH == 0:
                await asyncio.sleep(pause)
    return deleted


def _user_log_channel(bot: Any, guild_id: int, channel_id: int) -> Any:
    guild = None
    lookup = getattr(bot, "get_guild", None)
    if callable(lookup):
        guild = lookup(guild_id)
    if guild is not None:
        get_channel = getattr(guild, "get_channel", None)
        if callable(get_channel):
            found = get_channel(channel_id)
            if found is not None:
                return found
    get_channel = getattr(bot, "get_channel", None)
    if callable(get_channel):
        return get_channel(channel_id)
    return None


def _report_purge(bot: Any, report: dict[str, int]) -> None:
    rows = sum(report.values())
    tables = sum(1 for count in report.values() if count)
    text = copy.purge_report(rows, tables)
    pump = getattr(bot, "log_pump", None)
    submit = getattr(pump, "submit", None)
    if not callable(submit):
        return
    seen: set[int] = set()
    configs = getattr(bot, "config_by_guild", {}) or {}
    for config in configs.values():
        channel_id = getattr(config, "admin_log_channel_id", None)
        if channel_id is None or int(channel_id) in seen:
            continue
        seen.add(int(channel_id))
        submit(
            int(channel_id),
            view=event(copy.DATA_PURGE_TITLE, text, tone=Tone.INFO, source=copy.ADMIN_LOG_SOURCE),
        )


async def _anonymize_member(session: AsyncSession, guild_id: int, user_id: int) -> int:
    row = await session.scalar(
        select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
    )
    if row is None:
        return 0
    row.user_id = -int(row.id)
    row.username = ""
    row.display_name = ""
    row.account_created_at = None
    row.booster_since = None
    row.last_seen_at = None
    row.last_message_at = None
    row.first_message_at = None
    row.message_count = 0
    row.voice_seconds = 0
    row.attachment_count = 0
    row.sticker_count = 0
    row.reply_count = 0
    row.link_count = 0
    return 1


async def _anonymize_owner(session: AsyncSession, model: Any, guild_id: int, user_id: int) -> int:
    return await _changed(
        session,
        update(model).where(model.guild_id == guild_id, model.owner_id == user_id).values(owner_id=FORGOTTEN_USER_ID),
    )


async def _clear_actor(session: AsyncSession, model: Any, guild_id: int, user_id: int, field: str) -> int:
    column = getattr(model, field)
    return await _changed(
        session,
        update(model).where(model.guild_id == guild_id, column == user_id).values({field: None}),
    )


async def _anonymize_voice_rooms(session: AsyncSession, guild_id: int, user_id: int) -> int:
    """uq_voice_room_owner: один хозяин на гильдию. 0 — если свободен, иначе -id строки."""
    rooms = (
        await session.scalars(select(VoiceRoom).where(VoiceRoom.guild_id == guild_id, VoiceRoom.owner_id == user_id))
    ).all()
    if not rooms:
        return 0
    taken = await session.scalar(
        select(VoiceRoom.id).where(VoiceRoom.guild_id == guild_id, VoiceRoom.owner_id == FORGOTTEN_USER_ID).limit(1)
    )
    for room in rooms:
        if taken is None:
            room.owner_id = FORGOTTEN_USER_ID
            taken = room.id
        else:
            room.owner_id = -int(room.id)
    return len(rooms)


async def _gone(session: AsyncSession, stmt: Any) -> int:
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def _changed(session: AsyncSession, stmt: Any) -> int:
    return await _gone(session, stmt)
