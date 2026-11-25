from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildLayout, ModCase, ModEvidence, ModSettings
from conductor.db.session import session_scope
from conductor.domain.access import MOD_KICK, AccessSnapshot, PermissionRule, Rank, empty_rank_roles
from conductor.domain.admin import AdminCtx
from conductor.domain.create_lock import hold_from_wait
from conductor.domain.moderation import (
    CaseKind,
    EvidenceDenial,
    ModVerdict,
    StoredCase,
    dm_deliverable,
    may_evidence_close,
    may_evidence_reason,
    needs_evidence,
)
from conductor.presentation import copy
from conductor.presentation.interact import run_teardown, split_teardown
from conductor.presentation.layout import collected_text
from conductor.presentation.moderation import case_dm_view
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.admin_pages import page_by_id
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.evidence import EvidenceResult, EvidenceService
from conductor.services.moderation import ModerationService
from conductor.services.role_broker import RoleApplyResult
from conductor.services.transcripts import collect_transcript, format_transcript_message

WATCHER_ROLE = 11
CHIEF_ROLE = 33
REASON = "нарушение правил чата"
DETAIL = "подробное объяснение наказания для участника"


async def _closed(packed: object) -> EvidenceResult:
    body, teardown = split_teardown(packed)
    await run_teardown(teardown)
    assert isinstance(body, EvidenceResult)
    return body


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _member(user_id: int, role_ids: tuple[int, ...] = (), *, admin: bool = False) -> SimpleNamespace:
    dms: list[object] = []

    async def send(**kwargs: object) -> None:
        dms.append(kwargs)

    return SimpleNamespace(
        id=user_id,
        bot=False,
        mention=f"<@{user_id}>",
        display_name=f"user-{user_id}",
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=admin),
        send=send,
        dms=dms,
    )


def _ranked_snapshot() -> AccessSnapshot:
    return AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.WATCHER: (WATCHER_ROLE,), Rank.CHIEF: (CHIEF_ROLE,), Rank.SPECIAL: (44,)},
        rules={MOD_KICK: PermissionRule(MOD_KICK, frozenset({Rank.CHIEF, Rank.SPECIAL}))},
        special_locked=True,
    )


def _bot(sessions, *, timeline: list[str] | None = None) -> SimpleNamespace:
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        user=SimpleNamespace(id=99),
        outbound=None,
        roles=_FakeRoles(),
        log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
        config_by_guild={1: SimpleNamespace(user_log_channel_id=10, admin_log_channel_id=11, timezone="UTC")},
        queue=SimpleNamespace(schedule_later=lambda *_a, **_k: None, cancel_later=lambda *_a, **_k: None),
        scheduler=None,
        get_guild=lambda _gid: None,
    )
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), _ranked_snapshot())
    bot.evidence = EvidenceService(bot)
    bot.timeline = timeline if timeline is not None else []
    return bot


class _FakeRoles:
    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def apply(self, _claim: object) -> RoleApplyResult:
        return RoleApplyResult(ok=True)


class _Category:
    type = discord.ChannelType.category

    def __init__(self, channel_id: int = 50, *, manage_channels: bool | None = None) -> None:
        self.id = channel_id
        if manage_channels is not None:
            granted = manage_channels
            self.permissions_for = lambda _member, allowed=granted: SimpleNamespace(manage_channels=allowed)


class _TextChannel:
    def __init__(self, channel_id: int, *, posts: list | None = None) -> None:
        self.id = channel_id
        self.posts = posts if posts is not None else []
        self.deleted = False
        self.category = None
        self.history_rows: list[object] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.posts.append(kwargs)
        return SimpleNamespace(id=900 + len(self.posts))

    def history(self, limit: object = None, oldest_first: bool = False):
        async def _gen():
            for row in self.history_rows:
                yield row

        return _gen()

    async def delete(self, reason: str | None = None) -> None:
        self.deleted = True

    async def edit(self, *, category: object | None = None, reason: str | None = None) -> None:
        self.category = category


