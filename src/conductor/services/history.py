from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import discord
import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conductor.db.models import (
    ActivityBucket,
    ChannelDaily,
    ChannelScan,
    GuildConfig,
    IngestedMessage,
    IngestedReaction,
    InteractionEdge,
    MemberChannel,
    MemberDaily,
    MemberRecord,
)
from conductor.db.session import dialect_insert, session_scope
from conductor.domain.activity import EventKind, as_utc
from conductor.domain.history import (
    MessageFacts,
    PairScore,
    TieKind,
    already_ingested,
    count_links,
    directed_ties,
    emoji_key,
    extend_window,
    partners_for,
    planned_history_walks,
    score_pairs,
    unique_user_ids,
    walk_reached_boundary,
)
from conductor.services.activity import ActivityCollector

log = structlog.get_logger("conductor.history")
CHANNEL_CONCURRENCY = 4
FLUSH_EVERY_MESSAGES = 2000
LEDGER_HIGH_WATER = 8_000
CHANNEL_STALL_SECONDS = 90.0
REACTION_TIMEOUT_SECONDS = 20.0
_REACTION_FETCH = asyncio.Semaphore(2)


@dataclass(frozen=True, slots=True)
class IngestTick:
    done: int
    total: int
    messages: int
    channel: str


IngestProgress = Callable[[IngestTick], Awaitable[None]]


@dataclass(slots=True)
class IngestReport:
    channels: int = 0
    skipped: int = 0
    messages: int = 0
    authors: int = 0
    ties: int = 0
    reactions: int = 0
    reset: bool = False
    timed_out: int = 0


@dataclass(slots=True)
class HistoryCoverage:
    channels: int = 0
    messages: int = 0
    oldest_id: int | None = None
    newest_id: int | None = None


@dataclass(slots=True)
class MemberDossier:
    user_id: int
    messages: int = 0
    attachments: int = 0
    stickers: int = 0
    replies: int = 0
    links: int = 0
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    channels: list[tuple[int, int]] = field(default_factory=list)
    partners: list[tuple[int, PairScore]] = field(default_factory=list)
    mentions_given: int = 0
    mentions_received: int = 0
    replies_given: int = 0
    replies_received: int = 0
    reactions_given: int = 0
    reactions_received: int = 0


def facts_from_message(message: discord.Message) -> MessageFacts | None:
    guild = message.guild
    if guild is None or message.author.bot:
        return None
    author = message.author
    reply_to: int | None = None
    reference = message.reference
    if reference is not None and isinstance(reference.resolved, discord.Message):
        resolved = reference.resolved
        if not resolved.author.bot:
            reply_to = resolved.author.id
    mention_ids = [user.id for user in message.mentions if not user.bot]
    mention_ids.extend(message.raw_mentions)
    display = author.display_name if isinstance(author, discord.Member) else author.name
    created = as_utc(message.created_at)
    return MessageFacts(
        guild_id=guild.id,
        user_id=author.id,
        channel_id=message.channel.id,
        message_id=message.id,
        created_at=created,
        is_bot=False,
        attachments=len(message.attachments),
        stickers=len(message.stickers),
        embeds=len(message.embeds),
        links=count_links(message.content),
        mention_ids=unique_user_ids(author.id, mention_ids),
        reply_to_id=reply_to,
        author_name=str(author)[:40],
        display_name=display[:80],
    )


