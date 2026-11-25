"""Поведение с поддельными часами: срок жизни записи не зависит от datetime.now."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from tests.test_dossier import REASON, WATCHER_ROLE, _member
from tests.test_dossier import _bot as _mod_bot

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.session import session_scope
from conductor.domain.access import HELP_READ, MOD_TIMEOUT, Rank
from conductor.domain.dossier import is_watch_active
from conductor.domain.lfg import is_live
from conductor.domain.moderation import CaseKind, ModVerdict, timeout_until
from conductor.domain.staff_feed import ACTION_WATCH_HIT, ACTION_WATCH_SET
from conductor.services.access import cached_decide, ensure_defaults, set_rank_role
from conductor.services.cache import CacheHub
from conductor.services.role_broker import RoleApplyResult
from conductor.services.watchers_report import WatchersReportService

STAMP = datetime(2026, 9, 7, 8, tzinfo=UTC)
HOUR = STAMP + timedelta(hours=1)
CENTURY = STAMP + timedelta(days=365 * 100)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'conductor.db').as_posix()}"


class _StaffRoles:
    def __init__(self) -> None:
        self.claims: list[object] = []

    async def apply(self, claim: object) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True, called=True)


def test_timeout_and_lfg_ttl_follow_injected_now() -> None:
    until = timeout_until(STAMP, timedelta(days=7))
    assert until > HOUR
    assert until < CENTURY
    assert is_live(closed_at=None, expires_at=until, now=HOUR) is True
    assert is_live(closed_at=None, expires_at=until, now=CENTURY) is False
    watch = SimpleNamespace(until=until)
    assert is_watch_active(watch, HOUR) is True
    assert is_watch_active(watch, CENTURY) is False


async def test_watch_hit_active_in_an_hour_and_expired_in_a_century(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _mod_bot(sessions)
    posts: list[str] = []

    async def capture(_guild_id: int, **kwargs: object) -> int | None:
        posts.append(str(kwargs.get("action")))
        return 1

    bot.staff_feed = SimpleNamespace(post=capture)
    guild = SimpleNamespace(id=1, owner_id=1)
    actor = _member(2, (WATCHER_ROLE,))
    target = _member(3)
    try:
        stored = await bot.moderation.set_watch(
            1, 3, set_by=2, actor_rank=Rank.WATCHER, reason=REASON, days_raw="7", now=STAMP
        )
        assert stored is not None
        assert ACTION_WATCH_SET in posts
        hour = await bot.moderation.apply(
            guild, actor, target, kind=CaseKind.TIMEOUT, reason=REASON, duration_raw="10m", now=HOUR
        )
        assert hour.verdict is ModVerdict.OK
        assert posts.count(ACTION_WATCH_HIT) == 1
        century = await bot.moderation.apply(
            guild, actor, target, kind=CaseKind.WARN, reason=REASON, now=CENTURY
        )
        assert century.verdict is ModVerdict.OK
        assert posts.count(ACTION_WATCH_HIT) == 1
    finally:
        await engine.dispose()


async def test_suspend_denies_in_an_hour_and_expires_in_a_century(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    roles = _StaffRoles()
    posted: list[object] = []

    async def post(*_args: object, **kwargs: object) -> int:
        posted.append(kwargs)
        return 1

    dms: list[object] = []

    async def send(**_kwargs: object) -> None:
        dms.append(True)

    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        roles=roles,
        staff_feed=SimpleNamespace(post=post),
        get_user=lambda _uid: SimpleNamespace(send=send),
        outbound=None,
        scheduler=None,
        is_developer=lambda _uid: False,
    )
    service = WatchersReportService(bot)
    try:
        async with session_scope(sessions) as session:
            await ensure_defaults(session, 1)
            await set_rank_role(session, 1, Rank.WATCHER, 10)
        result = await service.suspend(
            1,
            2,
            actor_id=9,
            actor_rank=Rank.SPECIAL,
            target_rank=Rank.WATCHER,
            days_raw="7",
            reason="три отмены за месяц",
            now=STAMP,
        )
        assert getattr(result, "until", None) is not None
        assert dms
        assert posted
        denied = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=MOD_TIMEOUT,
            now=HOUR,
        )
        assert denied.allowed is False
        help_ok = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=HELP_READ,
            now=HOUR,
        )
        assert help_ok.allowed is True
        expired = await cached_decide(
            bot,  # type: ignore[arg-type]
            guild_id=1,
            user_id=2,
            member_role_ids=(10,),
            is_administrator=False,
            key=MOD_TIMEOUT,
            now=CENTURY,
        )
        assert expired.allowed is True
    finally:
        await engine.dispose()
