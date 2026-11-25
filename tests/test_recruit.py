from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildConfig, MemberRecord, RecruitLink, VoiceHub
from conductor.db.session import session_scope
from conductor.domain.access import RECRUIT_MANAGE, RECRUIT_READ, Rank, can
from conductor.domain.privacy import FORGOTTEN_REASON, FORGOTTEN_USER_ID
from conductor.domain.recruit import (
    RecruitLink as LinkView,
)
from conductor.domain.recruit import (
    RecruitListing,
    active_link,
    is_active,
    parse_code,
    summarize,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.recruit import _flip, listing_line, report_card
from conductor.services.admin_pages import page_by_id
from conductor.services.privacy import PrivacyService
from conductor.services.recruit import RecruitService

STAMP = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _sessions(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    return create_sessionmaker(engine), engine


def test_is_active_boundary_and_leave() -> None:
    assert is_active(True, 5) is False
    assert is_active(True, 6) is True
    assert is_active(False, 6) is False
    assert can(Rank.CHIEF, key=RECRUIT_MANAGE) is True
    assert can(Rank.SENIOR, key=RECRUIT_MANAGE) is False
    assert can(Rank.SENIOR, key=RECRUIT_READ) is True
    assert can(Rank.WATCHER, key=RECRUIT_READ) is False
    assert parse_code("https://discord.gg/abc123") == "abc123"


def test_one_active_per_person_and_summary() -> None:
    first = LinkView(
        id=1,
        guild_id=1,
        code="aaa",
        owner_user_id=7,
        channel_id=10,
        created_by=2,
        created_at=STAMP,
    )
    second = LinkView(
        id=2,
        guild_id=1,
        code="bbb",
        owner_user_id=7,
        channel_id=10,
        created_by=2,
        created_at=STAMP,
        revoked_at=STAMP,
        revoked_by=2,
    )
    assert active_link((second, first), 7) is first
    people = [
        SimpleNamespace(invite_code="aaa", currently_member=True, message_count=5),
        SimpleNamespace(invite_code="aaa", currently_member=True, message_count=6),
        SimpleNamespace(invite_code="zzz", currently_member=True, message_count=9),
    ]
    quiet = summarize(people[:1], "aaa")
    assert (quiet.joined_total, quiet.active, quiet.present) == (1, 0, 1)
    busy = summarize(people, "aaa")
    assert (busy.joined_total, busy.active, busy.present) == (2, 1, 2)


class _Invite:
    def __init__(self, code: str) -> None:
        self.code = code
        self.url = f"https://discord.gg/{code}"
        self.deleted = False

    async def delete(self, reason: str | None = None) -> None:
        _ = reason
        self.deleted = True


class _Channel:
    def __init__(self, channel_id: int, guild: _Guild) -> None:
        self.id = channel_id
        self._guild = guild

    async def create_invite(self, **_kwargs: object) -> _Invite:
        invite = _Invite(f"r{len(self._guild.live) + 1}")
        self._guild.live.append(invite)
        return invite


class _Guild:
    def __init__(self) -> None:
        self.id = 1
        self.live: list[_Invite] = []
        self.channels = [_Channel(10, self)]

    def get_channel(self, channel_id: int) -> _Channel | None:
        return next((item for item in self.channels if item.id == int(channel_id)), None)

    async def invites(self) -> list[_Invite]:
        return list(self.live)


def _bot(sessions: object) -> SimpleNamespace:
    async def refresh(_guild_id: int) -> None:
        return None

    return SimpleNamespace(
        sessions=sessions,
        outbound=None,
        refresh_config=refresh,
        config_by_guild={},
    )


async def _seed_channel(sessions: object, *, channel_id: int = 10) -> None:
    async with session_scope(sessions) as session:
        session.add(GuildConfig(guild_id=1, recruit_channel_id=channel_id))


async def test_issue_is_idempotent_and_revoke_keeps_history(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        await _seed_channel(sessions)
        first, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and first is not None
        assert first.reused is False
        again, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and again is not None
        assert again.reused is True
        assert again.link.code == first.link.code
        revoked, error = await service.revoke(guild, 7, actor_id=2, now=STAMP)
        assert error is None and revoked is not None
        assert revoked.revoked_at is not None
        assert guild.live[0].deleted is True
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(RecruitLink).where(RecruitLink.guild_id == 1))).all()
        assert len(rows) == 1
        assert rows[0].revoked_at is not None
    finally:
        await engine.dispose()


async def test_summary_on_members_invite_code(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        await _seed_channel(sessions)
        issued, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and issued is not None
        async with session_scope(sessions) as session:
            session.add(
                MemberRecord(
                    guild_id=1,
                    user_id=20,
                    username="joiner",
                    display_name="Joiner",
                    currently_member=True,
                    first_joined_at=STAMP,
                    last_joined_at=STAMP,
                    invite_code=issued.link.code,
                    message_count=5,
                )
            )
        card = await service.report_one(guild, code=issued.link.code)
        assert card is not None
        assert (card.summary.joined_total, card.summary.active, card.summary.present) == (1, 0, 1)
        async with session_scope(sessions) as session:
            row = await session.scalar(select(MemberRecord).where(MemberRecord.user_id == 20))
            assert row is not None
            row.message_count = 6
        card = await service.report_one(guild, code=issued.link.code)
        assert card is not None
        assert card.summary.active == 1
        text = collected_text(report_card(card))
        assert issued.link.code in text
        assert "<@7>" in text
    finally:
        await engine.dispose()


async def test_forget_keeps_row_and_clears_owner(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        await _seed_channel(sessions)
        issued, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and issued is not None
        privacy = PrivacyService(bot)
        report = await privacy.forget(1, 7)
        assert report.tables["recruit_links"] == 1
        async with session_scope(sessions) as session:
            row = await session.scalar(select(RecruitLink).where(RecruitLink.code == issued.link.code))
        assert row is not None
        assert row.owner_user_id == FORGOTTEN_USER_ID
        listing = await service.report_listing(guild)
        assert listing.rows[0].owner_user_id == FORGOTTEN_USER_ID
        assert FORGOTTEN_REASON in listing_line(listing.rows[0])
    finally:
        await engine.dispose()


async def test_blocked_hub_and_log_denied(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    log_channel = _Channel(40, guild)
    hub_channel = _Channel(50, guild)
    guild.channels.append(log_channel)
    guild.channels.append(hub_channel)
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        async with session_scope(sessions) as session:
            session.add(GuildConfig(guild_id=1, recruit_channel_id=10, user_log_channel_id=40))
            session.add(VoiceHub(guild_id=1, channel_id=50, settings="{}"))
        issued, error = await service.issue(guild, 7, actor_id=2, channel=log_channel, now=STAMP)
        assert issued is None
        assert error == copy.RECRUIT_CHANNEL_BLOCKED
        issued, error = await service.issue(guild, 7, actor_id=2, channel=hub_channel, now=STAMP)
        assert issued is None
        assert error == copy.RECRUIT_CHANNEL_BLOCKED
        page = page_by_id("recruit")
        assert page is not None
        bot.recruit = service
        from conductor.domain.admin import AdminCtx

        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
        denied = await page.handle(ctx, "recruit.channel", "40")
        assert denied.ok is False
        assert denied.error == copy.RECRUIT_CHANNEL_BLOCKED
    finally:
        await engine.dispose()


async def test_missing_discord_invite_is_marked_deleted(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        await _seed_channel(sessions)
        issued, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and issued is not None
        guild.live.clear()
        card = await service.report_one(guild, code=issued.link.code)
        assert card is not None
        assert card.link.invite_deleted is True
        assert card.link.revoked_at is not None
        text = collected_text(report_card(card))
        assert copy.RECRUIT_STATUS_DELETED in text
    finally:
        await engine.dispose()


async def test_report_listing_does_not_fetch_invites(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    guild = _Guild()
    bot = _bot(sessions)
    service = RecruitService(bot)
    try:
        await _seed_channel(sessions)
        issued, error = await service.issue(guild, 7, actor_id=2, now=STAMP)
        assert error is None and issued is not None
        guild.live.clear()
        calls = 0
        original = guild.invites

        async def counting() -> list[_Invite]:
            nonlocal calls
            calls += 1
            return await original()

        guild.invites = counting  # type: ignore[method-assign]
        listing = await service.report_listing(guild)
        assert listing.rows
        assert listing.rows[0].invite_deleted is False
        assert calls == 0
        card = await service.report_one(guild, code=issued.link.code)
        assert calls == 1
        assert card is not None
        assert card.link.invite_deleted is True
    finally:
        await engine.dispose()


class _AckTrace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.response = _AckResponse(self.order)
        self.guild = SimpleNamespace(id=1)
        self.client = SimpleNamespace()

    async def edit_original_response(self, **_kwargs: object) -> None:
        self.order.append("edit_original_response")


class _AckResponse:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def is_done(self) -> bool:
        return "defer" in self._order

    async def defer(self, **_kwargs: object) -> None:
        self._order.append("defer")


async def test_recruit_flip_defers_before_listing() -> None:
    interaction = _AckTrace()

    async def report_listing(_guild: object, *, page: int = 1) -> RecruitListing:
        interaction.order.append("work")
        return RecruitListing(rows=(), page=page, pages=1)

    interaction.client.recruit = SimpleNamespace(report_listing=report_listing)
    await _flip(interaction, 2)  # type: ignore[arg-type]
    assert interaction.order == ["defer", "work", "edit_original_response"]


async def test_admin_sets_default_channel(tmp_path: Path) -> None:
    sessions, engine = await _sessions(tmp_path)
    bot = _bot(sessions)
    service = RecruitService(bot)
    bot.recruit = service
    try:
        from conductor.domain.admin import AdminCtx

        page = page_by_id("recruit")
        assert page is not None
        ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
        ready = await page.ready(ctx)
        assert ready.ok is False
        result = await page.handle(ctx, "recruit.channel", "10")
        assert result.ok is True
        assert await service.channel_id(1) == 10
        assert (await page.ready(ctx)).ok is True
    finally:
        await engine.dispose()
