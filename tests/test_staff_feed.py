from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.cogs.admin_logs import AdminLogs
from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildConfig, StaffAction, Ticket, TicketCategory, TicketPanel
from conductor.db.session import session_scope
from conductor.domain.access import (
    MOD_BAN,
    AccessDecision,
    AccessSnapshot,
    PermissionRule,
    Rank,
    empty_rank_roles,
)
from conductor.domain.admin import AdminCtx
from conductor.domain.giveaway import GiveawaySpec
from conductor.domain.moderation import CaseKind, ModVerdict
from conductor.domain.poll import PollCard, PollKind
from conductor.domain.staff_feed import (
    ACTION_BYPASS,
    ACTION_EXTERNAL,
    ACTION_GIVEAWAY_START,
    ACTION_GUARD_FORGED,
    ACTION_PARDON,
    ACTION_POLL_REVEAL,
    ACTION_QUOTA_DENIED,
    ACTION_TICKET_CLAIM,
    ACTION_TICKET_CLOSE,
    ACTION_WATCH_NOTE,
    case_action,
    should_ping_chief,
)
from conductor.domain.tickets import (
    ClaimVerdict,
    CloseVerdict,
    Outcome,
    TicketState,
    dump_ticket_settings,
    parse_ticket_settings,
)
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, run_guard
from conductor.presentation.layout import collected_text
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.admin_pages import page_by_id
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.giveaway import GiveawayService
from conductor.services.moderation import ModerationService
from conductor.services.poll import PollService
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult
from conductor.services.staff_feed import StaffFeed
from conductor.services.tickets import TicketService
from conductor.services.watchers import WatchersService

WATCHER_ROLE = 11
CHIEF_ROLE = 33
REASON = "нарушение правил чата"
STAMP = datetime(2026, 9, 7, 8, tzinfo=UTC)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _member(user_id: int, role_ids: tuple[int, ...] = (), *, admin: bool = False) -> SimpleNamespace:
    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    return SimpleNamespace(
        id=user_id,
        bot=False,
        mention=f"<@{user_id}>",
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=admin),
        timeout=timeout,
    )


def _ranked_snapshot() -> AccessSnapshot:
    return AccessSnapshot(
        rank_roles=empty_rank_roles() | {Rank.WATCHER: (WATCHER_ROLE,), Rank.CHIEF: (CHIEF_ROLE,), Rank.SPECIAL: (44,)},
        rules={MOD_BAN: PermissionRule(MOD_BAN, frozenset({Rank.CHIEF, Rank.SPECIAL}))},
        special_locked=True,
    )


class _FakeRoles:
    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def apply(self, _claim: object) -> RoleApplyResult:
        return RoleApplyResult(ok=True)


def _bot(sessions, *, feed_channel: int | None = 12) -> SimpleNamespace:
    submitted: list[tuple[object, dict[str, object]]] = []
    posts: list[dict[str, object]] = []
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        user=SimpleNamespace(id=99),
        outbound=None,
        roles=_FakeRoles(),
        log_pump=SimpleNamespace(submit=lambda *args, **kwargs: submitted.append((args, kwargs))),
        config_by_guild={
            1: SimpleNamespace(
                user_log_channel_id=10,
                admin_log_channel_id=11,
                staff_feed_channel_id=feed_channel,
                timezone="UTC",
            )
        },
        queue=SimpleNamespace(schedule_later=lambda *_a, **_k: None, cancel_later=lambda *_a, **_k: None),
        scheduler=None,
        settings=SimpleNamespace(tickets_per_category=1),
        get_guild=lambda _gid: None,
        submitted=submitted,
        posts=posts,
        spawn=None,
        guilds=(),
    )
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), _ranked_snapshot())
    feed = StaffFeed(bot)

    async def capture(guild_id: int, **kwargs: object) -> int | None:
        result = await StaffFeed.post(feed, guild_id, **kwargs)
        if result is not None:
            posts.append({"guild_id": guild_id, **kwargs})
        return result

    feed.post = capture  # type: ignore[method-assign]
    bot.staff_feed = feed
    return bot


