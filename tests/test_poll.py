from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import PollVote as VoteRow
from conductor.db.session import session_scope
from conductor.domain.poll import (
    FLUSH_INTERVAL,
    ManageVerdict,
    PollCard,
    PollKind,
    PollState,
    PollVote,
    VoteVerdict,
    apply_vote,
    parse_options,
    parse_spec,
    shown_count,
    shown_voters,
)
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.presentation.poll import (
    PollCloseButton,
    PollDeleteButton,
    PollPickSelect,
    PollVoteButton,
    closed_view,
    live_view,
    option_lines,
    reveal_view,
)
from conductor.services.poll import PollService
from conductor.services.queue import ActionQueue

STAMP = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeMessage:
    def __init__(self, message_id: int, *, channel_id: int = 5, guild_id: int = 1) -> None:
        self.id = message_id
        self.edits = 0
        self.deleted = False
        self.views: list[object] = []
        self.edit_kwargs: list[dict[str, object]] = []
        self.jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def edit(self, **kwargs: object) -> FakeMessage:
        self.edits += 1
        self.views.append(kwargs.get("view"))
        self.edit_kwargs.append(kwargs)
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id: int = 5, guild_id: int = 1) -> None:
        self.id = channel_id
        self.guild_id = guild_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.sends: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        self.sends.append(kwargs)
        message = FakeMessage(100 + len(self.sent), channel_id=self.id, guild_id=self.guild_id)
        self.sent.append(message)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage | None:
        return self.messages.get(int(message_id))


class FakeMember:
    def __init__(self, user_id: int, *, bot: bool = False) -> None:
        self.id = user_id
        self.bot = bot


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels: dict[int, FakeChannel] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


class FakePump:
    def __init__(self) -> None:
        self.items: list[tuple[int | None, dict[str, object]]] = []

    def submit(self, channel_id: int | None, **kwargs: object) -> None:
        self.items.append((channel_id, kwargs))


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _no_sleep(_delay: float) -> None:
    return None


def _spawn_box() -> tuple[list[asyncio.Task[object]], object]:
    spawned: list[asyncio.Task[object]] = []

    def spawn(coro: object, *, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        spawned.append(task)
        return task

    return spawned, spawn


async def _await_spawned(spawned: list[asyncio.Task[object]]) -> None:
    if spawned:
        await asyncio.gather(*spawned)
        spawned.clear()


def _bot(sessions, guild: FakeGuild, queue: ActionQueue, pump: FakePump | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=queue,
        scheduler=None,
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        sleep=_no_sleep,
        log_pump=pump or FakePump(),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11)},
    )


def _spec(
    *,
    minutes: int = 10,
    kind: PollKind = PollKind.PUBLIC,
    allow_change: bool = True,
    options: str = "Да\nНет",
    question: str = "Идём?",
):
    parsed = parse_spec(
        question=question,
        options=options,
        duration=str(minutes),
        kind=kind,
        allow_change=allow_change,
    )
    assert parsed is not None
    return parsed


def _voter_mentions(text: str, user_ids: tuple[int, ...]) -> set[int]:
    return {user_id for user_id in user_ids if f"<@{user_id}>" in text}


def test_parse_options_enforces_two_to_ten() -> None:
    assert parse_options("только один") is None
    two = parse_options("А\nБ")
    assert two == ("А", "Б")
    ten = parse_options("\n".join(str(i) for i in range(10)))
    assert ten is not None and len(ten) == 10
    assert parse_options("\n".join(str(i) for i in range(11))) is None
    assert parse_options("А\nА") is None
    assert parse_options("А\n\nБ") == ("А", "Б")


