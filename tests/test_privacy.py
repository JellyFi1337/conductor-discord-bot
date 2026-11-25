from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import (
    ActivityBucket,
    AutomodHit,
    AutomodRequest,
    Base,
    ChannelDaily,
    Gathering,
    GatheringRsvp,
    Giveaway,
    GiveawayEntry,
    GiveawayWinner,
    GroupMember,
    GuildConfig,
    IngestedMessage,
    IngestedReaction,
    InteractionEdge,
    MemberDaily,
    MemberRecord,
    MemberWatch,
    Panel,
    Poll,
    PollVote,
    PrivateGroup,
    ReputationEvent,
    RoleChange,
    StaffAction,
    Ticket,
    TicketTranscript,
    VoiceRoom,
    VoiceRoomKick,
    VoiceRoomLfg,
    VoiceRoomRequest,
    VoiceSession,
    WatcherNote,
)
from conductor.db.session import session_scope
from conductor.domain.privacy import (
    BACKUP_KEEP,
    CACHE_TTL_SECONDS,
    FORGET_EXPORT_KEYS,
    FORGET_SENTINEL_KEYS,
    FORGOTTEN_REASON,
    FORGOTTEN_USER_ID,
    KIND_DERIVED,
    KIND_KEEP,
    PURGE_PERIOD,
    RETENTION,
    RETENTION_DAYS,
    backup_filename,
    backups_to_drop,
    export_filename,
    keep,
    retention_cutoff,
    snowflake_at,
)
from conductor.domain.records import CachedMessage
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.privacy import forget_confirm, forget_done
from conductor.services.cache import CacheHub, CacheMapping
from conductor.services.message_cache import MessageCache
from conductor.services.privacy import PrivacyService

STAMP = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _bot(
    sessions,
    tmp_path: Path,
    *,
    keep: int = 7,
    days: int = 90,
    ticket_transcript_days: int = 365,
) -> SimpleNamespace:
    cache = MessageCache(maxlen=20, ttl=60)
    forgotten: list[tuple[int, int]] = []
    return SimpleNamespace(
        sessions=sessions,
        settings=SimpleNamespace(
            message_cache_ttl=CACHE_TTL_SECONDS,
            data_retention_days=days,
            ticket_transcript_days=ticket_transcript_days,
            sqlite_backup_keep=keep,
            sqlite_path=tmp_path / "conductor.db",
        ),
        messages=cache,
        caches=CacheHub(),
        forget_voice=lambda guild_id, user_id: forgotten.append((guild_id, user_id)),
        forgotten=forgotten,
        scheduler=None,
    )


def test_defaults_are_explicit() -> None:
    assert CACHE_TTL_SECONDS == 3600
    assert RETENTION_DAYS == 90
    assert PURGE_PERIOD == 24 * 3600
    assert BACKUP_KEEP == 7
    assert FORGOTTEN_USER_ID == 0
    assert FORGOTTEN_REASON == "участник удалён"
    assert not (set(FORGET_EXPORT_KEYS) & FORGET_SENTINEL_KEYS)
    assert FORGET_EXPORT_KEYS["tickets_owned"] == "tickets"
    assert FORGET_EXPORT_KEYS["giveaway_entries"] == "giveaway_entries"
    assert export_filename(42) == "conductor-42.json"
    assert retention_cutoff(STAMP, days=90) == STAMP - timedelta(days=90)
    assert retention_cutoff(STAMP, days=0) is None
    assert backup_filename(STAMP) == "conductor-20260905-120000.db"


def test_readme_says_sqlite_snapshot_is_not_gdpr() -> None:
    text = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    assert "VACUUM INTO" in text
    assert "не для GDPR" in text


def test_backup_rotation_keeps_newest_names() -> None:
    folder = Path("unused")
    paths = [folder / backup_filename(STAMP + timedelta(hours=hour)) for hour in range(5)]
    dropped = backups_to_drop(paths, keep=2)
    assert [path.name for path in dropped] == [path.name for path in paths[:3]]


