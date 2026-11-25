from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conductor.domain.observe import (
    ALERT_INTERVAL,
    STRUCTURE_CARD_CAP,
    AlertKind,
    broker_trips,
    cap_cards,
    fingerprint_changed,
    next_streak,
    should_emit,
    structure_fingerprint,
)
from conductor.domain.permissions import REQUIRED_PERMISSIONS
from conductor.domain.pulse import PulseAlertRules, PulseInputs, PulseSnapshot, health_score
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.services.observe import TICK_PERIOD, ObserveService


class FakePump:
    def __init__(self) -> None:
        self.items: list[tuple[int | None, dict[str, object]]] = []
        self.dropped = 0
        self.on_drop = None

    def submit(self, channel_id: int | None, **kwargs: object) -> None:
        self.items.append((channel_id, kwargs))


class _Conn:
    def __init__(self, *, boom: bool = False) -> None:
        self._boom = boom

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, _stmt: object) -> None:
        if self._boom:
            raise RuntimeError("db down")


class _Engine:
    def __init__(self, *, boom: bool = False) -> None:
        self._boom = boom

    def connect(self) -> _Conn:
        return _Conn(boom=self._boom)


def _perms(*, missing: str | None = None) -> SimpleNamespace:
    flags = {item.attr: True for item in REQUIRED_PERMISSIONS}
    if missing:
        flags[missing] = False
    return SimpleNamespace(**flags)


def _config(*, admin: int | None = 99, ruby: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        admin_log_channel_id=admin,
        user_log_channel_id=None,
        ruby_role_id=ruby,
        emerald_role_id=None,
        sapphire_role_id=None,
        amethyst_role_id=None,
        census_at=None,
    )


def _guild(
    guild_id: int = 1,
    *,
    missing: str | None = None,
    top: int = 10,
    roles: dict[int, SimpleNamespace] | None = None,
) -> SimpleNamespace:
    found = roles or {}
    return SimpleNamespace(
        id=guild_id,
        me=SimpleNamespace(guild_permissions=_perms(missing=missing), top_role=SimpleNamespace(position=top)),
        member_count=1,
        members=[object()],
        text_channels=[],
        voice_channels=[],
        stage_channels=[],
        threads=[],
        get_channel=lambda _channel_id: None,
        get_role=lambda role_id: found.get(role_id),
    )


def _bot(
    *,
    guilds: tuple[object, ...] = (),
    configs: dict[int, object] | None = None,
    pump: FakePump | None = None,
    engine: object | None = None,
    dropped: int = 0,
) -> SimpleNamespace:
    pump = pump or FakePump()
    pump.dropped = dropped
    return SimpleNamespace(
        log_pump=pump,
        guilds=guilds,
        config_by_guild=configs or {},
        scheduler=None,
        engine=_Engine() if engine is None else engine,
        intents=SimpleNamespace(members=True, message_content=True),
        latency=0.05,
        history=SimpleNamespace(is_caught_up=lambda *_args: True),
        fence=SimpleNamespace(mode=SimpleNamespace(value="live")),
        serves=lambda _gid: True,
        caches=SimpleNamespace(hits=0, misses=0, size=0),
        messages=[],
        outbound=SimpleNamespace(depth=0, limit=8, waiting=0),
        interactions=SimpleNamespace(trips=0),
        queue=SimpleNamespace(job_count=0, later_count=0),
        motion=SimpleNamespace(loaded_count=0, total_count=8),
        _bg_tasks=set(),
    )


def test_should_emit_once_per_hour() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert should_emit(None, now=now) is True
    assert should_emit(now, now=now + timedelta(minutes=59)) is False
    assert should_emit(now, now=now + ALERT_INTERVAL) is True


def test_broker_trips_on_third_reject() -> None:
    streak = 0
    streak = next_streak(streak, ok=False)
    assert broker_trips(streak) is False
    streak = next_streak(streak, ok=False)
    assert broker_trips(streak) is False
    streak = next_streak(streak, ok=False)
    assert broker_trips(streak) is True
    assert next_streak(streak, ok=True) == 0


def test_structure_fingerprint_ignores_order_and_changes_on_new_problem() -> None:
    first = structure_fingerprint((("hub", "gone"), ("rules", "rights")))
    second = structure_fingerprint((("rules", "rights"), ("hub", "gone")))
    third = structure_fingerprint((("hub", "gone"), ("rules", "missing")))
    assert first == second
    assert fingerprint_changed(None, first) is True
    assert fingerprint_changed(first, second) is False
    assert fingerprint_changed(first, third) is True


