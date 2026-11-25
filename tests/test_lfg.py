from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import LfgMember, LfgPost, Panel
from conductor.db.session import session_scope
from conductor.domain.access import AccessDecision, Rank
from conductor.domain.discord_limits import TEXT_DISPLAY_TOTAL
from conductor.domain.lfg import (
    LFG_ROLE_COOLDOWN,
    CloseReason,
    LfgCard,
    NoteReject,
    PostVerdict,
    can_post,
    check_note,
    is_full,
    parse_need,
    playable_games,
    room_limit,
    should_attach_room,
)
from conductor.domain.rooms import RoomDenial, RoomOutcome, StoredRoom
from conductor.domain.staff_feed import ACTION_LFG_CLOSE
from conductor.presentation import copy
from conductor.presentation.copy import STALE_CARD
from conductor.presentation.layout import collected_text
from conductor.presentation.lfg import LfgJoinButton, LfgListButton, LfgNewButton, live_view
from conductor.presentation.roster import RosterView
from conductor.services.lfg import LfgService
from conductor.services.limits import InteractionLimiter


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[FakeMessage] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        message = FakeMessage(1000 + len(self.messages))
        message.kwargs = kwargs
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise LookupError(message_id)


class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(
        self,
        user_id: int,
        guild: FakeGuild,
        *,
        roles: tuple[int, ...] = (),
        name: str = "РЅРёРє",
        admin: bool = False,
    ) -> None:
        self.id = user_id
        self.guild = guild
        self.roles = [FakeRole(role_id, str(role_id)) for role_id in roles]
        self.display_name = name
        self.voice = None
        self.bot = False
        self.guild_permissions = SimpleNamespace(administrator=admin)


class FakeGuild:
    def __init__(self) -> None:
        self.id = 1
        self.channels = {20: FakeChannel(20)}
        self.roles = {7: FakeRole(7, "Forts"), 99: FakeRole(99, "РџРѕРёСЃРє РёРіСЂС‹")}
        self.members: dict[int, FakeMember] = {}

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(int(channel_id))

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.roles.get(int(role_id))

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(int(user_id))


class FakeSelfRoles:
    async def game_options(self, guild_id: int) -> tuple[SimpleNamespace, ...]:
        _ = guild_id
        return (SimpleNamespace(role_id=7, label="Forts"),)

    async def role_for_tag(self, guild_id: int, tag: str) -> int | None:
        _ = guild_id
        return 99 if tag == "lfg" else None

    async def menus(self, guild_id: int) -> tuple[SimpleNamespace, ...]:
        _ = guild_id
        return (SimpleNamespace(channel_id=30),)


class FakeRooms:
    def __init__(self) -> None:
        self.hub = SimpleNamespace(channel_id=9)
        self.owned: list[StoredRoom] = []
        self.created: list[tuple[str | None, int | None]] = []
        self.permitted: list[tuple[int, int]] = []
        self._next_id = 3
        self._next_channel = 500

    async def hub_for(self, session: object, guild_id: int) -> object:
        _ = session, guild_id
        return self.hub

    async def rooms_for(self, session: object, guild_id: int) -> list[StoredRoom]:
        _ = session, guild_id
        return list(self.owned)

    async def create_for(
        self, member: FakeMember, *, name: str | None = None, user_limit: int | None = None
    ) -> RoomOutcome:
        existing = next((room for room in self.owned if room.owner_id == member.id), None)
        if existing is not None:
            return RoomOutcome(channel_id=existing.channel_id)
        if self.hub is None:
            return RoomOutcome(denial=RoomDenial.NO_HUB)
        channel_id = self._next_channel
        self._next_channel += 1
        room = StoredRoom(
            guild_id=member.guild.id,
            channel_id=channel_id,
            owner_id=member.id,
            hub_id=9,
            id=self._next_id,
        )
        self._next_id += 1
        self.owned.append(room)
        self.created.append((name, user_limit))
        return RoomOutcome(channel_id=channel_id)

    async def room_by_channel(self, guild_id: int, channel_id: int) -> StoredRoom | None:
        _ = guild_id
        return next((room for room in self.owned if room.channel_id == int(channel_id)), None)

    async def permit_member(self, guild: object, ref: int | str, target: object) -> None:
        _ = guild
        self.permitted.append((int(ref), int(target.id)))