async def test_export_then_forget_keeps_anonymized_buckets(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions, tmp_path)
    service = PrivacyService(bot)
    old = STAMP - timedelta(days=3)
    try:
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    display_name="Ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                    invite_code="abc",
                    message_count=10,
                )
            )
            session.add(MemberDaily(guild_id=1, user_id=7, day=old, messages=4, voice_seconds=0, reactions=1))
            session.add(InteractionEdge(guild_id=1, source_id=7, target_id=9, kind="reply", count=2, last_at=old))
            session.add(ActivityBucket(guild_id=1, period_start=old, period_kind="day", event_kind="message", count=4))
            session.add(ChannelDaily(guild_id=1, channel_id=3, day=old, event_kind="message", count=4))
        payload, name = await service.export_person(1, 7, now=STAMP)
        assert name == "conductor-7.json"
        data = json.loads(payload.decode("utf-8"))
        assert set(FORGET_EXPORT_KEYS.values()) <= set(data)
        assert data["member"]["username"] == "ann"
        assert data["member"]["invite_code"] == "abc"
        assert data["daily"][0]["messages"] == 4
        assert data["anonymized_kept"]["activity_buckets"] is True
        bot.messages.store(
            CachedMessage(
                id=1,
                guild_id=1,
                channel_id=3,
                author_id=7,
                author_name="Ann",
                content="секрет",
                created_at=STAMP,
                attachments=(),
            )
        )
        report = await service.forget(1, 7)
        assert report.tables["members"] == 1
        assert report.tables["member_daily"] == 1
        assert report.tables["interaction_edges"] == 1
        assert report.cache == 1
        assert bot.forgotten == [(1, 7)]
        empty, _name = await service.export_person(1, 7, now=STAMP)
        gone = json.loads(empty.decode("utf-8"))
        assert gone["member"] is None
        assert gone["daily"] == []
        assert gone["anonymized_kept"]["activity_buckets"] is True
        assert gone["anonymized_kept"]["channel_daily"] is True
        async with session_scope(sessions) as session:
            buckets = await session.scalar(select(func.count()).select_from(ActivityBucket))
            channels = await session.scalar(select(func.count()).select_from(ChannelDaily))
            people = await session.scalar(select(func.count()).select_from(MemberRecord))
            leftover = await session.scalar(select(MemberRecord).where(MemberRecord.guild_id == 1))
        assert buckets == 1
        assert channels == 1
        assert people == 1
        assert leftover is not None
        assert leftover.user_id != 7
        assert leftover.username == ""
        assert leftover.invite_code == "abc"
        assert bot.messages.get(1) is None
        body = collected_text(forget_done(rows=report.db_rows, cache=report.cache))
        assert "Строк в базе" in body
        assert copy.DATA_FORGET_KEPT in body
    finally:
        await engine.dispose()


async def test_export_keys_cover_forget(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path))
    try:
        payload, _name = await service.export_person(1, 7, now=STAMP)
        data = json.loads(payload.decode("utf-8"))
        report = await service.forget(1, 7)
        assert set(FORGET_EXPORT_KEYS.values()) <= set(data)
        assert set(report.tables) == set(FORGET_EXPORT_KEYS) | FORGET_SENTINEL_KEYS
    finally:
        await engine.dispose()


