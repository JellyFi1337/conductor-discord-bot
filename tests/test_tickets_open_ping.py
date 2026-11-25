"""Тег автора при открытии тикета и полнота транскрипта."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ModCase, Ticket, TicketCategory, TicketPanel
from conductor.db.session import session_scope
from conductor.domain.moderation import AppealVerdict
from conductor.domain.tickets import CategoryKind, OpenVerdict
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.services.cache import CacheHub
from conductor.services.tickets import TicketService
from conductor.services.transcripts import collect_transcript, format_transcript_message


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


class _Aiter:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0

    def __aiter__(self) -> _Aiter:
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class _PingChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = ""
        self.overwrites: dict[object, object] = {}
        self.posts: list[dict[str, object]] = []
        self.edits: list[dict[str, object]] = []
        self.deletes = 0

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.posts.append(kwargs)
        message = SimpleNamespace(id=200 + len(self.posts))

        async def edit(**edit_kwargs: object) -> SimpleNamespace:
            self.edits.append(edit_kwargs)
            return message

        message.edit = edit
        return message

    async def delete(self, **_kwargs: object) -> None:
        self.deletes += 1


class _Member:
    def __init__(self, user_id: int, guild: object, *, fail_dm: bool = False) -> None:
        self.id = user_id
        self.guild = guild
        self.display_name = "SecretName"
        self.roles = ()
        self.joined_at = None
        self.dms: list[dict[str, object]] = []
        self.fail_dm = fail_dm
        self.dm_channel = SimpleNamespace(id=555)
        self.guild_permissions = SimpleNamespace(administrator=False)

    async def send(self, **kwargs: object) -> SimpleNamespace:
        if self.fail_dm:
            raise discord.Forbidden(MagicMock(status=403), "blocked")
        self.dms.append(kwargs)
        return SimpleNamespace(id=1, channel=self.dm_channel)


def _mentioned_user_ids(payload: dict[str, object]) -> list[int]:
    mentions = payload.get("allowed_mentions")
    users = getattr(mentions, "users", None)
    if not users or users is True:
        return []
    return [int(getattr(item, "id", item)) for item in users]


def _channel_texts(channel: _PingChannel) -> list[str]:
    views = [item["view"] for item in (*channel.posts, *channel.edits) if "view" in item]
    return [collected_text(view) for view in views]  # type: ignore[arg-type]


def _history_message(
    *,
    author_id: int,
    name: str,
    content: str,
    stamp: datetime,
    attachments: tuple[object, ...] = (),
    edited_at: datetime | None = None,
    message_snapshots: tuple[object, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, display_name=name),
        content=content,
        created_at=stamp,
        attachments=attachments,
        edited_at=edited_at,
        message_snapshots=message_snapshots,
    )


async def _open_guild(
    tmp_path: Path,
    *,
    kind: str = CategoryKind.GENERAL,
) -> tuple[object, _Member, list[_PingChannel], TicketService, object]:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_PingChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _PingChannel:
        channel = _PingChannel(100 + len(created))
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    everyone = object()
    bot_member = object()
    members: dict[int, _Member] = {}
    guild = SimpleNamespace(
        id=1,
        me=bot_member,
        default_role=everyone,
        get_channel=lambda cid: next((item for item in created if item.id == cid), None),
        get_role=lambda _rid: None,
        get_member=lambda uid: members.get(uid),
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    member = _Member(42, guild)
    members[42] = member
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        get_guild=lambda gid: guild if gid == 1 else None,
        user=SimpleNamespace(id=99),
        fetch_user=None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_scope(sessions) as session:
        panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
        session.add(panel)
        await session.flush()
        session.add(
            TicketCategory(
                panel_id=panel.id,
                guild_id=1,
                name="Вопрос" if kind != CategoryKind.ANONYMOUS else "Анонимно",
                position=0,
                kind=kind,
            )
        )
    return engine, member, created, service, guild


async def test_open_ticket_pings_author_in_new_message(tmp_path: Path) -> None:
    engine, member, created, service, guild = await _open_guild(tmp_path)
    try:
        result = await service.open_ticket(guild, member, 1)  # type: ignore[arg-type]
        assert result.verdict is OpenVerdict.OK
        channel = created[0]
        pinged = [item for item in channel.posts if 42 in _mentioned_user_ids(item)]
        assert pinged
        text = collected_text(pinged[-1]["view"])  # type: ignore[arg-type]
        assert "<@42>" in text
        assert copy.TICKET_WELCOME_ASK in text
        assert copy.TICKET_CLOSE in text
        assert copy.TICKET_ESCALATE in text
        assert all(42 not in _mentioned_user_ids(item) for item in channel.edits)
        assert pinged[-1] not in channel.edits
    finally:
        await engine.dispose()


async def test_anonymous_open_has_no_author_mention_and_sends_dm(tmp_path: Path) -> None:
    engine, member, created, service, guild = await _open_guild(tmp_path, kind=CategoryKind.ANONYMOUS)
    try:
        result = await service.open_ticket(guild, member, 1)  # type: ignore[arg-type]
        assert result.verdict is OpenVerdict.OK
        channel = created[0]
        texts = _channel_texts(channel)
        assert all("<@42>" not in text for text in texts)
        assert all("SecretName" not in text for text in texts)
        assert all(42 not in _mentioned_user_ids(item) for item in (*channel.posts, *channel.edits))
        assert member.dms
        assert "без вашего имени" in collected_text(member.dms[0]["view"])  # type: ignore[arg-type]
        async with session_scope(service._bot.sessions) as session:
            row = await session.scalar(select(Ticket))
        assert row is not None
        assert row.anonymous is True
    finally:
        await engine.dispose()


async def test_appeal_open_pings_author(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    created: list[_PingChannel] = []

    async def create_text_channel(name: str, **kwargs: object) -> _PingChannel:
        channel = _PingChannel(100)
        channel.name = name
        channel.overwrites = dict(kwargs.get("overwrites") or {})
        created.append(channel)
        return channel

    everyone = object()
    members: dict[int, _Member] = {}
    guild = SimpleNamespace(
        id=1,
        me=object(),
        default_role=everyone,
        get_channel=lambda cid: next((item for item in created if item.id == cid), None),
        get_role=lambda _rid: None,
        get_member=lambda uid: members.get(uid),
        create_text_channel=create_text_channel,
        owner_id=99,
    )
    member = _Member(42, guild)
    members[42] = member
    members[7] = _Member(7, guild)
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        guard=None,
        outbound=None,
        roles=None,
        motion=None,
        get_guild=lambda gid: guild if gid == 1 else None,
        user=SimpleNamespace(id=99),
        fetch_user=None,
    )
    service = TicketService(bot)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            panel = TicketPanel(guild_id=1, channel_id=3, settings="{}", created_at=stamp, created_by=7)
            session.add(panel)
            session.add(
                ModCase(
                    guild_id=1,
                    number=4,
                    owner_id=42,
                    created_by=7,
                    actor_rank=1,
                    kind="warn",
                    reason="нарушение правил чата",
                    created_at=stamp,
                )
            )
        result = await service.open_case_appeal(
            guild,
            member,
            1,
            text="считаю наказание несправедливым полностью",
            now=stamp,
        )
        assert result.verdict is AppealVerdict.OK
        channel = created[0]
        pinged = [item for item in channel.posts if 42 in _mentioned_user_ids(item)]
        assert pinged
        text = collected_text(pinged[-1]["view"])  # type: ignore[arg-type]
        assert "<@42>" in text
        assert copy.TICKET_WELCOME_ASK in text
        assert copy.CASE_KEEP in text
        assert copy.CASE_REVOKE_BTN in text
        assert all(42 not in _mentioned_user_ids(item) for item in channel.edits)
    finally:
        await engine.dispose()


async def test_transcript_keeps_attachment_edit_and_forward() -> None:
    stamp = datetime(2026, 9, 7, 12, tzinfo=UTC)
    history = [
        _history_message(author_id=1, name="Ann", content="один", stamp=stamp),
        _history_message(
            author_id=1,
            name="Ann",
            content="скрин",
            stamp=stamp,
            attachments=(SimpleNamespace(url="https://cdn.example/a.png"),),
        ),
        _history_message(author_id=2, name="Bob", content="правка", stamp=stamp, edited_at=stamp),
        _history_message(
            author_id=3,
            name="Cara",
            content="форвард",
            stamp=stamp,
            message_snapshots=(SimpleNamespace(content="цитата из чата"),),
        ),
        _history_message(author_id=4, name="Dan", content="пять", stamp=stamp),
    ]
    channel = SimpleNamespace(history=lambda **_k: _Aiter(history))
    text, count = await collect_transcript(channel)
    assert count == 5
    assert "[2026-09-07 12:00 UTC] Ann (1): один" in text
    assert "https://cdn.example/a.png" in text
    assert f"Bob (2): правка {copy.TICKET_TRANSCRIPT_EDITED}" in text
    assert copy.ticket_transcript_forward("цитата из чата") in text
    assert "Dan (4): пять" in text
    line = format_transcript_message(history[1])
    assert "Ann (1)" in line
    assert "https://cdn.example/a.png" in line


async def test_anonymous_transcript_hides_author_id() -> None:
    owner = 424242424242
    stamp = datetime(2026, 1, 1, 12, tzinfo=UTC)
    history = [
        _history_message(author_id=owner, name="SecretName", content="из лички", stamp=stamp),
        _history_message(
            author_id=owner,
            name="SecretName",
            content="скрин",
            stamp=stamp,
            attachments=(SimpleNamespace(url="https://cdn.example/b.png"),),
            edited_at=stamp,
        ),
        _history_message(author_id=7, name="Следящий", content="ответ", stamp=stamp),
        _history_message(
            author_id=owner,
            name="SecretName",
            content="форвард",
            stamp=stamp,
            message_snapshots=(SimpleNamespace(content="чужое"),),
        ),
        _history_message(author_id=7, name="Следящий", content="закрываем", stamp=stamp),
    ]
    header = copy.ticket_transcript_header(
        number=1,
        category="Анонимно",
        author=copy.TICKET_ANON_TRANSCRIPT,
        claimer="—",
        opened=stamp.strftime("%Y-%m-%d %H:%M UTC"),
        closed="—",
        outcome="—",
        reason="—",
    )
    channel = SimpleNamespace(history=lambda **_k: _Aiter(history))
    text, count = await collect_transcript(channel, header=header, anonymous_author_id=owner)
    assert count == 5
    assert str(owner) not in text
    assert "SecretName" not in text
    assert copy.TICKET_ANON_TRANSCRIPT in text
    assert "https://cdn.example/b.png" in text
    assert copy.TICKET_TRANSCRIPT_EDITED in text
    assert "чужое" in text
    assert "Следящий (7): ответ" in text
