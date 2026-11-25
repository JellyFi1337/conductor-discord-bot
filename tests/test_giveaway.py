from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import Giveaway, GiveawayEntry, GiveawayWinner, MemberRecord, Reputation
from conductor.db.session import session_scope
from conductor.domain.giveaway import (
    COUNTER_INTERVAL,
    Candidate,
    ConditionKind,
    GiveawayCard,
    GiveawayRules,
    GiveawaySpec,
    GiveawayState,
    JoinVerdict,
    ManageVerdict,
    WinnerPlace,
    can_manage,
    evaluate,
    parse_duration,
    parse_rules,
    parse_spec,
    pick_weighted,
    pick_winners,
    prize_role_allowed,
    ticket_weight,
)
from conductor.presentation import copy
from conductor.presentation.giveaway import (
    GiveawayCancelButton,
    GiveawayEndButton,
    GiveawayJoinButton,
    GiveawayRerollButton,
    finished_view,
    live_view,
)
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.services.giveaway import GiveawayService
from conductor.services.queue import ActionQueue
from conductor.services.role_broker import RoleApplyResult, RoleClaim, RoleSubsystem

STAMP = datetime(2026, 9, 5, 5, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits = 0
        self.views: list[object] = []

    async def edit(self, **kwargs: object) -> FakeMessage:
        self.edits += 1
        self.views.append(kwargs.get("view"))
        return self


class FakeChannel:
    def __init__(self, channel_id: int = 5) -> None:
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []

    async def send(self, **_kwargs: object) -> FakeMessage:
        message = FakeMessage(100 + len(self.sent))
        self.sent.append(message)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id: int) -> FakeMessage | None:
        return self.messages.get(int(message_id))


class FakeMember:
    def __init__(
        self,
        user_id: int,
        *,
        bot: bool = False,
        roles: tuple[int, ...] = (),
        in_voice: bool = False,
        created_at: datetime | None = None,
        joined_at: datetime | None = None,
    ) -> None:
        self.id = user_id
        self.bot = bot
        self.roles = [SimpleNamespace(id=role_id) for role_id in roles]
        self.voice = SimpleNamespace(channel=SimpleNamespace(id=99)) if in_voice else None
        self.created_at = created_at
        self.joined_at = joined_at


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.channels: dict[int, FakeChannel] = {}
        self.members: dict[int, FakeMember] = {}

    def add_channel(self, channel: FakeChannel) -> FakeChannel:
        self.channels[channel.id] = channel
        return channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)


class FakeRoles:
    def __init__(self) -> None:
        self.claims: list[RoleClaim] = []
        self.bound: dict[tuple[int, RoleSubsystem], tuple[int, ...]] = {}

    def bind(self, guild_id: int, subsystem: RoleSubsystem, role_ids: object) -> None:
        self.bound[(guild_id, subsystem)] = tuple(int(role_id) for role_id in role_ids)  # type: ignore[arg-type]

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True, called=True, add=claim.add)


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


def _bot(sessions, guild: FakeGuild, queue: ActionQueue, roles: FakeRoles | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        queue=queue,
        scheduler=None,
        roles=roles or FakeRoles(),
        guilds=(guild,),
        get_guild=lambda gid: guild if gid == guild.id else None,
        sleep=_no_sleep,
        config_by_guild={
            1: SimpleNamespace(
                ruby_role_id=9001,
                emerald_role_id=None,
                sapphire_role_id=None,
                amethyst_role_id=None,
            )
        },
        motion=None,
    )


def _spec(*, minutes: int = 10, winners: int = 1, prize: str = "Nitro") -> GiveawaySpec:
    parsed = parse_spec(prize=prize, winners=winners, duration=str(minutes))
    assert parsed is not None
    return parsed


def test_parse_duration_accepts_tokens_and_plain_minutes() -> None:
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1h") == timedelta(hours=1)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration("90") == timedelta(minutes=90)
    assert parse_duration("0") is None
    assert parse_duration("31d") is None