class _Guild:
    def __init__(self, *, timeline: list[str] | None = None) -> None:
        self.id = 1
        self.owner_id = 1
        self.default_role = SimpleNamespace(id=1)
        self.me = SimpleNamespace(id=99)
        self.members: dict[int, object] = {}
        self.channels: dict[int, object] = {50: _Category(50)}
        self.roles: dict[int, object] = {CHIEF_ROLE: SimpleNamespace(id=CHIEF_ROLE)}
        self.bans: list[object] = []
        self.timeline = timeline if timeline is not None else []
        self.created: list[object] = []

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)

    def get_channel(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)

    def get_role(self, role_id: int) -> object | None:
        return self.roles.get(role_id)

    async def create_text_channel(
        self,
        name: str,
        category: object = None,
        overwrites: object = None,
        reason: str | None = None,
    ):
        channel = _TextChannel(80 + len(self.created))
        self.created.append((name, category, overwrites))
        self.channels[channel.id] = channel
        return channel

    async def ban(self, target: object, delete_message_seconds: int = 0, reason: str | None = None) -> None:
        self.timeline.append("ban")
        self.bans.append((int(target.id), delete_message_seconds, reason))


def test_needs_evidence_matrix() -> None:
    staff = (
        (CaseKind.WARN, True),
        (CaseKind.TIMEOUT, True),
        (CaseKind.MUTE, True),
        (CaseKind.KICK, True),
        (CaseKind.BAN, True),
        (CaseKind.SOFTBAN, True),
        (CaseKind.UNMUTE, False),
        (CaseKind.UNBAN, False),
        (CaseKind.PURGE, False),
        (CaseKind.LOCK, False),
        (CaseKind.NOTE, False),
    )
    for kind, expected in staff:
        assert needs_evidence(kind, False) is expected
        assert needs_evidence(kind, True) is False
    assert dm_deliverable(CaseKind.TIMEOUT) is True
    assert dm_deliverable(CaseKind.BAN) is False
    assert dm_deliverable(CaseKind.KICK) is False


def test_stranger_cannot_press_reason_or_close() -> None:
    assert may_evidence_reason(actor_id=2, created_by=2, has_close_key=False) is True
    assert may_evidence_reason(actor_id=9, created_by=2, has_close_key=False) is False
    assert may_evidence_reason(actor_id=9, created_by=2, has_close_key=True) is True
    assert may_evidence_close(has_close_key=False) is False
    assert may_evidence_close(has_close_key=True) is True


def test_dm_cards_reserve_appeal_slot() -> None:
    case = StoredCase(
        id=1,
        guild_id=1,
        number=4,
        owner_id=3,
        created_by=2,
        actor_rank=1,
        kind=CaseKind.TIMEOUT,
        reason=REASON,
        duration_seconds=7200,
    )
    plain = collected_text(case_dm_view(case, rank="следящий", duration="2 ч"))
    assert "Дело #4" in plain
    assert copy.CASE_APPEAL not in plain
    with_appeal = collected_text(case_dm_view(case, rank="следящий", duration="2 ч", appeal_button=True))
    assert copy.CASE_APPEAL in with_appeal


def test_transcript_lists_attachment_urls() -> None:
    message = SimpleNamespace(
        created_at=datetime(2026, 9, 7, 12, tzinfo=UTC),
        author=SimpleNamespace(display_name="Ann"),
        content="скрин",
        attachments=(SimpleNamespace(url="https://cdn.example/a.png"),),
    )
    line = format_transcript_message(message)
    assert "Ann" in line
    assert "https://cdn.example/a.png" in line


async def test_collect_transcript_empty_channel() -> None:
    text, count = await collect_transcript(SimpleNamespace())
    assert text == copy.TICKET_TRANSCRIPT_EMPTY
    assert count == 0


async def test_timeout_sends_dm_and_opens_channel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    target.timeout = timeout
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50, evidence_archive_channel_id=None))
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="2h",
            now=datetime(2026, 9, 7, 8, tzinfo=UTC),
        )
        assert outcome.verdict is ModVerdict.OK
        assert target.dms
        assert "подробное объяснение" in collected_text(target.dms[0]["view"])
        assert guild.created
        async with session_scope(sessions) as session:
            row = await session.scalar(select(ModEvidence))
            assert row is not None
            assert row.dm1_ok is True
            assert row.channel_id is not None
            assert row.detail_reason is None
    finally:
        await engine.dispose()