class HistoryLedger:
    """Buffers historical message facts, ties, and per-channel scan windows."""

    def __init__(self) -> None:
        self._windows: dict[tuple[int, int], tuple[int, int]] = {}
        self._scan_counts: dict[tuple[int, int], int] = defaultdict(int)
        self._scan_dirty: set[tuple[int, int]] = set()
        self._caught_up: set[tuple[int, int]] = set()
        self._edges: dict[tuple[int, int, int, str], int] = defaultdict(int)
        self._edge_last: dict[tuple[int, int, int, str], datetime] = {}
        self._channels: dict[tuple[int, int, int], list[int | datetime]] = {}
        self._member_msgs: dict[tuple[int, int], int] = defaultdict(int)
        self._member_attach: dict[tuple[int, int], int] = defaultdict(int)
        self._member_stickers: dict[tuple[int, int], int] = defaultdict(int)
        self._member_replies: dict[tuple[int, int], int] = defaultdict(int)
        self._member_links: dict[tuple[int, int], int] = defaultdict(int)
        self._first_message: dict[tuple[int, int], datetime] = {}
        self._last_message: dict[tuple[int, int], datetime] = {}
        self._names: dict[tuple[int, int], tuple[str, str]] = {}
        self._seen_messages: set[tuple[int, int]] = set()
        self._seen_reactions: set[tuple[int, int, int, str]] = set()
        self._new_messages: list[tuple[int, int, int]] = []
        self._new_reactions: list[tuple[int, int, int, str]] = []
        self._lock = asyncio.Lock()

    @property
    def buffered(self) -> int:
        return (
            len(self._new_messages)
            + len(self._new_reactions)
            + len(self._edges)
            + len(self._channels)
            + len(self._member_msgs)
            + len(self._scan_dirty)
        )

    def over_capacity(self, *, high_water: int = LEDGER_HIGH_WATER) -> bool:
        return self.buffered >= high_water

    def known_message(self, guild_id: int, message_id: int) -> bool:
        return (guild_id, message_id) in self._seen_messages

    def accept_live(self, guild_id: int, channel_id: int, message_id: int) -> bool:
        if (guild_id, message_id) in self._seen_messages:
            return False
        window = self._windows.get((guild_id, channel_id))
        if window is None:
            return False
        return not already_ingested(message_id, window[0], window[1])

    def clear_windows(self) -> None:
        self._windows.clear()
        self._scan_counts.clear()
        self._scan_dirty.clear()
        self._caught_up.clear()
        self._seen_messages.clear()
        self._seen_reactions.clear()
        self._new_messages.clear()
        self._new_reactions.clear()

    def is_caught_up(self, guild_id: int, channel_id: int) -> bool:
        return (guild_id, channel_id) in self._caught_up

    def mark_caught_up(self, guild_id: int, channel_id: int) -> None:
        key = (guild_id, channel_id)
        self._caught_up.add(key)
        if key in self._windows:
            self._scan_dirty.add(key)

    async def load(self, factory: async_sessionmaker[AsyncSession], guild_id: int) -> None:
        async with session_scope(factory) as session:
            scans = (await session.scalars(select(ChannelScan).where(ChannelScan.guild_id == guild_id))).all()
        async with self._lock:
            for row in scans:
                key = (row.guild_id, row.channel_id)
                self._windows[key] = (row.oldest_message_id, row.newest_message_id)
                self._scan_counts[key] = row.messages_ingested
                if row.caught_up:
                    self._caught_up.add(key)
                else:
                    self._caught_up.discard(key)

    async def recorded_message(
        self,
        factory: async_sessionmaker[AsyncSession],
        guild_id: int,
        message_id: int,
    ) -> bool:
        if (int(guild_id), int(message_id)) in self._seen_messages:
            return True
        async with session_scope(factory) as session:
            found = await session.scalar(
                select(IngestedMessage.message_id).where(
                    IngestedMessage.guild_id == int(guild_id),
                    IngestedMessage.message_id == int(message_id),
                )
            )
        return found is not None

    def apply(self, facts: MessageFacts) -> bool:
        if facts.is_bot:
            return False
        msg_key = (facts.guild_id, facts.message_id)
        if msg_key in self._seen_messages:
            return False
        key = (facts.guild_id, facts.channel_id)
        window = self._windows.get(key)
        oldest = newest = None
        if window is not None:
            oldest, newest = window
            if already_ingested(facts.message_id, oldest, newest):
                return False
        self._windows[key] = extend_window(facts.message_id, oldest, newest)
        self._scan_counts[key] += 1
        self._scan_dirty.add(key)
        self._seen_messages.add(msg_key)
        self._new_messages.append((facts.guild_id, facts.channel_id, facts.message_id))
        self._accumulate(facts)
        for source_id, target_id, kind in directed_ties(facts):
            self._add_edge(facts.guild_id, source_id, target_id, kind, facts.created_at)
        return True

    def apply_reaction(
        self,
        guild_id: int,
        source_id: int,
        target_id: int,
        moment: datetime,
        *,
        message_id: int,
        emoji: str,
    ) -> bool:
        if source_id == target_id:
            return False
        key = (guild_id, message_id, source_id, emoji[:128])
        if key in self._seen_reactions:
            return False
        self._seen_reactions.add(key)
        self._new_reactions.append(key)
        self._add_edge(guild_id, source_id, target_id, TieKind.REACTION, as_utc(moment))
        return True

    def _accumulate(self, facts: MessageFacts) -> None:
        member_key = (facts.guild_id, facts.user_id)
        self._member_msgs[member_key] += 1
        self._member_attach[member_key] += facts.attachments
        self._member_stickers[member_key] += facts.stickers
        self._member_links[member_key] += facts.links
        if facts.reply_to_id is not None:
            self._member_replies[member_key] += 1
        first = self._first_message.get(member_key)
        if first is None or facts.created_at < first:
            self._first_message[member_key] = facts.created_at
        last = self._last_message.get(member_key)
        if last is None or facts.created_at > last:
            self._last_message[member_key] = facts.created_at
        if facts.author_name:
            self._names[member_key] = (facts.author_name, facts.display_name or facts.author_name)
        channel_key = (facts.guild_id, facts.user_id, facts.channel_id)
        row = self._channels.get(channel_key)
        reply_inc = 1 if facts.reply_to_id else 0
        if row is None:
            self._channels[channel_key] = [1, facts.attachments, reply_inc, facts.created_at]
            return
        row[0] = int(row[0]) + 1
        row[1] = int(row[1]) + facts.attachments
        row[2] = int(row[2]) + reply_inc
        if facts.created_at > row[3]:  # type: ignore[operator]
            row[3] = facts.created_at

    def _add_edge(
        self,
        guild_id: int,
        source_id: int,
        target_id: int,
        kind: TieKind,
        moment: datetime,
    ) -> None:
        key = (guild_id, source_id, target_id, str(kind))
        self._edges[key] += 1
        previous = self._edge_last.get(key)
        if previous is None or moment > previous:
            self._edge_last[key] = moment

    def snapshot_window(self, guild_id: int, channel_id: int) -> tuple[int, int] | None:
        return self._windows.get((guild_id, channel_id))

    async def flush(self, factory: async_sessionmaker[AsyncSession]) -> int:
        async with self._lock:
            edges = dict(self._edges)
            edge_last = dict(self._edge_last)
            channels = dict(self._channels)
            member_msgs = dict(self._member_msgs)
            member_attach = dict(self._member_attach)
            member_stickers = dict(self._member_stickers)
            member_replies = dict(self._member_replies)
            member_links = dict(self._member_links)
            first_message = dict(self._first_message)
            last_message = dict(self._last_message)
            names = dict(self._names)
            dirty = set(self._scan_dirty)
            windows = {key: self._windows[key] for key in dirty}
            counts = {key: self._scan_counts[key] for key in dirty}
            caught_up = {key: key in self._caught_up for key in dirty}
            new_messages = list(self._new_messages)
            new_reactions = list(self._new_reactions)
            for guild_id, _channel_id, message_id in new_messages:
                self._seen_messages.discard((guild_id, message_id))
            for key in new_reactions:
                self._seen_reactions.discard(key)
            self._edges.clear()
            self._edge_last.clear()
            self._channels.clear()
            self._member_msgs.clear()
            self._member_attach.clear()
            self._member_stickers.clear()
            self._member_replies.clear()
            self._member_links.clear()
            self._first_message.clear()
            self._last_message.clear()
            self._names.clear()
            self._scan_dirty.clear()
            self._new_messages.clear()
            self._new_reactions.clear()
        total = len(edges) + len(channels) + len(member_msgs) + len(dirty) + len(new_messages) + len(new_reactions)
        if total == 0:
            return 0
        now = datetime.now(UTC)
        async with session_scope(factory) as session:
            await _insert_ingested_messages(session, new_messages)
            await _insert_ingested_reactions(session, new_reactions)
            for (guild_id, source_id, target_id, kind), amount in edges.items():
                await _increment_edge(
                    session,
                    guild_id,
                    source_id,
                    target_id,
                    kind,
                    amount,
                    edge_last.get((guild_id, source_id, target_id, kind)),
                )
            for (guild_id, user_id, channel_id), row in channels.items():
                last_at = row[3] if isinstance(row[3], datetime) else now
                await _increment_member_channel(
                    session,
                    guild_id,
                    user_id,
                    channel_id,
                    messages=int(row[0]),
                    attachments=int(row[1]),
                    replies=int(row[2]),
                    last_at=last_at,
                )
            keys = set(member_msgs) | set(member_attach) | set(member_replies)
            for key in keys:
                await _increment_member_content(
                    session,
                    guild_id=key[0],
                    user_id=key[1],
                    messages=member_msgs.get(key, 0),
                    attachments=member_attach.get(key, 0),
                    stickers=member_stickers.get(key, 0),
                    replies=member_replies.get(key, 0),
                    links=member_links.get(key, 0),
                    first_message_at=first_message.get(key),
                    last_message_at=last_message.get(key),
                    names=names.get(key),
                )
            for key, (oldest, newest) in windows.items():
                await _upsert_scan(
                    session,
                    key[0],
                    key[1],
                    oldest,
                    newest,
                    counts.get(key, 0),
                    now,
                    caught_up=caught_up.get(key, False),
                )
        return total