def test_pick_winners_never_repeats_and_honours_exclude() -> None:
    pool = (1, 2, 3, 4, 5)
    first = pick_winners(pool, 2, rng=random.Random(0))
    assert len(first) == 2
    assert len(set(first)) == 2
    assert set(first) <= set(pool)
    second = pick_winners(pool, 2, exclude=first, rng=random.Random(1))
    assert not set(second) & set(first)
    empty = pick_winners(pool, 2, exclude=pool, rng=random.Random(2))
    assert empty == ()


def test_stranger_cannot_manage() -> None:
    assert can_manage(actor_id=1, host_id=1, has_manage=False) is True
    assert can_manage(actor_id=2, host_id=1, has_manage=True) is True
    assert can_manage(actor_id=9, host_id=1, has_manage=False) is False


def test_old_roles_are_never_prize_roles() -> None:
    assert prize_role_allowed(10, whitelist={10}, old_ids={10}) is False
    assert prize_role_allowed(10, whitelist={10}, old_ids=()) is True
    assert prize_role_allowed(11, whitelist={10}, old_ids=()) is False
    assert prize_role_allowed(None, whitelist={10}, old_ids={10}) is True


def test_live_and_finished_cards_keep_persistent_ids() -> None:
    card = GiveawayCard(
        giveaway_id=8,
        prize="Nitro на месяц",
        host_id=1,
        winners=2,
        ends_at=STAMP + timedelta(hours=1),
        entries=84,
    )
    live = live_view(card)
    text = collected_text(live)
    assert "Nitro на месяц" in text
    assert "Участвую · 84" in text
    ids = [getattr(item, "custom_id", "") for item in live.walk_children()]
    assert "c:gw:join:8" in ids
    assert "c:gw:end:8" in ids
    assert "c:gw:cancel:8" in ids
    assert parse_custom_id("c:gw:join:8") is not None
    finished = finished_view(
        GiveawayCard(
            giveaway_id=8,
            prize="Nitro на месяц",
            host_id=1,
            winners=2,
            ends_at=STAMP,
            entries=84,
            state=GiveawayState.ENDED,
            winner_ids=(2, 3),
        )
    )
    finished_ids = [getattr(item, "custom_id", "") for item in finished.walk_children()]
    assert "c:gw:reroll:8" in finished_ids
    assert GiveawayJoinButton in dynamic_items()
    assert GiveawayEndButton in dynamic_items()
    assert GiveawayCancelButton in dynamic_items()
    assert GiveawayRerollButton in dynamic_items()