def test_cap_cards_keeps_limit_and_counts_rest() -> None:
    items = tuple(range(7))
    shown, rest = cap_cards(items)
    assert shown == (0, 1, 2)
    assert rest == 4
    assert STRUCTURE_CARD_CAP == 3
    empty, none_left = cap_cards(())
    assert empty == ()
    assert none_left == 0


def test_observe_registers_periodic_tick() -> None:
    jobs: list[tuple[float, str]] = []

    def every(period: float, name: str, _fn: object) -> None:
        jobs.append((period, name))

    ObserveService(SimpleNamespace(log_pump=None, scheduler=SimpleNamespace(every=every)))
    assert jobs == [(TICK_PERIOD, "observe")]


def test_alert_skips_guild_without_staff_channel() -> None:
    pump = FakePump()
    svc = ObserveService(_bot(pump=pump, configs={1: _config(admin=None)}))
    assert svc.report(1, AlertKind.QUEUE_FULL, copy.ALERT_QUEUE) is False
    assert pump.items == []


def test_same_alert_is_deduped_for_an_hour() -> None:
    pump = FakePump()
    svc = ObserveService(_bot(pump=pump, configs={1: _config()}))
    t0 = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert svc.report(1, AlertKind.QUEUE_FULL, copy.ALERT_QUEUE, now=t0) is True
    assert svc.report(1, AlertKind.QUEUE_FULL, copy.ALERT_QUEUE, now=t0 + timedelta(minutes=59)) is False
    assert svc.report(1, AlertKind.QUEUE_FULL, copy.ALERT_QUEUE, now=t0 + timedelta(hours=1)) is True
    assert len(pump.items) == 2
    assert pump.items[0][0] == 99


async def test_queue_full_drop_fans_out_to_staff_channel() -> None:
    pump = FakePump()
    guild = _guild()
    bot = _bot(guilds=(guild,), configs={1: _config()}, pump=pump)
    ObserveService(bot)
    assert callable(pump.on_drop)
    pump.on_drop()
    assert len(pump.items) == 1
    assert pump.items[0][0] == 99


async def test_tick_alerts_database_down_once() -> None:
    pump = FakePump()
    guild = _guild()
    svc = ObserveService(_bot(guilds=(guild,), configs={1: _config()}, pump=pump, engine=_Engine(boom=True)))
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 1
    assert await svc.tick_all(now=now + timedelta(minutes=1)) == 0
    assert len(pump.items) == 1


async def test_tick_alerts_when_bot_role_is_below_managed() -> None:
    pump = FakePump()
    guild = _guild(top=2, roles={100: SimpleNamespace(position=5)})
    svc = ObserveService(_bot(guilds=(guild,), configs={1: _config(ruby=100)}, pump=pump))
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 1
    assert await svc.tick_all(now=now + timedelta(minutes=10)) == 0


async def test_tick_alerts_lost_permission() -> None:
    pump = FakePump()
    guild = _guild(missing="manage_roles")
    svc = ObserveService(_bot(guilds=(guild,), configs={1: _config()}, pump=pump))
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 1
    assert await svc.tick_all(now=now + timedelta(minutes=10)) == 0


def test_broker_alert_fires_on_third_http_reject() -> None:
    pump = FakePump()
    svc = ObserveService(_bot(pump=pump, configs={1: _config()}))
    t0 = datetime(2026, 9, 5, 12, tzinfo=UTC)
    svc.note_broker(1, ok=False, now=t0)
    svc.note_broker(1, ok=False, now=t0)
    assert pump.items == []
    svc.note_broker(1, ok=False, now=t0)
    assert len(pump.items) == 1
    svc.note_broker(1, ok=False, now=t0)
    assert len(pump.items) == 1
    svc.note_broker(1, ok=True, now=t0)
    svc.note_broker(1, ok=False, now=t0)
    svc.note_broker(1, ok=False, now=t0)
    assert len(pump.items) == 1


async def test_tick_queue_full_from_dropped_counter() -> None:
    pump = FakePump()
    guild = _guild()
    svc = ObserveService(_bot(guilds=(guild,), configs={1: _config()}, pump=pump, dropped=4))
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 1
    assert await svc.tick_all(now=now + timedelta(seconds=30)) == 0


