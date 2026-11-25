from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from conductor.cogs.diagnostics import CheckStatus, DiagnoseSnapshot, build_diagnose_lines
from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ActivityBucket, GuildConfig, MemberDaily, MemberRecord, RecruitLink, VoiceSession
from conductor.db.session import session_scope
from conductor.domain.access import PULSE_ALERTS, PULSE_STAFF, Rank, can
from conductor.domain.activity import EventKind, PeriodKind, day_bucket, hour_bucket
from conductor.domain.observe import TAB_NOW, TAB_SETUP, AlertKind
from conductor.domain.pulse import (
    AUTHOR_SATURATION,
    AUTHORS_DROP_RATIO,
    CountStyle,
    HealthBand,
    PulseAlertRules,
    PulseInputs,
    PulseSnapshot,
    QualityFlag,
    below_median_ratio,
    catalog_bot_names,
    evaluate_pulse_alerts,
    format_count,
    health_score,
    is_catalog_bot_name,
    public_online,
    series_median,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.pulse import alerts_view, now_view
from conductor.services.cache import CacheHub
from conductor.services.observe import collect_diagnose_snapshot
from conductor.services.pulse import PulseService, bump_pulse

STAMP = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _inputs(**overrides: object) -> PulseInputs:
    values: dict[str, object] = {
        "humans": 100,
        "bots": 2,
        "boosters": 4,
        "premium_tier": 2,
        "voice_now": 8,
        "online": 40,
        "unique_authors_24h": 15,
        "messages_24h": 200,
        "messages_7d_avg": 100.0,
        "voice_minutes_24h": 2000,
        "joins_7d": 10,
        "leaves_7d": 5,
        "tickets_open": 1,
        "duty_watchers": 2,
        "next_gathering_at": STAMP + timedelta(hours=3),
        "next_gathering_title": "клановый вечер",
        "next_gathering_id": 4,
        "has_presence_intent": False,
        "census_age_hours": 2.0,
        "has_message_history": True,
        "has_voice_sessions": True,
    }
    values.update(overrides)
    return PulseInputs(**values)  # type: ignore[arg-type]


def test_pulse_staff_and_alerts_defaults() -> None:
    assert can(Rank.NONE, key=PULSE_STAFF) is False
    assert can(Rank.WATCHER, key=PULSE_STAFF) is True
    assert can(Rank.CHIEF, key=PULSE_ALERTS) is True
    assert can(Rank.WATCHER, key=PULSE_ALERTS) is False


def test_format_count_plain_and_compact() -> None:
    assert format_count(999) == "999"
    assert format_count(1000, CountStyle.COMPACT) == "1.0K"
    assert format_count(12_300, "k") == "12.3K"
    assert format_count(999, "k") == "999"


def test_health_score_bounds_and_weights() -> None:
    top = health_score(_inputs(), STAMP)
    assert top.score == 100
    assert top.band is HealthBand.PEAK
    assert top.parts.authors == 100
    assert top.parts.voice == 100
    assert top.parts.retention == 100
    assert top.parts.trend == 100

    floor = health_score(
        _inputs(
            unique_authors_24h=0,
            messages_24h=0,
            messages_7d_avg=0.0,
            voice_minutes_24h=0,
            joins_7d=0,
            leaves_7d=0,
        ),
        STAMP,
    )
    assert floor.score == 0
    assert floor.band is HealthBand.QUIET


def test_one_author_on_large_guild_is_not_a_hundred() -> None:
    report = health_score(_inputs(humans=600, unique_authors_24h=1, voice_minutes_24h=0), STAMP)
    assert report.parts.authors < 5
    assert report.parts.authors == round(100 * (1 / 600) / AUTHOR_SATURATION)


def test_unknown_when_history_and_voice_are_empty() -> None:
    report = health_score(
        _inputs(has_message_history=False, has_voice_sessions=False, unique_authors_24h=80),
        STAMP,
    )
    assert report.band is HealthBand.UNKNOWN
    assert report.score == 0
    assert QualityFlag.NO_HISTORY in report.flags
    assert QualityFlag.NO_VOICE_SESSIONS in report.flags


def test_no_presence_hides_online_instead_of_zero() -> None:
    hidden = _inputs(has_presence_intent=False, online=0)
    shown = _inputs(has_presence_intent=True, online=12)
    assert public_online(hidden) is None
    assert public_online(shown) == 12
    assert QualityFlag.NO_PRESENCE in health_score(hidden, STAMP).flags


def test_retention_with_zero_leaves_does_not_divide() -> None:
    report = health_score(_inputs(joins_7d=4, leaves_7d=0), STAMP)
    assert report.parts.retention == 100


def test_stale_and_missing_census_flags() -> None:
    missing = health_score(_inputs(census_age_hours=None), STAMP)
    stale = health_score(_inputs(census_age_hours=8 * 24), STAMP)
    assert QualityFlag.NO_CENSUS in missing.flags
    assert QualityFlag.STALE_CENSUS in stale.flags
    assert QualityFlag.NO_CENSUS not in stale.flags


def test_public_card_has_no_names_and_no_fake_online() -> None:
    report = health_score(_inputs(), STAMP)
    snap = PulseSnapshot(inputs=_inputs(), report=report, assembled_at=STAMP)
    text = collected_text(now_view(snap, staff=False))
    assert copy.PULSE_TITLE in text
    assert "в голосе **8**" in text
    assert "онлайн" not in text
    assert "@" not in text
    assert copy.PULSE_FOOTNOTE in text
    assert copy.PULSE_STAFF_TITLE not in text
    staff = collected_text(now_view(snap, staff=True))
    assert copy.PULSE_STAFF_TITLE in staff
    assert "входы **10**" in staff
    assert "по рекрут-ссылкам **0**" in staff
    assert "vanity **0**" in staff
    assert "без кода **0**" in staff
    split = collected_text(
        now_view(
            PulseSnapshot(
                inputs=_inputs(joins_7d_coded=4, joins_7d_vanity=1, joins_7d_unknown=2),
                report=health_score(_inputs(), STAMP),
                assembled_at=STAMP,
            ),
            staff=True,
        )
    )
    assert "по рекрут-ссылкам **4**" in split
    assert "vanity **1**" in split
    assert "без кода **2**" in split
    live = _inputs(has_presence_intent=True, online=12)
    live_text = collected_text(
        now_view(PulseSnapshot(inputs=live, report=health_score(live, STAMP), assembled_at=STAMP), staff=False)
    )
    assert "онлайн **12**" in live_text


def _diagnose(**overrides: object) -> DiagnoseSnapshot:
    from conductor.cogs.diagnostics import LogChannelView

    logs = LogChannelView(True, "u", True, True)
    values: dict[str, object] = {
        "members_intent": True,
        "message_content_intent": True,
        "members_loaded": 1,
        "member_count": 1,
        "permissions": {},
        "bot_top_position": 1,
        "managed_roles": (),
        "user_log": logs,
        "admin_log": logs,
        "latency_ms": 10,
        "log_backlog": 0,
        "log_sent": 0,
        "log_failed": 0,
        "log_dropped": 0,
        "log_pump_alive": True,
        "unread_count": 0,
        "unread_names": (),
    }
    values.update(overrides)
    return DiagnoseSnapshot(**values)  # type: ignore[arg-type]


def test_diagnose_now_mentions_pulse() -> None:
    empty = _diagnose()
    never = [line.title for line in build_diagnose_lines(empty, tab=TAB_NOW)]
    assert copy.DIAGNOSE_PULSE_NEVER in never

    ready = _diagnose(
        pulse_band="alive",
        pulse_assembled_at=STAMP,
        pulse_invalidated_at=STAMP - timedelta(minutes=5),
        pulse_has_presence=False,
    )
    titles = [line.title for line in build_diagnose_lines(ready, tab=TAB_NOW)]
    joined = "\n".join(titles)
    assert "живой" in joined
    assert "Presence выключен" in joined
    pulse_lines = [line for line in build_diagnose_lines(ready, tab=TAB_NOW) if "Пульс" in line.title]
    assert pulse_lines
    assert all(line.status is not CheckStatus.FAIL for line in pulse_lines)
    assert any("Сигналы пульса" in line.title for line in build_diagnose_lines(ready, tab=TAB_NOW))
    off = [line.title for line in build_diagnose_lines(_diagnose(pulse_alerts_enabled=False), tab=TAB_NOW)]
    assert copy.DIAGNOSE_PULSE_ALERTS_OFF in off


def test_catalog_bot_names_match_only_known_apps() -> None:
    assert catalog_bot_names(("DSMonitoring", "MEE6", "Server Monitoring")) == (
        "DSMonitoring",
        "Server Monitoring",
    )
    assert catalog_bot_names(("dsmonitoring#0015", "Conductor", "Ann")) == ("dsmonitoring#0015",)
    assert is_catalog_bot_name("Server Monitoring") is True
    assert is_catalog_bot_name("MEE6") is False
    assert catalog_bot_names(()) == ()


def test_diagnose_flags_catalog_bot() -> None:
    lines = build_diagnose_lines(_diagnose(catalog_bots=("DSMonitoring",)), tab=TAB_SETUP)
    warn = next(line for line in lines if copy.DIAGNOSE_CATALOG_BOT in line.title)
    assert warn.status is CheckStatus.WARN
    assert "/pulse retire" not in warn.action
    assert copy.DIAGNOSE_CATALOG_BOT_ACTION in warn.action
    clean = [line.title for line in build_diagnose_lines(_diagnose(), tab=TAB_SETUP)]
    assert all(copy.DIAGNOSE_CATALOG_BOT not in title for title in clean)


def test_diagnose_snapshot_lists_catalog_bots() -> None:
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(guild_permissions=None, top_role=SimpleNamespace(position=1)),
        member_count=3,
        members=(
            SimpleNamespace(bot=True, name="DSMonitoring"),
            SimpleNamespace(bot=True, name="MEE6"),
            SimpleNamespace(bot=False, name="Server Monitoring"),
        ),
        text_channels=[],
        voice_channels=[],
        stage_channels=[],
        threads=[],
        get_channel=lambda _id: None,
        get_role=lambda _id: None,
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(members=True, message_content=True),
        log_pump=None,
        history=SimpleNamespace(is_caught_up=lambda *_args: True),
        fence=SimpleNamespace(mode=SimpleNamespace(value="live")),
        serves=lambda _gid: True,
        caches=None,
        messages=[],
        outbound=None,
        interactions=None,
        queue=None,
        motion=None,
        _bg_tasks=set(),
        latency=0.01,
    )
    snap = collect_diagnose_snapshot(bot, guild, None)
    assert snap.catalog_bots == ("DSMonitoring",)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