async def test_dm1_happens_before_ban(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    timeline: list[str] = []
    bot = _bot(sessions, timeline=timeline)
    guild = _Guild(timeline=timeline)
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)

    async def send(**kwargs: object) -> None:
        timeline.append("dm")
        target.dms.append(kwargs)

    target.send = send
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        started = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=stamp)
        assert started.pending_id is not None
        outcome = await service.confirm_heavy(guild, started.pending_id, now=stamp)
        assert outcome.verdict is ModVerdict.OK
        assert timeline == ["dm", "ban"]
    finally:
        await engine.dispose()


async def test_forbidden_dm_does_not_fail_command(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)

    async def send(**_kwargs: object) -> None:
        raise discord.Forbidden(MagicMock(status=403), "blocked")

    target.send = send

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    target.timeout = timeout
    guild.members[2] = actor
    guild.members[3] = target
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="10m",
            now=datetime(2026, 9, 7, 8, tzinfo=UTC),
        )
        assert outcome.verdict is ModVerdict.OK
        async with session_scope(sessions) as session:
            row = await session.scalar(select(ModEvidence))
            assert row is not None
            assert row.dm1_ok is False
    finally:
        await engine.dispose()


async def test_send_reason_writes_detail_and_deletes_channel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    guild.channels[70] = channel
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            case = ModCase(
                guild_id=1,
                number=1,
                owner_id=3,
                created_by=2,
                actor_rank=1,
                kind=CaseKind.TIMEOUT,
                reason=REASON,
                duration_seconds=600,
                created_at=stamp,
            )
            session.add(case)
            await session.flush()
            evidence = ModEvidence(
                guild_id=1,
                case_id=case.id,
                channel_id=70,
                created_at=stamp,
                dm1_ok=True,
            )
            session.add(evidence)
            await session.flush()
            evidence_id = int(evidence.id)
        result = await _closed(await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp))
        assert result.ok is True
        assert result.dm2_ok is True
        assert channel.deleted is True
        assert target.dms
        async with session_scope(sessions) as session:
            row = await session.get(ModEvidence, evidence_id)
            assert row is not None
            assert row.detail_reason == DETAIL
            assert row.sent_at is not None
            assert row.transcript is not None
    finally:
        await engine.dispose()


async def test_sweep_reminds_once_then_escalates_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    channel = _TextChannel(70)
    guild.channels[70] = channel
    bot.get_guild = lambda _gid: guild
    created = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            session.add(ModSettings(guild_id=1, evidence_remind_hours=24))
            case = ModCase(
                guild_id=1,
                number=1,
                owner_id=3,
                created_by=2,
                actor_rank=1,
                kind=CaseKind.MUTE,
                reason=REASON,
                duration_seconds=None,
                created_at=created,
            )
            session.add(case)
            await session.flush()
            session.add(
                ModEvidence(
                    guild_id=1,
                    case_id=case.id,
                    channel_id=70,
                    created_at=created,
                    dm1_ok=True,
                )
            )
        await bot.evidence.sweep(now=created + timedelta(hours=24))
        await bot.evidence.sweep(now=created + timedelta(hours=24, minutes=10))
        remind = [item for item in channel.posts if copy.EVIDENCE_REMIND_TITLE in collected_text(item["view"])]
        assert len(remind) == 1
        users = getattr(remind[0]["allowed_mentions"], "users", None)
        assert users
        await bot.evidence.sweep(now=created + timedelta(hours=72))
        await bot.evidence.sweep(now=created + timedelta(hours=73))
        escalate = [item for item in channel.posts if copy.EVIDENCE_ESCALATE_TITLE in collected_text(item["view"])]
        assert len(escalate) == 1
    finally:
        await engine.dispose()


async def test_bot_actor_skips_evidence(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(99, (WATCHER_ROLE,))
    target = _member(3)

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    target.timeout = timeout
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="5m",
            now=datetime(2026, 9, 7, 8, tzinfo=UTC),
        )
        assert outcome.verdict is ModVerdict.OK
        async with session_scope(sessions) as session:
            assert await session.scalar(select(ModEvidence)) is None
        assert target.dms == []
    finally:
        await engine.dispose()