def _pulse_inputs(**overrides: object) -> PulseInputs:
    values: dict[str, object] = {
        "humans": 10,
        "bots": 0,
        "boosters": 3,
        "premium_tier": 1,
        "voice_now": 0,
        "online": None,
        "unique_authors_24h": 2,
        "messages_24h": 10,
        "messages_7d_avg": 20.0,
        "voice_minutes_24h": 0,
        "joins_7d": 1,
        "leaves_7d": 1,
        "tickets_open": 0,
        "duty_watchers": 0,
        "next_gathering_at": None,
        "next_gathering_title": None,
        "next_gathering_id": None,
        "has_presence_intent": False,
        "census_age_hours": 1.0,
        "has_message_history": True,
        "has_voice_sessions": True,
        "joins_24h": 0,
        "leaves_24h": 8,
        "authors_7d": (10, 10, 10, 10, 10, 10, 10),
        "peak_hour": 12,
        "peak_voice_7d": (6, 6, 6, 6, 6, 6, 6),
        "boosters_24h_ago": 5,
    }
    values.update(overrides)
    return PulseInputs(**values)  # type: ignore[arg-type]


class FakePulse:
    def __init__(self, inputs: PulseInputs, rules: PulseAlertRules | None = None) -> None:
        self.inputs = inputs
        self.rules = rules or PulseAlertRules()
        self.calls = 0

    async def snapshot(self, guild: object, *, now: datetime) -> PulseSnapshot:
        _ = guild
        self.calls += 1
        return PulseSnapshot(inputs=self.inputs, report=health_score(self.inputs, now), assembled_at=now)

    async def alert_rules(self, guild_id: int) -> PulseAlertRules:
        _ = guild_id
        return self.rules

    def last_alert_rules(self, guild_id: int) -> PulseAlertRules:
        _ = guild_id
        return self.rules


async def test_pulse_signals_use_collector_and_dedup() -> None:
    pump = FakePump()
    pulse = FakePulse(_pulse_inputs())
    bot = _bot(guilds=(_guild(),), configs={1: _config()}, pump=pump)
    bot.pulse = pulse
    svc = ObserveService(bot)
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    posted = await svc.tick_all(now=now)
    assert posted >= 1
    assert pulse.calls == 1
    texts = [collected_text(kwargs["view"]) for _channel, kwargs in pump.items]
    assert any(copy.SIGNAL_TITLE in text for text in texts)
    assert all(copy.ALERT_TITLE not in text for text in texts)
    assert any(copy.SIGNAL_LOOK in text for text in texts)
    assert all("@" not in text for text in texts)
    first = len(pump.items)
    assert await svc.tick_all(now=now + timedelta(minutes=10)) == 0
    assert len(pump.items) == first
    assert svc.last_pulse_alert_at(1) == now


async def test_disabled_pulse_signal_does_not_emit() -> None:
    pump = FakePump()
    pulse = FakePulse(_pulse_inputs(), rules=PulseAlertRules(enabled=False))
    bot = _bot(guilds=(_guild(),), configs={1: _config()}, pump=pump)
    bot.pulse = pulse
    svc = ObserveService(bot)
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 0
    assert pump.items == []
    assert pulse.calls == 0


async def test_pulse_signals_emit_at_any_hour() -> None:
    pump = FakePump()
    pulse = FakePulse(_pulse_inputs())
    bot = _bot(guilds=(_guild(),), configs={1: _config()}, pump=pump)
    bot.pulse = pulse
    svc = ObserveService(bot)
    night = datetime(2026, 9, 5, 3, tzinfo=UTC)
    assert await svc.tick_all(now=night) >= 1
    assert pulse.calls == 1
    assert svc.last_pulse_alert_at(1) == night
    texts = [collected_text(kwargs["view"]) for _channel, kwargs in pump.items]
    assert any(copy.SIGNAL_TITLE in text for text in texts)


async def test_empty_pulse_snapshot_is_silent() -> None:
    pump = FakePump()
    empty = _pulse_inputs(
        voice_now=0,
        peak_hour=None,
        peak_voice_7d=(),
        joins_24h=0,
        leaves_24h=0,
        unique_authors_24h=0,
        authors_7d=(0, 0, 0, 0, 0, 0, 0),
        boosters=0,
        boosters_24h_ago=None,
    )
    pulse = FakePulse(empty)
    bot = _bot(guilds=(_guild(),), configs={1: _config()}, pump=pump)
    bot.pulse = pulse
    svc = ObserveService(bot)
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert await svc.tick_all(now=now) == 0
    assert pump.items == []
    assert pulse.calls == 1