async def _sessions(tmp_path: Path):
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    return create_sessionmaker(engine), engine


def _guild(*, members: tuple[object, ...] = ()) -> SimpleNamespace:
    human = SimpleNamespace(bot=False, premium_since=None, status="online")
    return SimpleNamespace(
        id=7,
        members=members or (human, human),
        voice_channels=(SimpleNamespace(members=(human,)),),
        stage_channels=(),
        premium_subscription_count=3,
    )


async def test_snapshot_cache_invalidates_on_event(tmp_path: Path) -> None:
    factory, _engine = await _sessions(tmp_path)
    bot = SimpleNamespace(sessions=factory, caches=CacheHub(), intents=SimpleNamespace(presences=False))
    service = PulseService(bot)
    guild = _guild()
    async with session_scope(factory) as session:
        session.add(
            MemberRecord(
                guild_id=7,
                user_id=1,
                username="ann",
                display_name="Ann",
                is_bot=False,
                currently_member=True,
                first_joined_at=STAMP,
                last_joined_at=STAMP,
            )
        )
        session.add(
            ActivityBucket(
                guild_id=7,
                period_start=hour_bucket(STAMP),
                period_kind=PeriodKind.HOUR.value,
                event_kind=EventKind.MESSAGE.value,
                count=12,
            )
        )
        session.add(
            ActivityBucket(
                guild_id=7,
                period_start=day_bucket(STAMP),
                period_kind=PeriodKind.DAY.value,
                event_kind=EventKind.MESSAGE.value,
                count=12,
            )
        )
        session.add(MemberDaily(guild_id=7, user_id=1, day=day_bucket(STAMP), messages=4, voice_seconds=0, reactions=0))
        session.add(
            VoiceSession(
                guild_id=7,
                user_id=1,
                channel_id=9,
                joined_at=STAMP - timedelta(hours=2),
                left_at=STAMP - timedelta(hours=1),
                duration_seconds=3600,
                last_event_at=STAMP - timedelta(hours=1),
            )
        )
        config = await session.get(GuildConfig, 7)
        if config is None:
            session.add(GuildConfig(guild_id=7, census_at=STAMP - timedelta(hours=3)))
        else:
            config.census_at = STAMP - timedelta(hours=3)

    first = await service.snapshot(guild, now=STAMP)
    assert first.from_cache is False
    assert first.inputs.voice_now == 1
    assert first.inputs.has_presence_intent is False
    assert public_online(first.inputs) is None
    collected = {"n": 0}
    original = service._collect

    async def _counted(guild_obj: object, moment: datetime) -> PulseInputs:
        collected["n"] += 1
        return await original(guild_obj, moment)

    service._collect = _counted  # type: ignore[method-assign]
    second = await service.snapshot(guild, now=STAMP)
    assert second.from_cache is True
    assert second.assembled_at == first.assembled_at
    assert collected["n"] == 0
    later = STAMP + timedelta(seconds=5)
    bump_pulse(SimpleNamespace(pulse=service), 7, now=later)
    assert service.last_invalidated_at(7) == later
    third = await service.snapshot(guild, now=later)
    assert third.from_cache is False
    assert collected["n"] == 1