def test_apply_vote_change_does_not_look_like_a_second_ballot() -> None:
    assert apply_vote(previous=None, choice=0, option_count=2, allow_change=True, open=True) is VoteVerdict.CAST
    assert apply_vote(previous=0, choice=1, option_count=2, allow_change=True, open=True) is VoteVerdict.CHANGED
    assert apply_vote(previous=0, choice=1, option_count=2, allow_change=False, open=True) is VoteVerdict.LOCKED
    assert apply_vote(previous=0, choice=0, option_count=2, allow_change=True, open=True) is VoteVerdict.ALREADY
    assert apply_vote(previous=None, choice=0, option_count=2, allow_change=True, open=False) is VoteVerdict.CLOSED


def test_anonymous_and_hidden_never_leak_voters_on_the_card() -> None:
    votes = (PollVote(11, 0), PollVote(12, 1))
    anon = PollCard(
        poll_id=3,
        question="Идём?",
        host_id=1,
        kind=PollKind.ANONYMOUS,
        allow_change=True,
        options=("Да", "Нет"),
        ends_at=STAMP + timedelta(hours=1),
        votes=votes,
    )
    live = collected_text(live_view(anon))
    assert "Да — 1" in live
    assert _voter_mentions(live, (11, 12)) == set()
    closed = collected_text(closed_view(replace(anon, state=PollState.CLOSED)))
    assert _voter_mentions(closed, (11, 12)) == set()
    hidden = PollCard(
        poll_id=3,
        question="Идём?",
        host_id=1,
        kind=PollKind.HIDDEN,
        allow_change=True,
        options=("Да", "Нет"),
        ends_at=STAMP + timedelta(hours=1),
        votes=votes,
    )
    hush = collected_text(live_view(hidden))
    assert "Да — 0" in hush
    assert "Нет — 0" in hush
    assert _voter_mentions(hush, (11, 12)) == set()
    opened = collected_text(closed_view(replace(hidden, state=PollState.CLOSED)))
    assert "Да — 1" in opened
    assert "<@11>" in opened
    public = PollCard(
        poll_id=3,
        question="Идём?",
        host_id=1,
        kind=PollKind.PUBLIC,
        allow_change=True,
        options=("Да", "Нет"),
        ends_at=STAMP + timedelta(hours=1),
        votes=votes,
        state=PollState.OPEN,
    )
    names = collected_text(live_view(public))
    assert "<@11>" in names and "<@12>" in names
    leaked = collected_text(reveal_view(anon))
    assert "<@11>" in leaked and copy_notice_in_reveal(leaked)
    ids = [getattr(item, "custom_id", "") for item in live_view(anon).walk_children()]
    assert "c:pl:v0:3" in ids
    assert "c:pl:close:3" in ids
    assert "c:pl:del:3" in ids
    assert parse_custom_id("c:pl:v0:3") is not None
    assert PollVoteButton in dynamic_items()
    assert PollCloseButton in dynamic_items()
    assert PollDeleteButton in dynamic_items()
    assert PollPickSelect in dynamic_items()


def copy_notice_in_reveal(text: str) -> bool:
    from conductor.presentation import copy

    return copy.POLL_REVEAL_NOTICE in text


def test_six_options_use_a_select() -> None:
    card = PollCard(
        poll_id=4,
        question="Цвет?",
        host_id=1,
        kind=PollKind.PUBLIC,
        allow_change=True,
        options=("а", "б", "в", "г", "д", "е"),
        ends_at=STAMP,
    )
    ids = [getattr(item, "custom_id", "") for item in live_view(card).walk_children()]
    assert "c:pl:pick:4" in ids
    assert "c:pl:v0:4" not in ids


def test_shown_helpers_hide_anonymous_names_and_hidden_counts() -> None:
    assert shown_voters(PollKind.ANONYMOUS, PollState.CLOSED, (1, 2)) == ()
    assert shown_voters(PollKind.PUBLIC, PollState.OPEN, (1,)) == (1,)
    assert shown_count(PollKind.HIDDEN, PollState.OPEN, 7) == 0
    assert shown_count(PollKind.HIDDEN, PollState.CLOSED, 7) == 7
    assert option_lines(
        PollCard(
            poll_id=1,
            question="?",
            host_id=1,
            kind=PollKind.ANONYMOUS,
            allow_change=True,
            options=("А", "Б"),
            ends_at=STAMP,
            votes=(PollVote(9, 0),),
        )
    ) == ("• А — 1", "• Б — 0")