def _feed_cards(bot: SimpleNamespace) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for args, kwargs in bot.submitted:
        view = kwargs.get("view")
        if view is None:
            continue
        if copy.STAFF_FEED_TITLE in collected_text(view):
            found.append({"args": args, **kwargs})
    return found


def _roles(mentions: object) -> list[int]:
    users = getattr(mentions, "roles", None) or ()
    return [int(getattr(item, "id", item)) for item in users]


async def _actions(sessions) -> list[StaffAction]:
    async with session_scope(sessions) as session:
        return list((await session.scalars(select(StaffAction).order_by(StaffAction.id))).all())


def test_ping_only_for_ban_kick_and_external() -> None:
    assert should_ping_chief(case_action(CaseKind.TIMEOUT)) is False
    assert should_ping_chief(case_action(CaseKind.BAN)) is True
    assert should_ping_chief(case_action(CaseKind.KICK)) is True
    assert should_ping_chief(ACTION_EXTERNAL) is True
    assert should_ping_chief(ACTION_QUOTA_DENIED, ping_chief=True) is True


async def test_timeout_posts_once_without_chief_mention(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    try:
        outcome = await service.apply(
            SimpleNamespace(id=1, owner_id=1),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="2h",
            now=STAMP,
        )
        assert outcome.verdict is ModVerdict.OK
        assert len(bot.posts) == 1
        assert bot.posts[0]["action"] == "case.timeout"
        cards = _feed_cards(bot)
        assert len(cards) == 1
        assert _roles(cards[0]["allowed_mentions"]) == []
        assert cards[0]["silent"] is True
        rows = await _actions(sessions)
        assert len(rows) == 1
        assert rows[0].action == "case.timeout"
        assert rows[0].target_id == 3
    finally:
        await engine.dispose()


async def test_ban_pings_chief_role_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    guild = SimpleNamespace(id=1, owner_id=1, members={}, bans=[], get_member=lambda uid: guild.members.get(uid))

    async def ban(target: object, delete_message_seconds: int = 0, reason: str | None = None) -> None:
        guild.bans.append(target)

    guild.ban = ban
    actor = _member(2, (CHIEF_ROLE,))
    target = _member(3)
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    try:
        started = await service.begin_heavy(guild, actor, target, kind=CaseKind.BAN, reason=REASON, now=STAMP)
        assert started.pending_id is not None
        outcome = await service.confirm_heavy(guild, started.pending_id, now=STAMP)
        assert outcome.verdict is ModVerdict.OK
        assert len(bot.posts) == 1
        assert bot.posts[0]["action"] == "case.ban"
        cards = _feed_cards(bot)
        assert len(cards) == 1
        assert _roles(cards[0]["allowed_mentions"]) == [CHIEF_ROLE]
        assert cards[0]["silent"] is False
    finally:
        await engine.dispose()


async def test_missing_channel_still_writes_row(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions, feed_channel=None)
    try:
        outcome = await ModerationService(bot).apply(
            SimpleNamespace(id=1, owner_id=1),
            _member(2, (WATCHER_ROLE,)),
            _member(3),
            kind=CaseKind.WARN,
            reason=REASON,
            now=STAMP,
        )
        assert outcome.verdict is ModVerdict.OK
        assert len(bot.posts) == 1
        assert _feed_cards(bot) == []
        rows = await _actions(sessions)
        assert len(rows) == 1
        assert rows[0].action == "case.warn"
    finally:
        await engine.dispose()


async def test_quota_denied_and_bypass_each_post_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    guild = SimpleNamespace(id=1, owner_id=1)
    try:
        for _ in range(10):
            ok = await service.apply(
                guild, actor, target, kind=CaseKind.TIMEOUT, reason=REASON, duration_raw="10m", now=STAMP
            )
            assert ok.verdict is ModVerdict.OK
        bot.posts.clear()
        denied = await service.apply(guild, actor, target, kind=CaseKind.MUTE, reason=REASON, now=STAMP)
        assert denied.verdict is ModVerdict.QUOTA
        again = await service.apply(guild, actor, target, kind=CaseKind.MUTE, reason=REASON, now=STAMP)
        assert again.verdict is ModVerdict.QUOTA
        denied_posts = [item for item in bot.posts if item["action"] == ACTION_QUOTA_DENIED]
        assert len(denied_posts) == 1
        actor.guild_permissions = SimpleNamespace(administrator=True)
        bypassed = await service.apply(
            guild, actor, target, kind=CaseKind.TIMEOUT, reason=REASON, duration_raw="10m", now=STAMP
        )
        assert bypassed.verdict is ModVerdict.OK
        bypass_posts = [item for item in bot.posts if item["action"] == ACTION_BYPASS]
        assert len(bypass_posts) == 1
    finally:
        await engine.dispose()


async def test_purge_and_lock_each_call_helper_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    actor = _member(2, (WATCHER_ROLE,))

    class _Hist:
        def __init__(self) -> None:
            self.id = 70
            self.mention = "#chat"

        def history(self, **_kwargs: object):
            async def _gen():
                if False:
                    yield None

            return _gen()

    overwrite = SimpleNamespace(send_messages=True)

    class _Lock:
        id = 80
        mention = "#general"
        slowmode_delay = 0

        def overwrites_for(self, _role: object) -> SimpleNamespace:
            return overwrite

        async def set_permissions(self, _role: object, **_kwargs: object) -> None:
            return None

    guild = SimpleNamespace(id=1, owner_id=1, default_role=SimpleNamespace(id=1))
    try:
        purged = await service.purge(guild, actor, _Hist(), reason=REASON, now=STAMP)
        assert purged.verdict is ModVerdict.OK
        locked = await service.lock_channel(guild, actor, _Lock(), reason=REASON, duration_raw="1h", now=STAMP)
        assert locked.verdict is ModVerdict.OK
        kinds = [item["action"] for item in bot.posts]
        assert kinds.count("case.purge") == 1
        assert kinds.count("case.lock") == 1
    finally:
        await engine.dispose()


async def test_ticket_claim_and_close_each_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = TicketService(bot)
    channel = SimpleNamespace(
        id=10,
        history=lambda **_k: _empty_history(),
        send=lambda **_k: _async_ns(99),
        mention="#t",
    )
    archive = SimpleNamespace(id=20, send=lambda **_k: _async_ns(100))
    guild = SimpleNamespace(
        id=1,
        get_channel=lambda cid: {10: channel, 20: archive}.get(cid),
        get_thread=lambda _cid: None,
        get_member=lambda _uid: None,
    )
    actor = SimpleNamespace(
        id=7,
        roles=[SimpleNamespace(id=CHIEF_ROLE)],
        guild_permissions=SimpleNamespace(administrator=False),
    )
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(
                guild_id=1,
                channel_id=3,
                settings=dump_ticket_settings(parse_ticket_settings('{"archive_channel_id": 20}')),
                created_at=STAMP,
                created_by=7,
            )
            session.add(panel)
            await session.flush()
            category = TicketCategory(panel_id=panel.id, guild_id=1, name="Жалоба", position=0)
            session.add(category)
            await session.flush()
            session.add(
                Ticket(
                    guild_id=1,
                    number=1,
                    owner_id=1,
                    category_id=category.id,
                    panel_id=panel.id,
                    state=TicketState.OPEN,
                    claimed_rank=0,
                    channel_id=10,
                    created_at=STAMP,
                )
            )
        claimed = await service.claim_ticket(guild, 1, actor)
        closed = await service.close_ticket(guild, 1, actor, reason="готово", outcome=Outcome.RESOLVED)
        assert claimed is ClaimVerdict.OK
        assert closed is CloseVerdict.OK
        actions = [item["action"] for item in bot.posts]
        assert actions.count(ACTION_TICKET_CLAIM) == 1
        assert actions.count(ACTION_TICKET_CLOSE) == 1
    finally:
        await engine.dispose()


def _empty_history():
    async def _gen():
        if False:
            yield None

    return _gen()


async def _async_ns(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=message_id)


async def test_watchers_note_posts_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    try:
        await WatchersService(bot).record_staff(
            1,
            "заметка",
            actor_id=2,
            actor_rank=Rank.SENIOR,
            action=ACTION_WATCH_NOTE,
            target=3,
            details="текст",
        )
        assert [item["action"] for item in bot.posts] == [ACTION_WATCH_NOTE]
        rows = await _actions(sessions)
        assert len(rows) == 1
        assert rows[0].target_id == 3
    finally:
        await engine.dispose()


async def test_poll_giveaway_pardon_each_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    channel = SimpleNamespace(id=5, send=lambda **_k: _async_ns(100))
    guild = SimpleNamespace(id=1, get_channel=lambda _cid: channel, get_member=lambda _uid: None, members={})
    try:
        polls = PollService(bot)
        await polls._announce_reveal(
            guild,
            PollCard(
                poll_id=4,
                question="кто?",
                host_id=2,
                kind=PollKind.ANONYMOUS,
                allow_change=False,
                options=("да", "нет"),
                ends_at=STAMP,
                channel_id=5,
            ),
            actor_id=2,
        )
        started = await GiveawayService(bot).start(
            guild,
            channel,
            actor_id=2,
            spec=GiveawaySpec(prize="ключ", winners=1, duration=timedelta(minutes=5)),
            now=STAMP,
        )
        assert started is not None
        await ReputationService(bot).pardon(guild, 3, actor_id=2, now=STAMP)
        actions = [item["action"] for item in bot.posts]
        assert actions.count(ACTION_POLL_REVEAL) == 1
        assert actions.count(ACTION_GIVEAWAY_START) == 1
        assert actions.count(ACTION_PARDON) == 1
    finally:
        await engine.dispose()


async def test_forged_ref_posts_once(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)

    async def decide_access(**_kwargs: object) -> AccessDecision:
        return AccessDecision(True, Rank.CHIEF)

    bot.decide_access = decide_access

    async def send_message(**_kwargs: object) -> None:
        return None

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(id=2, roles=(), guild_permissions=SimpleNamespace(administrator=False)),
        client=bot,
        response=SimpleNamespace(is_done=lambda: False, send_message=send_message),
        data={"custom_id": "c:tk:close:8"},
    )

    async def load(*, guild_id: int, ref: str) -> GuardResource:
        return GuardResource(ref=ref, guild_id=2, owner_id=9)

    try:
        verdict = await run_guard(interaction, ref="8", load=load, key="tickets.close_own")
        assert verdict.forged is True
        assert [item["action"] for item in bot.posts] == [ACTION_GUARD_FORGED]
    finally:
        await engine.dispose()