async def test_inflow_7d_splits_coded_vanity_and_unknown(tmp_path: Path) -> None:
    factory, engine = await _sessions(tmp_path)
    try:
        bot = SimpleNamespace(sessions=factory, caches=None, intents=SimpleNamespace(presences=False))
        service = PulseService(bot)
        guild = _guild()
        guild.vanity_url_code = "cool"
        async with session_scope(factory) as session:
            for user_id, code, joined in (
                (1, "abc", STAMP - timedelta(days=1)),
                (2, "xyz", STAMP - timedelta(days=2)),
                (3, "cool", STAMP - timedelta(hours=3)),
                (4, None, STAMP - timedelta(days=1)),
                (5, "old", STAMP - timedelta(days=8)),
                (6, "botcode", STAMP - timedelta(hours=1)),
            ):
                session.add(
                    MemberRecord(
                        guild_id=7,
                        user_id=user_id,
                        username=f"u{user_id}",
                        display_name=f"U{user_id}",
                        is_bot=user_id == 6,
                        currently_member=True,
                        first_joined_at=joined,
                        last_joined_at=joined,
                        invite_code=code,
                    )
                )
            for owner_id, token in ((10, "abc"), (11, "xyz")):
                session.add(
                    RecruitLink(
                        guild_id=7,
                        code=token,
                        owner_user_id=owner_id,
                        channel_id=1,
                        created_by=10,
                        created_at=STAMP,
                    )
                )
        coded, vanity, unknown = await service._inflow_7d(7, guild, STAMP)
        assert (coded, vanity, unknown) == (2, 1, 1)
    finally:
        await engine.dispose()


