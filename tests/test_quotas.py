from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import ModCase, ModIntent
from conductor.db.session import session_scope
from conductor.domain.access import MOD_KICK, AccessSnapshot, PermissionRule, Rank, empty_rank_roles
from conductor.domain.admin import AdminCtx
from conductor.domain.moderation import CaseKind, ModVerdict
from conductor.domain.quotas import (
    ALLOWED,
    DEFAULT_TABLE,
    UNLIMITED,
    QuotaDenied,
    QuotaGroup,
    QuotaUsed,
    at_daily_limit,
    day_start,
    group_of,
    parse_quota_json,
    parse_quota_pair,
    quota_verdict,
)
from conductor.presentation import copy
from conductor.presentation.layout import collected_text
from conductor.presentation.moderation import outcome_view
from conductor.services.access import SNAPSHOT_CACHE_KEY
from conductor.services.admin_quotas import PAGE as QUOTAS_PAGE
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.moderation import ModerationService
from conductor.services.role_broker import RoleApplyResult

WATCHER_ROLE = 11
SENIOR_ROLE = 22
CHIEF_ROLE = 33
SPECIAL_ROLE = 44
REASON = "нарушение правил чата"


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


def _member(user_id: int, role_ids: tuple[int, ...] = (), *, admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        bot=False,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=admin),
    )


def _ranked_snapshot(**rules: PermissionRule) -> AccessSnapshot:
    return AccessSnapshot(
        rank_roles=empty_rank_roles()
        | {
            Rank.WATCHER: (WATCHER_ROLE,),
            Rank.SENIOR: (SENIOR_ROLE,),
            Rank.CHIEF: (CHIEF_ROLE,),
            Rank.SPECIAL: (SPECIAL_ROLE,),
        },
        rules=rules,
        special_locked=True,
    )


def _bot(sessions, submitted: list | None = None) -> SimpleNamespace:
    seen: list[object] = submitted if submitted is not None else []
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        user=SimpleNamespace(id=99),
        outbound=None,
        roles=_FakeRoles(),
        log_pump=SimpleNamespace(submit=lambda *args, **kwargs: seen.append((args, kwargs))),
        config_by_guild={
            1: SimpleNamespace(user_log_channel_id=10, admin_log_channel_id=11, timezone="UTC"),
        },
        queue=SimpleNamespace(schedule_later=lambda *_a, **_k: None, cancel_later=lambda *_a, **_k: None),
        get_guild=lambda _gid: None,
    )
    bot.caches.permissions.set((1, SNAPSHOT_CACHE_KEY), _ranked_snapshot())
    return bot


class _FakeRoles:
    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def apply(self, _claim: object) -> RoleApplyResult:
        return RoleApplyResult(ok=True)


class _BanGuild:
    def __init__(self) -> None:
        self.id = 1
        self.owner_id = 1
        self.bans: list[object] = []
        self.members: dict[int, object] = {}
        self.kicks: list[object] = []

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)

    async def ban(self, target: object, delete_message_seconds: int = 0, reason: str | None = None) -> None:
        self.bans.append((int(target.id), delete_message_seconds, reason))


def test_day_start_utc_boundary() -> None:
    before = datetime(2026, 9, 6, 23, 30, tzinfo=UTC)
    after = datetime(2026, 9, 7, 0, 30, tzinfo=UTC)
    assert day_start(before, "UTC") == datetime(2026, 9, 6, tzinfo=UTC)
    assert day_start(after, "UTC") == datetime(2026, 9, 7, tzinfo=UTC)
    moscow = datetime(2026, 9, 6, 22, 30, tzinfo=UTC)
    assert day_start(moscow, "Europe/Moscow") == datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    assert day_start(moscow, ZoneInfo("UTC")) == datetime(2026, 9, 6, tzinfo=UTC)


def test_groups_share_counters() -> None:
    assert group_of(CaseKind.TIMEOUT) is QuotaGroup.TIMEOUTS
    assert group_of(CaseKind.MUTE) is QuotaGroup.TIMEOUTS
    assert group_of(CaseKind.BAN) is QuotaGroup.BANS
    assert group_of(CaseKind.KICK) is QuotaGroup.BANS
    assert group_of(CaseKind.WARN) is None
    assert group_of(CaseKind.UNBAN) is None
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    denied = quota_verdict(Rank.WATCHER, 10, DEFAULT_TABLE, CaseKind.MUTE, False, now=now, tz="UTC")
    assert isinstance(denied, QuotaDenied)
    assert denied.limit == 10
    assert denied.used == 10
    assert denied.group == "timeouts"
    senior = quota_verdict(Rank.SENIOR, 1, DEFAULT_TABLE, CaseKind.KICK, False, now=now, tz="UTC")
    assert isinstance(senior, QuotaDenied)
    watcher_ban = quota_verdict(Rank.WATCHER, 0, DEFAULT_TABLE, CaseKind.KICK, False, now=now, tz="UTC")
    assert isinstance(watcher_ban, QuotaDenied)
    assert watcher_ban.limit == 0