async def test_export_lists_entries_votes_and_owned_ids(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path))
    later = STAMP + timedelta(hours=2)
    try:
        async with session_scope(sessions) as session:
            giveaway = Giveaway(
                guild_id=1,
                channel_id=20,
                owner_id=7,
                prize="роль",
                ends_at=later,
                created_at=STAMP,
            )
            poll = Poll(
                guild_id=1,
                channel_id=21,
                owner_id=8,
                question="кто?",
                options_json="[]",
                ends_at=later,
                created_at=STAMP,
            )
            gathering = Gathering(
                guild_id=1,
                channel_id=22,
                owner_id=8,
                title="рейд",
                starts_at=later,
                created_at=STAMP,
            )
            group = PrivateGroup(
                guild_id=1,
                owner_id=8,
                title="отряд",
                channel_id=23,
                created_at=STAMP,
                last_activity_at=STAMP,
            )
            owned_group = PrivateGroup(
                guild_id=1,
                owner_id=7,
                title="своя",
                channel_id=24,
                created_at=STAMP,
                last_activity_at=STAMP,
            )
            room = VoiceRoom(guild_id=1, channel_id=100, owner_id=8, hub_id=1, created_at=STAMP)
            own_room = VoiceRoom(guild_id=1, channel_id=101, owner_id=7, hub_id=1, created_at=STAMP)
            session.add_all([giveaway, poll, gathering, group, owned_group, room, own_room])
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=7,
                    category_id=1,
                    panel_id=1,
                    claimed_by=9,
                    form_answers="телефон",
                    channel_id=10,
                    created_at=STAMP,
                )
            )
            await session.flush()
            session.add(GiveawayEntry(giveaway_id=giveaway.id, user_id=7, created_at=STAMP))
            session.add(GiveawayWinner(giveaway_id=giveaway.id, user_id=7, round=1, place="main", created_at=STAMP))
            session.add(
                PollVote(
                    poll_id=poll.id,
                    user_id=7,
                    option_index=0,
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            session.add(
                GatheringRsvp(
                    gathering_id=gathering.id,
                    user_id=7,
                    status="going",
                    waitlisted=False,
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            session.add(GroupMember(group_id=group.id, user_id=7))
            session.add(VoiceRoomKick(room_id=room.id, user_id=7))
            session.add(VoiceRoomRequest(room_id=room.id, user_id=7, created_at=STAMP))
            session.add(VoiceRoomLfg(room_id=room.id, user_id=7))
        payload, _name = await service.export_person(1, 7, now=STAMP)
        data = json.loads(payload.decode("utf-8"))
        assert set(FORGET_EXPORT_KEYS.values()) <= set(data)
        assert data["tickets"][0]["claimed_by"] == 9
        assert data["tickets"][0]["form_answers"] == "телефон"
        assert data["giveaways"][0]["prize"] == "роль"
        assert data["giveaway_entries"][0]["giveaway_id"] == data["giveaways"][0]["id"]
        assert data["giveaway_winners"][0]["place"] == "main"
        assert data["poll_votes"][0]["option_index"] == 0
        assert data["gathering_rsvps"][0]["status"] == "going"
        assert data["group_members"][0]["group_id"] == group.id
        assert data["voice_room_kicks"][0]["room_id"] == room.id
        assert data["voice_room_requests"][0]["room_id"] == room.id
        assert data["voice_room_lfg"][0]["room_id"] == room.id
        assert data["voice_rooms"][0]["channel_id"] == 101
        assert data["groups"][0]["title"] == "своя"
        assert data["polls"] == []
        assert data["gatherings"] == []
        report = await service.forget(1, 7)
        assert report.tables["giveaway_entries"] == 1
        assert report.tables["giveaway_winners"] == 1
        assert report.tables["poll_votes"] == 1
        assert report.tables["gathering_rsvps"] == 1
        assert report.tables["group_members"] == 1
        assert report.tables["voice_room_kicks"] == 1
        empty, _gone = await service.export_person(1, 7, now=STAMP)
        gone = json.loads(empty.decode("utf-8"))
        assert gone["tickets"] == []
        assert gone["giveaways"] == []
        assert gone["giveaway_entries"] == []
        assert gone["giveaway_winners"] == []
        assert gone["poll_votes"] == []
        assert gone["gathering_rsvps"] == []
        assert gone["group_members"] == []
        assert gone["voice_room_kicks"] == []
        assert gone["voice_room_requests"] == []
        assert gone["voice_room_lfg"] == []
        assert gone["voice_rooms"] == []
        assert gone["groups"] == []
    finally:
        await engine.dispose()


async def _noop(_interaction: object) -> None:
    return None


def test_forget_cards_name_leftover_operations() -> None:
    assert "без хозяина" in copy.DATA_FORGET_HINT
    assert copy.DATA_FORGET_KEPT in copy.data_forget_body(rows=2, cache=0)
    confirm = collected_text(forget_confirm(on_yes=_noop, on_no=_noop))
    assert "без хозяина" in confirm
    assert "Счётчики сервера" in confirm


async def test_forget_strips_identity_from_owned_resources(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path))
    later = STAMP + timedelta(hours=2)
    try:
        async with session_scope(sessions) as session:
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=7,
                    category_id=1,
                    panel_id=1,
                    claimed_by=7,
                    closed_by=7,
                    close_reason="Ann просила закрыть",
                    form_answers="телефон 123",
                    staff_notes='[{"who":7}]',
                    channel_id=10,
                    created_at=STAMP,
                )
            )
            session.add(
                Ticket(
                    guild_id=1,
                    number=2,
                    owner_id=8,
                    category_id=1,
                    panel_id=1,
                    claimed_by=7,
                    closed_by=7,
                    close_reason="закрыл Ann",
                    form_answers="чужая анкета",
                    channel_id=11,
                    created_at=STAMP,
                )
            )
            session.add(
                Ticket(
                    guild_id=2,
                    number=1,
                    owner_id=7,
                    category_id=1,
                    panel_id=1,
                    channel_id=12,
                    created_at=STAMP,
                )
            )
            session.add(VoiceRoom(guild_id=1, channel_id=100, owner_id=0, hub_id=1, created_at=STAMP))
            session.add(VoiceRoom(guild_id=1, channel_id=101, owner_id=7, hub_id=1, created_at=STAMP))
            session.add(
                Giveaway(
                    guild_id=1,
                    channel_id=20,
                    owner_id=7,
                    prize="роль",
                    closed_by=7,
                    ends_at=later,
                    created_at=STAMP,
                )
            )
            session.add(
                Poll(
                    guild_id=1,
                    channel_id=21,
                    owner_id=7,
                    question="кто?",
                    options_json="[]",
                    closed_by=7,
                    deanonymized_by=7,
                    ends_at=later,
                    created_at=STAMP,
                )
            )
            session.add(
                Gathering(
                    guild_id=1,
                    channel_id=22,
                    owner_id=7,
                    title="рейд",
                    closed_by=7,
                    starts_at=later,
                    created_at=STAMP,
                )
            )
            session.add(
                PrivateGroup(
                    guild_id=1,
                    owner_id=7,
                    title="отряд",
                    channel_id=23,
                    created_at=STAMP,
                    last_activity_at=STAMP,
                    archived_by=7,
                )
            )
            session.add(WatcherNote(guild_id=1, user_id=7, author_id=8, body="о нём", created_at=STAMP))
            session.add(WatcherNote(guild_id=1, user_id=9, author_id=7, body="его перо", created_at=STAMP))
            session.add(WatcherNote(guild_id=1, user_id=9, author_id=8, body="чужая", created_at=STAMP))
            session.add(WatcherNote(guild_id=2, user_id=9, author_id=7, body="другой сервер", created_at=STAMP))
        report = await service.forget(1, 7)
        assert report.tables["tickets_owned"] == 1
        assert report.tables["tickets_claimed"] == 2
        assert report.tables["tickets_closed"] == 2
        assert report.tables["voice_rooms"] == 1
        assert report.tables["giveaways"] == 1
        assert report.tables["polls"] == 1
        assert report.tables["gatherings"] == 1
        assert report.tables["groups"] == 1
        assert report.tables["watcher_notes"] == 2
        async with session_scope(sessions) as session:
            tickets = (await session.scalars(select(Ticket).where(Ticket.guild_id == 1))).all()
            assert {row.number: row.owner_id for row in tickets} == {1: FORGOTTEN_USER_ID, 2: 8}
            owned = next(row for row in tickets if row.number == 1)
            assert owned.claimed_by is None
            assert owned.closed_by is None
            assert owned.form_answers == ""
            assert owned.staff_notes == "[]"
            assert owned.close_reason == FORGOTTEN_REASON
            claimed = next(row for row in tickets if row.number == 2)
            assert claimed.claimed_by is None
            assert claimed.closed_by is None
            assert claimed.close_reason == FORGOTTEN_REASON
            assert claimed.form_answers == "чужая анкета"
            other_guild = await session.scalar(select(Ticket).where(Ticket.guild_id == 2, Ticket.owner_id == 7))
            assert other_guild is not None
            rooms = (await session.scalars(select(VoiceRoom).where(VoiceRoom.guild_id == 1))).all()
            by_channel = {row.channel_id: row for row in rooms}
            assert by_channel[100].owner_id == FORGOTTEN_USER_ID
            assert by_channel[101].owner_id == -int(by_channel[101].id)
            assert 7 not in {row.owner_id for row in rooms}
            giveaway = await session.scalar(select(Giveaway).where(Giveaway.guild_id == 1))
            assert giveaway is not None
            assert giveaway.owner_id == FORGOTTEN_USER_ID
            assert giveaway.closed_by is None
            poll = await session.scalar(select(Poll).where(Poll.guild_id == 1))
            assert poll is not None
            assert poll.owner_id == FORGOTTEN_USER_ID
            assert poll.closed_by is None
            assert poll.deanonymized_by is None
            gathering = await session.scalar(select(Gathering).where(Gathering.guild_id == 1))
            assert gathering is not None
            assert gathering.owner_id == FORGOTTEN_USER_ID
            assert gathering.closed_by is None
            group = await session.scalar(select(PrivateGroup).where(PrivateGroup.guild_id == 1))
            assert group is not None
            assert group.owner_id == FORGOTTEN_USER_ID
            assert group.archived_by is None
            leftover = (await session.scalars(select(WatcherNote).where(WatcherNote.guild_id == 1))).all()
            assert [(row.user_id, row.author_id, row.body) for row in leftover] == [(9, 8, "чужая")]
            foreign = await session.scalar(
                select(WatcherNote).where(WatcherNote.guild_id == 2, WatcherNote.author_id == 7)
            )
            assert foreign is not None
            for model, field in (
                (Ticket, Ticket.owner_id),
                (Ticket, Ticket.claimed_by),
                (Ticket, Ticket.closed_by),
                (VoiceRoom, VoiceRoom.owner_id),
                (Giveaway, Giveaway.owner_id),
                (Giveaway, Giveaway.closed_by),
                (Poll, Poll.owner_id),
                (Poll, Poll.closed_by),
                (Poll, Poll.deanonymized_by),
                (Gathering, Gathering.owner_id),
                (Gathering, Gathering.closed_by),
                (PrivateGroup, PrivateGroup.owner_id),
                (PrivateGroup, PrivateGroup.archived_by),
                (WatcherNote, WatcherNote.user_id),
                (WatcherNote, WatcherNote.author_id),
            ):
                hit = await session.scalar(select(model.id).where(model.guild_id == 1, field == 7).limit(1))
                assert hit is None, f"{model.__tablename__}.{field.key}"
    finally:
        await engine.dispose()


async def test_forget_covers_new_tables_and_keeps_staff_actions(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path))
    later = STAMP + timedelta(days=1)
    try:
        async with session_scope(sessions) as session:
            session.add(
                AutomodHit(
                    guild_id=1,
                    user_id=7,
                    rule="caps",
                    channel_id=3,
                    message_id=11,
                    created_at=STAMP,
                )
            )
            session.add(
                AutomodRequest(
                    guild_id=1,
                    author_id=7,
                    rule="invite",
                    proposal="поднять лимит",
                    reason="часто ложные",
                    created_at=STAMP,
                )
            )
            session.add(
                MemberWatch(
                    guild_id=1,
                    user_id=7,
                    set_by=8,
                    reason="проверка",
                    until=later,
                    created_at=STAMP,
                )
            )
            session.add(
                StaffAction(
                    guild_id=1,
                    actor_id=7,
                    actor_rank=1,
                    action="warn",
                    target_id=9,
                    ref="1",
                    details="замечание",
                    created_at=STAMP,
                )
            )
            session.add(
                StaffAction(
                    guild_id=1,
                    actor_id=8,
                    actor_rank=2,
                    action="note",
                    target_id=7,
                    ref="2",
                    details="чужое",
                    created_at=STAMP,
                )
            )
        payload, _name = await service.export_person(1, 7, now=STAMP)
        data = json.loads(payload.decode("utf-8"))
        assert data["automod_hits"][0]["rule"] == "caps"
        assert data["automod_requests"][0]["author_id"] == 7
        assert data["member_watch"][0]["user_id"] == 7
        assert {row["actor_id"] for row in data["staff_actions"]} == {7, 8}
        report = await service.forget(1, 7)
        assert report.tables["automod_hits"] == 1
        assert report.tables["automod_requests"] == 1
        assert report.tables["member_watch"] == 1
        assert report.tables["staff_actions"] == 2
        empty, _gone = await service.export_person(1, 7, now=STAMP)
        gone = json.loads(empty.decode("utf-8"))
        assert gone["automod_hits"] == []
        assert gone["automod_requests"] == []
        assert gone["member_watch"] == []
        assert gone["staff_actions"] == []
        async with session_scope(sessions) as session:
            hits = await session.scalar(select(func.count()).select_from(AutomodHit))
            requests = await session.scalar(select(func.count()).select_from(AutomodRequest))
            watches = await session.scalar(select(func.count()).select_from(MemberWatch))
            actions = list((await session.scalars(select(StaffAction).where(StaffAction.guild_id == 1))).all())
        assert hits == 0
        assert requests == 0
        assert watches == 0
        assert len(actions) == 2
        assert {row.actor_id for row in actions} == {FORGOTTEN_USER_ID, 8}
        assert {row.target_id for row in actions} == {FORGOTTEN_USER_ID, 9}
        assert 7 not in {row.actor_id for row in actions}
        assert 7 not in {row.target_id for row in actions}
    finally:
        await engine.dispose()