async def reset_message_derived(session: AsyncSession, guild_id: int) -> None:
    await session.execute(
        delete(ActivityBucket).where(
            ActivityBucket.guild_id == guild_id,
            ActivityBucket.event_kind == EventKind.MESSAGE,
        )
    )
    await session.execute(
        delete(ChannelDaily).where(
            ChannelDaily.guild_id == guild_id,
            ChannelDaily.event_kind == EventKind.MESSAGE,
        )
    )
    await session.execute(update(MemberDaily).where(MemberDaily.guild_id == guild_id).values(messages=0, reactions=0))
    await session.execute(
        update(MemberRecord)
        .where(MemberRecord.guild_id == guild_id)
        .values(
            message_count=0,
            attachment_count=0,
            sticker_count=0,
            reply_count=0,
            link_count=0,
            first_message_at=None,
            last_message_at=None,
        )
    )
    await session.execute(delete(InteractionEdge).where(InteractionEdge.guild_id == guild_id))
    await session.execute(delete(MemberChannel).where(MemberChannel.guild_id == guild_id))
    await session.execute(delete(ChannelScan).where(ChannelScan.guild_id == guild_id))
    await session.execute(delete(IngestedMessage).where(IngestedMessage.guild_id == guild_id))
    await session.execute(delete(IngestedReaction).where(IngestedReaction.guild_id == guild_id))