async def test_stranger_cannot_close(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = PollService(_bot(sessions, guild, queue))
    try:
        started = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert started is not None
        poll_id = started.poll_id
        result = await service.close(guild, poll_id, actor_id=99, has_manage=False)
        assert result is ManageVerdict.DENIED
        card = await service.open_in_channel(guild.id, channel.id)
        assert card is not None
        assert card.state is PollState.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_vote_change_does_not_double_the_counter(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = PollService(_bot(sessions, guild, queue))
    try:
        started = await service.start(guild, channel, actor_id=1, spec=_spec(allow_change=True), now=STAMP)
        assert started is not None
        poll_id = started.poll_id
        assert await service.vote(guild, poll_id, FakeMember(11), 0) is VoteVerdict.CAST
        assert await service.vote(guild, poll_id, FakeMember(11), 1) is VoteVerdict.CHANGED
        async with session_scope(sessions) as session:
            n = await session.scalar(select(func.count()).select_from(VoteRow).where(VoteRow.poll_id == poll_id))
            row = await session.scalar(select(VoteRow).where(VoteRow.poll_id == poll_id, VoteRow.user_id == 11))
        assert n == 1
        assert row is not None
        assert row.option_index == 1
        locked = await service.start(
            guild,
            guild.add_channel(FakeChannel(channel_id=6)),
            actor_id=1,
            spec=_spec(allow_change=False),
            now=STAMP,
        )
        assert locked is not None
        locked_id = locked.poll_id
        assert await service.vote(guild, locked_id, FakeMember(11), 0) is VoteVerdict.CAST
        assert await service.vote(guild, locked_id, FakeMember(11), 1) is VoteVerdict.LOCKED
        async with session_scope(sessions) as session:
            locked = await session.scalar(select(VoteRow).where(VoteRow.poll_id == locked_id, VoteRow.user_id == 11))
        assert locked is not None
        assert locked.option_index == 0
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_anonymous_stays_nameless_through_vote_close_and_reveal(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    pump = FakePump()
    service = PollService(_bot(sessions, guild, queue, pump))
    try:
        started = await service.start(guild, channel, actor_id=1, spec=_spec(kind=PollKind.ANONYMOUS), now=STAMP)
        assert started is not None
        poll_id = started.poll_id
        assert await service.vote(guild, poll_id, FakeMember(11), 0) is VoteVerdict.CAST
        clock.advance(FLUSH_INTERVAL)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        live = await service.open_in_channel(guild.id, channel.id)
        assert live is not None
        shown = collected_text(live_view(live))
        assert _voter_mentions(shown, (11,)) == set()
        assert "Да — 1" in shown
        ended = await service.close(guild, poll_id, actor_id=1, has_manage=False, now=STAMP + timedelta(minutes=10))
        assert ended is ManageVerdict.OK
        await _await_spawned(spawned)
        card = await service.latest_anonymous(guild.id, channel.id)
        assert card is not None
        closed = collected_text(closed_view(card))
        assert _voter_mentions(closed, (11,)) == set()
        revealed = await service.deanonymize(guild, poll_id, actor_id=99, now=STAMP + timedelta(minutes=11))
        assert revealed is ManageVerdict.OK
        await _await_spawned(spawned)
        assert len(channel.sent) >= 2
        again = await service.latest_anonymous(guild.id, channel.id)
        assert again is not None
        assert _voter_mentions(collected_text(closed_view(again)), (11,)) == set()
        assert "<@11>" in collected_text(reveal_view(again))
        assert pump.items
        assert pump.items[0][0] == 11
        still = await service.deanonymize(guild, poll_id, actor_id=99)
        assert still is ManageVerdict.ALREADY
        opened = await service.start(
            guild,
            guild.add_channel(FakeChannel(channel_id=7)),
            actor_id=1,
            spec=_spec(kind=PollKind.ANONYMOUS),
            now=STAMP,
        )
        assert opened is not None
        assert await service.deanonymize(guild, opened.poll_id, actor_id=99) is ManageVerdict.OPEN
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_timer_closes_from_database(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    dead = ActionQueue(spawn, clock=clock)
    bot = _bot(sessions, guild, dead)
    service = PollService(bot)
    try:
        started = await service.start(guild, channel, actor_id=1, spec=_spec(minutes=10), now=STAMP)
        assert started is not None
        clock2 = FakeClock()
        live = ActionQueue(spawn, clock=clock2)
        bot.queue = live
        restored = PollService(bot)
        count = await restored.restore(now=STAMP + timedelta(minutes=4))
        assert count == 1
        clock2.advance(5 * 60)
        assert await live.fire_due() == 0
        clock2.advance(60)
        assert await live.fire_due() == 1
        await _await_spawned(spawned)
        card = await restored.open_in_channel(guild.id, channel.id)
        assert card is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


def _role_ids(mentions: object) -> list[int]:
    roles = getattr(mentions, "roles", False)
    if not roles or roles is True:
        return []
    return [int(role.id) for role in roles]


async def test_start_without_settings_stays_in_current_channel(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = PollService(_bot(sessions, guild, queue))
    try:
        started = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert started is not None
        assert started.redirected is False
        assert started.channel_id == channel.id
        assert len(channel.sent) == 1
        mentions = channel.sends[0]["allowed_mentions"]
        assert getattr(mentions, "everyone", True) is False
        assert _role_ids(mentions) == []
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_start_posts_to_configured_channel_and_pings_one_role(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    here = guild.add_channel(FakeChannel(channel_id=5))
    polls = guild.add_channel(FakeChannel(channel_id=80))
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = PollService(_bot(sessions, guild, queue))
    try:
        await service.set_channel(guild.id, polls.id)
        await service.set_ping_role(guild.id, 77)
        started = await service.start(guild, here, actor_id=1, spec=_spec(), now=STAMP)
        assert started is not None
        assert started.redirected is True
        assert started.channel_id == polls.id
        assert here.sent == []
        assert len(polls.sent) == 1
        mentions = polls.sends[0]["allowed_mentions"]
        assert getattr(mentions, "everyone", True) is False
        assert _role_ids(mentions) == [77]
        shown = collected_text(polls.sends[0]["view"])
        assert "<@&77>" in shown
        closed = await service.close(guild, started.poll_id, actor_id=1, has_manage=False)
        assert closed is ManageVerdict.OK
        await _await_spawned(spawned)
        edit = polls.messages[started.message_id].edit_kwargs[0]["allowed_mentions"]
        assert getattr(edit, "everyone", True) is False
        assert _role_ids(edit) == []
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_empty_ping_role_falls_back_to_polls_tag(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    here = guild.add_channel(FakeChannel(channel_id=5))
    polls = guild.add_channel(FakeChannel(channel_id=80))
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)

    async def role_for_tag(_guild_id: int, tag: str) -> int | None:
        return 88 if tag == "polls" else None

    bot.self_roles = SimpleNamespace(role_for_tag=role_for_tag)
    service = PollService(bot)
    try:
        await service.set_channel(guild.id, polls.id)
        started = await service.start(guild, here, actor_id=1, spec=_spec(), now=STAMP)
        assert started is not None
        mentions = polls.sends[0]["allowed_mentions"]
        assert _role_ids(mentions) == [88]
        shown = collected_text(polls.sends[0]["view"])
        assert "<@&88>" in shown
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_missing_configured_channel_does_not_fall_back(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    here = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    service = PollService(_bot(sessions, guild, queue))
    try:
        await service.set_channel(guild.id, 99)
        started = await service.start(guild, here, actor_id=1, spec=_spec(), now=STAMP)
        assert started is None
        assert here.sent == []
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
