from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor import __version__
from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import BugReport, GuildLayout
from conductor.db.session import session_scope
from conductor.domain.access import ACTION_DEFAULTS, BUG_TRIAGE, Rank
from conductor.domain.bugs import BugMatch, BugStatus, DecideVerdict, SubmitVerdict, parse_code, rate_ok
from conductor.domain.errors import CriticalError, ErrorKind
from conductor.domain.seed import FEATURES, SKELETON, creatable_keys, feature_of
from conductor.presentation import copy
from conductor.presentation.bugs import BugFixButton, BugReportModal, report_view
from conductor.presentation.layout import collected_text, failure
from conductor.presentation.modal import SafeModal
from conductor.services.bugs import BugService
from conductor.services.errors import CriticalLog
from conductor.services.queue import ActionQueue

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 9, 18, 18, 0, tzinfo=UTC)


class FakeMessage:
    def __init__(self) -> None:
        self.id = 1


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: list[object] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        self.sent.append(kwargs.get("view"))
        return FakeMessage()


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.sent: list[object] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        self.sent.append(kwargs.get("view"))
        return FakeMessage()


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels: dict[int, FakeChannel] = {}
        self.members: dict[int, FakeUser] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeUser | None:
        return self.members.get(user_id)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _bot(sessions, guild: FakeGuild, *, user: FakeUser | None = None, errors: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=ActionQueue(lambda coro, *, name: coro),
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        get_user=lambda uid: user if user is not None and uid == user.id else None,
        errors=errors,
        serves=lambda _gid: True,
    )


async def _bind_channel(sessions, guild_id: int, channel_id: int) -> None:
    async with session_scope(sessions) as session:
        session.add(GuildLayout(guild_id=guild_id, key="bug_reports", channel_id=channel_id))


def test_version_matches_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == "0.9.0"
    assert data["project"]["version"] == __version__


def test_diagnose_heading_includes_version() -> None:
    heading = copy.diagnose_heading("Диагностика", __version__)
    assert heading.endswith(f"· {__version__}")
    assert "Диагностика" in heading


def test_failure_button_carries_code() -> None:
    view = failure("E-1A2B")
    text = collected_text(view)
    assert "E-1A2B" in text
    ids = [getattr(item, "custom_id", "") for item in view.walk_children()]
    assert any("E-1A2B" in str(item) for item in ids)


def test_bug_modal_is_safe_modal() -> None:
    assert issubclass(BugReportModal, SafeModal)


def test_skeleton_covers_bug_reports_and_group_archive() -> None:
    by_key = {entry.key: entry for entry in SKELETON}
    assert "bug_reports" in by_key
    assert "cat_group_archive" in by_key
    assert by_key["bug_reports"].creatable is True
    assert "bug_reports" in creatable_keys()
    assert feature_of("bug_reports") == "logs"
    assert feature_of("cat_group_archive") == "group_archive"
    owned = {key for _feature, _copy, keys in FEATURES for key in keys}
    assert "bug_reports" in owned
    assert "cat_group_archive" in owned
    assert "баг-репорты" in by_key["bug_reports"].diagnose_label
    assert by_key["cat_group_archive"].diagnose_label


def test_parse_code_and_rate() -> None:
    assert parse_code("e-1a2b") == "E-1A2B"
    assert parse_code("") is None
    assert parse_code("not-a-id") is None
    assert rate_ok(2) is True
    assert rate_ok(3) is False


def test_triage_key_defaults() -> None:
    assert BugFixButton.access_key == BUG_TRIAGE
    allowed = ACTION_DEFAULTS[BUG_TRIAGE]
    assert Rank.WATCHER not in allowed
    assert Rank.SENIOR in allowed
    assert Rank.CHIEF in allowed
    assert Rank.SPECIAL in allowed