async def ingest_guild(
    guild: discord.Guild,
    *,
    sessions: async_sessionmaker[AsyncSession],
    collector: ActivityCollector,
    ledger: HistoryLedger,
    skip_channel_ids: set[int],
    per_channel: int | None,
    with_reactions: bool,
    on_progress: IngestProgress | None = None,
) -> IngestReport:
    report = IngestReport()
    async with session_scope(sessions) as session:
        existing = await session.scalar(select(ChannelScan.id).where(ChannelScan.guild_id == guild.id).limit(1))
        if existing is None:
            await reset_message_derived(session, guild.id)
            report.reset = True
    if report.reset:
        ledger.clear_windows()

    if on_progress:
        await on_progress(IngestTick(done=0, total=0, messages=0, channel=""))
    targets = await _history_targets(guild, skip_channel_ids)
    readable = [channel for channel in targets if _can_read(guild, channel)]
    report.skipped = len(targets) - len(readable)
    log.info(
        "history_ingest_start",
        guild_id=guild.id,
        targets=len(readable),
        skipped=report.skipped,
        unlimited=per_channel is None or per_channel <= 0,
        reactions=with_reactions,
        reset=report.reset,
    )
    authors: set[int] = set()
    sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)
    flush_lock = asyncio.Lock()
    since_flush = 0
    finished = 0
    if on_progress:
        await on_progress(IngestTick(done=0, total=len(readable), messages=0, channel=""))

    async def maybe_flush(added: int) -> None:
        nonlocal since_flush
        should_flush = False
        async with flush_lock:
            since_flush += added
            if since_flush >= FLUSH_EVERY_MESSAGES or ledger.over_capacity():
                since_flush = 0
                should_flush = True
        if should_flush:
            await ledger.flush(sessions)
            await collector.flush(sessions)

    async def maybe_progress(label: str) -> None:
        if on_progress is None:
            return
        await on_progress(IngestTick(done=finished, total=len(readable), messages=report.messages, channel=label))

    async def run_channel(channel: discord.abc.Messageable) -> None:
        nonlocal finished
        channel_id = getattr(channel, "id", None)
        name = getattr(channel, "name", None) or str(channel_id or "?")
        started = time.monotonic()
        async with sem:
            try:
                result = await _ingest_channel(
                    channel,
                    guild_id=guild.id,
                    sessions=sessions,
                    collector=collector,
                    ledger=ledger,
                    per_channel=per_channel,
                    with_reactions=with_reactions,
                    authors=authors,
                    report=report,
                    on_buffer=maybe_flush,
                )
            except discord.HTTPException:
                report.skipped += 1
                finished += 1
                return
            except Exception:
                log.exception("history_ingest_channel_failed", channel_id=channel_id)
                report.skipped += 1
                finished += 1
                return
            if result.reached_start and channel_id is not None:
                ledger.mark_caught_up(guild.id, channel_id)
            if result.timed_out:
                report.timed_out += 1
            report.channels += 1
            report.messages += result.added
            finished += 1
            await maybe_flush(result.added)
            await maybe_progress(name)
            log.info(
                "history_ingest_channel",
                channel_id=channel_id,
                added=result.added,
                total=report.messages,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                caught_up=result.reached_start,
                timed_out=result.timed_out,
            )

    await asyncio.gather(*(run_channel(channel) for channel in readable))
    await ledger.flush(sessions)
    await collector.flush(sessions)
    report.authors = len(authors)
    async with session_scope(sessions) as session:
        report.ties = await _count_ties(session, guild.id)
    log.info(
        "history_ingest_done",
        guild_id=guild.id,
        messages=report.messages,
        channels=report.channels,
        timed_out=report.timed_out,
    )
    return report