async def test_retention_drops_old_derived_rows(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path, days=90))
    stale = STAMP - timedelta(days=100)
    fresh = STAMP - timedelta(days=2)
    try:
        async with session_scope(sessions) as session:
            session.add(MemberDaily(guild_id=1, user_id=7, day=stale, messages=1))
            session.add(MemberDaily(guild_id=1, user_id=7, day=fresh, messages=2))
            session.add(ChannelDaily(guild_id=1, channel_id=3, day=stale, event_kind="message", count=1))
            session.add(ChannelDaily(guild_id=1, channel_id=3, day=fresh, event_kind="message", count=2))
            session.add(
                ActivityBucket(guild_id=1, period_start=stale, period_kind="day", event_kind="message", count=1)
            )
            session.add(
                ActivityBucket(guild_id=1, period_start=fresh, period_kind="day", event_kind="message", count=2)
            )
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=7,
                    channel_id=9,
                    joined_at=stale,
                    left_at=stale,
                    duration_seconds=60,
                    last_event_at=stale,
                )
            )
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=8,
                    channel_id=9,
                    joined_at=fresh,
                    left_at=fresh,
                    duration_seconds=60,
                    last_event_at=fresh,
                )
            )
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=9,
                    channel_id=9,
                    joined_at=STAMP - timedelta(days=3),
                    left_at=None,
                    last_event_at=STAMP - timedelta(days=3),
                )
            )
            session.add(
                ReputationEvent(
                    guild_id=1,
                    user_id=7,
                    kind="growth",
                    delta=1,
                    cut=0,
                    score_after=100,
                    created_at=stale,
                )
            )
            session.add(
                ReputationEvent(
                    guild_id=1,
                    user_id=8,
                    kind="growth",
                    delta=1,
                    cut=0,
                    score_after=100,
                    created_at=fresh,
                )
            )
        removed = await service.purge_tick(now=STAMP)
        assert removed == 5
        async with session_scope(sessions) as session:
            daily = (await session.scalars(select(MemberDaily))).all()
            assert len(daily) == 1
            assert daily[0].messages == 2
            channels = (await session.scalars(select(ChannelDaily))).all()
            assert len(channels) == 1
            buckets = (await session.scalars(select(ActivityBucket))).all()
            assert len(buckets) == 1
            voices = (await session.scalars(select(VoiceSession))).all()
            assert {(row.user_id, row.left_at is None) for row in voices} == {(8, False), (9, False)}
            hung = next(row for row in voices if row.user_id == 9)
            assert hung.truncated is True
            assert hung.left_at is not None
            events = (await session.scalars(select(ReputationEvent))).all()
            assert [row.user_id for row in events] == [8]
        assert await service.purge_tick(now=STAMP) == 0
    finally:
        await engine.dispose()


