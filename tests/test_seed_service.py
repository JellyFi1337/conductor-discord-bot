from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.domain.create_lock import BUCKET_CHANNEL, hold_from_wait
from conductor.domain.seed import SeedMode, SeedVerdict, SkeletonKind, required_keys, skeleton_entries
from conductor.presentation import copy
from conductor.services.seed import GuildSeed
from test_seed import _bot, _FakeGuild, _plant_skeleton, _url


def _log_creates(guild: _FakeGuild) -> list[tuple[str, str]]:
    log: list[tuple[str, str]] = []
    orig_category = guild.create_category
    orig_text = guild.create_text_channel
    orig_voice = guild.create_voice_channel
    orig_role = guild.create_role

    async def create_category(name: str, **kwargs: object) -> object:
        log.append(("category", name))
        return await orig_category(name, **kwargs)

    async def create_text_channel(name: str, **kwargs: object) -> object:
        log.append(("text", name))
        return await orig_text(name, **kwargs)

    async def create_voice_channel(name: str, **kwargs: object) -> object:
        log.append(("voice", name))
        return await orig_voice(name, **kwargs)

    async def create_role(name: str, **kwargs: object) -> object:
        log.append(("role", name))
        return await orig_role(name, **kwargs)

    guild.create_category = create_category  # type: ignore[method-assign]
    guild.create_text_channel = create_text_channel  # type: ignore[method-assign]
    guild.create_voice_channel = create_voice_channel  # type: ignore[method-assign]
    guild.create_role = create_role  # type: ignore[method-assign]
    return log


async def test_build_creates_required_keys_in_dependency_order(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr("conductor.services.seed.note_success", lambda *args, **kwargs: calls.append(args))
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    log = _log_creates(guild)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    # Сборка создаёт весь каркас без ролей Old: обязательные строки и зону общения.
    wanted = {entry.key for entry in skeleton_entries()}
    assert required_keys() < wanted
    try:
        with patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)):
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.created == len(wanted)
        assert len(log) == len(wanted)
        assert report.created_categories == 9
        kinds = [kind for kind, _name in log]
        last_category = max(i for i, kind in enumerate(kinds) if kind == "category")
        first_channel = next(i for i, kind in enumerate(kinds) if kind in {"text", "voice"})
        first_role = next(i for i, kind in enumerate(kinds) if kind == "role")
        assert last_category < first_channel
        assert first_role > last_category
        cats = {name: index for index, (kind, name) in enumerate(log) if kind == "category"}
        by_name = {entry.name: entry for entry in skeleton_entries()}
        for index, (kind, name) in enumerate(log):
            if kind not in {"text", "voice"}:
                continue
            parent_key = by_name[name].category
            parent = next(entry.name for entry in skeleton_entries() if entry.key == parent_key)
            assert cats[parent] < index
        assert calls
    finally:
        await engine.dispose()


async def test_build_is_idempotent_on_assembled_guild(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        await _plant_skeleton(guild)
        with patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)):
            first = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
            again = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert first.created == 0
        assert again.created == 0
        assert again.verdict is SeedVerdict.OK
        guild.create_category = AsyncMock(side_effect=AssertionError("create"))  # type: ignore[method-assign]
        with patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)):
            idle = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert idle.created == 0
        guild.create_category.assert_not_called()
    finally:
        await engine.dispose()


async def test_adopt_does_not_create(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    log = _log_creates(guild)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        report = await service.run(guild, actor_id=7, mode=SeedMode.ADOPT)
        assert report.created == 0
        assert log == []
        assert report.missing
    finally:
        await engine.dispose()


async def test_blocked_create_is_held(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    log = _log_creates(guild)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        with patch(
            "conductor.services.seed.hold_for",
            AsyncMock(return_value=hold_from_wait(bucket=BUCKET_CHANNEL, wait=120, now=0.0)),
        ):
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.verdict is SeedVerdict.HELD
        assert report.created == 0
        assert report.held
        assert log == []
        assert copy.SEED_CREATE_HELD in report.notes
    finally:
        await engine.dispose()


async def test_role_http_exception_is_failed_not_raised(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    log = _log_creates(guild)

    async def boom(name: str, **kwargs: object) -> object:
        del name, kwargs
        raise discord.HTTPException(SimpleNamespace(status=500, reason="fail"), "fail")

    guild.create_role = boom  # type: ignore[method-assign]
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        with (
            patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)),
            patch("conductor.services.seed.trip", return_value=None) as tripped,
        ):
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.failed
        assert report.created > 0
        assert any(kind == "category" for kind, _name in log)
        assert tripped.called
    finally:
        await engine.dispose()


async def test_role_headroom_checked_before_create(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    orig_role = guild.create_role

    async def high(name: str, **kwargs: object) -> object:
        role = await orig_role(name, **kwargs)
        role.position = int(guild.bot_role.position) + 1
        return role

    guild.create_role = high  # type: ignore[method-assign]
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        with patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)):
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        seeded = [role for role in guild.roles if role not in {guild.default_role, guild.bot_role}]
        assert len(seeded) == 1
        assert report.failed
        assert any("места под ролью бота" in note for note in report.notes)
    finally:
        await engine.dispose()


async def test_ingest_stays_in_adopt(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    log = _log_creates(guild)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    try:
        report = await service.ingest(guild, kind=SkeletonKind.TEXT, name="правила", actor_id=7)
        assert report is not None
        assert report.created == 0
        assert log == []
    finally:
        await engine.dispose()


async def test_build_aligns_positions_of_created_roles(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _bot(sessions, guild)
    service = GuildSeed(bot)
    calls: list[object] = []
    original = guild.edit_role_positions

    async def spy(positions: object, **kwargs: object) -> object:
        calls.append(positions)
        return await original(positions, **kwargs)

    guild.edit_role_positions = spy  # type: ignore[method-assign]
    try:
        with patch("conductor.services.seed.hold_for", AsyncMock(return_value=None)):
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.created_roles > 0
        assert calls, "первая сборка не выровняла позиции созданных ролей"
    finally:
        await engine.dispose()


async def test_build_with_real_governor_completes_without_local_quotas(tmp_path: Path) -> None:
    """Реальный регулятор и hold_for: вся структура за одну команду, без дублей."""
    from conductor.services.rate_observer import RateObserver
    from conductor.services.traffic import TrafficGovernor

    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _bot(sessions, guild)
    bot.rate_observer = RateObserver()
    bot.traffic = TrafficGovernor(bot, observer=bot.rate_observer)  # без переопределений — как в проде
    service = GuildSeed(bot)
    channel_like = {entry.key for entry in skeleton_entries() if entry.kind is not SkeletonKind.ROLE}
    try:
        report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.created - report.created_roles == len(channel_like)
        assert report.created == len(skeleton_entries())
        assert not report.missing
        assert not report.failed
        assert not report.held
        initial = await service.snapshot(guild)
        assert all(initial[entry.key].bound_id for entry in skeleton_entries())
        repeat = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert repeat.created == 0
        assert not repeat.held
        assert not repeat.failed
        final = await service.snapshot(guild)
        assert all(final[key].bound_id == item.bound_id for key, item in initial.items() if item.bound_id)
    finally:
        await engine.dispose()