async def test_snapshot_ttl_expires_without_event(tmp_path: Path) -> None:
    factory, _engine = await _sessions(tmp_path)
    clock = {"now": 0.0}
    bot = SimpleNamespace(
        sessions=factory,
        caches=CacheHub(clock=lambda: clock["now"]),
        intents=SimpleNamespace(presences=False),
    )
    service = PulseService(bot)
    guild = _guild()
    first = await service.snapshot(guild, now=STAMP)
    assert first.from_cache is False
    assert (await service.snapshot(guild, now=STAMP)).from_cache is True
    clock["now"] += 31
    expired = await service.snapshot(guild, now=STAMP)
    assert expired.from_cache is False


def test_series_median_and_half_ratio() -> None:
    assert series_median(()) is None
    assert series_median((1, 2, 3, 4, 5)) == 3
    assert series_median((1, 2, 3, 4)) == 2.5
    assert below_median_ratio(4, (10, 10, 10), ratio=AUTHORS_DROP_RATIO) is True
    assert below_median_ratio(5, (10, 10, 10), ratio=AUTHORS_DROP_RATIO) is False
    assert below_median_ratio(0, (0, 0, 0), ratio=AUTHORS_DROP_RATIO) is False


def test_voice_empty_needs_median_floor() -> None:
    low = evaluate_pulse_alerts(
        _inputs(voice_now=0, peak_hour=12, peak_voice_7d=(4, 4, 4, 4, 4, 4, 4)),
        PulseAlertRules(),
        now=STAMP,
    )
    ready = evaluate_pulse_alerts(
        _inputs(voice_now=0, peak_hour=12, peak_voice_7d=(5, 5, 5, 5, 5, 5, 5)),
        PulseAlertRules(),
        now=STAMP,
    )
    assert AlertKind.VOICE_EMPTY_AT_PEAK not in {item.kind for item in low}
    assert AlertKind.VOICE_EMPTY_AT_PEAK in {item.kind for item in ready}


