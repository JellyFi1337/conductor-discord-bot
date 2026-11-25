from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from conductor.bot import Conductor
from conductor.config import Settings
from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import VoiceSession
from conductor.db.session import session_scope
from conductor.domain.activity import as_utc
from conductor.domain.voice import LiveVoice, OpenVoiceSession, reconcile_sessions, session_seconds
from conductor.services.activity import ActivityCollector
from conductor.services.voice_sessions import VoiceSessionTracker

USER_LOGS_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "cogs" / "user_logs.py"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _tracker(tmp_path: Path) -> tuple[VoiceSessionTracker, object, object]:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    return VoiceSessionTracker(ActivityCollector()), sessions, engine


def test_session_seconds_never_goes_negative() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert session_seconds(start, start) == 0
    assert session_seconds(start, start - timedelta(seconds=30)) == 0
    assert session_seconds(start, start + timedelta(minutes=2, seconds=5)) == 125


def test_stale_open_session_closes_at_last_event_not_now() -> None:
    joined = datetime(2026, 1, 1, 11, 50, tzinfo=UTC)
    last_event = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    plan = reconcile_sessions(
        (
            OpenVoiceSession(
                id=1,
                guild_id=1,
                user_id=42,
                channel_id=9,
                joined_at=joined,
                last_event_at=last_event,
            ),
        ),
        (),
        now,
    )
    assert len(plan.close) == 1
    assert plan.close[0].left_at == last_event
    assert plan.close[0].seconds == 600
    assert plan.keep == ()
    assert plan.open == ()


def test_live_session_is_kept_not_reopened() -> None:
    joined = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    plan = reconcile_sessions(
        (
            OpenVoiceSession(
                id=1,
                guild_id=1,
                user_id=42,
                channel_id=9,
                joined_at=joined,
                last_event_at=joined,
            ),
        ),
        (LiveVoice(guild_id=1, user_id=42, channel_id=11),),
        now,
    )
    assert plan.close == ()
    assert plan.open == ()
    assert len(plan.keep) == 1
    assert plan.keep[0].session.joined_at == joined
    assert plan.keep[0].channel_id == 11
    assert plan.keep[0].last_event_at == now


def test_duplicate_open_rows_collapse_to_one() -> None:
    joined = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    later = datetime(2026, 1, 1, 11, 30, tzinfo=UTC)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    plan = reconcile_sessions(
        (
            OpenVoiceSession(id=1, guild_id=1, user_id=42, channel_id=9, joined_at=joined, last_event_at=joined),
            OpenVoiceSession(id=2, guild_id=1, user_id=42, channel_id=9, joined_at=later, last_event_at=later),
        ),
        (LiveVoice(guild_id=1, user_id=42, channel_id=9),),
        now,
    )
    assert len(plan.keep) == 1
    assert plan.keep[0].session.id == 1
    assert len(plan.close) == 1
    assert plan.close[0].session.id == 2


def test_user_logs_does_not_count_voice_seconds() -> None:
    source = USER_LOGS_PY.read_text(encoding="utf-8")
    assert "note_voice_seconds" not in source
    assert "voice_joined" not in source
    assert "collector" not in source


async def test_double_leave_does_not_double_seconds(tmp_path: Path) -> None:
    tracker, sessions, engine = await _tracker(tmp_path)
    joined = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    left = joined + timedelta(minutes=5)
    try:
        await tracker.apply(
            sessions,
            guild_id=1,
            user_id=42,
            before_channel_id=None,
            after_channel_id=9,
            now=joined,
        )
        first = await tracker.apply(
            sessions,
            guild_id=1,
            user_id=42,
            before_channel_id=9,
            after_channel_id=None,
            now=left,
        )
        second = await tracker.apply(
            sessions,
            guild_id=1,
            user_id=42,
            before_channel_id=9,
            after_channel_id=None,
            now=left + timedelta(seconds=1),
        )
        assert first == 300
        assert second is None
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(VoiceSession).where(VoiceSession.user_id == 42))).all()
        assert len(rows) == 1
        assert rows[0].duration_seconds == 300
        assert as_utc(rows[0].left_at) == left
    finally:
        await engine.dispose()


async def test_restore_closes_stale_at_last_event(tmp_path: Path) -> None:
    tracker, sessions, engine = await _tracker(tmp_path)
    joined = datetime(2026, 1, 1, 11, 50, tzinfo=UTC)
    last_event = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=42,
                    channel_id=9,
                    joined_at=joined,
                    left_at=None,
                    duration_seconds=None,
                    last_event_at=last_event,
                )
            )
        await tracker.restore(sessions, guild_id=1, live=(), now=now)
        async with session_scope(sessions) as session:
            row = await session.scalar(select(VoiceSession).where(VoiceSession.user_id == 42))
        assert row is not None
        assert as_utc(row.left_at) == last_event
        assert row.duration_seconds == 600
        assert tracker.opened_at(1, 42) is None
    finally:
        await engine.dispose()


async def test_restore_keeps_live_session_without_doubling(tmp_path: Path) -> None:
    tracker, sessions, engine = await _tracker(tmp_path)
    joined = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    now = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    left = now + timedelta(minutes=10)
    try:
        async with session_scope(sessions) as session:
            session.add(
                VoiceSession(
                    guild_id=1,
                    user_id=42,
                    channel_id=9,
                    joined_at=joined,
                    left_at=None,
                    duration_seconds=None,
                    last_event_at=joined,
                )
            )
        await tracker.restore(
            sessions,
            guild_id=1,
            live=(LiveVoice(guild_id=1, user_id=42, channel_id=11),),
            now=now,
        )
        seconds = await tracker.apply(
            sessions,
            guild_id=1,
            user_id=42,
            before_channel_id=11,
            after_channel_id=None,
            now=left,
        )
        assert seconds == 7800
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(VoiceSession).where(VoiceSession.user_id == 42))).all()
        assert len(rows) == 1
        assert as_utc(rows[0].joined_at) == joined
        assert rows[0].duration_seconds == 7800
    finally:
        await engine.dispose()


async def test_spawn_logs_task_exception(tmp_path: Path) -> None:
    bot = Conductor(Settings(discord_token="t", database_url=_url(tmp_path)))
    seen: list[tuple[str, dict]] = []
    try:

        def _capture(event: str, **kwargs: object) -> None:
            seen.append((event, kwargs))

        with (
            patch("conductor.bot.log.error", side_effect=_capture),
            patch("conductor.services.errors.log.error", side_effect=_capture),
        ):
            bot.spawn(_boom(), name="test-boom")
            await asyncio.sleep(0.05)
        assert any(event in {"background_task_failed", "critical_error"} for event, _kwargs in seen)
        assert any("spawn-boom" in str(kwargs) for _event, kwargs in seen)
    finally:
        await bot.close()


async def _boom() -> None:
    raise RuntimeError("spawn-boom")