async def test_admin_moderation_page_writes_evidence(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
    page = page_by_id("mute")
    assert page is not None
    assert page.label == copy.ADMIN_PAGE_MUTE
    try:
        opened = await page.handle(ctx, "mute.evidence_hours", "")
        assert opened.modal == "number"
        written = await page.handle(ctx, "mute.evidence_hours.set", "12")
        assert written.ok is True
        bound = await page.handle(ctx, "mute.evidence_category", "50")
        assert bound.ok is True
        async with session_scope(sessions) as session:
            row = await session.get(ModSettings, 1)
            assert row is not None
            assert row.evidence_remind_hours == 12
            assert row.evidence_category_id == 50
    finally:
        await engine.dispose()


async def _open_evidence(sessions, *, channel_id: int = 70) -> int:
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        case = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add(case)
        await session.flush()
        evidence = ModEvidence(
            guild_id=1,
            case_id=case.id,
            channel_id=channel_id,
            created_at=stamp,
            dm1_ok=True,
        )
        session.add(evidence)
        await session.flush()
        return int(evidence.id)


async def test_second_send_reason_does_not_dm_twice(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    guild.channels[70] = channel
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        evidence_id = await _open_evidence(sessions)
        first_p, second_p = await asyncio.gather(
            bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp),
            bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp),
        )
        first, first_teardown = split_teardown(first_p)
        second, second_teardown = split_teardown(second_p)
        winners = [item for item in (first, second) if item.ok]
        assert len(winners) == 1
        stale = first if not first.ok else second
        assert stale.error == copy.EVIDENCE_ALREADY_CLOSED
        assert len(target.dms) == 1
        await run_teardown(first_teardown)
        await run_teardown(second_teardown)
        assert channel.deleted is True
    finally:
        await engine.dispose()


async def test_teardown_delete_failure_keeps_claim(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)

    async def boom(reason: str | None = None) -> None:
        raise discord.HTTPException(SimpleNamespace(status=500, reason="fail"), "fail")

    channel.delete = boom  # type: ignore[method-assign]
    guild.channels[70] = channel
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        evidence_id = await _open_evidence(sessions)
        result = await _closed(await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp))
        assert result.ok is True
        async with session_scope(sessions) as session:
            row = await session.get(ModEvidence, evidence_id)
        assert row is not None
        assert row.sent_at is not None
        assert row.closed_by == actor.id
        assert channel.deleted is False
    finally:
        await engine.dispose()


async def test_sweep_closes_missing_channel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = _Guild()
    bot.get_guild = lambda _gid: guild
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        evidence_id = await _open_evidence(sessions)
        await bot.evidence.sweep(now=stamp)
        async with session_scope(sessions) as session:
            row = await session.get(ModEvidence, evidence_id)
        assert row is not None
        assert row.sent_at is not None
        assert row.closed_by == 0
    finally:
        await engine.dispose()


def _timeout_case(case_id: int = 1) -> StoredCase:
    return StoredCase(
        id=case_id,
        guild_id=1,
        number=case_id,
        owner_id=3,
        created_by=2,
        actor_rank=1,
        kind=CaseKind.TIMEOUT,
        reason=REASON,
        duration_seconds=600,
        created_at=datetime(2026, 9, 7, 8, tzinfo=UTC),
    )


class _FakeSeed:
    def __init__(self, guild: _Guild, sessions, *, bind: bool = True) -> None:
        self.calls: list[str] = []
        self._guild = guild
        self._sessions = sessions
        self._bind = bind

    async def repair(self, guild: object, key: str, *, actor_id: int, mode: object = None) -> SimpleNamespace:
        self.calls.append(str(key))
        if not self._bind:
            return SimpleNamespace()
        guild.channels[50] = _Category(50)
        async with session_scope(self._sessions) as session:
            row = await session.get(ModSettings, int(guild.id))
            if row is None:
                session.add(ModSettings(guild_id=int(guild.id), evidence_category_id=50))
            else:
                row.evidence_category_id = 50
        return SimpleNamespace()

    async def reapply_rights(self, guild: object, *, actor_id: int, keys: object = None) -> SimpleNamespace:
        self.calls.append("rights")
        return SimpleNamespace()