async def test_external_sanction_dedups_audit_entry(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    cog = AdminLogs(bot)
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99), get_member=lambda _uid: None)
    entry = SimpleNamespace(
        id=777,
        action=SimpleNamespace(name="ban"),
        user=SimpleNamespace(id=4),
        target=SimpleNamespace(id=5),
        guild=guild,
    )
    try:
        await cog.record_external(entry)
        await cog.record_external(entry)
        assert [item["action"] for item in bot.posts] == [ACTION_EXTERNAL]
        rows = await _actions(sessions)
        assert len(rows) == 1
        assert rows[0].ref == "777"
        cards = _feed_cards(bot)
        assert _roles(cards[0]["allowed_mentions"]) == [CHIEF_ROLE]
    finally:
        await engine.dispose()


async def test_admin_logs_page_writes_staff_channel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)

    async def refresh_config(guild_id: int) -> None:
        async with session_scope(sessions) as session:
            row = await session.get(GuildConfig, guild_id)
            if row is not None:
                bot.config_by_guild[guild_id] = row

    bot.refresh_config = refresh_config
    bot.sessions = sessions
    page = page_by_id("logs")
    assert page is not None
    ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
    try:
        result = await page.handle(ctx, "logs.staff", "77")
        assert result.ok is True
        snap = await page.snapshot(ctx, 1)
        assert copy.ADMIN_FIELD_STAFF_FEED in snap.fields
        async with session_scope(sessions) as session:
            row = await session.get(GuildConfig, 1)
            assert row is not None
            assert row.staff_feed_channel_id == 77
    finally:
        await engine.dispose()