async def test_stranger_cancel_is_denied(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert giveaway_id is not None
        result = await service.cancel(guild, giveaway_id, actor_id=99, has_manage=False)
        assert result is ManageVerdict.DENIED
        async with session_scope(sessions) as session:
            row = await session.get(Giveaway, giveaway_id)
            assert row is not None
            assert row.cancelled_at is None
            assert row.ended_at is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_reroll_takes_from_remaining_pool(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    service._rng = random.Random(0)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(winners=1), now=STAMP)
        assert giveaway_id is not None
        for user_id in (11, 12, 13):
            assert (await service.join(guild, giveaway_id, FakeMember(user_id))).verdict is JoinVerdict.JOINED
        ended = await service.conclude(giveaway_id, actor_id=1, now=STAMP + timedelta(minutes=10))
        assert ended is ManageVerdict.OK
        async with session_scope(sessions) as session:
            first = set(
                (
                    await session.scalars(
                        select(GiveawayWinner.user_id).where(GiveawayWinner.giveaway_id == giveaway_id)
                    )
                ).all()
            )
        assert first
        later = STAMP + timedelta(minutes=11)
        again = await service.reroll(guild, giveaway_id, actor_id=1, has_manage=False, now=later)
        assert again is ManageVerdict.OK
        async with session_scope(sessions) as session:
            found = select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id)
            rows = (await session.scalars(found)).all()
            by_round = {}
            for row in rows:
                by_round.setdefault(row.round, set()).add(row.user_id)
        assert 1 in by_round and 2 in by_round
        assert by_round[1].isdisjoint(by_round[2])
        twice = await service.reroll(guild, giveaway_id, actor_id=99, has_manage=False)
        assert twice is ManageVerdict.DENIED
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_timer_restores_from_database(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    dead = ActionQueue(spawn, clock=clock)
    bot = _bot(sessions, guild, dead)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(minutes=10), now=STAMP)
        assert giveaway_id is not None
        await service.join(guild, giveaway_id, FakeMember(11))
        clock2 = FakeClock()
        live = ActionQueue(spawn, clock=clock2)
        bot.queue = live
        restored = GiveawayService(bot)
        count = await restored.restore(now=STAMP + timedelta(minutes=4))
        assert count == 1
        assert live.later_count == 1
        again = await restored.restore(now=STAMP + timedelta(minutes=4))
        assert again == 1
        assert live.later_count == 1
        clock2.advance(5 * 60)
        assert await live.fire_due() == 0
        clock2.advance(60)
        assert await live.fire_due() == 1
        await _await_spawned(spawned)
        async with session_scope(sessions) as session:
            row = await session.get(Giveaway, giveaway_id)
            assert row is not None
            assert row.ended_at is not None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_counter_does_not_edit_faster_than_budget(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    clock = FakeClock()
    queue = ActionQueue(spawn, clock=clock)
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert giveaway_id is not None
        message = channel.sent[0]
        for user_id in (11, 12, 13, 14):
            assert (await service.join(guild, giveaway_id, FakeMember(user_id))).verdict is JoinVerdict.JOINED
        clock.advance(COUNTER_INTERVAL - 0.1)
        assert await queue.fire_due() == 0
        await _await_spawned(spawned)
        assert message.edits == 0
        clock.advance(0.1)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        assert message.edits == 1
        assert (await service.join(guild, giveaway_id, FakeMember(15))).verdict is JoinVerdict.JOINED
        clock.advance(COUNTER_INTERVAL - 0.1)
        assert await queue.fire_due() == 0
        await _await_spawned(spawned)
        assert message.edits == 1
        clock.advance(0.1)
        assert await queue.fire_due() == 1
        await _await_spawned(spawned)
        assert message.edits == 2
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_prize_role_goes_through_broker_and_skips_old(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    roles = FakeRoles()
    bot = _bot(sessions, guild, queue, roles)
    service = GiveawayService(bot)
    try:
        assert await service.allow_role(1, 9001) is False
        assert await service.allow_role(1, 77) is True
        giveaway_id = await service.start(
            guild,
            channel,
            actor_id=1,
            spec=_spec(winners=1),
            prize_role_id=77,
            now=STAMP,
        )
        assert giveaway_id is not None
        await service.join(guild, giveaway_id, FakeMember(11))
        await service.conclude(giveaway_id, actor_id=1, now=STAMP + timedelta(minutes=10))
        await _await_spawned(spawned)
        assert roles.claims
        claim = roles.claims[0]
        assert claim.subsystem is RoleSubsystem.GIVEAWAY
        assert claim.add == (77,)
        assert claim.urgent is True
        blocked = await service.start(
            guild,
            channel,
            actor_id=1,
            spec=_spec(),
            prize_role_id=9001,
            now=STAMP,
        )
        assert blocked is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


def test_parse_rules_reads_legacy_not_sinner_as_min_tier() -> None:
    rules = parse_rules('{"not_sinner": true}')
    assert rules.min_tier is True
    dumped = parse_rules('{"min_tier": true}')
    assert dumped.min_tier is True


def test_evaluate_reports_every_failing_condition() -> None:
    rules = GiveawayRules(
        require_role_id=5,
        min_messages=10,
        min_account_days=30,
        min_tenure_days=14,
        min_tier=True,
        in_voice=True,
    )
    gaps = evaluate(Candidate(user_id=1, sinner=True), rules, now=STAMP)
    assert {gap.kind for gap in gaps} == {
        ConditionKind.ROLE,
        ConditionKind.MESSAGES,
        ConditionKind.ACCOUNT,
        ConditionKind.TENURE,
        ConditionKind.MIN_TIER,
        ConditionKind.VOICE,
    }
    ok = Candidate(
        user_id=1,
        role_ids=frozenset({5}),
        messages=10,
        account_created_at=STAMP - timedelta(days=40),
        joined_at=STAMP - timedelta(days=20),
        in_voice=True,
    )
    assert evaluate(ok, rules, now=STAMP) == ()


def test_ticket_weight_is_off_by_default_and_caps_when_on() -> None:
    busy = Candidate(user_id=1, messages=100, voice_seconds=3_600)
    assert ticket_weight(busy, GiveawayRules()) == 1
    assert ticket_weight(busy, GiveawayRules(weight=True)) == 1 + 100 // 50 + (3_600 // 60) // 30
    assert ticket_weight(Candidate(user_id=1, messages=50_000, voice_seconds=10_800), GiveawayRules(weight=True)) == 10
    assert ticket_weight(busy, GiveawayRules(weight=True, max_tickets=3)) == 3


def test_pick_weighted_never_repeats_and_honours_exclude() -> None:
    pool = ((1, 50), (2, 50), (3, 50), (1, 9))
    first = pick_weighted(pool, 2, rng=random.Random(0))
    assert len(first) == 2
    assert len(set(first)) == 2
    assert set(first) <= {1, 2, 3}
    second = pick_weighted(pool, 2, exclude=first, rng=random.Random(1))
    assert not set(second) & set(first)
    empty = pick_weighted(pool, 2, exclude={1, 2, 3}, rng=random.Random(2))
    assert empty == ()


def test_card_shows_terms_and_formula_before_join() -> None:
    plain = GiveawayCard(
        giveaway_id=8,
        prize="Nitro",
        host_id=1,
        winners=1,
        ends_at=STAMP + timedelta(hours=1),
        entries=0,
        rules=GiveawayRules(min_tier=True, min_messages=20),
    )
    text = collected_text(live_view(plain))
    assert copy.GIVEAWAY_TERMS in text
    assert "не изгой" in text
    assert "от 20 сообщений" in text
    assert "⌊сообщений" not in text
    weighted = GiveawayCard(
        giveaway_id=8,
        prize="Nitro",
        host_id=1,
        winners=1,
        ends_at=STAMP + timedelta(hours=1),
        entries=0,
        rules=GiveawayRules(weight=True, consolation_count=2, consolation_text="роль без приза"),
    )
    heavy = collected_text(live_view(weighted))
    assert copy.GIVEAWAY_WEIGHT in heavy
    assert "1 + ⌊сообщений / 50⌋ + ⌊минут войса / 30⌋, не больше 10" in heavy
    assert copy.GIVEAWAY_CONSOLATION in heavy
    assert "роль без приза" in heavy


async def _seed_member(
    sessions,
    user_id: int,
    *,
    messages: int = 0,
    voice_seconds: int = 0,
    created_at: datetime | None = None,
    joined_at: datetime | None = None,
    score: int | None = None,
) -> None:
    joined = joined_at or STAMP - timedelta(days=400)
    created = created_at or STAMP - timedelta(days=400)
    async with session_scope(sessions) as session:
        session.add(
            MemberRecord(
                guild_id=1,
                user_id=user_id,
                username="u",
                display_name="u",
                first_joined_at=joined,
                last_joined_at=joined,
                account_created_at=created,
                message_count=messages,
                voice_seconds=voice_seconds,
            )
        )
        if score is not None:
            session.add(Reputation(guild_id=1, user_id=user_id, score=score))


async def test_unfit_join_names_the_gap_and_writes_no_entry(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(
            guild,
            channel,
            actor_id=1,
            spec=_spec(),
            rules=GiveawayRules(min_messages=10, in_voice=True, require_role_id=5),
            now=STAMP,
        )
        assert giveaway_id is not None
        card = await service.open_in_channel(guild.id, channel.id)
        assert card is not None
        preview = collected_text(live_view(card))
        assert copy.GIVEAWAY_TERMS in preview
        assert "от 10 сообщений" in preview
        assert "сейчас в голосовом" in preview
        await _seed_member(sessions, 11, messages=2)
        member = FakeMember(11)
        result = await service.join(guild, giveaway_id, member)
        assert result.verdict is JoinVerdict.UNFIT
        kinds = {gap.kind for gap in result.gaps}
        assert ConditionKind.MESSAGES in kinds
        assert ConditionKind.VOICE in kinds
        assert ConditionKind.ROLE in kinds
        explained = copy.giveaway_unfit(result.gaps)
        assert "сообщений 2 из 10" in explained
        assert "голосовом" in explained
        async with session_scope(sessions) as session:
            found = await session.scalar(
                select(GiveawayEntry.id).where(GiveawayEntry.giveaway_id == giveaway_id, GiveawayEntry.user_id == 11)
            )
            assert found is None
        ready = FakeMember(11, roles=(5,), in_voice=True)
        await _seed_member(sessions, 12, messages=10)
        assert (
            await service.join(guild, giveaway_id, FakeMember(12, roles=(5,), in_voice=True))
        ).verdict is JoinVerdict.JOINED
        assert (await service.join(guild, giveaway_id, ready)).verdict is JoinVerdict.UNFIT
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_draw_rechecks_and_drops_who_became_sinner(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(
            guild,
            channel,
            actor_id=1,
            spec=_spec(winners=1),
            rules=GiveawayRules(min_tier=True),
            now=STAMP,
        )
        assert giveaway_id is not None
        for user_id in (11, 12):
            assert (await service.join(guild, giveaway_id, FakeMember(user_id))).verdict is JoinVerdict.JOINED
        async with session_scope(sessions) as session:
            session.add(Reputation(guild_id=1, user_id=11, score=0))
        ended = await service.conclude(giveaway_id, actor_id=1, now=STAMP + timedelta(minutes=10))
        assert ended is ManageVerdict.OK
        async with session_scope(sessions) as session:
            rows = (
                await session.scalars(select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id))
            ).all()
            winners = {row.user_id for row in rows if row.place == WinnerPlace.MAIN}
        assert 11 not in winners
        assert winners == {12}
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_consolation_gets_extra_role_not_the_prize(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    roles = FakeRoles()
    bot = _bot(sessions, guild, queue, roles)
    service = GiveawayService(bot)
    try:
        assert await service.allow_role(1, 77) is True
        assert await service.allow_role(1, 88) is True
        giveaway_id = await service.start(
            guild,
            channel,
            actor_id=1,
            spec=_spec(winners=1),
            prize_role_id=77,
            rules=GiveawayRules(
                consolation_role_id=88,
                consolation_text="без приза",
                consolation_count=1,
            ),
            now=STAMP,
        )
        assert giveaway_id is not None
        for user_id in (11, 12, 13):
            assert (await service.join(guild, giveaway_id, FakeMember(user_id))).verdict is JoinVerdict.JOINED
        ended = await service.conclude(giveaway_id, actor_id=1, now=STAMP + timedelta(minutes=10))
        assert ended is ManageVerdict.OK
        await _await_spawned(spawned)
        async with session_scope(sessions) as session:
            rows = (
                await session.scalars(select(GiveawayWinner).where(GiveawayWinner.giveaway_id == giveaway_id))
            ).all()
            mains = {row.user_id for row in rows if row.place == WinnerPlace.MAIN}
            extras = {row.user_id for row in rows if row.place == WinnerPlace.EXTRA}
        assert len(mains) == 1
        assert len(extras) == 1
        assert mains.isdisjoint(extras)
        prize_claims = [claim for claim in roles.claims if claim.add == (77,)]
        extra_claims = [claim for claim in roles.claims if claim.add == (88,)]
        assert {claim.user_id for claim in prize_claims} == mains
        assert {claim.user_id for claim in extra_claims} == extras
        assert all(claim.subsystem is RoleSubsystem.GIVEAWAY and claim.urgent is True for claim in extra_claims)
        assert all(claim.reason == "утешение розыгрыша" for claim in extra_claims)
        card = await service.ended_in_channel(guild.id, channel.id)
        assert card is not None
        done = collected_text(finished_view(card))
        assert copy.GIVEAWAY_CONSOLATION in done
        assert "без приза" in done
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_alt_protection_needs_account_age_and_tenure(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    rules = GiveawayRules(min_account_days=30, min_tenure_days=14)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(), rules=rules, now=STAMP)
        assert giveaway_id is not None
        young = FakeMember(
            11,
            created_at=STAMP - timedelta(days=5),
            joined_at=STAMP - timedelta(days=1),
        )
        guild.members[11] = young
        young_result = await service.join(guild, giveaway_id, young, now=STAMP)
        assert young_result.verdict is JoinVerdict.UNFIT
        assert {gap.kind for gap in young_result.gaps} == {ConditionKind.ACCOUNT, ConditionKind.TENURE}
        new_here = FakeMember(
            12,
            created_at=STAMP - timedelta(days=400),
            joined_at=STAMP - timedelta(days=2),
        )
        guild.members[12] = new_here
        tenure = await service.join(guild, giveaway_id, new_here, now=STAMP)
        assert tenure.verdict is JoinVerdict.UNFIT
        assert {gap.kind for gap in tenure.gaps} == {ConditionKind.TENURE}
        settled = FakeMember(
            13,
            created_at=STAMP - timedelta(days=400),
            joined_at=STAMP - timedelta(days=20),
        )
        guild.members[13] = settled
        assert (await service.join(guild, giveaway_id, settled, now=STAMP)).verdict is JoinVerdict.JOINED
    finally:
        await _await_spawned(spawned)
        await engine.dispose()


async def test_weight_stays_off_unless_started_on_and_formula_is_frozen(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = FakeGuild()
    channel = guild.add_channel(FakeChannel())
    spawned, spawn = _spawn_box()
    queue = ActionQueue(spawn, clock=FakeClock())
    bot = _bot(sessions, guild, queue)
    service = GiveawayService(bot)
    try:
        giveaway_id = await service.start(guild, channel, actor_id=1, spec=_spec(), now=STAMP)
        assert giveaway_id is not None
        card = await service.open_in_channel(guild.id, channel.id)
        assert card is not None
        assert card.rules.weight is False
        assert "⌊сообщений" not in collected_text(live_view(card))
        assert await service.set_weight_formula(1, messages=20, voice_minutes=10, cap=5) == (20, 10, 5)
        still = await service.open_in_channel(guild.id, channel.id)
        assert still is not None
        assert still.rules.weight is False
        assert still.rules.messages_per_ticket == 50
        other = guild.add_channel(FakeChannel(channel_id=6))
        weighted_id = await service.start(
            guild,
            other,
            actor_id=1,
            spec=_spec(),
            rules=GiveawayRules(weight=True, messages_per_ticket=20, voice_minutes_per_ticket=10, max_tickets=5),
            now=STAMP,
        )
        assert weighted_id is not None
        heavy = await service.open_in_channel(guild.id, other.id)
        assert heavy is not None
        assert heavy.rules.weight is True
        preview = collected_text(live_view(heavy))
        assert "1 + ⌊сообщений / 20⌋ + ⌊минут войса / 10⌋, не больше 5" in preview
        assert await service.set_weight_formula(1, cap=11) is None
    finally:
        await _await_spawned(spawned)
        await engine.dispose()