def test_special_is_unlimited() -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    assert DEFAULT_TABLE.for_rank(Rank.SPECIAL).timeouts == UNLIMITED
    assert quota_verdict(Rank.SPECIAL, 500, DEFAULT_TABLE, CaseKind.BAN, False, now=now, tz="UTC") is ALLOWED
    assert at_daily_limit(Rank.SPECIAL, QuotaUsed(90, 90), DEFAULT_TABLE) is False


def test_bypass_allows_even_when_spent() -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    assert quota_verdict(Rank.WATCHER, 10, DEFAULT_TABLE, CaseKind.TIMEOUT, True, now=now, tz="UTC") is ALLOWED


def test_parse_quota_json_merges_defaults() -> None:
    table = parse_quota_json('{"watcher": {"timeouts": 4}}')
    assert table.for_rank(Rank.WATCHER).timeouts == 4
    assert table.for_rank(Rank.WATCHER).bans == 0
    assert table.for_rank(Rank.SENIOR).bans == 1
    assert parse_quota_pair("10 -1") == (10, -1)
    assert parse_quota_pair("10 −1") == (10, -1)
    assert parse_quota_pair("10") is None
    assert parse_quota_pair("-2 1") is None


async def test_used_today_counts_groups_and_skips_bot(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        async with session_scope(sessions) as session:
            session.add_all(
                [
                    _case(1, 2, CaseKind.TIMEOUT, stamp),
                    _case(2, 2, CaseKind.MUTE, stamp),
                    _case(3, 2, CaseKind.WARN, stamp),
                    _case(4, 2, CaseKind.BAN, stamp),
                    _case(5, 99, CaseKind.TIMEOUT, stamp),
                    _case(6, 2, CaseKind.TIMEOUT, stamp - timedelta(days=1)),
                ]
            )
        used = await service.used_today(1, 2, now=stamp)
        assert used == QuotaUsed(timeouts=2, bans=1)
        assert await service.used_today(1, 99, now=stamp) == QuotaUsed()
        assert await service.used_today(1, 2, now=stamp + timedelta(days=1)) == QuotaUsed()
    finally:
        await engine.dispose()


async def test_watcher_eleventh_mute_is_denied_before_enact(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    submitted: list[object] = []
    bot = _bot(sessions, submitted)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    calls: list[object] = []

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        calls.append((duration, reason))

    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    target.timeout = timeout
    guild = SimpleNamespace(id=1, owner_id=1)
    try:
        for _ in range(10):
            outcome = await service.apply(
                guild,
                actor,
                target,
                kind=CaseKind.TIMEOUT,
                reason=REASON,
                duration_raw="10m",
                now=stamp,
            )
            assert outcome.verdict is ModVerdict.OK
        assert len(calls) == 10
        denied = await service.apply(guild, actor, target, kind=CaseKind.MUTE, reason=REASON, now=stamp)
        assert denied.verdict is ModVerdict.QUOTA
        assert denied.quota_used == 10
        assert denied.quota_limit == 10
        assert denied.quota_group == "timeouts"
        assert denied.quota_resets_at == datetime(2026, 9, 8, tzinfo=UTC)
        text = collected_text(outcome_view(denied))
        assert "10 из 10" in text
        assert "тайм-аутов" in text
        warn = await service.apply(guild, actor, target, kind=CaseKind.WARN, reason=REASON, now=stamp)
        assert warn.verdict is ModVerdict.OK
        again = await service.apply(guild, actor, target, kind=CaseKind.MUTE, reason=REASON, now=stamp)
        assert again.verdict is ModVerdict.QUOTA
        denial_cards = [item for item in submitted if copy.QUOTA_DENIED_TITLE in collected_text(item[1]["view"])]
        assert len(denial_cards) == 1
    finally:
        await engine.dispose()


async def test_confirm_heavy_after_quota_is_spent_is_denied(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    bot.caches.permissions.set(
        (1, SNAPSHOT_CACHE_KEY),
        _ranked_snapshot(**{MOD_KICK: PermissionRule(MOD_KICK, frozenset({Rank.SENIOR, Rank.CHIEF, Rank.SPECIAL}))}),
    )
    guild = _BanGuild()
    actor = _member(2, (SENIOR_ROLE,))

    async def kick(*, reason: str | None = None) -> None:
        guild.kicks.append(reason)

    target = _member(3)
    target.kick = kick
    guild.members[2] = actor
    guild.members[3] = target
    bot.get_guild = lambda _gid: guild
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    try:
        started = await service.begin_heavy(guild, actor, target, kind=CaseKind.KICK, reason=REASON, now=stamp)
        assert started.verdict is ModVerdict.OK
        assert started.pending_id is not None
        async with session_scope(sessions) as session:
            session.add(_case(1, 2, CaseKind.BAN, stamp))
        outcome = await service.confirm_heavy(guild, started.pending_id, now=stamp)
        assert outcome.verdict is ModVerdict.QUOTA
        assert guild.kicks == []
        async with session_scope(sessions) as session:
            stored = await session.get(ModIntent, started.pending_id)
            assert stored is not None
            assert stored.consumed_at is None
    finally:
        await engine.dispose()


async def test_bypass_logs_once_per_day(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    submitted: list[object] = []
    bot = _bot(sessions, submitted)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    actor = _member(2, (WATCHER_ROLE,), admin=True)
    target = _member(3)

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    target.timeout = timeout
    guild = SimpleNamespace(id=1, owner_id=1)
    try:
        async with session_scope(sessions) as session:
            session.add_all([_case(index, 2, CaseKind.TIMEOUT, stamp) for index in range(1, 11)])
        first = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="5m",
            now=stamp,
        )
        assert first.verdict is ModVerdict.OK
        second = await service.apply(
            guild,
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="5m",
            now=stamp,
        )
        assert second.verdict is ModVerdict.OK
        bypass_cards = [item for item in submitted if copy.LIMIT_BYPASS_TITLE in collected_text(item[1]["view"])]
        assert len(bypass_cards) == 1
        assert "quota.timeouts" in collected_text(bypass_cards[0][1]["view"])
    finally:
        await engine.dispose()


async def test_special_is_not_capped(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)

    async def timeout(duration: object, *, reason: str | None = None) -> None:
        return None

    actor = _member(2, (SPECIAL_ROLE,))
    target = _member(3)
    target.timeout = timeout
    try:
        async with session_scope(sessions) as session:
            session.add_all([_case(index, 2, CaseKind.TIMEOUT, stamp) for index in range(1, 41)])
        outcome = await service.apply(
            SimpleNamespace(id=1, owner_id=1),
            actor,
            target,
            kind=CaseKind.TIMEOUT,
            reason=REASON,
            duration_raw="5m",
            now=stamp,
        )
        assert outcome.verdict is ModVerdict.OK
    finally:
        await engine.dispose()


def test_quota_today_copy() -> None:
    assert copy.quota_today(timeouts=3, timeout_limit=10, bans=0, ban_limit=1) == "сегодня 3/10 · 0/1"
    assert copy.quota_today(timeouts=2, timeout_limit=-1, bans=0, ban_limit=-1) == "сегодня 2/∞ · 0/∞"


def test_at_limit_ignores_unused_zero_ban_quota() -> None:
    assert at_daily_limit(Rank.WATCHER, QuotaUsed(0, 0), DEFAULT_TABLE) is False
    assert at_daily_limit(Rank.WATCHER, QuotaUsed(10, 0), DEFAULT_TABLE) is True


def test_domain_quotas_has_no_discord_or_sqlalchemy() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "conductor" / "domain" / "quotas.py"
    text = source.read_text(encoding="utf-8")
    assert "discord" not in text
    assert "sqlalchemy" not in text


def test_watchers_line_includes_today() -> None:
    line = copy.watchers_member_line("<@2>", on_duty=True, cases=2, today="сегодня 3/10 · 0/1")
    assert "сегодня 3/10 · 0/1" in line
    assert copy.WATCHERS_ON_DUTY in line


async def test_admin_quotas_page_writes_limits(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    bot.moderation = service
    ctx = AdminCtx(bot=bot, guild_id=1, actor_id=2)
    try:
        snap = await QUOTAS_PAGE.snapshot(ctx, 1)
        assert copy.quota_rank_limits(timeouts=10, bans=0) in snap.fields.values()
        opened = await QUOTAS_PAGE.handle(ctx, "quota.watcher", "")
        assert opened.modal == "numbers"
        assert opened.target == "quota.watcher.set"
        assert opened.defaults == ("10", "0")
        written = await QUOTAS_PAGE.handle(ctx, "quota.watcher.set", "4 −1")
        assert written.ok is True
        table = await service.quota_table(1)
        assert table.for_rank(Rank.WATCHER).timeouts == 4
        assert table.for_rank(Rank.WATCHER).bans == -1
        assert DEFAULT_TABLE.for_rank(Rank.WATCHER).timeouts == 10
        bad = await QUOTAS_PAGE.handle(ctx, "quota.watcher.set", "10")
        assert bad.ok is False
    finally:
        await engine.dispose()


async def test_quota_capped_count_today(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _bot(sessions)
    service = ModerationService(bot)
    stamp = datetime(2026, 9, 7, 8, tzinfo=UTC)
    actor = _member(2, (WATCHER_ROLE,))
    guild = SimpleNamespace(id=1, owner_id=1, members=[actor])
    try:
        async with session_scope(sessions) as session:
            session.add_all([_case(index, 2, CaseKind.TIMEOUT, stamp) for index in range(1, 11)])
        assert await service.quota_capped_count(guild, now=stamp) == 1
        assert await service.quota_capped_count(guild, now=stamp + timedelta(days=1)) == 0
    finally:
        await engine.dispose()


def _case(number: int, actor_id: int, kind: CaseKind, when: datetime) -> ModCase:
    return ModCase(
        guild_id=1,
        number=number,
        owner_id=3,
        created_by=actor_id,
        actor_rank=1,
        kind=kind,
        reason=REASON,
        duration_seconds=60,
        created_at=when,
    )