async def test_purge_report_reaches_each_admin_log_once(tmp_path: Path) -> None:
    """The report walks the live ``CacheMapping`` (not a dict) and dedupes shared channels."""
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions, tmp_path)
    bot.config_by_guild = CacheMapping(bot.caches.guild_config)
    bot.config_by_guild[1] = SimpleNamespace(admin_log_channel_id=500)
    bot.config_by_guild[2] = SimpleNamespace(admin_log_channel_id=500)
    bot.config_by_guild[3] = SimpleNamespace(admin_log_channel_id=None)
    submitted: list[int] = []
    bot.log_pump = SimpleNamespace(submit=lambda channel_id, **_kwargs: submitted.append(channel_id))
    service = PrivacyService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(MemberDaily(guild_id=1, user_id=7, day=STAMP - timedelta(days=400), messages=1))
        assert await service.purge_tick(now=STAMP, pause=0) == 1
        assert submitted == [500]
    finally:
        await engine.dispose()


async def test_retention_zero_keeps_derived_rows(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path, days=0))
    try:
        async with session_scope(sessions) as session:
            session.add(MemberDaily(guild_id=1, user_id=7, day=STAMP - timedelta(days=400), messages=1))
        assert await service.purge_tick(now=STAMP) == 0
    finally:
        await engine.dispose()