def test_signal_copy_points_to_pulse_without_names() -> None:
    text = "\n".join(
        (
            copy.signal_voice(median=6, voice=0),
            copy.signal_invert(leaves=5, threshold=5),
            copy.signal_authors(authors=2, median=10, ratio=AUTHORS_DROP_RATIO),
            copy.signal_boosts(now=1, was=4),
            copy.SIGNAL_LOOK,
        )
    )
    assert "медиана" in text
    assert "/stats" in text
    assert "Сейчас" in text
    assert "Сводка" in text
    assert "@" not in text
    card = collected_text(alerts_view(PulseAlertRules(enabled=True, voice_floor=8)))
    assert copy.SIGNAL_TITLE in card
    assert copy.SIGNAL_ON in card
    assert "8" in card
    off = collected_text(alerts_view(PulseAlertRules(enabled=False)))
    assert copy.SIGNAL_OFF in off


def test_join_leave_invert_needs_five_leaves() -> None:
    four = evaluate_pulse_alerts(_inputs(joins_24h=0, leaves_24h=4), PulseAlertRules(), now=STAMP)
    five = evaluate_pulse_alerts(_inputs(joins_24h=3, leaves_24h=5), PulseAlertRules(), now=STAMP)
    more_joins = evaluate_pulse_alerts(_inputs(joins_24h=6, leaves_24h=5), PulseAlertRules(), now=STAMP)
    assert AlertKind.JOIN_LEAVE_INVERT not in {item.kind for item in four}
    assert AlertKind.JOIN_LEAVE_INVERT in {item.kind for item in five}
    assert AlertKind.JOIN_LEAVE_INVERT not in {item.kind for item in more_joins}


def test_empty_guild_and_disabled_alerts_are_silent() -> None:
    empty = evaluate_pulse_alerts(_inputs(), PulseAlertRules(), now=STAMP)
    assert empty == ()
    firing = _inputs(
        voice_now=0,
        peak_hour=12,
        peak_voice_7d=(6, 6, 6, 6, 6, 6, 6),
        joins_24h=0,
        leaves_24h=8,
        unique_authors_24h=2,
        authors_7d=(10, 10, 10, 10, 10, 10, 10),
        boosters=1,
        boosters_24h_ago=4,
    )
    on = evaluate_pulse_alerts(firing, PulseAlertRules(), now=STAMP)
    kinds = {item.kind for item in on}
    assert AlertKind.VOICE_EMPTY_AT_PEAK in kinds
    assert AlertKind.JOIN_LEAVE_INVERT in kinds
    assert AlertKind.AUTHORS_DROP in kinds
    assert AlertKind.BOOSTS_DROP in kinds
    off = evaluate_pulse_alerts(firing, PulseAlertRules(enabled=False), now=STAMP)
    assert off == ()
    not_peak = evaluate_pulse_alerts(firing, PulseAlertRules(), now=STAMP.replace(hour=3))
    assert AlertKind.VOICE_EMPTY_AT_PEAK not in {item.kind for item in not_peak}


