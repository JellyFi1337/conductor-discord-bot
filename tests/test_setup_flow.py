"""Сквозные сценарии настройки: пустой сервер, усыновление, поломка и починка."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from conductor.db.engine import apply_migrations, create_engine, create_sessionmaker
from conductor.db.models import GuildAccessState, GuildRank, Panel
from conductor.db.session import session_scope
from conductor.domain.access import Rank, special_locked_now
from conductor.domain.audit import (
    PANEL_LIVE_STATE,
    audit_guild,
    live_panels_without_message,
    missing_bindings,
    orphaned_special,
)
from conductor.domain.blueprint import IssueLevel, parse_blueprint
from conductor.domain.seed import LAYOUT, SeedMode, SeedVerdict, SkeletonStatus, feature_verdicts, role_order
from conductor.domain.showcase import SHOWCASES, ShowcaseState
from conductor.services.access import load_snapshot
from conductor.services.blueprint import BlueprintService
from conductor.services.panels import record_panel
from conductor.services.seed import GuildSeed
from conductor.services.setup_map import SetupMapper
from conductor.services.showcase import ShowcaseService
from conductor.services.structure import StructureWatch
from test_seed import _bot, _FakeGuild, _FakeMember, _url

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_skeleton.py"


class FakePump:
    def __init__(self) -> None:
        self.items: list[tuple[int | None, dict[str, object]]] = []

    def submit(self, channel_id: int | None, **kwargs: object) -> None:
        self.items.append((channel_id, kwargs))


def _flow_bot(sessions: object, guild: _FakeGuild):
    bot = _bot(sessions, guild)
    bot.seed = GuildSeed(bot)
    bot.showcase = ShowcaseService(bot)
    bot.setup_map = SetupMapper(bot)
    bot.log_pump = FakePump()
    bot.guilds = [guild]
    bot.scheduler = SimpleNamespace(every=lambda *_a, **_k: None)
    bot.structure = StructureWatch(bot)
    return bot


def _blocked() -> object:
    return patch("conductor.services.seed.hold_for", AsyncMock(return_value=None))


def _existing_objects(guild: _FakeGuild) -> tuple[object, ...]:
    return tuple((*guild.categories, *guild.text_channels, *guild.voice_channels, *guild.roles))


def _spy_name_edits(items: tuple[object, ...]) -> list[int]:
    renamed: list[int] = []
    for item in items:
        original = getattr(item, "edit", None)
        if not callable(original):
            continue

        async def tracked(*_args: object, orig=original, obj=item, **kwargs: object) -> None:
            if "name" in kwargs:
                renamed.append(int(obj.id))
            await orig(*_args, **kwargs)

        item.edit = tracked  # type: ignore[method-assign]
    return renamed


async def _align_roles(guild: _FakeGuild, service: GuildSeed) -> None:
    existing = await service.snapshot(guild)
    top = int(guild.me.top_role.position)
    ordered = []
    for key in role_order():
        item = existing.get(key)
        if item is None or not item.bound_id:
            continue
        role = guild.get_role(int(item.bound_id))
        if role is not None:
            ordered.append(role)
    await guild.edit_role_positions({role: top - 1 - index for index, role in enumerate(ordered)})


async def _plant_live_showcases(bot: object, guild: _FakeGuild, *, actor_id: int = 7) -> None:
    before = await bot.showcase.statuses(guild)
    posted = await bot.showcase.post_missing(guild, actor_id=actor_id)
    targets = [
        spec
        for spec in SHOWCASES
        if before.get(spec.id) in {ShowcaseState.MISSING, ShowcaseState.STALE}
    ]
    assert len(posted) == len(targets)
    assert all(item.ok for item in posted)
    bound = await bot.showcase.bound_ids(int(guild.id))
    async with session_scope(bot.sessions) as session:
        for spec, item in zip(targets, posted, strict=True):
            channel_id = item.channel_id or bound.get(spec.channel_key)
            assert channel_id
            await record_panel(
                session,
                kind=spec.panel_kind,
                guild_id=int(guild.id),
                channel_id=int(channel_id),
                message_id=int(channel_id),
                ref=spec.id,
                created_by=actor_id,
            )
    bot.showcase._drop_cache(int(guild.id))


async def _assert_features_ok(bot: object, guild: _FakeGuild) -> None:
    checks = await bot.seed.diagnose_checks(guild)
    states = await bot.showcase.statuses(guild)
    showcases = {spec.id: states[spec.id].value for spec in SHOWCASES if spec.id in states}
    verdicts = feature_verdicts(checks, showcases=showcases, with_old=False)
    failed = [
        (item.feature, item.status.value, item.note, item.missing, item.broken)
        for item in verdicts
        if item.status is not SkeletonStatus.OK
    ]
    assert failed == [], failed


def test_orphaned_special_is_empty_binding_after_ever_bound() -> None:
    assert orphaned_special(ever_bound=False, special_role_ids=()) is False
    assert orphaned_special(ever_bound=True, special_role_ids=()) is True
    assert orphaned_special(ever_bound=True, special_role_ids=(11,)) is False
    assert orphaned_special(ever_bound=True, special_role_ids=(11,), known_role_ids=frozenset()) is True
    assert orphaned_special(ever_bound=True, special_role_ids=(11,), known_role_ids=frozenset({11})) is False


def test_audit_flags_missing_ids_and_live_without_message() -> None:
    report = audit_guild(
        guild_id=7,
        bound={"rules": 10, "automod": 20},
        live_panels=(("vf", 10, 0, PANEL_LIVE_STATE), ("am", 20, 5, PANEL_LIVE_STATE)),
        scheme_drift=("skeleton.build",),
        special_ever_bound=True,
        special_role_ids=(),
        known_object_ids=frozenset({10}),
        known_role_ids=frozenset(),
    )
    assert report.orphaned_special is True
    assert report.missing_ids[0].key == "automod"
    assert report.live_without_message[0].kind == "vf"
    assert report.scheme_drift == ("skeleton.build",)
    assert missing_bindings({"rules": 10}, None) == ()
    assert live_panels_without_message((("vf", 1, 2, "stale"),)) == ()


def test_verify_skeleton_script_is_read_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"--yes"' not in text
    assert "'--yes'" not in text
    assert "session.add" not in text
    assert "session.commit" not in text
    assert "load_dotenv" in text
    assert "Settings()" in text
    assert "orphaned_special" in text
    assert "special_ever_bound" in text
    tree = ast.parse(text)
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "add_argument" in names
    assert "commit" not in names
    options: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                options.append(arg.value)
    assert "--yes" not in options


async def test_empty_server_becomes_ready(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _flow_bot(sessions, guild)
    service = bot.seed
    try:
        with _blocked():
            report = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.verdict is SeedVerdict.OK
        assert report.created > 0
        with _blocked():
            idle = await service.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert idle.verdict is SeedVerdict.OK
        assert idle.created == 0
        await _align_roles(guild, service)
        rights = await service.reapply_rights(guild, actor_id=7)
        assert rights.verdict is SeedVerdict.OK
        await _plant_live_showcases(bot, guild)
        await _assert_features_ok(bot, guild)
        async with session_scope(sessions) as session:
            access = await load_snapshot(session, 99)
            ranks = (await session.scalars(select(GuildRank).where(GuildRank.guild_id == 99))).all()
            state = await session.get(GuildAccessState, 99)
            live = (await session.scalars(select(Panel).where(Panel.guild_id == 99))).all()
        assert {row.rank for row in ranks} >= {"special", "chief", "senior", "watcher"}
        assert special_locked_now(access.rank_roles, ever_bound=True) is True
        assert state is not None
        assert state.special_ever_bound is True
        assert live
        known = frozenset(int(item.id) for item in _existing_objects(guild))
        special_ids = tuple(int(row.role_id) for row in ranks if row.rank == Rank.SPECIAL.key)
        audit = audit_guild(
            guild_id=99,
            bound={"role_special": special_ids[0]} if special_ids else {},
            live_panels=tuple(
                (str(row.kind), int(row.channel_id), int(row.message_id), str(row.state)) for row in live
            ),
            scheme_drift=(),
            special_ever_bound=True,
            special_role_ids=special_ids,
            known_object_ids=known,
            known_role_ids=frozenset(int(role.id) for role in guild.roles),
        )
        assert audit.orphaned_special is False
        raw = await BlueprintService(bot).export(guild, with_docs=True)
        _, issues = parse_blueprint(json.loads(raw.decode("utf-8")))
        errors = [(item.path, item.message) for item in issues if item.level is IssueLevel.ERROR]
        assert errors == []
    finally:
        await engine.dispose()


async def test_live_server_adopts_without_renaming(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99)
    bot = _flow_bot(sessions, guild)
    await guild.create_category("Общение")
    await guild.create_category("Архив-свой")
    await guild.create_text_channel("правила-сервера")
    await guild.create_text_channel("общий")
    await guild.create_text_channel("новости")
    await guild.create_text_channel("флуд")
    await guild.create_text_channel("мемы")
    await guild.create_text_channel("ссылки")
    await guild.create_text_channel("голосования-свои")
    await guild.create_voice_channel("музыка")
    await guild.create_voice_channel("ожидание")
    await guild.create_role("модератор")
    await guild.create_role("вип")
    originals = _existing_objects(guild)
    names = {int(item.id): str(item.name) for item in originals}
    renamed = _spy_name_edits(originals)
    try:
        rows = await bot.setup_map.build(guild)
        bot.setup_map.stash(int(guild.id), rows)
        with _blocked():
            report = await bot.setup_map.apply(guild, actor_id=7)
        assert report.verdict in {SeedVerdict.OK, SeedVerdict.PARTIAL, SeedVerdict.HELD}
        for item in originals:
            assert str(item.name) == names[int(item.id)]
        assert renamed == []
    finally:
        await engine.dispose()


async def test_break_and_repair_cycle(tmp_path: Path) -> None:
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _flow_bot(sessions, guild)
    try:
        with _blocked():
            report = await bot.seed.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.verdict is SeedVerdict.OK
        await _align_roles(guild, bot.seed)
        await bot.seed.reapply_rights(guild, actor_id=7)
        await _plant_live_showcases(bot, guild)
        await _assert_features_ok(bot, guild)
        automod = next(item for item in guild.text_channels if item.name == LAYOUT.automod)
        evidence = next(item for item in guild.categories if item.name == LAYOUT.cat_evidence)
        watcher = next(item for item in guild.roles if item.name == LAYOUT.role_watcher)
        children = [item for item in guild.text_channels if getattr(item, "category", None) is evidence]
        guild.members.append(_FakeMember(8, roles=[watcher]))
        guild.text_channels.remove(automod)
        for child in children:
            guild.text_channels.remove(child)
        guild.categories.remove(evidence)
        guild.roles.remove(watcher)
        losses = await bot.structure.sweep(guild)
        assert {loss.key for loss in losses} >= {"automod", "cat_evidence", "role_watcher"}
        assert bot.log_pump.items
        notified = len(bot.log_pump.items)
        with _blocked():
            for key in ("automod", "cat_evidence", "role_watcher", "evidence_archive"):
                repaired = await bot.structure.restore(guild, key, actor_id=7)
                assert repaired.verdict in {SeedVerdict.OK, SeedVerdict.PARTIAL}
        await _align_roles(guild, bot.seed)
        await bot.seed.reapply_rights(guild, actor_id=7)
        await _plant_live_showcases(bot, guild)
        await _assert_features_ok(bot, guild)
        bot.structure = StructureWatch(bot)
        await bot.structure.sweep(guild)
        assert len(bot.log_pump.items) == notified
    finally:
        await engine.dispose()


async def test_recreated_gate_role_reaches_existing_channels(tmp_path: Path) -> None:
    """Пересозданная «Верифицирован» обязана попасть в overwrites уже стоящих каналов без ручной пересборки."""
    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _flow_bot(sessions, guild)
    try:
        with _blocked():
            report = await bot.seed.run(guild, actor_id=7, mode=SeedMode.BUILD)
        assert report.verdict is SeedVerdict.OK
        await _align_roles(guild, bot.seed)
        await bot.seed.reapply_rights(guild, actor_id=7)
        verified = next(item for item in guild.roles if item.name == LAYOUT.role_verified)
        guild.roles.remove(verified)
        await bot.structure.sweep(guild)
        with _blocked():
            repaired = await bot.structure.restore(guild, "role_verified", actor_id=7)
        assert repaired.verdict in {SeedVerdict.OK, SeedVerdict.PARTIAL}
        existing = await bot.seed.snapshot(guild, with_old=True)
        broken = sorted(
            key
            for key, item in existing.items()
            if item.exists and not item.overwrites_ok and not key.startswith("role_")
        )
        assert broken == []
    finally:
        await engine.dispose()


async def test_seed_does_not_repost_live_showcases(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from conductor.domain.showcase import SHOWCASES, ShowcaseState

    engine = create_engine(_url(tmp_path))
    await apply_migrations(engine)
    sessions = create_sessionmaker(engine)
    guild = _FakeGuild(99, owner_id=21)
    bot = _flow_bot(sessions, guild)
    try:
        states = {spec.id: ShowcaseState.LIVE for spec in SHOWCASES}
        states[SHOWCASES[0].id] = ShowcaseState.STALE
        post = AsyncMock(return_value=SimpleNamespace(ok=True))
        bot.showcase = SimpleNamespace(statuses=AsyncMock(return_value=states), post=post)
        objects = {spec.channel_key: SimpleNamespace(id=1000 + index) for index, spec in enumerate(SHOWCASES)}
        notes: list[str] = []
        await bot.seed._panels(guild, objects, actor_id=7, notes=notes, only_keys=None)
        posted = [call.args[1].id for call in post.await_args_list]
        assert posted == [SHOWCASES[0].id]
        assert notes == []
    finally:
        await engine.dispose()