async def test_forget_scrubs_name_from_ticket_transcript(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path))
    try:
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    display_name="Ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=7,
                    category_id=1,
                    panel_id=1,
                    state="closed",
                    claimed_rank=0,
                    channel_id=10,
                    created_at=STAMP,
                    closed_at=STAMP,
                )
            )
            session.add(
                TicketTranscript(
                    ticket_id=1,
                    guild_id=1,
                    body="Ann (<@7>) написала: секрет\nследящий: ок",
                    created_at=STAMP,
                )
            )
        payload, _name = await service.export_person(1, 7, now=STAMP)
        data = json.loads(payload.decode("utf-8"))
        assert data["ticket_transcripts"][0]["body"].startswith("Ann")
        report = await service.forget(1, 7)
        assert report.tables["ticket_transcripts"] == 1
        async with session_scope(sessions) as session:
            row = await session.get(TicketTranscript, 1)
        assert row is not None
        assert "Ann" not in row.body
        assert "ann" not in row.body
        assert "<@7>" not in row.body
        assert FORGOTTEN_REASON in row.body
    finally:
        await engine.dispose()


async def test_ticket_transcript_retention_on_fake_clock(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path, days=0, ticket_transcript_days=30))
    stale = STAMP - timedelta(days=40)
    fresh = STAMP - timedelta(days=5)
    try:
        async with session_scope(sessions) as session:
            session.add(TicketTranscript(ticket_id=1, guild_id=1, body="старый", created_at=stale))
            session.add(TicketTranscript(ticket_id=2, guild_id=1, body="свежий", created_at=fresh))
        removed = await service.purge_tick(now=STAMP)
        assert removed == 1
        async with session_scope(sessions) as session:
            rows = list((await session.scalars(select(TicketTranscript))).all())
        assert [row.ticket_id for row in rows] == [2]
        assert await service.purge_tick(now=STAMP) == 0
    finally:
        await engine.dispose()