async def test_submit_writes_row_and_card(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(40))
    user = FakeUser(7)
    guild.members[7] = user
    bot = _bot(sessions, guild, user=user)
    service = BugService(bot)
    await _bind_channel(sessions, guild.id, channel.id)
    try:
        result = await service.submit(
            guild,  # type: ignore[arg-type]
            user,  # type: ignore[arg-type]
            did="нажал /profile",
            happened="карточка не открылась",
            code="E-1A2B",
            now=STAMP,
        )
        assert result.verdict is SubmitVerdict.OK
        assert result.card is not None
        assert result.card.version == "0.9.0"
        async with session_scope(sessions) as session:
            row = await session.scalar(select(BugReport).where(BugReport.guild_id == 1))
        assert row is not None
        assert row.user_id == 7
        assert row.code == "E-1A2B"
        assert row.version == "0.9.0"
        assert row.status == BugStatus.NEW.value
        assert channel.sent
        text = collected_text(channel.sent[0])  # type: ignore[arg-type]
        assert "0.9.0" in text
        assert "E-1A2B" in text
        assert "<@7>" in text
    finally:
        await engine.dispose()


async def test_submit_matches_critical_log(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(40))
    user = FakeUser(7)
    errors = CriticalLog(SimpleNamespace(config_by_guild={1: SimpleNamespace(admin_log_channel_id=55)}))
    errors._remember(
        CriticalError(
            code="E-9F00",
            kind=ErrorKind.COMMAND,
            action="/profile",
            error_type="TypeError",
            error_text="boom",
            origin="profile.py:12",
            guild_id=1,
        ),
        "https://discord.com/channels/1/55",
    )
    bot = _bot(sessions, guild, user=user, errors=errors)
    service = BugService(bot)
    await _bind_channel(sessions, guild.id, channel.id)
    try:
        result = await service.submit(
            guild,  # type: ignore[arg-type]
            user,  # type: ignore[arg-type]
            did="карточка ошибки",
            happened="нажал сообщить",
            code="E-9F00",
            now=STAMP,
        )
        assert result.verdict is SubmitVerdict.OK
        assert result.card is not None
        assert result.card.match == BugMatch(
            code="E-9F00",
            error_type="TypeError",
            origin="profile.py:12",
            jump_url="https://discord.com/channels/1/55",
        )
        text = collected_text(report_view(result.card))
        assert "https://discord.com/channels/1/55" in text
    finally:
        await engine.dispose()


async def test_fourth_report_in_hour_is_denied(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(40))
    user = FakeUser(7)
    service = BugService(_bot(sessions, guild, user=user))
    await _bind_channel(sessions, guild.id, channel.id)
    try:
        for index in range(3):
            result = await service.submit(
                guild,  # type: ignore[arg-type]
                user,  # type: ignore[arg-type]
                did=f"шаг {index}",
                happened="сбой",
                now=STAMP + timedelta(minutes=index),
            )
            assert result.verdict is SubmitVerdict.OK
        denied = await service.submit(
            guild,  # type: ignore[arg-type]
            user,  # type: ignore[arg-type]
            did="ещё раз",
            happened="сбой",
            now=STAMP + timedelta(minutes=10),
        )
        assert denied.verdict is SubmitVerdict.RATE
        later = await service.submit(
            guild,  # type: ignore[arg-type]
            user,  # type: ignore[arg-type]
            did="через час",
            happened="сбой",
            now=STAMP + timedelta(hours=1, minutes=1),
        )
        assert later.verdict is SubmitVerdict.OK
    finally:
        await engine.dispose()


async def test_fix_updates_status_and_dms_author(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel(40))
    author = FakeUser(7)
    staff = FakeUser(8)
    guild.members[7] = author
    service = BugService(_bot(sessions, guild, user=author))
    await _bind_channel(sessions, guild.id, channel.id)
    try:
        created = await service.submit(
            guild,  # type: ignore[arg-type]
            author,  # type: ignore[arg-type]
            did="кнопка",
            happened="не сработала",
            now=STAMP,
        )
        assert created.card is not None
        result = await service.decide(
            guild,  # type: ignore[arg-type]
            staff,  # type: ignore[arg-type]
            str(created.card.report_id),
            BugStatus.FIXED,
            now=STAMP + timedelta(minutes=5),
        )
        assert result.verdict is DecideVerdict.OK
        assert result.card is not None
        assert result.card.status == BugStatus.FIXED.value
        async with session_scope(sessions) as session:
            row = await session.scalar(select(BugReport).where(BugReport.id == created.card.report_id))
        assert row is not None
        assert row.status == "fixed"
        assert row.decided_by == 8
        assert author.sent
    finally:
        await engine.dispose()
