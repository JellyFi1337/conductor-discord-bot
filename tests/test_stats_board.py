from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from tests.test_pulse import STAMP, _inputs

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ChannelDaily, MemberChannel, VoiceSession
from conductor.db.session import session_scope
from conductor.domain.activity import EventKind
from conductor.domain.pulse import PulseSnapshot, health_score
from conductor.domain.stats_board import (
    ROLE_CAP,
    BoardInputs,
    ChannelCount,
    RankCount,
    RoleCount,
    card_fingerprint,
    dump_role_ids,
    parse_role_ids,
    voice_hours,
)
from conductor.presentation import copy
from conductor.presentation.layout import COMPONENT_LIMIT, TEXT_LIMIT, collected_text
from conductor.presentation.stats_board import board_card
from conductor.services.cache import CacheHub
from conductor.services.stats import top_channels, voice_seconds
from conductor.services.stats_board import StatsBoardService

GUILD_ID = 1
DOMAIN_PY = Path(__file__).resolve().parents[1] / "src" / "conductor" / "domain" / "stats_board.py"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _board(**overrides: object) -> BoardInputs:
    values: dict[str, object] = {
        "ranks": (RankCount("watcher", 3), RankCount("senior", 1)),
        "duty": 2,
        "humans": 100,
        "bots": 2,
        "boosters": 4,
        "roles": (RoleCount(11, 7),),
        "channels": (ChannelCount(21, 40, 5), ChannelCount(22, 10, 2)),
        "voice_seconds": 7200,
        "tickets_open": 1,
        "gathering_title": "клановый вечер",
        "gathering_at": STAMP + timedelta(hours=3),
        "gathering_id": 4,
    }
    values.update(overrides)
    return BoardInputs(**values)  # type: ignore[arg-type]


def test_domain_has_no_discord_or_sqlalchemy() -> None:
    source = DOMAIN_PY.read_text(encoding="utf-8")
    assert "discord" not in source
    assert "sqlalchemy" not in source


def test_card_fingerprint_stable_and_changes() -> None:
    first = card_fingerprint(_board())
    assert first == card_fingerprint(_board())
    assert first != card_fingerprint(_board(humans=101))
    assert first != card_fingerprint(_board(duty=3))
    assert first != card_fingerprint(_board(channels=(ChannelCount(21, 41, 5),)))
    later = card_fingerprint(_board())
    assert first == later


def test_parse_role_ids_caps_and_dedupes() -> None:
    assert parse_role_ids("") == ()
    assert parse_role_ids("[11, 12]") == (11, 12)
    assert parse_role_ids("11 12 11 13") == (11, 12, 13)
    assert dump_role_ids((11, 12)) == "[11, 12]"
    ids = parse_role_ids(list(range(1, 21)))
    assert len(ids) == ROLE_CAP
    assert ids[0] == 1
    assert ids[-1] == 15


def test_board_card_fits_limits_with_full_lists() -> None:
    roles = tuple(RoleCount(1000 + index, index + 1) for index in range(ROLE_CAP))
    channels = tuple(ChannelCount(2000 + index, 100 - index, 10) for index in range(10))
    view = board_card(_board(roles=roles, channels=channels), updated_at=STAMP)
    text = collected_text(view)
    assert len(text) <= TEXT_LIMIT
    assert view.total_children_count <= COMPONENT_LIMIT
    assert copy.STATS_BOARD_TITLE in text
    assert copy.stats_board_people(humans=100, bots=2, boosts=4) in text
    assert copy.pulse_duty(2) in text
    assert copy.stats_board_voice(voice_hours(7200)) in text
    assert copy.STATS_BOARD_FOOTER in text
    assert "<@&1000>" in text
    assert "<#2000>" in text