async def test_sqlite_backup_roundtrip_and_rotation(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path, keep=2))
    try:
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=7,
                    username="ann",
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                )
            )
        first = await service.backup_tick(now=STAMP)
        assert first is not None and first.is_file()
        copy_engine = create_engine(f"sqlite+aiosqlite:///{first.as_posix()}")
        try:
            copy_sessions = create_sessionmaker(copy_engine)
            async with session_scope(copy_sessions) as session:
                row = await session.scalar(select(MemberRecord).where(MemberRecord.user_id == 7))
                assert row is not None
                assert row.username == "ann"
        finally:
            await copy_engine.dispose()
        await service.backup_tick(now=STAMP + timedelta(hours=1))
        await service.backup_tick(now=STAMP + timedelta(hours=2))
        folder = tmp_path / "backups"
        left = sorted(path.name for path in folder.glob("conductor-*.db"))
        assert len(left) == 2
        assert backup_filename(STAMP) not in left
    finally:
        await engine.dispose()


async def test_postgres_url_skips_sqlite_backup(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions, tmp_path, keep=7)
    bot.settings.sqlite_path = None
    service = PrivacyService(bot)
    try:
        assert await service.backup_tick(now=STAMP) is None
        assert not (tmp_path / "backups").exists()
    finally:
        await engine.dispose()


def test_retention_covers_every_table() -> None:
    assert set(RETENTION) == set(Base.metadata.tables)
    for name, rule in RETENTION.items():
        if rule.kind == KIND_KEEP:
            assert rule.reason, name
        if rule.kind == KIND_DERIVED:
            assert rule.parent in RETENTION


def test_keep_requires_reason() -> None:
    try:
        keep("")
    except ValueError:
        return
    raise AssertionError("empty keep reason")