class FakeFeed:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post(self, guild_id: int, **kwargs: object) -> int:
        self.calls.append({"guild_id": guild_id, **kwargs})
        return 1


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _member(guild: FakeGuild, user_id: int, *, roles: tuple[int, ...] = (7,), name: str = "РЅРёРє") -> FakeMember:
    member = FakeMember(user_id, guild, roles=roles, name=name)
    guild.members[user_id] = member
    return member


def _bot(sessions, *, clock: Clock, rooms: FakeRooms | None = None, staff: bool = False) -> SimpleNamespace:
    async def decide_access(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=staff, developer=False)

    return SimpleNamespace(
        sessions=sessions,
        scheduler=None,
        self_roles=FakeSelfRoles(),
        rooms=rooms or FakeRooms(),
        staff_feed=FakeFeed(),
        decide_access=decide_access,
        get_guild=lambda _gid: None,
        caches=None,
    )


async def _service(tmp_path: Path, *, clock: Clock, rooms: FakeRooms | None = None, staff: bool = False):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions, clock=clock, rooms=rooms, staff=staff)
    service = LfgService(bot, clock=clock)
    return engine, sessions, bot, service


def test_note_and_need_and_room_flags() -> None:
    assert parse_need("0") == 0
    assert parse_need("1") == 1
    assert parse_need(" 100 ") == 100
    assert parse_need("-1") is None
    assert parse_need("101") is None
    assert parse_need("abc") is None
    assert is_full(0, 5000) is False
    assert is_full(2, 2) is True
    assert room_limit(0) == 0
    assert room_limit(98) == 99
    assert room_limit(99) == 0
    assert room_limit(100) == 0
    ok, text, reason = check_note("Р±С‹СЃС‚СЂС‹Р№ СЂР°РЅ")
    assert ok is True
    assert text == "Р±С‹СЃС‚СЂС‹Р№ СЂР°РЅ"
    assert reason == ""
    assert check_note("x" * 101)[2] == NoteReject.LONG.value
    assert check_note("@everyone")[2] == NoteReject.MENTION.value
    assert check_note("<@123>")[2] == NoteReject.MENTION.value
    assert check_note("discord.gg/abc")[2] == NoteReject.INVITE.value
    assert check_note("https://example.com")[2] == NoteReject.AD.value
    assert playable_games(((7, "Forts"), (8, "Dota")), (7,)) == ((7, "Forts"),)
    assert should_attach_room(owns_room=False, in_hub=False, in_room=False) is False
    assert should_attach_room(owns_room=True, in_hub=False, in_room=False) is True
    assert (
        can_post(
            has_live=True,
            last_created_at=None,
            now=datetime(2026, 9, 7, tzinfo=UTC),
            cooldown=timedelta(minutes=30),
        )
        is PostVerdict.LIVE
    )


