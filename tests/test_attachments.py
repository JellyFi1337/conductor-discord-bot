from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord import ui
from tests.support.attachments import assert_attachments_resolved

from conductor.db.models import ModSettings
from conductor.db.session import session_scope
from conductor.domain.tickets import StoredTicket
from conductor.presentation.guard import GuardVerdict
from conductor.presentation.layout import (
    FileCard,
    card_file,
    channel_kwargs,
    edit_kwargs,
    message_kwargs,
    panel,
)
from conductor.presentation.moderation import CaseEvidenceFileButton
from conductor.presentation.tickets import TicketTranscriptButton, ticket_anon_relay_view
from conductor.services.tickets import TicketService, _attachment_file


def test_panel_with_card_file_builds_file_card() -> None:
    view = panel("Заголовок", files=[card_file("a.txt", "x")])
    assert isinstance(view, FileCard)
    files = [item for item in view.walk_children() if isinstance(item, ui.File)]
    assert len(files) == 1
    assert files[0].url == "attachment://a.txt"


def test_kwargs_attach_fresh_files() -> None:
    view = panel("Заголовок", files=[card_file("a.txt", "x")])
    message = message_kwargs(view)
    channel = channel_kwargs(view)
    edited = edit_kwargs(view)
    assert message["files"][0].filename == "a.txt"
    assert channel["files"][0].filename == "a.txt"
    assert edited["attachments"][0].filename == "a.txt"
    assert_attachments_resolved(message)
    assert_attachments_resolved(channel)
    assert_attachments_resolved(edited)


def test_fresh_files_are_independent() -> None:
    view = panel("Заголовок", files=[card_file("a.txt", "hello")])
    first = view.fresh_files()
    second = view.fresh_files()
    assert first[0] is not second[0]
    assert first[0].fp.read() == b"hello"
    assert second[0].fp.read() == b"hello"


def test_ordinary_card_kwargs_keys_unchanged() -> None:
    kwargs = message_kwargs(panel("Заголовок"))
    assert set(kwargs) == {"view", "ephemeral", "allowed_mentions"}
    assert "files" not in channel_kwargs(panel("Заголовок"))
    assert "attachments" not in edit_kwargs(panel("Заголовок"))


class _Capture:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)

        async def edit(**ekwargs: object) -> SimpleNamespace:
            self.edits.append(ekwargs)
            return message

        message = SimpleNamespace(id=1, edit=edit)
        return message


def _ticket() -> StoredTicket:
    return StoredTicket(
        guild_id=1,
        number=4,
        author_id=1,
        category_id=1,
        channel_id=10,
        id=1,
        category_name="Жалоба",
        close_reason="закрыто",
    )


async def test_ticket_transcript_short_path_attaches_file() -> None:
    archive = _Capture()
    service = TicketService(SimpleNamespace(outbound=None))
    await service._send_transcript(
        SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        _ticket(),
        "лог тикета".encode(),
        1,
        archive,
    )
    assert archive.sent
    assert_attachments_resolved(archive.sent[-1])


async def test_ticket_transcript_progress_path_attaches_file() -> None:
    archive = _Capture()
    service = TicketService(SimpleNamespace(outbound=None))
    await service._send_transcript(
        SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        _ticket(),
        "длинный лог".encode(),
        40,
        archive,
    )
    assert archive.edits
    assert_attachments_resolved(archive.edits[-1])


async def test_transcript_button_attaches_file() -> None:
    sent: list[dict[str, object]] = []

    class _Response:
        def __init__(self) -> None:
            self._done = False

        def is_done(self) -> bool:
            return self._done

        async def send_message(self, **kwargs: object) -> None:
            sent.append(kwargs)
            self._done = True

    tickets = SimpleNamespace(transcript_file=AsyncMock(return_value=(b"hello", "ticket-5.txt")))
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1, filesize_limit=10 * 1024 * 1024),
        user=SimpleNamespace(id=7),
        response=_Response(),
        client=SimpleNamespace(tickets=tickets),
    )
    await TicketTranscriptButton(5).on_action(interaction, GuardVerdict(ok=True, steps=()))
    assert sent
    assert_attachments_resolved(sent[0])


async def test_evidence_transcript_attaches_file(tmp_path: Path) -> None:
    from tests.test_evidence import (
        DETAIL,
        WATCHER_ROLE,
        _bot,
        _engine,
        _Guild,
        _member,
        _open_evidence,
        _TextChannel,
    )

    from conductor.presentation.interact import split_teardown

    engine, sessions = await _engine(tmp_path, "attach_evidence")
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
        result, _teardown = split_teardown(
            await bot.evidence.send_reason(guild, evidence_id, actor, DETAIL, now=stamp)
        )
        assert result.ok is True
        assert archive.posts
        assert_attachments_resolved(archive.posts[-1])
    finally:
        await engine.dispose()


async def test_appeal_evidence_button_attaches_file() -> None:
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


async def test_anonymous_attachment_is_resolved() -> None:
    class _Attach:
        filename = "shot.png"
        size = 5
        url = None

        async def read(self) -> bytes:
            return b"hello"

    media = await _attachment_file(_Attach())
    assert media is not None
    channel = _Capture()
    service = TicketService(SimpleNamespace(outbound=None))
    posted = await service._send_relay_pages(channel, ticket_anon_relay_view, "из лички", (media,))
    assert posted is True
    assert_attachments_resolved(channel.sent[0])