async def load_coverage(session: AsyncSession, guild_id: int) -> HistoryCoverage:
    rows = (await session.scalars(select(ChannelScan).where(ChannelScan.guild_id == guild_id))).all()
    coverage = HistoryCoverage(channels=len(rows), messages=sum(row.messages_ingested for row in rows))
    if rows:
        coverage.oldest_id = min(row.oldest_message_id for row in rows)
        coverage.newest_id = max(row.newest_message_id for row in rows)
    return coverage


async def load_pairs(session: AsyncSession, guild_id: int) -> list[PairScore]:
    rows = (
        await session.execute(
            select(
                InteractionEdge.source_id,
                InteractionEdge.target_id,
                InteractionEdge.kind,
                InteractionEdge.count,
            ).where(InteractionEdge.guild_id == guild_id)
        )
    ).all()
    return score_pairs([(int(source), int(target), str(kind), int(count)) for source, target, kind, count in rows])


async def load_dossier(session: AsyncSession, guild_id: int, user_id: int) -> MemberDossier:
    dossier = MemberDossier(user_id=user_id)
    record = await session.scalar(
        select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
    )
    if record is not None:
        dossier.messages = record.message_count or 0
        dossier.attachments = record.attachment_count or 0
        dossier.stickers = record.sticker_count or 0
        dossier.replies = record.reply_count or 0
        dossier.links = record.link_count or 0
        dossier.first_message_at = record.first_message_at
        dossier.last_message_at = record.last_message_at
    channel_rows = (
        await session.execute(
            select(MemberChannel.channel_id, MemberChannel.messages)
            .where(MemberChannel.guild_id == guild_id, MemberChannel.user_id == user_id)
            .order_by(MemberChannel.messages.desc())
            .limit(8)
        )
    ).all()
    dossier.channels = [(int(channel_id), int(count)) for channel_id, count in channel_rows]
    if not dossier.messages:
        dossier.messages = sum(count for _, count in dossier.channels)
    pairs = await load_pairs(session, guild_id)
    dossier.partners = partners_for(pairs, user_id)[:8]
    edge_rows = (
        await session.execute(
            select(
                InteractionEdge.source_id,
                InteractionEdge.target_id,
                InteractionEdge.kind,
                InteractionEdge.count,
            ).where(
                InteractionEdge.guild_id == guild_id,
                (InteractionEdge.source_id == user_id) | (InteractionEdge.target_id == user_id),
            )
        )
    ).all()
    for source_id, _target_id, kind, count in edge_rows:
        amount = int(count)
        if kind == TieKind.MENTION:
            if source_id == user_id:
                dossier.mentions_given += amount
            else:
                dossier.mentions_received += amount
        elif kind == TieKind.REPLY:
            if source_id == user_id:
                dossier.replies_given += amount
            else:
                dossier.replies_received += amount
        elif kind == TieKind.REACTION:
            if source_id == user_id:
                dossier.reactions_given += amount
            else:
                dossier.reactions_received += amount
    return dossier