async def test_game_and_lfg_ping_cooldowns(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, sessions, _bot_obj, service = await _service(tmp_path, clock=clock)
    guild = FakeGuild()
    try:
        await service.set_channel(1, 20)
        await service.set_numbers(1, 120, 15, 30)
        first = await service.publish(guild, _member(guild, 10), game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert first.error == ""
        assert first.game_pinged is True
        assert first.lfg_pinged is True
        clock.advance(timedelta(minutes=5))
        second = await service.publish(guild, _member(guild, 11), game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert second.error == ""
        assert second.game_pinged is False
        assert second.lfg_pinged is False
        async with session_scope(sessions) as session:
            rows = (await session.scalars(select(LfgPost).order_by(LfgPost.id))).all()
            assert [bool(row.game_pinged) for row in rows] == [True, False]
            assert [bool(row.lfg_pinged) for row in rows] == [True, False]
        clock.advance(timedelta(minutes=10))
        third = await service.publish(guild, _member(guild, 12), game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert third.game_pinged is True
        assert third.lfg_pinged is False
        clock.advance(LFG_ROLE_COOLDOWN)
        fourth = await service.publish(guild, _member(guild, 13), game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert fourth.lfg_pinged is True
        message = guild.channels[20].messages[0]
        mentions = message.kwargs["allowed_mentions"]
        assert [role.id for role in mentions.roles] == [7, 99]
        skipped = guild.channels[20].messages[1]
        assert not skipped.kwargs["allowed_mentions"].roles
    finally:
        await engine.dispose()


async def test_one_live_post_and_user_cooldown(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, _sessions, _bot_obj, service = await _service(tmp_path, clock=clock)
    guild = FakeGuild()
    author = _member(guild, 10)
    try:
        await service.set_channel(1, 20)
        first = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert first.error == ""
        again = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert again.error == PostVerdict.LIVE.value
        closed = await service.close(guild, int(first.post_id or 0), author)  # type: ignore[arg-type]
        assert closed.error == ""
        cooled = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert cooled.error == PostVerdict.COOLDOWN.value
        clock.advance(timedelta(minutes=30))
        later = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert later.error == ""
        assert later.post_id != first.post_id
    finally:
        await engine.dispose()


async def test_need_filled_closes_full_and_creates_room(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    rooms = FakeRooms()
    engine, sessions, _bot_obj, service = await _service(tmp_path, clock=clock, rooms=rooms)
    guild = FakeGuild()
    author = _member(guild, 10, name="РђРЅСЏ")
    first = _member(guild, 11, name="Р‘РѕР±")
    second = _member(guild, 12, name="РљРёСЂР°")
    extra = _member(guild, 13)
    try:
        await service.set_channel(1, 20)
        posted = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        assert posted.room_id is None
        assert rooms.created == []
        joined = await service.join(guild, int(posted.post_id or 0), first)  # type: ignore[arg-type]
        assert joined.error == ""
        assert rooms.created
        assert rooms.created[0][1] == 3
        assert "Forts" in str(rooms.created[0][0])
        filled = await service.join(guild, int(posted.post_id or 0), second)  # type: ignore[arg-type]
        assert filled.error == ""
        late = await service.join(guild, int(posted.post_id or 0), extra)  # type: ignore[arg-type]
        assert late.error == "full" or late.error == "closed"
        author_join = await service.join(guild, int(posted.post_id or 0), author)  # type: ignore[arg-type]
        assert author_join.error in {"author", "closed", "full"}
        async with session_scope(sessions) as session:
            row = await session.get(LfgPost, posted.post_id)
            assert row is not None
            assert row.closed_reason == CloseReason.FULL.value
            assert row.room_id == rooms.owned[0].id
        assert {item[1] for item in rooms.permitted} == {11, 12}
        assert await service.author_count(1, 10, now=clock.now) == 1
        games = await service.playable(1, [7])
        assert games == ((7, "Forts"),)
        empty = await service.playable(1, [])
        assert empty == ()
    finally:
        await engine.dispose()


async def test_unlimited_need_keeps_thousand_open(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    rooms = FakeRooms()
    engine, sessions, bot, service = await _service(tmp_path, clock=clock, rooms=rooms)
    bot.lfg = service
    guild = FakeGuild()
    author = _member(guild, 10)
    extra = _member(guild, 5000)
    try:
        await service.set_channel(1, 20)
        posted = await service.publish(guild, author, game_role_id=7, need="0", note="")  # type: ignore[arg-type]
        assert posted.error == ""
        moment = clock.now
        async with session_scope(sessions) as session:
            session.add_all(
                [
                    LfgMember(post_id=int(posted.post_id or 0), user_id=2000 + index, joined_at=moment)
                    for index in range(1000)
                ]
            )
        joined = await service.join(guild, int(posted.post_id or 0), extra)  # type: ignore[arg-type]
        assert joined.error == ""
        async with session_scope(sessions) as session:
            row = await session.get(LfgPost, posted.post_id)
            assert row is not None
            assert row.closed_at is None
            assert row.closed_reason is None
        ids = await service.signed_ids(guild, int(posted.post_id or 0))  # type: ignore[arg-type]
        assert len(ids) == 1001
        assert extra.id in ids
        card = LfgCard(
            post_id=int(posted.post_id or 0),
            game="Forts",
            author_id=author.id,
            need=0,
            signed=len(ids),
            note="",
            expires_at=clock.now + timedelta(minutes=120),
            signed_ids=tuple(ids[:10]),
        )
        text = collected_text(live_view(card))
        assert len(text) <= TEXT_DISPLAY_TOTAL
        assert "без ограничения" in text
        assert "и ещё" in text
        roster = RosterView(copy.LFG_FIELD_LIST, ids, guild=guild)  # type: ignore[arg-type]
        assert len(collected_text(roster)) <= TEXT_DISPLAY_TOTAL
        press = _press(bot, guild, extra, f"c:lf:list:{posted.post_id}")
        await LfgListButton(posted.post_id).callback(press)  # type: ignore[arg-type]
        roster_text = collected_text(press.response.payloads[0]["view"])  # type: ignore[arg-type]
        assert len(roster_text) <= TEXT_DISPLAY_TOTAL
        assert rooms.created
        assert rooms.created[0][1] == 0
    finally:
        await engine.dispose()


async def test_staff_close_writes_actions(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, _sessions, bot, service = await _service(tmp_path, clock=clock, staff=True)
    guild = FakeGuild()
    author = _member(guild, 10)
    staff = _member(guild, 99, roles=())
    try:
        await service.set_channel(1, 20)
        posted = await service.publish(guild, author, game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        closed = await service.close(guild, int(posted.post_id or 0), staff)  # type: ignore[arg-type]
        assert closed.error == ""
        assert bot.staff_feed.calls
        assert bot.staff_feed.calls[0]["action"] == ACTION_LFG_CLOSE
        assert bot.staff_feed.calls[0]["target"] == 10
        assert bot.staff_feed.calls[0]["ref"] == posted.post_id
    finally:
        await engine.dispose()


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.modals: list[object] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True

    async def send_modal(self, modal: object) -> None:
        self.modals.append(modal)
        self._done = True


def _press(bot: SimpleNamespace, guild: FakeGuild, member: FakeMember, custom_id: str) -> SimpleNamespace:
    user = MagicMock(spec=discord.Member)
    user.id = member.id
    user.roles = member.roles
    user.guild_permissions = SimpleNamespace(administrator=False)
    user.display_name = member.display_name
    user.bot = False
    user.voice = None
    user.guild = guild
    response = _Response()

    async def followup_send(**kwargs: object) -> None:
        response.payloads.append(kwargs)

    async def decide_access(**_kwargs: object) -> AccessDecision:
        return AccessDecision(False, Rank.NONE)

    bot.decide_access = decide_access
    bot.interactions = InteractionLimiter(limit=50, window=60)
    bot.log_pump = SimpleNamespace(submit=lambda *_a, **_k: None)
    bot.config_by_guild = SimpleNamespace(get=lambda *_a, **_k: None)
    return SimpleNamespace(
        guild=guild,
        user=user,
        client=bot,
        response=response,
        followup=SimpleNamespace(send=followup_send),
        data={"custom_id": custom_id},
    )


async def test_buttons_load_post_and_starter_as_guard_resources(tmp_path: Path) -> None:
    """Regression: the base loader returned None, so every LFG button answered В«СѓСЃС‚Р°СЂРµР»РѕВ»."""
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, sessions, bot, service = await _service(tmp_path, clock=clock)
    bot.lfg = service
    guild = FakeGuild()
    author = _member(guild, 10, name="РђРЅСЏ")
    joiner = _member(guild, 11, name="Р‘РѕР±")
    try:
        await service.set_channel(1, 20)
        assert await service.post_starter(guild, guild.channels[20], actor_id=10)
        posted = await service.publish(guild, author, game_role_id=7, need="3", note="")  # type: ignore[arg-type]
        post_id = int(posted.post_id or 0)

        press = _press(bot, guild, joiner, f"c:lf:join:{post_id}")
        await LfgJoinButton(post_id).callback(press)  # type: ignore[arg-type]
        text = collected_text(press.response.payloads[0]["view"])  # type: ignore[arg-type]
        assert STALE_CARD not in text
        async with session_scope(sessions) as session:
            signed = (await session.scalars(select(LfgMember.user_id).where(LfgMember.post_id == post_id))).all()
        assert list(signed) == [11]

        starter = _press(bot, guild, joiner, "c:lf:new:1")
        await LfgNewButton(1).callback(starter)  # type: ignore[arg-type]
        assert starter.response.modals, "single playable game must open the modal, not defer"

        await service.close(guild, post_id, author)  # type: ignore[arg-type]
        late = _press(bot, guild, _member(guild, 12), f"c:lf:join:{post_id}")
        await LfgJoinButton(post_id).callback(late)  # type: ignore[arg-type]
        assert STALE_CARD in collected_text(late.response.payloads[0]["view"])  # type: ignore[arg-type]

        missing = _press(bot, guild, joiner, "c:lf:join:777")
        await LfgJoinButton(777).callback(missing)  # type: ignore[arg-type]
        assert STALE_CARD in collected_text(missing.response.payloads[0]["view"])  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def test_starter_edits_stale_panel_instead_of_repost(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, sessions, _bot, service = await _service(tmp_path, clock=clock)
    guild = FakeGuild()
    try:
        await service.set_channel(1, 20)
        channel = guild.channels[20]
        assert await service.post_starter(guild, channel, actor_id=10)
        assert len(channel.messages) == 1
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(Panel))).one()
            row.state = "stale"
        assert await service.post_starter(guild, channel, actor_id=10)
        assert len(channel.messages) == 1
        assert channel.messages[0].edits
        async with session_scope(sessions) as session:
            row = (await session.scalars(select(Panel))).one()
            assert row.state == "live"
    finally:
        await engine.dispose()


async def test_starter_opens_without_live_panel_row(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, _sessions, bot, service = await _service(tmp_path, clock=clock)
    bot.lfg = service
    guild = FakeGuild()
    joiner = _member(guild, 11)
    try:
        starter = _press(bot, guild, joiner, "c:lf:new:1")
        await LfgNewButton(1).callback(starter)  # type: ignore[arg-type]
        assert starter.response.modals
    finally:
        await engine.dispose()


async def test_sweep_expires_card(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 7, 12, 0, tzinfo=UTC))
    engine, sessions, _bot_obj, service = await _service(tmp_path, clock=clock)
    guild = FakeGuild()
    try:
        await service.set_channel(1, 20)
        await service.set_numbers(1, 15, 15, 30)
        posted = await service.publish(guild, _member(guild, 10), game_role_id=7, need="2", note="")  # type: ignore[arg-type]
        clock.advance(timedelta(minutes=15))
        service._bot.get_guild = lambda _gid: guild
        await service.sweep()
        async with session_scope(sessions) as session:
            row = await session.get(LfgPost, posted.post_id)
            assert row is not None
            assert row.closed_reason == CloseReason.EXPIRED.value
    finally:
        await engine.dispose()