async def test_alert_rules_persist_on_and_off(tmp_path: Path) -> None:
    factory, engine = await _sessions(tmp_path)
    bot = SimpleNamespace(sessions=factory, caches=None, intents=SimpleNamespace(presences=False))
    service = PulseService(bot)
    try:
        verdict, rules = await service.set_alerts(7, enabled=True, voice_floor=8, leave_floor=6, authors_pct=40)
        assert verdict.value == "ok"
        assert rules.voice_floor == 8
        assert rules.authors_ratio == 0.4
        loaded = await service.alert_rules(7)
        assert loaded.enabled is True
        assert loaded.leave_floor == 6
        off, silent = await service.set_alerts(7, enabled=False)
        assert off.value == "ok"
        assert silent.enabled is False
        assert silent.voice_floor == 8
        bad, _rules = await service.set_alerts(7, enabled=True, authors_pct=0)
        assert bad.value == "bad_authors"
    finally:
        await engine.dispose()


def test_boost_lookback_needs_old_sample() -> None:
    service = PulseService(SimpleNamespace(sessions=None, caches=None, intents=SimpleNamespace(presences=False)))
    service._remember_boosters(7, 3, STAMP)
    assert service._boosters_24h_ago(7, STAMP) is None
    service._remember_boosters(7, 5, STAMP - timedelta(hours=24))
    assert service._boosters_24h_ago(7, STAMP) == 5


async def test_collector_fills_alert_inputs_from_buckets(tmp_path: Path) -> None:
    factory, engine = await _sessions(tmp_path)
    bot = SimpleNamespace(sessions=factory, caches=CacheHub(), intents=SimpleNamespace(presences=False))
    service = PulseService(bot)
    try:
        async with session_scope(factory) as session:
            for offset in range(7):
                day = day_bucket(STAMP) - timedelta(days=offset)
                noon = day.replace(hour=12)
                session.add(
                    ActivityBucket(
                        guild_id=7,
                        period_start=noon,
                        period_kind=PeriodKind.HOUR.value,
                        event_kind=EventKind.MESSAGE.value,
                        count=10,
                    )
                )
                session.add(
                    ActivityBucket(
                        guild_id=7,
                        period_start=noon,
                        period_kind=PeriodKind.HOUR.value,
                        event_kind=EventKind.VOICE_MINUTE.value,
                        count=6,
                    )
                )
                session.add(MemberDaily(guild_id=7, user_id=1, day=day, messages=2, voice_seconds=0, reactions=0))
                session.add(MemberDaily(guild_id=7, user_id=2, day=day, messages=2, voice_seconds=0, reactions=0))
            session.add(
                ActivityBucket(
                    guild_id=7,
                    period_start=hour_bucket(STAMP - timedelta(hours=2)),
                    period_kind=PeriodKind.HOUR.value,
                    event_kind=EventKind.JOIN.value,
                    count=1,
                )
            )
            session.add(
                ActivityBucket(
                    guild_id=7,
                    period_start=hour_bucket(STAMP - timedelta(hours=1)),
                    period_kind=PeriodKind.HOUR.value,
                    event_kind=EventKind.LEAVE.value,
                    count=7,
                )
            )
        snap = await service.snapshot(_guild(), now=STAMP)
        assert snap.inputs.peak_hour == 12
        assert snap.inputs.peak_voice_7d == (6, 6, 6, 6, 6, 6, 6)
        assert snap.inputs.joins_24h == 1
        assert snap.inputs.leaves_24h == 7
        assert snap.inputs.authors_7d == (2, 2, 2, 2, 2, 2, 2)
        assert snap.inputs.boosters_24h_ago is None
        kinds = {item.kind for item in evaluate_pulse_alerts(snap.inputs, PulseAlertRules(), now=STAMP)}
        assert AlertKind.JOIN_LEAVE_INVERT in kinds
        assert AlertKind.VOICE_EMPTY_AT_PEAK not in kinds
    finally:
        await engine.dispose()