async def load_channel_ranks(session: AsyncSession, guild_id: int) -> list[tuple[int, int, int]]:
    rows = (
        await session.execute(
            select(
                MemberChannel.channel_id,
                func.sum(MemberChannel.messages),
                func.count(MemberChannel.user_id),
            )
            .where(MemberChannel.guild_id == guild_id)
            .group_by(MemberChannel.channel_id)
            .order_by(func.sum(MemberChannel.messages).desc())
            .limit(8)
        )
    ).all()
    return [(int(channel_id), int(total or 0), int(authors or 0)) for channel_id, total, authors in rows]


def skip_ids_from_config(config: GuildConfig | None) -> set[int]:
    if config is None:
        return set()
    return {
        channel_id
        for channel_id in (
            config.user_log_channel_id,
            config.admin_log_channel_id,
            getattr(config, "staff_feed_channel_id", None),
            getattr(config, "automod_channel_id", None),
        )
        if channel_id
    }


async def _count_ties(session: AsyncSession, guild_id: int) -> int:
    return int(
        await session.scalar(
            select(func.coalesce(func.sum(InteractionEdge.count), 0)).where(InteractionEdge.guild_id == guild_id)
        )
        or 0
    )


async def _history_targets(
    guild: discord.Guild,
    skip_channel_ids: set[int],
) -> list[discord.abc.Messageable]:
    seen: set[int] = set()
    targets: list[discord.abc.Messageable] = []

    def add(channel: discord.abc.Messageable) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id is None or channel_id in seen or channel_id in skip_channel_ids:
            return
        seen.add(channel_id)
        targets.append(channel)

    for channel in guild.text_channels:
        add(channel)
    for channel in guild.voice_channels:
        add(channel)
    for channel in guild.stage_channels:
        add(channel)
    for thread in guild.threads:
        add(thread)
    for forum in guild.forums:
        for thread in forum.threads:
            add(thread)
    parents = list(guild.text_channels) + list(guild.forums)
    for parent in parents:
        for thread in await _archived_threads(parent, private=False):
            add(thread)
        if isinstance(parent, discord.TextChannel):
            for thread in await _archived_threads(parent, private=True):
                add(thread)
    return targets


async def _archived_threads(parent: discord.abc.GuildChannel, *, private: bool) -> list[discord.Thread]:
    found: list[discord.Thread] = []
    before: discord.Thread | None = None
    while True:
        kwargs: dict[str, object] = {"limit": 100}
        if private:
            kwargs["private"] = True
        if before is not None:
            kwargs["before"] = before
        batch: list[discord.Thread] = []
        try:
            async for thread in parent.archived_threads(**kwargs):
                batch.append(thread)
        except (discord.HTTPException, TypeError, AttributeError):
            break
        if not batch:
            break
        found.extend(batch)
        before = batch[-1]
        if len(batch) < 100:
            break
    return found


def _can_read(guild: discord.Guild, channel: discord.abc.Messageable) -> bool:
    me = guild.me
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    permissions = channel.permissions_for(me)
    return bool(permissions.read_message_history and permissions.view_channel)


@dataclass(slots=True)
class _ChannelPass:
    added: int = 0
    reached_start: bool = False
    timed_out: bool = False


