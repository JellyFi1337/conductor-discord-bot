"""Закрытие дел: архив, ЛС и порядок ответа до teardown."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.support.attachments import assert_attachments_resolved
from tests.test_evidence import (
    DETAIL,
    WATCHER_ROLE,
    _bot,
    _Category,
    _engine,
    _Guild,
    _member,
    _open_evidence,
    _TextChannel,
)

from conductor.db.models import ModSettings
from conductor.db.session import session_scope
from conductor.presentation import copy
from conductor.presentation.guard import GuardVerdict
from conductor.presentation.interact import run_teardown, split_teardown
from conductor.presentation.moderation import CaseEvidenceFileButton
from conductor.services.evidence import EvidenceResult


def _stamp() -> datetime:
    return datetime(2026, 9, 7, 8, tzinfo=UTC)


def _history(stamp: datetime) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            author=SimpleNamespace(id=2, display_name="Mod"),
            content="улика в канале",
            created_at=stamp,
            attachments=(),
            edited_at=None,
            message_snapshots=(),
        )
    ]


async def _prepare(
    tmp_path: Path,
    name: str,
    *,
    archive: object | None,
) -> tuple[object, async_sessionmaker, object, object, object, _TextChannel, object]:
    engine, sessions = await _engine(tmp_path, name)
    bot = _bot(sessions)
    guild = _Guild()
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    channel = _TextChannel(70)
    channel.history_rows = _history(_stamp())
    guild.channels[70] = channel
    guild.members[2] = actor
    guild.members[3] = target
    if archive is not None:
        guild.channels[int(archive.id)] = archive
        async with session_scope(sessions) as session:
            session.add(ModSettings(guild_id=1, evidence_archive_channel_id=int(archive.id)))
    return engine, sessions, bot, guild, actor, channel, target


async def test_text_archive_posts_file_then_deletes_channel(tmp_path: Path) -> None:
    archive = _TextChannel(91)
    engine, sessions, bot, guild, actor, channel, _target = await _prepare(
        tmp_path, "text_archive", archive=archive
    )
    stamp = _stamp()
    events: list[str] = []
    original_delete = channel.delete

    async def tracked_delete(*, reason: str | None = None) -> None:
        events.append("delete")
        await original_delete(reason=reason)

    channel.delete = tracked_delete  # type: ignore[method-assign]
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        packed = await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp)
        body, teardown = split_teardown(packed)
        assert isinstance(body, EvidenceResult)
        assert body.ok is True
        assert archive.posts
        assert_attachments_resolved(archive.posts[0])
        assert channel.deleted is False
        events.append("respond")
        await run_teardown(teardown)
        assert channel.deleted is True
        assert events == ["respond", "delete"]
        assert copy.EVIDENCE_DM_DELIVERED in (body.detail or "")
        assert copy.EVIDENCE_TRANSCRIPT_IN_ARCHIVE in (body.detail or "")
    finally:
        await engine.dispose()


async def test_category_archive_moves_channel(tmp_path: Path) -> None:
    archive = _Category(90)
    engine, sessions, bot, guild, actor, channel, _target = await _prepare(
        tmp_path, "cat_archive", archive=archive
    )
    stamp = _stamp()
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        packed = await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp)
        body, teardown = split_teardown(packed)
        assert isinstance(body, EvidenceResult)
        assert body.ok is True
        assert channel.deleted is False
        assert channel.category is None
        await run_teardown(teardown)
        assert channel.deleted is False
        assert channel.category is archive
    finally:
        await engine.dispose()


async def test_repeat_close_is_denied(tmp_path: Path) -> None:
    archive = _TextChannel(91)
    engine, sessions, bot, guild, actor, channel, _target = await _prepare(
        tmp_path, "repeat_close", archive=archive
    )
    stamp = _stamp()
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        first = await _closed_ok(await bot.evidence.close_without_dm(guild, evidence_id, actor, now=stamp))
        assert first.ok is True
        assert channel.deleted is True
        second, teardown = split_teardown(
            await bot.evidence.close_without_dm(guild, evidence_id, actor, now=stamp)
        )
        assert isinstance(second, EvidenceResult)
        assert second.ok is False
        assert second.error == copy.EVIDENCE_ALREADY_CLOSED
        assert teardown is None
        again, _teardown = split_teardown(
            await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp)
        )
        assert again.ok is False
        assert again.error == copy.EVIDENCE_ALREADY_CLOSED
    finally:
        await engine.dispose()


async def test_forbidden_dm_still_archives_transcript(tmp_path: Path) -> None:
    archive = _TextChannel(91)
    engine, sessions, bot, guild, actor, channel, target = await _prepare(
        tmp_path, "dm_closed", archive=archive
    )

    async def boom(**_kwargs: object) -> None:
        raise discord.Forbidden(MagicMock(status=403), "blocked")

    target.send = boom
    stamp = _stamp()
    try:
        evidence_id = await _open_evidence(sessions, channel_id=70)
        packed = await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp)
        body, teardown = split_teardown(packed)
        assert isinstance(body, EvidenceResult)
        assert body.ok is True
        assert body.dm2_ok is False
        assert copy.EVIDENCE_DM_CLOSED_SHORT in (body.detail or "")
        assert archive.posts
        assert_attachments_resolved(archive.posts[0])
        assert channel.deleted is False
        await run_teardown(teardown)
        assert channel.deleted is True
    finally:
        await engine.dispose()


async def test_appeal_evidence_file_attaches_via_card_file() -> None:
    sent: list[dict[str, object]] = []

    class _Response:
        def __init__(self) -> None:
            self._done = False

        def is_done(self) -> bool:
            return self._done

        async def send_message(self, **kwargs: object) -> None:
            sent.append(kwargs)
            self._done = True

    tickets = SimpleNamespace(appeal_evidence_file=AsyncMock(return_value=("delo-1.txt", "улика")))
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        user=SimpleNamespace(id=7),
        response=_Response(),
        client=SimpleNamespace(tickets=tickets, moderation=SimpleNamespace()),
    )
    await CaseEvidenceFileButton(1).on_action(interaction, GuardVerdict(ok=True, steps=()))
    assert sent
    assert_attachments_resolved(sent[0])


async def _closed_ok(packed: object) -> EvidenceResult:
    body, teardown = split_teardown(packed)
    await run_teardown(teardown)
    assert isinstance(body, EvidenceResult)
    return body