async def _engine(tmp_path: Path, name: str):
    folder = tmp_path / name
    folder.mkdir()
    engine = create_engine(_url(folder))
    await apply_migrations(engine)
    return engine, create_sessionmaker(engine)


async def test_every_evidence_denial_reaches_result(tmp_path: Path) -> None:
    seen: dict[EvidenceDenial, object] = {}
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)

    engine, sessions = await _engine(tmp_path, "no_category")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels.clear()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.ok is False
        assert result.denial is EvidenceDenial.NO_CATEGORY
        assert result.error == copy.EVIDENCE_DENIAL_NO_CATEGORY
        assert actor.dms
    finally:
        await engine.dispose()

    engine, sessions = await _engine(tmp_path, "gone")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels.clear()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.denial is EvidenceDenial.CATEGORY_GONE
    finally:
        await engine.dispose()

    engine, sessions = await _engine(tmp_path, "not_category")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    text = _TextChannel(70)
    guild.channels = {70: text}
    guild.members[2] = actor
    guild.members[3] = target
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=70))
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.denial is EvidenceDenial.NOT_A_CATEGORY
        assert not guild.created
    finally:
        await engine.dispose()

    engine, sessions = await _engine(tmp_path, "bot_perms")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels[50] = _Category(50, manage_channels=False)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.denial is EvidenceDenial.BOT_PERMS
        assert not guild.created
    finally:
        await engine.dispose()

    engine, sessions = await _engine(tmp_path, "held")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))

    async def held(*_args: object, **_kwargs: object):
        return hold_from_wait(bucket="channel", wait=60, now=0.0)

    try:
        with patch("conductor.services.evidence.hold_for", held):
            result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.denial is EvidenceDenial.HELD
        assert not guild.created
    finally:
        await engine.dispose()

    engine, sessions = await _engine(tmp_path, "failed")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise discord.HTTPException(MagicMock(status=500), "fail")

    guild.create_text_channel = boom
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        seen[result.denial] = result
        assert result.denial is EvidenceDenial.FAILED
    finally:
        await engine.dispose()

    assert set(seen) == set(EvidenceDenial)


async def test_ensure_category_repairs_once_then_creates(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "repair_create")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels.clear()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    seed = _FakeSeed(guild, sessions, bind=True)
    bot.seed = seed
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        assert result.ok is True
        assert result.denial is None
        assert seed.calls == ["cat_evidence"]
        assert guild.created
        created_category = guild.created[0][1]
        assert created_category is guild.channels[50]
        assert getattr(created_category, "type", None) is discord.ChannelType.category
    finally:
        await engine.dispose()


async def test_ensure_category_skips_repair_inside_hour(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "repair_cap")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels.clear()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    seed = _FakeSeed(guild, sessions, bind=False)
    bot.seed = seed
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        first = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        second = await bot.evidence.open_channel(guild, _timeout_case(2), actor, target, dm1_ok=True, now=stamp)
        assert first.denial is EvidenceDenial.NO_CATEGORY
        assert second.denial is EvidenceDenial.NO_CATEGORY
        assert seed.calls == ["cat_evidence"]
        later = await bot.evidence.open_channel(
            guild, _timeout_case(3), actor, target, dm1_ok=True, now=stamp + timedelta(hours=1)
        )
        assert later.denial is EvidenceDenial.NO_CATEGORY
        assert seed.calls == ["cat_evidence", "cat_evidence"]
    finally:
        await engine.dispose()


async def test_ensure_category_binds_layout_without_repair(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "layout")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    seed = _FakeSeed(guild, sessions, bind=False)
    bot.seed = seed
    async with session_scope(sessions) as session:
        session.add(GuildLayout(guild_id=1, key="cat_evidence", channel_id=50))
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        result = await bot.evidence.open_channel(guild, _timeout_case(1), actor, target, dm1_ok=True, now=stamp)
        assert result.ok is True
        assert seed.calls == []
        assert guild.created
        async with session_scope(sessions) as session:
            row = await session.get(ModSettings, 1)
            assert row is not None
            assert row.evidence_category_id == 50
    finally:
        await engine.dispose()