async def test_retention_rules_on_fixture(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    service = PrivacyService(_bot(sessions, tmp_path, days=90))
    old_panel = STAMP - timedelta(days=8)
    fresh_panel = STAMP - timedelta(days=2)
    old_roles = STAMP - timedelta(days=181)
    old_staff = STAMP - timedelta(days=366)
    old_hit = STAMP - timedelta(days=91)
    old_snow = snowflake_at(STAMP - timedelta(days=100))
    fresh_snow = snowflake_at(STAMP - timedelta(days=2))
    try:
        async with session_scope(sessions) as session:
            session.add(
                Panel(
                    kind="ticket",
                    guild_id=1,
                    channel_id=2,
                    message_id=10,
                    ref="a",
                    created_by=1,
                    created_at=old_panel,
                    state="stale",
                )
            )
            session.add(
                Panel(
                    kind="ticket",
                    guild_id=1,
                    channel_id=2,
                    message_id=11,
                    ref="b",
                    created_by=1,
                    created_at=old_panel,
                    state="live",
                )
            )
            session.add(
                Panel(
                    kind="ticket",
                    guild_id=1,
                    channel_id=2,
                    message_id=12,
                    ref="c",
                    created_by=1,
                    created_at=fresh_panel,
                    state="stale",
                )
            )
            session.add(
                RoleChange(
                    guild_id=1,
                    user_id=7,
                    subsystem="loyalty",
                    add_role_ids="[]",
                    remove_role_ids="[]",
                    reason="тест",
                    created_at=old_roles,
                )
            )
            session.add(
                RoleChange(
                    guild_id=1,
                    user_id=8,
                    subsystem="loyalty",
                    add_role_ids="[]",
                    remove_role_ids="[]",
                    reason="свежий",
                    created_at=STAMP - timedelta(days=10),
                )
            )
            session.add(
                StaffAction(
                    guild_id=1,
                    actor_id=2,
                    action="note",
                    created_at=old_staff,
                )
            )
            session.add(
                StaffAction(
                    guild_id=1,
                    actor_id=2,
                    action="note",
                    created_at=STAMP - timedelta(days=10),
                )
            )
            session.add(
                AutomodHit(
                    guild_id=1,
                    user_id=7,
                    rule="spam",
                    channel_id=3,
                    message_id=1,
                    created_at=old_hit,
                )
            )
            session.add(
                AutomodHit(
                    guild_id=1,
                    user_id=8,
                    rule="spam",
                    channel_id=3,
                    message_id=2,
                    created_at=STAMP - timedelta(days=2),
                )
            )
            session.add(IngestedMessage(guild_id=1, channel_id=3, message_id=old_snow))
            session.add(IngestedMessage(guild_id=1, channel_id=3, message_id=fresh_snow))
            session.add(IngestedReaction(guild_id=1, message_id=old_snow, source_id=7, emoji="👍"))
            session.add(IngestedReaction(guild_id=1, message_id=fresh_snow, source_id=7, emoji="👍"))
            session.add(
                ReputationEvent(
                    guild_id=1,
                    user_id=7,
                    kind="growth",
                    delta=1,
                    cut=0,
                    score_after=50,
                    created_at=STAMP - timedelta(days=100),
                )
            )
            session.add(
                ReputationEvent(
                    guild_id=1,
                    user_id=8,
                    kind="hit",
                    delta=-10,
                    cut=0,
                    score_after=40,
                    case_id=1,
                    created_at=STAMP - timedelta(days=100),
                )
            )
        removed = await service.purge_tick(now=STAMP, pause=0)
        assert removed >= 7
        async with session_scope(sessions) as session:
            panels = list((await session.scalars(select(Panel))).all())
            assert {row.message_id for row in panels} == {11, 12}
            roles = list((await session.scalars(select(RoleChange))).all())
            assert [row.user_id for row in roles] == [8]
            actions = list((await session.scalars(select(StaffAction))).all())
            assert len(actions) == 1
            hits = list((await session.scalars(select(AutomodHit))).all())
            assert [row.user_id for row in hits] == [8]
            messages = list((await session.scalars(select(IngestedMessage))).all())
            assert [row.message_id for row in messages] == [fresh_snow]
            reactions = list((await session.scalars(select(IngestedReaction))).all())
            assert [row.message_id for row in reactions] == [fresh_snow]
            events = list((await session.scalars(select(ReputationEvent))).all())
            assert [row.user_id for row in events] == [8]
            assert events[0].case_id == 1
    finally:
        await engine.dispose()


class _FakeMessage:
    def __init__(self, author_id: int, created_at) -> None:
        self.author = SimpleNamespace(id=author_id)
        self.created_at = created_at
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _FakeHistory:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    def __aiter__(self):
        async def _gen():
            for item in self._messages:
                yield item

        return _gen()


class _FakeChannel:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    def history(self, limit=None, *, before=None, oldest_first=False, **_kwargs):
        selected = [item for item in self._messages if before is None or item.created_at < before]
        ordered = sorted(selected, key=lambda item: item.created_at, reverse=not oldest_first)
        return _FakeHistory(ordered)


async def test_user_log_cards_are_purged_by_keep_days(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot_id = 99
    old = _FakeMessage(bot_id, STAMP - timedelta(days=100))
    human = _FakeMessage(7, STAMP - timedelta(days=100))
    fresh = _FakeMessage(bot_id, STAMP - timedelta(days=2))
    channel = _FakeChannel([old, human, fresh])
    bot = _bot(sessions, tmp_path, days=0)
    bot.user = SimpleNamespace(id=bot_id)
    bot.get_guild = lambda _gid: SimpleNamespace(get_channel=lambda _cid: channel)
    service = PrivacyService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, user_log_channel_id=5, user_log_keep_days=90))
        assert await service.purge_tick(now=STAMP, pause=0) == 0
        assert old.deleted is True
        assert human.deleted is False
        assert fresh.deleted is False
    finally:
        await engine.dispose()