async def _ingest_channel(
    channel: discord.abc.Messageable,
    *,
    guild_id: int,
    sessions: async_sessionmaker[AsyncSession],
    collector: ActivityCollector,
    ledger: HistoryLedger,
    per_channel: int | None,
    with_reactions: bool,
    authors: set[int],
    report: IngestReport,
    on_buffer: Callable[[int], Awaitable[None]] | None = None,
) -> _ChannelPass:
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return _ChannelPass()
    window = ledger.snapshot_window(guild_id, channel_id)
    check_table = window is not None
    limit = None if per_channel is None or per_channel <= 0 else per_channel
    result = _ChannelPass(reached_start=ledger.is_caught_up(guild_id, channel_id))

    async def consume(iterator) -> tuple[int, int]:
        added = 0
        fetched = 0
        while True:
            try:
                message = await asyncio.wait_for(anext(iterator), timeout=CHANNEL_STALL_SECONDS)
            except StopAsyncIteration:
                break
            except TimeoutError:
                result.timed_out = True
                log.warning("history_channel_stalled", channel_id=channel_id)
                break
            fetched += 1
            added += await _ingest_message(
                message,
                sessions=sessions,
                collector=collector,
                ledger=ledger,
                with_reactions=with_reactions,
                authors=authors,
                report=report,
                check_table=check_table,
            )
            if on_buffer is not None and ledger.over_capacity():
                await on_buffer(0)
        return added, fetched

    for walk in planned_history_walks(window, caught_up=result.reached_start):
        kwargs: dict[str, object] = {"limit": limit}
        if walk.after_id is not None:
            kwargs["after"] = discord.Object(id=walk.after_id)
        if walk.before_id is not None:
            kwargs["before"] = discord.Object(id=walk.before_id)
        added, fetched = await consume(channel.history(**kwargs))
        result.added += added
        if result.timed_out:
            break
        if walk.after_id is None and walk_reached_boundary(fetched, limit):
            result.reached_start = True
    return result


async def _ingest_message(
    message: discord.Message,
    *,
    sessions: async_sessionmaker[AsyncSession],
    collector: ActivityCollector,
    ledger: HistoryLedger,
    with_reactions: bool,
    authors: set[int],
    report: IngestReport,
    check_table: bool = False,
) -> int:
    facts = facts_from_message(message)
    if facts is None:
        return 0
    if ledger.known_message(facts.guild_id, facts.message_id):
        return 0
    window = ledger.snapshot_window(facts.guild_id, facts.channel_id)
    if window is not None and already_ingested(facts.message_id, window[0], window[1]):
        return 0
    if check_table and await ledger.recorded_message(sessions, facts.guild_id, facts.message_id):
        return 0
    partners: list[tuple[int, int, str]] = []
    if with_reactions and message.reactions:
        partners = await _reaction_partners(message)
    if not ledger.apply(facts):
        return 0
    authors.add(facts.user_id)
    collector.bump(
        facts.guild_id,
        facts.created_at,
        EventKind.MESSAGE,
        channel_id=facts.channel_id,
        user_id=facts.user_id,
    )
    for source_id, target_id, emoji in partners:
        if ledger.apply_reaction(
            facts.guild_id,
            source_id,
            target_id,
            facts.created_at,
            message_id=facts.message_id,
            emoji=emoji,
        ):
            report.reactions += 1
    return 1


async def _reaction_partners(message: discord.Message) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for reaction in message.reactions:
        token = emoji_key(reaction.emoji)
        try:
            async with _REACTION_FETCH:
                found.extend(
                    await asyncio.wait_for(
                        _collect_reaction_users(reaction, message, token),
                        timeout=REACTION_TIMEOUT_SECONDS,
                    )
                )
        except TimeoutError:
            log.warning("reaction_users_timeout", message_id=message.id, emoji=token)
        except discord.HTTPException:
            continue
    return found


async def _collect_reaction_users(
    reaction: discord.Reaction,
    message: discord.Message,
    token: str,
) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    async for user in reaction.users(limit=None):
        if user.bot or user.id == message.author.id:
            continue
        found.append((user.id, message.author.id, token))
    return found


async def _insert_ingested_messages(session: AsyncSession, rows: list[tuple[int, int, int]]) -> None:
    if not rows:
        return
    payload = [
        {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id}
        for guild_id, channel_id, message_id in rows
    ]
    for start in range(0, len(payload), 400):
        chunk = payload[start : start + 400]
        stmt = dialect_insert(session)(IngestedMessage).on_conflict_do_nothing(
            index_elements=["guild_id", "message_id"]
        )
        await session.execute(stmt, chunk)