async def test_close_moves_channel_into_archive_category(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "archive_move")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    archive = _Category(90)
    guild.channels[70] = channel
    guild.channels[90] = archive
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_archive_channel_id=90))
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        result = await _closed(await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp))
        assert result.ok is True
        assert channel.deleted is False
        assert channel.category is archive
    finally:
        await engine.dispose()


async def test_close_with_text_archive_posts_then_deletes(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "archive_text")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    archive = _TextChannel(91)
    guild.channels[70] = channel
    guild.channels[91] = archive
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_archive_channel_id=91))
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        result = await _closed(await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp))
        assert result.ok is True
        assert channel.deleted is True
        assert archive.posts
    finally:
        await engine.dispose()


async def test_close_unbound_archive_does_not_crash(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "archive_off")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    guild.channels[70] = channel
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        result = await _closed(await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp))
        assert result.ok is True
        assert channel.deleted is True
        denial, opened, overdue = await bot.evidence.health(1, guild=guild)
        assert denial is EvidenceDenial.NO_CATEGORY
        assert await bot.evidence.archive_status(1, guild=guild) == "off"
    finally:
        await engine.dispose()


async def test_repair_category_calls_reapply_rights(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "repair_click")
    bot = _bot(sessions)
    guild = _Guild()
    guild.channels.clear()
    seed = _FakeSeed(guild, sessions, bind=True)
    bot.seed = seed
    try:
        denial = await bot.evidence.repair_category(guild, 2)
        assert denial is None
        assert seed.calls == ["cat_evidence", "rights"]
    finally:
        await engine.dispose()


async def test_held_open_keeps_null_channel_and_no_second_case(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "held_null")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        case = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add(case)
        await session.flush()
        case_id = int(case.id)
    stored = _timeout_case(case_id)

    async def held(*_args: object, **_kwargs: object):
        return hold_from_wait(bucket="channel", wait=60, now=0.0)

    try:
        with patch("conductor.services.evidence.hold_for", held):
            first = await bot.evidence.open_channel(guild, stored, actor, target, dm1_ok=True, now=stamp)
            second = await bot.evidence.open_channel(guild, stored, actor, target, dm1_ok=True, now=stamp)
        assert first.denial is EvidenceDenial.HELD
        assert second.denial is EvidenceDenial.HELD
        assert "Открывать дело заново не нужно" in (first.error or "")
        async with session_scope(sessions) as session:
            cases = (await session.scalars(select(ModCase))).all()
            rows = (await session.scalars(select(ModEvidence))).all()
        assert len(cases) == 1
        assert len(rows) == 1
        assert rows[0].channel_id is None
        assert not guild.created
    finally:
        await engine.dispose()


async def test_sweep_creates_pending_channel_after_hold(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_ok")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        case = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add(case)
        await session.flush()
        session.add(ModEvidence(guild_id=1, case_id=int(case.id), channel_id=None, created_at=stamp, dm1_ok=True))
    try:
        await bot.evidence.sweep(now=stamp)
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(ModEvidence))).first()
        assert row is not None
        assert row.channel_id is not None
        assert guild.created
        channel = guild.channels[row.channel_id]
        assert channel.posts
        assert actor.dms
    finally:
        await engine.dispose()


async def test_sweep_skips_pending_while_held(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_held")
    bot = _bot(sessions)
    guild = _Guild()
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        case = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add(case)
        await session.flush()
        session.add(ModEvidence(guild_id=1, case_id=int(case.id), channel_id=None, created_at=stamp, dm1_ok=True))

    async def held(*_args: object, **_kwargs: object):
        return hold_from_wait(bucket="channel", wait=60, now=0.0)

    try:
        with patch("conductor.services.evidence.hold_for", held):
            await bot.evidence.sweep(now=stamp)
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(ModEvidence))).first()
        assert row is not None
        assert row.channel_id is None
        assert not guild.created
    finally:
        await engine.dispose()