async def test_top_channels_and_voice_on_fixture(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        async with session_scope(sessions) as session:
            session.add(
                ChannelDaily(
                    guild_id=GUILD_ID,
                    channel_id=21,
                    day=day,
                    event_kind=EventKind.MESSAGE,
                    count=40,
                )
            )
            session.add(
                ChannelDaily(
                    guild_id=GUILD_ID,
                    channel_id=22,
                    day=day,
                    event_kind=EventKind.MESSAGE,
                    count=10,
                )
            )
            session.add(
                ChannelDaily(
                    guild_id=GUILD_ID,
                    channel_id=21,
                    day=day - timedelta(days=20),
                    event_kind=EventKind.MESSAGE,
                    count=99,
                )
            )
            session.add(
                MemberChannel(
                    guild_id=GUILD_ID,
                    user_id=5,
                    channel_id=21,
                    messages=4,
                    last_at=now - timedelta(days=1),
                )
            )
            session.add(
                MemberChannel(
                    guild_id=GUILD_ID,
                    user_id=6,
                    channel_id=21,
                    messages=2,
                    last_at=now - timedelta(days=1),
                )
            )
            session.add(
                MemberChannel(
                    guild_id=GUILD_ID,
                    user_id=7,
                    channel_id=21,
                    messages=1,
                    last_at=now - timedelta(days=20),
                )
            )
            session.add(
                VoiceSession(
                    guild_id=GUILD_ID,
                    user_id=5,
                    channel_id=30,
                    joined_at=now - timedelta(hours=3),
                    left_at=now - timedelta(hours=1),
                    duration_seconds=7200,
                    last_event_at=now - timedelta(hours=1),
                )
            )
        async with session_scope(sessions) as session:
            ranks = await top_channels(session, GUILD_ID, days=7, now=now)
            seconds = await voice_seconds(session, GUILD_ID, days=7, now=now)
        assert ranks[0] == (21, 40, 2)
        assert ranks[1] == (22, 10, 0)
        assert seconds == 7200
    finally:
        await engine.dispose()


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Message:
    def __init__(self) -> None:
        self.id = 99
        self.edits = 0

    async def edit(self, **_kwargs: object) -> None:
        self.edits += 1


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.message = _Message()
        self.sent = 0

    async def fetch_message(self, _message_id: int) -> _Message:
        return self.message

    async def send(self, **_kwargs: object) -> _Message:
        self.sent += 1
        return self.message


class _Pulse:
    def __init__(self, snap: PulseSnapshot) -> None:
        self.snap = snap

    async def snapshot(self, _guild: object, *, now: datetime | None = None) -> PulseSnapshot:
        _ = now
        return self.snap


def _snap() -> PulseSnapshot:
    inputs = _inputs()
    return PulseSnapshot(inputs=inputs, report=health_score(inputs, STAMP), assembled_at=STAMP)


async def test_refresh_skips_edit_when_hash_unchanged(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    clock = _Clock()
    channel = _Channel(40)
    guild = SimpleNamespace(
        id=GUILD_ID,
        members=(),
        get_channel=lambda _channel_id: channel,
        get_role=lambda _role_id: None,
    )
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        scheduler=None,
        serves=lambda _gid: True,
        outbound=None,
        get_guild=lambda _gid: guild,
        guilds=(guild,),
        roles=SimpleNamespace(bind=lambda *_a, **_k: None),
    )
    bot.pulse = _Pulse(_snap())
    service = StatsBoardService(bot, clock=clock)
    try:
        await service.set_channel(GUILD_ID, 40)
        posted = await service.post(guild, channel, actor_id=2, now=STAMP)
        assert posted is True
        assert channel.sent == 1
        first_edits = channel.message.edits
        clock.now += 30
        again = await service.refresh(guild, now=STAMP)
        assert again is False
        assert channel.message.edits == first_edits
        bot.pulse = _Pulse(
            PulseSnapshot(
                inputs=_inputs(humans=120),
                report=health_score(_inputs(humans=120), STAMP),
                assembled_at=STAMP,
            )
        )
        service._last_edit[GUILD_ID] = clock.now
        blocked = await service.refresh(guild, now=STAMP)
        assert blocked is False
        clock.now += 3
        changed = await service.refresh(guild, now=STAMP)
        assert changed is True
        assert channel.message.edits == first_edits + 1
    finally:
        await engine.dispose()