async def _insert_ingested_reactions(session: AsyncSession, rows: list[tuple[int, int, int, str]]) -> None:
    if not rows:
        return
    payload = [
        {
            "guild_id": guild_id,
            "message_id": message_id,
            "source_id": source_id,
            "emoji": emoji,
        }
        for guild_id, message_id, source_id, emoji in rows
    ]
    for start in range(0, len(payload), 400):
        chunk = payload[start : start + 400]
        stmt = dialect_insert(session)(IngestedReaction).on_conflict_do_nothing(
            index_elements=["guild_id", "message_id", "source_id", "emoji"]
        )
        await session.execute(stmt, chunk)


async def _increment_edge(
    session: AsyncSession,
    guild_id: int,
    source_id: int,
    target_id: int,
    kind: str,
    amount: int,
    last_at: datetime | None,
) -> None:
    row = await session.scalar(
        select(InteractionEdge).where(
            InteractionEdge.guild_id == guild_id,
            InteractionEdge.source_id == source_id,
            InteractionEdge.target_id == target_id,
            InteractionEdge.kind == kind,
        )
    )
    if row is None:
        session.add(
            InteractionEdge(
                guild_id=guild_id,
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                count=amount,
                last_at=last_at,
            )
        )
        return
    row.count += amount
    if last_at is not None and (row.last_at is None or as_utc(last_at) >= as_utc(row.last_at)):
        row.last_at = last_at


async def _increment_member_channel(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    channel_id: int,
    *,
    messages: int,
    attachments: int,
    replies: int,
    last_at: datetime,
) -> None:
    row = await session.scalar(
        select(MemberChannel).where(
            MemberChannel.guild_id == guild_id,
            MemberChannel.user_id == user_id,
            MemberChannel.channel_id == channel_id,
        )
    )
    if row is None:
        session.add(
            MemberChannel(
                guild_id=guild_id,
                user_id=user_id,
                channel_id=channel_id,
                messages=messages,
                attachments=attachments,
                replies=replies,
                last_at=last_at,
            )
        )
        return
    row.messages += messages
    row.attachments += attachments
    row.replies += replies
    if row.last_at is None or as_utc(last_at) >= as_utc(row.last_at):
        row.last_at = last_at


async def _increment_member_content(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    messages: int,
    attachments: int,
    stickers: int,
    replies: int,
    links: int,
    first_message_at: datetime | None,
    last_message_at: datetime | None,
    names: tuple[str, str] | None,
) -> None:
    member = await session.scalar(
        select(MemberRecord).where(MemberRecord.guild_id == guild_id, MemberRecord.user_id == user_id)
    )
    if member is None:
        return
    member.message_count = (member.message_count or 0) + messages
    member.attachment_count = (member.attachment_count or 0) + attachments
    member.sticker_count = (member.sticker_count or 0) + stickers
    member.reply_count = (member.reply_count or 0) + replies
    member.link_count = (member.link_count or 0) + links
    if first_message_at is not None:
        stored = member.first_message_at
        if stored is None or as_utc(first_message_at) < as_utc(stored):
            member.first_message_at = first_message_at
    if last_message_at is not None:
        stored_last = member.last_message_at
        if stored_last is None or as_utc(last_message_at) > as_utc(stored_last):
            member.last_message_at = last_message_at
    if names:
        member.username = names[0][:40]
        member.display_name = names[1][:80]


async def _upsert_scan(
    session: AsyncSession,
    guild_id: int,
    channel_id: int,
    oldest: int,
    newest: int,
    ingested: int,
    scanned_at: datetime,
    *,
    caught_up: bool,
) -> None:
    row = await session.scalar(
        select(ChannelScan).where(ChannelScan.guild_id == guild_id, ChannelScan.channel_id == channel_id)
    )
    if row is None:
        session.add(
            ChannelScan(
                guild_id=guild_id,
                channel_id=channel_id,
                oldest_message_id=oldest,
                newest_message_id=newest,
                messages_ingested=ingested,
                scanned_at=scanned_at,
                caught_up=caught_up,
            )
        )
        return
    row.oldest_message_id = min(row.oldest_message_id, oldest)
    row.newest_message_id = max(row.newest_message_id, newest)
    row.messages_ingested = ingested
    row.scanned_at = scanned_at
    row.caught_up = caught_up or row.caught_up
