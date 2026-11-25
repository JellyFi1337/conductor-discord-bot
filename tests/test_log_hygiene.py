from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.session import session_scope
from conductor.domain.moderation import CaseCreated
from conductor.services.cache import CacheHub
from conductor.services.domain_events import DomainEvents
from conductor.services.panels import probe_tick, record_panel
from conductor.services.reputation import ReputationService
from conductor.services.role_broker import RoleApplyResult, RoleClaim

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 9, 5, tzinfo=UTC)


class _LogCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def info(self, event: str, **_kwargs: object) -> None:
        self.events.append(("info", event))

    def debug(self, event: str, **_kwargs: object) -> None:
        self.events.append(("debug", event))

    def warning(self, event: str, **_kwargs: object) -> None:
        self.events.append(("warning", event))

    def exception(self, event: str, **_kwargs: object) -> None:
        self.events.append(("exception", event))


class _BanGuild:
    def __init__(self) -> None:
        self.id = 1
        self.members: dict[int, SimpleNamespace] = {}

    def get_member(self, user_id: int) -> SimpleNamespace | None:
        return self.members.get(user_id)


class _FakeRoles:
    def __init__(self) -> None:
        self.claims: list[RoleClaim] = []

    def bind(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def apply(self, claim: RoleClaim) -> RoleApplyResult:
        self.claims.append(claim)
        return RoleApplyResult(ok=True)


def _url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


def _panel_bot(sessions: object) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=sessions,
        get_channel=lambda *_: None,
        add_view=lambda *_a, **_k: None,
        _panel_probe_after=0,
        _booted=True,
        serves=lambda _gid: True,
    )


async def test_probe_without_changes_has_no_info(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _panel_bot(sessions)
    capture = _LogCapture()
    monkeypatch.setattr("conductor.services.panels.log", capture)
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="vc",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=4,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def alive(_channel_id: int, _message_id: int) -> bool:
            return True

        await probe_tick(bot, probe=alive)
        await probe_tick(bot, probe=alive)
        assert not any(level == "info" for level, _event in capture.events)
        assert not any(event == "panels_probe_circle" and level == "info" for level, event in capture.events)
    finally:
        await engine.dispose()


async def test_probe_with_repair_logs_info(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    bot = _panel_bot(sessions)
    capture = _LogCapture()
    monkeypatch.setattr("conductor.services.panels.log", capture)
    try:
        async with session_scope(sessions) as session:
            await record_panel(
                session,
                kind="vc",
                guild_id=1,
                channel_id=20,
                message_id=31,
                ref=4,
                created_by=7,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async def missing(_channel_id: int, _message_id: int) -> bool:
            return False

        report = await probe_tick(bot, probe=missing)
        assert report.stale == 1
        assert ("info", "panels_marked_stale") in capture.events
    finally:
        await engine.dispose()


async def test_departed_reputation_reconcile_has_no_warning(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _BanGuild()
    guild.members[3] = SimpleNamespace(id=3, roles=())
    bot = SimpleNamespace(
        sessions=sessions,
        caches=CacheHub(),
        events=DomainEvents(),
        spawn=None,
        scheduler=None,
        guilds=(),
        get_guild=lambda _gid: guild,
        roles=_FakeRoles(),
        user=SimpleNamespace(id=99),
        log_pump=SimpleNamespace(submit=lambda *_args, **_kwargs: None),
        config_by_guild={1: SimpleNamespace(admin_log_channel_id=11)},
    )
    service = ReputationService(bot)
    capture = _LogCapture()
    monkeypatch.setattr("conductor.services.reputation.log", capture)
    try:
        await service.update_config(1, warn_hit=5, max_hit_per_hour=200)
        await service.set_tier_role(1, 3, 73)
        await service.apply_case(
            CaseCreated(
                guild_id=1,
                case_id=1,
                number=1,
                target_id=3,
                actor_id=2,
                actor_rank=1,
                kind="warn",
                reason="test",
                duration_seconds=None,
                created_at=STAMP,
            ),
            now=STAMP,
        )
        guild.members.pop(3)
        bot.roles.claims.clear()
        capture.events.clear()
        await service.reconcile_member(1, 3)
        assert not any(level == "warning" for level, _event in capture.events)
        assert bot.roles.claims == []
    finally:
        await engine.dispose()


def test_compose_keeps_host_dns() -> None:
    """Публичный DNS в compose ломал вход там, где 1.1.1.1 и 8.8.8.8 недоступны."""
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, service in data["services"].items():
        assert "dns" not in service, name
    env = data["services"]["bot"]["environment"]
    assert env["POSTGRES_HOST"] == "postgres"


def test_discord_gateway_loggers_are_not_silenced() -> None:
    source = (ROOT / "src" / "conductor" / "logging.py").read_text(encoding="utf-8")
    assert "discord.client" not in source
    assert "discord.gateway" not in source