async def test_sweep_pending_caps_three_per_guild(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_cap")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        for number in range(1, 5):
            case = ModCase(
                guild_id=1,
                number=number,
                owner_id=3,
                created_by=2,
                actor_rank=1,
                kind=CaseKind.TIMEOUT,
                reason=REASON,
                duration_seconds=600,
                created_at=stamp,
            )
            session.add(case)
            await session.flush()
            session.add(ModEvidence(guild_id=1, case_id=int(case.id), channel_id=None, created_at=stamp, dm1_ok=True))
    try:
        await bot.evidence.sweep(now=stamp)
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(ModEvidence))).all()
        opened = [row for row in rows if row.channel_id is not None]
        pending = [row for row in rows if row.channel_id is None]
        assert len(opened) == 3
        assert len(pending) == 1
    finally:
        await engine.dispose()


async def test_sweep_pending_skips_revoked_and_without_evidence(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_skip")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        revoked = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
            revoked_at=stamp,
        )
        note = ModCase(
            guild_id=1,
            number=2,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.NOTE,
            reason=REASON,
            created_at=stamp,
        )
        live = ModCase(
            guild_id=1,
            number=3,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add_all((revoked, note, live))
        await session.flush()
        session.add_all(
            (
                ModEvidence(guild_id=1, case_id=int(revoked.id), channel_id=None, created_at=stamp, dm1_ok=True),
                ModEvidence(guild_id=1, case_id=int(note.id), channel_id=None, created_at=stamp, dm1_ok=True),
                ModEvidence(guild_id=1, case_id=int(live.id), channel_id=None, created_at=stamp, dm1_ok=True),
            )
        )
    try:
        await bot.evidence.sweep(now=stamp)
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(ModEvidence))).all()
        opened = [row for row in rows if row.channel_id is not None]
        assert len(opened) == 1
        assert len(guild.created) == 1
    finally:
        await engine.dispose()


async def test_sweep_pending_failed_counts_toward_cap(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_fail_cap")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        for number in range(1, 5):
            case = ModCase(
                guild_id=1,
                number=number,
                owner_id=3,
                created_by=2,
                actor_rank=1,
                kind=CaseKind.TIMEOUT,
                reason=REASON,
                duration_seconds=600,
                created_at=stamp,
            )
            session.add(case)
            await session.flush()
            session.add(ModEvidence(guild_id=1, case_id=int(case.id), channel_id=None, created_at=stamp, dm1_ok=True))
    calls: list[int] = []

    async def boom(*_args: object, **_kwargs: object):
        calls.append(1)
        return None, EvidenceDenial.FAILED, None

    try:
        with patch.object(bot.evidence, "_create_channel", boom):
            await bot.evidence.sweep(now=stamp)
        assert len(calls) == 3
    finally:
        await engine.dispose()


async def test_sweep_pending_non_hold_error_backs_off(tmp_path: Path) -> None:
    engine, sessions = await _engine(tmp_path, "pending_backoff")
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda gid: guild if gid == 1 else None
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    async with session_scope(sessions) as session:
        session.add(ModSettings(guild_id=1, evidence_category_id=50))
        case = ModCase(
            guild_id=1,
            number=1,
            owner_id=3,
            created_by=2,
            actor_rank=1,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_seconds=600,
            created_at=stamp,
        )
        session.add(case)
        await session.flush()
        session.add(ModEvidence(guild_id=1, case_id=int(case.id), channel_id=None, created_at=stamp, dm1_ok=True))
    calls: list[int] = []

    async def boom(*_args: object, **_kwargs: object):
        calls.append(1)
        return None, EvidenceDenial.FAILED, None

    try:
        with patch.object(bot.evidence, "_create_channel", boom):
            await bot.evidence.sweep(now=stamp)
            assert len(calls) == 1
            await bot.evidence.sweep(now=stamp)
            assert len(calls) == 1
            await bot.evidence.sweep(now=stamp + timedelta(minutes=11))
            assert len(calls) == 2
            await bot.evidence.sweep(now=stamp + timedelta(minutes=12))
            assert len(calls) == 2
            await bot.evidence.sweep(now=stamp + timedelta(hours=2))
            assert len(calls) == 3
    finally:
        await engine.dispose()
